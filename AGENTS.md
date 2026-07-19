# AGENTS.md — context for AI agents working in this repo

This file is the brief an AI agent should read before touching anything.
Read it once at the start of a session; refer back when something surprises you.

## Project at a glance

`luckjingle-print` is a Python CLI that drives a LuckJingle / DingDang
(`com.dingdang.newprint`) thermal printer over BLE. The protocol was
**reverse-engineered from the Android APK** — the canonical reference is
[`PROTOCOL.md`](./PROTOCOL.md). Treat that doc as ground truth for any byte
sequence; if code and PROTOCOL.md disagree, PROTOCOL.md wins (and the code
needs fixing).

The package is managed by **uv** with src-layout and hatchling. The CLI
binary is `luckjingle-print` (also aliased as `ljprint`, also runnable as
`python -m luckjingle`).

## Layout

```
pyproject.toml              uv project (deps, scripts, build, tool config)
uv.lock                     pinned resolutions — commit this
README.md                   user-facing quickstart
PROTOCOL.md                 BLE protocol reference (load-bearing)
AGENTS.md                   this file
src/luckjingle/
  protocol.py               byte builders, status decoder, mode presets  — pure
  transport.py              PrinterTransport: BLE + credit flow + write lock
  rendering.py              text/image/pdf/qr/barcode/grid → 1-bit raster
  printer.py                high-level Printer class (async context manager)
  cli.py                    argparse dispatch (~24 subcommands)
  __main__.py               enables `python -m luckjingle`
  completions/              bash + zsh completion scripts (bundled in wheel)
tests/                      156 tests, no real hardware required
docs/superpowers/specs/     design spec (layout superseded by uv migration)
work/                       jadx decompilation of the APK (reference)
Luck+Jingle_2.7.16_APKPure.xapk  the original Android app
```

## Development workflow

```sh
uv sync                  # install runtime + dev (PEP 735 default group)
uv run pytest -q         # run the full suite — should always be green
uv run pytest tests/test_transport.py::test_concurrent_exchanges_get_distinct_replies -v
uv run luckjingle-print --help
uv build                 # build sdist + wheel into dist/
```

Do NOT use raw `pip`, `python -m venv`, or `.venv/bin/...` paths — go through
`uv run`. The dev group (`pytest`) is synced by default; no `--extra` flag.

The 156 tests must stay green. They never touch BLE hardware — they use
`MockTransport` (in `tests/test_printer.py`) or `FakeBleakClient`
(in `tests/test_transport.py`). If you add a hardware-dependent test, mark
it clearly and keep it out of the default suite.

## Code conventions

- **`cmd_*` functions in `protocol.py`** return `bytes` for a single command.
  Pure, no I/O. Every command in `PROTOCOL.md` §3 has one.
- **`Printer` is an async context manager** — `async with Printer(mac) as p:`.
  Don't call `connect()`/`disconnect()` manually outside it.
- **All writes go through `PrinterTransport.send`** which holds `_write_lock`.
  Do not write to `client.write_gatt_char` directly. See pitfalls below.
- **Tests use duck-typed transports** injected into `Printer(mac, transport=...)`.
  The type checker complains (`cast(PrinterTransport, ...)` is used) — leave
  the casts, they're the documented seam.
- **No comments in code unless the user asked** — matches the repo style.
  Docstrings on modules and public functions are fine.
- **Python ≥ 3.10**: PEP 585 generics are OK (`dict[str, bool]`), `from __future__ import annotations` everywhere.
- **Line length 100** (`[tool.ruff]` in pyproject).

## Critical pitfalls

### 1. LSP will report false bleak/luckjingle import errors

The LSP doesn't see the uv-managed venv. Diagnostics like
`Import "bleak" could not be resolved` or `Import "luckjingle" could not be resolved`
on test files are **environmental noise**. Verify with `uv run pytest` —
that's the source of truth. Don't add `# type: ignore` or restructure code
to chase these.

### 2. Do not weaken the transport's `_write_lock` / `_expecting_reply`

The protocol has NO request/reply correlation field — replies are matched
purely by ordering. Two bugs were caught and fixed in review:
- Concurrent `exchange()` callers race on the shared reply slot
  → serialized by `_write_lock` (covers all writes, not just `wait_for=True`).
- Async events arriving while a sender is between `event.set()` and `finally`
  clobber the reply → `_expecting_reply` is consumed atomically in
  `_on_response`, only the FIRST notification becomes the reply.

Removing either guard reintroduces subtle races. The regression tests are
`test_concurrent_exchanges_get_distinct_replies` and
`test_async_event_while_idle_does_not_poison_next_reply` — sanity-checked
to actually fail without the guards.

### 3. The printer auto-sleeps

Real hardware (`60:6E:41:53:BC:3B`, model `D1Y-KD`, advertised `DP_D1_BC3B`)
goes to BLE sleep to save battery. If `info`/`print-*` returns
"Device with address … was not found", the printer is asleep. Ask the user
to press the button on the device; you can't wake it remotely.

The printer does NOT advertise the `0000ff00` service UUID in packets —
discovery is by name prefix (`DP_`, `LJ_`, `GT_`, etc.). The `scan`
subcommand handles both.

### 4. PEP 735 dev deps, not optional-dependencies

`pytest` lives in `[dependency-groups].dev`, not `[project.optional-dependencies].dev`.
That means `uv sync` (no flags) installs it. If you add a dev tool, put it
in the same group — don't recreate the old `--extra dev` pattern.

### 5. Version is single-sourced from package metadata

`src/luckjingle/__init__.py` uses `importlib.metadata.version("luckjingle-print")`
to read the version. Bump it in `pyproject.toml` only. Don't add a second
`__version__ = "..."` literal.

### 6. Completions must register the canonical binary names

The completion scripts (`src/luckjingle/completions/*`) must `complete -F` /
`#compdef` against `luckjingle-print` and `ljprint` (the installed entry-point
names), not the deleted legacy `luckjingle_print.py`. There are regression
tests in `tests/test_cli.py::test_completions_*_registers_canonical_command_names`.

## When working on the BLE stack

Before changing `protocol.py`, `transport.py`, or any command byte:
1. Re-read the relevant section of `PROTOCOL.md`.
2. Run `uv run pytest tests/test_protocol.py` — every documented command has
   an exact-byte test.
3. If verifying against real hardware, ask the user first; don't drain their
   paper or change settings without permission.
4. If you discover the protocol does something not in PROTOCOL.md, update
   the doc AND add a test — don't let them drift.

## Out of scope (don't add without asking)

- **Firmware OTA update** — bricking risk if interrupted; explicitly skipped.
- **OCR** — needs system Tesseract.
- **Sheet-label / TSPL printing** — different protocol entirely.
- **Native text commands** — the firmware has no fonts; every print path
  rasterises via `GS v 0`.
- **The Android app's UI workflows** (mistake collection, exam papers,
  template gallery, in-app image editor) — they're UI-driven and out of
  scope for a headless CLI.

## Review history

Two senior reviews have been done (see task IDs in session log). All flagged
Critical and Important issues are addressed. New substantive work should be
reviewed before being called done — dispatch a `general` subagent with the
`requesting-code-review` skill.

## Skills to load at session start

Relevant skills for this codebase:

- **brainstorming** — for new features (the design here went through it)
- **requesting-code-review** — before claiming substantive work is done
- **systematic-debugging** — for any BLE/transport bug
