"""
Wallet Crypto - Secure key management.

Industry-standard security:
- BIP-39 seed phrases
- BIP-32/44 HD derivation
- Argon2id key derivation (memory-hard)
- AES-256-GCM authenticated encryption

Keys never exist unencrypted on disk.
"""

import os
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from ..utils import validate_name

# Cryptography
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2.low_level import hash_secret_raw, Type

# Ethereum
from mnemonic import Mnemonic
from eth_account import Account
from eth_account.hdaccount import generate_mnemonic, seed_from_mnemonic, key_from_seed

# Enable HD wallet features
Account.enable_unaudited_hdwallet_features()


# ============================================
# Security Constants
# ============================================

# Argon2id parameters (OWASP recommendations for high-security)
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32  # 256 bits for AES-256

# AES-GCM constants
AES_KEY_SIZE = 32
AES_IV_SIZE = 12  # 96 bits (recommended for GCM)

# BIP-44 derivation path for Ethereum
ETH_DERIVATION_PATH = "m/44'/60'/0'/0/{}"

# Sentinel for unencrypted wallets
NO_PASSWORD_SENTINEL = "__NO_PASSWORD__"

# Secure file permissions (Unix only)
SECURE_FILE_MODE = 0o600  # Owner read/write only


def set_secure_permissions(filepath: Path) -> None:
    """
    Set restrictive file permissions on Unix systems.

    Sets file to mode 0600 (owner read/write only) to protect sensitive wallet data.
    No-op on Windows (NTFS uses ACLs, not Unix permissions).
    """
    if os.name == 'posix':
        try:
            os.chmod(filepath, SECURE_FILE_MODE)
        except OSError:
            # Best effort - don't fail save operation if chmod fails
            pass


# ============================================
# Data Classes
# ============================================

@dataclass
class WalletAddress:
    """A derived address from the HD wallet."""
    path: str       # BIP-44 path (e.g., "m/44'/60'/0'/0/0")
    address: str    # 0x... address
    label: str      # User-friendly name


# ============================================
# Key Derivation
# ============================================

def derive_key(password: str, salt: bytes) -> bytes:
    """
    Derive an encryption key from password using Argon2id.

    Argon2id is memory-hard, making brute-force attacks expensive.
    With these parameters, each password guess requires ~64MB RAM
    and takes ~1 second on modern hardware.
    """
    return hash_secret_raw(
        secret=password.encode('utf-8'),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID
    )


# ============================================
# Encryption
# ============================================

def encrypt_seed(seed_phrase: str, password: str) -> tuple[bytes, bytes, bytes, bytes]:
    """
    Encrypt a seed phrase with a password.

    Returns: (encrypted_data, iv, tag, salt)
    """
    salt = secrets.token_bytes(16)
    key = derive_key(password, salt)
    iv = secrets.token_bytes(AES_IV_SIZE)

    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(iv, seed_phrase.encode('utf-8'), None)

    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]

    return ciphertext, iv, tag, salt


def decrypt_seed(encrypted_seed: bytes, iv: bytes, tag: bytes,
                 salt: bytes, password: str) -> str:
    """
    Decrypt a seed phrase with a password.

    Raises: InvalidTag if password is wrong or data is tampered.
    """
    key = derive_key(password, salt)
    ciphertext_and_tag = encrypted_seed + tag

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext_and_tag, None)

    return plaintext.decode('utf-8')


# ============================================
# Wallet Class
# ============================================

