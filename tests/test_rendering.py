"""Tests for rendering.py — raster encoding of all input types.

No real hardware or BLE involved. Run with:  uv run pytest tests/test_rendering.py -v
"""
from __future__ import annotations

import sys

import pytest
from PIL import Image

from luckjingle import protocol, rendering


# ---------------------------------------------------------------------------
# image_to_raster — the shared final step
# ---------------------------------------------------------------------------

def test_image_to_raster_dimensions_384px():
    img = Image.new("1", (384, 10), color=255)
    raster, bpr, height = rendering.image_to_raster(img)
    assert bpr == 48   # 384/8
    assert height == 10
    assert len(raster) == 48 * 10


def test_image_to_raster_non_byte_aligned_width():
    # 12 px wide -> 2 bytes per row, last 4 bits of byte 2 are padding
    img = Image.new("1", (12, 1), color=255)
    raster, bpr, height = rendering.image_to_raster(img)
    assert bpr == 2
    assert len(raster) == 2


def test_image_to_raster_black_pixel_is_bit_one():
    img = Image.new("1", (8, 1), color=255)  # all white
    img.putpixel((0, 0), 0)  # leftmost pixel = black
    raster, bpr, height = rendering.image_to_raster(img)
    assert bpr == 1
    # MSB-first: leftmost pixel is bit 7 -> 0x80
    assert raster[0] == 0x80


def test_image_to_raster_all_black():
    img = Image.new("1", (8, 1), color=0)  # all black
    raster, _, _ = rendering.image_to_raster(img)
    assert raster[0] == 0xFF


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def test_text_to_image_basic_dimensions():
    img = rendering.text_to_image("Hello", width=384, font_size=32)
    assert img.mode == "1"
    assert img.width == 384
    assert img.height > 0


def test_text_to_image_multiline():
    img = rendering.text_to_image("Line one\nLine two\nLine three", width=384, font_size=24)
    assert img.width == 384
    assert img.height > 10  # multiple lines


def test_text_to_image_wraps_long_lines():
    long = "word " * 200  # far wider than 384px
    img = rendering.text_to_image(long, width=384, font_size=32)
    # Should wrap; the height should be many line-heights.
    assert img.height > 32 * 5


def test_text_to_image_alignment_changes_pixel_offset():
    left = rendering.text_to_image("X", width=384, font_size=40, align="left")
    right = rendering.text_to_image("X", width=384, font_size=40, align="right")
    center = rendering.text_to_image("X", width=384, font_size=40, align="center")
    def first_black_col(img):
        pixels = img.load()
        assert pixels is not None
        for x in range(img.width):
            for y in range(img.height):
                if pixels[x, y] == 0:
                    return x
        return None
    l = first_black_col(left)
    c = first_black_col(center)
    r = first_black_col(right)
    assert l is not None and c is not None and r is not None
    assert l < c < r


def test_text_to_image_bold_uses_different_pixels_than_regular():
    regular = rendering.text_to_image("Test", width=384, font_size=40, bold=False)
    bold = rendering.text_to_image("Test", width=384, font_size=40, bold=True)
    # Bold produces more black pixels than regular.
    reg_blacks = sum(1 for b in regular.tobytes() for bit in range(8) if not (b >> (7 - bit)) & 1)
    bold_blacks = sum(1 for b in bold.tobytes() for bit in range(8) if not (b >> (7 - bit)) & 1)
    assert bold_blacks > reg_blacks


# ---------------------------------------------------------------------------
# Image file rendering + dithering
# ---------------------------------------------------------------------------

def test_image_file_to_image_scales_to_width(tmp_path):
    src = Image.new("RGB", (200, 100), color=(128, 128, 128))
    path = tmp_path / "src.png"
    src.save(path)
    img = rendering.image_file_to_image(path, width=384)
    assert img.mode == "1"
    assert img.width == 384
    # Aspect ratio preserved: 200x100 -> 384x192.
    assert img.height == 192


def test_binarise_floyd_returns_mode_1():
    src = Image.new("L", (20, 20), color=128)
    out = rendering.binarise(src, dither="floyd")
    assert out.mode == "1"
    assert out.size == (20, 20)


