"""SQLite order store — the idempotency backbone.

Every receipt gets exactly one row keyed by receipt_id. The state machine is
defined for the whole project now so the label phase slots in without a
schema migration:

    new -> slip_printed                      (phase 1 stops here)
        -> label_purchased -> label_printed
        -> tracking_posted -> done
    any state -> held                        (needs human attention)

Phase 1 only moves orders new -> slip_printed / held, but records every
transition in `events` so there is a full audit trail per order.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

STATES = (
    "new",
    "slip_printed",
    "label_purchased",
    "label_printed",
    "tracking_posted",
    "done",
    "held",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    receipt_id  INTEGER PRIMARY KEY,
    state       TEXT NOT NULL,
    buyer_name  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    error       TEXT,
    receipt_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL,
    at         REAL NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS labels (
    receipt_id      INTEGER PRIMARY KEY,
    object_id       TEXT,
    carrier         TEXT,
    service         TEXT,
    amount          TEXT,
    currency        TEXT,
    tracking_number TEXT,
    tracking_url    TEXT,
    label_url       TEXT,
    is_test         INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    service_token   TEXT,
    weight_oz       REAL,
    length_in       REAL,
    width_in        REAL,
    height_in       REAL
);
CREATE TABLE IF NOT EXISTS label_attempts (
    receipt_id INTEGER PRIMARY KEY,
    at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_stock (
    listing_id INTEGER PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    quantity   INTEGER,
    level      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""

# Everything save_label() writes, in column order. What was actually shipped
# (service, weight, dimensions, price) is kept so it can be sent to Etsy with
# the tracking number later — the config may have changed by then, so this
# must be a record of the shipment, not something recomputed.
_LABEL_COLUMNS = (
    "object_id",
    "carrier",
    "service",
    "service_token",
    "amount",
    "currency",
    "tracking_number",
    "tracking_url",
    "label_url",
    "weight_oz",
    "length_in",
    "width_in",
    "height_in",
)

# Numeric columns default to NULL when unknown; the text ones to "" so that
# code formatting them into a string never prints "None".
_LABEL_NUMERIC = frozenset({"weight_oz", "length_in", "width_in", "height_in"})

# Columns added after the first release, for databases created before them.
_LABEL_MIGRATIONS = {
    "service_token": "TEXT",
    "weight_oz": "REAL",
    "length_in": "REAL",
    "width_in": "REAL",
    "height_in": "REAL",
}


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns missing from a database created by an older version."""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(labels)")}
        for column, decl in _LABEL_MIGRATIONS.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE labels ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.conn.close()

    def register(self, receipt: dict) -> bool:
        """Insert a receipt as 'new' if unseen. Returns True if newly added."""
        now = time.time()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO orders "
            "(receipt_id, state, buyer_name, created_at, updated_at, receipt_json) "
            "VALUES (?, 'new', ?, ?, ?, ?)",
            (receipt["receipt_id"], receipt.get("name"), now, now, json.dumps(receipt)),
        )
        if cur.rowcount:
            self._log_event(receipt["receipt_id"], None, "new", "first seen in poll")
        self.conn.commit()
        return bool(cur.rowcount)

    def get(self, receipt_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM orders WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()

    def get_receipt_json(self, receipt_id: int) -> dict | None:
        row = self.get(receipt_id)
        return json.loads(row["receipt_json"]) if row and row["receipt_json"] else None

    def in_state(self, state: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders WHERE state = ? ORDER BY created_at", (state,)
        ).fetchall()

    def unfinished(self) -> list[sqlite3.Row]:
        """Orders this program still considers its problem.

        Everything except 'done' — including 'held', which is precisely the
        state an order sits in when someone fulfils it another way.
        """
        return self.conn.execute(
            "SELECT * FROM orders WHERE state != 'done' ORDER BY created_at"
        ).fetchall()

    def all_orders(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()

    def transition(self, receipt_id: int, to_state: str, note: str = "") -> None:
        if to_state not in STATES:
            raise ValueError(f"Unknown state {to_state!r}")
        row = self.get(receipt_id)
        if row is None:
            raise KeyError(f"Receipt {receipt_id} not in store")
        self.conn.execute(
            "UPDATE orders SET state = ?, updated_at = ?, error = ? WHERE receipt_id = ?",
            (to_state, time.time(), note if to_state == "held" else None, receipt_id),
        )
        self._log_event(receipt_id, row["state"], to_state, note)
        self.conn.commit()

    def hold(self, receipt_id: int, reason: str) -> None:
        self.transition(receipt_id, "held", reason)

    def note(self, receipt_id: int, note: str) -> None:
        """Record something that happened to an order without moving it.

        A refund request is the case this exists for: it needs to show up in
        the order's history, but it says nothing about how far the order got.
        """
        row = self.get(receipt_id)
        if row is None:
            raise KeyError(f"Receipt {receipt_id} not in store")
        self._log_event(receipt_id, row["state"], row["state"], note)
        self.conn.commit()

    # -- labels (phase 2) ---------------------------------------------------

    def record_label_attempt(self, receipt_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO label_attempts (receipt_id, at) VALUES (?, ?)",
            (receipt_id, time.time()),
        )
        self.conn.commit()

    def label_attempted(self, receipt_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM label_attempts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            is not None
        )

    def clear_label_attempt(self, receipt_id: int) -> None:
        self.conn.execute("DELETE FROM label_attempts WHERE receipt_id = ?", (receipt_id,))
        self.conn.commit()

    def save_label(self, receipt_id: int, **fields) -> None:
        unknown = set(fields) - set(_LABEL_COLUMNS) - {"is_test"}
        if unknown:
            raise TypeError(f"save_label got unknown field(s): {', '.join(sorted(unknown))}")
        columns = ("receipt_id", *_LABEL_COLUMNS, "is_test", "created_at")
        values = (
            receipt_id,
            *(fields.get(c, None if c in _LABEL_NUMERIC else "") for c in _LABEL_COLUMNS),
            int(fields.get("is_test", False)),
            time.time(),
        )
        placeholders = ", ".join("?" * len(columns))
        self.conn.execute(
            f"INSERT OR REPLACE INTO labels ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()

    def get_label(self, receipt_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM labels WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()

    # -- listing stock ------------------------------------------------------

    def stock_levels(self) -> dict[int, str]:
        """listing_id -> the level we last alerted at, so repeats stay quiet."""
        return {
            row["listing_id"]: row["level"]
            for row in self.conn.execute("SELECT listing_id, level FROM listing_stock")
        }

    def stock_state(self) -> dict[int, tuple[str, str, int | None]]:
        return {
            row["listing_id"]: (row["level"], row["title"], row["quantity"])
            for row in self.conn.execute(
                "SELECT listing_id, level, title, quantity FROM listing_stock"
            )
        }

    def save_stock_state(self, state: dict[int, tuple[str, str, int | None]]) -> None:
        now = time.time()
        self.conn.executemany(
            "INSERT OR REPLACE INTO listing_stock "
            "(listing_id, level, title, quantity, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(lid, level, title, qty, now) for lid, (level, title, qty) in state.items()],
        )
        self.conn.commit()

    def events(self, receipt_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE receipt_id = ? ORDER BY at", (receipt_id,)
        ).fetchall()

    def _log_event(self, receipt_id: int, from_state: str | None, to_state: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO events (receipt_id, at, from_state, to_state, note) VALUES (?, ?, ?, ?, ?)",
            (receipt_id, time.time(), from_state, to_state, note),
        )
