"""
Policy command implementations.
"""

from typing import TYPE_CHECKING

import math

from .result import CommandResult
from ..models.policy import DefiRules, TradingRules


def _parse_limit_float(s: str) -> float:
    """Parse a CLI numeric limit, rejecting NaN/Infinity - a non-finite
    limit would silently disable the cap it sets. Raises ValueError, which
    each option's handler turns into "Invalid value for --flag"."""
    v = float(s)
    if not math.isfinite(v):
        raise ValueError(f"not a finite number: {s}")
    return v


def _parse_domain_list(args: list[str], i: int) -> tuple[list[str], int]:
    """Parse the value of a --allow-domains / --block-domains flag at args[i].

    Returns (domains, next_i). Naming domains sets the list; an empty value, a
    missing value (the flag is last), or a value that is itself another flag all
    clear it. An empty allow list means "any domain", an empty block list means
    "block none" - so clearing is how the user opens a list back up, and there
    is no magic word for it.
    """
    if i + 1 < len(args) and not args[i + 1].startswith("--"):
        return [d.strip() for d in args[i + 1].split(",") if d.strip()], i + 2
    return [], i + 1


if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


class PolicyCommands:
    """Policy-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        """Route policy subcommands."""
        if not args or args[0] in ("--help", "-h"):
            return self._help()

        subcmd = args[0].lower()

        if subcmd == "list":
            return self._list()
        elif subcmd == "show":
            if "--help" in args or "-h" in args:
                return self._show_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: policy show <name>")
            return self._show(args[1])
        elif subcmd == "create":
            if "--help" in args or "-h" in args:
                return self._create_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: policy create <name> [options]. Use --help for options.")
            return self._create(args[1:])
        elif subcmd == "edit":
            if "--help" in args or "-h" in args:
                return self._edit_help()
            if len(args) < 2:
                return CommandResult.fail("Usage: policy edit <name> [options]. Use --help for options.")
            return self._edit(args[1:])
        elif subcmd == "delete":
            if "--help" in args or "-h" in args:
                return CommandResult.ok("policy delete - Remove a spending policy\n\nUsage: policy delete <name>\n\nAgents using this policy will be decommissioned.\nYou will be asked to confirm.")
            if len(args) < 2:
                return CommandResult.fail("Usage: policy delete <name>")
            return self._delete(args[1])
        else:
            return CommandResult.fail(f"Unknown subcommand: {subcmd}")

    def _help(self) -> CommandResult:
        """Show policy command help."""
        help_text = """policy - Manage spending policies

Subcommands:
  list                - List all policies
  show <policy>       - Show policy details
  create <name>       - Create policy (use --help for options)
  edit <policy>       - Edit policy (use --help for options)
  delete <policy>     - Delete policy

Use 'policy <subcommand> --help' for detailed options."""
        return CommandResult.ok(help_text)

    def _find_policy(self, identifier: str):
        """Find policy by name."""
        for p in self.core.get_all_policies():
            if p.name.lower() == identifier.lower():
                return p
        return None

    def _list(self) -> CommandResult:
        """List all policies."""
        policies = self.core.get_all_policies()
        if not policies:
            return CommandResult.ok("No policies defined.")

        lines = ["Policies:"]
        for policy in policies:
            daily = policy.daily_limit_micro / 1_000_000
            per_req = policy.per_request_max_micro / 1_000_000 if policy.per_request_max_micro is not None else None
            auto = policy.auto_approve_below_micro / 1_000_000 if policy.auto_approve_below_micro else 0
            per_req_str = f"${per_req:.2f}" if per_req is not None else "unlimited"
            x402_str = " [x402]" if policy.x402_enabled else ""
            trading_str = " [trading]" if policy.is_trading_enabled() else ""
            morpho_str = " [morpho]" if policy.is_defi_enabled() else ""
            lines.append(f"  {policy.name}  daily: ${daily:.2f}  max: {per_req_str}  auto: ${auto:.2f}{x402_str}{trading_str}{morpho_str}")

        return CommandResult.ok("\n".join(lines), data={"policies": [
            {
                "id": p.id,
                "name": p.name,
                "x402_enabled": p.x402_enabled,
                "daily_limit": p.daily_limit_micro / 1_000_000,
                "per_request_max": p.per_request_max_micro / 1_000_000 if p.per_request_max_micro is not None else None,
                "auto_approve_below": (p.auto_approve_below_micro / 1_000_000) if p.auto_approve_below_micro else 0,
                "trading_enabled": p.is_trading_enabled(),
                "morpho_enabled": p.is_defi_enabled(),
            }
            for p in policies
        ]})

    def _show_help(self) -> CommandResult:
        """Help for policy show."""
        return CommandResult.ok("""policy show - Display policy details

