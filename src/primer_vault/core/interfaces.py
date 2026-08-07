"""
Abstract interfaces for pluggable components.

These define contracts that different implementations can fulfill.
"""

from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from ..services.signing import SigningRequest


@dataclass
class ApprovalResult:
    """Result of an approval request."""
    approved: bool
    reason: Optional[str] = None


@runtime_checkable
class ApprovalHandler(Protocol):
    """
    Protocol for handling payment approval requests.

    Different implementations:
    - GUI: Shows dialog, waits for user click
    - Console: Prints prompt, waits for y/n
    - Headless: Auto-decides based on policy (usually reject)

    Using Protocol instead of ABC to avoid metaclass conflicts with PyQt.
    """

    def request_approval(self, request: "SigningRequest") -> None:
        """
        Called when a payment needs manual approval.

        The implementation should:
        1. Present the request to the user/system somehow
        2. Eventually call core.approve_request(id) or core.reject_request(id)

        This is async - the method returns immediately.
        The actual decision comes later via the core methods.
        """
        ...

    def on_approval_resolved(self, request_id: str, approved: bool, reason: Optional[str] = None) -> None:
        """
        Called when an approval has been resolved (by any client).

        Allows the handler to update its state (e.g., close a dialog).
        """
        ...


class HeadlessApprovalHandler:
    """
    Approval handler for headless mode.

    Implements the ApprovalHandler protocol.

    No user interface available, so:
    - Requests that need approval are auto-rejected
    - Or optionally queued with a timeout
    """

    def __init__(self, core: "Vault", auto_reject: bool = True, timeout_seconds: int = 0):
        self._core = core
        self._auto_reject = auto_reject
        self._timeout_seconds = timeout_seconds

    def request_approval(self, request: "SigningRequest") -> None:
        """Auto-reject since no user is available."""
        if self._auto_reject:
            self._core.reject_request(
                request.id,
                "No operator available (headless mode)"
            )
        # If not auto_reject, the request sits in pending until timeout or manual resolution

    def on_approval_resolved(self, request_id: str, approved: bool, reason: Optional[str] = None) -> None:
        """Nothing to do in headless mode."""
        pass
