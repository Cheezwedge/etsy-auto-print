"""Telling Etsy "shipped" when the parcel ships, not when the label prints.

Posting tracking at print time sends the buyer a "shipped" email early, and
Etsy computes its delivery estimate from that moment — so a parcel that sits
until the next post-office run gets an estimate that is days optimistic.
"""

from datetime import date, datetime, timedelta

import pytest

from etsy_auto_print import pipeline, shipdate
from etsy_auto_print.pipeline import advance_order, tracking_is_due
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.shipdate import (
    IMMEDIATELY,
    NEXT_BUSINESS_DAY,
    next_business_day,
    postal_holidays,
    ship_day,
    tracking_due,
)
from etsy_auto_print.store import Store

from .test_labels import FakeShippo, make_labeler
from .test_tracking import FakeEtsy, RecordingNotifier


def at(y, m, d, hour=12) -> float:
    """A local-time timestamp, so the tests don't depend on the machine's zone."""
    return datetime(y, m, d, hour).timestamp()


# --- the calendar -----------------------------------------------------------


def test_the_2026_postal_holidays_are_right():
    assert postal_holidays(2026) >= {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7),
        date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26),
        date(2026, 12, 25),
    }


def test_a_saturday_holiday_is_observed_on_the_friday():
    # July 4 2026 is a Saturday; the post office closes Friday the 3rd.
    assert date(2026, 7, 3) in postal_holidays(2026)


def test_a_saturday_new_year_closes_the_previous_december_31():
    # Jan 1 2028 is a Saturday, observed Friday Dec 31 2027.
    assert date(2027, 12, 31) in postal_holidays(2027)


def test_friday_ships_on_monday():
    assert next_business_day(date(2026, 9, 25)) == date(2026, 9, 28)


def test_the_friday_before_labor_day_ships_on_tuesday():
    assert next_business_day(date(2026, 9, 4)) == date(2026, 9, 8)


def test_thanksgiving_is_skipped_but_black_friday_is_a_business_day():
    assert next_business_day(date(2026, 11, 25)) == date(2026, 11, 27)


def test_christmas_eve_ships_after_the_weekend():
    assert next_business_day(date(2026, 12, 24)) == date(2026, 12, 28)


def test_a_weekend_print_ships_on_monday():
    assert ship_day(at(2026, 9, 26), None, NEXT_BUSINESS_DAY) == date(2026, 9, 28)


# --- Etsy's ship-by date wins ------------------------------------------------


def receipt_due(y, m, d):
    return {"transactions": [{"expected_ship_date": int(at(y, m, d, 23))}]}


def test_it_never_waits_past_etsys_ship_by_date():
    # A late mark-as-shipped is what Etsy's seller metrics penalise; an early
    # delivery estimate is the lesser problem.
    printed_friday = at(2026, 9, 25)
    assert ship_day(printed_friday, receipt_due(2026, 9, 25), NEXT_BUSINESS_DAY) \
        == date(2026, 9, 25)


def test_the_earliest_line_sets_the_deadline():
    receipt = {"transactions": [
        {"expected_ship_date": int(at(2026, 10, 9))},
        {"expected_ship_date": int(at(2026, 9, 25))},
    ]}
    assert ship_day(at(2026, 9, 25), receipt, NEXT_BUSINESS_DAY) == date(2026, 9, 25)


def test_a_distant_deadline_does_not_move_the_ship_day():
    assert ship_day(at(2026, 9, 25), receipt_due(2026, 10, 9), NEXT_BUSINESS_DAY) \
        == date(2026, 9, 28)


def test_a_deadline_already_passed_posts_immediately():
    now = datetime(2026, 9, 29, 6)          # before the usual posting hour
    assert tracking_due(now, at(2026, 9, 25), receipt_due(2026, 9, 26),
                        NEXT_BUSINESS_DAY, 8)


# --- the time of day ----------------------------------------------------------


def test_not_before_the_ship_day():
    now = datetime(2026, 9, 27, 20)
    assert not tracking_due(now, at(2026, 9, 25), None, NEXT_BUSINESS_DAY, 8)


def test_not_before_the_posting_hour_on_the_day():
    # The buyer's "shipped" email shouldn't land at 00:03.
    now = datetime(2026, 9, 28, 7, 59)
    assert not tracking_due(now, at(2026, 9, 25), None, NEXT_BUSINESS_DAY, 8)


