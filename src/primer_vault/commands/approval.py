"""
Approval command implementations (pending, approve, reject).
"""

from typing import TYPE_CHECKING

from .result import CommandResult

if TYPE_CHECKING:
    from ..core import Vault
    from .handler import CommandHandler


class ApprovalCommands:
    """Approval-related commands."""

    def __init__(self, core: "Vault", handler: "CommandHandler"):
        self.core = core
        self.handler = handler

    def pending(self, args: list[str]) -> CommandResult:
        """List pending approval requests."""
        if args and args[0] in ("--help", "-h"):
            return CommandResult.ok("pending - List all pending approval requests")

        pending = self.core.get_pending_requests()
        if not pending:
            return CommandResult.ok("No pending requests.")

        lines = ["Pending Requests:"]
        for req in pending:
            amount = req.amount_micro / 1_000_000
            lines.append(f"  {req.id[:8]}  {req.agent_name}  ${amount:.6f}  {req.network}")
            # The recipient in full, on its own line, for the reason the GUI
            # dialog gives (main_window, "Recipient:"): approving is the one
            # moment a person is asked to authorise a destination. A 16-character
            # prefix is 14 hex digits of 40 and hides the tail entirely - and the
            # tail is the half people actually compare against a known-good copy.
            # `approve` acts on what this line showed, so it has to show enough.
            lines.append(f"            -> {req.recipient}")

        return CommandResult.ok("\n".join(lines), data={"pending": [
            {
                "id": req.id,
                "agent_name": req.agent_name,
                "amount": req.amount_micro / 1_000_000,
                "recipient": req.recipient,
            }
            for req in pending
        ]})

    def approve(self, args: list[str]) -> CommandResult:
        """Approve a pending request."""
        if not args or args[0] in ("--help", "-h"):
            return CommandResult.ok("""approve - Approve a pending payment request

Usage: approve <request_id>

The request_id can be a prefix (e.g., first 8 chars).
Use 'pending' to see waiting requests.""")

        match, error = self._resolve_pending(args[0])
        if error:
            return CommandResult.fail(error)

        result = self.core.approve_request(match.id)
        if result.get("status") == "success":
            return CommandResult.ok(f"Request {match.id[:8]} approved.")
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

    def _resolve_pending(self, request_id: str):
        """Find the one pending request whose id starts with `request_id`.

        Returns (request, None) on a unique match, or (None, error) when the
        prefix is empty, matches nothing, or matches more than one - so a short
        or empty prefix can never silently act on the wrong queued payment.
        """
        if not request_id:
            return None, "Give a request id (see 'pending'); an empty id is refused."
        matches = [r for r in self.core.get_pending_requests()
                   if r.id.startswith(request_id)]
        if not matches:
            return None, f"Request not found: {request_id}"
        if len(matches) > 1:
            ids = ", ".join(r.id[:8] for r in matches)
            return None, (f"'{request_id}' matches {len(matches)} requests "
                          f"({ids}); use more characters.")
        return matches[0], None

    def reject(self, args: list[str]) -> CommandResult:
        """Reject a pending request."""
        if not args or args[0] in ("--help", "-h"):
            return CommandResult.ok("""reject - Reject a pending payment request

Usage: reject <request_id> [reason]

Arguments:
  <request_id>   Request ID (or prefix)
  [reason]       Optional rejection reason

Example:
  reject a1b2c3d4 "Amount too high\"""")

        reason = " ".join(args[1:]) if len(args) > 1 else "Rejected via console"

        match, error = self._resolve_pending(args[0])
        if error:
            return CommandResult.fail(error)

        self.core.reject_request(match.id, reason)
        return CommandResult.ok(f"Request {match.id[:8]} rejected.")
