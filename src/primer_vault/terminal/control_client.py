"""The attaching side of the control channel.

Deliberately thin. It sends a command line and renders what comes back; it does
not know what any command means, and it holds no engine state. That is what
keeps `commands/` free of remote-caller branches - the commands run on the
engine's side, against a real `Vault`, and this only carries text.
"""

import logging
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

from ..commands import CommandResult
from .control import LineReader, ControlError, read_endpoint, send_line

logger = logging.getLogger(__name__)

#: How long to wait for a reply. Some commands are genuinely slow - a trade
#: quote reaches out to a node, unlocking runs Argon2id - so this is generous.
REPLY_TIMEOUT_SECONDS = 180

#: How long to wait for the initial connection. A running engine answers at
#: once; anything slower is almost certainly a stale endpoint file.
CONNECT_TIMEOUT_SECONDS = 5


class NoEngineRunning(Exception):
    """No engine is listening on this data directory's control channel."""


class ControlClient:
    """Drives a `Vault` running in another process on this machine."""

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[LineReader] = None
        self._event_sock: Optional[socket.socket] = None
        self._event_thread: Optional[threading.Thread] = None
        self._stopping = False

    # ------------------------------------------------------------------ setup

    def connect(self) -> None:
        endpoint = read_endpoint(self._data_dir)
        if endpoint is None:
            raise NoEngineRunning(
                "Vault is running but did not leave a control channel to reach "
                "it on. It may still be starting up.")
        self._sock = self._open(endpoint, mode="execute")
        self._reader = LineReader(self._sock)
        self._sock.settimeout(REPLY_TIMEOUT_SECONDS)

    def _open(self, endpoint: dict, mode: str) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(("127.0.0.1", int(endpoint["port"])))
        except OSError as e:
            sock.close()
            # A stale endpoint file is the ordinary case here: the engine was
            # killed without cleanup, so the file outlived the socket.
            raise NoEngineRunning(
                f"Nothing is listening on the recorded control channel ({e}). "
                "The Vault that wrote it is no longer running.") from e
        send_line(sock, {"token": endpoint["token"], "mode": mode})
        reader = LineReader(sock)
        hello = reader.read()
        if not isinstance(hello, dict) or hello.get("kind") != "ready":
            sock.close()
            reason = (hello or {}).get("error", "the engine refused the connection")
            raise NoEngineRunning(f"Could not attach: {reason}")
        return sock

    def close(self) -> None:
        self._stopping = True
        for sock in (self._sock, self._event_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._sock = None
        self._event_sock = None

    # --------------------------------------------------------------- commands

    def execute(self, command: str, inputs: Optional[dict] = None) -> CommandResult:
        """Run one command on the engine and return what it printed."""
        if self._sock is None or self._reader is None:
            raise NoEngineRunning("Not attached to an engine")
        try:
            send_line(self._sock, {"command": command, "inputs": inputs})
            message = self._reader.read()
        except (OSError, ControlError) as e:
            raise NoEngineRunning(f"Lost the connection to Vault: {e}") from e

        if message is None:
            raise NoEngineRunning("Vault closed the connection")
        if message.get("kind") == "error":
            return CommandResult.fail(message.get("error", "Unknown error"))
        return CommandResult(
            success=bool(message.get("success")),
            output=message.get("output") or "",
            data=message.get("data"),
            error=message.get("error"),
            needs_input=message.get("needs_input"),
        )

    # ----------------------------------------------------------------- events

    def stream_events(self, on_event: Callable[[str, dict], None]) -> None:
        """Deliver engine events to `on_event(type, data)` on a background thread.

        Best effort by design. If the stream cannot be opened, or drops, the
        session carries on without a live feed rather than failing - the
        operator can still type `pending`, which is the thing that matters.
        """
        endpoint = read_endpoint(self._data_dir)
        if endpoint is None:
            return
        try:
            self._event_sock = self._open(endpoint, mode="events")
        except NoEngineRunning as e:
            logger.debug("No live feed: %s", e)
            return
        self._event_sock.settimeout(None)

        def pump():
            reader = LineReader(self._event_sock)
            while not self._stopping:
                try:
                    message = reader.read()
                except (OSError, ControlError):
                    return
                if message is None:
                    return
                if message.get("kind") == "event":
                    try:
                        on_event(message.get("type", ""), message.get("data") or {})
                    except Exception:
                        logger.exception("Event handler failed")

        self._event_thread = threading.Thread(target=pump, daemon=True,
                                              name="vault-control-events")
        self._event_thread.start()
