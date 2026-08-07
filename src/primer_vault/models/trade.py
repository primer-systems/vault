"""
Trade models — the agent→Vault trade request and its quote/result.

A trade is a single-pool Uniswap v3 or v4 swap. The agent fully specifies it
(tokens, amount, pool fee tier, slippage); Vault re-quotes, validates against
policy, and executes, rejects, or escalates. Token→token routes are two separate
legs the agent submits itself.

V4 trades require additional fields: tick_spacing and hooks (to identify the pool).
Version is inferred from field presence if not explicitly specified.

This module is pure data (Qt-free), usable from the GUI, CLI, and headless daemon.
"""

import re
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

from ..networks import is_native_eth, ETH_ADDRESS


_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Valid Uniswap v3 fee tiers. 0 is allowed for wrap/unwrap operations (no pool needed).
# V4 allows any uint24 value, so this is only enforced for V3.
_VALID_V3_FEE_TIERS = (0, 100, 500, 3000, 10000)
MAX_SLIPPAGE_BPS = 5000  # 50% — hard ceiling on what a request may even ask for
MAX_FEE_TIER = 1_000_000  # V4 allows any uint24, but cap at 100% for sanity
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def is_address(value: str) -> bool:
    """True if `value` is a syntactically valid EVM address."""
    return isinstance(value, str) and bool(_ADDRESS_RE.match(value))


@dataclass
class TradeRequest:
    """A swap request from an agent.

    `amount_in` is a human-decimal string (e.g. "10.5"); the atomic value is
    resolved once the token's decimals are known. `fee_tier` names the pool.

    V4 trades require `tick_spacing` and `hooks` to identify the pool.
    Version is inferred from field presence if `dex_version` is not specified.
    """
    id: str
    agent_id: str
    chain_id: int
    token_in: str            # contract address or "ETH"
    token_out: str           # contract address or "ETH"
    amount_in: str           # human-decimal string
    fee_tier: int            # V3: 100/500/3000/10000; V4: any uint24
    max_slippage_bps: int
    created_at: str
    recipient: Optional[str] = None   # defaults to the agent's wallet at execution
    deadline: Optional[int] = None    # unix seconds; None → engine default
    status: str = "pending"
    # V4-specific fields (presence implies V4 if dex_version not set)
    dex_version: Optional[str] = None   # "v3" or "v4"; inferred if absent
    tick_spacing: Optional[int] = None  # required for V4
    hooks: Optional[str] = None         # required for V4 (zero address = no hooks)

    @classmethod
    def create(cls, agent_id: str, token_in: str, token_out: str, amount_in: str,
               fee_tier: int, max_slippage_bps: int, chain_id: int = 4663,
               recipient: Optional[str] = None, deadline: Optional[int] = None,
               dex_version: Optional[str] = None, tick_spacing: Optional[int] = None,
               hooks: Optional[str] = None) -> "TradeRequest":
        return cls(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            chain_id=chain_id,
            token_in=token_in,
            token_out=token_out,
            amount_in=str(amount_in),
            fee_tier=fee_tier,
            max_slippage_bps=max_slippage_bps,
            created_at=datetime.now(timezone.utc).isoformat(),
            recipient=recipient,
            deadline=deadline,
            dex_version=dex_version,
            tick_spacing=tick_spacing,
            hooks=hooks,
        )

    def infer_version(self) -> str:
        """Infer DEX version from fields. Returns 'v3' or 'v4'."""
        if self.dex_version:
            return self.dex_version.lower()
        # Infer from V4-specific fields
        if self.tick_spacing is not None or self.hooks is not None:
            return "v4"
        return "v3"

    def is_v4(self) -> bool:
        """True if this is a V4 trade."""
        return self.infer_version() == "v4"

    def validate_shape(self) -> tuple[bool, str]:
        """Structural validation only (not policy). Returns (ok, reason)."""
        # Accept native ETH ("ETH" or address(0)) or valid ERC-20 addresses
        if not is_native_eth(self.token_in) and not is_address(self.token_in):
            return False, f"token_in is not a valid address: {self.token_in}"
        if not is_native_eth(self.token_out) and not is_address(self.token_out):
            return False, f"token_out is not a valid address: {self.token_out}"
        # Normalize for comparison (ETH -> address(0))
        token_in_norm = ETH_ADDRESS if is_native_eth(self.token_in) else self.token_in.lower()
        token_out_norm = ETH_ADDRESS if is_native_eth(self.token_out) else self.token_out.lower()
        if token_in_norm == token_out_norm:
            return False, "token_in and token_out are the same"
        if self.recipient is not None and not is_address(self.recipient):
            return False, f"recipient is not a valid address: {self.recipient}"

        # Validate dex_version if specified
        if self.dex_version is not None:
            if self.dex_version.lower() not in ("v3", "v4"):
                return False, f"dex_version must be 'v3' or 'v4', got {self.dex_version}"

        # Infer version and validate consistency
        version = self.infer_version()
        has_v4_fields = self.tick_spacing is not None or self.hooks is not None

        # Check for mismatch: explicit v3 but has V4 fields
        if self.dex_version and self.dex_version.lower() == "v3" and has_v4_fields:
            return False, "dex_version is 'v3' but tick_spacing or hooks is set (V4 fields)"

        # Version-specific validation
        if version == "v4":
            # V4 requires both tick_spacing and hooks
            if self.tick_spacing is None:
                return False, "V4 trade requires tick_spacing"
            if self.hooks is None:
                return False, "V4 trade requires hooks (use zero address for no hooks)"
            if not is_address(self.hooks):
                return False, f"hooks is not a valid address: {self.hooks}"
            if not isinstance(self.tick_spacing, int) or self.tick_spacing <= 0:
                return False, f"tick_spacing must be a positive integer, got {self.tick_spacing}"
            # V4 allows any fee tier (uint24), just sanity check
            if not isinstance(self.fee_tier, int) or self.fee_tier < 0 or self.fee_tier > MAX_FEE_TIER:
                return False, f"fee_tier must be 0-{MAX_FEE_TIER}, got {self.fee_tier}"
        else:
            # V3 validation
            if self.fee_tier not in _VALID_V3_FEE_TIERS:
                return False, f"fee_tier must be one of {_VALID_V3_FEE_TIERS}, got {self.fee_tier}"

        try:
            amt = float(self.amount_in)
        except (TypeError, ValueError):
            return False, f"amount_in is not a number: {self.amount_in}"
        if amt <= 0:
            return False, "amount_in must be positive"
        if not isinstance(self.max_slippage_bps, int) or self.max_slippage_bps < 0:
            return False, "max_slippage_bps must be a non-negative integer"
        if self.max_slippage_bps > MAX_SLIPPAGE_BPS:
            return False, f"max_slippage_bps exceeds hard ceiling ({MAX_SLIPPAGE_BPS})"
        return True, ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TradeRequest":
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            agent_id=data["agent_id"],
            chain_id=int(data.get("chain_id", 4663)),
            token_in=data["token_in"],
            token_out=data["token_out"],
            amount_in=str(data["amount_in"]),
            fee_tier=int(data["fee_tier"]),
            max_slippage_bps=int(data["max_slippage_bps"]),
            created_at=data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            recipient=data.get("recipient"),
            deadline=data.get("deadline"),
            dex_version=data.get("dex_version"),
            tick_spacing=int(data["tick_spacing"]) if data.get("tick_spacing") is not None else None,
            hooks=data.get("hooks"),
        )