def test_binarise_threshold_extremes():
    src = Image.new("L", (4, 1))
    src.putpixel((0, 0), 50)   # below threshold -> black
    src.putpixel((1, 0), 200)  # above threshold -> white
    src.putpixel((2, 0), 50)
    src.putpixel((3, 0), 200)
    out = rendering.binarise(src, dither="threshold", threshold=128)
    px = out.load()
    assert px is not None
    assert px[0, 0] == 0
    assert px[1, 0] == 255
    assert px[2, 0] == 0
    assert px[3, 0] == 255


def test_binarise_unknown_raises():
    with pytest.raises(ValueError):
        rendering.binarise(Image.new("L", (1, 1)), dither="bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stack
# ---------------------------------------------------------------------------

def test_stack_images_vertical_concatenates():
    a = Image.new("1", (100, 10), color=255)
    b = Image.new("1", (100, 20), color=255)
    out = rendering.stack_images_vertical([a, b])
    assert out.width == 100
    assert out.height == 30


def test_stack_images_vertical_empty_raises():
    with pytest.raises(ValueError):
        rendering.stack_images_vertical([])


# ---------------------------------------------------------------------------
# QR
# ---------------------------------------------------------------------------

def test_qr_to_image_basic():
    img = rendering.qr_to_image("hello", width=200)
    assert img.mode == "1"
    assert img.width == 200
    # A QR code has both black and white pixels.
    px = img.load()
    assert px is not None
    has_black = any(px[x, y] == 0 for x in range(img.width) for y in range(img.height))
    has_white = any(px[x, y] == 255 for x in range(img.width) for y in range(img.height))
    assert has_black and has_white


def test_qr_to_image_missing_dep_message(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "qrcode" or name.startswith("qrcode."):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Also purge from sys.modules so the import retry runs.
    for k in list(sys.modules):
        if k == "qrcode" or k.startswith("qrcode."):
            del sys.modules[k]
    with pytest.raises(rendering.MissingDependencyError) as exc:
        rendering.qr_to_image("hi", width=100)
    assert "qrcode" in str(exc.value)


# ---------------------------------------------------------------------------
# Barcode
# ---------------------------------------------------------------------------

def test_barcode_to_image_basic():
    img = rendering.barcode_to_image("code128", "12345", width=300)
    assert img.mode == "1"
    assert img.width == 300


def test_barcode_unknown_type_raises():
    with pytest.raises(ValueError):
        rendering.barcode_to_image("nonsense_type", "data", width=200)


# ---------------------------------------------------------------------------
# Grid / ruled / lined
# ---------------------------------------------------------------------------

def test_grid_dimensions():
    img = rendering.grid_to_image("grid", width=384, rows=10, cols=6)
    assert img.mode == "1"
    assert img.width == 384
    assert img.height > 0


def test_ruled_paper_has_horizontal_lines_only():
    # A ruled image's first row should contain black pixels (top line).
    img = rendering.grid_to_image("ruled", width=200, rows=5, line_spacing=20)
    px = img.load()
    assert px is not None
    top_row_blacks = sum(1 for x in range(img.width) if px[x, 0] == 0)
    assert top_row_blacks > 100  # most of the row is the line


def test_grid_unknown_style_raises():
    with pytest.raises(ValueError):
        rendering.grid_to_image("bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Page-range parser
# ---------------------------------------------------------------------------

def test_parse_page_range_none_returns_all():
    assert rendering._parse_page_range(None, 5) == [0, 1, 2, 3, 4]


def test_parse_page_range_single():
    assert rendering._parse_page_range("3", 10) == [2]


def test_parse_page_range_list():
    assert rendering._parse_page_range("1,3,5", 10) == [0, 2, 4]


def test_parse_page_range_span():
    assert rendering._parse_page_range("2-4", 10) == [1, 2, 3]


def test_parse_page_range_mixed():
    assert rendering._parse_page_range("1,3-5,7", 10) == [0, 2, 3, 4, 6]


def test_parse_page_range_open_ended_span():
    assert rendering._parse_page_range("3-", 5) == [2, 3, 4]


def test_parse_page_range_out_of_bounds_clamped():
    assert rendering._parse_page_range("8-12", 5) == []
    assert rendering._parse_page_range("1-100", 5) == [0, 1, 2, 3, 4]
