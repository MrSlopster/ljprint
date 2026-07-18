"""LuckJingle thermal printer Python package.

Public API:
    from luckjingle import Printer, protocol, rendering
    from luckjingle.cli import main
"""
from importlib.metadata import PackageNotFoundError, version

from . import protocol, rendering
from .printer import Printer
from .transport import PrinterTransport

__all__ = ["Printer", "PrinterTransport", "protocol", "rendering"]


def _resolve_version() -> str:
    """Single-source the version from the installed package metadata
    (populated by hatchling from pyproject.toml's `[project] version`).
    Falls back to a sentinel for direct-from-source runs that bypass pip/uv
    (e.g. `python src/luckjingle/cli.py` with no install)."""
    try:
        return version("luckjingle-print")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()
