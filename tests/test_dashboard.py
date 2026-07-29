import pytest

pytest.importorskip("flask")

from etsy_auto_print.dashboard import create_app  # noqa: E402

CONFIG = """
[etsy]
keystring = "k"
shared_secret = "s"

[printer]
backend = "file"
outbox = "outbox"

[labels]
enabled = true
shippo_token = "shippo_test_fake"
items_csv = "items.csv"
default_parcel = "medium"

[labels.ship_from]
name = "Shop"
street1 = "1 St"
city = "Portland"
state = "OR"
zip = "97201"
country = "US"
email = "shop@example.com"

[labels.parcels.small]
length_in = 6.0
width_in = 4.0
height_in = 2.0
packaging_oz = 1.0

[labels.parcels.medium]
length_in = 10.0
width_in = 7.0
height_in = 4.0
packaging_oz = 3.0
"""

ITEMS_CSV = "sku,weight_oz,parcel,notes\nMUG-1,14,medium,best seller\n"


@pytest.fixture
def app_dir(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / "items.csv").write_text(ITEMS_CSV)
    return tmp_path


@pytest.fixture
def client(app_dir):
    return create_app(app_dir / "config.toml").test_client()


@pytest.mark.parametrize("route", ["/", "/orders", "/items", "/config", "/logs"])
def test_pages_render_real_html(client, route):
    resp = client.get(route)
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert text.strip().startswith("<!doctype html>")
    # Regression: nested templates were double-escaped, so pages rendered as
    # visible HTML source instead of a page.
    assert "&lt;div" not in text


def test_products_page_lists_csv_rows_and_offers_parcels(client):
    text = client.get("/items").get_data(as_text=True)
    assert "MUG-1" in text
    assert 'value="medium"' in text and 'value="small"' in text


def test_invalid_toml_is_not_saved(client, app_dir):
    before = (app_dir / "config.toml").read_text()
    resp = client.post("/config", data={"text": "not = = valid"}, follow_redirects=True)
    assert (app_dir / "config.toml").read_text() == before
    assert "Not saved" in resp.get_data(as_text=True)


def test_semantically_invalid_config_is_not_saved(client, app_dir):
    before = (app_dir / "config.toml").read_text()
    broken = before.replace('email = "shop@example.com"', "")
    client.post("/config", data={"text": broken}, follow_redirects=True)
    assert (app_dir / "config.toml").read_text() == before


def test_valid_config_saves_with_backup(client, app_dir):
    # Regression: validation ran in a temp dir, so relative paths such as
    # items_csv could never resolve and no config was ever savable.
    before = (app_dir / "config.toml").read_text()
    client.post(
        "/config", data={"text": before + "\n[poll]\ninterval_seconds = 90\n"},
        follow_redirects=True,
    )
    assert "interval_seconds = 90" in (app_dir / "config.toml").read_text()
    assert (app_dir / "config.toml.bak").read_text() == before
    assert not list(app_dir.glob(".validate-*")), "validation probe file leaked"


def test_products_save_roundtrip_drops_blank_rows(client, app_dir):
    client.post(
        "/items",
        data={
            "sku": ["NEW-1", "", "MUG-1"],
            "weight_oz": ["3.5", "", "14"],
            "parcel": ["small", "", "medium"],
            "notes": ["added", "", ""],
        },
        follow_redirects=True,
    )
    saved = (app_dir / "items.csv").read_text()
    assert "NEW-1,3.5,small,added" in saved
    assert saved.count("\n") == 3  # header + 2 real rows


def test_products_reject_non_numeric_weight(client, app_dir):
    before = (app_dir / "items.csv").read_text()
    resp = client.post(
        "/items",
        data={"sku": ["X"], "weight_oz": ["heavy"], "parcel": [""], "notes": [""]},
        follow_redirects=True,
    )
    assert "not a number" in resp.get_data(as_text=True)
    assert (app_dir / "items.csv").read_text() == before


def test_password_gates_every_page(app_dir):
    client = create_app(app_dir / "config.toml", password="hunter2").test_client()
    assert client.get("/", follow_redirects=False).status_code == 302
    client.post("/login", data={"password": "wrong"})
    assert client.get("/", follow_redirects=False).status_code == 302
    client.post("/login", data={"password": "hunter2"})
    assert client.get("/").status_code == 200