Usage: policy show <name>

Shows daily limit, per-request max, auto-approve threshold,
allowed/blocked domains, and network restrictions.""")

    def _show(self, identifier: str) -> CommandResult:
        """Show policy details."""
        policy = self._find_policy(identifier)
        if not policy:
            return CommandResult.fail(f"Policy not found: {identifier}")

        daily = policy.daily_limit_micro / 1_000_000
        per_req = policy.per_request_max_micro / 1_000_000 if policy.per_request_max_micro is not None else None
        auto = policy.auto_approve_below_micro / 1_000_000 if policy.auto_approve_below_micro else 0
        per_req_str = f"${per_req:.2f}" if per_req is not None else "Unlimited"

        lines = [
            f"Policy: {policy.name}",
            "",
            "x402 Payments:",
            f"  Enabled:          {'Yes' if policy.x402_enabled else 'No'}",
        ]
        if policy.x402_enabled:
            lines.extend([
                f"  Daily Limit:      ${daily:.2f}",
                f"  Per Request Max:  {per_req_str}",
                f"  Auto-approve:     ${auto:.2f}",
                f"  Allowed Domains:  {', '.join(policy.allowed_domains) if policy.allowed_domains else 'All'}",
                f"  Blocked Domains:  {', '.join(policy.blocked_domains) if policy.blocked_domains else 'None'}",
                f"  Networks:         {', '.join(str(n) for n in policy.networks) if policy.networks else 'All'}",
            ])

        # Trading rules section
        tr = policy.trading_rules
        if tr:
            lines.append("")
            lines.append("Trading:")
            lines.append(f"  Enabled:          {'Yes' if tr.enabled else 'No'}")
            if tr.enabled:
                lines.append(f"  Per Trade Max:    ${tr.per_trade_max_usd:.2f}")
                lines.append(f"  Daily Volume:     ${tr.daily_volume_limit_usd:.2f}")
                auto_str = f"${tr.auto_approve_below_usd:.2f}" if tr.auto_approve_below_usd else "Manual only"
                lines.append(f"  Auto-approve:     {auto_str}")
                lines.append(f"  Min ETH Balance:  {tr.min_reserve_eth:.6f} ETH")
                lines.append(f"  Max Slippage:     {tr.max_slippage_percent:.1f}%")
                lines.append(f"  Max Price Impact: {tr.max_price_impact_percent:.1f}%")
        else:
            lines.append("")
            lines.append("Trading:            Disabled")

        # Build data dict
        policy_data = {
            "id": policy.id,
            "name": policy.name,
            "x402_enabled": policy.x402_enabled,
            "daily_limit": daily,
            "per_request_max": per_req,
            "auto_approve_below": auto,
            "allowed_domains": policy.allowed_domains,
            "blocked_domains": policy.blocked_domains,
            "networks": policy.networks,
        }
        if tr:
            policy_data["trading"] = {
                "enabled": tr.enabled,
                "per_trade_max_usd": tr.per_trade_max_usd,
                "daily_volume_limit_usd": tr.daily_volume_limit_usd,
                "auto_approve_below_usd": tr.auto_approve_below_usd,
                "min_reserve_eth": tr.min_reserve_eth,
                "max_slippage_percent": tr.max_slippage_percent,
                "max_price_impact_percent": tr.max_price_impact_percent,
            }

        return CommandResult.ok("\n".join(lines), data={"policy": policy_data})

    def _create_help(self) -> CommandResult:
        """Help for policy create."""
        return CommandResult.ok("""policy create - Create a new spending policy

