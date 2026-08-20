"""Printing a label that arrived as a download.

International orders are bought on Etsy, because Etsy fills in the customs
form. The PDF then has to reach the same thermal printer, and the alternative
to this is scp plus a hand-written lp command on every one.
"""

import io

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
enabled = false
"""


@pytest.fixture
def app_dir(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    return tmp_path


@pytest.fixture
def client(app_dir):
    return create_app(app_dir / "config.toml").test_client()


def upload(client, name, data=b"%PDF-1.4 fake"):
    return client.post(
        "/print-label",
        data={"label": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_a_pdf_reaches_the_printer(client, app_dir):
    resp = upload(client, "etsy-label.pdf")
    assert "Sent etsy-label.pdf" in resp.get_data(as_text=True)
    assert (app_dir / "outbox" / "etsy-label.pdf").read_bytes() == b"%PDF-1.4 fake"


def test_the_upload_form_is_on_the_orders_page(client):
    text = client.get("/orders").get_data(as_text=True)
    assert 'action="/print-label"' in text
    assert 'enctype="multipart/form-data"' in text


def test_zpl_and_png_are_accepted(client, app_dir):
    upload(client, "label.zpl", b"^XA^XZ")
    upload(client, "label.png", b"\x89PNG\r\n")
    assert (app_dir / "outbox" / "label.zpl").exists()
    assert (app_dir / "outbox" / "label.png").exists()


def test_an_unsupported_type_is_refused_and_prints_nothing(client, app_dir):
    resp = upload(client, "invoice.docx", b"junk")
    assert "Cannot print" in resp.get_data(as_text=True)
    assert not (app_dir / "outbox" / "invoice.docx").exists()


def test_an_empty_file_is_refused(client):
    # A cancelled download would otherwise reach the printer as nothing and
    # look like a hardware fault.
    resp = upload(client, "label.pdf", b"")
    assert "is empty" in resp.get_data(as_text=True)


def test_choosing_no_file_says_so(client):
    resp = client.post("/print-label", data={}, follow_redirects=True)
    assert "No file chosen" in resp.get_data(as_text=True)


def test_a_hostile_filename_cannot_escape_the_outbox(client, app_dir):
    # The name becomes a path in the file backend and a job title in CUPS.
    upload(client, "../../etc/passwd.pdf")
    assert not (app_dir.parent / "etc").exists()
    assert list((app_dir / "outbox").glob("*.pdf"))


def test_a_print_failure_is_reported_not_a_traceback(client, monkeypatch):
    from etsy_auto_print import dashboard
    from etsy_auto_print.printer import PrintError

    class Broken:
        def print_bytes(self, *a):
            raise PrintError("lp failed for queue 'label'")

    monkeypatch.setattr(dashboard, "get_printer", lambda cfg: Broken())
    resp = upload(client, "label.pdf")
    assert "Print failed" in resp.get_data(as_text=True)


def test_a_broken_config_does_not_crash_the_upload(client, app_dir):
    (app_dir / "config.toml").write_text("not = = toml")
    resp = upload(client, "label.pdf")
    assert resp.status_code == 200


def test_uploads_are_capped(client):
    from etsy_auto_print.dashboard import MAX_UPLOAD_BYTES

    assert client.application.config["MAX_CONTENT_LENGTH"] == MAX_UPLOAD_BYTES


def test_the_page_says_the_size_limit(client):
    assert "20 MB" in client.get("/orders").get_data(as_text=True)


def test_upload_is_behind_the_password(app_dir):
    client = create_app(app_dir / "config.toml", password="hunter2").test_client()
    resp = client.post(
        "/print-label",
        data={"label": (io.BytesIO(b"%PDF"), "l.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# --- saying whether the file was converted ---------------------------------

ZPL_CONFIG = CONFIG.replace(
    '[labels]\nenabled = false',
    '[labels]\nenabled = false\nfile_type = "ZPLII"',
)


def test_the_flash_says_when_nothing_was_converted(client):
    # "Sent X to CUPS queue label" reads identically whether or not the file
    # was made printable — and that ambiguity is what hid a PDF going to a
    # raw ZPL queue and vanishing.
    resp = upload(client, "label.pdf")
    assert "unchanged" in resp.get_data(as_text=True)


def test_the_flash_says_when_it_did_convert(app_dir, monkeypatch):
    from etsy_auto_print import printer as printer_module

    (app_dir / "config.toml").write_text(ZPL_CONFIG)
    monkeypatch.setattr(printer_module, "pdf_to_zpl", lambda data: b"^XA^XZ",
                        raising=False)
    monkeypatch.setattr(
        "etsy_auto_print.pdf2zpl.pdf_to_zpl", lambda data, **kw: b"^XA^XZ"
    )
    client = create_app(app_dir / "config.toml").test_client()
    resp = upload(client, "label.pdf")
    text = resp.get_data(as_text=True)
    assert "converted PDF to ZPL" in text


def test_a_zpl_upload_is_never_described_as_converted(client):
    resp = upload(client, "label.zpl", b"^XA^XZ")
    assert "converted" not in resp.get_data(as_text=True)