class Wallet:
    """
    Secure Ethereum wallet with HD key derivation.

    Usage:
        # Create new wallet
        wallet = Wallet.create("my-password")
        seed = wallet.seed_phrase  # Store securely offline
        wallet.save("wallet.json")

        # Load existing wallet
        wallet = Wallet.load("wallet.json", "my-password")

        # Get address
        address = wallet.get_address(0)

        # Sign a message
        signature = wallet.sign_message(message, address_index=0)
    """

    def __init__(self, seed_phrase: str, password: str, derivation_path: str = None):
        """Initialize wallet with seed phrase (decrypted in memory)."""
        self._seed_phrase = seed_phrase
        self._password = password
        self._addresses: list[WalletAddress] = []
        self._derivation_path = derivation_path or ETH_DERIVATION_PATH

        mnemo = Mnemonic("english")
        if not mnemo.check(seed_phrase):
            raise ValueError("Invalid seed phrase")

        self._seed = seed_from_mnemonic(seed_phrase, passphrase="")

    @property
    def seed_phrase(self) -> str:
        """The seed phrase (sensitive - only show during backup!)."""
        return self._seed_phrase

    @classmethod
    def create(cls, password: str, word_count: int = 12) -> 'Wallet':
        """
        Create a new wallet with a fresh seed phrase.

        Args:
            password: Password to encrypt the wallet
            word_count: 12 (128-bit) or 24 (256-bit) word seed

        Returns:
            New Wallet instance
        """
        if word_count == 12:
            seed_phrase = generate_mnemonic(num_words=12, lang="english")
        elif word_count == 24:
            seed_phrase = generate_mnemonic(num_words=24, lang="english")
        else:
            raise ValueError("word_count must be 12 or 24")

        wallet = cls(seed_phrase, password)
        wallet.derive_address(0, "Primary")
        return wallet

    @classmethod
    def restore(cls, seed_phrase: str, password: str, derivation_path: str = None) -> 'Wallet':
        """
        Restore a wallet from an existing seed phrase.

        Args:
            seed_phrase: 12 or 24 word BIP-39 seed phrase
            password: Password to encrypt the wallet
            derivation_path: Custom derivation path template
        """
        if derivation_path:
            parts = derivation_path.rstrip('/').split('/')
            if parts and parts[-1].isdigit():
                pass
            elif '{}' not in derivation_path:
                derivation_path = derivation_path.rstrip('/') + '/{}'

        wallet = cls(seed_phrase, password, derivation_path)
        wallet.derive_address(0, "Primary")
        return wallet

    def derive_address(self, index: int, label: str = "") -> WalletAddress:
        """
        Derive an address at the given index.

        Args:
            index: Address index (0, 1, 2, ...)
            label: User-friendly label

        Returns:
            WalletAddress with the derived address
        """
        path = self._derivation_path.format(index)
        private_key = key_from_seed(self._seed, path)
        account = Account.from_key(private_key)

        addr = WalletAddress(
            path=path,
            address=account.address,
            label=label or f"Address {index}"
        )

        existing = [a for a in self._addresses if a.path == path]
        if not existing:
            self._addresses.append(addr)

        return addr

    def get_address(self, index: int) -> str:
        """Get the address at the given index (derives if needed)."""
        path = self._derivation_path.format(index)
        for addr in self._addresses:
            if addr.path == path:
                return addr.address
        return self.derive_address(index).address

    def get_private_key(self, index: int) -> bytes:
        """
        Get the private key for an address index.

        WARNING: Handle with extreme care! Only for signing.
        """
        path = self._derivation_path.format(index)
        return key_from_seed(self._seed, path)

    def get_account(self, index: int) -> Account:
        """Get an eth_account Account object for signing."""
        private_key = self.get_private_key(index)
        return Account.from_key(private_key)

    @property
    def addresses(self) -> list[WalletAddress]:
        """All derived addresses."""
        return self._addresses.copy()

    @property
    def primary_address(self) -> str:
        """The first (primary) address."""
        return self.get_address(0)

    # ============================================
    # File Operations
    # ============================================

    def save(self, filepath: str | Path) -> None:
        """
        Save wallet to file.

        If password is NO_PASSWORD_SENTINEL, saves unencrypted (for testnet use).
        Otherwise, the seed phrase is encrypted with AES-256-GCM.
        """
        filepath = Path(filepath)

        if self._password == NO_PASSWORD_SENTINEL:
            # Unencrypted storage - seed phrase in plaintext
            wallet_data = {
                "version": 1,
                "type": "hd",
                "encrypted": False,
                "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "derivation_path": self._derivation_path,
                "seed_phrase": self._seed_phrase,
                "addresses": [asdict(a) for a in self._addresses]
            }
        else:
            # Encrypted storage
            encrypted_seed, iv, tag, salt = encrypt_seed(
                self._seed_phrase,
                self._password
            )

            wallet_data = {
                "version": 1,
                "type": "hd",
                "encrypted": True,
                "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "derivation_path": self._derivation_path,
                "kdf": {
                    "algorithm": "argon2id",
                    "salt": salt.hex(),
                    "time_cost": ARGON2_TIME_COST,
                    "memory_cost": ARGON2_MEMORY_COST,
                    "parallelism": ARGON2_PARALLELISM
                },
                "encrypted_seed": encrypted_seed.hex(),
                "iv": iv.hex(),
                "tag": tag.hex(),
                "addresses": [asdict(a) for a in self._addresses]
            }

        temp_path = filepath.with_suffix('.tmp')
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(wallet_data, f, indent=2)

        temp_path.replace(filepath)
        set_secure_permissions(filepath)

    @classmethod
    def load(cls, filepath: str | Path, password: str = None) -> 'Wallet':
        """
        Load wallet from file.

        Handles both encrypted and unencrypted wallet files.

        Raises:
            FileNotFoundError: If wallet file doesn't exist
            ValueError: If password is wrong (for encrypted wallets)
        """
        filepath = Path(filepath)

        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if data.get("version") != 1:
            raise ValueError(f"Unsupported wallet version: {data.get('version')}")

        # Check if wallet is unencrypted
        is_encrypted = data.get("encrypted", True)  # Default to encrypted

        if not is_encrypted:
            # Unencrypted wallet - load directly
            seed_phrase = data["seed_phrase"]
            derivation_path = data.get("derivation_path", ETH_DERIVATION_PATH)
            wallet = cls(seed_phrase, NO_PASSWORD_SENTINEL, derivation_path)
        else:
            # Encrypted wallet - decrypt with password
            kdf = data["kdf"]
            salt = bytes.fromhex(kdf["salt"])
            encrypted_seed = bytes.fromhex(data["encrypted_seed"])
            iv = bytes.fromhex(data["iv"])
            tag = bytes.fromhex(data["tag"])

            try:
                seed_phrase = decrypt_seed(encrypted_seed, iv, tag, salt, password)
            except Exception as e:
                raise ValueError("Wrong password or corrupted wallet file") from e

            derivation_path = data.get("derivation_path", ETH_DERIVATION_PATH)
            wallet = cls(seed_phrase, password, derivation_path)

        for addr_data in data.get("addresses", []):
            wallet._addresses.append(WalletAddress(**addr_data))

        return wallet

    @staticmethod
    def is_encrypted(filepath: str | Path) -> bool:
        """Check if a wallet file is encrypted."""
        filepath = Path(filepath)
        if not filepath.exists():
            return True  # Assume encrypted for non-existent files
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get("encrypted", True)  # Default to encrypted
        except (json.JSONDecodeError, IOError):
            return True

    @staticmethod
    def exists(filepath: str | Path) -> bool:
        """Check if a wallet file exists."""
        return Path(filepath).exists()

    # ============================================
    # Signing
    # ============================================

    def sign_message(self, message: str | bytes, address_index: int = 0) -> bytes:
        """
        Sign a message with the private key at the given index.

        Returns: 65-byte signature (r + s + v)
        """
        account = self.get_account(address_index)

        if isinstance(message, str):
            message = message.encode('utf-8')

        signed = account.sign_message(signable_message=message)
        return signed.signature

    def sign_typed_data(self, typed_data: dict, address_index: int = 0):
        """
        Sign EIP-712 typed data (used by x402).

        Returns: SignedMessage with signature
        """
        account = self.get_account(address_index)
        return account.sign_typed_data(full_message=typed_data)

    # ============================================
    # Security: Memory Cleanup
    # ============================================

    def lock(self) -> None:
        """
        Lock the wallet, clearing sensitive data from memory.

        After locking, the wallet cannot sign until reloaded.
        """
        if hasattr(self, '_seed_phrase'):
            self._seed_phrase = None
        if hasattr(self, '_seed'):
            self._seed = None
        if hasattr(self, '_password'):
            self._password = None

    def __del__(self):
        """Attempt to clear sensitive data on destruction."""
        self.lock()


