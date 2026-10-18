"""Label delay: wait, re-check, then buy postage once.

The wait is part of the order row, so closing the database and opening it
again — a Pi reboot — must not lose the order and must not buy twice.
"""

import copy
import json
import logging
import sqlite3
import time

import pytest

from etsy_auto_print.etsy import EtsyApiError
from etsy_auto_print.pipeline import (
    describe_delay,
    format_countdown,
    parse_delay,
    poll_once,
    print_now_order,
    set_label_delay,
    skip_waiting_order,
)
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.store import Store
from .test_labels import FakeShippo, make_labeler

PLACED = 1_752_000_000  # the receipt fixture's created_timestamp
DELAY = 30 * 60


class RecordingShippo(FakeShippo):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_to = None

    def create_address(self, address, validate=True):
        self.last_to = address
        return super().create_address(address, validate)


class Etsy:
    def __init__(self, receipt, fresh=None, fail=None):
        self.receipts = [receipt]
        # Kept even when `receipts` is emptied, so a poll that simply doesn't
        # list the order is not the same thing as Etsy deleting it.
        self.known = {receipt["receipt_id"]: receipt}
        self.fresh = fresh
        self.fail = fail
        self.fetched = []

    def get_open_receipts(self):
        return list(self.receipts)

    def get_receipt(self, receipt_id):
        self.fetched.append(receipt_id)
        if self.fail is not None:
            raise self.fail
        if self.fresh is not None:
            return self.fresh
        if receipt_id in self.known:
            return self.known[receipt_id]
        raise EtsyApiError(404, "not found")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


@pytest.fixture
def printer(tmp_path):
    return FilePrinter(tmp_path / "outbox")


def run(client, store, printer, shippo, delay, now):
    labeler = make_labeler(store, printer, shippo)
    return poll_once(
        client, store, printer, labeler, delay_seconds=delay, now=now,
    )


def files(tmp_path):
    return list((tmp_path / "outbox").glob("*"))


def test_delay_zero_buys_immediately_and_does_not_recheck(store, printer, receipt, tmp_path):
    client = Etsy(receipt)
    client.get_receipt = lambda rid: (_ for _ in ()).throw(AssertionError("re-checked"))
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=0, now=PLACED + 5)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"
    # A second poll, still with delay 0, must not buy again.
    run(client, store, printer, shippo, delay=0, now=PLACED + 10)
    assert shippo.buy_calls == 1


def test_a_delay_holds_the_order_until_it_elapses(store, printer, receipt, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="etsy-auto-print")
    client = Etsy(receipt)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + 10)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    assert client.fetched == []
    assert files(tmp_path) == []
    assert "before buying postage" in caplog.text

    # Still inside the window: no second "holding" event, still no purchase.
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY - 1)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    assert [e["to_state"] for e in store.events(12345)].count("waiting") == 1

    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"
    assert client.fetched == [12345]
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 3600)
    assert shippo.buy_calls == 1


def test_lengthening_the_delay_keeps_a_waiting_order_waiting(store, printer, receipt, tmp_path):
    client = Etsy(receipt)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=60, now=PLACED + 10)
    assert store.get(12345)["state"] == "waiting"
    # 90s would have been enough for a 60s delay. A longer delay wins.
    run(client, store, printer, shippo, delay=3600, now=PLACED + 90)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    assert files(tmp_path) == []


def test_clearing_the_delay_releases_a_waiting_order(store, printer, receipt):
    client = Etsy(receipt)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + 10)
    run(client, store, printer, shippo, delay=0, now=PLACED + 11)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"


def test_recheck_uses_the_fresh_address(store, printer, receipt, tmp_path):
    fresh = copy.deepcopy(receipt)
    fresh["first_line"] = "99 New Street"
    fresh["name"] = "New Name"
    client = Etsy(receipt, fresh=fresh)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 1)
    slip = (tmp_path / "outbox" / "packing-slip-12345.txt").read_text()
    assert "99 New Street" in slip
    assert "42 Test Lane" not in slip
    assert shippo.last_to["street1"] == "99 New Street"
    assert store.get_receipt_json(12345)["first_line"] == "99 New Street"


