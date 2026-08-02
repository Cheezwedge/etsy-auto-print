"""Thin client for the Shippo API (label purchase provider).

Kept deliberately small and isolated: if we ever switch to EasyPost, this
module and labels.py are the only things that change.
"""

from __future__ import annotations

import requests

BASE = "https://api.goshippo.com"

TEST_TOKEN_PREFIX = "shippo_test_"


class ShippoError(Exception):
    pass


class LiveTokenError(ShippoError):
    pass


def make_client(token: str, allow_live: bool = False) -> "ShippoClient":
    """Build a client, refusing live tokens unless explicitly allowed.

    This is the wall between development and real postage: with the default
    allow_live=false, only shippo_test_* tokens are accepted.
    """
    if not token.startswith(TEST_TOKEN_PREFIX) and not allow_live:
        raise LiveTokenError(
            "Shippo token is not a test token, and labels.allow_live is false. "
            "Use your test token (starts with 'shippo_test_'), or set "
            "allow_live = true in config.toml when you are ready for real postage."
        )
    return ShippoClient(token)


class ShippoClient:
    def __init__(self, token: str):
        self.token = token

    @property
    def is_test(self) -> bool:
        return self.token.startswith(TEST_TOKEN_PREFIX)

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            resp = requests.request(
                method,
                f"{BASE}{path}",
                json=payload,
                headers={"Authorization": f"ShippoToken {self.token}"},
                timeout=60,
            )
        except requests.RequestException as exc:
            # Never leak a requests exception: callers handle ShippoError by
            # holding the order, and a dropped connection should hold it, not
            # take the whole service down mid-poll.
            raise ShippoError(f"could not reach Shippo: {exc}") from exc
        if resp.status_code >= 400:
            raise ShippoError(f"Shippo API error {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def create_address(self, address: dict, validate: bool = True) -> dict:
        return self._request("POST", "/addresses/", {**address, "validate": validate})

    def create_shipment(self, address_from: dict, address_to: dict, parcel: dict) -> dict:
        return self._request(
            "POST",
            "/shipments/",
            {
                "address_from": address_from,
                "address_to": address_to,
                "parcels": [parcel],
                "async": False,
            },
        )

    def buy_label(self, rate_object_id: str, file_type: str) -> dict:
        return self._request(
            "POST",
            "/transactions/",
            {"rate": rate_object_id, "label_file_type": file_type, "async": False},
        )

    def download(self, url: str) -> bytes:
        try:
            resp = requests.get(url, timeout=60)
        except requests.RequestException as exc:
            raise ShippoError(f"label download failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ShippoError(f"Label download failed ({resp.status_code}): {url}")
        return resp.content
