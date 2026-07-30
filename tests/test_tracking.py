import pytest

from etsy_auto_print.etsy import EtsyApiError
from etsy_auto_print.pipeline import advance_order, post_tracking
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.store import Store

from .test_labels import FakeShippo, make_labeler


class FakeEtsy:
    def __init__(self, fail=False, reject_extras=False):
        self.fail = fail
        self.reject_extras = reject_extras
        self.shipments = []
        self.calls = []

    def create_receipt_shipment(
        self, receipt_id, tracking_code, carrier_name, send_bcc=True, **extras
    ):
        self.calls.append(extras)
        if self.fail:
            raise EtsyApiError(500, "server error")
        if self.reject_extras and extras:
            raise EtsyApiError(400, "invalid ship_date")
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


def test_tracking_upload_includes_the_shipment_details(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, LiveFakeShippo())
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    extras = etsy.calls[0]
    assert extras["mail_class"] == "Ground Advantage"
    assert extras["weight"] == 15.5 and extras["weight_units"] == "oz"
    # Longest side first, as Etsy documents the fields.
    assert (extras["length"], extras["width"], extras["height"]) == (10.0, 7.0, 4.0)
    assert extras["dimension_units"] == "in"
    assert extras["shipping_label_cost"] == 5.50
    assert extras["shipping_label_currency"] == "USD"
    assert len(extras["ship_date"]) == len("2026-07-30")


def test_rejected_details_fall_back_to_tracking_only(store, printer, receipt):
    """The extras are cosmetic; the tracking number is not. A 400 on the rich
    payload must not hold an order the buyer is waiting on."""
    store.register(receipt)
    labeler = make_labeler(store, printer, LiveFakeShippo())
    etsy = FakeEtsy(reject_extras=True)
    assert advance_order(receipt, store, printer, labeler, etsy) is True
    assert store.get(12345)["state"] == "done"
    assert len(etsy.calls) == 2 and etsy.calls[0] and etsy.calls[1] == {}
    assert etsy.shipments == [(12345, "9400TEST", "usps")]


def test_server_error_still_holds_without_a_retry(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, LiveFakeShippo())
    etsy = FakeEtsy(fail=True)
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "held"
    assert len(etsy.calls) == 1  # a 5xx is not a payload problem


def test_label_row_without_parcel_columns_sends_what_it_has(store, printer, receipt):
    """Labels bought before the parcel columns existed still post tracking."""
    store.register(receipt)
    store.transition(12345, "slip_printed", "")
    store.save_label(
        12345, carrier="USPS", service="Priority Mail", amount="",
        tracking_number="9400OLD", is_test=False,
    )
    store.transition(12345, "label_purchased", "")
    store.transition(12345, "label_printed", "")
    etsy = FakeEtsy()
    assert post_tracking(12345, store, etsy) is True
    extras = etsy.calls[0]
    assert extras["mail_class"] == "Priority Mail"
    assert "weight" not in extras and "length" not in extras
    assert "shipping_label_cost" not in extras


def test_hold_notification_on_label_failure(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, FakeShippo(fail_buy=True))
    notifier = RecordingNotifier()
    advance_order(receipt, store, printer, labeler, None, notifier)
    assert store.get(12345)["state"] == "held"
    assert len(notifier.sent) == 1
    assert "12345" in notifier.sent[0][0]
