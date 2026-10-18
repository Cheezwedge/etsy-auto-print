"""Order pipeline: poll Etsy for paid, unshipped orders and advance each
through the state machine — packing slip (phase 1), shipping label
(phase 2), then tracking posted back to Etsy (phase 3).

Tracking is only ever posted for real (non-test) labels: a test label's fake
tracking number must never reach a buyer.

A label delay, when one is set, holds a new order in `waiting` until it has
aged from the time the buyer placed it. Postage is bought only after a fresh
read of the receipt. The wait lives in SQLite, so a restart finishes the same
order once and does not start a second purchase.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

from . import shipdate, stock
from .etsy import EtsyApiError, EtsyClient
from .labels import LabelError, Labeler
from .notify import Notifier
from .printer import Printer, PrintError
from .slip import render_packing_slip
from .zpl import render_slip_zpl
from .store import Store, placed_timestamp

log = logging.getLogger("etsy-auto-print")

# Shippo provider name -> Etsy carrier_name
CARRIER_NAMES = {
    "USPS": "usps",
    "UPS": "ups",
    "FedEx": "fedex",
    "DHL Express": "dhl-express",
}


class StockWatcher:
    """Checks listing stock on its own slower clock inside the poll loop.

    Listings change on the timescale of sales, so this runs hourly by
    default rather than every poll — the order pipeline's own Etsy calls
    matter more than stock does, and they share a rate limit.
    """

    def __init__(self, config, store: Store, notifier: Notifier | None = None):
        self.config = config
        self.store = store
        self.notifier = notifier
        # None, not 0.0: "never checked" has to be true at any clock value,
        # not merely because real timestamps are large.
        self.last_check: float | None = None
        self.disabled_reason = ""

    def due(self, now: float) -> bool:
        if not self.config.enabled or self.disabled_reason:
            return False
        if self.last_check is None:
            return True
        return now - self.last_check >= self.config.interval_minutes * 60

    def check(self, client: EtsyClient, now: float | None = None) -> list[stock.Alert]:
        self.last_check = time.time() if now is None else now
        try:
            listings, complete = client.get_all_listings()
        except EtsyApiError as exc:
            # Never let stock monitoring break order processing: this runs
            # inside the same loop that ships parcels.
            log.warning("Stock check skipped: %s", exc)
            return []

        alerts, current = stock.evaluate(
            listings, complete, self.store.stock_levels(), self.config.low_threshold
        )
        self.store.save_stock_state(current)

        for alert in alerts:
            log.warning("Listing %s is %s: %s", alert.listing_id, alert.headline,
                        alert.title)
            if self.notifier:
                detail = f"{alert.title}\n{alert.reason}."
                if not complete:
                    detail += ("\n\nRe-run `etsy-auto-print auth` to grant listings_r "
                               "for exact stock numbers.")
                self.notifier.send(f"Etsy listing {alert.headline}", detail)
        return alerts


# A release touched more recently than this still belongs to whoever claimed
# it — the poller, or Print now in the dashboard. Those are two processes, and
# Shippo can be slow; taking the order away mid-purchase is how postage gets
# bought twice. A crash leaves the order in `releasing` until this expires,
# and the next poll finishes that same purchase.
STALE_RELEASE_SECONDS = 300

MAX_DELAY_SECONDS = 14 * 24 * 3600

_DELAY_AMOUNT = "label_delay_amount"
_DELAY_UNIT = "label_delay_unit"
_RELEASE_NOTE = "re-checking the order before buying postage"


def parse_delay(amount: int, unit: str) -> int:
    """Seconds to wait. `amount` is a whole number of minutes or hours."""
    if unit not in ("minutes", "hours"):
        raise ValueError("Delay unit must be minutes or hours.")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError("Delay must be a whole number of minutes or hours.")
    if amount < 0:
        raise ValueError("Delay cannot be negative.")
    seconds = amount * (3600 if unit == "hours" else 60)
    if seconds > MAX_DELAY_SECONDS:
        raise ValueError("Delay cannot be longer than 14 days.")
    return seconds


def delay_phrase(amount: int, unit: str) -> str:
    if amount == 0:
        return "off (print on the next poll)"
    word = unit[:-1] if amount == 1 and unit.endswith("s") else unit
    return f"{amount} {word}"


def set_label_delay(store: Store, amount: int, unit: str) -> int:
    """Persist the dashboard delay. Returns the wait in seconds."""
    seconds = parse_delay(amount, unit)
    store.set_meta(_DELAY_AMOUNT, str(int(amount)))
    store.set_meta(_DELAY_UNIT, unit)
    return seconds


def _nonnegative_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def describe_delay(store: Store, config_minutes: int = 0) -> tuple[int, str, int]:
    """The delay in force: (amount, unit, seconds).

    A value saved from the dashboard wins. Until someone saves one, the
    minutes in config.toml apply (0 if that was never set either).
    """
    raw = store.get_meta(_DELAY_AMOUNT)
    if raw is None:
        minutes = _nonnegative_int(config_minutes)
        return minutes, "minutes", minutes * 60
    unit = store.get_meta(_DELAY_UNIT) or "minutes"
    try:
        amount = int(str(raw).strip())
        seconds = parse_delay(amount, unit)
    except (TypeError, ValueError):
        log.warning(
            "Stored label delay %r %r is invalid — using the config value", raw, unit
        )
        minutes = _nonnegative_int(config_minutes)
        return minutes, "minutes", minutes * 60
    return amount, unit, seconds


def format_countdown(seconds: int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "due now"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"in {hours}h {minutes:02d}m"
    if minutes:
        return f"in {minutes}m {secs:02d}s"
    return f"in {secs}s"


def release_timestamp(row, delay_seconds: int) -> float:
    """When postage may be bought for this order."""
    if row["release_at"] is not None:
        return float(row["release_at"])
    anchor = row["placed_at"] if row["placed_at"] is not None else row["created_at"]
    return float(anchor) + max(0, int(delay_seconds))


def still_holding(row, delay_seconds: int, now: float) -> bool:
    if row["release_at"] is not None:
        return now < float(row["release_at"])
    if delay_seconds <= 0:
        return False
    return now < release_timestamp(row, delay_seconds)


def waiting_to_print(store: Store, delay_seconds: int, now: float | None = None) -> list[dict]:
    """Orders the dashboard should show with a countdown."""
    now = time.time() if now is None else now
    rows = []
    for row in store.all_orders():
        if row["state"] not in ("waiting", "releasing"):
            continue
        release = release_timestamp(row, delay_seconds)
        remaining = int(release - now)
        events = store.events(row["receipt_id"])
        note = (events[-1]["note"] if events else "") or ""
        if row["state"] == "releasing":
            countdown = "printing…"
        elif remaining <= 0:
            countdown = "due now"
        else:
            countdown = format_countdown(remaining)
        rows.append({
            "receipt_id": row["receipt_id"],
            "buyer": row["buyer_name"] or "?",
            "state": row["state"],
            "release_at": int(release),
            "countdown": countdown,
            "detail": note if "re-check" in note else "",
        })
    return rows


def skip_postage_reason(receipt: dict) -> str:
    """Why this receipt must not be bought, or "" when it is still shippable.

    Checked against a receipt just fetched from Etsy, not the copy we stored
    when the order first appeared — that copy is exactly what may have gone
    stale during the delay.
    """
    status = str(receipt.get("status") or "").lower()
    if "cancel" in status:
        return f"cancelled — Etsy status is {status!r}"
    if "refund" in status:
        return f"refunded — Etsy status is {status!r}"
    if receipt.get("is_shipped") is True or receipt.get("was_shipped") is True:
        return "already shipped — not buying postage"
    if receipt.get("shipments"):
        return "already has a label — not buying postage"
    return ""


class _Recheck:
    def __init__(self, action: str, reason: str = "", receipt: dict | None = None):
        self.action = action
        self.reason = reason
        self.receipt = receipt


def recheck_before_postage(etsy, store: Store, rid: int, fallback: dict) -> _Recheck:
    """Re-read the order. Never buy when that read fails or says not to."""
    if etsy is None or not hasattr(etsy, "get_receipt"):
        return _Recheck(
            "later",
            "could not re-check the order before buying postage — will try again",
        )
    try:
        fresh = etsy.get_receipt(rid)
    except EtsyApiError as exc:
        if exc.status == 404:
            return _Recheck("skip", "no such receipt on Etsy — not buying postage")
        return _Recheck(
            "later",
            "could not re-check the order before buying postage "
            f"({exc}) — will try again",
        )
    if not isinstance(fresh, dict):
        return _Recheck(
            "later",
            "could not re-check the order before buying postage — will try again",
        )
    if "receipt_id" not in fresh:
        fresh = {**fresh, "receipt_id": rid}
    if store.get_label(rid) is not None:
        return _Recheck(
            "print-existing",
            "already has a label — not buying postage again",
            fresh,
        )
    reason = skip_postage_reason(fresh)
    if reason:
        return _Recheck("skip", reason, fresh)
    return _Recheck("buy", "", fresh)


def enter_waiting(store: Store, row, delay_seconds: int) -> bool:
    if row["state"] == "waiting":
        return False
    release = release_timestamp(row, delay_seconds)
    when = datetime.fromtimestamp(release).strftime("%Y-%m-%d %H:%M")
    note = f"holding until {when} before buying postage"
    store.transition(row["receipt_id"], "waiting", note)
    log.info("Order #%s: %s", row["receipt_id"], note)
    return True


def handle_receipt(
    receipt: dict,
    store: Store,
    printer: Printer,
    labeler: Labeler | None,
    etsy: EtsyClient | None,
    notifier: Notifier | None,
    delay_seconds: int,
    now: float,
    already_owned: bool = False,
) -> bool:
    """One open receipt: wait, re-check, or advance. True if it moved."""
    rid = receipt["receipt_id"]
    row = store.get(rid)
    if row is None:
        return False
    if row["placed_at"] is None and row["state"] in ("new", "waiting", "releasing"):
        store.set_placed_at(rid, placed_timestamp(receipt, row["created_at"]))
        row = store.get(rid)
    state = row["state"]

    if state == "new" and is_digital_only(receipt):
        return advance_order(receipt, store, printer, labeler, etsy, notifier)

    # No delay and nobody has pinned a release time: historical behavior.
    # Do not add an Etsy round-trip on the path every existing order takes.
    if (
        not already_owned
        and state == "new"
        and delay_seconds <= 0
        and row["release_at"] is None
    ):
        return advance_order(receipt, store, printer, labeler, etsy, notifier)

    if (
        not already_owned
        and state in ("new", "waiting")
        and still_holding(row, delay_seconds, now)
    ):
        return enter_waiting(store, row, delay_seconds)

    if state in ("new", "waiting", "releasing") or already_owned:
        if not already_owned:
            if state == "releasing":
                if not store.claim_stale_release(rid, now - STALE_RELEASE_SECONDS):
                    log.info("Order #%s is already being released", rid)
                    return False
            elif not store.claim_releasing(rid, _RELEASE_NOTE):
                log.info("Order #%s is already being released", rid)
                return False
        outcome = recheck_before_postage(etsy, store, rid, receipt)
        if outcome.action == "later":
            store.transition(rid, "waiting", outcome.reason)
            log.warning("Order #%s: %s", rid, outcome.reason)
            return False
        if outcome.action == "skip":
            store.transition(rid, "done", outcome.reason)
            log.info("Order #%s skipped: %s", rid, outcome.reason)
            return True
        if outcome.action == "print-existing":
            log.info("Order #%s: %s", rid, outcome.reason)
            current = store.get(rid)["state"]
            if current in ("new", "waiting", "releasing"):
                store.transition(rid, "label_purchased", outcome.reason)
            return advance_order(
                outcome.receipt or receipt, store, printer, labeler, etsy, notifier
            )
        fresh = outcome.receipt or receipt
        store.update_receipt(fresh)
        return advance_order(fresh, store, printer, labeler, etsy, notifier)

    return advance_order(receipt, store, printer, labeler, etsy, notifier)


def print_now_order(
    receipt_id: int,
    store: Store,
    printer: Printer,
    labeler: Labeler | None,
    etsy: EtsyClient | None,
    notifier: Notifier | None,
    now: float | None = None,
    delay_seconds: int | None = None,
    config_delay_minutes: int = 0,
) -> tuple[bool, str]:
    """Buy postage and print a waiting order without waiting out the delay."""
    now = time.time() if now is None else now
    if delay_seconds is None:
        delay_seconds = describe_delay(store, config_delay_minutes)[2]
    row = store.get(receipt_id)
    if row is None:
        raise KeyError(f"Order #{receipt_id} not found in the local database")
    if row["state"] not in ("new", "waiting", "releasing"):
        raise ValueError(f"Order #{receipt_id} is {row['state']}, not waiting to print")
    owned = False
    if row["state"] == "releasing":
        if not store.claim_stale_release(receipt_id, now - STALE_RELEASE_SECONDS):
            return True, f"Order #{receipt_id} is already being printed."
        owned = True
    else:
        store.set_release_at(receipt_id, 0.0)
    receipt = store.get_receipt_json(receipt_id)
    if receipt is None:
        raise KeyError(f"Order #{receipt_id} has no stored receipt")
    handle_receipt(
        receipt, store, printer, labeler, etsy, notifier,
        delay_seconds, now, already_owned=owned,
    )
    row = store.get(receipt_id)
    state = row["state"]
    events = store.events(receipt_id)
    note = (events[-1]["note"] if events else "") or ""
    if state == "done":
        return True, f"Order #{receipt_id} not printed: {note}"
    if state == "held":
        return False, f"Order #{receipt_id} held: {row['error']}"
    if state in ("waiting", "new", "releasing"):
        return False, f"Order #{receipt_id} is still waiting: {note}"
    return True, f"Order #{receipt_id} is now {state}."


def skip_waiting_order(store: Store, receipt_id: int) -> str:
    """Drop a waiting order without buying postage."""
    row = store.get(receipt_id)
    if row is None:
        raise KeyError(f"Order #{receipt_id} not found in the local database")
    if store.get_label(receipt_id) is not None or store.label_attempted(receipt_id):
        raise ValueError(
            f"Order #{receipt_id} already has a label or a purchase in progress — not skipping"
        )
    note = "skipped from the dashboard — postage not purchased"
    if not store.transition_if(receipt_id, ("new", "waiting"), "done", note):
        current = store.get(receipt_id)["state"]
        raise ValueError(f"Order #{receipt_id} is {current}, not waiting to print")
    log.info("Order #%s skipped: %s", receipt_id, note)
    return note


def poll_once(
    client: EtsyClient,
    store: Store,
    printer: Printer,
    labeler: Labeler | None = None,
    notifier: Notifier | None = None,
    stock_watcher: "StockWatcher | None" = None,
    delay_seconds: int | None = None,
    config_delay_minutes: int = 0,
    now: float | None = None,
) -> int:
    """One poll pass. Returns the number of orders that made progress.

    `delay_seconds` overrides the saved delay (tests). Left unset, the delay
    is read from the database on every pass, so a change on the dashboard
    applies without restarting the poller. `now` overrides the clock.
    """
    now = time.time() if now is None else now
    if delay_seconds is None:
        delay_seconds = describe_delay(store, config_delay_minutes)[2]

    if stock_watcher and stock_watcher.due(now):
        stock_watcher.check(client, now)

    receipts = client.get_open_receipts()
    log.info("Poll: %d open (paid, unshipped) receipt(s)", len(receipts))
    open_ids = {r["receipt_id"] for r in receipts}
    reconcile(client, store, open_ids)

    progressed = 0
    for receipt in receipts:
        rid = receipt["receipt_id"]
        if store.register(receipt, now=now):
            log.info("New order #%s from %s", rid, receipt.get("name", "?"))
        if handle_receipt(
            receipt, store, printer, labeler, client, notifier, delay_seconds, now
        ):
            progressed += 1

    # A crash mid-release leaves the order in `releasing`. If this poll's
    # open list doesn't include it, the loop above never sees it — finish
    # it anyway, or a restart would strand postage that was already decided.
    for row in store.in_state("releasing"):
        rid = row["receipt_id"]
        if rid in open_ids:
            continue
        receipt = store.get_receipt_json(rid)
        if receipt is None:
            continue
        if handle_receipt(
            receipt, store, printer, labeler, client, notifier, delay_seconds, now
        ):
            progressed += 1
    return progressed


def is_digital_only(receipt: dict) -> bool:
    """True when nothing on this order is physically shipped.

    An instant-download listing has no SKU, no weight and no parcel, so the
    pipeline would print a pick slip for nothing and then hold the order
    forever on a missing weight — a false alarm on every digital sale.

    A mixed order still ships: only when *every* line is digital is there
    nothing to pack. An order with no recognisable lines is treated as
    physical, because holding one of those is recoverable and silently
    completing a real order is not.
    """
    transactions = receipt.get("transactions") or []
    return bool(transactions) and all(
        txn.get("is_digital") is True for txn in transactions
    )


def was_fulfilled_elsewhere(receipt: dict) -> str:
    """Why Etsy no longer considers this order outstanding, if it doesn't.

    Returns a reason to close the order with, or "" to leave it alone.
    Deliberately conservative: an unrecognised shape means do nothing, since
    wrongly closing a live order means it never ships.
    """
    if receipt.get("is_shipped") is True:
        return "shipped outside this program — marked shipped on Etsy"
    status = str(receipt.get("status") or "").lower()
    if "cancel" in status or "refund" in status:
        return f"no longer to be shipped — Etsy status is {status!r}"
    return ""


def reconcile(client: EtsyClient, store: Store, open_ids: set[int]) -> int:
    """Close out orders Etsy has stopped listing as awaiting shipment.

    An international order bought through Etsy, or an order cancelled there,
    vanishes from the open-receipts poll — and would otherwise sit 'held' in
    here forever, keeping a permanent "needs attention" on the dashboard for
    something already dealt with.

    Absence from the poll is only the trigger. Each candidate is confirmed by
    fetching the receipt, because an Etsy hiccup returning an empty list must
    never silently close every order in flight.
    """
    closed = 0
    for row in store.unfinished():
        rid = row["receipt_id"]
        if rid in open_ids:
            continue
        try:
            receipt = client.get_receipt(rid)
        except EtsyApiError as exc:
            if exc.status == 404:
                # Etsy has no such receipt — a fake order from `test-order`,
                # or one deleted outright. Closing it stops us re-asking
                # about it on every single poll, forever.
                store.transition(rid, "done", "no such receipt on Etsy")
                closed += 1
                continue
            log.debug("Order #%s: could not re-check (%s)", rid, exc)
            continue
        reason = was_fulfilled_elsewhere(receipt)
        if not reason:
            continue
        store.transition(rid, "done", reason)
        log.info("Order #%s closed: %s", rid, reason)
        closed += 1
    return closed


def advance_order(
    receipt: dict,
    store: Store,
    printer: Printer,
    labeler: Labeler | None = None,
    etsy: EtsyClient | None = None,
    notifier: Notifier | None = None,
) -> bool:
    """Advance one order as far as it can go. Returns True if it moved."""
    rid = receipt["receipt_id"]
    moved = False
    started_in = store.get(rid)["state"]

    def hold(reason: str) -> None:
        log.error("Order #%s held: %s", rid, reason)
        store.hold(rid, reason)
        if notifier:
            notifier.send(
                f"Etsy order #{rid} needs attention",
                f"{receipt.get('name', '?')}: {reason}\n"
                f"Fix the cause, then run: etsy-auto-print retry {rid}",
            )

    if store.get(rid)["state"] in ("new", "releasing") and is_digital_only(receipt):
        store.transition(rid, "done", "digital download — nothing to ship")
        log.info("Order #%s is a digital download; nothing to print or ship", rid)
        return True

    if store.get(rid)["state"] in ("new", "releasing"):
        try:
            destination = printer.print_slip(
                f"packing-slip-{rid}",
                render_packing_slip(receipt),
                render_slip_zpl(receipt),
            )
        except PrintError as exc:
            hold(str(exc))
            return False
        store.transition(rid, "slip_printed", f"slip -> {destination}")
        log.info("Order #%s: packing slip -> %s", rid, destination)
        moved = True

    if labeler and store.get(rid)["state"] in ("slip_printed", "label_purchased"):
        try:
            labeler.advance(receipt)
            moved = True
        except LabelError as exc:
            # Losing the purchase race after the other process saved a label
            # must not mark that order held. A print failure still holds:
            # the postage is spent and nobody has been told.
            text = str(exc)
            if "previous label purchase attempt" in text and store.get_label(rid) is not None:
                log.info(
                    "Order #%s: not holding — a label is already recorded (%s)",
                    rid,
                    exc,
                )
                return False
            hold(text)
            return False

    reason = None
    if etsy and store.get(rid)["state"] == "label_printed":
        reason = tracking_due_reason(rid, receipt, store, labeler)
    if reason is not None:
        result = post_tracking(rid, store, etsy, reason)
        if result is True:
            moved = True
        elif isinstance(result, str):
            hold(result)
            return False

    # An order that worked used to say nothing at all: the label just appeared
    # on the printer. That's fine standing next to it and useless anywhere
    # else, and it makes silence ambiguous — no alert meant either "nothing
    # sold" or "something sold and you didn't notice".
    # Every step that could run has run by now, so whatever state the order
    # landed in is where it rests: label_printed or done with labels on,
    # slip_printed for a slip-only shop. Anything held has already alerted.
    ended_in = store.get(rid)["state"]
    waiting = ""
    if ended_in == "label_printed" and ended_in != started_in:
        day = scheduled_ship_day(rid, receipt, store, labeler)
        if day is not None:
            waiting = shipdate.promise(_ship_policy(labeler)[0], day, receipt)
            log.info("Order #%s: %s", rid, waiting)
    if notifier and ended_in != started_in and ended_in != "held":
        notifier.send_order_ready(
            f"Etsy order #{rid} printed — ready to pack",
            "\n".join(filter(None, [
                receipt.get("name", "?"), pack_list(receipt), waiting,
            ])),
        )

    return moved


def _ship_policy(labeler: Labeler | None) -> tuple[str, int]:
    if labeler is None:
        return shipdate.IMMEDIATELY, 0
    config = labeler.config
    return (getattr(config, "mark_shipped", shipdate.IMMEDIATELY),
            getattr(config, "mark_shipped_hour", 8))


def scheduled_ship_day(
    rid: int, receipt: dict, store: Store, labeler: Labeler | None
) -> date | None:
    """The day Etsy will be told, or None if it isn't waiting for a day.

    Test labels never reach Etsy at all, so they have no ship day to report.
    """
    policy, _ = _ship_policy(labeler)
    label = store.get_label(rid)
    if policy == shipdate.IMMEDIATELY or label is None or label["is_test"]:
        return None
    return shipdate.ship_day(label["created_at"], receipt, policy)


def tracking_is_due(
    rid: int, receipt: dict, store: Store, labeler: Labeler | None,
    now: datetime | None = None,
) -> bool:
    return tracking_due_reason(rid, receipt, store, labeler, now) is not None


def tracking_due_reason(
    rid: int, receipt: dict, store: Store, labeler: Labeler | None,
    now: datetime | None = None,
) -> str | None:
    """Why Etsy should be told now, or None to keep waiting."""
    policy, hour = _ship_policy(labeler)
    label = store.get_label(rid)
    if label is None or not label["created_at"]:
        return ""          # post_tracking reports the missing label itself
    now = now or datetime.now()
    reason = shipdate.due_reason(now, label["created_at"], receipt, policy, hour)
    if reason is not None:
        return reason
    if shipdate.waits_for_carrier(policy, receipt) and not label["is_test"]:
        if carrier_has_it(rid, label, labeler.client, now):
            return "USPS scanned it"
    return None


# Scans are checked at most this often per order. The poll runs every few
# minutes; USPS doesn't move that fast, and Shippo rate-limits tracking.
SCAN_CHECK_SECONDS = 30 * 60
_last_scan_check: dict[int, float] = {}


def carrier_has_it(rid: int, label, shippo, now: datetime) -> bool:
    """Has USPS scanned this parcel yet?

    A failure to ask counts as "not yet": the ship-by date still guarantees
    Etsy hears in time, so a Shippo outage can delay the message but can
    never make the shop late.
    """
    last = _last_scan_check.get(rid)
    if last is not None and now.timestamp() - last < SCAN_CHECK_SECONDS:
        return False
    _last_scan_check[rid] = now.timestamp()
    try:
        status = shippo.tracking_status(label["carrier"], label["tracking_number"])
    except Exception as exc:          # ShippoError, or anything unexpected
        log.warning("Order #%s: couldn't check the USPS scan (%s)", rid, exc)
        return False
    if status in shipdate.CARRIER_HAS_IT:
        log.info("Order #%s: USPS has it (%s) — telling Etsy", rid, status)
        _last_scan_check.pop(rid, None)
        return True
    return False


def pack_list(receipt: dict) -> str:
    """What to put in the box, for a notification read away from the printer."""
    lines = []
    for txn in receipt.get("transactions") or []:
        sku = txn.get("sku") or txn.get("title") or "?"
        lines.append(f"{txn.get('quantity', 1)} x {sku}")
    return "\n".join(lines) or "(no items listed)"


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def shipment_extras(label, ship_on: date | None = None) -> dict:
    """The optional shipment details Etsy accepts, from the purchased label.

    Etsy uses these to give the buyer richer, faster tracking updates. Every
    one is optional and every one is skipped when unknown, so an older label
    row (recorded before these columns existed) simply sends less.
    """
    extras: dict = {}
    if label["service"]:
        extras["mail_class"] = label["service"]

    weight = _num(label["weight_oz"])
    if weight:
        extras.update(weight=weight, weight_units="oz")

    dims = [_num(label[c]) for c in ("length_in", "width_in", "height_in")]
    if all(dims):
        # Etsy wants them ordered longest side first.
        length, width, height = sorted(dims, reverse=True)
        extras.update(
            length=length, width=width, height=height, dimension_units="in"
        )

    cost = _num(label["amount"])
    if cost is not None:
        extras["shipping_label_cost"] = cost
        extras["shipping_label_currency"] = label["currency"] or "USD"

    # The day Etsy is told, which is the day it actually ships — not the day
    # the label was bought, and in local time: a UTC date is already
    # tomorrow on a US evening.
    extras["ship_date"] = (ship_on or date.today()).isoformat()
    return extras


def post_tracking(
    rid: int, store: Store, etsy: EtsyClient, reason: str = ""
) -> bool | str | None:
    """Post a real label's tracking number to Etsy.

    Returns True on success, None when skipped (test label), or an error
    string the caller should hold the order with. `reason` (why now) is
    kept in the order's history next to the tracking number.
    """
    label = store.get_label(rid)
    if label is None:
        return "label_printed state but no label recorded — inspect with: show"
    if label["is_test"]:
        # Never send fake tracking to a real buyer. Order stays at
        # label_printed; it completes only with a live label.
        return None
    carrier = CARRIER_NAMES.get(label["carrier"], label["carrier"].lower())
    extras = shipment_extras(label)
    try:
        etsy.create_receipt_shipment(rid, label["tracking_number"], carrier, **extras)
    except EtsyApiError as exc:
        # The extra shipment details are a nice-to-have; getting the tracking
        # number to the buyer is not. If Etsy rejects the enriched payload,
        # fall back to the two fields that have always worked rather than
        # holding an order over a cosmetic field. (A 4xx means Etsy validated
        # and refused the request, so nothing was recorded to duplicate.)
        if not extras or not 400 <= exc.status < 500:
            return f"tracking upload to Etsy failed: {exc}"
        log.warning(
            "Order #%s: Etsy rejected the detailed tracking upload (%s) — "
            "retrying with tracking number only",
            rid,
            exc,
        )
        try:
            etsy.create_receipt_shipment(rid, label["tracking_number"], carrier)
        except EtsyApiError as retry_exc:
            return f"tracking upload to Etsy failed: {retry_exc}"
    why = f" — {reason}" if reason else ""
    store.transition(rid, "tracking_posted", f"{carrier} {label['tracking_number']}{why}")
    store.transition(rid, "done", "buyer notified by Etsy")
    log.info("Order #%s: tracking posted (%s)%s, order complete",
             rid, label["tracking_number"], why)
    return True


def reprint(receipt_id: int, store: Store, printer: Printer) -> str:
    receipt = store.get_receipt_json(receipt_id)
    if receipt is None:
        raise KeyError(f"Order #{receipt_id} not found in the local database")
    destination = printer.print_slip(
        f"packing-slip-{receipt_id}",
        render_packing_slip(receipt),
        render_slip_zpl(receipt),
    )
    if store.get(receipt_id)["state"] == "held":
        store.transition(receipt_id, "slip_printed", f"reprint -> {destination}")
    return destination
