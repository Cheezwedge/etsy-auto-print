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
"""


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

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

    def events(self, receipt_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE receipt_id = ? ORDER BY at", (receipt_id,)
        ).fetchall()

    def _log_event(self, receipt_id: int, from_state: str | None, to_state: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO events (receipt_id, at, from_state, to_state, note) VALUES (?, ?, ?, ?, ?)",
            (receipt_id, time.time(), from_state, to_state, note),
        )
