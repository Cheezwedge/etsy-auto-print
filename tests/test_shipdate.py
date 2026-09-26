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


def test_shipdate_module_names_both_policies():
    assert set(shipdate.POLICIES) == {IMMEDIATELY, NEXT_BUSINESS_DAY}


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
