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


# --- settings that would otherwise do nothing at all ----------------------

BOX = '[labels.parcel]\nlength_in = 8\nwidth_in = 4\nheight_in = 1.5\npackaging_oz = 0.5\n'


def with_box(tmp_path, extra=""):
    return write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\n' + SHIP_FROM + BOX + extra,
    )


def test_a_per_item_weight_in_the_box_table_is_rejected(tmp_path):
    # It reads exactly like it should work, and it is silently ignored — the
    # label then ships declaring only the empty mailer's weight.
    cfg = with_box(tmp_path, "weight_oz = 2.5\n")
    with pytest.raises(ConfigError, match="weight_oz"):
        load_config(cfg)


def test_the_rejection_says_where_the_setting_does_belong(tmp_path):
    cfg = with_box(tmp_path, "weight_oz = 2.5\n")
    with pytest.raises(ConfigError, match="items CSV|default_item_oz"):
        load_config(cfg)


@pytest.mark.parametrize("typo", ["length = 8", "packaging_weight_oz = 1", "max_item = 2"])
def test_near_misses_are_caught_and_named(tmp_path, typo):
    with pytest.raises(ConfigError, match="did you mean"):
        load_config(with_box(tmp_path, typo + "\n"))


@pytest.mark.parametrize("setting", ["max_items = 1", "default_item_oz = 2.5"])
def test_the_real_optional_settings_still_load(tmp_path, setting):
    key, value = setting.split(" = ")
    assert load_config(with_box(tmp_path, setting + "\n")).labels.parcels["default"][key]


def test_an_unrecognized_key_names_the_table_it_is_in(tmp_path):
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\ndefault_parcel = "small"\n'
        + SHIP_FROM
        + '[labels.parcels.small]\nlength_in = 6\nwidth_in = 4\nheight_in = 2\n'
        'packaging_oz = 1\nweight_oz = 3\n',
    )
    with pytest.raises(ConfigError, match=r"labels\.parcels\.small"):
        load_config(cfg)


def test_both_box_styles_at_once_is_refused(tmp_path):
    # [labels.parcel] is ignored outright when presets exist, so the shop
    # would be shipping in a box it never configured, with no symptom.
    cfg = write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\ndefault_parcel = "small"\n'
        + SHIP_FROM
        + '[labels.parcel]\nlength_in = 8\nwidth_in = 4\nheight_in = 1.5\n'
        'packaging_oz = 0.5\n'
        + '[labels.parcels.small]\nlength_in = 6\nwidth_in = 4\nheight_in = 2\n'
        'packaging_oz = 1\n',
    )
    with pytest.raises(ConfigError, match="would be ignored entirely"):
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


# --- per-SKU box names that no longer exist --------------------------------


def two_boxes(tmp_path, csv_rows, extra=""):
    (tmp_path / "items.csv").write_text("sku,weight_oz,parcel,notes\n" + csv_rows)
    return write(
        tmp_path,
        '[labels]\nenabled = true\nshippo_token = "t"\ndefault_parcel = "single"\n'
        'items_csv = "items.csv"\n' + extra
        + SHIP_FROM
        + '[labels.parcels.single]\nlength_in = 8\nwidth_in = 4\nheight_in = 1.5\n'
        'packaging_oz = 0.5\nmax_items = 1\n'
        + '[labels.parcels.multi]\nlength_in = 12\nwidth_in = 8.5\nheight_in = 2\n'
        'packaging_oz = 1.0\nmax_items = 4\n',
    )


def test_a_csv_naming_a_renamed_box_fails_at_startup(tmp_path):
    # Renaming a preset leaves every row pointing at a box that's gone.
    # Discovering that one held order at a time, with a buyer waiting on
    # each, is the worst possible moment.
    cfg = two_boxes(tmp_path, "SKU-A,2.5,default,\n")
    with pytest.raises(ConfigError, match="name a box that isn't configured"):
        load_config(cfg)


def test_the_error_names_the_rows_the_boxes_and_the_file(tmp_path):
    cfg = two_boxes(tmp_path, "SKU-A,2.5,default,\nSKU-B,2.5,default,\n")
    with pytest.raises(ConfigError) as caught:
        load_config(cfg)
    message = str(caught.value)
    assert "SKU-A -> default" in message and "SKU-B -> default" in message
    assert "multi, single" in message          # what you could have used
    assert "items.csv" in message              # where to fix it
    assert "'single'" in message               # what blank would mean


