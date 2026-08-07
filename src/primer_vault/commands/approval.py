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
            lines.append(f"  {req.id[:8]}  {req.agent_name}  ${amount:.6f}  -> {req.recipient[:16]}...")

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

        request_id = args[0]
        pending = self.core.get_pending_requests()
        match = None
        for req in pending:
            if req.id.startswith(request_id):
                match = req
                break

        if not match:
            return CommandResult.fail(f"Request not found: {request_id}")

        result = self.core.approve_request(match.id)
        if result.get("status") == "success":
            return CommandResult.ok(f"Request {request_id[:8]} approved.")
        else:
            return CommandResult.fail(result.get("error", "Unknown error"))

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

        request_id = args[0]
        reason = " ".join(args[1:]) if len(args) > 1 else "Rejected via console"

        pending = self.core.get_pending_requests()
        match = None
        for req in pending:
            if req.id.startswith(request_id):
                match = req
                break

        if not match:
            return CommandResult.fail(f"Request not found: {request_id}")

        self.core.reject_request(match.id, reason)
        return CommandResult.ok(f"Request {request_id[:8]} rejected.")
