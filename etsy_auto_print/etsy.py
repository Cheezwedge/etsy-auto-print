"""Thin client for the Etsy Open API v3 endpoints we use."""

from __future__ import annotations

import requests

from .auth import TokenStore
from .config import Config

API_BASE = "https://api.etsy.com/v3/application"


class EtsyApiError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        super().__init__(f"Etsy API error {status}: {body[:300]}")


class EtsyClient:
    def __init__(self, config: Config, tokens: TokenStore):
        self.config = config
        self.tokens = tokens
        self._shop_id: int | None = config.shop_id or tokens.get_cached_shop_id()

    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs) -> dict:
        headers = {
            "x-api-key": self.config.api_key,
            "Authorization": f"Bearer {self.tokens.access_token()}",
        }
        resp = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=30, **kwargs)
        if resp.status_code == 401 and retry_auth:
            self.tokens.force_refresh()
            return self._request(method, path, retry_auth=False, **kwargs)
        if resp.status_code >= 400:
            raise EtsyApiError(resp.status_code, resp.text)
        return resp.json()

    def get_me(self) -> dict:
        return self._request("GET", "/users/me")

    @property
    def shop_id(self) -> int:
        if self._shop_id is None:
            me = self.get_me()
            shop_id = me.get("shop_id")
            if not shop_id:
                raise EtsyApiError(0, "This Etsy account has no shop associated with it.")
            self._shop_id = int(shop_id)
            self.tokens.cache_shop_id(self._shop_id)
        return self._shop_id

    def get_open_receipts(self) -> list[dict]:
        """All paid, unshipped receipts, oldest first. Paginates past 100."""
        receipts: list[dict] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                f"/shops/{self.shop_id}/receipts",
                params={
                    "was_paid": "true",
                    "was_shipped": "false",
                    "limit": 100,
                    "offset": offset,
                    "sort_on": "created",
                    "sort_order": "asc",
                },
            )
            results = page.get("results", [])
            receipts.extend(results)
            offset += len(results)
            if len(results) < 100 or offset >= page.get("count", 0):
                return receipts

    def get_receipt(self, receipt_id: int) -> dict:
        return self._request("GET", f"/shops/{self.shop_id}/receipts/{receipt_id}")

    def create_receipt_shipment(
        self, receipt_id: int, tracking_code: str, carrier_name: str, send_bcc: bool = True
    ) -> dict:
        """Post tracking to Etsy: marks the order shipped and emails the buyer."""
        return self._request(
            "POST",
            f"/shops/{self.shop_id}/receipts/{receipt_id}/tracking",
            json={
                "tracking_code": tracking_code,
                "carrier_name": carrier_name,
                "send_bcc": send_bcc,
            },
        )
