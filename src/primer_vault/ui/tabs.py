"""
UI Tabs - Main application tabs.

Contains:
- PoliciesTab: Manage spend policies
- AgentsTab: Manage registered agents
- HistoryTab: Transaction history
- WalletTab: Multi-wallet management
- SettingsTab: Advanced application settings
- LogTab: Real-time logs
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QCheckBox, QSpinBox, QLineEdit, QTextEdit, QGroupBox,
    QFormLayout, QFileDialog, QDialog, QFrame, QSplitter
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QByteArray
from PyQt6.QtGui import QFont, QColor, QPixmap, QIcon
from typing import Optional
import csv
import html as _html
import logging

logger = logging.getLogger(__name__)

from .theme import (
    Theme, ask_question, FramelessDialog, FramelessMessageBox,
    active, status_color, CONSOLE,
)
from .dialogs import (
    AgentRegistrationDialog, CommissionDialog, EditAgentDialog,
    NewPolicyDialog
)
from ..models import SpendPolicy, Agent, Transaction
from ..core.settings import DEFAULT_PORT
from ..wallet import VaultWallet, AddressEntry, NO_PASSWORD_SENTINEL
from ..wallet.dialogs import (
    AddAddressDialog, SeedSelectionDialog, DerivationBrowserDialog,
    NewSeedDialog, ImportSeedToWalletDialog, ImportPrivateKeyToWalletDialog,
    VaultWalletUnlockDialog, CreateWalletWizard,
    AddWalletChoiceDialog, WalletFilenameDialog, WalletSettingsDialog,
)
from ..networks import NETWORKS, DEFAULT_NETWORK, MultiNetworkBalanceFetcher, format_address, Balance
from ..version import __version__
# Note: Do NOT import get_wallet_dir or get_app_dir here. Use core methods instead.
# See ARCHITECTURE.md "Common Mistakes" section.

# Vault wallet filename
PRIMER_VAULT_WALLET_FILE = "primer_vault.wallet"


# ============================================
# Balance Fetcher Thread
# ============================================

class BalanceFetcherThread(QThread):
    """Background thread for fetching balances."""

    balances_updated = pyqtSignal(dict)  # chain_id -> list[Balance]

    def __init__(self, address: str, custom_rpcs: Optional[dict[int, str]] = None):
        super().__init__()
        self.address = address
        self.custom_rpcs = custom_rpcs

    def run(self):
        import logging
        logger = logging.getLogger(__name__)
        try:
            fetcher = MultiNetworkBalanceFetcher(custom_rpcs=self.custom_rpcs)
            balances = fetcher.get_all_balances(self.address)
            self.balances_updated.emit(balances)
        except Exception as e:
            logger.warning(f"Balance fetch error: {e}")
            self.balances_updated.emit({})


class IconFetcherThread(QThread):
    """Background thread for fetching token icons from URLs."""

    icon_fetched = pyqtSignal(str, bytes)  # (url, image_data)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        import logging
        import urllib.request
        import urllib.error
        logger = logging.getLogger(__name__)
        try:
            req = urllib.request.Request(
                self.url,
                headers={"User-Agent": "PrimerVault/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = response.read()
                self.icon_fetched.emit(self.url, data)
        except Exception as e:
            logger.debug(f"Icon fetch error for {self.url}: {e}")
            # Emit empty data on failure
            self.icon_fetched.emit(self.url, b"")


# ============================================
# Policies Tab
# ============================================

class PoliciesTab(QWidget):
    """Tab for managing spend policies."""

    policy_changed = pyqtSignal()
    policy_deleted = pyqtSignal(str, list)  # policy_id, list of decommissioned agent names
    activity = pyqtSignal(str, bool)  # message, is_error

    def __init__(self, core):
        """
        Args:
            core: Vault core instance
        """
        super().__init__()
        self.core = core

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()

        add_btn = QPushButton("+ New Policy")
        add_btn.clicked.connect(self.add_policy)
        toolbar.addWidget(add_btn)

        toolbar.addStretch()

        layout.addLayout(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "Name", "Trading", "x402", "Auto-Approve", "Linked to", "Actions"
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 60)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 70)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(self.on_row_double_clicked)

        self.populate_table()
        layout.addWidget(self.table)

        help_text = QLabel(
            "Spend policies control how much agents can spend. "
            "Double-click to edit."
        )
        help_text.setWordWrap(True)
        help_text.setProperty("role", "muted")
        layout.addWidget(help_text)

    def populate_table(self):
        """Refresh the table with current policies."""
        policies = self.core.get_all_policies()
        agents = self.core.get_all_agents()
        self.table.setRowCount(len(policies))

        for row, policy in enumerate(policies):
            # Name
            self.table.setItem(row, 0, QTableWidgetItem(policy.name))

            # Trading: tick or cross
            trading_item = QTableWidgetItem("\u2713" if policy.is_trading_enabled() else "\u2717")
            trading_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, trading_item)

            # x402: tick or cross
            x402_item = QTableWidgetItem("\u2713" if policy.x402_enabled else "\u2717")
            x402_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 2, x402_item)

            # Auto-Approve: trading / x402 / both / off
            has_x402_auto = policy.x402_enabled and policy.auto_approve_below_micro is not None
            has_trading_auto = (
                policy.trading_rules is not None
                and policy.trading_rules.enabled
                and policy.trading_rules.auto_approve_below_usd is not None
            )
            if has_x402_auto and has_trading_auto:
                auto_text = "both"
            elif has_trading_auto:
                auto_text = "trading"
            elif has_x402_auto:
                auto_text = "x402"
            else:
                auto_text = "off"
            self.table.setItem(row, 3, QTableWidgetItem(auto_text))

            # Linked to: count of agents using this policy
            agent_count = sum(1 for a in agents if a.policy_id == policy.id)
            if agent_count == 1:
                linked_text = "1 agent"
            else:
                linked_text = f"{agent_count} agents"
            self.table.setItem(row, 4, QTableWidgetItem(linked_text))

            # Actions: delete button
            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(4, 0, 4, 0)
            actions_layout.setSpacing(0)

            delete_btn = QPushButton("X")
            delete_btn.setFixedSize(28, 24)
            delete_btn.setProperty("compact", "true")
            delete_btn.setToolTip("Delete policy")
            delete_btn.setProperty("variant", "danger")
            delete_btn.clicked.connect(lambda checked, p=policy: self.delete_policy(p))
            actions_layout.addWidget(delete_btn)
            actions_layout.addStretch()

            self.table.setCellWidget(row, 5, actions_widget)

    def add_policy(self):
        """Show dialog to create a new policy."""
        dialog = NewPolicyDialog(self, core=self.core)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            policy_data = dialog.get_policy_data()
            policy = self.core.create_policy(**policy_data)
            self.populate_table()
            self.policy_changed.emit()
            self.activity.emit(f"Policy '{policy.name}' created", False)

    def edit_policy(self, policy: SpendPolicy):
        """Show dialog to edit an existing policy."""
        dialog = NewPolicyDialog(self, policy, core=self.core)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            policy_data = dialog.get_policy_data()
            # Update policy fields from dialog data
            policy.name = policy_data["name"]
            policy.x402_enabled = policy_data["x402_enabled"]
            policy.daily_limit_micro = policy_data["daily_limit_micro"]
            policy.per_request_max_micro = policy_data["per_request_max_micro"]
            policy.auto_approve_below_micro = policy_data["auto_approve_below_micro"]
            policy.allowed_domains = policy_data.get("allowed_domains") or []
            policy.blocked_domains = policy_data.get("blocked_domains") or []
            policy.networks = policy_data.get("networks") or []
            policy.trading_rules = policy_data.get("trading_rules")
            self.core.update_policy(policy)
            self.populate_table()
            self.policy_changed.emit()
            self.activity.emit(f"Policy '{policy.name}' updated", False)

    def delete_policy(self, policy: SpendPolicy):
        """Delete a policy after confirmation."""
        # Check if any agents use this policy
        agents_using = [a.name for a in self.core.get_all_agents() if a.policy_id == policy.id]

        if agents_using:
            message = (
                f"Delete policy '{policy.name}'?\n\n"
                f"This will decommission {len(agents_using)} agent(s):\n"
                f"{', '.join(agents_using[:5])}"
            )
            if len(agents_using) > 5:
                message += f" (+{len(agents_using) - 5} more)"
        else:
            message = f"Delete policy '{policy.name}'?\n\nThis cannot be undone."

        if ask_question(self, "Delete Policy", message):
            decommissioned = self.core.delete_policy(policy.id)
            self.populate_table()
            self.policy_changed.emit()
            self.policy_deleted.emit(policy.id, decommissioned)

    def on_row_double_clicked(self, index):
        """Handle double-click on a table row to edit the policy."""
        row = index.row()
        policies = self.core.get_all_policies()
        if 0 <= row < len(policies):
            self.edit_policy(policies[row])




# ============================================
# Agents Tab
# ============================================

class AgentsTab(QWidget):
    """Tab showing registered agents."""

    agent_changed = pyqtSignal()
    activity = pyqtSignal(str, bool)  # message, is_error

    # Agent lifecycle status -> palette token.
    _AGENT_STATUS_TOKEN = {
        "active": "accent_dim",
        "suspended": "error",
        "limit_reached": "warn",
    }

    def __init__(self, core):
        """
        Args:
            core: Vault core instance
        """
        super().__init__()
        self.core = core
        self.wallets: list[WalletInfo] = []
        self.get_wallet_fn = None  # Set by MainWindow for wallet access

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()

        add_btn = QPushButton("+ Register Agent")
        add_btn.clicked.connect(self.register_agent)
        toolbar.addWidget(add_btn)

        toolbar.addStretch()

        layout.addLayout(toolbar)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "Name", "Code", "Verify Key", "Policy", "Spent Today", "Status", "Actions"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(4, 100)  # Spent Today
        self.table.setColumnWidth(5, 90)   # Status
        self.table.setColumnWidth(6, 95)   # Actions
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(self.on_row_double_clicked)

        self.populate_table()
        layout.addWidget(self.table)

        help_text = QLabel(
            "Register agents to generate tokens. Commission them with a spend policy to enable signing. "
            "Double-click to edit."
        )
        help_text.setWordWrap(True)
        help_text.setProperty("role", "muted")
        layout.addWidget(help_text)

    def refresh_theme(self):
        """Repaint palette-driven status colors after a theme switch."""
        self.populate_table()

    def populate_table(self):
        """Refresh the table with current agents."""
        agents = self.core.get_all_agents()
        self.table.setRowCount(len(agents))

        for row, agent in enumerate(agents):
            self.table.setItem(row, 0, QTableWidgetItem(agent.name))

            id_item = QTableWidgetItem(agent.id)
            id_item.setFont(QFont(Theme.MONO_FONT, 9))
            self.table.setItem(row, 1, id_item)

            pubkey_short = agent.auth_key[:8] + "..." + agent.auth_key[-6:]
            pubkey_item = QTableWidgetItem(pubkey_short)
            pubkey_item.setFont(QFont(Theme.MONO_FONT, 9))
            self.table.setItem(row, 2, pubkey_item)

            # Clear any stale cell widget from a previous populate — otherwise an
            # agent that goes unassigned -> commissioned shows both the policy name
            # and the leftover "(assign policy)" link stacked in the same cell.
            self.table.removeCellWidget(row, 3)
            if agent.policy_id:
                policy = self.core.get_policy(agent.policy_id)
                policy_name = policy.name if policy else "Unknown"
                self.table.setItem(row, 3, QTableWidgetItem(policy_name))
            else:
                # Clickable "(assign policy)" for uncommissioned agents
                self.table.setItem(row, 3, QTableWidgetItem(""))  # clear stale text under the widget
                policy_link = QLabel('<a href="#" style="color: {}; font-style: italic;">(assign policy)</a>'.format(active()["accent_dim"]))
                policy_link.setOpenExternalLinks(False)
                policy_link.linkActivated.connect(lambda _, a=agent: self.commission_agent(a))
                policy_link.setCursor(Qt.CursorShape.PointingHandCursor)
                self.table.setCellWidget(row, 3, policy_link)

            self.table.setItem(row, 4, QTableWidgetItem(agent.format_spent_today()))

            # Display status - use "pending" for uncommissioned
            display_status = "pending" if agent.status == "uncommissioned" else agent.status
            status_item = QTableWidgetItem(display_status)
            status_item.setForeground(QColor(active()[self._AGENT_STATUS_TOKEN.get(agent.status, "muted")]))
            self.table.setItem(row, 5, status_item)

            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(4, 0, 4, 0)
            actions_layout.setSpacing(4)

            # Primary action button: Commission / Activate / Suspend
            primary_btn = QPushButton()
            primary_btn.setFixedSize(28, 24)
            primary_btn.setProperty("compact", "true")

            if agent.status == "uncommissioned":
                primary_btn.setText("C")
                primary_btn.setToolTip("Commission")
                primary_btn.setProperty("variant", "primary")
                primary_btn.clicked.connect(lambda checked, a=agent: self.commission_agent(a))
            elif agent.status == "suspended":
                primary_btn.setText("A")
                primary_btn.setToolTip("Activate")
                primary_btn.setProperty("variant", "primary")
                primary_btn.clicked.connect(lambda checked, a=agent: self.activate_agent(a))
            elif agent.status == "active":
                primary_btn.setText("S")
                primary_btn.setToolTip("Suspend")
                primary_btn.setProperty("variant", "danger")
                primary_btn.clicked.connect(lambda checked, a=agent: self.suspend_agent(a))
            else:
                # limit_reached or other states - show suspend option
                primary_btn.setText("S")
                primary_btn.setToolTip("Suspend")
                primary_btn.setProperty("variant", "danger")
                primary_btn.clicked.connect(lambda checked, a=agent: self.suspend_agent(a))

            actions_layout.addWidget(primary_btn)

            # Delete button - always available
            delete_btn = QPushButton("X")
            delete_btn.setFixedSize(28, 24)
            delete_btn.setProperty("compact", "true")
            delete_btn.setToolTip("Delete")
            delete_btn.setProperty("variant", "danger")
            delete_btn.clicked.connect(lambda checked, a=agent: self.delete_agent(a))
            actions_layout.addWidget(delete_btn)

            actions_layout.addStretch()
            self.table.setCellWidget(row, 6, actions_widget)

    def register_agent(self):
        """Show dialog to register a new agent."""
        # Check if wallet is unlocked (core needs it for agent secret encryption)
        if not self.core.is_wallet_unlocked():
            FramelessMessageBox.warning(
                self,
                "Wallet Required",
                "Please unlock a wallet first before registering an agent.\n\n"
                "Agent credentials are encrypted with your wallet password for security."
            )
            return

        dialog = AgentRegistrationDialog(self.core, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            agent = dialog.get_agent()
            # Agent already created via core.create_agent() in dialog
            self.populate_table()
            self.agent_changed.emit()
            self.activity.emit(f"Agent '{agent.name}' registered (ID: {agent.id})", False)

    def commission_agent(self, agent: Agent):
        """Show dialog to commission an agent with a policy."""
        dialog = CommissionDialog(agent, self.core, self.wallets, self.get_wallet_fn, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Core already updated the agent - just refresh display
            self.populate_table()
            self.agent_changed.emit()

            # Get fresh agent data from core for accurate logging
            updated_agent = self.core.get_agent_by_code(agent.code)
            policy = self.core.get_policy(updated_agent.policy_id) if updated_agent and updated_agent.policy_id else None
            policy_name = policy.name if policy else "Unknown"
            self.activity.emit(f"Agent '{agent.name}' commissioned with policy '{policy_name}'", False)

            # Log Intent Mandate generation
            mandate = dialog.get_intent_mandate()
            if mandate:
                mandate_id = mandate.get('id', 'unknown')[:8]
                self.activity.emit(f"Intent Mandate generated for '{agent.name}' (ID: {mandate_id}...)", False)
                # Log registry upload if it happened
                if mandate.get('registryUrl'):
                    self.activity.emit(f"Mandate published to: {mandate.get('registryUrl')}", False)

    def suspend_agent(self, agent: Agent):
        """Suspend an active agent."""
        if ask_question(
            self,
            "Suspend Agent",
            f"Suspend agent '{agent.name}'?\n\nThis will reject all signing requests from this agent."
        ):
            self.core.suspend_agent(agent.code)
            self.populate_table()
            self.agent_changed.emit()
            self.activity.emit(f"Agent '{agent.name}' suspended", False)

    def activate_agent(self, agent: Agent):
        """Reactivate a suspended agent."""
        self.core.activate_agent(agent.code)
        self.populate_table()
        self.agent_changed.emit()
        self.activity.emit(f"Agent '{agent.name}' activated", False)

    def delete_agent(self, agent: Agent):
        """Delete an agent."""
        if ask_question(
            self,
            "Delete Agent",
            f"Delete agent '{agent.name}'?\n\nThis cannot be undone."
        ):
            self.core.delete_agent(agent.code)
            self.populate_table()
            self.agent_changed.emit()
            self.activity.emit(f"Agent '{agent.name}' deleted", False)

    def on_row_double_clicked(self, index):
        """Handle double-click on a table row to edit the agent."""
        row = index.row()
        agents = self.core.get_all_agents()
        if 0 <= row < len(agents):
            self.edit_agent(agents[row])

    def edit_agent(self, agent: Agent):
        """Show dialog to edit an agent's policy and wallet."""
        old_policy_id = agent.policy_id
        old_wallet = agent.wallet_address
        old_status = agent.status
        old_mandate = agent.intent_mandate

        dialog = EditAgentDialog(agent, self.core, self.wallets, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        mandate_revoked = dialog.was_mandate_revoked()

        # If mandate was revoked, always save even if dialog was cancelled
        if accepted or mandate_revoked:
            self.core.update_agent(agent)
            self.populate_table()
            self.agent_changed.emit()

            # Log what changed
            changes = []
            if agent.policy_id != old_policy_id:
                if agent.policy_id:
                    policy = self.core.get_policy(agent.policy_id)
                    changes.append(f"policy → {policy.name if policy else 'Unknown'}")
                else:
                    changes.append("policy removed")
            if agent.wallet_address != old_wallet:
                if agent.wallet_address:
                    changes.append(f"wallet → {agent.wallet_address[:10]}...")
                else:
                    changes.append("wallet removed")
            if agent.status != old_status:
                changes.append(f"status → {agent.status}")
            if mandate_revoked and old_mandate is not None:
                changes.append("mandate revoked")

            if changes:
                self.activity.emit(f"Agent '{agent.name}' updated: {', '.join(changes)}", False)


# ============================================
# History Tab
# ============================================

class HistoryTab(QWidget):
    """Tab showing transaction history."""

    def __init__(self, core):
        """
        Args:
            core: Vault core instance
        """
        super().__init__()
        self.core = core
        self._transactions: list[Transaction] = []
        self._wallet_names: dict[str, str] = {}  # address -> name

        layout = QVBoxLayout(self)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Filter:"))

        self.agent_filter = QComboBox()
        self.agent_filter.addItem("All Agents", None)
        self.agent_filter.currentIndexChanged.connect(self.apply_filters)
        filters.addWidget(self.agent_filter)

        self.type_filter = QComboBox()
        self.type_filter.addItems(["All Types", "x402", "Trade", "Send"])
        self.type_filter.currentIndexChanged.connect(self.apply_filters)
        filters.addWidget(self.type_filter)

        self.status_filter = QComboBox()
        self.status_filter.addItems(["All Status", "Signed", "Settled", "Rejected", "Pending"])
        self.status_filter.currentIndexChanged.connect(self.apply_filters)
        filters.addWidget(self.status_filter)

        filters.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        filters.addWidget(refresh_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_history)
        filters.addWidget(clear_btn)

        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self.export_csv)
        filters.addWidget(export_btn)

        layout.addLayout(filters)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        # Column order: Time, Type, Out, In, Status, Agent, Address
        self.table.setHorizontalHeaderLabels(["Time", "Type", "Out", "In", "Status", "Agent", "Address"])
        # Fixed widths for most columns, Address stretches to fill
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)  # Time
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)  # Type
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)  # Out
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # In
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)  # Status
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)  # Agent
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)  # Addr - flex
        self.table.setColumnWidth(0, 100)   # Time
        self.table.setColumnWidth(1, 60)    # Type
        self.table.setColumnWidth(2, 120)   # Out
        self.table.setColumnWidth(3, 120)   # In
        self.table.setColumnWidth(4, 80)    # Status
        self.table.setColumnWidth(5, 140)   # Agent
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(self.on_row_double_clicked)

        layout.addWidget(self.table)

        self.empty_label = QLabel("No transactions yet")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setProperty("role", "muted")
        layout.addWidget(self.empty_label)

        # Auto-refresh timer (every 5 seconds)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(5000)

        self.refresh()

    def refresh(self):
        """Reload transactions from core."""
        self._transactions = self.core.get_recent_transactions(limit=1000)
        # Build wallet address -> name mapping
        self._wallet_names = {}
        for addr in self.core.get_wallet_addresses():
            self._wallet_names[addr["address"].lower()] = addr["name"]
        self.update_agent_filter()
        self.apply_filters()

    def refresh_theme(self):
        """Repaint palette-driven status colors after a theme switch."""
        self.apply_filters()

    def clear_history(self):
        """Clear all transaction history after confirmation."""
        count = len(self._transactions)
        if count == 0:
            return

        if FramelessMessageBox.question(
            self,
            "Clear History",
            f"Delete all {count} transaction(s) from history?\n\nThis cannot be undone.",
            default_no=True
        ):
            self.core.clear_transactions()
            self.refresh()

    def update_agent_filter(self):
        """Update agent filter dropdown with current agents."""
        current = self.agent_filter.currentData()
        self.agent_filter.blockSignals(True)
        self.agent_filter.clear()
        self.agent_filter.addItem("All Agents", None)

        agent_names = sorted(set(tx.agent_name for tx in self._transactions))
        for name in agent_names:
            self.agent_filter.addItem(name, name)

        if current:
            idx = self.agent_filter.findData(current)
            if idx >= 0:
                self.agent_filter.setCurrentIndex(idx)

        self.agent_filter.blockSignals(False)

    def apply_filters(self):
        """Apply current filters and update table."""
        agent_name = self.agent_filter.currentData()
        type_text = self.type_filter.currentText().lower()
        status_text = self.status_filter.currentText().lower()

        filtered = self._transactions

        if agent_name:
            filtered = [tx for tx in filtered if tx.agent_name == agent_name]

        # Filter by type
        if type_text == "x402":
            filtered = [tx for tx in filtered if getattr(tx, 'type', 'x402') == 'x402']
        elif type_text == "trade":
            filtered = [tx for tx in filtered if getattr(tx, 'type', 'x402') == 'trade']
        elif type_text == "send":
            filtered = [tx for tx in filtered if getattr(tx, 'type', 'x402') == 'transfer']
        # "all types" shows everything

        if status_text == "signed":
            filtered = [tx for tx in filtered if tx.status in ("signed", "submitted")]
        elif status_text == "settled":
            filtered = [tx for tx in filtered if tx.status == "settled"]
        elif status_text == "rejected":
            filtered = [tx for tx in filtered if tx.status in ("rejected", "failed")]
        elif status_text == "pending":
            filtered = [tx for tx in filtered if tx.status in ("received", "signed", "submitted")]
        # "all status" shows everything

        self.populate_table(filtered)

    def populate_table(self, transactions: list[Transaction]):
        """Populate table with transactions."""
        self.table.setRowCount(len(transactions))

        self.empty_label.setVisible(len(transactions) == 0)
        self.table.setVisible(len(transactions) > 0)

        for row, tx in enumerate(transactions):
            tx_type = getattr(tx, 'type', 'x402')

            # Column order: Time(0), Type(1), Out(2), In(3), Status(4), Agent(5), Addr(6)

            # Store transaction ID in the first column for double-click lookup
            time_item = QTableWidgetItem(tx.format_time())
            time_item.setData(Qt.ItemDataRole.UserRole, tx.id)
            self.table.setItem(row, 0, time_item)

            # Type label
            type_labels = {'x402': 'x402', 'trade': 'Trade', 'transfer': 'Send'}
            type_item = QTableWidgetItem(type_labels.get(tx_type, tx_type))
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, type_item)

            # "Out" column - what was spent
            if tx_type == 'trade':
                sym_in = getattr(tx, 'symbol_in', '?')
                amt_in = getattr(tx, 'amount_in', '?')
                out_text = f"{amt_in} {sym_in}"
            elif tx_type == 'transfer':
                sym = getattr(tx, 'transfer_symbol', 'ETH')
                amt = getattr(tx, 'transfer_amount', '?')
                out_text = f"{amt} {sym}"
            else:
                out_text = tx.format_amount()
            self.table.setItem(row, 2, QTableWidgetItem(out_text))

            # "In" column - buy token for settled trades, n/a for others
            if tx_type == 'trade':
                if tx.status == 'rejected':
                    in_text = "n/a"
                else:
                    sym_out = getattr(tx, 'symbol_out', '?')
                    amt_out = getattr(tx, 'amount_out', None)
                    in_text = f"{amt_out} {sym_out}" if amt_out else sym_out
            else:
                in_text = "n/a"
            self.table.setItem(row, 3, QTableWidgetItem(in_text))

            # Status with color coding and verification indicator
            status_text = tx.status.upper()
            color = status_color(tx.status)

            # Override color and text for settled transactions based on verification
            if tx.status == "settled":
                p = active()
                if tx.verification_status == "verified":
                    status_text = "SETTLED ✓"
                    color = p["success"]
                elif tx.verification_status == "not_found":
                    status_text = "SETTLED ✗"
                    color = p["error"]
                elif tx.verification_status == "failed":
                    status_text = "SETTLED ✗"
                    color = p["error"]
                elif tx.verification_status == "pending":
                    status_text = "SETTLED"
                    color = p["pending"]
                # else: no verification_status = green SETTLED (default, presumed ok)

            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(QColor(color))
            self.table.setItem(row, 4, status_item)

            # Agent name with ID
            agent_text = f"{tx.agent_name} ({tx.agent_id})"
            self.table.setItem(row, 5, QTableWidgetItem(agent_text))

            # Wallet name (looked up from address)
            wallet_text = ""
            if tx.wallet_address:
                wallet_text = self._wallet_names.get(tx.wallet_address.lower(), tx.wallet_id or "")
            wallet_item = QTableWidgetItem(wallet_text)
            wallet_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 6, wallet_item)

    def on_row_double_clicked(self, index):
        """Handle double-click on a row to show transaction details."""
        row = index.row()
        time_item = self.table.item(row, 0)
        if time_item:
            tx_id = time_item.data(Qt.ItemDataRole.UserRole)
            if tx_id:
                tx = self.core.get_transaction(tx_id)
                if tx:
                    self.show_transaction_detail(tx)

    def show_transaction_detail(self, tx: Transaction):
        """Show a dialog with full transaction details."""
        from .dialogs import TransactionDetailDialog
        dialog = TransactionDetailDialog(tx, self.core, self)
        dialog.exec()

    def export_csv(self):
        """Export transactions to CSV file."""
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Transactions", "transactions.csv", "CSV Files (*.csv)"
        )
        if not filename:
            return

        try:
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Timestamp", "Type", "Agent", "ID", "Out", "In", "Recipient",
                    "Network", "Status", "Auto", "Wallet ID", "TX Hash"
                ])
                for tx in self._transactions:
                    tx_type = getattr(tx, 'type', 'x402')

                    # Format "Out" based on type
                    if tx_type == 'trade':
                        out_val = f"{getattr(tx, 'amount_in', '')} {getattr(tx, 'symbol_in', '')}"
                    elif tx_type == 'transfer':
                        out_val = f"{getattr(tx, 'transfer_amount', '')} {getattr(tx, 'transfer_symbol', 'ETH')}"
                    else:
                        out_val = tx.format_amount()

                    # Format "In" based on type
                    if tx_type == 'trade':
                        in_val = f"{getattr(tx, 'amount_out', '') or '?'} {getattr(tx, 'symbol_out', '')}"
                    elif tx_type == 'transfer':
                        in_val = ""
                    else:
                        in_val = tx.resource or ""

                    writer.writerow([
                        tx.timestamp,
                        tx_type,
                        tx.agent_name,
                        tx.agent_id,
                        out_val,
                        in_val,
                        tx.recipient or "",
                        tx.network,
                        tx.status,
                        "Yes" if tx.auto_approved else "No",
                        tx.wallet_id or "",
                        tx.tx_hash or ""
                    ])
            FramelessMessageBox.information(self, "Export Complete", f"Exported {len(self._transactions)} transactions to {filename}")
        except Exception as e:
            FramelessMessageBox.warning(self, "Export Failed", f"Could not export: {e}")


