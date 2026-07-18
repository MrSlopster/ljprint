"""Byte-level tests for protocol.py — every command, decoder, and preset.

Run with:  uv run pytest tests/test_protocol.py -v
"""
from __future__ import annotations

import struct

from luckjingle import protocol


# ---------------------------------------------------------------------------
# Printer-control commands
# ---------------------------------------------------------------------------

def test_cmd_enable_printer_default():
    assert protocol.cmd_enable_printer(3) == bytes([0x10, 0xFF, 0xF1, 0x03])


def test_cmd_enable_printer_other_modes():
    assert protocol.cmd_enable_printer(0) == bytes([0x10, 0xFF, 0xF1, 0x00])
    assert protocol.cmd_enable_printer(255) == bytes([0x10, 0xFF, 0xF1, 0xFF])


def test_cmd_wakeup_is_twelve_zeros():
    assert protocol.cmd_wakeup() == bytes(12)


def test_cmd_stop_print_job():
    assert protocol.cmd_stop_print_job() == bytes([0x10, 0xFF, 0xF1, 0x45])


def test_cmd_recovery():
    assert protocol.cmd_recovery() == bytes([0x10, 0xFF, 0x04])


def test_cmd_set_density():
    assert protocol.cmd_set_density(0) == bytes([0x10, 0xFF, 0x10, 0x00, 0x00])
    assert protocol.cmd_set_density(2) == bytes([0x10, 0xFF, 0x10, 0x00, 0x02])


def test_cmd_set_speed():
    assert protocol.cmd_set_speed(3) == bytes([0x10, 0xFF, 0xC0, 0x03])


def test_cmd_set_paper_type():
    # Tattoo preset uses kind=1 mask=0x40
    assert protocol.cmd_set_paper_type(1, 0x40) == bytes([0x1F, 0x80, 0x01, 0x40])
    # Water-transfer preset uses kind=1 mask=0x60
    assert protocol.cmd_set_paper_type(1, 0x60) == bytes([0x1F, 0x80, 0x01, 0x60])


def test_cmd_set_heating_level():
    assert protocol.cmd_set_heating_level(5) == bytes([0x1F, 0x70, 0x01, 0x05])


def test_cmd_set_shuttime():
    # Little-endian minutes
    assert protocol.cmd_set_shuttime(30) == bytes([0x10, 0xFF, 0x12]) + struct.pack("<H", 30)
    assert protocol.cmd_set_shuttime(600) == bytes([0x10, 0xFF, 0x12]) + struct.pack("<H", 600)


def test_cmd_set_width():
    assert protocol.cmd_set_width(384) == bytes([0x10, 0xFF, 0x15]) + struct.pack("<H", 384)


def test_cmd_set_printer_mode():
    assert protocol.cmd_set_printer_mode(2) == bytes([0x10, 0xFF, 0x30, 0x27, 0x02])


def test_cmd_set_time():
    # 10 FF 53 4A flag yyHi yyLo MM DD hh mm ss
    cmd = protocol.cmd_set_time(0, 2026, 7, 18, 15, 30, 45)
    assert cmd == bytes([0x10, 0xFF, 0x53, 0x4A, 0x00,
                         0x07, 0xEA,   # 2026 = 0x07EA
                         0x07, 0x12, 0x0F, 0x1E, 0x2D])


def test_cmd_set_platform():
    assert protocol.cmd_set_platform() == bytes([0xFC, 0xFF, 0x00, 0x02, 0x45, 0x02, 0x00, 0x46])


def test_cmd_position_adjust():
    assert protocol.cmd_position_adjust(0x51) == bytes([0x1F, 0x11, 0x51])


def test_cmd_reverse_feed():
    assert protocol.cmd_reverse_feed(20) == bytes([0x1F, 0x11, 0x11, 0x14])


# ---------------------------------------------------------------------------
# Query commands
# ---------------------------------------------------------------------------

def test_cmd_status():
    assert protocol.cmd_status() == bytes([0x10, 0xFF, 0x40])


def test_cmd_battery():
    assert protocol.cmd_battery() == bytes([0x10, 0xFF, 0x50, 0xF1])


def test_cmd_get_model_version_sn():
    assert protocol.cmd_get_model() == bytes([0x10, 0xFF, 0x20, 0xF0])
    assert protocol.cmd_get_version() == bytes([0x10, 0xFF, 0x20, 0xF1])
    assert protocol.cmd_get_sn() == bytes([0x10, 0xFF, 0x20, 0xF2])


