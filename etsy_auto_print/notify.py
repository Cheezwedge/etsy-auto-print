"""Failure notifications.

Held orders need a human; this makes sure a human finds out. Two backends,
either or both may be configured in [notify]:

- ntfy_url        — ntfy.sh (or a self-hosted server): subscribe to the
                     topic in the ntfy phone app.
- pushover_user_key + pushover_api_token — pushover.net: install the app,
                     log into your account, create an application token.

Unconfigured, notifications just go to the log.

Notification failures are deliberately swallowed (logged, never raised):
alerting must never break the pipeline it's alerting about.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("etsy-auto-print")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class Notifier:
    def __init__(
        self,
        ntfy_url: str | None = None,
        pushover_user_key: str | None = None,
        pushover_api_token: str | None = None,
    ):
        self.ntfy_url = ntfy_url
        self.pushover_user_key = pushover_user_key
        self.pushover_api_token = pushover_api_token

    def send(self, title: str, message: str) -> None:
        log.warning("NOTIFY: %s — %s", title, message)
        if self.ntfy_url:
            self._send_ntfy(title, message)
        if self.pushover_user_key and self.pushover_api_token:
            self._send_pushover(title, message)

    def _send_ntfy(self, title: str, message: str) -> None:
        try:
            requests.post(
                self.ntfy_url,
                data=message.encode(),
                headers={"Title": title, "Priority": "high", "Tags": "package"},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.error("ntfy delivery failed: %s", exc)

    def _send_pushover(self, title: str, message: str) -> None:
        try:
            requests.post(
                PUSHOVER_URL,
                data={
                    "token": self.pushover_api_token,
                    "user": self.pushover_user_key,
                    "title": title,
                    "message": message,
                    "priority": 1,  # high priority: bypasses phone quiet hours
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            log.error("Pushover delivery failed: %s", exc)