def test_a_blank_parcel_column_falls_back_to_the_default(tmp_path):
    labels = load_config(two_boxes(tmp_path, "SKU-A,2.5,,\n")).labels
    assert labels.item_parcels == {}
    assert labels.default_parcel == "single"


def test_correct_box_names_load(tmp_path):
    labels = load_config(two_boxes(tmp_path, "SKU-A,2.5,multi,\n")).labels
    assert labels.item_parcels == {"SKU-A": "multi"}


def test_a_long_list_of_bad_rows_is_truncated(tmp_path):
    rows = "".join(f"SKU-{i},2.5,gone,\n" for i in range(14))
    with pytest.raises(ConfigError, match=r"and 4 more"):
        load_config(two_boxes(tmp_path, rows))


def test_a_bad_box_name_in_toml_points_at_the_toml(tmp_path):
    cfg = two_boxes(tmp_path, "SKU-A,2.5,,\n",
                    extra='[labels.item_parcels]\n"SKU-A" = "nope"\n')
    with pytest.raises(ConfigError, match="nope"):
        load_config(cfg)


# --- finding the config from somewhere else --------------------------------


def test_a_missing_config_says_where_it_looked(tmp_path, monkeypatch):
    # `ssh host 'etsy-auto-print ...'` runs in the home directory, and
    # "Config file not found: config.toml" gives no clue that the cwd is why.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "etsy_auto_print.config.installed_config_path", lambda: tmp_path / "nope.toml"
    )
    with pytest.raises(ConfigError) as caught:
        load_config()
    message = str(caught.value)
    assert str(tmp_path) in message
    assert "cd there first" in message and "-c " in message


def test_it_falls_back_to_the_checkout_when_run_from_elsewhere(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.toml").write_text(
        BASE + "[labels]\nenabled = false\n"
    )
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        "etsy_auto_print.config.installed_config_path", lambda: project / "config.toml"
    )
    assert load_config().keystring == "k"
    # Announced, not silent — it must never quietly load a config you didn't mean.
    assert str(project / "config.toml") in capsys.readouterr().err


def test_an_explicit_path_never_falls_back(tmp_path, monkeypatch):
    # -c means that file and no other; guessing would be worse than failing.
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.toml").write_text(BASE + "[labels]\nenabled = false\n")
    monkeypatch.setattr(
        "etsy_auto_print.config.installed_config_path", lambda: project / "config.toml"
    )
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "typo.toml")


def test_a_config_in_the_cwd_still_wins(tmp_path, monkeypatch):
    here = tmp_path / "here"
    here.mkdir()
    (here / "config.toml").write_text(BASE + '[labels]\nenabled = false\n')
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.toml").write_text(
        '[etsy]\nkeystring = "WRONG"\nshared_secret = "s"\n[labels]\nenabled = false\n'
    )
    monkeypatch.chdir(here)
    monkeypatch.setattr(
        "etsy_auto_print.config.installed_config_path", lambda: other / "config.toml"
    )
    assert load_config().keystring == "k"


# --- optional scopes -------------------------------------------------------


def minimal(tmp_path, etsy_extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[etsy]\nkeystring = "k"\nshared_secret = "s"\n' + etsy_extra
        + "[labels]\nenabled = false\n"
    )
    return load_config(cfg)


def test_listings_r_is_requested_by_default(tmp_path):
    config = minimal(tmp_path)
    assert config.optional_scopes == ("listings_r",)
    assert "listings_r" in config.oauth_scopes


def test_asking_for_nothing_optional_still_asks_for_the_essentials(tmp_path):
    # Etsy may refuse an optional scope outright; dropping it must not drop
    # the three the pipeline cannot work without.
    config = minimal(tmp_path, "optional_scopes = []\n")
    assert config.optional_scopes == ()
    assert "listings_r" not in config.oauth_scopes
    for scope in ("transactions_r", "transactions_w", "shops_r"):
        assert scope in config.oauth_scopes
