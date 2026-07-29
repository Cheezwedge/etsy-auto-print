"""Configuration loading.

All paths in config.toml are resolved relative to the directory containing
the config file, so the service behaves the same whether started from the
repo root or from systemd.
"""

from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.toml"

# Scopes: transactions_w is not used until the label phase, but requesting it
# now means we won't need a second consent screen later. shops_r is required
# for the /users/me lookup used to auto-discover shop_id.
OAUTH_SCOPES = "transactions_r transactions_w shops_r"


class ConfigError(Exception):
    pass


@dataclass
class LabelConfig:
    enabled: bool
    token: str
    allow_live: bool
    file_type: str
    ship_from: dict
    parcels: dict  # preset name -> box dims/packaging_oz
    item_parcels: dict  # SKU -> preset name
    default_parcel: str | None
    item_weights_oz: dict
    allowed_providers: list

    @property
    def parcel(self) -> dict:
        """The single/primary box preset — for commands that just need *a*
        representative parcel (e.g. test-label) rather than per-order logic."""
        name = self.default_parcel or next(iter(self.parcels))
        return self.parcels[name]


@dataclass
class Config:
    keystring: str
    shared_secret: str
    shop_id: int | None
    redirect_port: int
    poll_interval: int
    printer_backend: str
    outbox: Path
    cups_queue: str | None
    slip_queue: str | None
    db_path: Path
    tokens_path: Path
    labels: LabelConfig
    ntfy_url: str | None
    pushover_user_key: str | None
    pushover_api_token: str | None

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"

    @property
    def api_key(self) -> str:
        # Etsy requires "keystring:shared_secret" in x-api-key as of Feb 9 2026
        # (previously the keystring alone was sufficient).
        return f"{self.keystring}:{self.shared_secret}"


# email is on the required list because USPS (the default carrier) rejects
# label purchases when the sender address has no contact email.
_SHIP_FROM_REQUIRED = ("name", "street1", "city", "state", "zip", "country", "email")
_PARCEL_REQUIRED = ("length_in", "width_in", "height_in", "packaging_oz")


CSV_SKU_COLUMNS = ("sku", "SKU", "Sku")
CSV_WEIGHT_COLUMNS = ("weight_oz", "weight", "oz", "Weight (oz)", "weight (oz)")
CSV_PARCEL_COLUMNS = ("parcel", "box", "parcel_preset", "Box", "Parcel")


