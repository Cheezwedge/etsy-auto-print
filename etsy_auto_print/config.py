"""Configuration loading.

All paths in config.toml are resolved relative to the directory containing
the config file, so the service behaves the same whether started from the
repo root or from systemd.
"""

from __future__ import annotations

import csv
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.toml"

# Scopes: transactions_w is not used until the label phase, but requesting it
# now means we won't need a second consent screen later. shops_r is required
# for the /users/me lookup used to auto-discover shop_id.
REQUIRED_SCOPES = "transactions_r transactions_w shops_r"

# listings_r is wanted, not required. It is the only way to see a listing that
# has gone sold_out — Etsy drops those from the public "active" endpoint
# entirely, so without it a sold-out listing is invisible rather than
# reported. Everything else works without it, so a token granted before this
# existed keeps working and stock monitoring degrades instead of failing.
#
# Etsy does not always grant it: a personal app with provisional access can
# come back with the scope simply absent. That is not something the shop can
# fix, so [etsy] optional_scopes = [] stops both asking for it and warning
# about its absence — a permanent amber row you cannot act on is worse than
# no row at all.
DEFAULT_OPTIONAL_SCOPES = ("listings_r",)

OPTIONAL_SCOPES = " ".join(DEFAULT_OPTIONAL_SCOPES)
OAUTH_SCOPES = f"{REQUIRED_SCOPES} {OPTIONAL_SCOPES}"


class ConfigError(Exception):
    pass


def normalize_service(name: str) -> str:
    """Etsy service names are free text typed into a shipping profile, so
    compare them case- and whitespace-insensitively."""
    return " ".join(name.lower().replace("-", " ").split())


# Etsy's shipping-service names -> Shippo service level tokens. These are the
# services Etsy offers for domestic and international USPS shipping; the map
# exists so that when a buyer pays for a specific one, we buy that exact
# service instead of whatever happens to be cheapest.
#
# Override or extend any of it with [labels.service_map] in config.toml — the
# names must match what your Etsy shipping profile calls them. Verify the
# tokens against your own account with: etsy-auto-print services
DEFAULT_SERVICE_MAP = {
    normalize_service(k): v
    for k, v in {
        "USPS Priority Mail Express": "usps_priority_express",
        "USPS Priority Mail": "usps_priority",
        # USPS folded First-Class Package Service into Ground Advantage in
        # 2023. "First-Class Mail" as a *parcel* service no longer exists, so
        # the honest equivalent for a package is Ground Advantage. (usps_first
        # still exists in Shippo, but only for letters and flats.)
        "USPS First-Class Mail": "usps_ground_advantage",
        "USPS Ground Advantage": "usps_ground_advantage",
        "USPS Priority Mail International": "usps_priority_mail_international",
        "USPS Priority Mail Express International": "usps_priority_mail_express_international",
        "Standard International": "usps_first_class_package_international_service",
    }.items()
}


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
    service_map: dict  # normalized Etsy service name -> Shippo servicelevel token
    hold_unmapped_upgrade: bool
    validate_addresses: bool

    @property
    def parcel(self) -> dict:
        """The single/primary box preset — for commands that just need *a*
        representative parcel (e.g. test-label) rather than per-order logic."""
        name = self.default_parcel or next(iter(self.parcels))
        return self.parcels[name]


@dataclass
class StockConfig:
    """Watching for listings that have sold out or are about to.

    A sold-out listing is silent: Etsy stops showing it, no order arrives,
    and nothing in the order pipeline can notice — the shop is just closed
    for that item until someone happens to look.
    """
    enabled: bool
    low_threshold: int
    interval_minutes: int


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
    slip_format: str
    db_path: Path
    tokens_path: Path
    labels: LabelConfig
    stock: StockConfig
    ntfy_url: str | None
    pushover_user_key: str | None
    pushover_api_token: str | None
    dashboard_password: str | None
    notify_on_order: bool
    optional_scopes: tuple

    @property
    def oauth_scopes(self) -> str:
        """What to ask Etsy for at the consent screen."""
        return " ".join((*REQUIRED_SCOPES.split(), *self.optional_scopes))

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
_PARCEL_OPTIONAL = ("max_items", "default_item_oz")

