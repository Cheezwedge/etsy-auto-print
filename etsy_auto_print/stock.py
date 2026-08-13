"""Noticing when a listing sells out.

A sold-out listing is the quietest possible failure. Nothing errors, no
order arrives, the pipeline has nothing to hold — the shop is simply closed
for that item until somebody happens to look at it. Cancelling an order
restores nothing: Etsy decremented the quantity when it was placed, and a
listing that was at 1 is now at 0 and marked sold out.

Alerts are edge-triggered against stored state, so a listing that stays out
of stock notifies once rather than every hour.
"""

from __future__ import annotations

from dataclasses import dataclass

OUT = "out"
LOW = "low"
OK = ""


@dataclass(frozen=True)
class Alert:
    listing_id: int
    title: str
    level: str          # OUT or LOW
    quantity: int | None
    reason: str

    @property
    def headline(self) -> str:
        return "out of stock" if self.level == OUT else "low stock"


def _quantity(listing: dict) -> int | None:
    value = listing.get("quantity")
    return value if isinstance(value, int) else None


def level_for(listing: dict, low_threshold: int) -> tuple[str, str]:
    """(level, reason) for one listing that Etsy still knows about."""
    if listing.get("state") == "sold_out":
        return OUT, "Etsy has marked it sold out"
    quantity = _quantity(listing)
    if quantity is None:
        # No number to judge by. Saying nothing beats inventing an alert.
        return OK, ""
    if quantity <= 0:
        return OUT, "quantity is 0"
    if quantity <= low_threshold:
        return LOW, f"only {quantity} left"
    return OK, ""


def evaluate(
    listings: list[dict],
    complete: bool,
    known: dict[int, str],
    low_threshold: int,
) -> tuple[list[Alert], dict[int, tuple[str, str, int | None]]]:
    """Work out what to shout about.

    `known` maps listing_id -> the level we last alerted at, so a listing
    that is still out of stock stays quiet. Returns the alerts to send and
    the full current state to store.

    `complete` False means the caller could only see active listings. A
    listing we have seen before and can no longer see has most likely sold
    out — that is exactly the case this exists to catch — but it could also
    have been deactivated or expired, so the wording stays honest.
    """
    current: dict[int, tuple[str, str, int | None]] = {}
    alerts: list[Alert] = []

    seen: set[int] = set()
    for listing in listings:
        listing_id = listing.get("listing_id")
        if not isinstance(listing_id, int):
            continue
        seen.add(listing_id)
        title = (listing.get("title") or "?")[:80]
        level, reason = level_for(listing, low_threshold)
        current[listing_id] = (level, title, _quantity(listing))
        if level != OK and known.get(listing_id, OK) != level:
            alerts.append(Alert(listing_id, title, level, _quantity(listing), reason))

    if not complete:
        # Fallback mode: infer from disappearance.
        for listing_id, previous in known.items():
            if listing_id in seen:
                continue
            title = "a listing"
            current[listing_id] = (OUT, title, 0)
            if previous != OUT:
                alerts.append(Alert(
                    listing_id, title, OUT, None,
                    "it is no longer listed as active — most likely sold out, "
                    "or else deactivated or expired",
                ))

    return alerts, current


def summarize(state: dict[int, tuple[str, str, int | None]]) -> tuple[int, int]:
    """(out_of_stock, low) counts, for the health check row."""
    levels = [level for level, _, _ in state.values()]
    return levels.count(OUT), levels.count(LOW)
