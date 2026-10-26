"""Box and settings edits land in config.toml without a migration."""

import tomllib

from etsy_auto_print.config import load_config
from etsy_auto_print.config_form import (
    apply_boxes,
    apply_settings,
    boxes_from_labels,
    default_parcel_from_labels,
    read_settings,
    sync_item_parcels,
)

SHIP = """
[labels.ship_from]
name = "Shop"
street1 = "1 St"
city = "Portland"
state = "OR"
zip = "97201"
country = "US"
email = "shop@example.com"
"""

OWNER = """
# do not drop this comment
[etsy]
keystring = "k"
shared_secret = "s"

[printer]
backend = "file"
outbox = "outbox"

[labels]
enabled = true
shippo_token = "shippo_test_fake"
default_parcel = "single"
items_csv = "items.csv"

""" + SHIP + """
[labels.parcels.single]
length_in = 8
width_in = 4
height_in = 1.5
packaging_oz = 0.5
max_items = 1

[labels.parcels.multi]
length_in = 12
width_in = 8.5
height_in = 2
packaging_oz = 1.0
max_items = 4

[labels.parcels.clamp]
length_in = 6
width_in = 4
height_in = 3
packaging_oz = 1.8

[labels.item_parcels]
"Track-Clamp-Small" = "clamp"
"Track-Clamp-Large" = "clamp"
"""


def _ready(tmp_path, text=OWNER, rows="TRACK,2,single,track\n"):
    (tmp_path / "items.csv").write_text("sku,weight_oz,parcel,notes\n" + rows)
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


class Form(dict):
    """Enough of a Flask form for the editor: get() and getlist()."""

    def getlist(self, name):
        value = self.get(name, [])
        if isinstance(value, list):
            return value
        return [value]


def _boxes(text):
    labels = tomllib.loads(text)["labels"]
    rows = boxes_from_labels(labels)
    return rows, default_parcel_from_labels(labels)


def test_the_owner_file_loads_with_no_migration(tmp_path):
    labels = load_config(_ready(tmp_path)).labels
    assert set(labels.parcels) == {"single", "multi", "clamp"}
    assert labels.parcels["single"]["packaging_oz"] == 0.5
    assert labels.parcels["clamp"]["packaging_oz"] == 1.8
    assert labels.parcels["clamp"].get("max_items") is None
    assert labels.item_parcels["Track-Clamp-Small"] == "clamp"
    assert labels.default_parcel == "single"
    assert labels.item_weights_oz["TRACK"] == 2


def test_editing_one_box_keeps_the_others_and_the_comment(tmp_path):
    path = _ready(tmp_path)
    rows, default = _boxes(path.read_text())
    for row in rows:
        if row["name"] == "clamp":
            row["packaging_oz"] = "2.2"
    # parse_boxes wants a form; apply_boxes wants typed numbers.
    typed = []
    for row in rows:
        typed.append({
            **row,
            "length_in": float(row["length_in"]),
            "width_in": float(row["width_in"]),
            "height_in": float(row["height_in"]),
            "packaging_oz": float(row["packaging_oz"]),
            "max_items": int(row["max_items"]) if str(row["max_items"]).strip() else None,
        })
    updated, rename = apply_boxes(path.read_text(), typed, default)
    assert rename == {}
    assert "# do not drop this comment" in updated
    assert 'shippo_token = "shippo_test_fake"' in updated
    path.write_text(updated)
    labels = load_config(path).labels
    assert labels.parcels["clamp"]["packaging_oz"] == 2.2
    assert labels.parcels["single"]["max_items"] == 1
    assert labels.parcels["multi"]["packaging_oz"] == 1.0
    assert labels.item_parcels["Track-Clamp-Large"] == "clamp"


def test_renaming_a_box_follows_the_sku_map(tmp_path):
    path = _ready(tmp_path)
    rows, default = _boxes(path.read_text())
    typed = []
    for row in rows:
        name = "mailer" if row["name"] == "single" else row["name"]
        typed.append({
            **row,
            "name": name,
            "length_in": float(row["length_in"]),
            "width_in": float(row["width_in"]),
            "height_in": float(row["height_in"]),
            "packaging_oz": float(row["packaging_oz"]),
            "max_items": int(row["max_items"]) if str(row["max_items"]).strip() else None,
        })
    updated, rename = apply_boxes(path.read_text(), typed, "mailer")
    assert rename == {"single": "mailer"}
    path.write_text(updated)
    # The products file still says "single" until the dashboard applies the
    # rename map. Do that here so load_config sees one consistent name.
    csv = tmp_path / "items.csv"
    csv.write_text(csv.read_text().replace(",single,", ",mailer,"))
    labels = load_config(path).labels
    assert "single" not in labels.parcels
    assert labels.parcels["mailer"]["packaging_oz"] == 0.5
    assert labels.default_parcel == "mailer"
    assert labels.item_parcels["Track-Clamp-Small"] == "clamp"


def test_a_legacy_single_box_stays_a_single_block(tmp_path):
    text = (
        '[etsy]\nkeystring = "k"\nshared_secret = "s"\n'
        '[printer]\nbackend = "file"\n'
        '[labels]\nenabled = true\nshippo_token = "t"\n'
        + SHIP
        + "[labels.parcel]\nlength_in = 10\nwidth_in = 7\nheight_in = 4\n"
        "packaging_oz = 3\ndefault_item_oz = 8\n"
    )
    path = _ready(tmp_path, text, "")
    box = {
        "original": "default",
        "name": "default",
        "length_in": 10.0,
        "width_in": 7.0,
        "height_in": 5.0,
        "packaging_oz": 3.0,
        "max_items": None,
    }
    updated, _ = apply_boxes(text, [box], "default")
    assert "[labels.parcel]" in updated
    assert "[labels.parcels." not in updated
    assert "default_item_oz = 8" in updated or "default_item_oz = 8.0" in updated
    path.write_text(updated)
    labels = load_config(path).labels
    assert labels.parcels["default"]["height_in"] == 5
    assert labels.parcels["default"]["default_item_oz"] == 8