def test_from_the_posting_hour_on():
    now = datetime(2026, 9, 28, 8)
    assert tracking_due(now, at(2026, 9, 25), None, NEXT_BUSINESS_DAY, 8)


def test_a_missed_day_still_posts():
    # The Pi was off all Monday; it must catch up, not wait for another Monday.
    now = datetime(2026, 9, 30, 3)
    assert tracking_due(now, at(2026, 9, 25), None, NEXT_BUSINESS_DAY, 8)


def test_immediately_is_always_due():
    assert tracking_due(datetime(2026, 9, 25, 0), at(2026, 9, 25), None, IMMEDIATELY, 8)


# --- in the pipeline ----------------------------------------------------------


class LiveFakeShippo(FakeShippo):
    is_test = False


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


@pytest.fixture
def printer(tmp_path):
    return FilePrinter(tmp_path / "outbox")


def waiting_labeler(store, printer, client=None):
    return make_labeler(store, printer, client or LiveFakeShippo(),
                        mark_shipped=NEXT_BUSINESS_DAY)


def frozen_at(monkeypatch, moment: datetime):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return moment.date()

    monkeypatch.setattr(pipeline, "datetime", Frozen)
    monkeypatch.setattr(pipeline, "date", FrozenDate)


def test_the_label_prints_but_etsy_is_not_told_yet(store, printer, receipt):
    store.register(receipt)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, waiting_labeler(store, printer), etsy)
    assert store.get(12345)["state"] == "label_printed"
    assert etsy.shipments == []
    assert list((printer.outbox).glob("label-*"))          # it did print


def test_etsy_is_told_on_the_ship_day(store, printer, receipt, monkeypatch):
    store.register(receipt)
    labeler = waiting_labeler(store, printer)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)

    frozen_at(monkeypatch, datetime.now().replace(hour=10) + timedelta(days=5))
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "done"
    assert etsy.shipments == [(12345, "9400TEST", "usps")]


def test_the_ship_date_sent_is_the_day_it_is_posted(store, printer, receipt, monkeypatch):
    # Previously the label's purchase date, in UTC — which on a US evening is
    # already tomorrow.
    store.register(receipt)
    labeler = waiting_labeler(store, printer)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    posting = datetime.now().replace(hour=10) + timedelta(days=5)
    frozen_at(monkeypatch, posting)
    advance_order(receipt, store, printer, labeler, etsy)
    assert etsy.calls[0]["ship_date"] == posting.date().isoformat()


def test_the_ready_to_pack_push_says_when_etsy_will_hear(store, printer, receipt):
    store.register(receipt)
    notifier = RecordingNotifier()
    notifier.send_order_ready = notifier.send
    advance_order(receipt, store, printer, waiting_labeler(store, printer),
                  FakeEtsy(), notifier)
    assert "Etsy will be told it shipped on" in notifier.sent[0][1]


def test_a_test_label_never_promises_an_etsy_date(store, printer, receipt):
    store.register(receipt)
    notifier = RecordingNotifier()
    notifier.send_order_ready = notifier.send
    labeler = make_labeler(store, printer, FakeShippo(), mark_shipped=NEXT_BUSINESS_DAY)
    advance_order(receipt, store, printer, labeler, FakeEtsy(), notifier)
    assert "Etsy will be told" not in notifier.sent[0][1]


def test_an_order_due_today_by_etsys_deadline_goes_straight_through(
    store, printer, receipt
):
    receipt["transactions"][0]["expected_ship_date"] = int(datetime.now().timestamp())
    store.register(receipt)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, waiting_labeler(store, printer), etsy)
    assert store.get(12345)["state"] == "done"


def test_a_label_without_a_timestamp_is_not_held_back(store, printer, receipt):
    store.register(receipt)
    assert tracking_is_due(12345, receipt, store, None) is True


def test_the_default_is_next_business_day(tmp_path):
    from etsy_auto_print.config import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text('[etsy]\nkeystring = "k"\nshared_secret = "s"\n[labels]\nenabled = false\n')
    labels = load_config(cfg).labels
    assert labels.mark_shipped == NEXT_BUSINESS_DAY
    assert labels.mark_shipped_hour == 8


