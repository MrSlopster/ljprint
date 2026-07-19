"""LuckJingle printer — command-line interface.

Subcommands grouped: print, settings, queries, utility.
See `luckjingle-print --help` for the full list.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
from importlib import resources
from typing import Optional

from bleak import BleakScanner

from . import protocol, rendering
from .printer import Printer, PreflightError

LOGGER = logging.getLogger("luckjingle.cli")

# Completion scripts are bundled inside the package at luckjingle/completions/
# so they survive a `pip install` (not just dev mode). Look them up via
# importlib.resources for editible-install safety.
COMPLETION_FILES = {
    "bash": "luckjingle_print.bash",
    "zsh":  "_luckjingle_print",
}

# Friendly-name prefixes for scan matching (PROTOCOL.md §4). Separators vary
# across device variants (DP_D1_BC3B vs LJ-D1 vs GT-01), so match without one.
PRINTER_NAME_PREFIXES = ("dp", "lj", "luck", "gt", "ay", "aiyin", "hanyin", "print")

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_ARG = 2
EXIT_MISSING_DEP = 3
EXIT_PREFLIGHT = 4


# ---------------------------------------------------------------------------
# Argparse helpers
# ---------------------------------------------------------------------------

def _add_print_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", default="normal",
                   choices=[m.value for m in protocol.PrintMode],
                   help="Print mode preset")
    p.add_argument("--force", action="store_true",
                   help="Print even if pre-flight status check fails")


def _add_dither_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dither", choices=["floyd", "threshold", "none"], default="floyd",
                   help="Binarisation algorithm (default: floyd)")
    p.add_argument("--threshold", type=int, default=128,
                   help="Luminance cutoff for --dither threshold/none (default: 128)")


def _mac_arg(p: argparse.ArgumentParser) -> None:
    """Add a `mac` positional that falls back to $LUCKJINGLE_PRINTER.

    `nargs="?"` lets the positional be omitted when the env var is set.
    Validation (refuse None) happens centrally in `main()`.
    """
    env_default = os.environ.get(protocol.DEFAULT_MAC_ENV)
    p.add_argument(
        "mac", nargs="?", default=env_default,
        help=f"Printer MAC (default: ${protocol.DEFAULT_MAC_ENV} env var"
             + (f", currently {env_default!r})" if env_default else ")"),
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def matches_printer_name(name: str) -> bool:
    """True when a BLE friendly name matches a known printer-family prefix.

    `LJ-*` and `GT-*` use hyphens while `DP_*` uses underscores, so the
    prefixes omit the separator and match either form.
    """
    lowered = name.lower()
    return any(lowered.startswith(p) for p in PRINTER_NAME_PREFIXES)


async def cmd_scan(args) -> int:
    print(f"Scanning for {args.duration:.0f}s ...\n")
    seen: dict[str, tuple] = {}

    def cb(dev, adv):
        seen[dev.address] = (dev, adv)

    async with BleakScanner(detection_callback=cb):
        await asyncio.sleep(args.duration)

    matches, others = [], []
    for addr, (dev, adv) in seen.items():
        uuids = list(adv.service_uuids or [])
        name = dev.name or getattr(adv, "local_name", None) or ""
        is_luck = any(u.lower().startswith("0000ff00") for u in uuids)
        row = (addr, name or None, uuids, adv.rssi)
        (matches if (is_luck or matches_printer_name(name)) else others).append(row)

    if matches:
        print("LuckJingle printers found:")
        for addr, name, uuids, rssi in matches:
            print(f"  {addr}  rssi={rssi}  name={name!r}  uuids={uuids}")
        print()
    else:
        print("No devices matched by service UUID or name prefix.\n")
    if others:
        print("Other BLE devices nearby (try `info <mac>` to probe by GATT):")
        for addr, name, uuids, rssi in sorted(others):
            print(f"  {addr}  rssi={rssi}  name={name!r}  uuids={uuids}")
    return EXIT_OK


async def cmd_info(args) -> int:
    async with Printer(args.mac) as p:
        info = await p.get_info()
    print(f"== LuckJingle printer @ {info['mac']} ==")
    if info["status"]:
        print(f"  status   : {info['status']}")
    if info["battery"] is not None:
        print(f"  battery  : {info['battery']}%")
    print(f"  model    : {info['model']!r}")
    print(f"  firmware : {info['firmware']!r}")
    print(f"  serial   : {info['serial']!r}")
    print(f"  pkt size : {info['packet_size']} bytes (negotiated)")
    return EXIT_OK


async def cmd_status(args) -> int:
    async with Printer(args.mac) as p:
        status = await p.get_status()
    print(status)
    return EXIT_OK


async def cmd_watch(args) -> int:
    print(f"Watching {args.mac} — Ctrl-C to stop. Polling every {args.interval}s.\n")
    async with Printer(args.mac) as p:
        def listener(kind: str, payload: bytes):
            print(f"[event] {kind}: {payload.hex()}")
        p.transport.add_event_listener(listener)
        try:
            while True:
                status = await p.get_status()
                battery = await p.get_battery()
                battery_s = f"{battery}%" if battery is not None else "?"
                print(f"  status={status}  battery={battery_s}")
                await asyncio.sleep(args.interval)
        except asyncio.CancelledError:
            pass
    return EXIT_OK


async def cmd_raw(args) -> int:
    payload = bytes.fromhex(args.hex.replace(" ", "").replace(":", ""))
    async with Printer(args.mac) as p:
        reply = await p.transport.exchange(payload, timeout=10.0)
    print(f"Sent {len(payload)} bytes; reply={reply.hex() if reply else 'None'}")
    return EXIT_OK


async def cmd_gatt_map(args) -> int:
    from bleak import BleakClient
    async with BleakClient(args.mac, timeout=20.0) as client:
        print(f"== GATT services @ {args.mac} ==")
        for s in client.services:
            print(f"  service {s.uuid}  ({s.description or '-'})")
            for c in s.characteristics:
                props = ",".join(c.properties)
                print(f"    char {c.uuid}  handle={c.handle}  props={props}")
    return EXIT_OK


async def cmd_print_text(args) -> int:
    text = _resolve_text_arg(args.text)
    if not text:
        sys.stderr.write("error: no text to print (stdin was empty)\n")
        return EXIT_BAD_ARG
    async with Printer(args.mac) as p:
        await p.print_text(text, mode=args.mode,
                           font_size=args.font_size, bold=args.bold,
                           align=args.align, force=args.force)
    # Informational message goes to stderr so it doesn't pollute downstream
    # pipes when the user is piping text INTO the command.
    sys.stderr.write(f"Printed {len(text)} chars to {args.mac}.\n")
    return EXIT_OK


def _resolve_text_arg(text: str) -> str:
    """If `text` is the conventional `-` sentinel, slurp stdin instead.
    Supports `fortune | luckjingle-print print -` and heredocs:
        luckjingle-print print - <<EOF
        multi
        line
        EOF
    """
    if text != "-":
        return text
    return sys.stdin.read()


async def cmd_print_image(args) -> int:
    async with Printer(args.mac) as p:
        await p.print_image_file(args.file, mode=args.mode,
                                 dither=args.dither, threshold=args.threshold,
                                 force=args.force)
    print(f"Printed {args.file} to {args.mac}.")
    return EXIT_OK


async def cmd_print_pdf(args) -> int:
    async with Printer(args.mac) as p:
        await p.print_pdf(args.file, mode=args.mode, page_range=args.pages,
                          dither=args.dither, threshold=args.threshold,
                          force=args.force)
    print(f"Printed {args.file} to {args.mac}.")
    return EXIT_OK


async def cmd_print_qr(args) -> int:
    async with Printer(args.mac) as p:
        await p.print_qr(args.data, mode=args.mode, box_size=args.box_size,
                         force=args.force)
    print(f"Printed QR for {args.data!r} to {args.mac}.")
    return EXIT_OK


async def cmd_print_barcode(args) -> int:
    async with Printer(args.mac) as p:
        await p.print_barcode(args.btype, args.data, mode=args.mode, force=args.force)
    print(f"Printed {args.btype} barcode to {args.mac}.")
    return EXIT_OK


async def cmd_print_grid(args) -> int:
    async with Printer(args.mac) as p:
        await p.print_grid(args.style, rows=args.rows, cols=args.cols,
                           line_spacing=args.line_spacing, mode=args.mode,
                           force=args.force)
    print(f"Printed {args.style} to {args.mac}.")
    return EXIT_OK


async def _run_setting(args, method_name: str, value_arg: str | None = None) -> int:
    async with Printer(args.mac) as p:
        method = getattr(p, method_name)
        if value_arg is None:
            await method()
        else:
            await method(getattr(args, value_arg))
    print("OK")
    return EXIT_OK


async def cmd_set_density(args) -> int: return await _run_setting(args, "set_density", "level")
async def cmd_set_speed(args) -> int: return await _run_setting(args, "set_speed", "speed")
async def cmd_set_heating(args) -> int: return await _run_setting(args, "set_heating_level", "level")
async def cmd_set_shuttime(args) -> int: return await _run_setting(args, "set_shuttime", "minutes")
async def cmd_set_width(args) -> int: return await _run_setting(args, "set_width", "pixels")
async def cmd_set_paper_type(args) -> int:
    async with Printer(args.mac) as p:
        await p.set_paper_type(args.kind, args.mask)
    print("OK")
    return EXIT_OK
async def cmd_set_time(args) -> int: return await _run_setting(args, "set_rtc")
async def cmd_reset(args) -> int: return await _run_setting(args, "recovery")


async def cmd_completions(args) -> int:
    """Print a shell-completion script to stdout for `eval` or sourcing."""
    shell = args.shell or _detect_shell()
    if shell is None:
        sys.stderr.write(
            "Could not detect shell from $SHELL. Pass --shell bash or --shell zsh.\n"
        )
        return EXIT_BAD_ARG
    filename = COMPLETION_FILES.get(shell)
    if filename is None:
        sys.stderr.write(f"Unsupported shell: {shell!r}. Choose bash or zsh.\n")
        return EXIT_BAD_ARG
    try:
        text = (resources.files("luckjingle") / "completions" / filename).read_text()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        sys.stderr.write(f"Completion file not found: {exc}\n")
        return EXIT_ERROR
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return EXIT_OK


def _detect_shell() -> Optional[str]:
    shell_path = os.environ.get("SHELL", "")
    shell_name = pathlib.Path(shell_path).name.lower() if shell_path else ""
    if "bash" in shell_name:
        return "bash"
    if "zsh" in shell_name:
        return "zsh"
    return None


# ---------------------------------------------------------------------------
# Argparse construction
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="luckjingle-print",
        description="LuckJingle thermal printer BLE utility (see PROTOCOL.md).",
    )
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase logging: -v INFO, -vv DEBUG")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    # ---- utility ----
    s = sub.add_parser("scan", help="Scan for nearby LuckJingle printers.")
    s.add_argument("--duration", type=float, default=10.0,
                   help="Scan duration in seconds (default: 10)")
    s.set_defaults(handler=cmd_scan)

    s = sub.add_parser("gatt-map", help="Dump the full GATT layout of a device.")
    _mac_arg(s)
    s.set_defaults(handler=cmd_gatt_map)

    s = sub.add_parser("raw", help="Send a raw hex byte string.")
    _mac_arg(s)
    s.add_argument("hex", help="Hex bytes, e.g. '10 FF 40' or '10FF40'")
    s.set_defaults(handler=cmd_raw)

    # ---- queries ----
    s = sub.add_parser("info", help="Combined snapshot: status, battery, model, firmware, serial.")
    _mac_arg(s)
    s.set_defaults(handler=cmd_info)

    s = sub.add_parser("status", help="One-shot status query.")
    _mac_arg(s)
    s.set_defaults(handler=cmd_status)

    s = sub.add_parser("watch", help="Poll status and stream async printer events.")
    _mac_arg(s)
    s.add_argument("--interval", type=float, default=5.0,
                   help="Seconds between status polls (default: 5)")
    s.set_defaults(handler=cmd_watch)

    # ---- print ----
    _text_help = ('Text to print, or "-" to read from stdin '
                  '(e.g. `fortune | luckjingle-print print -`)')
    # `print` is a backward-compat alias: it was the original name
    # (single-file demo) and must keep the exact same flags.
    for name, help_text in (("print-text", "Render text and print it."),
                            ("print", "Alias for print-text.")):
        s = sub.add_parser(name, help=help_text)
        _mac_arg(s)
        s.add_argument("text", help=_text_help)
        s.add_argument("--font-size", type=int, default=32,
                       help="Font size in pixels (default: 32)")
        s.add_argument("--bold", action="store_true", help="Render with the bold font")
        s.add_argument("--align", choices=["left", "center", "right"], default="left",
                       help="Text alignment (default: left)")
        _add_print_common(s)
        s.set_defaults(handler=cmd_print_text)

    s = sub.add_parser("print-image", help="Print a PNG/JPG image file.")
    _mac_arg(s)
    s.add_argument("file", help="Path to image file")
    _add_dither_args(s)
    _add_print_common(s)
    s.set_defaults(handler=cmd_print_image)

    s = sub.add_parser("print-pdf", help="Render PDF pages and print each (stacked).")
    _mac_arg(s)
    s.add_argument("file", help="Path to PDF file")
    s.add_argument("--pages", default=None, help="Page range, e.g. '1,3,5-7' (default: all)")
    _add_dither_args(s)
    _add_print_common(s)
    s.set_defaults(handler=cmd_print_pdf)

    s = sub.add_parser("print-qr", help="Generate a QR code and print it.")
    _mac_arg(s)
    s.add_argument("data", help="Data to encode")
    s.add_argument("--box-size", type=int, default=None,
                   help="QR module size in pixels before scaling (default: 10)")
    _add_print_common(s)
    s.set_defaults(handler=cmd_print_qr)

    s = sub.add_parser("print-barcode", help="Generate a 1-D barcode and print it.")
    _mac_arg(s)
    s.add_argument("btype", help="Barcode type, e.g. code128, code39, ean13")
    s.add_argument("data", help="Data to encode")
    _add_print_common(s)
    s.set_defaults(handler=cmd_print_barcode)

    s = sub.add_parser("print-grid", help="Generate and print ruled/grid paper.")
    _mac_arg(s)
    s.add_argument("--style", choices=["grid", "ruled", "lined"], default="grid",
                   help="Template style (default: grid)")
    s.add_argument("--rows", type=int, default=20, help="Number of rows (default: 20)")
    s.add_argument("--cols", type=int, default=8,
                   help="Number of columns, grid style only (default: 8)")
    s.add_argument("--line-spacing", type=int, default=32,
                   help="Row spacing in pixels, ruled/lined styles (default: 32)")
    _add_print_common(s)
    s.set_defaults(handler=cmd_print_grid)

    # ---- settings ----
    s = sub.add_parser("set-density", help="Set print density.")
    _mac_arg(s)
    s.add_argument("level", type=int, help="Density level (0-2 typical)")
    s.set_defaults(handler=cmd_set_density)

    s = sub.add_parser("set-speed", help="Set print speed.")
    _mac_arg(s)
    s.add_argument("speed", type=int, help="Speed value")
    s.set_defaults(handler=cmd_set_speed)

    s = sub.add_parser("set-paper-type", help="Set raw paper type (see PROTOCOL.md §3.2).")
    _mac_arg(s)
    s.add_argument("kind", type=int, help="Paper-type kind byte (e.g. 1)")
    s.add_argument("mask", type=int, help="Paper-type mask byte (e.g. 64 = tattoo)")
    s.set_defaults(handler=cmd_set_paper_type)

    s = sub.add_parser("set-heating", help="Set heating level.")
    _mac_arg(s)
    s.add_argument("level", type=int, help="Heating level")
    s.set_defaults(handler=cmd_set_heating)

    s = sub.add_parser("set-shuttime", help="Set auto-shutdown timer.")
    _mac_arg(s)
    s.add_argument("minutes", type=int, help="Minutes until auto-shutdown")
    s.set_defaults(handler=cmd_set_shuttime)

    s = sub.add_parser("set-width", help="Set print width.")
    _mac_arg(s)
    s.add_argument("pixels", type=int, help="Print width in dots (e.g. 384 or 832)")
    s.set_defaults(handler=cmd_set_width)

    s = sub.add_parser("set-time", help="Sync printer RTC to system time.")
    _mac_arg(s)
    s.set_defaults(handler=cmd_set_time)

    s = sub.add_parser("reset", help="Send recovery / reset command.")
    _mac_arg(s)
    s.set_defaults(handler=cmd_reset)

    # ---- completions ----
    s = sub.add_parser("completions",
                       help="Print a bash or zsh completion script to eval or source.")
    s.add_argument("--shell", choices=["bash", "zsh"], default=None,
                   help="Target shell (default: detect from $SHELL)")
    s.set_defaults(handler=cmd_completions)

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # `scan` has no `mac`; every other subcommand does. If neither an arg
    # nor $LUCKJINGLE_PRINTER was supplied, error out with a clear hint
    # rather than letting BleakClient(None) fail cryptically downstream.
    if "mac" in args and not args.mac:
        sys.stderr.write(
            f"error: no printer MAC. Pass it as a positional argument or set "
            f"the ${protocol.DEFAULT_MAC_ENV} environment variable.\n"
        )
        return EXIT_BAD_ARG
    try:
        return asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130
    except rendering.MissingDependencyError as exc:
        sys.stderr.write(f"Missing dependency: {exc}\n")
        return EXIT_MISSING_DEP
    except PreflightError as exc:
        sys.stderr.write(f"Preflight failed: {exc}\n")
        return EXIT_PREFLIGHT
    except RuntimeError as exc:
        # Covers BLE connect failures and other protocol-level errors.
        sys.stderr.write(f"Error: {exc}\n")
        return EXIT_ERROR
    except FileNotFoundError as exc:
        sys.stderr.write(f"File not found: {exc.filename}\n")
        return EXIT_BAD_ARG
    except Exception as exc:
        LOGGER.error("%s", exc)
        return EXIT_ERROR
