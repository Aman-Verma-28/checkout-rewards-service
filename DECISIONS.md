# Decisions

**Approximate time spent:** _TO FILL IN before submission_

The CRUD part of this brief is small. The work is in the moments where two things happen at once: a retry arriving while the first attempt is still running, ten customers buying the last three pairs of sneakers, two checkouts holding the same coupon, an admin clicking "generate" twice. So I started from the invariants, placed each one where the database can enforce it, and wrote tests that attack those invariants with overlapping requests.

## 1. Invariants and where they are enforced

Every invariant has a primary guard in the code path and a backstop in the schema, so a bug fails loudly instead of corrupting data.

| # | Invariant | Primary guard | Backstop | Test |
|---|---|---|---|---|
| I1 | Stock is never oversold | Checkout runs under the write lock, checks every line, then `UPDATE … SET inventory = inventory - q WHERE id = ? AND inventory >= q` ([store.py](app/store.py) `checkout`) | `CHECK (inventory >= 0)` | `test_concurrent_checkouts_never_oversell…` |
| I2 | A cart produces at most one order | Checkout first claims the cart: `UPDATE carts SET status='checked_out' WHERE id=? AND status='open'` | `orders.cart_id UNIQUE` | `test_duplicate_checkout_requests…` |
| I3 | A retry never creates a second order or charges stock twice | A claim that matches 0 rows goes to `_replay`, which returns the stored order | I2 | same |
| I4 | A failed checkout consumes nothing: no stock, no coupon, cart still open | The whole checkout is one transaction; any `ApiError` rolls it back | none needed | `test_failed_checkout_keeps_the_coupon_and_the_stock`, oversell test (losers leak no stock) |
| I5 | A coupon is redeemed at most once | Checked inside the locked transaction (`_redeemable_percent`) | `orders.coupon_code UNIQUE`: the order row *is* the redemption | `test_coupon_is_redeemed_by_exactly_one…` |
| I6 | At most one coupon per milestone, and only once reached | `generate_coupon` counts orders under the write lock | `coupons.milestone UNIQUE` | `test_concurrent_coupon_generation…` |
| I7 | An order explains itself forever | Lines snapshot name, unit price and quantity at checkout | `CHECK line_total = unit_price * quantity`, `CHECK total = subtotal - discount` | `test_price_change_before_checkout…` |
| I8 | Discount is deterministic and the total is never negative | `discount_cents()` = floor, capped at the subtotal | `CHECK discount_cents BETWEEN 0 AND subtotal_cents` | `test_discount_rounds_down…` |
| I9 | A checked-out cart never changes | Every cart write claims the row with `… WHERE status = 'open'` (`_touch_open_cart`) | none | `test_cart_edit_racing_checkout…` |
| I10 | Invalid products or quantities never enter a cart | Strict pydantic types (`StrictInt`, 1–1000, `extra="forbid"`), product existence and stock checks | `CHECK (quantity > 0)`, foreign keys | `test_invalid_input_never_enters_a_cart` |
| I11 | The report reconciles with orders and coupons, and reading it changes nothing | Aggregates come from the same rows the lists return, in one read snapshot. Coupon status is derived, not stored | read-only transaction | `test_report_reconciles…` |

## 2. Ambiguities and the semantics I chose

