"""
DeFi models — the agent→Vault lending request and its quote/result.

A position operation is a supply or a withdrawal against one venue: a Morpho
vault, or a single Morpho Blue market. The agent names the venue and the amount;
Vault re-reads the venue, values it, checks the policy, and executes, rejects, or
escalates. Same shape as the trading lane, and deliberately so.

What is different from a trade, and drives everything here:

- **A withdrawal is denominated two ways.** By assets ("give me $100 back") or by
  shares ("burn all of them"). Both are needed: the first is what an agent asks
  for, the second is the only way to exit fully without leaving dust behind,
  because the share price moves between quoting and settling.
- **The venue is not a pair, it is a contract.** There is no equivalent of "no
  pool at this fee tier" to fall back on. A venue is permitted or it is not, and
  that is settled against the policy's allowlist for that protocol.

Pure data (Qt-free), usable from either edition.
"""

import re
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_MARKET_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

#: Largest amount a request may name, in human units. Not a policy limit - the
#: policy caps value in dollars, which is the meaningful ceiling. This only keeps
#: absurd values out of the arithmetic, matching `models/trade.py`.
MAX_AMOUNT = Decimal("1e30")

ACTIONS = ("supply", "withdraw")

#: How a withdrawal's `amount` is denominated. Morpho takes either, and the two
#: are not interchangeable: assets is what an agent means by "give me $10 back",
#: shares is the only denomination that can name a position exactly, because the
#: share price moves between quoting and settling. A supply is always in assets
#: - you cannot deposit shares you do not have yet.
DENOMINATIONS = ("assets", "shares")
DEFAULT_DENOMINATION = "assets"

#: Lending protocols this lane can drive. One today; the tuple exists so that
#: adding a second is a change in one place rather than a new string appearing
#: in four validators. A venue id means nothing without knowing whose it is -
#: two protocols can both use an address as an identifier - so the request names
#: the protocol rather than leaving it to be guessed from the id's shape.
PROTOCOLS = ("morpho",)
DEFAULT_PROTOCOL = "morpho"


def is_address(value: str) -> bool:
    """True if `value` is a syntactically valid EVM address."""
    return isinstance(value, str) and bool(_ADDRESS_RE.match(value))


def is_market_key(value: str) -> bool:
    """True if `value` is a syntactically valid 32-byte market id."""
    return isinstance(value, str) and bool(_MARKET_KEY_RE.match(value))


