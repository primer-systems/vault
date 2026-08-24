"""
Ledger Hardware Wallet Dialogs.

Provides UI for:
- Connecting to a Ledger device
- Selecting derivation path
- Choosing addresses to add
- Confirming signatures on the device
"""

import logging
from typing import Callable

from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QButtonGroup, QLineEdit,
    QWidget, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

from .theme import FramelessDialog, set_role
from ..wallet.ledger import FATAL_EXCEPTIONS

logger = logging.getLogger(__name__)


class LedgerWorker(QThread):
    """Background thread for Ledger device operations.

    Always emits `finished` exactly once, with either the result or the
    exception. Callers block on that signal - a dialog waiting for it, or a
    request thread waiting on a queue it fills - so a run() that ended without
    emitting would hang them indefinitely.
    """

    finished = pyqtSignal(object)  # Result or exception
    error = pyqtSignal(str)        # Error message

    def __init__(self, func: Callable, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
        except FATAL_EXCEPTIONS:
            raise
        except BaseException as e:
            # BaseException, not Exception: ledgerblue raises bare BaseException
            # for USB write failures, which would otherwise kill this thread
            # silently and strand whoever is waiting on `finished`.
            logger.exception("Ledger operation failed")
            try:
                self.error.emit(str(e))
            finally:
                self.finished.emit(e)
            return

        self.finished.emit(result)


class LedgerConnectDialog(FramelessDialog):
    """
    Dialog for connecting a Ledger and choosing a derivation path.

    Steps:
    1. Connect - Detect device and verify Ethereum app is open
    2. Path - Select derivation path type

    Picking the actual addresses is deliberately NOT done here: on accept, the
    caller opens the same DerivationBrowserDialog the software seeds use, so
    both flows look and behave identically. On success this dialog exposes
    `device`, `path_type` and `custom_path` for building a LedgerAddressSource.
    """

    def __init__(self, parent=None):
        super().__init__("Connect Ledger", parent)
        self.setFixedWidth(500)
        self.setMinimumHeight(360)

        self.device = None          # Connected LedgerDevice
        self.path_type = None       # Chosen LedgerPathType
        self.custom_path = None     # Template, when path_type is CUSTOM
        self._worker = None

        layout = self.content_layout
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # Stacked widget for steps
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # Step 1: Connect
        self._build_connect_page()

        # Step 2: Path selection
        self._build_path_page()

        # Start on connect page
        self.stack.setCurrentIndex(0)

    def _build_connect_page(self):
        """Build the device connection page."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Instructions
        instructions = QLabel(
            "1. Connect your Ledger device via USB\n"
            "2. Unlock with your PIN\n"
            "3. Open the Ethereum app"
        )
        instructions.setProperty("role", "muted")
        layout.addWidget(instructions)

        layout.addSpacing(16)

        # Status area
        self.connect_status = QLabel("Click 'Detect' to search for device...")
        self.connect_status.setWordWrap(True)
        layout.addWidget(self.connect_status)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        btn_layout.addStretch()

        self.detect_btn = QPushButton("Detect")
        self.detect_btn.setProperty("variant", "primary")
        self.detect_btn.clicked.connect(self._on_detect)
        btn_layout.addWidget(self.detect_btn)

        layout.addLayout(btn_layout)

        self.stack.addWidget(page)

    def _build_path_page(self):
        """Build the derivation path selection page."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("Select Derivation Path")
        title.setProperty("role", "heading")
        layout.addWidget(title)

        # Path type radio buttons
        self.path_group = QButtonGroup(self)

        paths = [
            ("ledger_live", "Ledger Live", "m/44'/60'/x'/0/0", True),
            ("bip44", "BIP44 Standard", "m/44'/60'/0'/0/x", False),
            ("legacy_mew", "Legacy (MEW)", "m/44'/60'/0'/x", False),
            ("custom", "Custom", "", False),
        ]

        for path_id, name, template, is_default in paths:
            radio = QRadioButton(f"{name}")
            if template:
                radio.setToolTip(template)
            radio.setProperty("path_id", path_id)
            radio.setChecked(is_default)
            self.path_group.addButton(radio)
            layout.addWidget(radio)

            # Add template label for non-custom paths
            if template:
                template_label = QLabel(f"    {template}")
                template_label.setProperty("role", "dim")
                layout.addWidget(template_label)

        # Custom path input. Always present, enabled only when Custom is
        # selected: showing it on demand reflowed the page and squashed the
        # buttons, so it holds its space instead.
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(0, 0, 0, 0)
        custom_row.addSpacing(24)  # Align with the path templates above

        self.custom_path_input = QLineEdit()
        self.custom_path_input.setPlaceholderText("m/44'/60'/0'/0/{}")
        self.custom_path_input.setEnabled(False)
        self.custom_path_input.returnPressed.connect(self._on_path_continue)
        custom_row.addWidget(self.custom_path_input)

        layout.addLayout(custom_row)

        # Connect radio buttons to show/hide custom input
        self.path_group.buttonToggled.connect(self._on_path_type_changed)

        self.path_status = QLabel("")
        self.path_status.setWordWrap(True)
        layout.addWidget(self.path_status)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()

        back_btn = QPushButton("Back")
        back_btn.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        btn_layout.addWidget(back_btn)

        btn_layout.addStretch()

        continue_btn = QPushButton("Continue")
        continue_btn.setProperty("variant", "primary")
        continue_btn.clicked.connect(self._on_path_continue)
        btn_layout.addWidget(continue_btn)

        layout.addLayout(btn_layout)

        self.stack.addWidget(page)

    def _on_path_type_changed(self, button, checked):
        """Handle path type radio button change."""
        if not checked:
            return

        is_custom = button.property("path_id") == "custom"
        self.custom_path_input.setEnabled(is_custom)
        if is_custom:
            self.custom_path_input.setFocus()

        # Clear any stale "enter a path" complaint from a previous attempt.
        self.path_status.setText("")
        self.path_status.setProperty("role", "")
        set_role(self.path_status)

    def _on_detect(self):
        """Detect Ledger device."""
        from ..wallet.ledger import LedgerDevice

        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting...")
        self.connect_status.setText("Searching for Ledger device...")
        self.connect_status.setProperty("role", "")
        set_role(self.connect_status)

        def detect():
            return LedgerDevice.discover()

        self._worker = LedgerWorker(detect)
        self._worker.finished.connect(self._on_detect_finished)
        self._worker.start()

    def _on_detect_finished(self, result):
        """Handle detection result."""
        from ..wallet.ledger import LedgerNotFoundError, LedgerLockedError, LedgerAppNotOpenError

        self.detect_btn.setEnabled(True)
        self.detect_btn.setText("Detect")

        if isinstance(result, BaseException):
            error_msg = str(result)
            if isinstance(result, LedgerNotFoundError):
                error_msg = "No Ledger device found. Please connect your Ledger and try again."
            elif isinstance(result, LedgerLockedError):
                error_msg = "Ledger is locked. Please unlock with your PIN and try again."
            elif isinstance(result, LedgerAppNotOpenError):
                error_msg = "Please open the Ethereum app on your Ledger and try again."

            self.connect_status.setText(error_msg)
            self.connect_status.setProperty("role", "error")
            set_role(self.connect_status)
            return

        if result is None:
            self.connect_status.setText("No Ledger device found. Please connect your Ledger.")
            self.connect_status.setProperty("role", "error")
            set_role(self.connect_status)
            return

        # Success - store device and move to path selection
        self.device = result
        self.connect_status.setText("Device connected!")
        self.connect_status.setProperty("role", "success")
        set_role(self.connect_status)

        # Move to path selection after brief delay
        QTimer.singleShot(500, lambda: self.stack.setCurrentIndex(1))

    def _on_path_continue(self):
        """Validate the chosen path and finish - address picking happens next,
        in the shared DerivationBrowserDialog."""
        from ..wallet.ledger import LedgerPathType

        path_id = None
        for button in self.path_group.buttons():
            if button.isChecked():
                path_id = button.property("path_id")
                break

        path_type_map = {
            "ledger_live": LedgerPathType.LEDGER_LIVE,
            "bip44": LedgerPathType.BIP44,
            "legacy_mew": LedgerPathType.LEGACY_MEW,
            "custom": LedgerPathType.CUSTOM,
        }
        self.path_type = path_type_map.get(path_id, LedgerPathType.LEDGER_LIVE)

        if self.path_type == LedgerPathType.CUSTOM:
            self.custom_path = self.custom_path_input.text().strip()
            if not self.custom_path:
                self.path_status.setText("Enter a derivation path, e.g. m/44'/60'/0'/0/{}")
                self.path_status.setProperty("role", "error")
                set_role(self.path_status)
                return
        else:
            self.custom_path = None

        self.accept()


class LedgerSignDialog(FramelessDialog):
    """
    Dialog shown while waiting for user to confirm a signature on the Ledger device.

    Displays transaction/message details and waits for device confirmation.
    """

    def __init__(self, operation: str, details: str, parent=None):
        """
        Args:
            operation: Type of operation ("Payment", "Trade", etc.)
            details: Human-readable details of what's being signed
        """
        super().__init__("Confirm on Ledger", parent)
        self.setFixedWidth(400)

        self._cancelled = False
        self._worker = None

        layout = self.content_layout
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Instructions
        instructions = QLabel(
            "Please review and confirm the transaction\n"
            "on your Ledger device."
        )
        instructions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(instructions)

        layout.addSpacing(8)

        # Operation type
        op_label = QLabel(operation)
        op_label.setProperty("role", "heading")
        op_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(op_label)

        # Details
        details_label = QLabel(details)
        details_label.setWordWrap(True)
        details_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        details_label.setProperty("role", "muted")
        layout.addWidget(details_label)

        layout.addSpacing(16)

        # Status
        self.status_label = QLabel("Waiting for confirmation...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        layout.addStretch()

        # Cancel button
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(cancel_btn, alignment=Qt.AlignmentFlag.AlignCenter)

    def _on_cancel(self):
        """Handle cancel button."""
        self._cancelled = True
        self.reject()

    @property
    def was_cancelled(self) -> bool:
        """Check if the user cancelled the operation."""
        return self._cancelled

    def set_status(self, text: str, is_error: bool = False):
        """Update the status text."""
        self.status_label.setText(text)
        self.status_label.setProperty("role", "error" if is_error else "")
        set_role(self.status_label)

    def set_success(self):
        """Show success state."""
        self.status_label.setText("Confirmed!")
        self.status_label.setProperty("role", "success")
        set_role(self.status_label)
