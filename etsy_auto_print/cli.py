"""Command-line interface.

    etsy-auto-print auth              one-time browser OAuth consent
    etsy-auto-print poll              single poll pass
    etsy-auto-print run               poll forever (systemd target)
    etsy-auto-print status            show every order and its state
    etsy-auto-print check             health-check every connection
    etsy-auto-print show ID           full detail + event history for one order
    etsy-auto-print reprint ID        re-render + re-print a slip (un-holds)
    etsy-auto-print test-slip         print a sample slip with fake data
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from datetime import datetime

from . import checks
from .auth import AuthError, TokenStore, authorize
from .config import Config, ConfigError, load_config
from .etsy import EtsyApiError, EtsyClient
from .labels import LabelError, Labeler, build_address_to, compute_parcel, pick_rate
from .notify import Notifier
from .pipeline import advance_order, poll_once, reprint
from .printer import get_printer
from .shippo import ShippoError, make_client
from .slip import render_packing_slip
from .store import Store

SAMPLE_RECEIPT = {
    "receipt_id": 999999999,
    "name": "Jane Sample",
    "first_line": "123 Example Street",
    "second_line": "Apt 4B",
    "city": "Portland",
    "state": "OR",
    "zip": "97201",
    "country_iso": "US",
    "created_timestamp": int(time.time()),
    "is_gift": True,
    "gift_message": "Happy birthday!",
    "message_from_buyer": "Please ship after the 20th.",
    "grandtotal": {"amount": 3450, "divisor": 100, "currency_code": "USD"},
    "transactions": [
        {
            "title": "Hand-thrown ceramic mug",
            "quantity": 2,
            "sku": "MUG-BLUE-12OZ",
            "variations": [{"formatted_name": "Color", "formatted_value": "Ocean Blue"}],
        }
    ],
}


def _build_client(config) -> EtsyClient:
    tokens = TokenStore(config)
    if not tokens.authorized:
        raise AuthError("Not authorized yet — run: etsy-auto-print auth")
    return EtsyClient(config, tokens)


def _build_labeler(config: Config, store: Store, printer) -> Labeler | None:
    if not config.labels.enabled:
        return None
    client = make_client(config.labels.token, config.labels.allow_live)
    if client.is_test:
        logging.info("Shippo TEST mode: labels are fake and free")
    return Labeler(config.labels, store, printer, client)


def _build_notifier(config: Config) -> Notifier:
    return Notifier(config.ntfy_url, config.pushover_user_key, config.pushover_api_token)


def cmd_auth(config, args) -> int:
    authorize(config, open_browser=not args.no_browser)
    client = _build_client(config)
    me = client.get_me()
    print(f"Connected: user {me.get('user_id')}, shop {client.shop_id}")
    return 0


def cmd_poll(config, args) -> int:
    client = _build_client(config)
    store = Store(config.db_path)
    printer = get_printer(config)
    labeler = _build_labeler(config, store, printer)
    n = poll_once(client, store, printer, labeler, _build_notifier(config))
    print(f"{n} order(s) made progress.")
    return 0


def cmd_run(config, args) -> int:
    client = _build_client(config)
    store = Store(config.db_path)
    printer = get_printer(config)
    labeler = _build_labeler(config, store, printer)
    notifier = _build_notifier(config)
    notify_channels = ", ".join(
        c for c in (
            "ntfy" if config.ntfy_url else None,
            "Pushover" if config.pushover_user_key and config.pushover_api_token else None,
        ) if c
    ) or "log only"
    logging.info(
        "Polling every %ss (printer: %s, labels: %s, notify: %s). Ctrl-C to stop.",
        config.poll_interval,
        config.printer_backend,
        "on" if labeler else "off",
        notify_channels,
    )
    while True:
        try:
            poll_once(client, store, printer, labeler, notifier)
        except (EtsyApiError, AuthError) as exc:
            # Transient API failures shouldn't kill the service; the next
            # poll retries and nothing is lost (state lives in SQLite).
            logging.error("Poll failed: %s", exc)
        time.sleep(config.poll_interval)


def cmd_status(config, args) -> int:
    store = Store(config.db_path)
    orders = store.all_orders()
    if not orders:
        print("No orders recorded yet.")
        return 0
    print(f"{'ORDER':<14}{'STATE':<18}{'BUYER':<24}{'UPDATED':<18}NOTE")
    for row in orders:
        updated = datetime.fromtimestamp(row["updated_at"]).strftime("%m-%d %H:%M")
        note = row["error"] or ""
        print(
            f"{row['receipt_id']:<14}{row['state']:<18}"
            f"{(row['buyer_name'] or '?')[:22]:<24}{updated:<18}{note}"
        )
    held = store.in_state("held")
    if held:
        print(f"\n{len(held)} order(s) HELD and need attention (see 'show', then 'reprint').")
    return 0


_CHECK_MARK = {checks.OK: "ok  ", checks.WARN: "warn", checks.FAIL: "FAIL"}


def cmd_check(config, args) -> int:
    """Run every health check — the dashboard's Status page, over SSH.

    Exit code 1 if anything failed, so it can drive a shortcut or a cron
    alert without parsing the output.
    """
    results = checks.run_all(config)
    for check in results:
        print(f"[{_CHECK_MARK[check.state]}] {check.name}: {check.detail}")
        for key, value in check.facts.items():
            print(f"           {key}: {value}")
        if check.hint and check.state != checks.OK:
            print(f"           -> {check.hint}")
    failed = [c.name for c in results if c.state == checks.FAIL]
    if failed:
        print(f"\n{len(failed)} check(s) failing: {', '.join(failed)}")
        return 1
    return 0


def cmd_show(config, args) -> int:
    store = Store(config.db_path)
    row = store.get(args.receipt_id)
    if row is None:
        print(f"Order #{args.receipt_id} not found.", file=sys.stderr)
        return 1
    print(f"Order #{row['receipt_id']}  state={row['state']}  buyer={row['buyer_name']}")
    if row["error"]:
        print(f"Held because: {row['error']}")
    label = store.get_label(args.receipt_id)
    if label:
        test = " [TEST]" if label["is_test"] else ""
        print(
            f"Label: {label['carrier']} {label['service']} "
            f"{label['amount']} {label['currency']}{test}\n"
            f"Tracking: {label['tracking_number']}  {label['tracking_url']}"
        )
    print("\nHistory:")
    for ev in store.events(args.receipt_id):
        at = datetime.fromtimestamp(ev["at"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {at}  {ev['from_state'] or '-':>14} -> {ev['to_state']:<14} {ev['note']}")
    receipt = store.get_receipt_json(args.receipt_id)
    if receipt:
        print("\n" + render_packing_slip(receipt))
    return 0


def cmd_reprint(config, args) -> int:
    store = Store(config.db_path)
    printer = get_printer(config)
    try:
        destination = reprint(args.receipt_id, store, printer)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Reprinted -> {destination}")
    return 0


def cmd_test_slip(config, args) -> int:
    printer = get_printer(config)
    destination = printer.print_text("packing-slip-SAMPLE", render_packing_slip(SAMPLE_RECEIPT))
    print(f"Sample slip -> {destination}")
    return 0


def cmd_test_notify(config, args) -> int:
    if not config.ntfy_url and not (config.pushover_user_key and config.pushover_api_token):
        print(
            "No notification channel is configured — set notify.ntfy_url and/or "
            "notify.pushover_user_key + notify.pushover_api_token in config.toml.",
            file=sys.stderr,
        )
        return 1
    _build_notifier(config).send("etsy-auto-print test", "Notifications are working. This is a test.")
    channels = ", ".join(
        c for c in (
            "ntfy" if config.ntfy_url else None,
            "Pushover" if config.pushover_user_key and config.pushover_api_token else None,
        ) if c
    )
    print(f"Sent a test notification via: {channels}")
    return 0


# Fields whose values are buyer PII. Redaction replaces the value but keeps
# null/empty/present distinguishable, so the response *shape* stays readable
# (e.g. whether Etsy populated shipping_method at all).
_PII_FIELDS = frozenset({
    "name", "first_line", "second_line", "city", "zip", "formatted_address",
    "buyer_email", "seller_email", "buyer_user_id", "message_from_buyer",
    "message_from_seller", "message_from_payment", "gift_message",
    "gift_sender", "gift_wrap_price",
})


def _redact(value, key=None):
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if key in _PII_FIELDS and value not in (None, "", 0):
        return f"<redacted {type(value).__name__}>"
    return value


def cmd_dashboard(config, args) -> int:
    """Serve the local web dashboard."""
    try:
        from .dashboard import create_app
    except ImportError:
        print(
            "The dashboard needs Flask. Install it with:\n"
            '  pip install -e ".[dashboard]"',
            file=sys.stderr,
        )
        return 1

    password = args.password or config.dashboard_password
    if args.host not in ("127.0.0.1", "localhost") and not password:
        print(
            f"Refusing to serve on {args.host} without a password.\n\n"
            "This page shows API tokens that can spend real money, so exposing it\n"
            "to the network unprotected is not safe. Either:\n"
            "  • set dashboard.password in config.toml (or pass --password), or\n"
            "  • leave the default localhost binding and tunnel in:\n"
            f"      ssh -L {args.port}:localhost:{args.port} <user>@<pi>\n"
            f"    then open http://localhost:{args.port}",
            file=sys.stderr,
        )
        return 1

    app = create_app(Path(args.config or "config.toml"), password)
    where = "this machine only" if args.host in ("127.0.0.1", "localhost") else "the local network"
    logging.info("Dashboard on http://%s:%s (reachable from %s)", args.host, args.port, where)
    if args.host in ("127.0.0.1", "localhost"):
        logging.info(
            "Headless? From your laptop: ssh -L %s:localhost:%s <user>@<this-host>",
            args.port, args.port,
        )
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def cmd_dump_receipt(config, args) -> int:
    """Print the raw JSON Etsy returns for a receipt (for inspection/debugging)."""
    import json

    client = _build_client(config)
    if args.receipt_id:
        receipts = [client.get_receipt(args.receipt_id)]
    else:
        receipts = client.get_recent_receipts(limit=args.limit)
        if not receipts:
            print("No receipts found in this shop at all.", file=sys.stderr)
            return 1

    for receipt in receipts:
        data = receipt if args.raw else _redact(receipt)
        print(json.dumps(data, indent=2, sort_keys=True))
        print()

    if not args.raw:
        print(
            "# Buyer details are redacted; pass --raw to see them. Redacted values "
            "still show whether Etsy populated the field (null vs <redacted>).",
            file=sys.stderr,
        )
    return 0


def cmd_retry(config, args) -> int:
    """Re-run the pipeline for a held order after fixing the cause."""
    store = Store(config.db_path)
    printer = get_printer(config)
    labeler = _build_labeler(config, store, printer)
    receipt = store.get_receipt_json(args.receipt_id)
    if receipt is None:
        print(f"Order #{args.receipt_id} not found.", file=sys.stderr)
        return 1
    row = store.get(args.receipt_id)
    if row["state"] == "held":
        # Resume from the furthest completed step.
        events = [e["to_state"] for e in store.events(args.receipt_id)]
        if "label_printed" in events and store.get_label(args.receipt_id):
            resume = "label_printed"
        elif store.get_label(args.receipt_id):
            resume = "label_purchased"
        elif "slip_printed" in events:
            resume = "slip_printed"
        else:
            resume = "new"
        store.transition(args.receipt_id, resume, "manual retry")
    try:
        etsy = _build_client(config)
    except AuthError:
        etsy = None  # slip/label steps still work without Etsy auth
    if advance_order(receipt, store, printer, labeler, etsy, _build_notifier(config)):
        print(f"Order #{args.receipt_id} advanced to {store.get(args.receipt_id)['state']}.")
        return 0
    print(f"Order #{args.receipt_id} did not advance (state: {store.get(args.receipt_id)['state']}).")
    return 1


def cmd_reprint_label(config, args) -> int:
    store = Store(config.db_path)
    printer = get_printer(config)
    labeler = _build_labeler(config, store, printer)
    if labeler is None:
        print("Labels are not enabled in config.toml.", file=sys.stderr)
        return 1
    if store.get_label(args.receipt_id) is None:
        print(f"No purchased label for order #{args.receipt_id}.", file=sys.stderr)
        return 1
    labeler._print(args.receipt_id)
    return 0


def cmd_clear_attempt(config, args) -> int:
    store = Store(config.db_path)
    if not store.label_attempted(args.receipt_id):
        print(f"No pending purchase attempt for order #{args.receipt_id}.")
        return 0
    store.clear_label_attempt(args.receipt_id)
    print(
        f"Cleared. Only do this after confirming in the Shippo dashboard that "
        f"order #{args.receipt_id} was NOT charged."
    )
    return 0


def cmd_quote(config, args) -> int:
    """Show available rates for an order without buying anything."""
    store = Store(config.db_path)
    if not config.labels.enabled:
        print("Labels are not enabled in config.toml.", file=sys.stderr)
        return 1
    receipt = store.get_receipt_json(args.receipt_id)
    if receipt is None:
        print(f"Order #{args.receipt_id} not found.", file=sys.stderr)
        return 1
    client = make_client(config.labels.token, config.labels.allow_live)
    parcel = compute_parcel(receipt, config.labels)
    shipment = client.create_shipment(
        config.labels.ship_from, build_address_to(receipt), parcel
    )
    rates = shipment.get("rates", [])
    if not rates:
        print("No rates returned.")
        return 1
    chosen = pick_rate(rates, config.labels.allowed_providers)
    print(f"Parcel: {parcel['weight']} oz  ({'TEST' if client.is_test else 'LIVE'})")
    for r in sorted(rates, key=lambda r: float(r["amount"])):
        mark = " <== would buy" if r["object_id"] == chosen["object_id"] else ""
        days = f"~{r['estimated_days']}d" if r.get("estimated_days") else ""
        print(
            f"  {r['amount']:>7} {r['currency']}  {r['provider']:<6} "
            f"{r.get('servicelevel', {}).get('name', ''):<28}{days}{mark}"
        )
    return 0


SAMPLE_ADDRESS_TO = {
    "name": "Shippo Test Recipient",
    "street1": "215 Clayton St.",
    "city": "San Francisco",
    "state": "CA",
    "zip": "94117",
    "country": "US",
}


def cmd_test_label(config, args) -> int:
    """Buy and print a TEST label end-to-end, without touching the order db."""
    if not config.labels.enabled:
        print("Labels are not enabled in config.toml.", file=sys.stderr)
        return 1
    client = make_client(config.labels.token, config.labels.allow_live)
    if not client.is_test:
        print("Refusing: test-label requires a shippo_test_ token.", file=sys.stderr)
        return 1
    printer = get_printer(config)
    parcel = {
        "length": config.labels.parcel["length_in"],
        "width": config.labels.parcel["width_in"],
        "height": config.labels.parcel["height_in"],
        "distance_unit": "in",
        "weight": config.labels.parcel["packaging_oz"] + 8,
        "mass_unit": "oz",
    }
    shipment = client.create_shipment(config.labels.ship_from, SAMPLE_ADDRESS_TO, parcel)
    rate = pick_rate(shipment.get("rates", []), config.labels.allowed_providers)
    print(f"Buying TEST label: {rate['provider']} {rate['servicelevel']['name']} "
          f"{rate['amount']} {rate['currency']}")
    txn = client.buy_label(rate["object_id"], config.labels.file_type)
    if txn.get("status") != "SUCCESS":
        msgs = "; ".join(m.get("text", str(m)) for m in txn.get("messages", []))
        print(f"Purchase failed: {msgs or txn.get('status')}", file=sys.stderr)
        return 1
    ext = {"ZPLII": "zpl", "PNG": "png"}.get(config.labels.file_type, "pdf")
    destination = printer.print_bytes("label-TEST", client.download(txn["label_url"]), ext)
    print(f"Tracking (test): {txn.get('tracking_number')}")
    print(f"Label -> {destination}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="etsy-auto-print",
        description="Automatic packing slips (phase 1) for Etsy orders.",
    )
    parser.add_argument("-c", "--config", default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth", help="run the one-time OAuth consent flow")
    p.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    p.set_defaults(func=cmd_auth)

    sub.add_parser("poll", help="check for new orders once").set_defaults(func=cmd_poll)
    sub.add_parser("run", help="poll continuously").set_defaults(func=cmd_run)
    sub.add_parser("status", help="list orders and states").set_defaults(func=cmd_status)
    sub.add_parser(
        "check", help="health-check every connection (Etsy, Shippo, printer, ...)"
    ).set_defaults(func=cmd_check)

    p = sub.add_parser("show", help="details + history for one order")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("reprint", help="re-print a packing slip")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_reprint)

    sub.add_parser("test-slip", help="print a sample slip with fake data").set_defaults(
        func=cmd_test_slip
    )
    sub.add_parser("test-label", help="buy + print a Shippo TEST label").set_defaults(
        func=cmd_test_label
    )
    sub.add_parser("test-notify", help="send a test ntfy notification").set_defaults(
        func=cmd_test_notify
    )

    p = sub.add_parser("retry", help="re-run the pipeline for a held order")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("reprint-label", help="re-print an already-purchased label")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_reprint_label)

    p = sub.add_parser("quote", help="show shipping rates for an order (no purchase)")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("dashboard", help="serve the local web dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default localhost; 0.0.0.0 needs a password)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--password", default=None, help="overrides dashboard.password in config")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser(
        "dump-receipt", help="print the raw JSON Etsy returns for recent order(s)"
    )
    p.add_argument("receipt_id", type=int, nargs="?", help="specific order; default: most recent")
    p.add_argument("--limit", type=int, default=1, help="how many recent receipts (default 1)")
    p.add_argument("--raw", action="store_true", help="include buyer PII (default: redacted)")
    p.set_defaults(func=cmd_dump_receipt)

    p = sub.add_parser(
        "clear-attempt", help="clear a stuck purchase-attempt marker (see docs first)"
    )
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_clear_attempt)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = load_config(args.config)
        return args.func(config, args)
    except (ConfigError, AuthError, EtsyApiError, LabelError, ShippoError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
