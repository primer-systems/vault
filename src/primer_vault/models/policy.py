"""
Spend Policy model.

Defines reusable spending rules that can be attached to agents.
Supports three lanes: x402 payments, trading (swaps), and DeFi (lending).
"""

import math
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional
from urllib.parse import urlparse


@dataclass
class TradingRules:
    """
    Trading policy rules for controlling agent swap behavior.

    All monetary values are in USD. Vault converts token amounts using
    external oracle prices (CoinGecko) for policy enforcement.
    """
    enabled: bool = False                           # False = reject all trades
    per_trade_max_usd: float = 100.0                # Max notional per swap ($)
    daily_volume_limit_usd: float = 500.0           # Max total volume per day ($)
    auto_approve_below_usd: Optional[float] = None  # None = manual approval for all
    min_reserve_eth: float = 0.0001                 # Halt trading below this ETH balance (0 = no floor)
    max_slippage_percent: float = 3.0               # Reject if slippage > this %
    #: Most a trade may pay away in price impact plus fee.
    #:
    #: Set by the user only. The agent chooses which pool to trade through, so
    #: this is the user's ceiling on how bad that choice may be; letting the
    #: agent propose it would remove the protection it exists to provide.
    #:
    #: Must clear the fee of the tiers in use - a 1% pool starts at 1% before
    #: anything is wrong with it.
    max_price_impact_percent: float = 5.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TradingRules":
        """Create from dictionary with input validation."""
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled must be bool, got {type(enabled).__name__}")

        per_trade_max = data.get("per_trade_max_usd", 100.0)
        if not isinstance(per_trade_max, (int, float)) or per_trade_max < 0:
            raise ValueError(f"per_trade_max_usd must be non-negative number, got {per_trade_max}")

        daily_volume = data.get("daily_volume_limit_usd", 500.0)
        if not isinstance(daily_volume, (int, float)) or daily_volume < 0:
            raise ValueError(f"daily_volume_limit_usd must be non-negative number, got {daily_volume}")

        auto_approve = data.get("auto_approve_below_usd")
        if auto_approve is not None and (not isinstance(auto_approve, (int, float)) or auto_approve < 0):
            raise ValueError(f"auto_approve_below_usd must be non-negative number or None, got {auto_approve}")

        min_reserve = data.get("min_reserve_eth", 0.0001)
        if not isinstance(min_reserve, (int, float)) or min_reserve < 0:
            raise ValueError(f"min_reserve_eth must be non-negative number, got {min_reserve}")

        max_slippage = data.get("max_slippage_percent", 3.0)
        if not isinstance(max_slippage, (int, float)) or max_slippage < 0:
            raise ValueError(f"max_slippage_percent must be non-negative number, got {max_slippage}")

        # Absent in policies written before this rule existed; the default
        # applies rather than leaving the trade unchecked.
        max_impact = data.get("max_price_impact_percent", 5.0)
        if not isinstance(max_impact, (int, float)) or max_impact < 0:
            raise ValueError(
                f"max_price_impact_percent must be non-negative number, got {max_impact}")

        # A non-finite limit passes the >= 0 checks above but makes every
        # comparison against it False, disabling the cap. Reject it.
        for _n, _v in (("per_trade_max_usd", per_trade_max),
                       ("daily_volume_limit_usd", daily_volume),
                       ("min_reserve_eth", min_reserve),
                       ("max_slippage_percent", max_slippage),
                       ("max_price_impact_percent", max_impact)):
            if not math.isfinite(_v):
                raise ValueError(f"{_n} must be a finite number, got {_v}")
        if auto_approve is not None and not math.isfinite(auto_approve):
            raise ValueError(
                f"auto_approve_below_usd must be finite, got {auto_approve}")

        return cls(
            enabled=enabled,
            per_trade_max_usd=float(per_trade_max),
            daily_volume_limit_usd=float(daily_volume),
            auto_approve_below_usd=float(auto_approve) if auto_approve is not None else None,
            min_reserve_eth=float(min_reserve),
            max_slippage_percent=float(max_slippage),
            max_price_impact_percent=float(max_impact),
        )

    def format_per_trade_max(self) -> str:
        """Format per-trade max for display."""
        return f"${self.per_trade_max_usd:.2f}"

    def format_daily_volume_limit(self) -> str:
        """Format daily volume limit for display."""
        return f"${self.daily_volume_limit_usd:.2f}"

    def format_auto_approve(self) -> str:
        """Format auto-approve threshold for display."""
        if self.auto_approve_below_usd is None:
            return "Manual only"
        return f"${self.auto_approve_below_usd:.2f}"

    def format_min_reserve(self) -> str:
        """Format min reserve for display."""
        if self.min_reserve_eth == 0:
            return "None"
        return f"{self.min_reserve_eth:.6f} ETH"

    def format_max_slippage(self) -> str:
        """Format max slippage for display."""
        return f"{self.max_slippage_percent:.1f}%"

    def format_max_price_impact(self) -> str:
        """Format the price impact ceiling for display."""
        return f"{self.max_price_impact_percent:.1f}%"


