#!/usr/bin/env python3
"""Vault Terminal - the composition root for the terminal edition.

One command, and no modes:

    primer-vault                  a session: prompt, live feed, every command
    primer-vault wallet list      run one command, print, exit
    primer-vault install-service  register with the OS service manager, exit

Whether this process *is* the engine or *talks to* one is not a mode either -
it is a fact about the machine, decided by the instance lock. Only one process
may hold a data directory (two would erase each other's spend records and
seeds), so: try to build a `Vault`; if the lock is already held, attach to the
engine that holds it over the local control channel. Either way the operator
gets the same prompt and the same commands.

There is no flag for whether a person is watching. The engine cannot know that,
so it does not try - an escalation always queues, always appears in the feed,
and expires on its own timeout if nobody answers. What a terminal changes is
only where output goes: to a prompt, or to the log file.
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _configure_console_encoding():
    """Make stdout/stderr able to carry the non-ASCII characters we print.

    Windows picks the encoding from where output is going. A modern console gets
    UTF-8; anything else - piped, redirected to a file, or a legacy code page -
    falls back to cp1252, which cannot encode the banner's block characters or
    the arrows in trade rows. `primer-vault history > trades.txt` then dies with
    UnicodeEncodeError. errors="replace" is the important half: an old terminal
    degrades to "?" instead of taking the process down.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def print_usage():
    from .version import __version__
    print(f"""Vault {__version__}

Usage:
  primer-vault                     Open a session (prompt + live feed)
  primer-vault <command> [args]    Run a single command and exit
  primer-vault install-service     Run Vault at boot, then exit

If Vault is already running on this data directory - started by you or by the
system at boot - both forms attach to it rather than starting a second one.

Flags (single-command mode):
  --yes, -y           Auto-confirm destructive actions
  --password <pw>     Password for wallet operations
  --json              Print one JSON object instead of formatted text, for
                      callers that are programs rather than people

Environment:
  PRIMER_VAULT_PASSWORD   Password for wallet operations. Prefer this to
                          --password, which other users on the machine can read
                          from the process list and which your shell records in
                          its history.
  PRIMER_VAULT_DATA_DIR   Where Vault keeps its wallet, policies and settings.
                          Set it to run two independent Vaults on one machine.

Where to set them:
  this terminal only    Linux/macOS:  export PRIMER_VAULT_PASSWORD="..."
                        Windows:      $env:PRIMER_VAULT_PASSWORD="..."
  every boot, Linux     put PRIMER_VAULT_PASSWORD=... in /etc/primer-vault.env
                        and uncomment the EnvironmentFile= line in
                        /etc/systemd/system/primer-vault.service
  every boot, Windows   System Properties > Environment Variables > System
                        variables > New

Examples:
  primer-vault status
  primer-vault agent list
  primer-vault pending
  primer-vault policy delete old-policy --yes
  PRIMER_VAULT_PASSWORD="a-strong-passphrase" primer-vault wallet open main

For command help:
  primer-vault help
  primer-vault <command> --help
""")


# --------------------------------------------------------------------- engine

def _start_engine(data_dir: Path):
    """Build the engine, apply its startup settings, and open the channel.

    Returns (core, control_server). The caller owns both and must stop them.
    """
    from .core import Vault
    from .core.interfaces import HeadlessApprovalHandler
    from .terminal.control_server import ControlServer

    core = Vault(data_dir=data_dir)

    # Queue escalations rather than refusing them outright. Whether a person is
    # here to answer is not knowable, so the request waits for its timeout and
    # anyone attached sees it in the feed.
    core.set_approval_handler(HeadlessApprovalHandler(core, auto_reject=False))

    from .terminal.hardware import register_hardware_handlers
    register_hardware_handlers(core)

    control = ControlServer(core, data_dir)
    control.start()

    _apply_startup_settings(core)
    return core, control


def _apply_startup_settings(core) -> None:
    """Unlock the configured wallet and start the agent API, if asked.

    This is what makes an unattended restart come back serving rather than
    coming back useless. Both are settings, not flags, because the thing that
    starts Vault after a reboot is a service manager and it cannot type.
    """
    settings = core.settings_manager

    wallet_name = settings.get_startup_wallet()
    password = os.environ.get("PRIMER_VAULT_PASSWORD")
    if wallet_name and password is not None:
        wallet_path = core.get_wallet_dir() / wallet_name
        if not wallet_path.suffix:
            wallet_path = wallet_path.with_suffix(".wallet")
        result = core.load_wallet(str(wallet_path), password)
        if result.get("success"):
            logger.info("Wallet '%s' unlocked at startup", wallet_name)
        else:
            logger.warning("Could not unlock '%s' at startup: %s",
                           wallet_name, result.get("error"))
    elif wallet_name:
        logger.warning(
            "Wallet '%s' is set to open at startup but PRIMER_VAULT_PASSWORD is "
            "not set, so it stays locked and nothing can be signed.", wallet_name)

    if settings.get_start_agent_api():
        port = settings.get_default_port()
        if not core.start_server(port, settings.get_allow_lan()):
            logger.error(
                "The agent API could not bind port %d - something else holds it. "
                "Vault is running, but agents cannot reach it.", port)


