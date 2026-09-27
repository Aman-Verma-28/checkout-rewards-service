"""HTTP layer: routing, request validation and the error envelope. No business rules here."""
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
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


class CheckoutBody(Body):
    coupon_code: str | None = Field(None, max_length=64)
    # Optional guard: the subtotal the client showed the customer (GET /carts/{id}).
    # If prices moved since, checkout fails with PRICE_CHANGED instead of charging the new amount.
    expected_subtotal_cents: StrictInt | None = Field(None, ge=0)


class ProductPatch(Body):
    price_cents: StrictInt | None = Field(None, ge=0, le=100_000_000)
    inventory: StrictInt | None = Field(None, ge=0, le=1_000_000)


# ---- storefront ---------------------------------------------------------


@router.get("/", include_in_schema=False)
def demo():
    return FileResponse(Path(__file__).with_name("demo.html"))


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


@router.post("/carts/{cart_id}/checkout", status_code=201)
def checkout(cart_id: str, request: Request, body: CheckoutBody | None = None):
    body = body or CheckoutBody()
    order, replayed = store(request).checkout(cart_id, body.coupon_code, body.expected_subtotal_cents)
    if replayed:
        return JSONResponse(order, headers={"Idempotent-Replayed": "true"})
    return JSONResponse(order, status_code=201)


@router.get("/orders/{order_id}")
def get_order(order_id: str, request: Request):
    return store(request).get_order(order_id)


# ---- admin (no auth by design; every /admin route is an operator action) ----


@router.post("/admin/coupons", status_code=201)
def generate_coupon(request: Request):
    return store(request).generate_coupon()


@router.get("/admin/coupons")
def list_coupons(request: Request):
    return store(request).list_coupons()


@router.get("/admin/orders")
def list_orders(request: Request):
    return store(request).list_orders()


@router.get("/admin/report")
def report(request: Request):
    return store(request).report()


@router.patch("/admin/products/{product_id}")
def update_product(product_id: str, body: ProductPatch, request: Request):
    return store(request).update_product(product_id, body.price_cents, body.inventory)


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


def create_app(db_path: str | None = None, every_n: int | None = None,
               percent: int | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DB_PATH", "store.db")
    every_n = every_n if every_n is not None else int(os.environ.get("COUPON_EVERY_N", "5"))
    percent = percent if percent is not None else int(os.environ.get("COUPON_PERCENT", "10"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = Store(db_path, every_n, percent)  # creates schema, seeds on first run
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
