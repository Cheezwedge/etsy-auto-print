import pytest

from etsy_auto_print.etsy import SHIPMENT_EXTRAS, EtsyClient, _extract_scopes


class FakeTokens:
    def access_token(self):
        return "1.token"

    def get_cached_shop_id(self):
        return 42

    def force_refresh(self):
        return "1.token"


class FakeConfig:
    api_key = "key:secret"
    shop_id = 42


def client(monkeypatch, capture):
    c = EtsyClient(FakeConfig(), FakeTokens())
    monkeypatch.setattr(c, "_request", lambda method, path, **kw: capture.update(
        method=method, path=path, **kw) or {})
    return c


def test_shipment_extras_are_whitelisted(monkeypatch):
    capture = {}
    c = client(monkeypatch, capture)
    c.create_receipt_shipment(1, "9400", "usps", mail_class="Ground Advantage")
    assert capture["json"]["mail_class"] == "Ground Advantage"

    # A typo'd field would be silently dropped by Etsy, so refuse it here.
    with pytest.raises(TypeError, match="mailclass"):
        c.create_receipt_shipment(1, "9400", "usps", mailclass="Ground")


def test_none_valued_extras_are_omitted(monkeypatch):
    capture = {}
    client(monkeypatch, capture).create_receipt_shipment(
        1, "9400", "usps", weight=None, mail_class="Priority Mail"
    )
    body = capture["json"]
    assert "weight" not in body
    assert set(body) == {"tracking_code", "carrier_name", "send_bcc", "mail_class"}


def test_shipment_extras_matches_the_spec():
    # Guards against a rename drifting from Etsy's documented field names.
    assert {"mail_class", "weight", "weight_units", "ship_date"} <= SHIPMENT_EXTRAS
    assert "tracking_code" not in SHIPMENT_EXTRAS  # required, not an extra


@pytest.mark.parametrize(
    "payload",
    [
        ["transactions_r", "shops_r"],
        {"scopes": ["transactions_r", "shops_r"]},
        {"scopes": "transactions_r shops_r"},
        {"results": ["transactions_r", "shops_r"]},
        {"whatever_etsy_calls_it": ["transactions_r", "shops_r"]},
    ],
)
def test_extract_scopes_accepts_every_plausible_shape(payload):
    # Etsy documents the /scopes response as an opaque object, so this must
    # not depend on one guessed key.
    assert _extract_scopes(payload) == ["transactions_r", "shops_r"]


def test_extract_scopes_gives_up_quietly():
    assert _extract_scopes({}) == []
    assert _extract_scopes({"count": 2}) == []
    assert _extract_scopes(None) == []


# --- active listings ------------------------------------------------------


def paging_client(pages):
    """A client whose _request replays the given pages of results."""
    c = EtsyClient(FakeConfig(), FakeTokens())
    calls = []

    def fake(method, path, **kw):
        calls.append((path, kw.get("params", {})))
        return pages[len(calls) - 1]

    c._request = fake
    c.calls = calls
    return c


def test_active_listings_stop_after_a_short_page():
    c = paging_client([{"count": 2, "results": [{"title": "A"}, {"title": "B"}]}])
    assert [listing["title"] for listing in c.get_active_listings()] == ["A", "B"]
    assert len(c.calls) == 1
    assert c.calls[0][0] == "/shops/42/listings/active"


def test_active_listings_paginate_past_a_hundred():
    # A full first page is indistinguishable from "there is more" without
    # asking again — a 100-listing shop must not be silently truncated.
    full = [{"title": f"L{i}"} for i in range(100)]
    c = paging_client([
        {"count": 130, "results": full},
        {"count": 130, "results": [{"title": "L100"}]},
    ])
    listings = c.get_active_listings()
    assert len(listings) == 101
    assert c.calls[1][1]["offset"] == 100


def test_active_listings_of_an_empty_shop_are_empty():
    assert paging_client([{"count": 0, "results": []}]).get_active_listings() == []
