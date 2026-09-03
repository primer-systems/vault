"""
Abstract interfaces for pluggable components.

These define contracts that different implementations can fulfill.
"""

from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from ..services.signing import SigningRequest
    from .vault import Vault


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
    Approval handler for an engine with no dialog to raise.

    Implements the ApprovalHandler protocol. "Headless" here names a
    capability, not an edition or a mode: it is the handler used when nothing
    has registered a way to put a question in front of a person. The desktop
    registers its own; the terminal uses this one with `auto_reject=False` so
    requests queue and surface in the live feed instead.

    - auto_reject=True:  requests that need approval are rejected at once
    - auto_reject=False: they queue and expire on their own timeout
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
                "No operator available"
            )
        # If not auto_reject, the request sits in pending until timeout or manual resolution

    def on_approval_resolved(self, request_id: str, approved: bool, reason: Optional[str] = None) -> None:
        """Nothing to do: there is no dialog to close."""
        pass
