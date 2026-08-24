"""
Wallet package - Secure key management for Vault.

Contains:
- VaultWallet: the wallet itself - many BIP-39/44 seeds, imported keys and
  hardware-backed addresses, all encrypted under one master key
- WalletInfo: display metadata for a wallet, holding none of its contents
- Ledger: hardware wallet support
- Dialogs: UI for wallet management (import separately to avoid circular imports)
"""

from .crypto import (
    NO_PASSWORD_SENTINEL,
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    validate_wallet_password,
    # New multi-seed wallet
    VaultWallet,
    UnsupportedWalletVersion,
    CorruptedWalletFile,
    SeedEntry,
    AddressEntry,
    # Wallet types
    WALLET_TYPE_SOFTWARE,
    WALLET_TYPE_LEDGER,
)
from .manager import WalletInfo
from .ledger import (
    LedgerDevice,
    LedgerAddress,
    LedgerPathType,
    LedgerError,
    LedgerNotFoundError,
    LedgerLockedError,
    LedgerAppNotOpenError,
    LedgerRejectedError,
    LedgerDisconnectedError,
    LedgerBlindSigningDisabledError,
    get_derivation_path,
    get_path_type_display_name,
    get_path_type_description,
)

# NOTE: Dialogs are NOT imported at package level to avoid circular imports.
# UI code that needs dialogs should import directly:
#   from wallet.dialogs import AddAddressDialog, DerivationBrowserDialog, etc.
#
# This allows core code to do `from wallet import VaultWallet` without
# pulling in Qt dependencies.

__all__ = [
    # Crypto
    "NO_PASSWORD_SENTINEL",
    "MIN_PASSWORD_LENGTH",
    "WeakPasswordError",
    "validate_wallet_password",
    # Multi-seed wallet
    "VaultWallet",
    "UnsupportedWalletVersion",
    "CorruptedWalletFile",
    "SeedEntry",
    "AddressEntry",
    "WALLET_TYPE_SOFTWARE",
    "WALLET_TYPE_LEDGER",
    # Metadata
    "WalletInfo",
    # Ledger hardware wallet
    "LedgerDevice",
    "LedgerAddress",
    "LedgerPathType",
    "LedgerError",
    "LedgerNotFoundError",
    "LedgerLockedError",
    "LedgerAppNotOpenError",
    "LedgerRejectedError",
    "LedgerDisconnectedError",
    "LedgerBlindSigningDisabledError",
    "get_derivation_path",
    "get_path_type_display_name",
    "get_path_type_description",
    # NOTE: Dialogs not exported here - import from wallet.dialogs directly
]
