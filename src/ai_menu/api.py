"""FastAPI entry point for the single household MVP."""

import asyncio
from collections import OrderedDict
import logging
from contextlib import asynccontextmanager, suppress
from datetime import date
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Annotated, Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ingredient_images import IngredientImageGenerator
from .inventory_service import InventoryError, StockPatch
from .menu_service import MenuService, today
from .recipe_ai import ChatRecipeGenerator, RecipeAIError
from .paths import DEFAULT_DATABASE, STATIC_ROOT
from .scheduler import run_scheduler

Positive = Annotated[int, Field(strict=True, gt=0)]
Version = Annotated[int, Field(strict=True, ge=0)]
Slot = Annotated[int, Field(strict=True, ge=1, le=3)]
Note = Annotated[str, Field(max_length=200)]
Key = Annotated[UUID, Header(alias="Idempotency-Key")]
MenuDate = Annotated[date, Header(alias="X-Menu-Date")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParseInput(Input):
    text: str = Field(min_length=1, max_length=500)


class StockInput(Input):
    ingredient_id: Positive | None = None
    ingredient_name: str | None = Field(default=None, max_length=40)
    state: Literal["enough", "low", "empty"]
    note: Note | None
    prioritize: bool = Field(strict=True)


class Deletion(Input):
    stock_version: Version


class StockConfirmation(Input):
    stock_version: Version
    items: list[StockInput] = Field(min_length=1, max_length=50)


class MenuRevision(Input):
    revision: Positive
    stock_version: Version


class Replacement(MenuRevision):
    scope: Literal["one", "all"]
    slot: Slot | None = None

    @model_validator(mode="after")
    def validate_slot(self):
        if (self.scope == "one" and self.slot is None) or (
            self.scope == "all" and "slot" in self.model_fields_set
        ):
            raise ValueError("slot is required for scope=one and omitted for scope=all")
        return self


class Remaining(Input):
    ingredient_id: Positive
    outcome: Literal["empty", "low", "unchanged"]
    note: Note | None = None

    @model_validator(mode="after")
    def validate_note(self):
        if self.outcome != "low" and "note" in self.model_fields_set:
            raise ValueError("note is only accepted for outcome=low")
        return self


class CookConfirmation(MenuRevision):
    cooked_slots: list[Slot] = Field(min_length=1, max_length=3)
    remaining: list[Remaining]

    @model_validator(mode="after")
    def check_slots(self):
        if len(set(self.cooked_slots)) != len(self.cooked_slots):
            raise ValueError("cooked_slots must be distinct")
        return self


class Ingredient(BaseModel):
    id: int
    code: str
    name: str
    stock_policy: Literal["tracked", "unlimited", "pantry"]
    food_group: Literal["plant", "egg", "meat", "other_animal"]


class IngredientList(BaseModel):
    items: list[Ingredient]


class StockItem(BaseModel):
    ingredient: Ingredient
    state: Literal["enough", "low", "empty"] | None
    note: str | None
    prioritize: bool
    updated_at: str | None


class Inventory(BaseModel):
    stock_version: int
    items: list[StockItem]


class Unresolved(BaseModel):
    text: str
    reason: str


class Preview(BaseModel):
    stock_version: int
    items: list[StockInput]
    unresolved: list[Unresolved]


class DishIngredient(BaseModel):
    ingredient_id: int
    name: str
    stock_policy: str
    amount_hint: str


class Dish(BaseModel):
    slot: int
    recipe_id: int
    name: str
    minutes: int
    instructions: list[str]
    ingredients: list[DishIngredient]


class Diagnostics(BaseModel):
    reason: Literal["no_feasible_menu"]
    unavailable_ingredient_ids: list[int]


class Menu(BaseModel):
    menu_date: date
    status: Literal["not_generated", "ready", "insufficient", "cooked"]
    revision: int | None
    stock_version: int | None
    current_stock_version: int
    stale: bool
    generated_at: str | None
    dishes: list[Dish]
    diagnostics: Diagnostics | None


class Cooking(BaseModel):
    menu_date: date
    menu_revision: int
    cooked_slots: list[int]
    confirmed_at: str


class CookResult(BaseModel):
    menu: Menu
    cooking: Cooking
    stock_version: int


class HistoryEntry(BaseModel):
    cooking: Cooking
    dishes: list[Dish]


class History(BaseModel):
    items: list[HistoryEntry]
    next_before: date | None


def error_response(status, code, details):
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": code.replace("_", " ").lower(), "details": details},
    })


