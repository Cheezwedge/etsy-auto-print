#!/usr/bin/env bash
# Update this checkout in place.
#
#   ./update.sh
#
# Pulls the latest commit, installs dependency changes, installs the poller
# and the dashboard as systemd services if they are not already (enabled at
# boot, restart on failure), then restarts both. Safe to run again.
#
# A restart does not cut off a purchase or a print. The poller finishes the
# poll it is already in when systemd asks it to stop, and this script waits
# until no order is mid-purchase or mid-print before that ask. If the wait
# runs out, the purchase claim already recorded in orders.db still stops a
# second charge.
#
# First time, from the checkout (the script is not there until you pull):
#   cd ~/etsy-auto-print && git pull && ./update.sh
# Every time after, from anywhere:
#   etsy-auto-print update

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

POLLER="etsy-auto-print"
DASHBOARD="etsy-auto-print-dashboard"
PY="$REPO/.venv/bin/python"
PIP="$REPO/.venv/bin/pip"
# How long to wait for a purchase or print already underway. Past this, the
# claim lock is what keeps a restart from buying postage twice.
WAIT_SECONDS="${ETSY_UPDATE_WAIT_SECONDS:-300}"

DRY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        -h|--help)
            echo "Usage: $0 [--dry-run]"
            echo "Pull, install dependencies, and restart the poller and the dashboard."
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--dry-run]" >&2
            exit 2
            ;;
    esac
done

run() {
    if [ "$DRY" -eq 1 ]; then
        printf 'would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

if [ "$(id -un)" = "root" ]; then
    echo "Run the update as your normal user, not root." >&2
    echo "It will ask for sudo only when it needs to install or restart a service." >&2
    exit 1
fi

if [ ! -d "$REPO/.git" ]; then
    echo "No git checkout at $REPO — cannot pull an update." >&2
    exit 1
fi

if [ ! -x "$PY" ] || [ ! -x "$PIP" ]; then
    echo "No virtualenv at $REPO/.venv." >&2
    echo "Create it once, then re-run this:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e \".[dashboard]\"" >&2
    exit 1
fi

echo "Pulling the latest code (fast-forward only)..."
run git pull --ff-only

echo "Installing dependencies..."
run "$PIP" install -e ".[dashboard]"

# Write a unit when it is missing or older than the one this checkout would
# generate. Rewriting an unchanged unit would ask for a sudo password on
# every update; restart itself is allowed without one.
ensure_unit() {
    local which="$1"
    local unit="$2"
    if [ "$DRY" -eq 1 ]; then
        echo "would ensure $unit is installed (enabled at boot, restart on failure)"
        return 0
    fi
    local generated target
    generated="$(./systemd/install-service.sh "$which" --print)"
    target="/etc/systemd/system/$unit.service"
    if [ -f "$target" ] && [ "$(cat "$target")" = "$generated" ]; then
        echo "$unit is already installed."
        return 0
    fi
    echo "Installing $unit (enabled at boot, restart on failure)..."
    ./systemd/install-service.sh "$which" --no-restart
}

ensure_unit run "$POLLER"
ensure_unit dashboard "$DASHBOARD"

echo "Waiting for any in-progress postage purchase or print to finish..."
if [ "$DRY" -eq 1 ]; then
    echo "would wait until no postage purchase or label print is in progress"
else
    set +e
    "$PY" -m etsy_auto_print.update --wait "$WAIT_SECONDS"
    wait_status=$?
    set -e
    if [ "$wait_status" -eq 2 ]; then
        echo "A purchase or print was still marked in progress after ${WAIT_SECONDS}s." >&2
        echo "Restarting anyway. A claim recorded before money moves will not buy postage a second time." >&2
    elif [ "$wait_status" -ne 0 ]; then
        echo "Could not check for an in-progress purchase (exit $wait_status)." >&2
        exit "$wait_status"
    fi
fi

# A dashboard started by the desktop launcher holds port 8765, so the service
# cannot start. Stop that copy only after the wait, so Print now is finished.
if [ "$DRY" -eq 0 ]; then
    if pgrep -u "$(id -un)" -f "etsy-auto-print dashboard" >/dev/null \
            && ! systemctl is-active --quiet "$DASHBOARD"; then
        echo "Stopping the dashboard that is running outside systemd."
        pkill -u "$(id -un)" -f "etsy-auto-print dashboard" || true
        sleep 1
    fi
fi

restart_unit() {
    local unit="$1"
    local err
    if [ "$DRY" -eq 1 ]; then
        echo "would run: sudo -n systemctl restart $unit"
        return 0
    fi
    # `&&` so a refused sudo does not abort the script before the fallback.
    err="$(sudo -n systemctl restart "$unit" 2>&1)" && return 0
    # The installer originally allowed only the dashboard's --no-block form.
    # Use it rather than failing an update that is otherwise done.
    if [ "$unit" = "$DASHBOARD" ] && sudo -n systemctl restart --no-block "$unit" 2>/dev/null; then
        local _i
        for _i in 1 2 3 4 5 6 7 8 9 10; do
            systemctl is-active --quiet "$unit" && return 0
            sleep 1
        done
    fi
    if [ -n "$err" ]; then
        echo "$err" >&2
    fi
    echo "Could not restart $unit." >&2
    echo "If sudo asked for a password, run ./systemd/install-service.sh once, then ./update.sh again." >&2
    echo "Or restart it by hand: sudo systemctl restart $unit" >&2
    return 1
}

echo "Restarting $POLLER (it finishes the current poll before it exits)..."
restart_unit "$POLLER"

# The poller restart can itself finish a purchase that started during the
# first wait. Don't stop the dashboard — Print now is its child — until
# that purchase and print are done.
if [ "$DRY" -eq 0 ]; then
    set +e
    "$PY" -m etsy_auto_print.update --wait "$WAIT_SECONDS"
    wait_status=$?
    set -e
    if [ "$wait_status" -eq 2 ]; then
        echo "A purchase or print was still in progress. Restarting the dashboard anyway." >&2
        echo "The claim lock will not buy postage a second time." >&2
    elif [ "$wait_status" -ne 0 ]; then
        exit "$wait_status"
    fi
fi

echo "Restarting $DASHBOARD..."
restart_unit "$DASHBOARD"

if [ "$DRY" -eq 0 ]; then
    if ! systemctl is-active --quiet "$POLLER"; then
        echo "$POLLER did not come back. Check: journalctl -u $POLLER -n 40 --no-pager" >&2
        exit 1
    fi
    if ! systemctl is-active --quiet "$DASHBOARD"; then
        echo "$DASHBOARD did not come back. Check: journalctl -u $DASHBOARD -n 40 --no-pager" >&2
        exit 1
    fi
fi

echo
"$PY" -m etsy_auto_print.update --success-line
