import pytest

from etsy_auto_print.etsy import EtsyApiError
from etsy_auto_print.pipeline import advance_order, post_tracking
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.store import Store

from .test_labels import FakeShippo, make_labeler


class FakeEtsy:
    def __init__(self, fail=False):
        self.fail = fail
        self.shipments = []

    def create_receipt_shipment(self, receipt_id, tracking_code, carrier_name, send_bcc=True):
        if self.fail:
            raise EtsyApiError(500, "server error")
        self.shipments.append((receipt_id, tracking_code, carrier_name))
        return {}


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))


class LiveFakeShippo(FakeShippo):
    is_test = False


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


@pytest.fixture
def printer(tmp_path):
    return FilePrinter(tmp_path / "outbox")


def test_live_label_posts_tracking_and_completes(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, LiveFakeShippo())
    etsy = FakeEtsy()
    assert advance_order(receipt, store, printer, labeler, etsy) is True
    assert store.get(12345)["state"] == "done"
    assert etsy.shipments == [(12345, "9400TEST", "usps")]


def test_test_label_never_posts_tracking(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, FakeShippo())  # test-mode client
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    # Stops at label_printed: fake tracking must never reach a buyer.
    assert store.get(12345)["state"] == "label_printed"
    assert etsy.shipments == []
    # And repeated polls don't change that or error.
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "label_printed"


def test_tracking_failure_holds_and_notifies(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, LiveFakeShippo())
    notifier = RecordingNotifier()
    advance_order(receipt, store, printer, labeler, FakeEtsy(fail=True), notifier)
    row = store.get(12345)
    assert row["state"] == "held"
    assert "tracking upload to Etsy failed" in row["error"]
    assert len(notifier.sent) == 1
    assert "retry 12345" in notifier.sent[0][1]


def test_post_tracking_without_label_reports_error(store, printer, receipt):
    store.register(receipt)
    store.transition(12345, "slip_printed", "")
    store.transition(12345, "label_purchased", "")
    store.transition(12345, "label_printed", "")  # inconsistent: no label row
    result = post_tracking(12345, store, FakeEtsy())
    assert isinstance(result, str) and "no label recorded" in result


def test_hold_notification_on_label_failure(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, FakeShippo(fail_buy=True))
    notifier = RecordingNotifier()
    advance_order(receipt, store, printer, labeler, None, notifier)
    assert store.get(12345)["state"] == "held"
    assert len(notifier.sent) == 1
    assert "12345" in notifier.sent[0][0]
