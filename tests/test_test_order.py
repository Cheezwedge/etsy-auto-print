"""`test-order` — one fake order down the real pipeline."""

import argparse

import pytest

from etsy_auto_print import cli
from etsy_auto_print.config import load_config

from .test_labels import FakeShippo

CONFIG = """
[etsy]
keystring = "k"
shared_secret = "s"

[printer]
backend = "file"
outbox = "outbox"
slip_format = "zpl"

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

[labels.item_weights_oz]
"MUG-BLUE-12OZ" = 14.0
"STAND-WAL" = 9.5
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    return load_config(tmp_path / "config.toml")


@pytest.fixture(autouse=True)
def fake_shippo(monkeypatch):
    monkeypatch.setattr(cli, "make_client", lambda token, allow_live: FakeShippo())


def run(config, **kw):
    return cli.cmd_test_order(config, argparse.Namespace(sku=kw.get("sku")))


def test_prints_slip_then_label(config, tmp_path, capsys):
    assert run(config) == 0
    out = capsys.readouterr().out
    assert "slip_printed" in out and "label_printed" in out
    # Both came out, and the slip is the ZPL one that goes to the label printer.
    assert (tmp_path / "outbox" / "packing-slip-999999999.zpl").exists()
    assert (tmp_path / "outbox" / "label-999999999.pdf").exists()


def test_uses_a_sku_you_actually_sell(config, capsys):
    run(config)
    # Picks the first configured SKU, so the weight and box lookup is real
    # rather than the sample data's placeholder.
    assert "SKU MUG-BLUE-12OZ" in capsys.readouterr().out


def test_specific_sku_can_be_named(config, capsys):
    run(config, sku="STAND-WAL")
    assert "SKU STAND-WAL" in capsys.readouterr().out


def test_unknown_sku_warns_and_then_holds(config, capsys):
    assert run(config, sku="NOT-A-REAL-SKU") == 1
    captured = capsys.readouterr()
    assert "no weight configured" in captured.out
    assert "HELD" in captured.err


def test_the_fake_order_never_lands_in_the_real_database(config, tmp_path):
    run(config)
    # A temp database is used, so `status` and the double-buy guard that
    # protect real orders never see order #999999999.
    assert not (tmp_path / "orders.db").exists()


def test_live_token_is_refused(config, monkeypatch, capsys):
    class LiveShippo(FakeShippo):
        is_test = False

    monkeypatch.setattr(cli, "make_client", lambda token, allow_live: LiveShippo())
    assert run(config) == 1
    assert "real postage" in capsys.readouterr().err


def test_works_with_labels_disabled(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text(CONFIG.replace("enabled = true", "enabled = false"))
    monkeypatch.chdir(tmp_path)
    assert run(load_config(tmp_path / "config.toml")) == 0
    out = capsys.readouterr().out
    assert "slip_printed" in out
    assert "Labels are disabled" in out