@dataclass
class DefiRules:
    """DeFi policy rules for controlling agent lending behaviour.

    Reading these alongside `TradingRules` is worth a moment, because they are
    deliberately not the same shape.

    A swap is a **flow**: money leaves, the trade is over, and a daily total
    plus a per-trade cap bounds it completely. A deposit is a **stock**: it sits
    there earning, its value moves, and it is still at risk tomorrow. An agent
    depositing $500 a day for twenty days trips no daily cap and is $10,000
    exposed.

    So the money limits here are:

    - `max_deposit_usd` — per operation, the flow.
    - `max_total_deployed_usd` / `max_deployed_percent` — the stock, and the
      one a daily cap cannot express. Read from chain, never accumulated
      locally: yield accrues, so a running deposits-minus-withdrawals total is
      wrong the moment it is written, and it never sees a deposit the user made
      through Morpho's own interface with the same wallet.

    There is no daily volume cap. `max_ops_per_day * max_deposit_usd` already
    bounds a day's spending, and total exposure bounds what is actually at
    risk. A fourth number would constrain nothing the other three do not.

    `max_ops_per_day` is not a money limit at all - it bounds **gas**. A deposit
    costs ~369,000 gas, and an agent looping deposit-withdraw-deposit keeps
    exposure flat, trips no dollar limit, and drains the wallet's ETH. The ETH
    floor in `TradingRules` is a floor, not a rate limit: it stops the bleeding
    only once the wallet is nearly dry, by which point swaps and x402 payments
    are dead too. Treat it as a circuit breaker, not a budget - something is
    plainly wrong if an agent deposits two hundred times in a day.
    """
    enabled: bool = False                            # False = reject all DeFi ops
    #: Keep the agent to venues Steakhouse curates. This is the control a user
    #: actually sees; `morpho_curators` below is how it is enforced.
    #:
    #: Unticked, any Morpho venue on the chain is permitted - including the ~120
    #: markets with no curator behind them, some of which have an oracle the
    #: market's own creator controls. That is a real way to lose the deposit
    #: rather than merely a worse rate, and it is still the user's call: every
    #: other limit here can be turned off too.
    restrict_to_steakhouse: bool = True
    #: Morpho curators whose venues the agent may use. This IS the allowlist,
    #: and it is resolved against the chain rather than configured: a curator
    #: address resolves to the vaults it curates and, through them, to the
    #: markets it has put a cap behind. On Robinhood Chain that turns one
    #: address into two vaults and four markets, out of 124 markets that exist -
    #: the rest being empty, test, or junk, most with a bespoke oracle nobody
    #: has vetted.
    #:
    #: Named for its protocol because "curator" is a Morpho concept and does not
    #: generalise - Aave has no such role. The money limits above it are
    #: protocol-agnostic and stay flat; only the allowlist is protocol-shaped.
    #: A second protocol adds its own field beside this one with a default, so
    #: policies already on disk keep loading and nothing needs migrating.
    #:
    #: Empty means nothing is permitted. That is the opposite of how
    #: `allowed_domains` reads, and deliberately so: an empty domain list means
    #: "no restriction" because a payment names a URL, while an empty curator
    #: list means "no venues" because a deposit names a contract. Defaulting an
    #: unconfigured policy to "any contract" is not a default anyone wants.
    morpho_curators: list[str] = field(default_factory=list)
    max_deposit_usd: float = 100.0                   # Max per deposit ($)
    #: Ceiling on everything deployed at once. None = no absolute ceiling, but
    #: see `validate` - one of the two exposure limits must be set.
    max_total_deployed_usd: Optional[float] = None
    #: The same ceiling as a share of the agent's USDG, 0-100. None = not used.
    #:
    #: The denominator is USDG held **plus** USDG already deployed. Liquid-only
    #: would be self-defeating: deploy 50% of $1,000, and 50% of the remaining
    #: $500 is $250, so the agent is instantly over a limit it just obeyed.
    max_deployed_percent: Optional[float] = None
    max_ops_per_day: int = 20                        # Gas circuit breaker
    auto_approve_below_usd: Optional[float] = None   # None = manual approval for all

    # Only USDG counts toward exposure. Other assets are valued at zero - not
    # because they are worthless, but because a limit denominated in something
    # Vault cannot price independently is a limit an attacker can move. $100
    # USDG and $1,000 of anything else is $100 for these purposes.

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DefiRules":
        """Create from dictionary with input validation."""
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled must be bool, got {type(enabled).__name__}")

        # Absent in policies written before the toggle existed. Defaulting to
        # True is the safe reading: those policies were saved with a curator
        # list, which is what restriction means.
        restrict = data.get("restrict_to_steakhouse", True)
        if not isinstance(restrict, bool):
            raise ValueError(
                f"restrict_to_steakhouse must be bool, got {type(restrict).__name__}")

        curators = data.get("morpho_curators") or []
        if not isinstance(curators, list):
            raise ValueError(
                f"morpho_curators must be a list, got {type(curators).__name__}")
        for entry in curators:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    f"morpho_curators entries must be non-empty strings, got {entry!r}")

        max_deposit = data.get("max_deposit_usd", 100.0)
        if not isinstance(max_deposit, (int, float)) or isinstance(max_deposit, bool) \
                or max_deposit < 0:
            raise ValueError(
                f"max_deposit_usd must be a non-negative number, got {max_deposit}")

        max_total = data.get("max_total_deployed_usd")
        if max_total is not None and (not isinstance(max_total, (int, float))
                                      or isinstance(max_total, bool) or max_total < 0):
            raise ValueError(
                f"max_total_deployed_usd must be a non-negative number or None, "
                f"got {max_total}")

        max_percent = data.get("max_deployed_percent")
        if max_percent is not None:
            if not isinstance(max_percent, (int, float)) or isinstance(max_percent, bool):
                raise ValueError(
                    f"max_deployed_percent must be a number or None, got {max_percent}")
            if not 0 <= max_percent <= 100:
                raise ValueError(
                    f"max_deployed_percent must be between 0 and 100, got {max_percent}")

        max_ops = data.get("max_ops_per_day", 20)
        if not isinstance(max_ops, int) or isinstance(max_ops, bool) or max_ops < 0:
            raise ValueError(
                f"max_ops_per_day must be a non-negative integer, got {max_ops}")

        auto_approve = data.get("auto_approve_below_usd")
        if auto_approve is not None and (not isinstance(auto_approve, (int, float))
                                         or isinstance(auto_approve, bool)
                                         or auto_approve < 0):
            raise ValueError(
                f"auto_approve_below_usd must be a non-negative number or None, "
                f"got {auto_approve}")

        # A non-finite limit passes the >= 0 checks above but makes every
        # comparison against it False, disabling the cap. Reject it, exactly as
        # TradingRules does.
        for _n, _v in (("max_deposit_usd", max_deposit),
                       ("max_total_deployed_usd", max_total),
                       ("max_deployed_percent", max_percent),
                       ("auto_approve_below_usd", auto_approve)):
            if _v is not None and not math.isfinite(_v):
                raise ValueError(f"{_n} must be a finite number, got {_v}")

        return cls(
            enabled=enabled,
            restrict_to_steakhouse=restrict,
            morpho_curators=[c.strip() for c in curators],
            max_deposit_usd=float(max_deposit),
            max_total_deployed_usd=float(max_total) if max_total is not None else None,
            max_deployed_percent=float(max_percent) if max_percent is not None else None,
            max_ops_per_day=int(max_ops),
            auto_approve_below_usd=(float(auto_approve)
                                    if auto_approve is not None else None),
        )

    def validate(self) -> tuple[bool, str]:
        """Whether this ruleset can actually be enforced. (ok, reason).

        Checked when the policy is enabled rather than when it is parsed, so a
        half-filled policy can still be saved and finished later.
        """
        if not self.enabled:
            return True, ""
        if self.restrict_to_steakhouse and not self.any_venue_source():
            return False, ("Morpho lending is restricted to Steakhouse but no "
                           "curator is configured, so no venue would be permitted")
        if self.max_total_deployed_usd is None and self.max_deployed_percent is None:
            return False, ("Morpho lending is enabled but neither a total "
                           "exposure limit nor a percentage limit is set")
        return True, ""

    def exposure_limit_usd(self, total_usdg: float) -> float:
        """The binding ceiling on everything deployed at once.

        `total_usdg` is USDG held plus USDG already deployed - see
        `max_deployed_percent` for why the deployed half has to be in there.

        Whichever of the two limits is lower binds. Both being set is the common
        case: an absolute figure the user is comfortable with, and a proportion
        that keeps it sensible as the balance changes.
        """
        limits = []
        if self.max_total_deployed_usd is not None:
            limits.append(self.max_total_deployed_usd)
        if self.max_deployed_percent is not None:
            limits.append(max(0.0, total_usdg) * self.max_deployed_percent / 100.0)
        if not limits:
            # `validate` refuses this combination while enabled. Reached only by
            # a disabled policy being inspected, where every operation is
            # refused anyway; zero is the safe answer, not infinity.
            return 0.0
        return min(limits)

    def any_venue_source(self) -> bool:
        """Whether anything at all resolves to a permitted venue.

        One question with one answer today and more than one later: a second
        protocol adds its allowlist here rather than to every caller that wants
        to know whether the lane can do anything.
        """
        return bool(self.morpho_curators)

    def is_curator_trusted(self, curator: str) -> bool:
        """Whether this curator is one the user named. Case-insensitive."""
        if not curator:
            return False
        return curator.lower() in {c.lower() for c in self.morpho_curators}

    def format_max_deposit(self) -> str:
        """Format the per-deposit cap for display."""
        return f"${self.max_deposit_usd:.2f}"

    def format_exposure_limit(self) -> str:
        """Format the exposure ceiling for display, showing both halves."""
        parts = []
        if self.max_total_deployed_usd is not None:
            parts.append(f"${self.max_total_deployed_usd:.2f}")
        if self.max_deployed_percent is not None:
            parts.append(f"{self.max_deployed_percent:.0f}% of USDG")
        if not parts:
            return "—"
        return " or ".join(parts) + (", whichever is lower" if len(parts) > 1 else "")

    def format_auto_approve(self) -> str:
        """Format auto-approve threshold for display."""
        if self.auto_approve_below_usd is None:
            return "Manual only"
        return f"${self.auto_approve_below_usd:.2f}"

    def format_ops_per_day(self) -> str:
        """Format the daily operation ceiling for display."""
        return f"{self.max_ops_per_day} per day"

    def format_restriction(self) -> str:
        """Format the venue restriction for display."""
        if self.restrict_to_steakhouse:
            return "Steakhouse only"
        return "Any Morpho venue"

    def format_curators(self) -> str:
        """Format the trusted curator list for display."""
        if not self.morpho_curators:
            return "None — no venues permitted"
        if len(self.morpho_curators) == 1:
            return f"1 curator ({self.morpho_curators[0][:10]}…)"
        return f"{len(self.morpho_curators)} curators"


