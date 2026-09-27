"""SQLite schema, seed data and transaction helpers.

Every invariant has a constraint here as the last line of defence, so a bug in
application code fails loudly instead of corrupting data.
"""
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    inventory   INTEGER NOT NULL CHECK (inventory >= 0)          -- never oversell
);

CREATE TABLE IF NOT EXISTS carts (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'checked_out')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cart_items (
    cart_id    TEXT NOT NULL REFERENCES carts(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (cart_id, product_id)
);

CREATE TABLE IF NOT EXISTS coupons (
    code       TEXT PRIMARY KEY,
    milestone  INTEGER NOT NULL UNIQUE CHECK (milestone > 0),    -- one coupon per milestone
    percent    INTEGER NOT NULL CHECK (percent BETWEEN 1 AND 100),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,
    cart_id          TEXT NOT NULL UNIQUE REFERENCES carts(id),   -- one order per cart
    coupon_code      TEXT UNIQUE REFERENCES coupons(code),        -- one redemption per coupon
    discount_percent INTEGER NOT NULL DEFAULT 0,
    subtotal_cents   INTEGER NOT NULL CHECK (subtotal_cents >= 0),
    discount_cents   INTEGER NOT NULL CHECK (discount_cents BETWEEN 0 AND subtotal_cents),
    total_cents      INTEGER NOT NULL CHECK (total_cents = subtotal_cents - discount_cents),
    created_at       TEXT NOT NULL
);

-- Immutable snapshot of what was bought and at what price.
CREATE TABLE IF NOT EXISTS order_lines (
    order_id         TEXT NOT NULL REFERENCES orders(id),
    product_id       TEXT NOT NULL,
    name             TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents INTEGER NOT NULL CHECK (line_total_cents = unit_price_cents * quantity),
    PRIMARY KEY (order_id, product_id)
);
"""

SEED_PRODUCTS = [
    ("tshirt", "Cotton T-Shirt", 1999, 100),
    ("mug", "Ceramic Mug", 1250, 50),
    ("hoodie", "Zip Hoodie", 4999, 25),
    ("cap", "Baseball Cap", 1575, 40),
    ("bottle", "Steel Water Bottle", 2333, 60),
    ("sneakers-ltd", "Limited Edition Sneakers", 12999, 3),
]


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves.
    # timeout: how long BEGIN IMMEDIATE waits for another writer before "database is locked".
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: str) -> None:
    """Create the schema and seed products. Idempotent, so it is safe to retry.

    Several worker processes can boot against a new file at once. SQLite refuses (rather
    than waits for) a lock upgrade that could deadlock, so a "locked" error here is retried.
    """
    for attempt in range(50):
        conn = connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")  # readers never block the writer
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT OR IGNORE INTO products (id, name, price_cents, inventory) VALUES (?, ?, ?, ?)",
                SEED_PRODUCTS,
            )
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.1)
        finally:
            conn.close()


@contextmanager
def transaction(path: str, write: bool):
    """One connection, one transaction. Commits on success, rolls back on any exception.

    Writes use BEGIN IMMEDIATE: the write lock is taken up front, so read-check-write
    sequences never interleave and never fail halfway on a lock upgrade.
    Reads use a deferred BEGIN, which in WAL mode is a consistent snapshot.
    """
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
