"""Turn dashboard form posts into edits of config.toml.

The file on disk stays the source of truth. These helpers change the keys
the forms own and leave every other line — tokens, comments, service-name
map — untouched. load_config() is still what decides whether the result is
allowed to be saved.
"""

from __future__ import annotations

import re
import tomllib

from .config import DEFAULT_OPTIONAL_SCOPES, _PARCEL_OPTIONAL, _PARCEL_REQUIRED
from .shipdate import POLICIES
from .toml_edit import delete_table, set_key

# Closed sets the form offers. A value already in the file that isn't in
# the list is still shown, so saving doesn't silently drop it.
FILE_TYPES = ("PDF_4x6", "PNG", "ZPLII")
PRINTER_BACKENDS = ("file", "cups")
SLIP_FORMATS = ("text", "zpl")
COMMON_PROVIDERS = ("USPS", "UPS", "FedEx", "DHL Express")

_BOX_NAME = re.compile(r"[A-Za-z0-9_-]+")
_PARCEL_KEYS = set(_PARCEL_REQUIRED) | set(_PARCEL_OPTIONAL)


class SettingsError(ValueError):
    """What the user typed can't be written. The file is unchanged."""


def parcel_table_names(labels: dict) -> list[str]:
    """Box names in file order. A legacy ``[labels.parcel]`` is ``default``."""
    parcels = labels.get("parcels")
    if isinstance(parcels, dict) and parcels:
        return list(parcels)
    if isinstance(labels.get("parcel"), dict) and labels.get("parcel"):
        return ["default"]
    return []


def boxes_from_labels(labels: dict) -> list[dict]:
    parcels = labels.get("parcels")
    if isinstance(parcels, dict) and parcels:
        source = parcels
    elif isinstance(labels.get("parcel"), dict) and labels.get("parcel"):
        source = {"default": labels["parcel"]}
    else:
        return []
    rows = []
    for name, box in source.items():
        box = box or {}
        max_items = box.get("max_items")
        rows.append({
            "original": name,
            "name": name,
            "length_in": _field_number(box.get("length_in", "")),
            "width_in": _field_number(box.get("width_in", "")),
            "height_in": _field_number(box.get("height_in", "")),
            "packaging_oz": _field_number(box.get("packaging_oz", "")),
            "max_items": "" if max_items is None else max_items,
        })
    return rows


def default_parcel_from_labels(labels: dict) -> str:
    if labels.get("default_parcel"):
        return str(labels["default_parcel"])
    if isinstance(labels.get("parcels"), dict) and labels.get("parcels"):
        return ""
    if labels.get("parcel"):
        return "default"
    return ""


def rows_from_box_form(form) -> list[dict]:
    """Posted box rows, including ones that aren't valid yet, for redisplay."""
    names = _getlist(form, "name")
    originals = _getlist(form, "original_name")
    lengths = _getlist(form, "length_in")
    widths = _getlist(form, "width_in")
    heights = _getlist(form, "height_in")
    weights = _getlist(form, "packaging_oz")
    limits = _getlist(form, "max_items")
    rows = []
    for i, name in enumerate(names):
        row = {
            "original": (originals[i] if i < len(originals) else "").strip(),
            "name": name.strip(),
            "length_in": (lengths[i] if i < len(lengths) else "").strip(),
            "width_in": (widths[i] if i < len(widths) else "").strip(),
            "height_in": (heights[i] if i < len(heights) else "").strip(),
            "packaging_oz": (weights[i] if i < len(weights) else "").strip(),
            "max_items": (limits[i] if i < len(limits) else "").strip(),
        }
        if not any(row[k] for k in ("name", "length_in", "width_in", "height_in",
                                    "packaging_oz", "max_items")):
            continue
        rows.append(row)
    return rows


