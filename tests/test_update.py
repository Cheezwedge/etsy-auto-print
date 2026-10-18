"""Updating the Pi is one command, and it must not buy postage twice.

`update.sh` pulls, installs, and restarts both services. The pieces under
test are the ones a restart can get wrong: waiting while a purchase or a
print is actually in progress, letting the current poll finish, and printing
the same version the dashboard header shows.
"""

import os
import subprocess
from pathlib import Path

import pytest

from etsy_auto_print.about import version_label
from etsy_auto_print.pipeline import STALE_RELEASE_SECONDS
from etsy_auto_print.store import Store
from etsy_auto_print.update import (
    in_flight,
    interruptible_sleep,
    request_stop,
    serve_until_stopped,
    success_line,
    wait_until_idle,
)

REPO = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


def _order(store, rid=9001, at=NOW):
    store.register(
        {"receipt_id": rid, "name": "Casey", "created_timestamp": at},
        now=at,
    )
    return rid


def _stamp(store, rid, updated_at):
    store.conn.execute(
        "UPDATE orders SET updated_at = ? WHERE receipt_id = ?",
        (updated_at, rid),
    )
    store.conn.commit()


def _attempt_at(store, rid, at):
    store.conn.execute(
        "UPDATE label_attempts SET at = ? WHERE receipt_id = ?",
        (at, rid),
    )
    store.conn.commit()


def test_nothing_in_flight_when_the_queue_is_quiet(store):
    _order(store)
    assert in_flight(store, NOW) == []


def test_a_live_release_is_in_flight(store):
    rid = _order(store)
    assert store.claim_releasing(rid, "re-checking")
    _stamp(store, rid, NOW - 5)
    reasons = in_flight(store, NOW)
    assert len(reasons) == 1
    assert f"#{rid}" in reasons[0]
    assert "released" in reasons[0]


def test_a_stale_release_is_not_in_flight(store):
    # Older than the window the next poll uses to resume a crashed release.
    # Waiting would block the update forever; the claim still stops a second buy.
    rid = _order(store)
    assert store.claim_releasing(rid, "re-checking")
    _stamp(store, rid, NOW - STALE_RELEASE_SECONDS - 1)
    assert in_flight(store, NOW) == []


def test_a_purchase_claim_without_a_label_is_in_flight(store):
    rid = _order(store)
    store.transition(rid, "slip_printed", "slip")
    store.record_label_attempt(rid)
    _attempt_at(store, rid, NOW - 2)
    reasons = in_flight(store, NOW)
    assert any(f"#{rid}" in reason and "buying postage" in reason for reason in reasons)


def test_a_saved_label_is_not_still_being_bought(store):
    rid = _order(store)
    store.transition(rid, "slip_printed", "slip")
    store.record_label_attempt(rid)
    _attempt_at(store, rid, NOW - 2)
    store.save_label(rid, tracking_number="9400")
    assert in_flight(store, NOW) == []


def test_a_held_attempt_is_not_in_flight(store):
    rid = _order(store)
    store.transition(rid, "slip_printed", "slip")
    store.record_label_attempt(rid)
    _attempt_at(store, rid, NOW - 2)
    store.hold(rid, "label purchase failed")
    assert in_flight(store, NOW) == []


def test_an_old_purchase_claim_is_left_to_the_lock(store):
    rid = _order(store)
    store.transition(rid, "slip_printed", "slip")
    store.record_label_attempt(rid)
    _attempt_at(store, rid, NOW - STALE_RELEASE_SECONDS - 5)
    assert in_flight(store, NOW) == []


def test_a_fresh_label_purchase_is_still_printing(store):
    rid = _order(store)
    store.transition(rid, "label_purchased", "bought")
    _stamp(store, rid, NOW - 3)
    reasons = in_flight(store, NOW)
    assert len(reasons) == 1
    assert "printing" in reasons[0]


def test_an_old_label_purchase_is_not_an_active_print(store):
    rid = _order(store)
    store.transition(rid, "label_purchased", "bought")
    _stamp(store, rid, NOW - STALE_RELEASE_SECONDS - 1)
    assert in_flight(store, NOW) == []


def test_wait_returns_immediately_when_idle(store):
    slept = []
    assert wait_until_idle(store, 30, sleep=slept.append) == []
    assert slept == []


def test_wait_gives_up_and_names_the_order(store):
    rid = _order(store)
    assert store.claim_releasing(rid, "re-checking")
    _stamp(store, rid, NOW - 1)
    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    still = wait_until_idle(
        store, seconds=2, now=lambda: NOW, sleep=sleep, monotonic=monotonic,
    )
    assert still and f"#{rid}" in still[0]
    assert clock["t"] >= 2


def test_wait_returns_once_the_purchase_finishes(store):
    rid = _order(store)
    assert store.claim_releasing(rid, "re-checking")
    _stamp(store, rid, NOW - 1)
    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds
        store.transition(rid, "done", "printed")

    assert wait_until_idle(
        store, seconds=30, now=lambda: NOW, sleep=sleep, monotonic=monotonic,
    ) == []


