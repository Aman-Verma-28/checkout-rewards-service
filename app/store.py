"""Domain logic. Every public method is exactly one database transaction."""
import uuid
from datetime import datetime, timezone

from app import db


class ApiError(Exception):
    """A business-rule failure with a stable, machine-readable code."""

    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        db.init(db_path)

    def _write(self):
        return db.transaction(self.db_path, write=True)

    def _read(self):
        return db.transaction(self.db_path, write=False)

    # ---- products -------------------------------------------------------

    def list_products(self) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute("SELECT id, name, price_cents, inventory FROM products ORDER BY id")
            return [dict(r) for r in rows]

    # ---- carts ----------------------------------------------------------

    def create_cart(self) -> dict:
        cart_id, now = _id("cart"), _now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO carts (id, created_at, updated_at) VALUES (?, ?, ?)", (cart_id, now, now)
            )
            return self._cart_view(conn, cart_id)

    def get_cart(self, cart_id: str) -> dict:
        with self._read() as conn:
            return self._cart_view(conn, cart_id)

    def set_item(self, cart_id: str, product_id: str, quantity: int) -> dict:
        """Add a product or change its quantity (absolute, so a retried request is harmless)."""
        with self._write() as conn:
            self._touch_open_cart(conn, cart_id)
            product = conn.execute(
                "SELECT inventory FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            if product is None:
                raise ApiError(404, "PRODUCT_NOT_FOUND", f"Product {product_id!r} does not exist.")
            if quantity > product["inventory"]:
                # Not a reservation: checkout re-checks. This only stops obviously unfillable carts.
                raise ApiError(
                    409, "INSUFFICIENT_INVENTORY", "Not enough inventory for the requested quantity.",
                    items=[{"product_id": product_id, "requested": quantity,
                            "available": product["inventory"]}],
                )
            conn.execute(
                """INSERT INTO cart_items (cart_id, product_id, quantity) VALUES (?, ?, ?)
                   ON CONFLICT (cart_id, product_id) DO UPDATE SET quantity = excluded.quantity""",
                (cart_id, product_id, quantity),
            )
            return self._cart_view(conn, cart_id)

    def remove_item(self, cart_id: str, product_id: str) -> dict:
        """Idempotent: removing an item that is not in the cart is a no-op."""
        with self._write() as conn:
            self._touch_open_cart(conn, cart_id)
            conn.execute(
                "DELETE FROM cart_items WHERE cart_id = ? AND product_id = ?", (cart_id, product_id)
            )
            return self._cart_view(conn, cart_id)

    def _touch_open_cart(self, conn, cart_id: str) -> None:
        """Claim the cart row for this transaction, or fail if it is missing or checked out.

        A conditional UPDATE rather than a SELECT, so on a row-locking database it also
        serialises against a concurrent checkout of the same cart.
        """
        claimed = conn.execute(
            "UPDATE carts SET updated_at = ? WHERE id = ? AND status = 'open'", (_now(), cart_id)
        ).rowcount
        if not claimed:
            self._raise_cart_unavailable(conn, cart_id)

    def _raise_cart_unavailable(self, conn, cart_id: str):
        order = conn.execute("SELECT id FROM orders WHERE cart_id = ?", (cart_id,)).fetchone()
        if order is None:
            raise ApiError(404, "CART_NOT_FOUND", f"Cart {cart_id!r} does not exist.")
        raise ApiError(
            409, "CART_NOT_OPEN", "Cart is already checked out and can no longer change.",
            order_id=order["id"],
        )

    def _cart_view(self, conn, cart_id: str) -> dict:
        cart = conn.execute("SELECT id, status, created_at FROM carts WHERE id = ?", (cart_id,)).fetchone()
        if cart is None:
            raise ApiError(404, "CART_NOT_FOUND", f"Cart {cart_id!r} does not exist.")
        rows = conn.execute(
            """SELECT i.product_id, p.name, p.price_cents, p.inventory, i.quantity
               FROM cart_items i JOIN products p ON p.id = i.product_id
               WHERE i.cart_id = ? ORDER BY i.product_id""",
            (cart_id,),
        ).fetchall()
        items = [
            {
                "product_id": r["product_id"],
                "name": r["name"],
                "unit_price_cents": r["price_cents"],  # live price; the order snapshots it
                "quantity": r["quantity"],
                "line_total_cents": r["price_cents"] * r["quantity"],
                "available": r["inventory"] >= r["quantity"],
            }
            for r in rows
        ]
        view = {
            "id": cart["id"],
            "status": cart["status"],
            "items": items,
            "subtotal_cents": sum(i["line_total_cents"] for i in items),
            "created_at": cart["created_at"],
        }
        if cart["status"] == "checked_out":
            view["order_id"] = conn.execute(
                "SELECT id FROM orders WHERE cart_id = ?", (cart_id,)
            ).fetchone()["id"]
        return view
