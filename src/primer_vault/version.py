"""Vault version - single source of truth."""

__version__ = "0.2.1"

#: How Vault identifies itself to the services it calls. Derived here so a
#: release cannot leave a stale version behind in a header.
USER_AGENT = f"PrimerVault/{__version__} (+https://primer.systems)"
