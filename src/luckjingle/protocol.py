"""LuckJingle printer — protocol-level command bytes.

Pure functions: byte builders, decoders, constants. No I/O, no asyncio, no BLE.
Mirrors the operations in `com.luckprinter.sdk_new.device.normal.base.BaseNormalDevice`.

See ../../PROTOCOL.md for the protocol reference this module encodes.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Constants (one place; CLI and Printer both consume)
# ---------------------------------------------------------------------------

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
RESPONSE_CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
CREDIT_CHAR_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"

DEFAULT_PACKET_SIZE = 20
DEFAULT_PRINT_WIDTH_PX = 384
A4_PRINT_WIDTH_PX = 832
DEFAULT_END_LINE_DOTS_NARROW = 80
DEFAULT_END_LINE_DOTS_WIDE = 120

# Environment variable name for the default printer MAC. The CLI reads this
# as a fallback when no `mac` positional is given.
DEFAULT_MAC_ENV = "LUCKJINGLE_PRINTER"


class PrintMode(str, Enum):
    """Mode presets matching BaseNormalDevice's per-printmode methods."""
    NORMAL = "normal"
    TATTOO = "tattoo"
    WATER_TRANSFER = "water-transfer"
    A4 = "a4"
    # `label` / sheet_label uses TSPL — different protocol, not supported here.


@dataclass(frozen=True)
class ModePreset:
    """What the Printer does before/after the raster payload for a given mode."""
    name: str
    paper_type_cmd: Optional[bytes]   # sent before raster; None = no paper-type change
    end_line_dots: int               # ESC J n sent after raster
    print_width: int                 # default rendering width


MODE_PRESETS: dict[PrintMode, ModePreset] = {
    PrintMode.NORMAL: ModePreset(
        name="normal",
        paper_type_cmd=None,
        end_line_dots=DEFAULT_END_LINE_DOTS_NARROW,
        print_width=DEFAULT_PRINT_WIDTH_PX,
    ),
    PrintMode.TATTOO: ModePreset(
        name="tattoo",
        # BaseNormalDevice.printTattooOnce: setPaperType(1, 64, null) -> 1F 80 01 40
        paper_type_cmd=bytes([0x1F, 0x80, 0x01, 0x40]),
        end_line_dots=DEFAULT_END_LINE_DOTS_NARROW,
        print_width=DEFAULT_PRINT_WIDTH_PX,
    ),
    PrintMode.WATER_TRANSFER: ModePreset(
        name="water-transfer",
        # BaseNormalDevice.printWaterTransferOnce: setPaperType(1, 96, null) -> 1F 80 01 60
        paper_type_cmd=bytes([0x1F, 0x80, 0x01, 0x60]),
        end_line_dots=DEFAULT_END_LINE_DOTS_NARROW,
        print_width=DEFAULT_PRINT_WIDTH_PX,
    ),
    PrintMode.A4: ModePreset(
        name="a4",
        paper_type_cmd=None,
        end_line_dots=DEFAULT_END_LINE_DOTS_WIDE,
        print_width=A4_PRINT_WIDTH_PX,
    ),
}


def get_preset(mode: PrintMode | str) -> ModePreset:
    if isinstance(mode, str):
        mode = PrintMode(mode)
    return MODE_PRESETS[mode]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _u16_le(value: int) -> bytes:
    return struct.pack("<H", value & 0xFFFF)


# ---------------------------------------------------------------------------
# Command builders — printer control (PROTOCOL.md §3.2)
# ---------------------------------------------------------------------------

def cmd_enable_printer(mode: int = 3) -> bytes:
    """Enable printer for printing. Default mode 3 per BaseNormalDevice.enablePrinterLuck."""
    return bytes([0x10, 0xFF, 0xF1, mode & 0xFF])


def cmd_wakeup() -> bytes:
    """12-byte wake-up pulse (BaseNormalDevice.printerWakeupLuck)."""
    return bytes([0x00] * 12)


def cmd_stop_print_job() -> bytes:
    """Stop / flush current print job (10 FF F1 45). Printer replies 0xAA or 'OK'."""
    return bytes([0x10, 0xFF, 0xF1, 0x45])


def cmd_recovery() -> bytes:
    """Reset / recovery (10 FF 04)."""
    return bytes([0x10, 0xFF, 0x04])


def cmd_set_density(level: int) -> bytes:
    """Set density (10 FF 10 00 n). Level range printer-specific (0–2 typical)."""
    return bytes([0x10, 0xFF, 0x10, 0x00, level & 0xFF])


def cmd_set_speed(speed: int) -> bytes:
    """Set print speed (10 FF C0 n)."""
    return bytes([0x10, 0xFF, 0xC0, speed & 0xFF])


def cmd_set_paper_type(kind: int, mask: int) -> bytes:
    """Set paper type (1F 80 kind mask). e.g. kind=1 mask=0x40 -> tattoo."""
    return bytes([0x1F, 0x80, kind & 0xFF, mask & 0xFF])


def cmd_set_heating_level(level: int) -> bytes:
    """Set heating level (1F 70 01 n)."""
    return bytes([0x1F, 0x70, 0x01, level & 0xFF])


def cmd_set_shuttime(minutes: int) -> bytes:
    """Set auto-shutdown timer in minutes (10 FF 12 nL nH, little-endian)."""
    return bytes([0x10, 0xFF, 0x12]) + _u16_le(minutes)


def cmd_set_width(pixels: int) -> bytes:
    """Set print width in dots (10 FF 15 nL nH)."""
    return bytes([0x10, 0xFF, 0x15]) + _u16_le(pixels)


