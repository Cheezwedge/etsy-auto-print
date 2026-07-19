"""Order pipeline: poll Etsy for paid, unshipped orders and advance each
through the state machine — packing slip (phase 1), shipping label
(phase 2), then tracking posted back to Etsy (phase 3).

Tracking is only ever posted for real (non-test) labels: a test label's fake
tracking number must never reach a buyer.
"""

from __future__ import annotations

import logging

from .etsy import EtsyApiError, EtsyClient
from .labels import LabelError, Labeler
from .notify import Notifier
from .printer import Printer, PrintError
from .slip import render_packing_slip
from .store import Store

log = logging.getLogger("etsy-auto-print")

# Shippo provider name -> Etsy carrier_name
CARRIER_NAMES = {
    "USPS": "usps",
    "UPS": "ups",
    "FedEx": "fedex",
    "DHL Express": "dhl-express",
}


def poll_once(
    client: EtsyClient,
    store: Store,
    printer: Printer,
    labeler: Labeler | None = None,
    notifier: Notifier | None = None,
) -> int:
    """One poll pass. Returns the number of orders that made progress."""
    receipts = client.get_open_receipts()
    log.info("Poll: %d open (paid, unshipped) receipt(s)", len(receipts))

    progressed = 0
    for receipt in receipts:
        rid = receipt["receipt_id"]
        if store.register(receipt):
            log.info("New order #%s from %s", rid, receipt.get("name", "?"))
        if advance_order(receipt, store, printer, labeler, etsy=client, notifier=notifier):
            progressed += 1
    return progressed


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

    def hold(reason: str) -> None:
        log.error("Order #%s held: %s", rid, reason)
        store.hold(rid, reason)
        if notifier:
            notifier.send(
                f"Etsy order #{rid} needs attention",
                f"{receipt.get('name', '?')}: {reason}\n"
                f"Fix the cause, then run: etsy-auto-print retry {rid}",
            )

    if store.get(rid)["state"] == "new":
        try:
            slip = render_packing_slip(receipt)
            destination = printer.print_text(f"packing-slip-{rid}", slip)
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

    return moved


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
    try:
        etsy.create_receipt_shipment(rid, label["tracking_number"], carrier)
    except EtsyApiError as exc:
        return f"tracking upload to Etsy failed: {exc}"
    store.transition(rid, "tracking_posted", f"{carrier} {label['tracking_number']}")
    store.transition(rid, "done", "buyer notified by Etsy")
    log.info("Order #%s: tracking posted (%s), order complete", rid, label["tracking_number"])
    return True


def reprint(receipt_id: int, store: Store, printer: Printer) -> str:
    receipt = store.get_receipt_json(receipt_id)
    if receipt is None:
        raise KeyError(f"Order #{receipt_id} not found in the local database")
    slip = render_packing_slip(receipt)
    destination = printer.print_text(f"packing-slip-{receipt_id}", slip)
    if store.get(receipt_id)["state"] == "held":
        store.transition(receipt_id, "slip_printed", f"reprint -> {destination}")
    return destination
