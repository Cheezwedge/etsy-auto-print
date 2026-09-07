"""Order pipeline: poll Etsy for paid, unshipped orders and advance each
through the state machine — packing slip (phase 1), shipping label
(phase 2), then tracking posted back to Etsy (phase 3).

Tracking is only ever posted for real (non-test) labels: a test label's fake
tracking number must never reach a buyer.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import stock
from .etsy import EtsyApiError, EtsyClient
from .labels import LabelError, Labeler
from .notify import Notifier
from .printer import Printer, PrintError
from .slip import render_packing_slip
from .zpl import render_slip_zpl
from .store import Store

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


def poll_once(
    client: EtsyClient,
    store: Store,
    printer: Printer,
    labeler: Labeler | None = None,
    notifier: Notifier | None = None,
    stock_watcher: "StockWatcher | None" = None,
) -> int:
    """One poll pass. Returns the number of orders that made progress."""
    if stock_watcher and stock_watcher.due(time.time()):
        stock_watcher.check(client)

    receipts = client.get_open_receipts()
    log.info("Poll: %d open (paid, unshipped) receipt(s)", len(receipts))
    reconcile(client, store, {r["receipt_id"] for r in receipts})

    progressed = 0
    for receipt in receipts:
        rid = receipt["receipt_id"]
        if store.register(receipt):
            log.info("New order #%s from %s", rid, receipt.get("name", "?"))
        if advance_order(receipt, store, printer, labeler, etsy=client, notifier=notifier):
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

    if store.get(rid)["state"] == "new" and is_digital_only(receipt):
        store.transition(rid, "done", "digital download — nothing to ship")
        log.info("Order #%s is a digital download; nothing to print or ship", rid)
        return True

    if store.get(rid)["state"] == "new":
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
            hold(str(exc))
            return False

    if etsy and store.get(rid)["state"] == "label_printed":
        result = post_tracking(rid, store, etsy)
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
    if notifier and ended_in != started_in and ended_in != "held":
        notifier.send_order_ready(
            f"Etsy order #{rid} printed — ready to pack",
            f"{receipt.get('name', '?')}\n{pack_list(receipt)}",
        )

    return moved


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


def shipment_extras(label) -> dict:
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

    if label["created_at"]:
        extras["ship_date"] = datetime.fromtimestamp(
            label["created_at"], tz=timezone.utc
        ).strftime("%Y-%m-%d")
    return extras


def post_tracking(rid: int, store: Store, etsy: EtsyClient) -> bool | str | None:
    """Post a real label's tracking number to Etsy.

    Returns True on success, None when skipped (test label), or an error
    string the caller should hold the order with.
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
    store.transition(rid, "tracking_posted", f"{carrier} {label['tracking_number']}")
    store.transition(rid, "done", "buyer notified by Etsy")
    log.info("Order #%s: tracking posted (%s), order complete", rid, label["tracking_number"])
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