@pytest.mark.parametrize("line, message", [
    ('mark_shipped = "tomorrow"', "expected one of"),
    ("mark_shipped_hour = 25", "0-23"),
])
def test_bad_settings_are_rejected(tmp_path, line, message):
    from etsy_auto_print.config import ConfigError, load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text('[etsy]\nkeystring = "k"\nshared_secret = "s"\n'
                   f"[labels]\nenabled = false\n{line}\n")
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)


def test_shipdate_module_names_every_policy():
    assert set(shipdate.POLICIES) == {
        IMMEDIATELY, NEXT_BUSINESS_DAY, shipdate.CARRIER_SCAN,
    }


# --- shipping same-day: mark-shipped ------------------------------------------

import argparse  # noqa: E402
import io  # noqa: E402

from etsy_auto_print import cli  # noqa: E402


def printed_and_waiting(store, printer, receipt):
    store.register(receipt)
    advance_order(receipt, store, printer, waiting_labeler(store, printer), FakeEtsy())
    assert store.get(receipt["receipt_id"])["state"] == "label_printed"


def run_mark(monkeypatch, store, etsy, ids=(), all_=False, stdin=""):
    monkeypatch.setattr(cli, "_build_client", lambda cfg: etsy)
    monkeypatch.setattr(cli, "Store", lambda path: store)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    config = type("C", (), {"db_path": None})()
    return cli.cmd_mark_shipped(
        config, argparse.Namespace(receipt_ids=list(ids), all=all_)
    )


def test_marking_one_order_tells_etsy_now(store, printer, receipt, monkeypatch, capsys):
    printed_and_waiting(store, printer, receipt)
    etsy = FakeEtsy()
    assert run_mark(monkeypatch, store, etsy, ids=[12345]) == 0
    assert etsy.shipments == [(12345, "9400TEST", "usps")]
    assert store.get(12345)["state"] == "done"
    assert "shipped today" in capsys.readouterr().out


def test_mark_all_covers_every_waiting_order(store, printer, receipt, monkeypatch):
    printed_and_waiting(store, printer, receipt)
    other = dict(receipt, receipt_id=67890)
    printed_and_waiting(store, printer, other)
    etsy = FakeEtsy()
    run_mark(monkeypatch, store, etsy, all_=True)
    assert sorted(s[0] for s in etsy.shipments) == [12345, 67890]


def test_scanned_slips_are_read_until_a_blank_line(store, printer, receipt, monkeypatch):
    # A USB barcode scanner types the slip's barcode and presses Enter.
    printed_and_waiting(store, printer, receipt)
    etsy = FakeEtsy()
    run_mark(monkeypatch, store, etsy, stdin="12345\nnot-a-number\n\n99999\n")
    assert etsy.shipments == [(12345, "9400TEST", "usps")]


def test_an_order_not_waiting_is_left_alone(store, printer, receipt, monkeypatch, capsys):
    store.register(receipt)                 # still 'new': nothing printed
    etsy = FakeEtsy()
    assert run_mark(monkeypatch, store, etsy, ids=[12345]) == 0
    assert etsy.shipments == []
    assert "nothing to mark" in capsys.readouterr().out


def test_an_unknown_order_number_fails_loudly(store, monkeypatch, capsys):
    assert run_mark(monkeypatch, store, FakeEtsy(), ids=[424242]) == 1
    assert "not an order" in capsys.readouterr().out


def test_an_etsy_failure_is_reported_and_the_order_stays_waiting(
    store, printer, receipt, monkeypatch
):
    printed_and_waiting(store, printer, receipt)
    assert run_mark(monkeypatch, store, FakeEtsy(fail=True), ids=[12345]) == 1
    # Not held: the order is fine, Etsy was just unavailable. It still posts
    # by itself on the ship day, or the next time this is run.
    assert store.get(12345)["state"] == "label_printed"


def test_nothing_waiting_says_so(store, monkeypatch, capsys):
    assert run_mark(monkeypatch, store, FakeEtsy(), all_=True) == 0
    assert "Nothing waiting" in capsys.readouterr().out


# --- carrier_scan: wait for USPS, but never past Etsy's ship-by date --------