@pytest.mark.parametrize("patch,phrase", [
    ({"status": "Canceled"}, "cancelled"),
    ({"status": "Fully Refunded"}, "refunded"),
    ({"is_shipped": True}, "already shipped"),
    ({"shipments": [{"tracking_code": "9400"}]}, "already has a label"),
])
def test_recheck_skips_before_buying(store, printer, receipt, tmp_path, caplog, patch, phrase):
    caplog.set_level(logging.INFO, logger="etsy-auto-print")
    fresh = copy.deepcopy(receipt)
    fresh.update(patch)
    client = Etsy(receipt, fresh=fresh)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 1)
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "done"
    assert files(tmp_path) == []
    assert phrase in caplog.text
    notes = " ".join(e["note"] or "" for e in store.events(12345))
    assert phrase in notes


def test_a_failed_recheck_does_not_buy_and_retries(store, printer, receipt):
    client = Etsy(receipt, fail=EtsyApiError(503, "etsy down"))
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 1)
    assert shippo.buy_calls == 0
    row = store.get(12345)
    assert row["state"] == "waiting"
    assert "re-check" in store.events(12345)[-1]["note"]

    client.fail = None
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 2)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"


def test_a_deleted_receipt_is_skipped(store, printer, receipt, caplog):
    caplog.set_level(logging.INFO, logger="etsy-auto-print")
    client = Etsy(receipt, fail=EtsyApiError(404, "gone"))
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 1)
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "done"
    assert "no such receipt" in caplog.text


def test_restart_mid_delay_prints_once(tmp_path, receipt):
    db = tmp_path / "orders.db"
    out = tmp_path / "outbox"
    client = Etsy(receipt)

    store = Store(db)
    shippo = RecordingShippo()
    run(client, store, FilePrinter(out), shippo, delay=DELAY, now=PLACED + 10)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    store.close()

    # Reboot: new process, same database. The window has not ended.
    store = Store(db)
    shippo = RecordingShippo()
    run(client, store, FilePrinter(out), shippo, delay=DELAY, now=PLACED + 20)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    assert store.get(12345) is not None
    store.close()

    # Window ended. One purchase.
    store = Store(db)
    shippo = RecordingShippo()
    run(client, store, FilePrinter(out), shippo, delay=DELAY, now=PLACED + DELAY + 1)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"
    store.close()

    # And a further restart does not buy a second label.
    store = Store(db)
    shippo = RecordingShippo()
    run(client, store, FilePrinter(out), shippo, delay=DELAY, now=PLACED + DELAY + 9999)
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "label_printed"


def test_a_release_interrupted_by_restart_finishes_even_if_the_poll_misses_it(
    tmp_path, receipt
):
    # Crash after the delay elapsed and this process had claimed the order,
    # then the next poll's open list doesn't include it. It still has to print.
    db = tmp_path / "orders.db"
    store = Store(db)
    store.register(receipt)
    store.transition(12345, "waiting", "holding")
    assert store.claim_releasing(12345, "buying")
    store.conn.execute(
        "UPDATE orders SET updated_at = ? WHERE receipt_id = ?", (PLACED, 12345)
    )
    store.conn.commit()
    store.close()

    store = Store(db)
    client = Etsy(receipt)
    client.receipts = []
    shippo = RecordingShippo()
    run(client, store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=PLACED + 5000)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"
    run(client, store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=PLACED + 6000)
    assert shippo.buy_calls == 1


def test_print_now_does_not_steal_an_in_flight_release(store, printer, receipt):
    store.register(receipt)
    store.transition(12345, "waiting", "holding")
    assert store.claim_releasing(12345, "poller")
    shippo = RecordingShippo()
    ok, message = print_now_order(
        12345, store, printer, make_labeler(store, printer, shippo), Etsy(receipt), None,
        now=time.time(), delay_seconds=DELAY,
    )
    assert ok is True
    assert "already being printed" in message
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "releasing"


def test_a_poll_that_misses_the_order_does_not_drop_it(store, printer, receipt):
    client = Etsy(receipt)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + 10)
    client.receipts = []
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 5)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    client.receipts = [receipt]
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 6)
    assert shippo.buy_calls == 1


def test_a_fresh_release_is_not_bought_twice_and_a_stale_one_finishes(store, printer, receipt):
    store.register(receipt)
    store.transition(12345, "waiting", "holding")
    assert store.claim_releasing(12345, "in flight")
    client = Etsy(receipt)
    shippo = RecordingShippo()
    now = time.time()
    run(client, store, printer, shippo, delay=DELAY, now=now)
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "releasing"

    run(client, store, printer, shippo, delay=DELAY, now=now + 400)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"
    run(client, store, printer, shippo, delay=DELAY, now=now + 800)
    assert shippo.buy_calls == 1


