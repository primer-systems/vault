"""
Admin API browser-origin rejection tests.

The Admin API listens on 127.0.0.1 so a separate CLI process can drive a core
that lives in the GUI (or headless daemon) process. HTTP on loopback cannot tell
that CLI apart from a browser tab - both are just local TCP connections - so a
web page the user has open could otherwise POST to /agents in the background.
Same-origin rules stop it reading the reply, but the command would still run,
and a silently-created agent is useful to an attacker even blind.

Vault's own clients never set `Origin`: the CLI uses urlopen, and the GUI holds
the core object directly and never speaks HTTP. A browser always sets it on
requests a page initiates. These tests pin that discriminator down, because it
is a security control that nothing else in the suite would notice losing.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.core import Vault
from primer_vault.daemon.admin_api import AdminAPIServer
from primer_vault.client.core_client import CoreClient, ClientConfig

TEST_PORT = 19407  # Non-standard port, avoids clashing with a real instance

# Origins a browser can actually produce. "null" is what a sandboxed iframe or a
# file:// page sends, so it must be rejected like any other.
BROWSER_ORIGINS = [
    "https://evil.example",
    "http://evil.example",
    "http://localhost",
    "http://localhost:3000",
    "http://127.0.0.1:4664",
    "null",
]

MUTATING_REQUESTS = [
    ("POST", "/agents", {"name": "should-not-exist"}),
    ("POST", "/policies", {"name": "should-not-exist"}),
    ("PUT", "/settings", {"key": "allow_lan", "value": True}),
    ("PATCH", "/agents/ABC123", {"name": "renamed"}),
    ("DELETE", "/agents/ABC123", None),
]

READ_REQUESTS = [
    ("GET", "/status"),
    ("GET", "/agents"),
    ("GET", "/policies"),
    ("GET", "/wallet/status"),
    ("GET", "/transactions"),
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A core + AdminAPIServer for the module. Mode is set per-test."""
    data_dir = tmp_path_factory.mktemp("origin_guard_data")
    (data_dir / "wallets").mkdir(exist_ok=True)
    core = Vault(data_dir=data_dir)
    wallet_path = str(data_dir / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, "testpass")
    core.load_wallet(wallet_path, "testpass")
    srv = AdminAPIServer(core, port=TEST_PORT)
    srv.start()
    time.sleep(0.2)  # Let the server thread bind
    yield core, srv
    srv.stop()


@pytest.fixture
def open_mode(server):
    """Admin API in 'open' mode - the setting that exposes the port."""
    core, _ = server
    core.settings_manager.set_admin_api_mode("open")
    return core


@pytest.fixture
def gui_only_mode(server):
    """Admin API in its default 'gui_only' mode."""
    core, _ = server
    core.settings_manager.set_admin_api_mode("gui_only")
    return core


def request(method, path, origin=None, body=None, extra_headers=None):
    """Issue a raw request. Returns (status, parsed_body)."""
    req = urllib.request.Request(f"http://127.0.0.1:{TEST_PORT}{path}", method=method)
    if origin is not None:
        req.add_header("Origin", origin)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        # text/plain is what an attacker would use: it is a CORS "simple" content
        # type, so the browser sends it without asking permission first.
        req.add_header("Content-Type", "text/plain")
        req.data = json.dumps(body).encode()
    def parse(raw):
        # Handlers answer in JSON; the stdlib's own error pages (405, 501) do not.
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, parse(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read().decode())


# -------------------------------------------------------------------------
# A browser is turned away
# -------------------------------------------------------------------------

class TestBrowserOriginRejected:

    @pytest.mark.parametrize("origin", BROWSER_ORIGINS)
    def test_every_browser_origin_is_rejected(self, open_mode, origin):
        status, body = request("GET", "/status", origin=origin)
        assert status == 403
        assert body["code"] == "BROWSER_ORIGIN_REJECTED"

    @pytest.mark.parametrize("method,path,payload", MUTATING_REQUESTS)
    def test_mutating_requests_are_rejected(self, open_mode, method, path, payload):
        status, body = request(method, path, origin="https://evil.example", body=payload)
        assert status == 403
        assert body["code"] == "BROWSER_ORIGIN_REJECTED"

    @pytest.mark.parametrize("method,path", READ_REQUESTS)
    def test_read_requests_are_rejected(self, open_mode, method, path):
        status, body = request(method, path, origin="https://evil.example")
        assert status == 403
        assert body["code"] == "BROWSER_ORIGIN_REJECTED"

    def test_rejected_in_gui_only_mode_too(self, gui_only_mode):
        """The guard is independent of the mode setting, not a side effect of it.

        /status is the one path gui_only lets through, so it is the only place a
        mode-shaped bug could hide behind a passing test.
        """
        status, body = request("GET", "/status", origin="https://evil.example")
        assert status == 403
        assert body["code"] == "BROWSER_ORIGIN_REJECTED"


