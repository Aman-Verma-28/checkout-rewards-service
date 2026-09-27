"""HTTP layer: routing, request validation and the error envelope. No business rules here."""
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.store import ApiError, Store

router = APIRouter()


def store(request: Request) -> Store:
    return request.app.state.store


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo'd field is an error, not silently ignored


class ItemBody(Body):
    quantity: StrictInt = Field(ge=1, le=1000)


# ---- storefront ---------------------------------------------------------


@router.get("/products")
def list_products(request: Request):
    return store(request).list_products()


@router.post("/carts", status_code=201)
def create_cart(request: Request):
    return store(request).create_cart()


@router.get("/carts/{cart_id}")
def get_cart(cart_id: str, request: Request):
    return store(request).get_cart(cart_id)


@router.put("/carts/{cart_id}/items/{product_id}")
def set_item(cart_id: str, product_id: str, body: ItemBody, request: Request):
    return store(request).set_item(cart_id, product_id, body.quantity)


@router.delete("/carts/{cart_id}/items/{product_id}")
def remove_item(cart_id: str, product_id: str, request: Request):
    return store(request).remove_item(cart_id, product_id)


# ---- errors -------------------------------------------------------------


def error(status: int, code: str, message: str, details: dict | None = None, headers=None):
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(body, status_code=status, headers=headers)


def on_api_error(request, exc: ApiError):
    return error(exc.status, exc.code, exc.message, exc.details)


def on_validation_error(request, exc: RequestValidationError):
    fields = [
        {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
        for e in exc.errors()
    ]
    return error(422, "VALIDATION_ERROR", "Request is invalid.", {"fields": fields})


def on_http_error(request, exc: StarletteHTTPException):
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
    return error(exc.status_code, code, str(exc.detail))


def on_db_error(request, exc: sqlite3.OperationalError):
    if "locked" in str(exc) or "busy" in str(exc):
        # Lock wait timed out before anything was written, so a retry is safe.
        return error(503, "SERVICE_BUSY", "Too many concurrent writes, retry shortly.",
                     headers={"Retry-After": "1"})
    return on_unexpected(request, exc)


def on_unexpected(request, exc: Exception):
    return error(500, "INTERNAL_ERROR", "Unexpected server error.")


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "store.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = Store(db_path)  # creates schema and seeds on first run
        yield

    app = FastAPI(title="Checkout & Rewards Service", lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(ApiError, on_api_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(sqlite3.OperationalError, on_db_error)
    app.add_exception_handler(Exception, on_unexpected)
    return app


app = create_app()