# ============================================
# Private Key Wallet (Single Address)
# ============================================

class PrivateKeyWallet:
    """
    Simple wallet from a single private key.

    Unlike HD wallets, this can only have one address.
    """

    def __init__(self, private_key: bytes, password: str):
        """Initialize wallet with private key (decrypted in memory)."""
        self._private_key = private_key
        self._password = password
        self._account = Account.from_key(private_key)

    @property
    def address(self) -> str:
        """The wallet address."""
        return self._account.address

    @property
    def addresses(self) -> list[WalletAddress]:
        """List of addresses (only one for private key wallets)."""
        return [WalletAddress(
            path="imported",
            address=self.address,
            label="Imported"
        )]

    @classmethod
    def from_private_key(cls, private_key: str, password: str) -> 'PrivateKeyWallet':
        """
        Create a wallet from a hex private key.

        Args:
            private_key: Hex private key (with or without 0x prefix)
            password: Password to encrypt the wallet
        """
        pkey = private_key.strip()
        if pkey.startswith("0x") or pkey.startswith("0X"):
            pkey = pkey[2:]

        pkey_bytes = bytes.fromhex(pkey)
        Account.from_key(pkey_bytes)  # Validate
        return cls(pkey_bytes, password)

    def get_address(self, index: int = 0) -> str:
        """Get address (only index 0 is valid)."""
        if index != 0:
            raise ValueError("Private key wallets only have one address")
        return self.address

    def get_private_key(self, index: int = 0) -> bytes:
        """Get the private key (only index 0 is valid)."""
        if index != 0:
            raise ValueError("Private key wallets only have one address")
        return self._private_key

    def get_account(self, index: int = 0) -> Account:
        """Get the account for signing."""
        if index != 0:
            raise ValueError("Private key wallets only have one address")
        return self._account

    def sign_message(self, message: str | bytes, address_index: int = 0) -> bytes:
        """Sign a message with the private key."""
        if address_index != 0:
            raise ValueError("Private key wallets only have one address")

        if isinstance(message, str):
            message = message.encode('utf-8')

        signed = self._account.sign_message(signable_message=message)
        return signed.signature

    def sign_typed_data(self, typed_data: dict, address_index: int = 0):
        """Sign EIP-712 typed data."""
        if address_index != 0:
            raise ValueError("Private key wallets only have one address")
        return self._account.sign_typed_data(full_message=typed_data)

    def save(self, filepath: str | Path) -> None:
        """Save wallet to file (encrypted or unencrypted based on password)."""
        filepath = Path(filepath)

        pkey_hex = self._private_key.hex()

        if self._password == NO_PASSWORD_SENTINEL:
            # Unencrypted storage
            wallet_data = {
                "version": 1,
                "type": "private_key",
                "encrypted": False,
                "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "private_key": pkey_hex,
                "address": self.address
            }
        else:
            # Encrypted storage
            encrypted_pkey, iv, tag, salt = encrypt_seed(pkey_hex, self._password)

            wallet_data = {
                "version": 1,
                "type": "private_key",
                "encrypted": True,
                "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "kdf": {
                    "algorithm": "argon2id",
                    "salt": salt.hex(),
                    "time_cost": ARGON2_TIME_COST,
                    "memory_cost": ARGON2_MEMORY_COST,
                    "parallelism": ARGON2_PARALLELISM
                },
                "encrypted_seed": encrypted_pkey.hex(),
                "iv": iv.hex(),
                "tag": tag.hex(),
                "address": self.address
            }

        temp_path = filepath.with_suffix('.tmp')
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(wallet_data, f, indent=2)

        temp_path.replace(filepath)
        set_secure_permissions(filepath)

    @classmethod
    def load(cls, filepath: str | Path, password: str = None) -> 'PrivateKeyWallet':
        """Load wallet from file (handles encrypted and unencrypted)."""
        filepath = Path(filepath)

        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if data.get("type") != "private_key":
            raise ValueError("Not a private key wallet file")

        is_encrypted = data.get("encrypted", True)  # Default to encrypted

        if not is_encrypted:
            # Unencrypted wallet
            pkey_hex = data["private_key"]
            pkey_bytes = bytes.fromhex(pkey_hex)
            return cls(pkey_bytes, NO_PASSWORD_SENTINEL)
        else:
            # Encrypted wallet
            kdf = data["kdf"]
            salt = bytes.fromhex(kdf["salt"])
            encrypted_pkey = bytes.fromhex(data["encrypted_seed"])
            iv = bytes.fromhex(data["iv"])
            tag = bytes.fromhex(data["tag"])

            try:
                pkey_hex = decrypt_seed(encrypted_pkey, iv, tag, salt, password)
            except Exception as e:
                raise ValueError("Wrong password or corrupted wallet file") from e

            pkey_bytes = bytes.fromhex(pkey_hex)
            return cls(pkey_bytes, password)

    def lock(self) -> None:
        """Lock the wallet, clearing sensitive data from memory."""
        if hasattr(self, '_private_key'):
            self._private_key = None
        if hasattr(self, '_account'):
            self._account = None
        if hasattr(self, '_password'):
            self._password = None

    def __del__(self):
        """Attempt to clear sensitive data on destruction."""
        self.lock()


