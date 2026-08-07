"""
UI Dialogs - Application dialogs for agents, policies, and wallets.

Contains dialogs for:
- Agent registration and commission
- Policy creation/editing
- Wallet management
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QGroupBox, QFormLayout, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem,
    QDoubleSpinBox, QDialogButtonBox, QAbstractItemView,
    QApplication, QWidget, QRadioButton, QButtonGroup, QStackedWidget,
    QGridLayout, QTabWidget, QSpinBox, QGraphicsOpacityEffect
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QFont
from typing import Optional
from datetime import datetime

from .theme import (
    Theme, FramelessDialog, FramelessMessageBox,
    active, status_color, status_token, colored_span, set_role,
)

# Clipboard auto-clear timeout (seconds)
# Security: Sensitive data (secrets, private keys, seed phrases) should not
# remain in clipboard indefinitely. Auto-clear reduces exposure window.
CLIPBOARD_CLEAR_TIMEOUT = 60


def copy_sensitive_to_clipboard(text: str, parent: QWidget = None, timeout_sec: int = CLIPBOARD_CLEAR_TIMEOUT):
    """
    Copy sensitive data to clipboard with auto-clear.

    Security: Sensitive data like agent secrets, private keys, or seed phrases
    should not remain in the clipboard indefinitely. This function:
    1. Copies the text to clipboard
    2. Schedules automatic clearing after timeout (default 60 seconds)
    3. Only clears if clipboard still contains our text (user may have copied something else)

    Args:
        text: Sensitive text to copy
        parent: Parent widget for notification dialog (optional)
        timeout_sec: Seconds before auto-clear (default: 60)
    """
    clipboard = QApplication.clipboard()
    clipboard.setText(text)

    # Schedule clipboard clear
    def clear_clipboard():
        if clipboard.text() == text:
            clipboard.clear()

    QTimer.singleShot(timeout_sec * 1000, clear_clipboard)

    # Show notification if parent available
    if parent:
        FramelessMessageBox.information(
            parent,
            "Copied",
            f"Data copied to clipboard.\n\nClipboard will auto-clear in {timeout_sec} seconds."
        )
from ..models import SpendPolicy, Agent, TradingRules
from ..wallet import WalletInfo, Wallet, PrivateKeyWallet, AddressEntry
from ..networks import NETWORKS, format_address, resolve_network, get_dex
from ..utils import agent_config_snippet

# Type alias for wallet info objects (both old and new)
WalletInfoLike = WalletInfo | AddressEntry


# ============================================
# Agent Registration Dialog
# ============================================

class AgentRegistrationDialog(FramelessDialog):
    """Two-page wizard for registering a new agent with configurable authentication."""

    # Pages
    PAGE_CONFIGURE = 0
    PAGE_CREDENTIALS = 1

    def __init__(self, core, parent=None):
        """
        Create agent registration dialog.

        Args:
            core: Vault core instance (required)
            parent: Parent widget
        """
        super().__init__("Register New Agent", parent, width=550)
        self.setMinimumHeight(400)

        self._core = core
        self.agent = None
        self.agent_token = None
        self.config_text = ""

        layout = self.content_layout
        layout.setSpacing(12)

        # Stacked widget for pages
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # Create pages
        self._create_configure_page()
        self._create_credentials_page()

        # Navigation buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.action_btn = QPushButton("Generate Token")
        self.action_btn.setDefault(True)
        self.action_btn.clicked.connect(self._on_action)
        btn_layout.addWidget(self.action_btn)

        layout.addLayout(btn_layout)

        # Start on configure page
        self.stack.setCurrentIndex(self.PAGE_CONFIGURE)

    def _create_configure_page(self):
        """Page 1: Agent name and authentication mode selection."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        # Description
        desc = QLabel(
            "Register an AI agent to use with Vault. Choose a name and"
            "authentication method for the agent."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(8)

        # Agent name
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g., claude-dev, research-bot")
        form.addRow("Agent Name:", self.name_input)
        layout.addLayout(form)

        layout.addSpacing(12)

        # Authentication Mode section
        auth_group = QGroupBox("Authentication Mode")
        auth_layout = QVBoxLayout(auth_group)

        self.auth_mode_group = QButtonGroup(self)

        # HMAC option (default)
        self.hmac_radio = QRadioButton("HMAC-SHA256 Signing (Recommended)")
        self.hmac_radio.setChecked(True)
        self.auth_mode_group.addButton(self.hmac_radio, 0)
        auth_layout.addWidget(self.hmac_radio)

        hmac_desc = QLabel(
            "Agent signs each request with a shared secret. The secret is never "
            "transmitted—only proof of knowing it. More secure but requires signing code."
        )
        hmac_desc.setWordWrap(True)
        hmac_desc.setProperty("role", "hint")
        auth_layout.addWidget(hmac_desc)

        auth_layout.addSpacing(12)

        # Bearer option
        self.bearer_radio = QRadioButton("Bearer Token (Simple)")
        self.auth_mode_group.addButton(self.bearer_radio, 1)
        auth_layout.addWidget(self.bearer_radio)

        bearer_desc = QLabel(
            "Agent sends the token directly with requests. Simpler for agents that "
            "struggle with signing, but the token is transmitted with every request."
        )
        bearer_desc.setWordWrap(True)
        bearer_desc.setProperty("role", "hint")
        auth_layout.addWidget(bearer_desc)

        # Security warning for bearer
        self.bearer_warning = QLabel(
            "⚠️ Less secure: Anyone who intercepts the token can impersonate this agent."
        )
        self.bearer_warning.setWordWrap(True)
        self.bearer_warning.setProperty("role", "warn")
        self.bearer_warning.setVisible(False)
        auth_layout.addWidget(self.bearer_warning)

        # Show/hide warning based on selection
        self.bearer_radio.toggled.connect(self.bearer_warning.setVisible)

        layout.addWidget(auth_group)

        layout.addStretch()
        self.stack.addWidget(page)

    def _create_credentials_page(self):
        """Page 2: Generated token display."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        # Title
        title = QLabel("Agent Credentials")
        title.setFont(QFont("", 11, QFont.Weight.Bold))
        layout.addWidget(title)

        # Description
        desc = QLabel("Copy this configuration to your agent's environment:")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(4)

        # Config display - more space now that it has the full page
        self.config_display = QTextEdit()
        self.config_display.setReadOnly(True)
        self.config_display.setFont(QFont(Theme.MONO_FONT, 10))
        self.config_display.setMinimumHeight(140)
        layout.addWidget(self.config_display)

        # Copy button row
        copy_row = QHBoxLayout()
        copy_row.addStretch()

        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.setFixedWidth(160)
        copy_btn.clicked.connect(self._copy_config)
        copy_row.addWidget(copy_btn)

        layout.addLayout(copy_row)

        layout.addSpacing(8)

        # Port note
        port_note = QLabel(
            "Note: If you change the server port, update PRIMER_VAULT_URL in your agent configuration."
        )
        port_note.setWordWrap(True)
        port_note.setProperty("role", "muted")
        layout.addWidget(port_note)

        layout.addSpacing(8)

        # Warning
        warning = QLabel(
            "⚠️ Save this configuration now! The token cannot be retrieved later."
        )
        warning.setWordWrap(True)
        warning.setProperty("role", "warn")
        layout.addWidget(warning)

        layout.addStretch()
        self.stack.addWidget(page)

    def keyPressEvent(self, event):
        """Handle key presses - prevent Enter from closing dialog on page 1."""
        from PyQt6.QtCore import Qt
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Handle Enter explicitly via action button logic
            self._on_action()
            return  # Don't propagate to QDialog default handling
        super().keyPressEvent(event)

    def _on_action(self):
        """Handle action button click based on current page."""
        if self.stack.currentIndex() == self.PAGE_CONFIGURE:
            self._generate_token()
        else:
            self._register_agent()

    def _generate_token(self):
        """Generate authentication credentials via core and move to credentials page."""
        name = self.name_input.text().strip()
        if not name:
            FramelessMessageBox.warning(self, "Validation Error", "Agent name is required.")
            return

        # Check for duplicate name using core
        for agent in self._core.get_all_agents():
            if agent.name == name:
                FramelessMessageBox.warning(self, "Duplicate Name", f"Agent name '{name}' already exists.")
                return

        # Determine auth mode
        auth_mode = "hmac" if self.hmac_radio.isChecked() else "bearer"

        # Create agent via core - this handles all the crypto
        try:
            self.agent, self.agent_token = self._core.create_agent(name, auth_mode)
        except Exception as e:
            FramelessMessageBox.warning(self, "Error", f"Failed to create agent: {e}")
            return

        # Build config text
        self.config_text = agent_config_snippet(self.agent.id, self.agent_token, auth_mode)

        self.config_display.setPlainText(self.config_text)

        # Move to credentials page
        self.stack.setCurrentIndex(self.PAGE_CREDENTIALS)
        self.action_btn.setText("Done")

    def _copy_config(self):
        """Copy configuration to clipboard."""
        if self.config_text:
            QApplication.clipboard().setText(self.config_text)
            FramelessMessageBox.information(self, "Copied", "Agent configuration copied to clipboard.")

    def _register_agent(self):
        """Complete registration - agent already created via core."""
        # Agent was created when Generate Token was clicked
        # Just close the dialog
        self.accept()

    def get_agent(self) -> Agent:
        """Return the created agent."""
        return self.agent


# ============================================
# Commission Dialog
# ============================================

class CommissionDialog(FramelessDialog):
    """Dialog for commissioning an agent with a spend policy and signing address."""

    def __init__(
        self,
        agent: Agent,
        core,
        wallets: list[WalletInfoLike] = None,
        get_wallet_fn=None,
        parent=None
    ):
        """
        Args:
            agent: Agent to commission
            core: Vault core instance
            wallets: List of available wallet addresses
            get_wallet_fn: Function to get unlocked wallet by address
            parent: Parent widget
        """
        super().__init__(f"Commission Agent: {agent.name}", parent, width=500)
        self.agent = agent
        self.core = core
        self.wallets = wallets or []
        self.get_wallet_fn = get_wallet_fn  # Function to get unlocked wallet by address
        self.selected_policy: Optional[SpendPolicy] = None
        self.selected_wallet_address: Optional[str] = None
        self.wallet_sort_by_id = True
        self.generated_mandate: Optional[dict] = None

        layout = self.content_layout

        desc = QLabel(
            "Select a spend policy and signing address to enable this agent. "
            "The agent will sign payments using the linked address."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(12)

        policy_label = QLabel("Spend Policy:")
        layout.addWidget(policy_label)

        self.policy_combo = QComboBox()
        policies = self.core.get_all_policies()

        if not policies:
            self.policy_combo.addItem("No policies available", None)
            self.policy_combo.setEnabled(False)
        else:
            self.policy_combo.addItem("Select a policy...", None)
            for policy in policies:
                self.policy_combo.addItem(policy.name, policy.id)

        self.policy_combo.currentIndexChanged.connect(self.on_selection_changed)
        layout.addWidget(self.policy_combo)

        self.policy_details = QWidget()
        details_layout = QVBoxLayout(self.policy_details)
        details_layout.setContentsMargins(8, 4, 8, 4)
        details_layout.setSpacing(2)

        self.detail_trading = QLabel("Trading: —")
        self.detail_trading.setProperty("role", "muted")
        details_layout.addWidget(self.detail_trading)

        self.detail_x402 = QLabel("x402: —")
        self.detail_x402.setProperty("role", "muted")
        details_layout.addWidget(self.detail_x402)

        policy_hint = QLabel("See Policies tab for more information.")
        policy_hint.setProperty("role", "hint")
        details_layout.addWidget(policy_hint)

        layout.addWidget(self.policy_details)

        layout.addSpacing(8)

        wallet_header = QHBoxLayout()
        wallet_label = QLabel("Signing Address:")
        wallet_header.addWidget(wallet_label)

        wallet_header.addStretch()

        sort_label = QLabel("Sort:")
        sort_label.setProperty("role", "hint")
        wallet_header.addWidget(sort_label)

        self.sort_id_btn = QPushButton("ID")
        self.sort_id_btn.setMaximumWidth(40)
        self.sort_id_btn.setProperty("sort", True)
        self.sort_id_btn.clicked.connect(lambda: self.sort_wallets(by_id=True))
        wallet_header.addWidget(self.sort_id_btn)

        self.sort_name_btn = QPushButton("Name")
        self.sort_name_btn.setMaximumWidth(50)
        self.sort_name_btn.setProperty("sort", True)
        self.sort_name_btn.clicked.connect(lambda: self.sort_wallets(by_id=False))
        wallet_header.addWidget(self.sort_name_btn)
        self._mark_active_sort(by_id=True)

        layout.addLayout(wallet_header)

        self.wallet_list = QListWidget()
        self.wallet_list.setMaximumHeight(150)
        self.wallet_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.wallet_list.itemSelectionChanged.connect(self.on_wallet_selected)
        self.wallet_list.setFont(QFont(Theme.MONO_FONT, 9))

        if self.wallets:
            self.populate_wallet_list()
        else:
            self.wallet_list.setEnabled(False)
            item = QListWidgetItem("No addresses available")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.wallet_list.addItem(item)

        layout.addWidget(self.wallet_list)

        # AP2 Intent Mandate generation option
        layout.addSpacing(8)

        ap2_group = QGroupBox("AP2 Integration (Optional)")
        ap2_layout = QVBoxLayout(ap2_group)

        self.generate_mandate_checkbox = QCheckBox("Generate Intent Mandate VDC")
        self.generate_mandate_checkbox.setToolTip(
            "Generate an AP2-compatible Verifiable Digital Credential documenting "
            "this agent's authorization to make payments within the policy limits."
        )
        self.generate_mandate_checkbox.stateChanged.connect(self._on_mandate_checkbox_changed)
        ap2_layout.addWidget(self.generate_mandate_checkbox)

        # Registry upload option (enabled only when mandate generation is checked)
        self.upload_registry_checkbox = QCheckBox("Upload to AP2 Registry")
        self.upload_registry_checkbox.setEnabled(False)
        self.upload_registry_checkbox.setToolTip(
            "Publish the Intent Mandate to the Vault AP2 Registry for external verification. "
            "Merchants can verify this agent's authorization at ap2.primer.systems"
        )
        ap2_layout.addWidget(self.upload_registry_checkbox)

        mandate_note = QLabel(
            "An Intent Mandate is a cryptographically signed document that external"
            "parties can use to verify this agent's spending authorization."
        )
        mandate_note.setWordWrap(True)
        mandate_note.setProperty("role", "hint")
        ap2_layout.addWidget(mandate_note)

        layout.addWidget(ap2_group)

        self.no_policy_warning = QLabel(
            "⚠️ No spend policies exist. Create a policy in the Policies tab first."
        )
        self.no_policy_warning.setWordWrap(True)
        self.no_policy_warning.setProperty("role", "warn")
        self.no_policy_warning.setVisible(not policies)
        layout.addWidget(self.no_policy_warning)

        self.no_wallet_warning = QLabel(
            "⚠️ No addresses available. Add an address in the Wallet tab first."
        )
        self.no_wallet_warning.setWordWrap(True)
        self.no_wallet_warning.setProperty("role", "warn")
        self.no_wallet_warning.setVisible(not self.wallets)
        layout.addWidget(self.no_wallet_warning)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.commission_btn = QPushButton("Commission Agent")
        self.commission_btn.setEnabled(False)
        self.commission_btn.clicked.connect(self.commission)
        btn_layout.addWidget(self.commission_btn)

        layout.addLayout(btn_layout)

    def populate_wallet_list(self):
        """Populate the wallet list, sorted by current sort order."""
        self.wallet_list.clear()
        # Support both WalletInfo (wallet_id) and AddressEntry (id)
        def get_id(w):
            return getattr(w, 'wallet_id', None) or getattr(w, 'id', '')
        sorted_wallets = sorted(
            self.wallets,
            key=lambda w: get_id(w) if self.wallet_sort_by_id else w.name.lower()
        )
        for wallet_info in sorted_wallets:
            addr_short = format_address(wallet_info.address)
            entry_id = get_id(wallet_info)
            item = QListWidgetItem(f"{entry_id}  {wallet_info.name}  ({addr_short})")
            item.setData(Qt.ItemDataRole.UserRole, wallet_info.address)
            self.wallet_list.addItem(item)

    def _mark_active_sort(self, by_id: bool):
        """Bold the active sort button via the [active-sort] QSS state."""
        set_role(self.sort_id_btn, **{"active-sort": by_id})
        set_role(self.sort_name_btn, **{"active-sort": not by_id})

    def sort_wallets(self, by_id: bool):
        """Sort the wallet list by ID or name."""
        self.wallet_sort_by_id = by_id
        self._mark_active_sort(by_id)
        current_addr = self.selected_wallet_address
        self.populate_wallet_list()
        if current_addr:
            for i in range(self.wallet_list.count()):
                item = self.wallet_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == current_addr:
                    self.wallet_list.setCurrentItem(item)
                    break

    def on_wallet_selected(self):
        """Handle wallet selection from list."""
        selected = self.wallet_list.currentItem()
        if selected:
            self.selected_wallet_address = selected.data(Qt.ItemDataRole.UserRole)
        else:
            self.selected_wallet_address = None
        self.update_commission_button()

    def on_selection_changed(self, index: int = 0):
        """Handle policy selection change."""
        policy_id = self.policy_combo.currentData()

        if policy_id:
            policy = self.core.get_policy(policy_id)
            if policy:
                self.selected_policy = policy

                # Trading status
                if policy.is_trading_enabled():
                    tr = policy.trading_rules
                    self.detail_trading.setText(f"Trading: ${tr.daily_volume_limit_usd:.0f} per day")
                else:
                    self.detail_trading.setText("Trading: Disabled")

                # x402 status
                if policy.x402_enabled:
                    daily_usd = policy.daily_limit_micro / 1_000_000
                    self.detail_x402.setText(f"x402: ${daily_usd:.2f} per day")
                else:
                    self.detail_x402.setText("x402: Disabled")
        else:
            self.selected_policy = None
            self.detail_trading.setText("Trading: —")
            self.detail_x402.setText("x402: —")

        self.update_commission_button()

    def update_commission_button(self):
        """Enable commission button only if both policy and wallet are selected."""
        policy_id = self.policy_combo.currentData()
        can_commission = policy_id is not None and self.selected_wallet_address is not None
        self.commission_btn.setEnabled(can_commission)

    def _on_mandate_checkbox_changed(self, state: int):
        """Enable/disable registry upload based on mandate generation checkbox."""
        self.upload_registry_checkbox.setEnabled(state == Qt.CheckState.Checked.value)

    def commission(self):
        """Commission the agent with selected policy and wallet."""
        if not self.selected_policy or not self.selected_wallet_address:
            return

        # Generate IntentMandate if requested - all through core
        mandate = None
        if self.generate_mandate_checkbox.isChecked():
            mandate = self.core.generate_intent_mandate(
                agent_code=self.agent.code,
                policy_id=self.selected_policy.id,
                wallet_address=self.selected_wallet_address,
                sign=True  # Request signing if wallet is unlocked
            )
            self.generated_mandate = mandate

            # Upload to registry if requested (UI operation - shows message box)
            if self.upload_registry_checkbox.isChecked():
                self._upload_to_registry(mandate)

        # Single call to core - core handles agent state changes
        self.core.commission_agent(
            agent_code=self.agent.code,
            policy_id=self.selected_policy.id,
            wallet_address=self.selected_wallet_address,
            intent_mandate=mandate
        )
        self.accept()

    def get_policy_id(self) -> Optional[str]:
        """Return the selected policy ID."""
        return self.selected_policy.id if self.selected_policy else None

    def get_intent_mandate(self) -> Optional[dict]:
        """Return the generated Intent Mandate, if any."""
        return self.generated_mandate

    def _upload_to_registry(self, mandate: dict) -> bool:
        """
        Upload the Intent Mandate to the AP2 Registry.

        Returns True on success, False on failure.
        """
        result = self.core.upload_mandate_to_registry(mandate)

        if result.get("success"):
            # Store registry info on the mandate
            self.generated_mandate["registryId"] = result["mandate_id"]
            self.generated_mandate["registryUrl"] = result["viewer_url"]

            # Show success message
            FramelessMessageBox.information(
                self,
                "Mandate Published",
                f"Intent Mandate uploaded to AP2 Registry.\n\n{result['viewer_url']}"
            )
            return True
        else:
            FramelessMessageBox.warning(
                self,
                "Registry Upload Failed",
                f"Could not upload Intent Mandate to registry.\n\n"
                f"{result.get('error', 'Unknown error')}\n"
                f"The mandate was generated locally but not published."
            )
            return False


# ============================================
# Edit Agent Dialog
# ============================================

class EditAgentDialog(FramelessDialog):
    """Dialog for editing an agent's policy and signing address assignment."""

    def __init__(self, agent: Agent, core, wallets: list[WalletInfoLike] = None, parent=None):
        """
        Args:
            agent: Agent to edit
            core: Vault core instance
            wallets: List of available wallet addresses
            parent: Parent widget
        """
        super().__init__(f"Edit Agent: {agent.name}", parent, width=500)
        self.agent = agent
        self.core = core
        self.wallets = wallets or []
        self.selected_policy_id: Optional[str] = agent.policy_id
        self.selected_wallet_address: Optional[str] = agent.wallet_address
        self.wallet_sort_by_id = True

        layout = self.content_layout

        desc = QLabel(
            "Change the agent's spend policy or signing address. "
            "Removing either will decommission the agent."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Agent info
        info_group = QGroupBox("Agent Info")
        info_layout = QFormLayout(info_group)
        info_layout.addRow("ID:", QLabel(agent.id))
        info_layout.addRow("Status:", QLabel(agent.status))
        info_layout.addRow("Spent Today:", QLabel(agent.format_spent_today()))
        layout.addWidget(info_group)

        layout.addSpacing(8)

        # Policy selection
        policy_label = QLabel("Spend Policy:")
        layout.addWidget(policy_label)

        self.policy_combo = QComboBox()
        policies = self.core.get_all_policies()

        self.policy_combo.addItem("None (decommission)", None)
        for policy in policies:
            self.policy_combo.addItem(policy.name, policy.id)
            if policy.id == agent.policy_id:
                self.policy_combo.setCurrentIndex(self.policy_combo.count() - 1)

        self.policy_combo.currentIndexChanged.connect(self.on_policy_changed)
        layout.addWidget(self.policy_combo)

        # Policy details
        self.policy_details = QWidget()
        details_layout = QVBoxLayout(self.policy_details)
        details_layout.setContentsMargins(8, 4, 8, 4)
        details_layout.setSpacing(2)

        self.detail_trading = QLabel("Trading: —")
        self.detail_trading.setProperty("role", "muted")
        details_layout.addWidget(self.detail_trading)

        self.detail_x402 = QLabel("x402: —")
        self.detail_x402.setProperty("role", "muted")
        details_layout.addWidget(self.detail_x402)

        policy_hint = QLabel("See Policies tab for more information.")
        policy_hint.setProperty("role", "hint")
        details_layout.addWidget(policy_hint)

        layout.addWidget(self.policy_details)

        # Show current policy details
        self.on_policy_changed()

        layout.addSpacing(8)

        # Wallet selection
        wallet_header = QHBoxLayout()
        wallet_label = QLabel("Signing Address:")
        wallet_header.addWidget(wallet_label)

        wallet_header.addStretch()

        sort_label = QLabel("Sort:")
        sort_label.setProperty("role", "hint")
        wallet_header.addWidget(sort_label)

        self.sort_id_btn = QPushButton("ID")
        self.sort_id_btn.setMaximumWidth(40)
        self.sort_id_btn.setProperty("sort", True)
        self.sort_id_btn.clicked.connect(lambda: self.sort_wallets(by_id=True))
        wallet_header.addWidget(self.sort_id_btn)

        self.sort_name_btn = QPushButton("Name")
        self.sort_name_btn.setMaximumWidth(50)
        self.sort_name_btn.setProperty("sort", True)
        self.sort_name_btn.clicked.connect(lambda: self.sort_wallets(by_id=False))
        wallet_header.addWidget(self.sort_name_btn)
        self._mark_active_sort(by_id=True)

        layout.addLayout(wallet_header)

        self.wallet_list = QListWidget()
        self.wallet_list.setMaximumHeight(120)
        self.wallet_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.wallet_list.itemSelectionChanged.connect(self.on_wallet_selected)
        self.wallet_list.setFont(QFont(Theme.MONO_FONT, 9))

        # Add "None" option at the top
        none_item = QListWidgetItem("(None - decommission)")
        none_item.setData(Qt.ItemDataRole.UserRole, None)
        self.wallet_list.addItem(none_item)

        if self.wallets:
            self.populate_wallet_list()
        else:
            item = QListWidgetItem("No addresses available")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.wallet_list.addItem(item)

        # Select current wallet
        self.select_current_wallet()

        layout.addWidget(self.wallet_list)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()

        # View Instructions button
        self.instructions_btn = QPushButton("View Instructions")
        self.instructions_btn.clicked.connect(self._show_instructions)
        btn_layout.addWidget(self.instructions_btn)

        # Mandate button - shows "Create Mandate" or "View Mandate" based on state
        self.mandate_btn = QPushButton()
        self._update_mandate_button()
        btn_layout.addWidget(self.mandate_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.save_btn = QPushButton("Save Changes")
        self.save_btn.clicked.connect(self.save_changes)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)

    def populate_wallet_list(self):
        """Populate the wallet list (excluding the None item at index 0)."""
        # Remove all items except the first (None)
        while self.wallet_list.count() > 1:
            self.wallet_list.takeItem(1)

        # Support both WalletInfo (wallet_id) and AddressEntry (id)
        def get_id(w):
            return getattr(w, 'wallet_id', None) or getattr(w, 'id', '')
        sorted_wallets = sorted(
            self.wallets,
            key=lambda w: get_id(w) if self.wallet_sort_by_id else w.name.lower()
        )
        for wallet_info in sorted_wallets:
            addr_short = format_address(wallet_info.address)
            entry_id = get_id(wallet_info)
            item = QListWidgetItem(f"{entry_id}  {wallet_info.name}  ({addr_short})")
            item.setData(Qt.ItemDataRole.UserRole, wallet_info.address)
            self.wallet_list.addItem(item)

    def select_current_wallet(self):
        """Select the current wallet in the list."""
        if self.selected_wallet_address is None:
            self.wallet_list.setCurrentRow(0)  # Select "None"
        else:
            for i in range(self.wallet_list.count()):
                item = self.wallet_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == self.selected_wallet_address:
                    self.wallet_list.setCurrentItem(item)
                    break

    def _mark_active_sort(self, by_id: bool):
        """Bold the active sort button via the [active-sort] QSS state."""
        set_role(self.sort_id_btn, **{"active-sort": by_id})
        set_role(self.sort_name_btn, **{"active-sort": not by_id})

    def sort_wallets(self, by_id: bool):
        """Sort the wallet list by ID or name."""
        self.wallet_sort_by_id = by_id
        self._mark_active_sort(by_id)
        current_addr = self.selected_wallet_address
        self.populate_wallet_list()
        self.select_current_wallet()

    def on_wallet_selected(self):
        """Handle wallet selection from list."""
        selected = self.wallet_list.currentItem()
        if selected:
            self.selected_wallet_address = selected.data(Qt.ItemDataRole.UserRole)
        else:
            self.selected_wallet_address = None

    def on_policy_changed(self):
        """Handle policy selection change."""
        policy_id = self.policy_combo.currentData()
        self.selected_policy_id = policy_id

        if policy_id:
            policy = self.core.get_policy(policy_id)
            if policy:
                # Trading status
                if policy.is_trading_enabled():
                    tr = policy.trading_rules
                    self.detail_trading.setText(f"Trading: ${tr.daily_volume_limit_usd:.0f} per day")
                else:
                    self.detail_trading.setText("Trading: Disabled")

                # x402 status
                if policy.x402_enabled:
                    daily_usd = policy.daily_limit_micro / 1_000_000
                    self.detail_x402.setText(f"x402: ${daily_usd:.2f} per day")
                else:
                    self.detail_x402.setText("x402: Disabled")
                return

        self.detail_trading.setText("Trading: —")
        self.detail_x402.setText("x402: —")

    def save_changes(self):
        """Save the agent changes."""
        # Determine new status
        if self.selected_policy_id and self.selected_wallet_address:
            # Has both - keep active or activate
            if self.agent.status == "uncommissioned":
                self.agent.status = "active"
            # If suspended, leave suspended
            # If limit_reached, leave limit_reached
        else:
            # Missing one or both - decommission
            self.agent.status = "uncommissioned"

        self.agent.policy_id = self.selected_policy_id
        self.agent.wallet_address = self.selected_wallet_address
        self.accept()

    def _show_instructions(self):
        """Show the agent instructions dialog."""
        dialog = ViewInstructionsDialog(self.agent, self.core, parent=self)
        dialog.exec()

    def get_changes(self) -> tuple[Optional[str], Optional[str]]:
        """Return the new policy_id and wallet_address."""
        return self.selected_policy_id, self.selected_wallet_address

    def _update_mandate_button(self):
        """Update the mandate button text and handler based on current state."""
        # Disconnect any existing connections
        try:
            self.mandate_btn.clicked.disconnect()
        except TypeError:
            pass  # No connections to disconnect

        if self.agent.intent_mandate:
            self.mandate_btn.setText("View Mandate")
            self.mandate_btn.setToolTip("View the agent's Intent Mandate")
            self.mandate_btn.clicked.connect(self._view_mandate)
            self.mandate_btn.setEnabled(True)
        else:
            # Can only create mandate if agent is commissioned (has policy and wallet)
            can_create = self.agent.policy_id is not None and self.agent.wallet_address is not None
            self.mandate_btn.setText("Create Mandate")
            if can_create:
                self.mandate_btn.setToolTip("Generate an Intent Mandate for this agent")
                self.mandate_btn.clicked.connect(self._create_mandate)
                self.mandate_btn.setEnabled(True)
            else:
                self.mandate_btn.setToolTip("Agent must be commissioned first (assign policy and wallet)")
                self.mandate_btn.setEnabled(False)

    def _view_mandate(self):
        """Show the mandate viewer dialog."""
        # Get current policy to check for staleness
        current_policy = self.core.get_policy(self.agent.policy_id) if self.agent.policy_id else None
        dialog = MandateViewerDialog(self.agent, current_policy, self)
        dialog.exec()
        # If mandate was revoked, update button state
        if dialog.was_revoked():
            self._mandate_revoked = True
            self._update_mandate_button()

    def _create_mandate(self):
        """Create a new Intent Mandate for this agent."""
        policy = self.core.get_policy(self.agent.policy_id)
        if not policy:
            FramelessMessageBox.warning(self, "Error", "Cannot create mandate: policy not found")
            return

        # Ask about publishing to registry
        if not FramelessMessageBox.question(
            self,
            "Create Intent Mandate",
            "Generate an Intent Mandate for this agent?\n\n"
            "This documents the authorization granted to this agent under the current policy.",
            default_no=False
        ):
            return

        # Generate and set mandate through core
        mandate = self.core.generate_intent_mandate(
            agent_code=self.agent.code,
            policy_id=self.agent.policy_id,
            wallet_address=self.agent.wallet_address,
            sign=False  # Unsigned mandate
        )
        self.core.set_agent_mandate(self.agent.code, mandate)

        # Update local copy for display
        self.agent.intent_mandate = mandate
        self._mandate_created = True

        FramelessMessageBox.information(
            self,
            "Mandate Created",
            f"Intent Mandate generated successfully.\n\nID: {mandate.get('id', 'unknown')[:8]}..."
        )

        # Update button to show "View Mandate" now
        self._update_mandate_button()

    def was_mandate_revoked(self) -> bool:
        """Return True if the mandate was revoked during this dialog session."""
        return getattr(self, '_mandate_revoked', False)

    def was_mandate_created(self) -> bool:
        """Return True if a mandate was created during this dialog session."""
        return getattr(self, '_mandate_created', False)


# ============================================
# View Instructions Dialog
# ============================================

class ViewInstructionsDialog(FramelessDialog):
    """Dialog for viewing agent credentials and setup instructions."""

    def __init__(self, agent: Agent, core, parent=None):
        """
        Args:
            agent: Agent to show instructions for
            core: Vault core instance
            parent: Parent widget
        """
        super().__init__(f"Agent Instructions: {agent.name}", parent, width=550)
        self.setMinimumHeight(350)
        self.agent = agent
        self.core = core

        layout = self.content_layout
        layout.setSpacing(12)

        # Title
        title = QLabel("Agent Credentials")
        title.setFont(QFont("", 11, QFont.Weight.Bold))
        layout.addWidget(title)

        # Description
        desc = QLabel("Copy this configuration to your agent's environment:")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addSpacing(4)

        # Config display
        self.config_display = QTextEdit()
        self.config_display.setReadOnly(True)
        self.config_display.setFont(QFont(Theme.MONO_FONT, 10))
        self.config_display.setMinimumHeight(140)
        layout.addWidget(self.config_display)

        # Copy button row
        copy_row = QHBoxLayout()
        copy_row.addStretch()

        self.copy_btn = QPushButton("Copy to Clipboard")
        self.copy_btn.setFixedWidth(160)
        self.copy_btn.clicked.connect(self._copy_config)
        copy_row.addWidget(self.copy_btn)

        layout.addLayout(copy_row)

        layout.addSpacing(8)

        # Port note
        port_note = QLabel(
            "Note: If you change the server port, update PRIMER_VAULT_URL in your agent configuration."
        )
        port_note.setWordWrap(True)
        port_note.setProperty("role", "muted")
        layout.addWidget(port_note)

        layout.addSpacing(8)

        # Status/warning area (varies by auth mode and state)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Regenerate button (only for Bearer mode)
        self.regenerate_btn = QPushButton("Regenerate Token")
        self.regenerate_btn.setVisible(False)
        self.regenerate_btn.clicked.connect(self._regenerate_token)
        layout.addWidget(self.regenerate_btn)

        layout.addStretch()

        # Close button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        # Load credentials and populate display
        self._load_credentials()

    def _load_credentials(self):
        """Load credentials from core and populate the display."""
        try:
            agent_id, token, auth_mode = self.core.get_agent_credentials(self.agent.code)
        except Exception as e:
            self.config_display.setPlainText(f"Error loading credentials: {e}")
            return

        if auth_mode == "hmac":
            if token:
                # HMAC with decrypted secret
                self._show_hmac_credentials(agent_id, token)
            else:
                # HMAC but wallet locked or decryption failed
                self._show_hmac_locked(agent_id)
        else:
            # Bearer mode - token is unrecoverable
            self._show_bearer_credentials(agent_id)

    def _build_config_text(self, agent_id: str, token: str, auth_mode: str) -> str:
        """Build the configuration snippet text."""
        return agent_config_snippet(agent_id, token, auth_mode)

    def _show_hmac_credentials(self, agent_id: str, token: str):
        """Show full HMAC credentials with the decrypted secret."""
        config_text = self._build_config_text(agent_id, token, "hmac")
        self.config_display.setPlainText(config_text)

        self.status_label.setText(
            "This is your agent's HMAC signing secret. Keep it secure."
        )
        self.status_label.setProperty("role", "muted")

        self.regenerate_btn.setVisible(False)
        self.copy_btn.setVisible(True)
        self.copy_btn.setEnabled(True)
        self._current_config = config_text

    def _show_hmac_locked(self, agent_id: str):
        """Wallet locked: the HMAC secret can't be shown, and a config without the
        token is worthless — so don't present one. Show a clear message and hide
        the copy button instead of offering a broken snippet."""
        self.config_display.setPlainText(
            "The signing secret for this agent is encrypted with your wallet and "
            "can't be shown while the wallet is locked.\n\n"
            "Unlock the wallet, then reopen this dialog to view and copy the full "
            "configuration."
        )
        self.status_label.setText("Wallet locked — credentials unavailable.")
        self.status_label.setProperty("role", "warn")

        self.regenerate_btn.setVisible(False)
        self.copy_btn.setVisible(False)
        self._current_config = None

    def _show_bearer_credentials(self, agent_id: str):
        """Bearer token is shown once at creation and can't be retrieved, so there
        is no full config to copy — offer regeneration instead of a broken snippet."""
        self.config_display.setPlainText(
            "This agent uses a Bearer token, which is shown only once at creation "
            "and can't be retrieved.\n\n"
            "Use \"Regenerate Token\" below to issue a new token. This invalidates "
            "the current one."
        )
        self.status_label.setText("Bearer token — not recoverable.")
        self.status_label.setProperty("role", "warn")

        self.regenerate_btn.setVisible(True)
        self.copy_btn.setVisible(False)
        self._current_config = None

    def _copy_config(self):
        """Copy configuration to clipboard with auto-clear for security."""
        if hasattr(self, '_current_config') and self._current_config:
            copy_sensitive_to_clipboard(self._current_config, self)

    def _regenerate_token(self):
        """Regenerate Bearer token after confirmation."""
        if not FramelessMessageBox.question(
            self,
            "Regenerate Token",
            "This will create a new token and immediately invalidate the old one.\n\n"
            "Any agents using the old token will stop working until updated.\n\n"
            "Continue?",
            default_no=True
        ):
            return

        try:
            new_token = self.core.regenerate_agent_token(self.agent.code)
        except Exception as e:
            FramelessMessageBox.critical(self, "Error", f"Failed to regenerate token: {e}")
            return

        # Show the new token
        config_text = self._build_config_text(self.agent.id, new_token, "bearer")
        self.config_display.setPlainText(config_text)

        self.status_label.setText(
            "New token generated. Copy it now - it cannot be retrieved later."
        )
        self.status_label.setProperty("role", "success")

        self.copy_btn.setVisible(True)
        self.copy_btn.setEnabled(True)
        self._current_config = config_text

        FramelessMessageBox.information(
            self,
            "Token Regenerated",
            "A new Bearer token has been generated.\n\n"
            "Copy the configuration now and update your agent."
        )


# ============================================
# New Policy Dialog
# ============================================

class NewPolicyDialog(FramelessDialog):
    """Dialog for creating or editing a spend policy with Trading and x402 tabs."""

    def __init__(self, parent=None, policy: SpendPolicy = None, core=None):
        """
        Args:
            parent: Parent widget
            policy: Existing policy to edit (None for new policy)
            core: Vault core instance (required for validation)
        """
        title = "Edit Spend Policy" if policy else "New Spend Policy"
        super().__init__(title, parent, width=520)
        self.setMinimumHeight(580)
        self.existing_policy = policy
        self._core = core

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setSpacing(12)
        self.content_layout.addLayout(main_layout)

        # Name field (shared across both tabs)
        name_layout = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g., Low Spend, High Limit")
        if policy:
            self.name_input.setText(policy.name)
        name_layout.addRow("Name:", self.name_input)
        main_layout.addLayout(name_layout)

        # Tab widget
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Trading tab (LEFT)
        self._create_trading_tab(policy)

        # x402 tab (RIGHT)
        self._create_x402_tab(policy)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        main_layout.addWidget(buttons)

    def _create_trading_tab(self, policy: SpendPolicy = None):
        """Create the Trading tab with enable toggle and fields."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        # Enable toggle at top
        self.trading_enabled = QCheckBox("Enable Trading")
        self.trading_enabled.setProperty("role", "section-toggle")
        layout.addWidget(self.trading_enabled)

        # Container widget for all fields (so we can disable the whole thing)
        self._trading_form_container = QWidget()
        form = QFormLayout(self._trading_form_container)
        form.setSpacing(6)
        form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._trading_form_container)

        # Per Trade Max
        self.trade_max_input = QDoubleSpinBox()
        self.trade_max_input.setRange(0.01, 100000)
        self.trade_max_input.setDecimals(2)
        self.trade_max_input.setSuffix(" USD")
        self.trade_max_input.setValue(100.0)
        form.addRow("Per Trade Max:", self.trade_max_input)

        # Daily Volume Limit
        self.trade_daily_input = QDoubleSpinBox()
        self.trade_daily_input.setRange(0.01, 1000000)
        self.trade_daily_input.setDecimals(2)
        self.trade_daily_input.setSuffix(" USD")
        self.trade_daily_input.setValue(500.0)
        form.addRow("Daily Volume Limit:", self.trade_daily_input)

        # Auto-approve checkbox
        self.trade_auto_enabled = QCheckBox("Auto-approve trades below:")
        form.addRow(self.trade_auto_enabled)

        # Auto-approve threshold
        self.trade_auto_input = QDoubleSpinBox()
        self.trade_auto_input.setRange(0.01, 10000)
        self.trade_auto_input.setDecimals(2)
        self.trade_auto_input.setSuffix(" USD")
        self.trade_auto_input.setValue(10.0)
        self.trade_auto_input.setEnabled(False)
        form.addRow("", self.trade_auto_input)

        # Min ETH Reserve
        self.min_eth_input = QDoubleSpinBox()
        self.min_eth_input.setRange(0, 10)
        self.min_eth_input.setDecimals(6)
        self.min_eth_input.setSuffix(" ETH")
        self.min_eth_input.setValue(0.0001)
        form.addRow("Min ETH Reserve:", self.min_eth_input)

        # Max Slippage %
        self.max_slip_input = QDoubleSpinBox()
        self.max_slip_input.setRange(0.1, 50)
        self.max_slip_input.setDecimals(1)
        self.max_slip_input.setSuffix(" %")
        self.max_slip_input.setValue(3.0)
        form.addRow("Max Slippage:", self.max_slip_input)

        # Help text
        layout.addStretch()
        help_label = QLabel(
            "Trading allows agents to swap tokens via Uniswap. "
            "Vault re-quotes and validates each trade against these limits."
        )
        help_label.setWordWrap(True)
        help_label.setProperty("role", "hint")
        layout.addWidget(help_label)

        # Load existing values if editing
        if policy and policy.trading_rules:
            tr = policy.trading_rules
            self.trading_enabled.setChecked(tr.enabled)
            self.trade_max_input.setValue(tr.per_trade_max_usd)
            self.trade_daily_input.setValue(tr.daily_volume_limit_usd)
            if tr.auto_approve_below_usd is not None:
                self.trade_auto_enabled.setChecked(True)
                self.trade_auto_input.setValue(tr.auto_approve_below_usd)
            self.min_eth_input.setValue(tr.min_reserve_eth)
            self.max_slip_input.setValue(tr.max_slippage_percent)

        # Connect signals
        self.trading_enabled.toggled.connect(self._on_trading_toggled)
        self.trade_auto_enabled.toggled.connect(
            lambda checked: self.trade_auto_input.setEnabled(checked and self.trading_enabled.isChecked())
        )

        # Initial state: disabled
        self._on_trading_toggled(self.trading_enabled.isChecked())

        self.tabs.addTab(tab, "Trading")

    def _create_x402_tab(self, policy: SpendPolicy = None):
        """Create the x402 tab with enable toggle and fields."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        # Enable toggle at top
        self.x402_enabled = QCheckBox("Enable x402 Payments")
        self.x402_enabled.setProperty("role", "section-toggle")
        layout.addWidget(self.x402_enabled)

        # Container widget for all fields (so we can disable the whole thing)
        self._x402_form_container = QWidget()
        form = QFormLayout(self._x402_form_container)
        form.setSpacing(6)
        form.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._x402_form_container)

        # Daily Limit
        self.daily_limit_input = QDoubleSpinBox()
        self.daily_limit_input.setRange(0.000001, 100000)
        self.daily_limit_input.setDecimals(6)
        self.daily_limit_input.setSuffix(" USDG")
        self.daily_limit_input.setValue(10.0 if not policy else policy.daily_limit_micro / 1_000_000)
        form.addRow("Daily Limit:", self.daily_limit_input)

        # Per Request Max
        self.per_request_input = QDoubleSpinBox()
        self.per_request_input.setRange(0.000001, 10000)
        self.per_request_input.setDecimals(6)
        self.per_request_input.setSuffix(" USDG")
        if not policy:
            self.per_request_input.setValue(1.0)
        elif policy.per_request_max_micro is not None:
            self.per_request_input.setValue(policy.per_request_max_micro / 1_000_000)
        else:
            self.per_request_input.setValue(10000)
        form.addRow("Per Request Max:", self.per_request_input)

        # Auto-approve checkbox
        self.auto_approve_enabled = QCheckBox("Auto-approve payments below:")
        form.addRow(self.auto_approve_enabled)

        # Auto-approve threshold
        self.auto_approve_input = QDoubleSpinBox()
        self.auto_approve_input.setRange(0.000001, 1000)
        self.auto_approve_input.setDecimals(6)
        self.auto_approve_input.setSuffix(" USDG")
        self.auto_approve_input.setValue(0.10)
        self.auto_approve_input.setEnabled(False)
        form.addRow("", self.auto_approve_input)

        if policy and policy.auto_approve_below_micro is not None:
            self.auto_approve_enabled.setChecked(True)
            self.auto_approve_input.setValue(policy.auto_approve_below_micro / 1_000_000)

        # Auto-approve help
        auto_help = QLabel("Signs payments below threshold without confirmation.")
        auto_help.setProperty("role", "hint")
        form.addRow(auto_help)

        # Domain restrictions
        domains_group = QGroupBox("Domain Restrictions")
        domains_layout = QVBoxLayout(domains_group)

        # Allowed domains
        allowed_label = QLabel("Allowed domains (one per line):")
        domains_layout.addWidget(allowed_label)

        self.allowed_domains_input = QTextEdit()
        self.allowed_domains_input.setPlaceholderText("e.g., stripe.com\nopenai.com")
        self.allowed_domains_input.setMaximumHeight(60)
        self.allowed_domains_input.setFont(QFont(Theme.MONO_FONT, 9))
        if policy and policy.allowed_domains:
            self.allowed_domains_input.setPlainText("\n".join(policy.allowed_domains))
        domains_layout.addWidget(self.allowed_domains_input)

        allowed_help = QLabel("Leave empty to allow all.")
        allowed_help.setProperty("role", "hint")
        domains_layout.addWidget(allowed_help)

        # Blocked domains
        blocked_label = QLabel("Blocked domains (one per line):")
        domains_layout.addWidget(blocked_label)

        self.blocked_domains_input = QTextEdit()
        self.blocked_domains_input.setPlaceholderText("e.g., malicious-site.com")
        self.blocked_domains_input.setMaximumHeight(60)
        self.blocked_domains_input.setFont(QFont(Theme.MONO_FONT, 9))
        if policy and policy.blocked_domains:
            self.blocked_domains_input.setPlainText("\n".join(policy.blocked_domains))
        domains_layout.addWidget(self.blocked_domains_input)

        blocked_help = QLabel("Blocked overrides allowlist. Subdomains included.")
        blocked_help.setProperty("role", "hint")
        domains_layout.addWidget(blocked_help)

        form.addRow(domains_group)

        # Determine if x402 should be enabled
        x402_active = policy is not None and policy.x402_enabled

        # Connect signals
        self.x402_enabled.toggled.connect(self._on_x402_toggled)
        self.auto_approve_enabled.toggled.connect(
            lambda checked: self.auto_approve_input.setEnabled(checked and self.x402_enabled.isChecked())
        )

        # Initial state
        self.x402_enabled.setChecked(x402_active)
        self._on_x402_toggled(x402_active)

        self.tabs.addTab(tab, "x402")

    def _on_trading_toggled(self, enabled: bool):
        """Enable/disable trading fields based on toggle."""
        self._trading_form_container.setEnabled(enabled)
        # Use QGraphicsOpacityEffect for visual greying
        if enabled:
            self._trading_form_container.setGraphicsEffect(None)
        else:
            effect = QGraphicsOpacityEffect()
            effect.setOpacity(0.4)
            self._trading_form_container.setGraphicsEffect(effect)
        # Auto-approve input has additional dependency
        if enabled:
            self.trade_auto_input.setEnabled(self.trade_auto_enabled.isChecked())

    def _on_x402_toggled(self, enabled: bool):
        """Enable/disable x402 fields based on toggle."""
        self._x402_form_container.setEnabled(enabled)
        # Use QGraphicsOpacityEffect for visual greying
        if enabled:
            self._x402_form_container.setGraphicsEffect(None)
        else:
            effect = QGraphicsOpacityEffect()
            effect.setOpacity(0.4)
            self._x402_form_container.setGraphicsEffect(effect)
        # Auto-approve input has additional dependency
        if enabled:
            self.auto_approve_input.setEnabled(self.auto_approve_enabled.isChecked())

    def validate_and_accept(self):
        """Validate inputs before accepting."""
        name = self.name_input.text().strip()
        if not name:
            FramelessMessageBox.warning(self, "Validation Error", "Policy name is required.")
            return

        # Check for duplicate name using core
        if self._core:
            for policy in self._core.get_all_policies():
                if policy.name == name and (not self.existing_policy or policy.id != self.existing_policy.id):
                    FramelessMessageBox.warning(self, "Duplicate Name", f"Policy name '{name}' already exists.")
                    return

        # At least one lane must be enabled
        if not self.trading_enabled.isChecked() and not self.x402_enabled.isChecked():
            FramelessMessageBox.warning(
                self, "Validation Error",
                "At least one lane must be enabled (Trading or x402)."
            )
            return

        self.accept()

    def _parse_domains(self, text: str) -> list[str]:
        """Parse domain list from textarea, filtering empty lines."""
        lines = text.strip().split("\n")
        return [line.strip().lower() for line in lines if line.strip()]

    def get_policy(self) -> SpendPolicy:
        """Create a SpendPolicy from the dialog inputs."""
        name = self.name_input.text().strip()

        # x402 settings (use defaults if disabled)
        if self.x402_enabled.isChecked():
            daily_limit_micro = round(self.daily_limit_input.value() * 1_000_000)
            per_request_max_micro = round(self.per_request_input.value() * 1_000_000)
            auto_approve_below_micro = None
            if self.auto_approve_enabled.isChecked():
                auto_approve_below_micro = round(self.auto_approve_input.value() * 1_000_000)
            allowed_domains = self._parse_domains(self.allowed_domains_input.toPlainText())
            blocked_domains = self._parse_domains(self.blocked_domains_input.toPlainText())
        else:
            # x402 disabled - use minimal defaults
            daily_limit_micro = 0
            per_request_max_micro = 0
            auto_approve_below_micro = None
            allowed_domains = []
            blocked_domains = []

        # Trading rules
        trading_rules = None
        if self.trading_enabled.isChecked():
            auto_approve = None
            if self.trade_auto_enabled.isChecked():
                auto_approve = self.trade_auto_input.value()
            trading_rules = TradingRules(
                enabled=True,
                per_trade_max_usd=self.trade_max_input.value(),
                daily_volume_limit_usd=self.trade_daily_input.value(),
                auto_approve_below_usd=auto_approve,
                min_reserve_eth=self.min_eth_input.value(),
                max_slippage_percent=self.max_slip_input.value(),
            )

        # Networks: always include the default network (4663 - Primer Testnet)
        networks = [4663]

        return SpendPolicy.create(
            name=name,
            networks=networks,
            daily_limit_micro=daily_limit_micro,
            per_request_max_micro=per_request_max_micro,
            auto_approve_below_micro=auto_approve_below_micro,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            trading_rules=trading_rules,
            x402_enabled=self.x402_enabled.isChecked(),
        )

    def get_policy_data(self) -> dict:
        """Return policy parameters as dict for core.create_policy()."""
        name = self.name_input.text().strip()

        # Networks: always include the default network (4663 - Primer Testnet)
        networks = [4663]

        # x402 settings
        if self.x402_enabled.isChecked():
            daily_limit_micro = round(self.daily_limit_input.value() * 1_000_000)
            per_request_max_micro = round(self.per_request_input.value() * 1_000_000)
            auto_approve_below_micro = None
            if self.auto_approve_enabled.isChecked():
                auto_approve_below_micro = round(self.auto_approve_input.value() * 1_000_000)
            allowed_domains = self._parse_domains(self.allowed_domains_input.toPlainText())
            blocked_domains = self._parse_domains(self.blocked_domains_input.toPlainText())
        else:
            daily_limit_micro = 0
            per_request_max_micro = 0
            auto_approve_below_micro = None
            allowed_domains = []
            blocked_domains = []

        # Trading rules
        trading_rules = None
        if self.trading_enabled.isChecked():
            auto_approve = None
            if self.trade_auto_enabled.isChecked():
                auto_approve = self.trade_auto_input.value()
            trading_rules = TradingRules(
                enabled=True,
                per_trade_max_usd=self.trade_max_input.value(),
                daily_volume_limit_usd=self.trade_daily_input.value(),
                auto_approve_below_usd=auto_approve,
                min_reserve_eth=self.min_eth_input.value(),
                max_slippage_percent=self.max_slip_input.value(),
            )

        return {
            "name": name,
            "networks": networks,
            "daily_limit_micro": daily_limit_micro,
            "per_request_max_micro": per_request_max_micro,
            "auto_approve_below_micro": auto_approve_below_micro,
            "allowed_domains": allowed_domains if allowed_domains else None,
            "blocked_domains": blocked_domains if blocked_domains else None,
            "trading_rules": trading_rules,
            "x402_enabled": self.x402_enabled.isChecked(),
        }


# ============================================
# Settings Dialog
# ============================================

class SettingsDialog(FramelessDialog):
    """Dialog for application settings (notifications, startup, window behavior)."""

    def __init__(self, settings: dict, core=None, parent=None):
        super().__init__("Preferences", parent, width=450)

        self.core = core
        self._settings = settings.copy()
        self._changed = False

        layout = self.content_layout

        # Notifications group
        notif_group = QGroupBox("Notifications")
        notif_layout = QFormLayout(notif_group)

        notif_desc = QLabel("Control how Vault notifies you about events.")
        notif_desc.setWordWrap(True)
        notif_desc.setProperty("role", "muted")
        notif_layout.addRow(notif_desc)

        self.sound_checkbox = QCheckBox("Play sound for approval requests")
        self.sound_checkbox.setChecked(self._settings.get("sound_enabled", True))
        self.sound_checkbox.stateChanged.connect(self._on_setting_changed)
        notif_layout.addRow(self.sound_checkbox)

        self.toast_checkbox = QCheckBox("Show system notifications")
        self.toast_checkbox.setChecked(self._settings.get("toast_enabled", True))
        self.toast_checkbox.stateChanged.connect(self._on_setting_changed)
        notif_layout.addRow(self.toast_checkbox)

        self.flash_checkbox = QCheckBox("Flash taskbar for approval requests")
        self.flash_checkbox.setChecked(self._settings.get("flash_taskbar", True))
        self.flash_checkbox.stateChanged.connect(self._on_setting_changed)
        notif_layout.addRow(self.flash_checkbox)

        layout.addWidget(notif_group)

        # Window behavior group
        window_group = QGroupBox("Window and Appearance")
        window_layout = QFormLayout(window_group)

        self.minimize_to_tray_checkbox = QCheckBox("Minimize to system tray instead of taskbar")
        self.minimize_to_tray_checkbox.setChecked(self._settings.get("minimize_to_tray", False))
        self.minimize_to_tray_checkbox.stateChanged.connect(self._on_setting_changed)
        window_layout.addRow(self.minimize_to_tray_checkbox)

        self.close_to_tray_checkbox = QCheckBox("Close to system tray (keep running)")
        self.close_to_tray_checkbox.setChecked(self._settings.get("close_to_tray", False))
        self.close_to_tray_checkbox.stateChanged.connect(self._on_setting_changed)
        window_layout.addRow(self.close_to_tray_checkbox)

        self.start_minimized_checkbox = QCheckBox("Start minimized")
        self.start_minimized_checkbox.setChecked(self._settings.get("start_minimized", False))
        self.start_minimized_checkbox.stateChanged.connect(self._on_setting_changed)
        window_layout.addRow(self.start_minimized_checkbox)

        # Appearance — segmented Light / Dark
        from PyQt6.QtWidgets import QButtonGroup, QWidget, QHBoxLayout
        appearance_row = QWidget()
        appearance_layout = QHBoxLayout(appearance_row)
        appearance_layout.setContentsMargins(0, 0, 0, 0)
        appearance_layout.setSpacing(0)
        self.theme_group = QButtonGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_light_btn = QPushButton("Light")
        self.theme_dark_btn = QPushButton("Dark")
        for i, btn in enumerate((self.theme_light_btn, self.theme_dark_btn)):
            btn.setCheckable(True)
            btn.setProperty("segment", "true")
            self.theme_group.addButton(btn, i)
            appearance_layout.addWidget(btn)
        is_dark = self._settings.get("theme", "light") == "dark"
        (self.theme_dark_btn if is_dark else self.theme_light_btn).setChecked(True)
        self.theme_group.idClicked.connect(self._on_setting_changed)
        appearance_layout.addStretch()
        window_layout.addRow("Appearance:", appearance_row)

        layout.addWidget(window_group)

        # Note: "Start server automatically" moved to Settings > Network dialog

        # Logging group
        logging_group = QGroupBox("Logging")
        logging_layout = QFormLayout(logging_group)

        logging_desc = QLabel("Control log persistence between sessions.")
        logging_desc.setWordWrap(True)
        logging_desc.setProperty("role", "muted")
        logging_layout.addRow(logging_desc)

        from PyQt6.QtWidgets import QSpinBox

        self.log_lines_input = QSpinBox()
        self.log_lines_input.setRange(0, 10000)
        self.log_lines_input.setSingleStep(100)
        self.log_lines_input.setValue(self._settings.get("log_lines_on_startup", 0))
        self.log_lines_input.setToolTip("0 = start fresh each session")
        self.log_lines_input.setMaximumWidth(100)
        self.log_lines_input.valueChanged.connect(self._on_setting_changed)
        logging_layout.addRow("Load recent logs on startup:", self.log_lines_input)

        self.log_retention_input = QSpinBox()
        self.log_retention_input.setRange(0, 365)
        self.log_retention_input.setValue(self._settings.get("log_retention_days", 0))
        self.log_retention_input.setSuffix(" days")
        self.log_retention_input.setToolTip("0 = don't save logs to disk")
        self.log_retention_input.setMaximumWidth(100)
        self.log_retention_input.valueChanged.connect(self._on_setting_changed)
        logging_layout.addRow("Keep log files for:", self.log_retention_input)

        layout.addWidget(logging_group)

        # Security group
        security_group = QGroupBox("Security")
        security_layout = QFormLayout(security_group)

        security_desc = QLabel("Automatically lock wallet after period of inactivity.")
        security_desc.setWordWrap(True)
        security_desc.setProperty("role", "muted")
        security_layout.addRow(security_desc)

        self.auto_lock_input = QSpinBox()
        self.auto_lock_input.setRange(0, 60)
        self.auto_lock_input.setValue(self._settings.get("auto_lock_minutes", 0))
        self.auto_lock_input.setSuffix(" min")
        self.auto_lock_input.setToolTip("0 = disabled")
        self.auto_lock_input.setMaximumWidth(100)
        self.auto_lock_input.valueChanged.connect(self._on_setting_changed)
        security_layout.addRow("Auto-lock timeout:", self.auto_lock_input)

        # Replay window for signed requests
        replay_desc = QLabel("Maximum age for signed agent requests (prevents replay attacks).")
        replay_desc.setWordWrap(True)
        replay_desc.setProperty("role", "muted")
        security_layout.addRow(replay_desc)

        self.replay_window_input = QSpinBox()
        self.replay_window_input.setRange(30, 600)  # 30 seconds to 10 minutes
        self.replay_window_input.setValue(self._settings.get("replay_window_seconds", 300))
        self.replay_window_input.setSuffix(" sec")
        self.replay_window_input.setToolTip("How long signed requests remain valid (30-600 seconds)")
        self.replay_window_input.setMaximumWidth(100)
        self.replay_window_input.valueChanged.connect(self._on_setting_changed)
        security_layout.addRow("Replay window:", self.replay_window_input)

        # Admin API access mode
        admin_api_desc = QLabel("Control which processes can access the admin API.")
        admin_api_desc.setWordWrap(True)
        admin_api_desc.setProperty("role", "muted")
        security_layout.addRow(admin_api_desc)

        from ..core.settings import ADMIN_API_MODE_OPEN, ADMIN_API_MODE_GUI_ONLY
        self.admin_api_mode_combo = QComboBox()
        self.admin_api_mode_combo.addItem("Open (any local process)", ADMIN_API_MODE_OPEN)
        self.admin_api_mode_combo.addItem("GUI Only (block CLI/tools)", ADMIN_API_MODE_GUI_ONLY)
        # Set current value from settings if core is available
        if self.core:
            current_mode = self.core.settings_manager.get_admin_api_mode()
            index = 0 if current_mode == ADMIN_API_MODE_OPEN else 1
            self.admin_api_mode_combo.setCurrentIndex(index)
        self.admin_api_mode_combo.currentIndexChanged.connect(self._on_setting_changed)
        security_layout.addRow("Admin API:", self.admin_api_mode_combo)

        layout.addWidget(security_group)

        layout.addStretch()

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_setting_changed(self, state: int):
        """Handle any setting checkbox change."""
        self._changed = True

    def get_settings(self) -> dict:
        """Return the modified settings."""
        return {
            "sound_enabled": self.sound_checkbox.isChecked(),
            "toast_enabled": self.toast_checkbox.isChecked(),
            "flash_taskbar": self.flash_checkbox.isChecked(),
            "minimize_to_tray": self.minimize_to_tray_checkbox.isChecked(),
            "close_to_tray": self.close_to_tray_checkbox.isChecked(),
            "start_minimized": self.start_minimized_checkbox.isChecked(),
            "theme": "dark" if self.theme_dark_btn.isChecked() else "light",
            # Note: auto_start_server moved to Network Settings dialog
            "log_lines_on_startup": self.log_lines_input.value(),
            "log_retention_days": self.log_retention_input.value(),
            "auto_lock_minutes": self.auto_lock_input.value(),
            "replay_window_seconds": self.replay_window_input.value(),
            "admin_api_mode": self.admin_api_mode_combo.currentData(),
        }

    def has_changes(self) -> bool:
        """Check if settings were modified."""
        return self._changed


# ============================================
# Network Settings Dialog
# ============================================

# Default RHC RPC endpoint
DEFAULT_RHC_RPC = "https://rpc.mainnet.chain.robinhood.com"

# Uniswap v3 contract addresses on RHC — single source of truth is networks.DEX
# (Qt-free, shared with the trading engine).
_RHC_DEX = get_dex(4663)
RHC_UNISWAP_FACTORY = _RHC_DEX.factory
RHC_UNISWAP_QUOTER_V2 = _RHC_DEX.quoter_v2
RHC_UNISWAP_ROUTER = _RHC_DEX.swap_router


class NetworkSettingsDialog(FramelessDialog):
    """Dialog for network settings (agent server, RPC, advanced)."""

    # Signals for thread-safe UI updates (Qt signals are thread-safe)
    rhc_status_signal = pyqtSignal(bool, int, str)  # connected, block_num, error
    dex_status_signal = pyqtSignal(bool, str)  # available, error

    def __init__(self, core, settings: dict, parent=None):
        super().__init__("Network Settings", parent, width=480)
        self.core = core

        self._settings = settings.copy()
        self._changed = False

        # Connect signals to update methods (runs on main thread when emitted from any thread)
        self.rhc_status_signal.connect(self._update_rhc_status)
        self.dex_status_signal.connect(self._update_dex_status)

        layout = self.content_layout

        # === Status Section ===
        status_group = QGroupBox("Status")
        status_layout = QFormLayout(status_group)

        # Server status
        self.server_status_label = QLabel()
        self._update_server_status()
        status_layout.addRow("Agent Link:", self.server_status_label)

        # RHC status
        self.rhc_status_label = QLabel("Checking...")
        status_layout.addRow("Robinhood Chain:", self.rhc_status_label)

        # DEX status (Uniswap v3 on RHC)
        self.dex_status_label = QLabel("Checking...")
        status_layout.addRow("Uniswap:", self.dex_status_label)

        layout.addWidget(status_group)

        # === Agent Link Section ===
        agent_group = QGroupBox("Agent Link")
        agent_layout = QFormLayout(agent_group)

        from PyQt6.QtWidgets import QSpinBox
        from ..core.settings import DEFAULT_PORT

        # Port input with server button on same row
        port_row = QHBoxLayout()
        self.port_input = QSpinBox()
        self.port_input.setRange(1024, 65535)
        self.port_input.setValue(self._settings.get("server_port", DEFAULT_PORT))
        self.port_input.setMaximumWidth(80)
        self.port_input.setEnabled(self._settings.get("custom_port_enabled", False))
        self.port_input.valueChanged.connect(self._on_setting_changed)
        port_row.addWidget(self.port_input)

        self.custom_port_checkbox = QCheckBox("Custom")
        self.custom_port_checkbox.setChecked(self._settings.get("custom_port_enabled", False))
        self.custom_port_checkbox.stateChanged.connect(self._on_custom_port_toggled)
        port_row.addWidget(self.custom_port_checkbox)

        port_row.addStretch()

        # Server start/stop button (inline with port)
        self.server_btn = QPushButton()
        self._update_server_button()
        self.server_btn.clicked.connect(self._toggle_server)
        port_row.addWidget(self.server_btn)

        agent_layout.addRow("Port:", port_row)

        # Allow LAN (read from core settings)
        self.allow_lan_checkbox = QCheckBox("Allow LAN connections")
        self.allow_lan_checkbox.setToolTip("Bind to 0.0.0.0 to allow network access")
        self.allow_lan_checkbox.setChecked(self.core.settings_manager.get_allow_lan())
        self.allow_lan_checkbox.stateChanged.connect(self._on_setting_changed)
        agent_layout.addRow("", self.allow_lan_checkbox)

        # Auto-start server
        self.auto_start_checkbox = QCheckBox("Start automatically on launch")
        self.auto_start_checkbox.setChecked(self._settings.get("auto_start_server", True))
        self.auto_start_checkbox.stateChanged.connect(self._on_setting_changed)
        agent_layout.addRow("", self.auto_start_checkbox)

        layout.addWidget(agent_group)

        # === RHC RPC Section ===
        rpc_group = QGroupBox("Robinhood Chain RPC")
        rpc_layout = QVBoxLayout(rpc_group)

        rpc_desc = QLabel("Custom RPC endpoint for RHC mainnet. Leave blank to use default.")
        rpc_desc.setWordWrap(True)
        rpc_desc.setProperty("role", "muted")
        rpc_layout.addWidget(rpc_desc)

        rpc_row = QHBoxLayout()
        self.rpc_input = QLineEdit()
        self.rpc_input.setPlaceholderText(DEFAULT_RHC_RPC)
        self.rpc_input.setText(self._settings.get("rhc_rpc", ""))
        self.rpc_input.setFont(QFont(Theme.MONO_FONT, 9))
        self.rpc_input.textChanged.connect(self._on_setting_changed)
        rpc_row.addWidget(self.rpc_input)

        reset_btn = QPushButton("Reset")
        reset_btn.setMaximumWidth(60)
        reset_btn.clicked.connect(self._reset_rpc)
        rpc_row.addWidget(reset_btn)

        rpc_layout.addLayout(rpc_row)
        layout.addWidget(rpc_group)

        # === Advanced Section ===
        advanced_group = QGroupBox("Advanced")
        advanced_layout = QFormLayout(advanced_group)

        # Rate limit
        self.rate_limit_input = QSpinBox()
        self.rate_limit_input.setRange(0, 1000)
        self.rate_limit_input.setValue(self._settings.get("rate_limit", 300))
        self.rate_limit_input.setSuffix(" req/min")
        self.rate_limit_input.setToolTip("0 = unlimited")
        self.rate_limit_input.setMaximumWidth(120)
        self.rate_limit_input.valueChanged.connect(self._on_setting_changed)
        advanced_layout.addRow("Rate limit:", self.rate_limit_input)

        # Verify settlements (read from core settings)
        self.verify_checkbox = QCheckBox("Verify settlements on-chain")
        self.verify_checkbox.setToolTip("Verify transaction hashes on-chain after settlement")
        self.verify_checkbox.setChecked(self.core.settings_manager.get_verify_settlements())
        self.verify_checkbox.stateChanged.connect(self._on_setting_changed)
        advanced_layout.addRow("", self.verify_checkbox)

        # Uniswap v3 contract addresses (read-only by default, Edit button enables editing)
        uniswap_label = QLabel("Uniswap v3 Addresses")
        uniswap_label.setProperty("role", "muted")
        advanced_layout.addRow(uniswap_label)

        # Read-only address fields are greyed by the QLineEdit:read-only QSS rule;
        # toggling setReadOnly() alone drives the styling.

        # Factory address
        self.uniswap_factory_input = QLineEdit()
        self.uniswap_factory_input.setText(self._settings.get("uniswap_factory", RHC_UNISWAP_FACTORY))
        self.uniswap_factory_input.setFont(QFont(Theme.MONO_FONT, 9))
        self.uniswap_factory_input.setToolTip("Uniswap v3 Factory contract (used for status check)")
        self.uniswap_factory_input.setReadOnly(True)
        self.uniswap_factory_input.textChanged.connect(self._on_setting_changed)
        advanced_layout.addRow("Factory:", self.uniswap_factory_input)

        # QuoterV2 address
        self.uniswap_quoter_input = QLineEdit()
        self.uniswap_quoter_input.setText(self._settings.get("uniswap_quoter", RHC_UNISWAP_QUOTER_V2))
        self.uniswap_quoter_input.setFont(QFont(Theme.MONO_FONT, 9))
        self.uniswap_quoter_input.setToolTip("Uniswap v3 QuoterV2 contract")
        self.uniswap_quoter_input.setReadOnly(True)
        self.uniswap_quoter_input.textChanged.connect(self._on_setting_changed)
        advanced_layout.addRow("QuoterV2:", self.uniswap_quoter_input)

        # SwapRouter02 address
        self.uniswap_router_input = QLineEdit()
        self.uniswap_router_input.setText(self._settings.get("uniswap_router", RHC_UNISWAP_ROUTER))
        self.uniswap_router_input.setFont(QFont(Theme.MONO_FONT, 9))
        self.uniswap_router_input.setToolTip("Uniswap v3 SwapRouter02 contract")
        self.uniswap_router_input.setReadOnly(True)
        self.uniswap_router_input.textChanged.connect(self._on_setting_changed)
        advanced_layout.addRow("Router:", self.uniswap_router_input)

        # Edit and Reset buttons for addresses
        addr_btn_row = QHBoxLayout()
        self.edit_addresses_btn = QPushButton("Edit")
        self.edit_addresses_btn.setMaximumWidth(60)
        self.edit_addresses_btn.clicked.connect(self._toggle_address_editing)
        self._addresses_editable = False
        addr_btn_row.addWidget(self.edit_addresses_btn)

        self.reset_addresses_btn = QPushButton("Reset")
        self.reset_addresses_btn.setMaximumWidth(60)
        self.reset_addresses_btn.setToolTip("Reset to default Uniswap v3 addresses")
        self.reset_addresses_btn.clicked.connect(self._reset_addresses)
        addr_btn_row.addWidget(self.reset_addresses_btn)
        addr_btn_row.addStretch()

        advanced_layout.addRow("", addr_btn_row)

        layout.addWidget(advanced_group)

        layout.addStretch()

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Initial status checks
        self._check_rhc_connection()
        self._check_dex_connection()

        # Poll every 60 seconds while dialog is open
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_status)
        self._poll_timer.start(60000)  # 60 seconds

    def _poll_status(self):
        """Periodic status poll."""
        self._update_server_status()
        self._check_rhc_connection()
        self._check_dex_connection()

    def closeEvent(self, event):
        """Stop polling when dialog closes."""
        if hasattr(self, '_poll_timer'):
            self._poll_timer.stop()
        super().closeEvent(event)

    def _update_server_status(self):
        """Update the server status label."""
        if self.core.is_server_running():
            port = self.core.server_port
            self.server_status_label.setText(f"● Running on port {port}")
            set_role(self.server_status_label, status="on")
        else:
            self.server_status_label.setText("● Stopped")
            set_role(self.server_status_label, status="muted")

    def _update_server_button(self):
        """Update the server button text."""
        if self.core.is_server_running():
            self.server_btn.setText("Stop Server")
        else:
            self.server_btn.setText("Start Server")

    def _toggle_server(self):
        """Start or stop the server."""
        if self.core.is_server_running():
            self.core.stop_server()
        else:
            from ..core.settings import DEFAULT_PORT
            port = self.port_input.value() if self.custom_port_checkbox.isChecked() else DEFAULT_PORT
            allow_lan = self.allow_lan_checkbox.isChecked()
            self.core.start_server(port, allow_lan)

        # Update UI after a short delay
        QTimer.singleShot(100, self._update_server_status)
        QTimer.singleShot(100, self._update_server_button)

    def _on_custom_port_toggled(self, state: int):
        """Handle custom port checkbox toggle."""
        enabled = state == Qt.CheckState.Checked.value
        self.port_input.setEnabled(enabled)
        if not enabled:
            from ..core.settings import DEFAULT_PORT
            self.port_input.setValue(DEFAULT_PORT)
        self._on_setting_changed(state)

    def _reset_rpc(self):
        """Reset RPC to default."""
        self.rpc_input.clear()
        self._changed = True

    def _on_setting_changed(self, value):
        """Handle any setting change."""
        self._changed = True

    def _toggle_address_editing(self):
        """Toggle edit mode for Uniswap addresses.

        The QLineEdit:read-only QSS rule handles the greyed-out look, so we only
        flip setReadOnly() and re-polish so the pseudo-state restyles at once.
        """
        self._addresses_editable = not self._addresses_editable

        for field in (self.uniswap_factory_input,
                      self.uniswap_quoter_input,
                      self.uniswap_router_input):
            field.setReadOnly(not self._addresses_editable)
            style = field.style()
            if style is not None:
                style.unpolish(field)
                style.polish(field)

        # Update button text
        if self._addresses_editable:
            self.edit_addresses_btn.setText("Lock")
        else:
            self.edit_addresses_btn.setText("Edit")

    def _reset_addresses(self):
        """Reset Uniswap addresses to hardcoded defaults."""
        self.uniswap_factory_input.setText(RHC_UNISWAP_FACTORY)
        self.uniswap_quoter_input.setText(RHC_UNISWAP_QUOTER_V2)
        self.uniswap_router_input.setText(RHC_UNISWAP_ROUTER)
        self._changed = True

    def _check_rhc_connection(self):
        """Check RHC RPC connectivity in background (eth_blockNumber call)."""
        import threading

        # Capture values from UI thread before spawning background thread
        rpc_url = self.rpc_input.text().strip() or DEFAULT_RHC_RPC

        def check():
            import urllib.request
            import json
            import socket
            try:
                req = urllib.request.Request(
                    rpc_url,
                    data=json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "PrimerVault/1.0",
                    }
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    if "error" in data:
                        self.rhc_status_signal.emit(False, 0, "RPC error")
                    else:
                        block_hex = data.get("result", "0x0")
                        block_num = int(block_hex, 16)
                        self.rhc_status_signal.emit(True, block_num, "")
            except socket.timeout:
                self.rhc_status_signal.emit(False, 0, "Timeout")
            except urllib.error.URLError as e:
                reason = str(e.reason) if hasattr(e, 'reason') else str(e)
                self.rhc_status_signal.emit(False, 0, reason[:30])
            except Exception as e:
                self.rhc_status_signal.emit(False, 0, str(e)[:30])

        threading.Thread(target=check, daemon=True).start()

    def _update_rhc_status(self, connected: bool, block: int, error: str = ""):
        """Update RHC status label."""
        if connected:
            self.rhc_status_label.setText(f"● Block #{block:,}")
            set_role(self.rhc_status_label, status="on")
        else:
            msg = f"● Unreachable ({error})" if error else "● Unreachable"
            self.rhc_status_label.setText(msg)
            set_role(self.rhc_status_label, status="error")

    def _check_dex_connection(self):
        """Check Uniswap availability by verifying Factory contract has code."""
        import threading

        # Capture values from UI thread before spawning background thread
        factory_addr = self.uniswap_factory_input.text().strip() or RHC_UNISWAP_FACTORY
        rpc_url = self.rpc_input.text().strip() or DEFAULT_RHC_RPC

        def check():
            import urllib.request
            import json
            try:
                # Check if Uniswap Factory contract has code (proves deployment)
                call_data = {
                    "jsonrpc": "2.0",
                    "method": "eth_getCode",
                    "params": [factory_addr, "latest"],
                    "id": 1
                }
                req = urllib.request.Request(
                    rpc_url,
                    data=json.dumps(call_data).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "PrimerVault/1.0",
                    }
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    if "error" in data:
                        self.dex_status_signal.emit(False, "RPC error")
                    else:
                        code = data.get("result", "0x")
                        # Contract has code if result is more than just "0x"
                        if code and len(code) > 2:
                            self.dex_status_signal.emit(True, "")
                        else:
                            self.dex_status_signal.emit(False, "No contract")
            except Exception as e:
                self.dex_status_signal.emit(False, str(e)[:20])

        threading.Thread(target=check, daemon=True).start()

    def _update_dex_status(self, available: bool, error: str = ""):
        """Update DEX status label."""
        if available:
            self.dex_status_label.setText("● Available")
            set_role(self.dex_status_label, status="on")
        else:
            msg = f"● Unavailable ({error})" if error else "● Unavailable"
            self.dex_status_label.setText(msg)
            set_role(self.dex_status_label, status="error")

    def get_settings(self) -> dict:
        """Return the modified settings."""
        from ..core.settings import DEFAULT_PORT
        return {
            "server_port": self.port_input.value(),
            "custom_port_enabled": self.custom_port_checkbox.isChecked(),
            "allow_lan": self.allow_lan_checkbox.isChecked(),
            "auto_start_server": self.auto_start_checkbox.isChecked(),
            "rhc_rpc": self.rpc_input.text().strip(),
            "rate_limit": self.rate_limit_input.value(),
            "verify_settlements": self.verify_checkbox.isChecked(),
            "uniswap_factory": self.uniswap_factory_input.text().strip() or RHC_UNISWAP_FACTORY,
            "uniswap_quoter": self.uniswap_quoter_input.text().strip() or RHC_UNISWAP_QUOTER_V2,
            "uniswap_router": self.uniswap_router_input.text().strip() or RHC_UNISWAP_ROUTER,
        }

    def has_changes(self) -> bool:
        """Check if settings were modified."""
        return self._changed


