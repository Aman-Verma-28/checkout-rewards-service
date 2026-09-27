"""Domain logic. Every public method is exactly one database transaction."""
import secrets
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


def discount_cents(subtotal_cents: int, percent: int) -> int:
    """percent% of the subtotal, rounded down to the cent. Integer math, never above the subtotal."""
    return min(subtotal_cents, subtotal_cents * percent // 100)


class Store:
    def __init__(self, db_path: str, every_n: int, percent: int):
        if every_n < 1 or not 1 <= percent <= 100:
            raise ValueError("need every_n >= 1 and 1 <= percent <= 100")
        self.db_path, self.every_n, self.percent = db_path, every_n, percent
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

    def update_product(self, product_id: str, price_cents: int | None,
                       inventory: int | None) -> dict:
        """Admin: set price and/or stock. Only affects carts and future orders, never placed ones."""
        with self._write() as conn:
            updated = conn.execute(
                """UPDATE products SET price_cents = COALESCE(?, price_cents),
                                       inventory = COALESCE(?, inventory) WHERE id = ?""",
                (price_cents, inventory, product_id),
            ).rowcount
            if not updated:
                raise ApiError(404, "PRODUCT_NOT_FOUND", f"Product {product_id!r} does not exist.")
            return dict(conn.execute(
                "SELECT id, name, price_cents, inventory FROM products WHERE id = ?", (product_id,)
            ).fetchone())

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
        raise ApiError(409, "CART_ALREADY_CHECKED_OUT", "Cart is already checked out.",
                       order_id=order["id"])

    # ---- checkout -------------------------------------------------------

    def checkout(self, cart_id: str, coupon_code: str | None = None,
                 expected_subtotal_cents: int | None = None) -> tuple[dict, bool]:
        """Place the order for a cart. Returns (order, replayed).

        One transaction: the cart state change, stock decrements, coupon redemption and
        order snapshot all commit together or not at all, so a failed checkout leaves no
        trace and never consumes a coupon.
        The cart is the idempotency key: retrying a checked-out cart with the same coupon
        returns its order.
        """
        code = (coupon_code or "").strip().upper() or None
        with self._write() as conn:
            claimed = conn.execute(
                "UPDATE carts SET status = 'checked_out', updated_at = ? WHERE id = ? AND status = 'open'",
                (_now(), cart_id),
            ).rowcount
            if not claimed:
                return self._replay(conn, cart_id, code), True

            lines = conn.execute(
                """SELECT i.product_id, p.name, p.price_cents, p.inventory, i.quantity
                   FROM cart_items i JOIN products p ON p.id = i.product_id
                   WHERE i.cart_id = ? ORDER BY i.product_id""",
                (cart_id,),
            ).fetchall()
            if not lines:
                raise ApiError(422, "CART_EMPTY", "Cannot check out an empty cart.")
            short = [
                {"product_id": l["product_id"], "requested": l["quantity"], "available": l["inventory"]}
                for l in lines if l["quantity"] > l["inventory"]
            ]
            if short:
                raise ApiError(409, "INSUFFICIENT_INVENTORY",
                               "Some items no longer have enough inventory.", items=short)

            subtotal = sum(l["price_cents"] * l["quantity"] for l in lines)
            if expected_subtotal_cents is not None and expected_subtotal_cents != subtotal:
                # Prices moved since the client last showed the cart: refuse rather than charge it.
                raise ApiError(409, "PRICE_CHANGED",
                               "Cart prices changed since the client last saw them.",
                               expected_subtotal_cents=expected_subtotal_cents, subtotal_cents=subtotal)
            percent = self._redeemable_percent(conn, code) if code else 0
            discount = discount_cents(subtotal, percent)
            total = subtotal - discount

            order_id = _id("ord")
            # orders.coupon_code is UNIQUE: inserting the order *is* the redemption.
            conn.execute(
                """INSERT INTO orders (id, cart_id, coupon_code, discount_percent,
                                       subtotal_cents, discount_cents, total_cents, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, cart_id, code, percent, subtotal, discount, total, _now()),
            )
            for l in lines:
                # Conditional decrement: correct even without the IMMEDIATE lock (e.g. on
                # Postgres READ COMMITTED), where the check above could be stale.
                decremented = conn.execute(
                    "UPDATE products SET inventory = inventory - ? WHERE id = ? AND inventory >= ?",
                    (l["quantity"], l["product_id"], l["quantity"]),
                ).rowcount
                if not decremented:
                    raise ApiError(409, "INSUFFICIENT_INVENTORY",
                                   "Some items no longer have enough inventory.",
                                   items=[{"product_id": l["product_id"], "requested": l["quantity"]}])
                conn.execute(
                    """INSERT INTO order_lines
                       (order_id, product_id, name, unit_price_cents, quantity, line_total_cents)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (order_id, l["product_id"], l["name"], l["price_cents"], l["quantity"],
                     l["price_cents"] * l["quantity"]),
                )
            return self._order_view(conn, order_id), False

    def _replay(self, conn, cart_id: str, code: str | None) -> dict:
        order = conn.execute(
            "SELECT id, coupon_code FROM orders WHERE cart_id = ?", (cart_id,)
        ).fetchone()
        # Same cart + same coupon = the same request retried. A different coupon is a
        # different request for a cart that is already spent.
        if order is None or order["coupon_code"] != code:
            self._raise_cart_unavailable(conn, cart_id)
        return self._order_view(conn, order["id"])

    def _redeemable_percent(self, conn, code: str) -> int:
        coupon = conn.execute(
            """SELECT c.percent, o.id AS order_id
               FROM coupons c LEFT JOIN orders o ON o.coupon_code = c.code WHERE c.code = ?""",
            (code,),
        ).fetchone()
        if coupon is None:
            raise ApiError(422, "COUPON_NOT_FOUND", f"Coupon {code!r} does not exist.")
        if coupon["order_id"] is not None:
            raise ApiError(409, "COUPON_ALREADY_REDEEMED", f"Coupon {code!r} has already been used.")
        return coupon["percent"]

    # ---- coupons --------------------------------------------------------

    def generate_coupon(self) -> dict:
        """Issue the coupon for the oldest milestone that has been reached but not rewarded."""
        with self._write() as conn:
            placed = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            last = conn.execute("SELECT COALESCE(MAX(milestone), 0) FROM coupons").fetchone()[0]
            milestone = last + self.every_n
            if placed < milestone:
                raise ApiError(409, "NO_ELIGIBLE_MILESTONE",
                               "No order milestone is waiting for a coupon.",
                               orders_placed=placed, next_milestone=milestone)
            code = f"SAVE{self.percent}-{secrets.token_hex(5).upper()}"
            conn.execute(
                "INSERT INTO coupons (code, milestone, percent, created_at) VALUES (?, ?, ?, ?)",
                (code, milestone, self.percent, _now()),
            )
            return self._coupons(conn, "WHERE c.code = ?", (code,))[0]

    def list_coupons(self) -> list[dict]:
        with self._read() as conn:
            return self._coupons(conn)

    def _coupons(self, conn, where: str = "", params: tuple = ()) -> list[dict]:
        rows = conn.execute(
            f"""SELECT c.code, c.milestone, c.percent, c.created_at, o.id AS order_id
                FROM coupons c LEFT JOIN orders o ON o.coupon_code = c.code
                {where} ORDER BY c.milestone""",
            params,
        )
        return [
            {
                "code": r["code"],
                "milestone": r["milestone"],  # earned when this many orders had been placed
                "percent": r["percent"],
                "status": "redeemed" if r["order_id"] else "available",
                "redeemed_by_order_id": r["order_id"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ---- orders ---------------------------------------------------------

    def get_order(self, order_id: str) -> dict:
        with self._read() as conn:
            return self._order_view(conn, order_id)

    def list_orders(self) -> list[dict]:
        with self._read() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM orders ORDER BY created_at, id")]
            # Known limit: one query per order. Paginate and join once order volume matters.
            return [self._order_view(conn, i) for i in ids]

    def _order_view(self, conn, order_id: str) -> dict:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise ApiError(404, "ORDER_NOT_FOUND", f"Order {order_id!r} does not exist.")
        lines = conn.execute(
            """SELECT product_id, name, unit_price_cents, quantity, line_total_cents
               FROM order_lines WHERE order_id = ? ORDER BY product_id""",
            (order_id,),
        )
        return {
            "id": order["id"],
            "cart_id": order["cart_id"],
            "status": "placed",
            "lines": [dict(l) for l in lines],
            "subtotal_cents": order["subtotal_cents"],
            "coupon_code": order["coupon_code"],
            "discount_percent": order["discount_percent"],
            "discount_cents": order["discount_cents"],
            "total_cents": order["total_cents"],
            "created_at": order["created_at"],
        }

    # ---- reporting ------------------------------------------------------

    def report(self) -> dict:
        """Aggregates over orders and coupons, read from one snapshot. Never writes."""
        with self._read() as conn:
            products = conn.execute(
                """SELECT p.id AS product_id, p.name,
                          COALESCE(SUM(l.quantity), 0) AS quantity_sold,
                          COALESCE(SUM(l.line_total_cents), 0) AS gross_revenue_cents
                   FROM products p LEFT JOIN order_lines l ON l.product_id = p.id
                   GROUP BY p.id ORDER BY p.id"""
            ).fetchall()
            orders = conn.execute(
                """SELECT COUNT(*) AS placed,
                          COALESCE(SUM(subtotal_cents), 0) AS gross,
                          COALESCE(SUM(discount_cents), 0) AS discount,
                          COALESCE(SUM(total_cents), 0) AS net
                   FROM orders"""
            ).fetchone()
            coupons = conn.execute(
                """SELECT COUNT(*) AS generated, COUNT(o.id) AS redeemed,
                          COALESCE(MAX(c.milestone), 0) AS last_milestone
                   FROM coupons c LEFT JOIN orders o ON o.coupon_code = c.code"""
            ).fetchone()
        return {
            "orders_placed": orders["placed"],
            "gross_revenue_cents": orders["gross"],
            "total_discount_cents": orders["discount"],
            "net_revenue_cents": orders["net"],
            "products": [dict(r) for r in products],
            "coupons": {
                "generated": coupons["generated"],
                "available": coupons["generated"] - coupons["redeemed"],
                "redeemed": coupons["redeemed"],
            },
            "rewards": {
                "every_n": self.every_n,
                "percent": self.percent,
                "next_milestone": coupons["last_milestone"] + self.every_n,
                "milestones_awaiting_coupon": (orders["placed"] - coupons["last_milestone"]) // self.every_n,
            },
        }

    # ---- views ----------------------------------------------------------

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