from etsy_auto_print.shipdate import CARRIER_SCAN, promise, waits_for_carrier  # noqa: E402


def test_with_a_ship_by_date_the_latest_day_is_that_date():
    assert ship_day(at(2026, 9, 25), receipt_due(2026, 9, 30), CARRIER_SCAN) \
        == date(2026, 9, 30)


def test_without_a_ship_by_date_it_behaves_like_next_business_day():
    # Waiting on a deadline it can't see is exactly how a shop ends up late.
    assert ship_day(at(2026, 9, 25), None, CARRIER_SCAN) == date(2026, 9, 28)
    assert not waits_for_carrier(CARRIER_SCAN, None)


def test_the_deadline_day_posts_even_with_no_scan():
    now = datetime(2026, 9, 30, 0, 5)
    assert tracking_due(now, at(2026, 9, 25), receipt_due(2026, 9, 30),
                        CARRIER_SCAN, 8)


def test_before_the_deadline_the_date_alone_does_not_post():
    now = datetime(2026, 9, 28, 15)
    assert not tracking_due(now, at(2026, 9, 25), receipt_due(2026, 9, 30),
                            CARRIER_SCAN, 8)


def test_the_promise_says_what_will_actually_happen():
    assert "when USPS scans it" in promise(CARRIER_SCAN, date(2026, 9, 30),
                                           receipt_due(2026, 9, 30))
    # No deadline means no scan-waiting, so it mustn't claim otherwise.
    assert "when USPS scans" not in promise(CARRIER_SCAN, date(2026, 9, 28), None)


class TrackingShippo(LiveFakeShippo):
    def __init__(self, status="PRE_TRANSIT", fail=False):
        super().__init__()
        self.status = status
        self.fail = fail
        self.asked = 0

    def tracking_status(self, carrier, number):
        self.asked += 1
        if self.fail:
            from etsy_auto_print.shippo import ShippoError
            raise ShippoError("could not reach Shippo")
        return self.status


@pytest.fixture(autouse=True)
def fresh_scan_throttle(monkeypatch):
    monkeypatch.setattr(pipeline, "_last_scan_check", {})


def scanning(store, printer, receipt, shippo, deadline_days=5):
    receipt["transactions"][0]["expected_ship_date"] = int(
        (datetime.now() + timedelta(days=deadline_days)).timestamp())
    store.register(receipt)
    labeler = make_labeler(store, printer, shippo, mark_shipped=CARRIER_SCAN)
    return labeler


def test_a_usps_scan_tells_etsy_straight_away(store, printer, receipt):
    shippo = TrackingShippo(status="TRANSIT")
    labeler = scanning(store, printer, receipt, shippo)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "done"
    assert etsy.shipments == [(12345, "9400TEST", "usps")]


def test_label_created_is_not_a_scan(store, printer, receipt):
    shippo = TrackingShippo(status="PRE_TRANSIT")
    labeler = scanning(store, printer, receipt, shippo)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "label_printed"
    assert etsy.shipments == []


def test_no_scan_by_the_ship_by_date_posts_anyway(store, printer, receipt, monkeypatch):
    shippo = TrackingShippo(status="PRE_TRANSIT")
    labeler = scanning(store, printer, receipt, shippo, deadline_days=2)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    frozen_at(monkeypatch, datetime.now() + timedelta(days=2))
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "done"


def test_a_shippo_outage_delays_but_never_makes_the_shop_late(
    store, printer, receipt, monkeypatch
):
    shippo = TrackingShippo(fail=True)
    labeler = scanning(store, printer, receipt, shippo, deadline_days=2)
    etsy = FakeEtsy()
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "label_printed"     # not held
    frozen_at(monkeypatch, datetime.now() + timedelta(days=2))
    advance_order(receipt, store, printer, labeler, etsy)
    assert store.get(12345)["state"] == "done"


def test_usps_is_not_asked_on_every_poll(store, printer, receipt):
    shippo = TrackingShippo(status="PRE_TRANSIT")
    labeler = scanning(store, printer, receipt, shippo)
    etsy = FakeEtsy()
    for _ in range(5):
        advance_order(receipt, store, printer, labeler, etsy)
    assert shippo.asked == 1


