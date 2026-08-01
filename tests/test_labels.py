import pytest

from etsy_auto_print.config import DEFAULT_SERVICE_MAP, LabelConfig
from etsy_auto_print.labels import (
    LabelError,
    Labeler,
    build_address_to,
    compute_parcel,
    pick_rate,
    required_service,
)
from etsy_auto_print.pipeline import advance_order
from etsy_auto_print.printer import FilePrinter
from etsy_auto_print.shippo import LiveTokenError, make_client
from etsy_auto_print.store import Store


def label_config(**overrides) -> LabelConfig:
    defaults = dict(
        enabled=True,
        token="shippo_test_abc",
        allow_live=False,
        file_type="PDF_4x6",
        ship_from={
            "name": "Shop",
            "street1": "1 Shop St",
            "city": "Portland",
            "state": "OR",
            "zip": "97201",
            "country": "US",
            "email": "shop@example.com",
        },
        parcels={"default": {"length_in": 10, "width_in": 7, "height_in": 4, "packaging_oz": 3}},
        item_parcels={},
        default_parcel="default",
        item_weights_oz={"STAND-WAL": 9.5, "": 1.0},
        allowed_providers=["USPS"],
        service_map=dict(DEFAULT_SERVICE_MAP),
        hold_unmapped_upgrade=True,
    )
    defaults.update(overrides)
    return LabelConfig(**defaults)


RATES = [
    {"object_id": "r1", "provider": "USPS", "amount": "5.50", "currency": "USD",
     "servicelevel": {"token": "usps_ground_advantage", "name": "Ground Advantage"}},
    {"object_id": "r2", "provider": "USPS", "amount": "8.10", "currency": "USD",
     "servicelevel": {"token": "usps_priority", "name": "Priority Mail"}},
    {"object_id": "r3", "provider": "UPS", "amount": "4.99", "currency": "USD",
     "servicelevel": {"token": "ups_ground", "name": "UPS Ground"}},
]


class FakeShippo:
    is_test = True

    def __init__(self, fail_buy=False, invalid_address=False):
        self.fail_buy = fail_buy
        self.invalid_address = invalid_address
        self.buy_calls = 0

    def create_address(self, address, validate=True):
        return {
            **address,
            "validation_results": {"is_valid": not self.invalid_address, "messages": []},
        }

    def create_shipment(self, address_from, address_to, parcel):
        return {"rates": RATES}

    def buy_label(self, rate_object_id, file_type):
        self.buy_calls += 1
        if self.fail_buy:
            return {"status": "ERROR", "messages": [{"text": "carrier rejected"}]}
        return {
            "status": "SUCCESS",
            "object_id": "txn1",
            "tracking_number": "9400TEST",
            "tracking_url_provider": "https://tools.usps.com/track?9400TEST",
            "label_url": "https://example.com/label.pdf",
        }

    def download(self, url):
        return b"%PDF-fake-label"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "orders.db")


@pytest.fixture
def printer(tmp_path):
    return FilePrinter(tmp_path / "outbox")


def make_labeler(store, printer, client=None, **cfg):
    return Labeler(label_config(**cfg), store, printer, client or FakeShippo())


# --- pure helpers ---------------------------------------------------------


def test_compute_parcel_sums_weights(receipt):
    parcel = compute_parcel(receipt, label_config())
    # packaging 3 + stand 9.5*1 + stickers 1.0*3
    assert parcel["weight"] == 15.5
    assert parcel["mass_unit"] == "oz"


def test_compute_parcel_unmapped_sku_holds(receipt):
    cfg = label_config(item_weights_oz={})
    with pytest.raises(LabelError, match="no weight configured"):
        compute_parcel(receipt, cfg)


def test_compute_parcel_default_item_weight(receipt):
    cfg = label_config(
        item_weights_oz={},
        parcels={"default": {"length_in": 10, "width_in": 7, "height_in": 4,
                              "packaging_oz": 3, "default_item_oz": 2.0}},
    )
    assert compute_parcel(receipt, cfg)["weight"] == 3 + 2.0 * 4


def test_compute_parcel_picks_largest_preset_among_items_present(receipt):
    # receipt fixture has STAND-WAL (qty 1) and a blank-sku item (qty 3)
    cfg = label_config(
        parcels={
            "small": {"length_in": 6, "width_in": 4, "height_in": 2, "packaging_oz": 1},
            "large": {"length_in": 14, "width_in": 10, "height_in": 6, "packaging_oz": 5},
        },
        item_parcels={"STAND-WAL": "small", "": "large"},
        default_parcel=None,
        item_weights_oz={"STAND-WAL": 9.5, "": 1.0},
    )
    parcel = compute_parcel(receipt, cfg)
    # "large" has the bigger volume, so the whole order ships in it despite
    # only one line item mapping there — weight still sums every item.
    assert parcel["length"] == 14
    assert parcel["weight"] == 5 + 9.5 * 1 + 1.0 * 3


def test_compute_parcel_unmapped_sku_with_multiple_presets_holds(receipt):
    cfg = label_config(
        parcels={
            "small": {"length_in": 6, "width_in": 4, "height_in": 2, "packaging_oz": 1},
            "large": {"length_in": 14, "width_in": 10, "height_in": 6, "packaging_oz": 5},
        },
        item_parcels={"STAND-WAL": "small"},  # "" (sticker sku) left unmapped
        default_parcel=None,
    )
    with pytest.raises(LabelError, match="no box size configured"):
        compute_parcel(receipt, cfg)