# ============================================
# Transaction Detail Dialog
# ============================================

class TransactionDetailDialog(FramelessDialog):
    """Dialog showing full details of a transaction."""

    def __init__(self, tx, core, parent=None):
        """
        Args:
            tx: Transaction to display
            core: Vault core instance for verification/receipts
            parent: Parent widget
        """
        super().__init__(f"Transaction Details - {tx.id[:8]}", parent, width=550)
        self.setMinimumHeight(450)
        self.tx = tx
        self.core = core

        layout = self.content_layout
        layout.setSpacing(12)

        # Header with status
        header = QHBoxLayout()
        title = QLabel(f"Transaction {tx.id[:8]}...")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        header.addWidget(title)

        status_label = QLabel(tx.status.upper())
        status_label.setFont(QFont("", 10, QFont.Weight.Bold))
        # Settled but not verified reads as amber, not green.
        if tx.status == "settled" and getattr(tx, 'verification_status', None) != "verified":
            status_role = "pending"
        else:
            status_role = status_token(tx.status)
        set_role(status_label, status=status_role)
        header.addStretch()
        header.addWidget(status_label)

        layout.addLayout(header)

        # Info grid - type-aware
        info_group = QGroupBox("Details")
        info_layout = QFormLayout(info_group)
        info_layout.setSpacing(8)

        tx_type = getattr(tx, 'type', 'x402')
        type_labels = {'x402': 'x402 Payment', 'trade': 'Trade', 'transfer': 'Transfer'}
        info_layout.addRow("Type:", QLabel(type_labels.get(tx_type, tx_type)))
        info_layout.addRow("Agent:", QLabel(f"{tx.agent_name} ({tx.agent_id})"))
        info_layout.addRow("Network:", QLabel(tx.network))

        if tx_type == 'trade':
            # Trade-specific fields
            info_layout.addRow("Token In:", QLabel(f"{getattr(tx, 'symbol_in', '?')}"))
            info_layout.addRow("Amount In:", QLabel(f"{getattr(tx, 'amount_in', '?')}"))
            info_layout.addRow("Token Out:", QLabel(f"{getattr(tx, 'symbol_out', '?')}"))
            amt_out = getattr(tx, 'amount_out', None)
            info_layout.addRow("Amount Out:", QLabel(f"{amt_out}" if amt_out else "pending"))
            info_layout.addRow("Fee Tier:", QLabel(f"{getattr(tx, 'fee_tier', '?')} bps"))
            pool = getattr(tx, 'pool', None)
            if pool:
                pool_label = QLabel(pool)
                pool_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                info_layout.addRow("Pool:", pool_label)
        elif tx_type == 'transfer':
            # Transfer-specific fields
            info_layout.addRow("Token:", QLabel(f"{getattr(tx, 'transfer_symbol', 'ETH')}"))
            info_layout.addRow("Amount:", QLabel(f"{getattr(tx, 'transfer_amount', '?')}"))
            recipient_label = QLabel(tx.recipient or "?")
            recipient_label.setWordWrap(True)
            recipient_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            info_layout.addRow("Recipient:", recipient_label)
        else:
            # x402-specific fields
            info_layout.addRow("Amount:", QLabel(tx.format_amount()))
            recipient_label = QLabel(tx.recipient)
            recipient_label.setWordWrap(True)
            recipient_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            info_layout.addRow("Recipient:", recipient_label)
            if tx.resource:
                resource_label = QLabel(tx.resource)
                resource_label.setWordWrap(True)
                resource_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                info_layout.addRow("Resource:", resource_label)

        if tx.wallet_id:
            info_layout.addRow("Wallet:", QLabel(tx.wallet_id))

        layout.addWidget(info_group)

        # Timeline
        timeline_group = QGroupBox("Timeline")
        timeline_layout = QFormLayout(timeline_group)
        timeline_layout.setSpacing(6)

        timeline_layout.addRow("Received:", QLabel(tx.format_datetime()))

        if tx.signed_at:
            try:
                dt = datetime.fromisoformat(tx.signed_at.replace('Z', '+00:00'))
                signed_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                signed_str = tx.signed_at
            auto_str = " (auto)" if tx.auto_approved else ""
            timeline_layout.addRow("Signed:", QLabel(f"{signed_str}{auto_str}"))

        if tx.submitted_at:
            try:
                dt = datetime.fromisoformat(tx.submitted_at.replace('Z', '+00:00'))
                submitted_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                submitted_str = tx.submitted_at
            timeline_layout.addRow("Submitted:", QLabel(submitted_str))

        if tx.settled_at:
            try:
                dt = datetime.fromisoformat(tx.settled_at.replace('Z', '+00:00'))
                settled_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                settled_str = tx.settled_at
            timeline_layout.addRow("Settled:", QLabel(settled_str))

        if tx.reject_reason:
            reason_label = QLabel(tx.reject_reason)
            reason_label.setProperty("role", "error")
            timeline_layout.addRow("Reason:", reason_label)

        layout.addWidget(timeline_group)

        # Transaction hash (if settled)
        if tx.tx_hash:
            hash_group = QGroupBox("On-Chain")
            hash_layout = QVBoxLayout(hash_group)

            # Ensure tx_hash has 0x prefix for display and links
            display_hash = tx.tx_hash if tx.tx_hash.startswith("0x") else f"0x{tx.tx_hash}"

            hash_row = QHBoxLayout()
            hash_label = QLabel(display_hash)
            hash_label.setFont(QFont(Theme.MONO_FONT, 9))
            hash_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            hash_row.addWidget(hash_label)

            copy_btn = QPushButton("Copy")
            copy_btn.setMaximumWidth(60)
            copy_btn.clicked.connect(lambda h=display_hash: QApplication.clipboard().setText(h))
            hash_row.addWidget(copy_btn)

            hash_layout.addLayout(hash_row)

            # Verification status
            verify_row = QHBoxLayout()
            self.verify_status_label = QLabel(self._format_verification_status())
            verify_row.addWidget(self.verify_status_label)
            verify_row.addStretch()

            self.verify_btn = QPushButton("Verify")
            self.verify_btn.setMaximumWidth(70)
            self.verify_btn.setToolTip("Query blockchain to verify this transaction exists")
            self.verify_btn.clicked.connect(self._on_verify_clicked)
            # Disable if already verifying
            if tx.verification_status == "pending":
                self.verify_btn.setEnabled(False)
            verify_row.addWidget(self.verify_btn)

            hash_layout.addLayout(verify_row)

            # Link to block explorer (URL derived from the central registry)
            network_cfg = resolve_network(tx.network)
            explorer_url = f"{network_cfg.explorer_url}/tx/{display_hash}" if network_cfg else None

            if explorer_url:
                link = QLabel(f'<a href="{explorer_url}">View on Block Explorer</a>')
                link.setOpenExternalLinks(True)
                hash_layout.addWidget(link)

            layout.addWidget(hash_group)

        # x402 payload (collapsible)
        if tx.x402_data:
            payload_group = QGroupBox("x402 Payload")
            payload_layout = QVBoxLayout(payload_group)

            import json
            payload_text = QTextEdit()
            payload_text.setReadOnly(True)
            payload_text.setFont(QFont(Theme.MONO_FONT, 9))
            payload_text.setPlainText(json.dumps(tx.x402_data, indent=2))
            payload_text.setMaximumHeight(150)
            payload_layout.addWidget(payload_text)

            layout.addWidget(payload_group)

        layout.addStretch()

        # AP2 Receipt button and Close button
        button_row = QHBoxLayout()

        receipt_btn = QPushButton("View AP2 Receipt")
        receipt_btn.setToolTip("View AP2-formatted receipt for audit/compliance")
        receipt_btn.clicked.connect(self._show_ap2_receipt)
        button_row.addWidget(receipt_btn)

        button_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        button_row.addWidget(close_btn)

        layout.addLayout(button_row)

        # Poll for transaction updates if we have a verify button
        # (signing_service uses callbacks, not Qt signals)
        if hasattr(self, 'verify_btn'):
            from PyQt6.QtCore import QTimer
            self._update_timer = QTimer(self)
            self._update_timer.timeout.connect(self._check_for_updates)
            self._update_timer.start(500)  # Check every 500ms

    def _format_verification_status(self) -> str:
        """Format verification status for display."""
        status = self.tx.verification_status
        if status == "verified":
            block = self.tx.verification_block
            block_str = f" (block {block})" if block else ""
            return colored_span(f"✓ Verified{block_str}", "success")
        elif status == "failed":
            return colored_span("✗ Failed on-chain", "error")
        elif status == "not_found":
            return colored_span("✗ Not found on-chain", "error")
        elif status == "pending":
            return colored_span("⏳ Verifying...", "pending")
        else:
            return colored_span("Not verified", "muted")

    def _on_verify_clicked(self):
        """Handle verify button click."""
        self.verify_btn.setEnabled(False)
        self.tx.verification_status = "pending"  # Optimistic update
        self.verify_status_label.setText(self._format_verification_status())
        self.core.verify_transaction(self.tx)

    def _check_for_updates(self):
        """Poll for transaction updates (verification status changes)."""
        if not hasattr(self, 'verify_status_label'):
            return
        # Re-read from dialog's tx object which signing_service updates in place
        self.verify_status_label.setText(self._format_verification_status())
        if hasattr(self, 'verify_btn'):
            self.verify_btn.setEnabled(self.tx.verification_status != "pending")
        # Stop polling if verification is complete
        if self.tx.verification_status in ("verified", "failed", "not_found"):
            if hasattr(self, '_update_timer'):
                self._update_timer.stop()

    def _show_ap2_receipt(self):
        """Show AP2-formatted receipt in a dialog."""
        import json

        receipt = self.core.get_receipt(self.tx.id)

        if receipt.get("error"):
            FramelessMessageBox.warning(self, "Error", f"Could not get receipt: {receipt.get('error')}")
            return

        dialog = FramelessDialog(f"AP2 Receipt - {self.tx.id[:8]}", self)
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)

        layout = dialog.content_layout

        # Status header
        status = receipt.get("status", "unknown")
        status_label = QLabel(f"Status: {status.upper()}")
        status_label.setFont(QFont("", 12, QFont.Weight.Bold))
        set_role(status_label, status=("success" if status == "payment-completed" else "warn"))
        layout.addWidget(status_label)

        # JSON view
        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setFont(QFont(Theme.MONO_FONT, 9))
        text_edit.setPlainText(json.dumps(receipt, indent=2))
        layout.addWidget(text_edit)

        # Buttons
        button_row = QHBoxLayout()

        copy_btn = QPushButton("Copy JSON")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(json.dumps(receipt, indent=2)))
        button_row.addWidget(copy_btn)

        button_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        button_row.addWidget(close_btn)

        layout.addLayout(button_row)

        dialog.exec()


