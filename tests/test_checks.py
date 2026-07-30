import requests

from etsy_auto_print import checks
from etsy_auto_print.config import OAUTH_SCOPES


class StubConfig:
    api_key = "key:secret"


class Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def stub_scopes(monkeypatch, granted, authorized=True, error=None):
    monkeypatch.setattr(
        checks, "TokenStore", lambda config: type("T", (), {"authorized": authorized})()
    )

    class FakeClient:
        def __init__(self, config, tokens):
            pass

        def token_scopes(self):
            if error:
                raise error
            return granted

    monkeypatch.setattr(checks, "EtsyClient", FakeClient)


# --- unauthenticated reachability ----------------------------------------


def test_ping_ok(monkeypatch):
    monkeypatch.setattr(checks, "ping", lambda api_key, **kw: Resp(200))
    assert checks.check_etsy_api(StubConfig()).state == checks.OK


def test_ping_401_blames_the_app_credentials(monkeypatch):
    monkeypatch.setattr(checks, "ping", lambda api_key, **kw: Resp(401, "bad key"))
    result = checks.check_etsy_api(StubConfig())
    assert result.state == checks.FAIL
    assert "shared_secret" in result.hint


def test_ping_network_failure_is_a_check_not_an_exception(monkeypatch):
    def boom(api_key, **kw):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(checks, "ping", boom)
    result = checks.check_etsy_api(StubConfig())
    assert result.state == checks.FAIL and "unreachable" in result.detail


def test_ping_5xx_points_at_etsy(monkeypatch):
    monkeypatch.setattr(checks, "ping", lambda api_key, **kw: Resp(503, "down"))
    assert "outage" in checks.check_etsy_api(StubConfig()).hint


# --- token scopes ---------------------------------------------------------


def test_scopes_ok_when_all_required_are_granted(monkeypatch):
    stub_scopes(monkeypatch, OAUTH_SCOPES.split())
    assert checks.check_etsy_scopes(StubConfig()).state == checks.OK


def test_missing_scope_fails_and_names_it(monkeypatch):
    stub_scopes(monkeypatch, ["transactions_r", "transactions_w"])
    result = checks.check_etsy_scopes(StubConfig())
    assert result.state == checks.FAIL
    assert "shops_r" in result.detail
    assert "auth" in result.hint


def test_extra_scopes_are_fine(monkeypatch):
    stub_scopes(monkeypatch, [*OAUTH_SCOPES.split(), "listings_r"])
    assert checks.check_etsy_scopes(StubConfig()).state == checks.OK


def test_unreadable_scopes_warn_rather_than_fail(monkeypatch):
    # An undocumented response shape must not make a working system look broken.
    stub_scopes(monkeypatch, [], error=RuntimeError("500"))
    assert checks.check_etsy_scopes(StubConfig()).state == checks.WARN
    stub_scopes(monkeypatch, [])
    assert checks.check_etsy_scopes(StubConfig()).state == checks.WARN


def test_unauthorized_does_not_double_count_as_a_failure(monkeypatch):
    # "Etsy connection" already fails when unauthorized; this row shouldn't too.
    stub_scopes(monkeypatch, [], authorized=False)
    result = checks.check_etsy_scopes(StubConfig())
    assert result.state == checks.WARN and "not authorized" in result.detail
