"""The pick slip that pairs a shipping label with what goes in the box."""

import re

import pytest

from etsy_auto_print.pipeline import advance_order
from etsy_auto_print.printer import FilePrinter, Printer, SplitPrinter
from etsy_auto_print.store import Store
from etsy_auto_print.zpl import render_slip_zpl


def zpl(receipt, **kw) -> str:
    return render_slip_zpl(receipt, **kw).decode()


def test_slip_carries_the_sku_and_order_number(receipt):
    out = zpl(receipt)
    assert "Order #12345" in out
    assert "SKU STAND-WAL" in out
    assert "Alex Buyer" in out


def test_order_number_is_a_scannable_barcode(receipt):
    # A slip that gets separated from its label can still be matched back.
    out = zpl(receipt)
    assert "^BCN,70,Y,N,N^FD12345^FS" in out


def test_quantities_are_shown(receipt):
    receipt["transactions"] = [
        {"title": "Mug", "sku": "MUG-1", "quantity": 3},
    ]
    assert "3 x Mug" in zpl(receipt)


def test_variations_print_under_their_item(receipt):
    receipt["transactions"] = [{
        "title": "Mug", "sku": "MUG-1", "quantity": 1,
        "variations": [{"formatted_name": "Color", "formatted_value": "Ocean Blue"}],
    }]
    assert "Color: Ocean Blue" in zpl(receipt)


def test_gift_and_buyer_note_are_surfaced(receipt):
    receipt["is_gift"] = True
    receipt["gift_message"] = "Happy birthday!"
    receipt["message_from_buyer"] = "Please ship after the 20th."
    out = zpl(receipt)
    assert "** GIFT **" in out
    assert "Happy birthday!" in out
    assert "Please ship after the 20th." in out


def test_zpl_control_characters_in_buyer_data_are_neutralised(receipt):
    # ^ and ~ start ZPL commands; a buyer's note must not be able to issue one.
    receipt["message_from_buyer"] = "hi ^XA~JA there"
    out = zpl(receipt)
    assert "^XA~JA" not in out.replace("^XA^CI28", "")
    assert out.startswith("^XA^CI28") and out.rstrip().endswith("^XZ")


def test_media_size_is_declared_from_the_dpi(receipt):
    out = zpl(receipt)                       # 203dpi 4x6 default
    assert "^PW812" in out and "^LL1218" in out
    dense = zpl(receipt, dpi=300)
    assert "^PW1200" in dense and "^LL1800" in dense


def test_long_orders_stop_at_the_media_edge(receipt):
    receipt["transactions"] = [
        {"title": f"Very long product title number {i}", "sku": f"SKU-{i}", "quantity": 1}
        for i in range(40)
    ]
    out = zpl(receipt)
    assert "more items" in out
    # Nothing may be positioned past the bottom of the label.
    ys = [int(m) for m in re.findall(r"\^FO\d+,(\d+)", out)]
    assert max(ys) <= 1218


def test_utf8_is_enabled_for_accented_names(receipt):
    receipt["name"] = "Renée Müller"
    out = zpl(receipt)
    assert "^CI28" in out
    assert "Renée Müller" in out


# --- routing --------------------------------------------------------------


class RecordingPrinter(Printer):
    def __init__(self):
        self.text, self.binary = [], []

    def print_text(self, name, content):
        self.text.append(name)
        return "text:" + name

    def print_bytes(self, name, data, ext):
        self.binary.append((name, ext))
        return f"bytes:{name}.{ext}"


def test_zpl_slips_go_to_the_label_queue_not_the_slip_queue():
    # The whole point is that both come out of the one printer, in order.
    slips, labels = RecordingPrinter(), RecordingPrinter()
    printer = SplitPrinter(slips, labels, slip_zpl=True)
    printer.print_slip("packing-slip-1", "text form", b"^XA^XZ")
    assert labels.binary == [("packing-slip-1", "zpl")]
    assert slips.text == []


def test_text_slips_still_go_to_the_slip_queue():
    slips, labels = RecordingPrinter(), RecordingPrinter()
    printer = SplitPrinter(slips, labels, slip_zpl=False)
    printer.print_slip("packing-slip-1", "text form", b"^XA^XZ")
    assert slips.text == ["packing-slip-1"]
    assert labels.binary == []


def test_file_backend_mirrors_whichever_form_is_configured(tmp_path):
    FilePrinter(tmp_path / "a").print_slip("s", "text form", b"^XA^XZ")
    assert (tmp_path / "a" / "s.txt").read_text() == "text form"
    FilePrinter(tmp_path / "b", slip_zpl=True).print_slip("s", "text form", b"^XA^XZ")
    assert (tmp_path / "b" / "s.zpl").read_bytes() == b"^XA^XZ"


def test_pipeline_prints_the_slip_before_the_label(tmp_path, receipt):
    from .test_labels import FakeShippo, make_labeler

    store = Store(tmp_path / "orders.db")
    printer = RecordingPrinter()
    printer.print_slip = lambda name, text, z: printer.print_bytes(name, z, "zpl")
    store.register(receipt)
    advance_order(receipt, store, printer, make_labeler(store, printer, FakeShippo()))
    # Slip first, then that order's label — the pair is adjacent in the stack.
    assert [n for n, _ in printer.binary] == ["packing-slip-12345", "label-12345"]
