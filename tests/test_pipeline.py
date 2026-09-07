import pytest

from etsy_auto_print.pipeline import advance_order, poll_once, reprint
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


# --- digital downloads ------------------------------------------------------


def digital_receipt(**kw):
    return {
        "receipt_id": 555, "name": "A Buyer",
        "transactions": [
            {"title": "3D Print Files", "sku": "", "quantity": 1, "is_digital": True}
        ],
        **kw,
    }


def test_a_download_order_prints_nothing_and_completes(tmp_path):
    # An instant download has no SKU, no weight and no parcel: the pipeline
    # would print a pick slip for nothing, then hold forever on the missing
    # weight — a false alarm on every digital sale.
    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    receipt = digital_receipt()
    store.register(receipt)

    assert advance_order(receipt, store, printer) is True
    assert store.get(555)["state"] == "done"
    assert not list((tmp_path / "outbox").glob("*")), "nothing should have printed"


def test_a_download_order_raises_no_alarm(tmp_path):
    store = Store(tmp_path / "orders.db")
    sent = []
    receipt = digital_receipt()
    store.register(receipt)
    advance_order(
        receipt, store, FilePrinter(tmp_path / "outbox"),
        notifier=type("N", (), {"send": lambda self, *a: sent.append(a)})(),
    )
    assert sent == []


def test_a_download_order_is_not_repolled_forever(tmp_path):
    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    receipt = digital_receipt()
    store.register(receipt)
    advance_order(receipt, store, printer)
    # Already done: a second pass must report no movement, not redo it.
    assert advance_order(receipt, store, printer) is False


def test_a_mixed_order_still_ships(tmp_path):
    # One download alongside something physical is a parcel like any other.
    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    receipt = digital_receipt(transactions=[
        {"title": "3D Print Files", "sku": "", "quantity": 1, "is_digital": True},
        {"title": "Adapters", "sku": "TPU-A", "quantity": 1, "is_digital": False},
    ])
    store.register(receipt)
    advance_order(receipt, store, printer)
    assert store.get(555)["state"] == "slip_printed"
    assert list((tmp_path / "outbox").glob("packing-slip-*"))


def test_an_order_with_no_recognisable_lines_is_treated_as_physical(tmp_path):
    # Holding a digital order is an annoyance; silently completing a real
    # one loses a parcel. The unknown case has to fall the safe way.
    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    receipt = digital_receipt(transactions=[])
    store.register(receipt)
    advance_order(receipt, store, printer)
    assert store.get(555)["state"] != "done"


def test_a_missing_is_digital_flag_is_treated_as_physical(tmp_path):
    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    receipt = digital_receipt(transactions=[{"title": "Thing", "sku": "X", "quantity": 1}])
    store.register(receipt)
    advance_order(receipt, store, printer)
    assert store.get(555)["state"] == "slip_printed"


# --- telling you an order arrived -------------------------------------------


class Recorder:
    def __init__(self, on_order=True):
        self.on_order = on_order
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))

    def send_order_ready(self, title, message):
        if self.on_order:
            self.send(title, message)


def paid_receipt():
    return {
        "receipt_id": 700, "name": "A Buyer",
        "transactions": [
            {"title": "Adapters", "sku": "TPU-A", "quantity": 2, "is_digital": False}
        ],
    }


def test_a_printed_order_says_so(tmp_path):
    # A working order used to be completely silent: the label just appeared on
    # the printer, which is only a notification if you're standing next to it.
    store = Store(tmp_path / "orders.db")
    receipt = paid_receipt()
    store.register(receipt)
    notifier = Recorder()

    advance_order(receipt, store, FilePrinter(tmp_path / "outbox"), notifier=notifier)
    assert len(notifier.sent) == 1
    title, message = notifier.sent[0]
    assert "700" in title and "ready to pack" in title
    assert "2 x TPU-A" in message          # what actually goes in the box
    assert "A Buyer" in message


def test_it_does_not_repeat_on_every_poll(tmp_path):
    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    receipt = paid_receipt()
    store.register(receipt)
    notifier = Recorder()

    advance_order(receipt, store, printer, notifier=notifier)
    advance_order(receipt, store, printer, notifier=notifier)
    advance_order(receipt, store, printer, notifier=notifier)
    assert len(notifier.sent) == 1


def test_a_held_order_gets_the_problem_alert_not_the_ready_one(tmp_path):
    class Broken(FilePrinter):
        def print_slip(self, *a):
            raise PrintError("printer offline")

    store = Store(tmp_path / "orders.db")
    receipt = paid_receipt()
    store.register(receipt)
    notifier = Recorder()

    advance_order(receipt, store, Broken(tmp_path / "outbox"), notifier=notifier)
    assert len(notifier.sent) == 1
    assert "needs attention" in notifier.sent[0][0]


def test_a_shop_that_ships_all_day_can_turn_it_off(tmp_path):
    store = Store(tmp_path / "orders.db")
    receipt = paid_receipt()
    store.register(receipt)
    notifier = Recorder(on_order=False)

    advance_order(receipt, store, FilePrinter(tmp_path / "outbox"), notifier=notifier)
    assert notifier.sent == []


def test_a_digital_order_is_not_announced_as_ready_to_pack(tmp_path):
    # It goes straight to done with nothing to pack; a "ready to pack" push
    # would send someone to an empty printer.
    store = Store(tmp_path / "orders.db")
    receipt = digital_receipt()
    store.register(receipt)
    notifier = Recorder()
    advance_order(receipt, store, FilePrinter(tmp_path / "outbox"), notifier=notifier)
    assert notifier.sent == []
