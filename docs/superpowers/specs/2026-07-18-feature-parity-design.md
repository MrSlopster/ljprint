# LuckJingle CLI — Feature-Parity Build

**Date:** 2026-07-18
**Status:** Approved (CLI behavior spec); **Layout superseded** by the
uv / src-layout migration on 2026-07-18 (see `pyproject.toml` and top-level
`README.md` for current paths). The CLI surface, mode presets, error
handling, and out-of-scope notes below still apply.

**Source:** Reverse-engineered from `com.dingdang.newprint` (Luck Jingle) v2.7.16; see `PROTOCOL.md` for protocol details.

## Goal

Bring the `luckjingle_print` CLI to feature parity with the LuckJingle Android
app's printer-facing functionality, as far as a headless CLI can.

## Scope

In scope:
- All printer print paths: text, image (PNG/JPG), PDF, QR, barcode, ruled/grid paper.
- All printer settings: density, speed, paper type, heating level, shuttime, width, RTC, reset.
- Print-mode presets (normal, tattoo, water-transfer, A4) — bundle the SDK's per-mode paper-type + end-line-dot settings.
- Image pre-processing: Floyd-Steinberg dithering (matches `PrinterImageProcessor`), threshold control, contrast.
- Live status + async event monitor (subscribe to `ff01`).
- Multi-module package layout.

Out of scope (acknowledged):
- Firmware OTA update (bricking risk).
- OCR (needs system Tesseract).
- UI workflows that depend on a touchscreen (mistake-collection, exam-paper, template gallery, label/sheet TSPL, in-app image editor).
- Native text — the printer firmware has no fonts; every print path rasterises.
- The "wifi" device variant (different transport).

## Architecture

```
demo/
├── luckjingle/
│   ├── __init__.py
│   ├── protocol.py     # command byte builders, status decoder, mode/paper-type constants
│   ├── transport.py    # PrinterTransport: BLE connect, credit/MTU-aware send, disconnect
│   ├── rendering.py    # PIL Image → 1-bit raster (text/image/pdf/qr/barcode/grid)
│   ├── printer.py      # high-level Printer class (one method per operation)
│   └── cli.py          # argparse subcommands, ~25
├── luckjingle_print.py # thin entry: imports cli.main
├── tests/
│   ├── test_protocol.py
│   ├── test_rendering.py
│   └── test_printer.py
├── requirements.txt
└── README.md
```

Each module has a single responsibility and is independently testable. `protocol.py` has no I/O; `transport.py` has no rendering logic; `rendering.py` has no BLE; `printer.py` orchestrates transport + protocol + rendering; `cli.py` is a thin shell over `printer.py`.

## Print pipeline (shared)

```
input → PIL.Image (mode "1", width = print width) → raster bytes
      → GS v 0 envelope (1D 76 30 00 xL xH yL yH <data>)
      → enable → wake → [optional paper-type per mode] → raster → ESC J n → stop → wait 0xAA|OK
```

Pre-flight: query `10 FF 40`, abort (with `--force` override) if `out_of_paper` or `overheat` set.

## Print-mode presets (from `BaseNormalDevice`)

| Mode | Pre-print | Post-print | Default width |
|---|---|---|---|
| `normal` | (none) | `ESC J 80` (384px) or `ESC J 120` | 384 |
| `tattoo` | `1F 80 01 40` (paper type 1, mask 0x40) | `ESC J 80` | 384 |
| `water-transfer` | `1F 80 01 60` (paper type 1, mask 0x60) | `ESC J 80` | 384 |
| `a4` | (none) | `ESC J 120` | 832 |

`label` is intentionally omitted — sheet-label printing uses TSPL, a different protocol.

## CLI surface

Print:
```
print-text    <mac> TEXT [--mode M] [--font-size N] [--bold] [--align L|C|R]
print-image   <mac> FILE [--mode M] [--dither floyd|threshold|none] [--threshold N]
print-pdf     <mac> FILE [--mode M] [--pages RANGE]
print-qr      <mac> DATA [--mode M] [--box-size N]
print-barcode <mac> TYPE DATA [--mode M]
print-grid    <mac> --rows N --cols M [--style grid|ruled|lined]
```

Settings:
```
set-density    <mac> N
set-speed      <mac> N
set-paper-type <mac> KIND MASK
set-heating    <mac> N
set-shuttime   <mac> MINUTES
set-width      <mac> PIXELS
set-time       <mac>
reset          <mac>
```

Queries:
```
info    <mac>                 # combined snapshot
status  <mac>                 # one-shot status byte
watch   <mac> [--interval S]  # continuous; polls status + prints async events from ff01
```

Utility:
```
scan           [--duration S]
raw            <mac> HEX
gatt-map       <mac>          # full GATT dump
```

Backward compat: `print` stays as an alias for `print-text`.

## Dependencies

```
bleak>=0.21
pillow>=10.0
qrcode>=7.4
python-barcode>=0.15
pypdfium2>=4.0
numpy>=1.24
```

Optional imports: each rendering function checks for its dep at call time and raises a helpful `MissingDependencyError` if absent, so the CLI runs without e.g. `pypdfium2` until `print-pdf` is invoked.

## Testing

- `test_protocol.py`: every command builder produces exact documented bytes; status decode bitfield; mode preset tables.
- `test_rendering.py`: raster output dimensions for each input type; dithering produces different output than threshold; QR/barcode render to non-empty raster.
- `test_printer.py`: Printer class with a mock transport; verify call sequence per print mode (enable → wake → [paper type] → raster → feed → stop); verify settings send the right bytes.

No test requires real hardware. Hardware verification is done separately by running commands against the discovered printer `60:6E:41:53:BC:3B`.

## Error handling

- BLE connect failure → user-friendly message, exit 1.
- File not found / unsupported format → exit 2.
- Missing optional dependency → message naming the dep and pip command, exit 3.
- Pre-flight status check fails (out-of-paper, overheat) → warning, exit 4 unless `--force`.

## Deliverable verification

After build:
1. `pytest demo/tests/` — all green.
2. `python luckjingle_print.py --help` — shows all subcommands.
3. Live hardware smoke test: `info`, `status`, `print-qr "test"`, `print-text "test"` against the real printer.
4. Update `PROTOCOL.md` with any newly verified command bytes.