Usage: policy create <name> [options]

x402 Payment Options:
  --x402                   Enable x402 payments (default: enabled)
  --no-x402                Disable x402 payments
  --day <amount>           Daily spending limit in USD (default: 100)
  --txn <amount>           Per-transaction maximum in USD (default: 10)
  --auto <amount>          Auto-approve threshold in USD (default: none)
  --networks <ids>         Comma-separated chain IDs (default: all)
  --allow-domains <list>   Comma-separated allowed domains (empty = allow any)
  --block-domains <list>   Comma-separated blocked domains (empty = block none)

Trading Options:
  --trading                Enable trading for this policy
  --trade-max <amount>     Per-trade maximum in USD (default: 100)
  --trade-daily <amount>   Daily trading volume limit in USD (default: 500)
  --trade-auto <amount>    Auto-approve trades below this USD amount
  --min-eth <amount>       Halt trading below this ETH balance (default: 0.0001)
  --max-slip <percent>     Maximum slippage percent (default: 3.0)
  --max-impact <percent>   Max price impact incl. fee (default: 5.0)

Morpho Lending Options:
  --morpho                 Enable Morpho lending for this policy
  --morpho-max <amount>    Maximum per deposit in USD (default: 100)
  --morpho-total <amount>  Maximum deployed at once in USD (default: 500)
  --morpho-percent <pct>   Also cap at this share of USDG held plus deployed
  --morpho-ops <count>     Deposits + withdrawals per day (default: 20)
  --morpho-auto <amount>   Auto-approve operations below this USD amount
  --no-restrict            Allow any Morpho venue, not only Steakhouse's

