"""
Agent model and token generation.

Defines registered AI agents with two authentication modes:

1. HMAC-SHA256 (default, more secure):
   - Agent signs requests with shared secret
   - Secret never transmitted over the wire
   - Requires Python stdlib hmac/hashlib

2. Bearer Token (simpler, less secure):
   - Agent sends token directly with requests
   - Token transmitted with every request (interception risk)
   - Simpler for agents that struggle with signing
"""

import uuid
import hmac
import hashlib
import secrets
import string
from datetime import datetime, date, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

# Encryption imports (same as wallet encryption)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2.low_level import hash_secret_raw, Type


# ============================================
# Encryption Constants (same as wallet/crypto.py)
# ============================================

ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32  # 256 bits for AES-256
AES_IV_SIZE = 12  # 96 bits (recommended for GCM)


def generate_agent_id() -> str:
    """Generate a short, readable agent ID (6 chars: 3 letters + 3 digits)."""
    letters = ''.join(secrets.choice(string.ascii_uppercase) for _ in range(3))
    digits = ''.join(secrets.choice(string.digits) for _ in range(3))
    return letters + digits


def generate_agent_token() -> tuple[str, str]:
    """
    Generate an HMAC shared secret for agent authentication.

    Returns:
        (agent_token, shared_secret_hex) - both contain the same secret
        agent_token has AT_ prefix for user display
        shared_secret_hex is the raw secret for encryption/storage

    The shared secret is 256 bits (32 bytes) for HMAC-SHA256.
    Both the agent and server use this same secret - the agent to sign
    requests, and the server to verify them.
    """
    shared_secret = secrets.token_bytes(32)  # 256-bit random key
    shared_secret_hex = shared_secret.hex()
    agent_token = f"AT_{shared_secret_hex}"
    return agent_token, shared_secret_hex


def verify_agent_hmac(
    shared_secret_hex: str,
    message: bytes,
    signature_hex: str
) -> bool:
    """
    Verify an HMAC-SHA256 signature from an agent.

    Args:
        shared_secret_hex: Agent's shared secret (hex string)
        message: The signed message bytes
        signature_hex: The HMAC signature (hex string)

    Returns:
        True if valid, False otherwise
    """
    try:
        expected = hmac.new(
            bytes.fromhex(shared_secret_hex),
            message,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature_hex)
    except ValueError:
        return False


def verify_bearer_token(stored_hash: str, provided_token: str) -> bool:
    """
    Verify a bearer token against its stored hash.

    Args:
        stored_hash: SHA-256 hash of the token (stored in agent.auth_key)
        provided_token: The token provided by the agent (e.g., "AT_abc123...")

    Returns:
        True if the token matches the stored hash
    """
    try:
        token_hash = hashlib.sha256(provided_token.encode()).hexdigest()
        return hmac.compare_digest(stored_hash, token_hash)
    except Exception:
        return False


def hash_bearer_token(token: str) -> str:
    """
    Hash a bearer token for storage.

    Args:
        token: The bearer token (e.g., "AT_abc123...")

    Returns:
        SHA-256 hash of the token (hex string)
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ============================================
# Agent Secret Encryption
# ============================================

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive an encryption key from password using Argon2id."""
    return hash_secret_raw(
        secret=password.encode('utf-8'),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID
    )


def encrypt_agent_secret(shared_secret_hex: str, password: str, agent_id: str) -> tuple[str, str, str, str]:
    """
    Encrypt an agent's shared secret with a password.

    Args:
        shared_secret_hex: The shared secret to encrypt (hex string)
        password: The encryption password (typically wallet password)
        agent_id: The agent ID, used as AAD to bind ciphertext to this agent

    Returns:
        (encrypted_hex, iv_hex, tag_hex, salt_hex)
    """
    salt = secrets.token_bytes(16)
    key = _derive_key(password, salt)
    iv = secrets.token_bytes(AES_IV_SIZE)

    # Use agent_id as Associated Authenticated Data (AAD) for domain separation
    # This ensures the ciphertext can only be decrypted for this specific agent
    aad = agent_id.encode('utf-8')

    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(iv, shared_secret_hex.encode('utf-8'), aad)

    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]

    return ciphertext.hex(), iv.hex(), tag.hex(), salt.hex()


def decrypt_agent_secret(
    encrypted_hex: str,
    iv_hex: str,
    tag_hex: str,
    salt_hex: str,
    password: str,
    agent_id: str
) -> str:
    """
    Decrypt an agent's shared secret.

    Args:
        encrypted_hex: Encrypted secret (hex string)
        iv_hex: AES-GCM IV (hex string)
        tag_hex: AES-GCM auth tag (hex string)
        salt_hex: Argon2id salt (hex string)
        password: The encryption password
        agent_id: The agent ID, used as AAD (must match encryption)

    Returns:
        The decrypted shared secret (hex string)

    Raises:
        Exception if password is wrong, agent_id doesn't match, or data is corrupted
    """
    salt = bytes.fromhex(salt_hex)
    key = _derive_key(password, salt)

    ciphertext = bytes.fromhex(encrypted_hex)
    iv = bytes.fromhex(iv_hex)
    tag = bytes.fromhex(tag_hex)
    ciphertext_and_tag = ciphertext + tag

    # Use agent_id as AAD - must match what was used during encryption
    aad = agent_id.encode('utf-8')

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext_and_tag, aad)

    return plaintext.decode('utf-8')


