"""
Commands module - Pure Python command handling for Vault.

This module provides command parsing and execution independent of any UI framework.
Both CLI and Console use this module for command logic.
"""

from .result import CommandResult
from .handler import CommandHandler

__all__ = ["CommandResult", "CommandHandler"]