| Question the brief leaves open | What this service does |
|---|---|
| Price changes between add-to-cart and checkout | The cart shows live prices. Checkout charges the current price. The client can send `expected_subtotal_cents` (what it showed the customer) and gets 409 `PRICE_CHANGED` instead of a charge the customer never saw. See D5. |
| Availability changes before checkout | Nothing is reserved. Adding more than current stock is refused up front for fast feedback, but checkout is authoritative: it lists every short line, and the cart stays open to be fixed. See D6. |
| What counts as a "successfully placed order" | A committed order row. Failed checkouts leave no row; a replayed retry is the same row. Orders that used a coupon count too. |
| When is the *k*-th milestone reached? | When at least *k·n* orders have been placed. Coupons are not issued automatically; the admin asks. Due milestones never expire, and if the admin falls behind, each call issues the oldest one still due. See D7. |
| Who may use a coupon? | Whoever holds the code: there is no customer identity in this system. One coupon per order, no expiry, percent frozen at generation. |
| Unknown or already used coupon at checkout | The checkout fails (422 / 409). It never silently drops the coupon and charges full price, because the customer asked for a discounted order. |
| Discount scope | Whole-order subtotal, not per line. See section 5. |
| A retried checkout that differs from the original | Same cart and same coupon (including none) is the same request: replay. Different coupon: 409 `CART_ALREADY_CHECKED_OUT` with the existing `order_id`. `expected_subtotal_cents` is not part of the match: once the order exists, it is the truth. |
| `n` changes between restarts | Forward-only. Coupons store the order count that earned them, and the next milestone is the last rewarded one + the current `n`. Existing coupons are unaffected. |
| Quantity 0 | 422; removal is `DELETE`. Deleting a line that isn't in the cart is a 200 no-op, so retries are safe. |
| Cart after checkout | Frozen. `GET` still works and carries `order_id`; the order is the record. |
| Currency | One implicit currency in integer cents. |

## 3. Material decisions

### Decision D1: SQLite with explicit transactions, not an in-memory store or Postgres

**Context:** The invariants are all "check then write" rules that must hold when requests overlap. I needed something where the atomicity is real and visible in an interview, and cheap for a reviewer to run.

**Options considered:**
- In-memory dicts with a lock. Zero setup, but every invariant becomes hand-written locking with no atomic rollback. I'd be demonstrating mutex discipline rather than the design that would ship.
- Postgres via Docker. Production-like row locks, but reviewers need Docker, and tests need a running database.
- SQLite in WAL mode via stdlib `sqlite3`, no ORM.

**Choice:** SQLite, with raw SQL and one explicit transaction per operation.

**Why:** Real transactions and rollback, real constraints, true concurrency through uvicorn's thread pool, and even across worker processes, all with nothing to install. Without an ORM, each guard is a single SQL line I can point at. I also required that no invariant *depends* only on SQLite's global write lock: each has a conditional write or a constraint that would still hold on Postgres READ COMMITTED (section 8).

**Consequences:** Writes are serialised database-wide. That is fine for this scale but a throughput ceiling (section 8). It works across processes on one host, but not across hosts.

### Decision D2: The cart is the idempotency key

**Context:** Clients retry checkout after timeouts. A retry must not create a second order or charge stock twice, including when the retry arrives while the first attempt is still running.

**Options considered:**
- An `Idempotency-Key` header with a stored request hash and response (Stripe-style).
- The cart itself as the natural key, since the brief already says a cart can be checked out only once.

**Choice:** The cart. The claim `UPDATE carts SET status='checked_out' WHERE id=? AND status='open'` either wins, or matches 0 rows and replays the stored order (`200`, `Idempotent-Replayed: true`). The fingerprint that decides "same request" is the normalised coupon code.

**Why:** It protects every client, including ones that forget to send a header, with no extra table, no key expiry and no in-progress state. A concurrent duplicate simply waits for the lock and then replays. A header would add machinery for a guarantee the domain already gives.

**Consequences:** Only checkout gets this treatment. `POST /carts` is not idempotent, which is harmless because a retry makes an extra empty cart. Cart edits are made idempotent by design instead (D8). If one cart ever needed several orders, this would have to change to a header.

### Decision D3: Checkout is one claim-first transaction under `BEGIN IMMEDIATE`, with guards that don't need the lock

**Context:** Checkout touches four things (cart state, stock, coupon, order) and must be all-or-nothing, under concurrency.

