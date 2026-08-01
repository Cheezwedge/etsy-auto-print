# Shared setup for the etsy-* desktop launchers. Sourced, never executed.
#
# Before sourcing, the caller sets:
#   TARGET_ARG   the user@host argument it was given (may be empty)
#   LAUNCH_KIND  short word identifying the launcher, for the log
#
# Afterwards it can rely on: TARGET (validated), LOG, say(), and a window that
# stays up long enough to read whatever went wrong.

set -u

TARGET="${TARGET_ARG:-${ETSY_PI_HOST:-}}"

# Everything that happens is also appended here, so a window that closes too
# fast to read never loses the reason why.
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/etsy-auto-print/launcher.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || LOG=/dev/null
printf '\n=== %s === %s target=%s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "${LAUNCH_KIND:-launcher}" "${TARGET:-<none>}" \
  >>"$LOG" 2>/dev/null

say() {
  echo "$*"
  echo "$*" >>"$LOG" 2>/dev/null
}

# Keep the window up on failure — a launcher-spawned terminal usually closes
# the instant the script exits, taking the error message with it.
#
# Read from /dev/tty rather than stdin: a desktop launcher commonly hands the
# process /dev/null on stdin, which makes a plain `read` return instantly and
# close the window anyway — the exact thing this is here to prevent.
hold_window() {
  echo
  echo "Full log: $LOG"
  # Redirections apply left to right, so stderr must be silenced *before* the
  # /dev/tty open is attempted or its failure still reaches the screen.
  if read -r -p "Press Enter to close this window. " _ 2>/dev/null </dev/tty; then
    return
  fi
  # No terminal to prompt on. Stay alive briefly so the text stays readable.
  echo "(closing in 90s — Ctrl-C to close now)"
  sleep 90
}
trap hold_window EXIT

if [ -z "$TARGET" ]; then
  say "No Pi to connect to."
  echo
  say "Pass it as an argument:   $0 YOURUSER@YOURPI"
  say "or set the environment:   ETSY_PI_HOST=YOURUSER@YOURPI"
  exit 2
fi

if ! command -v ssh >/dev/null 2>&1; then
  say "ssh is not installed on this machine."
  say "Install it with:   sudo apt install openssh-client"
  exit 127
fi

# Reported the same way by every launcher, so the advice doesn't drift.
report_ssh_exit() {
  local status="$1"
  echo
  if [ "$status" -eq 0 ]; then
    say "Disconnected from ${TARGET}."
    return
  fi
  say "Connection to ${TARGET} ended with status ${status}."
  if [ "$status" -eq 255 ]; then
    say "ssh could not connect. Check the hostname, and that you can run:"
    say "    ssh ${TARGET}"
  fi
}
