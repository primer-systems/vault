"""
Event system for core notifications.

This replaces Qt signals with a simple callback-based event system.
The daemon broadcasts events, clients subscribe to receive them.
"""

from dataclasses import dataclass, field
from typing import Callable
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class EventType(Enum):
    """All event types that can be broadcast."""
    # Agent events
    AGENT_CREATED = "agent_created"
    AGENT_UPDATED = "agent_updated"
    AGENT_DELETED = "agent_deleted"

    # Policy events
    POLICY_CREATED = "policy_created"
    POLICY_UPDATED = "policy_updated"
    POLICY_DELETED = "policy_deleted"

    # Transaction events
    TRANSACTION_CREATED = "transaction_created"
    TRANSACTION_UPDATED = "transaction_updated"

    # Wallet events
    WALLET_LOCKED = "wallet_locked"
    WALLET_UNLOCKED = "wallet_unlocked"

    # Approval events
    APPROVAL_NEEDED = "approval_needed"
    APPROVAL_RESOLVED = "approval_resolved"

    # Server events
    SERVER_STARTED = "server_started"
    SERVER_STOPPED = "server_stopped"

    # Activity/logging
    ACTIVITY = "activity"

    # Trade events
    TRADE_EXECUTED = "trade_executed"

    # Settings events
    SETTINGS_CHANGED = "settings_changed"


@dataclass
class Event:
    """An event that can be broadcast to listeners."""
    type: EventType
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type.value,
            "data": self.data
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        """Create from dictionary."""
        return cls(
            type=EventType(d["type"]),
            data=d.get("data", {})
        )


# Type alias for event handlers
EventHandler = Callable[[Event], None]


class EventBus:
    """
    Simple publish-subscribe event bus.

    Replaces Qt signals for the core module.
    Listeners register callbacks, events are dispatched synchronously.
    """

    def __init__(self):
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to a specific event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to all events (useful for WebSocket broadcast)."""
        self._global_handlers.append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Unsubscribe from a specific event type."""
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
            except ValueError:
                pass

    def unsubscribe_all(self, handler: EventHandler) -> None:
        """Unsubscribe a global handler."""
        try:
            self._global_handlers.remove(handler)
        except ValueError:
            pass

    def emit(self, event: Event) -> None:
        """Emit an event to all subscribers."""
        # Notify specific handlers
        if event.type in self._handlers:
            for handler in self._handlers[event.type]:
                try:
                    handler(event)
                except Exception as e:
                    logger.error(f"Event handler error for {event.type}: {e}")

        # Notify global handlers
        for handler in self._global_handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Global event handler error: {e}")

    def emit_activity(self, message: str, is_error: bool = False,
                       detail: str = None) -> None:
        """Convenience method to emit an activity event.

        Args:
            message: Brief message for header display
            is_error: Whether this is an error message
            detail: Optional detailed message for logs tab (defaults to message)
        """
        self.emit(Event(
            type=EventType.ACTIVITY,
            data={"message": message, "is_error": is_error, "detail": detail}
        ))

    def emit_trade_executed(self, address: str) -> None:
        """Convenience method to emit a trade executed event.

        Args:
            address: The wallet address affected by the trade (for balance refresh).
        """
        self.emit(Event(
            type=EventType.TRADE_EXECUTED,
            data={"address": address}
        ))
