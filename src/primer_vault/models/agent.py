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
from datetime import datetime, date, timedelta, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..wallet.crypto import AES_IV_SIZE
from ..version import __version__


def encrypt_with_key_aad(key: bytes, plaintext: str, aad: str) -> tuple[str, str, str]:
    """Encrypt under a raw key, binding `aad` so the ciphertext is not portable."""
    iv = secrets.token_bytes(AES_IV_SIZE)
    blob = AESGCM(key).encrypt(iv, plaintext.encode('utf-8'), aad.encode('utf-8'))
    return blob[:-16].hex(), iv.hex(), blob[-16:].hex()


def decrypt_with_key_aad(key: bytes, ciphertext_hex: str, iv_hex: str,
                         tag_hex: str, aad: str) -> str:
    """Decrypt a value produced by encrypt_with_key_aad."""
    blob = bytes.fromhex(ciphertext_hex) + bytes.fromhex(tag_hex)
    return AESGCM(key).decrypt(
        bytes.fromhex(iv_hex), blob, aad.encode('utf-8')).decode('utf-8')


#: Least time that must pass before an allowance renews.
#:
#: The reset is deliberately keyed to the local day, because "my daily limit"
#: means the user's day - a limit renewing at four in the afternoon because that
#: is midnight UTC reads as broken. Timestamps stay UTC, since a record has to be
#: comparable to a chain and to other machines.
#:
#: A local day is 23 to 25 hours, so requiring 20 clears every legitimate one
#: while refusing the shortcuts: a clock set back, or a laptop carried across
#: enough timezones, would otherwise hand out a second allowance the same day.
MIN_HOURS_BETWEEN_RESETS = 20


def daily_allowance_is_due(last_date: str, last_at: str) -> bool:
    """True if a new daily allowance should start now.

    Two conditions, and both are needed:

    - the local date has moved *forward* past the one recorded. Comparing for
      mere inequality also fired when the date moved back, so a clock correction
      or a flight west renewed the allowance;
    - at least MIN_HOURS_BETWEEN_RESETS have passed since the last renewal. The
      date alone cannot see this, and without it a flight east crosses midnight
      early and renews after a few hours.

    Args:
        last_date: local date of the last reset, ISO (may be empty)
        last_at: UTC instant of the last reset, ISO (may be empty)
    """
    today = date.today().isoformat()
    if not last_date:
        return True
    if today <= last_date:
        return False
    if not last_at:
        # No instant recorded, so elapsed time cannot be checked. Renew rather
        # than refuse: refusing would never renew, since the instant is only
        # written by a renewal.
        return True
    try:
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_at)
    except ValueError:
        return True
    return elapsed >= timedelta(hours=MIN_HOURS_BETWEEN_RESETS)


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

def encrypt_agent_secret(shared_secret_hex: str, data_key: bytes, agent_id: str) -> tuple[str, str, str]:
    """
    Encrypt an agent's shared secret under the wallet's master key.

    The master key is used rather than the wallet password, so changing the
    password re-wraps that key and leaves this ciphertext valid.

    Args:
        shared_secret_hex: The shared secret to encrypt (hex string)
        data_key: The unlocked wallet's master key
        agent_id: The agent ID, bound in as associated data

    Returns:
        (encrypted_hex, iv_hex, tag_hex)
    """
    return encrypt_with_key_aad(data_key, shared_secret_hex, agent_id)


