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

from .config import normalize_service
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


def _holds(box: dict, item_count: int) -> bool:
    """A preset with no max_items declares no limit, so it holds anything."""
    limit = box.get("max_items")
    return limit is None or limit >= item_count


def compute_parcel(receipt: dict, label_config) -> dict:
    """One box per order. Each item is mapped to a parcel preset (by SKU, or
    the shop's default/only preset); if an order mixes items that map to
    different presets, everything ships in the single largest one by volume.
    Weight sums every item's own weight (x quantity) into that box.

    Box *dimensions* don't scale with quantity — five mugs don't fit in a
    one-mug box. A preset can declare max_items; when an order exceeds it we
    step up to the smallest configured box that does hold that many, and hold
    the order only when nothing does. Never ship a label whose dimensions are
    a lie, but don't hold an order the shop already owns a box for either.
    """
    transactions = receipt.get("transactions", [])
    presets_used: set[str] = set()
    item_weight_total = 0.0
    item_count = 0

    for txn in transactions:
        sku = txn.get("sku") or ""
        qty = txn.get("quantity", 1)
        title = txn.get("title", "?")[:40]
        item_count += qty

        preset_name = _preset_for_sku(sku, title, label_config)
        presets_used.add(preset_name)

        preset = label_config.parcels[preset_name]
        weight = label_config.item_weights_oz.get(sku, preset.get("default_item_oz"))
        if weight is None:
            raise LabelError(
                f"no weight configured for SKU {sku!r} ({title}) — add it to "
                f"[labels.item_weights_oz], the items CSV, or set default_item_oz "
                f"on the {preset_name!r} parcel"
            )
        item_weight_total += weight * qty

    if presets_used:
        chosen = max(presets_used, key=lambda n: _volume(label_config.parcels[n]))
    else:
        chosen = label_config.default_parcel or next(iter(label_config.parcels))
    box = label_config.parcels[chosen]

    max_items = box.get("max_items")
    if max_items is not None and item_count > max_items:
        bigger = [
            (n, b) for n, b in label_config.parcels.items()
            if _volume(b) > _volume(box) and _holds(b, item_count)
        ]
        if not bigger:
            raise LabelError(
                f"order has {item_count} items but the {chosen!r} box holds "
                f"max_items = {max_items}, and no larger box is configured that "
                f"holds {item_count} — pack it manually, add a bigger "
                f"[labels.parcels.<name>], or raise max_items if they do fit"
            )
        # Smallest box that actually holds the order: stepping straight to the
        # largest would overpay on dimensional weight for a two-pack.
        outgrown, chosen = chosen, min(bigger, key=lambda nb: _volume(nb[1]))[0]
        box = label_config.parcels[chosen]
        log.info(
            "Order has %d items, more than the %r box holds (%d) — using %r",
            item_count, outgrown, max_items, chosen,
        )

    total_oz = box["packaging_oz"] + item_weight_total

    return {
        "length": box["length_in"],
        "width": box["width_in"],
        "height": box["height_in"],
        "distance_unit": "in",
        "weight": round(total_oz, 2),
        "mass_unit": "oz",
    }


def _service_names(receipt: dict) -> tuple[set[str], set[str]]:
    """Shipping services named on the order, if any.

    Both live on the transaction, not the receipt: `shipping_upgrade` is what
    the buyer paid extra for (null unless they chose an upgrade), and
    `shipping_method` is the profile's service.
    """
    upgrades, methods = set(), set()
    for txn in receipt.get("transactions", []):
        if (upgrade := (txn.get("shipping_upgrade") or "").strip()):
            upgrades.add(upgrade)
        if (method := (txn.get("shipping_method") or "").strip()):
            methods.add(method)
    return upgrades, methods


def required_service(receipt: dict, label_config) -> str | None:
    """The Shippo service token this order must ship with, or None for "any".

    The asymmetry here is deliberate. An *upgrade* is money the buyer paid for
    a named service, so an upgrade we can't translate holds the order rather
    than quietly shipping something slower. A *method* is just the profile's
    standard service, which is populated on ordinary orders — holding on one
    we don't recognise would stop every order in the shop, so an unmapped
    method falls back to today's behavior (cheapest allowed rate).
    """
    upgrades, methods = _service_names(receipt)

    if len(upgrades) > 1:
        raise LabelError(
            "order has items with different shipping upgrades "
            f"({', '.join(sorted(upgrades))}) — one package can only ship one "
            "way, so split it manually"
        )

    if upgrades:
        name = next(iter(upgrades))
        token = label_config.service_map.get(normalize_service(name))
        if token:
            return token
        if label_config.hold_unmapped_upgrade:
            raise LabelError(
                f"buyer paid for shipping upgrade {name!r}, which is not in "
                f"[labels.service_map] — add a line mapping it to a Shippo "
                f"service (see: etsy-auto-print services), or set "
                f"labels.hold_unmapped_upgrade = false to ship the cheapest "
                f"rate anyway"
            )
        log.warning("Unmapped shipping upgrade %r — buying cheapest rate", name)
        return None

    # No upgrade: honor the standard service only when we recognise it.
    for name in sorted(methods):
        if token := label_config.service_map.get(normalize_service(name)):
            return token
    return None


