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
from ..models.agent import daily_allowance_is_due
from .server import server_stats
# Network name <-> CAIP-2 maps are derived from the central registry (networks.py).
from ..networks import NAME_TO_CAIP as NETWORK_V1_TO_CAIP, CAIP_TO_NAME as NETWORK_CAIP_TO_V1

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..models import PolicyStore
    from ..wallet import VaultWallet

# Default max age for signed requests (5 minutes)
DEFAULT_MAX_REQUEST_AGE_SECONDS = 300

# How long a payment may wait for a human before it is abandoned.
#
# The authorization it would produce is only valid for an hour, and the agent's
# own request signature only for five minutes, so a request parked much longer
# than this cannot lead anywhere useful. Matching the trading window keeps one
# number to remember rather than two.
#
# Pending approvals live in memory only and are lost on restart. That is
# deliberate: nothing has been signed or submitted while a request waits, so a
# lost one costs the agent a retry and risks nothing on-chain. Persisting
# transient state would buy a durable store and a migration to save that retry.
PENDING_REQUEST_TTL_SECONDS = 900  # 15 minutes

# How many requests one agent may have waiting at once.
#
# Per-agent rather than global: a global ceiling would let one noisy agent crowd
# every other agent's approvals out of the queue, and bury the person approving
# them.
MAX_PENDING_PER_AGENT = 20

# Verification outcomes. "failed" is reserved for what the chain reports, so a
# payment is never recorded as failed because Vault could not reach a node.
VERIFY_PENDING = "pending"          # queued, not yet checked
VERIFY_VERIFIED = "verified"        # the chain confirmed it succeeded
VERIFY_FAILED = "failed"            # the chain confirmed it reverted
VERIFY_NOT_FOUND = "not_found"      # the chain has no such transaction
VERIFY_UNAVAILABLE = "unavailable"  # Vault could not ask; says nothing either way

# Signature cache limits (for idempotency)
SIGNATURE_CACHE_MAX_SIZE = 1000