# ============================================
# Wallet Tab (New Multi-Seed Architecture)
# ============================================

class WalletTab(QWidget):
    """Tab for managing the wallet with multiple seeds and addresses."""

    wallets_changed = pyqtSignal(list)  # Emitted when address list changes (list of AddressEntry)
    wallet_deleted = pyqtSignal(str)    # Emitted when address deleted (0x address for agent cleanup)
    wallet_locked = pyqtSignal()        # Emitted when wallet is locked
    wallet_unlocked = pyqtSignal()      # Emitted when wallet is unlocked
    wallet_path_changed = pyqtSignal(str)  # Emitted when wallet path changes (for settings persistence)
    activity = pyqtSignal(str, bool)    # Activity log (message, is_error)

    def __init__(self, core):
        super().__init__()

        self.core = core  # Vault core instance (required)
        self._wallet: Optional[VaultWallet] = None
        wallet_dir = core.get_wallet_dir()
        self._wallet_path = wallet_dir / PRIMER_VAULT_WALLET_FILE
        self._custom_rpcs: dict[int, str] = {}
        self._balance_threads: list = []
        self._selected_network_chain_id: int = DEFAULT_NETWORK  # RHC (4663)

        # Lock state
        self._is_unlocked = False

        # Callback to get agents linked to an address (set by main_window)
        # Signature: (wallet_address: str) -> list[Agent]
        self._get_agents_for_address = None

        # Auto-lock timer (0 = disabled)
        self._auto_lock_minutes = 0
        self._auto_lock_timer = QTimer()
        self._auto_lock_timer.setSingleShot(True)
        self._auto_lock_timer.timeout.connect(self._on_auto_lock_timeout)

        layout = QVBoxLayout(self)

        # Lock overlay - shown when wallet is locked
        self.lock_overlay = QWidget()
        lock_layout = QVBoxLayout(self.lock_overlay)
        lock_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lock_icon = QLabel("🔒")
        lock_icon.setProperty("role", "icon-lg")
        lock_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lock_layout.addWidget(lock_icon)

        lock_title = QLabel("Wallet Locked")
        lock_title.setFont(QFont("", 14, QFont.Weight.Bold))
        lock_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lock_layout.addWidget(lock_title)

        lock_subtitle = QLabel("Enter your password to unlock")
        lock_subtitle.setProperty("role", "muted")
        lock_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lock_layout.addWidget(lock_subtitle)

        lock_layout.addSpacing(16)

        # Password input row
        pw_layout = QHBoxLayout()
        pw_layout.addStretch()

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Password")
        self.password_input.setMaximumWidth(200)
        self.password_input.returnPressed.connect(self.try_unlock)
        pw_layout.addWidget(self.password_input)

        unlock_btn = QPushButton("Unlock")
        unlock_btn.clicked.connect(self.try_unlock)
        pw_layout.addWidget(unlock_btn)

        pw_layout.addStretch()
        lock_layout.addLayout(pw_layout)

        self.lock_error_label = QLabel("")
        self.lock_error_label.setProperty("role", "error")
        self.lock_error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lock_layout.addWidget(self.lock_error_label)

        layout.addWidget(self.lock_overlay)

        # Main content (shown when unlocked)
        self.content_widget = QWidget()
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # Warning banner for unencrypted wallets
        self.unencrypted_warning = QLabel(
            "⚠️ Warning: This wallet has no password set and is NOT encrypted. "
            "Your private keys are stored in plaintext."
        )
        self.unencrypted_warning.setWordWrap(True)
        self.unencrypted_warning.setObjectName("warningBox")
        self.unencrypted_warning.setVisible(False)
        content_layout.addWidget(self.unencrypted_warning)

        toolbar = QHBoxLayout()

        # Button changes based on whether wallet exists
        self.add_btn = QPushButton("+ Add Wallet")
        self.add_btn.clicked.connect(self.on_add_button)
        toolbar.addWidget(self.add_btn)

        toolbar.addStretch()

        # Wallet name — underlined like a link; click opens wallet settings
        self.wallet_label = QLabel("")
        self.wallet_label.setTextFormat(Qt.TextFormat.RichText)
        self.wallet_label.setOpenExternalLinks(False)
        self.wallet_label.setToolTip("Wallet settings")
        self.wallet_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wallet_label.linkActivated.connect(lambda _: self._on_wallet_settings())
        toolbar.addWidget(self.wallet_label)

        content_layout.addLayout(toolbar)

        # === ADDRESSES PANE (compact, ~30% height) ===
        addresses_frame = QFrame()
        addresses_frame.setFrameShape(QFrame.Shape.NoFrame)
        addresses_layout = QVBoxLayout(addresses_frame)
        addresses_layout.setContentsMargins(0, 0, 0, 0)
        addresses_layout.setSpacing(0)

        # Addresses table: Seed | Name | Address (double-click to edit)
        self.address_table = QTableWidget()
        self.address_table.setColumnCount(3)
        self.address_table.setHorizontalHeaderLabels(["Seed", "Name", "Address"])
        self.address_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.address_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.address_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.address_table.setColumnWidth(0, 80)
        self.address_table.setColumnWidth(1, 120)
        self.address_table.setAlternatingRowColors(True)
        self.address_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.address_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.address_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.address_table.setSortingEnabled(False)  # Keep order from wallet
        self.address_table.itemSelectionChanged.connect(self._on_address_selected)
        self.address_table.doubleClicked.connect(self._on_address_double_clicked)
        self.address_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.address_table.customContextMenuRequested.connect(self._on_address_context_menu)
        addresses_layout.addWidget(self.address_table)

        # === ASSETS PANE (larger, ~70% height) ===
        assets_frame = QFrame()
        assets_frame.setFrameShape(QFrame.Shape.NoFrame)
        assets_layout = QVBoxLayout(assets_frame)
        assets_layout.setContentsMargins(0, 4, 0, 0)
        assets_layout.setSpacing(2)

        # Assets header with address label and Send button
        assets_header = QHBoxLayout()
        assets_header.setContentsMargins(0, 0, 0, 0)

        self.selected_address_label = QLabel("")
        self.selected_address_label.setProperty("role", "mono")
        self.selected_address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        assets_header.addWidget(self.selected_address_label)

        assets_header.addStretch()

        # Refresh button
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        self.refresh_btn.setVisible(False)
        assets_header.addWidget(self.refresh_btn)

        # Send button
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send_clicked)
        self.send_btn.setVisible(False)
        assets_header.addWidget(self.send_btn)

        assets_layout.addLayout(assets_header)

        # Assets table: Icon | Asset | Balance | Value
        self.assets_table = QTableWidget()
        self.assets_table.setColumnCount(4)
        self.assets_table.setHorizontalHeaderLabels(["", "Asset", "Balance", "Value"])
        self.assets_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.assets_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.assets_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.assets_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.assets_table.setColumnWidth(0, 32)
        self.assets_table.setColumnWidth(2, 140)
        self.assets_table.setColumnWidth(3, 100)
        self.assets_table.setAlternatingRowColors(True)
        self.assets_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.assets_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.assets_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.assets_table.verticalHeader().setVisible(False)
        self.assets_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.assets_table.customContextMenuRequested.connect(self._on_asset_context_menu)
        assets_layout.addWidget(self.assets_table)

        # Placeholder when no address selected
        self.no_address_label = QLabel("Select an address above to view assets")
        self.no_address_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_address_label.setProperty("role", "muted")
        assets_layout.addWidget(self.no_address_label)

        # Draggable divide between Addresses and Assets
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(addresses_frame)
        splitter.addWidget(assets_frame)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 420])
        content_layout.addWidget(splitter, stretch=1)

        # Cache for balances by address
        self._address_balances: dict[str, list[Balance]] = {}
        self._selected_address: Optional[str] = None

        # Icon cache: url -> QPixmap (or None if failed)
        self._icon_cache: dict[str, Optional[QPixmap]] = {}
        self._icon_threads: list[IconFetcherThread] = []

        self.table = self.address_table  # Alias for brevity

        layout.addWidget(self.content_widget)

        # Initialize state
        self._update_display()

    def _wallet_exists(self) -> bool:
        """Check if the primer.wallet file exists."""
        return self._wallet_path.exists()

    def _is_wallet_encrypted(self) -> bool:
        """Check if the wallet file is encrypted."""
        if not self._wallet_exists():
            return True  # Assume encrypted for non-existent
        return VaultWallet.is_file_encrypted(self._wallet_path)

    def _update_display(self):
        """Update UI based on current state."""
        wallet_exists = self._wallet_exists()

        # Update wallet label (just filename, no prefix)
        if wallet_exists:
            name = _html.escape(self._wallet_path.name)
            self.wallet_label.setText(
                f'<a href="#settings" style="text-decoration: underline;">{name}</a>'
            )
        else:
            self.wallet_label.setText("")

        if not wallet_exists:
            # No wallet yet - show content with "+ Add Wallet" button
            self.lock_overlay.setVisible(False)
            self.content_widget.setVisible(True)
            self.add_btn.setText("+ Add Wallet")
            self.unencrypted_warning.setVisible(False)
            self.address_table.setRowCount(0)
            self._clear_assets_pane()
        elif not self._is_wallet_encrypted():
            # Unencrypted wallet - auto-unlock and show warning
            self.lock_overlay.setVisible(False)
            self.content_widget.setVisible(True)
            self.add_btn.setText("+ Add Address")
            self.unencrypted_warning.setVisible(True)
            if not self._is_unlocked:
                self._auto_unlock()
            else:
                # Already unlocked (e.g., just created) - still need to populate
                self.populate_table()
        elif self._is_unlocked:
            # Unlocked encrypted wallet
            self.lock_overlay.setVisible(False)
            self.content_widget.setVisible(True)
            self.add_btn.setText("+ Add Address")
            self.unencrypted_warning.setVisible(False)
            self.populate_table()
        else:
            # Locked encrypted wallet
            self.lock_overlay.setVisible(True)
            self.content_widget.setVisible(False)
            self.unencrypted_warning.setVisible(False)

    def _auto_unlock(self):
        """Auto-unlock an unencrypted wallet."""
        if not self.core:
            return

        result = self.core.load_wallet(str(self._wallet_path), NO_PASSWORD_SENTINEL)
        if result.get("success"):
            self._wallet = self.core.get_wallet()
            self._is_unlocked = True
            self.populate_table()
            self.wallet_unlocked.emit()
            self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])

            # Refresh balances after auto-unlock
            QTimer.singleShot(500, self.refresh_all_balances)
        else:
            import logging
            logging.getLogger(__name__).warning(f"Failed to auto-unlock wallet: {result.get('error')}")

    @property
    def is_unlocked(self) -> bool:
        """Check if wallet is unlocked."""
        if not self._wallet_exists():
            return True  # No wallet = effectively unlocked (nothing to protect)
        if not self._is_wallet_encrypted():
            return True  # Unencrypted = always unlocked
        return self._is_unlocked

    @property
    def has_wallets(self) -> bool:
        """Check if any addresses exist."""
        if self._wallet:
            return len(self._wallet.addresses) > 0
        return self._wallet_exists()

    def set_agents_query_fn(self, fn):
        """Set callback function to query agents by wallet address.

        Args:
            fn: Function with signature (wallet_address: str) -> list[Agent]
        """
        self._get_agents_for_address = fn

    def set_wallet_path(self, path: str):
        """Set the wallet path (called from settings on startup)."""
        if path:
            from pathlib import Path
            self._wallet_path = Path(path)
            self._update_display()

    def get_wallet_path(self) -> str:
        """Get the current wallet path as string."""
        return str(self._wallet_path) if self._wallet_path else ""

    def _emit_path_changed(self):
        """Emit wallet path changed signal for settings persistence."""
        self.wallet_path_changed.emit(str(self._wallet_path))

    def sync_from_core(self):
        """Sync wallet state from core (called when core wallet state changes externally)."""
        if not self.core:
            return

        core_unlocked = self.core.is_wallet_unlocked()
        core_path = self.core.get_wallet_path()

        if core_unlocked and not self._is_unlocked:
            # Core has an unlocked wallet but we don't - sync our state
            self._wallet = self.core.get_wallet()
            self._is_unlocked = True
            if core_path:
                from pathlib import Path
                new_path = Path(core_path)
                if self._wallet_path != new_path:
                    self._wallet_path = new_path
                    self._emit_path_changed()

            self.password_input.clear()
            self.lock_error_label.setText("")
            self._update_display()
            self.wallet_unlocked.emit()
            self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])
            self.activity.emit("Wallet unlocked", False)

            # Start auto-lock timer if configured
            if self._auto_lock_minutes > 0:
                self._reset_auto_lock_timer()

            # Refresh balances
            QTimer.singleShot(500, self.refresh_all_balances)
        elif not core_unlocked and self._is_unlocked:
            # Core wallet is locked but we think it's unlocked - sync our state
            self._auto_lock_timer.stop()
            self._wallet = None
            self._is_unlocked = False
            self._update_display()
            self.wallet_locked.emit()

    def try_unlock(self):
        """Try to unlock the wallet with the entered password."""
        if not self.core:
            self.lock_error_label.setText("Error: Core not available")
            return

        # Empty password is allowed (wallets can have no password)
        password = self.password_input.text()

        result = self.core.load_wallet(str(self._wallet_path), password)

        if result.get("success"):
            self._wallet = self.core.get_wallet()  # Get reference for display
            self._is_unlocked = True

            self.password_input.clear()
            self.lock_error_label.setText("")
            self._update_display()
            self.wallet_unlocked.emit()
            self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])
            self.activity.emit("Wallet unlocked", False)

            # Start auto-lock timer if configured
            if self._auto_lock_minutes > 0:
                self._reset_auto_lock_timer()

            # Refresh balances
            QTimer.singleShot(500, self.refresh_all_balances)
        else:
            error = result.get("error", "Unknown error")
            if "password" in error.lower():
                self.lock_error_label.setText("Wrong password")
            elif "not found" in error.lower():
                self.lock_error_label.setText("Wallet file not found")
                self._update_display()
            else:
                self.lock_error_label.setText(f"Error: {error[:50]}")
            self.password_input.clear()
            self.password_input.setFocus()

    def lock(self):
        """Lock the wallet, clearing sensitive data from memory."""
        self._auto_lock_timer.stop()

        if self.core:
            self.core.lock_wallet()

        self._wallet = None
        self._is_unlocked = False
        self._update_display()
        self.wallet_locked.emit()

    def set_auto_lock_timeout(self, minutes: int):
        """Set the auto-lock timeout in minutes (0 = disabled)."""
        self._auto_lock_minutes = minutes
        if self._is_unlocked and minutes > 0:
            self._reset_auto_lock_timer()
        elif minutes == 0:
            self._auto_lock_timer.stop()

    def reset_activity(self):
        """Reset the auto-lock timer due to user activity."""
        if self._is_unlocked and self._auto_lock_minutes > 0:
            self._reset_auto_lock_timer()

    def _reset_auto_lock_timer(self):
        """Reset the auto-lock timer."""
        if self._auto_lock_minutes > 0:
            self._auto_lock_timer.start(self._auto_lock_minutes * 60 * 1000)

    def _on_auto_lock_timeout(self):
        """Handle auto-lock timer expiration."""
        if self._is_unlocked:
            self.activity.emit("Auto-locking wallet due to inactivity", False)
            self.lock()

    def get_unlocked_wallet(self, address: str) -> Optional[VaultWallet]:
        """Get the unlocked wallet if the address exists in it."""
        if self._wallet:
            entry = self._wallet.get_address_by_address(address)
            if entry:
                return self._wallet
        return None

    def populate_table(self):
        """Refresh the address table with current addresses."""
        if not self._wallet:
            self.address_table.setRowCount(0)
            self._clear_assets_pane()
            return

        addresses = self._wallet.addresses
        self.address_table.setRowCount(len(addresses))

        # Remember selection
        previously_selected = self._selected_address

        for row, addr in enumerate(addresses):
            # Seed column: show seed_id or "—" for imported
            seed_text = addr.seed_id if addr.seed_id else "—"
            if addr.seed_id and addr.index is not None:
                seed_text = f"{addr.seed_id}#{addr.index}"
            seed_item = QTableWidgetItem(seed_text)
            seed_item.setFont(QFont(Theme.MONO_FONT, 9))
            self.address_table.setItem(row, 0, seed_item)

            # Name
            name_item = QTableWidgetItem(addr.name)
            self.address_table.setItem(row, 1, name_item)

            # Address (truncated for display)
            addr_display = f"{addr.address[:8]}...{addr.address[-6:]}"
            addr_item = QTableWidgetItem(addr_display)
            addr_item.setFont(QFont(Theme.MONO_FONT, 9))
            addr_item.setToolTip(f"{addr.address}\nDouble-click to edit")
            addr_item.setData(Qt.ItemDataRole.UserRole, addr.address)  # Store full address
            self.address_table.setItem(row, 2, addr_item)

        # Restore selection or select first row
        if addresses:
            select_row = 0
            if previously_selected:
                for row in range(len(addresses)):
                    addr_item = self.address_table.item(row, 2)
                    if addr_item and addr_item.data(Qt.ItemDataRole.UserRole) == previously_selected:
                        select_row = row
                        break
            self.address_table.selectRow(select_row)
        else:
            self._clear_assets_pane()

    def _clear_assets_pane(self):
        """Clear the assets pane and show placeholder."""
        self._selected_address = None
        self.selected_address_label.setText("")
        self.assets_table.setRowCount(0)
        self.assets_table.setVisible(False)
        self.no_address_label.setVisible(True)
        self.send_btn.setVisible(False)
        self.refresh_btn.setVisible(False)

    def _on_address_selected(self):
        """Handle address selection change."""
        selected_rows = self.address_table.selectionModel().selectedRows()
        if not selected_rows:
            self._clear_assets_pane()
            return

        row = selected_rows[0].row()
        addr_item = self.address_table.item(row, 2)
        if not addr_item:
            self._clear_assets_pane()
            return

        full_address = addr_item.data(Qt.ItemDataRole.UserRole)
        if not full_address:
            self._clear_assets_pane()
            return

        self._selected_address = full_address
        self.selected_address_label.setText(full_address)
        self.assets_table.setVisible(True)
        self.no_address_label.setVisible(False)
        self.send_btn.setVisible(True)
        self.refresh_btn.setVisible(True)

        # Populate assets from cache
        self._populate_assets(full_address)

        # If no cached balance, auto-fetch
        if full_address not in self._address_balances:
            self.refresh_address_balance(full_address)

    def refresh_theme(self):
        """Repaint palette-driven cell colors after a theme switch."""
        if self._selected_address:
            self._populate_assets(self._selected_address)

    def _populate_assets(self, address: str):
        """Populate the assets table for a given address."""
        balances = self._address_balances.get(address, [])

        if not balances:
            # Empty - no balances fetched yet
            self.assets_table.setRowCount(0)
            return

        # Populate with actual balances
        self.assets_table.setRowCount(len(balances))

        for row, bal in enumerate(balances):
            # Icon - load from cache or fetch
            icon_item = QTableWidgetItem("")
            # Use icon_url from Blockscout, or fallback for native ETH
            icon_url = bal.icon_url
            if not icon_url and bal.is_native and bal.symbol == "ETH":
                icon_url = "https://assets.coingecko.com/coins/images/279/small/ethereum.png"

            if icon_url:
                if icon_url in self._icon_cache:
                    # Use cached icon
                    pixmap = self._icon_cache[icon_url]
                    if pixmap:
                        icon_item.setIcon(QIcon(pixmap))
                else:
                    # Start async fetch
                    self._fetch_icon(icon_url, row)
            self.assets_table.setItem(row, 0, icon_item)

            # Asset name/symbol
            asset_text = f"{bal.symbol}"
            if bal.name and bal.name != bal.symbol:
                asset_text = f"{bal.symbol} ({bal.name})"
            asset_item = QTableWidgetItem(asset_text)
            asset_item.setData(Qt.ItemDataRole.UserRole, bal)  # Store balance object
            self.assets_table.setItem(row, 1, asset_item)

            # Balance
            if bal.fetch_failed:
                balance_text = "Error"
                balance_item = QTableWidgetItem(balance_text)
                balance_item.setForeground(QColor(active()["error"]))
            else:
                # Format based on decimals - show more precision for small amounts
                if bal.formatted == 0:
                    balance_text = "0"
                elif bal.formatted < 0.01:
                    balance_text = f"{bal.formatted:.6f}"
                elif bal.formatted < 1:
                    balance_text = f"{bal.formatted:.4f}"
                else:
                    balance_text = f"{bal.formatted:,.4f}"
                balance_item = QTableWidgetItem(balance_text)
            balance_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            balance_item.setFont(QFont(Theme.MONO_FONT, 10))
            self.assets_table.setItem(row, 2, balance_item)

            # Value (USD) - use usd_value from Blockscout if available
            if bal.fetch_failed:
                value_text = "-"
            elif bal.usd_value is not None:
                value_text = f"${bal.usd_value:,.2f}"
            elif bal.symbol in ("USDG", "USDT", "DAI"):
                # Fallback for stablecoins without exchange_rate
                value_text = f"${bal.formatted:,.2f}"
            else:
                value_text = "-"
            value_item = QTableWidgetItem(value_text)
            value_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.assets_table.setItem(row, 3, value_item)

    def _fetch_icon(self, url: str, row: int):
        """Fetch an icon asynchronously and update the table when done."""
        # Mark as pending (None means in-progress/failed)
        self._icon_cache[url] = None

        thread = IconFetcherThread(url)
        thread.icon_fetched.connect(lambda u, data: self._on_icon_fetched(u, data, row))
        thread.finished.connect(lambda: self._cleanup_icon_thread(thread))
        self._icon_threads.append(thread)
        thread.start()

    def _on_icon_fetched(self, url: str, data: bytes, row: int):
        """Handle fetched icon data."""
        if not data:
            # Failed to fetch
            self._icon_cache[url] = None
            return

        pixmap = QPixmap()
        if pixmap.loadFromData(QByteArray(data)):
            # Scale to fit icon column (24x24)
            pixmap = pixmap.scaled(24, 24, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            self._icon_cache[url] = pixmap

            # Update the table cell if still valid
            if row < self.assets_table.rowCount():
                item = self.assets_table.item(row, 0)
                if item:
                    item.setIcon(QIcon(pixmap))

    def _cleanup_icon_thread(self, thread: IconFetcherThread):
        """Remove finished icon thread from list."""
        if thread in self._icon_threads:
            self._icon_threads.remove(thread)

    def _on_address_double_clicked(self, index):
        """Handle double-click on address row - open details dialog."""
        from .dialogs import AddressDetailsDialog

        row = index.row()
        addr_item = self.address_table.item(row, 2)
        if not addr_item or not self._wallet:
            return

        full_address = addr_item.data(Qt.ItemDataRole.UserRole)
        if not full_address:
            return

        entry = self._wallet.get_address_by_address(full_address)
        if not entry:
            return

        dialog = AddressDetailsDialog(
            entry=entry,
            wallet=self._wallet,
            on_rename=lambda new_name: self._rename_address(entry.id, new_name),
            on_delete=lambda: self.delete_address(entry.id),
            parent=self
        )
        dialog.exec()

    def _rename_address(self, address_id: str, new_name: str):
        """Rename an address."""
        if not self.core or not self._wallet:
            return
        try:
            self._wallet.rename_address(address_id, new_name)
            self._wallet.save(self._wallet_path)
            self.populate_table()
            self.activity.emit(f"Renamed address to '{new_name}'", False)
        except Exception as e:
            self.activity.emit(f"Error renaming: {e}", True)

    def _on_address_context_menu(self, pos):
        """Show context menu for address table."""
        from PyQt6.QtWidgets import QMenu
        item = self.address_table.itemAt(pos)
        if not item:
            return

        row = item.row()
        addr_item = self.address_table.item(row, 2)
        if not addr_item:
            return

        full_address = addr_item.data(Qt.ItemDataRole.UserRole)
        if not full_address:
            return

        # Find address entry
        entry = self._wallet.get_address_by_address(full_address) if self._wallet else None
        name = entry.name if entry else format_address(full_address)

        menu = QMenu(self)
        send_action = menu.addAction(f"Send from {name}...")
        copy_action = menu.addAction("Copy Address")

        action = menu.exec(self.address_table.mapToGlobal(pos))

        if action == send_action:
            self._open_send_dialog(from_address=full_address)
        elif action == copy_action:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(full_address)
            self.activity.emit(f"Copied address: {format_address(full_address)}", False)

    def _on_asset_context_menu(self, pos):
        """Show context menu for assets table."""
        from PyQt6.QtWidgets import QMenu
        item = self.assets_table.itemAt(pos)
        if not item or not self._selected_address:
            return

        row = item.row()
        asset_item = self.assets_table.item(row, 1)
        if not asset_item:
            return

        balance: Balance = asset_item.data(Qt.ItemDataRole.UserRole)
        if not balance:
            return

        menu = QMenu(self)
        send_action = menu.addAction(f"Send {balance.symbol}...")

        action = menu.exec(self.assets_table.mapToGlobal(pos))

        if action == send_action:
            self._open_send_dialog(from_address=self._selected_address, asset_symbol=balance.symbol)

    def _on_send_clicked(self):
        """Handle Send button click."""
        if self._selected_address:
            self._open_send_dialog(from_address=self._selected_address)

    def _on_refresh_clicked(self):
        """Handle Refresh button click."""
        if self._selected_address:
            self.refresh_address_balance(self._selected_address)

    def _open_send_dialog(self, from_address: str = None, asset_symbol: str = None):
        """Open the Send dialog with optional pre-selection."""
        if not self._wallet or not self._is_unlocked:
            FramelessMessageBox.warning(self, "Wallet Required", "Please unlock your wallet first.")
            return

        from .dialogs import SendDialog

        # Get all addresses for the From dropdown
        addresses = self._wallet.addresses

        # Get balances for the selected address
        balances = self._address_balances.get(from_address, []) if from_address else []

        get_key_fn = self.core.get_private_key_for_address if self.core else None

        dialog = SendDialog(
            parent=self,
            addresses=addresses,
            balances_by_address=self._address_balances,
            get_private_key_fn=get_key_fn,
            chain_id=self._selected_network_chain_id,
            preselect_from=from_address,
            preselect_asset=asset_symbol,
        )

        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Refresh balances after successful send
            self.refresh_all_balances()
            self.activity.emit("Transaction sent successfully", False)

    def on_add_button(self):
        """Handle the add button click."""
        if not self._wallet_exists():
            # Show choice dialog: Load or Create
            choice_dialog = AddWalletChoiceDialog(self)
            if choice_dialog.exec() != QDialog.DialogCode.Accepted:
                return

            if choice_dialog.choice == 'load':
                self._load_wallet()
            else:  # 'create'
                self._create_wallet()
        else:
            self._add_address()

    def _load_wallet(self):
        """Browse for and load an existing wallet file."""
        if not self.core:
            self.activity.emit("Error: Core not available", True)
            return

        # Start in the data directory
        start_dir = str(self.core.get_wallet_dir())

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Wallet",
            start_dir,
            "Wallet Files (*.wallet);;All Files (*)"
        )

        if not file_path:
            return

        from pathlib import Path
        wallet_path = Path(file_path)

        if not wallet_path.exists():
            FramelessMessageBox.warning(self, "File Not Found", f"Wallet file not found:\n{file_path}")
            return

        # Check if encrypted
        if VaultWallet.is_file_encrypted(wallet_path):
            # Show unlock dialog - this prompts for password
            dialog = VaultWalletUnlockDialog(wallet_path, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            # Dialog already loaded the wallet, now load through core
            password = dialog.password if hasattr(dialog, 'password') else ""
            result = self.core.load_wallet(str(wallet_path), password)
        else:
            # Load unencrypted through core
            result = self.core.load_wallet(str(wallet_path), NO_PASSWORD_SENTINEL)

        if not result.get("success"):
            FramelessMessageBox.warning(self, "Load Failed", f"Could not load wallet:\n{result.get('error')}")
            return

        # Update local state from core
        self._wallet = self.core.get_wallet()
        self._wallet_path = wallet_path
        self._is_unlocked = True
        self._update_display()
        self._emit_path_changed()
        self.wallet_unlocked.emit()
        self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])
        self.activity.emit(f"Loaded wallet: {wallet_path.name}", False)
        QTimer.singleShot(500, self.refresh_all_balances)

    def _create_wallet(self):
        """Show dialog to create a new wallet with custom filename."""
        if not self.core:
            self.activity.emit("Error: Core not available", True)
            return

        # First, get the filename
        filename_dialog = WalletFilenameDialog(self)
        if filename_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        wallet_filename = filename_dialog.filename
        new_wallet_path = self.core.get_wallet_dir() / wallet_filename

        # Check if file already exists
        if new_wallet_path.exists():
            if not FramelessMessageBox.question(
                self,
                "File Exists",
                f"A wallet named '{wallet_filename}' already exists.\n\nDo you want to overwrite it?",
                default_no=True
            ):
                return

        # Now run the wallet creation wizard
        dialog = CreateWalletWizard(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        password = dialog.password

        # Use core.create_wallet() - this creates, saves, and unlocks the wallet
        if dialog.method == 'import_pkey':
            # Private key import
            result = self.core.create_wallet(
                wallet_path=str(new_wallet_path),
                password=password,
                private_key=dialog.private_key,
                unlock=True
            )
        else:
            # Seed-based (new or imported)
            result = self.core.create_wallet(
                wallet_path=str(new_wallet_path),
                password=password,
                seed_phrase=dialog.seed_phrase,
                derivation_path=dialog.derivation_path,
                address_indices=dialog.selected_indices,
                address_names=dialog.selected_names,
                unlock=True
            )

        if result.get("success"):
            # Get the wallet reference from core (core already created and unlocked it)
            self._wallet = self.core.get_wallet()
            self._wallet_path = new_wallet_path
            self._is_unlocked = True
            self._update_display()
            self._emit_path_changed()
            self.wallet_unlocked.emit()
            self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])

            # Log activity
            addresses = result.get("addresses", [])
            if dialog.method == 'import_pkey':
                if addresses:
                    self.activity.emit(f"Imported private key: {format_address(addresses[0]['address'])}", False)
            else:
                action = "Created" if dialog.method == 'new_seed' else "Imported"
                self.activity.emit(f"{action} wallet with {len(addresses)} address(es)", False)

            self.activity.emit(f"Wallet created: {wallet_filename}", False)
            QTimer.singleShot(500, self.refresh_all_balances)
        else:
            self.activity.emit(f"Error: {result.get('error', 'Unknown error')}", True)
            self._wallet = None

    def _add_address(self):
        """Show dialog to add a new address to existing wallet."""
        if not self._wallet:
            return

        dialog = AddAddressDialog(self._wallet, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if dialog.choice == 'existing_seed':
            # Pass the selected seed from the dialog (avoids extra selection step)
            self._handle_existing_seed(dialog.selected_seed_id)
        elif dialog.choice == 'new_seed':
            self._handle_new_seed()
        elif dialog.choice == 'import_seed':
            self._handle_import_seed()
        elif dialog.choice == 'import_pkey':
            self._handle_import_pkey()

    def _handle_existing_seed(self, preselected_seed_id: str = None):
        """Handle deriving from an existing seed."""
        if not self._wallet:
            return

        # Use preselected seed if provided, otherwise show selection dialog
        if preselected_seed_id:
            seed_id = preselected_seed_id
        elif len(self._wallet.seeds) == 1:
            seed_id = self._wallet.seeds[0].id
        else:
            # Show seed selection dialog
            dialog = SeedSelectionDialog(self._wallet, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            seed_id = dialog.selected_seed_id

        # Show derivation browser
        browser = DerivationBrowserDialog(self._wallet, seed_id, self)
        if browser.exec() != QDialog.DialogCode.Accepted:
            return

        # Check if user requested to delete the entire seed
        if browser.delete_seed_requested:
            self._confirm_and_delete_seed(seed_id)
            return

        # Remove addresses that were unchecked
        removed = 0
        removed_addresses = []  # Track 0x addresses for agent cleanup
        for address_id in browser.removed_addresses:
            result = self.core.remove_address(address_id) if self.core else {"success": False}
            if result.get("success"):
                removed_addresses.append(result.get("removed_address"))
                removed += 1

        # Add selected addresses
        added = 0
        for index, name in browser.selected_addresses.items():
            result = self.core.add_address_from_seed(seed_id, index, name) if self.core else {"success": False}
            if result.get("success"):
                added += 1

        # Rename any existing addresses that were edited
        renamed = 0
        for address_id, new_name in browser.edited_existing.items():
            if self.core and self.core.rename_address(address_id, new_name):
                renamed += 1

        if added > 0 or renamed > 0 or removed > 0:
            self._wallet = self.core.get_wallet() if self.core else None  # Refresh local reference
            self.populate_table()
            self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])

            # Emit wallet_deleted for each removed address (for agent cleanup)
            for addr in removed_addresses:
                if addr:
                    self.wallet_deleted.emit(addr)

            if added > 0:
                self.activity.emit(f"Added {added} address{'es' if added > 1 else ''} from {seed_id}", False)
            if renamed > 0:
                self.activity.emit(f"Renamed {renamed} address{'es' if renamed > 1 else ''}", False)
            if removed > 0:
                self.activity.emit(f"Removed {removed} address{'es' if removed > 1 else ''}", False)
            QTimer.singleShot(500, self.refresh_all_balances)

    def _confirm_and_delete_seed(self, seed_id: str):
        """Delete a seed with agent decommission warning."""
        if not self.core or not self._wallet:
            return

        # Get all addresses from this seed
        seed_addresses = self._wallet.get_addresses_for_seed(seed_id)

        if not seed_addresses:
            # No addresses, just delete the seed
            result = self.core.remove_seed(seed_id, remove_addresses=True)
            if result.get("success"):
                self._wallet = self.core.get_wallet()
                self._update_display()
                self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])
                self.activity.emit(f"Deleted seed {seed_id} (no addresses)", False)
            return

        # Collect all linked agents across all addresses in the seed
        all_linked_agents = []
        address_0x_list = []  # For cleanup signals

        for addr in seed_addresses:
            address_0x_list.append(addr.address)
            if self._get_agents_for_address:
                linked = self._get_agents_for_address(addr.address)
                for agent in linked:
                    if agent not in all_linked_agents:
                        all_linked_agents.append(agent)

        # Build warning message
        warning_msg = (
            f"Delete seed '{seed_id}' and all {len(seed_addresses)} address(es)?\n\n"
        )

        if all_linked_agents:
            agent_names = [a.name for a in all_linked_agents]
            if len(agent_names) <= 5:
                agent_list = ", ".join(agent_names)
            else:
                agent_list = ", ".join(agent_names[:5]) + f" (+{len(agent_names) - 5} more)"
            warning_msg += f"The following agents will be DECOMMISSIONED:\n• {agent_list}\n\n"
        else:
            warning_msg += "No agents are linked to these addresses.\n\n"

        warning_msg += "This action cannot be undone."

        if FramelessMessageBox.question(
            self,
            "Delete Seed",
            warning_msg,
            default_no=True
        ):
            # Delete the seed and all its addresses through core
            result = self.core.remove_seed(seed_id, remove_addresses=True)

            if result.get("success"):
                # Emit wallet_deleted for each address (for agent cleanup)
                for addr_0x in result.get("removed_addresses", []):
                    self.wallet_deleted.emit(addr_0x)

                # Refresh local reference
                self._wallet = self.core.get_wallet()

                # Check if wallet is now empty
                if self.core.is_wallet_empty():
                    self.core.delete_wallet_file()
                    self._wallet = None
                    self._is_unlocked = False

                self._update_display()
                self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])
                self.activity.emit(f"Deleted seed {seed_id} with {len(seed_addresses)} address(es)", False)

    def _handle_new_seed(self):
        """Handle creating a new seed phrase."""
        if not self.core or not self._wallet:
            return

        dialog = NewSeedDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        result = self.core.add_seed(dialog.seed_phrase)
        if result.get("success"):
            seed_id = result.get("seed_id")
            self._wallet = self.core.get_wallet()  # Refresh local reference
            self.activity.emit(f"Created new seed {seed_id}", False)
            # Open derivation browser for the new seed
            self._handle_existing_seed(seed_id)
        else:
            self.activity.emit(f"Error: {result.get('error')}", True)

    def _handle_import_seed(self):
        """Handle importing a seed phrase."""
        if not self.core or not self._wallet:
            return

        dialog = ImportSeedToWalletDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Check if seed already exists
        existing_seed_ids = {s.id for s in self._wallet.seeds}

        result = self.core.add_seed(dialog.seed_phrase)
        if not result.get("success"):
            self.activity.emit(f"Error: {result.get('error')}", True)
            return

        seed_id = result.get("seed_id")
        self._wallet = self.core.get_wallet()  # Refresh local reference

        if seed_id in existing_seed_ids:
            # Seed already exists - show message and open derivation browser
            FramelessMessageBox.information(
                self,
                "Seed Already Exists",
                f"This seed phrase already exists as {seed_id}.\n\n"
                "Opening the derivation browser to manage addresses."
            )
        else:
            self.activity.emit(f"Imported seed as {seed_id}", False)

        # Open derivation browser for the seed (new or existing)
        self._handle_existing_seed(seed_id)

    def _handle_import_pkey(self):
        """Handle importing a private key."""
        if not self.core or not self._wallet:
            return

        dialog = ImportPrivateKeyToWalletDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        result = self.core.add_imported_key(dialog.private_key, dialog.name)
        if not result.get("success"):
            self.activity.emit(f"Error: {result.get('error')}", True)
            return

        self._wallet = self.core.get_wallet()  # Refresh local reference
        self.populate_table()
        self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])

        address = result.get("address")
        if address:
            self.activity.emit(f"Imported private key: {format_address(address)}", False)
        QTimer.singleShot(500, self.refresh_all_balances)

    def _on_sweep_clicked(self):
        """Open the sweep USDG dialog."""
        if not self._wallet or not self._is_unlocked:
            FramelessMessageBox.warning(self, "Wallet Required", "Please unlock your wallet first.")
            return

        addresses = self._wallet.addresses
        if len(addresses) < 2:
            FramelessMessageBox.information(
                self, "Not Enough Addresses",
                "Sweep requires at least 2 addresses (sponsor + donor or recipient)."
            )
            return

        from .dialogs import SweepUSDGDialog

        # Use core's method to get private keys
        get_key_fn = self.core.get_private_key_for_address if self.core else None
        dialog = SweepUSDGDialog(
            parent=self,
            addresses=addresses,
            get_private_key_fn=get_key_fn,
            chain_id=self._selected_network_chain_id
        )
        dialog.exec()

        # Refresh balances after sweep
        self.refresh_all_balances()

    def _on_wallet_settings(self):
        """Open the wallet settings dialog."""
        if not self._wallet:
            return

        logger.debug("Opening wallet settings dialog")
        dialog = WalletSettingsDialog(
            wallet=self._wallet,
            wallet_path=self._wallet_path,
            on_detach=self._on_detach_wallet,
            on_delete=self._on_delete_wallet,
            parent=self
        )
        dialog.exec()
        logger.debug(f"Wallet settings dialog closed, password_changed={dialog.password_changed}")

        # Refresh display if password was changed (encryption status may have changed)
        if dialog.password_changed:
            logger.debug("Refreshing display after password change...")
            self._update_display()
            logger.debug("Display refreshed")

    def _on_detach_wallet(self):
        """Detach (unload) the current wallet without deleting the file."""
        if not self.core or not self._wallet:
            return

        # Collect all linked agents across all addresses
        all_linked_agents = []
        for addr in self._wallet.addresses:
            if self._get_agents_for_address:
                linked = self._get_agents_for_address(addr.address)
                for agent in linked:
                    if agent not in all_linked_agents:
                        all_linked_agents.append(agent)

        # Build warning message
        warning_msg = f"Detach wallet '{self._wallet_path.name}'?\n\n"
        warning_msg += "The wallet file will NOT be deleted.\n\n"

        if all_linked_agents:
            agent_names = [a.name for a in all_linked_agents]
            if len(agent_names) <= 5:
                agent_list = ", ".join(agent_names)
            else:
                agent_list = ", ".join(agent_names[:5]) + f" (+{len(agent_names) - 5} more)"
            warning_msg += f"The following agents will be DECOMMISSIONED:\n• {agent_list}\n\n"
            warning_msg += "Note: Agents will NOT auto-commission if you re-load this wallet."
        else:
            warning_msg += "No agents are currently linked to this wallet."

        if FramelessMessageBox.question(
            self,
            "Detach Wallet",
            warning_msg,
            default_no=True
        ):
            wallet_name = self._wallet_path.name

            # Emit wallet_deleted for each address (for agent cleanup)
            for addr in self._wallet.addresses:
                self.wallet_deleted.emit(addr.address)

            # Detach through core
            self.core.detach_wallet()
            self._wallet = None
            self._is_unlocked = False
            self._wallet_path = self.core.get_wallet_dir() / PRIMER_VAULT_WALLET_FILE  # Reset to default

            self._update_display()
            self._emit_path_changed()
            self.wallets_changed.emit([])
            self.wallet_locked.emit()
            self.activity.emit(f"Detached wallet: {wallet_name}", False)

    def _on_delete_wallet(self):
        """Delete the wallet file permanently."""
        if not self.core or not self._wallet:
            return

        # Collect all linked agents across all addresses
        all_linked_agents = []
        for addr in self._wallet.addresses:
            if self._get_agents_for_address:
                linked = self._get_agents_for_address(addr.address)
                for agent in linked:
                    if agent not in all_linked_agents:
                        all_linked_agents.append(agent)

        # Create custom confirmation dialog
        dialog = FramelessDialog("Delete Wallet", self)
        dialog.setFixedWidth(400)

        layout = dialog.content_layout
        layout.setSpacing(12)

        # Warning message with bold "permanently deleted"
        wallet_name = self._wallet_path.name
        warning_label = QLabel()
        warning_label.setWordWrap(True)
        warning_text = f"Delete wallet '<b>{wallet_name}</b>'?<br><br>"
        warning_text += "The wallet file will be <b>permanently deleted</b>.<br>"
        warning_text += "Make sure you have backed up your seed phrase(s)!<br><br>"

        if all_linked_agents:
            agent_names = [a.name for a in all_linked_agents]
            if len(agent_names) <= 5:
                agent_list = ", ".join(agent_names)
            else:
                agent_list = ", ".join(agent_names[:5]) + f" (+{len(agent_names) - 5} more)"
            warning_text += f"The following agents will be decommissioned:<br>• {agent_list}<br><br>"
        else:
            warning_text += "No agents are currently linked to this wallet.<br><br>"

        warning_text += "This action cannot be undone."
        warning_label.setText(warning_text)
        layout.addWidget(warning_label)

        layout.addSpacing(8)

        # Confirmation input
        confirm_label = QLabel("Type <b>delete my wallet</b> to confirm:")
        layout.addWidget(confirm_label)

        confirm_input = QLineEdit()
        confirm_input.setPlaceholderText("delete my wallet")
        layout.addWidget(confirm_input)

        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.setEnabled(False)
        delete_btn.clicked.connect(dialog.accept)
        btn_layout.addWidget(delete_btn)

        layout.addLayout(btn_layout)

        # Enable delete button only when correct phrase is typed
        def on_text_changed(text):
            delete_btn.setEnabled(text.strip().lower() == "delete my wallet")
        confirm_input.textChanged.connect(on_text_changed)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Emit wallet_deleted for each address (for agent cleanup)
        for addr in self._wallet.addresses:
            self.wallet_deleted.emit(addr.address)

        # Delete through core
        self.core.delete_wallet_file()
        self._wallet = None
        self._is_unlocked = False

        # Clear the tables immediately
        self.address_table.setRowCount(0)
        self._clear_assets_pane()
        self._address_balances.clear()

        self._wallet_path = self.core.get_wallet_dir() / PRIMER_VAULT_WALLET_FILE  # Reset to default

        self._update_display()
        self._emit_path_changed()
        self.wallets_changed.emit([])
        self.wallet_locked.emit()
        self.activity.emit(f"Deleted wallet: {wallet_name}", False)

    def _save_wallet(self):
        """Save the wallet to disk."""
        if self.core:
            self.core.save_wallet()

    def delete_address(self, address_id: str):
        """Delete an address after confirmation."""
        if not self.core or not self._wallet:
            return

        # Find the address
        addr = None
        for a in self._wallet.addresses:
            if a.id == address_id:
                addr = a
                break

        if not addr:
            return

        # Check for linked agents
        linked_agents = []
        if self._get_agents_for_address:
            linked_agents = self._get_agents_for_address(addr.address)

        warning_msg = (
            f"Remove address '{addr.name}'?\n\n"
            f"Address: {format_address(addr.address)}\n\n"
        )

        if addr.seed_id:
            warning_msg += "This address can be re-derived from the seed later.\n\n"
        else:
            warning_msg += "WARNING: This is an imported private key. Make sure you have a backup!\n\n"

        # Show linked agents if any
        if linked_agents:
            agent_names = [a.name for a in linked_agents]
            if len(agent_names) <= 3:
                agent_list = ", ".join(agent_names)
            else:
                agent_list = ", ".join(agent_names[:3]) + f" (+{len(agent_names) - 3} more)"
            warning_msg += f"The following agents will be DECOMMISSIONED:\n• {agent_list}"
        else:
            warning_msg += "No agents are linked to this address."

        if FramelessMessageBox.question(
            self,
            "Remove Address",
            warning_msg,
            default_no=True
        ):
            result = self.core.remove_address(address_id)

            if result.get("success"):
                removed_address = result.get("removed_address")
                self.wallet_deleted.emit(removed_address)

                # Check if wallet is now empty
                if self.core.is_wallet_empty():
                    self.core.delete_wallet_file()
                    self._wallet = None
                    self._is_unlocked = False
                else:
                    self._wallet = self.core.get_wallet()

                self._update_display()
                self.wallets_changed.emit(self._wallet.addresses if self._wallet else [])
                self.activity.emit(f"Removed address: {format_address(removed_address)}", False)

    def refresh_all_balances(self):
        """Refresh balances for all addresses."""
        if not self._wallet:
            return
        for addr in self._wallet.addresses:
            self.refresh_address_balance(addr.address)

    def refresh_address_balance(self, address: str):
        """Refresh balance for a single address."""
        self.activity.emit(f"Fetching balance for {format_address(address)}...", False)

        thread = BalanceFetcherThread(address, self._custom_rpcs)
        thread.balances_updated.connect(lambda bal, addr=address: self.on_balance_updated(addr, bal))
        self._balance_threads.append(thread)
        thread.start()

    def set_custom_rpcs(self, rpcs: dict[int, str]):
        """Set custom RPC URLs for balance fetching."""
        self._custom_rpcs = rpcs.copy() if rpcs else {}

    def on_balance_updated(self, address: str, balances: dict):
        """Handle balance update for an address."""
        selected_chain = self._selected_network_chain_id

        # Store balances for the selected network
        if selected_chain in balances:
            self._address_balances[address] = balances[selected_chain]
        else:
            self._address_balances[address] = []

        # If this is the currently selected address, refresh the assets pane
        if address == self._selected_address:
            self._populate_assets(address)

        # Summarize balances for log
        bal_list = self._address_balances.get(address, [])
        if bal_list:
            # Check if any balance failed to fetch
            any_failed = any(bal.fetch_failed for bal in bal_list)
            if any_failed:
                summary = "error"
            else:
                summaries = []
                for bal in bal_list:
                    if bal.formatted > 0:
                        summaries.append(f"{bal.formatted:.4f} {bal.symbol}")
                summary = ", ".join(summaries) if summaries else "0"
        else:
            summary = "error fetching"

        self.activity.emit(f"Balance: {format_address(address)} = {summary}", False)

    def get_wallet_list(self) -> list[AddressEntry]:
        """Get list of all address entries (for other components)."""
        if self._wallet:
            return self._wallet.addresses
        return []