def label_metadata(receipt_id: int) -> str:
    """The searchable tag Shippo stores against the charge for an order.

    Recoverable both ways: printed by `clear-attempt` so you know what to
    search the Shippo dashboard for, and stored on the transaction so the
    search finds it.
    """
    return f"Etsy order #{receipt_id}"


def pick_rate(
    rates: list[dict], allowed_providers: list[str], service_token: str | None = None
) -> dict:
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

    if service_token:
        exact = [
            r for r in candidates
            if r.get("servicelevel", {}).get("token") == service_token
        ]
        if not exact:
            available = sorted(
                r.get("servicelevel", {}).get("token", "?") for r in candidates
            )
            raise LabelError(
                f"the order requires {service_token!r} but the carrier did not "
                f"quote it for this package (available: {', '.join(available)}) "
                "— the parcel may be too heavy or large for that service"
            )
        candidates = exact

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
                f"search the Shippo dashboard for {label_metadata(rid)!r} to see "
                "whether it was charged, before retrying "
                "(clear with: etsy-auto-print clear-attempt)"
            )

        address_to = build_address_to(receipt)
        if address_to["country"] != self.config.ship_from.get("country", "US"):
            # Stop here rather than let the carrier reject it. Shippo can buy
            # international labels, but only with a customs declaration, and
            # that needs per-item data (description, value, country of origin)
            # nobody can invent. Failing at the carrier instead reads like a
            # bug and burns an address-validation and shipment call first.
            raise LabelError(
                f"international order to {address_to['country']} — this program "
                "cannot buy it, because customs declarations are not configured. "
                "Buy this one on Etsy (Etsy fills in the customs form from the "
                "order), then it will close out here on its own."
            )
        try:
            created = self.client.create_address(address_to, validate=True)
        except ShippoError as exc:
            raise LabelError(f"address validation call failed: {exc}") from exc
        validation = created.get("validation_results") or {}
        if validation.get("is_valid") is False:
            msgs = "; ".join(m.get("text", "") for m in validation.get("messages", []))
            # A test token has no real address data behind it, so it reports
            # perfectly good addresses as invalid. Blocking on that would mean
            # no test order could ever reach the label step — and a test label
            # is never shipped to anyone, so there is nothing to protect.
            if self.client.is_test:
                log.warning(
                    "Order #%s: address validation says %s — ignoring, because a "
                    "TEST token cannot validate addresses. This check is live "
                    "in production.",
                    rid, msgs or "invalid",
                )
            elif not self.config.validate_addresses:
                log.warning(
                    "Order #%s: address validation says %s — continuing anyway "
                    "(labels.validate_addresses = false)", rid, msgs or "invalid",
                )
            else:
                raise LabelError(
                    f"address failed validation: {msgs or 'no details'} — check the "
                    "buyer's address on Etsy. If it is genuinely deliverable and "
                    "the carrier just doesn't recognise it (common for new builds "
                    "and rural routes), set labels.validate_addresses = false"
                )

        parcel = compute_parcel(receipt, self.config)
        service = required_service(receipt, self.config)
        try:
            shipment = self.client.create_shipment(self.config.ship_from, address_to, parcel)
        except ShippoError as exc:
            raise LabelError(f"shipment creation failed: {exc}") from exc
        rate = pick_rate(shipment.get("rates", []), self.config.allowed_providers, service)
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
            # The tag is what makes a Shippo charge identifiable after the
            # fact: without it the dashboard shows a row of near-identical
            # USPS charges, and "was order #123 already bought?" — the exact
            # question a crashed attempt leaves behind — is unanswerable.
            txn = self.client.buy_label(
                rate["object_id"],
                self.config.file_type,
                metadata=label_metadata(rid),
            )
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
            service_token=rate.get("servicelevel", {}).get("token", ""),
            amount=rate.get("amount", ""),
            currency=rate.get("currency", ""),
            tracking_number=txn.get("tracking_number", ""),
            tracking_url=txn.get("tracking_url_provider", ""),
            label_url=txn.get("label_url", ""),
            # What the carrier was actually told the package is; sent to Etsy
            # with the tracking number so its shipment record matches.
            weight_oz=parcel["weight"],
            length_in=parcel["length"],
            width_in=parcel["width"],
            height_in=parcel["height"],
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
