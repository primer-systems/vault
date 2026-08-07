#!/usr/bin/env python3
"""
Vault - x402 Agent Payment Authorization

Main entry point. Defaults to GUI mode.

Usage:
    primer_vault                     # GUI (desktop application)
    primer_vault --cli               # CLI interactive REPL
    primer_vault wallet list         # CLI single command
    primer_vault --headless          # Daemon only (no GUI)
"""

import sys
from pathlib import Path


def run_gui():
    """Run the GUI application."""
    # Windows: Set AppUserModelID so taskbar shows our icon, not Python's
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("systems.primer.primer_vault")

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon

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

    app = QApplication(sys.argv)
    app.setApplicationName("Vault")
    app.setOrganizationName("Primer")

    # Primer design-system stylesheet: light "cream" body. The dark header sets its
    # own background inline, so it stays dark (widget stylesheets beat the app one).
    from .ui.theme import build_light_qss
    app.setStyleSheet(build_light_qss())

    icon_path = get_assets_dir() / "icon256.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Create core (owns all state and logic)
    core = Vault()

    # Start admin API so CLI instances can share this core (single-instance shared state)
    try:
        from .daemon.admin_api import AdminAPIServer
        admin_api = AdminAPIServer(core, port=4664)
        admin_api.start()
    except OSError:
        pass  # Port already in use - another instance is running, proceed standalone

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
                 wallet: str = None, password: str = None):
    """Run daemon only (no GUI)."""
    from .daemon.app import run_daemon
    run_daemon(
        admin_port=admin_port,
        agent_port=agent_port,
        allow_lan=allow_lan,
        interactive=True,
        startup_wallet=wallet,
        startup_password=password,
    )


def run_cli():
    """Run CLI mode."""
    from .cli import main as cli_main
    cli_main()


def main():
    """Main entry point."""
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
        parser.add_argument("--password", type=str,
                            default=os.environ.get("PRIMER_VAULT_PASSWORD", None),
                            help="Wallet password (or set PRIMER_VAULT_PASSWORD env var)")
        args = parser.parse_args()
        run_headless(args.admin_port, args.agent_port, args.allow_lan,
                     wallet=args.wallet, password=args.password)
        return

    # Check for explicit CLI flag (for REPL)
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
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
