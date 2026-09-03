"""
Wallet UI Dialogs - Setup and management dialogs.

Provides dialogs for:
- First-run wallet setup (create/import)
- Password entry (unlock)
- Seed phrase backup display
- Wallet management
"""

from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QTextEdit, QWidget, QCheckBox, QComboBox, QSpinBox, QFrame, QGroupBox, QListWidget, QListWidgetItem, QFormLayout
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

from ..wallet.crypto import (VaultWallet, MIN_PASSWORD_LENGTH, WeakPasswordError,
                             validate_wallet_password, CorruptedWalletFile,
                             UnsupportedWalletVersion)
from eth_account.hdaccount import generate_mnemonic

from .dialogs import copy_sensitive_to_clipboard
from .theme import Theme, FramelessDialog, FramelessMessageBox, set_role
from .scrub import scrub_dialog


# ============================================
# Add Address Dialog (New Multi-Seed System)
# ============================================

class AddAddressDialog(FramelessDialog):
    """
    Dialog for adding a new address to the wallet.

    Options:
    1. Derive from existing seed (if seeds exist)
    2. Create new seed phrase
    3. Import seed phrase
    4. Import private key
    5. Connect Ledger hardware wallet
    """

    BUTTON_WIDTH = 200

    def __init__(self, wallet: VaultWallet, parent=None):
        super().__init__("Add Address", parent)
        self.setFixedWidth(450)
        self.setMinimumHeight(380)  # Slightly taller for Ledger section

        self.wallet = wallet
        self.choice = None  # 'existing_seed', 'new_seed', 'import_seed', 'import_pkey', 'ledger'
        self.selected_seed_id = None
        self.ledger_addresses = []  # List of (path, address, path_type, name) from Ledger
        self.ledger_renames = {}    # address_id -> new name
        self.ledger_removals = set()  # address_ids to remove

        layout = self.content_layout
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Derive group (only if seeds exist)
        if wallet.seeds:
            derive_group = QGroupBox("Derive from Software Wallet")
            derive_layout = QVBoxLayout(derive_group)
            derive_layout.setSpacing(10)
            derive_layout.setContentsMargins(12, 16, 12, 12)

            # Seed selection list
            self.seed_list = QListWidget()
            self.seed_list.setMaximumHeight(80)
            self.seed_list.setAlternatingRowColors(True)

            for seed in wallet.seeds:
                addresses = wallet.get_addresses_for_seed(seed.id)
                addr_count = len(addresses)
                item_text = f"{seed.id}"
                if addr_count > 0:
                    item_text += f"  ({addr_count} address{'es' if addr_count > 1 else ''})"
                else:
                    item_text += "  (no addresses yet)"
                item = QListWidgetItem(item_text)
                item.setData(Qt.ItemDataRole.UserRole, seed.id)
                self.seed_list.addItem(item)

            # Select first by default
            if self.seed_list.count() > 0:
                self.seed_list.setCurrentRow(0)

            derive_layout.addWidget(self.seed_list)

            derive_btn = QPushButton("Add from Existing Seed")
            derive_btn.setDefault(True)
            derive_btn.clicked.connect(self.on_existing_seed)
            derive_layout.addWidget(derive_btn)

            layout.addWidget(derive_group)

        # Spacing before standalone button
        layout.addSpacing(6)

        # Create new seed button (standalone, not in a box)
        new_seed_btn = QPushButton("Create New Seed Phrase")
        new_seed_btn.setFixedWidth(self.BUTTON_WIDTH)
        if not wallet.seeds:
            new_seed_btn.setDefault(True)
        new_seed_btn.clicked.connect(self.on_new_seed)
        layout.addWidget(new_seed_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Spacing after standalone button
        layout.addSpacing(6)

        # Import group
        import_group = QGroupBox("Import")
        import_layout = QHBoxLayout(import_group)
        import_layout.setSpacing(10)
        import_layout.setContentsMargins(12, 16, 12, 12)

        import_seed_btn = QPushButton("Seed Phrase")
        import_seed_btn.clicked.connect(self.on_import_seed)
        import_layout.addWidget(import_seed_btn)

        import_pkey_btn = QPushButton("Private Key")
        import_pkey_btn.clicked.connect(self.on_import_pkey)
        import_layout.addWidget(import_pkey_btn)

        layout.addWidget(import_group)

        # Hardware Wallet group
        hw_group = QGroupBox("Hardware Wallet")
        hw_layout = QVBoxLayout(hw_group)
        hw_layout.setSpacing(10)
        hw_layout.setContentsMargins(12, 16, 12, 12)

        ledger_btn = QPushButton("Connect Ledger")
        ledger_btn.clicked.connect(self.on_connect_ledger)
        hw_layout.addWidget(ledger_btn)

        layout.addWidget(hw_group)

        layout.addStretch()

        # Bottom buttons
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(100)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

    def on_existing_seed(self):
        if hasattr(self, 'seed_list'):
            current = self.seed_list.currentItem()
            if current:
                self.selected_seed_id = current.data(Qt.ItemDataRole.UserRole)
        self.choice = 'existing_seed'
        self.accept()

    def on_new_seed(self):
        self.choice = 'new_seed'
        self.accept()

    def on_import_seed(self):
        self.choice = 'import_seed'
        self.accept()

    def on_import_pkey(self):
        self.choice = 'import_pkey'
        self.accept()

    def on_connect_ledger(self):
        """Pick Ledger addresses and close, handing them to the wallet tab."""
        result = run_ledger_address_picker(self.wallet, self)
        if result is None:
            return

        self.ledger_addresses = result.addresses
        self.ledger_renames = result.renames
        self.ledger_removals = result.removals

        if self.ledger_addresses or self.ledger_renames or self.ledger_removals:
            self.choice = 'ledger'
            self.accept()


# ============================================
# Seed Selection Dialog
# ============================================

class SeedSelectionDialog(FramelessDialog):
    """Dialog for selecting which seed to derive from."""

    def __init__(self, wallet: VaultWallet, parent=None):
        super().__init__("Select Seed", parent)
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)

        self.wallet = wallet
        self.selected_seed_id = None

        layout = self.content_layout
        layout.setSpacing(12)

        subtitle = QLabel("Choose which seed to derive a new address from")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.seed_list = QListWidget()
        self.seed_list.setAlternatingRowColors(True)
        self.seed_list.itemDoubleClicked.connect(self.on_item_double_clicked)

        # Create next_btn before connecting the signal (on_selection_changed uses it)
        self.next_btn = QPushButton("Next")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self.on_next)
        self.next_btn.setEnabled(False)  # Disabled until selection

        # Now safe to connect signal that uses next_btn
        self.seed_list.currentItemChanged.connect(self.on_selection_changed)

        for seed in wallet.seeds:
            addresses = wallet.get_addresses_for_seed(seed.id)
            addr_count = len(addresses)

            item_text = f"{seed.id}"
            if addr_count > 0:
                item_text += f"  •  {addr_count} address{'es' if addr_count > 1 else ''}"
            else:
                item_text += "  •  no addresses yet"

            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, seed.id)
            self.seed_list.addItem(item)

        # Select first item by default
        if self.seed_list.count() > 0:
            self.seed_list.setCurrentRow(0)

        layout.addWidget(self.seed_list, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        # next_btn already created earlier (before signal connection)
        btn_layout.addWidget(self.next_btn)

        layout.addLayout(btn_layout)

    def on_selection_changed(self, current, previous):
        self.next_btn.setEnabled(current is not None)

    def on_item_double_clicked(self, item):
        self.selected_seed_id = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def on_next(self):
        current = self.seed_list.currentItem()
        if current:
            self.selected_seed_id = current.data(Qt.ItemDataRole.UserRole)
            self.accept()


# ============================================
# Derivation Browser Dialog
# ============================================

from ..wallet.address_source import AddressSource, SeedAddressSource, LedgerAddressSource  # noqa: E402


@dataclass
class LedgerPickResult:
    """What the user chose in the Ledger address picker."""
    addresses: list          # (path, address, path_type, name) tuples to add
    renames: dict            # address_id -> new name
    removals: set            # address_ids to remove


def run_ledger_address_picker(wallet: VaultWallet, parent) -> Optional[LedgerPickResult]:
    """
    Connect a Ledger and pick addresses from it.

    Two steps deliberately: LedgerConnectDialog only establishes the device and
    derivation path, then the same DerivationBrowserDialog used for software
    seeds does the picking, so both flows look identical.

    Shared by "Add Address" on an existing wallet and the new-wallet wizard, so
    there is exactly one Ledger setup path.

    Args:
        wallet: Wallet the addresses will join. For a wallet being created, pass
            the empty wallet-in-progress - it is only read, to spot addresses
            that are already present.
        parent: Parent widget for the dialogs

    Returns:
        The user's selection, or None if they cancelled at either step.
    """
    from .ledger_dialog import LedgerConnectDialog

    connect = LedgerConnectDialog(parent)
    if not connect.exec() or not connect.device:
        return None

    source = LedgerAddressSource(
        wallet, connect.device, connect.path_type, connect.custom_path)
    browser = DerivationBrowserDialog(wallet, parent=parent, source=source)
    if not browser.exec():
        return None

    return LedgerPickResult(
        addresses=[
            (source.path_for(index), address, connect.path_type.value, name)
            for index, address, name in browser.get_selected_rows()
        ],
        renames=dict(browser.edited_existing),
        removals=set(browser.removed_addresses),
    )


class DerivationBrowserDialog(FramelessDialog):
    """
    Dialog for browsing and selecting addresses from a seed.

    Features:
    - Shows addresses starting from a configurable index
    - Load more button to show additional addresses
    - Checkboxes to select which addresses to add
    - Inline name editing
    - Delete entire seed option (management mode only)

    Args:
        wallet: The wallet containing the seed
        seed_id: The seed ID to derive from
        parent: Parent widget
        creation_mode: If True, hides management features (Delete Seed) and
                      pre-selects first address. Used during wallet creation.
    """

    ADDRESSES_PER_PAGE = 10

    #: Delay before acting on a start-index change, so holding the spinner
    #: arrows or typing a multi-digit index triggers one sweep, not several.
    RELOAD_DEBOUNCE_MS = 350

    def __init__(self, wallet: VaultWallet, seed_id: str = None, parent=None,
                 creation_mode: bool = False, source: AddressSource = None):
        self.source = source or SeedAddressSource(wallet, seed_id)
        title = "Select Addresses" if creation_mode else self.source.title
        super().__init__(title, parent)
        self.creation_mode = creation_mode
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)

        self.wallet = wallet
        self.seed_id = seed_id
        self._loader = None
        self._load_error = None
        self._reload_pending = False

        # Coalesces rapid start-index changes into one device sweep.
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self.refresh_list)

        self.start_index = 0
        self.addresses_shown = self.ADDRESSES_PER_PAGE
        self.selected_addresses: dict[int, str] = {}  # index -> name (for new addresses)
        self.edited_existing: dict[str, str] = {}  # address_id -> new name for existing addresses
        self.removed_addresses: set[str] = set()  # address_ids to remove
        self.delete_seed_requested: bool = False  # If True, caller should delete entire seed

        # In creation mode, pre-select first address
        if creation_mode:
            self.selected_addresses[0] = self.source.default_name(0)

        # Start at 0 to show existing addresses for editing
        # Start from zero so addresses already in the wallet stay visible.

        layout = self.content_layout
        layout.setSpacing(8)

        # Header
        header_layout = QHBoxLayout()

        if creation_mode:
            title = QLabel("Choose which addresses to add to your wallet.")
        elif seed_id:
            title = QLabel(f"Add or edit addresses from seed {seed_id}")
        else:
            title = QLabel("Choose which addresses to add to your wallet.")
        header_layout.addWidget(title)

        header_layout.addStretch()

        # Start index control
        header_layout.addWidget(QLabel("Start from:"))
        self.start_spinner = QSpinBox()
        self.start_spinner.setMinimum(0)
        self.start_spinner.setMaximum(9999)
        self.start_spinner.setValue(self.start_index)
        self.start_spinner.setFixedWidth(80)
        self.start_spinner.valueChanged.connect(self.on_start_changed)
        header_layout.addWidget(self.start_spinner)

        layout.addLayout(header_layout)

        # Scrollable address list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setSpacing(4)
        self.list_layout.setContentsMargins(0, 0, 0, 0)

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, 1)

        # Load more button
        self.load_more_btn = QPushButton("Load More...")
        self.load_more_btn.setFixedWidth(150)
        self.load_more_btn.clicked.connect(self.load_more)
        layout.addWidget(self.load_more_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Footer buttons
        btn_layout = QHBoxLayout()

        selected_label = QLabel("1 selected" if creation_mode else "0 selected")
        self.selected_label = selected_label
        btn_layout.addWidget(selected_label)

        btn_layout.addStretch()

        # Delete seed button (management mode only)
        if not creation_mode and self.source.supports_delete:
            delete_seed_btn = QPushButton("Delete Seed")
            delete_seed_btn.setToolTip("Delete this seed and all its addresses")
            delete_seed_btn.setProperty("variant", "danger")
            delete_seed_btn.clicked.connect(self.on_delete_seed)
            btn_layout.addWidget(delete_seed_btn)
            btn_layout.addSpacing(16)

        cancel_btn = QPushButton("Back" if creation_mode else "Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Finish" if creation_mode else "Save Changes")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.on_save)
        self.add_btn = save_btn  # Keep reference name for update_selection_label
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

        # Populate initial addresses
        self.refresh_list()

    def on_start_changed(self, value: int):
        self.start_index = value
        self.addresses_shown = self.ADDRESSES_PER_PAGE
        self.selected_addresses.clear()

        # Debounce: the spinner fires per keystroke and per arrow click, and
        # each refresh can mean a full sweep of the device. Typing "50" would
        # otherwise kick off a sweep for 5 and another for 50.
        self._reload_timer.start(self.RELOAD_DEBOUNCE_MS)

    def refresh_list(self):
        """Render the visible range, fetching it first if the source is slow.

        Local seed derivation is always ready, so this stays synchronous for
        software wallets. A Ledger needs a USB sweep, which runs on a worker
        thread behind a status row rather than freezing the dialog.
        """
        if self.source.is_ready(self.start_index, self.addresses_shown):
            self._render_rows()
            return

        self._clear_rows()
        self._show_status(self.source.loading_text)
        self.load_more_btn.setEnabled(False)

        # Never run two sweeps at once. The device is a single serial resource,
        # so a second worker would just queue behind the first and re-render
        # stale ranges; remember the request and run it when this one lands.
        if self._loader is not None and self._loader.isRunning():
            self._reload_pending = True
            return

        self._start_loader()

    def _start_loader(self):
        """Kick off a background fetch for the current range."""
        from .ledger_dialog import LedgerWorker

        self._reload_pending = False
        self._loader = LedgerWorker(
            self.source.prepare, self.start_index, self.addresses_shown)
        self._loader.finished.connect(self._on_range_loaded)
        self._loader.start()

    def _on_range_loaded(self, result):
        """Handle completion of a background address fetch."""
        self.load_more_btn.setEnabled(True)

        # The range moved while we were fetching - go again for the current one.
        if self._reload_pending:
            self._start_loader()
            return

        if isinstance(result, BaseException):
            self._clear_rows()
            self._show_status(str(result), is_error=True)
            self.update_selection_label()
            return

        self._render_rows()

    def _clear_rows(self):
        """Remove all rows from the list."""
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_status(self, text: str, is_error: bool = False):
        """Show a single status line where the rows would go."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setProperty("role", "error" if is_error else "muted")
        set_role(label)
        self.list_layout.addWidget(label)
        self.list_layout.addStretch()

    def _render_rows(self):
        """Draw the address rows for the current range."""
        self._clear_rows()

        for i in range(self.addresses_shown):
            self.add_address_row(self.start_index + i)

        self.list_layout.addStretch()
        self.update_selection_label()

    def add_address_row(self, index: int):
        """Add a single address row to the list."""
        row = QFrame()
        row.setFrameShape(QFrame.Shape.StyledPanel)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 4, 8, 4)

        # Derive first: whether the row is "already in the wallet" is decided by
        # what the source finds for this address, not by the index alone.
        try:
            address = self.source.derive(index)
        except Exception as e:
            address = f"Error: {e}"

        existing = self.source.existing_entry(index, address) if address.startswith("0x") else None

        # Checkbox
        checkbox = QCheckBox()
        checkbox.setProperty("index", index)

        if existing:
            checkbox.setChecked(existing.id not in self.removed_addresses)
            checkbox.setToolTip("Uncheck to remove from wallet")
            checkbox.toggled.connect(
                lambda checked, aid=existing.id: self.on_existing_toggled(aid, checked))
        else:
            checkbox.setChecked(index in self.selected_addresses)
            checkbox.toggled.connect(
                lambda checked, idx=index: self.on_checkbox_toggled(idx, checked))

        row_layout.addWidget(checkbox)

        # Index label
        index_label = QLabel(f"#{index}")
        index_label.setFixedWidth(50)
        index_label.setFont(QFont("Consolas", 10))
        row_layout.addWidget(index_label)

        # Address label (truncated)
        addr_display = f"{address[:10]}...{address[-8:]}" if len(address) > 20 else address
        addr_label = QLabel(addr_display)
        addr_label.setFont(QFont("Consolas", 10))
        addr_label.setToolTip(address)
        row_layout.addWidget(addr_label, 1)

        # Name input
        name_input = QLineEdit()
        name_input.setPlaceholderText(self.source.default_name(index))
        name_input.setFixedWidth(150)
        name_input.setProperty("index", index)

        if existing:
            # Show existing name (editable)
            name_input.setText(existing.name)
            name_input.textChanged.connect(
                lambda text, aid=existing.id: self.on_existing_name_changed(aid, text))
        else:
            name_input.textChanged.connect(lambda text, idx=index: self.on_name_changed(idx, text))
            if index in self.selected_addresses:
                name_input.setText(self.selected_addresses[index])

        row_layout.addWidget(name_input)

        self.list_layout.addWidget(row)

    def on_checkbox_toggled(self, index: int, checked: bool):
        if checked:
            # Get name from input field
            name = self.source.default_name(index)  # Default
            # Find the name input for this index
            for i in range(self.list_layout.count()):
                item = self.list_layout.itemAt(i)
                if item and item.widget():
                    row = item.widget()
                    for child in row.findChildren(QLineEdit):
                        if child.property("index") == index:
                            name = child.text() or name
                            break
            self.selected_addresses[index] = name
        else:
            self.selected_addresses.pop(index, None)

        self.update_selection_label()

    def on_name_changed(self, index: int, text: str):
        if index in self.selected_addresses:
            self.selected_addresses[index] = text or self.source.default_name(index)

    def get_selected_rows(self) -> list[tuple[int, str, str]]:
        """Newly selected addresses as (index, address, name).

        Callers that need the address itself - hardware wallets, which have no
        seed to re-derive from - use this instead of reading selected_addresses.
        """
        rows = []
        for index, name in sorted(self.selected_addresses.items()):
            rows.append((index, self.source.derive(index), name))
        return rows

    def on_existing_name_changed(self, address_id: str, text: str):
        """Track name changes for existing addresses."""
        self.edited_existing[address_id] = text

    def on_existing_toggled(self, address_id: str, checked: bool):
        """Handle toggling an existing address (for removal)."""
        if checked:
            self.removed_addresses.discard(address_id)
        else:
            self.removed_addresses.add(address_id)
            # Remove from edited if marked for removal
            self.edited_existing.pop(address_id, None)
        self.update_selection_label()

    def update_selection_label(self):
        add_count = len(self.selected_addresses)
        remove_count = len(self.removed_addresses)
        edit_count = len(self.edited_existing)

        if self.creation_mode:
            # Simple count in creation mode
            self.selected_label.setText(f"{add_count} selected")
        else:
            # Detailed changes in management mode
            parts = []
            if add_count > 0:
                parts.append(f"+{add_count}")
            if remove_count > 0:
                parts.append(f"-{remove_count}")
            if edit_count > 0:
                parts.append(f"~{edit_count} edited")
            self.selected_label.setText(", ".join(parts) if parts else "No changes")

        # Always enable Save Changes button - user can close dialog at any time
        # This avoids issues with tracking name edits back to original values
        self.add_btn.setEnabled(True)

    def load_more(self):
        self.addresses_shown += self.ADDRESSES_PER_PAGE
        self.refresh_list()

    def on_save(self):
        # In creation mode, accept if at least one address is selected
        if self.creation_mode:
            if self.selected_addresses:
                self.accept()
            return

        # In management mode, accept if there are any changes
        if self.selected_addresses or self.removed_addresses or self.edited_existing:
            self.accept()

    def on_delete_seed(self):
        """Request deletion of the entire seed."""
        # Basic confirmation (agent warning will be shown by WalletTab)
        confirmed = FramelessMessageBox.ask_question(
            self,
            "Delete Seed",
            f"Delete seed '{self.seed_id}' and all its addresses?\n\n"
            "This action cannot be undone."
        )
        if confirmed:
            self.delete_seed_requested = True
            self.accept()


# ============================================
# New Seed Creation Dialog
# ============================================

class NewSeedDialog(FramelessDialog):
    """Dialog for creating a new seed phrase."""

    BUTTON_WIDTH = 150

    def __init__(self, parent=None):
        super().__init__("Create New Seed", parent)
        self.setFixedWidth(500)

        self.seed_phrase = None

        layout = self.content_layout
        layout.setSpacing(12)

        warning = QLabel(
            "A new seed phrase will be generated. "
            "Write it down and store it safely - it's the only way to recover these addresses."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        # Generate seed phrase
        self._generated_seed = generate_mnemonic(num_words=12, lang="english")

        # Display seed phrase
        words = self._generated_seed.split()
        formatted_words = []
        for i, word in enumerate(words, 1):
            formatted_words.append(f"{i:2}. {word}")

        seed_text = ""
        for i in range(0, len(formatted_words), 3):
            row = formatted_words[i:i+3]
            seed_text += "   ".join(f"{w:<12}" for w in row) + "\n"

        seed_box = QLabel(seed_text.strip())
        seed_box.setFont(QFont("Consolas", 11))
        seed_box.setObjectName("seedBox")
        layout.addWidget(seed_box)

        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setFixedWidth(self.BUTTON_WIDTH)
        copy_btn.clicked.connect(self.copy_seed)
        layout.addWidget(copy_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch()

        self.confirm_check = QCheckBox("I have written down my seed phrase")
        layout.addWidget(self.confirm_check)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(100)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        continue_btn = QPushButton("Continue")
        continue_btn.setFixedWidth(100)
        continue_btn.setDefault(True)
        continue_btn.clicked.connect(self.on_continue)
        btn_layout.addWidget(continue_btn)

        layout.addLayout(btn_layout)

    def copy_seed(self):
        copy_sensitive_to_clipboard(self._generated_seed, self)

    def on_continue(self):
        if not self.confirm_check.isChecked():
            FramelessMessageBox.show_warning(
                self,
                "Backup Required",
                "Please confirm you have written down your seed phrase."
            )
            return
        self.seed_phrase = self._generated_seed
        self.accept()

    def scrub(self):
        """Drop the generated phrase; see ui/scrub.py for why."""
        scrub_dialog(self, attrs=("seed_phrase", "_generated_seed"))

    def close(self):
        # accept()/reject() hide the dialog, and Qt does not deliver a
        # closeEvent to a hidden widget - so scrub on close() itself too.
        self.scrub()
        return super().close()

    def closeEvent(self, event):
        self.scrub()
        super().closeEvent(event)


# ============================================
# Import Seed Dialog (for new wallet system)
# ============================================

class ImportSeedToWalletDialog(FramelessDialog):
    """Dialog for importing a seed phrase into an existing wallet."""

    def __init__(self, parent=None):
        super().__init__("Import Seed Phrase", parent)
        self.setMinimumWidth(450)

        self.seed_phrase = None

        layout = self.content_layout
        layout.setSpacing(12)

        subtitle = QLabel("Enter your 12 or 24 word recovery phrase, separated by spaces.")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.seed_input = QTextEdit()
        self.seed_input.setPlaceholderText("word1 word2 word3 ...")
        self.seed_input.setFont(QFont("Consolas", 10))
        self.seed_input.setMaximumHeight(80)
        layout.addWidget(self.seed_input)

        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        layout.addWidget(self.error_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        import_btn = QPushButton("Import")
        import_btn.setDefault(True)
        import_btn.clicked.connect(self.on_import)
        btn_layout.addWidget(import_btn)

        layout.addLayout(btn_layout)

    def on_import(self):
        seed = self.seed_input.toPlainText().strip().lower()
        words = seed.split()

        if len(words) not in [12, 24]:
            self.error_label.setText(f"Expected 12 or 24 words, got {len(words)}")
            return

        from mnemonic import Mnemonic
        mnemo = Mnemonic("english")
        seed_phrase = " ".join(words)

        if not mnemo.check(seed_phrase):
            self.error_label.setText("Invalid seed phrase. Check for typos.")
            return

        self.seed_phrase = seed_phrase
        self.accept()

    def scrub(self):
        """Drop the typed phrase; see ui/scrub.py for why."""
        scrub_dialog(self, attrs=("seed_phrase",))

    def close(self):
        # accept()/reject() hide the dialog, and Qt does not deliver a
        # closeEvent to a hidden widget - so scrub on close() itself too.
        self.scrub()
        return super().close()

    def closeEvent(self, event):
        self.scrub()
        super().closeEvent(event)


# ============================================
# Import Private Key Dialog (for new wallet system)
# ============================================

class ImportPrivateKeyToWalletDialog(FramelessDialog):
    """Dialog for importing a private key into an existing wallet."""

    def __init__(self, parent=None):
        super().__init__("Import Private Key", parent)
        self.setMinimumWidth(400)

        self.private_key = None
        self.name = None

        layout = self.content_layout
        layout.setSpacing(12)

        subtitle = QLabel("Enter a 64-character hex private key (with or without 0x prefix).")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        warning = QLabel("Note: Imported private keys show '—' in the Seed column.")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self.pkey_input = QLineEdit()
        self.pkey_input.setPlaceholderText("0x... or 64 hex characters")
        self.pkey_input.setFont(QFont("Consolas", 10))
        self.pkey_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.pkey_input)

        self.show_check = QCheckBox("Show private key")
        self.show_check.toggled.connect(self.toggle_visibility)
        layout.addWidget(self.show_check)

        layout.addSpacing(8)

        layout.addWidget(QLabel("Name (optional):"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g., 'Old Wallet', 'Hardware Export'")
        layout.addWidget(self.name_input)

        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        layout.addWidget(self.error_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        import_btn = QPushButton("Import")
        import_btn.setDefault(True)
        import_btn.clicked.connect(self.on_import)
        btn_layout.addWidget(import_btn)

        layout.addLayout(btn_layout)

    def toggle_visibility(self, checked):
        if checked:
            self.pkey_input.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.pkey_input.setEchoMode(QLineEdit.EchoMode.Password)

    def on_import(self):
        pkey = self.pkey_input.text().strip()

        if pkey.startswith("0x") or pkey.startswith("0X"):
            pkey = pkey[2:]

        if len(pkey) != 64:
            self.error_label.setText(f"Expected 64 hex characters, got {len(pkey)}")
            return

        try:
            bytes.fromhex(pkey)
        except ValueError:
            self.error_label.setText("Invalid hex characters in private key")
            return

        try:
            from eth_account import Account
            Account.from_key(bytes.fromhex(pkey))
            self.private_key = pkey
            self.name = self.name_input.text().strip() or None
            self.accept()
        except Exception as e:
            self.error_label.setText(f"Invalid private key: {str(e)}")

    def scrub(self):
        """Drop the typed key; see ui/scrub.py for why."""
        scrub_dialog(self, attrs=("private_key",))

    def close(self):
        # accept()/reject() hide the dialog, and Qt does not deliver a
        # closeEvent to a hidden widget - so scrub on close() itself too.
        self.scrub()
        return super().close()

    def closeEvent(self, event):
        self.scrub()
        super().closeEvent(event)


# ============================================
# Primer Wallet Unlock Dialog
# ============================================

class VaultWalletUnlockDialog(FramelessDialog):
    """Dialog for unlocking the Primer wallet."""

    def __init__(self, wallet_path: Path, parent=None):
        super().__init__("Unlock Wallet", parent)
        self.setFixedWidth(350)

        self.wallet_path = wallet_path
        self.wallet = None

        layout = self.content_layout
        layout.setSpacing(12)

        layout.addSpacing(16)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Enter password")
        self.password_input.returnPressed.connect(self.on_unlock)
        layout.addWidget(self.password_input)

        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        layout.addWidget(self.error_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        unlock_btn = QPushButton("Unlock")
        unlock_btn.setDefault(True)
        unlock_btn.clicked.connect(self.on_unlock)
        btn_layout.addWidget(unlock_btn)

        layout.addLayout(btn_layout)

        layout.addSpacing(16)

    def on_unlock(self):
        password = self.password_input.text()

        if not password:
            self.error_label.setText("Please enter your password")
            return

        try:
            self.wallet = VaultWallet.load(self.wallet_path, password)
            # The caller loads the same file again through the core and needs
            # the password to do it; it reads this attribute after exec() and
            # is responsible for scrubbing the dialog once it has.
            self.password = password
            self.accept()
        except (CorruptedWalletFile, UnsupportedWalletVersion) as e:
            # Both subclass ValueError but neither is a password problem, so
            # they must not fall into "Wrong password" below - retyping the
            # password cannot fix either, and saying so sends the user after
            # the wrong problem.
            self.error_label.setText(f"This wallet file cannot be read: {e}")
        except ValueError:
            self.error_label.setText("Wrong password")
            self.password_input.clear()
            self.password_input.setFocus()

    def scrub(self):
        """Lock and drop the second decrypted wallet this dialog holds.

        self.wallet is a complete unlocked VaultWallet, separate from the copy
        the core holds - Vault.lock_wallet() can never reach it, so it has to
        be locked here or the seed phrases stay decrypted in memory for the
        rest of the session.
        """
        if getattr(self, "wallet", None) is not None:
            self.wallet.lock()
        scrub_dialog(self, attrs=("wallet", "password"))

    def close(self):
        # accept()/reject() hide the dialog, and Qt does not deliver a
        # closeEvent to a hidden widget - so scrub on close() itself too.
        self.scrub()
        return super().close()

    def closeEvent(self, event):
        self.scrub()
        super().closeEvent(event)


# ============================================
# Create Wallet Wizard (First Time Setup)
# ============================================

from PyQt6.QtWidgets import QStackedWidget, QRadioButton, QButtonGroup, QScrollArea


class CreateWalletWizard(FramelessDialog):
    """
    Multi-step wizard for creating a new Primer wallet.

    Steps:
    1. Password setup
    2. Method selection (create seed / import seed / import pkey)
    3. Method-specific step (show seed / enter seed / enter pkey)
    4. Derivation browser (for seed-based methods only)
    """

    BUTTON_WIDTH = 100

    # Pages
    PAGE_PASSWORD = 0
    PAGE_METHOD = 1
    PAGE_NEW_SEED = 2
    PAGE_IMPORT_SEED = 3
    PAGE_IMPORT_PKEY = 4

    def __init__(self, parent=None):
        super().__init__("Create New Wallet", parent)
        self.setFixedWidth(600)
        self.setMinimumHeight(450)

        # Results
        self.password = None
        self.seed_phrase = None
        self.private_key = None
        self.derivation_path = "m/44'/60'/0'/0/{}"
        self.method = None  # 'new_seed', 'import_seed', 'import_pkey', 'ledger'
        self.ledger_addresses = []  # (path, address, path_type, name) tuples
        self.selected_indices = [0]  # Default to first address
        self.selected_names: dict[int, str] = {}  # index -> custom name

        layout = self.content_layout
        layout.setSpacing(12)

        # Stacked widget for pages
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # Create pages
        self._create_password_page()
        self._create_method_page()
        self._create_new_seed_page()
        self._create_import_seed_page()
        self._create_import_pkey_page()

        # Error label (shared across pages)
        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        layout.addWidget(self.error_label)

        # Navigation buttons
        nav_layout = QHBoxLayout()
        nav_layout.addStretch()

        self.back_btn = QPushButton("Back")
        self.back_btn.setFixedWidth(self.BUTTON_WIDTH)
        self.back_btn.clicked.connect(self._go_back)
        self.back_btn.setVisible(False)
        nav_layout.addWidget(self.back_btn)

        self.next_btn = QPushButton("Next")
        self.next_btn.setFixedWidth(self.BUTTON_WIDTH)
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._go_next)
        nav_layout.addWidget(self.next_btn)

        layout.addLayout(nav_layout)

        # Start on password page
        self.stack.setCurrentIndex(self.PAGE_PASSWORD)
        self._update_nav_buttons()

    def _create_password_page(self):
        """Page 1: Password setup."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Set Wallet Password")
        title.setFont(QFont("", 12, QFont.Weight.Bold))
        layout.addWidget(title)

        desc = QLabel("This password encrypts your wallet. Keep it safe.")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(8)

        self.pw_password_input = QLineEdit()
        self.pw_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_password_input.setPlaceholderText("Enter password")
        self.pw_password_input.returnPressed.connect(self._go_next)
        layout.addWidget(self.pw_password_input)

        self.pw_confirm_input = QLineEdit()
        self.pw_confirm_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_confirm_input.setPlaceholderText("Confirm password")
        self.pw_confirm_input.returnPressed.connect(self._go_next)
        layout.addWidget(self.pw_confirm_input)

        pw_hint = QLabel(f"At least {MIN_PASSWORD_LENGTH} characters.")
        pw_hint.setProperty("role", "muted")
        layout.addWidget(pw_hint)

        layout.addStretch()
        self.stack.addWidget(page)

    def _create_method_page(self):
        """Page 2: Method selection."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Choose Setup Method")
        title.setFont(QFont("", 12, QFont.Weight.Bold))
        layout.addWidget(title)

        desc = QLabel("How would you like to create your first address?")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(16)

        # Radio buttons
        self.method_group = QButtonGroup(self)

        self.method_new_seed = QRadioButton("Create new seed phrase")
        self.method_new_seed.setChecked(True)
        self.method_group.addButton(self.method_new_seed, 0)
        layout.addWidget(self.method_new_seed)

        new_seed_desc = QLabel("Generate a new 12-word recovery phrase")
        new_seed_desc.setProperty("role", "hint")
        layout.addWidget(new_seed_desc)

        layout.addSpacing(8)

        self.method_import_seed = QRadioButton("Import existing seed phrase")
        self.method_group.addButton(self.method_import_seed, 1)
        layout.addWidget(self.method_import_seed)

        import_seed_desc = QLabel("Restore from a 12 or 24-word recovery phrase")
        import_seed_desc.setProperty("role", "hint")
        layout.addWidget(import_seed_desc)

        layout.addSpacing(8)

        self.method_import_pkey = QRadioButton("Import private key")
        self.method_group.addButton(self.method_import_pkey, 2)
        layout.addWidget(self.method_import_pkey)

        import_pkey_desc = QLabel("Import a single address from a hex private key")
        import_pkey_desc.setProperty("role", "hint")
        layout.addWidget(import_pkey_desc)

        layout.addSpacing(8)

        self.method_ledger = QRadioButton("Connect Ledger")
        self.method_group.addButton(self.method_ledger, 3)
        layout.addWidget(self.method_ledger)

        ledger_desc = QLabel("Use addresses from a Ledger device — keys never leave it")
        ledger_desc.setProperty("role", "hint")
        layout.addWidget(ledger_desc)

        layout.addStretch()
        self.stack.addWidget(page)

    def _create_new_seed_page(self):
        """Page 3A: Show generated seed phrase."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Your Recovery Phrase")
        title.setFont(QFont("", 12, QFont.Weight.Bold))
        layout.addWidget(title)

        warning = QLabel(
            "Write down these 12 words and store them safely. "
            "This is the ONLY way to recover your wallet."
        )
        warning.setWordWrap(True)
        warning.setProperty("role", "warn")
        layout.addWidget(warning)

        layout.addSpacing(8)

        # Seed display box
        self.ns_seed_display = QLabel()
        self.ns_seed_display.setFont(QFont("Consolas", 11))
        self.ns_seed_display.setObjectName("seedBox")
        self.ns_seed_display.setWordWrap(True)
        self.ns_seed_display.setMinimumHeight(80)
        layout.addWidget(self.ns_seed_display)

        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setFixedWidth(150)
        copy_btn.clicked.connect(self._copy_new_seed)
        layout.addWidget(copy_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(8)

        self.ns_confirm_check = QCheckBox("I have written down my recovery phrase")
        layout.addWidget(self.ns_confirm_check)

        layout.addStretch()
        self.stack.addWidget(page)

    def _create_import_seed_page(self):
        """Page 3B: Import seed phrase."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Import Recovery Phrase")
        title.setFont(QFont("", 12, QFont.Weight.Bold))
        layout.addWidget(title)

        desc = QLabel("Enter your 12 or 24-word recovery phrase, separated by spaces.")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.is_seed_input = QTextEdit()
        self.is_seed_input.setPlaceholderText("word1 word2 word3 ...")
        self.is_seed_input.setFont(QFont("Consolas", 10))
        self.is_seed_input.setMaximumHeight(80)
        layout.addWidget(self.is_seed_input)

        # Derivation path section
        layout.addSpacing(8)
        path_label = QLabel("Derivation Path:")
        layout.addWidget(path_label)

        self.is_path_combo = QComboBox()
        self.is_path_combo.addItem("Ethereum / Base (default)", "m/44'/60'/0'/0/{}")
        self.is_path_combo.addItem("Ledger Live", "m/44'/60'/{}'/0/0")
        layout.addWidget(self.is_path_combo)

        layout.addStretch()
        self.stack.addWidget(page)

    def _create_import_pkey_page(self):
        """Page 3C: Import private key."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        title = QLabel("Import Private Key")
        title.setFont(QFont("", 12, QFont.Weight.Bold))
        layout.addWidget(title)

        desc = QLabel("Enter your 64-character hex private key (with or without 0x prefix).")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        note = QLabel("Note: Private keys cannot derive additional addresses.")
        note.setProperty("role", "hint")
        layout.addWidget(note)

        layout.addSpacing(8)

        self.ip_pkey_input = QLineEdit()
        self.ip_pkey_input.setPlaceholderText("0x... or 64 hex characters")
        self.ip_pkey_input.setFont(QFont("Consolas", 10))
        self.ip_pkey_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.ip_pkey_input)

        self.ip_show_check = QCheckBox("Show private key")
        self.ip_show_check.toggled.connect(
            lambda c: self.ip_pkey_input.setEchoMode(
                QLineEdit.EchoMode.Normal if c else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(self.ip_show_check)

        layout.addStretch()
        self.stack.addWidget(page)

    def _update_nav_buttons(self):
        """Update navigation button visibility and text."""
        current = self.stack.currentIndex()

        # Back button visible after first page
        self.back_btn.setVisible(current > self.PAGE_PASSWORD)

        # Next button text - seed pages will open derivation dialog then finish
        if current == self.PAGE_IMPORT_PKEY:
            self.next_btn.setText("Finish")
        elif current in (self.PAGE_NEW_SEED, self.PAGE_IMPORT_SEED):
            self.next_btn.setText("Next")
        else:
            self.next_btn.setText("Next")

    def _go_back(self):
        """Navigate to previous page."""
        current = self.stack.currentIndex()
        self.error_label.clear()

        if current in (self.PAGE_NEW_SEED, self.PAGE_IMPORT_SEED, self.PAGE_IMPORT_PKEY):
            self.stack.setCurrentIndex(self.PAGE_METHOD)
        elif current == self.PAGE_METHOD:
            self.stack.setCurrentIndex(self.PAGE_PASSWORD)

        self._update_nav_buttons()

    def _go_next(self):
        """Navigate to next page or finish."""
        current = self.stack.currentIndex()
        self.error_label.clear()

        if current == self.PAGE_PASSWORD:
            if self._validate_password():
                self.stack.setCurrentIndex(self.PAGE_METHOD)

        elif current == self.PAGE_METHOD:
            if self.method_new_seed.isChecked():
                self.method = 'new_seed'
                self._generate_seed()
                self.stack.setCurrentIndex(self.PAGE_NEW_SEED)
            elif self.method_import_seed.isChecked():
                self.method = 'import_seed'
                self.stack.setCurrentIndex(self.PAGE_IMPORT_SEED)
            elif self.method_ledger.isChecked():
                # No wizard page of its own: the Ledger flow has its own dialogs,
                # and finishing them completes the wizard.
                self.method = 'ledger'
                if self._pick_ledger_addresses():
                    self.accept()
            else:
                self.method = 'import_pkey'
                self.stack.setCurrentIndex(self.PAGE_IMPORT_PKEY)

        elif current == self.PAGE_NEW_SEED:
            if self._validate_new_seed():
                if self._show_derivation_dialog():
                    self.accept()

        elif current == self.PAGE_IMPORT_SEED:
            if self._validate_import_seed():
                if self._show_derivation_dialog():
                    self.accept()

        elif current == self.PAGE_IMPORT_PKEY:
            if self._validate_import_pkey():
                self.accept()

        self._update_nav_buttons()

    def _validate_password(self) -> bool:
        """Validate password page."""
        password = self.pw_password_input.text()
        confirm = self.pw_confirm_input.text()

        if not password:
            self.error_label.setText("Please enter a password")
            return False

        if password != confirm:
            self.error_label.setText("Passwords do not match")
            return False

        # Same check the wallet itself enforces; run here so the message appears
        # on the page rather than as a failure after the wizard finishes.
        try:
            validate_wallet_password(password)
        except WeakPasswordError as e:
            self.error_label.setText(str(e))
            return False

        self.password = password
        return True

    def _generate_seed(self):
        """Generate a new seed phrase and display it."""
        self._generated_seed = generate_mnemonic(num_words=12, lang="english")

        # Format for display
        words = self._generated_seed.split()
        formatted = []
        for i, word in enumerate(words, 1):
            formatted.append(f"{i:2}. {word}")

        lines = []
        for i in range(0, len(formatted), 3):
            row = formatted[i:i+3]
            lines.append("   ".join(f"{w:<12}" for w in row))

        self.ns_seed_display.setText("\n".join(lines))
        self.ns_confirm_check.setChecked(False)

    def _copy_new_seed(self):
        """Copy generated seed to clipboard with auto-clear."""
        if hasattr(self, '_generated_seed'):
            copy_sensitive_to_clipboard(self._generated_seed, self)

    def _validate_new_seed(self) -> bool:
        """Validate new seed page."""
        if not self.ns_confirm_check.isChecked():
            self.error_label.setText("Please confirm you have written down your recovery phrase")
            return False

        self.seed_phrase = self._generated_seed
        return True

    def _validate_import_seed(self) -> bool:
        """Validate import seed page."""
        seed = self.is_seed_input.toPlainText().strip().lower()
        words = seed.split()

        if len(words) not in [12, 24]:
            self.error_label.setText(f"Expected 12 or 24 words, got {len(words)}")
            return False

        from mnemonic import Mnemonic
        mnemo = Mnemonic("english")
        seed_phrase = " ".join(words)

        if not mnemo.check(seed_phrase):
            self.error_label.setText("Invalid seed phrase. Check for typos.")
            return False

        self.seed_phrase = seed_phrase

        # Keep derivation path template (with {} placeholder for index)
        self.derivation_path = self.is_path_combo.currentData()

        return True

    def _show_derivation_dialog(self) -> bool:
        """Show the derivation browser dialog and get selected addresses.

        Returns True if user completed selection, False if cancelled.
        """
        # Create a temporary wallet with the seed
        temp_wallet = VaultWallet.create(self.password)
        seed_id = temp_wallet.add_seed(self.seed_phrase, self.derivation_path)

        # Open derivation browser in creation mode
        dialog = DerivationBrowserDialog(temp_wallet, seed_id, self, creation_mode=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        # Store results - convert to indices and names
        self.selected_indices = list(dialog.selected_addresses.keys())
        self.selected_names = dialog.selected_addresses.copy()

        return len(self.selected_indices) > 0

    def _pick_ledger_addresses(self) -> bool:
        """Run the shared Ledger picker. Returns True if addresses were chosen.

        The wallet passed in is an empty stand-in: at this point the real wallet
        does not exist yet, and the picker only reads it to spot addresses that
        are already present.
        """
        result = run_ledger_address_picker(VaultWallet.create(self.password), self)
        if result is None:
            return False

        if not result.addresses:
            self.error_label.setText("Select at least one address to continue")
            return False

        self.ledger_addresses = result.addresses
        return True

    def _validate_import_pkey(self) -> bool:
        """Validate import private key page."""
        pkey = self.ip_pkey_input.text().strip()

        if pkey.startswith("0x") or pkey.startswith("0X"):
            pkey = pkey[2:]

        if len(pkey) != 64:
            self.error_label.setText(f"Expected 64 hex characters, got {len(pkey)}")
            return False

        try:
            bytes.fromhex(pkey)
        except ValueError:
            self.error_label.setText("Invalid hex characters in private key")
            return False

        try:
            from eth_account import Account
            Account.from_key(bytes.fromhex(pkey))
            self.private_key = pkey
            return True
        except Exception as e:
            self.error_label.setText(f"Invalid private key: {str(e)}")
            return False

    def scrub(self):
        """Drop the chosen password, generated/typed seed and imported key;
        see ui/scrub.py for why. The caller reads these after exec()
        and calls this once the wallet is created."""
        scrub_dialog(self, attrs=("password", "seed_phrase", "private_key",
                                  "_generated_seed"))

    def close(self):
        # accept()/reject() hide the dialog, and Qt does not deliver a
        # closeEvent to a hidden widget - so scrub on close() itself too.
        self.scrub()
        return super().close()

    def closeEvent(self, event):
        self.scrub()
        super().closeEvent(event)


# ============================================
# Add Wallet Choice Dialog
# ============================================

class AddWalletChoiceDialog(FramelessDialog):
    """Dialog for choosing how to add a wallet: load existing or create new."""

    BUTTON_WIDTH = 200

    def __init__(self, parent=None):
        super().__init__("Add Wallet", parent)
        self.setFixedWidth(350)

        self.choice = None  # 'load' or 'create'

        layout = self.content_layout
        layout.setSpacing(12)

        layout.addSpacing(16)

        load_btn = QPushButton("Load Wallet File")
        load_btn.setFixedWidth(self.BUTTON_WIDTH)
        load_btn.clicked.connect(self.on_load)
        layout.addWidget(load_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(12)

        create_btn = QPushButton("Create New Wallet")
        create_btn.setFixedWidth(self.BUTTON_WIDTH)
        create_btn.setDefault(True)
        create_btn.clicked.connect(self.on_create)
        layout.addWidget(create_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(24)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedWidth(100)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(cancel_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(16)

    def on_load(self):
        self.choice = 'load'
        self.accept()

    def on_create(self):
        self.choice = 'create'
        self.accept()


# ============================================
# Wallet Filename Dialog
# ============================================

class WalletFilenameDialog(FramelessDialog):
    """Dialog for entering a custom wallet filename."""

    def __init__(self, parent=None):
        super().__init__("Create New Wallet", parent)
        self.setFixedWidth(400)

        self.filename = None

        layout = self.content_layout
        layout.setSpacing(12)

        desc = QLabel("Enter a name for your wallet file:")
        layout.addWidget(desc)

        # Filename input with .wallet suffix
        input_layout = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("my-wallet")
        input_layout.addWidget(self.name_input)

        suffix_label = QLabel(".wallet")
        suffix_label.setProperty("role", "muted")
        input_layout.addWidget(suffix_label)

        layout.addLayout(input_layout)

        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        layout.addWidget(self.error_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        continue_btn = QPushButton("Continue")
        continue_btn.setDefault(True)
        continue_btn.clicked.connect(self.on_continue)
        btn_layout.addWidget(continue_btn)

        layout.addLayout(btn_layout)

    def on_continue(self):
        name = self.name_input.text().strip()
        if not name:
            self.error_label.setText("Please enter a filename")
            return

        # Validate filename (no special characters)
        import re
        if not re.match(r'^[\w\-]+$', name):
            self.error_label.setText("Use only letters, numbers, underscores, and hyphens")
            return

        self.filename = f"{name}.wallet"
        self.accept()


# ============================================
# Wallet Settings Dialog
# ============================================

class WalletSettingsDialog(FramelessDialog):
    """
    Wallet settings modal - manage wallet password, info, and danger zone actions.

    Sections:
    - Info: Wallet file, created date, seeds/addresses count
    - Security: Change password (or set password if unencrypted)
    - Danger Zone: Detach, Delete
    """

    def __init__(
        self,
        wallet: VaultWallet,
        wallet_path: Path,
        on_detach,  # Callback for detach action
        on_delete,  # Callback for delete action
        parent=None
    ):
        super().__init__("Wallet Settings", parent)
        self.wallet = wallet
        self.wallet_path = wallet_path
        self.on_detach = on_detach
        self.on_delete = on_delete
        self._password_changed = False

        self.setMinimumWidth(400)
        self.setMinimumHeight(350)

        self._setup_ui()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(16)

        # === INFO SECTION ===
        info_group = QGroupBox("Wallet Info")
        info_layout = QFormLayout(info_group)

        # Filename
        self.filename_label = QLabel(self.wallet_path.name)
        self.filename_label.setFont(QFont(Theme.MONO_FONT, 9))
        info_layout.addRow("File:", self.filename_label)

        # Location
        location = str(self.wallet_path.parent)
        if len(location) > 40:
            location = "..." + location[-37:]
        self.location_label = QLabel(location)
        self.location_label.setProperty("role", "muted")
        self.location_label.setToolTip(str(self.wallet_path.parent))
        info_layout.addRow("Location:", self.location_label)

        # Created date
        created = self.wallet._created_at[:10] if self.wallet._created_at else "Unknown"
        info_layout.addRow("Created:", QLabel(created))

        # Seeds count
        seeds_count = len(self.wallet.seeds)
        info_layout.addRow("Seeds:", QLabel(str(seeds_count)))

        # Addresses count
        addr_count = len(self.wallet.addresses)
        info_layout.addRow("Addresses:", QLabel(str(addr_count)))

        # Encryption status
        if self.wallet.is_encrypted:
            enc_label = QLabel("✓ Encrypted")
            enc_label.setProperty("role", "success")
        else:
            enc_label = QLabel("⚠ Not encrypted")
            enc_label.setProperty("role", "warn")
        info_layout.addRow("Security:", enc_label)

        layout.addWidget(info_group)

        # === SECURITY SECTION ===
        security_group = QGroupBox("Security")
        security_layout = QVBoxLayout(security_group)

        if self.wallet.is_encrypted:
            security_desc = QLabel("Change your wallet password. You'll need to enter your current password.")
        else:
            security_desc = QLabel("Set a password to encrypt your wallet. This protects your private keys.")
            security_desc.setProperty("role", "warn")
        security_desc.setWordWrap(True)
        security_layout.addWidget(security_desc)

        change_pw_btn = QPushButton("Change Password..." if self.wallet.is_encrypted else "Set Password...")
        change_pw_btn.clicked.connect(self._on_change_password)
        security_layout.addWidget(change_pw_btn)

        layout.addWidget(security_group)

        # === DANGER ZONE ===
        danger_group = QGroupBox("Danger Zone")
        danger_group.setObjectName("dangerZone")
        danger_layout = QVBoxLayout(danger_group)

        # Detach
        detach_row = QHBoxLayout()
        detach_desc = QLabel("Unload wallet without deleting the file")
        detach_desc.setProperty("role", "muted")
        detach_row.addWidget(detach_desc)
        detach_row.addStretch()
        detach_btn = QPushButton("Detach")
        detach_btn.setProperty("variant", "danger-ghost")
        detach_btn.clicked.connect(self._on_detach)
        detach_row.addWidget(detach_btn)
        danger_layout.addLayout(detach_row)

        # Delete
        delete_row = QHBoxLayout()
        delete_desc = QLabel("Permanently delete wallet file")
        delete_desc.setProperty("role", "muted")
        delete_row.addWidget(delete_desc)
        delete_row.addStretch()
        delete_btn = QPushButton("Delete")
        delete_btn.setProperty("variant", "danger")
        delete_btn.clicked.connect(self._on_delete)
        delete_row.addWidget(delete_btn)
        danger_layout.addLayout(delete_row)

        layout.addWidget(danger_group)

        # === BUTTONS ===
        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _on_change_password(self):
        """Open change password dialog."""
        logger.debug("Opening change password dialog")
        dialog = ChangePasswordDialog(
            wallet=self.wallet,
            wallet_path=self.wallet_path,
            parent=self
        )
        result = dialog.exec()
        dialog.scrub()  # the typed passwords must not outlive the dialog
        logger.debug(f"Change password dialog returned: {result}")
        if result == QDialog.DialogCode.Accepted:
            logger.debug("Password change accepted, updating state...")
            self._password_changed = True
            # Update encryption status display
            self._refresh_encryption_status()
            logger.debug("Showing success message...")
            FramelessMessageBox.information(
                self,
                "Password Changed",
                "Your wallet password has been updated successfully."
            )
            logger.debug("Success message closed")

    def _refresh_encryption_status(self):
        """Refresh the encryption status label after password change."""
        # Find and update the security label in info section
        # For simplicity, just close and let parent handle refresh
        pass

    def _on_detach(self):
        """Handle detach button click - delegates to callback which has its own confirmation."""
        self.reject()  # Close settings dialog
        self.on_detach()  # Execute callback (has its own confirmation)

    def _on_delete(self):
        """Handle delete button click - delegates to callback which has its own confirmation."""
        self.reject()  # Close settings dialog
        self.on_delete()  # Execute callback (has type-to-confirm dialog)

    @property
    def password_changed(self) -> bool:
        """Check if password was changed during this dialog session."""
        return self._password_changed


class ChangePasswordDialog(FramelessDialog):
    """Dialog for changing wallet password."""

    def __init__(self, wallet: VaultWallet, wallet_path: Path, parent=None):
        super().__init__("Change Password", parent)
        self.wallet = wallet
        self.wallet_path = wallet_path

        self.setFixedWidth(400)

        self._setup_ui()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        # Current password (only if encrypted)
        if self.wallet.is_encrypted:
            current_group = QGroupBox("Current Password")
            current_layout = QVBoxLayout(current_group)

            self.current_input = QLineEdit()
            self.current_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.current_input.setPlaceholderText("Enter current password")
            current_layout.addWidget(self.current_input)

            layout.addWidget(current_group)
        else:
            self.current_input = None

        # New password
        new_group = QGroupBox("New Password")
        new_layout = QVBoxLayout(new_group)

        self.new_input = QLineEdit()
        self.new_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_input.setPlaceholderText("Enter new password")
        new_layout.addWidget(self.new_input)

        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_input.setPlaceholderText("Confirm new password")
        new_layout.addWidget(self.confirm_input)

        new_hint = QLabel(f"At least {MIN_PASSWORD_LENGTH} characters.")
        new_hint.setProperty("role", "muted")
        new_layout.addWidget(new_hint)

        layout.addWidget(new_group)

        # Error label
        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.save_btn = QPushButton("Save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)

    def _on_save(self):
        """Validate and save new password."""
        self.error_label.setText("")

        # Verify current password if wallet is encrypted
        if self.wallet.is_encrypted:
            current = self.current_input.text()
            if not current:
                self.error_label.setText("Please enter your current password")
                return

            if not self.wallet.verify_password(current):
                self.error_label.setText("Current password is incorrect")
                return

        # Get new password
        new_password = self.new_input.text()
        confirm = self.confirm_input.text()

        if not new_password:
            self.error_label.setText("Please enter a new password")
            return

        if new_password != confirm:
            self.error_label.setText("Passwords do not match")
            return

        try:
            validate_wallet_password(new_password)
        except WeakPasswordError as e:
            self.error_label.setText(str(e))
            return

        # Change password
        try:
            logger.debug("Starting password change...")
            self.wallet.change_password(new_password)
            logger.debug("Password changed, saving wallet...")
            self.wallet.save(self.wallet_path)
            logger.debug("Wallet saved, accepting dialog...")
            self.accept()
            logger.debug("Dialog accepted")
        except Exception as e:
            logger.exception("Failed to change password")
            self.error_label.setText(f"Failed to change password: {e}")

    def scrub(self):
        """Blank the three password fields; see ui/scrub.py for why.

        self.wallet is the core's own wallet, not a private copy - it must
        stay usable, so only the widgets are cleared."""
        scrub_dialog(self)

    def close(self):
        # accept()/reject() hide the dialog, and Qt does not deliver a
        # closeEvent to a hidden widget - so scrub on close() itself too.
        self.scrub()
        return super().close()

    def closeEvent(self, event):
        self.scrub()
        super().closeEvent(event)
