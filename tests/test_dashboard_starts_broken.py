"""The dashboard must start when the config on disk does not load.

It is the tool you fix a bad config with. Gating it on the config being
valid makes the one recovery path unreachable exactly when it's needed —
and the earlier tests for this called create_app() directly, which is not
how anyone starts it. These go through main(), where load_config actually
runs.
"""

import pytest

pytest.importorskip("flask")

from etsy_auto_print import cli  # noqa: E402

GOOD = """
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
length_in = 8
width_in = 4
height_in = 1.5
packaging_oz = 0.5
"""

BROKEN = GOOD + "weight_oz = 2.5\n"


@pytest.fixture
def served(monkeypatch):
    """Capture what main() hands to Flask instead of actually serving.

    cmd_dashboard imports create_app inside the function, so the patch has
    to land on the dashboard module — patching cli would leave the real
    Flask app to start and block.
    """
    from etsy_auto_print import dashboard

    calls = {}

    class FakeApp:
        def run(self, host, port, debug):
            calls.update(host=host, port=port)

    monkeypatch.setattr(
        dashboard, "create_app",
        lambda path, password: calls.update(path=path, password=password) or FakeApp(),
    )
    return calls


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return str(path)


def test_it_starts_on_a_config_that_does_not_load(tmp_path, served, capsys):
    code = cli.main(["-c", write(tmp_path, BROKEN), "dashboard"])
    assert code == 0
    assert served["port"] == 8765


def test_it_says_what_is_wrong_before_starting(tmp_path, served, capsys):
    cli.main(["-c", write(tmp_path, BROKEN), "dashboard"])
    err = capsys.readouterr().err
    assert "weight_oz" in err
    assert "Config tab" in err


def test_a_good_config_still_starts_silently(tmp_path, served, capsys):
    assert cli.main(["-c", write(tmp_path, GOOD), "dashboard"]) == 0
    assert "Config problem" not in capsys.readouterr().err


def test_the_password_survives_a_broken_config(tmp_path, served):
    # Read from raw TOML when load_config can't run — otherwise a typo in
    # [labels] would quietly unprotect a dashboard bound to the network.
    cli.main([
        "-c", write(tmp_path, BROKEN + '\n[dashboard]\npassword = "hunter2"\n'),
        "dashboard",
    ])
    assert served["password"] == "hunter2"


def test_a_network_bind_is_still_refused_without_a_password(tmp_path, served, capsys):
    code = cli.main([
        "-c", write(tmp_path, BROKEN), "dashboard", "--host", "0.0.0.0",
    ])
    assert code == 1
    assert "port" not in served, "must not serve"
    assert "without a password" in capsys.readouterr().err


def test_a_network_bind_is_allowed_when_the_raw_password_is_found(tmp_path, served):
    code = cli.main([
        "-c", write(tmp_path, BROKEN + '\n[dashboard]\npassword = "hunter2"\n'),
        "dashboard", "--host", "0.0.0.0",
    ])
    assert code == 0 and served["host"] == "0.0.0.0"


def test_unparseable_toml_also_starts_but_stays_local(tmp_path, served, capsys):
    # No password is recoverable from a file that isn't TOML, so the network
    # bind must fail closed rather than open.
    path = write(tmp_path, "not = = toml")
    assert cli.main(["-c", path, "dashboard"]) == 0
    assert cli.main(["-c", path, "dashboard", "--host", "0.0.0.0"]) == 1


def test_other_commands_still_refuse_a_broken_config(tmp_path, capsys):
    # Only the repair tool gets the exemption; `poll` on a bad config must
    # still stop rather than run on half-loaded settings.
    assert cli.main(["-c", write(tmp_path, BROKEN), "poll"]) == 1
    assert "weight_oz" in capsys.readouterr().err
