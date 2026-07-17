"""Phase 1 pipeline: poll Etsy for paid, unshipped orders and print a
packing slip for each exactly once."""

from __future__ import annotations

import logging

from .etsy import EtsyClient
from .printer import Printer, PrintError
from .slip import render_packing_slip
from .store import Store

log = logging.getLogger("etsy-auto-print")


def poll_once(client: EtsyClient, store: Store, printer: Printer) -> int:
    """One poll pass. Returns the number of newly processed orders."""
    receipts = client.get_open_receipts()
    log.info("Poll: %d open (paid, unshipped) receipt(s)", len(receipts))

    processed = 0
    for receipt in receipts:
        rid = receipt["receipt_id"]
        if store.register(receipt):
            log.info("New order #%s from %s", rid, receipt.get("name", "?"))
        row = store.get(rid)
        if row["state"] != "new":
            continue
        if process_order(receipt, store, printer):
            processed += 1
    return processed


def process_order(receipt: dict, store: Store, printer: Printer) -> bool:
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
