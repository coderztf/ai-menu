from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from ai_menu.api import create_app
from ai_menu.inventory_service import InventoryService
from ai_menu.seed_recipes import seed


class InventoryModelStub:
    def parse_inventory(self, text, known_names):
        common = [("番茄", "enough", None), ("生菜", "enough", None)]
        examples = {
            "番茄很多，生菜充足": common,
            "番茄很多，生菜充足，土豆充足，买了一包鸡肉":
                common + [("土豆", "enough", None), ("鸡肉", "enough", "一包")],
            "买了2根黄瓜，猪肉很多": [("黄瓜", "enough", "2根"), ("猪肉", "enough", None)],
            "还剩1根黄瓜": [("黄瓜", "low", "1根")],
        }
        return {"items": [
            dict(name=name, state=state, note=note, prioritize=False)
            for name, state, note in examples[text]
        ], "unresolved": []}


class MenuAPITest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "db.sqlite3"
        self.store = InventoryService(self.database)
        self.day = date(2026, 11, 16)
        with sqlite3.connect(self.database) as conn:
            schema = Path(__file__).resolve().parents[1] / "outputs" / "001_init.sql"
            conn.executescript(schema.read_text())
            conn.executescript((schema.parents[1] / "migrations" / "0002_archive_ingredient.sql").read_text())
        seed(self.database)
        seed(self.database)
        self.client = self.enterContext(TestClient(create_app(self.database, lambda: self.day,
                                                            recipe_generator=InventoryModelStub(), require_model=False)))
        self.base = "/api/v1"
        self.menu = self.base + "/menus/today"

    def post_menu(self, action, payload, key=None, day="2026-11-16"):
        return self.client.post(f"{self.menu}/{action}", json=payload, headers={
            "X-Menu-Date": day, "Idempotency-Key": key or str(uuid4()),
        })

    def stock(self, text):
        response = self.client.post(self.base + "/inventory/parse", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        parsed = response.json()
        self.assertEqual(parsed.pop("unresolved"), [])
        response = self.client.post(self.base + "/inventory/confirm", json=parsed,
                                    headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def event_count(self, table):
        with self.store.transaction() as conn:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_inventory_generate_replace_cook_and_replay(self):
        self.assertEqual(self.client.get(self.menu).json()["status"], "not_generated")
        first = self.post_menu("generate", {}).json()
        self.assertEqual(first["status"], "insufficient")
        self.assertTrue(first["diagnostics"]["unavailable_ingredient_ids"])
        stock = self.stock("番茄很多，生菜充足，土豆充足，买了一包鸡肉")
        count = self.event_count("inventory_event")
        self.assertTrue(self.client.get(self.menu).json()["stale"])
        with ThreadPoolExecutor(max_workers=2) as executor:
            generated = list(executor.map(lambda _: self.post_menu("generate", {}).json(), range(2)))
        self.assertEqual(generated[0], generated[1])
        menu = generated[0]
        self.assertEqual(menu["status"], "ready")
        self.assertEqual(len(menu["dishes"]), 3)
        queued = self.post_menu("replace", {
            "scope": "all", "revision": menu["revision"], "stock_version": stock["stock_version"],
        })
        self.assertEqual(queued.status_code, 202, queued.text)
        job = self.client.get(f"{self.menu}/replace/{queued.json()['job_id']}").json()
        self.assertEqual(job["status"], "succeeded")
        changed = job["menu"]
        self.assertNotEqual({d["recipe_id"] for d in menu["dishes"]},
                            {d["recipe_id"] for d in changed["dishes"]})
        self.assertEqual(self.client.get(self.base + "/inventory").json(), stock)
        self.assertEqual(self.event_count("inventory_event"), count)

        dish = next(d for d in changed["dishes"]
                    if any(i["stock_policy"] == "tracked" for i in d["ingredients"]))
        remaining = [{"ingredient_id": item["ingredient_id"], "outcome": "empty"}
                     for item in dish["ingredients"] if item["stock_policy"] == "tracked"]
        body = dict(revision=changed["revision"], stock_version=stock["stock_version"],
                    cooked_slots=[dish["slot"]], remaining=remaining)
        bad = self.post_menu("confirm-cooked", dict(body, remaining=[]))
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.client.get(self.menu).json(), changed)
        key = str(uuid4())
        response = self.post_menu("confirm-cooked", body, key)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["menu"]["status"], "cooked")
        self.assertEqual(result["stock_version"], stock["stock_version"] + 1)
        self.assertEqual(self.post_menu("confirm-cooked", body).status_code, 409)
        history = self.client.get(self.base + "/cooking-history").json()
        self.assertEqual(history["items"][0]["dishes"], [dish])
        self.assertEqual(self.client.get(self.base + "/cooking-history?before=2026-11-16").json()["items"], [])
        self.day = date(2026, 11, 17)
        self.assertEqual(self.post_menu("confirm-cooked", body, key).json(), result)
        wrong_day = self.post_menu("confirm-cooked", body).json()
        self.assertEqual(wrong_day["error"]["code"], "MENU_DATE_CHANGED")
        self.assertEqual(self.event_count("cooking_event"), 1)
        self.assertEqual(self.event_count("inventory_event"), count + len(remaining))

    def test_intake_creates_new_ingredient_only_on_confirmation(self):
        preview = self.client.post(self.base + "/inventory/parse", json={
            "text": "买了2根黄瓜，猪肉很多",
        }).json()
        self.assertEqual(preview["items"][0]["ingredient_name"], "黄瓜")
        self.assertEqual(preview["unresolved"][0]["reason"], "常备食材无需录入库存")
        self.assertEqual(self.client.get(self.base + "/ingredients?q=黄瓜").json()["items"], [])
        endpoint = self.base + "/inventory/confirm"
        body = {key: preview[key] for key in ("stock_version", "items")}
        headers = {"Idempotency-Key": str(uuid4())}
        response = self.client.post(endpoint, json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        item = next(item for item in response.json()["items"] if item["ingredient"]["name"] == "黄瓜")
        self.assertEqual(item["state"], "enough")
        self.assertEqual(self.client.post(endpoint, json=body, headers=headers).json(), response.json())
        self.assertEqual(self.event_count("inventory_event"), 1)
        duplicate = self.client.post(endpoint, json={
            "stock_version": 1,
            "items": [dict(ingredient_name="黄瓜", note=None, state="low", prioritize=False)],
        }, headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(len(self.client.get(self.base + "/ingredients?q=黄瓜").json()["items"]), 1)
        preview = self.client.post(self.base + "/inventory/parse", json={"text": "还剩1根黄瓜"}).json()
        self.assertEqual(preview["items"][0]["ingredient_id"], item["ingredient"]["id"])

    def test_delete_and_restore_ingredient(self):
        stock = self.stock("番茄很多，生菜充足")
        item = next(item for item in stock["items"] if item["ingredient"]["code"] == "tomato")
        ingredient_id = item["ingredient"]["id"]
        path = self.base + f"/ingredients/{ingredient_id}/delete"
        key = {"Idempotency-Key": str(uuid4())}
        body = {"stock_version": stock["stock_version"]}
        result = self.client.post(path, json=body, headers=key)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn(ingredient_id, [i["ingredient"]["id"] for i in result.json()["items"]])
        self.assertEqual(self.client.post(path, json=body, headers=key).json(), result.json())
        self.assertEqual(self.client.get(self.base + "/ingredients?q=番茄").json()["items"], [])
        self.assertEqual(self.client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())}).status_code, 409)
        restored = self.stock("番茄很多，生菜充足")
        self.assertIn(ingredient_id, [i["ingredient"]["id"] for i in restored["items"]])
        pork = self.client.get(self.base + "/ingredients?q=猪肉").json()["items"][0]["id"]
        response = self.client.post(self.base + f"/ingredients/{pork}/delete",
                                    json={"stock_version": restored["stock_version"]},
                                    headers={"Idempotency-Key": str(uuid4())})
        self.assertEqual(response.status_code, 422)

    def test_concurrent_intake_and_cooking(self):
        stock = self.stock("番茄很多，生菜充足")
        menu = self.post_menu("generate", {}).json()
        self.assertEqual(menu["status"], "ready")
        dish = next(d for d in menu["dishes"] if any(
            i["stock_policy"] == "tracked" for i in d["ingredients"]))
        tracked = [i["ingredient_id"] for i in dish["ingredients"] if i["stock_policy"] == "tracked"]
        confirmation = dict(revision=menu["revision"], stock_version=stock["stock_version"],
                            cooked_slots=[dish["slot"]],
                            remaining=[dict(ingredient_id=i, outcome="empty") for i in tracked])
        with ThreadPoolExecutor(max_workers=2) as executor:
            cooking = executor.submit(self.post_menu, "confirm-cooked", confirmation)
            intake = executor.submit(self.client.post, self.base + "/inventory/confirm",
                                     json={"stock_version": stock["stock_version"], "items": [
                                         dict(ingredient_id=tracked[0], state="low", note=None, prioritize=False)
                                     ]}, headers={"Idempotency-Key": str(uuid4())})
        self.assertCountEqual([cooking.result().status_code, intake.result().status_code], [200, 409])
        self.assertEqual(self.client.get(self.base + "/inventory").json()["stock_version"],
                         stock["stock_version"] + 1)

    def test_failed_event_insert_rolls_back_confirmation(self):
        stock = self.stock("番茄很多，生菜充足")
        menu = self.post_menu("generate", {}).json()
        dish = next(d for d in menu["dishes"] if any(
            i["stock_policy"] == "tracked" for i in d["ingredients"]))
        tracked = [i["ingredient_id"] for i in dish["ingredients"] if i["stock_policy"] == "tracked"]
        with self.store.transaction(write=True) as conn:
            conn.execute("""
                CREATE TRIGGER fail_cooking BEFORE INSERT ON cooking_event
                BEGIN SELECT RAISE(ABORT,'test failure'); END
            """)
        key = str(uuid4())
        body = dict(revision=menu["revision"], stock_version=stock["stock_version"],
                    cooked_slots=[dish["slot"]],
                    remaining=[dict(ingredient_id=i, outcome="empty") for i in tracked])
        with self.assertRaises(sqlite3.IntegrityError):
            self.post_menu("confirm-cooked", body, key)
        self.assertEqual(self.client.get(self.menu).json(), menu)
        self.assertEqual(self.client.get(self.base + "/inventory").json(), stock)
        self.assertEqual(self.event_count("cooking_event"), 0)
        with self.store.transaction(write=True) as conn:
            conn.execute("DROP TRIGGER fail_cooking")
        self.assertEqual(self.post_menu("confirm-cooked", body, key).status_code, 200)
