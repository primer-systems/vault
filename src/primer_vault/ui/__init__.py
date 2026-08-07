"""
UI package - PyQt6 user interface components.

Contains:
- Theme: Design system colors and fonts
- MainWindow: Main application window
- Tabs: All application tabs (Policies, Agents, History, Wallet, Logs)
- Dialogs: Agent registration, policy editing, wallet management, settings, network
"""

from .theme import (
    Theme,
    FramelessDialog,
    FramelessMessageBox,
    ask_question,
    show_warning,
    show_info,
)
from .main_window import MainWindow
from .tabs import (
    PoliciesTab,
    AgentsTab,
    HistoryTab,
    WalletTab,
    LogTab,
    BalanceFetcherThread,
)
from .dialogs import (
    AgentRegistrationDialog,
    CommissionDialog,
    NewPolicyDialog,
    SettingsDialog,
    NetworkSettingsDialog,
)
from .console import ConsoleWindow

__all__ = [
    # Theme
    "Theme",
    "FramelessDialog",
    "FramelessMessageBox",
    "ask_question",
    "show_warning",
    "show_info",
    # Main Window
    "MainWindow",
    # Tabs
    "PoliciesTab",
    "AgentsTab",
    "HistoryTab",
    "WalletTab",
    "LogTab",
    "BalanceFetcherThread",
    # Dialogs
    "AgentRegistrationDialog",
    "CommissionDialog",
    "NewPolicyDialog",
    "SettingsDialog",
    "NetworkSettingsDialog",
    # Console
    "ConsoleWindow",
]
