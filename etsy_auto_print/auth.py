"""Etsy OAuth 2.0 (authorization code + PKCE) and token storage.

Etsy personal apps use OAuth 2.0 with PKCE and a localhost redirect.
`authorize()` runs the one-time browser consent flow and saves tokens to
tokens.json; `TokenStore.access_token()` transparently refreshes afterwards.

Etsy specifics worth knowing:
- access tokens look like "{user_id}.{secret}" and last 1 hour
- refresh tokens are rotated on every refresh — always persist the new one
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from .config import OAUTH_SCOPES, Config

CONNECT_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

# Refresh this many seconds before the token actually expires.
EXPIRY_MARGIN = 120


class AuthError(Exception):
    pass


class TokenStore:
    """Loads, refreshes, and persists OAuth tokens (plus the cached shop_id)."""

    def __init__(self, config: Config):
        self.config = config
        self.path: Path = config.tokens_path
        self._data: dict = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    @property
    def authorized(self) -> bool:
        return bool(self._data.get("refresh_token"))

    @property
    def user_id(self) -> int | None:
        token = self._data.get("access_token", "")
        head = token.split(".", 1)[0]
        return int(head) if head.isdigit() else None

    def get_cached_shop_id(self) -> int | None:
        return self._data.get("shop_id")

    def cache_shop_id(self, shop_id: int) -> None:
        self._data["shop_id"] = shop_id
        self._save()

    def access_token(self) -> str:
        if not self.authorized:
            raise AuthError("Not authorized yet — run: etsy-auto-print auth")
        if time.time() > self._data.get("expires_at", 0) - EXPIRY_MARGIN:
            self._refresh()
        return self._data["access_token"]

    def force_refresh(self) -> str:
        self._refresh()
        return self._data["access_token"]

    def store_grant(self, token_response: dict) -> None:
        self._data.update(
            access_token=token_response["access_token"],
            refresh_token=token_response["refresh_token"],
            expires_at=time.time() + token_response.get("expires_in", 3600),
        )
        self._save()

    def _refresh(self) -> None:
        try:
            resp = self._post_token(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self.config.keystring,
                    "refresh_token": self._data["refresh_token"],
                },
                headers={"x-api-key": self.config.api_key},
            )
        except requests.RequestException as exc:
            raise AuthError(f"could not reach Etsy to refresh the token: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(
                f"Token refresh failed ({resp.status_code}): {resp.text[:300]}. "
                "If the refresh token expired, run: etsy-auto-print auth"
            )
        self.store_grant(resp.json())

    @staticmethod
    def _post_token(url, *, json, headers):
        return requests.post(url, json=json, headers=headers, timeout=30)

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))
        self.path.chmod(0o600)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the single OAuth redirect and stashes its query params."""

    result: dict = {}

    def do_GET(self):  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        _CallbackHandler.result = dict(urllib.parse.parse_qsl(parsed.query))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>Authorized \xe2\x9c\x94</h2>You can close this tab and "
            b"return to the terminal."
        )

    def log_message(self, *args):  # silence request logging
        pass


def authorize(config: Config, open_browser: bool = True) -> TokenStore:
    """Run the interactive PKCE consent flow and persist the tokens."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": config.keystring,
        "redirect_uri": config.redirect_uri,
        "scope": OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{CONNECT_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("localhost", config.redirect_port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Open this URL in your browser and approve access:\n")
    print(f"  {url}\n")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    print(f"Waiting for the redirect on {config.redirect_uri} ...")
    thread.join(timeout=600)
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise AuthError("Timed out waiting for the OAuth redirect (10 minutes).")
    if "error" in result:
        raise AuthError(f"Etsy returned an error: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise AuthError("OAuth state mismatch — possible CSRF; try again.")

    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "client_id": config.keystring,
            "redirect_uri": config.redirect_uri,
            "code": result["code"],
            "code_verifier": verifier,
        },
        headers={"x-api-key": config.api_key},
        timeout=30,
    )
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")

    store = TokenStore(config)
    store.store_grant(resp.json())
    print("Authorization complete — tokens saved to", store.path)
    return store
