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


def _clean_symbol(symbol) -> str:
    """Printable characters only, capped at 16 - a token symbol is untrusted
    contract text."""
    return "".join(c for c in (symbol or "") if c.isprintable())[:16].strip()


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
                entry = {
                    "id": request.id,
                    "agent_id": request.agent_id,
                    "token_in": request.token_in,
                    "token_out": request.token_out,
                    "amount_in": request.amount_in,
                    "fee_tier": request.fee_tier,
                }
            else:
                # CoreClient: dict with nested quote
                quote = item.get("quote", {})
                entry = {
                    "id": item.get("id"),
                    "agent_id": item.get("agent_id"),
                    "token_in": item.get("token_in"),
                    "token_out": item.get("token_out"),
                    "amount_in": item.get("amount_in"),
                    "fee_tier": item.get("fee_tier"),
                }
            # The terms the approval exists to let a person judge - the same
            # numbers the GUI dialog shows. amount_out_min is the one the swap
            # enforces on-chain; price impact is usually why it escalated.
            entry.update({
                "notional_usdg": _get(quote, "notional_usdg"),
                "symbol_in": _get(quote, "symbol_in"),
                "symbol_out": _get(quote, "symbol_out"),
                "amount_out_expected": _get(quote, "amount_out_expected"),
                "amount_out_min": _get(quote, "amount_out_min"),
                "token_out_decimals": _get(quote, "token_out_decimals"),
                "price_impact_pct": _get(quote, "price_impact_pct"),
                "slippage_bps": _get(quote, "effective_slippage_bps"),
            })
            result.append(entry)
        return result

    def pending(self, args: list[str]) -> CommandResult:
        pending = self._normalize_pending(self.core.get_pending_trades())
        if not pending:
            return CommandResult.ok("No pending trades.")
        lines = ["Pending Trades:"]
        for trade in pending:
            notional_usdg = trade.get("notional_usdg")
            notional = f"~${notional_usdg:.2f}" if notional_usdg else "?"
            # Identify tokens by contract address; the symbol is untrusted.
            sym_in = _clean_symbol(trade.get("symbol_in"))
            sym_out = _clean_symbol(trade.get("symbol_out"))
            lines.append(
                f"  {trade['id'][:8]}  {trade['agent_id']}  "
                f"{trade['amount_in']} {sym_in or '?'} -> {sym_out or '?'}  "
                f"(fee {trade['fee_tier']}, {notional})")
            lines.append(f"            selling: {trade['token_in']}")
            lines.append(f"            buying:  {trade['token_out']}")
            # Show the terms the GUI dialog shows: minimum output is the swap's
            # only on-chain protection, and approve executes with no second step.
            decimals = trade.get("token_out_decimals")
            expected, minimum = trade.get("amount_out_expected"), trade.get("amount_out_min")
            if decimals is not None and expected is not None and minimum is not None:
                from ..services.dex import from_atomic

                def human(atomic: int, decimals: int = decimals) -> str:
                    s = str(from_atomic(atomic, decimals))
                    return s if "." in s else s + ".0"

                lines.append(
                    f"            expected out: {human(expected)} {sym_out}   "
                    f"minimum out: {human(minimum)} {sym_out}")
            slippage_bps = trade.get("slippage_bps")
            impact = trade.get("price_impact_pct")
            terms = []
            if slippage_bps is not None:
                terms.append(f"max slippage: {slippage_bps / 100:g}%")
            terms.append("price impact: "
                         + (f"{impact:g}%" if impact is not None else "unknown"))
            lines.append(f"            {'   '.join(terms)}")
            # An unvalued trade was never checked against the caps; say so, as
            # the GUI dialog does.
            if not notional_usdg:
                lines.append(
                    "            WARNING: this trade could not be priced, so "
                    "your per-trade and daily limits could NOT be checked "
                    "against it. Approving accepts it on the numbers above "
                    "alone.")
        return CommandResult.ok("\n".join(lines))

    def _match(self, prefix: str):
        """The one pending trade whose id starts with `prefix`, or (None, error).

        An empty prefix, no match, or more than one match returns an error
        rather than acting - so a short or empty prefix cannot silently execute
        the wrong queued swap. `approve`/`reject` run with no second step.
        """
        if not prefix:
            return None, "Give a trade id (see 'trade pending'); an empty id is refused."
        pending = self._normalize_pending(self.core.get_pending_trades())
        matches = [t for t in pending if t["id"].startswith(prefix)]
        if not matches:
            return None, f"No pending trade: {prefix}"
        if len(matches) > 1:
            ids = ", ".join(t["id"][:8] for t in matches)
            return None, f"'{prefix}' matches {len(matches)} trades ({ids}); use more characters."
        return matches[0], None

    def approve(self, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult.fail("Usage: trade approve <id>")
        match, error = self._match(args[0])
        if error:
            return CommandResult.fail(error)
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
        match, error = self._match(args[0])
        if error:
            return CommandResult.fail(error)
        reason = " ".join(args[1:]) if len(args) > 1 else "Rejected via console"
        self.core.reject_trade(match["id"], reason)
        return CommandResult.ok(f"Trade {match['id'][:8]} rejected.")
