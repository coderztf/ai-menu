import unittest

from ai_menu.recommender import Recipe, Requirement, Stock, recommend


PORK = Requirement("pork", "meat", tracked=False)
EGG = Requirement("egg", "egg", tracked=False)
GARLIC = Requirement("garlic", tracked=False)
TOMATO = Requirement("tomato", accepts_low=True)
POTATO = Requirement("potato")
LETTUCE = Requirement("lettuce", accepts_low=True)
CHICKEN = Requirement("chicken", "meat")
BEEF = Requirement("beef", "meat")
LAMB = Requirement("lamb", "meat")
LARD = Requirement("lard", "other_animal", tracked=False)

PORK_DISH = Recipe(1, "蒜香肉片", 15, (PORK, GARLIC))
EGG_DISH = Recipe(2, "蒸蛋", 15, (EGG,))
TOMATO_DISH = Recipe(3, "蒜拌番茄", 8, (TOMATO, GARLIC))
EGG_TOMATO = Recipe(4, "番茄炒蛋", 10, (EGG, TOMATO))
POTATO_DISH = Recipe(5, "蒜炒土豆丝", 12, (POTATO, GARLIC))
LETTUCE_DISH = Recipe(6, "蒜蓉生菜", 10, (LETTUCE, GARLIC))
CHICKEN_DISH = Recipe(7, "土豆炖鸡", 25, (CHICKEN, POTATO))
BEEF_DISH = Recipe(8, "番茄牛肉", 25, (BEEF, TOMATO))
LAMB_DISH = Recipe(9, "蒜香羊肉", 20, (LAMB, GARLIC))
LARD_DISH = Recipe(10, "猪油炒生菜", 10, (LARD, LETTUCE))
SOUP = Recipe(11, "番茄蛋汤", 10, (EGG, TOMATO), kind="soup")
STAPLE = Recipe(12, "蒸土豆", 10, (POTATO,), kind="staple")
CATALOG = (
    PORK_DISH, EGG_DISH, TOMATO_DISH, EGG_TOMATO, POTATO_DISH, LETTUCE_DISH,
    CHICKEN_DISH, BEEF_DISH, LAMB_DISH, LARD_DISH, SOUP, STAPLE,
)
BASE_MENU = (PORK_DISH, EGG_DISH, TOMATO_DISH)


class RecommendTest(unittest.TestCase):
    def test_menu_01_dish_and_balance(self):
        self.assertEqual(recommend(BASE_MENU + (SOUP, STAPLE), {
            "tomato": Stock("enough"), "potato": Stock("enough"),
        }), BASE_MENU)
        self.assertIsNone(recommend((PORK_DISH, EGG_DISH, LARD_DISH), {
            "lettuce": Stock("enough"),
        }))

    def test_menu_02_missing_and_empty_tracked(self):
        self.assertEqual(recommend(CATALOG, {
            "tomato": Stock("low"), "chicken": Stock("empty"),
        }), BASE_MENU)

    def test_menu_03_low_requires_opt_in_and_cannot_be_shared(self):
        catalog = BASE_MENU + (EGG_TOMATO, POTATO_DISH)
        self.assertEqual(recommend(catalog, {
            "tomato": Stock("low"), "potato": Stock("low"),
        }), BASE_MENU)
        self.assertIsNone(recommend((PORK_DISH, TOMATO_DISH, EGG_TOMATO), {
            "tomato": Stock("low"),
        }))
        self.assertIsNotNone(recommend((PORK_DISH, TOMATO_DISH, EGG_TOMATO), {
            "tomato": Stock("enough"),
        }))

    def test_menu_04_priority_history_ties_and_input_preservation(self):
        catalog = BASE_MENU + (LETTUCE_DISH,)
        stock = {"tomato": Stock("low", True), "lettuce": Stock("low")}
        before = stock.copy()
        self.assertEqual(recommend(catalog, stock), (PORK_DISH, TOMATO_DISH, LETTUCE_DISH))
        self.assertEqual(stock, before)
        stock = {"tomato": Stock("low"), "lettuce": Stock("enough")}
        self.assertEqual(recommend(catalog, stock, [LETTUCE_DISH.id]), BASE_MENU)
        self.assertEqual(recommend(tuple(reversed(catalog)), stock),
                         recommend(catalog, stock))

    def test_menu_05_one_replacement_keeps_positions(self):
        stock = {"tomato": Stock("low"), "lettuce": Stock("low")}
        current = (TOMATO_DISH, PORK_DISH, EGG_DISH)
        self.assertEqual(recommend(CATALOG, stock, current=current, replace_slot=1),
                         (LETTUCE_DISH, PORK_DISH, EGG_DISH))

    def test_menu_06_whole_replacement_changes_recipe_set(self):
        stock = {"tomato": Stock("low"), "lettuce": Stock("low")}
        result = recommend(CATALOG, stock, current=BASE_MENU)
        self.assertIsNotNone(result)
        self.assertNotEqual(set(result), set(BASE_MENU))

    def test_menu_07_no_menu_or_replacement(self):
        self.assertIsNone(recommend(CATALOG, {}))
        self.assertIsNone(recommend(BASE_MENU, {"tomato": Stock("low")},
                                    current=BASE_MENU, replace_slot=3))