def image_error_response(ingredient_id, reason, upstream_status=None):
    logging.getLogger("uvicorn.error").warning(
        "Ingredient image generation failed: ingredient_id=%s reason=%s upstream_status=%s",
        ingredient_id, reason, upstream_status,
    )
    details = {"reason": reason}
    if upstream_status is not None:
        details["upstream_status"] = upstream_status
    return error_response(503, "IMAGE_AI_UNAVAILABLE", details)


def create_app(database, clock=today, *, schedule=False, recipe_generator=None, image_generator=None, require_model=True):
    service = MenuService(database, clock, recipe_generator, require_model=require_model)
    assets = STATIC_ROOT
    replacement_jobs = OrderedDict()
    jobs_lock = Lock()
    image_lock = Lock()
    image_cache = Path(f"{database}.ingredient-images")

    def run_replacement(key, day, payload):
        with jobs_lock:
            request, _ = replacement_jobs[key]
            replacement_jobs[key] = (request, {"job_id": key, "status": "running"})
        try:
            menu = service.write_menu("replace", day, key, payload)
            result = {"status": "succeeded", "menu": menu}
        except InventoryError as error:
            result = {"status": "failed", "error": {"code": error.code, "details": error.details}}
        except Exception as error:
            logging.getLogger("uvicorn.error").error(
                "Background menu replacement failed: job_id=%s error_type=%s", key, type(error).__name__,
            )
            result = {"status": "failed", "error": {"code": "MENU_JOB_FAILED", "details": {}}}
        with jobs_lock:
            replacement_jobs[key] = (request, {"job_id": key, **result})


    @asynccontextmanager
    async def lifespan(app):
        if not schedule:
            yield
            return
        task = asyncio.create_task(run_scheduler(service))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="家庭菜单", lifespan=lifespan)
    app.mount("/h5", StaticFiles(directory=assets), name="h5")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-cache"})

    @app.exception_handler(InventoryError)
    async def handle_domain_error(request: Request, error: InventoryError):
        status = {"INVALID_INPUT": 422, "RECIPE_AI_UNAVAILABLE": 503, "INVENTORY_AI_UNAVAILABLE": 503}.get(error.code, 409)
        if error.code == "INVENTORY_AI_UNAVAILABLE":
            logging.getLogger("uvicorn.error").warning(
                "Inventory preview failed: reason=%s upstream_status=%s",
                error.details.get("reason"), error.details.get("upstream_status"),
            )
        return error_response(status, error.code, error.details)

    @app.exception_handler(RequestValidationError)
    async def handle_input_error(request: Request, error: RequestValidationError):
        fields = [{"path": list(item["loc"]), "message": item["msg"]} for item in error.errors()]
        return error_response(422, "INVALID_INPUT", {"fields": fields})

    @app.exception_handler(sqlite3.OperationalError)
    async def handle_database_error(request: Request, error: sqlite3.OperationalError):
        if getattr(error, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_BUSY:
            return error_response(503, "WRITE_RETRY_EXHAUSTED", {})
        raise error

    @app.get("/api/v1/ingredients", response_model=IngredientList)
    def ingredients(q: Annotated[str | None, Query(max_length=80)] = None):
        with service.transaction() as conn:
            rows = conn.execute("""
                SELECT i.* FROM ingredient i
                WHERE NOT EXISTS (SELECT 1 FROM archived_ingredient ar WHERE ar.ingredient_id=i.id) AND (? IS NULL OR instr(i.name, ?) > 0 OR EXISTS (
                    SELECT 1 FROM ingredient_alias a WHERE a.ingredient_id=i.id AND instr(a.alias, ?) > 0
                )) ORDER BY i.id
            """, (q, q, q))
            return {"items": [dict(row) for row in rows]}

    def cached_image(ingredient_id):
        for extension in ("webp", "svg"):
            path = image_cache / f"{ingredient_id}.{extension}"
            if path.is_file():
                return path
        return None

    @app.get("/api/v1/ingredient-images")
    def ingredient_images():
        return {"ids": sorted({int(path.stem) for path in image_cache.iterdir()
                               if path.suffix in (".webp", ".svg")
                               and path.stem.isascii() and path.stem.isdecimal()
                               and path.is_file()}) if image_cache.is_dir() else []}

    @app.get("/api/v1/ingredient-images/{ingredient_id}")
    def ingredient_image(ingredient_id: Annotated[int, Field(gt=0)]):
        path = cached_image(ingredient_id)
        if path is None:
            return error_response(404, "INGREDIENT_IMAGE_NOT_FOUND", {})
        media_type = "image/svg+xml" if path.suffix == ".svg" else "image/webp"
        return FileResponse(path, media_type=media_type, headers={
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        })

    @app.post("/api/v1/ingredient-images/{ingredient_id}")
    def generate_ingredient_image(ingredient_id: Annotated[int, Field(gt=0)]):
        if cached_image(ingredient_id) is not None:
            return {"ingredient_id": ingredient_id}
        with service.transaction() as conn:
            ingredient = conn.execute("SELECT name FROM ingredient WHERE id=?", (ingredient_id,)).fetchone()
        if ingredient is None:
            return error_response(404, "INGREDIENT_NOT_FOUND", {})
        if image_generator is None:
            return image_error_response(ingredient_id, "not_configured")
        if not image_lock.acquire(blocking=False):
            return error_response(409, "IMAGE_AI_BUSY", {})
        try:
            if cached_image(ingredient_id) is None:
                extension = "svg" if image_generator.extension == "svg" else "webp"
                path = image_cache / f"{ingredient_id}.{extension}"
                image = image_generator.generate(ingredient["name"])
                image_cache.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(dir=image_cache, delete=False) as output:
                    temporary = Path(output.name)
                    try:
                        output.write(image)
                        output.close()
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
        except RecipeAIError as error:
            return image_error_response(ingredient_id, error.reason, error.upstream_status)
        except OSError:
            return image_error_response(ingredient_id, "storage_failed")
        finally:
            image_lock.release()
        return {"ingredient_id": ingredient_id}

    @app.post("/api/v1/ingredients/{ingredient_id}/delete", response_model=Inventory)
    def delete_ingredient(ingredient_id: Annotated[int, Field(gt=0)], body: Deletion, key: Key):
        return service.delete_ingredient(ingredient_id, body.stock_version, key)

    @app.get("/api/v1/inventory", response_model=Inventory)
    def inventory():
        return service.get_inventory()

    @app.post("/api/v1/inventory/parse", response_model=Preview)
    def parse(body: ParseInput):
        return service.parse(body.text)

    @app.post("/api/v1/inventory/confirm", response_model=Inventory)
    def update_inventory(body: StockConfirmation, key: Key):
        return service.confirm(body.stock_version, [StockPatch(**item.model_dump()) for item in body.items], key)

    @app.get("/api/v1/menus/today", response_model=Menu)
    def menu():
        return service.get_today()

    @app.post("/api/v1/menus/today/generate", response_model=Menu)
    def generate(body: Input, day: MenuDate, key: Key):
        return service.write_menu("generate", day.isoformat(), key, {})

    @app.post("/api/v1/menus/today/replace", status_code=202)
    def replace(body: Replacement, day: MenuDate, key: Key, background: BackgroundTasks):
        payload = body.model_dump(exclude_none=True)
        day = day.isoformat()
        key = str(key)
        request = (day, payload)
        with jobs_lock:
            existing = replacement_jobs.get(key)
            if existing is not None:
                if existing[0] != request:
                    raise InventoryError("IDEMPOTENCY_KEY_REUSED")
                return existing[1]
            if any(job["status"] in ("queued", "running") for _, job in replacement_jobs.values()):
                raise InventoryError("MENU_JOB_IN_PROGRESS")
            if len(replacement_jobs) >= 128:
                replacement_jobs.popitem(last=False)
            job = {"job_id": key, "status": "queued"}
            replacement_jobs[key] = (request, job)
        background.add_task(run_replacement, key, day, payload)
        return job

    @app.get("/api/v1/menus/today/replace/{job_id}")
    def replacement_status(job_id: UUID):
        with jobs_lock:
            existing = replacement_jobs.get(str(job_id))
        if existing is None:
            return error_response(404, "MENU_JOB_NOT_FOUND", {})
        return existing[1]

    @app.post("/api/v1/menus/today/confirm-cooked", response_model=CookResult)
    def confirm_cooked(body: CookConfirmation, day: MenuDate, key: Key):
        payload = body.model_dump(exclude_none=True)
        payload["cooked_slots"].sort()
        payload["remaining"].sort(key=lambda row: row["ingredient_id"])
        return service.write_menu("confirm-cooked", day.isoformat(), key, payload)

    @app.get("/api/v1/cooking-history", response_model=History)
    def history(before: date | None = None, limit: Annotated[int, Query(ge=1, le=50)] = 20):
        return service.history(before.isoformat() if before else None, limit)

    return app


app = create_app(
    os.environ.get("MENU_DATABASE", DEFAULT_DATABASE),
    schedule=True,
    require_model=True,
    recipe_generator=ChatRecipeGenerator.from_environment(),
    image_generator=IngredientImageGenerator.from_environment(),
)