# ============================================
# Mandate Viewer Dialog
# ============================================

class MandateViewerDialog(FramelessDialog):
    """Dialog for viewing and managing an agent's Intent Mandate."""

    def __init__(self, agent: Agent, current_policy: Optional[SpendPolicy] = None, parent=None):
        super().__init__(f"Intent Mandate - {agent.name}", parent, width=550)
        self.setMinimumHeight(500)
        self.agent = agent
        self.mandate = agent.intent_mandate
        self.current_policy = current_policy
        self.revoked = False

        layout = self.content_layout
        layout.setSpacing(12)

        # Check for staleness
        stale, stale_reason = self._check_staleness()
        if stale and self.mandate:
            stale_banner = QLabel(f"⚠️ Mandate is stale: {stale_reason}\nConsider re-commissioning to regenerate.")
            stale_banner.setWordWrap(True)
            stale_banner.setObjectName("warningBox")
            layout.addWidget(stale_banner)

        if not self.mandate:
            # No mandate
            no_mandate = QLabel("No Intent Mandate has been generated for this agent.")
            no_mandate.setProperty("role", "muted")
            no_mandate.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(no_mandate)

            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.reject)
            layout.addWidget(close_btn)
            return

        # Header
        header = QHBoxLayout()
        title = QLabel("Intent Mandate")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        header.addWidget(title)

        mandate_id = self.mandate.get('id', 'unknown')[:8]
        id_label = QLabel(f"Mandate: {mandate_id}...")
        id_label.setFont(QFont(Theme.MONO_FONT, 10))
        id_label.setProperty("role", "muted")
        header.addStretch()
        header.addWidget(id_label)

        layout.addLayout(header)

        # Agent info
        agent_group = QGroupBox("Agent")
        agent_layout = QFormLayout(agent_group)
        agent_layout.setSpacing(6)

        agent_info = self.mandate.get('agent', {})
        agent_id_label = QLabel(agent_info.get('id', 'Unknown'))
        agent_id_label.setFont(QFont(Theme.MONO_FONT, 10))
        agent_layout.addRow("Agent ID:", agent_id_label)

        fingerprint = agent_info.get('authKeyFingerprint', '')
        if fingerprint:
            fp_label = QLabel(fingerprint)
            fp_label.setFont(QFont(Theme.MONO_FONT, 9))
            fp_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            agent_layout.addRow("Auth Fingerprint:", fp_label)

        layout.addWidget(agent_group)

        # Authorization
        auth_group = QGroupBox("Authorization")
        auth_layout = QFormLayout(auth_group)
        auth_layout.setSpacing(6)

        auth = self.mandate.get('authorization', {})
        # Show policy name from current_policy if available, otherwise just policy ID
        policy_display = self.current_policy.name if self.current_policy else auth.get('policyId', 'Unknown')[:8] + '...'
        auth_layout.addRow("Policy:", QLabel(policy_display))

        limits = auth.get('limits', {})
        currency = limits.get('currency', 'USDG')
        decimals = limits.get('decimals', 6)
        divisor = 10 ** decimals
        daily = limits.get('dailyLimit') or 0
        per_req = limits.get('perRequestMax') or 0
        auto = limits.get('autoApproveBelow')  # Can be None for manual-only
        auth_layout.addRow("Daily Limit:", QLabel(f"{daily/divisor:.6f} {currency}"))
        auth_layout.addRow("Per Request Max:", QLabel(f"{per_req/divisor:.6f} {currency}"))
        auto_text = f"{auto/divisor:.6f} {currency}" if auto is not None else "Manual only"
        auth_layout.addRow("Auto-approve Below:", QLabel(auto_text))

        networks = auth.get('networks', [])
        auth_layout.addRow("Networks:", QLabel(", ".join(networks) if networks else "None"))

        domains = auth.get('domains', {})
        allowlist = domains.get('allowlist', [])
        blocklist = domains.get('blocklist', [])
        if allowlist:
            auth_layout.addRow("Allowed Domains:", QLabel(", ".join(allowlist)))
        if blocklist:
            auth_layout.addRow("Blocked Domains:", QLabel(", ".join(blocklist)))

        layout.addWidget(auth_group)

        # Wallet
        wallet_group = QGroupBox("Signing Wallet")
        wallet_layout = QFormLayout(wallet_group)

        wallet_info = self.mandate.get('wallet', {})
        address = wallet_info.get('address', 'Unknown')
        addr_label = QLabel(address)
        addr_label.setFont(QFont(Theme.MONO_FONT, 9))
        addr_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        wallet_layout.addRow("Address:", addr_label)

        layout.addWidget(wallet_group)

        # Signature
        sig_info = self.mandate.get('signature', {})
        if sig_info:
            sig_group = QGroupBox("Signature")
            sig_layout = QFormLayout(sig_group)

            sig_layout.addRow("Type:", QLabel(sig_info.get('type', 'Unknown')))
            signer = sig_info.get('signer', '')
            if signer:
                signer_label = QLabel(signer)
                signer_label.setFont(QFont(Theme.MONO_FONT, 9))
                sig_layout.addRow("Signer:", signer_label)

            sig_value = sig_info.get('value', '')
            if sig_value:
                sig_short = sig_value[:20] + "..." if len(sig_value) > 20 else sig_value
                sig_label = QLabel(sig_short)
                sig_label.setFont(QFont(Theme.MONO_FONT, 9))
                sig_label.setToolTip(sig_value)
                sig_layout.addRow("Signature:", sig_label)

            layout.addWidget(sig_group)

        # Timestamps
        issued_at = self.mandate.get('issuedAt', '')
        if issued_at:
            try:
                dt = datetime.fromisoformat(issued_at.replace('Z', '+00:00'))
                issued_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except (ValueError, TypeError):
                issued_str = issued_at
            issued_label = QLabel(f"Issued: {issued_str}")
            issued_label.setProperty("role", "muted")
            layout.addWidget(issued_label)

        # Registry URL if available
        registry_url = self.mandate.get('registryUrl', '')
        if registry_url:
            registry_row = QHBoxLayout()
            registry_label = QLabel(f'<a href="{registry_url}">View on AP2 Registry</a>')
            registry_label.setOpenExternalLinks(True)
            registry_row.addWidget(registry_label)
            registry_row.addStretch()
            layout.addLayout(registry_row)

        layout.addStretch()

        # Buttons
        button_row = QHBoxLayout()

        view_json_btn = QPushButton("View JSON")
        view_json_btn.clicked.connect(self._show_json)
        button_row.addWidget(view_json_btn)

        copy_btn = QPushButton("Copy JSON")
        copy_btn.clicked.connect(self._copy_json)
        button_row.addWidget(copy_btn)

        button_row.addStretch()

        revoke_btn = QPushButton("Revoke Mandate")
        revoke_btn.setToolTip("Remove the mandate from this agent (cannot be undone)")
        revoke_btn.setProperty("variant", "danger")
        revoke_btn.clicked.connect(self._revoke_mandate)
        button_row.addWidget(revoke_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)

        layout.addLayout(button_row)

    def _show_json(self):
        """Show full mandate JSON in a dialog."""
        import json

        dialog = FramelessDialog("Intent Mandate JSON", self)
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)

        layout = dialog.content_layout

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setFont(QFont(Theme.MONO_FONT, 9))
        text_edit.setPlainText(json.dumps(self.mandate, indent=2))
        layout.addWidget(text_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        dialog.exec()

    def _copy_json(self):
        """Copy mandate JSON to clipboard."""
        import json
        QApplication.clipboard().setText(json.dumps(self.mandate, indent=2))

    def _revoke_mandate(self):
        """Revoke the mandate from the agent."""
        if FramelessMessageBox.question(
            self,
            "Revoke Mandate",
            f"Revoke the Intent Mandate for agent '{self.agent.name}'?\n\n"
            "This will remove the mandate from this agent. "
            "The agent can be recommissioned to generate a new mandate.",
            default_no=True
        ):
            self.agent.intent_mandate = None
            self.revoked = True
            self.accept()

    def was_revoked(self) -> bool:
        """Return True if the mandate was revoked."""
        return self.revoked

    def _check_staleness(self) -> tuple[bool, Optional[str]]:
        """Check if mandate is stale (doesn't match current policy)."""
        if not self.mandate or not self.current_policy:
            return False, None

        auth = self.mandate.get("authorization", {})
        mandate_policy_id = auth.get("policyId")
        mandate_limits = auth.get("limits", {})

        # Check if policy ID changed
        if mandate_policy_id != self.current_policy.id:
            return True, "Agent assigned a different policy"

        # Check if limits changed
        if mandate_limits.get("dailyLimit") != self.current_policy.daily_limit_micro:
            return True, "Daily limit changed"
        if mandate_limits.get("perRequestMax") != self.current_policy.per_request_max_micro:
            return True, "Per-request maximum changed"
        if mandate_limits.get("autoApproveBelow") != self.current_policy.auto_approve_below_micro:
            return True, "Auto-approve threshold changed"

        # Check domain restrictions
        mandate_domains = auth.get("domains", {})
        mandate_allowlist = set(mandate_domains.get("allowlist", []))
        mandate_blocklist = set(mandate_domains.get("blocklist", []))
        current_allowlist = set(self.current_policy.allowed_domains or [])
        current_blocklist = set(self.current_policy.blocked_domains or [])

        if mandate_allowlist != current_allowlist:
            return True, "Allowed domains changed"
        if mandate_blocklist != current_blocklist:
            return True, "Blocked domains changed"

        return False, None


# ============================================
# Export Keys Dialog
# ============================================

class ExportKeysDialog(FramelessDialog):
    """Dialog for exporting private keys and seed phrases."""

    def __init__(self, wallets: list[WalletInfoLike], get_wallet_fn, parent=None):
        """
        Initialize the export keys dialog.

        Args:
            wallets: List of WalletInfo objects for available wallets
            get_wallet_fn: Function to get an unlocked wallet by address
            parent: Parent widget
        """
        super().__init__("Export Keys", parent, width=500)
        self.setMinimumHeight(400)
        self.wallets = wallets
        self.get_wallet_fn = get_wallet_fn

        layout = self.content_layout
        layout.setSpacing(12)

        # Warning header
        warning_box = QGroupBox()
        warning_box.setObjectName("warningBox")
        warning_layout = QVBoxLayout(warning_box)

        warning_title = QLabel("Security Warning")
        warning_title.setFont(QFont("", 11, QFont.Weight.Bold))
        warning_title.setProperty("role", "warn")
        warning_layout.addWidget(warning_title)

        warning_text = QLabel(
            "Private keys and seed phrases give full control over your funds. "
            "Never share them with anyone. Store backups securely offline."
        )
        warning_text.setWordWrap(True)
        warning_text.setProperty("role", "warn")
        warning_layout.addWidget(warning_text)

        layout.addWidget(warning_box)

        # Address selection
        addr_label = QLabel("Select address to export:")
        layout.addWidget(addr_label)

        self.address_combo = QComboBox()
        self.address_combo.setFont(QFont(Theme.MONO_FONT, 9))
        for wallet_info in wallets:
            addr_short = format_address(wallet_info.address)
            entry_id = getattr(wallet_info, 'wallet_id', None) or getattr(wallet_info, 'id', '')
            self.address_combo.addItem(
                f"{entry_id}  {wallet_info.name}  ({addr_short})",
                wallet_info
            )
        self.address_combo.currentIndexChanged.connect(self._on_address_changed)
        layout.addWidget(self.address_combo)

        # Export options
        options_group = QGroupBox("Export Options")
        options_layout = QVBoxLayout(options_group)

        self.export_pkey_checkbox = QCheckBox("Private Key (hex)")
        self.export_pkey_checkbox.setChecked(True)
        options_layout.addWidget(self.export_pkey_checkbox)

        self.export_seed_checkbox = QCheckBox("Seed Phrase (if available)")
        self.export_seed_checkbox.setEnabled(False)  # Will be updated based on wallet type
        options_layout.addWidget(self.export_seed_checkbox)

        layout.addWidget(options_group)

        # Output area
        output_group = QGroupBox("Exported Data")
        output_layout = QVBoxLayout(output_group)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setFont(QFont(Theme.MONO_FONT, 9))
        self.output_text.setPlaceholderText("Click 'Reveal Keys' to show sensitive data...")
        self.output_text.setMinimumHeight(100)
        output_layout.addWidget(self.output_text)

        # Copy button
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(self._copy_to_clipboard)
        output_layout.addWidget(copy_btn)

        layout.addWidget(output_group)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)

        self.reveal_btn = QPushButton("Reveal Keys")
        self.reveal_btn.setDefault(True)
        self.reveal_btn.clicked.connect(self._reveal_keys)
        btn_layout.addWidget(self.reveal_btn)

        layout.addLayout(btn_layout)

        # Initialize state
        self._on_address_changed()

    def _on_address_changed(self):
        """Handle address selection change."""
        wallet_info = self.address_combo.currentData()
        if not wallet_info:
            return

        # Check if this is an HD wallet (has seed) or private key wallet
        wallet = self.get_wallet_fn(wallet_info.address)
        if wallet:
            is_hd = hasattr(wallet, 'seed_phrase') and wallet.seed_phrase is not None
            self.export_seed_checkbox.setEnabled(is_hd)
            if is_hd:
                self.export_seed_checkbox.setText("Seed Phrase")
            else:
                self.export_seed_checkbox.setText("Seed Phrase (not available - private key wallet)")
                self.export_seed_checkbox.setChecked(False)

        # Clear output when address changes
        self.output_text.clear()

    def _reveal_keys(self):
        """Reveal the selected keys."""
        wallet_info = self.address_combo.currentData()
        if not wallet_info:
            FramelessMessageBox.warning(self, "Error", "No address selected.")
            return

        wallet = self.get_wallet_fn(wallet_info.address)
        if not wallet:
            FramelessMessageBox.warning(
                self, "Error",
                "Could not retrieve wallet. Make sure the wallet is unlocked."
            )
            return

        # Confirm before revealing
        if not FramelessMessageBox.question(
            self,
            "Confirm Export",
            "You are about to reveal sensitive key material.\n\n"
            "Make sure no one is watching your screen.\n\n"
            "Continue?",
            default_no=True
        ):
            return

        output_lines = []
        output_lines.append(f"Address: {wallet_info.address}")
        output_lines.append(f"Name: {wallet_info.name}")
        output_lines.append("")

        # Export private key
        if self.export_pkey_checkbox.isChecked():
            try:
                # Find the address index for HD wallets
                if hasattr(wallet, '_addresses'):
                    # HD wallet - find the index from derivation path
                    pkey = None
                    for addr in wallet._addresses:
                        if addr.address.lower() == wallet_info.address.lower():
                            # Extract index from path like "m/44'/60'/0'/0/0"
                            path_parts = addr.path.split('/')
                            if path_parts:
                                try:
                                    index = int(path_parts[-1])
                                    pkey = wallet.get_private_key(index)
                                except ValueError:
                                    pkey = wallet.get_private_key(0)
                            break
                    if pkey is None:
                        # Fallback to index 0
                        pkey = wallet.get_private_key(0)
                else:
                    # Private key wallet
                    pkey = wallet.get_private_key(0)

                output_lines.append("Private Key:")
                output_lines.append(f"0x{pkey.hex()}")
                output_lines.append("")
            except Exception as e:
                output_lines.append(f"Error getting private key: {e}")
                output_lines.append("")

        # Export seed phrase
        if self.export_seed_checkbox.isChecked() and self.export_seed_checkbox.isEnabled():
            try:
                seed = wallet.seed_phrase
                if seed:
                    output_lines.append("Seed Phrase:")
                    output_lines.append(seed)
                    output_lines.append("")
            except Exception as e:
                output_lines.append(f"Error getting seed phrase: {e}")
                output_lines.append("")

        self.output_text.setPlainText("\n".join(output_lines))

    def _copy_to_clipboard(self):
        """Copy the output to clipboard with auto-clear."""
        text = self.output_text.toPlainText()
        if not text or text == self.output_text.placeholderText():
            FramelessMessageBox.information(
                self, "Nothing to Copy",
                "Click 'Reveal Keys' first to show the data."
            )
            return

        copy_sensitive_to_clipboard(text, self)


