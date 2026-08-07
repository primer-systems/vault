"""
Wallet Manager - Multi-wallet support.

Manages the index of all wallets and wallet info metadata.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from .crypto import Wallet, PrivateKeyWallet, set_secure_permissions


MAX_WALLETS = 999  # Cap at W001-W999


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use as a filename.

    Removes/replaces characters that are invalid in Windows/Unix filenames.
    This allows Unicode characters (including emoji) while removing dangerous chars.
    """
    # Characters invalid in Windows filenames: < > : " / \ | ? *
    # Also remove control characters (0x00-0x1F)
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    sanitized = re.sub(invalid_chars, '_', name)
    # Collapse multiple underscores
    sanitized = re.sub(r'_+', '_', sanitized)
    # Strip leading/trailing underscores and whitespace
    sanitized = sanitized.strip('_ ')
    # If empty after sanitization, use a default
    if not sanitized:
        sanitized = 'wallet'
    return sanitized


@dataclass
class WalletInfo:
    """Metadata about a wallet (stored in index, not the wallet file itself)."""
    wallet_id: str       # Unique ID (W001-W999)
    name: str            # User-friendly name
    address: str         # Primary address (0x...)
    filename: str        # Filename in wallet dir (e.g., "default.json")
    created_at: str      # ISO timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WalletInfo":
        return cls(**data)

    def display_label(self) -> str:
        """Format for display: W001 - Name"""
        return f"{self.wallet_id} - {self.name}"


class WalletIndex:
    """Manages the index of all wallets (max 999)."""

    def __init__(self, wallet_dir: Path):
        self.wallet_dir = wallet_dir
        self.index_path = wallet_dir / "wallets.json"
        self._wallets: list[WalletInfo] = []
        self._load()

    def _load(self):
        """Load wallet index from disk."""
        if self.index_path.exists():
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._wallets = [WalletInfo.from_dict(w) for w in data]
            except (json.JSONDecodeError, KeyError):
                self._wallets = []

    def _generate_next_id(self, used_ids: set = None) -> Optional[str]:
        """Generate the next available wallet ID (W001-W999)."""
        if used_ids is None:
            used_ids = {w.wallet_id for w in self._wallets}
        for i in range(1, MAX_WALLETS + 1):
            wallet_id = f"W{i:03d}"
            if wallet_id not in used_ids:
                return wallet_id
        return None

    def _save(self):
        """Save wallet index to disk."""
        self.wallet_dir.mkdir(parents=True, exist_ok=True)
        data = [w.to_dict() for w in self._wallets]
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        set_secure_permissions(self.index_path)

    def can_add_wallet(self) -> bool:
        """Check if we can add another wallet (under cap)."""
        return len(self._wallets) < MAX_WALLETS

    def add_wallet(self, info: WalletInfo) -> bool:
        """Add a wallet to the index. Returns False if at capacity."""
        if len(self._wallets) >= MAX_WALLETS:
            return False
        self._wallets.append(info)
        self._save()
        return True

    def create_wallet_info(self, name: str, address: str, filename: str) -> Optional[WalletInfo]:
        """Create a new WalletInfo with auto-assigned ID. Returns None if at capacity."""
        wallet_id = self._generate_next_id()
        if not wallet_id:
            return None
        return WalletInfo(
            wallet_id=wallet_id,
            name=name,
            address=address,
            filename=filename,
            created_at=datetime.now(timezone.utc).isoformat()
        )

    def remove_wallet(self, address: str) -> Optional[WalletInfo]:
        """Remove a wallet by address. Returns the removed info or None."""
        for i, w in enumerate(self._wallets):
            if w.address == address:
                removed = self._wallets.pop(i)
                self._save()
                return removed
        return None

    def get_wallets(self) -> list[WalletInfo]:
        """Get all wallet infos."""
        return self._wallets.copy()

    def get_by_address(self, address: str) -> Optional[WalletInfo]:
        """Get wallet info by address."""
        for w in self._wallets:
            if w.address == address:
                return w
        return None

    def get_by_id(self, wallet_id: str) -> Optional[WalletInfo]:
        """Get wallet info by wallet ID."""
        for w in self._wallets:
            if w.wallet_id == wallet_id:
                return w
        return None

    def get_wallet_path(self, info: WalletInfo) -> Path:
        """Get the file path for a wallet."""
        return self.wallet_dir / info.filename

    def generate_filename(self) -> str:
        """Generate a unique filename for a new wallet."""
        import uuid
        return f"wallet_{uuid.uuid4().hex[:8]}.json"


class WalletManager:
    """
    Manages multiple wallets for the Vault application.

    Each wallet can be assigned to different agents with different
    spending limits.
    """

    def __init__(self, wallet_dir: str | Path):
        """
        Initialize wallet manager.

        Args:
            wallet_dir: Directory to store wallet files
        """
        self.wallet_dir = Path(wallet_dir)
        self.wallet_dir.mkdir(parents=True, exist_ok=True)
        self._active_wallet: Optional[Wallet] = None
        self._wallets: dict[str, Wallet] = {}  # name -> wallet

    def list_wallets(self) -> list[str]:
        """List all wallet files (by name)."""
        return [f.stem for f in self.wallet_dir.glob("*.json")]

    def wallet_path(self, name: str) -> Path:
        """Get the file path for a wallet by name (sanitized for filesystem safety)."""
        safe_name = sanitize_filename(name)
        return self.wallet_dir / f"{safe_name}.json"

    def create_wallet(self, name: str, password: str,
                      word_count: int = 12) -> Wallet:
        """Create a new wallet with the given name."""
        path = self.wallet_path(name)
        if path.exists():
            raise ValueError(f"Wallet '{name}' already exists")

        wallet = Wallet.create(password, word_count)
        wallet.save(path)
        self._wallets[name] = wallet
        return wallet

    def load_wallet(self, name: str, password: str) -> Wallet:
        """Load a wallet by name."""
        if name in self._wallets:
            return self._wallets[name]

        path = self.wallet_path(name)
        wallet = Wallet.load(path, password)
        self._wallets[name] = wallet
        return wallet

    def unlock_wallet(self, name: str, password: str) -> Wallet:
        """Alias for load_wallet."""
        return self.load_wallet(name, password)

    def lock_wallet(self, name: str) -> None:
        """Lock a wallet, clearing it from memory."""
        if name in self._wallets:
            self._wallets[name].lock()
            del self._wallets[name]

    def lock_all(self) -> None:
        """Lock all wallets."""
        for name in list(self._wallets.keys()):
            self.lock_wallet(name)

    @property
    def active_wallet(self) -> Optional[Wallet]:
        """The currently active wallet."""
        return self._active_wallet

    def set_active(self, name: str) -> None:
        """Set the active wallet by name."""
        if name not in self._wallets:
            raise ValueError(f"Wallet '{name}' is not loaded")
        self._active_wallet = self._wallets[name]