def _pick_column(row: dict, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if name in row:
            return name
    # last resort: case-insensitive match
    lowered = {k.lower().strip(): k for k in row if k}
    for name in candidates:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _load_items_csv(path: Path) -> tuple[dict, dict]:
    """Read a spreadsheet of per-SKU shipping data.

    Expected columns (header row required, extra columns ignored so the
    same sheet can carry notes/prices/etc.):
        sku       — must match the SKU set on the Etsy listing
        weight_oz — item weight in ounces
        parcel    — optional: which [labels.parcels.<name>] preset to use

    Returns (weights_by_sku, parcels_by_sku).
    """
    if not path.exists():
        raise ConfigError(
            f"labels.items_csv points at {path}, which does not exist. "
            "Create it with a header row: sku,weight_oz,parcel"
        )

    weights: dict[str, float] = {}
    parcels: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ConfigError(f"{path} is empty — it needs a header row: sku,weight_oz,parcel")

        sku_col = _pick_column({k: 1 for k in reader.fieldnames}, CSV_SKU_COLUMNS)
        weight_col = _pick_column({k: 1 for k in reader.fieldnames}, CSV_WEIGHT_COLUMNS)
        parcel_col = _pick_column({k: 1 for k in reader.fieldnames}, CSV_PARCEL_COLUMNS)
        if not sku_col:
            raise ConfigError(
                f"{path} has no 'sku' column (found: {', '.join(reader.fieldnames)})"
            )

        for lineno, row in enumerate(reader, start=2):
            sku = (row.get(sku_col) or "").strip()
            if not sku or sku.startswith("#"):
                continue  # blank row or comment

            if weight_col:
                raw = (row.get(weight_col) or "").strip()
                if raw:
                    try:
                        weights[sku] = float(raw)
                    except ValueError:
                        raise ConfigError(
                            f"{path} line {lineno}: weight {raw!r} for SKU {sku!r} is not a number"
                        ) from None
            if parcel_col:
                preset = (row.get(parcel_col) or "").strip()
                if preset:
                    parcels[sku] = preset

    return weights, parcels


def _load_parcels(section: dict) -> tuple[dict, dict, str | None]:
    """Normalizes the config's box setup to (parcels, item_parcels, default_parcel).

    Two supported shapes:
    - New: [labels.parcels.<name>] tables (one per box size) + optional
      [labels.item_parcels] (SKU -> preset name) + labels.default_parcel.
    - Old: a single [labels.parcel] table, used for every order regardless
      of contents. Normalized into one preset named "default" so the rest
      of the code only has to handle the multi-preset shape.
    """
    parcels_raw = section.get("parcels")
    if parcels_raw:
        return dict(parcels_raw), dict(section.get("item_parcels", {})), section.get("default_parcel")
    return {"default": dict(section.get("parcel", {}))}, {}, "default"


def _load_labels(raw: dict, base: Path) -> LabelConfig:
    section = raw.get("labels", {})
    enabled = bool(section.get("enabled", False))
    token = section.get("shippo_token", "")
    ship_from = section.get("ship_from", {})
    parcels, item_parcels, default_parcel = _load_parcels(section)

    # Per-SKU data can come from a spreadsheet, from TOML tables, or both.
    # TOML wins on conflict so a one-off override doesn't need a CSV edit.
    item_weights: dict = {}
    if section.get("items_csv"):
        csv_weights, csv_parcels = _load_items_csv(base / section["items_csv"])
        item_weights.update(csv_weights)
        item_parcels = {**csv_parcels, **item_parcels}
    item_weights.update(section.get("item_weights_oz", {}))

    if enabled:
        if not token:
            raise ConfigError("labels.enabled is true but labels.shippo_token is not set")
        missing = [k for k in _SHIP_FROM_REQUIRED if not ship_from.get(k)]
        if missing:
            raise ConfigError(f"[labels.ship_from] is missing: {', '.join(missing)}")
        if not any(parcels.values()):
            raise ConfigError(
                "No parcel/box size configured — set [labels.parcel] (single box) "
                "or [labels.parcels.<name>] (multiple box sizes)."
            )
        for name, box in parcels.items():
            missing = [k for k in _PARCEL_REQUIRED if box.get(k) is None]
            if missing:
                where = "[labels.parcel]" if name == "default" and "parcels" not in section else f"[labels.parcels.{name}]"
                raise ConfigError(f"{where} is missing: {', '.join(missing)}")
        if default_parcel and default_parcel not in parcels:
            raise ConfigError(
                f"labels.default_parcel = {default_parcel!r} but no such preset in "
                f"[labels.parcels] (have: {', '.join(parcels)})"
            )

    return LabelConfig(
        enabled=enabled,
        token=token,
        allow_live=bool(section.get("allow_live", False)),
        file_type=section.get("file_type", "PDF_4x6"),
        ship_from=ship_from,
        parcels=parcels,
        item_parcels=item_parcels,
        default_parcel=default_parcel,
        item_weights_oz=item_weights,
        allowed_providers=list(section.get("allowed_providers", ["USPS"])),
    )


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
    if not cfg_path.exists():
        raise ConfigError(
            f"Config file not found: {cfg_path}. "
            "Copy config.example.toml to config.toml and fill in your keystring."
        )
    with open(cfg_path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"{cfg_path} is not valid TOML: {exc}. "
                "Common cause: a string value without quotes "
                '(e.g. email = "you@example.com" needs the quotes).'
            ) from exc

    base = cfg_path.resolve().parent
    etsy = raw.get("etsy", {})
    poll = raw.get("poll", {})
    printer = raw.get("printer", {})
    paths = raw.get("paths", {})

    keystring = etsy.get("keystring", "")
    if not keystring or keystring == "your-etsy-app-keystring":
        raise ConfigError("Set etsy.keystring in config.toml to your Etsy app keystring.")

    shared_secret = etsy.get("shared_secret", "")
    if not shared_secret or shared_secret == "your-etsy-app-shared-secret":
        raise ConfigError(
            "Set etsy.shared_secret in config.toml to your Etsy app's shared secret "
            "(from the same Your Apps page as the keystring). Etsy now rejects API "
            "requests that don't include it."
        )

    backend = printer.get("backend", "file")
    if backend not in ("file", "cups"):
        raise ConfigError(f"printer.backend must be 'file' or 'cups', got {backend!r}")
    cups_queue = printer.get("cups_queue")
    if backend == "cups" and not cups_queue:
        raise ConfigError("printer.cups_queue is required when printer.backend = 'cups'")

    return Config(
        keystring=keystring,
        shared_secret=shared_secret,
        shop_id=etsy.get("shop_id"),
        redirect_port=int(etsy.get("redirect_port", 8231)),
        poll_interval=int(poll.get("interval_seconds", 180)),
        printer_backend=backend,
        outbox=base / printer.get("outbox", "outbox"),
        cups_queue=cups_queue,
        slip_queue=printer.get("slip_queue"),
        db_path=base / paths.get("db", "orders.db"),
        tokens_path=base / paths.get("tokens", "tokens.json"),
        labels=_load_labels(raw, base),
        ntfy_url=raw.get("notify", {}).get("ntfy_url") or None,
        pushover_user_key=raw.get("notify", {}).get("pushover_user_key") or None,
        pushover_api_token=raw.get("notify", {}).get("pushover_api_token") or None,
    )
