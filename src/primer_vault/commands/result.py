"""
CommandResult - Structured result from command execution.
"""

from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class CommandResult:
    """
    Result of executing a command.

    Attributes:
        success: Whether the command completed successfully
        output: Human-readable output text (plain text, no formatting)
        data: Optional structured data for programmatic use
        error: Error message if success is False
        needs_input: If set, command needs additional input before completing
    """
    success: bool
    output: str = ""
    data: Optional[dict] = None
    error: Optional[str] = None
    needs_input: Optional[dict] = None

    @classmethod
    def ok(cls, output: str = "", data: Optional[dict] = None) -> "CommandResult":
        """Create a successful result."""
        return cls(success=True, output=output, data=data)

    @classmethod
    def fail(cls, error: str, output: str = "") -> "CommandResult":
        """Create a failed result."""
        return cls(success=False, output=output, error=error)

    @classmethod
    def need_input(cls, input_type: str, prompt: str, **kwargs) -> "CommandResult":
        """
        Create a result indicating input is needed.

        Args:
            input_type: Type of input needed ("password", "confirm", "text")
            prompt: Prompt to show the user
            **kwargs: Additional context (e.g., command state to resume)
        """
        return cls(
            success=True,  # Not a failure, just needs more input
            output="",
            needs_input={
                "type": input_type,
                "prompt": prompt,
                **kwargs
            }
        )
