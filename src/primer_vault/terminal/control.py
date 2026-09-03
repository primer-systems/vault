"""The local control channel: where it lives, and how a line crosses it.

Only one process may hold a data directory - `instance_lock.py` enforces that,
because two processes each save whole files and would erase each other's spend
records and seeds. So an engine started by the operating system at boot cannot
be reached by a second `primer-vault` command: the second one is refused before
it opens anything. Without a channel, a running engine is a sealed box - queued
approvals unresolvable, a locked wallet un-unlockable - until it is killed.

This is that channel. It carries **command lines and rendered replies**, not a
mirror of the engine's API. That distinction is the whole design: commands
always execute against a real `Vault`, on the engine's side of the wire, so
`commands/` never learns that a remote caller exists. The bridge this replaces
proxied sixty-odd engine methods and left every command with two code paths -
one for a real object and one for a dict off the wire - and only one of them was
ever exercised.

**Protection.** A loopback socket on an ephemeral port, plus a token in a file
beside the wallet. The token file carries no protection the wallet file does not
already have, and it does not need to: anyone who can read the data directory
can read `*.wallet` too, and at that point the channel is not the weak link.
What this does buy is that nothing *outside* the data directory - no other
account's process reaching a fixed port, nothing on the network - can drive the
engine. The old admin API was on a fixed port with no token at all, which is why
it needed a "GUI only" mode to be safe.
"""

import json
import os
import secrets
import socket
from pathlib import Path
from typing import Optional

#: Endpoint description, written by the engine and read by anything attaching.
CONTROL_FILE = "control.json"

#: Longest single line accepted from a peer. Commands are short; this only
#: bounds a caller that announces a line and then never stops sending it.
MAX_LINE_BYTES = 1024 * 1024


class ControlError(Exception):
    """The channel could not be used."""


def endpoint_path(data_dir: Path) -> Path:
    return Path(data_dir) / CONTROL_FILE


def new_token() -> str:
    return secrets.token_hex(32)


def write_endpoint(data_dir: Path, port: int, token: str) -> Path:
    """Record where the engine is listening, readable only by this account.

    Written with 0600 from the moment it exists rather than chmod-ed after: a
    file created world-readable and narrowed a microsecond later is readable in
    that microsecond. On Windows the mode argument is largely advisory and the
    directory's ACL is what actually applies - the same ACL already governing
    the wallet file next to it.
    """
    path = endpoint_path(data_dir)
    payload = json.dumps({"port": port, "token": token, "pid": os.getpid()})
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def read_endpoint(data_dir: Path) -> Optional[dict]:
    """Return the recorded endpoint, or None if there is not a usable one.

    A stale file is not an error worth raising. The engine may have been killed
    without cleanup - power loss, SIGKILL - and the caller's next move is the
    same either way: treat it as "nothing is running".
    """
    path = endpoint_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data or "token" not in data:
        return None
    return data


def clear_endpoint(data_dir: Path) -> None:
    try:
        endpoint_path(data_dir).unlink()
    except OSError:
        pass


def send_line(sock: socket.socket, obj: dict) -> None:
    """Write one message.

    `default=str` because a `CommandResult.data` may carry whatever the command
    put there, including model objects. Rendering one as its string form is a
    cosmetic loss in a structured field nobody reads over the wire; raising
    would drop the connection and lose the reply the operator was waiting for.
    """
    sock.sendall((json.dumps(obj, default=str) + "\n").encode("utf-8"))


class LineReader:
    """Read newline-delimited JSON off a socket without unbounded buffering."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    def read(self) -> Optional[dict]:
        """Next message, or None when the peer has gone away."""
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_LINE_BYTES:
                raise ControlError("Control message too large")
            try:
                chunk = self._sock.recv(65536)
            except (OSError, socket.timeout):
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        if not line.strip():
            return {}
        try:
            return json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ControlError(f"Malformed control message: {e}") from e