@dataclass
class PositionRequest:
    """A supply or withdrawal request from an agent.

    `venue` is a vault address or a market id; which one it is comes from
    `venue_kind`, because a 40-hex address and a 64-hex market key are
    distinguishable but relying on that would make a typo in one look like the
    other.

    `amount` is a human-decimal string, resolved to atomic units once the
    venue's asset decimals are known - never before. `withdraw_all` is separate
    from it because a full exit is expressed in shares rather than assets.
    """
    id: str
    agent_id: str
    chain_id: int
    action: str                  # "supply" | "withdraw"
    venue: str                   # vault address, or market id
    venue_kind: str              # "vault" | "market"
    protocol: str                # which lending protocol the venue belongs to
    amount: str                  # human-decimal string; ignored if withdraw_all
    created_at: str
    #: Exit the whole position. Only meaningful for a withdrawal, and the only
    #: way to leave nothing behind: an asset-denominated withdrawal is quoted a
    #: block before it settles, and the share price moves underneath it.
    withdraw_all: bool = False
    #: What `amount` counts, on a withdrawal. "assets" is the ordinary case;
    #: "shares" names the position itself, which is what a caller wants when it
    #: is exiting a stated fraction rather than asking for a sum of money.
    #: `withdraw_all` is the whole-position case of the same idea and overrides
    #: this - it resolves to the full share balance whatever is set here.
    denomination: str = DEFAULT_DENOMINATION
    # The address the operation is executed from and delivered to. Set by the
    # service from the agent's commissioned address - never read from the
    # request body, so an agent cannot name a different address.
    wallet_address: Optional[str] = None
    deadline: Optional[int] = None    # unix seconds; None -> engine default
    status: str = "pending"

    @classmethod
    def create(cls, agent_id: str, action: str, venue: str, venue_kind: str,
               amount: str = "0", chain_id: int = 4663,
               withdraw_all: bool = False,
               wallet_address: Optional[str] = None,
               deadline: Optional[int] = None,
               protocol: str = DEFAULT_PROTOCOL,
               denomination: str = DEFAULT_DENOMINATION) -> "PositionRequest":
        return cls(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            chain_id=chain_id,
            action=action,
            venue=venue,
            venue_kind=venue_kind,
            protocol=protocol,
            amount=str(amount),
            created_at=datetime.now(timezone.utc).isoformat(),
            withdraw_all=withdraw_all,
            denomination=denomination,
            wallet_address=wallet_address,
            deadline=deadline,
        )

    @property
    def is_supply(self) -> bool:
        return self.action == "supply"

    @property
    def is_withdraw(self) -> bool:
        return self.action == "withdraw"

    @property
    def by_shares(self) -> bool:
        """Whether this operation is denominated in shares rather than assets.

        A full exit is the whole-position case of a share-denominated
        withdrawal, so it answers True without the caller having to set the
        denomination as well - which is what keeps `withdraw_all` and
        `denomination: shares` from being two mechanisms for one idea.
        """
        return self.is_withdraw and (
            self.withdraw_all or self.denomination == "shares")

    def validate_shape(self) -> tuple[bool, str]:
        """Structural validation only (not policy). Returns (ok, reason)."""
        if self.action not in ACTIONS:
            return False, f"action must be one of {ACTIONS}, got {self.action!r}"

        if self.denomination not in DENOMINATIONS:
            return False, (f"denomination must be one of {DENOMINATIONS}, got "
                           f"{self.denomination!r}")

        # There are no shares to hand over before the deposit that mints them.
        if self.is_supply and self.denomination == "shares":
            return False, "a supply is always denominated in assets, not shares"

        if self.protocol not in PROTOCOLS:
            return False, (f"protocol must be one of {PROTOCOLS}, got "
                           f"{self.protocol!r}")

        if self.venue_kind == "vault":
            if not is_address(self.venue):
                return False, f"venue is not a valid vault address: {self.venue}"
        elif self.venue_kind == "market":
            if not is_market_key(self.venue):
                return False, f"venue is not a valid market id: {self.venue}"
        else:
            return False, (f"venue_kind must be 'vault' or 'market', got "
                           f"{self.venue_kind!r}")

        if self.withdraw_all:
            if not self.is_withdraw:
                return False, "withdraw_all is only valid for a withdrawal"
            # The amount is unused in this case, so nothing to check.
            return True, ""

        # Same discipline as TradeRequest.validate_shape, and for the same
        # reasons: whitespace survives into an approval dialog and pushes the
        # terms off screen, and float() accepts "inf" and "nan", both of which
        # then pass a "> 0" test and reach the atomic conversion.
        if any(c.isspace() for c in str(self.amount)):
            return False, f"amount must not contain whitespace: {self.amount!r}"
        try:
            amount = Decimal(str(self.amount))
        except (TypeError, ValueError, InvalidOperation):
            return False, f"amount is not a number: {self.amount}"
        if not amount.is_finite():
            return False, f"amount must be a finite number: {self.amount}"
        if amount <= 0:
            return False, "amount must be positive"
        if amount > MAX_AMOUNT:
            return False, f"amount is implausibly large: {self.amount}"
        return True, ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PositionRequest":
        """Build a request from an agent's payload.

        `agent_id` and `wallet_address` are deliberately absent here: they are
        facts about the caller the service already knows from authenticating it,
        and reading them from the body is what let an agent act as someone else
        from an address it was never commissioned for.
        """
        required = ("action", "venue", "venue_kind")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")

        withdraw_all = data.get("withdraw_all", False)
        if not isinstance(withdraw_all, bool):
            raise ValueError(
                f"withdraw_all must be a boolean, got {withdraw_all!r}")

        # An amount is required unless the whole position is being withdrawn.
        # Defaulting it to zero instead would turn a forgotten field into a
        # request for nothing, which reads as success.
        if not withdraw_all and "amount" not in data:
            raise ValueError("missing required field(s): amount")

        return cls(
            id=str(uuid.uuid4()),
            agent_id="",
            chain_id=int(data.get("chain_id", 4663)),
            action=str(data["action"]),
            venue=str(data["venue"]),
            venue_kind=str(data["venue_kind"]),
            # Defaulted rather than required: there is one protocol today, and
            # an agent that names none means that one. When a second arrives an
            # unqualified request keeps meaning what it always meant.
            protocol=str(data.get("protocol", DEFAULT_PROTOCOL)),
            amount=str(data.get("amount", "0")),
            created_at=datetime.now(timezone.utc).isoformat(),
            withdraw_all=withdraw_all,
            # Defaulted rather than required, for the same reason `protocol` is:
            # every request written before shares were accepted meant assets,
            # and must keep meaning that.
            denomination=str(data.get("denomination", DEFAULT_DENOMINATION)),
            deadline=data.get("deadline"),
        )


