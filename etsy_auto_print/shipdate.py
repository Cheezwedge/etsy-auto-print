"""When to tell Etsy an order has shipped.

A label prints the moment an order arrives, but the parcel usually goes to
the post office later. Posting tracking at print time tells the buyer
"shipped" early, and Etsy computes its delivery estimate from that moment —
so the estimate is optimistic by however long the parcel sat on the shelf.

By default tracking is posted on the next business day after the label
printed, and the ship date sent to Etsy is that day. Two rails:

- never later than Etsy's own expected ship date for the order, because a
  late mark-as-shipped is what Etsy's seller metrics penalise; and
- a manual "shipped now" for days the parcel really does go out same-day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

IMMEDIATELY = "immediately"
NEXT_BUSINESS_DAY = "next_business_day"
# Wait for USPS to scan the parcel, but no later than Etsy's ship-by date.
# An order with no readable ship-by date falls back to NEXT_BUSINESS_DAY:
# waiting on a deadline it can't see could make the shop late.
CARRIER_SCAN = "carrier_scan"
POLICIES = (IMMEDIATELY, NEXT_BUSINESS_DAY, CARRIER_SCAN)

# Shippo tracking statuses that mean the carrier physically has the parcel.
# PRE_TRANSIT is only "label created"; UNKNOWN is nothing at all.
CARRIER_HAS_IT = frozenset({"TRANSIT", "DELIVERED", "RETURNED", "FAILURE"})


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth given weekday of a month (Mon=0); n=-1 for the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Federal rule: a Saturday holiday is observed Friday, Sunday on Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def postal_holidays(year: int) -> set[date]:
    """Days USPS doesn't collect or deliver: the eleven federal holidays."""
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4),
             date(year, 11, 11), date(year, 12, 25)]
    floating = [
        _nth_weekday(year, 1, 0, 3),    # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),    # Presidents' Day
        _nth_weekday(year, 5, 0, -1),   # Memorial Day
        _nth_weekday(year, 9, 0, 1),    # Labor Day
        _nth_weekday(year, 10, 0, 2),   # Columbus Day
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving
    ]
    holidays = {_observed(d) for d in fixed} | set(floating)
    # New Year's Day on a Saturday is observed on the previous year's Dec 31.
    if date(year + 1, 1, 1).weekday() == 5:
        holidays.add(date(year, 12, 31))
    return holidays


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in postal_holidays(day.year)


def next_business_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while not is_business_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def etsy_ship_by(receipt: dict | None) -> date | None:
    """The earliest date Etsy expects any line of this order to ship by."""
    stamps = [
        txn.get("expected_ship_date")
        for txn in (receipt or {}).get("transactions") or []
    ]
    stamps = [s for s in stamps if isinstance(s, (int, float)) and s > 0]
    return datetime.fromtimestamp(min(stamps)).date() if stamps else None


def ship_day(printed_at: float, receipt: dict | None, policy: str) -> date:
    """The day tracking goes to Etsy, in this machine's local time."""
    printed_on = datetime.fromtimestamp(printed_at).date()
    if policy == IMMEDIATELY:
        return printed_on
    deadline = etsy_ship_by(receipt)
    if policy == CARRIER_SCAN and deadline is not None:
        # The latest day. A USPS scan before then posts it sooner.
        return max(deadline, printed_on)
    planned = next_business_day(printed_on)
    # Etsy's ship-by date wins: marking shipped late costs seller metrics,
    # and an early estimate is the lesser problem.
    if deadline is not None and deadline < planned:
        return max(deadline, printed_on)
    return planned


def tracking_due(
    now: datetime, printed_at: float, receipt: dict | None, policy: str, post_hour: int
) -> bool:
    """Is it time to tell Etsy this order shipped?"""
    return due_reason(now, printed_at, receipt, policy, post_hour) is not None


def due_reason(
    now: datetime, printed_at: float, receipt: dict | None, policy: str, post_hour: int
) -> str | None:
    """Why it's time to tell Etsy, or None if it isn't yet.

    The reason goes into the order's history. Without it, "tracking posted
    two seconds after the label printed" can't be told apart from a bug.
    """
    if policy == IMMEDIATELY:
        return "mark_shipped = immediately"
    deadline = etsy_ship_by(receipt)
    if deadline is not None and now.date() >= deadline:
        # Etsy's ship-by day has arrived: post now, whatever the hour.
        return f"Etsy's ship-by date ({deadline:%a %b %-d}) has arrived"
    day = ship_day(printed_at, receipt, policy)
    if now.date() > day:
        return f"ship day ({day:%a %b %-d}) has passed"
    # On the ship day itself, wait for a sensible hour so the buyer's
    # "shipped" email doesn't arrive at 00:03.
    if now.date() == day and now.hour >= post_hour:
        return f"ship day ({day:%a %b %-d}), after {post_hour}:00"
    return None


def waits_for_carrier(policy: str, receipt: dict | None) -> bool:
    """Is this order waiting on a USPS scan, rather than a fixed day?"""
    return policy == CARRIER_SCAN and etsy_ship_by(receipt) is not None


def promise(policy: str, day: date, receipt: dict | None) -> str:
    """What the shop is told about when Etsy will hear, in one sentence."""
    if waits_for_carrier(policy, receipt):
        return (f"Etsy will be told when USPS scans it, or on {day:%a %b %-d} "
                "at the latest.")
    return f"Etsy will be told it shipped on {day:%a %b %-d}."