def test_only_one_process_can_claim_the_release(tmp_path, receipt):
    db = tmp_path / "orders.db"
    first = Store(db)
    first.register(receipt)
    first.transition(12345, "waiting", "holding")
    second = Store(db)
    assert first.claim_releasing(12345, "poller") is True
    assert second.claim_releasing(12345, "dashboard") is False
    assert second.get(12345)["state"] == "releasing"


def test_only_one_process_can_claim_the_purchase(store, receipt):
    store.register(receipt)
    assert store.claim_label_attempt(12345) is True
    assert store.claim_label_attempt(12345) is False


def test_restart_after_a_purchase_attempt_does_not_buy_again(tmp_path, receipt):
    db = tmp_path / "orders.db"
    store = Store(db)
    store.register(receipt)
    store.transition(12345, "waiting", "holding")
    assert store.claim_releasing(12345, "buying")
    store.record_label_attempt(12345)  # charged, or about to be; result never landed
    store.conn.execute(
        "UPDATE orders SET updated_at = ? WHERE receipt_id = ?", (PLACED, 12345)
    )
    store.conn.commit()
    store.close()

    store = Store(db)
    shippo = RecordingShippo()
    run(Etsy(receipt), store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=PLACED + 5000)
    assert shippo.buy_calls == 0
    row = store.get(12345)
    assert row is not None
    assert row["state"] == "held"
    assert "Shippo dashboard" in row["error"]


def test_an_existing_local_label_is_printed_not_bought_again(store, printer, receipt, caplog):
    caplog.set_level(logging.INFO, logger="etsy-auto-print")
    store.register(receipt)
    store.transition(12345, "slip_printed", "slip")
    store.save_label(
        12345, object_id="txn", carrier="USPS", service="Ground",
        tracking_number="9400", label_url="https://example.com/label.pdf",
        is_test=True,
    )
    store.claim_releasing(12345, "crash before the state moved")
    # claim_releasing only moves new/waiting. Force the crash window instead.
    store.conn.execute(
        "UPDATE orders SET state = 'releasing', updated_at = ? WHERE receipt_id = ?",
        (PLACED, 12345),
    )
    store.conn.commit()
    shippo = RecordingShippo()
    run(Etsy(receipt), store, printer, shippo, delay=DELAY, now=PLACED + 5000)
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "label_printed"
    assert "already has a label" in caplog.text


def test_print_now_buys_before_the_delay_ends(store, printer, receipt):
    client = Etsy(receipt)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + 10)
    assert store.get(12345)["state"] == "waiting"
    ok, message = print_now_order(
        12345, store, printer, make_labeler(store, printer, shippo), client, None,
        now=PLACED + 11, delay_seconds=DELAY,
    )
    assert ok is True
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"
    assert "label_printed" in message
    assert client.fetched == [12345]
    # The poller waking up afterwards must not buy a second label.
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 1)
    assert shippo.buy_calls == 1


def test_print_now_on_a_cancelled_order_does_not_buy(store, printer, receipt, caplog):
    caplog.set_level(logging.INFO, logger="etsy-auto-print")
    fresh = copy.deepcopy(receipt)
    fresh["status"] = "Canceled"
    client = Etsy(receipt, fresh=fresh)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + 10)
    ok, message = print_now_order(
        12345, store, printer, make_labeler(store, printer, shippo), client, None,
        now=PLACED + 11, delay_seconds=DELAY,
    )
    assert ok is True
    assert shippo.buy_calls == 0
    assert store.get(12345)["state"] == "done"
    assert "cancelled" in message
    assert "cancelled" in caplog.text


def test_skip_drops_the_order_and_a_later_poll_does_not_buy(store, printer, receipt, caplog):
    caplog.set_level(logging.INFO, logger="etsy-auto-print")
    client = Etsy(receipt)
    shippo = RecordingShippo()
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + 10)
    note = skip_waiting_order(store, 12345)
    assert "skipped" in note
    assert store.get(12345)["state"] == "done"
    run(client, store, printer, shippo, delay=DELAY, now=PLACED + DELAY + 1)
    assert shippo.buy_calls == 0
    assert files(printer.outbox.parent) == [] or not list(printer.outbox.glob("*"))
    assert "skipped" in caplog.text
    with pytest.raises(ValueError):
        skip_waiting_order(store, 12345)


