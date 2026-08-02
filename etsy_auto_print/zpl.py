"""Render a packing slip as ZPL, for shops with only a label printer.

A shipping label says nothing about what goes in the box: it carries the
buyer's address, the postage barcode, and that is all. With several orders
printing unattended, a stack of labels is not enough to pack from.

This renders the same packing slip as a 4x6 ZPL label so it can come out of
the *same* thermal printer, immediately before its shipping label — the two
arrive as a physical pair, and the order number prints as a scannable barcode
so a slip that gets separated can still be matched back.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone

# Zebra defaults: 203 dots per inch on 4x6 media.
DPI = 203
WIDTH_IN = 4.0
LENGTH_IN = 6.0

MARGIN = 24          # dots
_BODY = 30           # body font height in dots
_SMALL = 26

# Font 0 is proportional, so a character's advance varies. This is the average
# fraction of the font height a glyph takes, used to wrap conservatively —
# over-wrapping looks fine, over-running the media does not.
_CHAR_RATIO = 0.55


def _sanitize(text: str) -> str:
    """ZPL treats ^ and ~ as command prefixes, so they can't appear in data."""
    return text.replace("^", " ").replace("~", " ").replace("\\", " ").strip()


def _wrap(text: str, height: int, usable: int) -> list[str]:
    chars = max(8, int(usable / (height * _CHAR_RATIO)))
    return textwrap.wrap(text, chars) or [""]


class _Sheet:
    """Accumulates ZPL fields down the label, tracking the vertical cursor."""

    def __init__(self, width: int, length: int):
        self.width = width
        self.length = length
        self.y = MARGIN
        self.parts: list[str] = []

    @property
    def usable(self) -> int:
        return self.width - MARGIN * 2

    def room_for(self, dots: int) -> bool:
        return self.y + dots <= self.length - MARGIN

    def text(self, value: str, height: int = _BODY, indent: int = 0, gap: int = 6) -> None:
        value = _sanitize(value)
        if not value:
            return
        for line in _wrap(value, height, self.usable - indent):
            if not self.room_for(height):
                return
            self.parts.append(
                f"^FO{MARGIN + indent},{self.y}^A0N,{height},{int(height * 0.6)}"
                f"^FD{line}^FS"
            )
            self.y += height + gap

    def rule(self, gap: int = 10) -> None:
        if not self.room_for(4):
            return
        self.parts.append(f"^FO{MARGIN},{self.y}^GB{self.usable},3,3^FS")
        self.y += 3 + gap

    def space(self, dots: int = 12) -> None:
        self.y += dots

    def barcode(self, value: str, height: int = 70) -> None:
        if not self.room_for(height + 40):
            return
        # ^BC: Code 128. The "Y" prints the human-readable number underneath,
        # so the slip is still usable when the scanner isn't to hand.
        self.parts.append(
            f"^FO{MARGIN},{self.y}^BY2,3,{height}^BCN,{height},Y,N,N"
            f"^FD{_sanitize(value)}^FS"
        )
        self.y += height + 40


def render_slip_zpl(
    receipt: dict,
    shop_name: str = "",
    dpi: int = DPI,
    width_in: float = WIDTH_IN,
    length_in: float = LENGTH_IN,
) -> bytes:
    """A pick slip sized for the same media the shipping label prints on."""
    width = int(width_in * dpi)
    length = int(length_in * dpi)
    sheet = _Sheet(width, length)

    created = receipt.get("created_timestamp") or receipt.get("create_timestamp")
    date_str = (
        datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
        if created else ""
    )

    header = "PACKING SLIP"
    if shop_name:
        header = f"{_sanitize(shop_name)}  |  {header}"
    sheet.text(f"{header}   {date_str}".strip(), height=_SMALL, gap=10)

    rid = str(receipt.get("receipt_id", ""))
    sheet.text(f"Order #{rid}", height=46, gap=8)
    sheet.barcode(rid)
    sheet.rule()

    name = receipt.get("name") or ""
    if name:
        sheet.text(f"SHIP TO: {name}", height=_BODY)
    city_line = " ".join(
        p for p in (receipt.get("city", ""), receipt.get("state", ""),
                    receipt.get("zip", "")) if p
    )
    if city_line:
        sheet.text(city_line, height=_SMALL, indent=12)

    sheet.rule()
    sheet.text("ITEMS", height=_SMALL, gap=8)

    transactions = receipt.get("transactions", [])
    for txn in transactions:
        if not sheet.room_for(_BODY * 2):
            sheet.text("... more items — see the dashboard", height=_SMALL)
            break
        qty = txn.get("quantity", 1)
        sheet.text(f"{qty} x {txn.get('title', 'Unknown item')}", height=_BODY, gap=2)
        # The SKU is the thing being matched against the shelf, so it gets its
        # own line rather than being buried in the title.
        if txn.get("sku"):
            sheet.text(f"SKU {txn['sku']}", height=_BODY, indent=18, gap=2)
        for var in txn.get("variations", []):
            label, value = var.get("formatted_name"), var.get("formatted_value")
            if label and value:
                sheet.text(f"{label}: {value}", height=_SMALL, indent=18, gap=2)
        sheet.space(8)

    if receipt.get("is_gift"):
        sheet.rule(gap=6)
        sheet.text("** GIFT **", height=_BODY)
        if receipt.get("gift_message"):
            sheet.text(receipt["gift_message"], height=_SMALL, indent=12)
    if receipt.get("message_from_buyer"):
        sheet.rule(gap=6)
        sheet.text("Note from buyer:", height=_SMALL)
        sheet.text(receipt["message_from_buyer"], height=_SMALL, indent=12)

    body = "".join(sheet.parts)
    # ^CI28 = UTF-8, so buyer names with accents print as themselves.
    return (
        f"^XA^CI28^PW{width}^LL{length}^LH0,0{body}^XZ\n"
    ).encode("utf-8")