@dataclass
class PositionQuote:
    """Vault's independent read of what the request would actually do.

    Amounts are atomic. `assets` and `shares` are both present because the two
    denominations are not interchangeable and the caller needs whichever matches
    what it asked in - and the approval dialog needs both, so a person can see
    "$100" and "99.49 shares" rather than being asked to trust one of them.
    """
    venue: str
    venue_kind: str
    protocol: str
    action: str
    asset: str                            # the ERC-20 being supplied or withdrawn
    asset_decimals: int
    share_decimals: int
    assets: int                           # atomic, in asset units
    shares: int                           # atomic, in share units
    #: Which of the two numbers above the caller actually named, and so which
    #: one the transaction is built against. The other is this quote's estimate
    #: of it and will have moved by the time it settles - which is the whole
    #: reason a share-denominated exit exists.
    by_shares: bool = False
    #: Value of this operation in USD. None when it could not be valued, which
    #: escalates rather than proceeding - the same rule the trading lane applies.
    notional_usd: Optional[float] = None
    #: What the agent already has in this venue, atomic, in asset units.
    current_position_assets: int = 0
    #: What the whole venue could return right now. Reconstructed from the
    #: markets behind it, because Vault V2's `maxWithdraw` reports zero
    #: unconditionally. Informational: it moves with other people's borrowing.
    venue_withdrawable: Optional[int] = None
    asset_symbol: Optional[str] = None
    venue_name: Optional[str] = None
    curator: Optional[str] = None
    gas_estimate: Optional[int] = None
    #: Approvals that must land before this can settle. Counted, not carried -
    #: the transactions themselves are rebuilt at execution time against a fresh
    #: nonce.
    approvals_needed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PositionResult:
    """Outcome of a position request."""
    request_id: str
    status: str                   # executed | rejected | pending | failed
    reason: Optional[str] = None
    tx_hash: Optional[str] = None
    #: What the operation actually moved, read from the receipt. None means it
    #: could not be read - never the quote standing in for it.
    assets_moved: Optional[int] = None
    #: What the quote predicted, kept alongside so the two can be compared.
    assets_quoted: Optional[int] = None
    #: The position after the operation, when it could be read.
    position_after: Optional[int] = None
    quote: Optional[PositionQuote] = None
    #: Stable identifier for the failure, so a caller can branch on something
    #: other than the wording of `reason`. Absent on success.
    code: Optional[str] = None
    #: Whether resending the same request unchanged is sensible. A venue that
    #: cannot free the amount today may be able to tomorrow; asking to withdraw
    #: more than the position holds will never work. Without this the caller
    #: cannot tell those apart, and one of them loops forever.
    retryable: bool = False
    #: Hash of the ERC-20 approval this attempt sent, if one was needed and it
    #: settled - independent of whether the supply/withdraw itself then
    #: succeeded. An approval that lands is a fact worth reporting even when
    #: `tx_hash` above (the deposit's) ends up None or belongs to a failure.
    approval_tx_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def rejected(cls, request_id: str, reason: str,
                 quote: Optional[PositionQuote] = None,
                 code: Optional[str] = None) -> "PositionResult":
        return cls(request_id=request_id, status="rejected", reason=reason,
                   quote=quote, code=code)

    @classmethod
    def pending(cls, request_id: str,
                quote: Optional[PositionQuote] = None) -> "PositionResult":
        return cls(request_id=request_id, status="pending", quote=quote)

    @classmethod
    def failed(cls, request_id: str, reason: str,
               quote: Optional[PositionQuote] = None,
               tx_hash: Optional[str] = None, code: Optional[str] = None,
               retryable: bool = False,
               approval_tx_hash: Optional[str] = None) -> "PositionResult":
        return cls(request_id=request_id, status="failed", reason=reason,
                   quote=quote, tx_hash=tx_hash, code=code, retryable=retryable,
                   approval_tx_hash=approval_tx_hash)
