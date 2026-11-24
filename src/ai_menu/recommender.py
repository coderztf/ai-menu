"""Deterministic recommendation for three-person household meals."""

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Collection, Mapping


@dataclass(frozen=True)
class Requirement:
    code: str
    group: str = "plant"
    tracked: bool = True
    accepts_low: bool = False


@dataclass(frozen=True)
class Recipe:
    id: int
    name: str
    minutes: int
    ingredients: tuple[Requirement, ...]
    kind: str = "dish"
    active: bool = True


@dataclass(frozen=True)
class Stock:
    state: str
    prioritize: bool = False


Menu = tuple[Recipe, Recipe, Recipe]


def recommend(
    catalog: Collection[Recipe],
    stock: Mapping[str, Stock],
    recent_recipe_ids: Collection[int] = (),
    *,
    current: Menu | None = None,
    replace_slot: int | None = None,
) -> Menu | None:
    """Return a feasible menu, or None if a recommendation is unavailable.

    With current, exclude its recipe set. A one-based replace_slot fixes
    the other two slots. The caller keeps its current menu on failure.
    """
    if replace_slot is not None and (current is None or replace_slot not in (1, 2, 3)):
        raise ValueError("replace_slot requires a current menu and a slot from 1 to 3")

    available = {}
    for recipe in catalog:
        if not recipe.active or recipe.kind != "dish":
            continue
        tracked = tuple(item for item in recipe.ingredients if item.tracked)
        if any(
            item.code not in stock
            or stock[item.code].state == "empty"
            or (stock[item.code].state == "low" and not item.accepts_low)
            for item in tracked
        ):
            continue
        groups = {item.group for item in recipe.ingredients}
        available[recipe.id] = (
            recipe,
            {item.code for item in tracked},
            "meat" in groups,
            groups == {"plant"},
        )

    previous_ids = {recipe.id for recipe in current or ()}
    fixed_ids = {
        recipe.id for slot, recipe in enumerate(current or (), 1)
        if replace_slot is not None and slot != replace_slot
    }
    recent = set(recent_recipe_ids)
    best_key = None
    best_ids = None
    for ids in combinations(sorted(available), 3):
        ids_set = set(ids)
        if ids_set == previous_ids or not fixed_ids <= ids_set:
            continue
        rows = [available[recipe_id] for recipe_id in ids]
        if not any(row[2] for row in rows) or not any(row[3] for row in rows):
            continue
        usage = Counter(code for row in rows for code in row[1])
        if any(count > 1 and stock[code].state == "low" for code, count in usage.items()):
            continue

        score = sum(
            2 + 6 * stock[code].prioritize + 3 * (stock[code].state == "low")
            - 2 * (count - 1)
            for code, count in usage.items()
        )
        score -= 3 * len(ids_set & recent)
        score -= sum(row[0].minutes for row in rows) // 60
        key = (-score, ids)
        if best_key is None or key < best_key:
            best_key = key
            best_ids = ids

    if best_ids is None:
        return None
    if replace_slot is not None and current is not None:
        replacement = next(recipe_id for recipe_id in best_ids if recipe_id not in fixed_ids)
        return tuple(
            available[replacement if slot == replace_slot else recipe.id][0]
            for slot, recipe in enumerate(current, 1)
        )
    first, second, third = best_ids
    return available[first][0], available[second][0], available[third][0]