def generate_intent_mandate(
    agent: "Agent",
    policy: "SpendPolicy",
    wallet_address: str,
    signer_private_key: Optional[bytes] = None
) -> dict:
    """
    Generate an AP2-compatible IntentMandate VDC for an agent.

    The IntentMandate documents the user's authorization for an agent
    to make payments within the specified policy limits.

    Args:
        agent: The agent being commissioned
        policy: The spend policy assigned to the agent
        wallet_address: The wallet address for signing payments
        signer_private_key: Optional private key to sign the mandate

    Returns:
        IntentMandate document as a dict
    """
    import json
    from datetime import datetime, timezone

    mandate = {
        "type": "IntentMandate",
        "version": "ap2.primer/v0.1",
        "id": str(uuid.uuid4()),
        "issuedAt": datetime.now(timezone.utc).isoformat(),

        # Agent authorization
        # Privacy: We omit agent.name and agent.code to avoid leaking organizational info.
        # Only include id (lookup key) and auth fingerprint (for identification).
        "agent": {
            "id": agent.id,
            "authKeyFingerprint": hashlib.sha256(agent.auth_key.encode()).hexdigest()[:16],
        },

        # Payment authorization (maps to SpendPolicy)
        # Privacy: We omit policyName and domain restrictions to avoid leaking
        # organizational structure and business relationships.
        "authorization": {
            "policyId": policy.id,

            # Spending limits (ERC-20 style: symbol + decimals + raw values)
            "limits": {
                "currency": "USDG",
                "decimals": 6,
                "dailyLimit": policy.daily_limit_micro,
                "perRequestMax": policy.per_request_max_micro,
                "autoApproveBelow": policy.auto_approve_below_micro,
            },

            # Network restrictions (required for chain validation)
            "networks": [f"eip155:{chain_id}" for chain_id in (policy.networks or [])],
        },

        # Signing wallet
        "wallet": {
            "address": wallet_address,
        },

        # Issuer (Vault app)
        "issuer": {
            "type": "VaultDesktop",
            "version": "1.0",
        },
    }

    # Sign the mandate if private key is provided
    if signer_private_key:
        try:
            import logging
            from eth_account import Account
            from eth_account.messages import encode_defunct

            # Create canonical JSON of mandate content (sorted keys for reproducibility)
            mandate_json = json.dumps(mandate, separators=(',', ':'), sort_keys=True)

            # Sign using EIP-191 "Ethereum Signed Message" format
            # This adds the prefix "\x19Ethereum Signed Message:\n{len}" before signing
            message = encode_defunct(text=mandate_json)
            account = Account.from_key(signer_private_key)
            signed = account.sign_message(message)

            mandate["signature"] = {
                "type": "EIP191",
                "signer": account.address,
                "value": "0x" + signed.signature.hex(),
            }
        except Exception as e:
            # If signing fails, mandate is still valid but unsigned
            logging.getLogger(__name__).warning(f"Failed to sign IntentMandate: {e}")

    return mandate


# Import SpendPolicy type for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .policy import SpendPolicy


