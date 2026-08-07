#!/usr/bin/env python3
"""
Vault Headless - Run daemon without GUI.

This is a convenience wrapper around `python -m daemon.app`.

Usage:
    python primer_vault_headless.py
    python primer_vault_headless.py --admin-port 4664 --agent-port 4663
    python primer_vault_headless.py --allow-lan
"""

from .daemon.app import main

if __name__ == "__main__":
    main()
