"""
Core module - UI-agnostic business logic.

This module must NEVER import PyQt6 or any UI framework.
"""

from .vault import Vault
from .events import Event, EventBus, EventType
from .interfaces import ApprovalHandler
from .settings import SettingsManager, AppSettings

__all__ = ['Vault', 'Event', 'EventBus', 'EventType', 'ApprovalHandler', 'SettingsManager', 'AppSettings']
