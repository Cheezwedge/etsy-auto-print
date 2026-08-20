"""Printer backends behind one interface.

Phase 1 default is FilePrinter (no hardware needed): documents land in the
outbox directory. When a printer arrives, set printer.backend = "cups" and
printer.cups_queue in config.toml — nothing else changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config


class PrintError(Exception):
    pass


class Printer:
    def print_text(self, name: str, content: str) -> str:
        """Print a text document. Returns a human-readable destination."""
        raise NotImplementedError

    def print_bytes(self, name: str, data: bytes, ext: str) -> str:
        """Print a binary document (pdf/png/zpl). Returns the destination."""
        raise NotImplementedError

    def print_slip(self, name: str, text: str, zpl: bytes) -> str:
        """Print a packing slip, given both renderings of it.

        The caller can't know which form a given setup needs — a plain-paper
        queue wants the text, a lone label printer wants the ZPL — so it hands
        over both and the backend picks. Defaults to text.
        """
        return self.print_text(name, text)


class FilePrinter(Printer):
    def __init__(self, outbox: Path, slip_zpl: bool = False):
        self.outbox = outbox
        self.slip_zpl = slip_zpl
        outbox.mkdir(parents=True, exist_ok=True)

    def print_slip(self, name: str, text: str, zpl: bytes) -> str:
        # Mirror what the real printer would receive, so a dry run on the
        # desktop shows the same thing the Pi would produce.
        if self.slip_zpl:
            return self.print_bytes(name, zpl, "zpl")
        return self.print_text(name, text)

    def print_text(self, name: str, content: str) -> str:
        path = self.outbox / f"{name}.txt"
        path.write_text(content)
        return str(path)

    def print_bytes(self, name: str, data: bytes, ext: str) -> str:
        path = self.outbox / f"{name}.{ext}"
        path.write_bytes(data)
        return str(path)


class CupsPrinter(Printer):
    def __init__(self, queue: str):
        self.queue = queue

    def _lp(self, name: str, data: bytes, raw: bool) -> str:
        cmd = ["lp", "-d", self.queue, "-t", name]
        if raw:
            cmd += ["-o", "raw"]  # ZPL goes to the printer untouched
        result = subprocess.run(cmd + ["-"], input=data, capture_output=True)
        if result.returncode != 0:
            raise PrintError(
                f"lp failed for queue {self.queue!r}: {result.stderr.decode().strip()}"
            )
        return f"CUPS queue {self.queue} ({result.stdout.decode().strip()})"

    def print_text(self, name: str, content: str) -> str:
        return self._lp(name, content.encode(), raw=False)

    def print_bytes(self, name: str, data: bytes, ext: str) -> str:
        return self._lp(name, data, raw=(ext == "zpl"))


class SplitPrinter(Printer):
    """Slips and labels can go to different places: a raw ZPL label queue
    can't render plain text, so slips route to a second queue or the outbox."""

    def __init__(self, slip_printer: Printer, label_printer: Printer,
                 slip_zpl: bool = False):
        self.slip_printer = slip_printer
        self.label_printer = label_printer
        self.slip_zpl = slip_zpl

    def print_text(self, name: str, content: str) -> str:
        return self.slip_printer.print_text(name, content)

    def print_bytes(self, name: str, data: bytes, ext: str) -> str:
        return self.label_printer.print_bytes(name, data, ext)

    def print_slip(self, name: str, text: str, zpl: bytes) -> str:
        # ZPL slips go to the *label* queue on purpose: they come out of the
        # same printer, immediately before that order's shipping label, so the
        # pair is physically adjacent in the stack.
        if self.slip_zpl:
            return self.label_printer.print_bytes(name, zpl, "zpl")
        return self.slip_printer.print_text(name, text)


def prepare_for_label_queue(data: bytes, ext: str, config: Config) -> tuple[bytes, str]:
    """Convert an uploaded label if the label printer can't read it as-is.

    A ZPL printer is a raw CUPS queue: it accepts a PDF, prints nothing, and
    reports success — so this has to happen before the job is submitted, or
    the failure is invisible.

    Anything other than a PDF bound for a ZPL queue passes straight through.
    """
    if ext != "pdf" or config.labels.file_type != "ZPLII":
        return data, ext
    from .pdf2zpl import pdf_to_zpl

    return pdf_to_zpl(data), "zpl"


def get_printer(config: Config) -> Printer:
    slip_zpl = config.slip_format == "zpl"
    if config.printer_backend == "cups":
        label_printer = CupsPrinter(config.cups_queue)
        if config.slip_queue:
            slip_printer: Printer = CupsPrinter(config.slip_queue)
        else:
            # No separate slip printer: keep slips as files in the outbox.
            slip_printer = FilePrinter(config.outbox)
        return SplitPrinter(slip_printer, label_printer, slip_zpl=slip_zpl)
    return FilePrinter(config.outbox, slip_zpl=slip_zpl)
