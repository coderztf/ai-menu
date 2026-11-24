"""Transactional daily menus and cooking history."""

from collections import Counter
from itertools import combinations
from datetime import date, datetime, timedelta
from hashlib import sha256
import logging
import json
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .inventory_service import InventoryError, InventoryService, encode
from .recipe_ai import RecipeAIError, validate_candidates
from .recommender import Recipe, Requirement, Stock, recommend

SHANGHAI = ZoneInfo("Asia/Shanghai")


def today():
    return datetime.now(SHANGHAI).date()


class MenuService(InventoryService):
    def __init__(self, database, clock=today, generator=None, *, require_model=False):
        super().__init__(database, getattr(generator, "parse_inventory", None))
        self.require_model = require_model
        self.clock = clock
        self.generator = generator

    @staticmethod
    def _dishes(conn, day):
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT snapshot FROM daily_menu_item WHERE menu_date=? ORDER BY slot", (day,)
        )]

    def _menu(self, conn, day):
        version = self._version(conn)
        row = conn.execute("SELECT * FROM daily_menu WHERE menu_date=?", (day,)).fetchone()
        if row is None:
            return dict(menu_date=day, status="not_generated", revision=None,
                        stock_version=None, current_stock_version=version,
                        stale=False, generated_at=None, dishes=[], diagnostics=None)
        menu = dict(row)
        menu.update(current_stock_version=version, dishes=self._dishes(conn, day),
                    stale=row["status"] != "cooked" and row["stock_version"] != version,
                    diagnostics=json.loads(row["diagnostics"]) if row["diagnostics"] else None)
        return menu

    def get_today(self):
        with self.transaction() as conn:
            return self._menu(conn, self.clock().isoformat())

    @staticmethod
    def _catalog(conn, selected_ids=None):
        recipes = {}
        if selected_ids is None:
            predicate, arguments = "active=1", ()
        else:
            arguments = tuple(selected_ids)
            predicate = "id IN (" + ",".join("?" for _ in arguments) + ")" if arguments else "0"
        for row in conn.execute(f"SELECT * FROM recipe WHERE {predicate} ORDER BY id", arguments):
            recipes[row["id"]] = (row, [], [])
        for row in conn.execute("""
            SELECT ri.*, i.code, i.name, i.stock_policy, i.food_group
            FROM recipe_ingredient ri JOIN ingredient i ON i.id=ri.ingredient_id
            JOIN recipe r ON r.id=ri.recipe_id
            ORDER BY ri.recipe_id, ri.ingredient_id
        """):
            if row["recipe_id"] not in recipes:
                continue
            _, requirements, ingredients = recipes[row["recipe_id"]]
            tracked = row["stock_policy"] == "tracked"
            requirements.append(Requirement(row["code"], row["food_group"], tracked, bool(row["accepts_low"])))
            ingredients.append({key: row[key] for key in
                                ("ingredient_id", "name", "stock_policy", "amount_hint")})

        catalog = []
        snapshots = {}
        for recipe_id, (row, requirements, ingredients) in recipes.items():
            if not requirements:
                continue
            catalog.append(Recipe(recipe_id, row["name"], row["minutes"], tuple(requirements), row["kind"]))
            snapshots[recipe_id] = dict(recipe_id=recipe_id, name=row["name"], minutes=row["minutes"],
                                        instructions=json.loads(row["instructions"]), ingredients=ingredients)
        return catalog, snapshots

    def _recommend(self, conn, day, menu, action, payload, generated=None):
        generated_ids = []
        allowed = None
        if generated is not None:
            generated_ids = self._save_candidates(conn, generated)
            allowed = generated_ids + [dish["recipe_id"] for dish in menu["dishes"] if action == "replace"]
        catalog, snapshots = self._catalog(conn, allowed)
        stock = {item["ingredient"]["code"]: Stock(item["state"], item["prioritize"])
                 for item in self._snapshot(conn)["items"] if item["state"] is not None}
        since = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
        recent_rows = conn.execute("""
            SELECT mi.recipe_id, json_extract(mi.snapshot, '$.name') FROM cooking_event ce
            JOIN daily_menu_item mi ON mi.menu_date=ce.menu_date
            JOIN json_each(ce.cooked_slots) slots ON slots.value=mi.slot
            WHERE ce.menu_date>=? AND ce.menu_date<?
        """, (since, day)).fetchall()
        recent = {row[0] for row in recent_rows}
        if generated is not None:
            recent_names = {row[1] for row in recent_rows}
            recent.update(recipe.id for recipe in catalog if recipe.name in recent_names)
        current = None
        if action == "replace":
            by_id = {recipe.id: recipe for recipe in catalog}
            current = tuple(by_id.get(dish["recipe_id"]) or Recipe(
                dish["recipe_id"], dish["name"], dish["minutes"], (), active=False
            ) for dish in menu["dishes"])
        selection = recommend(catalog, stock, recent, current=current, replace_slot=payload.get("slot"))
        if selection is None and action == "replace":
            raise InventoryError("NO_ALTERNATIVE")
        diagnostics = None
        if selection is None:
            diagnostics = encode(self._diagnostics(catalog, snapshots, stock))
        conn.execute("""
            INSERT INTO daily_menu(menu_date, stock_version, status, diagnostics) VALUES (?,?,?,?)
            ON CONFLICT(menu_date) DO UPDATE SET revision=revision+1,
                stock_version=excluded.stock_version, status=excluded.status,
                diagnostics=excluded.diagnostics, generated_at=excluded.generated_at
        """, (day, self._version(conn), "ready" if selection else "insufficient", diagnostics))
        conn.execute("DELETE FROM daily_menu_item WHERE menu_date=?", (day,))
        if selection:
            conn.executemany("""
                INSERT INTO daily_menu_item(menu_date,slot,recipe_id,snapshot) VALUES (?,?,?,?)
            """, ((day, slot, recipe.id, encode(dict(snapshots[recipe.id], slot=slot)))
                  for slot, recipe in enumerate(selection, 1)))
        selected_ids = {recipe.id for recipe in selection or ()}
        unused = [(recipe_id,) for recipe_id in generated_ids if recipe_id not in selected_ids]
        conn.executemany("DELETE FROM recipe_ingredient WHERE recipe_id=?", unused)
        conn.executemany("DELETE FROM recipe WHERE id=?", unused)
        return self._menu(conn, day)

    @staticmethod
    def _save_candidates(conn, candidates):
        ingredient_ids = dict(conn.execute("SELECT code,id FROM ingredient"))
        recipe_ids = []
        for name, minutes, steps, uses in candidates:
            recipe_id = conn.execute("""
                INSERT INTO recipe(code,name,kind,minutes,instructions,active)
                VALUES (?,?,'dish',?,?,0) RETURNING id
            """, ("ai_" + uuid4().hex, name, minutes, encode(steps))).fetchone()[0]
            conn.executemany("""
                INSERT INTO recipe_ingredient(recipe_id,ingredient_id,amount_hint)
                VALUES (?,?,?)
            """, ((recipe_id, ingredient_ids[code], amount) for code, amount in uses.items()))
            recipe_ids.append(recipe_id)
        return recipe_ids

    @staticmethod
    def _diagnostics(catalog, snapshots, stock):
        dishes = []
        for recipe in catalog:
            if recipe.kind != "dish":
                continue
            groups = {item.group for item in recipe.ingredients}
            affected = set()
            low = set()
            for item, detail in zip(recipe.ingredients, snapshots[recipe.id]["ingredients"], strict=True):
                if not item.tracked:
                    continue
                state = stock.get(item.code, Stock("empty")).state
                if state == "empty" or (state == "low" and not item.accepts_low):
                    affected.add(detail["ingredient_id"])
                if state == "low":
                    low.add(item.code)
            dishes.append((recipe.id, "meat" in groups, groups == {"plant"}, affected, low))

        best = None
        unavailable = []
        for combo in combinations(dishes, 3):
            if not any(dish[1] for dish in combo) or not any(dish[2] for dish in combo):
                continue
            affected = set().union(*(dish[3] for dish in combo))
            conflicts = sum(count > 1 for count in Counter(
                code for dish in combo for code in dish[4]
            ).values())
            key = (len(affected), conflicts, tuple(dish[0] for dish in combo))
            if best is None or key < best:
                best, unavailable = key, sorted(affected)
        return {"reason": "no_feasible_menu", "unavailable_ingredient_ids": unavailable}

    def _cook(self, conn, menu, payload):
        day = menu["menu_date"]
        slots = sorted(payload["cooked_slots"])
        remaining = {row["ingredient_id"]: row for row in payload["remaining"]}
        involved = {item["ingredient_id"] for dish in menu["dishes"] if dish["slot"] in slots
                    for item in dish["ingredients"] if item["stock_policy"] == "tracked"}
        if set(remaining) != involved or len(remaining) != len(payload["remaining"]):
            raise InventoryError("INVALID_INPUT", field="remaining", required_ids=sorted(involved))

        changes = []
        for ingredient_id, row in remaining.items():
            if row["outcome"] == "unchanged":
                continue
            previous = conn.execute(
                "SELECT state,note,prioritize FROM inventory WHERE ingredient_id=?", (ingredient_id,)
            ).fetchone()
            before = (dict(previous, prioritize=bool(previous["prioritize"])) if previous else None)
            low = row["outcome"] == "low"
            after = dict(state=row["outcome"], note=row.get("note") if low else None,
                         prioritize=bool(low and before and before["prioritize"]))
            if before == after:
                continue
            conn.execute("""
                INSERT INTO inventory(ingredient_id,state,note,prioritize) VALUES (?,?,?,?)
                ON CONFLICT(ingredient_id) DO UPDATE SET state=excluded.state, note=excluded.note,
                    prioritize=excluded.prioritize, updated_at=excluded.updated_at
            """, (ingredient_id, after["state"], after["note"], after["prioritize"]))
            changes.append((ingredient_id, before, after))

        if changes:
            conn.execute("UPDATE household_state SET stock_version=stock_version+1 WHERE id=1")
        conn.execute("UPDATE daily_menu SET status='cooked',revision=revision+1 WHERE menu_date=?", (day,))
        confirmed = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
        cooking = dict(menu_date=day, menu_revision=menu["revision"], cooked_slots=slots,
                       confirmed_at=confirmed)
        return dict(menu=self._menu(conn, day), cooking=cooking, stock_version=self._version(conn)), changes

    def _prepare_write(self, conn, action, day, request_key, payload, operation, digest):
        saved = conn.execute("SELECT * FROM write_request WHERE request_key=?", (request_key,)).fetchone()
        if saved:
            if saved["operation"] != operation or saved["payload_hash"] != digest:
                raise InventoryError("IDEMPOTENCY_KEY_REUSED")
            return None, json.loads(saved["response_json"])
        if day != self.clock().isoformat():
            raise InventoryError("MENU_DATE_CHANGED", menu_date=self.clock().isoformat())
        menu = self._menu(conn, day)
        if action != "generate":
            if menu["status"] == "cooked":
                raise InventoryError("MENU_ALREADY_COOKED")
            if menu["status"] != "ready":
                raise InventoryError("MENU_NOT_READY")
            if payload["revision"] != menu["revision"]:
                raise InventoryError("MENU_REVISION_CONFLICT", revision=menu["revision"])
            if payload["stock_version"] != menu["current_stock_version"]:
                raise InventoryError("STOCK_VERSION_CONFLICT", stock_version=menu["current_stock_version"])

        return menu, None

    @staticmethod
    def _unchanged_generation(action, menu):
        return action == "generate" and (menu["status"] == "cooked" or (
            menu["status"] != "not_generated" and not menu["stale"]
        ))

    @staticmethod
    def _recipe_error(action, reason, upstream_status=None):
        logging.getLogger("uvicorn.error").warning(
            "AI recipe request failed: action=%s reason=%s upstream_status=%s",
            action, reason, upstream_status,
        )
        details = {"reason": reason}
        if upstream_status is not None:
            details["upstream_status"] = upstream_status
        return InventoryError("RECIPE_AI_UNAVAILABLE", **details)

    def write_menu(self, action, day, request_key, payload):
        operation = f"POST /api/v1/menus/today/{action}"
        request_key = str(UUID(str(request_key)))
        digest = sha256(encode(dict(day=day, payload=payload)).encode()).digest()
        args = (action, day, request_key, payload, operation, digest)
        generated = None
        basis_version = None
        if callable(getattr(self.generator, "generate", None)) and action in ("generate", "replace"):
            with self.transaction() as conn:
                menu, replay = self._prepare_write(conn, *args)
                if replay is not None:
                    return replay
                if not self._unchanged_generation(action, menu):
                    ingredients = [dict(row) for row in conn.execute("""
                        SELECT i.code,i.name,i.food_group,i.stock_policy,s.state
                        FROM ingredient i LEFT JOIN inventory s ON s.ingredient_id=i.id
                        WHERE i.stock_policy!='tracked' OR s.state='enough'
                        ORDER BY i.id
                    """)]
                    excluded = [dish["name"] for dish in menu["dishes"]] if action == "replace" else []
                    basis_version = menu["current_stock_version"]
            if basis_version is not None:
                try:
                    codes = {item["code"] for item in ingredients}
                    generated = validate_candidates(self.generator.generate(ingredients, excluded), codes, excluded)
                except RecipeAIError as error:
                    raise self._recipe_error(action, error.reason, error.upstream_status) from error

        with self.transaction(write=True) as conn:
            menu, replay = self._prepare_write(conn, *args)
            if replay is not None:
                return replay
            unchanged = self._unchanged_generation(action, menu)
            if generated is not None and not unchanged:
                if basis_version != menu["current_stock_version"]:
                    raise InventoryError("STOCK_VERSION_CONFLICT", stock_version=menu["current_stock_version"])

            changes = []
            if action == "confirm-cooked":
                result, changes = self._cook(conn, menu, payload)
            elif unchanged:
                result = menu
            else:
                if self.require_model and generated is None:
                    raise self._recipe_error(action, "not_configured")
                result = self._recommend(conn, day, menu, action, payload, generated)
            conn.execute("""
                INSERT INTO write_request(request_key,operation,payload_hash,http_status,response_json)
                VALUES (?,?,?,200,?)
            """, (request_key, operation, digest, encode(result)))
            if action == "confirm-cooked":
                event = result["cooking"]
                conn.execute("""
                    INSERT INTO cooking_event(menu_date,menu_revision,cooked_slots,request_key,confirmed_at)
                    VALUES (?,?,?,?,?)
                """, (day, event["menu_revision"], encode(event["cooked_slots"]), request_key, event["confirmed_at"]))
                conn.executemany("""
                    INSERT INTO inventory_event(request_key,ingredient_id,source,before_value,after_value)
                    VALUES (?,?,'cooking',?,?)
                """, ((request_key, ingredient_id, encode(before) if before is not None else None, encode(after))
                      for ingredient_id, before, after in changes))
            return result

    def history(self, before, limit):
        with self.transaction() as conn:
            rows = conn.execute("""
                SELECT menu_date,menu_revision,cooked_slots,confirmed_at FROM cooking_event
                WHERE (? IS NULL OR menu_date < ?) ORDER BY menu_date DESC LIMIT ?
            """, (before, before, limit + 1)).fetchall()
            entries = []
            for row in rows[:limit]:
                event = dict(row, cooked_slots=json.loads(row["cooked_slots"]))
                entries.append(dict(cooking=event, dishes=[
                    dish for dish in self._dishes(conn, row["menu_date"])
                    if dish["slot"] in event["cooked_slots"]
                ]))
            return dict(items=entries, next_before=rows[limit-1]["menu_date"] if len(rows) > limit else None)
