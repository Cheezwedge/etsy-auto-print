import pytest

pytest.importorskip("flask")

from etsy_auto_print import about, dashboard  # noqa: E402
from etsy_auto_print.dashboard import create_app, parse_pasted_rows  # noqa: E402

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
items_csv = "items.csv"
default_parcel = "medium"

[labels.ship_from]
name = "Shop"
street1 = "1 St"
city = "Portland"
state = "OR"
zip = "97201"
country = "US"
email = "shop@example.com"

[labels.parcels.small]
length_in = 6.0
width_in = 4.0
height_in = 2.0
packaging_oz = 1.0

[labels.parcels.medium]
length_in = 10.0
width_in = 7.0
height_in = 4.0
packaging_oz = 3.0
"""

ITEMS_CSV = "sku,weight_oz,parcel,notes\nMUG-1,14,medium,best seller\n"


@pytest.fixture
def app_dir(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "items.csv").write_text(ITEMS_CSV)
    return tmp_path


@pytest.fixture
def client(app_dir):
    return create_app(app_dir / "config.toml").test_client()


@pytest.mark.parametrize("route", ["/", "/orders", "/items", "/config", "/logs"])
def test_pages_render_real_html(client, route):
    resp = client.get(route)
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert text.strip().startswith("<!doctype html>")
    # Regression: nested templates were double-escaped, so pages rendered as
    # visible HTML source instead of a page.
    assert "&lt;div" not in text


def test_products_page_lists_csv_rows_and_offers_parcels(client):
    text = client.get("/items").get_data(as_text=True)
    assert "MUG-1" in text
    assert 'value="medium"' in text and 'value="small"' in text


def test_invalid_toml_is_not_saved(client, app_dir):
    before = (app_dir / "config.toml").read_text()
    resp = client.post("/config", data={"text": "not = = valid"}, follow_redirects=True)
    assert (app_dir / "config.toml").read_text() == before
    assert "Not saved" in resp.get_data(as_text=True)


def test_semantically_invalid_config_is_not_saved(client, app_dir):
    before = (app_dir / "config.toml").read_text()
    broken = before.replace('email = "shop@example.com"', "")
    client.post("/config", data={"text": broken}, follow_redirects=True)
    assert (app_dir / "config.toml").read_text() == before


def test_valid_config_saves_with_backup(client, app_dir):
    # Regression: validation ran in a temp dir, so relative paths such as
    # items_csv could never resolve and no config was ever savable.
    before = (app_dir / "config.toml").read_text()
    client.post(
        "/config", data={"text": before + "\n[poll]\ninterval_seconds = 90\n"},
        follow_redirects=True,
    )
    assert "interval_seconds = 90" in (app_dir / "config.toml").read_text()
    assert (app_dir / "config.toml.bak").read_text() == before
    assert not list(app_dir.glob(".validate-*")), "validation probe file leaked"


def test_products_save_roundtrip_drops_blank_rows(client, app_dir):
    client.post(
        "/items",
        data={
            "sku": ["NEW-1", "", "MUG-1"],
            "weight_oz": ["3.5", "", "14"],
            "parcel": ["small", "", "medium"],
            "notes": ["added", "", ""],
        },
        follow_redirects=True,
    )
    saved = (app_dir / "items.csv").read_text()
    assert "NEW-1,3.5,small,added" in saved
    assert saved.count("\n") == 3  # header + 2 real rows


def test_products_reject_non_numeric_weight(client, app_dir):
    before = (app_dir / "items.csv").read_text()
    resp = client.post(
        "/items",
        data={"sku": ["X"], "weight_oz": ["heavy"], "parcel": [""], "notes": [""]},
        follow_redirects=True,
    )
    assert "not a number" in resp.get_data(as_text=True)
    assert (app_dir / "items.csv").read_text() == before


def test_password_gates_every_page(app_dir):
    client = create_app(app_dir / "config.toml", password="hunter2").test_client()
    assert client.get("/", follow_redirects=False).status_code == 302
    client.post("/login", data={"password": "wrong"})
    assert client.get("/", follow_redirects=False).status_code == 302
    client.post("/login", data={"password": "hunter2"})
    assert client.get("/").status_code == 200


def test_save_redirects_to_status_with_checklist(client, app_dir):
    before = (app_dir / "config.toml").read_text()
    resp = client.post(
        "/config", data={"text": before + "\n[poll]\ninterval_seconds = 95\n"},
        follow_redirects=True,
    )
    text = resp.get_data(as_text=True)
    assert "what to check now" in text.lower()
    # Plain Save doesn't restart, so the checklist must say so.
    assert "Restart is still needed" in text


def test_save_and_apply_keeps_the_save_even_if_restart_fails(client, app_dir):
    # No systemd in the test environment, so the restart attempt fails — the
    # config must still be written, and the user still told to restart.
    before = (app_dir / "config.toml").read_text()
    resp = client.post(
        "/config",
        data={"text": before + "\n[poll]\ninterval_seconds = 99\n", "apply": "1"},
        follow_redirects=True,
    )
    assert "interval_seconds = 99" in (app_dir / "config.toml").read_text()
    text = resp.get_data(as_text=True)
    assert "restart failed" in text.lower()
    assert "Restart is still needed" in text


def test_products_apply_shows_checklist(client, app_dir):
    resp = client.post(
        "/items",
        data={"sku": ["A"], "weight_oz": ["2"], "parcel": ["small"], "notes": [""],
              "apply": "1"},
        follow_redirects=True,
    )
    assert "A,2,small," in (app_dir / "items.csv").read_text()
    assert "what to check now" in resp.get_data(as_text=True).lower()


def test_plain_status_visit_has_no_checklist(client):
    assert "what to check now" not in client.get("/").get_data(as_text=True).lower()


def test_naming_a_new_products_file_creates_it(client, app_dir):
    """Bootstrap: validation rejects a config pointing at a missing file, and
    the Products tab only appears once the config points at one. Saving the
    config has to break that circle."""
    before = (app_dir / "config.toml").read_text()
    resp = client.post(
        "/config",
        data={"text": before.replace('items_csv = "items.csv"', 'items_csv = "new.csv"')},
        follow_redirects=True,
    )
    assert (app_dir / "new.csv").read_text() == "sku,weight_oz,parcel,notes\n"
    assert "Created" in resp.get_data(as_text=True)
    assert 'items_csv = "new.csv"' in (app_dir / "config.toml").read_text()


def test_an_existing_products_file_is_never_overwritten(client, app_dir):
    before = (app_dir / "config.toml").read_text()
    products = (app_dir / "items.csv").read_text()
    client.post("/config", data={"text": before}, follow_redirects=True)
    assert (app_dir / "items.csv").read_text() == products


def test_no_file_is_created_from_unparseable_config(client, app_dir):
    client.post("/config", data={"text": "not = = valid"}, follow_redirects=True)
    assert sorted(p.name for p in app_dir.glob("*.csv")) == ["items.csv"]


# --- pasting a product list ------------------------------------------------


def test_pasted_tab_separated_cells_from_a_spreadsheet():
    # Copying cells out of Excel/Sheets puts tabs on the clipboard, not commas.
    rows = parse_pasted_rows("MUG-1\t14\tmedium\tbest seller\nSTICKER\t0.5\tsmall\t")
    assert rows[0] == {"sku": "MUG-1", "weight_oz": "14", "parcel": "medium",
                       "notes": "best seller"}
    assert rows[1]["sku"] == "STICKER" and rows[1]["notes"] == ""


def test_pasted_csv_is_accepted_too():
    rows = parse_pasted_rows("MUG-1,14,medium\nSTICKER,0.5,small")
    assert [r["sku"] for r in rows] == ["MUG-1", "STICKER"]


def test_header_row_maps_columns_in_any_order():
    rows = parse_pasted_rows(
        "Notes\tWeight (oz)\tSKU\n"
        "top seller\t14\tMUG-1"
    )
    assert rows == [{"sku": "MUG-1", "weight_oz": "14", "parcel": "",
                     "notes": "top seller"}]


def test_header_is_not_mistaken_for_a_product():
    rows = parse_pasted_rows("sku,weight_oz\nMUG-1,14")
    assert len(rows) == 1 and rows[0]["sku"] == "MUG-1"


def test_rows_without_a_header_keep_their_first_row():
    rows = parse_pasted_rows("MUG-1,14\nSTICKER,0.5")
    assert len(rows) == 2


def test_typed_out_ounces_are_tolerated_but_other_units_are_not():
    # The column's unit is fixed, so "14 oz" is noise. "400 g" must survive
    # intact to fail loudly at save rather than be read as 400 ounces.
    rows = parse_pasted_rows("MUG-1,14 oz\nBOWL,400 g")
    assert rows[0]["weight_oz"] == "14"
    assert rows[1]["weight_oz"] == "400 g"


def test_blank_and_unusable_pastes_are_rejected():
    for text in ["", "   \n\n  "]:
        with pytest.raises(ValueError, match="Nothing pasted"):
            parse_pasted_rows(text)
    with pytest.raises(ValueError, match="No products found"):
        parse_pasted_rows(",14,medium\n,0.5,small")


def test_import_loads_rows_into_the_editor_without_saving(client, app_dir):
    before = (app_dir / "items.csv").read_text()
    resp = client.post(
        "/items/import",
        data={"pasted": "NEW-1\t3.5\tsmall\tfrom a sheet", "mode": "replace"},
        follow_redirects=True,
    )
    text = resp.get_data(as_text=True)
    assert 'value="NEW-1"' in text          # in the table, ready to review
    assert "nothing is saved" in text.lower()
    assert (app_dir / "items.csv").read_text() == before   # ...and it wasn't


def test_import_replace_drops_existing_rows_from_the_form(client):
    text = client.post(
        "/items/import", data={"pasted": "NEW-1,3.5", "mode": "replace"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'value="MUG-1"' not in text


def test_import_append_keeps_them(client):
    text = client.post(
        "/items/import", data={"pasted": "NEW-1,3.5", "mode": "append"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'value="MUG-1"' in text and 'value="NEW-1"' in text


def test_bad_paste_reports_and_changes_nothing(client, app_dir):
    before = (app_dir / "items.csv").read_text()
    resp = client.post("/items/import", data={"pasted": "   "}, follow_redirects=True)
    assert "Nothing pasted" in resp.get_data(as_text=True)
    assert (app_dir / "items.csv").read_text() == before


# --- still usable when the config on disk is broken -------------------------


@pytest.fixture
def broken_config_client(app_dir):
    """A config that loads as TOML but fails validation.

    The dashboard is how you fix a bad config, so every page has to survive
    one. Each new rule added to load_config is a new way to lock the user
    out of the tool they'd use to undo it.
    """
    path = app_dir / "config.toml"
    path.write_text(
        path.read_text().replace(
            "[labels.parcels.small]", "[labels.parcels.small]\nweight_oz = 2.5"
        )
    )
    return create_app(path).test_client()


@pytest.mark.parametrize("route", ["/", "/orders", "/items", "/config", "/logs"])
def test_every_page_survives_a_config_that_will_not_load(broken_config_client, route):
    assert broken_config_client.get(route).status_code == 200


def test_the_config_tab_still_shows_the_text_to_fix(broken_config_client):
    # Not just a 200: the editor has to contain the offending file, or there
    # is nothing to correct.
    text = broken_config_client.get("/config").get_data(as_text=True)
    assert "weight_oz = 2.5" in text


def test_the_status_page_names_the_problem(broken_config_client):
    text = broken_config_client.get("/").get_data(as_text=True)
    assert "weight_oz" in text
    assert "Config tab" in text


def test_the_broken_config_can_be_fixed_from_the_page(broken_config_client, app_dir):
    fixed = (app_dir / "config.toml").read_text().replace("weight_oz = 2.5", "")
    broken_config_client.post("/config", data={"text": fixed}, follow_redirects=True)
    assert "weight_oz" not in (app_dir / "config.toml").read_text()


# --- knowing which build you're looking at ---------------------------------


def test_the_running_version_is_shown(client):
    # Without this the page gives no way to tell an updated build from a
    # stale process — which is how a fixed bug looks unfixed.
    assert "etsy-auto-print 9.9.9" in client.get("/").get_data(as_text=True).replace(
        "<span class=\"ver\">", "").replace("</span>", "")


@pytest.fixture(autouse=True)
def pinned_version(monkeypatch):
    monkeypatch.setattr(dashboard, "version_label", lambda: "9.9.9")
    monkeypatch.setattr(dashboard, "running_version", lambda: "9.9.9")


def test_a_stale_process_says_so(client, monkeypatch):
    # The dashboard is long-lived and separate from the poller, so a git pull
    # plus a service restart leaves it serving old code with no symptom
    # beyond a message that looks like an unfixed bug.
    monkeypatch.setattr(dashboard, "version_label", lambda: "0.1.0 (newer)")
    monkeypatch.setattr(dashboard, "running_version", lambda: "0.1.0 (older)")
    text = client.get("/").get_data(as_text=True)
    assert "older build" in text
    assert "etsy-auto-print-dashboard" in text


def test_running_version_is_pinned_once(monkeypatch):
    calls = []
    monkeypatch.setattr(about, "_started_with", None)
    monkeypatch.setattr(about, "version_label", lambda: calls.append(1) or f"v{len(calls)}")
    assert about.running_version() == "v1"
    assert about.running_version() == "v1"   # not re-read


# --- the Products tab when the CSV is what broke the config ----------------

TWO_BOXES = CONFIG.replace(
    '[labels.parcels.small]',
    'default_parcel = "small"\n[labels.parcels.small]',
)


@pytest.fixture
def stale_parcel_client(app_dir):
    """A CSV row naming a box that no longer exists.

    This is the lockout risk: the config fails to load *because of the CSV*,
    and the Products tab is the page you'd fix the CSV on.
    """
    (app_dir / "config.toml").write_text(TWO_BOXES)
    (app_dir / "items.csv").write_text(
        "sku,weight_oz,parcel,notes\nMUG-1,14,default,\n"
    )
    return create_app(app_dir / "config.toml").test_client()


def test_the_products_tab_still_lists_the_rows_it_has_to_fix(stale_parcel_client):
    text = stale_parcel_client.get("/items").get_data(as_text=True)
    assert 'value="MUG-1"' in text


def test_it_says_what_is_wrong(stale_parcel_client):
    text = stale_parcel_client.get("/items").get_data(as_text=True)
    assert "not configured" in text or "isn&#39;t configured" in text


def test_the_box_dropdown_still_offers_the_real_boxes(stale_parcel_client):
    # An empty dropdown would blank every row's parcel on save — turning a
    # fixable mistake into a silent data loss.
    text = stale_parcel_client.get("/items").get_data(as_text=True)
    assert 'value="small"' in text and 'value="medium"' in text


def test_the_fix_can_be_saved_from_the_page(stale_parcel_client, app_dir):
    stale_parcel_client.post(
        "/items",
        data={"sku": ["MUG-1"], "weight_oz": ["14"], "parcel": ["small"], "notes": [""]},
        follow_redirects=True,
    )
    assert "MUG-1,14,small," in (app_dir / "items.csv").read_text()
    # ...and the config loads again afterwards.
    from etsy_auto_print.config import load_config
    assert load_config(app_dir / "config.toml").labels.item_parcels == {"MUG-1": "small"}


# --- restarting the right process -------------------------------------------


def test_the_stale_banner_offers_the_button_that_actually_fixes_it(client, monkeypatch):
    # "Restart poller" restarts the wrong process, and the banner sits right
    # above it — so the obvious click looks like a fix that didn't work.
    monkeypatch.setattr(dashboard, "version_label", lambda: "0.2.0 (new)")
    monkeypatch.setattr(dashboard, "running_version", lambda: "0.1.0 (old)")
    text = client.get("/").get_data(as_text=True)
    assert "restart-dashboard" in text
    assert "Restart poller does not fix this" in text


def test_both_restarts_are_offered_and_named_apart(client):
    text = client.get("/").get_data(as_text=True)
    assert ">Restart poller<" in text
    assert ">Restart dashboard<" in text


class FakeSystemd:
    """Answers the questions the restart asks, one scenario per instance."""

    def __init__(self, installed=True, service_pid="self", sudo_ok=True,
                 restart_ok=True):
        import os
        self.installed = installed
        self.pid = str(os.getpid()) if service_pid == "self" else str(service_pid)
        self.sudo_ok = sudo_ok
        self.restart_ok = restart_ok
        self.ran = []

    def __call__(self, cmd, timeout=10):
        self.ran.append(cmd)
        if cmd[:2] == ["systemctl", "cat"]:
            return (0, "[Unit]") if self.installed else (1, "No files found")
        if cmd[:2] == ["systemctl", "show"]:
            return (0, self.pid)
        if cmd[:3] == ["sudo", "-n", "-l"]:
            return (0, "/usr/bin/systemctl") if self.sudo_ok else \
                (1, "sudo: a password is required")
        if cmd[:2] == ["sudo", "-n"]:
            return (0, "") if self.restart_ok else (1, "Failed to restart unit")
        return (0, "")

    def restarts(self):
        return [c for c in self.ran if c[:2] == ["sudo", "-n"] and "-l" not in c]


@pytest.fixture
def systemd(monkeypatch):
    fake = FakeSystemd()
    monkeypatch.setattr(dashboard.checks, "_run", fake)
    return fake


@pytest.fixture
def timers(monkeypatch):
    """The deferred restart, captured instead of fired."""
    captured = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            captured.append(self)

        def start(self):
            pass

    monkeypatch.setattr(dashboard.threading, "Timer", FakeTimer)
    return captured


def test_the_restart_uses_no_block(systemd):
    out = dashboard._restart_dashboard()
    (command,) = systemd.restarts()
    assert command[2:] == dashboard.RESTART_DASHBOARD
    assert "--no-block" in command
    assert "reload this page" in out


def test_sudo_is_asked_about_the_exact_command_it_will_run(systemd):
    # The old check asked about `systemctl is-active` and then ran
    # `systemctl restart` — sudo matches command lines exactly, so a yes to
    # one said nothing about the other.
    dashboard._restart_dashboard()
    checked = [c for c in systemd.ran if c[:3] == ["sudo", "-n", "-l"]]
    assert checked == [["sudo", "-n", "-l", *dashboard.RESTART_DASHBOARD]]


def test_it_refuses_when_there_is_no_dashboard_service(monkeypatch):
    # Stopping something nothing will restart leaves no dashboard at all.
    fake = FakeSystemd(installed=False)
    monkeypatch.setattr(dashboard.checks, "_run", fake)
    with pytest.raises(RuntimeError, match="install-service.sh dashboard"):
        dashboard._restart_dashboard()
    assert fake.restarts() == []


def test_it_refuses_when_this_page_is_not_the_service(monkeypatch):
    # The desktop launcher starts its own copy. Restarting the service then
    # leaves this page exactly as it was — and they fight over the port.
    fake = FakeSystemd(service_pid=1)
    monkeypatch.setattr(dashboard.checks, "_run", fake)
    with pytest.raises(RuntimeError, match="launcher"):
        dashboard._restart_dashboard()
    assert fake.restarts() == []


def test_a_service_that_cannot_start_is_blamed_on_the_port(monkeypatch):
    fake = FakeSystemd(service_pid=0)
    monkeypatch.setattr(dashboard.checks, "_run", fake)
    with pytest.raises(RuntimeError, match="holds its port"):
        dashboard._restart_dashboard()


def test_a_denied_sudo_says_how_to_allow_it(monkeypatch):
    fake = FakeSystemd(sudo_ok=False)
    monkeypatch.setattr(dashboard.checks, "_run", fake)
    with pytest.raises(RuntimeError, match="install-service.sh dashboard"):
        dashboard._restart_dashboard()
    assert fake.restarts() == []


def test_it_answers_with_a_waiting_page_not_a_redirect(client, systemd, timers):
    # The action kills the server that would serve a redirect.
    resp = client.post("/action/restart-dashboard")
    assert resp.status_code == 200
    assert "Restarting the dashboard" in resp.get_data(as_text=True)


def test_the_waiting_page_reloads_itself(client, systemd, timers):
    text = client.post("/action/restart-dashboard").get_data(as_text=True)
    assert 'http-equiv="refresh"' in text and "url=/" in text


def test_the_restart_is_deferred_until_after_the_response(client, systemd, timers):
    client.post("/action/restart-dashboard")
    assert len(timers) == 1 and timers[0].delay > 0
    assert systemd.restarts() == []           # not yet


def test_a_refused_restart_goes_back_to_the_page_instead_of_waiting(client, monkeypatch):
    # A waiting page for a restart that will never happen is worse than an
    # error, because it looks like it worked.
    monkeypatch.setattr(dashboard.checks, "_run", FakeSystemd(sudo_ok=False))
    resp = client.post("/action/restart-dashboard", follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "Restarting the dashboard" not in text
    assert "without a password" in text


def test_a_deferred_restart_that_fails_says_so_on_the_next_page(
    client, monkeypatch, timers
):
    # By the time it runs the response is gone. Silently dropping the error
    # is what made this look like it worked and changed nothing.
    fake = FakeSystemd(restart_ok=False)
    monkeypatch.setattr(dashboard.checks, "_run", fake)
    client.post("/action/restart-dashboard")
    timers[0].fn()                              # the restart fires, and fails
    text = client.get("/orders").get_data(as_text=True)
    assert "Restart dashboard failed" in text
    assert "Failed to restart unit" in text
    # ...once, not on every page from now on.
    assert "Restart dashboard failed" not in client.get("/orders").get_data(as_text=True)


def test_other_actions_still_redirect(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_do_action", lambda *a: "done")
    assert client.post("/action/poll").status_code == 302


# --- orders waiting to tell Etsy they shipped --------------------------------


@pytest.fixture
def waiting_order(app_dir):
    from etsy_auto_print.store import Store

    store = Store(app_dir / "orders.db")
    store.register({"receipt_id": 555, "name": "A Buyer", "transactions": []})
    for state in ("slip_printed", "label_purchased", "label_printed"):
        store.transition(555, state, "")
    store.save_label(555, object_id="t1", carrier="USPS", service="Ground Advantage",
                     tracking_number="9400LIVE", is_test=False)
    return 555


def test_a_waiting_order_shows_when_etsy_will_hear(client, waiting_order):
    text = client.get("/orders").get_data(as_text=True)
    assert "Etsy told" in text
    assert ">Shipped today<" in text


def test_the_post_office_button_appears_when_anything_is_waiting(client, waiting_order):
    text = client.get("/orders").get_data(as_text=True)
    assert "Mark all shipped now" in text
    assert "1 printed, waiting" in text


def test_no_post_office_button_when_nothing_is_waiting(client):
    assert "Mark all shipped now" not in client.get("/orders").get_data(as_text=True)


def test_a_test_label_is_never_offered_as_shippable(client, app_dir, waiting_order):
    # Test labels never reach Etsy, so a button to tell Etsy would be a lie.
    import sqlite3

    with sqlite3.connect(app_dir / "orders.db") as db:
        db.execute("UPDATE labels SET is_test = 1")
    assert ">Shipped today<" not in client.get("/orders").get_data(as_text=True)


@pytest.mark.parametrize("form, expected", [
    ({"receipt_id": "555"}, ["mark-shipped", "555"]),
    ({"all": "1"}, ["mark-shipped", "--all"]),
])
def test_the_buttons_run_mark_shipped(client, monkeypatch, form, expected):
    ran = []
    monkeypatch.setattr(dashboard.checks, "_run",
                        lambda cmd, timeout=10: (ran.append(cmd), (0, "ok"))[1])
    client.post("/action/mark-shipped", data=form)
    assert ran and ran[-1][-len(expected):] == expected


def test_a_bad_order_number_is_refused(client, monkeypatch):
    ran = []
    monkeypatch.setattr(dashboard.checks, "_run",
                        lambda cmd, timeout=10: (ran.append(cmd), (0, ""))[1])
    resp = client.post("/action/mark-shipped", data={"receipt_id": "1; rm -rf /"},
                       follow_redirects=True)
    # The redirect back runs the page's own health checks; what matters is
    # that the malformed id never reached a command line.
    assert not [cmd for cmd in ran if "mark-shipped" in cmd]
    assert "failed" in resp.get_data(as_text=True)