# ============================================
# Wallet Loader (Auto-detect type)
# ============================================

def load_wallet(filepath: str | Path, password: str = None) -> Wallet | PrivateKeyWallet:
    """
    Load a wallet file, auto-detecting the type.

    Returns either a Wallet (HD) or PrivateKeyWallet.
    Password can be None for unencrypted wallets.
    """
    filepath = Path(filepath)

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if data.get("type") == "private_key":
        return PrivateKeyWallet.load(filepath, password)
    else:
        return Wallet.load(filepath, password)


def is_wallet_encrypted(filepath: str | Path) -> bool:
    """Check if a wallet file is encrypted."""
    filepath = Path(filepath)
    if not filepath.exists():
        return True  # Assume encrypted for non-existent files
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("encrypted", True)  # Default to encrypted
    except (json.JSONDecodeError, IOError):
        return True


# ============================================
# Vault Wallet (Multi-Seed, Multi-Address)
# ============================================

@dataclass
class SeedEntry:
    """A seed phrase stored in the wallet."""
    id: str                    # S001, S002, etc.
    encrypted_phrase: str      # Encrypted seed phrase (hex)
    iv: str                    # AES IV (hex)
    tag: str                   # AES tag (hex)
    derivation_path: str       # Path template, e.g., "m/44'/60'/0'/0/{}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SeedEntry":
        return cls(**data)


