"""`primer-vault --help` agrees with the program.

README.md's Quick Start is three invocations:

    primer-vault                    # GUI mode (default)
    primer-vault --cli              # Interactive terminal
    primer-vault --headless         # Daemon mode (no GUI)

`--help` is where someone who did not read the README looks next.
"""

import io
import sys
from contextlib import redirect_stdout

import pytest


def _usage_text() -> str:
    from primer_vault import cli

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        cli.print_usage()
    return buffer.getvalue()


def test_bare_primer_vault_really_does_start_the_gui(monkeypatch):
    """Pins the behaviour the next test measures the help against: with no
    arguments and a terminal attached, `primer-vault` opens the window.
    """
    from primer_vault import app

    called = []
    monkeypatch.setattr(app, "run_gui", lambda: called.append("gui"))
    monkeypatch.setattr(app, "run_cli", lambda: called.append("cli"))
    monkeypatch.setattr(app, "_configure_console_encoding", lambda: None)
    monkeypatch.setattr(sys, "argv", ["primer-vault"])

    class _Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Terminal())

    app.main()

    assert called == ["gui"], (
        "README's Quick Start says a bare `primer-vault` is GUI mode; this test "
        "needs updating if that changed"
    )


def test_the_help_does_not_describe_bare_primer_vault_as_the_repl():
    """`primer-vault --help` prints one line for the no-argument form. Running
    it opens the desktop window, and the README says so - so a help line that
    calls it interactive/REPL mode sends the reader somewhere it does not go.
    """
    usage = _usage_text()

    line = next(
        (l for l in usage.splitlines()
         if l.strip().startswith("primer-vault ") and "<command>" not in l
         and l.split()[1:2] in ([], ["Start"], ["Show"], ["Run"])),
        None,
    )
    if line is None:
        line = next(l for l in usage.splitlines()
                    if l.strip().startswith("primer-vault") and "REPL" in l.upper())

    lowered = line.lower()
    assert not ("repl" in lowered or "interactive" in lowered), (
        "`primer-vault --help` describes the no-argument form as the interactive "
        f"REPL: {line.strip()!r}. Run with no arguments it opens the desktop "
        "window instead, which is what README.md's Quick Start says it does."
    )


def test_the_help_names_the_mode_flags_the_readme_quick_start_uses():
    """The Quick Start's three invocations differ only by a top-level flag.
    None of them appear in the program's own usage text.
    """
    usage = _usage_text()
    missing = [flag for flag in ("--cli", "--headless") if flag not in usage]

    assert not missing, (
        "`primer-vault --help` never mentions " + ", ".join(missing)
        + ", although README.md's Quick Start uses them to choose how Vault "
          "runs. The usage text lists only the flags that apply once you are "
          "already in single-command mode."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
