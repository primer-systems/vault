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

def test_a_windowed_build_that_cannot_open_a_window_tells_the_user_something(
        tmp_path, monkeypatch):
    """Vault.exe must not exit in silence when Qt will not start.

    Vault.spec builds with console=False, so the downloaded executable has no
    stdin, stdout or stderr. If Qt is missing or cannot open a display there is
    nothing to print to and no window to put a dialog in, and the process would
    otherwise close having said nothing at all - indistinguishable, to the
    person who double-clicked it, from an antivirus block.

    `_report_without_a_console` is the answer: a message box straight from
    user32, plus a line in the log. This test exists because the desktop
    edition no longer falls back to a terminal interface - it does not contain
    one - so this really is the last channel left.
    """
    from primer_vault import app_desktop

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    told = []
    monkeypatch.setattr(app_desktop, "_report_without_a_console",
                        lambda msg: told.append(msg))

    app_desktop._gui_unavailable(ImportError("No module named 'PyQt6'"))

    assert told, (
        "Vault.exe closed without a word: no console to print to, no message "
        "box, no window. The user cannot tell whether it worked, crashed, or "
        "was blocked by their antivirus.")
    assert "primer-vault" in told[0], (
        "The message should point at the terminal edition, which is the thing "
        "that does work on a machine with no graphics stack.")


# =========================================================================
# 3 & 4. README "Headless Mode" names both ports; neither clash is handled
# =========================================================================

def test_an_agent_api_that_could_not_bind_is_not_reported_as_up(tmp_path):
    """A machine set to serve agents from boot has nobody watching it.

    `config set start-agent-api on` makes Vault open the agent API for itself at
    launch. If the port is already taken, `start_server()` returns False - and
    the one thing that must not happen is for Vault to carry on as though agents
    can reach it. There is no window to check against and no operator to notice.
    """
    from primer_vault.app_terminal import _apply_startup_settings
    from primer_vault.core import Vault

    agent_port, blocker = _occupied_port()
    try:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        core = Vault(data_dir=data_dir)
        try:
            core.settings_manager.set_default_port(agent_port)
            core.settings_manager.set_start_agent_api(True)

            _apply_startup_settings(core)

            assert not core.is_server_running(), (
                f"the agent API reports itself running on port {agent_port}, "
                "which was already taken by something else. Agents would be "
                "talking to whatever holds that port."
            )
        finally:
            core.settings_manager.stop()
            core.release_instance_lock()
    finally:
        blocker.close()


def test_a_startup_wallet_with_no_password_does_not_claim_to_be_open(tmp_path):
    """The wallet name is a setting; its password is not, and comes from the
    environment. If the environment is missing it, Vault has to come up locked
    rather than pretending otherwise - a server that believes it is unlocked
    and cannot sign is worse than one that says it is locked.
    """
    import os

    from primer_vault.app_terminal import _apply_startup_settings
    from primer_vault.core import Vault

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        core.settings_manager.set_startup_wallet("main")
        previous = os.environ.pop("PRIMER_VAULT_PASSWORD", None)
        try:
            _apply_startup_settings(core)
        finally:
            if previous is not None:
                os.environ["PRIMER_VAULT_PASSWORD"] = previous

        assert not core.is_wallet_unlocked()
    finally:
        core.settings_manager.stop()
        core.release_instance_lock()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
