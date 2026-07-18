"""Order pipeline: poll Etsy for paid, unshipped orders and advance each
through the state machine — packing slip (phase 1), then shipping label
(phase 2, when labels are enabled in config)."""

from __future__ import annotations

import logging

from .etsy import EtsyClient
from .labels import LabelError, Labeler
from .printer import Printer, PrintError
from .slip import render_packing_slip
from .store import Store

log = logging.getLogger("etsy-auto-print")


def poll_once(
    client: EtsyClient, store: Store, printer: Printer, labeler: Labeler | None = None
) -> int:
    """One poll pass. Returns the number of orders that made progress."""
    receipts = client.get_open_receipts()
    log.info("Poll: %d open (paid, unshipped) receipt(s)", len(receipts))

    progressed = 0
    for receipt in receipts:
        rid = receipt["receipt_id"]
        if store.register(receipt):
            log.info("New order #%s from %s", rid, receipt.get("name", "?"))
        if advance_order(receipt, store, printer, labeler):
            progressed += 1
    return progressed


def advance_order(
    receipt: dict, store: Store, printer: Printer, labeler: Labeler | None = None
) -> bool:
    """Advance one order as far as it can go. Returns True if it moved."""
    rid = receipt["receipt_id"]
    moved = False

    if store.get(rid)["state"] == "new":
        if not _print_slip(receipt, store, printer):
            return False
        moved = True

    if labeler and store.get(rid)["state"] in ("slip_printed", "label_purchased"):
        try:
            labeler.advance(receipt)
            moved = True
        except LabelError as exc:
            log.error("Order #%s held: %s", rid, exc)
            store.hold(rid, str(exc))
            return False

    return moved


def _print_slip(receipt: dict, store: Store, printer: Printer) -> bool:
    rid = receipt["receipt_id"]
    try:
        slip = render_packing_slip(receipt)
        destination = printer.print_text(f"packing-slip-{rid}", slip)
    except PrintError as exc:
        log.error("Order #%s held: %s", rid, exc)
        store.hold(rid, str(exc))
        return False
    store.transition(rid, "slip_printed", f"slip -> {destination}")
    log.info("Order #%s: packing slip -> %s", rid, destination)
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
