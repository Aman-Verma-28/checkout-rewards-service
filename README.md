# Checkout & Rewards Service

Backend for a small ecommerce store: carts, checkout, orders, and a discount coupon earned every *n*th order. Built to stay correct when requests are retried, overlap, or compete for the same stock or coupon.

The reasoning behind every rule is in [DECISIONS.md](DECISIONS.md). This file covers how to run it and the API.

- **Stack:** Python 3.11+, FastAPI, SQLite (stdlib `sqlite3`, WAL mode). No external services.
- **Tests:** 10 tests against a real HTTP server, half of them concurrent or repeated requests.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app            # http://localhost:8000
```

On first start the service creates `store.db` and seeds six products. To start over, stop the server and delete `store.db*`.

| Env var | Default | Meaning |
|---|---|---|
| `COUPON_EVERY_N` | `5` | every *n*th placed order earns one coupon (`n ≥ 1`) |
| `COUPON_PERCENT` | `10` | coupon discount in whole percent (`1–100`) |
| `DB_PATH` | `store.db` | SQLite file |

`uvicorn app.main:app --workers 4` also works, and stays correct across processes (see DECISIONS.md, "Multiple instances").

- **Demo page:** http://localhost:8000/. A thin client over the API: edit a cart, check out, resend the same checkout, race 10 concurrent checkouts for the 3 limited sneakers, generate coupons, watch the report.
- **Interactive API docs (OpenAPI):** http://localhost:8000/docs

### Run the tests

```bash
.venv/bin/pytest -q
```

Each test starts its own uvicorn server on a random port with a fresh database (`n = 2`, `x = 10`), so concurrent tests exercise the real HTTP stack and thread pool.

| Test | Risk it covers |
|---|---|
| `test_concurrent_checkouts_never_oversell_and_losers_leave_no_trace` | 10 carts race for 3 units: exactly 3 orders, and losing carts leak no stock |
| `test_duplicate_checkout_requests_create_exactly_one_order` | 8 simultaneous retries of one checkout: one order, 7 replays, stock charged once |
| `test_coupon_is_redeemed_by_exactly_one_of_many_concurrent_checkouts` | 5 checkouts race for one coupon: exactly one discount |
| `test_concurrent_coupon_generation_rewards_each_milestone_once` | 6 simultaneous admin calls with 2 milestones due: exactly 2 coupons |
| `test_cart_edit_racing_checkout_never_changes_a_placed_order` | an edit racing a checkout is either in the order or refused, never lost |
| `test_failed_checkout_keeps_the_coupon_and_the_stock` | a failed checkout consumes nothing and the coupon still works afterwards |
| `test_price_change_before_checkout_is_caught_and_placed_orders_never_change` | price drift is refused, and a placed order is a snapshot |
| `test_invalid_input_never_enters_a_cart` | bad product, bad quantity, over-stock, spent cart, empty cart, unknown coupon |
| `test_report_reconciles_with_orders_and_coupons_and_never_mutates` | report equals sums over the orders and coupons, and reading it changes nothing |
| `test_discount_rounds_down_to_the_cent_and_never_exceeds_the_subtotal` | floor rounding, total never negative |

## API conventions

- **Money** is always an integer number of cents (`*_cents`), one implicit currency. No floats anywhere.
- **IDs** are server-generated opaque strings: `cart_…`, `ord_…`. Product IDs are readable slugs (`mug`, `sneakers-ltd`).
- **Admin routes** live under `/admin/*`. There is no authentication (out of scope per the brief); in production these sit behind operator auth.
- **Errors** always share one shape, with a stable `code` for clients to branch on:

```json
{"error": {"code": "INSUFFICIENT_INVENTORY",
           "message": "Some items no longer have enough inventory.",
           "details": {"items": [{"product_id": "sneakers-ltd", "requested": 3, "available": 2}]}}}
```

| Status | Code | When |
|---|---|---|
| 404 | `CART_NOT_FOUND`, `PRODUCT_NOT_FOUND`, `ORDER_NOT_FOUND`, `NOT_FOUND` | the resource in the URL does not exist |
| 409 | `CART_ALREADY_CHECKED_OUT` | changing a checked-out cart, or checking it out again with a different coupon. `details.order_id` names its order |
| 409 | `INSUFFICIENT_INVENTORY` | not enough stock, at add time or checkout. `details.items` lists every short line |
| 409 | `PRICE_CHANGED` | `expected_subtotal_cents` no longer matches the cart |
| 409 | `COUPON_ALREADY_REDEEMED` | coupon already used by another order |
| 409 | `NO_ELIGIBLE_MILESTONE` | admin asked for a coupon but none is due. `details` has `orders_placed`, `next_milestone` |
| 422 | `VALIDATION_ERROR` | malformed body: wrong type, out of range, unknown field. `details.fields` lists each problem |
| 422 | `CART_EMPTY` | checkout of a cart with no items |
| 422 | `COUPON_NOT_FOUND` | coupon code does not exist |
| 503 | `SERVICE_BUSY` | write lock not acquired within 5 s. Nothing was written; retry (`Retry-After: 1`) |
| 500 | `INTERNAL_ERROR` | bug. No details leaked |

409 means "valid request, but the current state forbids it"; the client can fix the state and retry. 422 means "this request can never succeed as written".

## Endpoints

| Method | Path | Success | Errors |
|---|---|---|---|
| GET | `/products` | 200 list | |
| POST | `/carts` | 201 cart | |
| GET | `/carts/{cart_id}` | 200 cart | 404 |
| PUT | `/carts/{cart_id}/items/{product_id}` | 200 cart | 404, 409 `INSUFFICIENT_INVENTORY` / `CART_ALREADY_CHECKED_OUT`, 422 |
| DELETE | `/carts/{cart_id}/items/{product_id}` | 200 cart | 404, 409 `CART_ALREADY_CHECKED_OUT` |
| POST | `/carts/{cart_id}/checkout` | 201 order (new), 200 order (replay) | 404, 409, 422 |
| GET | `/orders/{order_id}` | 200 order | 404 |
| POST | `/admin/coupons` | 201 coupon | 409 `NO_ELIGIBLE_MILESTONE` |
| GET | `/admin/coupons` | 200 list | |
| GET | `/admin/orders` | 200 list of orders | |
| GET | `/admin/report` | 200 report | |
| PATCH | `/admin/products/{product_id}` | 200 product | 404, 422 |

### Products

`GET /products` returns `[{"id": "mug", "name": "Ceramic Mug", "price_cents": 1250, "inventory": 50}, …]`

Seed data:

| id | name | price | stock |
|---|---|---|---|
| `bottle` | Steel Water Bottle | 23.33 | 60 |
| `cap` | Baseball Cap | 15.75 | 40 |
| `hoodie` | Zip Hoodie | 49.99 | 25 |
| `mug` | Ceramic Mug | 12.50 | 50 |
| `sneakers-ltd` | Limited Edition Sneakers | 129.99 | **3** |
| `tshirt` | Cotton T-Shirt | 19.99 | 100 |

### Carts

`POST /carts` (no body) → `201`. The cart object is returned by every cart endpoint:

```json
{"id": "cart_90787dc892044bad96c2a51282ddac6d", "status": "open",
 "items": [{"product_id": "bottle", "name": "Steel Water Bottle", "unit_price_cents": 2333,
            "quantity": 3, "line_total_cents": 6999, "available": true},
           {"product_id": "hoodie", "name": "Zip Hoodie", "unit_price_cents": 4999,
            "quantity": 1, "line_total_cents": 4999, "available": true}],
 "subtotal_cents": 11998, "created_at": "2026-09-27T14:16:12.578+00:00"}
```

- Prices in a cart are **live**: they reflect the product's current price. `available` is `false` when current stock no longer covers the quantity. The cart reserves nothing.
- Once checked out, `status` is `checked_out`, the cart gains `order_id`, and the order is the record of what was bought.

`PUT /carts/{cart_id}/items/{product_id}` with body `{"quantity": 3}` adds the product or sets its quantity. The quantity is **absolute**, so resending the request is harmless.
- `quantity` must be a JSON integer, 1–1000. `"3"`, `3.0`, `true`, `0` and unknown fields are 422.
- Unknown product → 404. More than current stock → 409 `INSUFFICIENT_INVENTORY`. Checked-out cart → 409 `CART_ALREADY_CHECKED_OUT`.

`DELETE /carts/{cart_id}/items/{product_id}` removes the line. Idempotent: removing a line that isn't there returns the cart unchanged.

### Checkout

`POST /carts/{cart_id}/checkout` with optional body:

```json
{"coupon_code": "SAVE10-D7D6D4D6C9", "expected_subtotal_cents": 3998}
```

- **`coupon_code`** (optional): trimmed and upper-cased before lookup. An unknown or used coupon fails the checkout (422 / 409). It never silently falls back to full price.
- **`expected_subtotal_cents`** (optional, recommended): the subtotal the client showed the customer. If prices moved since, checkout fails with 409 `PRICE_CHANGED` (`details.subtotal_cents` has the new figure) instead of charging it.

`201 Created` returns the order:

```json
{"id": "ord_9a53abf397d6443bac48cc9b362abf35", "cart_id": "cart_eaa7a1aecf3f4fe7826debdae1b48cbc",
 "status": "placed",
 "lines": [{"product_id": "tshirt", "name": "Cotton T-Shirt", "unit_price_cents": 1999,
            "quantity": 2, "line_total_cents": 3998}],
 "subtotal_cents": 3998, "coupon_code": "SAVE10-D7D6D4D6C9", "discount_percent": 10,
 "discount_cents": 399, "total_cents": 3599, "created_at": "2026-09-27T14:16:12.701+00:00"}
```

`discount_cents = floor(subtotal_cents × discount_percent / 100)` and `total_cents = subtotal_cents − discount_cents`. Lines are a snapshot of name and unit price at checkout, so later product changes never alter an order.

**Retries.** The cart is the idempotency key. Checking out an already checked-out cart with the **same** `coupon_code` (including none) returns the original order with `200 OK` and header `Idempotent-Replayed: true`. Nothing new is created and stock is not charged again. With a **different** coupon it is a different request for a spent cart: 409 `CART_ALREADY_CHECKED_OUT` with `details.order_id`.

**Failure.** Any error leaves the cart open and untouched: no stock taken, no coupon consumed. The client can fix the cart and try again.

### Orders

`GET /orders/{order_id}` → `200` order (same shape as above) or 404 `ORDER_NOT_FOUND`.

### Admin

`POST /admin/coupons` (no body) issues the coupon for the **oldest milestone that has been reached but not yet rewarded**. Milestone *k·n* is reached once *k·n* orders are placed.

`201` returns the coupon:

```json
{"code": "SAVE10-D7D6D4D6C9", "milestone": 2, "percent": 10, "status": "available",
 "redeemed_by_order_id": null, "created_at": "2026-09-27T14:16:12.657+00:00"}
```

Nothing due → 409 `NO_ELIGIBLE_MILESTONE`, for example `{"orders_placed": 2, "next_milestone": 4}`. If several milestones are due (the admin fell behind), each call issues the next one.

`GET /admin/coupons` lists every coupon (same shape, `status` is `available` or `redeemed`). `GET /admin/orders` lists every order.

`GET /admin/report` is read-only. It is computed from one consistent snapshot and reconciles with the two lists above:

```json
{"orders_placed": 3, "gross_revenue_cents": 17246, "total_discount_cents": 399, "net_revenue_cents": 16847,
 "products": [{"product_id": "bottle", "name": "Steel Water Bottle", "quantity_sold": 3, "gross_revenue_cents": 6999},
              {"product_id": "cap", "name": "Baseball Cap", "quantity_sold": 0, "gross_revenue_cents": 0}, "…"],
 "coupons": {"generated": 1, "available": 0, "redeemed": 1},
 "rewards": {"every_n": 2, "percent": 10, "next_milestone": 4, "milestones_awaiting_coupon": 0}}
```

- `gross` = sum of order subtotals (before discounts), `net` = sum of order totals, and `net = gross − total_discount`. `quantity_sold` counts successfully placed orders only.
- `rewards` tells the operator whether `POST /admin/coupons` would succeed.

`PATCH /admin/products/{product_id}` with `{"price_cents": 5499}` and/or `{"inventory": 3}` sets price and/or stock. It exists so reviewers can change price or availability mid-flow. It affects carts and future checkouts only.

## Walkthrough (curl)

With the server running with `COUPON_EVERY_N=2 .venv/bin/uvicorn app.main:app`:

```bash
B=localhost:8000; J='content-type: application/json'
id() { python3 -c 'import sys, json; d = json.load(sys.stdin); print(d.get("id") or d["code"])'; }

CART=$(curl -s -X POST $B/carts | id)
curl -s -X PUT $B/carts/$CART/items/bottle -H "$J" -d '{"quantity": 3}'
curl -s -X PUT $B/carts/$CART/items/hoodie -H "$J" -d '{"quantity": 1}'     # subtotal 11998
curl -s -i -X POST $B/carts/$CART/checkout -H "$J" -d '{"expected_subtotal_cents": 11998}'   # 201
curl -s -i -X POST $B/carts/$CART/checkout -H "$J" -d '{}'                  # 200, Idempotent-Replayed: true
curl -s -X PUT $B/carts/$CART/items/mug -H "$J" -d '{"quantity": 1}'        # 409 CART_ALREADY_CHECKED_OUT

C2=$(curl -s -X POST $B/carts | id); curl -s -X PUT $B/carts/$C2/items/mug -H "$J" -d '{"quantity": 1}'
curl -s -X POST $B/carts/$C2/checkout                                      # 2nd order: milestone reached
CODE=$(curl -s -X POST $B/admin/coupons | id)                              # 201 SAVE10-…
curl -s -X POST $B/admin/coupons                                           # 409 NO_ELIGIBLE_MILESTONE

C3=$(curl -s -X POST $B/carts | id); curl -s -X PUT $B/carts/$C3/items/tshirt -H "$J" -d '{"quantity": 2}'
curl -s -X POST $B/carts/$C3/checkout -H "$J" -d "{\"coupon_code\": \"$CODE\"}"   # discount 399, total 3599
curl -s $B/admin/report
```

Concurrency by hand: 25 carts for the 3 limited sneakers, each checked out twice at once (50 overlapping requests):

```bash
curl -s -X PATCH $B/admin/products/sneakers-ltd -H "$J" -d '{"inventory": 3}'
for i in $(seq 25); do c=$(curl -s -X POST $B/carts | id)
  curl -s -o /dev/null -X PUT $B/carts/$c/items/sneakers-ltd -H "$J" -d '{"quantity": 1}'; echo $c; done > carts.txt
cat carts.txt carts.txt | xargs -P 50 -I{} curl -s -o /dev/null -w "%{http_code}\n" -X POST $B/carts/{}/checkout | sort | uniq -c
#   3 200   <- the duplicate of each winning request, replayed
#   3 201   <- exactly 3 orders
#  44 409   <- INSUFFICIENT_INVENTORY
```

## Layout

```
app/db.py        schema with CHECK/UNIQUE constraints, seed data, transaction helper
app/store.py     all business rules; each public method is one transaction
app/main.py      HTTP routing, request validation, error envelope, config
app/demo.html    demo page
tests/           live-server tests (conftest.py starts uvicorn per test)
DECISIONS.md     invariants, semantics, design decisions, concurrency strategy
```
