"""Tests for cli.py — argparse wiring, env-var default, exit codes.

Does not hit Bluetooth. Run with:  uv run pytest tests/test_cli.py -v

Verifies:
- $LUCKJINGLE_PRINTER is used when no `mac` arg is given.
- Explicit `mac` arg overrides the env var.
- Missing mac returns EXIT_BAD_ARG.
- The `print` alias (regression-tested after the earlier crash fix) has the
  same args as `print-text`.
- Each print subcommand has the expected --mode/--force flags.
"""
from __future__ import annotations

import pytest

from luckjingle import protocol
from luckjingle.cli import build_parser, main, EXIT_BAD_ARG


# ---------------------------------------------------------------------------
# Env-var default for `mac`
# ---------------------------------------------------------------------------

def test_mac_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv(protocol.DEFAULT_MAC_ENV, "AA:BB:CC:DD:EE:FF")
    args = build_parser().parse_args(["info"])
    assert args.mac == "AA:BB:CC:DD:EE:FF"


def test_explicit_mac_arg_overrides_env_var(monkeypatch):
    monkeypatch.setenv(protocol.DEFAULT_MAC_ENV, "AA:BB:CC:DD:EE:FF")
    args = build_parser().parse_args(["info", "11:22:33:44:55:66"])
    assert args.mac == "11:22:33:44:55:66"


def test_mac_is_none_when_no_arg_and_no_env_var(monkeypatch):
    monkeypatch.delenv(protocol.DEFAULT_MAC_ENV, raising=False)
    args = build_parser().parse_args(["info"])
    assert args.mac is None


def test_main_returns_bad_arg_when_mac_missing(monkeypatch, capsys):
    monkeypatch.delenv(protocol.DEFAULT_MAC_ENV, raising=False)
    rc = main(["info"])
    assert rc == EXIT_BAD_ARG
    captured = capsys.readouterr()
    assert protocol.DEFAULT_MAC_ENV in captured.err
    # Compare against the lowercased message; "printer mac" both sides.
    assert "printer mac" in captured.err.lower()


def test_main_does_not_require_mac_for_scan(monkeypatch):
    """scan has no mac positional; env-var-or-not, it must not error."""
    monkeypatch.delenv(protocol.DEFAULT_MAC_ENV, raising=False)

    called = {"yes": False}

    async def fake_scan(args):
        called["yes"] = True
        return 0

    # Patch the handler on the parsed args by intercepting asyncio.run.
    import asyncio
    real_run = asyncio.run

    def fake_run(coro):
        # coro is a coroutine from args.handler(args); we don't actually
        # want to await it — we want to assert main() got past the mac check.
        coro.close()
        called["yes"] = True
        return 0

    monkeypatch.setattr(asyncio, "run", fake_run)
    rc = main(["scan", "--duration", "0.01"])
    assert rc == 0
    assert called["yes"]


# ---------------------------------------------------------------------------
# Print alias regression (was crashing per code review)
# ---------------------------------------------------------------------------

def test_print_alias_has_same_args_as_print_text():
    """Earlier bug: `print` subparser omitted --font-size/--bold/--align,
    so cmd_print_text crashed with AttributeError. Verify the fix."""
    a_print = build_parser().parse_args(
        ["print", "AA:BB:CC:DD:EE:FF", "hello",
         "--font-size", "48", "--bold", "--align", "center"])
    a_pt = build_parser().parse_args(
        ["print-text", "AA:BB:CC:DD:EE:FF", "hello",
         "--font-size", "48", "--bold", "--align", "center"])
    assert a_print.font_size == 48
    assert a_print.bold is True
    assert a_print.align == "center"
    # Same handler — `print` really is an alias.
    assert a_print.handler is a_pt.handler


def test_print_alias_has_mode_and_force_flags():
    a = build_parser().parse_args(["print", "AA:BB:CC:DD:EE:FF", "x", "--mode", "tattoo"])
    assert a.mode == "tattoo"
    assert a.force is False


# ---------------------------------------------------------------------------
# Every print subcommand has the common print flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "print-text", "print", "print-image", "print-pdf",
    "print-qr", "print-barcode", "print-grid",
])
def test_print_subcommands_have_mode_and_force(cmd):
    args = build_parser().parse_args([cmd, "AA:BB:CC:DD:EE:FF"] + _min_args(cmd))
    assert hasattr(args, "mode")
    assert hasattr(args, "force")
    assert args.mode == "normal"


def _min_args(cmd):
    if cmd in ("print-text", "print"):
        return ["x"]
    if cmd == "print-image":
        return ["some.png"]
    if cmd == "print-pdf":
        return ["some.pdf"]
    if cmd == "print-qr":
        return ["data"]
    if cmd == "print-barcode":
        return ["code128", "data"]
    if cmd == "print-grid":
        return []
    raise ValueError(cmd)


# ---------------------------------------------------------------------------
# Settings subcommands take int args
# ---------------------------------------------------------------------------

def test_set_density_takes_int():
    args = build_parser().parse_args(["set-density", "AA:BB:CC:DD:EE:FF", "2"])
    assert args.level == 2


