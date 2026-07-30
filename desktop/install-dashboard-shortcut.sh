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
echo "Search your applications for \"Etsy Label Dashboard\", or double-click the"
echo "desktop icon. On GNOME you may have to right-click it once and choose"
echo "\"Allow Launching\"."
