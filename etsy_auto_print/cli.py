"""Command-line interface.

    etsy-auto-print auth              one-time browser OAuth consent
    etsy-auto-print poll              single poll pass
    etsy-auto-print run               poll forever (systemd target)
    etsy-auto-print status            show every order and its state
    etsy-auto-print show ID           full detail + event history for one order
    etsy-auto-print reprint ID        re-render + re-print a slip (un-holds)
    etsy-auto-print test-slip         print a sample slip with fake data
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from .auth import AuthError, TokenStore, authorize
from .config import ConfigError, load_config
from .etsy import EtsyApiError, EtsyClient
from .pipeline import poll_once, reprint
from .printer import get_printer
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
    n = poll_once(client, store, printer)
    print(f"Processed {n} new order(s).")
    return 0


def cmd_run(config, args) -> int:
    client = _build_client(config)
    store = Store(config.db_path)
    printer = get_printer(config)
    logging.info(
        "Polling every %ss (printer backend: %s). Ctrl-C to stop.",
        config.poll_interval,
        config.printer_backend,
    )
    while True:
        try:
            poll_once(client, store, printer)
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


def cmd_show(config, args) -> int:
    store = Store(config.db_path)
    row = store.get(args.receipt_id)
    if row is None:
        print(f"Order #{args.receipt_id} not found.", file=sys.stderr)
        return 1
    print(f"Order #{row['receipt_id']}  state={row['state']}  buyer={row['buyer_name']}")
    if row["error"]:
        print(f"Held because: {row['error']}")
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

    p = sub.add_parser("show", help="details + history for one order")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("reprint", help="re-print a packing slip")
    p.add_argument("receipt_id", type=int)
    p.set_defaults(func=cmd_reprint)

    sub.add_parser("test-slip", help="print a sample slip with fake data").set_defaults(
        func=cmd_test_slip
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = load_config(args.config)
        return args.func(config, args)
    except (ConfigError, AuthError, EtsyApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
