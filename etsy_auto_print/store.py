"""SQLite order store — the idempotency backbone.

Every receipt gets exactly one row keyed by receipt_id. The state machine is
defined for the whole project now so the label phase slots in without a
schema migration:

    new -> slip_printed                      (phase 1 stops here)
        -> label_purchased -> label_printed
        -> tracking_posted -> done
    new -> waiting -> releasing              (label delay; releasing is in-flight)
        -> slip_printed -> ...
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
    "waiting",       # label delay has not elapsed
    "releasing",     # delay elapsed; postage re-check / purchase in flight
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
    receipt_json TEXT,
    placed_at   REAL,
    release_at  REAL
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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

# When the buyer placed the order, and an optional explicit print time.
# release_at NULL means "placed_at plus the configured delay"; 0 means
# print on the next pass (the dashboard's Print now).
_ORDER_MIGRATIONS = {
    "placed_at": "REAL",
    "release_at": "REAL",
}


class Store:
    def __init__(self, path: str | Path):
        # timeout: Print now runs in the dashboard process while the poller
        # may be claiming the same order. The claim itself is a short write;
        # wait for it rather than failing the poll.
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns missing from a database created by an older version."""
        self._add_columns("labels", _LABEL_MIGRATIONS)
        self._add_columns("orders", _ORDER_MIGRATIONS)

    def _add_columns(self, table: str, columns: dict) -> None:
        have = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in columns.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _begin_immediate(self) -> None:
        """Take the write lock before a check-then-update.

        Two processes (the poller and the dashboard) can both decide an
        order is ready. The check and the update have to be one transaction
        or both will buy postage.
        """
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "transaction" not in str(exc).lower():
                raise
            self.conn.rollback()
            self.conn.execute("BEGIN IMMEDIATE")

    def close(self) -> None:
        self.conn.close()

    def register(self, receipt: dict, now: float | None = None) -> bool:
        """Insert a receipt as 'new' if unseen. Returns True if newly added."""
        now = time.time() if now is None else now
        placed = placed_timestamp(receipt, now)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO orders "
            "(receipt_id, state, buyer_name, created_at, updated_at, receipt_json, placed_at) "
            "VALUES (?, 'new', ?, ?, ?, ?, ?)",
            (
                receipt["receipt_id"],
                receipt.get("name"),
                now,
                now,
                json.dumps(receipt),
                placed,
            ),
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

    def transition_if(
        self, receipt_id: int, from_states: tuple[str, ...], to_state: str, note: str = ""
    ) -> bool:
        """Move the order only if it is still in one of `from_states`.

        Returns False when another process already moved it.
        """
        if to_state not in STATES:
            raise ValueError(f"Unknown state {to_state!r}")
        self._begin_immediate()
        try:
            row = self.conn.execute(
                "SELECT state FROM orders WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if row is None or row["state"] not in from_states:
                self.conn.commit()
                return False
            self.conn.execute(
                "UPDATE orders SET state = ?, updated_at = ?, error = ? WHERE receipt_id = ?",
                (to_state, time.time(), note if to_state == "held" else None, receipt_id),
            )
            self._log_event(receipt_id, row["state"], to_state, note)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def update_receipt(self, receipt: dict) -> None:
        """Replace the stored receipt, leaving the state and the delay anchor."""
        self.conn.execute(
            "UPDATE orders SET buyer_name = ?, receipt_json = ? WHERE receipt_id = ?",
            (receipt.get("name"), json.dumps(receipt), receipt["receipt_id"]),
        )
        self.conn.commit()

    def set_placed_at(self, receipt_id: int, placed_at: float) -> None:
        """Record when the order was placed, once. Never moves an anchor."""
        self.conn.execute(
            "UPDATE orders SET placed_at = ? WHERE receipt_id = ? AND placed_at IS NULL",
            (placed_at, receipt_id),
        )
        self.conn.commit()

    def set_release_at(self, receipt_id: int, release_at: float) -> None:
        """Force the moment postage may be bought. 0 means immediately."""
        self.conn.execute(
            "UPDATE orders SET release_at = ? WHERE receipt_id = ?",
            (release_at, receipt_id),
        )
        self.conn.commit()

    def claim_releasing(self, receipt_id: int, note: str) -> bool:
        """Move new/waiting -> releasing. False if another process owns it."""
        self._begin_immediate()
        try:
            row = self.conn.execute(
                "SELECT state FROM orders WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if row is None or row["state"] not in ("new", "waiting"):
                self.conn.commit()
                return False
            self.conn.execute(
                "UPDATE orders SET state = 'releasing', updated_at = ? WHERE receipt_id = ?",
                (time.time(), receipt_id),
            )
            self._log_event(receipt_id, row["state"], "releasing", note)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def claim_stale_release(self, receipt_id: int, stale_before: float) -> bool:
        """Take over a release whose owner has gone quiet.

        `stale_before` is a timestamp: an in-flight release touched more
        recently than that still belongs to the other process.
        """
        self._begin_immediate()
        try:
            row = self.conn.execute(
                "SELECT state, updated_at FROM orders WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "releasing"
                or row["updated_at"] is None
                or row["updated_at"] >= stale_before
            ):
                self.conn.commit()
                return False
            self.conn.execute(
                "UPDATE orders SET updated_at = ? WHERE receipt_id = ?",
                (time.time(), receipt_id),
            )
            self._log_event(
                receipt_id,
                "releasing",
                "releasing",
                "resuming label release after a restart",
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

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

    def claim_label_attempt(self, receipt_id: int) -> bool:
        """Record that a purchase is starting. False if one was already claimed.

        One statement, so two processes cannot both pass the check and both
        pay Shippo. The loser must not buy.
        """
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO label_attempts (receipt_id, at) VALUES (?, ?)",
            (receipt_id, time.time()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def label_attempted(self, receipt_id: int) -> bool:
        return self.label_attempt_at(receipt_id) is not None

    def label_attempt_at(self, receipt_id: int) -> float | None:
        """When a purchase was claimed, or None if it never was."""
        row = self.conn.execute(
            "SELECT at FROM label_attempts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        return None if row is None else float(row["at"])

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

    def latest_label(self) -> sqlite3.Row | None:
        """The label most recently purchased, or printed again since then.

        `created_at` is the purchase. A reprint writes an event whose note
        starts with ``label ->``. Whichever of those is newer wins, so
        ``reprint-label last`` repeats the label that just came out of the
        printer rather than a purchase that is merely the newest row.
        """
        return self.conn.execute(
            """
            SELECT labels.receipt_id AS receipt_id,
                   orders.buyer_name AS buyer_name
            FROM labels
            LEFT JOIN orders ON orders.receipt_id = labels.receipt_id
            LEFT JOIN (
                SELECT receipt_id, MAX(at) AS at
                FROM events
                WHERE note LIKE 'label ->%'
                GROUP BY receipt_id
            ) AS printed ON printed.receipt_id = labels.receipt_id
            ORDER BY CASE
                       WHEN IFNULL(printed.at, 0) > labels.created_at THEN printed.at
                       ELSE labels.created_at
                     END DESC,
                     labels.receipt_id DESC
            LIMIT 1
            """
        ).fetchone()

    def latest_slip(self) -> sqlite3.Row | None:
        """The order whose packing slip was printed most recently."""
        return self.conn.execute(
            """
            SELECT orders.receipt_id AS receipt_id,
                   orders.buyer_name AS buyer_name
            FROM events
            JOIN orders ON orders.receipt_id = events.receipt_id
            WHERE events.to_state = 'slip_printed'
              AND orders.receipt_json IS NOT NULL
              AND orders.receipt_json != ''
            ORDER BY events.at DESC, events.id DESC
            LIMIT 1
            """
        ).fetchone()

    # -- odds and ends ------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

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


def placed_timestamp(receipt: dict, fallback: float) -> float:
    """When the buyer placed the order, else when we first saw it.

    The delay is measured from placement, so a poller that was off does not
    add a second wait on top of the time the order already sat. A missing
    or unusable timestamp falls back to first-seen, which is the only clock
    we have.
    """
    ts = receipt.get("created_timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return float(ts)
    return float(fallback)