# How many finished requests to keep an answer for.
#
# A request that has been signed, rejected or has failed is terminal, and the
# agent that submitted it comes back to read the outcome. The signature cache
# cannot serve that on its own: it is keyed for idempotency, and a failed
# signing has to be dropped from it so the agent can submit again - which would
# otherwise leave the poll with nothing to find.
MAX_RESOLVED_REQUESTS = 1000
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
        self._on_hardware_sign_needed: Optional[Callable] = None  # For hardware wallet signing
        self._wallet_provider = None  # Function to get unlocked wallet by address
        self._wallet_status_checker = None  # Function that returns True if any wallet is loaded
        self._rpc_resolver: Optional[Callable] = None  # Vault.get_rpc_url, injected by the core
        # Returns USD an agent has promised to trades not yet recorded. Injected
        # by the core, because the trading service is what holds them.
        self._reserved_volume_provider: Optional[Callable] = None
        self._pending_requests: dict[str, SigningRequest] = {}
        # request_id -> monotonic deadline, kept beside _pending_requests so an
        # abandoned request cannot sit in memory until the process ends.
        self._pending_deadlines: dict[str, float] = {}
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
        # Terminal outcomes by request id, for agents polling /sign/status.
        # Separate from the signature cache because the two want opposite things
        # from a failure: the cache must forget it so a resubmit is processed
        # fresh, this must remember it so the agent learns what happened.
        self._resolved: dict[str, dict] = {}
        # Lock for spending limit updates to prevent race conditions
        self._spending_lock = threading.Lock()

    def set_callbacks(
        self,
        on_approval_needed: Callable = None,
        on_request_signed: Callable = None,
        on_request_rejected: Callable = None,
        on_activity: Callable = None,
        on_transaction_updated: Callable = None,
        on_hardware_sign_needed: Callable = None
    ):
        """
        Set callback functions for events.

        These replace the old Qt signals. The GUI bridges these to Qt signals.

        Callbacks:
        - on_approval_needed(request: SigningRequest): Payment needs user approval
        - on_request_signed(agent_name, agent_id, wallet_id, amount_micro): Payment was signed
        - on_request_rejected(agent_id, reason): Payment was rejected
        - on_activity(message, is_error): Activity log message
        - on_transaction_updated(transaction_id): Transaction record was updated
        - on_hardware_sign_needed(typed_data, device_path, callback): Hardware wallet signing needed
            typed_data: EIP-712 typed data dict
            device_path: Derivation path on the Ledger device
            callback(signature_or_error): Called with signature hex or error string
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
        if on_hardware_sign_needed:
            self._on_hardware_sign_needed = on_hardware_sign_needed

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

    def _drop_pending(self, request_id: str) -> None:
        """Remove a request and its deadline together, so neither outlives the other."""
        self._pending_requests.pop(request_id, None)
        self._pending_deadlines.pop(request_id, None)

    def _expire_pending_requests(self) -> None:
        """Drop requests nobody approved in time.

        Run before every read of, or decision on, the pending set, so an expired
        request cannot be approved by whoever happens to look first.
        """
        now = time.monotonic()
        for request_id, deadline in list(self._pending_deadlines.items()):
            if deadline > now:
                continue
            request = self._pending_requests.get(request_id)
            self._drop_pending(request_id)
            if request is None:
                continue
            reason = (f"Expired: not approved within "
                      f"{PENDING_REQUEST_TTL_SECONDS // 60} minutes")
            result = {"status": "rejected", "reason": reason, "code": "REQUEST_EXPIRED"}
            if request.cache_key:
                self._signature_cache[request.cache_key] = (request_id, result)
            self._remember_outcome(request_id, result)
            self._emit_activity(
                f"Payment request from {request.agent_name} expired without approval",
                True)

    def _pending_count_for(self, agent_id: str) -> int:
        """How many requests this agent already has waiting."""
        return sum(1 for r in self._pending_requests.values() if r.agent_id == agent_id)

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

    def _remember_outcome(self, request_id: str, result: dict) -> dict:
        """Record a request's final answer for the agent to poll, oldest first out.

        dict preserves insertion order, so the oldest keys are the first ones.
        """
        if request_id:
            self._resolved[request_id] = result
            while len(self._resolved) > MAX_RESOLVED_REQUESTS:
                self._resolved.pop(next(iter(self._resolved)))
        return result

    def set_max_request_age(self, seconds: int):
        """Set the maximum age for signed requests (replay protection window)."""
        if seconds < 30:
            logger.warning(f"Max request age {seconds}s is very short, using 30s minimum")
            seconds = 30
        self._max_request_age_seconds = seconds
        logger.info(f"Max request age set to {seconds}s")

    def _check_asset_supported(self, chain_id: int, asset: str) -> Optional[str]:
        """Check the payment asset is USDG on this chain.

        Vault denominates every spending limit in USDG and reads the requested
        amount as micro-USDG. The asset is chosen by whoever issued the 402, so
        it is checked against the chain's USDG address here, before the amount
        is interpreted or any limit is applied.

        Returns None if the asset is supported, or an error message.
        """
        from ..networks import TOKENS

        expected = TOKENS["USDG"].addresses.get(chain_id)
        if not expected:
            return f"No supported payment asset is configured for chain {chain_id}"

        if not asset or asset.lower() != expected.lower():
            return (
                f"Unsupported payment asset {asset}. Vault settles x402 payments "
                f"in USDG ({expected}) on chain {chain_id}."
            )

        return None

    def _check_daily_reset(self, agent: "Agent") -> bool:
        """Check if agent's daily spending needs to be reset (new calendar day).

        Returns True if reset was performed.

        The reset writes `spent_today_micro`, the counter the binding spend check
        reads and increments under `_spending_lock`. So the reset is taken under
        that same lock, and re-confirms it is still due once it holds it: the
        first check is cheap but can be stale, and a reset that lands late - after
        another request (even an unauthenticated `/ping`) has already recorded a
        spend - would set the counter back to zero and hand out the day's
        allowance a second time. Re-checking under the lock closes that window.
        The disk write is done after the lock is released.
        """
        if not daily_allowance_is_due(agent.last_reset_date, agent.last_reset_at):
            return False
        with self._spending_lock:
            if not daily_allowance_is_due(agent.last_reset_date, agent.last_reset_at):
                return False
            old_spent = agent.spent_today_micro
            agent.reset_daily_spend()
        if self._policy_store:
            self._policy_store.update_agent(agent)
        if old_spent > 0:
            logger.info(f"Reset daily spending for agent {agent.id}: was {old_spent/1_000_000:.6f} USDG")
        return True

    def _queue_verification(self, tx: Transaction):
        """Queue a transaction for on-chain verification."""
        # Mark as pending verification
        tx.verification_status = VERIFY_PENDING
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
                tx.verification_status = VERIFY_NOT_FOUND
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
                tx.verification_status = VERIFY_UNAVAILABLE
                tx.verification_detail = f"unknown network {network_str}"
                if self._policy_store:
                    self._policy_store.update_transaction(tx)
                self._emit_transaction_updated(tx.id)
                return

            network = NETWORKS[chain_id]
            rpc_url = network.rpc_url
            if self._rpc_resolver is not None:
                try:
                    rpc_url = self._rpc_resolver(chain_id) or rpc_url
                except Exception:
                    logger.exception("RPC resolver failed for chain %s", chain_id)
            w3 = Web3(Web3.HTTPProvider(rpc_url))

            receipt = w3.eth.get_transaction_receipt(tx.tx_hash)
            if receipt and receipt.get("status") == 1:
                tx.verification_status = VERIFY_VERIFIED
                tx.verification_detail = None
                tx.verification_block = receipt.get("blockNumber")
                self._emit_activity(
                    f"Verified on-chain: {tx.tx_hash[:10]}... (block {tx.verification_block})",
                    False
                )
            elif receipt and receipt.get("status") == 0:
                tx.verification_status = VERIFY_FAILED
                tx.verification_detail = "the chain reports this transaction reverted"
                self._emit_activity(
                    f"Warning: tx {tx.tx_hash[:10]}... failed on-chain!",
                    True
                )
            else:
                tx.verification_status = VERIFY_NOT_FOUND
                self._emit_activity(
                    f"Warning: tx {tx.tx_hash[:10]}... not found on-chain",
                    True
                )
        except Exception as e:
            # Could not ask. That is not the same as being told no, and the
            # record has to say which, because "failed" against a payment is
            # read as a fact about the payment rather than about the network.
            logger.warning(f"Could not verify {tx.tx_hash}: {e}")
            self._emit_activity(
                f"Could not check {tx.tx_hash[:10]}... on-chain: {e}. "
                f"It will be retried.", True)
            tx.verification_status = VERIFY_UNAVAILABLE
            # Redacted: this reaches the AP2 receipt, which /receipt/{id} serves
            # unauthenticated, and the exception names the RPC URL (API key).
            from ..utils import redact_urls
            tx.verification_detail = redact_urls(str(e))

        # Persist the verification result
        if self._policy_store:
            self._policy_store.update_transaction(tx)
        self._emit_transaction_updated(tx.id)

    def verify_transaction(self, tx: Transaction):
        """Manually verify a transaction on-chain."""
        if tx.status != "settled" or not tx.tx_hash:
            self._emit_activity("Can only verify settled transactions with tx_hash", True)
            return

        tx.verification_status = VERIFY_PENDING
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
            if (tx.status == "settled" and tx.tx_hash
                    and tx.verification_status in (VERIFY_PENDING, VERIFY_UNAVAILABLE)):
                self._queue_verification(tx)
                stuck_count += 1
        if stuck_count:
            logger.info(f"Re-queued {stuck_count} stuck verification(s) from previous session")

    def set_wallet_provider(self, provider):
        """Set the wallet provider function (gets unlocked wallet by address)."""
        self._wallet_provider = provider

    def set_reserved_volume_provider(self, provider):
        """Set the callable that reports an agent's in-flight trading volume."""
        self._reserved_volume_provider = provider

    def set_wallet_status_checker(self, checker):
        """Set callable that returns True if any wallet is currently unlocked."""
        self._wallet_status_checker = checker

    def set_rpc_resolver(self, resolver):
        """Inject Vault.get_rpc_url so settlement verification uses the user's
        endpoint if they set one. Unset, the network's built-in endpoint is used."""
        self._rpc_resolver = resolver

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
        data_key: Optional[bytes],
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
            data_key: The unlocked wallet's master key, to decrypt the agent's
                shared secret (HMAC mode only; unused for bearer)
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
            agent, agent_id, signature_header, data_key, signed_fields)

    def verify_agent_signature(
        self,
        agent: Agent,
        agent_id: str,
        signature_header: str,
        data_key: Optional[bytes],
        signed_fields: dict,
    ) -> Optional[str]:
        """Verify an agent's bearer/HMAC auth over an arbitrary signed payload.

        `signed_fields` is merged into the HMAC message as
        ``{agent_id, timestamp, **signed_fields}`` (sorted keys). /sign passes the
        payment fields; /trade passes ``{"trade": {...}}``. Returns None on
        success, or an error string. HMAC mode needs the unlocked wallet's
        master key to decrypt the agent's shared secret; bearer mode ignores it.
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
            shared_secret = agent.decrypt_auth_key(data_key)
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
        data_key: Optional[bytes]
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
            shared_secret = agent.decrypt_auth_key(data_key)
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

        # Deliberately spare. /ping carries no signature - it is what an agent
        # calls to confirm its wiring before it can sign anything, so requiring
        # one would make setup impossible to debug.
        #
        # That means anyone who knows an agent id gets this reply, and an id is
        # six characters that travel in environment variables and logs. So it
        # answers only "is this agent able to work", and nothing about the
        # policy behind it. In particular it must not disclose the daily limit,
        # the spend so far, or auto_approve_below_micro - the last of which is
        # precisely the figure needed to size a payment that never prompts the
        # user.
        #
        # An agent that wants its own limits asks /mandate, which is signed and
        # returns them in full.
        return {
            "status": "ready",
            "agent_name": agent.name,
            "agent_status": agent.status,
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

        # For bearer auth: verify token BEFORE the wallet check — bearer doesn't
        # need the wallet key, so a wrong token must return AUTH_FAILED, and a
        # locked wallet must not be revealed to a caller that hasn't authenticated
        # (the same lock-oracle /sign guards against). See handle_sign_request.
        if agent.auth_mode == "bearer":
            auth_result = self._verify_mandate_auth(agent, agent_id, signature, None)
            if auth_result:
                return {
                    "status": "error",
                    "error": auth_result,
                    "code": "AUTH_FAILED"
                }

        wallet: "VaultWallet" = self._wallet_provider(agent.wallet_address)
        if not wallet:
            if self._wallet_status_checker and self._wallet_status_checker():
                return {
                    "status": "error",
                    "error": "Agent's wallet address is not available in the unlocked wallet. Check that the correct wallet is open.",
                    "code": "WALLET_ADDRESS_NOT_FOUND"
                }
            # Locked wallet. An HMAC credential can only be verified with the
            # wallet key, so an HMAC caller has NOT authenticated - answering
            # WALLET_LOCKED would hand it the same lock oracle. A bearer caller
            # was already verified above, so it has earned the honest answer.
            if agent.auth_mode == "bearer":
                return {
                    "status": "error",
                    "error": "Wallet is locked. Open Vault and unlock the wallet.",
                    "code": "WALLET_LOCKED"
                }
            self._emit_activity(f"Auth failed for {agent.name}: wallet locked", True)
            return {
                "status": "error",
                "error": "Authentication failed",
                "code": "AUTH_FAILED"
            }

        # For HMAC auth: verify signature (needs the wallet key to decrypt the
        # agent's shared secret). Bearer was already verified above.
        if agent.auth_mode != "bearer":
            auth_result = self._verify_mandate_auth(agent, agent_id, signature, wallet.data_key)
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
                "trading": self._trading_summary(agent, policy),
            }

        # Calculate remaining budget. Reported whenever there is a policy at all:
        # a daily limit of 0 has 0 remaining, which is a true answer, not a
        # missing one.
        remaining_today_micro = None
        if policy:
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

    def _trading_summary(self, agent: "Agent", policy: "SpendPolicy") -> dict:
        """The trading limits an agent is held to, so it can stay inside them.

        The payment side has always published its limits here, and the trading
        skill tells agents to check before spending a request - but the trading
        limits were not returned by anything, so the only way to find them was to
        trip them. This closes that.

        Telling the agent costs nothing: every limit is enforced whatever the
        agent believes, and one that wanted to map them could do so by bisection
        anyway. Knowing max_price_impact_percent in particular steers it toward a
        sensible pool, which is the behaviour worth encouraging.
        """
        rules = policy.trading_rules
        if rules is None or not rules.enabled:
            return {"enabled": False}

        # Volume is reset lazily, when a trade is evaluated. A read must not
        # write, so report what the figure will be rather than what is stored -
        # otherwise an agent checking after midnight sees yesterday's total.
        #
        # Asking the agent the same question the trading service asks, rather
        # than comparing dates here: two copies of the rule would answer
        # differently the moment either changed, and this one is published.
        due = daily_allowance_is_due(
            agent.last_trading_reset_date, agent.last_trading_reset_at)
        used = 0.0 if due else agent.trading_volume_today_usd

        # Trades queued for approval have already claimed their volume, so a
        # remaining figure that ignored them would be one the agent cannot spend.
        promised = 0.0
        if self._reserved_volume_provider:
            try:
                promised = self._reserved_volume_provider(agent.id)
            except Exception:
                logger.exception("could not read in-flight trading volume")

        return {
            "enabled": True,
            "per_trade_max_usd": rules.per_trade_max_usd,
            "daily_volume_limit_usd": rules.daily_volume_limit_usd,
            "auto_approve_below_usd": rules.auto_approve_below_usd,
            "max_slippage_percent": rules.max_slippage_percent,
            "max_price_impact_percent": rules.max_price_impact_percent,
            "min_reserve_eth": rules.min_reserve_eth,
            "volume_today_usd": used,
            "committed_today_usd": used + promised,
            "remaining_today_usd": max(
                0.0, rules.daily_volume_limit_usd - used - promised),
        }

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
                data_key=None,
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
            # Wallet is locked. An HMAC credential can only be verified with the
            # wallet key, so we have NOT authenticated this caller yet - and
            # answering WALLET_LOCKED here would tell an unauthenticated caller
            # the wallet is locked (a "keys are/aren't in memory" oracle they
            # could poll). Answer AUTH_FAILED instead, identical to an unlocked
            # wallet's answer to a bad credential. A bearer agent was already
            # verified above, so it has earned the honest WALLET_LOCKED.
            if agent.auth_mode == "bearer":
                return {
                    "status": "error",
                    "error": "Wallet is locked. Open Vault and unlock the wallet to enable signing.",
                    "code": "WALLET_LOCKED"
                }
            self._emit_activity(f"Auth failed for {agent.name}: wallet locked", True)
            return {
                "status": "error",
                "error": "Authentication failed",
                "code": "AUTH_FAILED"
            }

        # For HMAC auth: verify signature (needs wallet password to decrypt auth key)
        if agent.auth_mode != "bearer":
            auth_result = self._verify_agent_auth(
                agent, agent_id, signature,
                data_key=wallet.data_key,
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

        # In-memory cache miss. After a restart it is empty, but the payment's
        # transaction record is on disk - so consult it before signing again,
        # or one purchase becomes two independently redeemable authorizations.
        if signature and self._policy_store:
            prior = self._policy_store.get_transaction_by_cache_key(cache_key)
            if prior is not None:
                if prior.status == "settled" or prior.verification_status == "verified":
                    return {
                        "status": "error",
                        "code": "PAYMENT_ALREADY_SETTLED",
                        "error": "This payment was already settled on-chain. Use idempotency_key for a fresh payment.",
                        "previous_transaction_id": prior.id,
                        "hint": "Add 'idempotency_key': 'unique-string' to your request"
                    }
                # A prior authorization exists and may still be redeemable within
                # its validity window; signing a second one is the double-spend.
                return {
                    "status": "error",
                    "code": "PAYMENT_ALREADY_AUTHORIZED",
                    "error": "A payment for this request was already authorized and may still be redeemable. Use idempotency_key for a fresh payment.",
                    "previous_transaction_id": prior.id,
                    "hint": "Add 'idempotency_key': 'unique-string' to your request"
                }

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

        # Asset check runs before any limit is applied: the amount above was read
        # as micro-USDG, which only holds for USDG.
        asset_error = self._check_asset_supported(chain_id, requirements.asset)
        if asset_error:
            self._emit_activity(f"Request from {agent.name} rejected: {asset_error}", True)
            server_stats.rejected += 1
            tx = self._create_rejection_transaction(
                agent, amount_micro, network, recipient, asset_error, resource,
                decoded_x402_data, request_url=request_url
            )
            return {
                "status": "error",
                "error": asset_error,
                "code": "UNSUPPORTED_ASSET",
                "transaction_id": tx.id
            }

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

        # `is not None`, not truthiness: None is this field's "no per-request cap",
        # but 0 is a cap of zero and must refuse everything.
        if policy.per_request_max_micro is not None and amount_micro > policy.per_request_max_micro:
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

        # Unconditional: daily_limit_micro is always an integer, and 0 is a limit
        # of zero. Guarding this with `if policy.daily_limit_micro:` skips the
        # check entirely for a policy set to 0 - see models/policy.py.
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
            self._expire_pending_requests()
            if self._pending_count_for(agent_id) >= MAX_PENDING_PER_AGENT:
                reason = (f"{MAX_PENDING_PER_AGENT} requests from this agent are "
                          f"already waiting for approval")
                self._emit_activity(f"Request from {agent.name} refused: {reason}", True)
                server_stats.rejected += 1
                return {"status": "error", "error": reason, "code": "TOO_MANY_PENDING"}

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
            self._pending_deadlines[request_id] = (
                time.monotonic() + PENDING_REQUEST_TTL_SECONDS)

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
            request_url=request_url,
            cache_key=cache_key
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
        self._expire_pending_requests()
        request = self._pending_requests.get(request_id)
        if not request:
            return {"status": "error", "error": "Request not found or expired", "code": "REQUEST_NOT_FOUND"}

        if request.status != "pending":
            return {"status": "error", "error": f"Request already {request.status}", "code": "REQUEST_ALREADY_PROCESSED"}

        agent = self._policy_store.get_agent_by_id(request.agent_id)
        policy = self._policy_store.get_policy(agent.policy_id) if agent else None

        if not agent or not policy:
            self._drop_pending(request_id)
            return {"status": "error", "error": "Agent or policy no longer exists", "code": "AGENT_OR_POLICY_MISSING"}

        # Re-validate agent status (may have changed since request was queued)
        if agent.status == "suspended":
            self._drop_pending(request_id)
            return {"status": "error", "error": "Agent is now suspended", "code": "AGENT_SUSPENDED"}

        if agent.status == "limit_reached":
            self._drop_pending(request_id)
            return {"status": "error", "error": "Agent has reached daily limit", "code": "LIMIT_REACHED"}

        # Re-validate that payments are still switched on for this policy.
        # Every other check below is re-run at approval; this one and the domain
        # rules below were not, so turning payments off or blocking a domain left
        # anything already queued able to sail through.
        if not policy.is_x402_enabled():
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": "x402 payments are no longer enabled for this policy",
                "code": "X402_DISABLED"
            }

        # Re-validate domain rules against the same URL the request was judged on.
        domain_check_url = request.request_url or request.resource
        if policy.has_domain_restrictions() and not domain_check_url:
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": "Domain restrictions now apply but this request carries no URL to check",
                "code": "DOMAIN_URL_REQUIRED"
            }
        domain_allowed, domain_reason = policy.check_domain_allowed(domain_check_url)
        if not domain_allowed:
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": domain_reason,
                "code": "DOMAIN_NOT_ALLOWED"
            }

        # Re-validate policy limits (may have changed since request was queued).
        # Both tests are written the same way as at intake: `is not None` where
        # None means "no cap", unconditional where every policy carries a number.
        if policy.per_request_max_micro is not None and request.amount_micro > policy.per_request_max_micro:
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": f"Amount {request.amount_micro/1_000_000:.6f} USDG exceeds current per-request limit {policy.per_request_max_micro/1_000_000:.6f} USDG",
                "code": "EXCEEDS_PER_REQUEST_MAX"
            }

        remaining = policy.daily_limit_micro - agent.spent_today_micro
        if request.amount_micro > remaining:
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": f"Amount {request.amount_micro/1_000_000:.6f} USDG exceeds remaining daily limit {remaining/1_000_000:.6f} USDG",
                "code": "EXCEEDS_DAILY_LIMIT"
            }

        # Re-validate network is still enabled (global setting).
        #
        # The network is a fact about the payment, so it comes from the parsed
        # request - not a fresh dig into the raw agent message. Re-reading the
        # wire fields here would return "unknown" for anything the parser
        # accepted but that does not carry the field at that exact path, and
        # "unknown" yields chain_id 0, which the guards below treat as "nothing
        # to check" and skip. The policy state either side of this is still read
        # live, which is the point of re-validating at approval time.
        network = request.network
        caip_network = network_to_caip(network)
        try:
            chain_id = int(caip_network.split(":")[1]) if ":" in caip_network else 0
        except (ValueError, IndexError):
            chain_id = 0

        if chain_id and not self.is_network_enabled(chain_id):
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": f"Network {network} is no longer enabled",
                "code": "NETWORK_DISABLED"
            }

        # Re-validate network is allowed by policy (may have changed)
        if chain_id and policy.networks and chain_id not in policy.networks:
            self._drop_pending(request_id)
            return {
                "status": "error",
                "error": f"Network {network} is no longer allowed by policy",
                "code": "NETWORK_NOT_ALLOWED_BY_POLICY"
            }

        result = self._sign_payment(
            agent, policy, request.x402_data, request.amount_micro, request.x402_version,
            auto_approved=False,  # Manual approval
            request_url=request.request_url,
            cache_key=request.cache_key
        )

        if result.get("status") == "success":
            request.status = "approved"
            # Update signature cache with the signed result
            if request.cache_key:
                tx_id = result.get("transaction_id", request_id)
                self._signature_cache[request.cache_key] = (tx_id, result)
            self._drop_pending(request_id)
        else:
            # Signing failed after the user approved. Drop the idempotency entry
            # so the agent can submit the payment again, but keep the outcome
            # here - otherwise the agent polls a request that no longer exists
            # anywhere and cannot tell failure from never having been accepted.
            if request.cache_key and request.cache_key in self._signature_cache:
                del self._signature_cache[request.cache_key]
            self._drop_pending(request_id)

        self._remember_outcome(request_id, result)

        return result

    def reject_request(self, request_id: str, reason: str = "User rejected") -> dict:
        """Reject a pending request."""
        self._expire_pending_requests()
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

        self._drop_pending(request_id)
        self._remember_outcome(request_id, result)
        return result

    def get_pending_requests(self) -> list[SigningRequest]:
        """Get all pending approval requests."""
        self._expire_pending_requests()
        return list(self._pending_requests.values())

    def get_request_status(self, request_id: str) -> dict:
        """
        Get the status of a signing request.

        Returns the current status and, if approved, the signed payment header.
        This allows agents to poll for approval and retrieve the payment header.
        """
        # Check pending requests first
        self._expire_pending_requests()
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

        # A terminal outcome recorded when the request was resolved. This is
        # what answers a poll after a failed signing, where the idempotency
        # entry above was deliberately dropped.
        resolved = self._resolved.get(request_id)
        if resolved is not None:
            return resolved

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
        request_url: Optional[str] = None,
        cache_key: Optional[str] = None
    ) -> dict:
        """Actually sign a payment using the wallet."""
        try:
            # If the transaction ledger cannot be written - its file was
            # unreadable at startup and is now protected from being overwritten -
            # refuse rather than sign a payment Vault cannot record. That record
            # is the cross-restart idempotency guard (without it a retry becomes a
            # second redeemable authorization for the same purchase) and it is
            # where the payment shows up in the user's history. Fail closed and
            # tell the user, instead of signing blind and carrying on silently.
            _is_protected = getattr(self._policy_store, "is_transactions_protected", None)
            if callable(_is_protected) and _is_protected():
                self._emit_activity(
                    f"Payment for {agent.name} ({agent.id}) refused: Vault cannot "
                    f"write the transaction record. Restart Vault when "
                    f"transactions.json is readable.", True)
                return {
                    "status": "error",
                    "error": ("Vault cannot record this payment because the "
                              "transaction file is unavailable, and will not sign "
                              "a payment it cannot log. Restart Vault when the "
                              "file is readable."),
                    "code": "LEDGER_UNWRITABLE",
                }

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
            from .eip3009 import parse_x402, create_payment, _select_offer

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

            # The offer parse_x402 actually settled on. On a multi-offer 402 this
            # is not accepts[0], and both values taken from it below must describe
            # the offer that gets signed: the audit record's network (or on-chain
            # verification resolves the wrong chain and never confirms the
            # payment) and the `accepted` echo (or the merchant reconstructs the
            # wrong EIP-712 domain and rejects a valid signature).
            selected_accept = _select_offer(x402_data.get("accepts", [{}]))

            # Keep the merchant's raw network string for the audit record.
            original_network = selected_accept.get("network", "")
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

            # Every signing route converges here, so the asset is confirmed once
            # more immediately before the signature is produced.
            asset_error = self._check_asset_supported(
                requirements.chain_id, requirements.asset)
            if asset_error:
                self._emit_activity(f"Signing blocked: {asset_error}", True)
                return {
                    "status": "error",
                    "error": asset_error,
                    "code": "UNSUPPORTED_ASSET"
                }

            # Bind the signature to the amount that was checked. amount_micro is
            # the figure the limits were applied to, the daily spend is debited
            # by, and the transaction records; the value about to be signed is
            # read from the payload. In normal operation they are the same parse
            # and always equal - but the signature must never commit to an
            # amount that was never limit-checked, so refuse if they diverge.
            if int(requirements.max_amount_required) != amount_micro:
                return {
                    "status": "error",
                    "code": "AMOUNT_MISMATCH",
                    "error": ("Payment amount does not match the checked amount; "
                              "refusing to sign."),
                }

            # Check if this is a Ledger address
            if addr_entry.is_hardware:
                # Ledger hardware wallet signing
                if not self._on_hardware_sign_needed:
                    return {
                        "status": "error",
                        "error": "Ledger signing not available (GUI required)",
                        "code": "LEDGER_SIGN_NOT_AVAILABLE"
                    }

                # Import Ledger-specific functions
                from .eip3009 import build_transfer_authorization_typed_data, create_payment_from_signature

                # Use provided token metadata, fall back to requirements, then defaults
                name = requirements.token_name or "Global Dollar"
                version = requirements.token_version or "1"

                # Generate parameters for signing
                nonce_bytes = secrets.token_bytes(32)
                nonce_hex = "0x" + nonce_bytes.hex()
                now = int(time.time())
                valid_after = now - 60  # Valid 1 minute ago (clock skew tolerance)
                valid_before = now + 3600  # Valid for 1 hour

                # Build the typed data for the Ledger to sign
                typed_data = build_transfer_authorization_typed_data(
                    chain_id=requirements.chain_id,
                    token_address=requirements.asset,
                    token_name=name,
                    token_version=version,
                    from_address=agent.wallet_address,
                    to_address=requirements.pay_to,
                    value=int(requirements.max_amount_required),
                    valid_after=valid_after,
                    valid_before=valid_before,
                    nonce=nonce_bytes
                )

                # Call the Ledger signing callback (blocking - shows dialog)
                # The callback should return the signature hex string or raise an exception
                try:
                    signature = self._on_hardware_sign_needed(
                        typed_data,
                        addr_entry.device_path,
                        agent.wallet_address  # For verification
                    )
                except Exception as e:
                    error_msg = str(e)
                    if "rejected" in error_msg.lower() or "cancelled" in error_msg.lower():
                        return {
                            "status": "error",
                            "error": "Signing rejected on Ledger device",
                            "code": "LEDGER_REJECTED"
                        }
                    elif "disconnected" in error_msg.lower():
                        return {
                            "status": "error",
                            "error": "Ledger device disconnected",
                            "code": "LEDGER_DISCONNECTED"
                        }
                    return {
                        "status": "error",
                        "error": f"Ledger signing failed: {error_msg}",
                        "code": "LEDGER_ERROR"
                    }

                if not signature:
                    return {
                        "status": "error",
                        "error": "No signature received from Ledger",
                        "code": "LEDGER_NO_SIGNATURE"
                    }

                # Create payment from the Ledger signature
                payment = create_payment_from_signature(
                    from_address=agent.wallet_address,
                    requirements=requirements,
                    signature=signature,
                    valid_after=valid_after,
                    valid_before=valid_before,
                    nonce_hex=nonce_hex,
                    accepted=selected_accept,
                    resource=x402_data.get("resource"),
                )
            else:
                # Software wallet signing
                private_key_bytes = wallet.get_private_key(addr_entry.id)
                private_key_hex = private_key_bytes.hex()

                # Create payment using local EIP-3009 signing. Echo the merchant's
                # selected offer (`accepted`) and resource so the v2 payload is
                # spec-shaped; the signature itself is unaffected by these fields.
                payment = create_payment(
                    private_key_hex, requirements,
                    accepted=selected_accept,
                    resource=x402_data.get("resource"),
                )

            if x402_version == 1:
                payment = self._convert_payment_to_v1(payment)

            # Atomically check and update spending limits to prevent race conditions
            with self._spending_lock:
                # Re-check daily limit inside lock (initial check may have been racy).
                # This is the binding check - the two before it are advisory - so a
                # policy of 0 has to refuse here above all.
                remaining = policy.daily_limit_micro - agent.spent_today_micro
                if amount_micro > remaining:
                    return {
                        "status": "error",
                        "error": "Would exceed daily limit (concurrent request)",
                        "code": "EXCEEDS_DAILY_LIMIT",
                        "requested_micro": amount_micro,
                        "remaining_micro": remaining
                    }
                # The spend is recorded here, when the authorization is created,
                # and nothing anywhere ever subtracts it. A payment the merchant
                # rejects, one that reverts on-chain, one that is never redeemed:
                # all keep their spend until the next daily reset.
                #
                # That is deliberate, and load-bearing. The authorization is a
                # bearer instrument valid for a full hour (valid_before, below),
                # Vault cannot revoke it, and the only word that it failed arrives
                # over an agent-authenticated channel. Refund on that word and an
                # agent with a cooperative merchant reports every payment failed,
                # reclaims the budget, and redeems the signatures anyway - on-chain
                # spend then exceeds the daily limit without bound. A confirmed
                # revert is no safer: the signature stays good until it expires.
                #
                # The trading lane commits its volume at broadcast for the same
                # reason. Do not "fix" either one into refunding.
                agent.spent_today_micro += amount_micro
                if agent.spent_today_micro >= policy.daily_limit_micro:
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

            # Persist the idempotency key with the record so a duplicate request
            # after a restart is recognised from disk, not only from memory.
            if cache_key:
                tx.cache_key = cache_key

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
        error: Optional[str] = None,
        signature: Optional[str] = None
    ) -> dict:
        """
        Handle a callback from an agent reporting transaction status.

        Authenticated exactly like /sign and /trade: a callback writes the
        settlement record, so it has to prove it comes from the agent whose
        payment it claims to be reporting.

        Args:
            agent_id: The agent's ID
            transaction_id: The transaction ID from the sign response
            event: One of 'submitted', 'settled', 'failed'
            tx_hash: Transaction hash (required for 'settled')
            error: Error message (optional for 'failed')
            signature: Bearer token, or SIG:ts:hex over the callback fields

        Returns:
            Status response dict
        """
        if not self._policy_store:
            return {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"}

        # Verify agent exists
        agent = self._policy_store.get_agent_by_id(agent_id)
        if not agent:
            return {"status": "error", "error": "Agent not found", "code": "AGENT_NOT_FOUND"}

        auth_error = self._verify_callback_auth(
            agent, agent_id, signature, transaction_id, event, tx_hash)
        if auth_error:
            return auth_error

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
            # The allowance this payment consumed is not returned, and the user
            # deserves to hear why their remaining budget dropped for a payment
            # that visibly failed: the signed authorization is a bearer
            # instrument that stays redeemable until it expires, whatever this
            # callback says. See the comment at the spend increment in
            # _sign_payment.
            self._emit_activity(
                f"Payment failed for {agent.name}: {error or 'Unknown error'}. "
                f"Its {tx.format_amount()} stays spent until the daily reset - "
                f"the signed authorization remains redeemable.",
                True
            )
        else:
            return {"status": "error", "error": f"Unknown event: {event}", "code": "INVALID_EVENT"}

        self._policy_store.update_transaction(tx)
        self._emit_transaction_updated(tx.id)

        return {"status": "ok", "transaction_id": tx.id, "new_status": tx.status}

    def _verify_callback_auth(
        self,
        agent: Agent,
        agent_id: str,
        signature: Optional[str],
        transaction_id: str,
        event: str,
        tx_hash: Optional[str],
    ) -> Optional[dict]:
        """Authenticate a settlement callback. Returns None on success.

        Bearer agents are checked without the wallet, as elsewhere. An HMAC
        credential can only be verified with the wallet's master key, so when the
        wallet is locked the caller has NOT been authenticated - and answering
        WALLET_LOCKED would hand an unauthenticated caller a pollable "keys are in
        memory now" oracle, the same one /sign and /mandate refuse to give. So a
        locked wallet answers AUTH_FAILED, identical to an unlocked wallet's
        answer to a bad credential.
        """
        signed_fields = {
            "callback": {
                "transaction_id": transaction_id,
                "event": event,
                "tx_hash": tx_hash,
            }
        }

        if agent.auth_mode == "bearer":
            err = self.verify_agent_signature(agent, agent_id, signature, None, signed_fields)
            return None if not err else {
                "status": "error", "error": err, "code": "AUTH_FAILED"}

        if not self._wallet_provider:
            return {"status": "error", "error": "Wallet provider not set",
                    "code": "NO_WALLET_PROVIDER"}
        wallet = self._wallet_provider(agent.wallet_address)
        if not wallet:
            self._emit_activity(f"Auth failed for {agent.name}: wallet locked", True)
            return {"status": "error", "error": "Authentication failed",
                    "code": "AUTH_FAILED"}

        err = self.verify_agent_signature(
            agent, agent_id, signature, wallet.data_key, signed_fields)
        return None if not err else {
            "status": "error", "error": err, "code": "AUTH_FAILED"}

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

        USDG has 6 decimals, so the atomic amount is already micro-USDG
        (1_000_000 = $1.00). `_check_asset_supported` guarantees the asset is
        USDG before this runs.

        Raises ValueError on a non-integer, non-positive, or absurdly large
        amount.
        """
        try:
            amount_micro = int(raw_amount)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid amount: {raw_amount!r}") from e

        # Bounds validation
        if amount_micro <= 0:
            raise ValueError(f"amount must be positive, got {amount_micro}")
        if amount_micro > 10**15:  # $1 billion in micro-USDG - unreasonable upper bound
            raise ValueError(f"amount exceeds maximum ({amount_micro} > 10^15)")

        return amount_micro