def test_an_order_without_a_ship_by_date_never_waits_on_usps(store, printer, receipt):
    shippo = TrackingShippo(status="TRANSIT")
    store.register(receipt)                       # no expected_ship_date
    labeler = make_labeler(store, printer, shippo, mark_shipped=CARRIER_SCAN)
    advance_order(receipt, store, printer, labeler, FakeEtsy())
    assert shippo.asked == 0
    assert store.get(12345)["state"] == "label_printed"   # next business day


# --- the ship-by check command -------------------------------------------------


def run_ship_by(monkeypatch, receipts):
    monkeypatch.setattr(cli, "_build_client", lambda cfg: type(
        "C", (), {"get_recent_receipts": lambda self, limit: receipts})())
    return cli.cmd_ship_by(None, argparse.Namespace(limit=10))


def test_ship_by_reports_readable_dates(monkeypatch, capsys):
    code = run_ship_by(monkeypatch, [{
        "receipt_id": 1, "created_timestamp": int(at(2026, 9, 25)),
        "transactions": [{"expected_ship_date": int(at(2026, 9, 28))}],
    }])
    out = capsys.readouterr().out
    assert code == 0
    assert "ship by Mon Sep 28" in out
    assert "UTC" in out                    # the raw evidence, not just a verdict
    assert "carrier_scan" in out


def test_ship_by_calls_out_same_day_deadlines(monkeypatch, capsys):
    # A ship-by equal to the order day makes every waiting mode a no-op:
    # waiting at all would be a late shipment. That must be said, not hidden.
    code = run_ship_by(monkeypatch, [{
        "receipt_id": 1, "created_timestamp": int(at(2026, 9, 26, 9)),
        "transactions": [{"expected_ship_date": int(at(2026, 9, 26, 23))}],
    }])
    out = capsys.readouterr().out
    assert "same day as placed" in out
    assert "processing time" in out
    assert "Safe to use" not in out


def test_a_cancelled_order_is_not_a_missing_field(monkeypatch, capsys):
    # The field is there but blank because the order was cancelled; sending
    # someone to look for a different field name was wrong.
    code = run_ship_by(monkeypatch, [{
        "receipt_id": 4, "status": "Canceled",
        "transactions": [{"expected_ship_date": None}],
    }])
    out = capsys.readouterr().out
    assert code == 0
    assert "not shipping" in out and "MISSING" not in out


def test_a_blank_field_on_an_open_order_is_reported_as_blank(monkeypatch, capsys):
    code = run_ship_by(monkeypatch, [{
        "receipt_id": 5, "status": "Paid",
        "transactions": [{"expected_ship_date": None}],
    }])
    out = capsys.readouterr().out
    assert code == 1
    assert "EMPTY" in out and "present but blank" in out


def test_ship_by_names_the_fields_etsy_did_send(monkeypatch, capsys):
    code = run_ship_by(monkeypatch, [{
        "receipt_id": 2, "created_timestamp": int(at(2026, 9, 25)),
        "transactions": [{"ship_by_timestamp": 1, "shipping_method": "x"}],
    }])
    out = capsys.readouterr().out
    assert code == 1
    assert "MISSING" in out
    assert "ship_by_timestamp" in out and "shipping_method" in out


def test_ship_by_does_not_flag_digital_orders(monkeypatch, capsys):
    code = run_ship_by(monkeypatch, [{
        "receipt_id": 3, "transactions": [{"is_digital": True}],
    }])
    assert code == 0
    assert "nothing to ship" in capsys.readouterr().out


def test_track_reports_a_scan(store, printer, receipt, monkeypatch, capsys):
    labeler = scanning(store, printer, receipt, TrackingShippo(status="TRANSIT"))
    advance_order(receipt, store, printer, labeler, None)     # print, no Etsy
    monkeypatch.setattr(cli, "Store", lambda path: store)
    monkeypatch.setattr(cli, "make_client",
                        lambda token, allow_live: TrackingShippo(status="TRANSIT"))
    config = type("C", (), {"db_path": None, "labels": type(
        "L", (), {"token": "t", "allow_live": True})()})()
    assert cli.cmd_track(config, argparse.Namespace(receipt_id=12345)) == 0
    out = capsys.readouterr().out
    assert "Shippo status: TRANSIT" in out and "USPS has it" in out
