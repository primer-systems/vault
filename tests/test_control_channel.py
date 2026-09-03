"""The local control channel: reaching an engine that already holds the lock.

Only one process may hold a data directory, so a second `primer-vault` cannot
open one - it attaches instead. These tests cover that path, and the two things
about it that would be quietly dangerous if they broke: that a caller without
the token is refused, and that the endpoint file does not outlive the engine
(an attach that connects to a stale record is an attach to nothing, or worse,
to whatever took the port).
"""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.core.events import Event, EventType
from primer_vault.terminal.control import read_endpoint, endpoint_path, send_line, LineReader
from primer_vault.terminal.control_client import ControlClient, NoEngineRunning
from primer_vault.terminal.control_server import ControlServer


@pytest.fixture
def engine(tmp_path):
    """A running engine with its control channel open."""
    (tmp_path / "wallets").mkdir(exist_ok=True)
    core = Vault(data_dir=tmp_path)
    server = ControlServer(core, tmp_path)
    server.start()
    try:
        yield core, server, tmp_path
    finally:
        server.stop()
        core.release_instance_lock()


@pytest.fixture
def client(engine):
    _core, _server, data_dir = engine
    c = ControlClient(data_dir)
    c.connect()
    try:
        yield c
    finally:
        c.close()


# ----------------------------------------------------------------- the basics

def test_a_command_runs_on_the_engine_and_comes_back_rendered(client):
    """The wire carries a command line and the text it produced - not a mirror
    of the engine's API. That is what keeps `commands/` free of any branch for
    a remote caller."""
    result = client.execute("status")
    assert result.success
    assert result.output


def test_an_unknown_command_fails_the_same_way_it_would_locally(client):
    result = client.execute("this-is-not-a-command")
    assert not result.success
    assert "Unknown command" in (result.error or "")


def test_a_command_that_needs_input_can_be_answered_over_the_wire(client):
    """Multi-step commands hold state on the engine side, so an answer sent
    afterwards has to land on the same handler that asked the question."""
    first = client.execute("wallet create channel-test")
    assert first.needs_input, "expected a password prompt"
    assert first.needs_input.get("type") == "password"


def test_two_attached_sessions_do_not_answer_each_others_prompts(engine):
    """Each connection gets its own CommandHandler. Sharing one would let a
    second terminal supply the password a first one was asked for."""
    _core, _server, data_dir = engine
    a, b = ControlClient(data_dir), ControlClient(data_dir)
    a.connect()
    b.connect()
    try:
        started = a.execute("wallet create prompt-owner")
        assert started.needs_input
        # b never asked anything, so an answer from b must not resume a's command
        stray = b.execute("status")
        assert stray.success
        assert stray.output
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- the feed

def test_engine_events_reach_an_attached_terminal(engine):
    """An approval arriving while nobody is typing has to show up on its own.
    Polling `pending` was the old answer and it is why queued spend went
    unnoticed."""
    _core, _server, data_dir = engine
    core, _, _ = engine

    seen = []
    ready = threading.Event()

    def on_event(event_type, data):
        seen.append((event_type, data))
        ready.set()

    c = ControlClient(data_dir)
    c.connect()
    try:
        c.stream_events(on_event)
        time.sleep(0.3)  # let the events connection finish its handshake
        core.event_bus.emit(Event(type=EventType.ACTIVITY,
                                  data={"message": "hello from the engine"}))
        assert ready.wait(timeout=5), "no event arrived on the control channel"
    finally:
        c.close()

    types = [t for t, _ in seen]
    assert "activity" in types
    assert any(d.get("message") == "hello from the engine" for _, d in seen)


# ------------------------------------------------------------------ security

def test_a_caller_without_the_token_is_refused(engine):
    """The token is what stops another account's process on this machine from
    driving the wallet. The bridge this replaced had none at all."""
    _core, _server, data_dir = engine
    endpoint = read_endpoint(data_dir)

    sock = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5)
    try:
        send_line(sock, {"token": "not-the-token", "mode": "execute"})
        reply = LineReader(sock).read()
    finally:
        sock.close()

    assert reply is not None
    assert reply.get("kind") == "error"
    assert "authorised" in reply.get("error", "").lower()


def test_the_channel_is_not_reachable_from_off_the_machine(engine):
    """Bound to the loopback interface, so there is nothing on the network to
    firewall or to get wrong."""
    _core, server, data_dir = engine
    endpoint = read_endpoint(data_dir)
    assert server._sock.getsockname()[0] == "127.0.0.1"
    assert isinstance(endpoint["port"], int) and endpoint["port"] > 0


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX file modes; Windows uses the directory ACL")
def test_the_endpoint_file_is_readable_only_by_its_owner(engine):
    _core, _server, data_dir = engine
    mode = endpoint_path(data_dir).stat().st_mode & 0o777
    assert mode == 0o600, f"control.json is mode {mode:o}, not 600"


# --------------------------------------------------------------- lifecycle

def test_the_endpoint_file_does_not_outlive_the_engine(tmp_path):
    """A stale endpoint is worse than none: the next attach either connects to
    nothing or to whatever else took that port."""
    (tmp_path / "wallets").mkdir(exist_ok=True)
    core = Vault(data_dir=tmp_path)
    server = ControlServer(core, tmp_path)
    server.start()
    assert endpoint_path(tmp_path).exists()

    server.stop()
    core.release_instance_lock()
    assert not endpoint_path(tmp_path).exists()


def test_attaching_with_no_engine_running_says_so(tmp_path):
    c = ControlClient(tmp_path)
    with pytest.raises(NoEngineRunning):
        c.connect()


def test_attaching_through_a_stale_endpoint_file_says_so(tmp_path):
    """Killed with SIGKILL, or the power went out: the file survives, the
    socket does not."""
    endpoint_path(tmp_path).write_text(
        json.dumps({"port": 1, "token": "stale", "pid": 999999}), encoding="utf-8")
    c = ControlClient(tmp_path)
    with pytest.raises(NoEngineRunning) as info:
        c.connect()
    assert "no longer running" in str(info.value).lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
