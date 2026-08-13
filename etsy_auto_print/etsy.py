"""Thin client for the Etsy Open API v3 endpoints we use."""

from __future__ import annotations

import requests

from .auth import TokenStore
from .config import Config

API_BASE = "https://api.etsy.com/v3/application"

# Optional fields createReceiptShipment accepts alongside the tracking number.
# Guarded rather than passed through blindly: a typo here would be silently
# dropped by Etsy, and the shipment record would quietly lack the field.
SHIPMENT_EXTRAS = frozenset({
    "mail_class",
    "weight",
    "weight_units",
    "length",
    "width",
    "height",
    "dimension_units",
    "shipping_label_cost",
    "shipping_label_currency",
    "ship_date",
    "note_to_buyer",
    "ship_from_country",
    "ship_to_country",
    "customs_data",
    "duty_amount",
    "duty_currency",
    "incoterm",
})


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
        try:
            resp = requests.request(
                method, f"{API_BASE}{path}", headers=headers, timeout=30, **kwargs
            )
        except requests.RequestException as exc:
            # Status 0 marks "never reached Etsy", which callers treat as a
            # transient failure to retry rather than a rejected request.
            raise EtsyApiError(0, f"could not reach Etsy: {exc}") from exc
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

    def get_all_listings(self) -> tuple[list[dict], bool]:
        """Every listing including sold-out ones, if the token allows it.

        Returns (listings, complete). complete is False when we had to fall
        back to the public active-only endpoint, which omits sold_out
        listings entirely — the caller must then infer a sell-out from a
        listing disappearing rather than read it off the state.

        The private endpoint needs listings_r, which tokens issued before
        stock monitoring existed don't carry. Falling back keeps a live shop
        working instead of erroring on a scope it was never granted.
        """
        listings: list[dict] = []
        offset = 0
        while True:
            try:
                page = self._request(
                    "GET",
                    f"/shops/{self.shop_id}/listings",
                    params={"limit": 100, "offset": offset,
                            "state": "active,sold_out"},
                )
            except EtsyApiError as exc:
                if exc.status in (401, 403) and not listings:
                    return self.get_active_listings(), False
                raise
            results = page.get("results", [])
            listings.extend(results)
            offset += len(results)
            if len(results) < 100 or offset >= page.get("count", 0):
                return listings, True

    def get_active_listings(self) -> list[dict]:
        """Every listing currently buyable, oldest first. Paginates past 100.

        Used to answer the question worth asking before the first real order:
        does every listing someone can buy carry a SKU this program has a
        weight for? An order for a listing with no SKU holds, and finding
        that out from a live order costs a buyer their wait.
        """
        listings: list[dict] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                f"/shops/{self.shop_id}/listings/active",
                params={"limit": 100, "offset": offset},
            )
            results = page.get("results", [])
            listings.extend(results)
            offset += len(results)
            if len(results) < 100 or offset >= page.get("count", 0):
                return listings

    def get_recent_receipts(self, limit: int = 5) -> list[dict]:
        """Most recent receipts regardless of paid/shipped state.

        Unlike get_open_receipts (which drives the pipeline), this is for
        inspection: it includes already-shipped orders, so there is
        something to look at even when nothing is awaiting fulfillment.
        """
        page = self._request(
            "GET",
            f"/shops/{self.shop_id}/receipts",
            params={"limit": limit, "sort_on": "created", "sort_order": "desc"},
        )
        return page.get("results", [])

    def create_receipt_shipment(
        self,
        receipt_id: int,
        tracking_code: str,
        carrier_name: str,
        send_bcc: bool = True,
        **extras,
    ) -> dict:
        """Post tracking to Etsy: marks the order shipped and emails the buyer.

        Only tracking_code and carrier_name are required. Etsy accepts a dozen
        optional shipment details (see SHIPMENT_EXTRAS) which it uses to give
        buyers richer, faster tracking updates; pass whatever is known and
        omit the rest.
        """
        unknown = set(extras) - SHIPMENT_EXTRAS
        if unknown:
            raise TypeError(
                f"create_receipt_shipment got field(s) Etsy does not accept: "
                f"{', '.join(sorted(unknown))}"
            )
        body = {
            "tracking_code": tracking_code,
            "carrier_name": carrier_name,
            "send_bcc": send_bcc,
        }
        body.update({k: v for k, v in extras.items() if v is not None})
        return self._request(
            "POST", f"/shops/{self.shop_id}/receipts/{receipt_id}/tracking", json=body
        )

    def token_scopes(self) -> list[str]:
        """The scopes actually granted to the stored token.

        Lets a scope problem be reported as such instead of surfacing later as
        a mystery 403 on whichever endpoint happens to need it.
        """
        data = self._request(
            "POST", "/scopes", data={"token": self.tokens.access_token()}
        )
        return _extract_scopes(data)


def _extract_scopes(data) -> list[str]:
    """Pull the scope list out of POST /scopes.

    Etsy documents the response as an opaque object, so accept the shapes it
    could plausibly be rather than guessing one and breaking on the others.
    """
    if isinstance(data, list):
        return [str(item) for item in data]
    if not isinstance(data, dict):
        return []
    for key in ("scopes", "results", "scope"):
        value = data.get(key)
        if isinstance(value, str):
            return value.split()
        if isinstance(value, list):
            return [str(item) for item in value]
    for value in data.values():
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
    return []


def ping(api_key: str, timeout: int = 15) -> requests.Response:
    """Call Etsy's unauthenticated health endpoint.

    Needs only the x-api-key header, which separates "Etsy is down" and "our
    app credentials are wrong" from "our OAuth token went bad".
    """
    return requests.get(
        f"{API_BASE}/openapi-ping", headers={"x-api-key": api_key}, timeout=timeout
    )
