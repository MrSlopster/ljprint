"""LuckJingle printer — input rendering to 1-bit raster.

Every public function returns either a PIL `Image` in mode "1"
(white=255 background, black=0 ink) or a list of them (PDF, multi-page).
The Printer class feeds these through `image_to_raster()` and the `GS v 0`
command to the transport.

Optional dependencies (qrcode, python-barcode, pypdfium2, numpy) are imported
lazily so a missing dep only fails when its specific feature is used.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable, Literal, Optional

from PIL import Image, ImageDraw, ImageFont

from . import protocol

LOGGER = logging.getLogger("luckjingle.rendering")

Dither = Literal["floyd", "threshold", "none"]
Align = Literal["left", "center", "right"]
GridStyle = Literal["grid", "ruled", "lined"]


class MissingDependencyError(RuntimeError):
    """Raised when an optional rendering dep is needed but not installed."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try common Linux TrueType paths; fall back to PIL default bitmap font."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    LOGGER.warning("No TrueType font at expected paths; using PIL default font.")
    return ImageFont.load_default()


def to_width(img: Image.Image, width: int) -> Image.Image:
    """Scale `img` (any mode) so its width = `width`, preserving aspect ratio."""
    if img.width == width:
        return img
    new_h = max(1, round(img.height * (width / img.width)))
    return img.resize((width, new_h), Image.Resampling.LANCZOS)


def binarise(img: Image.Image, dither: Dither = "floyd", threshold: int = 128) -> Image.Image:
    """Convert any PIL image to mode "1" using the chosen algorithm.

    `threshold` applies to both "threshold" and "none" modes — it is the
    luminance value at and above which a pixel becomes white (default 128,
    matching PrinterImageProcessor.getBitmapByteArray).
    """
    gray = img.convert("L")
    if dither == "floyd":
        return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    if dither in ("threshold", "none"):
        import numpy as np
        arr = np.asarray(gray)
        bw = np.where(arr >= threshold, 255, 0).astype(np.uint8)
        return Image.fromarray(bw, mode="L").convert("1", dither=Image.Dither.NONE)
    raise ValueError(f"unknown dither mode: {dither!r}")


def to_width_bilevel(img: Image.Image, width: int) -> Image.Image:
    """Scale to `width` in grayscale, then threshold to mode "1" without dithering.

    Resizing must happen before binarising: PIL forces NEAREST resampling on
    mode-"1" images, which aliases thin features (barcode bars, QR modules).
    """
    gray = to_width(img.convert("L"), width)
    return gray.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)


def stack_images_vertical(images: Iterable[Image.Image]) -> Image.Image:
    """Stack images of equal width vertically into one tall image (mode "1")."""
    images = [im.convert("1") for im in images]
    if not images:
        raise ValueError("no images to stack")
    width = max(im.width for im in images)
    total_h = sum(im.height for im in images)
    canvas = Image.new("1", (width, total_h), color=255)
    y = 0
    for im in images:
        canvas.paste(im, (0, y))
        y += im.height
    return canvas


# ---------------------------------------------------------------------------
# image_to_raster — the shared final step (matches PrinterImageProcessor)
# ---------------------------------------------------------------------------

_INVERT_TABLE = bytes(b ^ 0xFF for b in range(256))


