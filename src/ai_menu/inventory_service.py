"""SQLite inventory reads, natural-language preview and atomic confirmation."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import sqlite3
from time import sleep
import unicodedata
from uuid import UUID

from .recipe_ai import RecipeAIError


class InventoryError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class StockPatch:
    ingredient_id: int | None
    state: str
    note: str | None = None
    prioritize: bool = False
    ingredient_name: str | None = None


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class InventoryService:
    def __init__(self, database, inventory_parser=None):
        self.database = database
        self.inventory_parser = inventory_parser

    @contextmanager
    def transaction(self, *, write=False):
        conn = sqlite3.connect(self.database, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 1000")
            if write:
                for attempt in range(3):
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as error:
                        busy = getattr(error, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_BUSY
                        if not busy or attempt == 2:
                            raise
                        sleep(0.05 * (attempt + 1))
            else:
                conn.execute("BEGIN")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _version(conn):
        return conn.execute("SELECT stock_version FROM household_state WHERE id=1").fetchone()[0]

    @classmethod
    def _snapshot(cls, conn):
        rows = conn.execute("""
            SELECT i.id, i.code, i.name, i.stock_policy, i.food_group,
                   s.state, s.note, s.prioritize, s.updated_at
            FROM ingredient i LEFT JOIN inventory s ON s.ingredient_id=i.id
            WHERE i.stock_policy='tracked' AND NOT EXISTS (SELECT 1 FROM archived_ingredient a WHERE a.ingredient_id=i.id) ORDER BY i.id
        """)
        fields = ("id", "code", "name", "stock_policy", "food_group")
        items = [{
            "ingredient": {field: row[field] for field in fields},
            "state": row["state"],
            "note": row["note"],
            "prioritize": bool(row["prioritize"]),
            "updated_at": row["updated_at"],
        } for row in rows]
        return {"stock_version": cls._version(conn), "items": items}

    def get_inventory(self):
        with self.transaction() as conn:
            return self._snapshot(conn)

    def parse(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise InventoryError("INVALID_INPUT")
        with self.transaction() as conn:
            version = self._version(conn)
            aliases = dict(conn.execute("""
                SELECT name, id FROM ingredient
                UNION ALL SELECT alias, ingredient_id FROM ingredient_alias
            """))
            tracked = {row[0] for row in conn.execute(
                "SELECT id FROM ingredient WHERE stock_policy='tracked'"
            )}

        if self.inventory_parser is None:
            raise InventoryError("INVENTORY_AI_UNAVAILABLE", reason="not_configured")
        try:
            result = self.inventory_parser(text, sorted(aliases))
            if not isinstance(result, dict):
                raise RecipeAIError("Expected an inventory object")
            rows, unknown = result.get("items"), result.get("unresolved")
            if (not isinstance(rows, list) or len(rows) > 50 or not isinstance(unknown, list)
                    or len(unknown) > 50 or any(not isinstance(part, str) or not part.strip()
                                                or part not in text for part in unknown)):
                raise RecipeAIError("Invalid inventory response")
            matches = {}
            unresolved = [{"text": part, "reason": "需要选择食材或确认状态"} for part in unknown]
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise RecipeAIError("Invalid inventory item")
                name = unicodedata.normalize("NFKC", row["name"]).strip()
                ingredient_id = aliases.get(name)
                state, note, prioritize = row.get("state"), row.get("note"), row.get("prioritize")
                if ((ingredient_id is None and not self._valid_name(name))
                        or state not in ("enough", "low", "empty")
                        or type(prioritize) is not bool
                        or (note is not None and (not isinstance(note, str) or len(note) > 200))
                        or (state == "empty" and (note is not None or prioritize))):
                    raise RecipeAIError("Invalid inventory item")
                if ingredient_id is not None and ingredient_id not in tracked:
                    unresolved.append({"text": name, "reason": "常备食材无需录入库存"})
                    continue
                patch = StockPatch(ingredient_id, state, note, prioritize,
                                   name if ingredient_id is None else None)
                matches.setdefault(ingredient_id or name, []).append((name, patch))
        except RecipeAIError as error:
            details = {"reason": error.reason}
            if error.upstream_status is not None:
                details["upstream_status"] = error.upstream_status
            raise InventoryError("INVENTORY_AI_UNAVAILABLE", **details) from error

        items = []
        for entries in matches.values():
            if len(entries) == 1:
                items.append(asdict(entries[0][1]))
            else:
                unresolved.extend({"text": name, "reason": "同一食材出现多次"} for name, _ in entries)
        return {"stock_version": version, "items": items, "unresolved": unresolved}

    @staticmethod
    def _valid_name(name):
        return re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9 ·-]{0,39}", name) is not None

    @staticmethod
    def _resolve_or_create(conn, name):
        alias = conn.execute("SELECT ingredient_id FROM ingredient_alias WHERE alias=?", (name,)).fetchone()
        if alias is not None:
            return alias[0]
        existing = conn.execute("SELECT id FROM ingredient WHERE name=? ORDER BY id LIMIT 1", (name,)).fetchone()
        if existing is not None:
            return existing[0]
        code = "custom_" + sha256(name.encode()).hexdigest()[:56]
        row = conn.execute("""
            INSERT INTO ingredient (code,name,stock_policy,food_group)
            VALUES (?,?,'tracked','plant') RETURNING id
        """, (code, name)).fetchone()
        conn.execute("INSERT INTO ingredient_alias(alias,ingredient_id) VALUES (?,?)", (name, row[0]))
        return row[0]

    def confirm(self, stock_version, patches, request_key):
        try:
            request_key = str(UUID(str(request_key)))
        except ValueError as exc:
            raise InventoryError("INVALID_INPUT", field="Idempotency-Key") from exc
        patches = tuple(patches)
        if type(stock_version) is not int or stock_version < 0 or not 1 <= len(patches) <= 50:
            raise InventoryError("INVALID_INPUT")
        identities = set()
        normalized = []
        for item in patches:
            name = item.ingredient_name
            if name is not None:
                if not isinstance(name, str):
                    raise InventoryError("INVALID_INPUT", field="ingredient_name")
                name = unicodedata.normalize("NFKC", name).strip()
                if not self._valid_name(name):
                    raise InventoryError("INVALID_INPUT", field="ingredient_name")
            identity = item.ingredient_id if name is None else name
            valid_identity = (type(item.ingredient_id) is int and item.ingredient_id > 0
                              if name is None else item.ingredient_id is None)
            if (not valid_identity or identity in identities
                or item.state not in ("enough", "low", "empty")
                or type(item.prioritize) is not bool
                or (item.note is not None and (not isinstance(item.note, str) or len(item.note) > 200))
                or (item.state == "empty" and (item.note is not None or item.prioritize))):
                raise InventoryError("INVALID_INPUT", field="items")
            identities.add(identity)
            data = asdict(item)
            if name is None:
                data.pop("ingredient_name")
            else:
                data["ingredient_name"] = name
            normalized.append(data)
        normalized.sort(key=lambda item: (item["ingredient_id"] or 0, item.get("ingredient_name", "")))
        request = {"stock_version": stock_version, "items": normalized}
        digest = sha256(encode(request).encode()).digest()
        operation = "POST /api/v1/inventory/confirm"

        with self.transaction(write=True) as conn:
            saved = conn.execute("SELECT * FROM write_request WHERE request_key=?", (request_key,)).fetchone()
            if saved is not None:
                if saved["operation"] != operation or saved["payload_hash"] != digest:
                    raise InventoryError("IDEMPOTENCY_KEY_REUSED")
                return json.loads(saved["response_json"])
            version = self._version(conn)
            if version != stock_version:
                raise InventoryError("STOCK_VERSION_CONFLICT", stock_version=version)

            changes = []
            restored = False
            resolved = set()
            for item in request["items"]:
                ingredient_id = item.pop("ingredient_id")
                name = item.pop("ingredient_name", None)
                if name is not None:
                    ingredient_id = self._resolve_or_create(conn, name)
                if ingredient_id in resolved:
                    raise InventoryError("INVALID_INPUT", field="items", ingredient_id=ingredient_id)
                resolved.add(ingredient_id)
                existing = conn.execute("""
                    SELECT i.stock_policy, s.state, s.note, s.prioritize
                    FROM ingredient i LEFT JOIN inventory s ON s.ingredient_id=i.id
                    WHERE i.id=?
                """, (ingredient_id,)).fetchone()
                if existing is None or existing["stock_policy"] != "tracked":
                    raise InventoryError("INVALID_INPUT", ingredient_id=ingredient_id)
                restored = bool(conn.execute("DELETE FROM archived_ingredient WHERE ingredient_id=?", (ingredient_id,)).rowcount) or restored
                before = None if existing["state"] is None else {
                    "state": existing["state"], "note": existing["note"],
                    "prioritize": bool(existing["prioritize"]),
                }
                if item != before:
                    changes.append((ingredient_id, before, item))

            for ingredient_id, _, item in changes:
                conn.execute("""
                    INSERT INTO inventory (ingredient_id,state,note,prioritize) VALUES (?,?,?,?)
                    ON CONFLICT(ingredient_id) DO UPDATE SET state=excluded.state,
                        note=excluded.note, prioritize=excluded.prioritize,
                        updated_at=excluded.updated_at
                """, (ingredient_id, item["state"], item["note"], item["prioritize"]))
            if changes or restored:
                conn.execute("UPDATE household_state SET stock_version=stock_version+1 WHERE id=1")
            result = self._snapshot(conn)
            conn.execute("""
                INSERT INTO write_request (request_key,operation,payload_hash,http_status,response_json)
                VALUES (?,?,?,200,?)
            """, (request_key, operation, digest, encode(result)))
            conn.executemany("""
                INSERT INTO inventory_event (request_key,ingredient_id,source,before_value,after_value)
                VALUES (?,?,'intake',?,?)
            """, ((request_key, ingredient_id, None if before is None else encode(before), encode(after))
                  for ingredient_id, before, after in changes))
            return result

    def delete_ingredient(self, ingredient_id, stock_version, request_key):
        request_key = str(UUID(str(request_key)))
        operation = f"POST /api/v1/ingredients/{ingredient_id}/delete"
        digest = sha256(encode({"stock_version": stock_version}).encode()).digest()
        with self.transaction(write=True) as conn:
            saved = conn.execute("SELECT * FROM write_request WHERE request_key=?", (request_key,)).fetchone()
            if saved:
                if saved["operation"] != operation or saved["payload_hash"] != digest:
                    raise InventoryError("IDEMPOTENCY_KEY_REUSED")
                return json.loads(saved["response_json"])
            if stock_version != self._version(conn):
                raise InventoryError("STOCK_VERSION_CONFLICT", stock_version=self._version(conn))
            row = conn.execute("SELECT stock_policy FROM ingredient WHERE id=?", (ingredient_id,)).fetchone()
            if row is None or row["stock_policy"] != "tracked":
                raise InventoryError("INVALID_INPUT", field="ingredient_id")
            changed = conn.execute("INSERT OR IGNORE INTO archived_ingredient(ingredient_id) VALUES (?)",
                                   (ingredient_id,)).rowcount
            previous = conn.execute("SELECT state,note,prioritize FROM inventory WHERE ingredient_id=?",
                                    (ingredient_id,)).fetchone()
            after = {"state": "empty", "note": None, "prioritize": False}
            if changed:
                conn.execute("""
                    INSERT INTO inventory(ingredient_id,state,note,prioritize) VALUES (?,'empty',NULL,0)
                    ON CONFLICT(ingredient_id) DO UPDATE SET state='empty',note=NULL,prioritize=0,
                    updated_at=excluded.updated_at
                """, (ingredient_id,))
                conn.execute("UPDATE household_state SET stock_version=stock_version+1 WHERE id=1")
            result = self._snapshot(conn)
            conn.execute("INSERT INTO write_request(request_key,operation,payload_hash,http_status,response_json) VALUES (?,?,?,200,?)",
                         (request_key, operation, digest, encode(result)))
            if changed:
                conn.execute("""
                    INSERT INTO inventory_event(request_key,ingredient_id,source,before_value,after_value)
                    VALUES (?,?,'intake',?,?)
                """, (request_key, ingredient_id, encode(dict(previous)) if previous else None, encode(after)))
            return result
