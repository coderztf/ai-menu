from dataclasses import asdict
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from ai_menu.inventory_service import InventoryError, InventoryService, StockPatch


class InventoryServiceTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "db.sqlite3"
        conn = sqlite3.connect(database)
        try:
            schema = Path(__file__).resolve().parents[1] / "outputs" / "001_init.sql"
            conn.executescript(schema.read_text())
            conn.executescript((schema.parents[1] / "migrations" / "0002_archive_ingredient.sql").read_text())
            self.ids = dict(conn.execute("SELECT code,id FROM ingredient"))
        finally:
            conn.close()
        self.parser = Mock()
        self.service = InventoryService(database, self.parser)

    def reply(self, *rows, unresolved=()):
        self.parser.return_value = {"items": [
            dict(name=name, state=state, note=note, prioritize=prioritize)
            for name, state, note, prioritize in rows
        ], "unresolved": list(unresolved)}

    def counts(self):
        with self.service.transaction() as conn:
            return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("inventory", "inventory_event", "write_request"))

    def test_preview_alias_unknown_and_confirmation(self):
        self.reply(("鸡肉", "enough", "一包", False), ("西红柿", "low", "2个", True),
                   ("牛肉", "empty", None, False), unresolved=("香菜",))
        preview = self.service.parse("买了一包鸡肉，还剩2个西红柿优先吃，没有牛肉，香菜")
        self.assertEqual(preview["unresolved"][0]["text"], "香菜")
        self.assertEqual(self.counts(), (0, 0, 0))
        patches = [StockPatch(**patch) for patch in preview["items"]]
        self.assertEqual(patches, [
            StockPatch(self.ids["chicken"], "enough", "一包"),
            StockPatch(self.ids["tomato"], "low", "2个", True),
            StockPatch(self.ids["beef"], "empty"),
        ])
        response = self.service.confirm(preview["stock_version"], patches, uuid4())
        self.assertEqual(response["stock_version"], 1)
        self.assertEqual(self.counts(), (3, 3, 1))
        self.assertEqual(response, self.service.get_inventory())

    def test_duplicate_aliases_require_resolution(self):
        self.reply(("番茄", "enough", "2个", False), ("西红柿", "empty", None, False))
        preview = self.service.parse("番茄2个，西红柿没了")
        self.assertEqual(preview["items"], [])
        self.assertEqual(len(preview["unresolved"]), 2)

    def test_restock_creates_new_ingredient_on_confirmation(self):
        text = "今天又买了一些平菇、还有不少虾仁"
        self.reply(("平菇", "low", None, False), ("虾仁", "enough", None, False))
        preview = self.service.parse(text)
        self.assertEqual(self.parser.call_args.args[0], text)
        self.assertEqual(preview["unresolved"], [])
        self.assertEqual(self.counts(), (0, 0, 0))
        patches = [StockPatch(**item) for item in preview["items"]]
        self.assertEqual(patches, [
            StockPatch(None, "low", ingredient_name="平菇"),
            StockPatch(None, "enough", ingredient_name="虾仁"),
        ])
        inventory = self.service.confirm(preview["stock_version"], patches, uuid4())
        mushroom = next(item for item in inventory["items"] if item["ingredient"]["name"] == "平菇")
        self.reply(("平菇", "low", "1包", False))
        preview = self.service.parse("还剩1包平菇")
        self.assertEqual(preview["items"], [
            asdict(StockPatch(mushroom["ingredient"]["id"], "low", "1包")),
        ])

    def test_invalid_model_response_leaves_inventory_unchanged(self):
        self.reply(("平菇", "unknown", None, False))
        with self.assertRaisesRegex(InventoryError, "INVENTORY_AI_UNAVAILABLE"):
            self.service.parse("有平菇")
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_replay_and_no_op(self):
        key = uuid4()
        patch = StockPatch(self.ids["tomato"], "low", "还剩两个", True)
        original = self.service.confirm(0, [patch], key)
        self.assertEqual(self.service.confirm(0, [patch], key), original)
        with self.assertRaisesRegex(InventoryError, "IDEMPOTENCY_KEY_REUSED"):
            self.service.confirm(1, [patch], key)
        self.assertEqual(self.service.confirm(1, [patch], uuid4()), original)
        self.assertEqual(self.counts(), (1, 1, 2))
        empty = StockPatch(self.ids["tomato"], "empty")
        self.service.confirm(1, [empty], uuid4())
        tomato = next(item for item in self.service.get_inventory()["items"]
                      if item["ingredient"]["code"] == "tomato")
        self.assertEqual((tomato["state"], tomato["note"], tomato["prioritize"]),
                         ("empty", None, False))
        self.assertEqual(self.service.confirm(0, [patch], key), original)

    def test_invalid_batch_keeps_database_unchanged(self):
        with self.assertRaisesRegex(InventoryError, "INVALID_INPUT"):
            self.service.confirm(0, [
                StockPatch(self.ids["chicken"], "enough"),
                StockPatch(self.ids["pork"], "low"),
            ], uuid4())
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_concurrent_updates_on_same_version(self):
        def submit(code):
            try:
                self.service.confirm(0, [StockPatch(self.ids[code], "low")], uuid4())
                return "ok"
            except InventoryError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, ("chicken", "tomato")))
        self.assertCountEqual(results, ["ok", "STOCK_VERSION_CONFLICT"])
        self.assertEqual(self.counts(), (1, 1, 1))
