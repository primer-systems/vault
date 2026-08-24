"""
Console Window - Terminal-style interface for Vault.

Provides command-line access to all Vault functions within the GUI.
"""

import html as _html
import re
from typing import Callable, TYPE_CHECKING

from PyQt6.QtWidgets import QWidget, QTextEdit, QLineEdit, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QTextCursor, QKeyEvent

from .theme import Theme, FramelessDialog, CONSOLE, set_role
from ..commands import CommandHandler, CommandResult


# Console output syntax highlighting.
# Section headers are ALL-CAPS lines (CONSOLE, AGENTS, POLICIES, ...).
# <placeholders> and [optional] args use Theme highlight colors.
_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 /&]*$")
_PLACEHOLDER_RE = re.compile(r"&lt;.+?&gt;")   # <arg>  (after HTML-escaping)
_OPTIONAL_RE = re.compile(r"\[.+?\]")           # [optional]

if TYPE_CHECKING:
    from ..core import Vault


# ASCII art banner - the console is a dark island, colors come from CONSOLE.
CONSOLE_BANNER = (
    '<pre style="font-family: Consolas, monospace; font-size: 12px; line-height: 1.1; margin: 8px;">'
    '<br>'
    f'<span style="color: {CONSOLE["error"]};">█ █ ▄▀█ █ █ █   ▀█▀</span><br>'
    f'<span style="color: {CONSOLE["error"]};">▀▄▀ █▀█ █▄█ █▄▄  █ </span><br>'
    '</pre>'
    f'<span style="color: {CONSOLE["muted"]};">Console</span><br>'
    f'<span style="color: {CONSOLE["border"]};">═══════════════════════════════════════</span><br>'
    f'<span style="color: {CONSOLE["muted"]};">Type </span><span style="color: {CONSOLE["text"]};">help</span>'
    f'<span style="color: {CONSOLE["muted"]};"> for available commands</span><br>'
    f'<span style="color: {CONSOLE["border"]};">═══════════════════════════════════════</span><br><br>'
)


