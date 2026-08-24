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
import logging
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from ..utils import fsync_dir, validate_name

# Cryptography
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2.low_level import hash_secret_raw, Type

# Ethereum
from mnemonic import Mnemonic
from eth_account import Account
from eth_account.hdaccount import seed_from_mnemonic, key_from_seed

# Enable HD wallet features
Account.enable_unaudited_hdwallet_features()

logger = logging.getLogger(__name__)


# ============================================
# Security Constants
# ============================================

# Argon2id parameters for wrapping the master key.
#
# These are the settings new wallets are written with. An existing wallet records
# the settings it was wrapped with and is opened using those, so raising these
# strands nothing.
#
# Memory is the lever that matters: it is the resource an attacker with graphics
# cards or custom hardware finds expensive to parallelise, far more than passes.
# 256 MB costs ~280ms to unlock on a current desktop, which is imperceptible for
# a once-per-session operation, and puts a single-core attacker at roughly 3.5
# guesses per second.
#
# This runs only on unlock, not per agent request, which is what affords a
# budget this large.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 262144  # 256 MB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32  # 256 bits for AES-256

# AES-GCM constants
AES_KEY_SIZE = 32
AES_IV_SIZE = 12  # 96 bits (recommended for GCM)

# BIP-44 derivation path for Ethereum
ETH_DERIVATION_PATH = "m/44'/60'/0'/0/{}"

# Placeholder callers pass to load() when opening a legacy unencrypted wallet
# file (load ignores the password for those). Creating an unencrypted wallet,
# or stripping the password from an encrypted one, is not possible from any
# interface: validate_wallet_password rejects this value outright, and
# create()/change_password() always wrap the master key. Only the read side
# exists, so legacy unencrypted wallet files still open.
NO_PASSWORD_SENTINEL = "__NO_PASSWORD__"

# On-disk wallet format. Older layouts are refused, not guessed at.
WALLET_FORMAT_VERSION = 3


class UnsupportedWalletVersion(ValueError):
    """The wallet file is not a format this build reads.

    Separate from a wrong password because the remedy is different and no
    password will ever open the file.
    """


class CorruptedWalletFile(ValueError):
    """The file cannot be read as a wallet at all.

    Separate from a wrong password for the same reason as above: retyping the
    password cannot fix it. Without a distinct type, a JSON parse error is a
    ValueError and falls into the wrong-password handler. A user sent after their password
    retries it, concludes they forgot it, and may abandon a recoverable
    wallet. The remedies are file-level: the .previous copy each save keeps,
    another backup, or re-importing the seed phrase.
    """

# Secure file permissions (Unix only)
SECURE_FILE_MODE = 0o600  # Owner read/write only


def _derive_private_key(seed_phrase: str, path: str) -> bytes:
    """Derive a private key from a seed phrase at the given derivation path.

    Every derivation goes through here so that no exception raised by the
    library reaches a caller with its message intact. eth_account puts the
    input in the message on at least one path - a phrase whose words are valid
    in more than one BIP-39 wordlist raises "Word(s) are valid in multiple
    languages: <the whole phrase>" - and Vault's callers format exceptions with
    str(e) straight into logs and API error responses. Which future library
    messages carry the input is not Vault's to know, so none of them cross this
    boundary.

    `from None` matters as much as the new message: it drops the original from
    the chain, so the global excepthook cannot print it as context.

    The two steps are caught separately so the caller still learns which one
    failed. Only the first is handed a secret.
    """
    try:
        seed_bytes = seed_from_mnemonic(seed_phrase, passphrase="")
    except Exception as e:
        raise ValueError(
            f"Could not read this seed phrase ({type(e).__name__}). "
            "Check that it is a valid BIP-39 English phrase."
        ) from None

    try:
        return key_from_seed(seed_bytes, path)
    except Exception as e:
        # The path holds no secret - it is the stored derivation template with
        # an index substituted in - so naming it is both safe and the only way
        # the caller can tell a bad index from a bad phrase.
        raise ValueError(
            f"Derivation path {path} is not valid ({type(e).__name__})."
        ) from None


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
# Key Derivation
# ============================================

