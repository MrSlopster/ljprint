"""Tests for the high-level Printer class using a mock transport.

Verifies the print-job call sequence per mode (enable -> wake ->
[paper type] -> raster -> feed -> stop) and that setters emit the right
bytes. No real BLE hardware required.

Run with:  uv run pytest tests/test_printer.py -v
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional, cast

import pytest

from luckjingle import protocol, rendering
from luckjingle.printer import Printer, PreflightError
from luckjingle.transport import PrinterTransport  # noqa: F401  (for typing only)

if TYPE_CHECKING:
    pass


class MockTransport:
    """Records every payload sent and lets tests pre-arm canned replies."""

    def __init__(self, replies: Optional[dict[bytes, bytes]] = None):
        self.sent: list[bytes] = []
        self._replies = replies or {}
        self.packet_size = 244
        self._event_listeners = []

    async def send(self, payload, *, wait_for=False, timeout=None):
        self.sent.append(payload)
        if wait_for:
            # Match the command by prefix; the printer reply scheme uses
            # distinct prefixes for each query.
            for prefix, reply in self._replies.items():
                if payload.startswith(prefix):
                    return reply
        return None

    async def exchange(self, payload, *, timeout=None):
        return await self.send(payload, wait_for=True, timeout=timeout)

    async def disconnect(self):
        pass

    def add_event_listener(self, listener):
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener):
        try:
            self._event_listeners.remove(listener)
        except ValueError:
            pass


@pytest.fixture
def printer_with_mock():
    transport = MockTransport(replies={
        bytes([0x10, 0xFF, 0x40]): b"\x00",                 # status: idle
        bytes([0x10, 0xFF, 0x50]): b"\x00\x62",             # battery: 98%
        bytes([0x10, 0xFF, 0x20, 0xF0]): b"D1Y-KD",         # model
        bytes([0x10, 0xFF, 0x20, 0xF1]): b"1.21",           # version
        bytes([0x10, 0xFF, 0x20, 0xF2]): b"D1YW23900823",   # SN
        bytes([0x10, 0xFF, 0xF1, 0x45]): b"\xAA",           # stop job ack
        bytes([0x1F, 0x80]): b"OK",                         # paper type ack
        bytes([0x10, 0xFF, 0x10]): b"OK",                   # density ack
    })
    p = Printer("AA:BB:CC:DD:EE:FF", transport=cast(PrinterTransport, transport))
    return p, transport


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_printer_uses_injected_transport(printer_with_mock):
    p, transport = printer_with_mock
    assert p.transport is transport


def test_transport_property_raises_when_disconnected():
    p = Printer("AA:BB:CC:DD:EE:FF")
    with pytest.raises(RuntimeError):
        _ = p.transport


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def test_get_status_returns_decoded_dict(printer_with_mock):
    p, transport = printer_with_mock
    status = asyncio.run(p.get_status())
    assert status["printing"] is False
    assert status["out_of_paper"] is False
    assert protocol.cmd_status() in transport.sent


def test_get_battery_returns_int(printer_with_mock):
    p, _ = printer_with_mock
    assert asyncio.run(p.get_battery()) == 98


def test_get_model_returns_ascii_string(printer_with_mock):
    p, _ = printer_with_mock
    assert asyncio.run(p.get_model()) == "D1Y-KD"


def test_get_sn_returns_ascii_string(printer_with_mock):
    p, _ = printer_with_mock
    assert asyncio.run(p.get_sn()) == "D1YW23900823"


def test_get_info_combines_all(printer_with_mock):
    p, _ = printer_with_mock
    info = asyncio.run(p.get_info())
    assert info["model"] == "D1Y-KD"
    assert info["firmware"] == "1.21"
    assert info["serial"] == "D1YW23900823"
    assert info["battery"] == 98
    assert info["status"]["printing"] is False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_set_density_sends_correct_bytes(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.set_density(2))
    assert protocol.cmd_set_density(2) in transport.sent


def test_set_paper_type_sends_correct_bytes(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.set_paper_type(1, 0x40))
    assert protocol.cmd_set_paper_type(1, 0x40) in transport.sent


def test_set_width_sends_correct_bytes(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.set_width(832))
    assert protocol.cmd_set_width(832) in transport.sent


# ---------------------------------------------------------------------------
# Print-job sequence
# ---------------------------------------------------------------------------

def test_print_text_normal_mode_full_sequence(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.print_text("Hi"))
    sent = transport.sent
    # First send is the preflight status query (10 FF 40); the print job
    # envelope follows after.
    assert sent[0] == protocol.cmd_status()
    job = sent[1:]
    # Expected order: enable, wake, raster, feed, stop
    assert job[0] == protocol.cmd_enable_printer(3)
    assert job[1] == protocol.cmd_wakeup()
    # Raster command starts with GS v 0
    assert job[2].startswith(bytes([0x1D, 0x76, 0x30, 0x00]))
    # Feed
    assert protocol.cmd_feed_dots(80) in job
    # Stop is last
    assert job[-1] == protocol.cmd_stop_print_job()


def test_print_text_tattoo_mode_sends_paper_type(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.print_text("tattoo test", mode="tattoo"))
    sent = transport.sent
    # Skip the preflight status query.
    job = sent[1:]
    assert job[0] == protocol.cmd_enable_printer(3)
    assert job[1] == protocol.cmd_wakeup()
    assert job[2] == protocol.cmd_set_paper_type(1, 0x40)
    # Raster after the paper-type setting
    assert job[3].startswith(bytes([0x1D, 0x76, 0x30, 0x00]))
    assert job[-1] == protocol.cmd_stop_print_job()


def test_print_text_a4_mode_wider_raster(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.print_text("wide", mode="a4"))
    sent = transport.sent
    # Find the raster command and verify its width dimension is 832 / 8 = 104 bytes.
    raster_cmd = next(b for b in sent if b.startswith(bytes([0x1D, 0x76, 0x30, 0x00])))
    bytes_per_row = raster_cmd[4] | (raster_cmd[5] << 8)
    assert bytes_per_row == 832 // 8
    # Feed is 120 for A4
    assert protocol.cmd_feed_dots(120) in sent


def test_print_qr_uses_raster_envelope(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.print_qr("data"))
    assert any(b.startswith(bytes([0x1D, 0x76, 0x30, 0x00])) for b in transport.sent)


def test_print_grid_uses_raster_envelope(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p.print_grid("ruled", rows=5))
    assert any(b.startswith(bytes([0x1D, 0x76, 0x30, 0x00])) for b in transport.sent)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def test_preflight_blocks_on_out_of_paper():
    transport = MockTransport(replies={
        bytes([0x10, 0xFF, 0x40]): b"\x04",  # out_of_paper bit set
    })
    p = Printer("AA:BB:CC:DD:EE:FF", transport=cast(PrinterTransport, transport))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(p.preflight())
    assert "out_of_paper" in str(exc.value)


def test_preflight_force_overrides_block():
    transport = MockTransport(replies={
        bytes([0x10, 0xFF, 0x40]): b"\x04",
        bytes([0x10, 0xFF, 0xF1, 0x45]): b"\xAA",
    })
    p = Printer("AA:BB:CC:DD:EE:FF", transport=cast(PrinterTransport, transport))
    # Should not raise.
    asyncio.run(p.print_text("force", force=True))
    assert any(b.startswith(bytes([0x1D, 0x76, 0x30, 0x00])) for b in transport.sent)


def test_preflight_blocks_on_overheat():
    transport = MockTransport(replies={
        bytes([0x10, 0xFF, 0x40]): b"\x10",  # bit 4 = overheat
    })
    p = Printer("AA:BB:CC:DD:EE:FF", transport=cast(PrinterTransport, transport))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(p.preflight())
    assert "overheat" in str(exc.value)


# ---------------------------------------------------------------------------
# Print pipeline internal helpers
# ---------------------------------------------------------------------------

def test_run_print_job_with_explicit_end_line_dots(printer_with_mock):
    p, transport = printer_with_mock
    asyncio.run(p._run_print_job(payload=protocol.cmd_raster_image(b"\x00" * 48, 48, 1),
                                  preset=protocol.get_preset("normal"),
                                  end_line_dots=200))
    assert protocol.cmd_feed_dots(200) in transport.sent