@dataclass
class AddressEntry:
    """An address in the wallet."""
    id: str                    # A001, A002, etc.
    name: str                  # User-friendly name
    address: str               # 0x... address
    seed_id: Optional[str] = None     # S001, S002, or None for imported
    index: Optional[int] = None       # Derivation index, or None for imported
    encrypted_pkey: Optional[str] = None  # For imported keys only (hex)
    pkey_iv: Optional[str] = None         # AES IV for imported key
    pkey_tag: Optional[str] = None        # AES tag for imported key

    def to_dict(self) -> dict:
        d = asdict(self)
        # Remove None values for cleaner JSON
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "AddressEntry":
        return cls(**data)


class VaultWallet:
    """
    Primer wallet with multiple seeds and addresses.

    Structure:
    - Single wallet file (primer.wallet)
    - One password protects everything
    - Multiple seed phrases (S001, S002, ...)
    - Multiple addresses derived from seeds or imported

    Usage:
        # Create new wallet
        wallet = VaultWallet.create("password")
        wallet.add_seed(seed_phrase)  # Returns seed_id like "S001"
        wallet.add_address_from_seed("S001", 0, "Main")
        wallet.save("primer.wallet")

        # Load existing
        wallet = VaultWallet.load("primer.wallet", "password")
    """

    def __init__(self, password: str, salt: bytes):
        """Initialize wallet (internal use - use create() or load())."""
        self._password = password
        self._salt = salt
        self._seeds: dict[str, SeedEntry] = {}       # id -> SeedEntry
        self._addresses: dict[str, AddressEntry] = {}  # id -> AddressEntry
        self._decrypted_seeds: dict[str, str] = {}   # id -> plaintext phrase
        self._decrypted_pkeys: dict[str, bytes] = {} # address_id -> private key bytes
        self._created_at: str = ""
        self._next_seed_num = 1
        self._next_addr_num = 1

    @property
    def is_encrypted(self) -> bool:
        """Check if wallet uses encryption."""
        return self._password != NO_PASSWORD_SENTINEL

    @property
    def seeds(self) -> list[SeedEntry]:
        """All seed entries."""
        return list(self._seeds.values())

    @property
    def addresses(self) -> list[AddressEntry]:
        """All address entries, sorted by seed_id then index."""
        return sorted(
            self._addresses.values(),
            key=lambda a: (a.seed_id or "ZZZZ", a.index if a.index is not None else 9999, a.id)
        )

    def get_seed_ids(self) -> list[str]:
        """Get all seed IDs in order."""
        return sorted(self._seeds.keys())

    def get_addresses_for_seed(self, seed_id: str) -> list[AddressEntry]:
        """Get all addresses derived from a specific seed."""
        return sorted(
            [a for a in self._addresses.values() if a.seed_id == seed_id],
            key=lambda a: (a.index if a.index is not None else 9999, a.id)
        )

    def get_imported_addresses(self) -> list[AddressEntry]:
        """Get all imported (non-seed) addresses."""
        return [a for a in self._addresses.values() if a.seed_id is None]

    @classmethod
    def create(cls, password: str) -> "VaultWallet":
        """Create a new empty wallet."""
        salt = secrets.token_bytes(16)
        wallet = cls(password, salt)
        wallet._created_at = datetime.now(timezone.utc).isoformat()
        return wallet

    def _encrypt_data(self, plaintext: str) -> tuple[str, str, str]:
        """Encrypt data with wallet password. Returns (ciphertext_hex, iv_hex, tag_hex)."""
        if self._password == NO_PASSWORD_SENTINEL:
            # Store as "plaintext" with markers
            return plaintext, "", ""

        key = derive_key(self._password, self._salt)
        iv = secrets.token_bytes(AES_IV_SIZE)
        aesgcm = AESGCM(key)
        ciphertext_and_tag = aesgcm.encrypt(iv, plaintext.encode('utf-8'), None)
        ciphertext = ciphertext_and_tag[:-16]
        tag = ciphertext_and_tag[-16:]
        return ciphertext.hex(), iv.hex(), tag.hex()

    def _decrypt_data(self, ciphertext_hex: str, iv_hex: str, tag_hex: str) -> str:
        """Decrypt data with wallet password."""
        if self._password == NO_PASSWORD_SENTINEL:
            # Data is plaintext
            return ciphertext_hex

        key = derive_key(self._password, self._salt)
        ciphertext = bytes.fromhex(ciphertext_hex)
        iv = bytes.fromhex(iv_hex)
        tag = bytes.fromhex(tag_hex)
        ciphertext_and_tag = ciphertext + tag
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(iv, ciphertext_and_tag, None)
        return plaintext.decode('utf-8')

    def _next_seed_id(self) -> str:
        """Generate next seed ID."""
        while True:
            sid = f"S{self._next_seed_num:03d}"
            self._next_seed_num += 1
            if sid not in self._seeds:
                return sid

    def _next_address_id(self) -> str:
        """Generate next address ID."""
        while True:
            aid = f"A{self._next_addr_num:03d}"
            self._next_addr_num += 1
            if aid not in self._addresses:
                return aid

    def add_seed(self, seed_phrase: str, derivation_path: str = None) -> str:
        """
        Add a new seed phrase to the wallet.

        Args:
            seed_phrase: BIP-39 mnemonic
            derivation_path: Custom path template (default: Ethereum BIP-44)

        Returns:
            Seed ID (e.g., "S001")
        """
        # Validate seed phrase
        mnemo = Mnemonic("english")
        if not mnemo.check(seed_phrase):
            raise ValueError("Invalid seed phrase")

        # Check for duplicates
        for sid, phrase in self._decrypted_seeds.items():
            if phrase == seed_phrase:
                return sid  # Already exists

        seed_id = self._next_seed_id()
        path = derivation_path or ETH_DERIVATION_PATH

        # Ensure path has {} placeholder
        if '{}' not in path:
            path = path.rstrip('/') + '/{}'

        encrypted, iv, tag = self._encrypt_data(seed_phrase)

        entry = SeedEntry(
            id=seed_id,
            encrypted_phrase=encrypted,
            iv=iv,
            tag=tag,
            derivation_path=path
        )
        self._seeds[seed_id] = entry
        self._decrypted_seeds[seed_id] = seed_phrase

        return seed_id

    def get_seed_phrase(self, seed_id: str) -> str:
        """Get decrypted seed phrase (for backup display)."""
        if seed_id not in self._decrypted_seeds:
            raise ValueError(f"Unknown seed: {seed_id}")
        return self._decrypted_seeds[seed_id]

    def derive_address_at_index(self, seed_id: str, index: int) -> str:
        """
        Derive the address at a specific index without adding it.

        Returns the 0x... address string.
        """
        if seed_id not in self._decrypted_seeds:
            raise ValueError(f"Unknown seed: {seed_id}")

        seed_phrase = self._decrypted_seeds[seed_id]
        seed_entry = self._seeds[seed_id]
        path = seed_entry.derivation_path.format(index)

        seed_bytes = seed_from_mnemonic(seed_phrase, passphrase="")
        private_key = key_from_seed(seed_bytes, path)
        account = Account.from_key(private_key)

        return account.address

    def add_address_from_seed(self, seed_id: str, index: int, name: str = None) -> str:
        """
        Add an address derived from a seed.

        Args:
            seed_id: The seed to derive from (e.g., "S001")
            index: Derivation index
            name: User-friendly name (default: "S001 #0")

        Returns:
            Address ID (e.g., "A001")
        """
        if seed_id not in self._seeds:
            raise ValueError(f"Unknown seed: {seed_id}")

        # Validate custom name if provided
        if name:
            validate_name(name, "Address name")

        # Check if this address already exists
        for addr in self._addresses.values():
            if addr.seed_id == seed_id and addr.index == index:
                return addr.id  # Already exists

        address = self.derive_address_at_index(seed_id, index)
        addr_name = name or f"{seed_id} #{index}"
        addr_id = self._next_address_id()

        entry = AddressEntry(
            id=addr_id,
            name=addr_name,
            address=address,
            seed_id=seed_id,
            index=index
        )
        self._addresses[addr_id] = entry

        return addr_id

    def add_imported_key(self, private_key: str, name: str = None) -> str:
        """
        Add an imported private key.

        Args:
            private_key: Hex private key (with or without 0x prefix)
            name: User-friendly name (default: "Imported #N")

        Returns:
            Address ID (e.g., "A001")
        """
        # Validate custom name if provided
        if name:
            validate_name(name, "Address name")

        pkey = private_key.strip()
        if pkey.startswith("0x") or pkey.startswith("0X"):
            pkey = pkey[2:]

        pkey_bytes = bytes.fromhex(pkey)
        account = Account.from_key(pkey_bytes)

        # Check for duplicate
        for addr in self._addresses.values():
            if addr.address.lower() == account.address.lower():
                return addr.id  # Already exists

        encrypted, iv, tag = self._encrypt_data(pkey)
        addr_id = self._next_address_id()

        # Count existing imported addresses for default name
        imported_count = len(self.get_imported_addresses())
        addr_name = name or f"Imported #{imported_count + 1}"

        entry = AddressEntry(
            id=addr_id,
            name=addr_name,
            address=account.address,
            seed_id=None,
            index=None,
            encrypted_pkey=encrypted,
            pkey_iv=iv,
            pkey_tag=tag
        )
        self._addresses[addr_id] = entry
        self._decrypted_pkeys[addr_id] = pkey_bytes

        return addr_id

    def rename_address(self, address_id: str, new_name: str) -> None:
        """Rename an address."""
        if address_id not in self._addresses:
            raise ValueError(f"Unknown address: {address_id}")
        validate_name(new_name, "Address name")
        self._addresses[address_id].name = new_name

    def remove_address(self, address_id: str) -> None:
        """Remove an address from the wallet."""
        if address_id not in self._addresses:
            raise ValueError(f"Unknown address: {address_id}")
        del self._addresses[address_id]
        if address_id in self._decrypted_pkeys:
            del self._decrypted_pkeys[address_id]

    def remove_seed(self, seed_id: str, remove_addresses: bool = True) -> None:
        """
        Remove a seed from the wallet.

        Args:
            seed_id: The seed to remove
            remove_addresses: If True, also remove all addresses derived from this seed
        """
        if seed_id not in self._seeds:
            raise ValueError(f"Unknown seed: {seed_id}")

        if remove_addresses:
            to_remove = [a.id for a in self._addresses.values() if a.seed_id == seed_id]
            for addr_id in to_remove:
                del self._addresses[addr_id]

        del self._seeds[seed_id]
        if seed_id in self._decrypted_seeds:
            del self._decrypted_seeds[seed_id]

    def get_private_key(self, address_id: str) -> bytes:
        """
        Get private key for an address.

        Returns the raw private key bytes for signing.
        """
        if address_id not in self._addresses:
            raise ValueError(f"Unknown address: {address_id}")

        addr = self._addresses[address_id]

        if addr.seed_id is not None:
            # Derived from seed
            seed_phrase = self._decrypted_seeds[addr.seed_id]
            seed_entry = self._seeds[addr.seed_id]
            path = seed_entry.derivation_path.format(addr.index)
            seed_bytes = seed_from_mnemonic(seed_phrase, passphrase="")
            return key_from_seed(seed_bytes, path)
        else:
            # Imported key
            if address_id in self._decrypted_pkeys:
                return self._decrypted_pkeys[address_id]
            # Decrypt if needed
            pkey_hex = self._decrypt_data(
                addr.encrypted_pkey,
                addr.pkey_iv,
                addr.pkey_tag
            )
            pkey_bytes = bytes.fromhex(pkey_hex)
            self._decrypted_pkeys[address_id] = pkey_bytes
            return pkey_bytes

    def get_account(self, address_id: str) -> Account:
        """Get eth_account Account for signing."""
        pkey = self.get_private_key(address_id)
        return Account.from_key(pkey)

    def get_address_by_address(self, address: str) -> Optional[AddressEntry]:
        """Find address entry by 0x address."""
        for entry in self._addresses.values():
            if entry.address.lower() == address.lower():
                return entry
        return None

    def sign_typed_data(self, typed_data: dict, address_id: str):
        """Sign EIP-712 typed data."""
        account = self.get_account(address_id)
        return account.sign_typed_data(full_message=typed_data)

    # ============================================
    # Password Management
    # ============================================

    def change_password(self, new_password: str) -> None:
        """
        Change the wallet password.

        Re-encrypts all seeds and private keys with the new password.
        The wallet must be saved after calling this method.

        Args:
            new_password: New password (or NO_PASSWORD_SENTINEL for unencrypted)
        """
        if new_password == self._password:
            return  # No change needed

        # Generate new salt for the new password
        new_salt = secrets.token_bytes(16)
        old_password = self._password
        old_salt = self._salt

        # Update password and salt
        self._password = new_password
        self._salt = new_salt

        # Re-encrypt all seeds
        for seed_id, phrase in self._decrypted_seeds.items():
            encrypted, iv, tag = self._encrypt_data(phrase)
            entry = self._seeds[seed_id]
            self._seeds[seed_id] = SeedEntry(
                id=entry.id,
                encrypted_phrase=encrypted,
                iv=iv,
                tag=tag,
                derivation_path=entry.derivation_path
            )

        # Re-encrypt all imported private keys
        for addr_id, pkey_bytes in self._decrypted_pkeys.items():
            entry = self._addresses[addr_id]
            if entry.encrypted_pkey:  # Only re-encrypt imported keys
                pkey_hex = pkey_bytes.hex()
                encrypted, iv, tag = self._encrypt_data(pkey_hex)
                self._addresses[addr_id] = AddressEntry(
                    id=entry.id,
                    name=entry.name,
                    address=entry.address,
                    seed_id=entry.seed_id,
                    index=entry.index,
                    encrypted_pkey=encrypted,
                    pkey_iv=iv,
                    pkey_tag=tag
                )

    def set_password(self, new_password: str) -> None:
        """
        Set password on an unencrypted wallet, or change existing password.

        Alias for change_password() with clearer semantics for unencrypted wallets.
        """
        self.change_password(new_password)

    # ============================================
    # File Operations
    # ============================================

    def save(self, filepath: str | Path) -> None:
        """Save wallet to file."""
        filepath = Path(filepath)

        wallet_data = {
            "version": 2,
            "encrypted": self.is_encrypted,
            "created_at": self._created_at,
            "kdf": {
                "algorithm": "argon2id",
                "salt": self._salt.hex(),
                "time_cost": ARGON2_TIME_COST,
                "memory_cost": ARGON2_MEMORY_COST,
                "parallelism": ARGON2_PARALLELISM
            },
            "seeds": [s.to_dict() for s in self._seeds.values()],
            "addresses": [a.to_dict() for a in self._addresses.values()]
        }

        # Ensure directory exists
        filepath.parent.mkdir(parents=True, exist_ok=True)

        temp_path = filepath.with_suffix('.tmp')
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(wallet_data, f, indent=2)

        temp_path.replace(filepath)
        set_secure_permissions(filepath)

    @classmethod
    def load(cls, filepath: str | Path, password: str) -> "VaultWallet":
        """
        Load wallet from file.

        Raises:
            FileNotFoundError: If wallet doesn't exist
            ValueError: If password is wrong or file is corrupted
        """
        filepath = Path(filepath)

        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        version = data.get("version", 1)
        if version != 2:
            raise ValueError(f"Unsupported wallet version: {version}")

        is_encrypted = data.get("encrypted", True)
        if not is_encrypted:
            password = NO_PASSWORD_SENTINEL

        kdf = data["kdf"]
        salt = bytes.fromhex(kdf["salt"])

        wallet = cls(password, salt)
        wallet._created_at = data.get("created_at", "")

        # Load and decrypt seeds
        for seed_data in data.get("seeds", []):
            entry = SeedEntry.from_dict(seed_data)
            wallet._seeds[entry.id] = entry

            # Decrypt seed phrase
            try:
                phrase = wallet._decrypt_data(
                    entry.encrypted_phrase,
                    entry.iv,
                    entry.tag
                )
                wallet._decrypted_seeds[entry.id] = phrase
            except Exception as e:
                raise ValueError("Wrong password or corrupted wallet") from e

            # Track highest seed number
            if entry.id.startswith("S"):
                try:
                    num = int(entry.id[1:])
                    wallet._next_seed_num = max(wallet._next_seed_num, num + 1)
                except ValueError:
                    pass

        # Load addresses
        for addr_data in data.get("addresses", []):
            entry = AddressEntry.from_dict(addr_data)
            wallet._addresses[entry.id] = entry

            # Decrypt imported private keys
            if entry.encrypted_pkey:
                try:
                    pkey_hex = wallet._decrypt_data(
                        entry.encrypted_pkey,
                        entry.pkey_iv,
                        entry.pkey_tag
                    )
                    wallet._decrypted_pkeys[entry.id] = bytes.fromhex(pkey_hex)
                except Exception as e:
                    raise ValueError("Wrong password or corrupted wallet") from e

            # Track highest address number
            if entry.id.startswith("A"):
                try:
                    num = int(entry.id[1:])
                    wallet._next_addr_num = max(wallet._next_addr_num, num + 1)
                except ValueError:
                    pass

        return wallet

    @staticmethod
    def exists(filepath: str | Path) -> bool:
        """Check if wallet file exists."""
        return Path(filepath).exists()

    @staticmethod
    def is_file_encrypted(filepath: str | Path) -> bool:
        """Check if a wallet file is encrypted."""
        filepath = Path(filepath)
        if not filepath.exists():
            return True
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get("encrypted", True)
        except (json.JSONDecodeError, IOError):
            return True

    def lock(self) -> None:
        """Lock the wallet, clearing sensitive data from memory."""
        self._decrypted_seeds.clear()
        self._decrypted_pkeys.clear()
        self._password = None

    def __del__(self):
        """Attempt to clear sensitive data on destruction."""
        self.lock()