class PromptLabel(QLabel):
    """Fixed prompt label that appears before the input."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFont(QFont(Theme.MONO_FONT, 11))
        # Styled by the QLabel#consolePrompt rule; [mode] toggles password color.
        self.setObjectName("consolePrompt")


class CommandInput(QLineEdit):
    """Custom input that handles command history and password mode."""

    def __init__(self, on_submit: Callable[[str], None], parent=None):
        super().__init__(parent)
        self._on_submit = on_submit
        self._history: list[str] = []
        self._history_index = -1
        self._current_input = ""
        self._password_mode = False

        self.setFont(QFont(Theme.MONO_FONT, 11))
        # Styled by the QLineEdit#consoleInput rule; [mode] toggles password color.
        self.setObjectName("consoleInput")
        self.set_password_mode(False)
        self.returnPressed.connect(self._submit)

    def set_password_mode(self, enabled: bool):
        """Enable or disable password input mode."""
        self._password_mode = enabled
        set_role(self, mode="password" if enabled else "normal")
        self.setEchoMode(
            QLineEdit.EchoMode.Password if enabled else QLineEdit.EchoMode.Normal
        )
        if not enabled:
            self.setPlaceholderText("")

    def clear_history(self):
        """Drop the command-history buffer and any in-progress text.

        Called when the wallet locks. A typed line can itself carry a secret -
        `seed import "<phrase>"`, `address import <key>`, `wallet open <name>
        --password <pw>` - and it is kept here to power Up-arrow recall. Blanking
        the output pane does not reach this second copy, so lock() would
        otherwise leave the phrase or key redisplayable with one keystroke.
        """
        self._history.clear()
        self._history_index = -1
        self._current_input = ""
        self.clear()

    def _submit(self):
        text = self.text()
        self.clear()

        # Don't add password entries to history
        if not self._password_mode and text.strip():
            self._history.append(text.strip())
            self._history_index = -1
            self._current_input = ""

        self._on_submit(text)

    def keyPressEvent(self, event: QKeyEvent):
        # Disable history navigation in password mode
        if self._password_mode:
            super().keyPressEvent(event)
            return

        if event.key() == Qt.Key.Key_Up:
            self._navigate_history(1)  # Up = go back in history
        elif event.key() == Qt.Key.Key_Down:
            self._navigate_history(-1)  # Down = go forward in history
        else:
            super().keyPressEvent(event)

    def _navigate_history(self, direction: int):
        if not self._history:
            return

        if self._history_index == -1:
            self._current_input = self.text()

        new_index = self._history_index + direction

        if new_index < -1:
            new_index = -1
        elif new_index >= len(self._history):
            new_index = len(self._history) - 1

        self._history_index = new_index

        if new_index == -1:
            self.setText(self._current_input)
        else:
            self.setText(self._history[-(new_index + 1)])


class ConsoleWindow(FramelessDialog):
    """Terminal-style console window for Vault."""

    def __init__(self, core: "Vault", parent=None):
        super().__init__("Primer Vault Console", parent, width=800)
        self.core = core
        self._handler = CommandHandler(self.core)

        # Pending input state for multi-step commands
        self._waiting_for_input = False
        self._input_field = None  # "password", "confirm", "text"

        self.setMinimumSize(700, 500)
        self.resize(800, 600)

        # Use content_layout from FramelessDialog, but with no margins for terminal look
        layout = self.content_layout
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Output area
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont(Theme.MONO_FONT, 10))
        self.output.setObjectName("console")
        layout.addWidget(self.output)

        # Input area with prompt label
        input_container = QWidget()
        input_container.setObjectName("consoleInputRow")
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(0)

        self.prompt_label = PromptLabel("primer-vault>")
        input_layout.addWidget(self.prompt_label)

        self.input = CommandInput(self._handle_input, self)
        input_layout.addWidget(self.input)

        layout.addWidget(input_container)

        # Show banner
        self.output.setHtml(CONSOLE_BANNER)

        # Subscribe to events
        self._setup_event_subscriptions()

        # Focus input
        self.input.setFocus()

    def _setup_event_subscriptions(self):
        """Subscribe to core events for live updates."""
        from ..core.events import EventType

        def on_event(event):
            QTimer.singleShot(0, lambda: self._show_event(event))

        self.core.event_bus.subscribe(EventType.ACTIVITY, on_event)
        self.core.event_bus.subscribe(EventType.APPROVAL_NEEDED, on_event)
        self.core.event_bus.subscribe(EventType.TRANSACTION_CREATED, on_event)
        # Clear the pane when the wallet locks: `seed create` / `address export`
        # print the phrase or key here, and lock() must leave nothing readable
        # on screen or in this widget's buffer. Synchronous, not deferred, so it
        # is gone the instant the wallet locks - the walk-away case auto-lock
        # exists for. Every other key-revealing surface scrubs the same way.
        self.core.event_bus.subscribe(EventType.WALLET_LOCKED,
                                      self._on_wallet_locked)

    def _on_wallet_locked(self, event=None):
        """Blank the output pane and its buffer, and drop the input command
        history, when the wallet locks. Both hold copies of what was typed, and
        a typed line can carry a seed phrase, a private key or the password."""
        self.output.setHtml(CONSOLE_BANNER)
        self.input.clear_history()

    def _show_event(self, event):
        """Display an event in the console."""
        from ..core.events import EventType

        if event.type == EventType.ACTIVITY:
            msg = event.data.get("message", "")
            is_error = event.data.get("is_error", False)
            color = CONSOLE["error"] if is_error else CONSOLE["muted"]
            self._append(f"[event] {msg}", color)
        elif event.type == EventType.APPROVAL_NEEDED:
            agent = event.data.get("agent_name", "unknown")
            amount = event.data.get("amount_micro", 0) / 1_000_000
            self._append(f"[approval needed] {agent} requests ${amount:.6f}", CONSOLE["approval"])

    def _append(self, text: str, color: str = None, *, markup: bool = False):
        """Append a line to the output.

        The output is a rich-text document, so `text` is escaped unless the
        caller says it has already built markup. Most of what lands here is not
        ours - agent names, a merchant's resource description, command output -
        and inserting that as markup let it garble or forge log lines in an audit
        trail. Only the help-text highlighter passes markup=True.
        """
        color = color or CONSOLE["text"]
        body = text if markup else _html.escape(text)
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(f'<span style="color: {color};">{body}</span><br>')
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _append_output(self, line: str, base_color: str):
        """Append one output line with help-text syntax highlighting.

        - ALL-CAPS section headers -> RUST (bold)
        - <placeholders> -> PLACEHOLDER (cyan)
        - [optional] args -> OPTIONAL (purple)
        - description after " - " -> muted grey
        - everything else -> base_color
        """
        stripped = line.strip()
        if stripped and _HEADER_RE.match(stripped):
            self._append(f"<b>{_html.escape(line)}</b>", Theme.RUST, markup=True)
            return

        # Split "command  - description" so the description renders dim grey.
        idx = line.find(" - ")
        cmd, desc = (line[:idx], line[idx:]) if idx != -1 else (line, "")

        cmd_html = _html.escape(cmd)
        cmd_html = _PLACEHOLDER_RE.sub(
            lambda m: f'<span style="color: {Theme.PLACEHOLDER};">{m.group(0)}</span>',
            cmd_html,
        )
        cmd_html = _OPTIONAL_RE.sub(
            lambda m: f'<span style="color: {Theme.OPTIONAL};">{m.group(0)}</span>',
            cmd_html,
        )

        if desc:
            cmd_html += f'<span style="color: {CONSOLE["muted"]};">{_html.escape(desc)}</span>'
        self._append(cmd_html, base_color, markup=True)

    def _append_lines(self, lines: list[tuple[str, str]]):
        """Append multiple lines. Each tuple is (text, color)."""
        for text, color in lines:
            self._append(text, color)

    def _handle_input(self, text: str):
        """Handle input - either a command or a pending input response."""
        if self._waiting_for_input:
            was_password = self.input._password_mode
            self._waiting_for_input = False
            field = self._input_field
            self._input_field = None
            # Reset prompt label
            self.prompt_label.setText("primer-vault>")
            set_role(self.prompt_label, mode="normal")
            self.input.set_password_mode(False)
            if was_password:
                self._append("> ********", CONSOLE["muted"])
            else:
                self._append(f"> {text.strip()}", CONSOLE["bright"])
            result = self._handler.execute("", inputs={field: text.strip()})
            self._handle_result(result)
            return

        text = text.strip()
        if not text:
            return

        self._append(f"primer-vault> {text}", CONSOLE["bright"])
        result = self._handler.execute(text)
        self._handle_result(result)

    def _handle_result(self, result: CommandResult):
        """Interpret a CommandResult and update the console."""
        # Special actions
        if result.data:
            action = result.data.get("action")
            if action == "clear":
                self.output.setHtml(CONSOLE_BANNER)
                return
            if action == "exit":
                self.hide()
                return

        # Needs more input (password, confirmation, etc.)
        if result.needs_input:
            ni = result.needs_input
            self._append(ni["prompt"], CONSOLE["muted"])
            self._input_field = ni["type"]
            self._waiting_for_input = True
            is_password = ni["type"] == "password"
            self.prompt_label.setText("password:" if is_password else "")
            set_role(self.prompt_label, mode="password" if is_password else "normal")
            self.input.set_password_mode(is_password)
            return

        # Output
        color = CONSOLE["text"] if result.success else CONSOLE["error"]
        if result.output:
            for line in result.output.split("\n"):
                self._append_output(line, color)
        if result.error:
            self._append(result.error, CONSOLE["error"])

    # _register_commands and all duplicate _cmd_* methods removed.
    # All commands are routed through CommandHandler (src/commands/).