def test_adding_a_second_box_converts_the_legacy_block(tmp_path):
    text = (
        '[etsy]\nkeystring = "k"\nshared_secret = "s"\n'
        '[printer]\nbackend = "file"\n'
        '[labels]\nenabled = true\nshippo_token = "t"\n'
        + SHIP
        + "[labels.parcel]\nlength_in = 8\nwidth_in = 4\nheight_in = 1.5\n"
        "packaging_oz = 0.5\n"
    )
    path = _ready(tmp_path, text, "")
    boxes = [
        {"original": "default", "name": "single", "length_in": 8.0, "width_in": 4.0,
         "height_in": 1.5, "packaging_oz": 0.5, "max_items": 1},
        {"original": "", "name": "multi", "length_in": 12.0, "width_in": 8.5,
         "height_in": 2.0, "packaging_oz": 1.0, "max_items": 4},
    ]
    updated, rename = apply_boxes(text, boxes, "single")
    assert rename == {"default": "single"}
    assert "[labels.parcel]\n" not in updated
    assert "[labels.parcels.single]" in updated
    assert "[labels.parcels.multi]" in updated
    path.write_text(updated)
    labels = load_config(path).labels
    assert labels.default_parcel == "single"
    assert labels.parcels["multi"]["max_items"] == 4


def test_an_unknown_parcel_key_is_dropped_when_the_box_is_saved(tmp_path):
    text = OWNER.replace(
        "[labels.parcels.single]\n",
        "[labels.parcels.single]\nweight_oz = 2.5\n",
    )
    path = _ready(tmp_path, text)
    rows, default = _boxes(text)
    typed = []
    for row in rows:
        typed.append({
            **row,
            "length_in": float(row["length_in"]),
            "width_in": float(row["width_in"]),
            "height_in": float(row["height_in"]),
            "packaging_oz": float(row["packaging_oz"]),
            "max_items": int(row["max_items"]) if str(row["max_items"]).strip() else None,
        })
    updated, _ = apply_boxes(text, typed, default)
    assert "weight_oz" not in updated
    path.write_text(updated)
    load_config(path)  # the bad key was why this file would not load


def test_sync_updates_a_toml_override_and_drops_a_blank_one():
    text = '[labels.item_parcels]\n"MUG-1" = "small"\n"GONE" = "small"\n'
    updated = sync_item_parcels(text, {"MUG-1": "medium"}, {"GONE"})
    assert '"MUG-1" = "medium"' in updated
    assert "GONE" not in updated


def test_sync_leaves_csv_only_skus_out_of_the_toml_table():
    text = '[labels]\nenabled = false\n'
    assert sync_item_parcels(text, {"MUG-1": "small"}, set()) == text


def test_settings_change_the_address_and_leave_the_token(tmp_path):
    path = _ready(tmp_path)
    form = _settings_form(path.read_text(), ship_street1="9 New St", file_type="ZPLII")
    updated = apply_settings(path.read_text(), form)
    assert 'shippo_token = "shippo_test_fake"' in updated
    assert "# do not drop this comment" in updated
    assert 'street1 = "9 New St"' in updated
    assert 'file_type = "ZPLII"' in updated
    path.write_text(updated)
    config = load_config(path)
    assert config.labels.ship_from["street1"] == "9 New St"
    assert config.labels.file_type == "ZPLII"
    assert config.labels.token == "shippo_test_fake"


def _settings_form(text, **overrides):
    settings = read_settings(tomllib.loads(text))
    settings.update(overrides)
    flags = (
        "labels_enabled", "allow_live", "validate_addresses", "hold_unmapped_upgrade",
        "stock_enabled", "notify_on_order", "listings_r",
    )
    data = {
        "file_type": settings["file_type"],
        "default_parcel": settings["default_parcel"],
        "mark_shipped": settings["mark_shipped"],
        "mark_shipped_hour": str(settings["mark_shipped_hour"]),
        "items_csv": settings["items_csv"],
        "allowed_providers": list(settings["allowed_providers"]),
        "ship_name": settings["ship_name"],
        "ship_street1": settings["ship_street1"],
        "ship_street2": settings["ship_street2"],
        "ship_city": settings["ship_city"],
        "ship_state": settings["ship_state"],
        "ship_zip": settings["ship_zip"],
        "ship_country": settings["ship_country"],
        "ship_email": settings["ship_email"],
        "ship_phone": settings["ship_phone"],
        "backend": settings["backend"],
        "outbox": settings["outbox"],
        "cups_queue": settings["cups_queue"],
        "slip_format": settings["slip_format"],
        "slip_queue": settings["slip_queue"],
        "interval_seconds": str(settings["interval_seconds"]),
        "label_delay_minutes": str(settings["label_delay_minutes"]),
        "redirect_port": str(settings["redirect_port"]),
        "low_threshold": str(settings["low_threshold"]),
        "interval_minutes": str(settings["interval_minutes"]),
    }
    for name in flags:
        data[name] = ["0", "1"] if settings[name] else ["0"]
    data.update({k: v for k, v in overrides.items() if k not in flags})
    # overrides of flags were applied to settings before the flag loop,
    # so they are already encoded. Re-apply a flag override that arrived
    # as a bool via settings.update above.
    return Form(data)
