"""The engine's side of the control channel.

Accepts two kinds of connection, both authenticated by the token from
`control.json`:

- **execute** - one command line in, one rendered reply out, repeatedly. The
  command runs against the real `Vault` through the same `CommandHandler` the
  desktop's console panel uses, so there is exactly one implementation of every
  command and no notion of a remote caller inside `commands/`.
- **events** - the engine's event stream, pushed as it happens, so an attached
  terminal sees an approval arrive rather than having to ask.

Each connection gets its own `CommandHandler` because a handler carries the
state of a half-finished command (the password it is waiting for). Sharing one
would let two attached terminals answer each other's prompts.
"""

import logging
import queue
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..commands import CommandHandler
from ..core.events import Event
from .control import LineReader, ControlError, new_token, send_line, write_endpoint, clear_endpoint

if TYPE_CHECKING:
    from ..core import Vault

logger = logging.getLogger(__name__)

#: Concurrently served connections. Callers are the operator's own terminals, so
#: this is generous and still bounds a runaway script.
MAX_CONNECTIONS = 16

#: How long a connection may go without sending anything before it is dropped.
#: Without it, a caller can open a socket and hold a thread for the life of the
#: process. Event streams are exempt - they are silent by design.
IDLE_TIMEOUT_SECONDS = 3600

#: Events buffered for a subscriber that has stopped reading. Past this the
#: slow subscriber is dropped rather than growing the engine's memory.
EVENT_QUEUE_MAX = 1000


class ControlServer:
    """Serves the local control channel for one `Vault`."""

    def __init__(self, core: "Vault", data_dir: Path):
        self._core = core
        self._data_dir = Path(data_dir)
        self._token = new_token()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._subscribers: set[queue.Queue] = set()
        self._subscribers_lock = threading.Lock()
        self._connections = threading.Semaphore(MAX_CONNECTIONS)

    # ------------------------------------------------------------------ start

    def start(self) -> int:
        """Bind, publish the endpoint, and serve. Returns the port."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Port 0: the OS picks a free one. Nothing else needs to guess it -
        # the endpoint file says where we landed - and a fixed port is how the
        # old admin API ended up fighting unrelated programs for 4664.
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(MAX_CONNECTIONS)
        port = self._sock.getsockname()[1]

        write_endpoint(self._data_dir, port, self._token)
        self._core.event_bus.subscribe_all(self._on_event)

        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="vault-control")
        self._thread.start()
        logger.info("Control channel listening on 127.0.0.1:%d", port)
        return port

    def stop(self) -> None:
        self._running = False
        try:
            self._core.event_bus.unsubscribe_all(self._on_event)
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        # Release anything blocked on an empty queue so its thread can exit.
        with self._subscribers_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.clear()
        clear_endpoint(self._data_dir)

    # ----------------------------------------------------------------- events

    def _on_event(self, event: Event) -> None:
        """Fan an engine event out to every attached terminal.

        Never raises and never blocks: this runs on whichever thread emitted the
        event, which may be the one signing a payment. A subscriber that has
        stopped reading is dropped, not waited for.
        """
        message = {"kind": "event", "type": event.type.value, "data": event.data}
        with self._subscribers_lock:
            stalled = []
            for q in self._subscribers:
                try:
                    q.put_nowait(message)
                except queue.Full:
                    stalled.append(q)
            for q in stalled:
                self._subscribers.discard(q)
                logger.warning("Dropped a control subscriber that stopped reading")

    # ------------------------------------------------------------------ serve

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return  # closed by stop()
            if not self._connections.acquire(blocking=False):
                try:
                    send_line(conn, {"kind": "error", "error": "Too many connections"})
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(IDLE_TIMEOUT_SECONDS)
            reader = LineReader(conn)
            hello = reader.read()
            if not isinstance(hello, dict) or hello.get("token") != self._token:
                send_line(conn, {"kind": "error", "error": "Not authorised"})
                return

            mode = hello.get("mode")
            if mode == "events":
                self._stream_events(conn)
            elif mode == "execute":
                self._run_commands(conn, reader)
            else:
                send_line(conn, {"kind": "error", "error": f"Unknown mode: {mode!r}"})
        except (OSError, ControlError) as e:
            logger.debug("Control connection ended: %s", e)
        except Exception:
            logger.exception("Control connection failed")
        finally:
            self._connections.release()
            try:
                conn.close()
            except OSError:
                pass

    def _stream_events(self, conn: socket.socket) -> None:
        q: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_MAX)
        with self._subscribers_lock:
            self._subscribers.add(q)
        # An event stream is silent whenever the engine is idle, so the idle
        # timeout that protects the command path would kill it.
        conn.settimeout(None)
        try:
            send_line(conn, {"kind": "ready"})
            while self._running:
                message = q.get()
                if message is None:
                    return
                send_line(conn, message)
        except OSError:
            return
        finally:
            with self._subscribers_lock:
                self._subscribers.discard(q)

    def _run_commands(self, conn: socket.socket, reader: LineReader) -> None:
        handler = CommandHandler(self._core)
        send_line(conn, {"kind": "ready"})
        while self._running:
            message = reader.read()
            if message is None:
                return
            command = message.get("command")
            if not isinstance(command, str):
                send_line(conn, {"kind": "error", "error": "No command given"})
                continue
            inputs = message.get("inputs")
            if inputs is not None and not isinstance(inputs, dict):
                send_line(conn, {"kind": "error", "error": "Malformed inputs"})
                continue
            result = handler.execute(command, inputs=inputs)
            send_line(conn, {
                "kind": "result",
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "data": result.data,
                "needs_input": result.needs_input,
            })
