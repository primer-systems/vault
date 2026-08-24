#!/usr/bin/env python3
"""
Vault - x402 Agent Payment Authorization

Main entry point. Defaults to GUI mode.

Usage:
    primer_vault                     # GUI (desktop application)
    primer_vault --cli               # CLI interactive REPL
    primer_vault status              # CLI single command
    primer_vault --headless          # Daemon only (no GUI)
"""

import sys


def _gui_unavailable(error: Exception) -> None:
    """Explain why the desktop window could not start, then hand over to the CLI.

    PyQt6 is a normal dependency, so a missing module means a damaged install.
    The more common cause is Qt being present but unable to open a window: on
    Linux it needs system graphics libraries that pip does not supply, and over
    SSH there is no display to draw on. Either way the user gets a sentence they
    can act on instead of a traceback.
    """
    if isinstance(error, ImportError):
        reason = "the PyQt6 graphics library is not available"
        fix = "pip install --upgrade --force-reinstall primer-vault"
    else:
        reason = f"Qt could not open a window ({error})"
        fix = ("on Linux, install the Qt system libraries "
               "(e.g. apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1)")

    if sys.stderr is not None:
        print("Vault's desktop window could not start.", file=sys.stderr)
        print(f"  Reason:  {reason}", file=sys.stderr)
        print(f"  Fix:     {fix}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Starting in terminal mode instead. Type 'help' for commands.",
              file=sys.stderr)
        print("", file=sys.stderr)
    else:
        _report_without_a_console(
            f"Vault's desktop window could not start.\n\n{reason}\n\n{fix}")


def _report_without_a_console(message: str) -> None:
    """Show `message` when there is no stderr to print it to.

    A windowed build has no console, and this path runs precisely when Qt is
    unavailable - so neither printing nor a Qt dialog can reach the user, and
    the app would otherwise close with nothing shown at all. Windows can put up
    a message box straight from user32, without Qt or a console; elsewhere the
    log file is the only place left to record it.
    """
    import logging
    logging.getLogger(__name__).critical(message)

    if sys.platform == "win32":
        try:
            import ctypes
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(None, message, "Vault", MB_ICONERROR)
        except Exception:
            pass  # Nothing further to try; the log line above is the record.


def run_gui():
    """Run the GUI application.

    Falls back to the CLI if the desktop window cannot be started.
    """
    # Windows: Set AppUserModelID so taskbar shows our icon, not Python's
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("systems.primer.primer_vault")

    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QIcon
    except ImportError as e:
        _gui_unavailable(e)
        run_cli()
        return

    from .core import Vault
    from .ui import MainWindow
    from .utils import get_assets_dir
    from .services.logging import configure_logging
    import logging
    import traceback

    configure_logging()

    # Set up global exception hook to catch crashes
    def exception_hook(exc_type, exc_value, exc_tb):
        logger = logging.getLogger(__name__)
        logger.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))
        # Also print to stderr for console visibility
        traceback.print_exception(exc_type, exc_value, exc_tb)
        # Call the default hook to ensure proper termination
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_hook

    try:
        app = QApplication(sys.argv)
    except Exception as e:
        _gui_unavailable(e)
        run_cli()
        return
    app.setApplicationName("Vault")
    app.setOrganizationName("Primer")

    # Primer design-system stylesheet: light "cream" body. The dark header sets its
    # own background inline, so it stays dark (widget stylesheets beat the app one).
    from .ui.theme import build_light_qss
    app.setStyleSheet(build_light_qss())

    icon_path = get_assets_dir() / "icon256.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Create core (owns all state and logic). Building it is the first thing
    # that touches the data folder, so an unwritable location surfaces here -
    # and so does another instance already holding the folder's lock.
    from .utils import DataDirectoryError
    from .instance_lock import InstanceAlreadyRunning
    try:
        core = Vault()
    except InstanceAlreadyRunning as e:
        from PyQt6.QtWidgets import QMessageBox
        logging.getLogger(__name__).info(str(e))
        # Benign outcome, not an error: the user double-launched. The running
        # instance keeps going untouched; this process just explains and leaves.
        QMessageBox.information(None, "Vault is already running", e.user_message())
        sys.exit(0)
    except DataDirectoryError as e:
        from PyQt6.QtWidgets import QMessageBox
        logging.getLogger(__name__).critical(str(e))
        # A plain QMessageBox rather than the themed one: this fires before any
        # window exists, and a fatal startup error is the wrong place to depend
        # on more of the UI still working.
        QMessageBox.critical(None, "Vault cannot start", e.user_message())
        sys.exit(1)

    # Start admin API so CLI instances can drive this core. Reaching this line
    # means we hold the instance lock, so a bind failure is NOT another Vault on
    # our data directory - it is some other program (or another Vault install
    # with its own data directory) holding the port. Run on without the admin
    # API rather than dying for it; the CLI just cannot attach this session.
    try:
        from .daemon.admin_api import AdminAPIServer
        admin_api = AdminAPIServer(core, port=4664)
        admin_api.start()
    except OSError as e:
        logging.getLogger(__name__).warning(
            f"Admin API port 4664 unavailable ({e}); CLI attach disabled this session")

    # Show main window
    window = MainWindow(core)
    window.show()

    # Initial activity message
    addresses = window.wallet_tab.get_wallet_list()
    if addresses:
        window.update_activity(f"Loaded {len(addresses)} address(es)")
    else:
        window.update_activity("Welcome to Vault - Create a wallet to get started")

    sys.exit(app.exec())


