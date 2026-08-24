"""Four things a reader meets in the first ten minutes.

  * `config help` offers `rpc <id> <url|->` to "set or clear" a chain's RPC
    endpoint, so `-` has to clear it.
  * A downloaded Vault.exe is a windowed build with no console; asked for `--cli`
    it has to say so somewhere the user can see.
  * README.md, "Headless Mode", gives a command naming both ports. A port
    already taken has to be reported, not announced as open or raised as a
    traceback.

All are read from the shipped code, not from a live install.
"""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _occupied_port() -> tuple[int, socket.socket]:
    """A bound, listening port, and the socket holding it."""
    s = socket.socket()
    if sys.platform == "win32":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s.getsockname()[1], s


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =========================================================================
# 1. `config help` documents a clear token the setter does not accept
# =========================================================================

def test_the_clear_token_config_help_documents_actually_clears_the_endpoint(tmp_path):
    """`config help` says `rpc <id> <url|->` sets *or clears* an endpoint.

    A reader who follows that and types `config set rpc 4663 -` must end up
    with no custom endpoint - the same state `config set rpc 4663 default`
    produces. Storing "-" as the endpoint leaves the chain unreachable.
    """
    import re

    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    core = Vault(data_dir=tmp_path / "data")
    handler = CommandHandler(core)

    help_text = handler.execute("config help").output
    tokens = re.search(r"rpc <id> <url\|([^>]*)>", help_text)
    assert tokens, (
        "config help no longer documents an rpc clear token - test needs updating")
    clear_token = tokens.group(1)

    handler.execute("config set rpc 4663 https://example.invalid/rpc")
    assert core.settings_manager.get_rpc_endpoint(4663) == "https://example.invalid/rpc"

    handler.execute(f"config set rpc 4663 {clear_token}")

    assert not core.settings_manager.get_rpc_endpoint(4663), (
        f"`config help` offers {clear_token!r} to clear the RPC endpoint, but "
        f"`config set rpc 4663 {clear_token}` stored "
        f"{core.settings_manager.get_rpc_endpoint(4663)!r} as the endpoint. "
        "Every on-chain read then fails with '(connection failed)', and nothing "
        "points the user back at the setting they just changed.")


# =========================================================================
# 2. A windowed build asked for --cli says nothing at all
# =========================================================================

def test_a_windowed_build_asked_for_cli_tells_the_user_something(tmp_path, monkeypatch):
    """`Vault.exe --cli` must not exit in silence.

    Vault.spec builds with console=False, so the downloaded executable has no
    stdin, stdout or stderr. cli.main() reaches `elif sys.stdin is None` and
    calls sys.exit(1) - correct when run_gui() handed over after already
    explaining itself, but this path is also reached when the user typed --cli,
    and then nothing has been said. app.py already has
    _report_without_a_console() for exactly this situation (a Windows message
    box, plus a log line).
    """
    from primer_vault import app, utils

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(utils, "get_app_dir", lambda: data_dir)
    monkeypatch.setattr("primer_vault.cli.get_app_dir", lambda: data_dir)

    # A frozen, windowed build: no standard streams at all.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "argv", ["Vault.exe", "--cli"])

    told = []
    monkeypatch.setattr(app, "_report_without_a_console", lambda msg: told.append(msg))

    with pytest.raises(SystemExit) as exit_info:
        app.main()

    assert exit_info.value.code == 1
    assert told, (
        "Vault.exe --cli exited 1 without a word: no console to print to, no "
        "message box, no window. The user cannot tell whether it worked, "
        "crashed, or was blocked by their antivirus.")


# =========================================================================
# 3 & 4. README "Headless Mode" names both ports; neither clash is handled
# =========================================================================

def test_the_daemon_does_not_report_an_agent_api_it_failed_to_open(tmp_path):
    """README.md sends the operator to
    `primer-vault --headless --agent-port 4663 --admin-port 4664 --wallet main`.

    If 4663 is already taken, Vault.start_server() returns False, Daemon.start()
    ignores the return value, and run_daemon prints
    "Agent API: http://localhost:4663". Nothing is listening there for Vault -
    whatever took the port is. A headless operator has no window to check
    against and is told the thing they cannot see is working.
    """
    from primer_vault.daemon.app import Daemon

    agent_port, blocker = _occupied_port()
    try:
        daemon = Daemon(
            data_dir=tmp_path / "data",
            admin_port=_free_port(),
            agent_port=agent_port,
        )
        try:
            daemon.start()
            agent_api_up = daemon._core.is_server_running()
            reported_up = daemon.is_running()
        finally:
            daemon.stop()
    finally:
        blocker.close()

    assert not agent_api_up, "the occupied port was not occupied"
    assert not reported_up, (
        f"the agent API could not bind port {agent_port}, but the daemon still "
        "reports itself started, logs 'Daemon started - ... Agent API on "
        f":{agent_port}', and run_daemon prints "
        f"'Agent API: http://localhost:{agent_port}'. No warning is logged.")


def test_the_daemon_explains_an_occupied_admin_port_instead_of_crashing(tmp_path):
    """Same command, admin port taken instead.

    run_daemon catches InstanceAlreadyRunning and DataDirectoryError and prints
    a sentence for each. A bind failure on the admin port is neither, so it
    leaves as a bare OSError traceback - the one failure mode run_gui() takes
    care to handle ("Run on without the admin API rather than dying for it",
    app.py).
    """
    from primer_vault.daemon.app import run_daemon

    admin_port, blocker = _occupied_port()
    try:
        with pytest.raises(BaseException) as raised:
            run_daemon(
                data_dir=tmp_path / "data",
                admin_port=admin_port,
                agent_port=_free_port(),
                interactive=False,
            )
    finally:
        blocker.close()

    assert isinstance(raised.value, SystemExit), (
        f"starting the headless daemon with port {admin_port} already in use "
        f"raised {type(raised.value).__name__}: {raised.value!r}. The operator "
        "gets a Python traceback rather than a sentence naming the port.")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
