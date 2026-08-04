"""`refund` — getting postage back on a label that will never be used.

Shippo stopped auto-refunding unused USPS labels in April 2024, so an
abandoned label is money gone unless someone asks for it back. Asking is
also destructive in one direction: a refunded label is rejected at the
counter, so the command has to be sure it's talking about the right label.
"""

import argparse

import pytest

from etsy_auto_print import cli, shippo
from etsy_auto_print.config import load_config
from etsy_auto_print.store import Store

CONFIG = """
[etsy]
keystring = "k"
shared_secret = "s"

[printer]
backend = "file"
outbox = "outbox"

[labels]
enabled = true
shippo_token = "shippo_test_fake"

[labels.ship_from]
name = "Shop"
street1 = "1 St"
city = "Portland"
state = "OR"
zip = "97201"
country = "US"
email = "shop@example.com"

[labels.parcel]
length_in = 10.0
width_in = 7.0
height_in = 4.0
packaging_oz = 3.0
"""

RECEIPT = {"receipt_id": 12345, "name": "A Buyer", "transactions": []}


class FakeShippo:
    def __init__(self, is_test=True, status="QUEUED", error=None):
        self.is_test = is_test
        self.status = status
        self.error = error
        self.refunded = []

    def create_refund(self, transaction_object_id):
        if self.error:
            raise self.error
        self.refunded.append(transaction_object_id)
        return {"object_id": "rf1", "status": self.status,
                "transaction": transaction_object_id}


@pytest.fixture
def config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    return load_config(tmp_path / "config.toml")


@pytest.fixture
def store(config):
    store = Store(config.db_path)
    store.register(RECEIPT)
    store.transition(12345, "slip_printed", "")
    return store


def with_label(store, **overrides):
    fields = {
        "object_id": "txn1", "carrier": "USPS", "service": "Ground Advantage",
        "amount": "5.50", "currency": "USD", "tracking_number": "9400TEST",
        "is_test": True,
    }
    store.save_label(12345, **{**fields, **overrides})


def run(config, client, receipt_id=12345):
    original = cli.make_client
    cli.make_client = lambda token, allow_live: client
    try:
        return cli.cmd_refund(config, argparse.Namespace(receipt_id=receipt_id))
    finally:
        cli.make_client = original


def test_refund_is_requested_and_status_reported(config, store, capsys):
    with_label(store)
    client = FakeShippo(status="QUEUED")
    assert run(config, client) == 0
    assert client.refunded == ["txn1"]
    out = capsys.readouterr().out
    assert "QUEUED" in out
    assert "5.50 USD" in out


def test_a_rejected_refund_is_not_dressed_up_as_success(config, store, capsys):
    with_label(store)
    assert run(config, FakeShippo(status="ERROR")) == 0
    out = capsys.readouterr().out
    assert "already used or scanned" in out
    # Nothing was refunded, so warning the user off shipping it would be wrong.
    assert "Do not ship this parcel" not in out


def test_a_pending_refund_says_it_may_take_days(config, store, capsys):
    with_label(store)
    run(config, FakeShippo(status="PENDING"))
    assert "14 days" in capsys.readouterr().out


def test_the_order_history_records_the_request(config, store):
    with_label(store)
    run(config, FakeShippo(status="SUCCESS"))
    notes = [e["note"] for e in store.events(12345)]
    assert any("refund requested: SUCCESS" in n for n in notes)


def test_asking_for_a_refund_does_not_move_the_order(config, store):
    # A refund says nothing about how far the order got; overwriting the state
    # would lose that.
    with_label(store)
    before = store.get(12345)["state"]
    run(config, FakeShippo())
    assert store.get(12345)["state"] == before


def test_an_order_with_no_label_refunds_nothing(config, store, capsys):
    client = FakeShippo()
    assert run(config, client) == 1
    assert client.refunded == []
    assert "No label purchased" in capsys.readouterr().err


def test_a_live_token_will_not_touch_a_test_label(config, store, capsys):
    # Shippo keeps test and live objects in separate universes: the live token
    # 404s on this transaction. Better to say why than relay "not found".
    with_label(store, is_test=True)
    client = FakeShippo(is_test=False)
    assert run(config, client) == 1
    assert client.refunded == []
    assert "TEST token but" in capsys.readouterr().err


def test_a_test_token_will_not_touch_a_live_label(config, store, capsys):
    with_label(store, is_test=False)
    client = FakeShippo(is_test=True)
    assert run(config, client) == 1
    assert client.refunded == []
    assert "LIVE token but" in capsys.readouterr().err


def test_test_mode_says_the_success_is_meaningless(config, store, capsys):
    # Shippo returns SUCCESS for every test-mode refund and invoices nothing,
    # so an unqualified "refunded!" would be a lie.
    with_label(store)
    run(config, FakeShippo(status="SUCCESS"))
    assert "never invoices" in capsys.readouterr().out


def test_a_failed_call_reports_instead_of_raising(config, store, capsys):
    with_label(store)
    client = FakeShippo(error=shippo.ShippoError("could not reach Shippo"))
    assert run(config, client) == 1
    assert "Refund request failed" in capsys.readouterr().err


# --- the client call itself ------------------------------------------------


def test_create_refund_posts_the_transaction_id(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "QUEUED"}

    def fake_request(method, url, json=None, headers=None, timeout=None):
        sent.update(method=method, url=url, payload=json)
        return Resp()

    monkeypatch.setattr(shippo.requests, "request", fake_request)
    shippo.ShippoClient("shippo_test_x").create_refund("txn1")
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/refunds/")
    assert sent["payload"] == {"transaction": "txn1", "async": False}


def test_buy_label_sends_metadata_only_when_there_is_some(monkeypatch):
    sent = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "SUCCESS"}

    monkeypatch.setattr(
        shippo.requests, "request",
        lambda method, url, json=None, **kw: (sent.append(json), Resp())[1],
    )
    client = shippo.ShippoClient("shippo_test_x")
    client.buy_label("r1", "ZPLII")
    client.buy_label("r1", "ZPLII", metadata="Etsy order #7")
    assert "metadata" not in sent[0]
    assert sent[1]["metadata"] == "Etsy order #7"


def test_overlong_metadata_is_trimmed_rather_than_risking_a_400(monkeypatch):
    sent = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "SUCCESS"}

    monkeypatch.setattr(
        shippo.requests, "request",
        lambda method, url, json=None, **kw: (sent.append(json), Resp())[1],
    )
    shippo.ShippoClient("shippo_test_x").buy_label("r1", "ZPLII", metadata="x" * 500)
    assert len(sent[0]["metadata"]) == 100
