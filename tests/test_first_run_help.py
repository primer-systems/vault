"""README claims and the program's own help, for a first run.

Three things a reader meets almost immediately:

  * README.md, "Single Instance": with the default Admin API setting, a running
    Vault answers the CLI with status only.
  * `primer-vault --help`, "Examples": the commands it offers to copy.
  * `help`, last line: "Use <command> --help for detailed options."

Each test takes one at face value and checks the shipped code honours it.
"""

import io
import re
import socket
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =========================================================================
# 1. README.md, "Single Instance"
# =========================================================================

def test_a_second_primer_vault_attaches_instead_of_being_refused(tmp_path):
    """README.md, "Single Instance".

    One process per data directory, because two would each save whole files and
    erase the other's spend records and seeds. What used to follow from that was
    a refusal: a second `primer-vault` printed "Vault is already running" and
    stopped, and the only way in was an unauthenticated HTTP port that shipped
    switched off.

    It now attaches. The lock is no longer an error path, it is the router - and
    an engine started by the system at boot has to be reachable, or a queued
    approval can never be answered.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler
    from primer_vault.instance_lock import InstanceAlreadyRunning
    from primer_vault.terminal.control_client import ControlClient
    from primer_vault.terminal.control_server import ControlServer

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    running = Vault(data_dir=data_dir)
    channel = ControlServer(running, data_dir)
    channel.start()
    try:
        # A second process on the same data directory is refused the lock...
        with pytest.raises(InstanceAlreadyRunning):
            Vault(data_dir=data_dir)

        # ...and reaches the first one over the control channel instead.
        client = ControlClient(data_dir)
        client.connect()
        try:
            result = client.execute("status")
        finally:
            client.close()
    finally:
        channel.stop()
        running.settings_manager.stop()
        running.release_instance_lock()

    assert result.success, (
        f"`status` was refused against a running engine: {result.error!r}. "
        "An attached terminal runs every command, not a privileged subset.")
    assert result.output

    # The same command, run in-process, is the same command. There is one
    # CommandHandler and one implementation behind it.
    direct = CommandHandler(Vault(data_dir=tmp_path / "other")).execute("status")
    assert direct.success


# =========================================================================
# 2. `primer-vault --help` examples
# =========================================================================

def _usage_examples() -> list[str]:
    """The `primer-vault ...` command lines under Examples in print_usage()."""
    from primer_vault import app_terminal

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        app_terminal.print_usage()
    usage = buffer.getvalue()

    block = usage.split("Examples:", 1)[1].split("\n\n", 1)[0]
    found = []
    for line in block.splitlines():
        line = line.strip()
        # Strip a leading environment assignment (VAR="..." primer-vault ...)
        line = re.sub(r'^\w+="[^"]*"\s+', "", line)
        if line.startswith("primer-vault "):
            found.append(line[len("primer-vault "):])
    return found


def test_every_example_in_the_usage_text_is_a_command_that_exists(tmp_path):
    """`primer-vault --help` prints a short Examples block. A reader copies one
    of those lines verbatim - that is what an example is for - so each has to
    name a subcommand the program actually routes.

    Only the routing is checked here: an example may legitimately fail for want
    of a wallet or a policy. "Unknown subcommand" means the line was never a
    command at all.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler
    from primer_vault.terminal.session import parse_global_flags

    examples = _usage_examples()
    assert examples, "could not read the Examples block - test needs updating"

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        handler = CommandHandler(core)
        bogus = []
        for example in examples:
            args, _ = parse_global_flags(example.split())
            result = handler.execute(" ".join(args))
            if not result.success and re.search(
                    r"unknown (sub)?command", result.error or "", re.I):
                bogus.append(f"`primer-vault {example}` -> {result.error!r}")
    finally:
        core.settings_manager.stop()
        core.release_instance_lock()

    assert not bogus, (
        "`primer-vault --help` offers examples that are not commands: "
        + "; ".join(bogus)
    )


# =========================================================================
# 3. `help` says every command group takes --help
# =========================================================================

def _help_command_groups(handler) -> list[str]:
    """Command names the routing table knows, minus the bare console verbs."""
    console_verbs = {"help", "clear", "exit", "status", "pending",
                     "approve", "reject"}
    return sorted(set(handler._commands) - console_verbs)


def test_every_command_group_answers_its_own_help_flag(tmp_path):
    """The last line of `help` is:

        Use <command> --help for detailed options.

    and `primer-vault --help` repeats it as `primer-vault <command> --help`.
    A group that treats `--help` as an unknown subcommand contradicts both.
    """
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        handler = CommandHandler(core)
        assert "Use <command> --help for detailed options." in \
            handler.execute("help").output, "help text changed - update this test"

        refused = []
        for group in _help_command_groups(handler):
            result = handler.execute(f"{group} --help")
            if not result.success:
                refused.append(f"`{group} --help` -> {result.error!r}")
    finally:
        core.settings_manager.stop()
        core.release_instance_lock()

    assert not refused, (
        "command groups that refuse the --help their own help text promises: "
        + "; ".join(refused)
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# =========================================================================
# 4. The first `address balance` on a brand-new wallet
# =========================================================================

def test_a_never_used_address_checks_its_balance_without_a_console_error(
        tmp_path, caplog):
    """A brand-new address has no on-chain history, and Robinhood Chain's
    Blockscout answers 500 for exactly that case (verified live: an unused
    address returns 500, a funded contract returns 200).

    `BlockscoutClient._request` logs that at WARNING (networks.py). The CLI
    never calls configure_logging(), so Python's last-resort handler puts the
    line - including the user's own address - straight on stderr. The first
    `address balance` after `wallet create` therefore prints what reads as an
    error before it prints the balance, for a wallet that is working normally.

    README.md, Network Calls: "Both fail gracefully."
    """
    import logging
    import urllib.error
    from primer_vault import networks
    from primer_vault.core import Vault
    from primer_vault.commands import CommandHandler

    def urlopen_500(request, *args, **kwargs):
        """Answer every Blockscout call the way the live service answers for an
        address with no on-chain history."""
        url = request.full_url if hasattr(request, "full_url") else str(request)
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    core = Vault(data_dir=data_dir)
    try:
        handler = CommandHandler(core)
        result = handler.execute("wallet create main")
        while result.needs_input:
            result = handler.execute(
                "wallet create main",
                inputs={"password": "a-strong-passphrase",
                        "value": "a-strong-passphrase"})
        assert result.success, result.error

        original = networks.urllib.request.urlopen
        networks.urllib.request.urlopen = urlopen_500
        try:
            with caplog.at_level(logging.WARNING, logger="primer_vault.networks"):
                handler.execute("address balance")
        finally:
            networks.urllib.request.urlopen = original
    finally:
        core.settings_manager.stop()
        core.release_instance_lock()

    noisy = [r.getMessage() for r in caplog.records
             if r.levelno >= logging.WARNING and "Blockscout" in r.getMessage()]

    assert not noisy, (
        "checking the balance of a never-used address writes what looks like an "
        "error to the console of a CLI that configures no logging: "
        + "; ".join(noisy)
    )
