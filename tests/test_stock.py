"""Noticing a sold-out listing.

The failure this exists for: a listing with quantity 1, an order placed and
then cancelled, and Etsy leaving the listing at 0 and marked sold out. No
error, no held order, no notification — the shop was simply shut for that
item and nothing in the system had any reason to say so.
"""

import pytest

from etsy_auto_print import stock
from etsy_auto_print.pipeline import StockWatcher
from etsy_auto_print.store import Store


def listing(listing_id=1, quantity=10, state="active", title="Adapters"):
    return {"listing_id": listing_id, "quantity": quantity, "state": state,
            "title": title}


def evaluate(listings, known=None, complete=True, threshold=2):
    return stock.evaluate(listings, complete, known or {}, threshold)


# --- what counts as out of stock -------------------------------------------


def test_sold_out_state_is_out():
    alerts, _ = evaluate([listing(state="sold_out", quantity=0)])
    assert [a.level for a in alerts] == [stock.OUT]


def test_zero_quantity_is_out_even_if_the_state_still_says_active():
    alerts, _ = evaluate([listing(quantity=0)])
    assert alerts[0].level == stock.OUT
    assert "quantity is 0" in alerts[0].reason


def test_at_or_below_the_threshold_is_low():
    alerts, _ = evaluate([listing(quantity=2)], threshold=2)
    assert alerts[0].level == stock.LOW
    assert "only 2 left" in alerts[0].reason


def test_above_the_threshold_is_silent():
    alerts, _ = evaluate([listing(quantity=3)], threshold=2)
    assert alerts == []


def test_a_listing_with_no_quantity_field_invents_no_alert():
    # Better to say nothing than to alarm on a shape we don't understand.
    alerts, _ = evaluate([{"listing_id": 1, "title": "X", "state": "active"}])
    assert alerts == []


def test_a_threshold_of_zero_only_alerts_on_actually_out():
    assert evaluate([listing(quantity=1)], threshold=0)[0] == []
    assert evaluate([listing(quantity=0)], threshold=0)[0][0].level == stock.OUT


# --- alerting once, not every hour ------------------------------------------


def test_an_already_reported_stockout_stays_quiet():
    alerts, _ = evaluate([listing(quantity=0)], known={1: stock.OUT})
    assert alerts == []


def test_going_from_low_to_out_alerts_again():
    # The escalation matters: "running low" and "nobody can buy this" are
    # different problems with different urgency.
    alerts, _ = evaluate([listing(quantity=0)], known={1: stock.LOW})
    assert alerts[0].level == stock.OUT


def test_restocking_clears_the_alert_silently():
    alerts, state = evaluate([listing(quantity=50)], known={1: stock.OUT})
    assert alerts == []
    assert state[1][0] == stock.OK      # ...and would alert again next time


def test_a_restocked_listing_can_alert_a_second_time():
    _, state = evaluate([listing(quantity=50)], known={1: stock.OUT})
    known = {lid: level for lid, (level, _, _) in state.items()}
    alerts, _ = evaluate([listing(quantity=0)], known=known)
    assert alerts[0].level == stock.OUT


# --- without the listings_r scope -------------------------------------------


def test_a_listing_that_vanished_from_active_is_treated_as_sold_out():
    # Etsy drops sold-out listings from the public endpoint, so disappearance
    # is the only signal available without the scope.
    alerts, _ = evaluate([], known={7: stock.OK}, complete=False)
    assert alerts[0].level == stock.OUT
    assert "most likely sold out" in alerts[0].reason


def test_the_guess_is_worded_as_a_guess():
    alerts, _ = evaluate([], known={7: stock.OK}, complete=False)
    assert "deactivated or expired" in alerts[0].reason


def test_a_vanished_listing_is_not_inferred_when_the_view_is_complete():
    # With listings_r we can see sold-out listings directly, so a listing
    # that is genuinely gone was deleted — not a stockout to shout about.
    alerts, _ = evaluate([], known={7: stock.OK}, complete=True)
    assert alerts == []


def test_a_vanished_listing_only_alerts_once():
    alerts, _ = evaluate([], known={7: stock.OUT}, complete=False)
    assert alerts == []


# --- counts for the health check --------------------------------------------


def test_summarize_counts_out_and_low_separately():
    _, state = evaluate(
        [listing(1, quantity=0), listing(2, quantity=1), listing(3, quantity=99)]
    )
    assert stock.summarize(state) == (1, 1)


# --- the watcher inside the poll loop ---------------------------------------


class Config:
    enabled = True
    low_threshold = 2
    interval_minutes = 60


class FakeEtsy:
    def __init__(self, listings, complete=True, error=None):
        self.listings = listings
        self.complete = complete
        self.error = error
        self.calls = 0

    def get_all_listings(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.listings, self.complete


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


def test_the_watcher_notifies_and_remembers(store):
    notifier = Recorder()
    watcher = StockWatcher(Config(), store, notifier)
    client = FakeEtsy([listing(quantity=0)])

    watcher.check(client)
    assert len(notifier.sent) == 1
    assert "out of stock" in notifier.sent[0][0]

    # Second sweep, same situation: the shop owner already knows.
    watcher.check(client)
    assert len(notifier.sent) == 1


def test_the_state_survives_a_restart(store, tmp_path):
    StockWatcher(Config(), store, Recorder()).check(FakeEtsy([listing(quantity=0)]))
    # A restarted process must not re-alert on everything it finds.
    reopened = Store(tmp_path / "orders.db")
    notifier = Recorder()
    StockWatcher(Config(), reopened, notifier).check(FakeEtsy([listing(quantity=0)]))
    assert notifier.sent == []


def test_the_interval_is_respected(store):
    watcher = StockWatcher(Config(), store, Recorder())
    assert watcher.due(0) is True           # never run: due immediately
    watcher.check(FakeEtsy([]), now=1000.0)
    assert watcher.due(1000.0) is False
    assert watcher.due(1000.0 + 59 * 60) is False
    assert watcher.due(1000.0 + 61 * 60) is True


def test_disabled_never_runs(store):
    class Off(Config):
        enabled = False

    assert StockWatcher(Off(), store, Recorder()).due(10**9) is False


def test_an_etsy_failure_does_not_break_the_poll_loop(store):
    # This runs inside the loop that ships parcels. Stock is the least
    # important thing in it and must never be what takes it down.
    from etsy_auto_print.etsy import EtsyApiError

    watcher = StockWatcher(Config(), store, Recorder())
    assert watcher.check(FakeEtsy([], error=EtsyApiError(500, "boom"))) == []


def test_the_fallback_notification_says_how_to_get_real_numbers(store):
    notifier = Recorder()
    watcher = StockWatcher(Config(), store, notifier)
    store.save_stock_state({7: (stock.OK, "Adapters", 1)})
    watcher.check(FakeEtsy([], complete=False))
    assert "listings_r" in notifier.sent[0][1]