def parse_boxes(form) -> tuple[list[dict], str]:
    """Validate posted boxes. Returns (typed rows, default parcel name)."""
    rows = rows_from_box_form(form)
    if not rows:
        raise SettingsError("Add at least one box.")
    seen: set[str] = set()
    typed = []
    for row in rows:
        name = row["name"]
        if not name:
            raise SettingsError("Every box needs a name.")
        original = row["original"]
        # A box already saved under a stranger name can be kept. A new name
        # has to be a plain TOML key so the table header stays readable.
        if not _BOX_NAME.fullmatch(name) and name != original:
            raise SettingsError(
                f"Box name {name!r} can use letters, numbers, dashes, and underscores."
            )
        if name in seen:
            raise SettingsError(f"Two boxes are named {name!r}.")
        seen.add(name)
        typed.append({
            **row,
            "length_in": _positive(row["length_in"], f"{name} length"),
            "width_in": _positive(row["width_in"], f"{name} width"),
            "height_in": _positive(row["height_in"], f"{name} height"),
            "packaging_oz": _nonnegative(row["packaging_oz"], f"{name} empty weight"),
            "max_items": _max_items(row["max_items"], name),
        })
    default = str(_get(form, "default_parcel", "")).strip()
    return typed, default


def apply_boxes(text: str, boxes: list[dict], default_parcel: str) -> tuple[str, dict[str, str]]:
    """Write the box tables. Returns (new text, rename map of old name -> new).

    A file that still has the single ``[labels.parcel]`` block keeps that
    shape when there is still exactly one box and it is still called
    ``default``. Adding or renaming switches it to ``[labels.parcels.<name>]``,
    which is what load_config already accepts.
    """
    raw = tomllib.loads(text)
    labels = raw.get("labels", {})
    if not isinstance(labels, dict):
        raise SettingsError("[labels] is not a table.")

    rename = {}
    for box in boxes:
        original = box.get("original") or ""
        if original and original != box["name"]:
            rename[original] = box["name"]
    if default_parcel in rename:
        default_parcel = rename[default_parcel]

    known = {box["name"] for box in boxes}
    if default_parcel and default_parcel not in known:
        raise SettingsError(
            f"Default box {default_parcel!r} is not one of the boxes on this page."
        )

    legacy_only = (
        isinstance(labels.get("parcel"), dict)
        and labels.get("parcel")
        and not (isinstance(labels.get("parcels"), dict) and labels.get("parcels"))
    )
    # One box for everything keeps the original [labels.parcel] block, so a
    # file written that way is not rewritten into the multi-box shape just
    # because someone opened this page and saved.
    if legacy_only and len(boxes) == 1 and boxes[0]["name"] == "default":
        text = _write_box(text, "labels.parcel", boxes[0], labels.get("parcel") or {})
        return text, rename

    existing: dict = {}
    if isinstance(labels.get("parcels"), dict):
        existing = dict(labels["parcels"])
    elif legacy_only:
        existing = {"default": labels.get("parcel") or {}}
    # The two shapes are mutually exclusive. Anything we save from here is
    # the multi-box shape, so the single block has to go.
    text = delete_table(text, "labels.parcel")

    for name in list(existing):
        if name not in known:
            # Legacy [labels.parcel] was already removed above; this drops a
            # renamed or deleted [labels.parcels.<name>] table.
            text = delete_table(text, f"labels.parcels.{name}")

    for box in boxes:
        source_name = box.get("original") or box["name"]
        previous = existing.get(source_name) or existing.get(box["name"]) or {}
        if not isinstance(previous, dict):
            previous = {}
        text = _write_box(text, f"labels.parcels.{box['name']}", box, previous)

    text = set_key(text, "labels", "default_parcel", default_parcel or None)
    text = _rename_item_parcels(text, rename)
    return text, rename


def _write_box(text: str, table: str, box: dict, previous: dict) -> str:
    for key in ("length_in", "width_in", "height_in", "packaging_oz"):
        text = set_key(text, table, key, box[key])
    text = set_key(text, table, "max_items", box["max_items"])
    if previous.get("default_item_oz") is not None:
        text = set_key(text, table, "default_item_oz", previous["default_item_oz"])
    for key in list(previous):
        if key not in _PARCEL_KEYS:
            text = set_key(text, table, key, None)
    return text