def _install_signal_handlers(shutdown) -> None:
    """Stop cleanly on Ctrl-C and on the signal a service manager sends.

    Without SIGTERM handling, `systemctl stop` kills the process outright and
    the wallet is never locked on the way down.
    """
    import signal

    def handler(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not the main thread, or not supported on this platform


# ----------------------------------------------------------------------- main

def main():
    _configure_console_encoding()

    args = sys.argv[1:]

    # `--help` describes how to run Vault; the `help` command describes what
    # you can type once it is running. Different questions, different answers,
    # so `help` falls through to the engine like any other command.
    if args and args[0] in ("--help", "-h"):
        print_usage()
        sys.exit(0)
    if args and args[0] in ("--version", "-V"):
        from .version import __version__
        print(f"Vault {__version__}")
        sys.exit(0)
    if args and args[0] == "install-service":
        from .terminal.service_install import install_service
        sys.exit(install_service(args[1:]))

    from .utils import get_app_dir, DataDirectoryError
    try:
        data_dir = get_app_dir()
    except DataDirectoryError as e:
        print(e.user_message(), file=sys.stderr)
        sys.exit(1)

    from .services.logging import configure_logging
    configure_logging()

    from .instance_lock import InstanceAlreadyRunning
    try:
        core, control = _start_engine(data_dir)
    except InstanceAlreadyRunning:
        sys.exit(_attach(data_dir, args))
    except DataDirectoryError as e:
        print(e.user_message(), file=sys.stderr)
        sys.exit(1)

    from .terminal.session import (LocalBackend, parse_global_flags, run_interactive,
                                   run_one_shot, run_piped)

    def shutdown():
        try:
            control.stop()
        finally:
            if core.is_server_running():
                core.stop_server()
            core.lock_wallet()
            core.release_instance_lock()

    _install_signal_handlers(shutdown)

    backend = LocalBackend(core)
    code = 0
    try:
        # Split the global flags here rather than inside run_one_shot, so that
        # `--json` reaches piped mode too. A command line that is nothing but
        # flags is not a command - it is a piped or interactive session that
        # happens to have been told how to answer.
        command_args, script_ctx = parse_global_flags(args)
        if command_args:
            code = run_one_shot(backend, command_args, script_ctx)
        elif sys.stdin is None or not sys.stdin.isatty():
            # No terminal: a service manager started us, or input is piped.
            # Either way there is nobody to prompt.
            if sys.stdin is not None:
                run_piped(backend, script_ctx)
            else:
                _run_unattended(core)
        else:
            run_interactive(backend)
    finally:
        shutdown()
    sys.exit(code)


def _run_unattended(core) -> None:
    """Serve until told to stop, with no prompt and nobody watching.

    Reached when a service manager starts Vault: there is no terminal to read
    from and no terminal to print to, so the event feed goes to the log file
    that `configure_logging` set up and this thread simply waits.
    """
    import threading

    logger.info("Vault running with no terminal attached; events go to the log")
    stop = threading.Event()

    def relay(event):
        from .terminal.session import format_event
        line = format_event(event.type.value, event.data)
        if line:
            logger.info(line)

    core.event_bus.subscribe_all(relay)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass


def _attach(data_dir: Path, args: list[str]) -> int:
    """Drive the engine that already holds this data directory."""
    from .terminal.control_client import ControlClient, NoEngineRunning
    from .terminal.session import (parse_global_flags, run_interactive, run_one_shot,
                                   run_piped)

    client = ControlClient(data_dir)
    try:
        client.connect()
    except NoEngineRunning as e:
        print(str(e), file=sys.stderr)
        print("\nAnother Vault holds this data folder, so this one cannot open "
              "it, and it is not answering on its control channel. If it is "
              "still starting, try again in a moment.", file=sys.stderr)
        return 1

    try:
        command_args, script_ctx = parse_global_flags(args)
        if command_args:
            return run_one_shot(client, command_args, script_ctx)
        if sys.stdin is not None and not sys.stdin.isatty():
            run_piped(client, script_ctx)
            return 0
        run_interactive(client)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    main()