# Where a plausible-looking key actually belongs, for the error message. A
# misplaced key is silently ignored otherwise, and a parcel setting that does
# nothing is invisible until a label ships with the wrong weight on it.
_PARCEL_MISPLACED = {
    "weight_oz": "per-item weights go in the items CSV, or use default_item_oz "
                 "here to give every item in this box the same weight",
    "weight": "per-item weights go in the items CSV, or use default_item_oz here",
    "oz": "per-item weights go in the items CSV, or use default_item_oz here",
    "packaging_weight_oz": "did you mean packaging_oz?",
    "length": "did you mean length_in?",
    "width": "did you mean width_in?",
    "height": "did you mean height_in?",
    "max_item": "did you mean max_items?",
}


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
        # Deliberately an error rather than "no products": a typo'd path that
        # quietly loaded nothing would hold every order later with a confusing
        # "no weight configured" instead of naming the real problem.
        raise ConfigError(
            f"labels.items_csv points at {path}, which does not exist. Create it:\n"
            f"    printf 'sku,weight_oz,parcel,notes\\n' > {path}\n"
            "(the dashboard's Config tab creates it for you when you save)"
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
    if parcels_raw and section.get("parcel"):
        # Silently preferring one would be worse than refusing: the ignored
        # table is a box the shop believes it configured, and the symptom is
        # labels going out with another box's dimensions on them.
        raise ConfigError(
            "config.toml has both [labels.parcel] (one box for everything) and "
            f"[labels.parcels.*] ({', '.join(sorted(parcels_raw))}) — they are "
            "alternatives, and [labels.parcel] would be ignored entirely.\n"
            "Delete [labels.parcel], or move it in as another "
            "[labels.parcels.<name>] preset."
        )
    if parcels_raw:
        return dict(parcels_raw), dict(section.get("item_parcels", {})), section.get("default_parcel")
    return {"default": dict(section.get("parcel", {}))}, {}, "default"


def _load_stock(section: dict) -> StockConfig:
    threshold = int(section.get("low_threshold", 2))
    if threshold < 0:
        raise ConfigError("stock.low_threshold cannot be negative")
    minutes = int(section.get("interval_minutes", 60))
    if minutes < 5:
        # Listings change on the timescale of sales, not seconds, and Etsy
        # rate-limits. A tight loop here buys nothing and risks 429s on the
        # endpoints the order pipeline depends on.
        raise ConfigError("stock.interval_minutes must be at least 5")
    return StockConfig(
        enabled=bool(section.get("enabled", True)),
        low_threshold=threshold,
        interval_minutes=minutes,
    )


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
            where = (
                "[labels.parcel]"
                if name == "default" and "parcels" not in section
                else f"[labels.parcels.{name}]"
            )
            missing = [k for k in _PARCEL_REQUIRED if box.get(k) is None]
            if missing:
                raise ConfigError(f"{where} is missing: {', '.join(missing)}")
            for key in box:
                if key in _PARCEL_REQUIRED or key in _PARCEL_OPTIONAL:
                    continue
                hint = _PARCEL_MISPLACED.get(key)
                raise ConfigError(
                    f"{where} has an unrecognized setting {key!r}"
                    + (f" — {hint}" if hint else "")
                    + f"\nValid keys here: "
                    f"{', '.join((*_PARCEL_REQUIRED, *_PARCEL_OPTIONAL))}"
                )
        if default_parcel and default_parcel not in parcels:
            raise ConfigError(
                f"labels.default_parcel = {default_parcel!r} but no such preset in "
                f"[labels.parcels] (have: {', '.join(parcels)})"
            )
        # Per-SKU box names, checked here rather than at label time. Renaming
        # a preset leaves every row pointing at a box that no longer exists,
        # and finding that out one held order at a time — with a buyer
        # waiting on each — is the worst possible moment.
        stale = sorted({
            (sku, name) for sku, name in item_parcels.items() if name not in parcels
        })
        if stale:
            where = f"the items CSV ({section['items_csv']})" if section.get(
                "items_csv") else "[labels.item_parcels]"
            listed = "\n".join(f"  {sku} -> {name}" for sku, name in stale[:10])
            more = f"\n  ...and {len(stale) - 10} more" if len(stale) > 10 else ""
            raise ConfigError(
                f"{len(stale)} product(s) name a box that isn't configured:\n"
                f"{listed}{more}\n"
                f"Configured boxes: {', '.join(sorted(parcels))}\n"
                f"Fix the parcel column in {where} — leave it blank to use "
                f"labels.default_parcel"
                + (f" ({default_parcel!r})" if default_parcel else "")
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
        service_map={
            **DEFAULT_SERVICE_MAP,
            **{normalize_service(k): v for k, v in section.get("service_map", {}).items()},
        },
        hold_unmapped_upgrade=bool(section.get("hold_unmapped_upgrade", True)),
        validate_addresses=bool(section.get("validate_addresses", True)),
    )


def installed_config_path() -> Path:
    """Where config.toml lives for an editable install of this checkout.

    `ssh host 'etsy-auto-print ...'` runs in the home directory, not the
    project, so a bare relative config.toml isn't there. Falling back to the
    checkout means one-liners over SSH work; it's announced rather than
    silent, so it can't quietly pick up a config you didn't mean.
    """
    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
    if not cfg_path.exists() and path is None:
        fallback = installed_config_path()
        if fallback.exists():
            print(f"Using {fallback}", file=sys.stderr)
            cfg_path = fallback
    if not cfg_path.exists():
        looked = Path.cwd()
        raise ConfigError(
            f"Config file not found: {cfg_path} (looked in {looked}).\n"
            f"If you meant the one in your project, either cd there first or "
            f"pass -c /path/to/config.toml.\n"
            "Starting from scratch? Copy config.example.toml to config.toml "
            "and fill in your keystring."
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

    slip_format = str(printer.get("slip_format", "text")).lower()
    if slip_format not in ("text", "zpl"):
        raise ConfigError(
            f"printer.slip_format must be 'text' or 'zpl', not {slip_format!r}"
        )
    if slip_format == "zpl" and printer.get("slip_queue"):
        raise ConfigError(
            "printer.slip_format = 'zpl' prints slips on the label printer, so "
            "printer.slip_queue must not be set — remove one of the two."
        )

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
        slip_format=slip_format,
        db_path=base / paths.get("db", "orders.db"),
        tokens_path=base / paths.get("tokens", "tokens.json"),
        labels=_load_labels(raw, base),
        stock=_load_stock(raw.get("stock", {})),
        ntfy_url=raw.get("notify", {}).get("ntfy_url") or None,
        pushover_user_key=raw.get("notify", {}).get("pushover_user_key") or None,
        pushover_api_token=raw.get("notify", {}).get("pushover_api_token") or None,
        dashboard_password=raw.get("dashboard", {}).get("password") or None,
        notify_on_order=bool(raw.get("notify", {}).get("on_order", True)),
        optional_scopes=tuple(
            etsy.get("optional_scopes", DEFAULT_OPTIONAL_SCOPES)
        ),
    )