# ============================================
# Withdraw USDG Dialog
# ============================================

class WithdrawUSDGDialog(FramelessDialog):
    """Dialog for withdrawing USDG from a wallet (requires gas)."""

    def __init__(
        self,
        wallet_entry: AddressEntry,
        balance: float,
        get_private_key_fn,
        chain_id: int,
        parent=None
    ):
        """
        Initialize the withdraw dialog.

        Args:
            wallet_entry: The AddressEntry for the source wallet
            balance: Current USDG balance
            get_private_key_fn: Function to get private key by address_id
            chain_id: Chain ID for the transaction
            parent: Parent widget
        """
        super().__init__(f"Withdraw USDG - {wallet_entry.id}", parent, width=450)
        self.setFixedHeight(420)
        self.wallet_entry = wallet_entry
        self.balance = balance
        self.get_private_key_fn = get_private_key_fn
        self.chain_id = chain_id

        layout = self.content_layout
        layout.setSpacing(12)

        # Source info
        source_group = QGroupBox("Source")
        source_layout = QFormLayout(source_group)

        addr_label = QLabel(wallet_entry.address)
        addr_label.setFont(QFont(Theme.MONO_FONT, 9))
        addr_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        source_layout.addRow("Address:", addr_label)

        balance_label = QLabel(f"{balance:.6f} USDG")
        balance_label.setProperty("role", "strong")
        source_layout.addRow("Balance:", balance_label)

        network = NETWORKS.get(chain_id)
        network_label = QLabel(network.display_name if network else f"Chain {chain_id}")
        source_layout.addRow("Network:", network_label)

        layout.addWidget(source_group)

        # Warning about gas
        warning_box = QGroupBox()
        warning_box.setObjectName("warningBox")
        warning_layout = QVBoxLayout(warning_box)
        warning_text = QLabel(
            "This transaction requires gas. Ensure the source wallet has "
            f"sufficient {network.native_symbol if network else 'native token'} for gas fees."
        )
        warning_text.setWordWrap(True)
        warning_text.setProperty("role", "warn")
        warning_layout.addWidget(warning_text)
        layout.addWidget(warning_box)

        # Destination
        dest_group = QGroupBox("Destination")
        dest_layout = QFormLayout(dest_group)

        self.dest_input = QLineEdit()
        self.dest_input.setPlaceholderText("0x...")
        self.dest_input.setFont(QFont(Theme.MONO_FONT, 9))
        dest_layout.addRow("Address:", self.dest_input)

        layout.addWidget(dest_group)

        # Amount
        amount_group = QGroupBox("Amount")
        amount_layout = QFormLayout(amount_group)

        self.amount_input = QDoubleSpinBox()
        self.amount_input.setDecimals(6)  # USDG has 6 decimals
        self.amount_input.setMinimum(0.000001)
        self.amount_input.setMaximum(balance)
        self.amount_input.setValue(balance)
        self.amount_input.setSuffix(" USDG")
        amount_layout.addRow("Amount:", self.amount_input)

        max_btn = QPushButton("Max")
        max_btn.clicked.connect(lambda: self.amount_input.setValue(balance))
        amount_layout.addRow("", max_btn)

        layout.addWidget(amount_group)

        # Export key link
        export_link = QLabel('<a href="#">Export private key to use with external wallet</a>')
        export_link.setOpenExternalLinks(False)
        export_link.linkActivated.connect(self._export_key)
        export_link.setProperty("role", "muted")
        layout.addWidget(export_link)

        # Status area (hidden by default)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.send_btn = QPushButton("Send USDG")
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self._execute_withdraw)
        btn_layout.addWidget(self.send_btn)

        layout.addLayout(btn_layout)

    def _export_key(self):
        """Export the private key for this wallet."""
        if not FramelessMessageBox.question(
            self,
            "Export Private Key",
            "Are you sure you want to export the private key?\n\n"
            "Anyone with this key can access all funds in this wallet.\n"
            "Never share it with anyone.",
            default_no=True
        ):
            return

        try:
            pkey = self.get_private_key_fn(self.wallet_entry.id)
            if pkey:
                # Convert bytes to hex string if needed
                if isinstance(pkey, bytes):
                    pkey = "0x" + pkey.hex()
                copy_sensitive_to_clipboard(pkey, self)
        except Exception as e:
            FramelessMessageBox.critical(self, "Error", f"Failed to export key: {e}")

    def _execute_withdraw(self):
        """Execute the USDG transfer."""
        # Validate destination
        dest = self.dest_input.text().strip()
        if not dest or not dest.startswith("0x") or len(dest) != 42:
            FramelessMessageBox.warning(self, "Invalid Address", "Please enter a valid destination address.")
            return

        amount = self.amount_input.value()
        if amount <= 0 or amount > self.balance:
            FramelessMessageBox.warning(self, "Invalid Amount", "Please enter a valid amount.")
            return

        # Confirm
        if not FramelessMessageBox.question(
            self,
            "Confirm Transfer",
            f"Send {amount:.6f} USDG to:\n\n"
            f"{dest[:20]}...{dest[-8:]}\n\n"
            "This will use gas from the source wallet.\n"
            "Continue?",
            default_no=True
        ):
            return

        self.send_btn.setEnabled(False)
        self.send_btn.setText("Sending...")
        self.status_label.setText("Preparing transaction...")
        self.status_label.setVisible(True)
        QApplication.processEvents()

        try:
            from web3 import Web3
            from ..networks import TOKENS

            network = NETWORKS.get(self.chain_id)
            if not network:
                raise ValueError(f"Network not found: {self.chain_id}")

            usdg_config = TOKENS.get("USDG")
            if not usdg_config or self.chain_id not in usdg_config.addresses:
                raise ValueError(f"USDG not configured for chain {self.chain_id}")

            usdg_address = usdg_config.addresses[self.chain_id]
            # Use Decimal to avoid floating point errors, then floor to ensure we don't exceed balance
            from decimal import Decimal, ROUND_DOWN
            amount_decimal = Decimal(str(amount)).quantize(Decimal('0.000001'), rounding=ROUND_DOWN)
            amount_raw = int(amount_decimal * 1_000_000)

            # Get private key
            pkey = self.get_private_key_fn(self.wallet_entry.id)
            if not pkey:
                raise ValueError("Could not retrieve private key")

            # Connect to network
            w3 = Web3(Web3.HTTPProvider(network.rpc_url))
            if not w3.is_connected():
                raise ValueError(f"Could not connect to {network.display_name}")

            # ERC-20 transfer ABI
            erc20_abi = [
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(usdg_address),
                abi=erc20_abi
            )

            # Build transaction
            from_addr = Web3.to_checksum_address(self.wallet_entry.address)
            to_addr = Web3.to_checksum_address(dest)

            self.status_label.setText("Estimating gas...")
            QApplication.processEvents()

            nonce = w3.eth.get_transaction_count(from_addr)
            gas_price = w3.eth.gas_price

            tx = contract.functions.transfer(to_addr, amount_raw).build_transaction({
                'from': from_addr,
                'nonce': nonce,
                'gasPrice': gas_price,
                'chainId': self.chain_id,
            })

            # Estimate gas
            try:
                gas_estimate = w3.eth.estimate_gas(tx)
                tx['gas'] = int(gas_estimate * 1.2)  # 20% buffer
            except Exception as e:
                raise ValueError(f"Gas estimation failed: {e}")

            self.status_label.setText("Signing transaction...")
            QApplication.processEvents()

            # Sign and send
            from eth_account import Account
            account = Account.from_key(pkey)
            signed_tx = account.sign_transaction(tx)

            self.status_label.setText("Broadcasting transaction...")
            QApplication.processEvents()

            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            self.status_label.setText(f"Transaction sent: {tx_hash_hex[:16]}...")
            self.status_label.setProperty("role", "success")

            # Show success with explorer link
            explorer_url = f"{network.explorer_url}/tx/0x{tx_hash_hex}"
            FramelessMessageBox.information(
                self,
                "Transaction Sent",
                f"USDG transfer submitted!\n\n"
                f"Amount: {amount:.6f} USDG\n"
                f"To: {dest[:16]}...{dest[-8:]}\n\n"
                f"Transaction: {tx_hash_hex[:16]}...\n\n"
                f"View on explorer:\n{explorer_url}"
            )

            self.accept()

        except Exception as e:
            self.status_label.setText(f"Error: {e}")
            self.status_label.setProperty("role", "error")
            FramelessMessageBox.critical(self, "Transfer Failed", str(e))

        finally:
            self.send_btn.setEnabled(True)
            self.send_btn.setText("Send USDG")


