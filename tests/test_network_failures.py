"""A dropped connection must hold the order, not kill the service.

The Pi is on Wi-Fi and runs unattended for weeks; a transient network failure
is expected, not exceptional. What must never happen is a raw requests
exception escaping a client and taking the poll loop down with it.
"""

import pytest
import requests

from etsy_auto_print import auth, etsy, shippo
from etsy_auto_print.pipeline import advance_order
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.store import Store

from .test_labels import FakeShippo, make_labeler


def boom(*args, **kwargs):
    raise requests.ConnectionError("network is down")


def test_shippo_connection_failure_becomes_a_shippo_error(monkeypatch):
    monkeypatch.setattr(requests, "request", boom)
    client = shippo.ShippoClient("shippo_test_x")
    with pytest.raises(shippo.ShippoError, match="could not reach Shippo"):
        client.create_address({"zip": "97201"})


def test_shippo_download_failure_becomes_a_shippo_error(monkeypatch):
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(shippo.ShippoError, match="label download failed"):
        shippo.ShippoClient("shippo_test_x").download("https://example.com/l.pdf")


def test_etsy_connection_failure_becomes_an_api_error(monkeypatch):
    monkeypatch.setattr(requests, "request", boom)

    class Tokens:
        def access_token(self):
            return "1.t"

        def get_cached_shop_id(self):
            return 1

    class Cfg:
        api_key = "k:s"
        shop_id = 1

    with pytest.raises(etsy.EtsyApiError) as caught:
        etsy.EtsyClient(Cfg(), Tokens()).get_me()
    # Status 0 means "never reached Etsy", so it isn't mistaken for a 4xx
    # rejection that shouldn't be retried.
    assert caught.value.status == 0


def test_token_refresh_connection_failure_becomes_an_auth_error(monkeypatch):
    monkeypatch.setattr(auth.TokenStore, "_post_token", staticmethod(boom))

    class Cfg:
        keystring = "k"
        api_key = "k:s"
        tokens_path = None

    store = auth.TokenStore.__new__(auth.TokenStore)
    store.config = Cfg()
    store._data = {"refresh_token": "r"}
    with pytest.raises(auth.AuthError, match="could not reach Etsy"):
        store._refresh()


def test_a_network_failure_mid_purchase_holds_the_order(tmp_path, receipt):
    class Offline(FakeShippo):
        def create_address(self, address, validate=True):
            raise shippo.ShippoError("could not reach Shippo: network is down")

    store = Store(tmp_path / "orders.db")
    printer = FilePrinter(tmp_path / "outbox")
    store.register(receipt)
    advance_order(receipt, store, printer, make_labeler(store, printer, Offline()))
    row = store.get(12345)
    assert row["state"] == "held"
    assert "could not reach Shippo" in row["error"]
