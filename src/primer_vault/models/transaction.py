"""
Transaction model.

Records payment signing requests, trades, and transfers.

Transaction types:
- x402: Payment for API access via x402 protocol
- trade: Token swap via Uniswap
- transfer: Outbound token transfer

Status lifecycle:
- received: Request received from agent
- signed: Payment signed and returned to agent (x402 only)
- rejected: Request rejected (policy, limit, user denied, etc.)
- submitted: Agent sent payment header to target API (x402 only)
- settled: Confirmed on-chain (with tx_hash)
- failed: Failed on-chain
"""

import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional


# Valid status values
STATUS_RECEIVED = "received"
STATUS_SIGNED = "signed"
STATUS_REJECTED = "rejected"
STATUS_SUBMITTED = "submitted"
STATUS_SETTLED = "settled"
STATUS_FAILED = "failed"

# Transaction types
TYPE_X402 = "x402"
TYPE_TRADE = "trade"
TYPE_TRANSFER = "transfer"


@dataclass
class Transaction:
    """A payment, trade, or transfer record."""
    id: str
    timestamp: str                    # ISO format - when request was received
    agent_id: str                     # Short agent ID (e.g., "ABC123") - user-facing
    agent_name: str
    agent_code: str                   # Agent internal code (UUID) - for storage lookups
    amount_micro: int                 # Amount in micro-USDG (6 decimals: 1_000_000 = $1.00)
    recipient: str                    # payTo address or resource URL
    network: str                      # Network identifier (CAIP-2 or v1 name)
    status: str                       # received | signed | rejected | submitted | settled | failed
    auto_approved: bool               # Was this auto-approved by policy?
    type: str = TYPE_X402             # x402 | trade | transfer
    wallet_address: Optional[str] = None  # Wallet used for signing
    wallet_id: Optional[str] = None   # Wallet ID (W001, etc.)
    tx_hash: Optional[str] = None     # On-chain tx hash if settled
    resource: Optional[str] = None    # Resource URL from x402 request (often path-only)
    request_url: Optional[str] = None # Full URL agent fetched (for domain verification/audit)
    reject_reason: Optional[str] = None  # Reason for rejection
    x402_data: Optional[dict] = None  # Full x402 payload for detail view
    mandate_id: Optional[str] = None  # AP2 IntentMandate ID
    signed_at: Optional[str] = None   # When we signed it
    submitted_at: Optional[str] = None  # When agent submitted to target API
    settled_at: Optional[str] = None  # When settled on-chain
    verification_status: Optional[str] = None  # None, "verified", "failed", "not_found", "pending"
    verification_block: Optional[int] = None   # Block number if verified
    # Trade-specific fields
    token_in: Optional[str] = None    # Input token address (trade)
    token_out: Optional[str] = None   # Output token address (trade)
    symbol_in: Optional[str] = None   # Input token symbol (trade)
    symbol_out: Optional[str] = None  # Output token symbol (trade)
    amount_in: Optional[str] = None   # Amount of input token (trade, raw string)
    amount_out: Optional[str] = None  # Amount of output token received (trade)
    fee_tier: Optional[int] = None    # Uniswap fee tier in bps (trade)
    slippage_bps: Optional[int] = None  # Slippage tolerance in bps (trade)
    pool: Optional[str] = None        # Pool address used (trade)
    # Transfer-specific fields
    transfer_token: Optional[str] = None   # Token address (transfer, None = native)
    transfer_symbol: Optional[str] = None  # Token symbol (transfer)
    transfer_amount: Optional[str] = None  # Amount transferred (raw string)

    @classmethod
    def create(
        cls,
        agent_id: str,
        agent_name: str,
        agent_code: str,
        amount_micro: int,
        recipient: str,
        network: str,
        status: str = STATUS_RECEIVED,
        auto_approved: bool = False,
        tx_type: str = TYPE_X402,
        wallet_address: Optional[str] = None,
        wallet_id: Optional[str] = None,
        resource: Optional[str] = None,
        request_url: Optional[str] = None,
        x402_data: Optional[dict] = None,
        # Trade fields
        token_in: Optional[str] = None,
        token_out: Optional[str] = None,
        symbol_in: Optional[str] = None,
        symbol_out: Optional[str] = None,
        amount_in: Optional[str] = None,
        amount_out: Optional[str] = None,
        fee_tier: Optional[int] = None,
        slippage_bps: Optional[int] = None,
        pool: Optional[str] = None,
        # Transfer fields
        transfer_token: Optional[str] = None,
        transfer_symbol: Optional[str] = None,
        transfer_amount: Optional[str] = None,
    ) -> "Transaction":
        """Create a new transaction record."""
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            agent_name=agent_name,
            agent_code=agent_code,
            amount_micro=amount_micro,
            recipient=recipient,
            network=network,
            status=status,
            auto_approved=auto_approved,
            type=tx_type,
            wallet_address=wallet_address,
            wallet_id=wallet_id,
            resource=resource,
            request_url=request_url,
            x402_data=x402_data,
            token_in=token_in,
            token_out=token_out,
            symbol_in=symbol_in,
            symbol_out=symbol_out,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_tier=fee_tier,
            slippage_bps=slippage_bps,
            pool=pool,
            transfer_token=transfer_token,
            transfer_symbol=transfer_symbol,
            transfer_amount=transfer_amount,
        )

    @classmethod
    def create_trade(
        cls,
        agent_id: str,
        agent_name: str,
        agent_code: str,
        network: str,
        token_in: str,
        token_out: str,
        symbol_in: str,
        symbol_out: str,
        amount_in: str,
        fee_tier: int,
        wallet_address: str,
        wallet_id: str,
        slippage_bps: int = 50,
        pool: Optional[str] = None,
        auto_approved: bool = False,
    ) -> "Transaction":
        """Create a trade transaction record."""
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            agent_name=agent_name,
            agent_code=agent_code,
            amount_micro=0,  # Not used for trades
            recipient="",    # Not used for trades
            network=network,
            status=STATUS_RECEIVED,
            auto_approved=auto_approved,
            type=TYPE_TRADE,
            wallet_address=wallet_address,
            wallet_id=wallet_id,
            token_in=token_in,
            token_out=token_out,
            symbol_in=symbol_in,
            symbol_out=symbol_out,
            amount_in=amount_in,
            fee_tier=fee_tier,
            slippage_bps=slippage_bps,
            pool=pool,
        )

    @classmethod
    def create_transfer(
        cls,
        agent_id: str,
        agent_name: str,
        agent_code: str,
        network: str,
        recipient: str,
        transfer_amount: str,
        wallet_address: str,
        wallet_id: str,
        transfer_token: Optional[str] = None,
        transfer_symbol: str = "ETH",
        auto_approved: bool = False,
    ) -> "Transaction":
        """Create an outbound transfer transaction record."""
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            agent_name=agent_name,
            agent_code=agent_code,
            amount_micro=0,  # Not used for transfers
            recipient=recipient,
            network=network,
            status=STATUS_RECEIVED,
            auto_approved=auto_approved,
            type=TYPE_TRANSFER,
            wallet_address=wallet_address,
            wallet_id=wallet_id,
            transfer_token=transfer_token,
            transfer_symbol=transfer_symbol,
            transfer_amount=transfer_amount,
        )

    def mark_signed(self, wallet_address: str, wallet_id: str, auto_approved: bool = False) -> None:
        """Mark transaction as signed."""
        self.status = STATUS_SIGNED
        self.wallet_address = wallet_address
        self.wallet_id = wallet_id
        self.auto_approved = auto_approved
        self.signed_at = datetime.now(timezone.utc).isoformat()

    def mark_rejected(self, reason: str) -> None:
        """Mark transaction as rejected."""
        self.status = STATUS_REJECTED
        self.reject_reason = reason

    def mark_submitted(self) -> None:
        """Mark transaction as submitted to target API by agent."""
        self.status = STATUS_SUBMITTED
        self.submitted_at = datetime.now(timezone.utc).isoformat()

    def mark_settled(self, tx_hash: str) -> None:
        """Mark transaction as settled on-chain."""
        self.status = STATUS_SETTLED
        self.tx_hash = tx_hash
        self.settled_at = datetime.now(timezone.utc).isoformat()

    def mark_failed(self, error: Optional[str] = None) -> None:
        """Mark transaction as failed."""
        self.status = STATUS_FAILED
        if error:
            self.reject_reason = error

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return asdict(self)

    # Valid status values for validation
    VALID_STATUSES = (STATUS_RECEIVED, STATUS_SIGNED, STATUS_REJECTED, STATUS_SUBMITTED, STATUS_SETTLED, STATUS_FAILED)
    VALID_TYPES = (TYPE_X402, TYPE_TRADE, TYPE_TRANSFER)

    @classmethod
    def from_dict(cls, data: dict) -> "Transaction":
        """Create from dictionary with input validation."""
        # Validate amount is non-negative
        amount = data.get("amount_micro", 0)
        if not isinstance(amount, int) or amount < 0:
            raise ValueError(f"amount_micro must be non-negative integer, got {amount}")

        status = data.get("status", "")
        if status not in cls.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {cls.VALID_STATUSES}")

        # Default to x402 if type not specified
        if "type" not in data:
            data["type"] = TYPE_X402

        tx_type = data.get("type", TYPE_X402)
        if tx_type not in cls.VALID_TYPES:
            raise ValueError(f"Invalid type: {tx_type}. Must be one of {cls.VALID_TYPES}")

        return cls(**data)

    def format_amount(self) -> str:
        """Format amount as USDG with 6 decimal precision."""
        return f"{self.amount_micro / 1_000_000:.6f} USDG"

    def format_amount_precise(self) -> str:
        """Format amount with full 6-decimal precision for formal documents."""
        return f"{self.amount_micro / 1_000_000:.6f} USDG"

    def format_time(self) -> str:
        """Format timestamp as MM-DD HH:MM (11 chars)."""
        try:
            dt = datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
            return dt.strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            return self.timestamp[:11] if len(self.timestamp) >= 11 else self.timestamp

    def format_date(self) -> str:
        """Format timestamp as YYYY-MM-DD."""
        try:
            dt = datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            return self.timestamp[:10] if len(self.timestamp) >= 10 else self.timestamp

    def format_datetime(self) -> str:
        """Format timestamp as YYYY-MM-DD HH:MM:SS."""
        try:
            dt = datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            return self.timestamp

    # -------------------------------------------------------------------------
    # Type-aware display helpers (for unified history display)
    # -------------------------------------------------------------------------

    def display_out(self) -> str:
        """What was spent/sent (unified column for all types)."""
        if self.type == TYPE_TRADE:
            sym = self.symbol_in or "?"
            return f"{self.amount_in} {sym}" if self.amount_in else "?"
        elif self.type == TYPE_TRANSFER:
            sym = self.transfer_symbol or "ETH"
            return f"{self.transfer_amount} {sym}" if self.transfer_amount else "?"
        else:  # x402
            return self.format_amount()

    def display_in(self) -> str:
        """What was received (unified column for all types)."""
        if self.type == TYPE_TRADE:
            sym = self.symbol_out or "?"
            return f"{self.amount_out} {sym}" if self.amount_out else "?"
        elif self.type == TYPE_TRANSFER:
            # Transfers out don't "receive" anything
            return "—"
        else:  # x402
            # For x402, the "in" is the resource/API access
            return self.resource or self.request_url or "—"

    def display_type_label(self) -> str:
        """Human-readable type label."""
        return {
            TYPE_X402: "x402",
            TYPE_TRADE: "Trade",
            TYPE_TRANSFER: "Transfer",
        }.get(self.type, self.type)

    def display_counterparty(self) -> str:
        """Who we transacted with (recipient for transfers/x402, pool for trades)."""
        if self.type == TYPE_TRADE:
            return self.pool[:10] + "…" if self.pool and len(self.pool) > 12 else (self.pool or "—")
        else:
            return self.recipient[:10] + "…" if self.recipient and len(self.recipient) > 12 else (self.recipient or "—")

    @property
    def is_pending(self) -> bool:
        """Check if transaction is still pending (received or signed but not settled)."""
        return self.status in (STATUS_RECEIVED, STATUS_SIGNED, STATUS_SUBMITTED)

    @property
    def is_complete(self) -> bool:
        """Check if transaction is complete (settled, rejected, or failed)."""
        return self.status in (STATUS_SETTLED, STATUS_REJECTED, STATUS_FAILED)

    def to_ap2_receipt(self, policy_name: Optional[str] = None) -> dict:
        """
        Generate an AP2-formatted receipt for this transaction.

        This provides an auditable record in AP2-compatible format showing:
        - Intent (what was authorized via policy)
        - Authorization (who approved, when)
        - Settlement (on-chain proof)
        """
        # Map our status to AP2 payment status
        ap2_status_map = {
            STATUS_RECEIVED: "payment-required",
            STATUS_SIGNED: "payment-verified",
            STATUS_REJECTED: "payment-rejected",
            STATUS_SUBMITTED: "payment-submitted",
            STATUS_SETTLED: "payment-completed",
            STATUS_FAILED: "payment-failed",
        }

        receipt = {
            "type": "AP2Receipt",
            "version": "ap2.primer/v0.1",
            "transactionId": self.id,
            "status": ap2_status_map.get(self.status, self.status),
            "timestamp": self.timestamp,

            # Intent - what was authorized
            # Note: mandateId may be None for agents commissioned before mandate feature
            "intent": {
                "type": "IntentMandate",
                "mandateId": self.mandate_id,
                "policyName": policy_name,
                "agent": {
                    "id": self.agent_id,
                    "name": self.agent_name,
                }
            },

            # Authorization - who approved
            "authorization": {
                "method": "auto" if self.auto_approved else "manual",
                "authorizedAt": self.signed_at,
                "wallet": {
                    "address": self.wallet_address,
                    "id": self.wallet_id
                } if self.wallet_address else None
            },

            # Payment details
            "payment": {
                "amount": {
                    "micro": self.amount_micro,  # 6 decimal places (1_000_000 = 1 USDG)
                    "formatted": self.format_amount_precise()  # Always 6 decimals for formal receipts
                },
                "recipient": self.recipient,
                "network": self.network,
                "resource": self.resource,
                "requestUrl": self.request_url  # Full URL agent fetched (for audit)
            },

            # Settlement - on-chain proof
            "settlement": None
        }

        if self.status == STATUS_SETTLED and self.tx_hash:
            receipt["settlement"] = {
                "txHash": self.tx_hash,
                "settledAt": self.settled_at,
                "verification": {
                    "status": self.verification_status or "unverified",
                    "block": self.verification_block
                } if self.verification_status else None
            }

        if self.status == STATUS_REJECTED:
            receipt["rejection"] = {
                "reason": self.reject_reason
            }

        if self.status == STATUS_FAILED:
            receipt["failure"] = {
                "reason": self.reject_reason
            }

        return receipt
