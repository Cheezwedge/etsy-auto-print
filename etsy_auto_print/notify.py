"""Failure notifications.

Held orders need a human; this makes sure a human finds out. Backed by
ntfy.sh (or any self-hosted ntfy server): configure [notify] ntfy_url in
config.toml and subscribe to the topic in the ntfy phone app or browser.
Unconfigured, notifications just go to the log.

Notification failures are deliberately swallowed (logged, never raised):
alerting must never break the pipeline it's alerting about.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("etsy-auto-print")


class Notifier:
    def __init__(self, ntfy_url: str | None = None):
        self.ntfy_url = ntfy_url

    def send(self, title: str, message: str) -> None:
        log.warning("NOTIFY: %s — %s", title, message)
        if not self.ntfy_url:
            return
        try:
            requests.post(
                self.ntfy_url,
                data=message.encode(),
                headers={"Title": title, "Priority": "high", "Tags": "package"},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.error("Notification delivery failed: %s", exc)
