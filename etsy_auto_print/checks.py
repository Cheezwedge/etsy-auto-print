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
from .config import Config
from .etsy import EtsyClient

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
    _, jobs = _run(["lpq", "-P", queue])
    pending = "" if "no entries" in jobs.lower() else jobs.splitlines()[-1][:120]
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


def run_all(config: Config) -> list[Check]:
    return [
        check_service(),
        check_etsy(config),
        check_shippo(config),
        check_printer(config),
        check_notifications(config),
    ]