def test_the_poll_loop_finishes_the_current_poll_and_does_not_start_another():
    calls = []
    state = {"stop": False}

    def poll():
        calls.append("poll")
        state["stop"] = True

    def sleep(seconds):
        calls.append("sleep")

    serve_until_stopped(poll, 100, lambda: state["stop"], sleep=sleep)
    assert calls == ["poll"]


def test_the_poll_loop_leaves_the_idle_wait_when_asked_to_stop():
    calls = []
    state = {"stop": False}

    def poll():
        calls.append("poll")

    def sleep(seconds):
        calls.append("sleep")
        state["stop"] = True

    serve_until_stopped(poll, 100, lambda: state["stop"], sleep=sleep)
    assert calls == ["poll", "sleep"]


def test_interruptible_sleep_returns_when_stopped():
    slept = []

    def sleep(seconds):
        slept.append(seconds)

    interruptible_sleep(10, lambda: True, sleep=sleep, monotonic=lambda: 0)
    assert slept == []


def test_a_second_signal_stops_immediately():
    state = {"flag": False}
    request_stop(state, 15)
    assert state["flag"] is True
    with pytest.raises(KeyboardInterrupt):
        request_stop(state, 15)


def test_success_line_matches_the_dashboard_header_and_git():
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True,
    ).strip()
    label = version_label()
    assert sha in label
    line = success_line(label, sha)
    assert line.startswith(f"Updated etsy-auto-print to {label}.")
    assert f"git rev-parse --short HEAD is {sha}" in line
    assert f"the dashboard header shows {label}." in line


def test_update_script_parses_and_is_executable():
    script = REPO / "update.sh"
    assert os.access(script, os.X_OK)
    subprocess.check_call(["bash", "-n", str(script)])


def test_the_cli_update_command_runs_the_same_script():
    python = REPO / ".venv" / "bin" / "etsy-auto-print"
    if not python.is_file():
        pytest.skip("needs the installed etsy-auto-print command")
    out = subprocess.check_output(
        [str(python), "update", "--dry-run"], cwd=REPO, text=True, timeout=30,
    )
    assert "would run: git pull --ff-only" in out
    assert "Updated etsy-auto-print to" in out


def test_dry_run_plans_a_pull_an_install_and_both_restarts():
    python = REPO / ".venv" / "bin" / "python"
    if not python.is_file():
        pytest.skip("dry-run uses the project virtualenv")
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True,
    ).strip()
    out = subprocess.check_output(
        [str(REPO / "update.sh"), "--dry-run"], cwd=REPO, text=True, timeout=30,
    )
    assert "would run: git pull --ff-only" in out
    assert "pip install -e" in out and "dashboard" in out
    assert "would ensure etsy-auto-print is installed" in out
    assert "would ensure etsy-auto-print-dashboard is installed" in out
    assert "would wait until no postage purchase or label print is in progress" in out
    assert "would run: sudo -n systemctl restart etsy-auto-print\n" in out
    assert "would run: sudo -n systemctl restart etsy-auto-print-dashboard" in out
    assert f"git rev-parse --short HEAD is {sha}" in out
    assert "Updated etsy-auto-print to" in out
    # The plan is printed; the real commands are not executed.
    assert "would run:" in out


def test_dashboard_unit_is_enabled_at_boot_and_restarts_on_failure():
    out = subprocess.check_output(
        [str(REPO / "systemd" / "install-service.sh"), "dashboard", "--print"],
        cwd=REPO, text=True,
    )
    assert "Restart=on-failure" in out
    assert "WantedBy=multi-user.target" in out
    assert "TimeoutStopSec=300" in out
    assert out.strip().endswith("WantedBy=multi-user.target") or "WantedBy=multi-user.target" in out
    assert "etsy-auto-print dashboard" in out
    reference = (REPO / "systemd" / "etsy-auto-print-dashboard.service").read_text()
    assert "Restart=on-failure" in reference
    assert "WantedBy=multi-user.target" in reference
    assert "TimeoutStopSec=300" in reference


def test_poller_unit_waits_for_the_current_poll_to_finish():
    out = subprocess.check_output(
        [str(REPO / "systemd" / "install-service.sh"), "run", "--print"],
        cwd=REPO, text=True,
    )
    assert "Restart=on-failure" in out
    assert "TimeoutStopSec=300" in out
    assert "etsy-auto-print run" in out


def test_installer_rejects_unknown_flags():
    result = subprocess.run(
        [str(REPO / "systemd" / "install-service.sh"), "--bogus"],
        cwd=REPO, text=True, capture_output=True,
    )
    assert result.returncode == 2


def test_installer_allows_a_restartless_install():
    # --print exits before sudo. --no-restart must be accepted beside it,
    # which is how `update.sh` installs a missing dashboard without
    # restarting in the same breath.
    out = subprocess.check_output(
        [
            str(REPO / "systemd" / "install-service.sh"),
            "dashboard", "--print", "--no-restart",
        ],
        cwd=REPO, text=True,
    )
    assert "Restart=on-failure" in out
    script = (REPO / "systemd" / "install-service.sh").read_text()
    assert "restart etsy-auto-print-dashboard" in script
    assert "restart --no-block etsy-auto-print-dashboard" in script
