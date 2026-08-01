#!/usr/bin/env bash
# Renamed: this installs both launchers now, not just the dashboard.
# Kept so the command in older instructions still works.
exec "$(cd "$(dirname "$0")" && pwd)/install-shortcuts.sh" "$@"