def test_cmd_get_density_speed_shuttime():
    assert protocol.cmd_get_density() == bytes([0x10, 0xFF, 0x11])
    assert protocol.cmd_get_speed() == bytes([0x10, 0xFF, 0x20, 0xA0])
    assert protocol.cmd_get_shuttime() == bytes([0x10, 0xFF, 0x13])


def test_cmd_get_device_boot_and_legacy_info():
    assert protocol.cmd_get_device_boot() == bytes([0x10, 0xFF, 0x20, 0xEF])
    assert protocol.cmd_get_info_legacy() == bytes([0x10, 0xFF, 0x70])


def test_cmd_get_time_format():
    assert protocol.cmd_get_time_format() == bytes([0x10, 0xFF, 0xB0])


# ---------------------------------------------------------------------------
# Print-data commands
# ---------------------------------------------------------------------------

def test_cmd_feed_dots_uses_esc_j():
    assert protocol.cmd_feed_dots(80) == bytes([0x1B, 0x4A, 0x50])
    assert protocol.cmd_feed_dots(120) == bytes([0x1B, 0x4A, 0x78])
    assert protocol.cmd_feed_dots(255) == bytes([0x1B, 0x4A, 0xFF])


def test_cmd_form_feed():
    assert protocol.cmd_form_feed() == bytes([0x1D, 0x0C])


def test_cmd_raster_image_header_matches_gs_v_0():
    pixels = bytes([0xFF, 0x00, 0x80, 0x00])
    out = protocol.cmd_raster_image(pixels, bytes_per_row=2, height_px=2)
    assert out[:8] == bytes([0x1D, 0x76, 0x30, 0x00]) + struct.pack("<HH", 2, 2)
    assert out[8:] == pixels


# ---------------------------------------------------------------------------
# Status decoder
# ---------------------------------------------------------------------------

def test_decode_status_idle():
    s = protocol.decode_status(0x00)
    assert not any(s.values())


def test_decode_status_out_of_paper():
    s = protocol.decode_status(0x04)
    assert s["out_of_paper"] is True
    assert s["printing"] is False


def test_decode_status_overheat_via_bit4():
    s = protocol.decode_status(0x10)
    assert s["overheat"] is True


def test_decode_status_overheat_via_bit6():
    s = protocol.decode_status(0x40)
    assert s["overheat"] is True


def test_decode_status_combined():
    # printing + low_battery + needs_recharge
    s = protocol.decode_status(0x01 | 0x08 | 0x20)
    assert s["printing"] is True
    assert s["low_battery"] is True
    assert s["needs_recharge"] is True


# ---------------------------------------------------------------------------
# Mode presets
# ---------------------------------------------------------------------------

def test_mode_presets_normal():
    p = protocol.get_preset("normal")
    assert p.name == "normal"
    assert p.paper_type_cmd is None
    assert p.end_line_dots == 80
    assert p.print_width == 384


def test_mode_presets_tattoo():
    p = protocol.get_preset("tattoo")
    assert p.paper_type_cmd == bytes([0x1F, 0x80, 0x01, 0x40])
    assert p.end_line_dots == 80


def test_mode_presets_water_transfer():
    p = protocol.get_preset("water-transfer")
    assert p.paper_type_cmd == bytes([0x1F, 0x80, 0x01, 0x60])


def test_mode_presets_a4():
    p = protocol.get_preset("a4")
    assert p.paper_type_cmd is None
    assert p.print_width == 832
    assert p.end_line_dots == 120


def test_mode_presets_enum_and_string_equivalent():
    assert protocol.get_preset(protocol.PrintMode.NORMAL) == protocol.get_preset("normal")


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------

def test_classify_ok():
    assert protocol.classify_response(b"OK") == "ok"


def test_classify_print_complete():
    assert protocol.classify_response(b"\xAA") == "print_complete"


def test_classify_ascii():
    assert protocol.classify_response(b"D1YW23900823") == "ascii:D1YW23900823"


def test_classify_tag_value():
    assert protocol.classify_response(b"\x00\x62") == "tag0x00=98"


def test_classify_label_paper_error():
    assert protocol.classify_response(protocol.LABEL_PAPER_ERROR_EVENT) == "label_paper_error"


def test_classify_single_byte():
    assert protocol.classify_response(b"\x00") == "byte:00"
