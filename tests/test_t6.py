from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from ai_menu.api import create_app
from ai_menu.inventory_service import StockPatch
from ai_menu.menu_service import MenuService, SHANGHAI
from ai_menu.scheduler import generate_if_due
from ai_menu.seed_recipes import seed


class LocalH5Test(unittest.TestCase):
    def test_home_and_assets(self):
        with TestClient(create_app(":memory:")) as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("今天吃什么", home.text)
            for path in ("/h5/app.js", "/h5/styles.css"):
                self.assertEqual(client.get(path).status_code, 200)

    def test_schedule_boundary_replay_and_cooked_menu(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "db.sqlite3"
            before = datetime(2026, 11, 16, 5, 59, tzinfo=SHANGHAI)
            due = before.replace(hour=6, minute=0)
            service = MenuService(database, before.date)
            conn = sqlite3.connect(database)
            try:
                schema = Path(__file__).resolve().parents[1] / "outputs" / "001_init.sql"
                conn.executescript(schema.read_text())
                conn.executescript((schema.parents[1] / "migrations" / "0002_archive_ingredient.sql").read_text())
                ids = dict(conn.execute("SELECT code,id FROM ingredient"))
            finally:
                conn.close()
            seed(database)

            service.confirm(0, [StockPatch(ids["lettuce"], "enough")], uuid4())
            self.assertIsNone(generate_if_due(service, before))
            self.assertEqual(service.get_today()["status"], "not_generated")
            menu = generate_if_due(service, due)
            self.assertEqual(menu["status"], "ready")
            self.assertEqual(generate_if_due(service, due.replace(hour=8)), menu)
            self.assertEqual(service.get_inventory()["stock_version"], 1)
            dish = next(dish for dish in menu["dishes"] if all(
                item["stock_policy"] != "tracked" for item in dish["ingredients"]
            ))
            service.write_menu("confirm-cooked", menu["menu_date"], uuid4(), {
                "revision": menu["revision"], "stock_version": 1,
                "cooked_slots": [dish["slot"]], "remaining": [],
            })
            generate_if_due(service, due.replace(hour=9))
            self.assertEqual(service.get_today()["status"], "cooked")
            self.assertEqual(len(service.history(None, 20)["items"]), 1)
