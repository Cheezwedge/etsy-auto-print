"""`listings` — the pre-flight for a shop that is actually live.

Fake orders test the pipeline. Only this tests the join between the pipeline
and what a buyer can actually click Buy on: a listing whose SKU this program
has no weight for holds the order, and a held first order is a real person
waiting.
"""

import argparse

import pytest

from etsy_auto_print import cli
from etsy_auto_print.config import load_config

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
length_in = 8
width_in = 4
height_in = 1.5
packaging_oz = 0.5

[labels.item_weights_oz]
"TPU-Assorted" = 2.5
"TPU-Old-Hole" = 2.5
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    return load_config(tmp_path / "config.toml")


def run(config, listings, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_build_client",
        lambda cfg: type("C", (), {"get_active_listings": lambda self: listings})(),
    )
    code = cli.cmd_listings(config, argparse.Namespace())
    return code, capsys.readouterr().out


def listing(title, *skus):
    return {"title": title, "skus": list(skus)}


def test_a_fully_configured_shop_is_cleared_to_take_orders(config, monkeypatch, capsys):
    code, out = run(config, [
        listing("Assorted set", "TPU-Assorted"),
        listing("Old style", "TPU-Old-Hole"),
    ], monkeypatch, capsys)
    assert code == 0
    assert "Safe to take an order" in out
    assert "2.5 oz" in out


def test_a_listing_with_no_sku_is_the_loudest_finding(config, monkeypatch, capsys):
    # Etsy doesn't require a SKU, so this is the easy thing to forget — and
    # the receipt then arrives with nothing to look a weight up by.
    code, out = run(config, [listing("Assorted set")], monkeypatch, capsys)
    assert code == 1
    assert "no SKU set on this listing" in out


def test_an_unconfigured_sku_is_caught(config, monkeypatch, capsys):
    code, out = run(config, [listing("New style", "TPU-New-Post")], monkeypatch, capsys)
    assert code == 1
    assert "no weight configured" in out


def test_a_case_or_spacing_mismatch_is_named_as_such(config, monkeypatch, capsys):
    # "TPU-assorted" against "TPU-Assorted" looks correct in both places;
    # "has no weight configured" would send you looking in the wrong file.
    code, out = run(config, [listing("Assorted", "TPU-assorted ")], monkeypatch, capsys)
    assert code == 1
    assert "differs only by case or spacing" in out
    assert "TPU-Assorted" in out


def test_every_problem_is_reported_not_just_the_first(config, monkeypatch, capsys):
    code, out = run(config, [
        listing("One"), listing("Two", "NOPE"), listing("Three", "TPU-Assorted"),
    ], monkeypatch, capsys)
    assert code == 1
    assert "2 problem(s)" in out


def test_a_listing_with_several_variations_checks_each(config, monkeypatch, capsys):
    code, out = run(
        config, [listing("All four", "TPU-Assorted", "TPU-Old-Hole", "TPU-New-Post")],
        monkeypatch, capsys,
    )
    assert code == 1
    assert "TPU-New-Post" in out


def test_configured_skus_not_on_sale_are_noted_but_not_failed(config, monkeypatch, capsys):
    # A retired variation or a draft listing is normal, not an error.
    code, out = run(config, [listing("Assorted", "TPU-Assorted")], monkeypatch, capsys)
    assert code == 0
    assert "not on any active listing: TPU-Old-Hole" in out
    assert "Harmless" in out


def test_an_empty_shop_says_so_rather_than_passing(config, monkeypatch, capsys):
    code, out = run(config, [], monkeypatch, capsys)
    assert "nothing can be ordered yet" in out.lower()
    assert "Safe to take an order" not in out


def test_a_missing_skus_field_is_treated_as_no_sku(config, monkeypatch, capsys):
    # Defensive: don't crash on a listing shape without the field at all.
    code, out = run(config, [{"title": "Assorted"}], monkeypatch, capsys)
    assert code == 1
    assert "no SKU set" in out


def test_blank_skus_do_not_count_as_configured(config, monkeypatch, capsys):
    code, out = run(config, [listing("Assorted", "", "  ")], monkeypatch, capsys)
    assert code == 1
    assert "no SKU set" in out


# --- digital listings ------------------------------------------------------


def download(title):
    """What Etsy actually returns for an instant-download listing."""
    return {"title": title, "skus": [], "listing_type": "download"}


def test_a_digital_listing_is_not_a_missing_sku(config, monkeypatch, capsys):
    # Nothing is packed or posted, so having no SKU is correct here. Flagging
    # it trains you to ignore the one check whose job is catching a real one.
    code, out = run(config, [
        listing("Assorted set", "TPU-Assorted"),
        download("3D Print Files"),
    ], monkeypatch, capsys)
    assert code == 0
    assert "HOLD" not in out
    assert "1 digital listing(s)" in out
    assert "3D Print Files" in out


@pytest.mark.parametrize("shape", [
    {"listing_type": "download"},   # the documented v3 field
    {"is_digital": True},           # alternate spellings kept deliberately
    {"type": "download"},
])
def test_every_spelling_of_download_is_recognised(config, monkeypatch, capsys, shape):
    code, out = run(config, [{"title": "Files", "skus": [], **shape}], monkeypatch, capsys)
    assert code == 0 and "digital listing(s)" in out


def test_a_hold_reports_what_etsy_called_the_listing(config, monkeypatch, capsys):
    # The first attempt at this matched on a field Etsy doesn't send, and the
    # output gave no way to tell. Now the value itself is on the line.
    code, out = run(
        config, [{"title": "Mystery", "skus": [], "listing_type": "physical"}],
        monkeypatch, capsys,
    )
    assert code == 1
    assert "Etsy calls it: physical" in out


def test_a_hold_says_so_when_etsy_reports_no_type_at_all(config, monkeypatch, capsys):
    code, out = run(config, [{"title": "Mystery", "skus": []}], monkeypatch, capsys)
    assert "Etsy calls it: not reported" in out


def test_a_physical_listing_is_never_excused_as_digital(config, monkeypatch, capsys):
    # "both" ships something, and an unknown value must fail loud, not quiet.
    for shape in [{"listing_type": "both"}, {"listing_type": "physical"},
                  {"type": "both"}, {"is_digital": False}, {}]:
        code, out = run(config, [{"title": "Adapters", "skus": [], **shape}],
                        monkeypatch, capsys)
        assert code == 1, shape
        assert "no SKU set" in out


def test_a_shop_of_only_downloads_needs_no_products(config, monkeypatch, capsys):
    code, out = run(config, [download("Files A"), download("Files B")], monkeypatch, capsys)
    assert code == 0
    assert "2 digital listing(s)" in out
