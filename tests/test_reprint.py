"""Reprinting a label or slip that already exists. Never buys postage."""

import pytest

from etsy_auto_print import cli
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
length_in = 10
width_in = 7
height_in = 4
packaging_oz = 3
"""

PDF = b"%PDF-existing-label"


class ReprintShippo:
    """A Shippo stand-in that can only hand back a label it was already given."""

    is_test = True

    def __init__(self):
        self.buy_calls = 0
        self.downloads = []

    def download(self, url):
        self.downloads.append(url)
        return PDF

    def buy_label(self, *args, **kwargs):
        self.buy_calls += 1
        raise AssertionError("reprint bought postage")

    def create_address(self, *args, **kwargs):
        raise AssertionError("reprint validated an address")

    def create_shipment(self, *args, **kwargs):
        raise AssertionError("reprint created a shipment")


@pytest.fixture
def app(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    shippo = ReprintShippo()
    monkeypatch.setattr(cli, "make_client", lambda token, allow_live=False: shippo)
    return tmp_path, Store(tmp_path / "orders.db"), shippo


def run(tmp_path, *args):
    return cli.main(["-c", str(tmp_path / "config.toml"), *args])


def add_purchased(store, rid, name, created_at):
    store.register(
        {"receipt_id": rid, "name": name, "transactions": []},
        now=created_at,
    )
    store.transition(rid, "slip_printed", "slip -> outbox")
    store.transition(rid, "label_purchased", "bought")
    store.save_label(
        rid,
        label_url=f"https://example.test/{rid}.pdf",
        carrier="USPS",
        service="Ground Advantage",
        tracking_number=f"9400{rid}",
        is_test=True,
    )
    store.conn.execute(
        "UPDATE labels SET created_at = ? WHERE receipt_id = ?",
        (created_at, rid),
    )
    store.conn.commit()


def test_reprint_label_prints_the_saved_pdf_and_buys_nothing(app, capsys):
    tmp_path, store, shippo = app
    add_purchased(store, 15, "Ada Lovelace", 10)
    assert run(tmp_path, "reprint-label", "15") == 0
    assert (tmp_path / "outbox" / "label-15.pdf").read_bytes() == PDF
    assert shippo.downloads == ["https://example.test/15.pdf"]
    assert shippo.buy_calls == 0
    out = capsys.readouterr().out
    assert "Reprinted label for order #15 (Ada Lovelace) ->" in out
    assert store.get(15)["state"] == "label_printed"
    assert any(event["note"].startswith("label ->") for event in store.events(15))


def test_reprinting_a_finished_order_stays_finished_and_is_recorded(app, capsys):
    tmp_path, store, shippo = app
    add_purchased(store, 7, "Done Buyer", 10)
    for state in ("label_printed", "tracking_posted", "done"):
        store.transition(7, state, state)
    before = len(store.events(7))
    assert run(tmp_path, "reprint-label", "7") == 0
    assert store.get(7)["state"] == "done"
    events = store.events(7)
    assert len(events) == before + 1
    last = events[-1]
    assert last["from_state"] == "done" and last["to_state"] == "done"
    assert last["note"].startswith("label ->")
    assert "order #7 (Done Buyer)" in capsys.readouterr().out
    assert shippo.buy_calls == 0
    assert (tmp_path / "outbox" / "label-7.pdf").read_bytes() == PDF


def test_reprinting_a_held_label_unholds_it(app):
    tmp_path, store, shippo = app
    add_purchased(store, 8, "Held", 10)
    store.hold(8, "printer jam")
    assert run(tmp_path, "reprint-label", "8") == 0
    row = store.get(8)
    assert row["state"] == "label_printed"
    assert row["error"] is None
    assert shippo.buy_calls == 0


def test_last_follows_the_label_printed_most_recently(app, capsys):
    tmp_path, store, shippo = app
    add_purchased(store, 1, "Older", 100)
    store.transition(1, "label_printed", "label -> outbox/label-1.pdf")
    store.conn.execute(
        "UPDATE events SET at = 110 WHERE receipt_id = 1 AND note LIKE 'label ->%'"
    )
    add_purchased(store, 2, "Newer Purchase", 200)
    store.transition(2, "label_printed", "label -> outbox/label-2.pdf")
    store.conn.execute(
        "UPDATE events SET at = 210 WHERE receipt_id = 2 AND note LIKE 'label ->%'"
    )
    # The older label was sent to the printer again after the newer purchase.
    store.note(1, "label -> outbox/label-1.pdf")
    store.conn.execute(
        "UPDATE events SET at = 300 WHERE receipt_id = 1 AND id = "
        "(SELECT MAX(id) FROM events WHERE receipt_id = 1)"
    )
    store.conn.commit()

    assert run(tmp_path, "reprint-label", "last") == 0
    out = capsys.readouterr().out
    assert "order #1 (Older)" in out
    assert "order #2" not in out
    assert shippo.downloads == ["https://example.test/1.pdf"]
    assert shippo.buy_calls == 0


def test_last_is_the_newest_purchase_when_nothing_has_printed(app, capsys):
    tmp_path, store, shippo = app
    add_purchased(store, 1, "Old", 50)
    add_purchased(store, 2, "New", 80)
    assert run(tmp_path, "reprint-label", "last") == 0
    assert "order #2 (New)" in capsys.readouterr().out
    assert (tmp_path / "outbox" / "label-2.pdf").read_bytes() == PDF
    assert shippo.buy_calls == 0
    assert store.get(2)["state"] == "label_printed"


def test_last_omits_the_buyer_when_the_order_has_no_name(app, capsys):
    tmp_path, store, _shippo = app
    add_purchased(store, 9, None, 1)
    assert run(tmp_path, "reprint-label", "LAST") == 0
    out = capsys.readouterr().out
    assert "order #9 ->" in out
    assert "order #9 (" not in out


def test_last_fails_clearly_when_no_label_has_been_bought(app, capsys):
    tmp_path, store, shippo = app
    store.register({"receipt_id": 4, "name": "Waiting", "transactions": []})
    assert run(tmp_path, "reprint-label", "last") == 1
    err = capsys.readouterr().err
    assert "No purchased label yet" in err
    assert shippo.buy_calls == 0
    assert shippo.downloads == []
    assert list((tmp_path / "outbox").glob("*")) == []


def test_reprint_label_refuses_an_order_with_no_label(app, capsys):
    tmp_path, store, shippo = app
    store.register({"receipt_id": 4, "name": "Waiting", "transactions": []})
    assert run(tmp_path, "reprint-label", "4") == 1
    assert "No purchased label for order #4." in capsys.readouterr().err
    assert shippo.downloads == []


def test_reprint_label_refuses_when_labels_are_disabled(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text(CONFIG.replace("enabled = true", "enabled = false"))
    store = Store(tmp_path / "orders.db")
    add_purchased(store, 1, "Ada", 1)
    shippo = ReprintShippo()
    monkeypatch.setattr(cli, "make_client", lambda *args, **kwargs: shippo)
    code = cli.main(["-c", str(tmp_path / "config.toml"), "reprint-label", "1"])
    assert code == 1
    assert "not enabled" in capsys.readouterr().err
    assert shippo.downloads == []


def test_reprint_label_rejects_a_word_that_is_not_last(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["reprint-label", "newest"])
    assert exc.value.code == 2
    assert "last" in capsys.readouterr().err


def test_reprint_last_names_the_most_recent_slip(app, capsys):
    tmp_path, store, _shippo = app
    store.register({"receipt_id": 1, "name": "First", "transactions": []})
    store.transition(1, "slip_printed", "slip -> a")
    store.conn.execute(
        "UPDATE events SET at = 10 WHERE receipt_id = 1 AND to_state = 'slip_printed'"
    )
    store.register({"receipt_id": 2, "name": "Second", "transactions": []})
    store.transition(2, "slip_printed", "slip -> b")
    store.conn.execute(
        "UPDATE events SET at = 20 WHERE receipt_id = 2 AND to_state = 'slip_printed'"
    )
    store.conn.commit()

    assert run(tmp_path, "reprint", "last") == 0
    out = capsys.readouterr().out
    assert "Reprinted slip for order #2 (Second) ->" in out
    assert (tmp_path / "outbox" / "packing-slip-2.txt").exists()
    assert not (tmp_path / "outbox" / "packing-slip-1.txt").exists()


def test_reprint_last_fails_when_nothing_has_been_printed(app, capsys):
    tmp_path, store, _shippo = app
    store.register({"receipt_id": 1, "name": "New", "transactions": []})
    assert run(tmp_path, "reprint", "last") == 1
    assert "No packing slip has been printed yet" in capsys.readouterr().err
    assert list((tmp_path / "outbox").glob("*")) == []


def test_reprint_by_id_keeps_its_message(app, capsys):
    tmp_path, store, _shippo = app
    store.register({"receipt_id": 5, "name": "Pat", "transactions": []})
    store.transition(5, "slip_printed", "slip")
    assert run(tmp_path, "reprint", "5") == 0
    out = capsys.readouterr().out
    assert out.startswith("Reprinted -> ")
    assert "order #5" not in out