# ============================================
# Sweep USDG Dialog
# ============================================

class SweepUSDGDialog(FramelessDialog):
    """
    Dialog for sweeping USDG from multiple donor addresses to a single recipient.
    Uses EIP-3009 transferWithAuthorization so sponsor pays all gas.
    """

    def __init__(
        self,
        parent: QWidget,
        addresses: list,  # List of AddressEntry objects
        get_private_key_fn,  # Function(address_id) -> bytes
        chain_id: int,
    ):
        super().__init__("Sweep USDG", parent, width=550)
        self.setMinimumHeight(500)
        self.addresses = addresses
        self.get_private_key_fn = get_private_key_fn
        self.chain_id = chain_id

        self._setup_ui()
        self._refresh_balances()

    def _setup_ui(self):
        layout = self.content_layout

        # Network selector
        network_layout = QHBoxLayout()
        network_layout.addWidget(QLabel("Network:"))
        self.network_combo = QComboBox()
        from ..networks import NETWORKS
        for cid, network in NETWORKS.items():
            self.network_combo.addItem(network.display_name, cid)
            if cid == self.chain_id:
                self.network_combo.setCurrentIndex(self.network_combo.count() - 1)
        self.network_combo.currentIndexChanged.connect(self._on_network_changed)
        self.network_combo.setFixedWidth(180)
        network_layout.addWidget(self.network_combo)
        network_layout.addStretch()
        layout.addLayout(network_layout)

        # Sponsor (pays gas)
        sponsor_group = QGroupBox("Sponsor (pays gas)")
        sponsor_layout = QFormLayout(sponsor_group)
        self.sponsor_combo = QComboBox()
        self.sponsor_combo.setFont(QFont(Theme.MONO_FONT, 9))
        sponsor_layout.addRow("Address:", self.sponsor_combo)
        self.sponsor_balance_label = QLabel("")
        self.sponsor_balance_label.setProperty("role", "muted")
        sponsor_layout.addRow("", self.sponsor_balance_label)
        layout.addWidget(sponsor_group)

        # Recipient
        recipient_group = QGroupBox("Recipient (receives all USDG)")
        recipient_layout = QVBoxLayout(recipient_group)

        self.use_internal_radio = QRadioButton("Use wallet address")
        self.use_internal_radio.setChecked(True)
        self.use_internal_radio.toggled.connect(self._on_recipient_mode_changed)
        recipient_layout.addWidget(self.use_internal_radio)

        self.recipient_combo = QComboBox()
        self.recipient_combo.setFont(QFont(Theme.MONO_FONT, 9))
        recipient_layout.addWidget(self.recipient_combo)

        self.use_external_radio = QRadioButton("Use external address")
        self.use_external_radio.toggled.connect(self._on_recipient_mode_changed)
        recipient_layout.addWidget(self.use_external_radio)

        self.external_input = QLineEdit()
        self.external_input.setPlaceholderText("0x...")
        self.external_input.setFont(QFont(Theme.MONO_FONT, 9))
        self.external_input.setEnabled(False)
        recipient_layout.addWidget(self.external_input)

        layout.addWidget(recipient_group)

        # Donors (sources)
        donor_group = QGroupBox("Donors (sweep FROM these addresses)")
        donor_layout = QVBoxLayout(donor_group)

        # Select all / deselect all
        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self._select_all_donors)
        btn_row.addWidget(select_all_btn)

        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(self._deselect_all_donors)
        btn_row.addWidget(deselect_all_btn)

        btn_row.addStretch()

        self.total_label = QLabel("Selected: 0.000000 USDG")
        self.total_label.setProperty("role", "total")
        btn_row.addWidget(self.total_label)

        donor_layout.addLayout(btn_row)

        # Donor list with checkboxes
        self.donor_list = QListWidget()
        self.donor_list.setFont(QFont(Theme.MONO_FONT, 9))
        self.donor_list.setMinimumHeight(150)
        self.donor_list.itemChanged.connect(self._on_donor_selection_changed)
        donor_layout.addWidget(self.donor_list)

        layout.addWidget(donor_group)

        # Status
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        # Progress (hidden by default)
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        layout.addWidget(self.progress_label)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.sweep_btn = QPushButton("Sweep USDG")
        self.sweep_btn.setDefault(True)
        self.sweep_btn.clicked.connect(self._execute_sweep)
        btn_layout.addWidget(self.sweep_btn)

        layout.addLayout(btn_layout)

    def _on_network_changed(self):
        """Handle network change."""
        self.chain_id = self.network_combo.currentData()
        self._refresh_balances()

    def _on_recipient_mode_changed(self):
        """Toggle between internal and external recipient."""
        use_internal = self.use_internal_radio.isChecked()
        self.recipient_combo.setEnabled(use_internal)
        self.external_input.setEnabled(not use_internal)

    def _refresh_balances(self):
        """Refresh all balance displays for current network."""
        from ..networks import NETWORKS, BalanceFetcher, TOKENS
        from web3 import Web3

        network = NETWORKS.get(self.chain_id)
        if not network:
            return

        # Clear and repopulate combos
        self.sponsor_combo.clear()
        self.recipient_combo.clear()
        self.donor_list.clear()

        self._balances = {}  # address -> (usdg_balance, native_balance)

        try:
            fetcher = BalanceFetcher(network)
            usdg_config = TOKENS.get("USDG")
            usdg_addr = usdg_config.addresses.get(self.chain_id) if usdg_config else None

            for addr_entry in self.addresses:
                address = addr_entry.address
                try:
                    balances = fetcher.get_all_balances(address)
                    native_bal = 0.0
                    usdg_bal = 0.0
                    usdg_raw = 0
                    native_failed = False
                    usdg_failed = False

                    for bal in balances:
                        if bal.symbol == network.native_symbol:
                            native_bal = bal.formatted
                            native_failed = bal.fetch_failed
                        elif bal.symbol == "USDG":
                            usdg_bal = bal.formatted
                            usdg_raw = bal.raw
                            usdg_failed = bal.fetch_failed

                    self._balances[address] = {
                        'native': native_bal,
                        'usdg': usdg_bal,
                        'usdg_raw': usdg_raw,
                        'entry': addr_entry,
                        'native_failed': native_failed,
                        'usdg_failed': usdg_failed,
                    }

                    short_addr = f"{address[:8]}...{address[-6:]}"
                    name = addr_entry.name if addr_entry.name else ""
                    name_display = f" [{name}]" if name else ""

                    # Sponsor combo - show name + native balance or "--" if failed
                    if native_failed:
                        sponsor_text = f"{short_addr}{name_display}  (-- {network.native_symbol})"
                    else:
                        sponsor_text = f"{short_addr}{name_display}  ({native_bal:.6f} {network.native_symbol})"
                    self.sponsor_combo.addItem(sponsor_text, address)

                    # Recipient combo - show name
                    self.recipient_combo.addItem(f"{short_addr}{name_display}", address)

                    # Donor list - show name + USDG balance or "--" if failed
                    if usdg_failed:
                        item = QListWidgetItem(f"{short_addr}{name_display}    -- USDG (RPC error)")
                        item.setForeground(Qt.GlobalColor.red)
                    else:
                        item = QListWidgetItem(f"{short_addr}{name_display}    {usdg_bal:.6f} USDG")
                        # Gray out if confirmed zero
                        if usdg_bal == 0:
                            item.setForeground(Qt.GlobalColor.gray)
                    item.setData(Qt.ItemDataRole.UserRole, address)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    # Auto-check if has USDG (and fetch succeeded)
                    item.setCheckState(Qt.CheckState.Checked if usdg_bal > 0 and not usdg_failed else Qt.CheckState.Unchecked)
                    self.donor_list.addItem(item)

                except Exception as e:
                    # Still add to combos even if balance fetch fails entirely
                    short_addr = f"{address[:8]}...{address[-6:]}"
                    name = addr_entry.name if addr_entry.name else ""
                    name_display = f" [{name}]" if name else ""
                    self.sponsor_combo.addItem(f"{short_addr}{name_display}  (-- RPC error)", address)
                    self.recipient_combo.addItem(f"{short_addr}{name_display}", address)
                    self._balances[address] = {
                        'native': 0.0,
                        'usdg': 0.0,
                        'usdg_raw': 0,
                        'entry': addr_entry,
                        'native_failed': True,
                        'usdg_failed': True,
                    }
                    # Add to donor list with error indicator
                    item = QListWidgetItem(f"{short_addr}{name_display}    -- USDG (RPC error)")
                    item.setData(Qt.ItemDataRole.UserRole, address)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setForeground(Qt.GlobalColor.red)
                    self.donor_list.addItem(item)

            self._update_totals()

        except Exception as e:
            # If fetcher fails entirely, still populate addresses without balances
            for addr_entry in self.addresses:
                address = addr_entry.address
                short_addr = f"{address[:8]}...{address[-6:]}"
                name = addr_entry.name if addr_entry.name else ""
                name_display = f" [{name}]" if name else ""
                self.sponsor_combo.addItem(f"{short_addr}{name_display}  (RPC error)", address)
                self.recipient_combo.addItem(f"{short_addr}{name_display}", address)
                self._balances[address] = {
                    'native': 0.0,
                    'usdg': 0.0,
                    'usdg_raw': 0,
                    'entry': addr_entry
                }
            FramelessMessageBox.warning(self, "Error", f"Failed to fetch balances: {e}")

    def _select_all_donors(self):
        """Select all donors with non-zero balance."""
        for i in range(self.donor_list.count()):
            item = self.donor_list.item(i)
            addr = item.data(Qt.ItemDataRole.UserRole)
            if addr in self._balances and self._balances[addr]['usdg'] > 0:
                item.setCheckState(Qt.CheckState.Checked)
        self._update_totals()

    def _deselect_all_donors(self):
        """Deselect all donors."""
        for i in range(self.donor_list.count()):
            item = self.donor_list.item(i)
            item.setCheckState(Qt.CheckState.Unchecked)
        self._update_totals()

    def _on_donor_selection_changed(self, item):
        """Handle donor checkbox change."""
        self._update_totals()

    def _update_totals(self):
        """Update the total selected amount."""
        total = 0.0
        count = 0
        for i in range(self.donor_list.count()):
            item = self.donor_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                addr = item.data(Qt.ItemDataRole.UserRole)
                if addr in self._balances:
                    total += self._balances[addr]['usdg']
                    count += 1

        self.total_label.setText(f"Selected: {count} addresses, {total:.6f} USDG")
        self.sweep_btn.setText(f"Sweep {total:.6f} USDG")
        self.sweep_btn.setEnabled(count > 0 and total > 0)

    def _get_selected_donors(self) -> list:
        """Get list of selected donor addresses with balances."""
        donors = []
        for i in range(self.donor_list.count()):
            item = self.donor_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                addr = item.data(Qt.ItemDataRole.UserRole)
                if addr in self._balances and self._balances[addr]['usdg_raw'] > 0:
                    donors.append({
                        'address': addr,
                        'entry': self._balances[addr]['entry'],
                        'usdg_raw': self._balances[addr]['usdg_raw'],
                        'usdg': self._balances[addr]['usdg']
                    })
        return donors

    def _execute_sweep(self):
        """Execute the sweep operation."""
        from ..networks import NETWORKS, TOKENS
        from web3 import Web3
        from eth_account import Account
        from ..services.eip3009 import sign_transfer_authorization
        import secrets
        import time

        # Get inputs
        sponsor_addr = self.sponsor_combo.currentData()
        if not sponsor_addr:
            FramelessMessageBox.warning(self, "Error", "Please select a sponsor address.")
            return

        if self.use_internal_radio.isChecked():
            recipient_addr = self.recipient_combo.currentData()
        else:
            recipient_addr = self.external_input.text().strip()

        if not recipient_addr or not recipient_addr.startswith("0x") or len(recipient_addr) != 42:
            FramelessMessageBox.warning(self, "Error", "Please enter a valid recipient address.")
            return

        donors = self._get_selected_donors()
        if not donors:
            FramelessMessageBox.warning(self, "Error", "Please select at least one donor address.")
            return

        # Confirm
        total_usdg = sum(d['usdg'] for d in donors)
        if not FramelessMessageBox.question(
            self,
            "Confirm Sweep",
            f"Sweep {total_usdg:.6f} USDG from {len(donors)} address(es)\n\n"
            f"To: {recipient_addr[:16]}...{recipient_addr[-8:]}\n"
            f"Gas paid by: {sponsor_addr[:16]}...{sponsor_addr[-8:]}\n\n"
            "This will execute multiple transactions. Continue?",
            default_no=True
        ):
            return

        # Disable UI
        self.sweep_btn.setEnabled(False)
        self.sweep_btn.setText("Sweeping...")
        self.status_label.setVisible(True)
        self.progress_label.setVisible(True)

        try:
            network = NETWORKS.get(self.chain_id)
            usdg_config = TOKENS.get("USDG")
            usdg_address = usdg_config.addresses.get(self.chain_id)

            if not network or not usdg_address:
                raise ValueError("Network or USDG not configured")

            # Connect to network
            w3 = Web3(Web3.HTTPProvider(network.rpc_url))
            if not w3.is_connected():
                raise ValueError(f"Could not connect to {network.display_name}")

            # Get sponsor private key
            sponsor_entry = self._balances[sponsor_addr]['entry']
            sponsor_pkey = self.get_private_key_fn(sponsor_entry.id)
            if not sponsor_pkey:
                raise ValueError("Could not get sponsor private key")
            if isinstance(sponsor_pkey, bytes):
                sponsor_pkey = "0x" + sponsor_pkey.hex()

            sponsor_account = Account.from_key(sponsor_pkey)

            # USDG contract ABI for transferWithAuthorization
            twa_abi = [{
                "inputs": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                    {"name": "v", "type": "uint8"},
                    {"name": "r", "type": "bytes32"},
                    {"name": "s", "type": "bytes32"}
                ],
                "name": "transferWithAuthorization",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            }]

            usdg_contract = w3.eth.contract(
                address=Web3.to_checksum_address(usdg_address),
                abi=twa_abi
            )

            # Process each donor
            successful = 0
            failed = 0
            tx_hashes = []

            for idx, donor in enumerate(donors):
                self.progress_label.setText(f"Processing {idx + 1}/{len(donors)}: {donor['address'][:10]}...")
                QApplication.processEvents()

                try:
                    # Get donor private key
                    donor_pkey = self.get_private_key_fn(donor['entry'].id)
                    if not donor_pkey:
                        raise ValueError("Could not get donor private key")
                    if isinstance(donor_pkey, bytes):
                        donor_pkey = "0x" + donor_pkey.hex()

                    # Sign authorization
                    now = int(time.time())
                    valid_after = now - 60
                    valid_before = now + 3600
                    nonce_bytes = secrets.token_bytes(32)

                    signature = sign_transfer_authorization(
                        private_key=donor_pkey,
                        chain_id=self.chain_id,
                        token_address=usdg_address,
                        token_name="USD Coin",
                        token_version="2",
                        from_address=donor['address'],
                        to_address=recipient_addr,
                        value=donor['usdg_raw'],
                        valid_after=valid_after,
                        valid_before=valid_before,
                        nonce=nonce_bytes
                    )

                    # Parse signature into v, r, s
                    sig_bytes = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
                    r = sig_bytes[:32]
                    s = sig_bytes[32:64]
                    v = sig_bytes[64]

                    # Build transaction from sponsor
                    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(sponsor_addr))
                    gas_price = w3.eth.gas_price

                    tx = usdg_contract.functions.transferWithAuthorization(
                        Web3.to_checksum_address(donor['address']),
                        Web3.to_checksum_address(recipient_addr),
                        donor['usdg_raw'],
                        valid_after,
                        valid_before,
                        nonce_bytes,
                        v,
                        r,
                        s
                    ).build_transaction({
                        'from': Web3.to_checksum_address(sponsor_addr),
                        'nonce': nonce,
                        'gasPrice': gas_price,
                        'chainId': self.chain_id,
                    })

                    # Estimate gas
                    gas_estimate = w3.eth.estimate_gas(tx)
                    tx['gas'] = int(gas_estimate * 1.2)

                    # Sign and send
                    signed_tx = sponsor_account.sign_transaction(tx)
                    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    tx_hashes.append(tx_hash.hex())
                    successful += 1

                    self.status_label.setText(f"Sent: ${donor['usdg']:.6f} from {donor['address'][:10]}...")
                    self.status_label.setProperty("role", "accent")

                except Exception as e:
                    failed += 1
                    self.status_label.setText(f"Failed: {donor['address'][:10]}... - {e}")
                    self.status_label.setProperty("role", "error")

                QApplication.processEvents()

            # Summary
            self.progress_label.setText(f"Complete: {successful} successful, {failed} failed")

            if successful > 0:
                FramelessMessageBox.information(
                    self,
                    "Sweep Complete",
                    f"Successfully swept {successful} address(es).\n"
                    f"Failed: {failed}\n\n"
                    f"Transactions:\n" + "\n".join(tx_hashes[:5]) +
                    ("\n..." if len(tx_hashes) > 5 else "")
                )
                self.accept()
            else:
                FramelessMessageBox.warning(self, "Sweep Failed", "All transfers failed.")

        except Exception as e:
            self.status_label.setText(f"Error: {e}")
            self.status_label.setProperty("role", "error")
            FramelessMessageBox.critical(self, "Sweep Failed", str(e))

        finally:
            self.sweep_btn.setEnabled(True)
            self.sweep_btn.setText("Sweep USDG")


