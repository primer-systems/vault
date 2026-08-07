"""
Vault - Desktop x402 payment manager for AI agents.

Delegate spending authority to agents without sharing private keys.
"""

from .version import __version__
from .app import main

__all__ = ["__version__", "main"]
