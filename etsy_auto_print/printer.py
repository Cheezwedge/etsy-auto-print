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


class FilePrinter(Printer):
    def __init__(self, outbox: Path):
        self.outbox = outbox
        outbox.mkdir(parents=True, exist_ok=True)

    def print_text(self, name: str, content: str) -> str:
        path = self.outbox / f"{name}.txt"
        path.write_text(content)
        return str(path)


class CupsPrinter(Printer):
    def __init__(self, queue: str):
        self.queue = queue

    def print_text(self, name: str, content: str) -> str:
        result = subprocess.run(
            ["lp", "-d", self.queue, "-t", name, "-"],
            input=content.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            raise PrintError(
                f"lp failed for queue {self.queue!r}: {result.stderr.decode().strip()}"
            )
        return f"CUPS queue {self.queue} ({result.stdout.decode().strip()})"


def get_printer(config: Config) -> Printer:
    if config.printer_backend == "cups":
        return CupsPrinter(config.cups_queue)
    return FilePrinter(config.outbox)