def derive_key(password: str, salt: bytes) -> bytes:
    """
    Derive an encryption key from password using Argon2id.

    Argon2id is memory-hard, so each guess costs an attacker the full memory
    footprint as well as the time - that is what makes guessing expensive.

    Cost at the parameters above, measured rather than estimated: ~280ms and
    256MB per guess on a current desktop CPU, so roughly 3.5 guesses per second
    per core. If the parameters change, measure again and restate the figure
    here - an estimate stated as fact here misleads about how much protection a
    weak password actually has.
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


# Wallet types
WALLET_TYPE_SOFTWARE = "software"
WALLET_TYPE_LEDGER = "ledger"

#: Display names per hardware wallet_type. Add a row when adding a brand.
DEVICE_LABELS = {
    WALLET_TYPE_LEDGER: "Ledger",
}

#: Address ID prefix per hardware wallet_type (L001, ...). Existing wallets
#: already contain these IDs, so a brand's prefix must never change.
DEVICE_ID_PREFIXES = {
    WALLET_TYPE_LEDGER: "L",
}


@dataclass
class AddressEntry:
    """An address in the wallet.

    Supports three types of addresses:
    - Seed-derived: seed_id + index set, derives from HD seed
    - Imported: encrypted_pkey set, raw private key
    - Hardware: wallet_type names the device (e.g. "ledger"), device_path set,
      signing happens on the device

    For hardware addresses, no secrets are stored - the device holds the keys.

    The device fields are deliberately brand-neutral: every hardware wallet is
    identified by a derivation path and a path convention, so a second brand
    needs a new wallet_type value rather than new columns.
    """
    id: str                    # A001, A002, L001, etc.
    name: str                  # User-friendly name
    address: str               # 0x... address
    seed_id: Optional[str] = None     # S001, S002, or None for imported/hardware
    index: Optional[int] = None       # Derivation index, or None for imported/hardware
    encrypted_pkey: Optional[str] = None  # For imported keys only (hex)
    pkey_iv: Optional[str] = None         # AES IV for imported key
    pkey_tag: Optional[str] = None        # AES tag for imported key
    # Hardware wallet fields
    wallet_type: str = WALLET_TYPE_SOFTWARE  # "software" or a device name e.g. "ledger"
    device_path: Optional[str] = None        # Full derivation path on the device
    device_path_type: Optional[str] = None   # "ledger_live", "bip44", "legacy_mew", "custom"

    @property
    def is_hardware(self) -> bool:
        """True if signing requires a physical device."""
        return self.wallet_type != WALLET_TYPE_SOFTWARE

    @property
    def is_ledger(self) -> bool:
        """True if this address is backed by a Ledger specifically.

        Prefer is_hardware for routing; this is for the few places that need
        to know the brand (labels, device drivers).
        """
        return self.wallet_type == WALLET_TYPE_LEDGER

    @property
    def is_software(self) -> bool:
        """True if this address is backed by a software key (seed or imported)."""
        return self.wallet_type == WALLET_TYPE_SOFTWARE

    @property
    def device_label(self) -> str:
        """Human-readable device name, e.g. "Ledger". Empty for software."""
        if not self.is_hardware:
            return ""
        return DEVICE_LABELS.get(self.wallet_type, self.wallet_type.title())

    def to_dict(self) -> dict:
        # Drop None values so each entry carries only the fields its kind uses:
        # a seed address has no pkey fields, a hardware address has no secrets.
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "AddressEntry":
        return cls(**data)


# ============================================
# Master Key
# ============================================
#
# A wallet has one randomly generated master key. Every secret the wallet holds
# - seed phrases, imported private keys - and every secret that belongs to the
# wallet but is stored elsewhere (agent credentials) is encrypted under it.
#
# The password never encrypts a secret directly; it only wraps this key. So a
# password change re-wraps exactly one value and cannot leave a secret behind
# under the old password. Unwrapping is also what proves the password correct,
# and the KDF parameters travel with the wrapping, so they can be retuned later
# without stranding wallets written by an earlier build.


#: Shortest password a wallet may be locked with. Matches Rabby's floor.
#:
#: A length floor only - no composition rules. Requiring a capital, a digit and a
#: symbol is deprecated practice (NIST SP 800-63B): it annoys people into
#: predictable shapes like "Password1!", which lowers real entropy rather than
#: raising it. Whitespace counts as characters and nothing is stripped, so a
#: pasted passphrase arrives exactly as the user's password manager produced it.
#:
#: This floor removes indefensibly short passwords. It does not make a short-but-
#: guessable one safe: how long an attacker needs is set by how predictable the
#: password is, and only the person choosing it controls that.
MIN_PASSWORD_LENGTH = 8


class WeakPasswordError(ValueError):
    """The password is too short to lock a wallet with."""


def validate_wallet_password(password: str) -> None:
    """Check a password is acceptable for locking a wallet.

    Called wherever a password becomes the thing that wraps the master key -
    creation and change - so no interface can forget it. Deliberately NOT called
    when opening a wallet: the file is already encrypted with whatever password
    was set, and enforcing a policy at that point would lock people out of their
    own wallets the moment the policy changed.

    NO_PASSWORD_SENTINEL is rejected by name. In legacy wallet files it
    marks "unencrypted", and it travels in the same channel as real passwords,
    so accepting it as a password would quietly encrypt a wallet under a
    published magic value - and treating it as the legacy switch would quietly
    encrypt nothing at all. Legacy files that carry it still open; see load().

    Raises:
        WeakPasswordError: if the password is shorter than MIN_PASSWORD_LENGTH
            or is the reserved NO_PASSWORD_SENTINEL value
    """
    if password == NO_PASSWORD_SENTINEL:
        raise WeakPasswordError(
            "This value is reserved and cannot be used as a wallet password. "
            "Unencrypted wallets can no longer be created; choose a real "
            f"password of at least {MIN_PASSWORD_LENGTH} characters.")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def encrypt_with_key(key: bytes, plaintext: str) -> tuple[str, str, str]:
    """Encrypt a string under a raw AES-256 key. Returns hex (ciphertext, iv, tag)."""
    iv = secrets.token_bytes(AES_IV_SIZE)
    blob = AESGCM(key).encrypt(iv, plaintext.encode('utf-8'), None)
    return blob[:-16].hex(), iv.hex(), blob[-16:].hex()


def decrypt_with_key(key: bytes, ciphertext_hex: str, iv_hex: str, tag_hex: str) -> str:
    """Decrypt a string produced by encrypt_with_key. Raises on tampering."""
    blob = bytes.fromhex(ciphertext_hex) + bytes.fromhex(tag_hex)
    return AESGCM(key).decrypt(bytes.fromhex(iv_hex), blob, None).decode('utf-8')


def wrap_data_key(data_key: bytes, password: str) -> dict:
    """Wrap the master key with a password-derived key, for storage."""
    salt = secrets.token_bytes(16)
    kek = derive_key(password, salt)
    ciphertext, iv, tag = encrypt_with_key(kek, data_key.hex())
    return {
        "algorithm": "argon2id",
        "salt": salt.hex(),
        "time_cost": ARGON2_TIME_COST,
        "memory_cost": ARGON2_MEMORY_COST,
        "parallelism": ARGON2_PARALLELISM,
        "ciphertext": ciphertext,
        "iv": iv,
        "tag": tag,
    }


def unwrap_data_key(wrapped: dict, password: str) -> bytes:
    """Recover the master key from its stored wrapping.

    KDF parameters are read from the wrapping rather than from the constants
    above, so a wallet written with different settings still opens.

    Raises ValueError if the password is wrong or the wrapping is damaged.
    """
    try:
        kek = hash_secret_raw(
            secret=password.encode('utf-8'),
            salt=bytes.fromhex(wrapped["salt"]),
            time_cost=int(wrapped["time_cost"]),
            memory_cost=int(wrapped["memory_cost"]),
            parallelism=int(wrapped["parallelism"]),
            hash_len=ARGON2_HASH_LEN,
            type=Type.ID,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("Wallet key wrapping is malformed") from e
    try:
        return bytes.fromhex(decrypt_with_key(
            kek, wrapped["ciphertext"], wrapped["iv"], wrapped["tag"]))
    except Exception as e:
        raise ValueError("Wrong password or corrupted wallet") from e


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

    def __init__(self, data_key: bytes, encrypted: bool):
        """Initialize wallet (internal use - use create() or load()).

        `data_key` is the master key described above. `_wrapped_key` is its
        stored wrapping, kept so the wallet can be saved again without holding
        on to the password after unlock.
        """
        self._data_key: Optional[bytes] = data_key
        self._encrypted = encrypted
        self._wrapped_key: Optional[dict] = None
        self._seeds: dict[str, SeedEntry] = {}       # id -> SeedEntry
        self._addresses: dict[str, AddressEntry] = {}  # id -> AddressEntry
        self._decrypted_seeds: dict[str, str] = {}   # id -> plaintext phrase
        self._decrypted_pkeys: dict[str, bytes] = {} # address_id -> private key bytes
        self._created_at: str = ""
        self._next_seed_num = 1
        self._next_addr_num = 1
        self._next_device_num = 1  # For hardware addresses: L001, L002, etc.
        # Set by change_password, cleared by the save that follows it. The
        # previous-version copy is the file as it was, so after a re-key it
        # still opens with the password being replaced - see save().
        self._rekeyed_since_save = False

    @property
    def is_encrypted(self) -> bool:
        """True if this wallet is protected by a password."""
        return self._encrypted

    @property
    def data_key(self) -> bytes:
        """The master key, for encrypting data belonging to this wallet.

        Agent credentials are encrypted with this rather than with the password,
        so rotating the password cannot orphan them.

        Raises ValueError if the wallet is locked.
        """
        if self._data_key is None:
            raise ValueError("Wallet is locked")
        return self._data_key

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
        """Create a new empty wallet with a fresh random master key.

        Every new wallet is encrypted. Unencrypted wallets can only reach this
        process as legacy files through load().

        Raises WeakPasswordError if the password is too short or reserved.
        """
        validate_wallet_password(password)
        data_key = secrets.token_bytes(AES_KEY_SIZE)
        wallet = cls(data_key, encrypted=True)
        wallet._wrapped_key = wrap_data_key(data_key, password)
        wallet._created_at = datetime.now(timezone.utc).isoformat()
        return wallet

    def _encrypt_data(self, plaintext: str) -> tuple[str, str, str]:
        """Encrypt data under the master key. Returns hex (ciphertext, iv, tag)."""
        return encrypt_with_key(self.data_key, plaintext)

    def _decrypt_data(self, ciphertext_hex: str, iv_hex: str, tag_hex: str) -> str:
        """Decrypt data encrypted under the master key."""
        return decrypt_with_key(self.data_key, ciphertext_hex, iv_hex, tag_hex)

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

    def _next_device_id(self, wallet_type: str) -> str:
        """Generate the next hardware address ID for a device type (L001, ...)."""
        prefix = DEVICE_ID_PREFIXES.get(wallet_type)
        if not prefix:
            raise ValueError(f"No address ID prefix registered for wallet type {wallet_type!r}")

        while True:
            did = f"{prefix}{self._next_device_num:03d}"
            self._next_device_num += 1
            if did not in self._addresses:
                return did

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

        private_key = _derive_private_key(seed_phrase, path)
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

    def add_hardware_address(
        self,
        address: str,
        path: str,
        path_type: str,
        name: str = None,
        wallet_type: str = WALLET_TYPE_LEDGER
    ) -> str:
        """
        Add a hardware wallet address.

        No secrets are stored - the device holds the private keys.

        Args:
            address: The 0x... address derived from the device
            path: Full derivation path (e.g., "m/44'/60'/0'/0/0")
            path_type: Path type ("ledger_live", "bip44", "legacy_mew", "custom")
            name: User-friendly name (default: "<Device> #N")
            wallet_type: Which device this came from (default: Ledger)

        Returns:
            Address ID (e.g., "L001")
        """
        # Validate custom name if provided
        if name:
            validate_name(name, "Address name")

        # Normalize address
        address = address.strip()
        if not address.startswith("0x"):
            address = "0x" + address

        # Check for duplicate
        for addr in self._addresses.values():
            if addr.address.lower() == address.lower():
                return addr.id  # Already exists

        addr_id = self._next_device_id(wallet_type)

        # Count existing addresses from this device for the default name
        label = DEVICE_LABELS.get(wallet_type, wallet_type.title())
        device_count = len(self.get_hardware_addresses(wallet_type))
        addr_name = name or f"{label} #{device_count + 1}"

        entry = AddressEntry(
            id=addr_id,
            name=addr_name,
            address=address,
            wallet_type=wallet_type,
            device_path=path,
            device_path_type=path_type
        )
        self._addresses[addr_id] = entry

        return addr_id

    def get_hardware_addresses(self, wallet_type: str = None) -> list[AddressEntry]:
        """Get hardware wallet addresses, optionally for one device type only."""
        return [
            a for a in self._addresses.values()
            if a.is_hardware and (wallet_type is None or a.wallet_type == wallet_type)
        ]

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
        Get private key for a software address.

        Returns the raw private key bytes for signing.

        Raises:
            ValueError: If the wallet is locked, the address is unknown, or the
                address is backed by a hardware device
        """
        # Locking clears the decrypted seeds, so a seed-derived address would
        # otherwise fail on a missing dictionary key and surface to the caller as
        # a bare KeyError naming a seed id - true, but useless to act on.
        if self._data_key is None:
            raise ValueError("Wallet is locked")

        if address_id not in self._addresses:
            raise ValueError(f"Unknown address: {address_id}")

        addr = self._addresses[address_id]

        # Hardware addresses have no private key here - the device holds it
        if addr.is_hardware:
            raise ValueError(
                f"Cannot get private key for {addr.device_label} address {address_id}. "
                "Signing must happen on the device."
            )

        if addr.seed_id is not None:
            # Derived from seed
            seed_phrase = self._decrypted_seeds[addr.seed_id]
            seed_entry = self._seeds[addr.seed_id]
            path = seed_entry.derivation_path.format(addr.index)
            return _derive_private_key(seed_phrase, path)
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

    def verify_password(self, password: str) -> bool:
        """Check a password against this wallet without changing anything.

        Unwraps the stored master key and compares it to the one in use, so the
        answer comes from the wrapping itself rather than from a retained copy
        of the password.
        """
        if not self._encrypted:
            return password == NO_PASSWORD_SENTINEL
        if not self._wrapped_key:
            return False
        try:
            return unwrap_data_key(self._wrapped_key, password) == self.data_key
        except ValueError:
            return False

    def change_password(self, new_password: str) -> None:
        """Change the wallet password.

        Only the wrapping of the master key changes. Seeds, imported keys and
        agent credentials are encrypted under that key rather than under the
        password, so none of them are touched and none can be left behind.

        The wallet must be saved after calling this.

        Encryption can only be added or re-keyed here, never removed: this is
        also the path that upgrades a legacy unencrypted wallet when the user
        sets a password on it.

        Args:
            new_password: New password

        Raises:
            WeakPasswordError: if the new password is too short or reserved
        """
        validate_wallet_password(new_password)
        self._encrypted = True
        self._wrapped_key = wrap_data_key(self.data_key, new_password)
        self._rekeyed_since_save = True

    # ============================================
    # File Operations
    # ============================================

    def save(self, filepath: str | Path) -> None:
        """Write the wallet so it is never seen partially written or unprotected.

        The same durable-write sequence PolicyStore uses, and for the same
        reasons - written out in full there, in models/store.py.

        Two details matter more here than they do for a policy file:

        Permissions go on before the move, not after. Setting them afterwards
        leaves a window in which the wallet exists at its real path with default
        permissions, which for the file holding every seed and key is the wrong
        window to leave open.

        The contents are flushed all the way to disk before the move, so a power
        loss cannot leave a wallet whose directory entry points at unwritten
        blocks. A wallet that saved and then vanished is unrecoverable in a way
        an ordinary data file is not - the seed is not written down anywhere
        else.

        The temporary file is unique per save and hidden. The old name was
        `filepath.with_suffix(".tmp")`, which is fixed - two saves at once wrote
        to one path - and, because with_suffix replaces rather than appends,
        `my.backup.wallet` produced `my.backup.tmp`. It also sat visible in the
        data directory, which the settings watcher observes.
        """
        filepath = Path(filepath)

        wallet_data = {
            "version": WALLET_FORMAT_VERSION,
            "encrypted": self._encrypted,
            "created_at": self._created_at,
            "seeds": [s.to_dict() for s in self._seeds.values()],
            "addresses": [a.to_dict() for a in self._addresses.values()]
        }
        if self._encrypted:
            wallet_data["wrapped_key"] = self._wrapped_key
        else:
            # No password: the master key is stored in the clear. That is exactly
            # as readable as the secrets it protects would be without it.
            wallet_data["data_key"] = self.data_key.hex()

        filepath.parent.mkdir(parents=True, exist_ok=True)

        # Alongside the target, because a move is only atomic within one
        # filesystem.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(filepath.parent), prefix=f".{filepath.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(wallet_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            set_secure_permissions(tmp_path)
            # The version being replaced is kept as `<name>.previous` - the
            # only file-level recovery a damaged wallet has. Written after the
            # new contents are safely on disk, so a failed save cannot cost
            # the backup as well.
            #
            # Except after a password change. The copy is the file as it was,
            # so it still opens with the password being replaced, and it holds
            # the same master key - which means the same seeds. Someone who
            # changes their password because the old one leaked would be left
            # with a fully readable wallet under the leaked password. The copy
            # is dropped instead, and the next ordinary save makes a fresh one
            # under the new password.
            if filepath.exists():
                if self._rekeyed_since_save:
                    self._discard_previous_version(filepath)
                else:
                    self._keep_previous_version(filepath)
            os.replace(tmp_path, filepath)
            # Make the renames durable, not just atomic - one directory fsync
            # covers the .previous rename above too, since both entries live
            # in this directory. See fsync_dir for the power-loss window this
            # closes.
            fsync_dir(filepath.parent)
            # Only once the new file is in place: a save that fails leaves the
            # old wallet live, so it must keep its re-key pending too.
            self._rekeyed_since_save = False
        except BaseException:
            # A failed save must not leave a partial wallet lying in the data
            # directory under a name that looks like a backup.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _discard_previous_version(filepath: Path) -> None:
        """Remove `<name>.previous`, which a re-key has made unsafe to keep.

        Best effort, like keeping it: a wallet save must not fail because the
        old copy could not be removed. A failure is logged loudly, because what
        is left behind still opens with the previous password.
        """
        previous = filepath.with_name(filepath.name + ".previous")
        try:
            previous.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(
                "Could not remove the previous-version copy of %s after a "
                "password change - it still opens with the old password: %s",
                filepath.name, e)

    @staticmethod
    def _keep_previous_version(filepath: Path) -> None:
        """Copy the wallet as it stands to `<name>.previous`, atomically.

        Written just before each save replaces the file, so one save ago is
        always on disk. That is the recovery path CorruptedWalletFile points
        at: corruption is usually recent, and the previous version opens
        normally.

        A copy rather than a rename, so the live wallet never has a moment of
        not existing. The same durable-write sequence as the wallet itself,
        because a backup that can itself be torn by a crash is not a backup.

        Two consequences are deliberate and worth knowing:
        - The copy holds whatever the wallet held one save ago - including a
          seed removed since, under a password changed since. Recoverability
          is the point; deleting the wallet deletes the copy too.
        - It is best effort. A wallet save must not fail because its backup
          could not be written, so failures are logged and the save proceeds.
        """
        previous = filepath.with_name(filepath.name + ".previous")
        try:
            current_bytes = filepath.read_bytes()
            fd, tmp_name = tempfile.mkstemp(
                dir=str(filepath.parent), prefix=f".{previous.name}.", suffix=".tmp")
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(current_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                set_secure_permissions(tmp)
                os.replace(tmp, previous)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning(
                "Could not keep a previous-version copy of %s: %s",
                filepath.name, e)

    @staticmethod
    def _sweep_stale_temp_files(directory: Path, name: str) -> None:
        """Remove leftover atomic-write temp files for the wallet ``name``.

        `save` writes a complete wallet to a hidden temp file and renames it into
        place; a power loss or a kill between the write and the rename raises
        nothing, so `save`'s cleanup never runs and the temp is simply left on
        disk. Each one is a whole wallet, readable under the password in force
        when it was written - so one that outlives a password-change re-key
        still opens under the old, possibly leaked, password, the very thing
        `_discard_previous_version` drops `.previous` to prevent. Nothing else
        removes them, so Vault clears them when it opens the wallet.

        Only this wallet's own temp files are swept: `save` and
        `_keep_previous_version` both name theirs ``.<name>.<rand>.tmp`` and
        ``.<name>.previous.<rand>.tmp``, so both begin ``.<name>.`` - which the
        prefix below matches without touching an unrelated hidden ``.tmp`` that
        happens to share the directory (a real risk when the wallet lives in a
        folder the user also keeps other things in).

        Safe because Vault holds a single-instance lock: no other process is
        part-way through a save in this directory, so no live temp is at risk.
        `.previous` (the real backup) has no `.tmp` suffix and is left alone.
        """
        prefix = f".{name}."
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(prefix) and entry.name.endswith(".tmp"):
                try:
                    entry.unlink()
                except OSError as e:
                    logger.warning(
                        "Could not remove stale wallet temp file %s: %s",
                        entry.name, e)

    @classmethod
    def load(cls, filepath: str | Path, password: str) -> "VaultWallet":
        """Load wallet from file.

        Unwrapping the master key is the password check, so a wrong password is
        rejected whether or not this wallet happens to hold any secrets.

        Raises:
            FileNotFoundError: If wallet doesn't exist
            ValueError: If the version is unsupported, the password is wrong,
                or the file is corrupted
        """
        filepath = Path(filepath)

        # Opening the wallet is Vault's startup moment for this directory, and
        # the one chance to clear temp files a crashed save left behind before
        # they outlive a later re-key. See _sweep_stale_temp_files.
        cls._sweep_stale_temp_files(filepath.parent, filepath.name)

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise CorruptedWalletFile(
                f"the file is not readable as JSON: {e}") from e
        if not isinstance(data, dict):
            raise CorruptedWalletFile("the file does not contain a wallet record")

        version = data.get("version")
        if version != WALLET_FORMAT_VERSION:
            raise UnsupportedWalletVersion(
                f"This wallet file is format version {version}; this build of Vault "
                f"reads version {WALLET_FORMAT_VERSION}.")

        encrypted = data.get("encrypted", True)
        if not encrypted:
            # Unencrypted wallets are no longer opened by this build - only ever
            # created by an older one, and their master key sits in the file in
            # the clear. Refuse rather than open a wallet with no protection at
            # rest. The remedy is a one-time conversion: open the file in Vault
            # 0.1 (still on GitHub and PyPI), set a password to encrypt it, and
            # it opens here.
            raise UnsupportedWalletVersion(
                "This wallet file is unencrypted, which this version of Vault "
                "does not open. Open it in an earlier Vault (0.1), set a password "
                "to encrypt it, and then open it here.")

        wrapped = data.get("wrapped_key")
        if not isinstance(wrapped, dict):
            # Structural damage, not a password problem - unwrap failure
            # below is the only true wrong-password signal.
            raise CorruptedWalletFile(
                "the wallet is marked encrypted but carries no key wrapping")
        data_key = unwrap_data_key(wrapped, password)

        wallet = cls(data_key, encrypted)
        wallet._wrapped_key = wrapped
        wallet._created_at = data.get("created_at", "")

        # The master key unwrapped above, so the password is correct. A
        # decryption failure now is damage inside the payload, not a wrong
        # password - raise it as such so the caller names the real fault and
        # the recovery copy, rather than sending the user after their password.
        for seed_data in data.get("seeds", []):
            entry = SeedEntry.from_dict(seed_data)
            wallet._seeds[entry.id] = entry
            try:
                wallet._decrypted_seeds[entry.id] = wallet._decrypt_data(
                    entry.encrypted_phrase, entry.iv, entry.tag)
            except Exception as e:
                raise CorruptedWalletFile(
                    f"seed {entry.id} could not be decrypted") from e
            if entry.id.startswith("S"):
                try:
                    wallet._next_seed_num = max(wallet._next_seed_num, int(entry.id[1:]) + 1)
                except ValueError:
                    pass

        for addr_data in data.get("addresses", []):
            entry = AddressEntry.from_dict(addr_data)
            wallet._addresses[entry.id] = entry

            if entry.encrypted_pkey:
                try:
                    wallet._decrypted_pkeys[entry.id] = bytes.fromhex(wallet._decrypt_data(
                        entry.encrypted_pkey, entry.pkey_iv, entry.pkey_tag))
                except Exception as e:
                    raise CorruptedWalletFile(
                        f"address {entry.id} private key could not be decrypted") from e

            if entry.id.startswith("A"):
                try:
                    wallet._next_addr_num = max(wallet._next_addr_num, int(entry.id[1:]) + 1)
                except ValueError:
                    pass
            elif entry.id[:1] in set(DEVICE_ID_PREFIXES.values()):
                try:
                    wallet._next_device_num = max(wallet._next_device_num, int(entry.id[1:]) + 1)
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
        except (ValueError, AttributeError, OSError):
            # Damaged in any way - not UTF-8 (UnicodeDecodeError), not JSON
            # (JSONDecodeError), or not an object (AttributeError on .get).
            # Report "encrypted" so the caller routes to the unlock/load path,
            # where load_wallet raises CorruptedWalletFile and the user is shown
            # the real fault and the .previous copy - never a crash on startup.
            return True

    def lock(self) -> None:
        """Lock the wallet, dropping the master key and every decrypted secret."""
        self._decrypted_seeds.clear()
        self._decrypted_pkeys.clear()
        self._data_key = None

    def __del__(self):
        """Attempt to clear sensitive data on destruction."""
        self.lock()
