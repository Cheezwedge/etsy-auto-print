#!/usr/bin/env bash
# Installs the "Etsy Label Dashboard" launcher on this Linux desktop.
#
#     ./desktop/install-dashboard-shortcut.sh youruser@printpi.local
#
# Puts the script in ~/.local/bin, the launcher in the applications menu, and
# a copy on the Desktop if you have one. Re-run it any time to change the Pi
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
SCRIPT="$BIN_DIR/etsy-dashboard"
DESKTOP_FILE="etsy-auto-print-dashboard.desktop"

mkdir -p "$BIN_DIR" "$APP_DIR"
install -m 0755 "$SRC_DIR/etsy-dashboard" "$SCRIPT"

write_launcher() {
  cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Etsy Label Dashboard
GenericName=Shipping label dashboard
Comment=Connect to the print Pi, start the dashboard, and open it in a browser
Exec=$SCRIPT $TARGET
Icon=printer
Terminal=true
Categories=Utility;Network;
Keywords=etsy;shipping;label;printer;dashboard;
StartupNotify=false
EOF
  chmod 0755 "$1"
}

write_launcher "$APP_DIR/$DESKTOP_FILE"

# A desktop copy is optional — some setups have no ~/Desktop at all.
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
  write_launcher "$DESKTOP_DIR/$DESKTOP_FILE"
  # GNOME requires this before it will run a launcher from the desktop.
  gio set "$DESKTOP_DIR/$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

command -v update-desktop-database >/dev/null 2>&1 &&
  update-desktop-database "$APP_DIR" 2>/dev/null || true

echo "Installed:"
echo "  script    $SCRIPT"
echo "  launcher  $APP_DIR/$DESKTOP_FILE"
[ -d "$DESKTOP_DIR" ] && echo "  desktop   $DESKTOP_DIR/$DESKTOP_FILE"
echo
echo "Connects to: $TARGET"
echo "Launch log:  ${XDG_CACHE_HOME:-$HOME/.cache}/etsy-auto-print/launcher.log"
echo "Search your applications for \"Etsy Label Dashboard\", or double-click the"
echo "desktop icon. On GNOME you may have to right-click it once and choose"
echo "\"Allow Launching\"."
