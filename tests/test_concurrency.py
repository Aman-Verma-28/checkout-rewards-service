"""Competing and repeated operations: the failure modes a happy-path test never sees."""
from concurrent.futures import ThreadPoolExecutor

import httpx

from conftest import EVERY_N, cart_with, error_code, place_order, post_concurrently, stock, earn_coupon


def test_concurrent_checkouts_never_oversell_and_losers_leave_no_trace(api):
    # 10 carts race for 3 limited units. Each cart also holds a bottle, which sorts first
    # and is decremented before the sneakers, so a non-atomic checkout would leak bottles.
    carts = [cart_with(api, {"bottle": 1, "sneakers-ltd": 1}) for _ in range(10)]

    responses = post_concurrently(api, [(f"/carts/{c}/checkout", None) for c in carts])

    assert sorted(r.status_code for r in responses) == [201] * 3 + [409] * 7
    assert {error_code(r) for r in responses if r.status_code == 409} == {"INSUFFICIENT_INVENTORY"}
    assert stock(api, "sneakers-ltd") == 0
    assert stock(api, "bottle") == 60 - 3
    assert api.get("/admin/report").json()["orders_placed"] == 3
    losers = [c for c, r in zip(carts, responses) if r.status_code == 409]
    assert all(api.get(f"/carts/{c}").json()["status"] == "open" for c in losers)


def test_duplicate_checkout_requests_create_exactly_one_order(api):
    cart = cart_with(api, {"mug": 2})

    responses = post_concurrently(api, [(f"/carts/{cart}/checkout", None)] * 8)

    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert all(r.headers["Idempotent-Replayed"] == "true" for r in responses if r.status_code == 200)
    assert stock(api, "mug") == 50 - 2
    assert api.get("/admin/report").json()["orders_placed"] == 1

    # A late retry still replays; a *different* request for the spent cart does not.
    order_id = responses[0].json()["id"]
    assert api.post(f"/carts/{cart}/checkout").json()["id"] == order_id
    r = api.post(f"/carts/{cart}/checkout", json={"coupon_code": "SAVE10-OTHER"})
    assert r.status_code == 409 and error_code(r) == "CART_ALREADY_CHECKED_OUT"
    assert r.json()["error"]["details"]["order_id"] == order_id


def test_coupon_is_redeemed_by_exactly_one_of_many_concurrent_checkouts(api):
    code = earn_coupon(api)
    carts = [cart_with(api, {"tshirt": 1}) for _ in range(5)]

    responses = post_concurrently(api, [(f"/carts/{c}/checkout", {"coupon_code": code}) for c in carts])

    winners = [r for r in responses if r.status_code == 201]
    assert len(winners) == 1
    assert winners[0].json()["discount_cents"] == 199  # floor(1999 * 10%)
    losers = [(c, r) for c, r in zip(carts, responses) if r.status_code != 201]
    assert all(r.status_code == 409 and error_code(r) == "COUPON_ALREADY_REDEEMED" for _, r in losers)
    assert all(api.get(f"/carts/{c}").json()["status"] == "open" for c, _ in losers)
    [coupon] = api.get("/admin/coupons").json()
    assert coupon["status"] == "redeemed" and coupon["redeemed_by_order_id"] == winners[0].json()["id"]


def test_concurrent_coupon_generation_rewards_each_milestone_once(api):
    for _ in range(EVERY_N - 1):
        place_order(api, {"cap": 1})
    assert error_code(api.post("/admin/coupons")) == "NO_ELIGIBLE_MILESTONE"
    for _ in range(EVERY_N + 1):
        place_order(api, {"cap": 1})  # now 2n orders placed: two milestones waiting

    responses = post_concurrently(api, [("/admin/coupons", None)] * 6)

    assert sorted(r.status_code for r in responses) == [201] * 2 + [409] * 4
    assert [c["milestone"] for c in api.get("/admin/coupons").json()] == [EVERY_N, 2 * EVERY_N]
    assert api.get("/admin/report").json()["rewards"]["milestones_awaiting_coupon"] == 0


def test_cart_edit_racing_checkout_never_changes_a_placed_order(api):
    # Whichever wins, the order must match the cart as it was when the order was placed,
    # and an edit that loses the race must be refused, not applied to a spent cart.
    for _ in range(10):
        cart = cart_with(api, {"hoodie": 1})
        with ThreadPoolExecutor(2) as pool:  # one connection per thread
            checkout = pool.submit(httpx.post, f"{api.base_url}/carts/{cart}/checkout")
            edit = pool.submit(httpx.put, f"{api.base_url}/carts/{cart}/items/hoodie", json={"quantity": 2})
        checkout, edit = checkout.result(), edit.result()

        assert checkout.status_code == 201
        [line] = checkout.json()["lines"]
        if edit.status_code == 200:
            assert line["quantity"] == 2
        else:
            assert error_code(edit) == "CART_ALREADY_CHECKED_OUT" and line["quantity"] == 1
        [cart_line] = api.get(f"/carts/{cart}").json()["items"]
        assert cart_line["quantity"] == line["quantity"]
