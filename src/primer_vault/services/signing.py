"""
Signing Service - Handles x402 payment signing.

Validates requests, enforces policies, and signs payments.

NOTE: This module has NO Qt dependencies. It uses callbacks/EventBus for notifications.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Callable, TYPE_CHECKING

from ..models import Agent, SpendPolicy, Transaction, verify_agent_hmac, verify_bearer_token
from .server import server_stats
# Network name <-> CAIP-2 maps are derived from the central registry (networks.py).
from ..networks import NAME_TO_CAIP as NETWORK_V1_TO_CAIP, CAIP_TO_NAME as NETWORK_CAIP_TO_V1

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..models import PolicyStore
    from ..wallet import VaultWallet

# Default max age for signed requests (5 minutes)
DEFAULT_MAX_REQUEST_AGE_SECONDS = 300

# Signature cache limits (for idempotency)
SIGNATURE_CACHE_MAX_SIZE = 1000
SIGNATURE_CACHE_PRUNE_COUNT = 100


# NETWORK_V1_TO_CAIP and NETWORK_CAIP_TO_V1 are imported from networks.py above
# (single source of truth). network_to_caip / network_to_v1 below use them.


def validate_x402_request(x402_data: dict) -> tuple[bool, int, str]:
    """
    Validate an x402 request and detect its version (v1 or v2).

    Thin wrapper over the single intake parser (eip3009.parse_x402) so there is
    exactly one place in the codebase that understands the wire dialects.
    Version is decided from `x402Version` when present, otherwise inferred from
    the amount field name - never from the network format.

    Returns (is_valid, detected_version, error_message).
    """
    from .eip3009 import parse_x402
    try:
        requirements = parse_x402(x402_data)
        return True, requirements.x402_version, ""
    except ValueError as e:
        return False, 0, str(e)


def network_to_caip(network: str) -> str:
    """Convert v1 network name to CAIP-2 format for internal use."""
    if network.startswith("eip155:"):
        return network
    return NETWORK_V1_TO_CAIP.get(network.lower(), network)


def network_to_v1(network: str) -> str:
    """Convert CAIP-2 network to v1 name for response."""
    if not network.startswith("eip155:"):
        return network
    return NETWORK_CAIP_TO_V1.get(network, network)


def decode_x402_from_response(x402_response: dict) -> tuple[dict, int, str]:
    """
    Decode x402 payment requirements from a raw 402 response.

    The agent forwards the entire 402 response unchanged:
    {
        "status": 402,
        "headers": {"X-PAYMENT": "...", ...} or {"PAYMENT-REQUIRED": "...", ...},
        "body": ...
    }

    Returns: (x402_data, version, error_message)
    - x402_data: The decoded payment requirements dict
    - version: 1 or 2
    - error_message: Empty string on success, error description on failure
    """
    if not isinstance(x402_response, dict):
        return {}, 0, "x402_response must be an object"

    headers = x402_response.get("headers", {})
    if not isinstance(headers, dict):
        return {}, 0, "x402_response.headers must be an object"

    # Normalize header keys to lowercase for case-insensitive lookup
    headers_lower = {k.lower(): v for k, v in headers.items()}

    # Try v2 first (PAYMENT-REQUIRED header)
    payment_required = headers_lower.get("payment-required")
    if payment_required:
        try:
            decoded = base64.b64decode(payment_required)
            x402_data = json.loads(decoded)
            return x402_data, 2, ""
        except Exception as e:
            return {}, 0, f"Failed to decode PAYMENT-REQUIRED header: {e}"

    # Try v1 (X-PAYMENT header)
    x_payment = headers_lower.get("x-payment")
    if x_payment:
        try:
            decoded = base64.b64decode(x_payment)
            x402_data = json.loads(decoded)
            return x402_data, 1, ""
        except Exception as e:
            return {}, 0, f"Failed to decode X-PAYMENT header: {e}"

    return {}, 0, "No x402 payment header found (expected PAYMENT-REQUIRED or X-PAYMENT)"


def decode_payment_required_header(header_value: str) -> tuple[dict, int, str]:
    """
    Decode x402 payment requirements from the Payment-Required header value.

    This is the simplified format where agents send just the header value:
    "eyJhY2NlcHRzIjpbey..." (base64-encoded JSON)

    Returns: (x402_data, version, error_message)
    - x402_data: The decoded payment requirements dict
    - version: 2 (Payment-Required is always v2)
    - error_message: Empty string on success, error description on failure
    """
    if not header_value:
        return {}, 0, "Empty payment_required header value"

    if not isinstance(header_value, str):
        return {}, 0, "payment_required must be a string (base64-encoded)"

    try:
        decoded = base64.b64decode(header_value)
        x402_data = json.loads(decoded)
        return x402_data, 2, ""
    except Exception as e:
        return {}, 0, f"Failed to decode payment_required header: {e}"


@dataclass
class SigningRequest:
    """A request from an agent to sign an x402 payment."""
    id: str
    agent_id: str      # Short agent ID (user-facing, e.g., "ABC123")
    agent_name: str
    amount_micro: int  # Micro-USDG (6 decimals: 1_000_000 = $1.00)
    network: str
    recipient: str
    resource: Optional[str]
    request_url: Optional[str]  # Full URL agent fetched (for domain verification/audit)
    x402_data: dict
    x402_version: int
    created_at: str
    status: str = "pending"
    signature: Optional[str] = None  # Original signature for idempotency cache updates
    cache_key: Optional[str] = None  # Full cache key including payload hash


class SigningService:
    """
    Handles x402 signing requests from agents.

    Flow:
    1. Agent sends request to /sign endpoint
    2. Service looks up agent, validates it's commissioned
    3. Checks policy limits
    4. If under auto-approve: sign immediately
    5. If over: queue for manual approval, call approval callback

    Idempotency:
    - Same signature = same request (retry) → return cached result
    - Different signature = new request → process normally
    - The signature includes a timestamp, so reusing signature = reusing timestamp

    Callbacks (set via set_callbacks):
    - on_approval_needed(request: SigningRequest)
    - on_request_signed(agent_name, agent_id, wallet_id, amount_micro)
    - on_request_rejected(agent_id, reason)
    - on_activity(message, is_error)
    - on_transaction_updated(transaction_id)
    """

    def __init__(self):
        self._policy_store: Optional["PolicyStore"] = None

        # Callbacks (replace Qt signals)
        self._on_approval_needed: Optional[Callable] = None
        self._on_request_signed: Optional[Callable] = None
        self._on_request_rejected: Optional[Callable] = None
        self._on_activity: Optional[Callable] = None
        self._on_transaction_updated: Optional[Callable] = None
        self._wallet_provider = None  # Function to get unlocked wallet by address
        self._wallet_status_checker = None  # Function that returns True if any wallet is loaded
        self._pending_requests: dict[str, SigningRequest] = {}
        # Global network enable/disable (overrides policy-level settings)
        self._enabled_networks: dict[int, bool] = {
            4663: True,   # Robinhood Chain - enabled by default
        }
        # On-chain verification of settlements (default on)
        self._verify_settlements = True
        # Max age for signed requests (configurable, default 5 minutes)
        self._max_request_age_seconds = DEFAULT_MAX_REQUEST_AGE_SECONDS
        # Idempotency cache: cache_key → (transaction_id, cached_result)
        # Cache key is "agent_id:signature" to prevent cross-agent pollution
        # Allows agents to retry with same signature and get same response
        self._signature_cache: dict[str, tuple[str, dict]] = {}
        # Request ID → cache_key mapping for status lookups
        # When agent polls /sign/status/{request_id}, we can find the cached result
        self._request_to_signature: dict[str, str] = {}
        # Lock for spending limit updates to prevent race conditions
        self._spending_lock = threading.Lock()

    def set_callbacks(
        self,
        on_approval_needed: Callable = None,
        on_request_signed: Callable = None,
        on_request_rejected: Callable = None,
        on_activity: Callable = None,
        on_transaction_updated: Callable = None
    ):
        """
        Set callback functions for events.

        These replace the old Qt signals. The GUI bridges these to Qt signals.
        """
        if on_approval_needed:
            self._on_approval_needed = on_approval_needed
        if on_request_signed:
            self._on_request_signed = on_request_signed
        if on_request_rejected:
            self._on_request_rejected = on_request_rejected
        if on_activity:
            self._on_activity = on_activity
        if on_transaction_updated:
            self._on_transaction_updated = on_transaction_updated

    def _emit_activity(self, message: str, is_error: bool = False):
        """Emit activity event via callback."""
        if self._on_activity:
            self._on_activity(message, is_error)

    def _emit_transaction_updated(self, transaction_id: str):
        """Emit transaction updated event via callback."""
        if self._on_transaction_updated:
            self._on_transaction_updated(transaction_id)

    def _emit_approval_needed(self, request: "SigningRequest"):
        """Emit approval needed event via callback."""
        if self._on_approval_needed:
            self._on_approval_needed(request)

    def _emit_request_signed(self, agent_name: str, agent_id: str, wallet_id: str, amount_micro: int):
        """Emit request signed event via callback."""
        if self._on_request_signed:
            self._on_request_signed(agent_name, agent_id, wallet_id, amount_micro)

    def _emit_request_rejected(self, agent_id: str, reason: str):
        """Emit request rejected event via callback."""
        if self._on_request_rejected:
            self._on_request_rejected(agent_id, reason)

    def set_verify_settlements(self, enabled: bool):
        """Enable or disable on-chain verification of settlements."""
        self._verify_settlements = enabled

    def _make_cache_key(
        self,
        agent_id: str,
        signature: str,
        payment_required: Optional[str] = None,
        x402_data: Optional[dict] = None,
        idempotency_key: Optional[str] = None
    ) -> str:
        """
        Create a cache key for idempotency.

        For HMAC mode, the signature already includes a hash of the payload,
        so agent_id:signature is sufficient for uniqueness.

        For bearer mode, the signature (token) is static, so we must include
        either an idempotency_key (if provided) or a hash of the payload.

        If idempotency_key is provided, it takes precedence over payload hashing.
        This allows agents to control caching behavior:
        - Same key = retry behavior (return cached result)
        - New key = fresh request (process normally)
        """
        # If idempotency_key provided, use it instead of payload hash
        if idempotency_key is not None:
            return f"{agent_id}:{signature}:{idempotency_key}"

        # Hash whichever payload format was provided
        if x402_data is not None:
            payload_str = json.dumps(x402_data, separators=(',', ':'), sort_keys=True)
        elif payment_required is not None:
            payload_str = payment_required  # Already a string
        else:
            return f"{agent_id}:{signature}"

        payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()[:16]
        return f"{agent_id}:{signature}:{payload_hash}"

    def _cache_result(
        self,
        agent_id: str,
        signature: str,
        tx_id: str,
        result: dict,
        request_id: str = None,
        payment_required: Optional[str] = None,
        x402_data: Optional[dict] = None,
        idempotency_key: Optional[str] = None
    ) -> dict:
        """Cache a result for idempotency and return it."""
        cache_key = self._make_cache_key(agent_id, signature, payment_required, x402_data, idempotency_key)
        self._signature_cache[cache_key] = (tx_id, result)
        # Also store request_id → cache_key mapping for status lookups
        if request_id:
            self._request_to_signature[request_id] = cache_key
        # Limit cache size to prevent memory growth
        if len(self._signature_cache) > SIGNATURE_CACHE_MAX_SIZE:
            # Remove oldest entries
            keys_to_remove = list(self._signature_cache.keys())[:SIGNATURE_CACHE_PRUNE_COUNT]
            for key in keys_to_remove:
                del self._signature_cache[key]
                # Also clean up request_id mappings
                for req_id, sig in list(self._request_to_signature.items()):
                    if sig == key:
                        del self._request_to_signature[req_id]
        return result

    def set_max_request_age(self, seconds: int):
        """Set the maximum age for signed requests (replay protection window)."""
        if seconds < 30:
            logger.warning(f"Max request age {seconds}s is very short, using 30s minimum")
            seconds = 30
        self._max_request_age_seconds = seconds
        logger.info(f"Max request age set to {seconds}s")

    def _check_daily_reset(self, agent: "Agent") -> bool:
        """Check if agent's daily spending needs to be reset (new calendar day).

        Returns True if reset was performed.
        """
        from datetime import date
        today = date.today().isoformat()
        if agent.last_reset_date != today:
            old_spent = agent.spent_today_micro
            agent.reset_daily_spend()
            if self._policy_store:
                self._policy_store.update_agent(agent)
            if old_spent > 0:
                logger.info(f"Reset daily spending for agent {agent.id}: was {old_spent/1_000_000:.6f} USDG")
            return True
        return False

    def _queue_verification(self, tx: Transaction):
        """Queue a transaction for on-chain verification."""
        # Mark as pending verification
        tx.verification_status = "pending"
        # Run verification in background thread after a short delay
        timer = threading.Timer(1.0, lambda: self._verify_transaction_sync(tx))
        timer.daemon = True
        timer.start()

    def _verify_transaction_sync(self, tx: Transaction):
        """Synchronously verify a transaction on-chain. Updates tx in place."""
        try:
            from ..networks import get_network_by_name, NETWORKS
            from web3 import Web3

            if not tx.tx_hash:
                tx.verification_status = "not_found"
                if self._policy_store:
                    self._policy_store.update_transaction(tx)
                self._emit_transaction_updated(tx.id)
                return

            # Determine chain_id from transaction network
            network_str = tx.network
            if network_str.startswith("eip155:"):
                chain_id = int(network_str.split(":")[1])
            else:
                # v1 name format
                net = get_network_by_name(network_str)
                chain_id = net.chain_id if net else None

            if not chain_id or chain_id not in NETWORKS:
                self._emit_activity(f"Cannot verify tx: unknown network {network_str}", True)
                tx.verification_status = "failed"
                if self._policy_store:
                    self._policy_store.update_transaction(tx)
                self._emit_transaction_updated(tx.id)
                return

            network = NETWORKS[chain_id]
            w3 = Web3(Web3.HTTPProvider(network.rpc_url))

            receipt = w3.eth.get_transaction_receipt(tx.tx_hash)
            if receipt and receipt.get("status") == 1:
                tx.verification_status = "verified"
                tx.verification_block = receipt.get("blockNumber")
                self._emit_activity(
                    f"Verified on-chain: {tx.tx_hash[:10]}... (block {tx.verification_block})",
                    False
                )
            elif receipt and receipt.get("status") == 0:
                tx.verification_status = "failed"
                self._emit_activity(
                    f"Warning: tx {tx.tx_hash[:10]}... failed on-chain!",
                    True
                )
            else:
                tx.verification_status = "not_found"
                self._emit_activity(
                    f"Warning: tx {tx.tx_hash[:10]}... not found on-chain",
                    True
                )
        except Exception as e:
            logger.warning(f"Transaction verification failed for {tx.tx_hash}: {e}")
            self._emit_activity(f"Verification error: {e}", True)
            tx.verification_status = "failed"

        # Persist the verification result
        if self._policy_store:
            self._policy_store.update_transaction(tx)
        self._emit_transaction_updated(tx.id)

    def verify_transaction(self, tx: Transaction):
        """Manually verify a transaction on-chain."""
        if tx.status != "settled" or not tx.tx_hash:
            self._emit_activity("Can only verify settled transactions with tx_hash", True)
            return

        tx.verification_status = "pending"
        self._emit_transaction_updated(tx.id)

        # Run verification in background thread after a short delay
        timer = threading.Timer(0.1, lambda: self._verify_transaction_sync(tx))
        timer.daemon = True
        timer.start()

    def set_network_enabled(self, chain_id: int, enabled: bool):
        """Enable or disable a network globally (overrides policy settings)."""
        self._enabled_networks[chain_id] = enabled

    def is_network_enabled(self, chain_id: int) -> bool:
        """Check if a network is globally enabled."""
        return self._enabled_networks.get(chain_id, False)

    def set_stores(self, policy_store: "PolicyStore"):
        """Set the data stores (called by MainWindow after init)."""
        self._policy_store = policy_store
        # Re-queue any transactions stuck in pending verification from previous session
        self._requeue_stuck_verifications()

    def _requeue_stuck_verifications(self):
        """Re-queue any transactions stuck in pending verification."""
        if not self._policy_store or not self._verify_settlements:
            return
        stuck_count = 0
        for tx in self._policy_store.get_all_transactions():
            if tx.status == "settled" and tx.verification_status == "pending" and tx.tx_hash:
                self._queue_verification(tx)
                stuck_count += 1
        if stuck_count:
            logger.info(f"Re-queued {stuck_count} stuck verification(s) from previous session")

    def set_wallet_provider(self, provider):
        """Set the wallet provider function (gets unlocked wallet by address)."""
        self._wallet_provider = provider

    def set_wallet_status_checker(self, checker):
        """Set callable that returns True if any wallet is currently unlocked."""
        self._wallet_status_checker = checker

    def _create_rejection_transaction(
        self,
        agent: Agent,
        amount_micro: int,
        network: str,
        recipient: str,
        reason: str,
        resource: Optional[str] = None,
        x402_data: Optional[dict] = None,
        request_url: Optional[str] = None
    ) -> Transaction:
        """
        Create a rejection transaction record for audit/compliance.

        This creates a formal AP2 rejection receipt documenting why a payment
        request was denied.
        """
        tx = Transaction.create(
            agent_id=agent.id,
            agent_name=agent.name,
            agent_code=agent.code,
            amount_micro=amount_micro,
            recipient=recipient,
            network=network,
            resource=resource,
            request_url=request_url,
            x402_data=x402_data
        )
        # Link to Intent Mandate if agent has one
        if agent.intent_mandate:
            tx.mandate_id = agent.intent_mandate.get("id")
        tx.mark_rejected(reason)
        self._policy_store.add_transaction(tx)
        self._emit_transaction_updated(tx.id)
        return tx

    def _verify_agent_auth(
        self,
        agent: Agent,
        agent_id: str,
        signature_header: str,
        wallet_password: str,
        payment_required: Optional[str] = None,
        x402_data: Optional[dict] = None,
        request_url: Optional[str] = None
    ) -> Optional[str]:
        """
        Verify agent authentication.

        Supports two authentication modes:
        - HMAC (default): Signature-based authentication
        - Bearer: Token-based authentication

        Returns None on success, error message on failure.

        For HMAC mode:
        - The signature_header format is: "SIG:<timestamp>:<hex_signature>"
        - Agent signs: HMAC-SHA256(JSON.stringify({agent_id, timestamp, payment_required}))
        - Or: HMAC-SHA256(JSON.stringify({agent_id, timestamp, x402_data})) for AP2 format
        - If request_url provided, it's included in the signed message
        - Verified with agent's decrypted shared secret
        - Timestamp checked for replay protection (5 minute window)

        For Bearer mode:
        - The signature_header contains the bearer token: "Bearer AT_..."
        - Token is verified by hashing and comparing to stored hash
        - No timestamp verification (simpler but less secure)

        Args:
            agent: The agent making the request
            agent_id: The agent's ID (for message reconstruction)
            signature_header: The SIG:timestamp:signature header OR "Bearer AT_..." token
            wallet_password: Password to decrypt agent's auth key (HMAC mode only)
            payment_required: Payment-Required header value (base64-encoded x402 payload)
            x402_data: Direct x402 data (if using AP2 format)
            request_url: The URL the agent fetched the 402 from (for domain verification)
        """
        # Build the payment-specific signed fields and delegate to the general
        # verifier (shared with /trade).
        signed_fields: dict = {}
        if x402_data is not None:
            signed_fields["x402_data"] = x402_data
        else:
            signed_fields["payment_required"] = payment_required
        if request_url:
            signed_fields["request_url"] = request_url
        return self.verify_agent_signature(
            agent, agent_id, signature_header, wallet_password, signed_fields)

    def verify_agent_signature(
        self,
        agent: Agent,
        agent_id: str,
        signature_header: str,
        wallet_password: Optional[str],
        signed_fields: dict,
    ) -> Optional[str]:
        """Verify an agent's bearer/HMAC auth over an arbitrary signed payload.

        `signed_fields` is merged into the HMAC message as
        ``{agent_id, timestamp, **signed_fields}`` (sorted keys). /sign passes the
        payment fields; /trade passes ``{"trade": {...}}``. Returns None on
        success, or an error string. HMAC mode needs the unlocked wallet's
        password to decrypt the agent's shared secret; bearer mode ignores it.
        """
        if not signature_header:
            return "Missing authentication"

        # Handle bearer token authentication
        if agent.auth_mode == "bearer":
            token = signature_header
            if token.startswith("Bearer "):
                token = token[7:]  # Strip "Bearer " prefix
            if not token.startswith("AT_"):
                return "Invalid bearer token format (expected AT_...)"
            if not verify_bearer_token(agent.auth_key, token):
                return "Invalid bearer token"
            return None

        # HMAC mode (default)
        if not signature_header.startswith("SIG:"):
            return "Invalid signature format (expected SIG:timestamp:signature)"

        parts = signature_header.split(":", 2)
        if len(parts) != 3:
            return "Invalid signature format"

        try:
            timestamp = int(parts[1])
            signature_hex = parts[2]
        except ValueError:
            return "Invalid timestamp in signature"

        # Check timestamp is recent (replay protection)
        now = int(time.time())
        if abs(now - timestamp) > self._max_request_age_seconds:
            if timestamp > now:
                return "Request timestamp is in the future"
            return "Request expired (timestamp too old)"

        message_data = {"agent_id": agent_id, "timestamp": timestamp, **signed_fields}
        message = json.dumps(message_data, separators=(',', ':'), sort_keys=True).encode('utf-8')

        # Decrypt agent's shared secret
        try:
            shared_secret = agent.decrypt_auth_key(wallet_password)
        except Exception as e:
            logger.error(f"Failed to decrypt agent auth key: {e}")
            return "Failed to decrypt agent credentials"

        # Verify HMAC signature
        if not verify_agent_hmac(shared_secret, message, signature_hex):
            return "Invalid signature"

        return None

    def _verify_mandate_auth(
        self,
        agent: Agent,
        agent_id: str,
        signature_header: str,
        wallet_password: str
    ) -> Optional[str]:
        """
        Verify agent authentication for /mandate endpoint.

        Simpler than _verify_agent_auth - just signs over agent_id + timestamp.

        Returns None on success, error message on failure.
        """
        if not signature_header:
            return "Missing authentication"

        # Check agent's auth mode
        auth_mode = agent.auth_mode

        # Handle bearer token authentication
        if auth_mode == "bearer":
            token = signature_header
            if token.startswith("Bearer "):
                token = token[7:]

            if not token.startswith("AT_"):
                return "Invalid bearer token format (expected AT_...)"

            if not verify_bearer_token(agent.auth_key, token):
                return "Invalid bearer token"

            return None

        # HMAC mode
        if not signature_header.startswith("SIG:"):
            return "Invalid signature format (expected SIG:timestamp:signature)"

        parts = signature_header.split(":", 2)
        if len(parts) != 3:
            return "Invalid signature format"

        try:
            timestamp = int(parts[1])
            signature_hex = parts[2]
        except ValueError:
            return "Invalid timestamp in signature"

        # Check timestamp is recent
        now = int(time.time())
        if abs(now - timestamp) > self._max_request_age_seconds:
            if timestamp > now:
                return "Request timestamp is in the future"
            return "Request expired (timestamp too old)"

        # For /mandate, sign over just agent_id + timestamp + action
        message_data = {
            "action": "get_mandate",
            "agent_id": agent_id,
            "timestamp": timestamp,
        }
        message = json.dumps(message_data, separators=(',', ':'), sort_keys=True).encode('utf-8')

        # Decrypt agent's shared secret
        try:
            shared_secret = agent.decrypt_auth_key(wallet_password)
        except Exception as e:
            logger.error(f"Failed to decrypt agent auth key: {e}")
            return "Failed to decrypt agent credentials"

        # Verify HMAC signature
        if not verify_agent_hmac(shared_secret, message, signature_hex):
            return "Invalid signature"

        return None

    def handle_ping(self, agent_id: str) -> dict:
        """Handle a ping/connection test from an agent."""
        if not self._policy_store:
            return {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"}

        agent = self._policy_store.get_agent_by_id(agent_id)
        if not agent:
            return {
                "status": "error",
                "error": "Agent not found",
                "code": "AGENT_NOT_FOUND"
            }

        # Check if daily spending needs to be reset (new calendar day)
        self._check_daily_reset(agent)

        if agent.status == "uncommissioned":
            return {
                "status": "error",
                "error": "Agent not commissioned",
                "code": "AGENT_NOT_COMMISSIONED",
                "agent_name": agent.name
            }

        if agent.status == "suspended":
            return {
                "status": "error",
                "error": "Agent suspended",
                "code": "AGENT_SUSPENDED",
                "agent_name": agent.name
            }

        policy = self._policy_store.get_policy(agent.policy_id) if agent.policy_id else None

        return {
            "status": "ready",
            "agent_name": agent.name,
            "agent_status": agent.status,
            "policy_name": policy.name if policy else None,
            "daily_limit_micro": policy.daily_limit_micro if policy else None,
            "spent_today_micro": agent.spent_today_micro,
            "auto_approve_below_micro": policy.auto_approve_below_micro if policy else None
        }

    def handle_get_mandate(self, agent_id: str, signature: str) -> dict:
        """
        Get an agent's Intent Mandate and policy summary.

        Requires authentication (signature) to prevent information disclosure.

        Returns the mandate (if any) plus current policy constraints so the agent
        can make informed decisions about which x402 endpoints to use.
        """
        if not self._policy_store:
            return {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"}

        agent = self._policy_store.get_agent_by_id(agent_id)
        if not agent:
            return {"status": "error", "error": "Agent not found", "code": "AGENT_NOT_FOUND"}

        # Check if daily spending needs to be reset (new calendar day)
        self._check_daily_reset(agent)

        if agent.status == "uncommissioned":
            return {
                "status": "error",
                "error": "Agent not commissioned",
                "code": "AGENT_NOT_COMMISSIONED"
            }

        if agent.status == "suspended":
            return {
                "status": "error",
                "error": "Agent suspended",
                "code": "AGENT_SUSPENDED"
            }

        # Verify authentication
        if not self._wallet_provider:
            return {"status": "error", "error": "Wallet provider not set", "code": "NO_WALLET_PROVIDER"}

        wallet: "VaultWallet" = self._wallet_provider(agent.wallet_address)
        if not wallet:
            if self._wallet_status_checker and self._wallet_status_checker():
                return {
                    "status": "error",
                    "error": "Agent's wallet address is not available in the unlocked wallet. Check that the correct wallet is open.",
                    "code": "WALLET_ADDRESS_NOT_FOUND"
                }
            return {
                "status": "error",
                "error": "Wallet is locked. Open Vault and unlock the wallet.",
                "code": "WALLET_LOCKED"
            }

        # Verify signature (for /mandate, we sign over just agent_id + timestamp)
        auth_result = self._verify_mandate_auth(agent, agent_id, signature, wallet._password)
        if auth_result:
            return {
                "status": "error",
                "error": auth_result,
                "code": "AUTH_FAILED"
            }

        policy = self._policy_store.get_policy(agent.policy_id) if agent.policy_id else None

        # Build policy summary for agent decision-making
        policy_summary = None
        if policy:
            policy_summary = {
                "name": policy.name,
                "daily_limit_micro": policy.daily_limit_micro,
                "per_request_max_micro": policy.per_request_max_micro,
                "auto_approve_below_micro": policy.auto_approve_below_micro,
                "allowed_domains": policy.allowed_domains if policy.allowed_domains else None,
                "blocked_domains": policy.blocked_domains if policy.blocked_domains else None,
            }

        # Calculate remaining budget
        remaining_today_micro = None
        if policy and policy.daily_limit_micro:
            remaining_today_micro = max(0, policy.daily_limit_micro - agent.spent_today_micro)

        result = {
            "status": "ok",
            "agent_name": agent.name,
            "agent_id": agent.id,
            "spent_today_micro": agent.spent_today_micro,
            "remaining_today_micro": remaining_today_micro,
            "policy": policy_summary,
        }

        # Include mandate if present
        if agent.intent_mandate:
            result["mandate"] = agent.intent_mandate
            result["mandate_id"] = agent.intent_mandate.get("id")
            result["mandate_registry_id"] = agent.intent_mandate.get("registryId")

            # Check if mandate is stale (policy changed since mandate was generated)
            stale, stale_reason = self._check_mandate_staleness(agent.intent_mandate, policy)
            if stale:
                result["mandate_stale"] = True
                result["mandate_stale_reason"] = stale_reason
        else:
            result["mandate"] = None
            result["mandate_note"] = "No Intent Mandate has been generated for this agent. Check back later or contact your administrator."

        return result

    def _check_mandate_staleness(self, mandate: dict, current_policy: Optional["SpendPolicy"]) -> tuple[bool, Optional[str]]:
        """
        Check if an Intent Mandate is stale (doesn't match current policy).

        Returns (is_stale, reason) tuple.
        """
        if not mandate or not current_policy:
            return False, None

        auth = mandate.get("authorization", {})
        mandate_policy_id = auth.get("policyId")
        mandate_limits = auth.get("limits", {})

        # Check if policy ID changed
        if mandate_policy_id != current_policy.id:
            return True, "Agent was assigned a different policy"

        # Check if limits changed
        if mandate_limits.get("dailyLimit") != current_policy.daily_limit_micro:
            return True, "Daily limit changed"
        if mandate_limits.get("perRequestMax") != current_policy.per_request_max_micro:
            return True, "Per-request maximum changed"
        if mandate_limits.get("autoApproveBelow") != current_policy.auto_approve_below_micro:
            return True, "Auto-approve threshold changed"

        # Note: Domain restrictions are NOT included in the mandate for privacy reasons.
        # They're enforced at signing time but not published. So we don't check them here.

        return False, None

    def handle_sign_request(
        self,
        agent_id: str,
        signature: str,
        payment_required: Optional[str] = None,
        x402_data: Optional[dict] = None,
        request_url: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> dict:
        """
        Handle a signing request from an agent.

        Args:
            agent_id: The agent's short ID (e.g., "ABC123")
            signature: HMAC-SHA256 signature in format "SIG:<timestamp>:<hex_signature>"
            payment_required: The Payment-Required header value (base64-encoded x402 payload)
            x402_data: Direct x402 payment requirements (AP2/A2A format)
            request_url: The URL the agent fetched the 402 from (for domain verification/audit)
            idempotency_key: Optional key to control caching behavior for bearer mode.
                If provided, this key (instead of payload hash) determines cache uniqueness.
                Allows repeated payments to same endpoint with unique keys per purchase.

        One of payment_required or x402_data must be provided.

        Idempotency:
        - Same signature + same idempotency_key = retry → return cached result
        - Same signature + different idempotency_key = fresh request → process normally
        - For HMAC mode: timestamp in signature already provides uniqueness
        - For bearer mode: use idempotency_key for repeated purchases to same endpoint

        Returns immediately with either:
        - Signed payment (if auto-approved)
        - Queued status (if needs manual approval)
        - Error (if rejected)
        """
        if not self._policy_store:
            return {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"}

        # Build cache key early (needed for both lookup and storage)
        cache_key = self._make_cache_key(agent_id, signature, payment_required, x402_data, idempotency_key)

        # Must have one of payment_required or x402_data
        if payment_required is None and x402_data is None:
            return {
                "status": "error",
                "error": "Must provide either payment_required or x402_data",
                "code": "MISSING_X402_DATA"
            }

        # Validate provided data types
        if payment_required is not None and not isinstance(payment_required, str):
            return {
                "status": "error",
                "error": "payment_required must be a string (base64-encoded)",
                "code": "INVALID_PAYMENT_REQUIRED"
            }
        if x402_data is not None and not isinstance(x402_data, dict):
            return {
                "status": "error",
                "error": "x402_data must be an object",
                "code": "INVALID_X402_DATA"
            }

        agent = self._policy_store.get_agent_by_id(agent_id)
        if not agent:
            self._emit_activity(f"Unknown agent ID: {agent_id}", True)
            return {
                "status": "error",
                "error": "Agent not found",
                "code": "AGENT_NOT_FOUND"
            }

        # Check if daily spending needs to be reset (new calendar day)
        self._check_daily_reset(agent)

        # Check agent status BEFORE auth (we need wallet for auth, and uncommissioned agents have no wallet)
        if agent.status == "uncommissioned":
            self._emit_activity(f"Uncommissioned agent tried to sign: {agent.name}", True)
            return {
                "status": "error",
                "error": "Agent not commissioned",
                "code": "AGENT_NOT_COMMISSIONED"
            }

        if agent.status == "suspended":
            self._emit_activity(f"Suspended agent tried to sign: {agent.name}", True)
            return {
                "status": "error",
                "error": "Agent suspended",
                "code": "AGENT_SUSPENDED"
            }

        # For bearer auth: verify token BEFORE wallet check — bearer doesn't need the wallet,
        # so a wrong token should return AUTH_FAILED not WALLET_LOCKED.
        if agent.auth_mode == "bearer":
            auth_result = self._verify_agent_auth(
                agent, agent_id, signature,
                wallet_password=None,
                payment_required=payment_required,
                x402_data=x402_data,
                request_url=request_url
            )
            if auth_result:
                self._emit_activity(f"Auth failed for {agent.name}: {auth_result}", True)
                return {
                    "status": "error",
                    "error": auth_result,
                    "code": "AUTH_FAILED"
                }

        # Get wallet (needed for signing, and for HMAC auth key decryption)
        if not self._wallet_provider:
            return {"status": "error", "error": "Wallet provider not set", "code": "NO_WALLET_PROVIDER"}

        wallet: "VaultWallet" = self._wallet_provider(agent.wallet_address)
        if not wallet:
            if self._wallet_status_checker and self._wallet_status_checker():
                return {
                    "status": "error",
                    "error": "Agent's wallet address is not available in the unlocked wallet. Check that the correct wallet is open.",
                    "code": "WALLET_ADDRESS_NOT_FOUND"
                }
            return {
                "status": "error",
                "error": "Wallet is locked. Open Vault and unlock the wallet to enable signing.",
                "code": "WALLET_LOCKED"
            }

        # For HMAC auth: verify signature (needs wallet password to decrypt auth key)
        if agent.auth_mode != "bearer":
            auth_result = self._verify_agent_auth(
                agent, agent_id, signature,
                wallet_password=wallet._password,
                payment_required=payment_required,
                x402_data=x402_data,
                request_url=request_url
            )
            if auth_result:
                self._emit_activity(f"Auth failed for {agent.name}: {auth_result}", True)
                return {
                    "status": "error",
                    "error": auth_result,
                    "code": "AUTH_FAILED"
                }

        if agent.status == "limit_reached":
            self._emit_activity(f"Agent at limit tried to sign: {agent.name}", True)
            return {
                "status": "error",
                "error": "Daily limit reached",
                "code": "LIMIT_REACHED"
            }

        policy = self._policy_store.get_policy(agent.policy_id)
        if not policy:
            return {
                "status": "error",
                "error": "Policy not found",
                "code": "POLICY_NOT_FOUND"
            }

        # Check if x402 payments are enabled for this policy
        if not policy.is_x402_enabled():
            self._emit_activity(f"x402 payments disabled for policy {policy.name}", True)
            return {
                "status": "error",
                "error": "x402 payments are disabled for this policy",
                "code": "X402_DISABLED"
            }

        # Idempotency check: AFTER security validations pass
        # Same agent + same signature + same payload = return cached result
        # This ensures suspended/decommissioned agents can't replay cached results
        if signature and cache_key in self._signature_cache:
            tx_id, cached_result = self._signature_cache[cache_key]

            # Check if this transaction's nonce is already spent (settled on-chain)
            # If so, reject rather than returning a useless payment header
            tx = self._policy_store.get_transaction(tx_id) if self._policy_store else None
            if tx:
                nonce_spent = (
                    tx.status == "settled" or  # Agent reported settlement
                    tx.verification_status == "verified"  # We verified on-chain
                )
                if nonce_spent:
                    logger.info(f"Rejecting cached result for {agent_id} - nonce already spent (tx: {tx_id[:8]}...)")
                    return {
                        "status": "error",
                        "code": "PAYMENT_ALREADY_SETTLED",
                        "error": "This payment was already settled on-chain. Use idempotency_key for a fresh payment.",
                        "previous_transaction_id": tx_id,
                        "hint": "Add 'idempotency_key': 'unique-string' to your request"
                    }

            # Not settled - safe to return cached result for retries
            logger.info(f"Returning cached result for {agent_id} (tx: {tx_id[:8]}...)")
            return cached_result

        # Handle the two input formats -> a single decoded dict
        if x402_data is not None:
            # AP2/A2A format: x402 data provided directly as JSON
            decoded_x402_data = x402_data
        else:
            # HTTP 402 format: decode from Payment-Required header value
            decoded_x402_data, _, decode_error = decode_payment_required_header(payment_required)
            if decode_error:
                self._emit_activity(f"Failed to decode payment_required from {agent.name}: {decode_error}", True)
                server_stats.rejected += 1
                return {
                    "status": "error",
                    "error": decode_error,
                    "code": "INVALID_PAYMENT_REQUIRED"
                }

        # Single dialect-aware parse: the one place that reads v1/v2 wire fields.
        # The amount/network/recipient/resource taken here are the SAME values
        # that flow to the policy checks and to the signature - they cannot drift.
        from .eip3009 import parse_x402
        try:
            requirements = parse_x402(decoded_x402_data)
            amount_micro = self._bounded_amount_micro(requirements.max_amount_required)
        except ValueError as e:
            self._emit_activity(f"Invalid x402 from {agent.name}: {e}", True)
            server_stats.rejected += 1
            return {
                "status": "error",
                "error": str(e),
                "code": "INVALID_X402_FORMAT"
            }

        x402_version = requirements.x402_version
        network = requirements.network  # CAIP-2
        recipient = requirements.pay_to
        resource = requirements.resource

        # Check if network is globally enabled (overrides policy-level settings)
        caip_network = network_to_caip(network)
        try:
            chain_id = int(caip_network.split(":")[1]) if ":" in caip_network else 0
        except (ValueError, IndexError):
            chain_id = 0

        if chain_id and not self.is_network_enabled(chain_id):
            network_name = network_to_v1(caip_network)
            reason = f"Network {network_name} is disabled"
            self._emit_activity(f"Request from {agent.name} rejected: {reason}", True)
            server_stats.rejected += 1
            # Create rejection receipt for audit trail
            tx = self._create_rejection_transaction(
                agent, amount_micro, network, recipient, reason, resource, decoded_x402_data,
                request_url=request_url
            )
            return {
                "status": "error",
                "error": reason,
                "code": "NETWORK_DISABLED",
                "transaction_id": tx.id
            }

        # Check if network is allowed by policy (policy-level restriction)
        if chain_id and policy.networks and chain_id not in policy.networks:
            network_name = network_to_v1(caip_network)
            allowed_names = [network_to_v1(f"eip155:{cid}") for cid in policy.networks]
            reason = f"Network {network_name} not allowed by policy (allowed: {', '.join(allowed_names)})"
            self._emit_activity(f"Request from {agent.name} rejected: {reason}", True)
            server_stats.rejected += 1
            # Create rejection receipt for audit trail
            tx = self._create_rejection_transaction(
                agent, amount_micro, network, recipient, reason, resource, decoded_x402_data,
                request_url=request_url
            )
            return {
                "status": "error",
                "error": reason,
                "code": "NETWORK_NOT_ALLOWED_BY_POLICY",
                "transaction_id": tx.id
            }

        # Check domain restrictions
        # Use request_url if provided (agent tells us where they got the 402),
        # otherwise fall back to x402 resource field
        domain_check_url = request_url or resource
        # If policy has domain restrictions but no URL to check, reject
        if policy.has_domain_restrictions() and not domain_check_url:
            reason = "Domain restrictions configured but no URL provided (include request_url or resource)"
            self._emit_activity(f"Request from {agent.name} rejected: {reason}", True)
            server_stats.rejected += 1
            tx = self._create_rejection_transaction(
                agent, amount_micro, network, recipient, reason, resource, decoded_x402_data,
                request_url=request_url
            )
            return {
                "status": "error",
                "error": reason,
                "code": "DOMAIN_URL_REQUIRED",
                "transaction_id": tx.id
            }
        domain_allowed, domain_reason = policy.check_domain_allowed(domain_check_url)
        if not domain_allowed:
            self._emit_activity(f"Request from {agent.name} rejected: {domain_reason}", True)
            server_stats.rejected += 1
            # Create rejection receipt for audit trail
            tx = self._create_rejection_transaction(
                agent, amount_micro, network, recipient, domain_reason, resource, decoded_x402_data,
                request_url=request_url
            )
            return {
                "status": "error",
                "error": domain_reason,
                "code": "DOMAIN_NOT_ALLOWED",
                "transaction_id": tx.id
            }

        if policy.per_request_max_micro and amount_micro > policy.per_request_max_micro:
            reason = f"Exceeds per-request maximum ({amount_micro/1_000_000:.6f} > {policy.per_request_max_micro/1_000_000:.6f} USDG)"
            self._emit_activity(f"Request from {agent.name}: {reason}", True)
            server_stats.rejected += 1
            # Create rejection receipt for audit trail
            tx = self._create_rejection_transaction(
                agent, amount_micro, network, recipient, reason, resource, decoded_x402_data,
                request_url=request_url
            )
            return {
                "status": "error",
                "error": "Exceeds per-request maximum",
                "code": "EXCEEDS_PER_REQUEST_MAX",
                "requested_micro": amount_micro,
                "max_micro": policy.per_request_max_micro,
                "transaction_id": tx.id
            }

        if policy.daily_limit_micro:
            remaining = policy.daily_limit_micro - agent.spent_today_micro
            if amount_micro > remaining:
                reason = f"Would exceed daily limit ({amount_micro/1_000_000:.6f} > {remaining/1_000_000:.6f} USDG remaining)"
                self._emit_activity(f"Request from {agent.name}: {reason}", True)
                server_stats.rejected += 1
                # Create rejection receipt for audit trail
                tx = self._create_rejection_transaction(
                    agent, amount_micro, network, recipient, reason, resource, decoded_x402_data,
                    request_url=request_url
                )
                return {
                    "status": "error",
                    "error": "Would exceed daily limit",
                    "code": "EXCEEDS_DAILY_LIMIT",
                    "requested_micro": amount_micro,
                    "remaining_micro": remaining,
                    "transaction_id": tx.id
                }

        needs_approval = (
            policy.auto_approve_below_micro is None or
            amount_micro >= policy.auto_approve_below_micro
        )

        if needs_approval:
            request_id = str(uuid.uuid4())
            request = SigningRequest(
                id=request_id,
                agent_id=agent_id,
                agent_name=agent.name,
                amount_micro=amount_micro,
                network=network,
                recipient=recipient,
                resource=resource,
                request_url=request_url,
                x402_data=decoded_x402_data,
                x402_version=x402_version,
                created_at=datetime.now(timezone.utc).isoformat(),
                status="pending"
            )
            # Store signature and cache key for idempotency updates after approval/rejection
            request.signature = signature
            request.cache_key = cache_key  # Full cache key including payload hash
            self._pending_requests[request_id] = request

            self._emit_activity(
                f"Payment request from {agent.name}: {amount_micro/1_000_000:.6f} USDG - awaiting approval",
                False
            )
            self._emit_approval_needed(request)

            result = {
                "status": "pending",
                "message": "Awaiting user approval",
                "request_id": request_id,
                "code": "APPROVAL_REQUIRED"
            }
            # Cache pending result so retries get same request_id
            return self._cache_result(
                agent_id, signature, request_id, result,
                request_id=request_id, payment_required=payment_required, x402_data=x402_data,
                idempotency_key=idempotency_key
            )

        result = self._sign_payment(
            agent, policy, decoded_x402_data, amount_micro, x402_version,
            auto_approved=True,  # Below auto-approve threshold
            request_url=request_url
        )
        # Cache successful result so retries get same response
        tx_id = result.get("transaction_id", "unknown")
        return self._cache_result(
            agent_id, signature, tx_id, result,
            payment_required=payment_required, x402_data=x402_data,
            idempotency_key=idempotency_key
        )

    def approve_request(self, request_id: str) -> dict:
        """Approve a pending request and sign the payment."""
        request = self._pending_requests.get(request_id)
        if not request:
            return {"status": "error", "error": "Request not found or expired", "code": "REQUEST_NOT_FOUND"}

        if request.status != "pending":
            return {"status": "error", "error": f"Request already {request.status}", "code": "REQUEST_ALREADY_PROCESSED"}

        agent = self._policy_store.get_agent_by_id(request.agent_id)
        policy = self._policy_store.get_policy(agent.policy_id) if agent else None

        if not agent or not policy:
            del self._pending_requests[request_id]
            return {"status": "error", "error": "Agent or policy no longer exists", "code": "AGENT_OR_POLICY_MISSING"}

        # Re-validate agent status (may have changed since request was queued)
        if agent.status == "suspended":
            del self._pending_requests[request_id]
            return {"status": "error", "error": "Agent is now suspended", "code": "AGENT_SUSPENDED"}

        if agent.status == "limit_reached":
            del self._pending_requests[request_id]
            return {"status": "error", "error": "Agent has reached daily limit", "code": "LIMIT_REACHED"}

        # Re-validate policy limits (may have changed since request was queued)
        if policy.per_request_max_micro and request.amount_micro > policy.per_request_max_micro:
            del self._pending_requests[request_id]
            return {
                "status": "error",
                "error": f"Amount {request.amount_micro/1_000_000:.6f} USDG exceeds current per-request limit {policy.per_request_max_micro/1_000_000:.6f} USDG",
                "code": "EXCEEDS_PER_REQUEST_MAX"
            }

        if policy.daily_limit_micro:
            remaining = policy.daily_limit_micro - agent.spent_today_micro
            if request.amount_micro > remaining:
                del self._pending_requests[request_id]
                return {
                    "status": "error",
                    "error": f"Amount {request.amount_micro/1_000_000:.6f} USDG exceeds remaining daily limit {remaining/1_000_000:.6f} USDG",
                    "code": "EXCEEDS_DAILY_LIMIT"
                }

        # Re-validate network is still enabled (global setting)
        network = request.x402_data.get("accepts", [{}])[0].get("network", "unknown")
        caip_network = network_to_caip(network)
        try:
            chain_id = int(caip_network.split(":")[1]) if ":" in caip_network else 0
        except (ValueError, IndexError):
            chain_id = 0

        if chain_id and not self.is_network_enabled(chain_id):
            del self._pending_requests[request_id]
            return {
                "status": "error",
                "error": f"Network {network} is no longer enabled",
                "code": "NETWORK_DISABLED"
            }

        # Re-validate network is allowed by policy (may have changed)
        if chain_id and policy.networks and chain_id not in policy.networks:
            del self._pending_requests[request_id]
            return {
                "status": "error",
                "error": f"Network {network} is no longer allowed by policy",
                "code": "NETWORK_NOT_ALLOWED_BY_POLICY"
            }

        result = self._sign_payment(
            agent, policy, request.x402_data, request.amount_micro, request.x402_version,
            auto_approved=False,  # Manual approval
            request_url=request.request_url
        )

        if result.get("status") == "success":
            request.status = "approved"
            # Update signature cache with the signed result
            if request.cache_key:
                tx_id = result.get("transaction_id", request_id)
                self._signature_cache[request.cache_key] = (tx_id, result)
            del self._pending_requests[request_id]
        else:
            # Signing failed — clean up so the operator can retry
            if request.cache_key and request.cache_key in self._signature_cache:
                del self._signature_cache[request.cache_key]
            del self._pending_requests[request_id]

        return result

    def reject_request(self, request_id: str, reason: str = "User rejected") -> dict:
        """Reject a pending request."""
        request = self._pending_requests.get(request_id)
        if not request:
            return {"status": "error", "error": "Request not found or expired", "code": "REQUEST_NOT_FOUND"}

        request.status = "rejected"

        # Get agent for transaction record
        agent = self._policy_store.get_agent_by_id(request.agent_id)
        if agent:
            # Create a rejection receipt for audit trail
            tx = self._create_rejection_transaction(
                agent, request.amount_micro, request.network, request.recipient,
                reason, request.resource, request.x402_data,
                request_url=request.request_url
            )
            tx_id = tx.id
        else:
            tx_id = request_id

        self._emit_activity(f"Rejected payment from {request.agent_name}: {reason}", False)
        self._emit_request_rejected(request.agent_id, reason)
        server_stats.rejected += 1

        result = {"status": "rejected", "reason": reason, "transaction_id": tx_id}

        # Update signature cache with rejection result
        if request.cache_key:
            self._signature_cache[request.cache_key] = (tx_id, result)

        del self._pending_requests[request_id]
        return result

    def get_pending_requests(self) -> list[SigningRequest]:
        """Get all pending approval requests."""
        return list(self._pending_requests.values())

    def get_request_status(self, request_id: str) -> dict:
        """
        Get the status of a signing request.

        Returns the current status and, if approved, the signed payment header.
        This allows agents to poll for approval and retrieve the payment header.
        """
        # Check pending requests first
        request = self._pending_requests.get(request_id)
        if request:
            return {
                "status": "pending",
                "message": "Awaiting user approval",
                "request_id": request_id,
                "code": "APPROVAL_REQUIRED",
                "amount_micro": request.amount_micro,
                "agent_id": request.agent_id,
                "created_at": request.created_at
            }

        # Look up by request_id → cache_key → cached result
        # This is the primary lookup path after approval
        cache_key = self._request_to_signature.get(request_id)
        if cache_key and cache_key in self._signature_cache:
            _, cached_result = self._signature_cache[cache_key]
            return cached_result

        # Fallback: check if there's a transaction with this ID
        if self._policy_store:
            tx = self._policy_store.get_transaction(request_id)
            if tx:
                if tx.status == "signed":
                    # Return the signed result format (without header - it wasn't cached)
                    return {
                        "status": "success",
                        "message": "Payment was signed (header no longer cached)",
                        "transaction_id": tx.id,
                        "code": "ALREADY_SIGNED"
                    }
                elif tx.status == "rejected":
                    return {
                        "status": "rejected",
                        "reason": tx.reject_reason or "Request rejected",
                        "transaction_id": tx.id
                    }

        return {
            "status": "error",
            "error": "Request not found",
            "code": "REQUEST_NOT_FOUND"
        }

    def _sign_payment(
        self,
        agent: Agent,
        policy: SpendPolicy,
        x402_data: dict,
        amount_micro: int,
        x402_version: int = 2,
        auto_approved: bool = True,
        existing_tx: Optional[Transaction] = None,
        request_url: Optional[str] = None
    ) -> dict:
        """Actually sign a payment using the wallet."""
        try:
            # Get unlocked wallet from provider
            if not self._wallet_provider:
                return {"status": "error", "error": "Wallet provider not set", "code": "NO_WALLET_PROVIDER"}

            wallet: "VaultWallet" = self._wallet_provider(agent.wallet_address)
            if not wallet:
                return {
                    "status": "error",
                    "error": "Wallet is locked. Open Vault and unlock the wallet to enable signing.",
                    "code": "WALLET_LOCKED"
                }

            # Find the address entry in the wallet
            addr_entry = wallet.get_address_by_address(agent.wallet_address)
            if not addr_entry:
                return {"status": "error", "error": "Address not found in wallet", "code": "ADDRESS_NOT_FOUND"}

            wallet_id = addr_entry.id  # Use address ID (A001, etc.)

            # Use local EIP-3009 signing (no external SDK dependency)
            from .eip3009 import parse_x402, create_payment

            # Single dialect-aware parse - deterministically the same result the
            # intake produced, so the value signed here equals the amount that
            # passed the policy checks. No independent re-reading of wire fields.
            try:
                requirements = parse_x402(x402_data)
            except ValueError as e:
                return {
                    "status": "error",
                    "error": str(e),
                    "code": "INVALID_X402_FORMAT"
                }

            # Keep the merchant's raw network string for the audit record.
            original_network = x402_data.get("accepts", [{}])[0].get("network", "")
            recipient = requirements.pay_to
            resource = requirements.resource

            # Validate Ethereum addresses before attempting to sign
            _eth_addr_re = re.compile(r'^0x[0-9a-fA-F]{40}$')
            for field, addr in [("payTo", requirements.pay_to),
                                 ("asset", requirements.asset)]:
                if addr and not _eth_addr_re.match(addr):
                    hex_len = len(addr) - 2 if addr.startswith("0x") else len(addr)
                    return {
                        "status": "error",
                        "error": (
                            f"Invalid {field} address in payment_required: "
                            f"'{addr}' is not a valid Ethereum address "
                            f"(expected 40 hex characters, got {hex_len})"
                        ),
                        "code": "INVALID_PAYMENT_DATA"
                    }

            # Get private key from wallet using address ID
            private_key_bytes = wallet.get_private_key(addr_entry.id)
            private_key_hex = private_key_bytes.hex()

            # Create payment using local EIP-3009 signing. Echo the merchant's
            # selected offer (`accepted`) and resource so the v2 payload is
            # spec-shaped; the signature itself is unaffected by these fields.
            selected_accept = x402_data.get("accepts", [{}])[0]
            payment = create_payment(
                private_key_hex, requirements,
                accepted=selected_accept,
                resource=x402_data.get("resource"),
            )

            if x402_version == 1:
                payment = self._convert_payment_to_v1(payment)

            # Atomically check and update spending limits to prevent race conditions
            with self._spending_lock:
                # Re-check daily limit inside lock (initial check may have been racy)
                if policy.daily_limit_micro:
                    remaining = policy.daily_limit_micro - agent.spent_today_micro
                    if amount_micro > remaining:
                        return {
                            "status": "error",
                            "error": "Would exceed daily limit (concurrent request)",
                            "code": "EXCEEDS_DAILY_LIMIT",
                            "requested_micro": amount_micro,
                            "remaining_micro": remaining
                        }
                agent.spent_today_micro += amount_micro
                if policy.daily_limit_micro and agent.spent_today_micro >= policy.daily_limit_micro:
                    agent.status = "limit_reached"
                self._policy_store.update_agent(agent)

            # Create or update transaction record
            if existing_tx:
                tx = existing_tx
                tx.mark_signed(agent.wallet_address, wallet_id, auto_approved)
            else:
                tx = Transaction.create(
                    agent_id=agent.id,
                    agent_name=agent.name,
                    agent_code=agent.code,
                    amount_micro=amount_micro,
                    recipient=recipient,
                    network=original_network,
                    resource=resource,
                    request_url=request_url,
                    x402_data=x402_data,
                    wallet_address=agent.wallet_address,
                    wallet_id=wallet_id
                )
                # Link to Intent Mandate if agent has one
                if agent.intent_mandate:
                    tx.mandate_id = agent.intent_mandate.get("id")
                tx.mark_signed(agent.wallet_address, wallet_id, auto_approved)

            self._policy_store.add_transaction(tx)
            self._emit_transaction_updated(tx.id)

            self._emit_activity(
                f"Signed {amount_micro/1_000_000:.6f} USDG payment for {agent.name} ({agent.id}) from {wallet_id}",
                False
            )
            self._emit_request_signed(agent.name, agent.id, wallet_id, amount_micro)
            server_stats.signed += 1

            # Encode payment as base64 for the header value
            payment_json = json.dumps(payment)
            payment_b64 = base64.b64encode(payment_json.encode()).decode()

            # Return header name based on version
            if x402_version == 1:
                header_name = "X-PAYMENT-RESPONSE"
            else:
                header_name = "PAYMENT-SIGNATURE"

            return {
                "status": "success",
                "x402_version": x402_version,
                "header_name": header_name,
                "header_value": payment_b64,
                "transaction_id": tx.id
            }

        except Exception as e:
            logger.error(f"Signing error: {e}", exc_info=True)
            self._emit_activity(f"Signing error: {e}", True)
            return {
                "status": "error",
                "error": "Payment signing failed",
                "code": "SIGNING_ERROR"
            }

    def _convert_payment_to_v1(self, payment: dict) -> dict:
        """Convert a v2 PaymentPayload to the flat v1 shape.

        v1 puts `scheme`/`network` at the top level (v1 network name) and has no
        `accepted`/`resource`. Only `payload` carries over unchanged.
        """
        accepted = payment.get("accepted", {})
        return {
            "x402Version": 1,
            "scheme": accepted.get("scheme", "exact"),
            "network": network_to_v1(accepted.get("network", "")),
            "payload": payment["payload"],
        }

    def handle_callback(
        self,
        agent_id: str,
        transaction_id: str,
        event: str,
        tx_hash: Optional[str] = None,
        error: Optional[str] = None
    ) -> dict:
        """
        Handle a callback from an agent reporting transaction status.

        Args:
            agent_id: The agent's ID
            transaction_id: The transaction ID from the sign response
            event: One of 'submitted', 'settled', 'failed'
            tx_hash: Transaction hash (required for 'settled')
            error: Error message (optional for 'failed')

        Returns:
            Status response dict
        """
        if not self._policy_store:
            return {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"}

        # Verify agent exists
        agent = self._policy_store.get_agent_by_id(agent_id)
        if not agent:
            return {"status": "error", "error": "Agent not found", "code": "AGENT_NOT_FOUND"}

        # Find the transaction
        tx = self._policy_store.get_transaction(transaction_id)
        if not tx:
            return {"status": "error", "error": "Transaction not found", "code": "TRANSACTION_NOT_FOUND"}

        # Verify this agent owns the transaction (check agent_id which is the short ID)
        if tx.agent_id != agent_id:
            return {"status": "error", "error": "Transaction does not belong to this agent", "code": "UNAUTHORIZED"}

        # Update transaction status based on event
        if event == "submitted":
            tx.mark_submitted()
            self._emit_activity(
                f"Agent {agent.name} submitted payment to target API ({transaction_id[:8]})",
                False
            )
        elif event == "settled":
            if not tx_hash:
                return {"status": "error", "error": "tx_hash required for settled event", "code": "MISSING_TX_HASH"}
            # Validate tx_hash format (0x + 64 hex chars)
            if not re.match(r'^0x[a-fA-F0-9]{64}$', tx_hash):
                return {"status": "error", "error": "Invalid tx_hash format", "code": "INVALID_TX_HASH"}
            tx.mark_settled(tx_hash)
            self._emit_activity(
                f"Payment settled for {agent.name}: {tx.format_amount()} (tx: {tx_hash[:10]}...)",
                False
            )
            # Queue on-chain verification if enabled
            if self._verify_settlements:
                self._queue_verification(tx)
        elif event == "failed":
            tx.mark_failed(error)
            self._emit_activity(
                f"Payment failed for {agent.name}: {error or 'Unknown error'}",
                True
            )
        else:
            return {"status": "error", "error": f"Unknown event: {event}", "code": "INVALID_EVENT"}

        self._policy_store.update_transaction(tx)
        self._emit_transaction_updated(tx.id)

        return {"status": "ok", "transaction_id": tx.id, "new_status": tx.status}

    def get_receipt(self, identifier: str) -> dict:
        """
        Get an AP2-formatted receipt for a transaction.

        Args:
            identifier: The transaction ID (UUID) or on-chain tx_hash (0x...)

        Returns:
            AP2-formatted receipt dict, or error dict if not found
        """
        if not self._policy_store:
            return {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"}

        # Try by transaction ID first
        tx = self._policy_store.get_transaction(identifier)

        # If not found and looks like a tx_hash, try that
        if not tx and identifier.startswith("0x"):
            tx = self._policy_store.get_transaction_by_hash(identifier)

        if not tx:
            return {"status": "error", "error": "Transaction not found", "code": "TRANSACTION_NOT_FOUND"}

        # Get policy name for the receipt
        agent = self._policy_store.get_agent_by_id(tx.agent_id)
        policy = self._policy_store.get_policy(agent.policy_id) if agent else None
        policy_name = policy.name if policy else None

        return tx.to_ap2_receipt(policy_name=policy_name)

    def _bounded_amount_micro(self, raw_amount) -> int:
        """
        Convert a parsed atomic amount to a bounds-checked int of micro-USDG.

        The amount is already in 6-decimal format for USDG (1_000_000 = $1.00),
        the standard for stablecoins on EVM chains (USDG/USDT: 6 decimals). An
        asset with different decimals (e.g. DAI with 18) would be miscounted.

        Raises ValueError on a non-integer, non-positive, or absurdly large
        amount.
        """
        try:
            amount_micro = int(raw_amount)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid amount: {raw_amount!r}")

        # Bounds validation
        if amount_micro <= 0:
            raise ValueError(f"amount must be positive, got {amount_micro}")
        if amount_micro > 10**15:  # $1 billion in micro-USDG - unreasonable upper bound
            raise ValueError(f"amount exceeds maximum ({amount_micro} > 10^15)")

        return amount_micro

    def _parse_amount_micro(self, x402_data: dict) -> int:
        """Dialect-aware amount extraction + bounds check (v1 or v2).

        Retained for callers/tests that hand in a raw x402 dict; delegates to
        the single intake parser so it can never disagree with the signed value.
        """
        from .eip3009 import parse_x402
        return self._bounded_amount_micro(parse_x402(x402_data).max_amount_required)