# -------------------------------------------------------------------------
# The rejection happens before anything runs
# -------------------------------------------------------------------------

class TestNoSideEffects:
    """A 403 after the work is done would be worse than useless."""

    def test_rejected_agent_creation_creates_no_agent(self, open_mode):
        core = open_mode
        before = {a.name for a in core.get_all_agents()}

        status, _ = request("POST", "/agents", origin="https://evil.example",
                            body={"name": "drive-by-agent"})
        assert status == 403

        after = {a.name for a in core.get_all_agents()}
        assert after == before
        assert "drive-by-agent" not in after

    def test_guard_runs_before_the_body_is_parsed(self, open_mode):
        """Malformed body still yields the origin error, not a parse error.

        Proves ordering: if the body were read first this would surface as a 400
        or a 500 instead.
        """
        req = urllib.request.Request(
            f"http://127.0.0.1:{TEST_PORT}/agents", method="POST")
        req.add_header("Origin", "https://evil.example")
        req.add_header("Content-Type", "text/plain")
        req.data = b"{not valid json at all"
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("expected HTTP 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
            assert json.loads(e.read())["code"] == "BROWSER_ORIGIN_REJECTED"


# -------------------------------------------------------------------------
# Vault's own clients still work
# -------------------------------------------------------------------------

class TestLegitimateClientsUnaffected:

    @pytest.mark.parametrize("method,path", READ_REQUESTS)
    def test_no_origin_header_is_allowed(self, open_mode, method, path):
        status, _ = request(method, path)
        assert status == 200

    def test_cli_can_still_create_an_agent(self, open_mode):
        """The exact capability the guard blocks for a browser must still work
        for the CLI, or the control is too blunt to ship."""
        core = open_mode
        status, body = request("POST", "/agents", body={"name": "legit-cli-agent"})
        assert status in (200, 201), body
        assert "legit-cli-agent" in {a.name for a in core.get_all_agents()}

    def test_core_client_round_trip(self, open_mode):
        """End-to-end through the real client the CLI uses, not just raw urllib."""
        client = CoreClient(ClientConfig(admin_port=TEST_PORT, timeout=5.0, retry_count=1))
        assert client.is_daemon_running()

    def test_empty_origin_header_is_treated_as_absent(self, open_mode):
        """Documents current behaviour: a present-but-empty Origin passes.

        No browser emits one, so this is not a bypass - but if that assumption
        ever changes, this test is the place it will be noticed.
        """
        status, _ = request("GET", "/status", origin="")
        assert status == 200

    def test_unrelated_headers_do_not_trigger_the_guard(self, open_mode):
        """Only Origin gates. Referer alone must not, or curl users break."""
        status, _ = request("GET", "/status",
                            extra_headers={"Referer": "https://evil.example",
                                           "User-Agent": "Mozilla/5.0"})
        assert status == 200


# -------------------------------------------------------------------------
# Preflight
# -------------------------------------------------------------------------

class TestPreflight:

    def test_preflight_is_not_answered(self, open_mode):
        """Nothing here is meant to be reached from a browser, so the server
        offers no CORS preflight at all. A page that cannot preflight cannot
        make the real request either, which is the outcome we want."""
        status, _ = request("OPTIONS", "/agents", origin="https://evil.example")
        assert status == 501


# -------------------------------------------------------------------------
# Unreadable settings
# -------------------------------------------------------------------------

class TestModeUnreadable:
    """If the mode setting cannot be read, assume the locked-down mode.

    A check that cannot establish whether a request is permitted must not let
    it through. /status must still
    pass, or the CLI decides no instance is running and starts a second core
    against the same data directory.
    """

    @pytest.fixture
    def broken_settings(self, server):
        core, _ = server
        real = core.settings_manager.get_admin_api_mode

        def boom():
            raise RuntimeError("settings not loaded")

        core.settings_manager.get_admin_api_mode = boom
        yield core
        core.settings_manager.get_admin_api_mode = real

    def test_status_still_allowed(self, broken_settings):
        """Instance detection must survive, or the CLI forks a second core."""
        status, _ = request("GET", "/status")
        assert status == 200

    @pytest.mark.parametrize("method,path,payload", MUTATING_REQUESTS)
    def test_mutating_requests_denied(self, broken_settings, method, path, payload):
        status, body = request(method, path, body=payload)
        assert status == 403
        assert body["code"] == "GUI_ONLY_MODE"

    def test_reads_other_than_status_denied(self, broken_settings):
        status, body = request("GET", "/agents")
        assert status == 403
        assert body["code"] == "GUI_ONLY_MODE"

    def test_no_agent_is_created(self, broken_settings):
        before = {a.name for a in broken_settings.get_all_agents()}
        request("POST", "/agents", body={"name": "created-while-settings-broken"})
        assert {a.name for a in broken_settings.get_all_agents()} == before