**Options considered:**
- Validate outside a transaction, then write. Racy.
- A deferred `BEGIN` (SQLite's default). This takes a read snapshot first and upgrades to a write later.
- `BEGIN IMMEDIATE`, which takes the write lock up front.

**Choice:** `BEGIN IMMEDIATE`, the cart claim as the first statement, a full stock check, conditional decrements in product-id order, then the order row (which is the coupon redemption), then commit. Any failure rolls everything back.

**Why:** With a deferred transaction, SQLite refuses a read-to-write upgrade that could deadlock, and fails immediately without waiting. I confirmed this by planting it (section 4): coupon generation, which reads before it writes, started returning 503s. `IMMEDIATE` makes waiting for the lock the normal path. Claim-first also means a duplicate checkout is resolved by the first statement, and on a row-locking database the claim is the row lock that serialises a checkout against edits to the same cart.

**Consequences:** Guards are layered: the lock, a pre-check that produces a complete error list, then a conditional write and a constraint. Removing any single layer is caught by another. The mutation check shows the conditional decrement on its own is redundant *here*; it exists for section 8.

### Decision D4: A coupon's redemption is the order row, and its status is derived

**Context:** A coupon must be redeemed once, never lost by a failed checkout, and the report must reconcile.

**Options considered:**
- A `status` column on coupons, flipped with a conditional `UPDATE` alongside the order insert.
- `orders.coupon_code UNIQUE`, with status computed as "an order references this code".

**Choice:** The second.

**Why:** One source of truth. Two copies of "is this coupon used?" can disagree after a bug. One copy cannot. The UNIQUE index is the concurrency guard on every major database, and because redemption is part of the order insert, a checkout that rolls back cannot leave a coupon half-consumed. Reported `redeemed` and `available` counts are computed from the orders, so they reconcile with `/admin/orders` by construction.

**Consequences:** Reading a coupon's status is a join. If cancellations are added, "un-redeeming" becomes a modelling decision (does a refunded order release its coupon?) rather than a flag flip.

### Decision D5: Live cart prices, with an optional client-side price guard

**Context:** The brief asks what happens when price or stock changes between add and checkout.

**Options considered:**
- Snapshot the price when the item is added and honour it. This exposes the store to stale or mistaken prices indefinitely, since carts never expire.
- Charge the current price silently. The customer may pay an amount they never saw.
- Charge the current price, but let the client state the subtotal it displayed, and refuse on mismatch.

**Choice:** The third, via `expected_subtotal_cents`.

**Why:** The store controls its prices, and the customer is never charged a surprise amount. The guard compares against the same `subtotal_cents` that `GET /carts/{id}` returns, so a client doesn't need to know the discount rule to use it. My first draft compared the post-discount total; see section 9.

**Consequences:** A client that omits the field gets "current price" semantics. It is optional, not required, to keep simple clients simple. That is a deliberate trade-off, and the README recommends sending it.

### Decision D6: No inventory reservation at add-to-cart

**Context:** Availability can change while an item sits in a cart.

**Options considered:**
- Reserve stock on add, with a TTL.
- Check at add time for feedback, and enforce only at checkout.

**Choice:** No reservation. The add-time check is advisory; checkout is authoritative.

**Why:** Reservations need expiry, a sweeper, and a rule for abandoned carts. Without customer identity, a reservation is also an easy way for anyone to lock up the limited sneakers. Payment is instant here, so a hold buys nothing.

**Consequences:** A customer can lose the last unit between cart and checkout. They get a precise `INSUFFICIENT_INVENTORY` listing every short line, and the cart stays open. With a real payment step, a short hold during payment becomes necessary (section 8).

### Decision D7: Admin-triggered generation, one coupon per call, oldest milestone first

**Context:** "An administrator can request coupon generation", and only for a reached, unrewarded milestone.

**Options considered:**
- Auto-issue a coupon inside the checkout that crosses a milestone.
- Admin-triggered, issuing all due coupons per call.
- Admin-triggered, issuing one coupon per call for the oldest milestone due.

**Choice:** The third. A milestone is stored as the order count that earned it (`coupons.milestone`, UNIQUE); the next one is `max(milestone) + n`.

**Why:** It follows the brief literally and keeps checkout free of reward side effects. One per call gives a simple response contract, one coupon or a 409 explaining why not, and the report says how many are still due. Storing the order count makes a coupon self-explanatory ("earned at order 10") and makes a change of `n` forward-only.

**Consequences:** Generation is not idempotent across retries. If two milestones are due and the admin's first call times out, a retry issues the *second* milestone's coupon. That is never a duplicate for one milestone, so the invariant holds, but it may surprise an operator (section 10).

### Decision D8: `PUT` with an absolute quantity is both "add" and "change"

**Context:** Cart edits get retried too.

**Options considered:**
- `POST …/items` to add (increment) plus `PATCH` to change.
- One `PUT /carts/{id}/items/{product_id}` with `{"quantity": n}`.

**Choice:** `PUT` with an absolute quantity. `DELETE` removes and is idempotent.

**Why:** An increment retried after a lost response doubles the line. A set is naturally idempotent. It also removes the ambiguity of adding a product that is already in the cart.

**Consequences:** A client that wants "+1" must know the current quantity. It does, because every cart call returns the cart.

### Decision D9: No fake payment gateway

**Context:** "Treat successful checkout as payment success, or introduce a small payment abstraction/fake."

**Options considered:**
- A `PaymentGateway` interface with a fake that always succeeds.
- A fake that can be told to fail, plus a compensation path.
- No payment step: the committed checkout is the successful payment.

**Choice:** No payment step.

**Why:** A fake that always succeeds adds code without testing anything. A fake that fails is only meaningful with the real design around it: a PENDING order, held stock and coupon, a gateway call *outside* the database transaction with an idempotency key, then confirm or release, plus a sweeper. That is a separate project, and a half-built version would be the least correct part of the codebase. I would rather describe it exactly (section 8) than simulate it loosely.

**Consequences:** Checkout can be one short transaction here. The production path changes checkout's shape, not its invariants.

### Decision D10: Test through a real server, and prove the tests can fail

**Context:** The brief warns that happy-path tests are not enough and says the service may be exercised concurrently.

**Options considered:**
- Test the store functions directly.
- Use FastAPI's in-process `TestClient`.
- Start uvicorn on a random port per test, and fire requests from separate threads and connections released together by a `threading.Barrier`.

**Choice:** The live server.

**Why:** Only the live server puts overlapping requests through the real thread pool, connection handling and error mapping. A concurrent test that passes proves little unless it fails against a broken implementation, so I planted bugs and checked that each one is caught (section 4).

**Consequences:** The suite takes about 2 s. It was stable over 15 consecutive runs. The edit-vs-checkout race really is contested: over 200 trials, checkout won 113 and the edit won 87.

## 4. Transaction, concurrency and idempotency strategy

**Unit of work.** Every public `Store` method is exactly one transaction on its own connection ([db.py](app/db.py) `transaction`).
- Writes use `BEGIN IMMEDIATE`, holding the database write lock for the transaction and waiting up to 5 s for it. If the wait times out, nothing has been written, so the API returns 503 `SERVICE_BUSY` with `Retry-After`, and a retry is safe.
- Reads use a deferred `BEGIN`, which in WAL mode is a consistent snapshot that neither blocks nor waits for the writer. The report is computed inside one.

**Checkout, step by step** (`Store.checkout`):
1. **Claim:** `UPDATE carts SET status='checked_out' … WHERE status='open'`. If 0 rows match, the cart is missing (404) or already checked out: replay the order or 409.
2. Load lines with live prices. An empty cart is 422.
3. Check every line against stock. If any are short, 409 with all of them listed.
4. Compute the subtotal. If it differs from `expected_subtotal_cents`, 409 `PRICE_CHANGED`.
5. Validate the coupon: 422 unknown, 409 used. Compute the floor discount.
6. Insert the order, which is the coupon redemption (UNIQUE).
7. For each line in product-id order: conditional decrement (0 rows means 409), then snapshot the line.
8. Commit. Any exception in steps 2–7 rolls back to the state before step 1.

**Retries and timeouts.** A client that times out cannot observe a half-done checkout: the transaction either committed, so a retry replays it, or it didn't, so a retry runs it fresh. That applies whether the retry arrives after the first attempt finished or while it is still holding the lock.

| Operation | Retry behaviour |
|---|---|
| `POST /carts/{id}/checkout` | Replays the same order (200). A different coupon is 409. |
| `PUT …/items/{p}` | Absolute set, so the same result |
| `DELETE …/items/{p}` | No-op the second time |
| `POST /carts` | Creates another empty cart (harmless) |
| `POST /admin/coupons` | Issues the next due milestone's coupon if another is due, otherwise 409. Never two for one milestone. |
| `GET` anything | Read-only |

**Evidence the tests catch real bugs.** I applied each bug to a copy of the code and ran the suite three times:

| Planted bug | Result |
|---|---|
| No transactions (every statement autocommits) | Caught by 6 tests. For example, a failed checkout leaves its cart claimed with no order, so the cart becomes unusable. |
| Deferred `BEGIN` instead of `IMMEDIATE` | Caught. Concurrent coupon generation returns 503 from refused lock upgrades. |
| All stock guards removed (no transaction, pre-check, conditional or CHECK) | Caught. 10 orders for 3 units, stock −7. |
| No transaction + no coupon pre-check (with or without the UNIQUE) | Caught by the coupon race test |
| Checkout claim ignores `status` | Caught. Retries hit `UNIQUE(cart_id)` and get 500 instead of a replay. |
| Replay ignores the coupon fingerprint | Caught |
| Half-up instead of floor rounding | Caught |
| Milestone off by one | Caught |
| Cart edits ignore `checked_out` | Caught by the race test and the validation test |
| Only the conditional decrement removed | **Survives, as expected.** Under the write lock the pre-check already stops it. It is the guard for a database without a global lock (section 8). |

**Beyond the tests.** I ran 25 carts for the 3 sneakers, each cart checked out twice at once (50 overlapping curl requests). I ran it against one worker, then against `uvicorn --workers 4` on 5 fresh databases. Every run produced exactly 3 orders (201), 3 replays (200), 44 × 409 and stock 0. The first multi-worker attempt found a real startup bug (section 9).

## 5. Money and rounding

- **Integer cents, everywhere.** `INTEGER` columns, Python `int`, JSON integers. The API rejects `3.0` as a quantity (`StrictInt`) rather than coercing it. No float ever touches a price.
- **Lines:** `line_total = unit_price × quantity`, exact, enforced by a `CHECK`. The subtotal is the sum of lines.
- **Discount:** computed once on the order subtotal as `floor(subtotal × percent / 100)`, in integer arithmetic (`subtotal * percent // 100`), capped at the subtotal.
  - Floor means the store never grants more than the advertised percentage. The customer loses under one cent at worst.
  - It is deterministic and independent of line order.
  - Per-line discounts were rejected because the sum of per-line floors differs from the floor of the sum, which would need an allocation rule.
- **Percent** is an integer 1–100, validated at startup, frozen on the coupon, and recorded on the order (`discount_percent`), so an order's arithmetic is self-contained.
- **Never negative:** the cap plus `CHECK (discount BETWEEN 0 AND subtotal)` plus `CHECK (total = subtotal − discount)`.
- **Bounds:** quantity ≤ 1000 and price ≤ 10⁸ cents keep every sum far below 64-bit overflow.
- **Report:** sums of integers, so `net = gross − discounts` holds exactly, and the test asserts it.

## 6. Error model

One envelope, `{"error": {"code", "message", "details"}}`, for everything, including framework validation errors, unknown routes and lock timeouts. Clients branch on `code`, which is stable, never on `message`. The full table is in the README.

- **The status class carries meaning.**
  - **404:** the resource in the URL doesn't exist.
  - **409:** valid request, but the current state forbids it. The client can change the state and retry.
  - **422:** this request can never succeed as written.
  - **503:** transient; nothing was written.
  - **500:** a bug, with nothing leaked.

  So `COUPON_NOT_FOUND` is 422, not 404: the cart in the URL exists, and the bad reference is in the body.
- **Details are actionable.** `INSUFFICIENT_INVENTORY` lists *every* short line with requested and available quantities, so the client fixes the cart in one pass. `CART_ALREADY_CHECKED_OUT` carries `order_id`, so a client that lost its response can recover the order. `PRICE_CHANGED` carries the new subtotal. `NO_ELIGIBLE_MILESTONE` tells the admin how far away the next one is.
- **A replay is not an error.** It returns 200 and the order, with a header for clients that care, so naive retry loops just work.
- **Coupon errors don't leak** who redeemed a coupon.
- **Constraint violations the lock makes unreachable** (for example a duplicate `coupon_code`) would surface as 500. The data stays correct, and a 500 there means a bug, not a user error. On Postgres they become reachable races and must map to 409 (section 8).

## 7. Implemented vs deferred

**Implemented:**
- Every required endpoint, plus `GET /products`, `GET /admin/orders`, `GET /admin/coupons` (so the report can be reconciled) and `PATCH /admin/products/{id}` (so price and stock drift can be exercised).
- All invariants in section 1, with 10 tests (5 concurrent) and the mutation check.
- A demo page and OpenAPI docs.

**Deferred, on purpose:**

| Deferred | Why it's safe to defer / what it would take |
|---|---|
| Authentication and authorization | Out of scope per the brief. `/admin/*` is the boundary to protect. |
| Customer identity, per-customer coupons, coupon expiry | No customers exist in the domain yet. Coupons are bearer codes (40 random bits); a production version needs rate limiting against guessing. |
| Real payment, holds and reservations | Section 8 describes the shape. It changes checkout from one transaction into a small state machine. |
| Cancellations and refunds | They need a rule for whether a refund un-redeems a coupon or un-counts a milestone, and per-line discount allocation. |
| Pagination on admin lists | `list_orders` does one query per order (marked `ponytail:` in the code). Fine at this size. |
| Abandoned-cart cleanup | Carts are tiny rows. A TTL job later. |
| Schema migrations | `CREATE TABLE IF NOT EXISTS` is enough for a fresh evaluation database. Use a migration tool once the schema has to evolve. |
| Metrics and structured logs | The first thing I'd add before running this for real (lock wait time, 503 rate, checkout latency). |
| Tax, multi-currency | Not in the brief. |

## 8. Multiple instances and production scale

**Today.** The SQLite file is correct across threads *and processes* on one host: I verified 4 workers across 5 fresh boots. It is not safe across hosts; a SQLite file on network storage is not a shared database. The app layer is already stateless, with no in-process locks or caches, so scaling out is a database change, not an application redesign.

**On Postgres (READ COMMITTED), the same statements carry the invariants:**
- **Cart claim:** `UPDATE carts … WHERE id=$1 AND status='open'` takes a row lock. A duplicate checkout or a cart edit blocks on it. When the winner commits, Postgres re-evaluates `status='open'` against the new row version, gets 0 rows, and the duplicate replays or the edit is refused. No change needed.
- **Stock:** `UPDATE products SET inventory = inventory - $q WHERE id = $p AND inventory >= $q` is re-checked after any lock wait, so there is no oversell. This is the guard that survived the SQLite mutation check: here it is the one doing the work. Decrements already run in product-id order, so two multi-line carts can't deadlock on each other.
- **Coupon:** the second inserter of the same `coupon_code` blocks on the unique index until the first commits, then gets a unique violation. **Change needed:** map that violation to 409 `COUPON_ALREADY_REDEEMED`. The pre-check is only a courtesy there.
- **Coupon generation:** two concurrent calls can both compute the same milestone, and `UNIQUE(milestone)` rejects the second. **Change needed:** map it to 409 (or retry once). A generation call racing a checkout at worst says "not yet", which is conservative.
- **Reads:** the report moves to one `REPEATABLE READ` transaction to keep the single-snapshot property.

**At higher scale:**
- **Payment.** Checkout becomes a small state machine:
  1. In one transaction: create a `PENDING` order, decrement stock, redeem the coupon.
  2. Call the payment provider outside any transaction, with the order id as its idempotency key.
  3. Mark the order `PLACED`, or `FAILED` and release stock and coupon in one compensating transaction.

  A sweeper resolves orders stuck in `PENDING` by asking the provider. Milestones count only `PLACED` orders. Webhooks are deduplicated by provider event id.
- **Hot products.** A flash sale serialises on one product row. Options, in the order I'd try them: keep transactions short (they already are); split stock across N bucket rows; issue reservation tokens from a queue in front of checkout.
- **Order counting.** `COUNT(*)` for milestones gets expensive. A `reward_state` counter row updated in checkout is exact, but becomes its own hot row. Better: derive the milestone from order sequence numbers, or maintain the count asynchronously, since generation is admin-triggered and can tolerate slight lag.
- **Reporting.** Move it to a read replica, or build a read model from an outbox of order events. That trades exact real-time figures for load isolation; I'd keep the exact query as the reconciliation job.
- **Other POSTs.** If clients need `POST /carts` to be retry-safe, add an `Idempotency-Key` table for it. D2 still holds for checkout.

## 9. How I used AI

I used Claude Code (Anthropic's coding agent) throughout.
- **What it did:** read the brief, proposed a plan (stack, schema, API, test list), wrote most of the code, the tests and first drafts of both documents, and ran the verification.
- **What I did:** made the scope and product decisions, reviewed the diffs, and made a rule that every behavioural claim in these documents must come from something that was actually executed: a test, a planted bug, a curl burst.

**Where I overrode it.** It recommended skipping the frontend, because the brief says a frontend "will not compensate for an unreliable backend". I asked for a small demo page anyway. In a live discussion I want to *show* ten checkouts racing for three sneakers, and a retry coming back as a replay, rather than narrate test output. I kept it to one static file with no logic of its own, so it cannot hide backend bugs.

**Where its output was wrong and the verification in the plan caught it:**
1. **Multi-process startup.** The generated init assumed one process. `uvicorn --workers 4` on a fresh database crashed at boot (`database is locked` from `PRAGMA journal_mode=WAL`, a lock upgrade SQLite refuses rather than waits on). Fixed by retrying the idempotent init, then re-verified with 5 fresh boots.
2. **The mutation harness itself.** Its first "no transactions" mutant broke *every* test for a trivial reason (`COMMIT` with no open transaction). That would have looked like strong coverage while proving nothing. It was rewritten as a real autocommit implementation before any result was trusted.
3. **The price guard's contract.** The first version compared the client's expected *post-discount total*, which a client could only compute by reimplementing the discount rule. It now compares the subtotal that `GET /carts` returns.
4. **A config fallback.** `every_n or env_default` silently turned an explicit `0` into the default instead of failing validation.

## 10. With another two hours

1. **Run this test suite against Postgres.** Section 8 is reasoned, not tested. I'd add a Postgres fixture, add the two unique-violation → 409 mappings, and run the same concurrent tests. The claim that every guard is portable should be proven the same way the SQLite claims were.
2. **Make coupon generation retry-safe.** Accept an optional `milestone` in the request, returning the existing coupon if that milestone is already rewarded, so an admin retry after a timeout can't issue a *different* milestone's coupon (D7).
3. **Measure the global write lock.** Find p50/p99 checkout latency and the 503 rate at 50–200 concurrent writers, then decide whether the 5 s busy timeout is right or should be shorter with client backoff.
4. **Model-based concurrency test.** Generate random interleavings of cart edits, checkouts, coupon use and generation, then assert the final state equals *some* sequential ordering of the successful operations. That finds the races I didn't think to write by hand.