def image_to_raster(img: Image.Image) -> tuple[bytes, int, int]:
    """Convert a PIL Image to (pixel_bytes, bytes_per_row, height_px) for GS v 0.

    Mirrors com.luckprinter.sdk_new.device.normal.base.PrinterImageProcessor.
    getBitmapByteArray: 1 bit per pixel, MSB-first, 1 = black, row-padded
    to a byte boundary.

    Mode "1" `tobytes()` already produces MSB-first bytes with rows padded to
    a byte boundary, just with 1 = white — so the payload is the inverted
    buffer with the padding bits of each row's last byte masked back to 0.
    """
    img = img.convert("1")
    width, height = img.width, img.height
    bytes_per_row = (width + 7) // 8
    raw = img.tobytes().translate(_INVERT_TABLE)
    pad_bits = bytes_per_row * 8 - width
    if pad_bits:
        out = bytearray(raw)
        mask = (0xFF << pad_bits) & 0xFF
        for i in range(bytes_per_row - 1, len(out), bytes_per_row):
            out[i] &= mask
        raw = bytes(out)
    return raw, bytes_per_row, height


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _break_long_word(word: str, fits) -> list[str]:
    """Split a single word into chunks that each satisfy `fits`."""
    if fits(word):
        return [word]
    parts: list[str] = []
    cur = ""
    for ch in word:
        if cur and not fits(cur + ch):
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    parts.append(cur)
    return parts


