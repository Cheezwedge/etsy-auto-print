"""The dashboard edits config.toml in place. Comments and secrets stay."""

from pathlib import Path

import pytest

from etsy_auto_print.toml_edit import delete_table, set_key

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = (ROOT / "config.example.toml").read_text()


def test_setting_a_key_keeps_the_comment_above_it_and_the_secret():
    updated = set_key(EXAMPLE, "labels", "file_type", "ZPLII")
    assert 'file_type = "ZPLII"' in updated
    assert "Label format: PDF_4x6" in updated
    assert 'keystring = "your-etsy-app-keystring"' in updated
    assert 'shippo_token = ""' in updated
    # The rest of the file is not rewritten into a different shape.
    assert "[labels.ship_from]" in updated
    assert "[labels.parcel]" in updated


def test_an_inline_comment_on_the_key_survives():
    text = '[labels]\nenabled = false  # slips only\n'
    assert set_key(text, "labels", "enabled", True) == (
        '[labels]\nenabled = true  # slips only\n'
    )


def test_a_missing_key_is_added_inside_its_table():
    text = "[labels]\nenabled = false\n\n[labels.ship_from]\nname = \"Shop\"\n"
    updated = set_key(text, "labels", "file_type", "PNG")
    assert updated.index("file_type") < updated.index("[labels.ship_from]")
    assert 'name = "Shop"' in updated


def test_a_missing_table_is_appended():
    text = "[labels]\nenabled = false\n"
    updated = set_key(text, "labels.ship_from", "city", "Portland")
    assert updated.endswith('[labels.ship_from]\ncity = "Portland"\n')
    assert updated.startswith("[labels]\nenabled = false\n")


def test_delete_key_and_delete_table_leave_the_neighbours():
    text = (
        "[labels]\nenabled = true\ndefault_parcel = \"single\"\n\n"
        "[labels.parcels.single]\nlength_in = 8\n\n"
        "[labels.parcels.multi]\nlength_in = 12\n"
    )
    without_key = set_key(text, "labels", "default_parcel", None)
    assert "default_parcel" not in without_key
    assert "enabled = true" in without_key
    without_table = delete_table(without_key, "labels.parcels.single")
    assert "[labels.parcels.single]" not in without_table
    assert "[labels.parcels.multi]" in without_table
    assert "length_in = 12" in without_table


def test_quoted_sku_keys_round_trip():
    text = '[labels.item_parcels]\n"Track-Clamp-Small" = "clamp"\n'
    updated = set_key(text, "labels.item_parcels", "Track-Clamp-Small", "multi")
    assert '"Track-Clamp-Small" = "multi"' in updated


def test_a_multiline_array_is_replaced_with_one_line():
    text = '[labels]\nallowed_providers = [\n  "USPS",\n]\nfile_type = "PDF_4x6"\n'
    updated = set_key(text, "labels", "allowed_providers", ["USPS", "UPS"])
    assert 'allowed_providers = ["USPS", "UPS"]\n' in updated
    assert 'file_type = "PDF_4x6"' in updated


def test_strings_with_quotes_are_escaped():
    text = "[labels.ship_from]\nname = \"Shop\"\n"
    updated = set_key(text, "labels.ship_from", "name", 'Say "hi"')
    assert r'name = "Say \"hi\""' in updated


def test_refuses_a_value_it_cannot_parse():
    text = "[labels]\nallowed_providers = [\n"
    with pytest.raises(Exception, match="could not parse"):
        set_key(text, "labels", "allowed_providers", ["USPS"])
