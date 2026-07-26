"""Configuration loading.

All paths in config.toml are resolved relative to the directory containing
the config file, so the service behaves the same whether started from the
repo root or from systemd.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.toml"

# Scopes: transactions_w is not used until the label phase, but requesting it
# now means we won't need a second consent screen later.
OAUTH_SCOPES = "transactions_r transactions_w"


class ConfigError(Exception):
    pass


@dataclass
class LabelConfig:
    enabled: bool
    token: str
    allow_live: bool
    file_type: str
    ship_from: dict
    parcel: dict
    item_weights_oz: dict
    allowed_providers: list


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


def _load_labels(raw: dict) -> LabelConfig:
    section = raw.get("labels", {})
    enabled = bool(section.get("enabled", False))
    token = section.get("shippo_token", "")
    ship_from = section.get("ship_from", {})
    parcel = section.get("parcel", {})

    if enabled:
        if not token:
            raise ConfigError("labels.enabled is true but labels.shippo_token is not set")
        missing = [k for k in _SHIP_FROM_REQUIRED if not ship_from.get(k)]
        if missing:
            raise ConfigError(f"[labels.ship_from] is missing: {', '.join(missing)}")
        missing = [k for k in _PARCEL_REQUIRED if parcel.get(k) is None]
        if missing:
            raise ConfigError(f"[labels.parcel] is missing: {', '.join(missing)}")

    return LabelConfig(
        enabled=enabled,
        token=token,
        allow_live=bool(section.get("allow_live", False)),
        file_type=section.get("file_type", "PDF_4x6"),
        ship_from=ship_from,
        parcel=parcel,
        item_weights_oz=section.get("item_weights_oz", {}),
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
        labels=_load_labels(raw),
        ntfy_url=raw.get("notify", {}).get("ntfy_url") or None,
    )
