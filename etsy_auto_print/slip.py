"""Render a plain-text packing slip from an Etsy receipt.

Plain text prints fine on any printer (laser, inkjet, or a thermal printer's
text mode) and needs no dependencies. Label rendering in the later phase is a
separate module — this one is only about what goes in the box.
"""

from __future__ import annotations

from datetime import datetime, timezone

WIDTH = 48  # fits 4x6 thermal media and looks fine on letter paper


def _money(m: dict | None) -> str:
    if not m:
        return ""
    amount = m.get("amount", 0) / m.get("divisor", 100)
    return f"{amount:.2f} {m.get('currency_code', '')}".strip()


def _address_lines(r: dict) -> list[str]:
    if r.get("formatted_address"):
        return [ln.strip() for ln in r["formatted_address"].splitlines() if ln.strip()]
    lines = [r.get("name", "")]
    for key in ("first_line", "second_line"):
        if r.get(key):
            lines.append(r[key])
    city_line = " ".join(
        p for p in (r.get("city", ""), r.get("state", ""), r.get("zip", "")) if p
    )
    if city_line:
        lines.append(city_line)
    if r.get("country_iso"):
        lines.append(r["country_iso"])
    return [ln for ln in lines if ln]


def render_packing_slip(receipt: dict, shop_name: str = "") -> str:
    rule = "=" * WIDTH
    thin = "-" * WIDTH
    created = receipt.get("created_timestamp") or receipt.get("create_timestamp")
    date_str = (
        datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
        if created
        else ""
    )

    out: list[str] = []
    if shop_name:
        out += [shop_name.center(WIDTH)]
    out += ["PACKING SLIP".center(WIDTH), rule]
    out += [f"Order #{receipt['receipt_id']}    {date_str}", ""]

    out += ["SHIP TO:"]
    out += [f"  {line}" for line in _address_lines(receipt)]
    out += ["", thin, "ITEMS", thin]

    for txn in receipt.get("transactions", []):
        qty = txn.get("quantity", 1)
        out.append(f"{qty} x {txn.get('title', 'Unknown item')}")
        if txn.get("sku"):
            out.append(f"      SKU: {txn['sku']}")
        for var in txn.get("variations", []):
            name, value = var.get("formatted_name"), var.get("formatted_value")
            if name and value:
                out.append(f"      {name}: {value}")
    out.append(thin)

    if receipt.get("is_gift"):
        out += ["", "** GIFT **"]
        if receipt.get("gift_message"):
            out += ["Gift message:", f"  {receipt['gift_message']}"]
    if receipt.get("message_from_buyer"):
        out += ["", "Note from buyer:", f"  {receipt['message_from_buyer']}"]

    total = _money(receipt.get("grandtotal"))
    if total:
        out += ["", f"Order total: {total}"]
    out += ["", "Thank you for your order!".center(WIDTH), ""]
    return "\n".join(out)
