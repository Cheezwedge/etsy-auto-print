import pytest

from etsy_auto_print.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


def test_register_is_idempotent(store, receipt):
    assert store.register(receipt) is True
    assert store.register(receipt) is False  # second poll: already known
    assert store.get(12345)["state"] == "new"


def test_register_preserves_state_on_repoll(store, receipt):
    store.register(receipt)
    store.transition(12345, "slip_printed", "printed")
    # Order still appears in the open-receipts poll until shipped on Etsy;
    # re-registering must not reset it to 'new'.
    store.register(receipt)
    assert store.get(12345)["state"] == "slip_printed"


def test_transition_and_events(store, receipt):
    store.register(receipt)
    store.transition(12345, "slip_printed", "slip -> outbox")
    events = store.events(12345)
    assert [e["to_state"] for e in events] == ["new", "slip_printed"]


def test_hold_records_reason_and_unhold_clears_it(store, receipt):
    store.register(receipt)
    store.hold(12345, "printer offline")
    row = store.get(12345)
    assert row["state"] == "held"
    assert row["error"] == "printer offline"
    store.transition(12345, "slip_printed", "reprint")
    assert store.get(12345)["error"] is None


def test_unknown_state_rejected(store, receipt):
    store.register(receipt)
    with pytest.raises(ValueError):
        store.transition(12345, "bogus")


def test_transition_unknown_receipt_rejected(store):
    with pytest.raises(KeyError):
        store.transition(999, "slip_printed")


def test_receipt_json_roundtrip(store, receipt):
    store.register(receipt)
    assert store.get_receipt_json(12345)["transactions"][0]["sku"] == "STAND-WAL"