# ============================================
# Send Dialog
# ============================================

class _GasPriceFetcher(QThread):
    """Fetches the current network gas price in the background (best-effort)."""

    done = pyqtSignal(object)  # gas price in wei, or None on failure

    def __init__(self, rpc_url: str, parent=None):
        super().__init__(parent)
        self._rpc_url = rpc_url

    def run(self):
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(self._rpc_url, request_kwargs={"timeout": 5}))
            self.done.emit(int(w3.eth.gas_price))
        except Exception:
            self.done.emit(None)


class SendDialog(FramelessDialog):
    """
    Dialog for sending tokens from any address in the wallet.
    Supports pre-selection of From address and Asset, but all fields are editable.
    """

    def __init__(
        self,
        parent: QWidget,
        addresses: list,  # List of AddressEntry objects
        balances_by_address: dict,  # address -> list[Balance]
        get_private_key_fn,  # Function(address_id) -> bytes
        chain_id: int,
        preselect_from: str = None,  # Pre-select this From address
        preselect_asset: str = None,  # Pre-select this asset symbol
    ):
        super().__init__("Send", parent, width=480)
        self.setMinimumHeight(420)
        self.addresses = addresses
        self.balances_by_address = balances_by_address
        self.get_private_key_fn = get_private_key_fn
        self.chain_id = chain_id
        self.preselect_from = preselect_from
        self.preselect_asset = preselect_asset

        self._gas_price_wei = None
        self._gas_fetcher = None

        self._setup_ui()
        self._apply_preselections()
        self._start_gas_fetch()

    def _setup_ui(self):
        layout = self.content_layout
        layout.setSpacing(12)

        # From address
        from_group = QGroupBox("From")
        from_layout = QFormLayout(from_group)

        self.from_combo = QComboBox()
        self.from_combo.setFont(QFont(Theme.MONO_FONT, 9))
        for addr in self.addresses:
            display = f"{addr.name} ({addr.address[:8]}...{addr.address[-6:]})"
            self.from_combo.addItem(display, addr.address)
        self.from_combo.currentIndexChanged.connect(self._on_from_changed)
        from_layout.addRow("Address:", self.from_combo)

        self.from_balance_label = QLabel("")
        self.from_balance_label.setProperty("role", "muted")
        from_layout.addRow("", self.from_balance_label)

        layout.addWidget(from_group)

        # To address
        to_group = QGroupBox("To")
        to_layout = QFormLayout(to_group)

        self.to_input = QLineEdit()
        self.to_input.setPlaceholderText("0x...")
        self.to_input.setFont(QFont(Theme.MONO_FONT, 9))
        to_layout.addRow("Address:", self.to_input)

        layout.addWidget(to_group)

        # Asset selection
        asset_group = QGroupBox("Asset")
        asset_layout = QFormLayout(asset_group)

        self.asset_combo = QComboBox()
        self.asset_combo.currentIndexChanged.connect(self._on_asset_changed)
        asset_layout.addRow("Token:", self.asset_combo)

        self.asset_balance_label = QLabel("")
        self.asset_balance_label.setProperty("role", "muted")
        asset_layout.addRow("Available:", self.asset_balance_label)

        layout.addWidget(asset_group)

        # Amount
        amount_group = QGroupBox("Amount")
        amount_layout = QFormLayout(amount_group)

        self.amount_input = QDoubleSpinBox()
        self.amount_input.setDecimals(8)  # Support high precision for ETH
        self.amount_input.setMinimum(0.00000001)
        self.amount_input.setMaximum(999999999)
        self.amount_input.setValue(0)

        max_btn = QPushButton("Max")
        max_btn.setFixedWidth(64)
        max_btn.clicked.connect(self._set_max_amount)

        amount_row = QHBoxLayout()
        amount_row.setSpacing(8)
        amount_row.addWidget(self.amount_input, stretch=1)
        amount_row.addWidget(max_btn)
        amount_layout.addRow("Amount:", amount_row)

        layout.addWidget(amount_group)

        # Status area (hidden by default)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        # Buttons
        btn_layout = QHBoxLayout()

        # Gas estimate, inline at the left of the button row
        self.gas_label = QLabel("")
        self.gas_label.setProperty("role", "muted")
        btn_layout.addWidget(self.gas_label)
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.send_btn = QPushButton("Send")
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self._execute_send)
        btn_layout.addWidget(self.send_btn)

        layout.addLayout(btn_layout)

    def _apply_preselections(self):
        """Apply pre-selections for From and Asset."""
        # Pre-select From address
        if self.preselect_from:
            for i in range(self.from_combo.count()):
                if self.from_combo.itemData(i) == self.preselect_from:
                    self.from_combo.setCurrentIndex(i)
                    break

        # Populate the asset dropdown for the current From address.
        # setCurrentIndex(0) emits no currentIndexChanged, so populate explicitly
        # instead of relying on the signal.
        self._on_from_changed()

        # Pre-select Asset (after From is set)
        if self.preselect_asset:
            for i in range(self.asset_combo.count()):
                if self.asset_combo.itemText(i).startswith(self.preselect_asset):
                    self.asset_combo.setCurrentIndex(i)
                    break

    def _on_from_changed(self):
        """Handle From address change - update asset list."""
        from_addr = self.from_combo.currentData()
        if not from_addr:
            return

        # Update asset combo
        self.asset_combo.clear()
        balances = self.balances_by_address.get(from_addr, [])

        if not balances:
            self.asset_combo.addItem("No assets")
            self.from_balance_label.setText("Loading...")
            return

        # Populate asset dropdown
        for bal in balances:
            if bal.fetch_failed:
                display = f"{bal.symbol} (error)"
            else:
                display = f"{bal.symbol} ({bal.formatted:,.6f})"
            self.asset_combo.addItem(display, bal)

        # Update summary
        summaries = []
        for bal in balances:
            if not bal.fetch_failed and bal.formatted > 0:
                summaries.append(f"{bal.formatted:,.4f} {bal.symbol}")
        if summaries:
            self.from_balance_label.setText(", ".join(summaries))
        else:
            self.from_balance_label.setText("0")

    def _on_asset_changed(self):
        """Handle Asset selection change."""
        bal = self.asset_combo.currentData()
        if not bal:
            self.asset_balance_label.setText("")
            self.amount_input.setMaximum(0)
            return

        if bal.fetch_failed:
            self.asset_balance_label.setText("Error fetching balance")
            self.amount_input.setMaximum(0)
        else:
            self.asset_balance_label.setText(f"{bal.formatted:,.8f} {bal.symbol}")
            self.amount_input.setMaximum(bal.formatted)
            self.amount_input.setSuffix(f" {bal.symbol}")

            # Set decimals based on asset
            if bal.symbol in ("ETH",):
                self.amount_input.setDecimals(8)
            elif bal.symbol in ("USDG", "USDT"):
                self.amount_input.setDecimals(6)
            else:
                self.amount_input.setDecimals(min(bal.decimals, 8))

        self._update_gas_line()

    def _start_gas_fetch(self):
        """Kick off a background fetch of the current gas price."""
        from ..networks import NETWORKS
        network = NETWORKS.get(self.chain_id)
        if not network:
            return
        self._gas_fetcher = _GasPriceFetcher(network.rpc_url, self)
        self._gas_fetcher.done.connect(self._on_gas_price)
        self._gas_fetcher.start()

    def _on_gas_price(self, price):
        """Store the fetched gas price and refresh the estimate line."""
        self._gas_price_wei = price
        self._update_gas_line()

    def _update_gas_line(self):
        """Show an always-present network-fee estimate below the fields."""
        from ..networks import NETWORKS
        network = NETWORKS.get(self.chain_id)
        native = network.native_symbol if network else "ETH"

        bal = self.asset_combo.currentData()
        is_native = bool(bal) and bal.symbol == native
        # Rough transfer gas: native send vs ERC-20 transfer
        gas_limit = 21000 if is_native else 65000

        if self._gas_price_wei:
            fee = (self._gas_price_wei * gas_limit) / 1e18
            self.gas_label.setText(f"Est. network fee ~{fee:.6f} {native}")
        else:
            self.gas_label.setText(f"Network fee paid in {native}")

    def _set_max_amount(self):
        """Set amount to maximum available."""
        bal = self.asset_combo.currentData()
        if bal and not bal.fetch_failed:
            self.amount_input.setValue(bal.formatted)

    def _get_address_entry(self, address: str):
        """Get AddressEntry for an address."""
        for entry in self.addresses:
            if entry.address == address:
                return entry
        return None

    def _execute_send(self):
        """Execute the token transfer."""
        from_addr = self.from_combo.currentData()
        to_addr = self.to_input.text().strip()
        asset = self.asset_combo.currentData()
        amount = self.amount_input.value()

        # Validate
        if not from_addr:
            FramelessMessageBox.warning(self, "Invalid", "Please select a source address.")
            return

        if not to_addr or not to_addr.startswith("0x") or len(to_addr) != 42:
            FramelessMessageBox.warning(self, "Invalid Address", "Please enter a valid destination address (0x...).")
            return

        if not asset:
            FramelessMessageBox.warning(self, "Invalid", "Please select an asset to send.")
            return

        if asset.fetch_failed:
            FramelessMessageBox.warning(self, "Error", "Cannot send - balance fetch failed for this asset.")
            return

        if amount <= 0 or amount > asset.formatted:
            FramelessMessageBox.warning(self, "Invalid Amount", "Please enter a valid amount.")
            return

        # Get entry for private key
        entry = self._get_address_entry(from_addr)
        if not entry:
            FramelessMessageBox.warning(self, "Error", "Address not found in wallet.")
            return

        # Confirm
        from ..networks import NETWORKS
        network = NETWORKS.get(self.chain_id)
        network_name = network.display_name if network else f"Chain {self.chain_id}"

        if not FramelessMessageBox.question(
            self,
            "Confirm Send",
            f"Send {amount:,.8g} {asset.symbol} to:\n\n"
            f"{to_addr[:16]}...{to_addr[-8:]}\n\n"
            f"From: {entry.name}\n"
            f"Network: {network_name}\n\n"
            "Continue?",
            default_no=True
        ):
            return

        self.send_btn.setEnabled(False)
        self.send_btn.setText("Sending...")
        self.status_label.setText("Preparing transaction...")
        self.status_label.setVisible(True)
        QApplication.processEvents()

        try:
            from web3 import Web3
            from eth_account import Account
            from decimal import Decimal, ROUND_DOWN

            if not network:
                raise ValueError(f"Network not found: {self.chain_id}")

            # Connect to network
            w3 = Web3(Web3.HTTPProvider(network.rpc_url))
            if not w3.is_connected():
                raise ValueError(f"Could not connect to {network.display_name}")

            from_checksum = Web3.to_checksum_address(from_addr)
            to_checksum = Web3.to_checksum_address(to_addr)

            # Get private key
            pkey = self.get_private_key_fn(entry.id)
            if not pkey:
                raise ValueError("Could not retrieve private key")

            account = Account.from_key(pkey)

            if asset.symbol == network.native_symbol:
                # Native token transfer (ETH)
                self._send_native(w3, account, from_checksum, to_checksum, amount, asset, network)
            else:
                # ERC-20 token transfer
                self._send_erc20(w3, account, from_checksum, to_checksum, amount, asset, network)

        except Exception as e:
            self.status_label.setText(f"Error: {e}")
            self.status_label.setProperty("role", "error")
            FramelessMessageBox.critical(self, "Send Failed", str(e))

        finally:
            self.send_btn.setEnabled(True)
            self.send_btn.setText("Send")

    def _send_native(self, w3, account, from_addr, to_addr, amount, asset, network):
        """Send native token (ETH)."""
        from decimal import Decimal, ROUND_DOWN

        self.status_label.setText("Estimating gas...")
        QApplication.processEvents()

        # Convert amount to wei
        amount_decimal = Decimal(str(amount)).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        amount_wei = int(amount_decimal * (10 ** asset.decimals))

        nonce = w3.eth.get_transaction_count(from_addr)
        gas_price = w3.eth.gas_price

        tx = {
            'from': from_addr,
            'to': to_addr,
            'value': amount_wei,
            'nonce': nonce,
            'gasPrice': gas_price,
            'chainId': self.chain_id,
        }

        # Estimate gas
        try:
            gas_estimate = w3.eth.estimate_gas(tx)
            tx['gas'] = int(gas_estimate * 1.2)  # 20% buffer
        except Exception as e:
            raise ValueError(f"Gas estimation failed: {e}")

        self.status_label.setText("Signing transaction...")
        QApplication.processEvents()

        signed_tx = account.sign_transaction(tx)

        self.status_label.setText("Broadcasting transaction...")
        QApplication.processEvents()

        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        self.status_label.setText(f"Transaction sent: {tx_hash_hex[:16]}...")
        self.status_label.setProperty("role", "success")

        # Show success with explorer link
        explorer_url = f"{network.explorer_url}/tx/0x{tx_hash_hex}"
        FramelessMessageBox.information(
            self,
            "Transaction Sent",
            f"{asset.symbol} transfer submitted!\n\n"
            f"Amount: {amount:,.8g} {asset.symbol}\n"
            f"To: {to_addr[:16]}...{to_addr[-8:]}\n\n"
            f"Transaction: {tx_hash_hex[:16]}...\n\n"
            f"View on explorer:\n{explorer_url}"
        )

        self.accept()

    def _send_erc20(self, w3, account, from_addr, to_addr, amount, asset, network):
        """Send ERC-20 token."""
        from decimal import Decimal, ROUND_DOWN

        self.status_label.setText("Preparing token transfer...")
        QApplication.processEvents()

        # Get token address from Balance object (discovered via Blockscout)
        if not asset.token_address:
            raise ValueError(f"Token {asset.symbol} has no contract address")

        token_address = asset.token_address

        # Convert amount to raw units
        amount_decimal = Decimal(str(amount)).quantize(
            Decimal(10) ** (-asset.decimals),
            rounding=ROUND_DOWN
        )
        amount_raw = int(amount_decimal * (10 ** asset.decimals))

        # ERC-20 transfer ABI
        erc20_abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]

        contract = w3.eth.contract(
            address=w3.to_checksum_address(token_address),
            abi=erc20_abi
        )

        self.status_label.setText("Estimating gas...")
        QApplication.processEvents()

        nonce = w3.eth.get_transaction_count(from_addr)
        gas_price = w3.eth.gas_price

        tx = contract.functions.transfer(to_addr, amount_raw).build_transaction({
            'from': from_addr,
            'nonce': nonce,
            'gasPrice': gas_price,
            'chainId': self.chain_id,
        })

        # Estimate gas
        try:
            gas_estimate = w3.eth.estimate_gas(tx)
            tx['gas'] = int(gas_estimate * 1.2)  # 20% buffer
        except Exception as e:
            raise ValueError(f"Gas estimation failed: {e}")

        self.status_label.setText("Signing transaction...")
        QApplication.processEvents()

        signed_tx = account.sign_transaction(tx)

        self.status_label.setText("Broadcasting transaction...")
        QApplication.processEvents()

        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        self.status_label.setText(f"Transaction sent: {tx_hash_hex[:16]}...")
        self.status_label.setProperty("role", "success")

        # Show success with explorer link
        explorer_url = f"{network.explorer_url}/tx/0x{tx_hash_hex}"
        FramelessMessageBox.information(
            self,
            "Transaction Sent",
            f"{asset.symbol} transfer submitted!\n\n"
            f"Amount: {amount:,.8g} {asset.symbol}\n"
            f"To: {to_addr[:16]}...{to_addr[-8:]}\n\n"
            f"Transaction: {tx_hash_hex[:16]}...\n\n"
            f"View on explorer:\n{explorer_url}"
        )

        self.accept()