@dataclass
class Agent:
    """
    A registered AI agent with configurable authentication.

    Authentication modes:
    - "hmac": Agent signs requests with HMAC-SHA256 (default, more secure)
      auth_key stores encrypted shared secret
    - "bearer": Agent sends token directly (simpler, less secure)
      auth_key stores sha256 hash of the token for verification

    For HMAC mode, the secret is encrypted at rest using AES-256-GCM with
    Argon2id key derivation, using the wallet password as the encryption key.
    """
    id: str                          # Short agent ID for API use (e.g., "ABC123")
    name: str
    code: str                        # Internal UUID for storage
    auth_key: str                    # HMAC: encrypted shared secret | Bearer: sha256(token)
    status: str                      # uncommissioned | active | suspended | limit_reached
    created_at: str
    auth_mode: str = "hmac"          # "hmac" (default) or "bearer"

    # Encryption metadata for auth_key (only used in HMAC mode)
    auth_key_iv: Optional[str] = None      # AES-GCM IV (hex)
    auth_key_tag: Optional[str] = None     # AES-GCM auth tag (hex)
    auth_key_salt: Optional[str] = None    # Argon2id salt (hex)

    # Commission settings (None if uncommissioned)
    policy_id: Optional[str] = None
    wallet_address: Optional[str] = None  # Ethereum address for signing

    # Spending tracking (x402 payments)
    spent_today_micro: int = 0  # Micro-USDG (6 decimals: 1_000_000 = $1.00)
    last_reset_date: str = ""

    # Trading volume tracking
    trading_volume_today_usd: float = 0.0  # USD volume traded today
    last_trading_reset_date: str = ""      # Calendar date for daily reset

    # AP2 IntentMandate VDC (optional, generated on commission)
    intent_mandate: Optional[dict] = None

    @classmethod
    def create(
        cls,
        name: str,
        encrypted_auth_key: str,
        auth_key_iv: Optional[str] = None,
        auth_key_tag: Optional[str] = None,
        auth_key_salt: Optional[str] = None,
        auth_mode: str = "hmac",
        agent_id: Optional[str] = None
    ) -> "Agent":
        """
        Create a new uncommissioned agent.

        Args:
            name: Agent display name
            encrypted_auth_key: For HMAC: encrypted shared secret | For Bearer: sha256(token)
            auth_key_iv: AES-GCM IV (hex) - required for HMAC mode
            auth_key_tag: AES-GCM auth tag (hex) - required for HMAC mode
            auth_key_salt: Argon2id salt (hex) - required for HMAC mode
            auth_mode: "hmac" (default, more secure) or "bearer" (simpler, less secure)
            agent_id: Optional pre-generated agent ID (required for HMAC mode AAD)
        """
        return cls(
            id=agent_id if agent_id else generate_agent_id(),
            name=name,
            code=str(uuid.uuid4()),
            auth_key=encrypted_auth_key,
            status="uncommissioned",
            created_at=datetime.now(timezone.utc).isoformat(),
            last_reset_date=date.today().isoformat(),
            auth_mode=auth_mode,
            auth_key_iv=auth_key_iv,
            auth_key_tag=auth_key_tag,
            auth_key_salt=auth_key_salt,
        )

    def commission(self, policy_id: str, wallet_address: str) -> None:
        """Commission an agent with a spend policy and wallet."""
        self.policy_id = policy_id
        self.wallet_address = wallet_address
        self.status = "active"

    def suspend(self) -> None:
        """Suspend an agent."""
        self.status = "suspended"

    def activate(self) -> None:
        """Reactivate a suspended agent."""
        if self.policy_id:
            self.status = "active"

    def reset_daily_spend(self) -> None:
        """Reset daily spending (call at midnight)."""
        self.spent_today_micro = 0
        self.last_reset_date = date.today().isoformat()
        if self.status == "limit_reached":
            self.status = "active"

    def add_spend(self, micro: int) -> None:
        """Record spending in micro-USDG (6 decimals)."""
        self.spent_today_micro += micro

    def reset_daily_trading_volume(self) -> None:
        """Reset daily trading volume (call at midnight)."""
        self.trading_volume_today_usd = 0.0
        self.last_trading_reset_date = date.today().isoformat()

    def add_trading_volume(self, usd_amount: float) -> None:
        """Record trading volume in USD."""
        self.trading_volume_today_usd += usd_amount

    def format_trading_volume_today(self) -> str:
        """Format today's trading volume as USD."""
        return f"${self.trading_volume_today_usd:.2f}"

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return asdict(self)

    # Valid status values
    VALID_STATUSES = ("uncommissioned", "active", "suspended", "limit_reached")

    @classmethod
    def from_dict(cls, data: dict) -> "Agent":
        """Create from dictionary with input validation."""
        # Validate critical fields to prevent data tampering
        spent = data.get("spent_today_micro", 0)
        if not isinstance(spent, int) or spent < 0:
            raise ValueError(f"spent_today_micro must be non-negative integer, got {spent}")

        # Validate trading volume (default to 0 if missing)
        trading_vol = data.get("trading_volume_today_usd", 0.0)
        if not isinstance(trading_vol, (int, float)) or trading_vol < 0:
            raise ValueError(f"trading_volume_today_usd must be non-negative number, got {trading_vol}")
        data["trading_volume_today_usd"] = float(trading_vol)

        # Ensure trading reset date exists (default to empty if missing)
        if "last_trading_reset_date" not in data:
            data["last_trading_reset_date"] = ""

        status = data.get("status", "")
        if status not in cls.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {cls.VALID_STATUSES}")

        return cls(**data)

    def format_spent_today(self) -> str:
        """Format today's spending as USDG with 6 decimal precision."""
        return f"{self.spent_today_micro / 1_000_000:.6f} USDG"

    def decrypt_auth_key(self, password: str) -> str:
        """
        Decrypt the agent's auth key (shared secret) using the wallet password.

        Returns:
            The decrypted shared secret (hex string) for HMAC verification

        Raises:
            ValueError if encryption metadata is missing
            Exception if password is wrong or agent_id doesn't match
        """
        if not self.auth_key_iv or not self.auth_key_tag or not self.auth_key_salt:
            raise ValueError("Agent auth key encryption metadata is missing")

        return decrypt_agent_secret(
            self.auth_key,
            self.auth_key_iv,
            self.auth_key_tag,
            self.auth_key_salt,
            password,
            self.id
        )

