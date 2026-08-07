"""
Daemon module - Runs the Vault core as a background service.

This module must NEVER import PyQt6 or any UI framework.
"""

from .app import run_daemon

__all__ = ['run_daemon']
