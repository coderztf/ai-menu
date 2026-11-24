"""Load the initial three-person recipe catalog into an initialized database."""

import argparse
from pathlib import Path

from .paths import DEFAULT_DATABASE
from .inventory_service import InventoryService, encode

# Each ingredient tuple is (dictionary code, three-person amount, accepts low stock).
RECIPES = (
    ("garlic-pork", "蒜香肉片", 15,
     ("猪肉切薄片，大蒜切末。", "热锅下油炒熟肉片，加入蒜末和少许盐炒匀。"),
     (("pork", "250克", False), ("garlic", "3瓣", False), ("salt", "少许", False))),
    ("steamed-egg", "蒸水蛋", 15,
     ("鸡蛋加盐打散，加温水搅匀。", "盖好后上锅蒸至蛋液凝固。"),
     (("egg", "3个", False), ("salt", "少许", False))),
    ("tomato-egg", "番茄炒蛋", 12,
     ("番茄切块，鸡蛋打散炒熟盛出。", "炒软番茄，倒回鸡蛋，加盐炒匀。"),
     (("tomato", "1到2个", True), ("egg", "3个", False), ("salt", "少许", False))),
    ("garlic-lettuce", "蒜蓉生菜", 10,
     ("生菜洗净沥水，大蒜切末。", "蒜末炒香后下生菜，炒至断生，加盐。"),
     (("lettuce", "1棵", False), ("garlic", "3瓣", False), ("salt", "少许", False))),
    ("garlic-potato", "蒜香土豆丝", 15,
     ("土豆切丝，冲去表面淀粉后沥水。", "炒香蒜末，下土豆丝炒熟并调味。"),
     (("potato", "2个", False), ("garlic", "2瓣", False), ("salt", "少许", False))),
    ("garlic-tomato", "蒜香番茄", 10,
     ("番茄切块，大蒜切碎。", "用蒜末炝锅，放番茄翻炒，加少许盐。"),
     (("tomato", "1到2个", True), ("garlic", "2瓣", False), ("salt", "少许", False))),
    ("potato-chicken", "土豆炖鸡块", 35,
     ("鸡肉和土豆切块，大蒜拍碎。", "炒香鸡块，加土豆和水，炖至鸡肉熟透、土豆软糯，加盐调味。"),
     (("chicken", "400克", False), ("potato", "2个", False), ("garlic", "2瓣", False), ("salt", "少许", False))),
    ("tomato-beef", "番茄炒牛肉", 20,
     ("牛肉逆纹切薄片，番茄切块。", "炒软番茄后翻炒牛肉至熟，加少许盐。"),
     (("beef", "250克", False), ("tomato", "1到2个", True), ("salt", "少许", False))),
    ("garlic-lamb", "蒜香羊肉", 15,
     ("羊肉切薄片，大蒜切末。", "热锅翻炒羊肉至熟，加入蒜末和盐炒匀。"),
     (("lamb", "250克", False), ("garlic", "3瓣", False), ("salt", "少许", False))),
)


def seed(database):
    with InventoryService(database).transaction(write=True) as conn:
        ids = dict(conn.execute("SELECT code,id FROM ingredient"))
        for code, name, minutes, steps, ingredients in RECIPES:
            row = conn.execute("""
                INSERT INTO recipe(code,name,kind,minutes,instructions) VALUES (?,?,'dish',?,?)
                ON CONFLICT(code) DO NOTHING RETURNING id
            """, (code, name, minutes, encode(steps))).fetchone()
            if row is not None:
                conn.executemany("""
                    INSERT INTO recipe_ingredient(recipe_id,ingredient_id,amount_hint,accepts_low)
                    VALUES (?,?,?,?)
                """, ((row[0], ids[item], amount, low) for item, amount, low in ingredients))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path, default=DEFAULT_DATABASE)
    seed(parser.parse_args().database)
