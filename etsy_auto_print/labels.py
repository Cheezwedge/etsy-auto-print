"""Label purchase and printing (phase 2).

Turns an Etsy receipt into a purchased, printed shipping label via Shippo:

    address -> validate -> parcel (from config presets) -> rates ->
    cheapest allowed rate -> buy -> download -> print

Anything that can't be resolved automatically raises LabelError, and the
pipeline holds the order for human review instead of guessing. Purchases are
guarded twice: the labels table records completed purchases (never buy twice)
and the attempts table records purchases *in flight*, so a crash between
"charged by Shippo" and "recorded locally" can never silently double-buy.
"""

from __future__ import annotations

import logging

from .printer import Printer, PrintError
from .shippo import ShippoClient, ShippoError
from .store import Store

log = logging.getLogger("etsy-auto-print")

_EXT_BY_FILE_TYPE = {"ZPLII": "zpl", "PNG": "png"}


class LabelError(Exception):
    """A holdable failure: the order needs a human, not a retry loop."""


def build_address_to(receipt: dict) -> dict:
    missing = [k for k in ("name", "first_line", "city", "zip", "country_iso") if not receipt.get(k)]
    if missing:
        raise LabelError(f"receipt is missing address fields: {', '.join(missing)}")
    return {
        "name": receipt["name"],
        "street1": receipt["first_line"],
        "street2": receipt.get("second_line") or "",
        "city": receipt["city"],
        "state": receipt.get("state") or "",
        "zip": receipt["zip"],
        "country": receipt["country_iso"],
    }


def _preset_for_sku(sku: str, title: str, label_config) -> str:
    name = label_config.item_parcels.get(sku) or label_config.default_parcel
    if name is None:
        if len(label_config.parcels) == 1:
            return next(iter(label_config.parcels))
        raise LabelError(
            f"no box size configured for SKU {sku!r} ({title}) — "
            "add it to [labels.item_parcels] or set labels.default_parcel"
        )
    if name not in label_config.parcels:
        raise LabelError(f"SKU {sku!r} ({title}) maps to unknown parcel preset {name!r}")
    return name


def _volume(box: dict) -> float:
    return box["length_in"] * box["width_in"] * box["height_in"]


def compute_parcel(receipt: dict, label_config) -> dict:
    """One box per order. Each item is mapped to a parcel preset (by SKU, or
    the shop's default/only preset); if an order mixes items that map to
    different presets, everything ships in the single largest one by volume.
    Weight sums every item's own weight into that box."""
    transactions = receipt.get("transactions", [])
    presets_used: set[str] = set()
    item_weight_total = 0.0

    for txn in transactions:
        sku = txn.get("sku") or ""
        qty = txn.get("quantity", 1)
        title = txn.get("title", "?")[:40]

        preset_name = _preset_for_sku(sku, title, label_config)
        presets_used.add(preset_name)

        preset = label_config.parcels[preset_name]
        weight = label_config.item_weights_oz.get(sku, preset.get("default_item_oz"))
        if weight is None:
            raise LabelError(
                f"no weight configured for SKU {sku!r} ({title}) — add it to "
                f"[labels.item_weights_oz] or set default_item_oz on the {preset_name!r} parcel"
            )
        item_weight_total += weight * qty

    if presets_used:
        chosen = max(presets_used, key=lambda n: _volume(label_config.parcels[n]))
    else:
        chosen = label_config.default_parcel or next(iter(label_config.parcels))
    box = label_config.parcels[chosen]
    total_oz = box["packaging_oz"] + item_weight_total

    return {
        "length": box["length_in"],
        "width": box["width_in"],
        "height": box["height_in"],
        "distance_unit": "in",
        "weight": round(total_oz, 2),
        "mass_unit": "oz",
    }