def _rename_item_parcels(text: str, rename: dict[str, str]) -> str:
    if not rename:
        return text
    raw = tomllib.loads(text)
    mapping = (raw.get("labels") or {}).get("item_parcels") or {}
    if not isinstance(mapping, dict):
        return text
    for sku, preset in mapping.items():
        if preset in rename:
            text = set_key(text, "labels.item_parcels", str(sku), rename[preset])
    return text


def sync_item_parcels(text: str, saved: dict[str, str], removed: set[str]) -> str:
    """Make ``[labels.item_parcels]`` agree with the products just saved.

    The CSV parcel column is what the Products tab writes. A TOML entry for
    the same SKU wins at load time, so leaving it stale would make the
    dropdown look like it hadn't saved. SKUs that were never in the TOML
    table stay CSV-only. A blank choice removes the TOML override so the
    default box applies.
    """
    raw = tomllib.loads(text)
    current = (raw.get("labels") or {}).get("item_parcels") or {}
    if not isinstance(current, dict) or not current:
        return text
    for sku in list(current):
        if sku in saved:
            parcel = saved[sku]
            if parcel:
                if str(current[sku]) != parcel:
                    text = set_key(text, "labels.item_parcels", str(sku), parcel)
            else:
                text = set_key(text, "labels.item_parcels", str(sku), None)
        elif sku in removed:
            text = set_key(text, "labels.item_parcels", str(sku), None)
    return text


def read_settings(raw: dict) -> dict:
    """The values the Config form displays. Missing keys use the same
    defaults load_config uses, so the form and the poller agree."""
    labels = raw.get("labels") or {}
    printer = raw.get("printer") or {}
    poll = raw.get("poll") or {}
    stock = raw.get("stock") or {}
    notify = raw.get("notify") or {}
    etsy = raw.get("etsy") or {}
    ship = labels.get("ship_from") or {}
    if not isinstance(ship, dict):
        ship = {}
    providers = labels.get("allowed_providers", ["USPS"])
    if not isinstance(providers, list):
        providers = ["USPS"]
    scopes = etsy.get("optional_scopes", list(DEFAULT_OPTIONAL_SCOPES))
    if isinstance(scopes, str):
        scopes = [scopes]
    return {
        "labels_enabled": bool(labels.get("enabled", False)),
        "allow_live": bool(labels.get("allow_live", False)),
        "file_type": str(labels.get("file_type", "PDF_4x6")),
        "allowed_providers": [str(p) for p in providers],
        "hold_unmapped_upgrade": bool(labels.get("hold_unmapped_upgrade", True)),
        "validate_addresses": bool(labels.get("validate_addresses", True)),
        "mark_shipped": str(labels.get("mark_shipped", "next_business_day")),
        "mark_shipped_hour": int(labels.get("mark_shipped_hour", 8) or 0),
        "default_parcel": default_parcel_from_labels(labels if isinstance(labels, dict) else {}),
        "items_csv": str(labels.get("items_csv") or ""),
        "ship_name": str(ship.get("name") or ""),
        "ship_street1": str(ship.get("street1") or ""),
        "ship_street2": str(ship.get("street2") or ""),
        "ship_city": str(ship.get("city") or ""),
        "ship_state": str(ship.get("state") or ""),
        "ship_zip": str(ship.get("zip") or ""),
        "ship_country": str(ship.get("country") or ""),
        "ship_email": str(ship.get("email") or ""),
        "ship_phone": str(ship.get("phone") or ""),
        "backend": str(printer.get("backend", "file")),
        "outbox": str(printer.get("outbox") or ""),
        "cups_queue": str(printer.get("cups_queue") or ""),
        "slip_format": str(printer.get("slip_format", "text")),
        "slip_queue": str(printer.get("slip_queue") or ""),
        "interval_seconds": int(poll.get("interval_seconds", 180) or 0),
        "label_delay_minutes": int(poll.get("label_delay_minutes", 0) or 0),
        "stock_enabled": bool(stock.get("enabled", True)),
        "low_threshold": int(stock.get("low_threshold", 2) or 0),
        "interval_minutes": int(stock.get("interval_minutes", 60) or 0),
        "notify_on_order": bool(notify.get("on_order", True)),
        "redirect_port": int(etsy.get("redirect_port", 8231) or 0),
        "listings_r": "listings_r" in {str(s) for s in scopes},
    }


