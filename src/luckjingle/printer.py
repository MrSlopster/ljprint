"""LuckJingle printer — high-level operations.

The `Printer` class wraps a `PrinterTransport` and exposes one async method
per user-facing operation (print text/image/pdf/qr/barcode/grid, set X, get X).
Print paths compose `rendering.*` -> `protocol.cmd_raster_image` ->
`transport.send`, wrapped by the enable/wake/stop sequence per print mode.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from . import protocol, rendering
from .transport import PrinterTransport

LOGGER = logging.getLogger("luckjingle.printer")


class PreflightError(RuntimeError):
    """Raised by `Printer.preflight()` when the printer reports a blocking
    condition (out-of-paper, overheat). Distinct from generic RuntimeError so
    the CLI can map it to a dedicated exit code without substring matching.
    """


class Printer:
    """High-level LuckJingle printer client. Async context manager.

    Usage:
        async with Printer("AA:BB:CC:DD:EE:FF") as p:
            await p.print_text("hello")
    """

    def __init__(self, mac: str, *, transport: Optional[PrinterTransport] = None):
        self.mac = mac
        self._transport: Optional[PrinterTransport] = transport
        self._owns_transport = transport is None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def __aenter__(self) -> "Printer":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self._transport is None:
            self._transport = await PrinterTransport.connect(self.mac)

    async def disconnect(self) -> None:
        if self._transport and self._owns_transport:
            await self._transport.disconnect()
            self._transport = None

    @property
    def transport(self) -> PrinterTransport:
        if self._transport is None:
            raise RuntimeError("Printer is not connected")
        return self._transport

    # -----------------------------------------------------------------
    # Queries
    # -----------------------------------------------------------------

    async def get_status(self) -> dict[str, bool]:
        reply = await self.transport.exchange(protocol.cmd_status(), timeout=5.0)
        if not reply:
            raise RuntimeError("printer did not reply to status query")
        return protocol.decode_status(reply[0])

    async def get_battery(self) -> Optional[int]:
        reply = await self.transport.exchange(protocol.cmd_battery(), timeout=5.0)
        if reply and len(reply) >= 2:
            return int(reply[1])
        return None

    async def get_model(self) -> str:
        return (await self._ascii_query(protocol.cmd_get_model())).strip()

    async def get_version(self) -> str:
        return (await self._ascii_query(protocol.cmd_get_version())).strip()

    async def get_sn(self) -> str:
        return (await self._ascii_query(protocol.cmd_get_sn())).strip()

    async def get_density(self) -> Optional[int]:
        reply = await self.transport.exchange(protocol.cmd_get_density(), timeout=5.0)
        return int(reply[0]) if reply else None

    async def get_speed(self) -> Optional[int]:
        reply = await self.transport.exchange(protocol.cmd_get_speed(), timeout=5.0)
        return int(reply[0]) if reply else None

    async def get_shuttime(self) -> Optional[int]:
        reply = await self.transport.exchange(protocol.cmd_get_shuttime(), timeout=5.0)
        if not reply:
            return None
        return int(reply[1]) if len(reply) == 2 else int(reply[0])

    async def get_info(self) -> dict[str, object]:
        """Combined snapshot for the `info` subcommand."""
        async def _safe(coro, default=None):
            try:
                return await coro
            except Exception as exc:
                LOGGER.debug("info query failed: %s", exc)
                return default

        status, battery, model, version, sn = await asyncio.gather(
            _safe(self.get_status(), {}),
            _safe(self.get_battery()),
            _safe(self.get_model(), ""),
            _safe(self.get_version(), ""),
            _safe(self.get_sn(), ""),
        )
        return {
            "mac": self.mac,
            "status": status,
            "battery": battery,
            "model": model,
            "firmware": version,
            "serial": sn,
            "packet_size": self.transport.packet_size,
        }

    async def _ascii_query(self, cmd: bytes) -> str:
        reply = await self.transport.exchange(cmd, timeout=5.0)
        if reply is None:
            return ""
        return reply.decode(errors="replace")

    # -----------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------

    async def set_density(self, level: int) -> None:
        await self.transport.send(protocol.cmd_set_density(level), wait_for=True)

    async def set_speed(self, speed: int) -> None:
        await self.transport.send(protocol.cmd_set_speed(speed), wait_for=True)

    async def set_paper_type(self, kind: int, mask: int) -> None:
        await self.transport.send(protocol.cmd_set_paper_type(kind, mask), wait_for=True)

    async def set_heating_level(self, level: int) -> None:
        await self.transport.send(protocol.cmd_set_heating_level(level), wait_for=True)

    async def set_shuttime(self, minutes: int) -> None:
        await self.transport.send(protocol.cmd_set_shuttime(minutes), wait_for=True)

    async def set_width(self, pixels: int) -> None:
        await self.transport.send(protocol.cmd_set_width(pixels), wait_for=True)

    async def set_printer_mode(self, mode: int) -> None:
        await self.transport.send(protocol.cmd_set_printer_mode(mode), wait_for=True)

    async def set_rtc(self, when: Optional[float] = None) -> None:
        """Sync the printer's RTC to the given epoch (default: now)."""
        when = when if when is not None else time.time()
        t = time.localtime(when)
        await self.transport.send(
            protocol.cmd_set_time(0, t.tm_year, t.tm_mon, t.tm_mday,
                                  t.tm_hour, t.tm_min, t.tm_sec),
            wait_for=True,
        )

    async def recovery(self) -> None:
        await self.transport.send(protocol.cmd_recovery(), wait_for=True)

    # -----------------------------------------------------------------
    # Print pipeline
    # -----------------------------------------------------------------

    async def preflight(self, force: bool = False) -> dict[str, bool]:
        """Check status before printing. Returns the status dict.

        If out-of-paper or overheat, raise `PreflightError` unless `force`.
        """
        status = await self.get_status()
        blocking = [k for k, v in status.items() if v and k in
                    ("out_of_paper", "overheat")]
        if blocking and not force:
            raise PreflightError(
                f"printer reports: {', '.join(blocking)} (use force=True to override)"
            )
        return status

    async def print_image(
        self,
        img: Image.Image,
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        end_line_dots: Optional[int] = None,
        force: bool = False,
    ) -> Optional[bytes]:
        """Print a single PIL image using the given mode preset."""
        await self.preflight(force=force)
        preset = protocol.get_preset(mode)
        raster, bpr, height = rendering.image_to_raster(img)
        return await self._run_print_job(
            payload=protocol.cmd_raster_image(raster, bpr, height),
            preset=preset,
            end_line_dots=end_line_dots,
        )

    async def print_images(
        self,
        images: list[Image.Image],
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        end_line_dots: Optional[int] = None,
        force: bool = False,
    ) -> Optional[bytes]:
        """Stack images vertically and print as one job (used for multi-page PDF)."""
        if not images:
            raise ValueError("no images to print")
        if len(images) == 1:
            return await self.print_image(images[0], mode=mode,
                                          end_line_dots=end_line_dots, force=force)
        return await self.print_image(
            rendering.stack_images_vertical(images),
            mode=mode, end_line_dots=end_line_dots, force=force,
        )

    async def print_text(
        self,
        text: str,
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        font_size: int = 32,
        bold: bool = False,
        align: rendering.Align = "left",
        force: bool = False,
    ) -> Optional[bytes]:
        preset = protocol.get_preset(mode)
        img = rendering.text_to_image(
            text, width=preset.print_width,
            font_size=font_size, bold=bold, align=align,
        )
        return await self.print_image(img, mode=mode, force=force)

    async def print_image_file(
        self,
        path: str | Path,
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        dither: rendering.Dither = "floyd",
        threshold: int = 128,
        force: bool = False,
    ) -> Optional[bytes]:
        preset = protocol.get_preset(mode)
        img = rendering.image_file_to_image(path, width=preset.print_width,
                                            dither=dither, threshold=threshold)
        return await self.print_image(img, mode=mode, force=force)

    async def print_pdf(
        self,
        path: str | Path,
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        page_range: Optional[str] = None,
        dither: rendering.Dither = "floyd",
        threshold: int = 128,
        force: bool = False,
    ) -> Optional[bytes]:
        preset = protocol.get_preset(mode)
        images = rendering.pdf_pages_to_images(path, width=preset.print_width,
                                               page_range=page_range,
                                               dither=dither, threshold=threshold)
        return await self.print_images(images, mode=mode, force=force)

    async def print_qr(
        self,
        data: str,
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        box_size: Optional[int] = None,
        force: bool = False,
    ) -> Optional[bytes]:
        preset = protocol.get_preset(mode)
        img = rendering.qr_to_image(data, width=preset.print_width, box_size=box_size)
        return await self.print_image(img, mode=mode, force=force)

    async def print_barcode(
        self,
        btype: str,
        data: str,
        *,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        force: bool = False,
    ) -> Optional[bytes]:
        preset = protocol.get_preset(mode)
        img = rendering.barcode_to_image(btype, data, width=preset.print_width)
        return await self.print_image(img, mode=mode, force=force)

    async def print_grid(
        self,
        style: rendering.GridStyle = "grid",
        *,
        rows: int = 20,
        cols: int = 8,
        line_spacing: int = 32,
        mode: protocol.PrintMode | str = protocol.PrintMode.NORMAL,
        force: bool = False,
    ) -> Optional[bytes]:
        preset = protocol.get_preset(mode)
        img = rendering.grid_to_image(style, width=preset.print_width,
                                      rows=rows, cols=cols, line_spacing=line_spacing)
        return await self.print_image(img, mode=mode, force=force)

    # -----------------------------------------------------------------
    # Internal: the standard print job envelope
    # -----------------------------------------------------------------

    async def _run_print_job(
        self,
        *,
        payload: bytes,
        preset: protocol.ModePreset,
        end_line_dots: Optional[int],
    ) -> Optional[bytes]:
        dots = end_line_dots if end_line_dots is not None else preset.end_line_dots
        LOGGER.debug("print job: mode=%s, payload=%d bytes, end-line dots=%d",
                     preset.name, len(payload), dots)
        await self.transport.send(protocol.cmd_enable_printer(3))
        await self.transport.send(protocol.cmd_wakeup())
        if preset.paper_type_cmd:
            await self.transport.send(preset.paper_type_cmd, wait_for=True)
        await asyncio.sleep(0.05)
        await self.transport.send(payload)
        await self.transport.send(protocol.cmd_feed_dots(dots))
        reply = await self.transport.send(protocol.cmd_stop_print_job(), wait_for=True, timeout=30.0)
        return reply
