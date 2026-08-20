"""Orders that get fulfilled somewhere other than here.

An international order bought through Etsy — because Etsy fills in the
customs form and this program can't — simply disappears from the
open-receipts poll. Without reconciliation it sits `held` forever, and the
dashboard shows a permanent "needs attention" for something already posted.
"""

import pytest

from etsy_auto_print.etsy import EtsyApiError
from etsy_auto_print.pipeline import reconcile, was_fulfilled_elsewhere
from etsy_auto_print.store import Store

RECEIPT = {"receipt_id": 500, "name": "A Buyer", "transactions": []}


class FakeEtsy:
    def __init__(self, receipts=None, error=None):
        self.receipts = receipts or {}
        self.error = error
        self.fetched = []

    def get_receipt(self, receipt_id):
        self.fetched.append(receipt_id)
        if self.error:
            raise self.error
        if receipt_id not in self.receipts:
            raise EtsyApiError(404, "not found")
        return self.receipts[receipt_id]


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "orders.db")
    store.register(RECEIPT)
    return store


def held(store, reason="international order to CA"):
    store.transition(500, "held", reason)
    return store


# --- what counts as dealt with ---------------------------------------------


def test_a_shipped_receipt_is_closed():
    assert "shipped outside" in was_fulfilled_elsewhere({"is_shipped": True})


def test_a_cancelled_receipt_is_closed():
    assert was_fulfilled_elsewhere({"status": "Canceled"})


def test_a_refunded_receipt_is_closed():
    assert was_fulfilled_elsewhere({"status": "refunded"})


def test_an_open_receipt_is_left_alone():
    assert was_fulfilled_elsewhere({"is_shipped": False, "status": "paid"}) == ""


def test_an_unrecognised_shape_is_left_alone():
    # Wrongly closing a live order means it never ships. Silence is safer.
    assert was_fulfilled_elsewhere({}) == ""


# --- the sweep --------------------------------------------------------------


def test_a_held_order_bought_on_etsy_closes_itself(store):
    held(store)
    client = FakeEtsy({500: {"is_shipped": True}})
    assert reconcile(client, store, open_ids=set()) == 1
    assert store.get(500)["state"] == "done"


def test_the_reason_lands_in_the_history(store):
    held(store)
    reconcile(FakeEtsy({500: {"is_shipped": True}}), store, set())
    notes = [e["note"] for e in store.events(500)]
    assert any("shipped outside this program" in n for n in notes)


def test_an_order_still_awaiting_shipment_is_untouched(store):
    held(store)
    client = FakeEtsy({500: {"is_shipped": True}})
    # Still in the open poll, so not a candidate — and no call is wasted.
    assert reconcile(client, store, open_ids={500}) == 0
    assert client.fetched == []
    assert store.get(500)["state"] == "held"


def test_absence_alone_never_closes_an_order(store):
    # An Etsy hiccup returning an empty list must not close everything in
    # flight; the receipt itself has to confirm it.
    held(store)
    client = FakeEtsy({500: {"is_shipped": False, "status": "paid"}})
    assert reconcile(client, store, open_ids=set()) == 0
    assert store.get(500)["state"] == "held"


def test_a_failed_recheck_changes_nothing(store):
    held(store)
    client = FakeEtsy(error=EtsyApiError(0, "network down"))
    assert reconcile(client, store, set()) == 0
    assert store.get(500)["state"] == "held"


def test_a_receipt_etsy_does_not_have_is_closed(store):
    # `test-order` leaves a fake receipt in the database. Without this it is
    # re-fetched on every poll for the rest of time.
    held(store)
    client = FakeEtsy({})          # 404
    assert reconcile(client, store, set()) == 1
    assert store.get(500)["state"] == "done"


def test_already_done_orders_are_not_re_examined(store):
    store.transition(500, "done", "finished")
    client = FakeEtsy({})
    assert reconcile(client, store, set()) == 0
    assert client.fetched == []


def test_a_test_labelled_order_cancelled_on_etsy_closes(store):
    # The phase-1 test order: real receipt, test label, cancelled afterwards.
    # It stops at label_printed by design and would otherwise never clear.
    store.transition(500, "slip_printed", "")
    store.transition(500, "label_purchased", "")
    store.transition(500, "label_printed", "")
    reconcile(FakeEtsy({500: {"status": "Canceled"}}), store, set())
    assert store.get(500)["state"] == "done"
