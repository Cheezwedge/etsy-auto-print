"""What version is actually running.

Surfaced in the dashboard header and in `check` because the alternative is
guessing. A dashboard is a separate long-lived process from the poller, so
`git pull` plus a service restart can leave the page serving old code — and
the only symptom is that a fixed bug appears not to be fixed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO), *args],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode(errors="replace").strip() or None


_started_with: str | None = None


def running_version() -> str:
    """The version this process started on.

    Pinned the first time it's asked, which for the dashboard is at startup.
    Comparing it with a fresh version_label() detects the case that keeps
    biting: code updated on disk, long-lived process still serving the old
    build, with a stale message as the only symptom.
    """
    global _started_with
    if _started_with is None:
        _started_with = version_label()
    return _started_with


def version_label() -> str:
    """e.g. "0.1.0 (9adf475)", or "0.1.0 (9adf475, modified)"."""
    try:
        from importlib.metadata import version

        base = version("etsy-auto-print")
    except Exception:
        base = "dev"

    if not (_REPO / ".git").exists():
        return base

    revision = _git("rev-parse", "--short", "HEAD")
    if not revision:
        return base
    # -uno: untracked files are output (outbox, label dumps, backups), not
    # edits to the code. Counting them makes every install look modified,
    # which is exactly when the marker stops meaning anything.
    if _git("status", "--porcelain", "-uno"):
        revision += ", modified"
    return f"{base} ({revision})"