def decrypt_agent_secret(
    encrypted_hex: str,
    iv_hex: str,
    tag_hex: str,
    data_key: bytes,
    agent_id: str
) -> str:
    """
    Decrypt an agent's shared secret.

    Args:
        encrypted_hex: Encrypted secret (hex string)
        iv_hex: AES-GCM IV (hex string)
        tag_hex: AES-GCM auth tag (hex string)
        data_key: The unlocked wallet's master key
        agent_id: The agent ID, bound in as associated data (must match)

    Returns:
        The decrypted shared secret (hex string)

    Raises:
        Exception if the key is wrong, the agent_id does not match, or the
        data is corrupted
    """
    return decrypt_with_key_aad(data_key, encrypted_hex, iv_hex, tag_hex, agent_id)


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

        # Issuer (Vault app). This is the app's version - the schema's own
        # version is the "ap2.primer/v0.1" above, and the two move separately.
        "issuer": {
            "type": "VaultDesktop",
            "version": __version__,
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

    For HMAC mode, the secret is encrypted at rest using AES-256-GCM under the
    wallet's master key, with the agent ID bound in as associated data.
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

    # Commission settings (None if uncommissioned)
    policy_id: Optional[str] = None
    wallet_address: Optional[str] = None  # Ethereum address for signing

    # Spending tracking (x402 payments)
    spent_today_micro: int = 0  # Micro-USDG (6 decimals: 1_000_000 = $1.00)
    last_reset_date: str = ""   # Local date of the last reset
    last_reset_at: str = ""     # UTC instant of the last reset

    # Trading volume tracking
    trading_volume_today_usd: float = 0.0  # USD volume traded today
    last_trading_reset_date: str = ""      # Local date of the last reset
    last_trading_reset_at: str = ""        # UTC instant of the last reset

    # DeFi operation tracking. A count, not a value: the DeFi lane bounds its
    # money with a limit on the position itself, read from chain, and this
    # bounds gas instead - a deposit costs ~369,000 of it, and a loop that
    # deposits and withdraws moves no net value while draining the wallet.
    defi_ops_today: int = 0                # Deposits + withdrawals today
    last_defi_reset_date: str = ""         # Local date of the last reset
    last_defi_reset_at: str = ""           # UTC instant of the last reset

    #: Venues this agent has supplied to, ever. Vault addresses and market ids.
    #:
    #: Exposure is measured by reading positions back off the chain, which means
    #: knowing where to look. The curated list answers that while the agent is
    #: restricted to it; with the restriction off there is no list, so without
    #: this the exposure cap would stop counting at exactly the moment the venue
    #: gate is open. Appended on a successful supply and never pruned - a venue
    #: that leaves the curated set is still somewhere the money is.
    defi_venues: list[str] = field(default_factory=list)

    # AP2 IntentMandate VDC (optional, generated on commission)
    intent_mandate: Optional[dict] = None

    @classmethod
    def create(
        cls,
        name: str,
        encrypted_auth_key: str,
        auth_key_iv: Optional[str] = None,
        auth_key_tag: Optional[str] = None,
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
            last_reset_at=datetime.now(timezone.utc).isoformat(),
            auth_mode=auth_mode,
            auth_key_iv=auth_key_iv,
            auth_key_tag=auth_key_tag,
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

    def spend_allowance_is_due(self) -> bool:
        """True if this agent's payment allowance should renew now."""
        return daily_allowance_is_due(self.last_reset_date, self.last_reset_at)

    def reset_daily_spend(self) -> None:
        """Start a new daily payment allowance.

        Records both the local date the allowance belongs to and the instant it
        began, because the date alone cannot say how long ago that was.
        """
        self.spent_today_micro = 0
        self.last_reset_date = date.today().isoformat()
        self.last_reset_at = datetime.now(timezone.utc).isoformat()
        if self.status == "limit_reached":
            self.status = "active"

    def add_spend(self, micro: int) -> None:
        """Record spending in micro-USDG (6 decimals)."""
        self.spent_today_micro += micro

    def trading_allowance_is_due(self) -> bool:
        """True if this agent's trading allowance should renew now."""
        return daily_allowance_is_due(
            self.last_trading_reset_date, self.last_trading_reset_at)

    def reset_daily_trading_volume(self) -> None:
        """Start a new daily trading allowance."""
        self.trading_volume_today_usd = 0.0
        self.last_trading_reset_date = date.today().isoformat()
        self.last_trading_reset_at = datetime.now(timezone.utc).isoformat()

    def defi_reset_due(self) -> bool:
        """True if today's DeFi operation count should start again."""
        return daily_allowance_is_due(
            self.last_defi_reset_date, self.last_defi_reset_at)

    def reset_daily_defi_ops(self) -> None:
        """Start a new day's DeFi operation count."""
        self.defi_ops_today = 0
        self.last_defi_reset_date = date.today().isoformat()
        self.last_defi_reset_at = datetime.now(timezone.utc).isoformat()

    def add_defi_op(self, count: int = 1) -> None:
        """Record DeFi operations against today's count."""
        self.defi_ops_today += count

    def remember_defi_venue(self, venue_id: str) -> bool:
        """Note that money went here. True if this is the first time.

        Case-insensitive, because a vault address arrives however the agent
        spelled it and the same venue recorded twice would be counted twice.
        """
        if not venue_id:
            return False
        if any(v.lower() == venue_id.lower() for v in self.defi_venues):
            return False
        self.defi_venues.append(venue_id)
        return True

    def format_defi_ops_today(self) -> str:
        """Format today's DeFi operation count for display."""
        return str(self.defi_ops_today)

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

        # Absent from every agent written before the DeFi lane existed, which is
        # the normal case. A bool passes an isinstance check for int and would
        # become a count of one, so it is excluded explicitly.
        defi_ops = data.get("defi_ops_today", 0)
        if not isinstance(defi_ops, int) or isinstance(defi_ops, bool) or defi_ops < 0:
            raise ValueError(
                f"defi_ops_today must be a non-negative integer, got {defi_ops}")
        data["defi_ops_today"] = int(defi_ops)
        if "last_defi_reset_date" not in data:
            data["last_defi_reset_date"] = ""

        venues = data.get("defi_venues") or []
        if not isinstance(venues, list) or any(not isinstance(v, str) for v in venues):
            raise ValueError(f"defi_venues must be a list of strings, got {venues!r}")
        data["defi_venues"] = list(venues)

        status = data.get("status", "")
        if status not in cls.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {cls.VALID_STATUSES}")

        return cls(**data)

    def format_spent_today(self) -> str:
        """Format today's spending as USDG with 6 decimal precision."""
        return f"{self.spent_today_micro / 1_000_000:.6f} USDG"

    def decrypt_auth_key(self, data_key: bytes) -> str:
        """
        Decrypt the agent's shared secret using the wallet's master key.

        Returns:
            The decrypted shared secret (hex string) for HMAC verification

        Raises:
            ValueError if encryption metadata is missing
            Exception if the key is wrong or agent_id doesn't match
        """
        if not self.auth_key_iv or not self.auth_key_tag:
            raise ValueError("Agent auth key encryption metadata is missing")

        return decrypt_agent_secret(
            self.auth_key,
            self.auth_key_iv,
            self.auth_key_tag,
            data_key,
            self.id
        )

