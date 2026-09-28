# Decisions

**Time spent:** roughly 2 hours. About 40 min of that was building and testing (the commit history shows it). The rest went into reading the brief, thinking through the cases below and reviewing.

Most of the brief is plain CRUD. What actually matters is what happens when requests overlap: a retry that lands while the first attempt is still running, 10 people going for the last 3 pairs of sneakers, two checkouts using the same coupon, an admin hitting generate twice. I work on payments, and a retry that charges someone twice is the bug I'm most paranoid about. So I started from the invariants, made sure the database itself enforces each one, and then wrote tests that try to break them with concurrent requests.

## 1. Invariants

Every invariant has a check in the code and a backstop in the schema. If the code has a bug, the DB rejects the write instead of quietly storing bad data.

| # | Invariant | Enforced by | Schema backstop | Test |
|---|---|---|---|---|
| I1 | Never oversell stock | checkout runs under the write lock, checks all lines, then `UPDATE ... SET inventory = inventory - q WHERE id = ? AND inventory >= q` (`checkout` in [store.py](app/store.py)) | `CHECK (inventory >= 0)` | `test_concurrent_checkouts_never_oversell...` |
| I2 | One cart gives at most one order | first statement of checkout claims the cart: `UPDATE carts SET status='checked_out' WHERE id=? AND status='open'` | `orders.cart_id UNIQUE` | `test_duplicate_checkout_requests...` |
| I3 | A retry never creates a second order or takes stock twice | if the claim matches 0 rows, `_replay` returns the stored order | I2 | same test |
| I4 | A failed checkout uses up nothing (stock, coupon, cart all untouched) | whole checkout is one transaction, any `ApiError` rolls it back | not needed | `test_failed_checkout_keeps_the_coupon_and_the_stock`, plus the oversell test (losing carts don't leak stock) |
| I5 | A coupon is redeemed at most once | checked inside the locked transaction (`_redeemable_percent`) | `orders.coupon_code UNIQUE`, so inserting the order is the redemption | `test_coupon_is_redeemed_by_exactly_one...` |
| I6 | At most one coupon per milestone, and only once it's reached | `generate_coupon` counts orders under the write lock | `coupons.milestone UNIQUE` | `test_concurrent_coupon_generation...` |
| I7 | An order can explain its own total | order lines snapshot name, unit price and quantity at checkout | `CHECK line_total = unit_price * quantity`, `CHECK total = subtotal - discount` | `test_price_change_before_checkout...` |
| I8 | Discount is deterministic and the total is never negative | `discount_cents()` rounds down and caps at the subtotal | `CHECK discount_cents BETWEEN 0 AND subtotal_cents` | `test_discount_rounds_down...` |
| I9 | A checked-out cart never changes | every cart write does `... WHERE status = 'open'` (`_touch_open_cart`) | none | `test_cart_edit_racing_checkout...` |
| I10 | Bad products or quantities never get into a cart | strict pydantic types (`StrictInt`, 1-1000, `extra="forbid"`) plus product and stock checks | `CHECK (quantity > 0)`, foreign keys | `test_invalid_input_never_enters_a_cart` |
| I11 | The report matches orders and coupons, and reading it changes nothing | aggregates over the same rows the list endpoints return, in one read snapshot. Coupon status is derived, not stored | none, it only runs SELECTs | `test_report_reconciles...` |

## 2. Ambiguities and what I went with

- **Price changes between add-to-cart and checkout.** The cart shows live prices and checkout charges the current price. The client can send `expected_subtotal_cents` (the subtotal it showed the customer), and if prices moved it gets 409 `PRICE_CHANGED` instead of a charge the customer never saw. See D5.
- **Stock changes before checkout.** No reservation. Adding more than current stock is rejected at add time so the customer finds out early, but checkout is the real check. It lists every short line and leaves the cart open. See D6.
- **What counts as a "successfully placed order".** A committed row in `orders`. A failed checkout leaves no row, a replayed retry is the same row, and orders that used a coupon count too.
- **When the k-th milestone is reached.** Once at least k*n orders exist. Coupons aren't issued automatically, the admin asks for them. Due milestones never expire, so if the admin is behind, each call issues the oldest one still waiting. See D7.
- **Who can use a coupon.** Whoever has the code. There's no customer identity in this system. One coupon per order, no expiry, and the percent is fixed when the coupon is generated.
- **Unknown or already used coupon at checkout.** Checkout fails (422 or 409). I didn't want to silently drop the coupon and charge full price, since the customer asked for the discounted order.
- **What the discount applies to.** The order subtotal, not each line. See section 5.
- **A retry that doesn't match the original.** Same cart and same coupon (or no coupon both times) is the same request, so it replays. A different coupon gets 409 `CART_ALREADY_CHECKED_OUT` with the existing `order_id`. `expected_subtotal_cents` isn't compared on a replay, because once the order exists the order is the source of truth.
- **`n` changing between restarts.** Only applies going forward. Each coupon stores the order count that earned it, and the next milestone is the last rewarded one plus the current `n`. Coupons already issued stay as they are.
- **Quantity 0.** 422, use `DELETE` to remove a line. Deleting a line that isn't in the cart returns 200 and changes nothing, so retries are fine.
- **The cart after checkout.** Frozen. `GET` still works and includes `order_id`. The order is the record of what was bought.
- **Currency.** One implicit currency, integer cents.

## 3. Design decisions

### D1: SQLite with explicit transactions, not in-memory or Postgres

**Context:** Pretty much every rule here is "check, then write", and it has to hold when requests overlap. I wanted real atomicity that I can walk someone through, and a setup a reviewer can run with one command.

**Options considered:**
- In-memory dicts plus a lock. No setup, but I'd be hand-rolling locking and rollback for every rule. That shows mutex discipline, not what I'd actually ship.
- Postgres in Docker. Real row locks, but reviewers need Docker and the tests need a live DB.
- SQLite in WAL mode through the stdlib `sqlite3`, no ORM.

**Choice:** SQLite, raw SQL, one explicit transaction per operation.

**Why:** Real transactions, rollback and constraints, and real concurrency through uvicorn's thread pool (even across worker processes), with nothing extra to install. No ORM means every guard is one SQL statement I can point at. I also didn't want any invariant to depend only on SQLite's global write lock, so each one also has a conditional write or a constraint that would still hold on Postgres READ COMMITTED (section 8).

**Consequences:** All writes are serialised across the whole DB. Fine at this scale, but it's a throughput ceiling. Works across processes on one machine, not across machines.

### D2: The cart is the idempotency key

**Context:** Clients retry checkout when they time out. A retry must not create a second order or take stock twice, and that includes a retry that shows up while the first attempt is still running.

**Options considered:**
- An `Idempotency-Key` header, storing the request hash and the response (the Stripe approach).
- Use the cart itself as the key. The brief already says a cart can only be checked out once.

**Choice:** The cart. The claim `UPDATE carts SET status='checked_out' WHERE id=? AND status='open'` either wins, or matches 0 rows and replays the stored order (`200`, `Idempotent-Replayed: true`). Whether a retry is "the same request" is decided by the normalised coupon code.

**Why:** It works for every client, including ones that forget to send a header, and there's no extra table, no key expiry and no in-progress state to manage. A duplicate that arrives mid-checkout just waits for the lock and then replays. The domain already gives me this guarantee, so a header would just be more moving parts.

**Consequences:** Only checkout works this way. `POST /carts` isn't idempotent, but a retry only creates an extra empty cart, which is harmless. Cart edits are idempotent by design instead (D8). If a cart ever had to produce more than one order, I'd switch to a header.

### D3: One claim-first transaction under `BEGIN IMMEDIATE`

**Context:** Checkout touches four things (cart state, stock, coupon, order) and has to be all or nothing, even under concurrency.

**Options considered:**
- Validate outside a transaction, then write. That races.
- A deferred `BEGIN` (SQLite's default), which reads first and upgrades to a write later.
- `BEGIN IMMEDIATE`, which takes the write lock up front.

**Choice:** `BEGIN IMMEDIATE`. The cart claim is the first statement, then a full stock check, then conditional decrements in product-id order, then the order row (which is also the coupon redemption), then commit. Any failure rolls the whole thing back.

**Why:** In a deferred transaction, if the read to write upgrade could deadlock, SQLite doesn't wait, it refuses straight away. I checked this by switching to a deferred `BEGIN` (section 4): coupon generation, which reads before it writes, started returning 503s under concurrency. With `IMMEDIATE`, waiting for the lock is just the normal path. Putting the claim first means a duplicate checkout is settled by the very first statement, and on a DB with row locks that same claim is what serialises checkout against edits to the same cart.

**Consequences:** The guards are layered: the lock, a pre-check that builds the full error list, then the conditional write and the constraint. Remove any one of them and another still catches the bug. The planted-bug runs showed the conditional decrement is redundant on SQLite. I kept it on purpose for the Postgres case (section 8).

### D4: The order row is the coupon redemption, and coupon status is derived

**Context:** A coupon can be redeemed once, must not get burned by a checkout that fails, and the report has to add up.

**Options considered:**
- A `status` column on coupons, flipped with a conditional `UPDATE` next to the order insert.
- `orders.coupon_code UNIQUE`, where a coupon counts as used when an order references it.

**Choice:** The second.

**Why:** One source of truth. If there are two records of whether a coupon is used, a bug can make them disagree. With one, it can't. A UNIQUE index is the concurrency guard on every major DB. Since the redemption is part of the order insert, a checkout that rolls back can't leave a coupon half used. The report's `redeemed` and `available` counts come straight from orders, so they always match `/admin/orders`.

**Consequences:** Reading a coupon's status needs a join. If cancellations come in later, un-redeeming becomes a real modelling question (does a refund give the coupon back?), not just flipping a flag.

### D5: Live cart prices, plus an optional price check from the client

**Context:** The brief asks what should happen when price or stock changes between adding an item and checking out.

**Options considered:**
- Lock the price when the item is added. Carts never expire, so a stale or wrong price could be honoured forever.
- Silently charge the current price. The customer might pay something they never saw.
- Charge the current price, but let the client say what subtotal it showed, and refuse if it doesn't match.

**Choice:** The third, through `expected_subtotal_cents`.

**Why:** The store stays in control of its prices and the customer never gets a surprise charge. The check compares against the same `subtotal_cents` that `GET /carts/{id}` returns, so the client doesn't need to know how the discount is calculated. The first version compared the post-discount total instead, which was wrong (section 9).

**Consequences:** A client that leaves the field out just gets the current price. I kept it optional so simple clients stay simple, and the README recommends sending it.

### D6: No stock reservation at add-to-cart

**Context:** Stock can change while items sit in a cart.

**Options considered:**
- Reserve stock when the item is added, with a TTL.
- Check at add time so the customer finds out early, and enforce only at checkout.

**Choice:** No reservation. The add-time check is just a heads-up, checkout decides.

**Why:** Reservations need expiry, a cleanup job and rules for abandoned carts. With no customer identity it would also be trivial for anyone to lock up all the limited sneakers. Payment is instant here, so holding stock doesn't buy anything.

**Consequences:** Someone can lose the last unit between cart and checkout. When that happens they get a precise `INSUFFICIENT_INVENTORY` with every short line, and the cart stays open. Once there's a real payment step, a short hold during payment becomes necessary (section 8).

### D7: Admin triggers generation, one coupon per call, oldest milestone first

**Context:** The brief says "An administrator can request coupon generation", and only for a milestone that's been reached and not yet rewarded.

**Options considered:**
- Issue the coupon automatically inside whichever checkout crosses a milestone.
- Admin-triggered, issue every due coupon in one call.
- Admin-triggered, one coupon per call for the oldest due milestone.

**Choice:** The third. A milestone is stored as the order count that earned it (`coupons.milestone`, UNIQUE), and the next one is `max(milestone) + n`.

**Why:** It follows the brief as written, and checkout doesn't need to know anything about rewards. One per call keeps the response simple: you get one coupon, or a 409 saying why not, and the report shows how many are still pending. Storing the order count makes each coupon self-explanatory ("earned at order 10") and means a change to `n` only applies going forward.

**Consequences:** Generation isn't idempotent across retries. If two milestones are due and the admin's first call times out, the retry issues the second milestone's coupon. No milestone ever gets two coupons, so the invariant holds, but an operator might not expect it. It's on my list for the next two hours (section 10).

### D8: One `PUT` with an absolute quantity for both add and update

**Context:** Cart edits get retried too.

**Options considered:**
- `POST .../items` to add (incrementing) and `PATCH` to change.
- A single `PUT /carts/{id}/items/{product_id}` with `{"quantity": n}`.

**Choice:** `PUT` with an absolute quantity. `DELETE` removes, and is idempotent too.

**Why:** An increment that gets retried after a lost response doubles the line. Setting a value is idempotent on its own. It also answers what "add" should do when the product is already in the cart.

**Consequences:** A client that wants "+1" needs the current quantity. It always has it, since every cart endpoint returns the cart.

### D9: No fake payment gateway

**Context:** The brief says to treat a successful checkout as payment success, or add a small payment abstraction or fake.

**Options considered:**
- A `PaymentGateway` interface with a fake that always succeeds.
- A fake that can be made to fail, plus a compensation path.
- No payment step: a committed checkout is the successful payment.

**Choice:** No payment step.

**Why:** A fake that always succeeds is extra code that tests nothing. A fake that can fail only makes sense with the real design around it: a PENDING order, stock and coupon held, the gateway called outside the DB transaction with an idempotency key, then confirm or release, plus a sweeper. That's a project on its own, and a half-built version would be the least correct part of the codebase. So I've written the real design down in section 8 instead of faking it.

**Consequences:** Checkout stays one short transaction here. The production version changes how checkout is structured, but not its invariants.

### D10: Tests hit a real server, and I checked that they can actually fail

**Context:** The brief says happy-path tests aren't enough and that the service may be hit with concurrent requests.

**Options considered:**
- Test the store functions directly.
- FastAPI's in-process `TestClient`.
- Start uvicorn on a random port for each test and fire requests from separate threads and connections, all released together with a `threading.Barrier`.

**Choice:** The live server.

**Why:** Only a real server pushes overlapping requests through the actual thread pool, connection handling and error mapping. And a concurrency test passing doesn't mean much unless it also fails against broken code, so I planted bugs and made sure each one got caught (section 4).

**Consequences:** The suite takes about 2s. It passed 15 runs in a row. The edit vs checkout race is a genuine race: over 200 tries, checkout won 113 and the edit won 87.

## 4. Transactions, concurrency and idempotency

**Unit of work.** Every public `Store` method is exactly one transaction on its own connection (`transaction` in [db.py](app/db.py)).
- Writes use `BEGIN IMMEDIATE`. It holds the DB write lock for the whole transaction and waits up to 5s to get it. If it gives up, nothing was written, so the API returns 503 `SERVICE_BUSY` with `Retry-After` and a retry is safe.
- Reads use a deferred `BEGIN`. In WAL mode that's a consistent snapshot that never blocks the writer and never waits for it. The report runs inside one.

**Checkout, step by step** (`Store.checkout`):
1. Claim the cart: `UPDATE carts SET status='checked_out' ... WHERE status='open'`. If 0 rows match, the cart either doesn't exist (404) or is already checked out, in which case it's a replay or a 409.
2. Load the lines at current prices. Empty cart is a 422.
3. Check every line against stock. If any are short, 409 listing all of them.
4. Compute the subtotal. If it doesn't match `expected_subtotal_cents`, 409 `PRICE_CHANGED`.
5. Check the coupon: 422 if unknown, 409 if already used. Compute the discount, rounded down.
6. Insert the order. Because of the UNIQUE, this is also the coupon redemption.
7. For each line, in product-id order: decrement stock conditionally (0 rows means 409), then snapshot the line.
8. Commit. An exception anywhere in steps 2 to 7 rolls everything back to how it was before step 1.

**Retries and timeouts.** A client that times out never sees a half-done checkout. Either the transaction committed and the retry replays it, or it didn't and the retry runs fresh. That holds whether the retry comes after the first attempt finished or while it's still holding the lock.

| Operation | What a retry does |
|---|---|
| `POST /carts/{id}/checkout` | replays the same order (200). A different coupon gets 409 |
| `PUT .../items/{p}` | sets the same value again, same result |
| `DELETE .../items/{p}` | nothing the second time |
| `POST /carts` | creates another empty cart (harmless) |
| `POST /admin/coupons` | issues the next due milestone's coupon if there is one, otherwise 409. Never two coupons for one milestone |
| any `GET` | read only |

**Checking the tests catch real bugs.** I planted each bug in a copy of the code and ran the suite 3 times:

| Planted bug | Result |
|---|---|
| No transactions (every statement commits on its own) | caught by 6 tests. For example, a failed checkout leaves its cart claimed with no order, so the cart is stuck |
| Deferred `BEGIN` instead of `IMMEDIATE` | caught, concurrent coupon generation gets 503s from refused lock upgrades |
| Every stock guard removed (no transaction, pre-check, conditional or CHECK) | caught, 10 orders for 3 units and stock at -7 |
| No transaction and no coupon pre-check (with or without the UNIQUE) | caught by the coupon race test |
| Checkout claim ignores `status` | caught, retries hit `UNIQUE(cart_id)` and get a 500 instead of a replay |
| Replay ignores the coupon | caught |
| Rounding half-up instead of down | caught |
| Milestone off by one | caught |
| Cart edits ignore `checked_out` | caught by the race test and the validation test |
| Only the conditional decrement removed | not caught, and that's expected. Under the write lock the pre-check already stops it. It's the guard for a DB without a global lock (section 8) |

**Outside the test suite.** I also created 25 carts for the 3 sneakers and checked each one out twice at the same time, so 50 overlapping curl requests. Ran that against one worker, then against `uvicorn --workers 4` on 5 fresh DBs. Every run ended with exactly 3 orders (201), 3 replays (200), 44 x 409, and stock at 0. The first multi-worker run found a real startup bug (section 9).

## 5. Money and rounding

- **Integer cents everywhere:** `INTEGER` columns, Python ints, JSON integers. The API rejects `3.0` as a quantity (`StrictInt`) instead of converting it. A float never touches a price.
- **Lines:** `line_total = unit_price * quantity`, exactly, enforced by a `CHECK`. The subtotal is the sum of the lines.
- **Discount:** computed once, on the order subtotal: `subtotal * percent // 100` (integer floor), capped at the subtotal.
  - Rounding down means the store never gives more than the advertised percentage. Worst case the customer misses out on less than a cent.
  - It's deterministic and line order doesn't matter.
  - I didn't discount each line separately, because the per-line amounts rounded down don't add up to the whole-order amount rounded down, and then I'd need a rule for where the leftover cent goes.
- **Percent:** an integer from 1 to 100, validated at startup, fixed on the coupon and recorded on the order (`discount_percent`), so every order has what it needs to redo its own maths.
- **Never negative:** the cap, plus `CHECK (discount BETWEEN 0 AND subtotal)`, plus `CHECK (total = subtotal - discount)`.
- **Bounds:** quantity <= 1000 and price <= 10^8 cents keep every sum far below 64-bit overflow.
- **Report:** only adds integers, so `net = gross - discounts` holds exactly. The report test checks it.

## 6. Error model

Everything goes through one envelope, `{"error": {"code", "message", "details"}}`, including framework validation errors, unknown routes and lock timeouts. Clients should branch on `code`, which is stable, and never on `message`. The full table is in the README.

What each status means:
- **404:** whatever is in the URL doesn't exist.
- **409:** the request is fine, but the current state doesn't allow it. Change the state and try again.
- **422:** this request can never work as written.
- **503:** temporary, and nothing was written.
- **500:** a bug. The response doesn't leak anything.

That's why `COUPON_NOT_FOUND` is a 422 and not a 404: the cart in the URL exists, the bad reference is in the body.

A few other choices:
- **Details you can act on.** `INSUFFICIENT_INVENTORY` lists every short line with requested and available amounts, so the client can fix the cart in one go. `CART_ALREADY_CHECKED_OUT` includes `order_id`, so a client that lost its response can still find its order. `PRICE_CHANGED` includes the new subtotal. `NO_ELIGIBLE_MILESTONE` tells the admin how far off the next milestone is.
- **A replay isn't an error.** It returns 200 with the order, plus a header for clients that care, so a simple retry loop just works.
- **Coupon errors don't reveal** who used the coupon.
- **Constraint violations the lock makes impossible** (a duplicate `coupon_code`, for example) would come back as a 500. The data stays correct, and a 500 there means a bug, not bad input. On Postgres they become real races and need to map to 409 (section 8).

## 7. What I built and what I left out

**Built:**
- Every endpoint the brief asks for, plus `GET /products`, `GET /admin/orders` and `GET /admin/coupons` (so the report can be checked against them) and `PATCH /admin/products/{id}` (to change price and stock mid-test).
- Every invariant in section 1, with 10 tests (5 of them concurrent) and the planted-bug check.
- A small demo page and the OpenAPI docs.

**Left out on purpose:**

| Left out | Why it's OK for now, and what adding it would take |
|---|---|
| Authentication and authorization | Out of scope per the brief. `/admin/*` is the part to protect. |
| Customer identity, per-customer coupons, coupon expiry | There are no customers in the domain yet. Coupons are bearer codes (40 random bits), so a real version needs rate limiting against guessing. |
| Real payments, holds and reservations | Design is in section 8. It turns checkout from one transaction into a small state machine. |
| Cancellations and refunds | Need a rule for whether a refund gives the coupon back or un-counts a milestone, plus a way to split the discount across lines. |
| Pagination on the admin lists | `list_orders` runs one query per order (marked "Known limit" in the code). Fine at this size. |
| Cleaning up abandoned carts | Carts are tiny rows. A TTL job later. |
| Schema migrations | `CREATE TABLE IF NOT EXISTS` is enough for a fresh DB to review. Add a migration tool once the schema has to change. |
| Metrics and structured logs | First thing I'd add before running this for real: lock wait time, 503 rate, checkout latency. |
| Tax and multiple currencies | Not in the brief. |

## 8. Multiple instances and production scale

**Right now.** The SQLite file stays correct across threads and processes on one machine (checked with 4 workers on 5 fresh boots). It isn't safe across machines, a SQLite file on network storage isn't a shared database. The app itself is already stateless, no in-process locks or caches, so scaling out means changing the database, not redesigning the app.

**On Postgres (READ COMMITTED), the same statements still enforce the invariants:**
- **Cart claim:** `UPDATE carts ... WHERE id=$1 AND status='open'` takes a row lock, so a duplicate checkout or a cart edit waits on it. When the winner commits, Postgres re-checks `status='open'` against the new version of the row, gets 0 rows, and the duplicate replays or the edit is refused. Nothing to change.
- **Stock:** `UPDATE products SET inventory = inventory - $q WHERE id = $p AND inventory >= $q` is re-checked after any lock wait, so no oversell. This is the guard that went uncaught in the SQLite bug runs. On Postgres it's the one actually doing the work. Decrements already happen in product-id order, so two multi-line carts can't deadlock each other.
- **Coupon:** the second insert with the same `coupon_code` waits on the unique index until the first commits, then fails with a unique violation. **To change:** map that to 409 `COUPON_ALREADY_REDEEMED`. The pre-check there is only for a nicer error message.
- **Coupon generation:** two calls at once can both pick the same milestone, and `UNIQUE(milestone)` rejects the second. **To change:** map that to 409, or retry once. If generation races a checkout, the worst case is it says "not yet", which is the safe side to be wrong on.
- **Reads:** the report moves into one `REPEATABLE READ` transaction so it still reads from a single snapshot.

**At bigger scale:**
- **Payments.** Checkout becomes a small state machine:
  1. In one transaction: create a `PENDING` order, decrement stock and redeem the coupon.
  2. Call the payment provider outside any transaction, with the order id as the idempotency key.
  3. Mark the order `PLACED`, or mark it `FAILED` and give back the stock and coupon in one compensating transaction.

  A sweeper settles anything stuck in `PENDING` by asking the provider. Only `PLACED` orders count toward milestones. Webhooks are deduplicated by the provider's event id.
- **Hot products.** A flash sale serialises on one product row. What I'd try, in this order: keep transactions short (they already are), split the stock across N bucket rows, then put a queue in front of checkout that hands out reservation tokens.
- **Counting orders.** `COUNT(*)` for milestones gets expensive. A `reward_state` counter row updated in checkout is exact, but it becomes its own hot row. Better to derive the milestone from order sequence numbers, or keep the count updated async. Generation is admin-triggered, so a little lag is fine.
- **Reporting.** Move it to a read replica, or build a read model from an outbox of order events. You lose exact real-time numbers but keep the load off checkout. I'd keep the exact query as a nightly reconciliation job.
- **Other POSTs.** If clients need `POST /carts` to be retry-safe, add an `Idempotency-Key` table for that endpoint. D2 still holds for checkout.

## 9. How I used AI

I used Claude Code heavily on this. It drafted the plan, most of the code and tests, and the first drafts of this doc and the README. I picked the stack and the scope, went through the design and signed off on it, and I'm responsible for every line. Happy to walk through any of it on the call.

Where I overrode it: it suggested skipping the frontend, since the brief says a frontend won't make up for an unreliable backend. I wanted a small demo page anyway. On a call it's much easier to show 10 checkouts going for 3 sneakers, and a retry coming back as a replay, than to read out test results. I kept it to one static file with no logic of its own, so it can't hide a backend bug.

The checks in the plan (the planted-bug runs and the multi-worker curl burst) caught four mistakes in the generated code:
1. **Startup with several workers.** The generated init assumed a single process. `uvicorn --workers 4` on a fresh DB crashed at boot with `database is locked` from `PRAGMA journal_mode=WAL`, a lock upgrade SQLite refuses rather than waits on. The init is idempotent, so it now retries. Re-checked across 5 fresh boots.
2. **The bug-planting harness.** Its first "no transactions" version broke every test for a trivial reason (`COMMIT` with no open transaction). That would have looked like great coverage while proving nothing. It got rewritten as a proper autocommit version before I trusted any result.
3. **The price check.** The first version compared the client's expected total after the discount, which a client could only know by copying the discount rule. It now compares the subtotal that `GET /carts` returns.
4. **A config fallback.** `every_n or env_default` quietly turned an explicit `0` into the default instead of rejecting it.

## 10. What I'd do with another two hours

1. **Run the same test suite on Postgres.** Section 8 is reasoned through, not tested. I'd add a Postgres fixture, add the two unique-violation to 409 mappings, and run the same concurrent tests. If I'm claiming every guard carries over, I should prove it the same way I did for SQLite.
2. **Make coupon generation retry-safe.** Accept an optional `milestone` in the request and return the existing coupon if that milestone is already rewarded, so an admin retrying after a timeout can't get a different milestone's coupon (D7).
3. **Measure what the global write lock costs.** p50/p99 checkout latency and the 503 rate at 50-200 concurrent writers, then decide whether a 5s busy timeout is right or whether it should be shorter with client-side backoff.
4. **A model-based concurrency test.** Generate random mixes of cart edits, checkouts, coupon use and generation, then check the final state matches some one-at-a-time ordering of the operations that succeeded. That finds the races I didn't think to write tests for.
