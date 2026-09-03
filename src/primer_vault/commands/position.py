"""
`position` and `venues` — the Morpho lending lane, from a prompt.

Mirrors `commands/trade.py`. Shared, so the desktop console and the terminal get
the same commands: the terminal has no approval dialog, so this is the only way
a person running it unattended can answer a queued supply or withdrawal.
"""

from typing import TYPE_CHECKING

from .result import CommandResult

if TYPE_CHECKING:
    from ..core.vault import Vault
    from .handler import CommandHandler


def _get(obj, key, default=None):
    """Read a field off a dataclass or a dict, whichever the caller has."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _amount(atomic, decimals) -> str:
    """Atomic units as a human figure. Integer arithmetic throughout - the
    asset is 6dp and a vault share is 18dp, and a float loses the low digits of
    the second."""
    if atomic is None or decimals is None:
        return "?"
    from decimal import Decimal
    return f"{Decimal(int(atomic)) / (10 ** int(decimals)):f}"


class PositionCommands:
    """Manage the agent lending queue and see what is held."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        if not args or args[0] in ("--help", "-h"):
            return CommandResult.ok(
                "position - Manage agent lending on Morpho\n\n"
                "Usage:\n"
                "  position pending                 List requests awaiting approval\n"
                "  position approve <id>            Approve and execute a request\n"
                "  position reject <id> [reason]    Reject a request")
        sub = args[0].lower()
        rest = args[1:]
        if sub == "pending":
            return self.pending(rest)
        if sub == "approve":
            return self.approve(rest)
        if sub == "reject":
            return self.reject(rest)
        return CommandResult.fail(f"Unknown position command: {sub}")

    def _normalize_pending(self, pending):
        """Flatten (PositionRequest, PositionQuote) pairs into rows to print."""
        rows = []
        for request, quote in pending:
            rows.append({
                "id": _get(request, "id"),
                "agent_id": _get(request, "agent_id"),
                "action": _get(request, "action"),
                "venue": _get(quote, "venue"),
                "venue_kind": _get(quote, "venue_kind"),
                "venue_name": _get(quote, "venue_name"),
                "assets": _get(quote, "assets"),
                "asset_decimals": _get(quote, "asset_decimals"),
                "asset_symbol": _get(quote, "asset_symbol"),
                "shares": _get(quote, "shares"),
                "share_decimals": _get(quote, "share_decimals"),
                "by_shares": _get(quote, "by_shares", False),
                "notional_usd": _get(quote, "notional_usd"),
                "position": _get(quote, "current_position_assets"),
                "withdrawable": _get(quote, "venue_withdrawable"),
                "approvals": _get(quote, "approvals_needed", 0),
            })
        return rows

    def pending(self, args: list[str]) -> CommandResult:
        rows = self._normalize_pending(self.core.get_pending_positions())
        if not rows:
            return CommandResult.ok("No pending lending requests.")
        lines = ["Pending Lending Requests:"]
        for row in rows:
            decimals = row["asset_decimals"]
            symbol = row["asset_symbol"] or ""
            usd = row["notional_usd"]
            value = f"~${usd:.2f}" if usd else "unpriced"
            verb = "supply" if row["action"] == "supply" else "withdraw"
            # The transaction is keyed to whichever denomination was named; the
            # other is only this quote's estimate and will have moved by
            # settlement - show the named one first and label it as such.
            if row["by_shares"]:
                amount, unit = _amount(row["shares"], row["share_decimals"]), "shares"
            else:
                amount, unit = _amount(row["assets"], decimals), symbol
            lines.append(
                f"  {row['id'][:8]}  {row['agent_id']}  "
                f"{verb} {amount} {unit}  ({value})")
            if row["by_shares"]:
                lines.append(
                    f"            estimated in {symbol}: "
                    f"{_amount(row['assets'], decimals)} "
                    f"(will move before settlement)")
            lines.append(
                f"            {row['venue_kind']}: {row['venue_name'] or ''}")
            lines.append(f"            {row['venue']}")
            lines.append(
                f"            already held: "
                f"{_amount(row['position'], decimals)} {symbol}")
            # What the venue could actually return right now. It moves with
            # other people's borrowing, so it is worth seeing before approving a
            # supply as well as a withdrawal.
            if row["withdrawable"] is not None:
                lines.append(
                    f"            venue can return now: "
                    f"{_amount(row['withdrawable'], decimals)} {symbol}")
            if row["approvals"]:
                lines.append(
                    f"            needs {row['approvals']} token approval(s) "
                    f"first, then the {verb}")
            if not usd:
                lines.append(
                    "            WARNING: this could not be priced, so your "
                    "deposit and exposure limits could NOT be checked against "
                    "it. Approving accepts it on the numbers above alone.")
        return CommandResult.ok("\n".join(lines))

    def _match(self, prefix: str):
        """The one pending request whose id starts with `prefix`, or (None, error).

        An empty prefix, no match, or more than one returns an error rather than
        acting: `approve` executes with no second step, so a short prefix must
        not be able to move money to the wrong venue.
        """
        if not prefix:
            return None, ("Give a request id (see 'position pending'); an empty "
                          "id is refused.")
        rows = self._normalize_pending(self.core.get_pending_positions())
        matches = [r for r in rows if r["id"].startswith(prefix)]
        if not matches:
            return None, f"No pending lending request: {prefix}"
        if len(matches) > 1:
            ids = ", ".join(r["id"][:8] for r in matches)
            return None, (f"'{prefix}' matches {len(matches)} requests ({ids}); "
                          f"use more characters.")
        return matches[0], None

    def approve(self, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult.fail("Usage: position approve <id>")
        match, error = self._match(args[0])
        if error:
            return CommandResult.fail(error)
        result = self.core.approve_position(match["id"])
        status = result.get("status")
        short = match["id"][:8]
        if status == "executed":
            return CommandResult.ok(
                f"Position {short} executed. tx: {result.get('tx_hash')}")
        if status == "failed":
            # Whether resending unchanged is sensible is the difference between
            # a venue that is short of liquidity today and a request that can
            # never work, so it is said rather than left to be guessed.
            again = (" This one is worth trying again later."
                     if result.get("retryable") else "")
            reason = str(result.get("reason") or "").rstrip()
            if again and reason and not reason.endswith((".", "!", "?")):
                reason += "."
            return CommandResult.fail(f"Position {short} failed: {reason}{again}")
        return CommandResult.ok(
            f"Position {short}: {status} ({result.get('reason', '')})")

    def reject(self, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult.fail("Usage: position reject <id> [reason]")
        match, error = self._match(args[0])
        if error:
            return CommandResult.fail(error)
        reason = " ".join(args[1:]) if len(args) > 1 else "Rejected via console"
        self.core.reject_position(match["id"], reason)
        return CommandResult.ok(f"Position {match['id'][:8]} rejected.")


class VenuesCommands:
    """What the Morpho lane permits, and what is held there."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def execute(self, args: list[str]) -> CommandResult:
        if args and args[0] in ("--help", "-h"):
            return CommandResult.ok(
                "venues - Morpho vaults and markets this Vault permits\n\n"
                "Usage:\n"
                "  venues            List permitted venues and what is held\n\n"
                "Reads the chain, so it can take a moment.")

        try:
            policies = self.core.get_all_policies()
        except Exception as e:
            return CommandResult.fail(f"Could not read policies: {e}")

        curators: list[str] = []
        restricted = True
        for policy in policies:
            rules = getattr(policy, "defi_rules", None)
            if rules is None or not rules.enabled:
                continue
            if not rules.restrict_to_steakhouse:
                restricted = False
            for curator in rules.morpho_curators:
                if curator not in curators:
                    curators.append(curator)

        if not curators and restricted:
            return CommandResult.ok(
                "No policy enables Morpho lending, so no venues are permitted.")

        try:
            venues = self.core.get_defi_venues(curators)
        except Exception as e:
            return CommandResult.fail(f"Could not read venues: {e}")

        if not venues:
            return CommandResult.ok("No venues resolved for the configured curators.")

        vaults = [v for v in venues if getattr(v, "kind", "") == "vault"]
        markets = [v for v in venues if getattr(v, "kind", "") == "market"]
        lines = []
        if vaults:
            lines.append("Vaults (the curator decides how the deposit is spread):")
            for v in vaults:
                total = _amount(v.total_assets, v.asset_decimals)
                lines.append(f"  {v.name}")
                lines.append(f"    {v.address}   size {total}")
        if markets:
            lines.append("")
            lines.append("Markets (the agent picks one loan book itself):")
            for m in markets:
                lines.append(
                    f"  {m.collateral_symbol} at {m.lltv_percent:.1f}% LLTV")
                lines.append(f"    {m.id}")
        if not restricted:
            lines.append("")
            lines.append(
                "A policy has the Steakhouse restriction off, so agents under it "
                "may use any Morpho venue - not only those listed here.")
        return CommandResult.ok("\n".join(lines))