def pick_rate(rates: list[dict], allowed_providers: list[str]) -> dict:
    candidates = [
        r
        for r in rates
        if not allowed_providers or r.get("provider") in allowed_providers
    ]
    if not candidates:
        providers = sorted({r.get("provider", "?") for r in rates})
        raise LabelError(
            f"no rates from allowed providers {allowed_providers} "
            f"(available: {providers or 'none'})"
        )
    return min(candidates, key=lambda r: float(r["amount"]))


class Labeler:
    def __init__(self, label_config, store: Store, printer: Printer, client: ShippoClient):
        self.config = label_config
        self.store = store
        self.printer = printer
        self.client = client

    def advance(self, receipt: dict) -> None:
        """Drive one order from slip_printed to label_printed."""
        rid = receipt["receipt_id"]
        if self.store.get_label(rid) is None:
            self._purchase(receipt)
        self._print(rid)

    def _purchase(self, receipt: dict) -> None:
        rid = receipt["receipt_id"]
        if self.store.label_attempted(rid):
            raise LabelError(
                "a previous label purchase attempt did not record a result — "
                "check the Shippo dashboard for a charge before retrying "
                "(clear with: etsy-auto-print clear-attempt)"
            )

        address_to = build_address_to(receipt)
        try:
            created = self.client.create_address(address_to, validate=True)
        except ShippoError as exc:
            raise LabelError(f"address validation call failed: {exc}") from exc
        validation = created.get("validation_results") or {}
        if validation.get("is_valid") is False:
            msgs = "; ".join(m.get("text", "") for m in validation.get("messages", []))
            raise LabelError(f"address failed validation: {msgs or 'no details'}")

        parcel = compute_parcel(receipt, self.config)
        try:
            shipment = self.client.create_shipment(self.config.ship_from, address_to, parcel)
        except ShippoError as exc:
            raise LabelError(f"shipment creation failed: {exc}") from exc
        rate = pick_rate(shipment.get("rates", []), self.config.allowed_providers)
        log.info(
            "Order #%s: buying %s %s at %s %s (%.2f oz)%s",
            rid,
            rate.get("provider"),
            rate.get("servicelevel", {}).get("name"),
            rate.get("amount"),
            rate.get("currency"),
            parcel["weight"],
            " [TEST]" if self.client.is_test else "",
        )

        # Record intent *before* money moves, result immediately after.
        self.store.record_label_attempt(rid)
        try:
            txn = self.client.buy_label(rate["object_id"], self.config.file_type)
        except ShippoError as exc:
            raise LabelError(f"label purchase failed: {exc}") from exc
        if txn.get("status") != "SUCCESS":
            msgs = "; ".join(
                m.get("text", str(m)) for m in txn.get("messages", [])
            )
            raise LabelError(f"label purchase not successful: {msgs or txn.get('status')}")

        self.store.save_label(
            rid,
            object_id=txn.get("object_id", ""),
            carrier=rate.get("provider", ""),
            service=rate.get("servicelevel", {}).get("name", ""),
            amount=rate.get("amount", ""),
            currency=rate.get("currency", ""),
            tracking_number=txn.get("tracking_number", ""),
            tracking_url=txn.get("tracking_url_provider", ""),
            label_url=txn.get("label_url", ""),
            is_test=self.client.is_test,
        )
        self.store.clear_label_attempt(rid)
        self.store.transition(
            rid,
            "label_purchased",
            f"{rate.get('provider')} {rate.get('servicelevel', {}).get('name')} "
            f"{rate.get('amount')} {rate.get('currency')}"
            + (" [TEST]" if self.client.is_test else ""),
        )

    def _print(self, rid: int) -> None:
        label = self.store.get_label(rid)
        ext = _EXT_BY_FILE_TYPE.get(self.config.file_type, "pdf")
        try:
            data = self.client.download(label["label_url"])
            destination = self.printer.print_bytes(f"label-{rid}", data, ext)
        except (ShippoError, PrintError) as exc:
            raise LabelError(f"label print failed (already purchased!): {exc}") from exc
        self.store.transition(rid, "label_printed", f"label -> {destination}")
        log.info("Order #%s: label -> %s", rid, destination)