def cmd_set_printer_mode(mode: int) -> bytes:
    """Set printer mode (10 FF 30 27 n)."""
    return bytes([0x10, 0xFF, 0x30, 0x27, mode & 0xFF])


def cmd_set_time(flag: int, year: int, month: int, day: int,
                 hour: int, minute: int, second: int) -> bytes:
    """Set RTC time (10 FF 53 4A flag yyHi yyLo MM DD hh mm ss)."""
    return bytes([
        0x10, 0xFF, 0x53, 0x4A, flag & 0xFF,
        (year >> 8) & 0xFF, year & 0xFF,
        month & 0xFF, day & 0xFF,
        hour & 0xFF, minute & 0xFF, second & 0xFF,
    ])


def cmd_set_platform() -> bytes:
    """Vendor platform identifier (FC FF 00 02 45 02 00 46). Sent once at app startup."""
    return bytes([0xFC, 0xFF, 0x00, 0x02, 0x45, 0x02, 0x00, 0x46])


def cmd_position_adjust(n: int) -> bytes:
    """Auto adjust paper position (1F 11 n)."""
    return bytes([0x1F, 0x11, n & 0xFF])


def cmd_reverse_feed(dots: int) -> bytes:
    """Reverse feed n dots (1F 11 11 n)."""
    return bytes([0x1F, 0x11, 0x11, dots & 0xFF])


# ---------------------------------------------------------------------------
# Command builders — queries (PROTOCOL.md §3.1)
# ---------------------------------------------------------------------------

def cmd_status() -> bytes:
    """Real-time status query (10 FF 40). Reply: 1 status byte."""
    return bytes([0x10, 0xFF, 0x40])


def cmd_battery() -> bytes:
    """Battery query (10 FF 50 F1). Reply: [tag, percent]."""
    return bytes([0x10, 0xFF, 0x50, 0xF1])


def cmd_get_density() -> bytes:
    return bytes([0x10, 0xFF, 0x11])


def cmd_get_speed() -> bytes:
    return bytes([0x10, 0xFF, 0x20, 0xA0])


def cmd_get_shuttime() -> bytes:
    return bytes([0x10, 0xFF, 0x13])


def cmd_get_time_format() -> bytes:
    return bytes([0x10, 0xFF, 0xB0])


def cmd_get_device_boot() -> bytes:
    return bytes([0x10, 0xFF, 0x20, 0xEF])


def cmd_get_model() -> bytes:
    return bytes([0x10, 0xFF, 0x20, 0xF0])


def cmd_get_version() -> bytes:
    return bytes([0x10, 0xFF, 0x20, 0xF1])


def cmd_get_sn() -> bytes:
    return bytes([0x10, 0xFF, 0x20, 0xF2])


def cmd_get_info_legacy() -> bytes:
    """Deprecated combined-info query (10 FF 70). Use specific getters instead."""
    return bytes([0x10, 0xFF, 0x70])


# ---------------------------------------------------------------------------
# Command builders — print data (PROTOCOL.md §3.3, §3.4)
# ---------------------------------------------------------------------------

def cmd_feed_dots(n: int) -> bytes:
    """ESC/POS paper feed: feed n dots after printing (1B 4A n)."""
    return bytes([0x1B, 0x4A, n & 0xFF])


def cmd_form_feed() -> bytes:
    """Page-mode form feed (1D 0C)."""
    return bytes([0x1D, 0x0C])


def cmd_raster_image(pixel_bytes: bytes, bytes_per_row: int, height_px: int) -> bytes:
    """ESC/POS raster bit image (GS v 0): 1D 76 30 00 xL xH yL yH <data>.

    Pixel bytes are MSB-first, 1 = black, row-padded to a byte boundary.
    """
    header = bytes([0x1D, 0x76, 0x30, 0x00]) + _u16_le(bytes_per_row) + _u16_le(height_px)
    return header + pixel_bytes


# ---------------------------------------------------------------------------
# Status byte decoder (PROTOCOL.md §3.1)
# ---------------------------------------------------------------------------

def decode_status(byte0: int) -> dict[str, bool]:
    """Decode the 1-byte status reply into named boolean fields."""
    return {
        "printing": bool(byte0 & 0x01),
        "power_on": bool(byte0 & 0x02),
        "out_of_paper": bool(byte0 & 0x04),
        "low_battery": bool(byte0 & 0x08),
        "overheat": bool(byte0 & 0x10) or bool(byte0 & 0x40),
        "needs_recharge": bool(byte0 & 0x20),
    }


# ---------------------------------------------------------------------------
# Async event identification
# ---------------------------------------------------------------------------

LABEL_PAPER_ERROR_EVENT = bytes([0xFC, 0xFF, 0x00, 0x02, 0x45, 0xFE, 0x01, 0xBB])


def classify_response(payload: bytes) -> str:
    """Classify a response payload received on ff01 for human-friendly logging."""
    if not payload:
        return "empty"
    if payload.startswith(b"OK"):
        return "ok"
    if payload[0] == 0xAA:
        return "print_complete"
    if payload == LABEL_PAPER_ERROR_EVENT:
        return "label_paper_error"
    if len(payload) == 1:
        return f"byte:{payload[0]:02x}"
    # 2-byte [tag, value]
    if len(payload) == 2:
        return f"tag0x{payload[0]:02x}={payload[1]}"
    # else: assume ASCII (SN / model / version / etc.)
    return f"ascii:{payload.decode(errors='replace').strip()}"