def apply_settings(text: str, form) -> str:
    """Patch the settings the Config form owns. Raises SettingsError."""
    try:
        raw = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"config.toml is not valid TOML ({exc}) — fix it in the editor below.") from exc

    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    etsy = raw.get("etsy") if isinstance(raw.get("etsy"), dict) else {}

    file_type = _choice(_get(form, "file_type"), FILE_TYPES, _extra(labels, "file_type"), "Label format")
    backend = _choice(_get(form, "backend"), PRINTER_BACKENDS, None, "Printer")
    slip_format = _choice(_get(form, "slip_format"), SLIP_FORMATS, None, "Slip format")
    mark_shipped = _choice(_get(form, "mark_shipped"), POLICIES, None, "When Etsy is told")
    hour = _int(_get(form, "mark_shipped_hour"), 0, 23, "Ship-by hour")
    interval = _int(_get(form, "interval_seconds"), 1, 86400, "Poll interval")
    delay = _int(_get(form, "label_delay_minutes"), 0, 14 * 24 * 60, "Label delay")
    low = _int(_get(form, "low_threshold"), 0, 100000, "Low-stock threshold")
    stock_every = _int(_get(form, "interval_minutes"), 5, 10080, "Stock check interval")
    port = _int(_get(form, "redirect_port"), 1, 65535, "OAuth redirect port")

    providers = [p for p in _getlist(form, "allowed_providers") if p and p != "0"]
    known = set(COMMON_PROVIDERS)
    current_providers = labels.get("allowed_providers") if isinstance(labels.get("allowed_providers"), list) else []
    known.update(str(p) for p in current_providers)
    unknown = [p for p in providers if p not in known]
    if unknown:
        raise SettingsError(f"Unknown carrier: {unknown[0]!r}.")

    text = set_key(text, "labels", "enabled", _flag(form, "labels_enabled"))
    text = set_key(text, "labels", "allow_live", _flag(form, "allow_live"))
    text = set_key(text, "labels", "file_type", file_type)
    text = set_key(text, "labels", "allowed_providers", providers)
    text = set_key(text, "labels", "hold_unmapped_upgrade", _flag(form, "hold_unmapped_upgrade"))
    text = set_key(text, "labels", "validate_addresses", _flag(form, "validate_addresses"))
    text = set_key(text, "labels", "mark_shipped", mark_shipped)
    text = set_key(text, "labels", "mark_shipped_hour", hour)

    default_parcel = str(_get(form, "default_parcel", "")).strip()
    names = parcel_table_names(labels)
    if default_parcel and names and default_parcel not in names:
        raise SettingsError(
            f"Default box {default_parcel!r} is not a packaging type "
            f"({', '.join(names)})."
        )
    text = set_key(text, "labels", "default_parcel", default_parcel or None)
    items_csv = _one_line(_get(form, "items_csv"), "Products file")
    text = set_key(text, "labels", "items_csv", items_csv or None)

    ship = {
        "name": _one_line(_get(form, "ship_name"), "Ship-from name"),
        "street1": _one_line(_get(form, "ship_street1"), "Street"),
        "street2": _one_line(_get(form, "ship_street2"), "Street 2"),
        "city": _one_line(_get(form, "ship_city"), "City"),
        "state": _one_line(_get(form, "ship_state"), "State"),
        "zip": _one_line(_get(form, "ship_zip"), "ZIP"),
        "country": _one_line(_get(form, "ship_country"), "Country"),
        "email": _one_line(_get(form, "ship_email"), "Email"),
        "phone": _one_line(_get(form, "ship_phone"), "Phone"),
    }
    for key, value in ship.items():
        text = set_key(text, "labels.ship_from", key, value or None)

    text = set_key(text, "printer", "backend", backend)
    outbox = _one_line(_get(form, "outbox"), "Outbox")
    text = set_key(text, "printer", "outbox", outbox or None)
    cups = _one_line(_get(form, "cups_queue"), "Label printer queue")
    text = set_key(text, "printer", "cups_queue", cups or None)
    text = set_key(text, "printer", "slip_format", slip_format)
    slip_queue = _one_line(_get(form, "slip_queue"), "Slip printer queue")
    text = set_key(text, "printer", "slip_queue", slip_queue or None)

    text = set_key(text, "poll", "interval_seconds", interval)
    text = set_key(text, "poll", "label_delay_minutes", delay)

    text = set_key(text, "stock", "enabled", _flag(form, "stock_enabled"))
    text = set_key(text, "stock", "low_threshold", low)
    text = set_key(text, "stock", "interval_minutes", stock_every)

    text = set_key(text, "notify", "on_order", _flag(form, "notify_on_order"))
    text = set_key(text, "etsy", "redirect_port", port)

    scopes = etsy.get("optional_scopes", list(DEFAULT_OPTIONAL_SCOPES))
    if isinstance(scopes, str):
        scopes = [scopes]
    rest = [str(s) for s in scopes if str(s) != "listings_r"]
    new_scopes = (["listings_r"] if _flag(form, "listings_r") else []) + rest
    text = set_key(text, "etsy", "optional_scopes", new_scopes)
    return text


