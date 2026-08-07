"""
Wallet package - Secure key management for Vault.

Contains:
- Wallet: HD wallet with BIP-39/44 derivation
- PrivateKeyWallet: Single-key wallet
- WalletManager: Multi-wallet support
- WalletIndex, WalletInfo: Wallet metadata tracking
- Dialogs: UI for wallet setup and management (import separately to avoid circular imports)
"""

from .crypto import (
    Wallet,
    PrivateKeyWallet,
    WalletAddress,
    load_wallet,
    is_wallet_encrypted,
    encrypt_seed,
    decrypt_seed,
    NO_PASSWORD_SENTINEL,
    # New multi-seed wallet
    VaultWallet,
    SeedEntry,
    AddressEntry,
)
from .manager import (
    WalletManager,
    WalletIndex,
    WalletInfo,
    MAX_WALLETS,
)

# NOTE: Dialogs are NOT imported at package level to avoid circular imports.
# UI code that needs dialogs should import directly:
#   from wallet.dialogs import WelcomeDialog, CreateWalletWizard, etc.
#
# This allows core code to do `from wallet import VaultWallet` without
# pulling in Qt dependencies.

__all__ = [
    # Crypto
    "Wallet",
    "PrivateKeyWallet",
    "WalletAddress",
    "load_wallet",
    "is_wallet_encrypted",
    "encrypt_seed",
    "decrypt_seed",
    "NO_PASSWORD_SENTINEL",
    # New multi-seed wallet
    "VaultWallet",
    "SeedEntry",
    "AddressEntry",
    # Manager
    "WalletManager",
    "WalletIndex",
    "WalletInfo",
    "MAX_WALLETS",
    # NOTE: Dialogs not exported here - import from wallet.dialogs directly
]
