import pytest

from etsy_auto_print.pipeline import poll_once, reprint
from etsy_auto_print.printer import FilePrinter, PrintError, Printer
from etsy_auto_print.store import Store


class FakeClient:
    def __init__(self, receipts):
        self.receipts = receipts

    def get_open_receipts(self):
        return self.receipts


class BrokenPrinter(Printer):
    def print_text(self, name, content):
        raise PrintError("out of paper")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


@pytest.fixture
def printer(tmp_path):
    return FilePrinter(tmp_path / "outbox")


def test_poll_prints_each_order_once(store, printer, receipt, tmp_path):
    client = FakeClient([receipt])
    assert poll_once(client, store, printer) == 1
    assert (tmp_path / "outbox" / "packing-slip-12345.txt").exists()
    # Same receipt still open on Etsy next poll — must not print again.
    assert poll_once(client, store, printer) == 0
    assert store.get(12345)["state"] == "slip_printed"


def test_print_failure_holds_order(store, receipt):
    client = FakeClient([receipt])
    assert poll_once(client, store, BrokenPrinter()) == 0
    row = store.get(12345)
    assert row["state"] == "held"
    assert "out of paper" in row["error"]


def test_reprint_unholds(store, printer, receipt):
    poll_once(FakeClient([receipt]), store, BrokenPrinter())
    destination = reprint(12345, store, printer)
    assert "packing-slip-12345" in destination
    assert store.get(12345)["state"] == "slip_printed"


def test_reprint_unknown_order(store, printer):
    with pytest.raises(KeyError):
        reprint(999, store, printer)


def test_cups_backend_splits_slips_from_labels(tmp_path):
    from etsy_auto_print.config import load_config
    from etsy_auto_print.printer import CupsPrinter, FilePrinter, SplitPrinter, get_printer

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[etsy]\nkeystring = "k"\nshared_secret = "s"\n'
        '[printer]\nbackend = "cups"\ncups_queue = "label"\n'
    )
    printer = get_printer(load_config(cfg_file))
    assert isinstance(printer, SplitPrinter)
    assert isinstance(printer.label_printer, CupsPrinter)
    assert isinstance(printer.slip_printer, FilePrinter)  # no slip_queue set

    cfg_file.write_text(
        '[etsy]\nkeystring = "k"\nshared_secret = "s"\n'
        '[printer]\nbackend = "cups"\ncups_queue = "label"\nslip_queue = "paper"\n'
    )
    printer = get_printer(load_config(cfg_file))
    assert isinstance(printer.slip_printer, CupsPrinter)
    assert printer.slip_printer.queue == "paper"
    assert printer.label_printer.queue == "label"
