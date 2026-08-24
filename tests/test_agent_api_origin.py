"""
The agent API's defences against a web page.

This is the server that signs payments and executes trades. It listens on
loopback, where a browser tab and a local program look identical at the socket,
so the same discriminator the Admin API uses applies here: a browser stamps
`Origin` on requests a page initiates, and Vault's own callers never do.

No response may advertise `Access-Control-Allow-Origin: *`, invite preflight, or
accept `/callback` without proof of identity: a page the user has open must not
be able to write false settlement records.

Two separate controls, both needed:
  - dropping CORS stops a page reading a reply;
  - refusing an Origin stops it firing a blind write, which against /callback
    was the whole attack.

None of this had a test: the HTTP layer was the least covered file in the tree.
"""

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.services import server as server_module
from primer_vault.services.server import AgentServer

TEST_PORT = 19663  # avoids clashing with a real instance on 4663

BROWSER_ORIGINS = [
    "https://evil.example",
    "http://localhost",
    "http://localhost:3000",
    "http://127.0.0.1:4663",
    "null",  # a sandboxed iframe or a file:// page
]


class RecordingSigningService:
    """Stands in for the signing service, recording what reached it."""

    def __init__(self):
        self.calls = []

    def handle_ping(self, agent_id):
        self.calls.append(("ping", agent_id))
        return {"status": "ready", "agent_name": "Bot"}

    def handle_callback(self, agent_id, transaction_id, event, tx_hash=None,
                        error=None, signature=None):
        self.calls.append(("callback", agent_id, event, signature))
        if not signature:
            return {"status": "error", "code": "AUTH_FAILED", "error": "Missing authentication"}
        return {"status": "ok", "transaction_id": transaction_id, "new_status": event}

    def handle_sign_request(self, *a, **k):
        self.calls.append(("sign",))
        return {"status": "error", "code": "AUTH_FAILED", "error": "no"}


@pytest.fixture
def running_server():
    svc = RecordingSigningService()
    srv = AgentServer()
    srv.set_signing_service(svc)
    assert srv.start(port=TEST_PORT), "test server did not start"
    try:
        yield svc
    finally:
        srv.stop()
        server_module._signing_service = None


def request(method, path, origin=None, body=None, headers=None):
    """Issue a request. Returns (status, parsed_body, response_headers)."""
    url = f"http://127.0.0.1:{TEST_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    if origin is not None:
        req.add_header("Origin", origin)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()

    def parse(raw):
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, parse(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read().decode()), dict(e.headers)


def raw_request(payload: bytes) -> str:
    """Send bytes straight down a socket, for headers urllib will not produce."""
    with socket.create_connection(("127.0.0.1", TEST_PORT), timeout=5) as sock:
        sock.sendall(payload)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if len(b"".join(chunks)) > 65536:
                break
    return b"".join(chunks).decode(errors="replace")


# ---------------------------------------------------------------------------
# A browser is turned away
# ---------------------------------------------------------------------------

class TestBrowserOriginRejected:

    @pytest.mark.parametrize("origin", BROWSER_ORIGINS)
    def test_post_with_an_origin_is_refused(self, running_server, origin):
        status, body, _ = request("POST", "/callback", origin=origin, body={
            "agent_id": "ABC123", "transaction_id": "t", "event": "settled"})
        assert status == 403
        assert body["code"] == "BROWSER_ORIGIN_REJECTED"

    @pytest.mark.parametrize("origin", BROWSER_ORIGINS)
    def test_get_with_an_origin_is_refused(self, running_server, origin):
        status, body, _ = request("GET", "/status", origin=origin)
        assert status == 403
        assert body["code"] == "BROWSER_ORIGIN_REJECTED"

    def test_the_refused_request_never_reaches_the_service(self, running_server):
        request("POST", "/callback", origin="https://evil.example", body={
            "agent_id": "ABC123", "transaction_id": "t", "event": "settled"})
        request("POST", "/ping", origin="https://evil.example", body={"agent_id": "ABC123"})
        assert running_server.calls == []

    def test_a_local_program_without_an_origin_is_served(self, running_server):
        status, body, _ = request("POST", "/ping", body={"agent_id": "ABC123"})
        assert status == 200
        assert body["status"] == "ready"


# ---------------------------------------------------------------------------
# Nothing invites a browser in
# ---------------------------------------------------------------------------

class TestNoCors:

    @pytest.mark.parametrize("method,path,body", [
        ("GET", "/health", None),
        ("GET", "/status", None),
        ("GET", "/nonexistent", None),
        ("POST", "/ping", {"agent_id": "ABC123"}),
        ("POST", "/nonexistent", {}),
    ])
    def test_no_cors_header_is_ever_sent(self, running_server, method, path, body):
        _, _, headers = request(method, path, body=body)
        assert not any(k.lower().startswith("access-control") for k in headers), headers

    def test_preflight_is_not_answered(self, running_server):
        """With no CORS to negotiate, a page cannot preflight - and a page that
        cannot preflight cannot make the real request either."""
        status, _, _ = request("OPTIONS", "/sign", origin="https://evil.example")
        assert status in (403, 501)

    def test_the_source_carries_no_cors_header_at_all(self):
        src = Path(__file__).parent.parent / "src" / "primer_vault"
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "Access-Control-Allow-Origin" not in text, path


# ---------------------------------------------------------------------------
# The settlement record needs proof of identity
# ---------------------------------------------------------------------------