Examples:
  policy create standard
  policy create premium --day 500 --txn 50 --auto 5
  policy create trader --trading --trade-max 200 --trade-daily 1000 --trade-auto 25
  policy create trading-only --no-x402 --trading --trade-max 100
  policy create lender --no-x402 --morpho --morpho-max 50 --morpho-total 250""")

    def _create(self, args: list[str]) -> CommandResult:
        """Create a new policy."""
        if not args:
            return CommandResult.fail("Usage: policy create <name> [options]. Use --help for options.")

        name = args[0]
        daily_limit = 100.0
        per_request_max = 10.0
        auto_approve = None
        networks = None
        allow_domains = None
        block_domains = None

        # x402 options
        x402_enabled = True  # Default enabled

        # Trading options
        trading_enabled = False
        morpho_enabled = False
        morpho_max = 100.0
        morpho_total = 500.0
        morpho_percent = None
        morpho_ops = 20
        morpho_auto = None
        morpho_restrict = True
        trade_max = 100.0
        trade_daily = 500.0
        trade_auto = None
        min_eth = 0.0001
        max_slip = 3.0
        max_impact = 5.0

        i = 1
        while i < len(args):
            if args[i] == "--day" and i + 1 < len(args):
                try:
                    daily_limit = _parse_limit_float(args[i + 1])
                    if daily_limit < 0:
                        return CommandResult.fail(f"Daily limit cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --day: {args[i + 1]}")
                i += 2
            elif args[i] == "--txn" and i + 1 < len(args):
                try:
                    per_request_max = _parse_limit_float(args[i + 1])
                    if per_request_max < 0:
                        return CommandResult.fail(f"Per-transaction max cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --txn: {args[i + 1]}")
                i += 2
            elif args[i] == "--auto" and i + 1 < len(args):
                try:
                    auto_approve = _parse_limit_float(args[i + 1])
                    if auto_approve < 0:
                        return CommandResult.fail(f"Auto-approve threshold cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --auto: {args[i + 1]}")
                i += 2
            elif args[i] == "--networks" and i + 1 < len(args):
                try:
                    networks = [int(n.strip()) for n in args[i + 1].split(",")]
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --networks: {args[i + 1]}")
                i += 2
            elif args[i] == "--allow-domains":
                allow_domains, i = _parse_domain_list(args, i)
            elif args[i] == "--block-domains":
                block_domains, i = _parse_domain_list(args, i)
            # x402 options
            elif args[i] == "--x402":
                x402_enabled = True
                i += 1
            elif args[i] == "--no-x402":
                x402_enabled = False
                i += 1
            # Trading options
            elif args[i] == "--trading":
                trading_enabled = True
                i += 1
            elif args[i] == "--trade-max" and i + 1 < len(args):
                try:
                    trade_max = _parse_limit_float(args[i + 1])
                    if trade_max < 0:
                        return CommandResult.fail(f"Trade max cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --trade-max: {args[i + 1]}")
                i += 2
            elif args[i] == "--trade-daily" and i + 1 < len(args):
                try:
                    trade_daily = _parse_limit_float(args[i + 1])
                    if trade_daily < 0:
                        return CommandResult.fail(f"Trade daily limit cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --trade-daily: {args[i + 1]}")
                i += 2
            elif args[i] == "--trade-auto" and i + 1 < len(args):
                try:
                    trade_auto = _parse_limit_float(args[i + 1])
                    if trade_auto < 0:
                        return CommandResult.fail(f"Trade auto-approve cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --trade-auto: {args[i + 1]}")
                i += 2
            elif args[i] == "--min-eth" and i + 1 < len(args):
                try:
                    min_eth = _parse_limit_float(args[i + 1])
                    if min_eth < 0:
                        return CommandResult.fail(f"Minimum ETH balance cannot be negative: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --min-eth: {args[i + 1]}")
                i += 2
            elif args[i] == "--max-slip" and i + 1 < len(args):
                try:
                    max_slip = _parse_limit_float(args[i + 1])
                    if max_slip < 0 or max_slip > 100:
                        return CommandResult.fail(f"Max slippage must be 0-100%: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --max-slip: {args[i + 1]}")
                i += 2
            elif args[i] == "--max-impact" and i + 1 < len(args):
                try:
                    max_impact = _parse_limit_float(args[i + 1])
                    if max_impact < 0 or max_impact > 100:
                        return CommandResult.fail(f"Max price impact must be 0-100%: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --max-impact: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho":
                morpho_enabled = True
                i += 1
            elif args[i] == "--no-restrict":
                morpho_restrict = False
                i += 1
            elif args[i] == "--morpho-max" and i + 1 < len(args):
                try:
                    morpho_max = _parse_limit_float(args[i + 1])
                except ValueError:
                    return CommandResult.fail(
                        f"Invalid value for --morpho-max: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-total" and i + 1 < len(args):
                try:
                    morpho_total = _parse_limit_float(args[i + 1])
                except ValueError:
                    return CommandResult.fail(
                        f"Invalid value for --morpho-total: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-percent" and i + 1 < len(args):
                try:
                    morpho_percent = _parse_limit_float(args[i + 1])
                    if not 0 <= morpho_percent <= 100:
                        return CommandResult.fail(
                            f"Morpho percent must be 0-100: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(
                        f"Invalid value for --morpho-percent: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-ops" and i + 1 < len(args):
                try:
                    morpho_ops = int(args[i + 1])
                    if morpho_ops < 1:
                        return CommandResult.fail(
                            "Morpho operations per day must be at least 1")
                except ValueError:
                    return CommandResult.fail(
                        f"Invalid value for --morpho-ops: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-auto" and i + 1 < len(args):
                try:
                    morpho_auto = _parse_limit_float(args[i + 1])
                except ValueError:
                    return CommandResult.fail(
                        f"Invalid value for --morpho-auto: {args[i + 1]}")
                i += 2
            elif args[i].startswith("--"):
                return CommandResult.fail(
                    f"Unknown option: {args[i]}\n"
                    "Use 'policy create --help' for available options."
                )
            else:
                i += 1

        # Build trading rules if enabled or any trading option was set
        trading_rules = None
        if trading_enabled:
            trading_rules = TradingRules(
                enabled=True,
                per_trade_max_usd=trade_max,
                daily_volume_limit_usd=trade_daily,
                auto_approve_below_usd=trade_auto,
                min_reserve_eth=min_eth,
                max_slippage_percent=max_slip,
                max_price_impact_percent=max_impact,
            )

        # Morpho rules. The curator list is not a command-line option: it is
        # the mechanism behind --no-restrict rather than a choice, and an
        # address typed at a prompt is exactly the kind of thing nobody can
        # check. Vault ships the list.
        defi_rules = None
        if morpho_enabled:
            from ..networks import DEFAULT_NETWORK, get_morpho
            config = get_morpho(DEFAULT_NETWORK)
            defi_rules = DefiRules(
                enabled=True,
                restrict_to_steakhouse=morpho_restrict,
                morpho_curators=list(config.default_curators) if config else [],
                max_deposit_usd=morpho_max,
                max_total_deployed_usd=morpho_total,
                max_deployed_percent=morpho_percent,
                max_ops_per_day=morpho_ops,
                auto_approve_below_usd=morpho_auto,
            )
            ok, reason = defi_rules.validate()
            if not ok:
                return CommandResult.fail(reason)

        try:
            policy = self.core.create_policy(
                name=name,
                daily_limit_micro=int(daily_limit * 1_000_000),
                per_request_max_micro=int(per_request_max * 1_000_000),
                auto_approve_below_micro=int(auto_approve * 1_000_000) if auto_approve else None,
                networks=networks,
                allowed_domains=allow_domains,
                blocked_domains=block_domains,
                trading_rules=trading_rules,
                x402_enabled=x402_enabled,
                defi_rules=defi_rules,
            )
            x402_status = " [x402]" if x402_enabled else ""
            trading_status = " [trading]" if trading_rules else ""
            morpho_status = ""
            if defi_rules:
                morpho_status = (" [morpho]" if defi_rules.restrict_to_steakhouse
                                 else " [morpho: any venue]")
            return CommandResult.ok(
                f"Policy '{policy.name}' created{x402_status}{trading_status}{morpho_status}.",
                data={"policy_id": policy.id})
        except ValueError as e:
            return CommandResult.fail(str(e))

    def _edit_help(self) -> CommandResult:
        """Help for policy edit."""
        return CommandResult.ok("""policy edit - Edit an existing policy

