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
