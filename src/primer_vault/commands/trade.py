"""
Trade command implementations: list pending trades, approve, reject.

Drives the manual-approval loop from the console/CLI while there's no GUI trade
dialog yet: an agent POSTs /trade, it lands pending, and the user approves or
rejects here. Approving executes the swap on-chain.

Works with both direct Vault core (returns TradeRequest/TradeQuote objects)
and CoreClient (returns dicts from Admin API).
"""

from typing import TYPE_CHECKING, Union

from .result import CommandResult

if TYPE_CHECKING:
    from ..core import Vault
    from ..client.core_client import CoreClient
    from .handler import CommandHandler


def _short(addr: str) -> str:
    return f"{addr[:8]}…{addr[-4:]}" if addr and len(addr) > 14 else (addr or "?")


def _get(obj, key, default=None):
    """Get attribute or dict key, supporting both object and dict access."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class TradeCommands:
    """Trade approval commands (console/CLI)."""

    def __init__(self, core: Union["Vault", "CoreClient"], handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        if not args or args[0] in ("--help", "-h"):
            return CommandResult.ok(
                "trade - Manage pending agent trades\n\n"
                "Usage:\n"
                "  trade pending                 List trades awaiting approval\n"
                "  trade approve <id>            Approve and execute a pending trade\n"
                "  trade reject <id> [reason]    Reject a pending trade")
        sub = args[0].lower()
        rest = args[1:]
        if sub == "pending":
            return self.pending(rest)
        if sub == "approve":
            return self.approve(rest)
        if sub == "reject":
            return self.reject(rest)
        return CommandResult.fail(f"Unknown trade command: {sub}")

    def _normalize_pending(self, pending):
        """Normalize pending trades to a consistent format.

        Direct Vault returns list of (TradeRequest, TradeQuote) tuples.
        CoreClient returns list of dicts with nested 'quote' field.
        """
        result = []
        for item in pending:
            if isinstance(item, tuple):
                # Direct Vault: (request, quote)
                request, quote = item
                result.append({
                    "id": request.id,
                    "agent_id": request.agent_id,
                    "token_in": request.token_in,
                    "token_out": request.token_out,
                    "amount_in": request.amount_in,
                    "fee_tier": request.fee_tier,
                    "notional_usdg": quote.notional_usdg,
                })
            else:
                # CoreClient: dict with nested quote
                quote = item.get("quote", {})
                result.append({
                    "id": item.get("id"),
                    "agent_id": item.get("agent_id"),
                    "token_in": item.get("token_in"),
                    "token_out": item.get("token_out"),
                    "amount_in": item.get("amount_in"),
                    "fee_tier": item.get("fee_tier"),
                    "notional_usdg": quote.get("notional_usdg"),
                })
        return result

    def pending(self, args: list[str]) -> CommandResult:
        pending = self._normalize_pending(self.core.get_pending_trades())
        if not pending:
            return CommandResult.ok("No pending trades.")
        lines = ["Pending Trades:"]
        for trade in pending:
            notional_usdg = trade.get("notional_usdg")
            notional = f"~${notional_usdg:.2f}" if notional_usdg else "?"
            lines.append(
                f"  {trade['id'][:8]}  {trade['agent_id']}  "
                f"{trade['amount_in']} {_short(trade['token_in'])} -> {_short(trade['token_out'])}  "
                f"(fee {trade['fee_tier']}, {notional})")
        return CommandResult.ok("\n".join(lines))

    def _match(self, prefix: str):
        """Find a pending trade by ID prefix."""
        pending = self._normalize_pending(self.core.get_pending_trades())
        for trade in pending:
            if trade["id"].startswith(prefix):
                return trade
        return None

    def approve(self, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult.fail("Usage: trade approve <id>")
        match = self._match(args[0])
        if not match:
            return CommandResult.fail(f"No pending trade: {args[0]}")
        result = self.core.approve_trade(match["id"])
        status = result.get("status")
        if status == "executed":
            return CommandResult.ok(f"Trade {match['id'][:8]} executed. tx: {result.get('tx_hash')}")
        if status == "failed":
            return CommandResult.fail(f"Trade {match['id'][:8]} failed: {result.get('reason')}")
        return CommandResult.ok(f"Trade {match['id'][:8]}: {status} ({result.get('reason', '')})")

    def reject(self, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult.fail("Usage: trade reject <id> [reason]")
        match = self._match(args[0])
        if not match:
            return CommandResult.fail(f"No pending trade: {args[0]}")
        reason = " ".join(args[1:]) if len(args) > 1 else "Rejected via console"
        self.core.reject_trade(match["id"], reason)
        return CommandResult.ok(f"Trade {match['id'][:8]} rejected.")
