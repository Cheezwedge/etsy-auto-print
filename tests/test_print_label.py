"""`print-label` — printing a label this program didn't buy.

International orders are bought through Etsy, because Etsy fills in the
customs form from the order and this program can't. The resulting PDF still
has to come out of the same thermal printer as everything else, and doing
that by hand means remembering the queue name and the raw/rendered flag.
"""

import argparse

import pytest

from etsy_auto_print import cli
from etsy_auto_print.config import load_config

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
def config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    return load_config(tmp_path / "config.toml")


def run(config, path, copies=1):
    return cli.cmd_print_label(
        config, argparse.Namespace(path=str(path), copies=copies)
    )


def test_a_pdf_reaches_the_printer(config, tmp_path, capsys):
    label = tmp_path / "etsy-label.pdf"
    label.write_bytes(b"%PDF-1.4 fake")
    assert run(config, label) == 0
    assert (tmp_path / "outbox" / "etsy-label.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert "etsy-label.pdf ->" in capsys.readouterr().out


def test_zpl_is_printed_as_zpl(config, tmp_path):
    label = tmp_path / "label.zpl"
    label.write_bytes(b"^XA^FO10,10^FDhi^FS^XZ")
    assert run(config, label) == 0
    assert (tmp_path / "outbox" / "label.zpl").exists()


def test_copies_print_more_than_once(config, tmp_path, capsys):
    label = tmp_path / "l.pdf"
    label.write_bytes(b"%PDF")
    run(config, label, copies=3)
    assert capsys.readouterr().out.count("->") == 3


def test_a_missing_file_is_reported_not_crashed(config, tmp_path, capsys):
    assert run(config, tmp_path / "nope.pdf") == 1
    assert "No such file" in capsys.readouterr().err


def test_an_empty_file_is_refused(config, tmp_path, capsys):
    # A truncated download would otherwise feed nothing to the printer and
    # look like a printer fault.
    label = tmp_path / "l.pdf"
    label.write_bytes(b"")
    assert run(config, label) == 1
    assert "is empty" in capsys.readouterr().err


def test_an_unsupported_type_names_what_is_supported(config, tmp_path, capsys):
    doc = tmp_path / "label.docx"
    doc.write_bytes(b"x")
    assert run(config, doc) == 1
    err = capsys.readouterr().err
    assert "pdf" in err and "zpl" in err


def test_a_file_with_no_extension_is_refused_clearly(config, tmp_path, capsys):
    doc = tmp_path / "label"
    doc.write_bytes(b"x")
    assert run(config, doc) == 1
    assert "no extension" in capsys.readouterr().err


def test_a_print_failure_is_reported_not_raised(config, tmp_path, monkeypatch, capsys):
    from etsy_auto_print.printer import PrintError

    class Broken:
        def print_bytes(self, *a):
            raise PrintError("lp failed for queue 'label'")

    monkeypatch.setattr(cli, "get_printer", lambda cfg: Broken())
    label = tmp_path / "l.pdf"
    label.write_bytes(b"%PDF")
    assert run(config, label) == 1
    assert "Print failed" in capsys.readouterr().err


def test_it_goes_to_the_label_queue_not_the_slip_queue(tmp_path, monkeypatch):
    # SplitPrinter sends print_bytes to the label printer. A label routed to
    # the plain-paper slip queue would come out on the wrong device.
    from etsy_auto_print.printer import Printer, SplitPrinter

    went = {}

    class Slip(Printer):
        def print_bytes(self, name, data, ext):
            went["to"] = "slip"
            return "slip"

    class Label(Printer):
        def print_bytes(self, name, data, ext):
            went["to"] = "label"
            return "label"

    (tmp_path / "config.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    config = load_config(tmp_path / "config.toml")
    monkeypatch.setattr(cli, "get_printer", lambda cfg: SplitPrinter(Slip(), Label()))

    label = tmp_path / "l.pdf"
    label.write_bytes(b"%PDF")
    run(config, label)
    assert went["to"] == "label"
