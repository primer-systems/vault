#!/usr/bin/env python3
"""Vault Desktop - the composition root for the desktop edition.

One window, one process, and no way in from outside it. There is no control
channel here: nothing on the machine drives the desktop app but the person
sitting at it. Anyone who wants to drive Vault from a terminal installs the
terminal edition, which owns its own engine.

This file and `app_terminal.py` are the only two places an edition is named.
Everything below them asks whether a capability is registered, never which
product is running.
"""

import logging
import sys
import traceback

logger = logging.getLogger(__name__)


def _report_without_a_console(message: str) -> None:
    """Show `message` when there is no stderr to print it to.

    A windowed build has no console, and this path runs precisely when Qt is
    unavailable - so neither printing nor a Qt dialog can reach the user, and
    the app would otherwise close with nothing shown at all. Windows can put up
    a message box straight from user32, without Qt or a console; elsewhere the
    log file is the only place left to record it.
    """
    logging.getLogger(__name__).critical(message)

    if sys.platform == "win32":
        try:
            import ctypes
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(None, message, "Vault", MB_ICONERROR)
        except Exception:
            pass  # Nothing further to try; the log line above is the record.


def _gui_unavailable(error: Exception) -> None:
    """Explain why the desktop window could not start, and stop.

    There is deliberately no fallback to a terminal interface. This is the
    desktop edition: it does not contain one, and pretending otherwise is what
    used to drag the whole terminal stack into the desktop build. Someone who
    wants Vault in a terminal installs the terminal edition.

    PyQt6 is a normal dependency of this edition, so a missing module means a
    damaged install. The more common cause is Qt being present but unable to
    open a window: on Linux it needs system graphics libraries that pip does not
    supply, and over SSH there is no display to draw on.
    """
    if isinstance(error, ImportError):
        reason = "the PyQt6 graphics library is not available"
        fix = "reinstall Vault, or run `pip install --force-reinstall primer-vault[desktop]`"
    else:
        reason = f"Qt could not open a window ({error})"
        fix = ("on Linux, install the Qt system libraries "
               "(e.g. apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1)")

    message = (f"Vault's desktop window could not start.\n\n"
               f"Reason:  {reason}\n"
               f"Fix:     {fix}\n\n"
               f"To run Vault in a terminal instead, install the terminal "
               f"edition:  pip install primer-vault")

    if sys.stderr is not None:
        print(message, file=sys.stderr)
    else:
        _report_without_a_console(message)


def main():
    """Start the desktop application."""
    # Windows: set the AppUserModelID so the taskbar shows our icon, not Python's.
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "systems.primer.primer_vault")

    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QIcon
    except ImportError as e:
        _gui_unavailable(e)
        sys.exit(1)

    from .core import Vault
    from .ui import MainWindow
    from .utils import get_assets_dir
    from .services.logging import configure_logging

    configure_logging()

    def exception_hook(exc_type, exc_value, exc_tb):
        logger.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))
        traceback.print_exception(exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_hook

    try:
        app = QApplication(sys.argv)
    except Exception as e:
        _gui_unavailable(e)
        sys.exit(1)
    app.setApplicationName("Vault")
    app.setOrganizationName("Primer")

    from .ui.theme import build_light_qss
    app.setStyleSheet(build_light_qss())

    icon_path = get_assets_dir() / "icon256.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Building the core is the first thing that touches the data folder, so an
    # unwritable location surfaces here - and so does another Vault already
    # holding that folder's lock.
    from .utils import DataDirectoryError
    from .instance_lock import InstanceAlreadyRunning
    try:
        core = Vault()
    except InstanceAlreadyRunning as e:
        from PyQt6.QtWidgets import QMessageBox
        logger.info(str(e))
        # Benign outcome, not an error: the user double-launched. The running
        # instance keeps going untouched; this process explains and leaves.
        QMessageBox.information(None, "Vault is already running", e.user_message())
        sys.exit(0)
    except DataDirectoryError as e:
        from PyQt6.QtWidgets import QMessageBox
        logger.critical(str(e))
        # A plain QMessageBox rather than the themed one: this fires before any
        # window exists, and a fatal startup error is the wrong place to depend
        # on more of the UI still working.
        QMessageBox.critical(None, "Vault cannot start", e.user_message())
        sys.exit(1)

    window = MainWindow(core)
    window.show()

    addresses = window.wallet_tab.get_wallet_list()
    if addresses:
        window.update_activity(f"Loaded {len(addresses)} address(es)")
    else:
        window.update_activity("Welcome to Vault - Create a wallet to get started")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
