"""Health checks for every moving part of the system.

Each check returns a Check: an ok/warn/fail state plus a human explanation
and, where useful, a hint about how to fix it. Checks never raise — a
broken dependency should render as a red box in the dashboard, not a
stack trace that takes the whole page down with it.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

import requests

from .auth import TokenStore
from .config import OPTIONAL_SCOPES, REQUIRED_SCOPES, Config
from . import stock
from .etsy import EtsyClient, ping
from .store import Store

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    state: str
    detail: str
    hint: str = ""
    facts: dict = field(default_factory=dict)


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).decode(errors="replace").strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def check_service(unit: str = "etsy-auto-print") -> Check:
    if not shutil.which("systemctl"):
        return Check("Background service", WARN, "systemctl not available on this host")
    code, out = _run(["systemctl", "is-active", unit])
    if out == "active":
        _, since = _run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit])
        return Check("Background service", OK, f"running (since {since or 'unknown'})")
    if out == "inactive":
        return Check(
            "Background service", FAIL, "not running — no orders are being processed",
            f"sudo systemctl start {unit}",
        )
    return Check("Background service", FAIL, f"state: {out or 'unknown'}",
                 f"journalctl -u {unit} -n 50")


def check_etsy_api(config: Config) -> Check:
    """Is Etsy reachable and are the app credentials good?

    This endpoint needs no OAuth token, so it tells "Etsy is down" and "the
    keystring/shared secret is wrong" apart from "our token went bad" — which
    otherwise all look like the same failed poll.
    """
    try:
        resp = ping(config.api_key)
    except requests.RequestException as exc:
        return Check("Etsy API reachable", FAIL, f"unreachable: {exc}"[:300],
                     "Check this machine's network connection")
    if resp.status_code == 200:
        return Check("Etsy API reachable", OK, "openapi-ping responded")
    if resp.status_code in (401, 403):
        return Check(
            "Etsy API reachable", FAIL,
            f"app credentials rejected ({resp.status_code}): {resp.text[:150]}",
            "Check etsy.keystring and etsy.shared_secret — this call uses no token",
        )
    return Check("Etsy API reachable", FAIL,
                 f"HTTP {resp.status_code}: {resp.text[:150]}",
                 "Etsy may be having an outage; check status.etsy.com")


def check_etsy_scopes(config: Config) -> Check:
    """Does the stored token still carry every permission the program needs?

    Etsy grants scopes at authorization time, so adding a feature that needs a
    new scope silently breaks an old token. Better a red row here than a 403
    on a live order.
    """
    required = set(REQUIRED_SCOPES.split())
    optional = set(OPTIONAL_SCOPES.split())
    tokens = TokenStore(config)
    if not tokens.authorized:
        # The row above already reports this as a failure; don't double-count it.
        return Check("Etsy permissions", WARN, "not authorized yet — nothing to check",
                     f"Will need: {' '.join(sorted(required))}")
    try:
        granted = set(EtsyClient(config, tokens).token_scopes())
    except Exception as exc:
        return Check("Etsy permissions", WARN,
                     f"could not read the token's scopes: {exc}"[:300])
    if not granted:
        return Check("Etsy permissions", WARN,
                     "Etsy returned no scope list for this token",
                     f"Expected: {' '.join(sorted(required))}")
    missing = sorted(required - granted)
    if missing:
        return Check(
            "Etsy permissions", FAIL, f"token is missing: {', '.join(missing)}",
            "Delete tokens.json and re-run: etsy-auto-print auth",
            facts={"granted": " ".join(sorted(granted))},
        )
    # Optional scopes cost a feature, not the pipeline. A token issued before
    # the feature existed must not turn the whole dashboard red.
    absent = sorted(optional - granted)
    if absent:
        return Check(
            "Etsy permissions", WARN,
            f"{' '.join(sorted(granted))} (no {', '.join(absent)})",
            "Re-run `etsy-auto-print auth` to add it — without listings_r, a "
            "sold-out listing can only be guessed at, not read",
        )
    return Check("Etsy permissions", OK, " ".join(sorted(granted)))


def check_etsy(config: Config) -> Check:
    tokens = TokenStore(config)
    if not tokens.authorized:
        return Check("Etsy connection", FAIL, "not authorized",
                     "etsy-auto-print auth  (see docs/RASPBERRY_PI.md for headless)")
    try:
        client = EtsyClient(config, tokens)
        me = client.get_me()
        return Check(
            "Etsy connection", OK, f"connected as user {me.get('user_id')}",
            facts={"shop_id": client.shop_id},
        )
    except Exception as exc:  # any failure is a red box, never a crash
        msg = str(exc)
        hint = ""
        if "scope" in msg:
            hint = "Scope changed — re-run: etsy-auto-print auth"
        elif "x-api-key" in msg or "shared secret" in msg.lower():
            hint = "Check etsy.keystring and etsy.shared_secret in config"
        return Check("Etsy connection", FAIL, msg[:300], hint)


def check_shippo(config: Config) -> Check:
    labels = config.labels
    if not labels.enabled:
        return Check("Shippo (labels)", WARN, "labels are disabled in config",
                     "Set labels.enabled = true to buy shipping labels")
    if not labels.token:
        return Check("Shippo (labels)", FAIL, "no API token configured")

    mode = "TEST" if labels.token.startswith("shippo_test_") else "LIVE"
    if mode == "LIVE" and not labels.allow_live:
        return Check("Shippo (labels)", FAIL,
                     "live token present but labels.allow_live is false",
                     "Set allow_live = true to permit real postage")
    try:
        resp = requests.get(
            "https://api.goshippo.com/addresses/?results=1",
            headers={"Authorization": f"ShippoToken {labels.token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            return Check("Shippo (labels)", FAIL, "token rejected (401)",
                         "Check labels.shippo_token")
        if resp.status_code >= 400:
            return Check("Shippo (labels)", FAIL, f"API error {resp.status_code}")
        state = OK if mode == "TEST" else WARN
        detail = (
            f"{mode} mode — labels are fake and free" if mode == "TEST"
            else f"{mode} mode — labels cost real money"
        )
        return Check("Shippo (labels)", state, detail, facts={"mode": mode})
    except requests.RequestException as exc:
        return Check("Shippo (labels)", FAIL, f"unreachable: {exc}"[:300])


# Values straight out of config.example.toml. Shipping with these means real
# parcels carry a return address that doesn't exist.
_PLACEHOLDER_SHIP_FROM = {
    "your shop name", "123 your street", "your city", "you@example.com",
    "your name", "123 main st",
}


def check_ship_from(config: Config) -> Check:
    """Is the return address real?

    Nothing downstream can tell: carriers accept a well-formed address they
    can find, and the example one is a findable street. It only shows up when
    an undeliverable parcel has nowhere to come back to.
    """
    labels = config.labels
    if not labels.enabled:
        return Check("Return address", WARN, "labels are disabled")
    left = [
        f"{key} = {value!r}"
        for key, value in labels.ship_from.items()
        if str(value).strip().lower() in _PLACEHOLDER_SHIP_FROM
    ]
    summary = ", ".join(
        str(labels.ship_from.get(k, "")) for k in ("name", "city", "state") if labels.ship_from.get(k)
    )
    if left:
        return Check(
            "Return address", FAIL,
            f"still the example values: {'; '.join(left)}",
            "Fix [labels.ship_from] before going live — undeliverable parcels "
            "have nowhere to return to",
        )
    return Check("Return address", OK, summary or "set")


def check_printer(config: Config) -> Check:
    if config.printer_backend == "file":
        return Check("Printer", WARN, f"file mode — output goes to {config.outbox}",
                     'Set printer.backend = "cups" once a printer is attached')
    queue = config.cups_queue
    code, out = _run(["lpstat", "-p", queue])
    if code != 0:
        return Check("Printer", FAIL, out[:300] or f"queue {queue!r} not found",
                     f"lpadmin -p {queue} -E -v usb://... -m raw")
    lowered = out.lower()
    if "disabled" in lowered:
        return Check("Printer", FAIL, out.splitlines()[0][:200], f"cupsenable {queue}")
    # A queue can be enabled but the printer unplugged; surface pending jobs.
    # lpq ships separately from the CUPS daemon and isn't always installed —
    # its absence says nothing about the queue, so it must not read as a
    # backlog. A check that cries wolf is worse than one that stays quiet.
    code, jobs = _run(["lpq", "-P", queue])
    lines = [ln for ln in jobs.splitlines() if ln.strip()]
    pending = ""
    if code == 0 and lines and "no entries" not in jobs.lower():
        pending = lines[-1][:120]
    detail = out.splitlines()[0][:200]
    if pending:
        return Check("Printer", WARN, f"{detail} — jobs waiting: {pending}",
                     "Printer may be offline or out of labels")
    return Check("Printer", OK, detail)


def check_notifications(config: Config) -> Check:
    channels = []
    if config.ntfy_url:
        channels.append("ntfy")
    if config.pushover_user_key and config.pushover_api_token:
        channels.append("Pushover")
    if not channels:
        return Check("Notifications", WARN, "no channel configured — alerts only go to the log",
                     "Set notify.ntfy_url or notify.pushover_* in config")
    return Check("Notifications", OK, ", ".join(channels))


def check_stock(config: Config) -> Check:
    """Listings that have sold out or are close to it.

    Reads the last stock sweep from the database rather than calling Etsy —
    this runs on every dashboard load, and a sold-out listing does not
    become un-sold-out in the seconds between them.
    """
    if not config.stock.enabled:
        return Check("Listing stock", WARN, "stock monitoring is disabled",
                     "Set enabled = true under [stock] to be told when a "
                     "listing sells out")
    try:
        state = Store(config.db_path).stock_state()
    except Exception as exc:
        return Check("Listing stock", WARN, f"could not read: {exc}"[:200])
    if not state:
        return Check("Listing stock", WARN, "not checked yet",
                     "The poller sweeps listings hourly; run `etsy-auto-print "
                     "listings` to check now")

    out, low = stock.summarize(state)
    names = [title for level, title, _ in state.values() if level == stock.OUT]
    if out:
        return Check(
            "Listing stock", FAIL,
            f"{out} listing(s) out of stock" + (f", {low} low" if low else ""),
            "Nobody can buy these. Raise the quantity on Etsy.",
            facts={"out of stock": "; ".join(n[:40] for n in names[:3])},
        )
    if low:
        return Check("Listing stock", WARN, f"{low} listing(s) low on stock",
                     "Raise the quantity on Etsy before they sell out")
    return Check("Listing stock", OK, f"{len(state)} listing(s) in stock")


def run_all(config: Config) -> list[Check]:
    return [
        check_service(),
        check_etsy_api(config),
        check_etsy(config),
        check_etsy_scopes(config),
        check_shippo(config),
        check_ship_from(config),
        check_printer(config),
        check_stock(config),
        check_notifications(config),
    ]