def test_a_digital_order_is_not_held_for_the_delay(store, printer, tmp_path):
    receipt = {
        "receipt_id": 555, "name": "A Buyer", "created_timestamp": PLACED,
        "transactions": [{"title": "Files", "sku": "", "quantity": 1, "is_digital": True}],
    }
    shippo = RecordingShippo()
    run(Etsy(receipt), store, printer, shippo, delay=DELAY, now=PLACED + 1)
    assert store.get(555)["state"] == "done"
    assert shippo.buy_calls == 0
    assert files(tmp_path) == []


def test_an_order_already_past_the_slip_is_not_put_back_on_hold(store, printer, receipt):
    store.register(receipt)
    store.transition(12345, "slip_printed", "slip already printed")
    shippo = RecordingShippo()
    run(Etsy(receipt), store, printer, shippo, delay=DELAY, now=PLACED + 10)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"


def test_saved_delay_survives_reopening_the_database_and_overrides_config(tmp_path):
    db = tmp_path / "orders.db"
    store = Store(db)
    assert describe_delay(store, 90) == (90, "minutes", 90 * 60)
    set_label_delay(store, 2, "hours")
    store.close()
    reopened = Store(db)
    assert describe_delay(reopened, 90) == (2, "hours", 2 * 3600)
    assert describe_delay(reopened, 0) == (2, "hours", 2 * 3600)


def test_the_poller_reads_the_saved_delay_when_none_is_passed(store, printer, receipt, tmp_path):
    set_label_delay(store, 1, "hours")
    shippo = RecordingShippo()
    labeler = make_labeler(store, printer, shippo)
    poll_once(
        Etsy(receipt), store, printer, labeler,
        delay_seconds=None, config_delay_minutes=0, now=PLACED + 10,
    )
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    assert files(tmp_path) == []


def test_config_minutes_apply_until_the_dashboard_saves(store, printer, receipt):
    shippo = RecordingShippo()
    labeler = make_labeler(store, printer, shippo)
    poll_once(
        Etsy(receipt), store, printer, labeler,
        delay_seconds=None, config_delay_minutes=60, now=PLACED + 10,
    )
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0


def test_upgrade_from_an_older_database_keeps_the_order(tmp_path, receipt):
    db = tmp_path / "orders.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE orders (
            receipt_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            buyer_name TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            error TEXT,
            receipt_json TEXT
        )
        """
    )
    now = PLACED + 10
    conn.execute(
        "INSERT INTO orders (receipt_id, state, buyer_name, created_at, updated_at, receipt_json) "
        "VALUES (?, 'slip_printed', ?, ?, ?, ?)",
        (12345, receipt["name"], now, now, json.dumps(receipt)),
    )
    conn.commit()
    conn.close()

    store = Store(db)
    row = store.get(12345)
    assert row["state"] == "slip_printed"
    assert row["placed_at"] is None
    shippo = RecordingShippo()
    run(Etsy(receipt), store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=PLACED + 15)
    assert shippo.buy_calls == 1
    assert store.get(12345)["state"] == "label_printed"


def test_parse_delay_and_countdown():
    assert parse_delay(0, "minutes") == 0
    assert parse_delay(2, "hours") == 7200
    assert parse_delay(14 * 24, "hours") == 14 * 24 * 3600
    with pytest.raises(ValueError):
        parse_delay(-1, "minutes")
    with pytest.raises(ValueError):
        parse_delay(1, "days")
    with pytest.raises(ValueError):
        parse_delay(14 * 24 + 1, "hours")
    assert format_countdown(0) == "due now"
    assert format_countdown(45) == "in 45s"
    assert format_countdown(90) == "in 1m 30s"
    assert format_countdown(3720) == "in 1h 02m"


def test_an_order_without_a_placed_time_keeps_its_anchor_across_restart(tmp_path, receipt):
    receipt = dict(receipt)
    receipt.pop("created_timestamp")
    db = tmp_path / "orders.db"
    seen = 1_700_000_000
    store = Store(db)
    shippo = RecordingShippo()
    run(Etsy(receipt), store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=seen)
    assert store.get(12345)["state"] == "waiting"
    assert store.get(12345)["placed_at"] == seen
    store.close()

    store = Store(db)
    shippo = RecordingShippo()
    run(Etsy(receipt), store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=seen + 10)
    assert store.get(12345)["state"] == "waiting"
    assert shippo.buy_calls == 0
    run(Etsy(receipt), store, FilePrinter(tmp_path / "outbox"), shippo,
        delay=DELAY, now=seen + DELAY)
    assert shippo.buy_calls == 1
