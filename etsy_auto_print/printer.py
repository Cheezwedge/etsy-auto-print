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


class FilePrinter(Printer):
    def __init__(self, outbox: Path):
        self.outbox = outbox
        outbox.mkdir(parents=True, exist_ok=True)

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


def get_printer(config: Config) -> Printer:
    if config.printer_backend == "cups":
        return CupsPrinter(config.cups_queue)
    return FilePrinter(config.outbox)
