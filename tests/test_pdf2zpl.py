"""Turning a PDF label into something a ZPL printer can actually print.

A ZPL thermal printer is a raw CUPS queue. Handed a PDF it accepts the job,
reports a request id, prints nothing, and gives no error anywhere — so the
conversion has to happen before submission or the failure is invisible.
"""

import shutil

import pytest

from etsy_auto_print import pdf2zpl
from etsy_auto_print.pdf2zpl import PdfConvertError, pbm_to_zpl, pdf_to_zpl


def pbm(width, height, rows: bytes, header_extra=b"") -> bytes:
    return b"P4\n" + header_extra + f"{width} {height}\n".encode() + rows


# --- PBM to ZPL, the part that must be exact --------------------------------


def test_a_single_black_pixel_row_becomes_a_graphic_field():
    # 8x1, all black: one byte, 0xFF.
    zpl = pbm_to_zpl(pbm(8, 1, b"\xff")).decode()
    assert "^GFA,1,1,1,FF^FS" in zpl
    assert zpl.startswith("^XA")
    assert zpl.rstrip().endswith("^XZ")


def test_the_label_is_sized_from_the_image():
    zpl = pbm_to_zpl(pbm(812, 2, b"\x00" * 204)).decode()
    assert "^PW812" in zpl
    assert "^LL2" in zpl


def test_rows_are_padded_to_whole_bytes():
    # 9 pixels wide needs 2 bytes per row, not 1.125.
    zpl = pbm_to_zpl(pbm(9, 2, b"\xff\x80" * 2)).decode()
    assert "^GFA,4,4,2," in zpl


def test_pixel_data_passes_through_untouched():
    # PBM P4 and ZPL ^GFA agree: 1 bit per pixel, MSB first, 1 = black. Any
    # inversion or bit-reversal here prints a photographic negative.
    zpl = pbm_to_zpl(pbm(16, 1, b"\xa5\x3c")).decode()
    assert "A53C^FS" in zpl


def test_header_comments_are_tolerated():
    zpl = pbm_to_zpl(pbm(8, 1, b"\xff", header_extra=b"# made by pdftoppm\n"))
    assert b"^GFA,1,1,1,FF" in zpl


def test_a_truncated_image_is_refused(capsys):
    # Half an image would print as half a label — better to say so.
    with pytest.raises(PdfConvertError, match="truncated"):
        pbm_to_zpl(pbm(800, 100, b"\x00" * 10))


def test_a_non_pbm_is_refused():
    with pytest.raises(PdfConvertError, match="P4"):
        pbm_to_zpl(b"\x89PNG\r\n\x1a\n")


def test_an_empty_image_is_refused():
    with pytest.raises(PdfConvertError, match="no pixels"):
        pbm_to_zpl(pbm(0, 0, b""))


# --- the PDF end ------------------------------------------------------------


def test_something_that_is_not_a_pdf_is_caught_early():
    # Cheap check before shelling out, and a clearer message than pdftoppm's.
    with pytest.raises(PdfConvertError, match="doesn't look like a PDF"):
        pdf_to_zpl(b"^XA^XZ")


def test_a_missing_pdftoppm_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(pdf2zpl.shutil, "which", lambda name: None)
    with pytest.raises(PdfConvertError, match="apt install poppler-utils"):
        pdf_to_zpl(b"%PDF-1.4\n")


def test_the_render_is_scaled_to_the_label_width(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        # pdftoppm writes <prefix>.pbm; fake that.
        from pathlib import Path

        Path(cmd[-1] + ".pbm").write_bytes(pbm(8, 1, b"\xff"))
        return Result()

    monkeypatch.setattr(pdf2zpl.shutil, "which", lambda name: "/usr/bin/pdftoppm")
    monkeypatch.setattr(pdf2zpl.subprocess, "run", fake_run)
    pdf_to_zpl(b"%PDF-1.4\n", dpi=203, width_in=4.0)

    cmd = seen["cmd"]
    assert "-mono" in cmd                     # 1 bit per pixel, as ^GFA wants
    assert "812" in cmd                       # 203 dpi x 4 in
    # Height must follow the aspect ratio: squashing distorts the barcode.
    assert cmd[cmd.index("-scale-to-y") + 1] == "-1"


def test_only_the_first_page_is_rendered(monkeypatch):
    seen = {}

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        from pathlib import Path

        Path(cmd[-1] + ".pbm").write_bytes(pbm(8, 1, b"\xff"))
        return Result()

    monkeypatch.setattr(pdf2zpl.shutil, "which", lambda name: "/usr/bin/pdftoppm")
    monkeypatch.setattr(pdf2zpl.subprocess, "run", fake_run)
    pdf_to_zpl(b"%PDF-1.4\n")
    cmd = seen["cmd"]
    assert cmd[cmd.index("-f") + 1] == "1" and cmd[cmd.index("-l") + 1] == "1"


def test_a_pdftoppm_failure_surfaces_its_message(monkeypatch):
    class Result:
        returncode = 1
        stderr = b"Syntax Error: Couldn't find trailer dictionary"

    monkeypatch.setattr(pdf2zpl.shutil, "which", lambda name: "/usr/bin/pdftoppm")
    monkeypatch.setattr(pdf2zpl.subprocess, "run", lambda cmd, **kw: Result())
    with pytest.raises(PdfConvertError, match="trailer dictionary"):
        pdf_to_zpl(b"%PDF-1.4\n")


# --- against a real PDF, when the tool is here ------------------------------


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="poppler-utils not installed")
def test_a_real_pdf_converts_to_plausible_zpl():
    # A minimal one-page 4x6 PDF (288 x 432 pt).
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 288 432]"
        b"/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length 44>>stream\n"
        b"0 0 0 rg 20 20 200 100 re f\n"
        b"endstream endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    )
    zpl = pdf_to_zpl(pdf)
    assert zpl.startswith(b"^XA")
    assert b"^GFA," in zpl
    assert b"^PW812" in zpl
    # 4x6 at 203 dpi is 1218 rows tall, give or take a rounding pixel.
    length = int(zpl.split(b"^LL")[1].split(b"\n")[0])
    assert 1200 <= length <= 1230
