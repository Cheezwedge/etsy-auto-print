"""Rasterise a PDF label into ZPL, for printers that only speak ZPL.

Labels bought outside this program arrive as PDFs — Etsy's international
labels especially, since Etsy fills in the customs form and we can't. A ZPL
thermal printer is normally a *raw* CUPS queue, so `lp` accepts the PDF,
hands the bytes straight to the printer, and the printer prints nothing.
The job even reports success, which makes it look like a hardware fault.

The conversion runs through pdftoppm's `-mono` output, which is PBM P4:
one bit per pixel, MSB first, rows padded to a byte boundary, 1 = black.
That is byte-for-byte what ZPL's ^GFA graphic field wants, so once the
header is parsed the pixel data needs no transformation at all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 203 dpi is the standard resolution of a 4x6 desktop label printer.
DEFAULT_DPI = 203
DEFAULT_WIDTH_IN = 4.0


class PdfConvertError(Exception):
    pass


def _require_pdftoppm() -> str:
    path = shutil.which("pdftoppm")
    if not path:
        raise PdfConvertError(
            "pdftoppm is not installed, so a PDF can't be converted for a ZPL "
            "printer. Install it with:  sudo apt install poppler-utils"
        )
    return path


def render_pdf_to_pbm(pdf: bytes, width_px: int, page: int = 1) -> bytes:
    """First page of a PDF as a 1-bit PBM, scaled to width_px wide."""
    tool = _require_pdftoppm()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "label.pdf"
        source.write_bytes(pdf)
        result = subprocess.run(
            [
                tool, "-mono", "-f", str(page), "-l", str(page),
                # Width fixed to the label, height follows the aspect ratio:
                # squashing a label to a fixed height would distort the
                # barcode, and an unscannable barcode is worse than no label.
                "-scale-to-x", str(width_px), "-scale-to-y", "-1",
                "-singlefile", str(source), str(Path(tmp) / "out"),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise PdfConvertError(
                f"pdftoppm failed: {result.stderr.decode(errors='replace')[:300]}"
            )
        rendered = Path(tmp) / "out.pbm"
        if not rendered.exists():
            raise PdfConvertError("pdftoppm produced no image")
        return rendered.read_bytes()


def _parse_pbm(pbm: bytes) -> tuple[int, int, bytes]:
    """(width, height, packed rows) from a binary PBM (P4)."""
    if not pbm.startswith(b"P4"):
        raise PdfConvertError("expected a binary PBM (P4) image")

    # Header tokens are whitespace-separated and '#' comments may appear
    # anywhere in it, including between the dimensions.
    position = 2
    numbers: list[int] = []
    while len(numbers) < 2:
        match = re.compile(rb"\s*(?:#[^\n]*\n\s*)*(\d+)").match(pbm, position)
        if not match:
            raise PdfConvertError("malformed PBM header")
        numbers.append(int(match.group(1)))
        position = match.end()
    # Exactly one whitespace character follows the height, then pixel data.
    data = pbm[position + 1:]

    width, height = numbers
    expected = ((width + 7) // 8) * height
    if len(data) < expected:
        raise PdfConvertError(
            f"PBM is truncated: expected {expected} bytes of pixels, got {len(data)}"
        )
    return width, height, data[:expected]


def pbm_to_zpl(pbm: bytes, dpi: int = DEFAULT_DPI) -> bytes:
    """Wrap a 1-bit PBM as a ZPL label containing one ^GFA graphic."""
    width, height, data = _parse_pbm(pbm)
    if not width or not height:
        raise PdfConvertError("PBM has no pixels")

    row_bytes = (width + 7) // 8
    total = row_bytes * height
    hex_data = data.hex().upper()

    return (
        "^XA\n"
        f"^PW{width}\n"
        f"^LL{height}\n"
        "^LH0,0\n"
        f"^FO0,0^GFA,{total},{total},{row_bytes},{hex_data}^FS\n"
        "^XZ\n"
    ).encode("ascii")


def pdf_to_zpl(
    pdf: bytes, dpi: int = DEFAULT_DPI, width_in: float = DEFAULT_WIDTH_IN
) -> bytes:
    """A PDF label as ZPL, ready for a raw label queue."""
    if not pdf.startswith(b"%PDF"):
        raise PdfConvertError("that doesn't look like a PDF")
    width_px = int(round(dpi * width_in))
    return pbm_to_zpl(render_pdf_to_pbm(pdf, width_px), dpi=dpi)