@dataclass
class SpendPolicy:
    """
    A reusable spending policy that can be attached to agents.

    Supports three lanes:
    - x402 payments: controlled by x402_enabled + daily_limit_micro, per_request_max_micro, etc.
    - Trading (swaps): controlled by trading_rules (optional)
    - DeFi (lending): controlled by defi_rules (optional)

    Reading the limits: all three treat 0 the same way - a limit of zero, which
    refuses everything. What differs is whether "no limit" can be said at all.
    per_request_max_micro and auto_approve_below_micro can say it, with None;
    daily_limit_micro cannot, because every policy bounds the day, and a large
    allowance is spelled as a large number.

    So a check against these fields is either `is not None` (where None is
    meaningful) or unconditional (where it is not). Never truthiness:
    `if policy.daily_limit_micro:` reads correctly and silently means the
    opposite, because 0 is falsy - which is exactly how a policy set to $0.00
    came to permit unlimited spending.
    """
    id: str
    name: str
    networks: list[int]              # Chain IDs this policy allows
    daily_limit_micro: int           # Micro-USDG (6 decimals: 10_000_000 = $10.00)
    per_request_max_micro: Optional[int]  # Micro-USDG (None = unlimited)
    auto_approve_below_micro: Optional[int]  # Auto-approve threshold (None = manual only)
    created_at: str
    # Domain restrictions (x402 only)
    allowed_domains: list[str] = field(default_factory=list)  # If non-empty, only these domains allowed
    blocked_domains: list[str] = field(default_factory=list)  # These domains are always blocked
    # Trading rules (optional - None means trading disabled)
    trading_rules: Optional[TradingRules] = None
    # x402 enabled flag (explicit toggle, parallel to trading_rules.enabled)
    x402_enabled: bool = True
    # DeFi rules (optional - None means lending disabled)
    defi_rules: Optional["DefiRules"] = None

    @classmethod
    def create(
        cls,
        name: str,
        networks: list[int],
        daily_limit_micro: int,
        per_request_max_micro: Optional[int] = None,
        auto_approve_below_micro: Optional[int] = None,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        trading_rules: Optional[TradingRules] = None,
        x402_enabled: bool = True,
        defi_rules: Optional["DefiRules"] = None
    ) -> "SpendPolicy":
        """Create a new spend policy.

        Validated to the same rules `from_dict` applies on the way back in, so a
        policy that can be created is always a policy that can be read again. A
        value accepted here but rejected there would be unreadable only after it
        had already reached disk, which is the worst moment to find out.

        Raises:
            ValueError: if a limit is not a non-negative integer.
        """
        if not isinstance(daily_limit_micro, int) or isinstance(daily_limit_micro, bool) \
                or daily_limit_micro < 0:
            raise ValueError(
                f"daily_limit_micro must be a non-negative integer, got "
                f"{daily_limit_micro!r}")
        for field_name, value in (("per_request_max_micro", per_request_max_micro),
                                  ("auto_approve_below_micro", auto_approve_below_micro)):
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"{field_name} must be a non-negative integer or None, got {value!r}")

        return cls(
            id=str(uuid.uuid4()),
            name=name,
            networks=list(networks) if networks else [],
            daily_limit_micro=daily_limit_micro,
            per_request_max_micro=per_request_max_micro,
            auto_approve_below_micro=auto_approve_below_micro,
            created_at=datetime.now(timezone.utc).isoformat(),
            allowed_domains=allowed_domains or [],
            blocked_domains=blocked_domains or [],
            trading_rules=trading_rules,
            x402_enabled=x402_enabled,
            defi_rules=defi_rules
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        d = asdict(self)
        # trading_rules is already converted by asdict, but may be None
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SpendPolicy":
        """Create from dictionary with input validation."""
        # Work on a copy: this method pops fields and defaults `networks`. If it
        # raises on a bad record, the store keeps the caller's dict verbatim as
        # the "unreadable, still repairable" copy - mutating it would strip
        # trading_rules and x402_enabled from that copy, and x402 would then
        # default back to enabled on the next load.
        data = dict(data)
        # Validate numeric fields are non-negative
        daily_limit = data.get("daily_limit_micro", 0)
        if not isinstance(daily_limit, int) or daily_limit < 0:
            raise ValueError(f"daily_limit_micro must be non-negative integer, got {daily_limit}")

        per_request_max = data.get("per_request_max_micro")
        if per_request_max is not None and (not isinstance(per_request_max, int) or per_request_max < 0):
            raise ValueError(f"per_request_max_micro must be non-negative integer or None, got {per_request_max}")

        auto_approve = data.get("auto_approve_below_micro")
        if auto_approve is not None and (not isinstance(auto_approve, int) or auto_approve < 0):
            raise ValueError(f"auto_approve_below_micro must be non-negative integer, got {auto_approve}")

        # Ensure networks is a list (handle None or missing)
        if data.get("networks") is None:
            data["networks"] = []

        # Parse trading_rules if present
        trading_rules_data = data.pop("trading_rules", None)
        trading_rules = None
        if trading_rules_data is not None:
            trading_rules = TradingRules.from_dict(trading_rules_data)

        # x402_enabled: explicit toggle for x402 payments
        x402_enabled = data.pop("x402_enabled", True)
        if not isinstance(x402_enabled, bool):
            raise ValueError(f"x402_enabled must be bool, got {type(x402_enabled).__name__}")

        # Absent in every policy written before this lane existed, which is the
        # normal case and must stay readable. None means the lane is off, the
        # same thing an explicitly disabled ruleset means.
        defi_rules_data = data.pop("defi_rules", None)
        defi_rules = None
        if defi_rules_data is not None:
            defi_rules = DefiRules.from_dict(defi_rules_data)

        policy = cls(**data, trading_rules=trading_rules, x402_enabled=x402_enabled,
                     defi_rules=defi_rules)
        return policy

    def check_domain_allowed(self, resource_url: str) -> tuple[bool, str]:
        """
        Check if a resource URL is allowed by this policy's domain rules.

        Returns (is_allowed, reason) where reason explains why if not allowed.

        Rules:
        - Empty allowlist + empty blocklist = all allowed
        - Empty allowlist + filled blocklist = all except blocklist
        - Filled allowlist + empty blocklist = only allowlist
        - Filled allowlist + filled blocklist = allowlist minus blocklist

        Domain matching includes subdomains automatically:
        - "stripe.com" matches "stripe.com", "api.stripe.com", "foo.bar.stripe.com"
        """
        if not resource_url:
            # No resource URL provided - allow (nothing to check)
            return True, ""

        host = self._extract_host(resource_url)
        if not host:
            # URL has no domain (e.g., just a path like "/api/resource")
            # If no domain restrictions configured, allow it
            if not self.allowed_domains and not self.blocked_domains:
                return True, ""
            # If restrictions exist, we can't verify - reject with clear message
            return False, f"Cannot verify domain (resource URL is path only: {resource_url[:50]})"

        host = host.lower()

        # Check blocklist first (applies in all cases)
        if self.blocked_domains:
            for blocked in self.blocked_domains:
                if self._domain_matches(blocked, host):
                    return False, f"Domain '{host}' is blocked"

        # If allowlist exists, host must match it
        if self.allowed_domains:
            for allowed in self.allowed_domains:
                if self._domain_matches(allowed, host):
                    return True, ""
            return False, f"Domain '{host}' not in allowlist"

        # No allowlist = all allowed (blocklist already checked)
        return True, ""

    def _extract_host(self, url: str) -> Optional[str]:
        """Extract hostname from URL."""
        try:
            parsed = urlparse(url)
            return parsed.hostname
        except Exception:
            return None

    def _domain_matches(self, domain_entry: str, host: str) -> bool:
        """
        Check if host matches a domain entry.

        Includes subdomains: "example.com" matches "example.com" and "api.example.com"
        """
        entry = domain_entry.lower().strip()
        if not entry:
            return False
        return host == entry or host.endswith("." + entry)

    def format_daily_limit(self) -> str:
        """Format daily limit as USDG with 6 decimal precision."""
        return f"{self.daily_limit_micro / 1_000_000:.6f} USDG"

    def format_per_request_max(self) -> str:
        """Format per-request max as USDG with 6 decimal precision."""
        if self.per_request_max_micro is None:
            return "—"
        return f"{self.per_request_max_micro / 1_000_000:.6f} USDG"

    def format_auto_approve(self) -> str:
        """Format auto-approve threshold as USDG with 6 decimal precision."""
        if self.auto_approve_below_micro is None:
            return "—"
        return f"{self.auto_approve_below_micro / 1_000_000:.6f} USDG"

    def format_domain_restrictions(self) -> str:
        """Format domain restrictions for display."""
        if not self.allowed_domains and not self.blocked_domains:
            return "—"

        parts = []
        if self.allowed_domains:
            parts.append(f"{len(self.allowed_domains)} allowed")
        if self.blocked_domains:
            parts.append(f"{len(self.blocked_domains)} blocked")
        return ", ".join(parts)

    def has_domain_restrictions(self) -> bool:
        """Check if this policy has any domain restrictions."""
        return bool(self.allowed_domains or self.blocked_domains)

    def is_x402_enabled(self) -> bool:
        """Check if x402 payments are enabled for this policy."""
        return self.x402_enabled

    def format_x402_status(self) -> str:
        """Format x402 status for display."""
        return "Enabled" if self.x402_enabled else "Disabled"

    def is_trading_enabled(self) -> bool:
        """Check if trading is enabled for this policy."""
        return self.trading_rules is not None and self.trading_rules.enabled

    def format_trading_status(self) -> str:
        """Format trading status for display."""
        if self.trading_rules is None:
            return "Disabled"
        if not self.trading_rules.enabled:
            return "Disabled"
        return "Enabled"

    def is_defi_enabled(self) -> bool:
        """Check if DeFi lending is enabled for this policy."""
        return self.defi_rules is not None and self.defi_rules.enabled

    def format_defi_status(self) -> str:
        """Format DeFi status for display."""
        if self.defi_rules is None or not self.defi_rules.enabled:
            return "Disabled"
        return "Enabled"

    def format_defi_summary(self) -> str:
        """Format a brief DeFi rules summary for display."""
        if not self.is_defi_enabled():
            return "—"
        dr = self.defi_rules
        return (f"{dr.format_restriction()}, max {dr.format_max_deposit()}/deposit, "
                f"{dr.format_exposure_limit()} deployed")

    def format_trading_summary(self) -> str:
        """Format a brief trading rules summary for display."""
        if not self.is_trading_enabled():
            return "—"
        tr = self.trading_rules
        return f"Max ${tr.per_trade_max_usd:.0f}/trade, ${tr.daily_volume_limit_usd:.0f}/day"