@dataclass
class TradeQuote:
    """Vault's independent re-quote of a request, with slippage applied."""
    token_in: str
    token_out: str
    fee_tier: int
    pool: str                             # Pool address or special marker (WRAP/UNWRAP)
    amount_in_atomic: int
    amount_out_expected: int              # Quoter output at current pool state
    amount_out_min: int                   # amount_out_expected * (1 - effective_slippage)
    token_in_decimals: int
    token_out_decimals: int
    effective_slippage_bps: int
    gas_estimate: int
    notional_usdg: Optional[float] = None # trade size valued in USDG, if priceable
    symbol_in: Optional[str] = None       # token symbol for display
    symbol_out: Optional[str] = None      # token symbol for display
    dex_version: str = "v3"               # "v3" or "v4"
    # V4-specific fields (populated for V4 quotes)
    tick_spacing: Optional[int] = None
    hooks: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradeResult:
    """Outcome of a trade request."""
    request_id: str
    status: str                   # executed | rejected | pending | simulated | failed
    reason: Optional[str] = None
    tx_hash: Optional[str] = None
    amount_out: Optional[int] = None
    quote: Optional[TradeQuote] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def rejected(cls, request_id: str, reason: str, quote: Optional[TradeQuote] = None) -> "TradeResult":
        return cls(request_id=request_id, status="rejected", reason=reason, quote=quote)

    @classmethod
    def pending(cls, request_id: str, quote: Optional[TradeQuote] = None) -> "TradeResult":
        return cls(request_id=request_id, status="pending", quote=quote)