Usage: policy edit <name> [options]

x402 Payment Options:
  --x402 <on|off>          Enable or disable x402 payments
  --day <amount>           Daily spending limit in USD
  --txn <amount>           Per-transaction maximum in USD
  --auto <amount>          Auto-approve threshold in USD
  --networks <ids>         Comma-separated chain IDs
  --allow-domains <list>   Comma-separated allowed domains (empty to clear = allow any)
  --block-domains <list>   Comma-separated blocked domains (empty to clear = block none)

Trading Options:
  --trading <on|off>       Enable or disable trading
  --trade-max <amount>     Per-trade maximum in USD
  --trade-daily <amount>   Daily trading volume limit in USD
  --trade-auto <amount>    Auto-approve trades below this USD amount (use 'off' to disable)
  --min-eth <amount>       Halt trading below this ETH balance
  --max-slip <percent>     Maximum slippage percent
  --max-impact <percent>   Max price impact incl. fee

Morpho Lending Options:
  --morpho <on|off>        Enable or disable Morpho lending
  --restrict <on|off>      Restrict to Steakhouse's vaults, or allow any venue
  --morpho-max <amount>    Maximum per deposit in USD
  --morpho-total <amount>  Maximum deployed at once in USD
  --morpho-percent <pct>   Also cap at this share of USDG held plus deployed (use 'off' to disable)
  --morpho-ops <count>     Deposits + withdrawals per day
  --morpho-auto <amount>   Auto-approve operations below this USD amount (use 'off' to disable)

