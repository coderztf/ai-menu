from contextlib import closing
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from ai_menu.api import create_app
from ai_menu.inventory_service import StockPatch
from ai_menu.menu_service import MenuService
from ai_menu.recipe_ai import ChatRecipeGenerator, RecipeAIError
from ai_menu.seed_recipes import seed


def candidate(name, code):
    return dict(name=name, kind="dish", minutes=10, instructions=["切好，烹调至熟。"],
                ingredients=[dict(code=code, amount_hint="三人份")])


class FakeGenerator:
    def __init__(self):
        self.calls = []
        self.fail = False

    def generate(self, ingredients, excluded):
        self.calls.append((ingredients, excluded))
        if self.fail:
            raise RecipeAIError("provider error")
        suffix = "二" if excluded else "一"
        return {"recipes": [
            candidate("葱香肉片" + suffix, "pork"),
            candidate("清蒸鸡蛋" + suffix, "egg"),
            candidate("清炒番茄" + suffix, "tomato"),
            candidate("羊肉小炒" + suffix, "lamb"),
            dict(candidate("鸡蛋汤", "egg"), kind="soup"),
        ]}


class RecipeAITest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "db.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            schema = Path(__file__).resolve().parents[1] / "outputs" / "001_init.sql"
            conn.executescript(schema.read_text())
            tomato = conn.execute("SELECT id FROM ingredient WHERE code='tomato'").fetchone()[0]
        seed(database)
        self.generator = FakeGenerator()
        clock = lambda: date(2026, 11, 16)
        self.store = MenuService(database, clock)
        self.store.confirm(0, [StockPatch(tomato, "enough")], uuid4())
        self.client = self.enterContext(TestClient(create_app(database, clock, recipe_generator=self.generator)))

    def post(self, action, body, key=None):
        return self.client.post(f"/api/v1/menus/today/{action}", json=body, headers={
            "Idempotency-Key": key or str(uuid4()), "X-Menu-Date": "2026-11-16",
        })

    def test_generate_and_replace_without_fixed_recipes(self):
        key = str(uuid4())
        first = self.post("generate", {}, key)
        self.assertEqual(first.status_code, 200, first.text)
        menu = first.json()
        self.assertEqual(menu["status"], "ready")
        self.assertTrue(all(dish["name"].endswith("一") for dish in menu["dishes"]))
        ingredients = self.generator.calls[0][0]
        self.assertNotIn("lamb", [item["code"] for item in ingredients])
        self.assertEqual(self.post("generate", {}, key).json(), menu)
        self.assertEqual(len(self.generator.calls), 1)
        replaced = self.post("replace", {
            "scope": "one", "slot": 1, "stock_version": 1, "revision": menu["revision"],
        })
        self.assertEqual(replaced.status_code, 202, replaced.text)
        job = self.client.get(f"/api/v1/menus/today/replace/{replaced.json()['job_id']}").json()
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["menu"]["dishes"][0]["name"].endswith("二"))
        self.assertEqual(job["menu"]["dishes"][1:], menu["dishes"][1:])
        self.assertEqual(self.store.get_inventory()["stock_version"], 1)

    def test_provider_error_keeps_menu_and_inventory(self):
        menu = self.post("generate", {}).json()
        self.generator.fail = True
        response = self.post("replace", {"scope": "all", "stock_version": 1,
                                         "revision": menu["revision"]})
        self.assertEqual(response.status_code, 202)
        job = self.client.get(f"/api/v1/menus/today/replace/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "RECIPE_AI_UNAVAILABLE")
        self.assertEqual(self.store.get_today(), menu)
        self.assertEqual(self.store.get_inventory()["stock_version"], 1)


class RecipeAITransportTest(unittest.TestCase):
    def test_chat_response(self):
        result = {"recipes": [candidate("番茄小炒", "tomato")]}
        response = {"choices": [{"message": {"content": json.dumps(result)}}]}
        generator = ChatRecipeGenerator("https://example.invalid/chat/completions", "test-model", "test-key")
        with patch("ai_menu.recipe_ai.urlopen", return_value=BytesIO(json.dumps(response).encode())) as transport:
            self.assertEqual(generator.generate([{"code": "tomato"}], []), result)
        request = transport.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "test-model")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")

class RecipeAIConfigTest(unittest.TestCase):
    def test_config_file_and_environment_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ai-config.yaml"
            path.write_text('ai:\n  url: "https://example.invalid/chat/completions"\n  model: "file-model"\n  api_key: "file-key"\n')
            with patch.dict("os.environ", {}, clear=True):
                generator = ChatRecipeGenerator.from_environment(path)
                self.assertEqual(generator.model, "file-model")
                self.assertEqual(generator.api_key, "file-key")
                with patch.dict("os.environ", {"MENU_AI_MODEL": "override"}):
                    self.assertEqual(ChatRecipeGenerator.from_environment(path).model, "override")

    def test_missing_empty_and_malformed_config(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            path = Path(directory) / "ai-config.yaml"
            self.assertIsNone(ChatRecipeGenerator.from_environment(path))
            path.write_text('ai:\n  url: ""\n  model: ""\n')
            self.assertIsNone(ChatRecipeGenerator.from_environment(path))
            path.write_text('ai: [\nsecret')
            with self.assertRaises(ValueError) as error:
                ChatRecipeGenerator.from_environment(path)
            self.assertNotIn("secret", str(error.exception))