class AddressDetailsDialog(FramelessDialog):
    """
    Dialog showing address details with rename and delete options.

    Opened by double-clicking an address row in the wallet table.
    """

    def __init__(self, entry, wallet, on_rename, on_delete, parent=None):
        """
        Args:
            entry: AddressEntry object
            wallet: VaultWallet for context
            on_rename: Callback(new_name) for rename action
            on_delete: Callback() for delete action
            parent: Parent widget
        """
        super().__init__("Address Details", parent)
        self.entry = entry
        self.wallet = wallet
        self.on_rename = on_rename
        self.on_delete = on_delete

        self.setMinimumWidth(400)

        layout = self.content_layout
        layout.setSpacing(12)

        # === INFO SECTION ===
        info_group = QGroupBox("Address Info")
        info_layout = QFormLayout(info_group)

        # Name (editable)
        self.name_input = QLineEdit(entry.name)
        self.name_input.setMaxLength(50)
        info_layout.addRow("Name:", self.name_input)

        # Full address
        addr_label = QLabel(entry.address)
        addr_label.setFont(QFont(Theme.MONO_FONT, 9))
        addr_label.setWordWrap(True)
        addr_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addRow("Address:", addr_label)

        # Source info
        if entry.seed_id:
            source_text = f"Derived from {entry.seed_id}, index #{entry.index}"
        else:
            source_text = "Imported private key"
        source_label = QLabel(source_text)
        source_label.setProperty("role", "muted")
        info_layout.addRow("Source:", source_label)

        layout.addWidget(info_group)

        # === ACTIONS ===
        actions_layout = QHBoxLayout()

        # Copy address button
        copy_btn = QPushButton("Copy Address")
        copy_btn.clicked.connect(self._copy_address)
        actions_layout.addWidget(copy_btn)

        actions_layout.addStretch()

        # Delete button (danger)
        delete_btn = QPushButton("Delete")
        delete_btn.setProperty("variant", "danger")
        delete_btn.clicked.connect(self._on_delete_clicked)
        actions_layout.addWidget(delete_btn)

        layout.addLayout(actions_layout)

        # === BUTTONS ===
        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        save_btn = QPushButton("Save")
        save_btn.setProperty("variant", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _copy_address(self):
        """Copy address to clipboard."""
        QApplication.clipboard().setText(self.entry.address)

    def _on_save(self):
        """Save name change if modified."""
        new_name = self.name_input.text().strip()
        if new_name and new_name != self.entry.name:
            self.on_rename(new_name)
        self.accept()

    def _on_delete_clicked(self):
        """Handle delete button - confirm and delete."""
        from .theme import FramelessMessageBox
        from ..networks import format_address

        warning_msg = (
            f"Delete address '{self.entry.name}'?\n\n"
            f"Address: {format_address(self.entry.address)}\n\n"
        )

        if self.entry.seed_id:
            warning_msg += "This address can be re-derived from the seed later."
        else:
            warning_msg += "WARNING: This is an imported private key.\nMake sure you have a backup!"

        if FramelessMessageBox.question(self, "Delete Address", warning_msg, default_no=True):
            self.on_delete()
            self.accept()