# ============================================
# Settings Tab
# ============================================

class SettingsTab(QWidget):
    """Tab for application settings (notifications, startup, appearance)."""

    settings_changed = pyqtSignal(dict)  # Emitted when any setting changes
    auto_lock_changed = pyqtSignal(int)  # Emitted when auto-lock timeout changes (minutes)

    def __init__(self, settings: dict = None):
        super().__init__()
        self._settings = settings or {}

        layout = QVBoxLayout(self)

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
        window_group = QGroupBox("Window Behavior")
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

        layout.addWidget(window_group)

        # Startup group
        startup_group = QGroupBox("Startup")
        startup_layout = QFormLayout(startup_group)

        self.auto_start_server_checkbox = QCheckBox("Start server automatically on launch")
        self.auto_start_server_checkbox.setChecked(self._settings.get("auto_start_server", True))
        self.auto_start_server_checkbox.stateChanged.connect(self._on_setting_changed)
        startup_layout.addRow(self.auto_start_server_checkbox)

        layout.addWidget(startup_group)

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
        self.auto_lock_input.valueChanged.connect(self._on_auto_lock_changed)
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

        layout.addWidget(security_group)

        layout.addStretch()

        # Version info at bottom
        version_label = QLabel(f"Vault v{__version__}")
        version_label.setProperty("role", "muted")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)

    def set_settings(self, settings: dict):
        """Update all settings from dict (used on load)."""
        self._settings = settings

        # Block signals while updating to avoid triggering saves
        self.sound_checkbox.blockSignals(True)
        self.toast_checkbox.blockSignals(True)
        self.flash_checkbox.blockSignals(True)
        self.minimize_to_tray_checkbox.blockSignals(True)
        self.close_to_tray_checkbox.blockSignals(True)
        self.start_minimized_checkbox.blockSignals(True)
        self.auto_start_server_checkbox.blockSignals(True)
        self.auto_lock_input.blockSignals(True)
        self.replay_window_input.blockSignals(True)

        self.sound_checkbox.setChecked(settings.get("sound_enabled", True))
        self.toast_checkbox.setChecked(settings.get("toast_enabled", True))
        self.flash_checkbox.setChecked(settings.get("flash_taskbar", True))
        self.minimize_to_tray_checkbox.setChecked(settings.get("minimize_to_tray", False))
        self.close_to_tray_checkbox.setChecked(settings.get("close_to_tray", False))
        self.start_minimized_checkbox.setChecked(settings.get("start_minimized", False))
        self.auto_start_server_checkbox.setChecked(settings.get("auto_start_server", False))
        self.auto_lock_input.setValue(settings.get("auto_lock_minutes", 0))
        self.replay_window_input.setValue(settings.get("replay_window_seconds", 300))

        self.sound_checkbox.blockSignals(False)
        self.toast_checkbox.blockSignals(False)
        self.flash_checkbox.blockSignals(False)
        self.minimize_to_tray_checkbox.blockSignals(False)
        self.close_to_tray_checkbox.blockSignals(False)
        self.start_minimized_checkbox.blockSignals(False)
        self.auto_start_server_checkbox.blockSignals(False)
        self.auto_lock_input.blockSignals(False)
        self.replay_window_input.blockSignals(False)

    def get_settings(self) -> dict:
        """Get current settings as dict."""
        return {
            "sound_enabled": self.sound_checkbox.isChecked(),
            "toast_enabled": self.toast_checkbox.isChecked(),
            "flash_taskbar": self.flash_checkbox.isChecked(),
            "minimize_to_tray": self.minimize_to_tray_checkbox.isChecked(),
            "close_to_tray": self.close_to_tray_checkbox.isChecked(),
            "start_minimized": self.start_minimized_checkbox.isChecked(),
            "auto_start_server": self.auto_start_server_checkbox.isChecked(),
            "auto_lock_minutes": self.auto_lock_input.value(),
            "replay_window_seconds": self.replay_window_input.value(),
        }

    def _on_setting_changed(self, state: int):
        """Handle any setting checkbox change."""
        self._settings.update(self.get_settings())
        self.settings_changed.emit(self._settings)

    def _on_auto_lock_changed(self, value: int):
        """Handle auto-lock timeout change."""
        self._settings["auto_lock_minutes"] = value
        self.settings_changed.emit(self._settings)
        self.auto_lock_changed.emit(value)


