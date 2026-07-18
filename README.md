# LuckJingle printer CLI

Python utility that connects to a LuckJingle (`com.dingdang.newprint`) thermal
printer over BLE and exposes the operations the Android app does, as a
headless CLI. Companion to `PROTOCOL.md` (the reverse-engineered protocol
reference).

## Setup

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/) (or any PEP 621
build frontend). Linux also needs BlueZ running
(`systemctl status bluetooth`); you typically need to be in the `bluetooth`
group or pre-pair the printer with `bluetoothctl pair <mac>`.

```sh
git clone <this repo> luckjingle-print && cd luckjingle-print
uv sync       # creates .venv, installs runtime + dev deps (PEP 735 default group)
```

Run any command via `uv run luckjingle-print …`. The console script is also
installed in `.venv/bin/`, and `python -m luckjingle …` works equivalently.

## Default printer via env var

Every subcommand that takes a `<mac>` argument also falls back to the
`LUCKJINGLE_PRINTER` environment variable, so you don't have to retype it:

```sh
export LUCKJINGLE_PRINTER=AA:BB:CC:DD:EE:FF

uv run luckjingle-print info                     # uses $LUCKJINGLE_PRINTER
uv run luckjingle-print print-text "hello"       # uses $LUCKJINGLE_PRINTER
uv run luckjingle-print info 11:22:33:44:55:66   # explicit arg always wins
```

If neither is supplied the CLI exits with code 2 and a clear hint.

## Shell completions

Tab-completion for subcommands, flags, mode/dither/style values, barcode
types, and printer MAC addresses (pulled live from `bluetoothctl devices`).

**Bash:**
```sh
# Persistent across shells:
uv run luckjingle-print completions --shell bash \
  | sudo tee /etc/bash_completion.d/luckjingle-print

# Or per-user via eval in ~/.bashrc:
echo 'eval "$(uv run --project /path/to/luckjingle-print luckjingle-print completions --shell bash)"' >> ~/.bashrc
```

**Zsh:**
```sh
mkdir -p ~/.zsh/completions
uv run luckjingle-print completions --shell zsh > ~/.zsh/completions/_luckjingle_print
echo 'fpath=(~/.zsh/completions $fpath)' >> ~/.zshrc
echo 'autoload -Uz compinit && compinit' >> ~/.zshrc
```

Omit `--shell` to auto-detect from `$SHELL`.

## Discovery

```sh
uv run luckjingle-print scan                          # find printers by UUID or name prefix
uv run luckjingle-print gatt-map AA:BB:CC:DD:EE:FF    # full GATT dump of any device
```

## Queries

```sh
uv run luckjingle-print info AA:BB:CC:DD:EE:FF          # combined snapshot
uv run luckjingle-print status AA:BB:CC:DD:EE:FF        # one-shot status bitfield
uv run luckjingle-print watch AA:BB:CC:DD:EE:FF --interval 5   # continuous
```

## Printing

Every print subcommand takes `--mode normal|tattoo|water-transfer|a4` (preset
paper-type + end-line dots) and `--force` (skip the pre-flight check).

```sh
# Text
uv run luckjingle-print print-text AA:BB:CC:DD:EE:FF "Hello, world!"
uv run luckjingle-print print-text AA:BB:CC:DD:EE:FF "Title" --font-size 48 --bold --align center
uv run luckjingle-print print-text AA:BB:CC:DD:EE:FF "tattoo stencil" --mode tattoo

# Image files (PNG/JPG) — auto-scaled to printer width
uv run luckjingle-print print-image AA:BB:CC:DD:EE:FF photo.jpg
uv run luckjingle-print print-image AA:BB:CC:DD:EE:FF logo.png --dither threshold --threshold 100

# PDF (multi-page is auto-stacked)
uv run luckjingle-print print-pdf AA:BB:CC:DD:EE:FF document.pdf
uv run luckjingle-print print-pdf AA:BB:CC:DD:EE:FF document.pdf --pages 1,3,5-7

# QR codes
uv run luckjingle-print print-qr AA:BB:CC:DD:EE:FF "https://example.com/hello"

# Barcodes
uv run luckjingle-print print-barcode AA:BB:CC:DD:EE:FF code128 "123456789012"
uv run luckjingle-print print-barcode AA:BB:CC:DD:EE:FF ean13 "4006381333931"

# Templates
uv run luckjingle-print print-grid AA:BB:CC:DD:EE:FF --style grid --rows 12 --cols 6
uv run luckjingle-print print-grid AA:BB:CC:DD:EE:FF --style ruled --rows 20 --line-spacing 32
```

## Settings

```sh
uv run luckjingle-print set-density   AA:BB:CC:DD:EE:FF 2     # 0–2 typical
uv run luckjingle-print set-speed     AA:BB:CC:DD:EE:FF 3
uv run luckjingle-print set-paper-type AA:BB:CC:DD:EE:FF 1 64 # 1F 80 kind mask (tattoo preset)
uv run luckjingle-print set-heating   AA:BB:CC:DD:EE:FF 5
uv run luckjingle-print set-shuttime  AA:BB:CC:DD:EE:FF 30    # auto-shutdown in 30 minutes
uv run luckjingle-print set-width     AA:BB:CC:DD:EE:FF 384
uv run luckjingle-print set-time      AA:BB:CC:DD:EE:FF       # sync RTC to system time
uv run luckjingle-print reset         AA:BB:CC:DD:EE:FF       # recovery
```

## Power users

```sh
uv run luckjingle-print raw AA:BB:CC:DD:EE:FF 10FF40      # query real-time status
uv run luckjingle-print raw AA:BB:CC:DD:EE:FF 10FF20F2    # query serial number
```

Add `-v` for INFO logging or `-vv` for DEBUG (shows credit/MTU negotiation
and the raw bytes returned by the printer).

## Print pipeline

```
input → PIL.Image (mode "1", width = print width) → raster bytes
      → GS v 0 envelope (1D 76 30 00 xL xH yL yH <data>)
      → enable → wake → [optional paper-type per mode] → raster → ESC J n → stop → wait 0xAA|OK
```

The pre-flight (`10 FF 40`) refuses to print if `out_of_paper` or `overheat`
is set; `--force` overrides.

## Package layout

```
src/luckjingle/
├── protocol.py          Command byte builders, status decoder, mode presets.
├── transport.py         BLE PrinterTransport with credit/MTU flow control.
├── rendering.py         text/image/pdf/qr/barcode/grid → 1-bit raster.
├── printer.py           High-level Printer class; one async method per op.
├── cli.py               Argparse subcommands (~23).
├── __main__.py          Enables `python -m luckjingle`.
└── completions/         Bundled bash + zsh completion scripts.
tests/                   127 tests (byte-level + rendering + printer + transport + CLI).
pyproject.toml           PEP 621 metadata, hatchling build, deps, dev extras.
```

## Development

```sh
uv sync            # install + lock runtime + dev deps (PEP 735 default-group)
uv run pytest -v   # run the full test suite (129 tests)
uv run pytest tests/test_transport.py::test_concurrent_exchanges_get_distinct_replies -v
uv build           # build sdist + wheel into dist/
```

Runtime deps (bleak, pillow, qrcode, python-barcode, pypdfium2, numpy) are
installed for everyone. Dev deps (pytest) come from the `dev` dependency
group and are synced by default — no `--extra` flag needed.

## Out of scope

- Firmware OTA update (bricking risk).
- OCR (needs Tesseract).
- The app's UI workflows (mistake collection, exam papers, template gallery).
- Sheet-label / TSPL printing (different protocol).
- Native text — the firmware has no fonts; every print path rasterises.
