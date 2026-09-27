"""Business rules and failure modes, one request at a time."""
from conftest import cart_with, earn_coupon, error_code, place_order, stock

from app.store import discount_cents


def test_discount_rounds_down_to_the_cent_and_never_exceeds_the_subtotal():
    assert discount_cents(6999, 10) == 699  # 699.9 -> 699, the store never over-discounts
    assert discount_cents(5, 10) == 0
    assert discount_cents(1999, 100) == 1999
    for subtotal in range(0, 2000, 7):
        for percent in (1, 10, 33, 99, 100):
            d = discount_cents(subtotal, percent)
            assert d * 100 <= subtotal * percent < (d + 1) * 100  # exactly the floor
            assert 0 <= subtotal - d <= subtotal


def test_failed_checkout_keeps_the_coupon_and_the_stock(api):
    code = earn_coupon(api)
    cart = cart_with(api, {"bottle": 1, "sneakers-ltd": 3})
    api.patch("/admin/products/sneakers-ltd", json={"inventory": 2})  # stock drops after add

    r = api.post(f"/carts/{cart}/checkout", json={"coupon_code": code})

    assert r.status_code == 409 and error_code(r) == "INSUFFICIENT_INVENTORY"
    assert r.json()["error"]["details"]["items"] == [
        {"product_id": "sneakers-ltd", "requested": 3, "available": 2}
    ]
    assert api.get("/admin/coupons").json()[0]["status"] == "available"
    assert (stock(api, "bottle"), stock(api, "sneakers-ltd")) == (60, 2)
    assert api.get(f"/carts/{cart}").json()["status"] == "open"

    # The customer fixes the cart and the same coupon still works.
    api.put(f"/carts/{cart}/items/sneakers-ltd", json={"quantity": 2})
    order = api.post(f"/carts/{cart}/checkout", json={"coupon_code": code}).json()
    assert order["subtotal_cents"] == 2333 + 2 * 12999
    assert order["discount_cents"] == 2833  # floor(28331 * 10%)
    assert order["total_cents"] == 28331 - 2833


def test_price_change_before_checkout_is_caught_and_placed_orders_never_change(api):
    cart = cart_with(api, {"hoodie": 1})
    seen = api.get(f"/carts/{cart}").json()["subtotal_cents"]
    api.patch("/admin/products/hoodie", json={"price_cents": 5499})

    r = api.post(f"/carts/{cart}/checkout", json={"expected_subtotal_cents": seen})
    assert r.status_code == 409 and error_code(r) == "PRICE_CHANGED"
    assert r.json()["error"]["details"] == {"expected_subtotal_cents": 4999, "subtotal_cents": 5499}
    assert api.get(f"/carts/{cart}").json()["items"][0]["unit_price_cents"] == 5499

    order = api.post(f"/carts/{cart}/checkout", json={"expected_subtotal_cents": 5499}).json()
    api.patch("/admin/products/hoodie", json={"price_cents": 1})
    assert api.get(f"/orders/{order['id']}").json() == order  # snapshot, not a live join


def test_invalid_input_never_enters_a_cart(api):
    cart = api.post("/carts").json()["id"]
    item = f"/carts/{cart}/items"
    for body in ({"quantity": 0}, {"quantity": -1}, {"quantity": 1001}, {"quantity": "2"},
                 {"quantity": 1.5}, {"quantity": True}, {}, {"quantity": 1, "qty": 1}):
        r = api.put(f"{item}/mug", json=body)
        assert r.status_code == 422 and error_code(r) == "VALIDATION_ERROR", body
    r = api.put(f"{item}/no-such-product", json={"quantity": 1})
    assert r.status_code == 404 and error_code(r) == "PRODUCT_NOT_FOUND"
    r = api.put(f"{item}/sneakers-ltd", json={"quantity": 4})
    assert r.status_code == 409 and error_code(r) == "INSUFFICIENT_INVENTORY"
    assert api.get(f"/carts/{cart}").json()["items"] == []

    r = api.post(f"/carts/{cart}/checkout")
    assert r.status_code == 422 and error_code(r) == "CART_EMPTY"
    api.put(f"{item}/mug", json={"quantity": 1})
    r = api.post(f"/carts/{cart}/checkout", json={"coupon_code": "NOPE"})
    assert r.status_code == 422 and error_code(r) == "COUPON_NOT_FOUND"

    api.post(f"/carts/{cart}/checkout")
    for r in (api.put(f"{item}/cap", json={"quantity": 1}), api.delete(f"{item}/mug")):
        assert r.status_code == 409 and error_code(r) == "CART_ALREADY_CHECKED_OUT"
    for path in ("/carts/cart_missing", "/orders/ord_missing"):
        assert api.get(path).status_code == 404


def test_report_reconciles_with_orders_and_coupons_and_never_mutates(api):
    code = earn_coupon(api)
    place_order(api, {"bottle": 3, "hoodie": 1}, coupon_code=code)
    replayed = cart_with(api, {"mug": 1})
    api.post(f"/carts/{replayed}/checkout")
    api.post(f"/carts/{replayed}/checkout")  # retry: must not count twice
    api.post(f"/carts/{cart_with(api, {'sneakers-ltd': 3})}/checkout",
             json={"coupon_code": "NOPE"})  # fails: must not count at all
    earn_coupon(api)  # one more coupon, left unredeemed

    def state():
        return [api.get(p).json() for p in ("/products", "/admin/orders", "/admin/coupons")]

    before = state()
    report = api.get("/admin/report").json()
    assert api.get("/admin/report").json() == report
    assert state() == before

    orders, coupons = before[1], before[2]
    lines = [l for o in orders for l in o["lines"]]
    assert report["orders_placed"] == len(orders) == 4  # 2 to earn + coupon + mug
    assert report["gross_revenue_cents"] == sum(o["subtotal_cents"] for o in orders)
    assert report["total_discount_cents"] == sum(o["discount_cents"] for o in orders) > 0
    assert report["net_revenue_cents"] == sum(o["total_cents"] for o in orders)
    assert report["net_revenue_cents"] == report["gross_revenue_cents"] - report["total_discount_cents"]
    for p in report["products"]:
        assert p["quantity_sold"] == sum(l["quantity"] for l in lines if l["product_id"] == p["product_id"])
    assert report["coupons"] == {
        "generated": len(coupons),
        "available": sum(c["status"] == "available" for c in coupons),
        "redeemed": sum(o["coupon_code"] is not None for o in orders),
    } == {"generated": 2, "available": 1, "redeemed": 1}
    for o in orders:
        assert o["subtotal_cents"] == sum(l["line_total_cents"] for l in o["lines"])
        assert o["total_cents"] == o["subtotal_cents"] - o["discount_cents"] >= 0
