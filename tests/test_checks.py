import requests

from etsy_auto_print import checks
from etsy_auto_print.config import OAUTH_SCOPES, REQUIRED_SCOPES


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


# --- return address -------------------------------------------------------


class Labels:
    def __init__(self, ship_from, enabled=True):
        self.ship_from = ship_from
        self.enabled = enabled


REAL = {"name": "Engworks", "street1": "9 Real Rd", "city": "Bend",
        "state": "OR", "zip": "97701", "country": "US", "email": "a@b.com"}


def cfg_with(ship_from, enabled=True):
    return type("C", (), {"labels": Labels(ship_from, enabled)})()


def test_real_return_address_passes():
    assert checks.check_ship_from(cfg_with(REAL)).state == checks.OK


def test_example_return_address_fails_before_it_ships():
    # Carriers accept it — the example street exists — so nothing downstream
    # catches this. It only surfaces when a parcel can't come back.
    bad = {**REAL, "street1": "123 Your Street", "name": "Your Shop Name"}
    result = checks.check_ship_from(cfg_with(bad))
    assert result.state == checks.FAIL
    assert "123 Your Street" in result.detail
    assert "going live" in result.hint


def test_example_email_alone_is_caught():
    assert checks.check_ship_from(
        cfg_with({**REAL, "email": "you@example.com"})).state == checks.FAIL


def test_disabled_labels_need_no_return_address():
    assert checks.check_ship_from(cfg_with({}, enabled=False)).state == checks.WARN


# --- printer --------------------------------------------------------------


class PrinterConfig:
    printer_backend = "cups"
    cups_queue = "label"
    outbox = "/tmp/outbox"


def stub_run(monkeypatch, responses):
    def fake(cmd, timeout=10):
        return responses[cmd[0]]
    monkeypatch.setattr(checks, "_run", fake)


IDLE = (0, "printer label is idle.  enabled since Mon 03 Aug 2026 09:10:29 PM PDT")


def test_missing_lpq_does_not_look_like_a_backlog(monkeypatch):
    # lpq ships separately from the CUPS daemon. Its absence used to render
    # as "jobs waiting: lpq not installed", leaving Status permanently amber.
    stub_run(monkeypatch, {"lpstat": IDLE, "lpq": (127, "lpq not installed")})
    result = checks.check_printer(PrinterConfig())
    assert result.state == checks.OK
    assert "lpq" not in result.detail


def test_empty_queue_is_ok(monkeypatch):
    stub_run(monkeypatch, {"lpstat": IDLE, "lpq": (0, "label is ready\nno entries")})
    assert checks.check_printer(PrinterConfig()).state == checks.OK


def test_real_backlog_still_warns(monkeypatch):
    stub_run(monkeypatch, {
        "lpstat": IDLE,
        "lpq": (0, "label is ready and printing\nRank Owner Job\n1st pi 12 label 900 bytes"),
    })
    result = checks.check_printer(PrinterConfig())
    assert result.state == checks.WARN
    assert "jobs waiting" in result.detail


def test_disabled_queue_fails(monkeypatch):
    stub_run(monkeypatch, {"lpstat": (0, "printer label disabled since ...")})
    result = checks.check_printer(PrinterConfig())
    assert result.state == checks.FAIL
    assert "cupsenable" in result.hint


# --- listing stock --------------------------------------------------------


class StockCfg:
    def __init__(self, enabled=True, low_threshold=2, interval_minutes=60):
        self.enabled = enabled
        self.low_threshold = low_threshold
        self.interval_minutes = interval_minutes


def stock_config(tmp_path, state=None, enabled=True):
    from etsy_auto_print.store import Store

    db = tmp_path / "orders.db"
    store = Store(db)
    if state:
        store.save_stock_state(state)
    return type("C", (), {"db_path": db, "stock": StockCfg(enabled)})()


def test_out_of_stock_fails_the_check(tmp_path):
    from etsy_auto_print import stock

    result = checks.check_stock(
        stock_config(tmp_path, {1: (stock.OUT, "Adapters", 0)})
    )
    assert result.state == checks.FAIL
    assert "1 listing(s) out of stock" in result.detail
    assert "Adapters" in result.facts["out of stock"]


def test_low_stock_only_warns(tmp_path):
    from etsy_auto_print import stock

    result = checks.check_stock(stock_config(tmp_path, {1: (stock.LOW, "Adapters", 1)}))
    assert result.state == checks.WARN


def test_healthy_stock_is_ok(tmp_path):
    from etsy_auto_print import stock

    result = checks.check_stock(stock_config(tmp_path, {1: (stock.OK, "Adapters", 40)}))
    assert result.state == checks.OK
    assert "1 listing(s) in stock" in result.detail


def test_never_swept_says_so_rather_than_claiming_health(tmp_path):
    result = checks.check_stock(stock_config(tmp_path))
    assert result.state == checks.WARN
    assert "not checked yet" in result.detail


def test_disabled_monitoring_is_not_a_failure(tmp_path):
    result = checks.check_stock(stock_config(tmp_path, enabled=False))
    assert result.state == checks.WARN


# --- optional scopes ------------------------------------------------------


def test_a_token_without_listings_r_warns_rather_than_fails(monkeypatch):
    # Tokens issued before stock monitoring existed lack it. Turning the
    # whole dashboard red over an optional feature trains you to ignore red.
    stub_scopes(monkeypatch, REQUIRED_SCOPES.split())
    result = checks.check_etsy_scopes(StubConfig())
    assert result.state == checks.WARN
    assert "listings_r" in result.detail
    assert "auth" in result.hint


def test_a_token_with_everything_is_ok(monkeypatch):
    stub_scopes(monkeypatch, OAUTH_SCOPES.split())
    assert checks.check_etsy_scopes(StubConfig()).state == checks.OK


def test_a_missing_required_scope_still_fails(monkeypatch):
    stub_scopes(monkeypatch, ["transactions_r", "listings_r"])
    assert checks.check_etsy_scopes(StubConfig()).state == checks.FAIL
