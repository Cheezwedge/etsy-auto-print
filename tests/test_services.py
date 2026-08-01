"""Buying the service the buyer actually paid for."""

import pytest

from etsy_auto_print.config import normalize_service
from etsy_auto_print.labels import LabelError, pick_rate, required_service
from etsy_auto_print.pipeline import advance_order
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.store import Store

from .test_labels import RATES, FakeShippo, label_config, make_labeler


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


@pytest.fixture
def printer(tmp_path):
    return FilePrinter(tmp_path / "outbox")


def with_shipping(receipt, upgrade=None, method=None):
    for txn in receipt["transactions"]:
        txn["shipping_upgrade"] = upgrade
        txn["shipping_method"] = method
    return receipt


# --- name matching --------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    ["USPS Priority Mail Express", "usps priority mail express",
     "USPS  Priority   Mail  Express ", "USPS Priority-Mail Express"],
)
def test_service_names_match_however_they_were_typed(typed):
    # Etsy service names are free text on a shipping profile.
    assert normalize_service(typed) in label_config().service_map


def test_all_six_etsy_services_are_mapped():
    for name in [
        "USPS Priority Mail Express",
        "USPS Priority Mail",
        "USPS First-Class Mail",
        "USPS Priority Mail International",
        "USPS Priority Mail Express International",
        "Standard International",
    ]:
        assert label_config().service_map.get(normalize_service(name)), name


# --- which service an order requires --------------------------------------


def test_no_shipping_fields_means_no_constraint(receipt):
    assert required_service(receipt, label_config()) is None


def test_upgrade_selects_that_service(receipt):
    with_shipping(receipt, upgrade="USPS Priority Mail Express")
    assert required_service(receipt, label_config()) == "usps_priority_express"


def test_unmapped_upgrade_holds_the_order(receipt):
    # The buyer paid for something specific; shipping it slower is not ours
    # to decide silently.
    with_shipping(receipt, upgrade="Overnight By Owl")
    with pytest.raises(LabelError, match="not in \\[labels.service_map\\]"):
        required_service(receipt, label_config())


def test_unmapped_upgrade_can_be_allowed_through(receipt):
    with_shipping(receipt, upgrade="Overnight By Owl")
    cfg = label_config(hold_unmapped_upgrade=False)
    assert required_service(receipt, cfg) is None


def test_known_method_is_honored_without_an_upgrade(receipt):
    with_shipping(receipt, method="USPS Priority Mail")
    assert required_service(receipt, label_config()) == "usps_priority"


def test_unknown_method_falls_back_to_cheapest(receipt):
    # shipping_method is populated on ordinary orders, so holding on an
    # unrecognised one would stop every order in the shop.
    with_shipping(receipt, method="My Standard Shipping")
    assert required_service(receipt, label_config()) is None


def test_upgrade_wins_over_method(receipt):
    with_shipping(receipt, upgrade="USPS Priority Mail Express", method="USPS Priority Mail")
    assert required_service(receipt, label_config()) == "usps_priority_express"


def test_conflicting_upgrades_hold(receipt):
    receipt["transactions"][0]["shipping_upgrade"] = "USPS Priority Mail"
    receipt["transactions"][1]["shipping_upgrade"] = "USPS Priority Mail Express"
    with pytest.raises(LabelError, match="different shipping upgrades"):
        required_service(receipt, label_config())


def test_custom_map_entry_overrides_the_default(receipt):
    with_shipping(receipt, upgrade="usps priority mail")
    cfg = label_config(
        service_map={**label_config().service_map, "usps priority mail": "usps_media_mail"}
    )
    assert required_service(receipt, cfg) == "usps_media_mail"


# --- rate selection -------------------------------------------------------


def test_pick_rate_honors_the_required_service():
    # r1 (Ground Advantage, 5.50) is cheapest; Priority is 8.10.
    assert pick_rate(RATES, ["USPS"])["object_id"] == "r1"
    assert pick_rate(RATES, ["USPS"], "usps_priority")["object_id"] == "r2"


def test_pick_rate_reports_a_service_the_carrier_did_not_quote():
    with pytest.raises(LabelError, match="did not quote it"):
        pick_rate(RATES, ["USPS"], "usps_priority_express")


# --- end to end -----------------------------------------------------------


def test_order_with_an_upgrade_buys_the_upgraded_service(store, printer, receipt):
    with_shipping(receipt, upgrade="USPS Priority Mail")
    store.register(receipt)
    advance_order(receipt, store, printer, make_labeler(store, printer, FakeShippo()))
    label = store.get_label(12345)
    assert label["service"] == "Priority Mail"
    assert label["amount"] == "8.10"  # not the cheaper Ground Advantage


def test_order_with_an_unknown_upgrade_is_held_before_buying(store, printer, receipt):
    with_shipping(receipt, upgrade="Same Day Teleport")
    store.register(receipt)
    client = FakeShippo()
    advance_order(receipt, store, printer, make_labeler(store, printer, client))
    row = store.get(12345)
    assert row["state"] == "held"
    assert "Same Day Teleport" in row["error"]
    assert client.buy_calls == 0
