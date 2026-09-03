"""`primer-vault --help` agrees with the program.

The Terminal edition has one command and no modes, which is exactly the kind of
claim that rots: a flag gets deleted from the dispatcher and lives on in the
usage text, or a new one appears and is never documented. These tests hold the
help and the program to each other.
"""

import io
import sys
from contextlib import redirect_stdout

import pytest

#: Flags that used to choose how Vault ran. There are no modes now - the
#: edition decides the interface and the instance lock decides start-or-attach -
#: so seeing any of these in the usage text means the help is describing a
#: program that no longer exists.
DELETED_MODE_FLAGS = ("--cli", "--headless", "--gui", "--unattended",
                      "--admin-open", "--admin-port")


def _usage_text() -> str:
    from primer_vault import app_terminal

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        app_terminal.print_usage()
    return buffer.getvalue()


def test_bare_primer_vault_opens_a_session(monkeypatch, tmp_path):
    """With a terminal attached and no arguments, `primer-vault` opens a
    session against the engine - it does not open a window, and it does not
    need a flag to say so."""
    from primer_vault import app_terminal

    called = []

    class _Control:
        def stop(self):
            called.append("control-stopped")

    class _Core:
        def is_server_running(self):
            return False

        def lock_wallet(self):
            return True

        def release_instance_lock(self):
            return None

    monkeypatch.setattr(app_terminal, "_configure_console_encoding", lambda: None)
    monkeypatch.setattr(app_terminal, "_start_engine",
                        lambda data_dir: (_Core(), _Control()))
    monkeypatch.setattr(app_terminal, "_install_signal_handlers", lambda shutdown: None)
    monkeypatch.setattr("primer_vault.utils.get_app_dir", lambda: tmp_path)
    monkeypatch.setattr("primer_vault.services.logging.configure_logging", lambda: None)
    monkeypatch.setattr("primer_vault.terminal.session.run_interactive",
                        lambda backend: called.append("session"))
    monkeypatch.setattr(sys, "argv", ["primer-vault"])

    class _Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Terminal())

    with pytest.raises(SystemExit) as exit_info:
        app_terminal.main()

    assert exit_info.value.code == 0
    assert "session" in called, (
        "A bare `primer-vault` should open a session. If that changed, this "
        "test and the usage text both need updating."
    )
    assert "control-stopped" in called, (
        "The control channel must be closed on the way out, or the endpoint "
        "file outlives the engine and the next attach connects to nothing."
    )


def test_the_help_does_not_advertise_a_deleted_mode_flag():
    usage = _usage_text()
    surviving = [flag for flag in DELETED_MODE_FLAGS if flag in usage]

    assert not surviving, (
        "`primer-vault --help` still offers " + ", ".join(surviving)
        + ", which the program no longer accepts. There are no mode flags: the "
          "edition decides the interface, and the instance lock decides whether "
          "this process is the engine or attaches to one."
    )


def test_the_help_describes_the_no_argument_form_as_a_session():
    """The first thing the usage text says about a bare `primer-vault` has to
    match what running it does."""
    usage = _usage_text()

    line = next(
        (l for l in usage.splitlines()
         if l.strip().startswith("primer-vault") and "<command>" not in l
         and "install-service" not in l),
        None,
    )
    assert line is not None, "The usage text never shows the no-argument form"
    assert "session" in line.lower(), (
        f"The no-argument form is described as {line.strip()!r}, but running it "
        "opens a session."
    )


def test_the_help_points_at_the_environment_variable_for_passwords():
    """A password on the command line is readable by every other user on the
    machine and lands in shell history. The help has to say so, because the
    unattended case is exactly where somebody reaches for the flag."""
    usage = _usage_text()
    assert "PRIMER_VAULT_PASSWORD" in usage


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
