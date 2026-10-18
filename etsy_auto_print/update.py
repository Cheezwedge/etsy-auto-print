"""One-step update: pull, install, restart both services.

The shell script `update.sh` does the git pull, the install, and the
systemctl restarts. This module is the part that has to be right when a
label is already being bought or printed, and the line that tells you the
version the dashboard header will show.

A restart sends SIGTERM. The poller treats that as "finish the poll already
in progress, then exit" — it does not start another one, and it does not
abort the purchase or the print that poll is in. `in_flight` is the other
half: the update waits until nothing is mid-purchase before it asks systemd
to stop anything. A claim older than the stale-release window is a crash,
not a live purchase; the next poll resumes it and the claim stops a second
charge.
"""

from __future__ import annotations

import sys
import time

from .about import version_label
from .pipeline import STALE_RELEASE_SECONDS
from .store import Store

# How long a label_purchased row still counts as "the printer is working".
# The transition happens immediately before the download and the print, so a
# fresh row is an in-progress print. An older one is a print that already
# finished or a crash the next poll will retry — not something to wait on.
PRINT_IN_FLIGHT_SECONDS = STALE_RELEASE_SECONDS


def request_stop(state: dict, signum: int) -> None:
    """First signal finishes the current poll. A second one stops now."""
    if state.get("flag"):
        raise KeyboardInterrupt
    state["flag"] = True
    state["signal"] = signum


def interruptible_sleep(seconds: float, stop, sleep=time.sleep, monotonic=time.monotonic) -> None:
    """Wait up to `seconds`, returning early once `stop()` is true."""
    deadline = monotonic() + seconds
    while not stop():
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        sleep(min(0.5, remaining))


def serve_until_stopped(poll, idle_seconds: float, stop, sleep=time.sleep) -> None:
    """Call poll, then idle, until stop() is true.

    stop() is checked before a poll and during the idle wait, never inside
    poll(). A SIGTERM that arrives while postage is being bought waits until
    that call returns.
    """
    while not stop():
        poll()
        if stop():
            return
        interruptible_sleep(idle_seconds, stop, sleep=sleep)


def in_flight(store: Store, now: float | None = None) -> list[str]:
    """Why a restart should wait, or [] when nothing is mid purchase or print.

    Each reason names the order. Callers print them and poll again.
    """
    now = time.time() if now is None else now
    reasons = []
    for row in store.in_state("releasing"):
        age = now - float(row["updated_at"] or 0)
        if age < STALE_RELEASE_SECONDS:
            reasons.append(
                f"order #{row['receipt_id']} is being released "
                f"({int(age)}s ago) — waiting so postage is not interrupted"
            )
    for row in store.all_orders():
        # held: the attempt already failed and is waiting for a person.
        # done: nothing left to buy or print. Neither is in progress.
        if row["state"] in ("held", "done"):
            continue
        rid = row["receipt_id"]
        if store.get_label(rid) is not None:
            continue
        attempt_at = store.label_attempt_at(rid)
        if attempt_at is None:
            continue
        age = now - attempt_at
        if age < STALE_RELEASE_SECONDS:
            reasons.append(
                f"order #{rid} is buying postage ({int(age)}s ago) — "
                "waiting so the purchase is not interrupted"
            )
    for row in store.in_state("label_purchased"):
        age = now - float(row["updated_at"] or 0)
        if age < PRINT_IN_FLIGHT_SECONDS:
            reasons.append(
                f"order #{row['receipt_id']} is printing a label "
                f"({int(age)}s ago) — waiting so the print is not interrupted"
            )
    return reasons


def wait_until_idle(
    store: Store,
    seconds: float,
    now=time.time,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> list[str]:
    """Block until `in_flight` is empty, or `seconds` has passed.

    Returns whatever is still in flight when time runs out (empty if idle).
    A timed-out purchase is left to the claim lock: restarting will not buy
    it a second time, and the next poll finishes or holds it.
    """
    deadline = monotonic() + max(0.0, seconds)
    announced = False
    while True:
        reasons = in_flight(store, now())
        if not reasons:
            return []
        if monotonic() >= deadline:
            return reasons
        if not announced:
            for reason in reasons:
                print(reason, flush=True)
            announced = True
        sleep(min(1.0, max(0.0, deadline - monotonic())))


def success_line(label: str, revision: str) -> str:
    """The line `update` prints. `label` is what the dashboard header shows."""
    return (
        f"Updated etsy-auto-print to {label}. "
        f"git rev-parse --short HEAD is {revision}; "
        f"the dashboard header shows {label}."
    )


def _revision() -> str:
    from .about import _git

    return _git("rev-parse", "--short", "HEAD") or "unknown"


def main(argv: list[str] | None = None) -> int:
    """Helpers for update.sh. Not the update itself — that is the script."""
    import argparse

    parser = argparse.ArgumentParser(prog="etsy-auto-print.update")
    parser.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help="wait until no purchase or print is in progress",
    )
    parser.add_argument(
        "--success-line",
        action="store_true",
        help="print the version line and exit",
    )
    parser.add_argument("-c", "--config", default=None, help="path to config.toml")
    args = parser.parse_args(argv)

    if args.success_line:
        print(success_line(version_label(), _revision()))
        return 0

    if args.wait is None:
        parser.error("pass --wait SECONDS or --success-line")

    try:
        from .config import load_config

        config = load_config(args.config)
    except Exception as exc:
        # A config that does not load has no orders database we can trust.
        # Updating anyway is safer than refusing: there is nothing to interrupt.
        print(
            f"Could not read config to check for an in-progress purchase ({exc}). "
            "Continuing. A purchase already claimed will not be bought twice.",
            file=sys.stderr,
        )
        return 0

    store = Store(config.db_path)
    try:
        still = wait_until_idle(store, args.wait)
    finally:
        store.close()
    if still:
        print("Still in progress after waiting:", file=sys.stderr)
        for reason in still:
            print(f"  {reason}", file=sys.stderr)
        return 2
    print("No postage purchase or label print is in progress.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
