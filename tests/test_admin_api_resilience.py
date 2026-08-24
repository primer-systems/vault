"""The Admin API must survive a local caller that behaves badly.

Port 4664 is how a CLI process drives a core living in the GUI. Anything running
on the machine can reach it, so "well-behaved client" is not an assumption worth
making - a buggy script is as damaging here as a hostile one.

Two properties this pins down:

  - One caller cannot deny the API to the others. A connection that announces a
    body and then sends nothing must not hold a worker indefinitely, and must
    not stop anyone else being served meanwhile.
  - A declared body size is a claim, not a fact. It is checked against a ceiling
    before any of it is read, so an implausible one costs nothing to refuse.
"""

import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.daemon.admin_api import (
    MAX_CONTENT_LENGTH, MAX_WORKER_THREADS, AdminAPIServer, AdminRequestHandler,
    ThreadedAdminServer,
)

TEST_PORT = 19408  # Distinct from the origin-guard suite's port


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("admin_resilience_data")
    (data_dir / "wallets").mkdir(exist_ok=True)
    core = Vault(data_dir=data_dir)
    core.settings_manager.set_admin_api_mode("open")
    srv = AdminAPIServer(core, port=TEST_PORT)
    srv.start()
    time.sleep(0.2)  # Let the server thread bind
    yield core
    srv.stop()


def get_status(timeout=5.0):
    """A plain GET /status, the call the CLI uses to find a running instance."""
    with urllib.request.urlopen(
            f"http://127.0.0.1:{TEST_PORT}/status", timeout=timeout) as r:
        return r.status


class TestConfiguration:
    """Guards on the settings themselves, so a future edit cannot quietly undo
    these without a test noticing."""

    def test_server_is_actually_threaded(self):
        from socketserver import ThreadingMixIn
        assert issubclass(ThreadedAdminServer, ThreadingMixIn)

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

        srv = ThreadedAdminServer(("127.0.0.1", 0), AdminRequestHandler)
        try:
            assert srv._slots._initial_value == MAX_WORKER_THREADS
            assert ThreadedAdminServer.process_request is not socketserver.ThreadingMixIn.process_request
        finally:
            srv.server_close()

    def test_a_handled_connection_returns_its_slot(self):
        """A cap that never gives slots back is a cap that stops serving."""
        srv = ThreadedAdminServer(("127.0.0.1", 0), AdminRequestHandler)
        try:
            a, b = socket.socketpair()
            before = srv._slots._value
            srv._slots.acquire()
            assert srv._slots._value == before - 1
            srv.shutdown_request(a)     # what the worker calls when done
            assert srv._slots._value == before
            b.close()
        finally:
            srv.server_close()

    def test_handler_has_a_socket_timeout(self):
        assert AdminRequestHandler.timeout is not None
        assert AdminRequestHandler.timeout > 0


class TestStalledConnection:

    def test_a_stalled_caller_does_not_block_everyone_else(self, server):
        """Announce a body, send nothing, never close. Others must still be served.

        This is the whole point: the stalled socket stays open for the duration
        of the test, so a single-threaded server has no way to answer the GET.
        """
        stalled = socket.create_connection(("127.0.0.1", TEST_PORT), timeout=5)
        try:
            stalled.sendall(
                b"POST /policies HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 1000\r\n"
                b"\r\n"
            )  # ...and now nothing. The 1000 bytes never arrive.
            time.sleep(0.3)  # Let the server pick it up and start waiting.

            assert get_status() == 200, "a stalled connection is blocking the Admin API"
        finally:
            stalled.close()

    def test_several_stalled_callers_still_leave_the_api_usable(self, server):
        """One is an accident; a handful is what a retry loop looks like."""
        stalled = []
        try:
            for _ in range(5):
                s = socket.create_connection(("127.0.0.1", TEST_PORT), timeout=5)
                s.sendall(
                    b"POST /policies HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Length: 1000\r\n"
                    b"\r\n"
                )
                stalled.append(s)
            time.sleep(0.3)

            assert get_status() == 200
        finally:
            for s in stalled:
                s.close()

    def test_concurrent_requests_are_served_concurrently(self, server):
        """Not just "eventually" - at the same time."""
        results = []

        def hit():
            try:
                results.append(get_status())
            except Exception as e:  # pragma: no cover - failure detail only
                results.append(e)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == [200] * 8


class TestOversizedBody:

    def test_a_body_larger_than_the_cap_is_refused(self, server):
        """The declared length is refused before any of it is read."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{TEST_PORT}/policies", method="POST")
        req.add_header("Content-Type", "application/json")
        req.data = b'{"name": "x"}'
        # Lie about the size: claim far more than the cap allows.
        req.add_header("Content-Length", str(MAX_CONTENT_LENGTH + 1))

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)

        assert exc.value.code == 400, "an oversized body should be a client error"

    def test_the_api_still_works_afterwards(self, server):
        assert get_status() == 200


class TestUnlockThrottle:
    """The only endpoint that takes a guess at a secret is metered and logged.

    Argon2id carries the actual security here, at roughly 280ms a guess. The
    throttle bounds unattended grinding and makes it visible in the log rather
    than silent.
    """

    def setup_method(self):
        from primer_vault.daemon import admin_api
        admin_api._unlock_attempts.record_success()  # clear state between tests

    def _attempt(self, password="wrong"):
        import json as _json
        req = urllib.request.Request(
            f"http://127.0.0.1:{TEST_PORT}/wallet/unlock", method="POST")
        req.add_header("Content-Type", "application/json")
        req.data = _json.dumps(
            {"wallet_path": "/nonexistent.wallet", "password": password}).encode()
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, {}
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read() or b"{}")

    def test_a_run_of_failures_is_paused(self, server):
        from primer_vault.daemon.admin_api import UNLOCK_MAX_FAILURES

        for _ in range(UNLOCK_MAX_FAILURES):
            status, _ = self._attempt()
            assert status == 401, "a wrong password should be rejected, not throttled yet"

        status, body = self._attempt()
        assert status == 429
        assert body.get("code") == "TOO_MANY_ATTEMPTS"

    def test_the_throttle_does_not_block_other_endpoints(self, server):
        from primer_vault.daemon.admin_api import UNLOCK_MAX_FAILURES

        for _ in range(UNLOCK_MAX_FAILURES + 1):
            self._attempt()

        assert get_status() == 200, "throttling unlock must not take the API down"


class TestErrorDetail:
    """A 500 says something happened; it does not say what, to the caller."""

    def test_internal_errors_do_not_return_exception_text(self, server, monkeypatch):
        import json as _json
        from primer_vault.daemon.admin_api import AdminRequestHandler

        secret = r"C:\Users\someone\secret-path\wallet.dat"

        def boom(self):
            raise RuntimeError(f"failed opening {secret}")

        monkeypatch.setattr(AdminRequestHandler, "_handle_get_agents", boom)

        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{TEST_PORT}/agents", timeout=10) as r:
                body = _json.loads(r.read())
                status = r.status
        except urllib.error.HTTPError as e:
            status, body = e.code, _json.loads(e.read() or b"{}")

        assert status == 500
        assert body.get("code") == "INTERNAL_ERROR"
        assert secret not in _json.dumps(body), "the reply leaked an internal path"