def run_headless(admin_port: int, agent_port: int, allow_lan: bool,
                 wallet: str = None, password: str = None,
                 admin_open: bool = False):
    """Run daemon only (no GUI)."""
    from .daemon.app import run_daemon
    run_daemon(
        admin_port=admin_port,
        agent_port=agent_port,
        allow_lan=allow_lan,
        interactive=True,
        startup_wallet=wallet,
        startup_password=password,
        admin_open=admin_open,
    )


def run_cli():
    """Run CLI mode."""
    from .cli import main as cli_main
    cli_main()


def _configure_console_encoding():
    """Make stdout/stderr able to carry the non-ASCII characters we print.

    Windows picks the encoding from where output is going. A modern console gets
    UTF-8; anything else - piped, redirected to a file, or a legacy code page -
    falls back to cp1252, which cannot encode the CLI banner's block characters
    or the arrows in trade rows. `primer-vault history > trades.txt` then dies
    with UnicodeEncodeError. errors="replace" is the important half: an old
    terminal degrades to "?" instead of taking the process down.

    Guards: stdout is None in the windowed frozen build (console=False), and
    test harnesses swap in stream objects with no reconfigure().
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main():
    """Main entry point."""
    _configure_console_encoding()

    # Check for headless flag first
    if "--headless" in sys.argv:
        import argparse
        import os
        parser = argparse.ArgumentParser()
        parser.add_argument("--headless", action="store_true")
        parser.add_argument("--admin-port", type=int, default=4664)
        parser.add_argument("--agent-port", type=int, default=4663)
        parser.add_argument("--allow-lan", action="store_true")
        parser.add_argument("--wallet", type=str, default=None,
                            help="Wallet name to unlock on startup")
        parser.add_argument("--admin-open", action="store_true",
                            help="Let local processes drive the Admin API. Needed for "
                                 "CLI control of a headless daemon, since there is no "
                                 "window to enable it from. Persists as a setting.")
        parser.add_argument("--password", type=str,
                            default=os.environ.get("PRIMER_VAULT_PASSWORD", None),
                            help="Wallet password (or set PRIMER_VAULT_PASSWORD env var)")
        args = parser.parse_args()
        run_headless(args.admin_port, args.agent_port, args.allow_lan,
                     wallet=args.wallet, password=args.password,
                     admin_open=args.admin_open)
        return

    # Check for explicit CLI flag (for REPL)
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        # A windowed build (console=False, e.g. the downloaded Vault.exe) has no
        # stdin/stdout/stderr, so an interactive REPL cannot run and run_cli
        # would exit(1) in total silence - indistinguishable from an antivirus
        # block. Say so through the one channel left (a Windows message box and
        # the log), and point at the routes that do work.
        if sys.stdin is None:
            _report_without_a_console(
                "Vault has no terminal to run --cli in. This downloaded build is "
                "a windowed application. Open the Vault window and use File > "
                "Console, or install the command line with "
                "`pip install primer-vault`.")
            sys.exit(1)
        run_cli()
        return

    # Check for explicit GUI flag
    if "--gui" in sys.argv:
        sys.argv.remove("--gui")
        run_gui()
        return

    # If there are command arguments (not just the script name), run CLI
    if len(sys.argv) > 1:
        run_cli()
        return

    # If stdin is not a terminal (piped/redirected), run CLI to handle the input
    # Note: sys.stdin can be None when running as a windowed GUI app
    if sys.stdin is not None and not sys.stdin.isatty():
        run_cli()
        return

    # Default: GUI mode (no arguments)
    run_gui()


if __name__ == "__main__":
    main()
