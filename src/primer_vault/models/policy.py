"""
Spend Policy model.

Defines reusable spending rules that can be attached to agents.
Supports two lanes: x402 payments and trading (swaps).
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
class SpendPolicy:
    """
    A reusable spending policy that can be attached to agents.

    Supports two lanes:
    - x402 payments: controlled by x402_enabled + daily_limit_micro, per_request_max_micro, etc.
    - Trading (swaps): controlled by trading_rules (optional)

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
        x402_enabled: bool = True
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
            x402_enabled=x402_enabled
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

        policy = cls(**data, trading_rules=trading_rules, x402_enabled=x402_enabled)
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

    def format_trading_summary(self) -> str:
        """Format a brief trading rules summary for display."""
        if not self.is_trading_enabled():
            return "—"
        tr = self.trading_rules
        return f"Max ${tr.per_trade_max_usd:.0f}/trade, ${tr.daily_volume_limit_usd:.0f}/day"