def test_set_shuttime_takes_int():
    args = build_parser().parse_args(["set-shuttime", "AA:BB:CC:DD:EE:FF", "30"])
    assert args.minutes == 30


# ---------------------------------------------------------------------------
# Print mode presets are exactly the documented set
# ---------------------------------------------------------------------------

def test_mode_choices_match_enum():
    args = build_parser().parse_args(
        ["print-text", "AA:BB:CC:DD:EE:FF", "x", "--mode", "water-transfer"])
    assert args.mode == "water-transfer"


def test_unknown_mode_is_rejected(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["print-text", "AA:BB:CC:DD:EE:FF", "x", "--mode", "bogus"])


# ---------------------------------------------------------------------------
# print-text / print : stdin via "-"
# ---------------------------------------------------------------------------

def test_resolve_text_arg_returns_literal_when_not_dash():
    from luckjingle.cli import _resolve_text_arg
    assert _resolve_text_arg("hello") == "hello"
    assert _resolve_text_arg("multi\nline\nstring") == "multi\nline\nstring"


def test_resolve_text_arg_reads_stdin_on_dash(monkeypatch):
    import io
    from luckjingle.cli import _resolve_text_arg
    monkeypatch.setattr("sys.stdin", io.StringIO("piped content\nsecond line"))
    assert _resolve_text_arg("-") == "piped content\nsecond line"


def test_resolve_text_arg_empty_stdin_returns_empty(monkeypatch):
    import io
    from luckjingle.cli import _resolve_text_arg
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _resolve_text_arg("-") == ""


def test_print_text_dash_arg_rejects_empty_stdin(monkeypatch, capsys):
    """`print MAC -` with empty stdin should exit EXIT_BAD_ARG, not crash."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setenv(protocol.DEFAULT_MAC_ENV, "AA:BB:CC:DD:EE:FF")

    # Short-circuit the BLE step by patching Printer to a no-op stub.
    import luckjingle.cli as cli_mod

    class _StubPrinter:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def print_text(self, *a, **kw): return None
    monkeypatch.setattr(cli_mod, "Printer", _StubPrinter)

    rc = main(["print", "-", ])
    assert rc == EXIT_BAD_ARG
    err = capsys.readouterr().err
    assert "no text to print" in err.lower()


def test_print_text_dash_arg_passes_stdin_to_printer(monkeypatch, capsys):
    """`echo foo | print MAC -` should reach Printer.print_text with the piped body."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    monkeypatch.setenv(protocol.DEFAULT_MAC_ENV, "AA:BB:CC:DD:EE:FF")

    captured: dict = {}

    import luckjingle.cli as cli_mod

    class _CapturingPrinter:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def print_text(self, text, **kw):
            captured["text"] = text
            return None
    monkeypatch.setattr(cli_mod, "Printer", _CapturingPrinter)

    rc = main(["print", "-"])
    assert rc == 0
    assert captured.get("text") == "from stdin"


# ---------------------------------------------------------------------------
# completions subcommand
# ---------------------------------------------------------------------------

def test_completions_subcommand_exists():
    args = build_parser().parse_args(["completions"])
    assert args.shell is None  # auto-detect by default
    args = build_parser().parse_args(["completions", "--shell", "bash"])
    assert args.shell == "bash"


def test_completions_unknown_shell_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["completions", "--shell", "fish"])


def test_completions_bash_prints_complete_F(capsys):
    rc = main(["completions", "--shell", "bash"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "complete -F _luckjingle_print" in out
    assert "_init_completion" in out or "COMP_WORDS" in out  # bash idiom


def test_completions_bash_registers_canonical_command_names(capsys):
    """Regression: after the uv migration, the console-script entry is
    `luckjingle-print` (hyphen), not the legacy `luckjingle_print.py` /
    `luckjingle_print` forms. The completion script MUST register against
    the actual installed binary names or Tab completion silently does nothing.
    """
    rc = main(["completions", "--shell", "bash"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "complete -F _luckjingle_print luckjingle-print" in out
    assert "complete -F _luckjingle_print ljprint" in out
    # No reference to the deleted entry-point script.
    assert "luckjingle_print.py" not in out


def test_completions_zsh_prints_compdef(capsys):
    rc = main(["completions", "--shell", "zsh"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("#compdef ")
    assert "_describe" in out  # zsh idiom


def test_completions_zsh_registers_canonical_command_names(capsys):
    """Same regression as the bash case: `#compdef` line must list the
    actual installed binary names."""
    rc = main(["completions", "--shell", "zsh"])
    assert rc == 0
    out = capsys.readouterr().out
    first_line = out.splitlines()[0]
    assert "luckjingle-print" in first_line
    assert "ljprint" in first_line


def test_completions_uses_shell_env_when_no_flag(capsys, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    rc = main(["completions"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("#compdef ")


def test_completions_errors_when_shell_undetectable(capsys, monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    rc = main(["completions"])
    assert rc == EXIT_BAD_ARG
    err = capsys.readouterr().err
    assert "shell" in err.lower()