def provider_choices(current: list[str]) -> list[str]:
    choices = list(COMMON_PROVIDERS)
    for name in current:
        if name not in choices:
            choices.append(name)
    return choices


def choice_list(current: str, base: tuple[str, ...]) -> list[str]:
    values = list(base)
    if current and current not in values:
        values.append(current)
    return values


def _choice(value: str, allowed, extra: str | None, label: str) -> str:
    value = str(value or "").strip()
    options = set(allowed)
    if extra:
        options.add(extra)
    if value not in options:
        raise SettingsError(f"{label} must be one of: {', '.join(allowed)}.")
    return value


def _extra(section: dict, key: str) -> str | None:
    value = section.get(key) if isinstance(section, dict) else None
    return str(value) if value else None


def _positive(raw: str, label: str) -> float:
    number = _number(raw, label)
    if number <= 0:
        raise SettingsError(f"{label} must be greater than 0.")
    return number


def _nonnegative(raw: str, label: str) -> float:
    number = _number(raw, label)
    if number < 0:
        raise SettingsError(f"{label} cannot be negative.")
    return number


def _number(raw: str, label: str) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{label} must be a number.") from None


def _max_items(raw: str, name: str):
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        number = int(raw)
    except ValueError:
        raise SettingsError(f"{name} max items must be a whole number.") from None
    if number < 1:
        raise SettingsError(f"{name} max items must be at least 1, or blank for no limit.")
    return number


def _int(raw: str, low: int, high: int, label: str) -> int:
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{label} must be a whole number.") from None
    if number < low or number > high:
        raise SettingsError(f"{label} must be between {low} and {high}.")
    return number


def _one_line(value, label: str) -> str:
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        raise SettingsError(f"{label} must be a single line.")
    return text


def _flag(form, name: str) -> bool:
    return "1" in _getlist(form, name)


def _get(form, name: str, default: str = "") -> str:
    if hasattr(form, "get"):
        value = form.get(name, default)
    else:
        value = default
    if isinstance(value, (list, tuple)):
        return str(value[-1]) if value else default
    return "" if value is None else str(value)


def _getlist(form, name: str) -> list[str]:
    if hasattr(form, "getlist"):
        return [str(v) for v in form.getlist(name)]
    value = form.get(name, []) if hasattr(form, "get") else []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _field_number(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        # 8.0 from TOML is still 8.0 when it was written 8.0; int 8 stays 8.
        # Keep one decimal only when the parsed value isn't a whole number.
        return str(int(value))
    return str(value)
