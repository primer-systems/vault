"""
Wallet metadata.

WalletInfo describes a wallet for display - its id, name and primary address -
without holding any of its contents. The wallet itself is a VaultWallet, loaded
from its own file.
"""

from dataclasses import dataclass, asdict


@dataclass
class WalletInfo:
    """Metadata about a wallet (stored in index, not the wallet file itself)."""
    wallet_id: str       # Unique ID (W001-W999)
    name: str            # User-friendly name
    address: str         # Primary address (0x...)
    filename: str        # Filename in wallet dir (e.g., "main.wallet")
    created_at: str      # ISO timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WalletInfo":
        return cls(**data)

    def display_label(self) -> str:
        """Format for display: W001 - Name"""
        return f"{self.wallet_id} - {self.name}"