# ============================================
# Log Tab
# ============================================

# ASCII art banner for the log terminal (a dark island - colors from CONSOLE).
PRIMER_VAULT_ASCII = (
    '<pre style="font-family: Consolas, monospace; font-size: 12px; line-height: 1.1; margin: 8px;">'
    '<br>'
    f'<span style="color: {CONSOLE["error"]};">█ █ ▄▀█ █ █ █   ▀█▀</span><br>'
    f'<span style="color: {CONSOLE["error"]};">▀▄▀ █▀█ █▄█ █▄▄  █ </span><br>'
    '</pre>'
    f'<span style="color: {CONSOLE["border"]};">═══════════════════════════════════════</span><br>'
    f'<span style="color: {CONSOLE["muted"]};"> Agentic trading hub for RWA on Robinhood Chain</span>'
    f'  <span style="color: {CONSOLE["border"]};">│</span>'
    f'  <span style="color: {CONSOLE["text"]};">v0.1.0</span>'
    f'  <span style="color: {CONSOLE["border"]};">│</span>'
    f'<span style="color: {CONSOLE["muted"]};">localhost:4663</span><br>'
    f'<span style="color: {CONSOLE["border"]};">═══════════════════════════════════════</span><br>'
)




class LogTab(QWidget):
    """Tab showing real-time application logs."""

    def __init__(self):
        super().__init__()
        self._retention_days = 0  # Set by main_window from settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(Theme.MONO_FONT, 10))

        self.log_view.setObjectName("terminal")

        # Set initial content with ASCII banner
        self.log_view.setHtml(PRIMER_VAULT_ASCII)

        layout.addWidget(self.log_view)

        # Control bar with dark styling
        controls = QHBoxLayout()
        controls.setContentsMargins(8, 4, 8, 8)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_logs)
        controls.addWidget(clear_btn)

        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self.copy_logs)
        controls.addWidget(copy_btn)

        controls.addStretch()
        layout.addLayout(controls)

    def set_retention_days(self, days: int):
        """Set log retention (0 = don't save to disk)."""
        self._retention_days = days

    def load_recent(self, max_lines: int):
        """Load recent logs from disk on startup."""
        if max_lines <= 0:
            return

        from ..services.logging import load_recent_logs, format_log_for_display

        lines = load_recent_logs(max_lines)
        if lines:
            # Keep banner, add historical logs
            for line in lines:
                display_line = format_log_for_display(line)
                self._append_colored(display_line)
            self._append_colored("--- Session started ---", color=CONSOLE["border"])

    def add_log(self, message: str):
        """Add a log entry with automatic color coding using Theme colors."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        plain = f"[{timestamp}] {message}"

        # Color code based on content (console island - colors from CONSOLE)
        msg = message.lower()
        if "error" in msg or "failed" in msg:
            color = CONSOLE["error"]      # errors
        elif "warning" in msg or "denied" in msg or "rejected" in msg:
            color = CONSOLE["warn"]       # warnings
        elif "approved" in msg or "success" in msg or "signed" in msg:
            color = CONSOLE["text"]       # success (lime)
        elif "request" in msg or "received" in msg:
            color = CONSOLE["incoming"]   # incoming (teal)
        elif "balance" in msg or "usdg" in msg:
            color = CONSOLE["text"]       # balance info (lime)
        else:
            color = CONSOLE["muted"]      # general

        self._append_colored(f"[{timestamp}] {message}", color)

        # Save to disk if retention is enabled (plain text)
        if self._retention_days > 0:
            from ..services.logging import append_log
            append_log(plain, self._retention_days)

    def _append_colored(self, text: str, color: str = CONSOLE["text"]):
        """Append text with specified color."""
        import html
        escaped = html.escape(text)
        self.log_view.append(f'<span style="color: {color};">{escaped}</span>')

    def clear_logs(self):
        """Clear logs and restore the ASCII banner."""
        self.log_view.setHtml(PRIMER_VAULT_ASCII)

    def copy_logs(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.log_view.toPlainText())
