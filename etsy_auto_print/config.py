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
class Config:
    keystring: str
    shop_id: int | None
    redirect_port: int
    poll_interval: int
    printer_backend: str
    outbox: Path
    cups_queue: str | None
    db_path: Path
    tokens_path: Path

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
    if not cfg_path.exists():
        raise ConfigError(
            f"Config file not found: {cfg_path}. "
            "Copy config.example.toml to config.toml and fill in your keystring."
        )
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)

    base = cfg_path.resolve().parent
    etsy = raw.get("etsy", {})
    poll = raw.get("poll", {})
    printer = raw.get("printer", {})
    paths = raw.get("paths", {})

    keystring = etsy.get("keystring", "")
    if not keystring or keystring == "your-etsy-app-keystring":
        raise ConfigError("Set etsy.keystring in config.toml to your Etsy app keystring.")

    backend = printer.get("backend", "file")
    if backend not in ("file", "cups"):
        raise ConfigError(f"printer.backend must be 'file' or 'cups', got {backend!r}")
    cups_queue = printer.get("cups_queue")
    if backend == "cups" and not cups_queue:
        raise ConfigError("printer.cups_queue is required when printer.backend = 'cups'")

    return Config(
        keystring=keystring,
        shop_id=etsy.get("shop_id"),
        redirect_port=int(etsy.get("redirect_port", 8231)),
        poll_interval=int(poll.get("interval_seconds", 180)),
        printer_backend=backend,
        outbox=base / printer.get("outbox", "outbox"),
        cups_queue=cups_queue,
        db_path=base / paths.get("db", "orders.db"),
        tokens_path=base / paths.get("tokens", "tokens.json"),
    )
