import pytest

from etsy_auto_print.config import ConfigError, load_config

BASE = '[etsy]\nkeystring = "k"\nshared_secret = "s"\n'
SHIP_FROM = (
    '[labels.ship_from]\n'
    'name = "Shop"\nstreet1 = "1 St"\ncity = "Portland"\nstate = "OR"\n'
    'zip = "97201"\ncountry = "US"\nemail = "shop@example.com"\n'
)


def write(tmp_path, body):
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE + body)
    return cfg


def test_old_style_single_parcel_normalizes_to_one_preset(tmp_path):
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\n'
        + SHIP_FROM
        + '[labels.parcel]\nlength_in = 10\nwidth_in = 7\nheight_in = 4\npackaging_oz = 3\n',
    )
    labels = load_config(cfg).labels
    assert labels.parcels == {"default": {"length_in": 10, "width_in": 7, "height_in": 4, "packaging_oz": 3}}
    assert labels.default_parcel == "default"
    assert labels.item_parcels == {}


def test_new_style_multi_preset_loads(tmp_path):
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\ndefault_parcel = "small"\n'
        + SHIP_FROM
        + '[labels.parcels.small]\nlength_in = 6\nwidth_in = 4\nheight_in = 2\npackaging_oz = 1\n'
        + '[labels.parcels.large]\nlength_in = 14\nwidth_in = 10\nheight_in = 6\npackaging_oz = 5\n'
        + '[labels.item_parcels]\n"MUG-1" = "large"\n',
    )
    labels = load_config(cfg).labels
    assert set(labels.parcels) == {"small", "large"}
    assert labels.default_parcel == "small"
    assert labels.item_parcels == {"MUG-1": "large"}


def test_missing_box_config_errors_when_enabled(tmp_path):
    cfg = write(tmp_path, '[labels]\nenabled = true\nshippo_token = "t"\n' + SHIP_FROM)
    with pytest.raises(ConfigError, match="No parcel/box size configured"):
        load_config(cfg)


def test_incomplete_preset_errors_with_its_name(tmp_path):
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\n'
        + SHIP_FROM
        + '[labels.parcels.small]\nlength_in = 6\nwidth_in = 4\n',  # missing height_in, packaging_oz
    )
    with pytest.raises(ConfigError, match=r"labels\.parcels\.small.*height_in"):
        load_config(cfg)


def test_unknown_default_parcel_errors(tmp_path):
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\ndefault_parcel = "ghost"\n'
        + SHIP_FROM
        + '[labels.parcels.small]\nlength_in = 6\nwidth_in = 4\nheight_in = 2\npackaging_oz = 1\n',
    )
    with pytest.raises(ConfigError, match="default_parcel.*ghost.*no such preset"):
        load_config(cfg)


def test_labels_disabled_skips_box_validation(tmp_path):
    cfg = write(tmp_path, "[labels]\nenabled = false\n")
    labels = load_config(cfg).labels
    assert labels.enabled is False


def test_items_csv_loads_weights_and_parcels(tmp_path):
    (tmp_path / "items.csv").write_text(
        "sku,weight_oz,parcel\n"
        "MUG-1,14,large\n"
        "STICKER,0.5,small\n"
    )
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\nitems_csv = "items.csv"\n'
        + SHIP_FROM
        + '[labels.parcels.small]\nlength_in = 6\nwidth_in = 4\nheight_in = 2\npackaging_oz = 1\n'
        + '[labels.parcels.large]\nlength_in = 14\nwidth_in = 10\nheight_in = 6\npackaging_oz = 5\n',
    )
    labels = load_config(cfg).labels
    assert labels.item_weights_oz == {"MUG-1": 14.0, "STICKER": 0.5}
    assert labels.item_parcels == {"MUG-1": "large", "STICKER": "small"}


def test_toml_entries_override_csv(tmp_path):
    (tmp_path / "items.csv").write_text("sku,weight_oz\nMUG-1,14\n")
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\nitems_csv = "items.csv"\n'
        + SHIP_FROM
        + '[labels.parcel]\nlength_in = 10\nwidth_in = 7\nheight_in = 4\npackaging_oz = 3\n'
        + '[labels.item_weights_oz]\n"MUG-1" = 99.0\n',
    )
    assert load_config(cfg).labels.item_weights_oz["MUG-1"] == 99.0


def test_csv_tolerates_extra_columns_blank_rows_and_bom(tmp_path):
    (tmp_path / "items.csv").write_text(
        "﻿sku,notes,weight_oz,price\n"
        "MUG-1,my best seller,14,25.00\n"
        "\n"
        "# a comment row,,,\n"
        "STICKER,,0.5,3.00\n"
    )
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\nitems_csv = "items.csv"\n'
        + SHIP_FROM
        + '[labels.parcel]\nlength_in = 10\nwidth_in = 7\nheight_in = 4\npackaging_oz = 3\n',
    )
    assert load_config(cfg).labels.item_weights_oz == {"MUG-1": 14.0, "STICKER": 0.5}


def test_csv_bad_weight_reports_line_number(tmp_path):
    (tmp_path / "items.csv").write_text("sku,weight_oz\nMUG-1,heavy\n")
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\nitems_csv = "items.csv"\n'
        + SHIP_FROM
        + '[labels.parcel]\nlength_in = 10\nwidth_in = 7\nheight_in = 4\npackaging_oz = 3\n',
    )
    with pytest.raises(ConfigError, match="line 2.*not a number"):
        load_config(cfg)


def test_missing_csv_file_errors_clearly(tmp_path):
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\nitems_csv = "nope.csv"\n'
        + SHIP_FROM
        + '[labels.parcel]\nlength_in = 10\nwidth_in = 7\nheight_in = 4\npackaging_oz = 3\n',
    )
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(cfg)