def test_pick_rate_cheapest_within_allowed_providers():
    assert pick_rate(RATES, ["USPS"])["object_id"] == "r1"
    assert pick_rate(RATES, [])["object_id"] == "r3"  # no filter: cheapest overall
    with pytest.raises(LabelError, match="no rates"):
        pick_rate(RATES, ["FedEx"])


def test_build_address_requires_fields(receipt):
    del receipt["zip"]
    with pytest.raises(LabelError, match="zip"):
        build_address_to(receipt)


def test_live_token_refused_without_allow_live():
    with pytest.raises(LiveTokenError):
        make_client("shippo_live_xyz", allow_live=False)
    make_client("shippo_live_xyz", allow_live=True)  # explicit opt-in works
    make_client("shippo_test_xyz", allow_live=False)  # test always fine


# --- full pipeline --------------------------------------------------------


def test_full_flow_slip_then_label(store, printer, receipt, tmp_path):
    store.register(receipt)
    labeler = make_labeler(store, printer)
    assert advance_order(receipt, store, printer, labeler) is True
    row = store.get(12345)
    assert row["state"] == "label_printed"
    assert (tmp_path / "outbox" / "packing-slip-12345.txt").exists()
    assert (tmp_path / "outbox" / "label-12345.pdf").read_bytes() == b"%PDF-fake-label"
    label = store.get_label(12345)
    assert label["tracking_number"] == "9400TEST"
    assert label["is_test"] == 1


def test_label_never_bought_twice(store, printer, receipt):
    store.register(receipt)
    client = FakeShippo()
    labeler = make_labeler(store, printer, client)
    advance_order(receipt, store, printer, labeler)
    # Order remains in open-receipts until tracking is posted (phase 3);
    # re-running the pipeline must not re-purchase.
    advance_order(receipt, store, printer, labeler)
    assert client.buy_calls == 1


def test_failed_purchase_holds_order(store, printer, receipt):
    store.register(receipt)
    labeler = make_labeler(store, printer, FakeShippo(fail_buy=True))
    advance_order(receipt, store, printer, labeler)
    row = store.get(12345)
    assert row["state"] == "held"
    assert "carrier rejected" in row["error"]


def test_invalid_address_holds_before_purchase(store, printer, receipt):
    store.register(receipt)
    client = FakeShippo(invalid_address=True)
    labeler = make_labeler(store, printer, client)
    advance_order(receipt, store, printer, labeler)
    assert store.get(12345)["state"] == "held"
    assert client.buy_calls == 0


def test_unrecorded_attempt_blocks_repurchase(store, printer, receipt):
    """Crash between charge and record must not lead to a silent double-buy."""
    store.register(receipt)
    store.transition(12345, "slip_printed", "slip done")
    store.record_label_attempt(12345)  # simulates a crash mid-purchase
    client = FakeShippo()
    labeler = make_labeler(store, printer, client)
    advance_order(receipt, store, printer, labeler)
    row = store.get(12345)
    assert row["state"] == "held"
    assert "Shippo dashboard" in row["error"]
    assert client.buy_calls == 0


def test_weight_scales_with_quantity_but_box_does_not(receipt):
    # receipt: STAND-WAL x1 (9.5oz) + blank-sku x3 (1.0oz) = 12.5 + 3 packaging
    parcel = compute_parcel(receipt, label_config())
    assert parcel["weight"] == 15.5
    assert (parcel["length"], parcel["width"], parcel["height"]) == (10, 7, 4)


def test_quantity_over_box_capacity_holds(receipt):
    cfg = label_config(
        parcels={
            "default": {"length_in": 10, "width_in": 7, "height_in": 4,
                        "packaging_oz": 3, "max_items": 2},
        },
    )
    # receipt has 4 items total (1 stand + 3 stickers) against max_items = 2
    with pytest.raises(LabelError, match="4 items.*max_items = 2"):
        compute_parcel(receipt, cfg)


def test_capacity_error_suggests_a_bigger_box_that_fits(receipt):
    cfg = label_config(
        parcels={
            "small": {"length_in": 6, "width_in": 4, "height_in": 2,
                      "packaging_oz": 1, "max_items": 2},
            "jumbo": {"length_in": 20, "width_in": 16, "height_in": 12,
                      "packaging_oz": 8, "max_items": 20},
        },
        item_parcels={"STAND-WAL": "small", "": "small"},
        default_parcel=None,
    )
    with pytest.raises(LabelError, match="would fit: jumbo"):
        compute_parcel(receipt, cfg)


def test_max_items_absent_means_no_capacity_limit(receipt):
    cfg = label_config()  # no max_items on the preset
    assert compute_parcel(receipt, cfg)["weight"] == 15.5


def test_capacity_counts_quantities_not_line_items(receipt):
    # One line item, quantity 5, against a box that holds 3.
    receipt["transactions"] = [{"title": "Mug", "sku": "STAND-WAL", "quantity": 5}]
    cfg = label_config(
        parcels={"default": {"length_in": 10, "width_in": 7, "height_in": 4,
                             "packaging_oz": 3, "max_items": 3}},
    )
    with pytest.raises(LabelError, match="5 items"):
        compute_parcel(receipt, cfg)