def text_to_image(
    text: str,
    *,
    width: int = protocol.DEFAULT_PRINT_WIDTH_PX,
    font_size: int = 32,
    bold: bool = False,
    align: Align = "left",
    line_gap: int = 4,
    margin: int = 4,
) -> Image.Image:
    """Render text with auto-wrap and alignment to a 1-bit image."""
    font = load_font(font_size, bold=bold)
    # Measure with a throwaway image (textlength needs a Draw context).
    measure = Image.new("1", (max(width, 1), 1), color=255)
    measure_draw = ImageDraw.Draw(measure)

    # Wrap each hard line at word boundaries; break oversize words by character.
    max_w = width - 2 * margin

    def fits(s: str) -> bool:
        return measure_draw.textlength(s, font=font) <= max_w

    lines: list[str] = []
    for hard_line in text.splitlines() or [""]:
        words = hard_line.split(" ")
        cur = words[0]
        for w in words[1:]:
            cand = f"{cur} {w}"
            if fits(cand):
                cur = cand
            else:
                lines.extend(_break_long_word(cur, fits))
                cur = w
        lines.extend(_break_long_word(cur, fits))

    bbox = font.getbbox("Ag")
    line_h = int(bbox[3]) + line_gap
    height = max(1, line_h * len(lines))
    img = Image.new("1", (width, height), color=255)
    draw = ImageDraw.Draw(img)
    y = 0
    for ln in lines:
        text_w = draw.textlength(ln, font=font)
        if align == "center":
            x = max(margin, (width - text_w) // 2)
        elif align == "right":
            x = max(margin, width - margin - text_w)
        else:
            x = margin
        draw.text((x, y), ln, font=font, fill=0)
        y += line_h
    return img


# ---------------------------------------------------------------------------
# Image file rendering
# ---------------------------------------------------------------------------

def image_file_to_image(
    path: str | Path,
    *,
    width: int = protocol.DEFAULT_PRINT_WIDTH_PX,
    dither: Dither = "floyd",
    threshold: int = 128,
) -> Image.Image:
    img = Image.open(path)
    img = to_width(img, width)
    return binarise(img, dither=dither, threshold=threshold)


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def pdf_pages_to_images(
    path: str | Path,
    *,
    width: int = protocol.DEFAULT_PRINT_WIDTH_PX,
    page_range: Optional[str] = None,
    dither: Dither = "floyd",
    threshold: int = 128,
) -> list[Image.Image]:
    """Render PDF pages to 1-bit images sized to the printer width."""
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError as exc:
        raise MissingDependencyError(
            "pypdfium2 is required for PDF rendering. "
            "Install with: pip install pypdfium2"
        ) from exc

    doc = pdfium.PdfDocument(str(path))
    try:
        total = len(doc)
        indices = _parse_page_range(page_range, total)
        images: list[Image.Image] = []
        for i in indices:
            page = doc[i]
            # Render at a scale that produces at least 2x the printer width, so
            # downscaling keeps text crisp.
            native_w = page.get_width()
            scale = max(2.0, (width * 2) / native_w) if native_w else 2.0
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            pil = to_width(pil, width)
            images.append(binarise(pil, dither=dither, threshold=threshold))
        return images
    finally:
        # pdfium.PdfDocument holds an mmap/file handle until close; explicit release.
        doc.close()


def _parse_page_range(spec: Optional[str], total: int) -> list[int]:
    """Parse '1,3,5-7' into 0-based indices; None = all."""
    if spec is None:
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i = int(lo) - 1
            hi_i = int(hi) - 1 if hi else total - 1
            out.extend(range(max(0, lo_i), min(total, hi_i + 1)))
        else:
            i = int(part) - 1
            if 0 <= i < total:
                out.append(i)
    return out


# ---------------------------------------------------------------------------
# QR code rendering
# ---------------------------------------------------------------------------

def qr_to_image(
    data: str,
    *,
    width: int = protocol.DEFAULT_PRINT_WIDTH_PX,
    box_size: Optional[int] = None,
    border: int = 2,
) -> Image.Image:
    """Generate a QR code sized to the printer width."""
    try:
        import qrcode  # type: ignore
        from qrcode.constants import ERROR_CORRECT_M  # type: ignore
    except ImportError as exc:
        raise MissingDependencyError(
            "qrcode is required for QR generation. "
            "Install with: pip install qrcode"
        ) from exc
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size or 10,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    # qrcode's make_image returns a PyPNGImage in stubs but a PIL.Image at runtime.
    img: Image.Image = qr.make_image(fill_color="black", back_color="white")  # type: ignore[assignment]
    return to_width_bilevel(img, width)


# ---------------------------------------------------------------------------
# Barcode rendering
# ---------------------------------------------------------------------------

def barcode_to_image(
    btype: str,
    data: str,
    *,
    width: int = protocol.DEFAULT_PRINT_WIDTH_PX,
) -> Image.Image:
    """Generate a 1-D barcode of the given type and scale to printer width."""
    try:
        import barcode  # type: ignore
        from barcode.writer import ImageWriter  # type: ignore
    except ImportError as exc:
        raise MissingDependencyError(
            "python-barcode is required for barcode generation. "
            "Install with: pip install python-barcode"
        ) from exc
    try:
        cls = barcode.get_barcode_class(btype)
    except barcode.errors.BarcodeNotFoundError as exc:
        raise ValueError(
            f"Unknown barcode type {btype!r}. "
            f"Try: code128, code39, ean13, ean8, upc, isbn, gs1"
        ) from exc
    obj = cls(data, writer=ImageWriter())
    buf = io.BytesIO()
    obj.write(buf)
    buf.seek(0)
    return to_width_bilevel(Image.open(buf), width)


# ---------------------------------------------------------------------------
# Grid / ruled / lined paper
# ---------------------------------------------------------------------------

def grid_to_image(
    style: GridStyle = "grid",
    *,
    width: int = protocol.DEFAULT_PRINT_WIDTH_PX,
    rows: int = 20,
    cols: int = 8,
    row_height: Optional[int] = None,
    line_spacing: int = 32,
) -> Image.Image:
    """Render ruled/grid/lined paper template."""
    if style == "grid":
        rh = row_height or max(20, (rows and (width // cols) or 32))
        height = rh * rows + 4
        img = Image.new("1", (width, height), color=255)
        draw = ImageDraw.Draw(img)
        for c in range(cols + 1):
            x = c * (width // cols)
            draw.line([(x, 0), (x, height)], fill=0, width=1)
        for r in range(rows + 1):
            y = r * rh
            draw.line([(0, y), (width, y)], fill=0, width=1)
        return img
    if style in ("ruled", "lined"):
        height = line_spacing * rows + 4
        img = Image.new("1", (width, height), color=255)
        draw = ImageDraw.Draw(img)
        for r in range(rows + 1):
            y = r * line_spacing
            draw.line([(0, y), (width, y)], fill=0, width=1)
        return img
    raise ValueError(f"unknown grid style: {style!r}")