class TestCallbackAuthentication:

    def test_callback_without_a_signature_is_refused(self, running_server):
        status, body, _ = request("POST", "/callback", body={
            "agent_id": "ABC123", "transaction_id": "t", "event": "settled",
            "tx_hash": "0x" + "11" * 32})
        assert status == 400
        assert body["code"] == "MISSING_SIGNATURE"

    def test_nothing_is_written_without_a_signature(self, running_server):
        request("POST", "/callback", body={
            "agent_id": "ABC123", "transaction_id": "t", "event": "settled"})
        assert running_server.calls == []

    def test_a_signature_is_passed_through_for_verification(self, running_server):
        status, _, _ = request("POST", "/callback", body={
            "agent_id": "ABC123", "signature": "SIG:1:deadbeef",
            "transaction_id": "t", "event": "submitted"})
        assert status == 200
        assert running_server.calls == [("callback", "ABC123", "submitted", "SIG:1:deadbeef")]

    def test_the_helper_builds_what_the_server_verifies(self):
        """The signed shape has to match on both sides. They drifted once
        before, and every signature the helper produced was rejected."""
        import hashlib
        import hmac
        sys.path.insert(0, str(
            Path(__file__).parent.parent / "src" / "primer_vault" /
            "skills" / "vault-x402-payment" / "scripts"))
        import primer_sign

        token = "AT_" + "ab" * 32
        signed = primer_sign.sign_callback("ABC123", token, "tx-1", "settled", "0x" + "cd" * 32)
        _, timestamp, provided = signed["signature"].split(":", 2)

        # Rebuild exactly as SigningService._verify_callback_auth does.
        message = json.dumps({
            "agent_id": "ABC123",
            "timestamp": int(timestamp),
            "callback": {"transaction_id": "tx-1", "event": "settled",
                         "tx_hash": "0x" + "cd" * 32},
        }, separators=(',', ':'), sort_keys=True).encode()
        expected = hmac.new(bytes.fromhex(token[3:]), message, hashlib.sha256).hexdigest()
        assert provided == expected


# ---------------------------------------------------------------------------
# The request size limit holds
# ---------------------------------------------------------------------------

class TestContentLength:

    @staticmethod
    def _post_with_length(value):
        return raw_request(
            f"POST /ping HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{TEST_PORT}\r\n"
            f"Content-Length: {value}\r\n"
            f"Connection: close\r\n\r\n".encode())

    def test_a_negative_length_is_refused(self, running_server):
        """A negative length passed the size check and then read to end of
        connection, so the 1 MB cap could be walked straight past."""
        resp = self._post_with_length("-1")
        assert "400" in resp.split("\r\n")[0]
        assert "INVALID_CONTENT_LENGTH" in resp

    def test_a_non_numeric_length_is_refused(self, running_server):
        resp = self._post_with_length("banana")
        assert "400" in resp.split("\r\n")[0]
        assert "INVALID_CONTENT_LENGTH" in resp

    def test_an_oversize_length_is_refused(self, running_server):
        resp = self._post_with_length("99999999")
        assert "413" in resp.split("\r\n")[0]

    def test_a_normal_body_still_works(self, running_server):
        status, body, _ = request("POST", "/ping", body={"agent_id": "ABC123"})
        assert status == 200


# ---------------------------------------------------------------------------
# Version reporting
# ---------------------------------------------------------------------------

def test_status_reports_the_real_version(running_server):
    from primer_vault.version import __version__
    _, body, _ = request("GET", "/status")
    assert body["version"] == __version__


# ---------------------------------------------------------------------------
# Pages this server renders cannot be made to carry code
# ---------------------------------------------------------------------------

HOSTILE = '<img src=x onerror="fetch(\'/callback\')">'


def _hostile_receipt():
    """A receipt where every free-text field is an injection attempt.

    The resource description is chosen by whoever issued the payment request, and
    agent and policy names are free text, so all of these are attacker-reachable.
    """
    return {
        "status": "settled", "transactionId": "id" + HOSTILE, "timestamp": HOSTILE,
        "intent": {"policyName": HOSTILE, "agent": {"name": HOSTILE, "code": HOSTILE}},
        "authorization": {"method": "auto", "authorizedAt": HOSTILE},
        "payment": {"amount": {"formatted": HOSTILE, "micro": 1},
                    "recipient": HOSTILE, "network": HOSTILE, "resource": HOSTILE},
        "settlement": {"txHash": HOSTILE, "settledAt": HOSTILE,
                       "verification": {"status": HOSTILE}},
    }


class TestReceiptPageCannotBeInjected:

    def _render(self):
        from primer_vault.services.server import AgentRequestHandler
        return AgentRequestHandler._render_receipt_html(None, _hostile_receipt())

    def test_the_hostile_string_never_appears_as_markup(self):
        page = self._render()
        assert HOSTILE not in page
        assert "<img src=x" not in page

    def test_it_appears_escaped_instead(self):
        assert "&lt;img src=x onerror=" in self._render()

    def test_no_event_handler_attribute_survives(self):
        page = self._render()
        assert "onerror=\"" not in page.replace("onerror=&quot;", "")

    def test_a_missing_field_does_not_break_the_page(self):
        """Escaping must cope with absent values, or a sparse receipt 500s."""
        from primer_vault.services.server import AgentRequestHandler
        page = AgentRequestHandler._render_receipt_html(None, {"status": "signed"})
        assert "<html" in page and "None" not in page.split("<style>")[0]


class TestHtmlPagesForbidScript:

    def test_html_responses_carry_a_no_script_policy(self, running_server):
        _, _, headers = request("GET", "/")
        csp = headers.get("Content-Security-Policy", "")
        assert "default-src 'none'" in csp
        assert "script-src" not in csp, "no script source should be permitted at all"
        assert headers.get("X-Content-Type-Options") == "nosniff"

    def test_json_responses_are_unaffected(self, running_server):
        _, body, headers = request("GET", "/health")
        assert body == {"status": "ok"}
