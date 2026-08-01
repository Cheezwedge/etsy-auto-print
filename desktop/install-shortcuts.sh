#!/usr/bin/env bash
# Installs the Etsy print-Pi launchers on this Linux desktop.
#
#     ./desktop/install-shortcuts.sh YOURUSER@YOURPI
#
# Puts the scripts in ~/.local/bin, launchers in the applications menu, and
# copies on the Desktop if you have one. Re-run it any time to change the Pi
# it points at.

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  read -r -p "Pi SSH target (e.g. youruser@printpi.local): " TARGET
fi
if [ -z "$TARGET" ]; then
  echo "Nothing to install without a target." >&2
  exit 2
fi

# --- check the target before baking it into a launcher --------------------
# Getting this wrong is invisible until you click the icon, so catch it here.

if [ "$TARGET" = "youruser@printpi.local" ]; then
  cat >&2 <<'EOF'
That is the example from the documentation, not your Pi.

Replace both halves with your own:
    <your Pi username>@<your Pi hostname or IP>

On the Pi, `whoami` gives the username and `hostname -I` gives the IP.
EOF
  exit 2
fi

HOST="${TARGET##*@}"
if ! getent hosts "$HOST" >/dev/null 2>&1; then
  cat >&2 <<EOF

Warning: this machine cannot resolve "$HOST".

If that is an mDNS name (anything ending in .local), your laptop needs
avahi/nss-mdns for it to work, and plenty of setups don't have it. The Pi's
IP address always works — run \`hostname -I\` on the Pi and use that instead.

EOF
  if [ -t 0 ]; then
    read -r -p "Install pointing at \"$HOST\" anyway? [y/N] " REPLY
    case "$REPLY" in
      [yY]*) ;;
      *) echo "Nothing installed."; exit 2 ;;
    esac
  else
    echo "Continuing anyway (not running interactively)." >&2
  fi
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
# A desktop copy is optional — some setups have no ~/Desktop at all.
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

mkdir -p "$BIN_DIR" "$APP_DIR"
install -m 0755 "$SRC_DIR/etsy-dashboard" "$BIN_DIR/etsy-dashboard"
install -m 0755 "$SRC_DIR/etsy-pi-shell" "$BIN_DIR/etsy-pi-shell"
install -m 0644 "$SRC_DIR/etsy-launcher-common.sh" "$BIN_DIR/etsy-launcher-common.sh"

# file | name | Exec command | icon | comment | keywords
#
# "Connect to Pi" is deliberately a bare ssh with no wrapper — nothing to go
# wrong, nothing between you and the session. The cost is that a failed
# connection closes the window with the error still on it; the wrapped
# shortcuts above it are the ones that keep the window open and log why.
LAUNCHERS="
etsy-auto-print-dashboard|Etsy Label Dashboard|$BIN_DIR/etsy-dashboard $TARGET|printer|Connect to the print Pi, start the dashboard, and open it in a browser|dashboard;labels;
etsy-auto-print-shell|Etsy Print Pi (SSH)|$BIN_DIR/etsy-pi-shell $TARGET|utilities-terminal|Open a terminal on the print Pi, in the project directory|ssh;terminal;shell;
etsy-auto-print-connect|Connect to Pi|ssh $TARGET|network-server|Plain SSH session on the print Pi|ssh;connect;
"

write_launcher() {  # $1 = path, $2 = name, $3 = exec, $4 = icon, $5 = comment, $6 = keywords
  cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$2
GenericName=Etsy shipping automation
Comment=$5
Exec=$3
Icon=$4
Terminal=true
Categories=Utility;Network;
Keywords=etsy;shipping;printer;pi;$6
StartupNotify=false
EOF
  chmod 0755 "$1"
}


echo "$LAUNCHERS" | while IFS='|' read -r file name exec_cmd icon comment keywords; do
  [ -n "${file:-}" ] || continue
  write_launcher "$APP_DIR/$file.desktop" "$name" "$exec_cmd" "$icon" "$comment" "$keywords"
  if [ -d "$DESKTOP_DIR" ]; then
    write_launcher "$DESKTOP_DIR/$file.desktop" "$name" "$exec_cmd" "$icon" "$comment" "$keywords"
    # GNOME requires this before it will run a launcher from the desktop.
    gio set "$DESKTOP_DIR/$file.desktop" metadata::trusted true 2>/dev/null || true
  fi
  echo "  $name"
done

command -v update-desktop-database >/dev/null 2>&1 &&
  update-desktop-database "$APP_DIR" 2>/dev/null || true

cat <<EOF

Installed the launchers above, in your applications menu$(
  [ -d "$DESKTOP_DIR" ] && echo " and on your desktop"
).

  Etsy Label Dashboard   the web UI — status, config, products, logs
  Etsy Print Pi (SSH)    a terminal on the Pi, in the project directory
  Connect to Pi          just \`ssh $TARGET\`, nothing else

Connects to: $TARGET
Launch log:  ${XDG_CACHE_HOME:-$HOME/.cache}/etsy-auto-print/launcher.log

On GNOME you may have to right-click each desktop icon once and choose
"Allow Launching".
EOF