Only specified options will be changed. Enabling Morpho lending for the first
time on a policy that has never had it seeds the same defaults as
'policy create --morpho'.

Examples:
  policy edit standard --day 200 --auto 10
  policy edit trader --trading on --trade-max 500
  policy edit test --x402 off --trading on
  policy edit lender --morpho on --morpho-max 50 --morpho-total 250""")

    def _edit(self, args: list[str]) -> CommandResult:
        """Edit an existing policy."""
        if not args:
            return CommandResult.fail("Usage: policy edit <name> [options]. Use --help for options.")

        name = args[0]
        policy = self._find_policy(name)
        if not policy:
            return CommandResult.fail(f"Policy not found: {name}")

        changes = []
        # Parsed changes are collected, not applied, until the whole
        # command validates - so a bad option leaves the policy untouched
        # rather than half-edited (it is the live store object).
        policy_changes = []       # (attr, value) on the policy itself
        trading_changes = []      # (attr, value) on its trading rules
        defi_changes = []         # (attr, value) on its Morpho/DeFi rules
        i = 1
        while i < len(args):
            if args[i] == "--day" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Daily limit cannot be negative: {args[i + 1]}")
                    policy_changes.append(("daily_limit_micro", int(value * 1_000_000)))
                    changes.append(f"daily limit: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --day: {args[i + 1]}")
                i += 2
            elif args[i] == "--txn" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Per-transaction max cannot be negative: {args[i + 1]}")
                    policy_changes.append(("per_request_max_micro", int(value * 1_000_000)))
                    changes.append(f"per-txn max: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --txn: {args[i + 1]}")
                i += 2
            elif args[i] == "--auto" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Auto-approve threshold cannot be negative: {args[i + 1]}")
                    policy_changes.append(("auto_approve_below_micro", int(value * 1_000_000)))
                    changes.append(f"auto-approve: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --auto: {args[i + 1]}")
                i += 2
            elif args[i] == "--networks" and i + 1 < len(args):
                try:
                    networks = [int(n.strip()) for n in args[i + 1].split(",")]
                    policy_changes.append(("networks", networks))
                    changes.append(f"networks: {args[i + 1]}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --networks: {args[i + 1]}")
                i += 2
            elif args[i] == "--allow-domains":
                domains, i = _parse_domain_list(args, i)
                policy_changes.append(("allowed_domains", domains))
                changes.append(f"allowed domains: {', '.join(domains) if domains else '(cleared)'}")
            elif args[i] == "--block-domains":
                domains, i = _parse_domain_list(args, i)
                policy_changes.append(("blocked_domains", domains))
                changes.append(f"blocked domains: {', '.join(domains) if domains else '(cleared)'}")
            # x402 option
            elif args[i] == "--x402" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val not in ("on", "off"):
                    return CommandResult.fail(f"--x402 must be 'on' or 'off', got: {args[i + 1]}")
                policy_changes.append(("x402_enabled", val == "on"))
                changes.append(f"x402: {val}")
                i += 2
            # Trading options
            elif args[i] == "--trading" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val not in ("on", "off"):
                    return CommandResult.fail(f"--trading must be 'on' or 'off', got: {args[i + 1]}")
                trading_changes.append(("enabled", val == "on"))
                changes.append(f"trading: {val}")
                i += 2
            elif args[i] == "--trade-max" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Trade max cannot be negative: {args[i + 1]}")
                    trading_changes.append(("per_trade_max_usd", value))
                    changes.append(f"trade max: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --trade-max: {args[i + 1]}")
                i += 2
            elif args[i] == "--trade-daily" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Trade daily limit cannot be negative: {args[i + 1]}")
                    trading_changes.append(("daily_volume_limit_usd", value))
                    changes.append(f"trade daily: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --trade-daily: {args[i + 1]}")
                i += 2
            elif args[i] == "--trade-auto" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val == "off":
                    trading_changes.append(("auto_approve_below_usd", None))
                    changes.append("trade auto: off")
                else:
                    try:
                        value = _parse_limit_float(args[i + 1])
                        if value < 0:
                            return CommandResult.fail(f"Trade auto-approve cannot be negative: {args[i + 1]}")
                        trading_changes.append(("auto_approve_below_usd", value))
                        changes.append(f"trade auto: ${value:.2f}")
                    except ValueError:
                        return CommandResult.fail(f"Invalid value for --trade-auto: {args[i + 1]}")
                i += 2
            elif args[i] == "--min-eth" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Minimum ETH balance cannot be negative: {args[i + 1]}")
                    trading_changes.append(("min_reserve_eth", value))
                    changes.append(f"min ETH: {value}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --min-eth: {args[i + 1]}")
                i += 2
            elif args[i] == "--max-slip" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0 or value > 100:
                        return CommandResult.fail(f"Max slippage must be 0-100%: {args[i + 1]}")
                    trading_changes.append(("max_slippage_percent", value))
                    changes.append(f"max slippage: {value}%")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --max-slip: {args[i + 1]}")
                i += 2
            elif args[i] == "--max-impact" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0 or value > 100:
                        return CommandResult.fail(f"Max price impact must be 0-100%: {args[i + 1]}")
                    trading_changes.append(("max_price_impact_percent", value))
                    changes.append(f"max price impact: {value}%")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --max-impact: {args[i + 1]}")
                i += 2
            # Morpho lending options
            elif args[i] == "--morpho" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val not in ("on", "off"):
                    return CommandResult.fail(f"--morpho must be 'on' or 'off', got: {args[i + 1]}")
                defi_changes.append(("enabled", val == "on"))
                changes.append(f"morpho: {val}")
                i += 2
            elif args[i] == "--restrict" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val not in ("on", "off"):
                    return CommandResult.fail(f"--restrict must be 'on' or 'off', got: {args[i + 1]}")
                defi_changes.append(("restrict_to_steakhouse", val == "on"))
                changes.append(f"restrict to Steakhouse: {val}")
                i += 2
            elif args[i] == "--morpho-max" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Morpho max deposit cannot be negative: {args[i + 1]}")
                    defi_changes.append(("max_deposit_usd", value))
                    changes.append(f"morpho max deposit: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --morpho-max: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-total" and i + 1 < len(args):
                try:
                    value = _parse_limit_float(args[i + 1])
                    if value < 0:
                        return CommandResult.fail(f"Morpho total deployed cannot be negative: {args[i + 1]}")
                    defi_changes.append(("max_total_deployed_usd", value))
                    changes.append(f"morpho max deployed: ${value:.2f}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --morpho-total: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-percent" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val == "off":
                    defi_changes.append(("max_deployed_percent", None))
                    changes.append("morpho percent cap: off")
                else:
                    try:
                        value = _parse_limit_float(args[i + 1])
                        if not 0 <= value <= 100:
                            return CommandResult.fail(f"Morpho percent must be 0-100: {args[i + 1]}")
                        defi_changes.append(("max_deployed_percent", value))
                        changes.append(f"morpho percent cap: {value}%")
                    except ValueError:
                        return CommandResult.fail(f"Invalid value for --morpho-percent: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-ops" and i + 1 < len(args):
                try:
                    value = int(args[i + 1])
                    if value < 1:
                        return CommandResult.fail("Morpho operations per day must be at least 1")
                    defi_changes.append(("max_ops_per_day", value))
                    changes.append(f"morpho ops/day: {value}")
                except ValueError:
                    return CommandResult.fail(f"Invalid value for --morpho-ops: {args[i + 1]}")
                i += 2
            elif args[i] == "--morpho-auto" and i + 1 < len(args):
                val = args[i + 1].strip().lower()
                if val == "off":
                    defi_changes.append(("auto_approve_below_usd", None))
                    changes.append("morpho auto: off")
                else:
                    try:
                        value = _parse_limit_float(args[i + 1])
                        if value < 0:
                            return CommandResult.fail(f"Morpho auto-approve cannot be negative: {args[i + 1]}")
                        defi_changes.append(("auto_approve_below_usd", value))
                        changes.append(f"morpho auto: ${value:.2f}")
                    except ValueError:
                        return CommandResult.fail(f"Invalid value for --morpho-auto: {args[i + 1]}")
                i += 2
            elif args[i].startswith("--"):
                return CommandResult.fail(
                    f"Unknown option: {args[i]}\n"
                    "Use 'policy edit --help' for available options."
                )
            else:
                i += 1

        if not changes:
            return CommandResult.fail("No changes specified. Use 'policy edit --help' for options.")

        # Every option parsed - now apply, so a failure above changed nothing.
        for attr, value in policy_changes:
            setattr(policy, attr, value)
        if trading_changes:
            if policy.trading_rules is None:
                policy.trading_rules = TradingRules()
            for field_name, value in trading_changes:
                setattr(policy.trading_rules, field_name, value)
        if defi_changes:
            if policy.defi_rules is None:
                # Same seeding policy create uses: the curator list is not a
                # choice offered here either (see policy create's comment),
                # so a policy edited into its first Morpho rules starts
                # trusting the same shipped curator create would have given it.
                from ..networks import DEFAULT_NETWORK, get_morpho
                config = get_morpho(DEFAULT_NETWORK)
                # Same defaults as policy create, so a first `--morpho on`
                # here succeeds validation the same way `policy create
                # --morpho` does, rather than failing for want of an
                # exposure limit nobody was asked to set.
                policy.defi_rules = DefiRules(
                    morpho_curators=list(config.default_curators) if config else [],
                    max_total_deployed_usd=500.0)
            for field_name, value in defi_changes:
                setattr(policy.defi_rules, field_name, value)
            ok, reason = policy.defi_rules.validate()
            if not ok:
                return CommandResult.fail(reason)

        try:
            self.core.update_policy(policy)
            return CommandResult.ok(f"Policy '{policy.name}' updated: {', '.join(changes)}")
        except Exception as e:
            return CommandResult.fail(str(e))

    def _delete(self, identifier: str, inputs: dict = None) -> CommandResult:
        """Delete a policy with confirmation."""
        policy = self._find_policy(identifier)
        if not policy:
            return CommandResult.fail(f"Policy not found: {identifier}")

        # Check if we have confirmation
        if inputs and inputs.get("confirm") == "YES":
            try:
                decommissioned = self.core.delete_policy(policy.id)
                lines = [f"Policy '{policy.name}' deleted."]
                if decommissioned:
                    lines.append(f"Decommissioned agents: {', '.join(decommissioned)}")
                return CommandResult.ok("\n".join(lines))
            except Exception as e:
                return CommandResult.fail(str(e))

        # Need confirmation
        self.handler._set_pending(
            lambda inp, **ctx: self._delete(ctx["identifier"], inp),
            identifier=identifier
        )
        return CommandResult.need_input(
            "confirm",
            f"Delete policy '{policy.name}'? Agents using it will be decommissioned.\nType YES to confirm:",
        )
