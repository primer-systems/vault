"""The agent API must survive a caller that goes quiet mid-request.

Port 4663 is where agents send signing and trade requests. A connection is held
for the length of one conversation, and the pool of workers is finite, so a
caller that announces a body and then stops sending would hold its worker for as
long as the process lives. Enough of those and agents get no answer at all, with
nothing in the window to explain it.

This happens by accident more than by intent - a machine that sleeps or a link
that drops mid-request never closes the socket, so there is nothing for the
server to notice on its own.

The balancing property is that the timeout must not touch a slow *response*.
Executing an auto-approved trade waits on block confirmations and can take
minutes; that is the server working, not the connection idling, and it must be
allowed to finish.
"""

import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.services.server import (
    MAX_WORKER_THREADS, SOCKET_TIMEOUT_SECONDS, AgentRequestHandler,
    ThreadedHTTPServer,
)

TEST_PORT = 19410


@pytest.fixture(scope="module")
def server():
    """The real handler, so routing and rejection behave as they do in the app."""
    srv = ThreadedHTTPServer(("127.0.0.1", TEST_PORT), AgentRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield srv
    srv.shutdown()
    srv.server_close()


def get_health(timeout=5.0):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{TEST_PORT}/health", timeout=timeout) as r:
        return r.status


def stalled_connection():
    """Announce a body, send none, and keep the socket open."""
    s = socket.create_connection(("127.0.0.1", TEST_PORT), timeout=5)
    s.sendall(
        b"POST /sign HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 5000\r\n"
        b"\r\n"
    )
    return s


class TestConfiguration:

    def test_handler_has_a_socket_timeout(self):
        assert AgentRequestHandler.timeout == SOCKET_TIMEOUT_SECONDS
        assert SOCKET_TIMEOUT_SECONDS > 0

    def test_the_live_agent_port_cannot_be_bound_over(self):
        """A local process must not be able to bind over a running agent API.

        HTTPServer sets allow_reuse_address on by default, which on Windows
        means SO_REUSEADDR - and there that flag lets a second socket bind a
        port already in active use. Any local process could then take agent
        traffic: payment and trade requests, carrying the tokens agents
        authenticate with. The admin server was hardened against this; this
        server serves the more sensitive port and was not.
        """
        srv = ThreadedHTTPServer(("127.0.0.1", 19411), AgentRequestHandler)
        try:
            hostile = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            hostile.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                with pytest.raises(OSError):
                    hostile.bind(("127.0.0.1", 19411))
            finally:
                hostile.close()

            with pytest.raises(OSError):
                ThreadedHTTPServer(("127.0.0.1", 19411), AgentRequestHandler)
        finally:
            srv.server_close()

    def test_worker_count_is_bounded(self):
        """Assert the ceiling exists, not that an attribute is set.

        This previously checked `max_children`, which ThreadingMixIn never
        reads — it belongs to ForkingMixIn. The attribute was present and the
        cap was not, so the test passed against a server with no ceiling at
        all. Check the thing that actually bounds concurrency.
        """
        import socketserver
        assert "max_children" not in socketserver.ThreadingMixIn.__dict__, (
            "if ThreadingMixIn gains max_children, prefer it over the semaphore")

        server = ThreadedHTTPServer(("127.0.0.1", 0), AgentRequestHandler)
        try:
            assert server._slots._initial_value == MAX_WORKER_THREADS
            assert ThreadedHTTPServer.process_request is not socketserver.ThreadingMixIn.process_request
        finally:
            server.server_close()

    def test_a_handled_connection_returns_its_slot(self):
        """A cap that never gives slots back is a cap that stops serving."""
        server = ThreadedHTTPServer(("127.0.0.1", 0), AgentRequestHandler)
        try:
            import socket as _socket
            a, b = _socket.socketpair()
            before = server._slots._value
            server._slots.acquire()
            assert server._slots._value == before - 1
            server.shutdown_request(a)     # what the worker calls when done
            assert server._slots._value == before
            b.close()
        finally:
            server.server_close()

    def test_server_is_threaded(self):
        from socketserver import ThreadingMixIn
        assert issubclass(ThreadedHTTPServer, ThreadingMixIn)


class TestStalledCallers:

    def test_a_stalled_caller_does_not_block_others(self, server):
        s = stalled_connection()
        try:
            time.sleep(0.3)
            assert get_health() == 200
        finally:
            s.close()

    def test_many_stalled_callers_leave_the_api_usable(self, server):
        """More stalled connections than a naive server could survive."""
        held = []
        try:
            for _ in range(12):
                held.append(stalled_connection())
            time.sleep(0.3)
            assert get_health() == 200
        finally:
            for s in held:
                s.close()

    def test_concurrent_callers_are_served_concurrently(self, server):
        results = []

        def hit():
            try:
                results.append(get_health())
            except Exception as e:  # pragma: no cover - failure detail only
                results.append(e)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == [200] * 8


class TestSlowWorkIsNotInterrupted:
    """The timeout counts silence on the socket, not time spent working."""

    def test_a_handler_slower_than_the_timeout_still_answers(self):
        """A trade waiting on confirmations outlives the timeout and completes.

        Uses its own server with a deliberately tiny timeout, so the property is
        demonstrated in seconds rather than by waiting out the real one.
        """
        from http.server import BaseHTTPRequestHandler

        work_seconds = 3.0
        tiny_timeout = 1.0

        class SlowHandler(BaseHTTPRequestHandler):
            timeout = tiny_timeout

            def log_message(self, *args):
                pass

            def do_GET(self):
                time.sleep(work_seconds)  # stands in for two block confirmations
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"executed")

        port = TEST_PORT + 1
        srv = ThreadedHTTPServer(("127.0.0.1", port), SlowHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            started = time.time()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=30) as r:
                body = r.read()
            elapsed = time.time() - started

            assert body == b"executed"
            assert elapsed >= work_seconds, "the handler did not run to completion"
        finally:
            srv.shutdown()
            srv.server_close()
