"""
Agent HTTP Server - Handles requests from AI agents.

Provides endpoints for:
- /health - Health check
- /status - Server status (JSON)
- /agent - Agent instructions (Markdown)
- /sign - Sign x402 payment requests
- /ping - Connection test for agents

NOTE: This module has NO Qt dependencies.
"""

import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional, Callable, TYPE_CHECKING

from ..design_tokens import web_css_vars, status_color, DARK

if TYPE_CHECKING:
    from .signing import SigningService
    from .trading import TradingService


class ServerStats:
    """Track server statistics for current session."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.signed = 0
        self.rejected = 0
        self.started_at: Optional[str] = None

    def start(self):
        from datetime import datetime
        self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Global server stats instance
server_stats = ServerStats()

# Global signing service reference (set by AgentServer.set_signing_service)
_signing_service: Optional["SigningService"] = None

# Global trading service reference (set by AgentServer.set_trading_service)
_trading_service: Optional["TradingService"] = None


class RateLimiter:
    """
    LOAD-01: Simple rate limiter to prevent abuse.

    Limits requests per IP per minute.

    Note: Duplicate request detection is now handled by signature-based
    idempotency in SigningService, not by this class.
    """

    def __init__(self, requests_per_minute: int = 300):
        self.requests_per_minute = requests_per_minute
        self._request_times: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_rate_limited(self, client_ip: str) -> bool:
        """Check if client has exceeded rate limit."""
        now = time.time()
        window_start = now - 60

        with self._lock:
            # Clean old entries
            self._request_times[client_ip] = [
                t for t in self._request_times[client_ip]
                if t > window_start
            ]
            # Check limit
            if len(self._request_times[client_ip]) >= self.requests_per_minute:
                return True
            # Record this request
            self._request_times[client_ip].append(now)
            return False

    def reset(self):
        """Reset all rate limiting state."""
        with self._lock:
            self._request_times.clear()

    def configure(self, requests_per_minute: int = None):
        """Update rate limiter configuration."""
        with self._lock:
            if requests_per_minute is not None:
                self.requests_per_minute = requests_per_minute


# Global rate limiter instance
rate_limiter = RateLimiter()


# Map error codes to appropriate HTTP status codes
ERROR_CODE_TO_HTTP_STATUS = {
    # 400 Bad Request - client errors, malformed request
    "INVALID_X402_FORMAT": 400,
    "INVALID_X402_RESPONSE": 400,
    "INVALID_X402_DATA": 400,
    "INVALID_PAYMENT_DATA": 400,
    "INVALID_PAYMENT_REQUIRED": 400,
    "INVALID_RESPONSE_STATUS": 400,
    "INVALID_REQUEST": 400,
    "MISSING_X402_DATA": 400,
    "INVALID_EVENT": 400,
    "MISSING_TX_HASH": 400,
    "INVALID_TX_HASH": 400,

    # 401 Unauthorized - authentication failure
    "AUTH_FAILED": 401,

    # 403 Forbidden - authenticated but not allowed
    "NETWORK_DISABLED": 403,
    "DOMAIN_NOT_ALLOWED": 403,
    "DOMAIN_URL_REQUIRED": 403,
    "AGENT_SUSPENDED": 403,
    "AGENT_NOT_COMMISSIONED": 403,
    "UNAUTHORIZED": 403,

    # 404 Not Found - resource doesn't exist
    "AGENT_NOT_FOUND": 404,
    "POLICY_NOT_FOUND": 404,
    "TRANSACTION_NOT_FOUND": 404,
    "REQUEST_NOT_FOUND": 404,
    "ADDRESS_NOT_FOUND": 404,
    "AGENT_OR_POLICY_MISSING": 404,

    # 409 Conflict - request conflicts with current state
    "REQUEST_ALREADY_PROCESSED": 409,
    "PAYMENT_ALREADY_SETTLED": 409,

    # 429 Too Many Requests - rate/limit exceeded
    "RATE_LIMIT_EXCEEDED": 429,
    "LIMIT_REACHED": 429,
    "EXCEEDS_DAILY_LIMIT": 429,
    "EXCEEDS_PER_REQUEST_MAX": 429,

    # 500 Internal Server Error - server-side issues
    "NO_WALLET_PROVIDER": 500,
    "SDK_NOT_FOUND": 500,
    "SIGNING_ERROR": 500,

    # 503 Service Unavailable - temporary, retryable
    "SERVICE_NOT_READY": 503,
    "WALLET_LOCKED": 503,
}


def get_http_status_for_error(error_code: str) -> int:
    """Get the appropriate HTTP status code for an error code."""
    return ERROR_CODE_TO_HTTP_STATUS.get(error_code, 400)


# Maximum request body size (1MB - sufficient for x402 payloads)
MAX_CONTENT_LENGTH = 1 * 1024 * 1024

# Validation patterns for path/query parameters
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)
TX_HASH_PATTERN = re.compile(r'^0x[0-9a-fA-F]{64}$')
AGENT_ID_PATTERN = re.compile(r'^[A-Z0-9]{3,8}$')


def get_signing_helper() -> str:
    """Return a Python script that agents can use to sign requests."""
    return '''#!/usr/bin/env python3
"""
Vault Signing Helper - Sign x402 payment requests for Vault.

Uses HMAC-SHA256 for signing (stdlib only, no extra dependencies).

Usage:
    python vault_sign.py <agent_id> <agent_token> <payment_required_header>

Or import and use directly:
    from vault_sign import sign_request, send_to_primer_vault
"""

import hmac
import hashlib
import json
import sys
import time
import urllib.request


def sign_request(agent_id: str, agent_token: str, payment_required: str, request_url: str = None) -> dict:
    """
    Sign a request for Vault using HMAC-SHA256.

    Args:
        agent_id: Your agent ID (e.g., "ABC123")
        agent_token: Your agent token (e.g., "AT_abc123...")
        payment_required: The Payment-Required header value from the 402 response
        request_url: Optional URL you fetched (for domain verification)

    Returns:
        The signed request ready to POST to Vault's /sign endpoint
    """
    # Extract shared secret from token (strip "AT_" prefix)
    shared_secret = bytes.fromhex(agent_token[3:])

    # Create message to sign
    timestamp = int(time.time())
    message_data = {
        "agent_id": agent_id,
        "timestamp": timestamp,
        "payment_required": payment_required
    }
    if request_url:
        message_data["request_url"] = request_url
    message = json.dumps(message_data, separators=(',', ':'), sort_keys=True).encode()

    # Sign with HMAC-SHA256
    sig = hmac.new(shared_secret, message, hashlib.sha256).hexdigest()

    result = {
        "agent_id": agent_id,
        "signature": f"SIG:{timestamp}:{sig}",
        "payment_required": payment_required
    }
    if request_url:
        result["request_url"] = request_url
    return result


def send_to_primer_vault(signed_request: dict, primer_vault_url: str = "http://localhost:4663") -> dict:
    """
    Send a signed request to Vault and get the payment header.

    Returns the Vault response with payment header on success.
    """
    url = f"{primer_vault_url}/sign"
    data = json.dumps(signed_request).encode()

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python primer_sign.py <agent_id> <agent_token> <payment_required_header> [request_url]")
        sys.exit(1)

    agent_id = sys.argv[1]
    agent_token = sys.argv[2]
    payment_required = sys.argv[3]
    request_url = sys.argv[4] if len(sys.argv) > 4 else None

    signed = sign_request(agent_id, agent_token, payment_required, request_url)
    result = send_to_primer_vault(signed)
    print(json.dumps(result, indent=2))
'''


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- ... ---) so two skill files
    can be concatenated into one readable document."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def _load_skill(skill_name: str) -> Optional[str]:
    """Load a skill's SKILL.txt (frontmatter stripped), or None if not found."""
    import sys
    from pathlib import Path

    bases = []
    if hasattr(sys, '_MEIPASS'):  # PyInstaller frozen exe
        bases.append(Path(sys._MEIPASS) / "skills")
    bases.append(Path(__file__).parent.parent / "skills")  # pip / dev

    for base in bases:
        path = base / skill_name / "SKILL.txt"
        if path.exists():
            return _strip_frontmatter(path.read_text(encoding="utf-8"))
    return None


def get_agent_instructions() -> str:
    """Serve combined agent instructions for both Vault skills.

    Trading first (Vault's primary purpose), then x402 payments. The skills stay
    as two separate, independently-installable files; this only joins them for the
    single /agent onboarding page.
    """
    sections = []
    for skill_name in ("vault-trading", "vault-x402-payment"):
        section = _load_skill(skill_name)
        if section:
            sections.append(section)

    if not sections:
        return "# Error\n\nCould not find skill instructions."

    header = (
        "# Primer Vault — Agent Instructions\n\n"
        "Primer Vault holds the user's crypto keys and enforces their limits, so "
        "you never handle keys directly. It does two things: **trading** (Uniswap "
        "swaps on Robinhood Chain) and **x402 payments** (paying for APIs). Both "
        "capabilities are documented below.\n"
    )
    return header + "\n\n---\n\n" + "\n\n---\n\n".join(sections)


def get_logo_base64() -> str:
    """Get the logo as a base64 data URI for embedding in HTML."""
    import base64
    import logging
    try:
        from ..utils import get_assets_dir
        logo_path = get_assets_dir() / "wm_stacked.png"
        if logo_path.exists():
            with open(logo_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("ascii")
                return f"data:image/png;base64,{encoded}"
    except Exception as e:
        logging.getLogger(__name__).debug(f"Could not load logo: {e}")
    return ""


def get_branded_html(port: int) -> str:
    """Generate the branded HTML status page."""
    logo_data_uri = get_logo_base64()
    logo_html = f'<img src="{logo_data_uri}" alt="Vault">' if logo_data_uri else '<span style="color: var(--accent); font-weight: 600; letter-spacing: 2px;">PRIMER VAULT</span>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Vault - Agent Link</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    {web_css_vars()}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'JetBrains Mono', monospace;
      background: var(--bg);
      color: var(--accent);
      line-height: 1.6;
      font-size: 14px;
      min-height: 100vh;
      padding: 40px 20px;
    }}
    .container {{ max-width: 800px; margin: 0 auto; }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 30px;
    }}
    .logo img {{ height: 24px; }}
    .page-title {{ margin-bottom: 30px; }}
    .label {{
      color: var(--dim);
      font-size: 12px;
      letter-spacing: 2px;
      margin-bottom: 8px;
    }}
    h1 {{ font-size: 16px; font-weight: 400; margin-bottom: 8px; }}
    .description {{ color: var(--muted); font-size: 12px; }}
    .description a {{ color: var(--accent); text-decoration: none; }}
    .stats-bar {{
      display: flex;
      gap: 30px;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      padding: 12px 0;
      margin-bottom: 30px;
    }}
    .stat {{ font-size: 12px; }}
    .stat-label {{ color: var(--muted); }}
    .stat-value {{ color: var(--accent); }}
    .stat-value.warning {{ color: var(--rust); }}
    .panel {{
      background: var(--accent-tint);
      border: 1px solid var(--line);
      margin-bottom: 20px;
    }}
    .panel-header {{
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .panel-num {{ color: var(--line); font-size: 11px; }}
    .panel-icon {{ color: var(--rust); font-size: 14px; }}
    .panel-name {{ color: var(--accent); font-size: 13px; }}
    .panel-body {{ padding: 16px; }}
    .panel-body p {{ color: var(--muted); font-size: 12px; margin-bottom: 16px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
    th {{
      padding: 10px 12px;
      border: 1px solid var(--line);
      text-align: left;
      background: var(--accent-tint-strong);
      font-weight: 600;
      color: var(--accent);
    }}
    td {{ padding: 10px 12px; border: 1px solid var(--line); color: var(--muted); }}
    td code {{ color: var(--accent); }}
    .method {{ padding: 2px 6px; border-radius: 2px; font-size: 10px; font-weight: 600; }}
    .method-get {{ background: var(--line); color: var(--fg); }}
    .method-post {{ background: var(--dim); color: var(--bg); }}
    .footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-top: 30px;
      border-top: 1px solid var(--line);
      margin-top: 30px;
      font-size: 12px;
      color: var(--muted);
    }}
    .footer a {{ color: var(--muted); text-decoration: none; margin-left: 16px; }}
    .footer a:hover {{ color: var(--accent); }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}
    .live-dot {{
      display: inline-block;
      width: 8px;
      height: 8px;
      background: var(--accent);
      border-radius: 50%;
      margin-right: 8px;
      animation: pulse 2s ease-in-out infinite;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="logo">{logo_html}</div>
    </div>

    <div class="page-title">
      <div class="label">AGENT_LISTENER</div>
      <h1>Payment Authorization Oracle</h1>
      <p class="description">Local signing service for AI agents using x402 protocol. <a href="https://primer.systems">-> primer.systems</a></p>
    </div>

    <div class="stats-bar">
      <div class="stat">
        <span class="stat-label">Server: </span>
        <span class="stat-value"><span class="live-dot"></span>ONLINE</span>
      </div>
      <div class="stat">
        <span class="stat-label">Version: </span>
        <span class="stat-value">0.1.0</span>
      </div>
      <div class="stat">
        <span class="stat-label">{server_stats.signed}</span>
        <span class="stat-value"> signed</span>
      </div>
      <div class="stat">
        <span class="stat-label">{server_stats.rejected}</span>
        <span class="stat-value warning"> rejected</span>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <span class="panel-num">[01]</span>
        <span class="panel-icon">^</span>
        <span class="panel-name">FUNCTION</span>
      </div>
      <div class="panel-body">
        <p>This server accepts x402 payment requests from your local AI agents, presents them for user approval in the Vault desktop app, and returns signed authorizations. Your keys never leave the app; agent spending is controlled by your pay policies.</p>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <span class="panel-num">[02]</span>
        <span class="panel-icon">+</span>
        <span class="panel-name">ENDPOINTS</span>
      </div>
      <div class="panel-body">
        <p>Base URL: <code>http://localhost:{port}</code></p>
        <table>
          <thead>
            <tr>
              <th>Endpoint</th>
              <th>Method</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><code>/</code></td>
              <td><span class="method method-get">GET</span></td>
              <td>This status page (HTML)</td>
            </tr>
            <tr>
              <td><code>/agent</code></td>
              <td><span class="method method-get">GET</span></td>
              <td>Agent instructions (Markdown)</td>
            </tr>
            <tr>
              <td><code>/status</code></td>
              <td><span class="method method-get">GET</span></td>
              <td>Server status (JSON)</td>
            </tr>
            <tr>
              <td><code>/health</code></td>
              <td><span class="method method-get">GET</span></td>
              <td>Health check for agents</td>
            </tr>
            <tr>
              <td><code>/sign</code></td>
              <td><span class="method method-post">POST</span></td>
              <td>Submit x402 request for signing</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <span class="panel-num">[03]</span>
        <span class="panel-icon">?</span>
        <span class="panel-name">USAGE</span>
      </div>
      <div class="panel-body">
        <p>To request signing, POST to <code>/sign</code> with:</p>
        <pre style="background: var(--bg); border: 1px solid var(--line); padding: 12px; margin-top: 8px; font-size: 11px; overflow-x: auto;">
{{
  "agent_id": "ABC123",
  "signature": "SIG:1707408000:a1b2c3...",
  "payment_required": "eyJhY2NlcHRzIjpbey4uLn1d...",
  "request_url": "https://api.example.com/resource"
}}</pre>
        <p style="margin-top: 16px;">For AI agents: read <code>/agent</code> for detailed instructions.</p>
      </div>
    </div>

    <div class="footer">
      <span>&copy; 2025 Primer Systems</span>
      <span style="color: var(--dim);">dev@primer.systems</span>
      <div>
        <a href="https://x.com/primersystems">X</a>
        <a href="https://t.me/primersystems">TG</a>
        <a href="https://github.com/primersystems">GIT</a>
      </div>
    </div>
  </div>
</body>
</html>'''


class AgentRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for agent x402 signing requests."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def _send_json_response(self, status: int, data: dict):
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_html_response(self, status: int, html: str):
        """Send an HTML response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_text_response(self, status: int, text: str, content_type: str = "text/plain"):
        """Send a plain text response."""
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text.encode())

    # EP-02/EP-03: Define which endpoints accept which methods
    # Note: /sign/status/{id} and /receipt/{id} are dynamic paths handled separately
    GET_ENDPOINTS = frozenset(["/", "/agent", "/sign/helper", "/status", "/health"])
    POST_ENDPOINTS = frozenset(["/ping", "/sign", "/callback", "/mandate", "/trade"])

    def _send_method_not_allowed(self, allowed_methods: list[str]):
        """Send 405 Method Not Allowed response."""
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Allow", ", ".join(allowed_methods))
        self.end_headers()
        self.wfile.write(json.dumps({
            "error": "Method not allowed",
            "code": "METHOD_NOT_ALLOWED",
            "allowed_methods": allowed_methods
        }).encode())

    def _send_service_unavailable(self, data: dict, retry_after: int = 5):
        """Send 503 Service Unavailable response with Retry-After header.

        Used for retryable errors like WALLET_LOCKED where the agent should
        wait and retry rather than give up.
        """
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        response = {**data, "retry_after": retry_after}
        self.wfile.write(json.dumps(response).encode())

    def _get_base_path(self) -> str:
        """Get the path without query string."""
        if "?" in self.path:
            return self.path.split("?")[0]
        return self.path

    def _get_client_ip(self) -> str:
        """Get the client IP address."""
        return self.client_address[0]

    def _check_rate_limit(self) -> bool:
        """Check rate limit and send 429 if exceeded. Returns True if request should proceed."""
        if rate_limiter.is_rate_limited(self._get_client_ip()):
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "60")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "Rate limit exceeded",
                "code": "RATE_LIMIT_EXCEEDED",
                "retry_after": 60
            }).encode())
            return False
        return True

    def _render_receipt_html(self, receipt: dict) -> str:
        """Render an AP2 receipt as a branded HTML page."""
        status = receipt.get("status", "unknown")
        status_bg = status_color(status, DARK)

        intent = receipt.get("intent", {})
        auth = receipt.get("authorization", {})
        payment = receipt.get("payment", {})
        settlement = receipt.get("settlement")

        logo_data_uri = get_logo_base64()
        logo_html = f'<img src="{logo_data_uri}" alt="Vault">' if logo_data_uri else '<span style="color: var(--accent);">PRIMER VAULT</span>'

        settlement_html = ""
        if settlement:
            tx_hash = settlement.get("txHash", "")
            verification = settlement.get("verification", {})
            settlement_html = f'''
            <div class="panel">
              <div class="panel-header">SETTLEMENT</div>
              <div class="panel-body">
                <div class="field"><span class="label">TX Hash:</span> <code>{tx_hash[:20]}...{tx_hash[-8:]}</code></div>
                <div class="field"><span class="label">Settled At:</span> {settlement.get("settledAt", "N/A")}</div>
                <div class="field"><span class="label">Verification:</span> {verification.get("status", "unverified") if verification else "unverified"}</div>
              </div>
            </div>'''

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AP2 Receipt - {receipt.get("transactionId", "")[:8]}</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    {web_css_vars()}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'JetBrains Mono', monospace; background: var(--bg); color: var(--fg); padding: 40px 20px; font-size: 13px; }}
    .container {{ max-width: 600px; margin: 0 auto; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 20px; border-bottom: 1px solid var(--line); margin-bottom: 20px; }}
    .logo img {{ height: 24px; }}
    .title {{ color: var(--dim); font-size: 11px; letter-spacing: 2px; margin-bottom: 8px; }}
    h1 {{ font-size: 16px; font-weight: 400; color: var(--accent); margin-bottom: 4px; }}
    .tx-id {{ color: var(--muted); font-size: 11px; }}
    .status {{ display: inline-block; padding: 4px 12px; border-radius: 2px; font-size: 11px; font-weight: 600; background: {status_bg}; color: var(--bg); margin: 16px 0; }}
    .panel {{ background: var(--accent-tint); border: 1px solid var(--line); margin-bottom: 16px; }}
    .panel-header {{ padding: 10px 14px; border-bottom: 1px solid var(--line); color: var(--dim); font-size: 11px; letter-spacing: 1px; }}
    .panel-body {{ padding: 14px; }}
    .field {{ margin-bottom: 8px; }}
    .label {{ color: var(--muted); }}
    code {{ color: var(--accent); }}
    .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--line); font-size: 11px; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="logo">{logo_html}</div>
      <span style="color: var(--dim); font-size: 11px;">AP2 RECEIPT</span>
    </div>

    <div class="title">TRANSACTION</div>
    <h1>{payment.get("amount", {}).get("formatted", "$0.00")} Payment</h1>
    <div class="tx-id">ID: {receipt.get("transactionId", "N/A")}</div>
    <div class="status">{status.upper()}</div>

    <div class="panel">
      <div class="panel-header">INTENT (AUTHORIZATION)</div>
      <div class="panel-body">
        <div class="field"><span class="label">Policy:</span> {intent.get("policyName", "N/A")}</div>
        <div class="field"><span class="label">Agent:</span> {intent.get("agent", {}).get("name", "N/A")} (<code>{intent.get("agent", {}).get("code", "N/A")}</code>)</div>
        <div class="field"><span class="label">Approval:</span> {"Auto-approved by policy" if auth.get("method") == "auto" else "Manually approved"}</div>
        <div class="field"><span class="label">Authorized At:</span> {auth.get("authorizedAt", "N/A")}</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">PAYMENT</div>
      <div class="panel-body">
        <div class="field"><span class="label">Amount:</span> <code>{payment.get("amount", {}).get("formatted", "$0.00")}</code> ({payment.get("amount", {}).get("cents", 0)} cents)</div>
        <div class="field"><span class="label">Network:</span> {payment.get("network", "N/A")}</div>
        <div class="field"><span class="label">Recipient:</span> <code>{payment.get("recipient", "N/A")[:20]}...</code></div>
        <div class="field"><span class="label">Resource:</span> {payment.get("resource", "N/A") or "N/A"}</div>
      </div>
    </div>

    {settlement_html}

    <div class="footer">
      Generated by Vault &middot; AP2 Protocol v0.1 &middot; {receipt.get("timestamp", "")}
    </div>
  </div>
</body>
</html>'''

    def do_GET(self):
        """Handle GET requests."""
        # LOAD-01: Check rate limit
        if not self._check_rate_limit():
            return

        base_path = self._get_base_path()

        # EP-02: Check if this is a POST-only endpoint accessed with GET
        if base_path in self.POST_ENDPOINTS:
            self._send_method_not_allowed(["POST"])
            return

        if self.path == "/":
            port = self.server.server_address[1]
            self._send_html_response(200, get_branded_html(port))
        elif self.path.startswith("/agent"):
            port = self.server.server_address[1]
            # Parse query params for agent_code
            self._send_text_response(200, get_agent_instructions(), "text/plain")
        elif self.path == "/sign/helper":
            self._send_text_response(200, get_signing_helper(), "text/x-python")
        elif self.path == "/status":
            self._send_json_response(200, {
                "service": "Vault",
                "version": "1.0.0",
                "status": "ready",
                "signed": server_stats.signed,
                "rejected": server_stats.rejected,
                "started_at": server_stats.started_at
            })
        elif self.path == "/health":
            self._send_json_response(200, {"status": "ok"})
        elif base_path.startswith("/sign/status/"):
            # Request status endpoint: GET /sign/status/{request_id}
            request_id = base_path[13:]  # Strip "/sign/status/"
            if not request_id:
                self._send_json_response(400, {"status": "error", "error": "Missing request ID", "code": "MISSING_REQUEST_ID"})
                return
            if not UUID_PATTERN.match(request_id):
                self._send_json_response(400, {"status": "error", "error": "Invalid request ID format", "code": "INVALID_REQUEST_ID"})
                return

            if _signing_service:
                result = _signing_service.get_request_status(request_id)
                if result.get("code") == "REQUEST_NOT_FOUND":
                    self._send_json_response(404, result)
                elif result.get("status") == "pending":
                    self._send_json_response(202, result)
                elif result.get("status") == "success":
                    self._send_json_response(200, result)
                else:
                    self._send_json_response(200, result)
            else:
                self._send_json_response(503, {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})
        elif base_path.startswith("/trade/status/"):
            # Trade status endpoint: GET /trade/status/{request_id}
            request_id = base_path[14:]  # Strip "/trade/status/"
            if not request_id:
                self._send_json_response(400, {"status": "error", "error": "Missing request ID", "code": "MISSING_REQUEST_ID"})
                return
            if not UUID_PATTERN.match(request_id):
                self._send_json_response(400, {"status": "error", "error": "Invalid request ID format", "code": "INVALID_REQUEST_ID"})
                return
            if _trading_service:
                result = _trading_service.get_trade_status(request_id)
                if result.get("code") == "UNKNOWN_REQUEST":
                    self._send_json_response(404, result)
                elif result.get("status") == "pending":
                    self._send_json_response(202, result)
                else:
                    self._send_json_response(200, result)
            else:
                self._send_json_response(503, {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})
        elif base_path.startswith("/receipt/"):
            # AP2 receipt endpoint: GET /receipt/{tx_id}
            tx_id = base_path[9:]  # Strip "/receipt/"
            if not tx_id:
                self._send_json_response(400, {"status": "error", "error": "Missing transaction ID", "code": "MISSING_TRANSACTION_ID"})
                return
            # Accept either UUID or tx_hash format
            if not UUID_PATTERN.match(tx_id) and not TX_HASH_PATTERN.match(tx_id):
                self._send_json_response(400, {"status": "error", "error": "Invalid transaction ID format", "code": "INVALID_TRANSACTION_ID"})
                return

            if _signing_service:
                result = _signing_service.get_receipt(tx_id)
                if result.get("error"):
                    # Ensure error response has status field
                    if "status" not in result:
                        result["status"] = "error"
                    self._send_json_response(404, result)
                else:
                    # Check Accept header for HTML vs JSON
                    accept = self.headers.get("Accept", "")
                    if "text/html" in accept:
                        self._send_html_response(200, self._render_receipt_html(result))
                    else:
                        self._send_json_response(200, result)
            else:
                self._send_json_response(503, {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})
        else:
            self._send_json_response(404, {"status": "error", "error": "Not found", "code": "NOT_FOUND"})

    def do_POST(self):
        """Handle POST requests - signing and ping requests."""
        # LOAD-01: Check rate limit
        if not self._check_rate_limit():
            return

        base_path = self._get_base_path()

        # EP-03: Check if this is a GET-only endpoint accessed with POST
        if base_path in self.GET_ENDPOINTS:
            self._send_method_not_allowed(["GET"])
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_CONTENT_LENGTH:
            self._send_json_response(413, {
                "status": "error",
                "error": f"Payload too large (max {MAX_CONTENT_LENGTH} bytes)",
                "code": "PAYLOAD_TOO_LARGE"
            })
            return
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"

        try:
            request_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json_response(400, {"status": "error", "error": "Invalid JSON", "code": "INVALID_JSON"})
            return

        if self.path == "/ping":
            agent_id = request_data.get("agent_id")
            if not agent_id:
                self._send_json_response(400, {"status": "error", "error": "Missing agent_id", "code": "MISSING_AGENT_ID"})
                return

            if _signing_service:
                result = _signing_service.handle_ping(agent_id)
                if result.get("status") == "ready":
                    status_code = 200
                else:
                    status_code = get_http_status_for_error(result.get("code"))
                self._send_json_response(status_code, result)
            else:
                self._send_json_response(503, {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})

        elif self.path == "/sign":
            agent_id = request_data.get("agent_id")
            signature = request_data.get("signature")
            payment_required = request_data.get("payment_required")  # HTTP 402 Payment-Required header value
            x402_data = request_data.get("x402_data")  # AP2/A2A direct JSON format
            request_url = request_data.get("request_url")  # URL agent fetched (for domain verification)
            idempotency_key = request_data.get("idempotency_key")  # Optional: unique key per purchase for bearer mode

            if not agent_id:
                self._send_json_response(400, {"status": "error", "error": "Missing agent_id", "code": "MISSING_AGENT_ID"})
                return

            if not signature:
                self._send_json_response(400, {"status": "error", "error": "Missing signature", "code": "MISSING_SIGNATURE"})
                return

            # Must provide either payment_required (HTTP 402 header) OR x402_data (A2A direct)
            if not payment_required and not x402_data:
                self._send_json_response(400, {
                    "status": "error",
                    "error": "Missing payment_required or x402_data",
                    "code": "MISSING_X402_DATA"
                })
                return

            # Note: Duplicate detection is now handled by signature-based idempotency
            # in SigningService. Same signature = same request = cached result.
            # Different signature (even for same x402 data) = new purchase = process.

            if _signing_service:
                result = _signing_service.handle_sign_request(
                    agent_id, signature,
                    payment_required=payment_required,
                    x402_data=x402_data,
                    request_url=request_url,
                    idempotency_key=idempotency_key
                )
                status = result.get("status")
                error_code = result.get("code")
                if status == "success":
                    status_code = 200
                elif status == "pending":
                    status_code = 202
                elif error_code == "WALLET_LOCKED":
                    # User-fixable: wallet needs to be unlocked in the app
                    self._send_service_unavailable(result)
                    return
                else:
                    # Use centralized error code mapping for consistent HTTP status codes
                    status_code = get_http_status_for_error(error_code)
                self._send_json_response(status_code, result)
            else:
                self._send_service_unavailable({"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})

        elif self.path == "/trade":
            agent_id = request_data.get("agent_id")
            signature = request_data.get("signature")
            trade = request_data.get("trade")

            if not agent_id:
                self._send_json_response(400, {"status": "error", "error": "Missing agent_id", "code": "MISSING_AGENT_ID"})
                return
            if not signature:
                self._send_json_response(400, {"status": "error", "error": "Missing signature", "code": "MISSING_SIGNATURE"})
                return
            if not isinstance(trade, dict):
                self._send_json_response(400, {"status": "error", "error": "Missing trade object", "code": "MISSING_TRADE"})
                return

            if _trading_service:
                result = _trading_service.handle_trade_request(agent_id, trade, signature=signature)
                status = result.get("status")
                error_code = result.get("code")
                if status in ("executed", "simulated"):
                    status_code = 200
                elif status == "pending":
                    status_code = 202
                elif error_code == "WALLET_LOCKED":
                    self._send_service_unavailable(result)
                    return
                elif status in ("rejected", "failed"):
                    status_code = 400
                else:
                    status_code = get_http_status_for_error(error_code) if error_code else 200
                self._send_json_response(status_code, result)
            else:
                self._send_service_unavailable({"status": "error", "error": "Trading service not ready", "code": "SERVICE_NOT_READY"})

        elif self.path == "/callback":
            # Agent callback to report transaction status
            agent_id = request_data.get("agent_id")
            transaction_id = request_data.get("transaction_id")
            event = request_data.get("event")  # submitted | settled | failed

            if not agent_id:
                self._send_json_response(400, {"status": "error", "error": "Missing agent_id", "code": "MISSING_AGENT_ID"})
                return

            if not transaction_id:
                self._send_json_response(400, {"status": "error", "error": "Missing transaction_id", "code": "MISSING_TRANSACTION_ID"})
                return

            if not event:
                self._send_json_response(400, {"status": "error", "error": "Missing event", "code": "MISSING_EVENT"})
                return

            valid_events = ("submitted", "settled", "failed")
            if event not in valid_events:
                self._send_json_response(400, {
                    "status": "error",
                    "error": f"Invalid event: {event}. Must be one of: submitted, settled, failed",
                    "code": "INVALID_EVENT"
                })
                return

            if _signing_service:
                tx_hash = request_data.get("tx_hash")
                error = request_data.get("error")
                result = _signing_service.handle_callback(agent_id, transaction_id, event, tx_hash, error)
                if result.get("status") == "ok":
                    status_code = 200
                else:
                    status_code = get_http_status_for_error(result.get("code"))
                self._send_json_response(status_code, result)
            else:
                self._send_json_response(503, {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})

        elif self.path == "/mandate":
            # Get agent's Intent Mandate and policy summary (requires authentication)
            agent_id = request_data.get("agent_id")
            signature = request_data.get("signature")

            if not agent_id:
                self._send_json_response(400, {"status": "error", "error": "Missing agent_id", "code": "MISSING_AGENT_ID"})
                return

            if not signature:
                self._send_json_response(400, {"status": "error", "error": "Missing signature", "code": "MISSING_SIGNATURE"})
                return

            if _signing_service:
                result = _signing_service.handle_get_mandate(agent_id, signature)
                if result.get("status") == "ok":
                    status_code = 200
                else:
                    status_code = get_http_status_for_error(result.get("code"))
                self._send_json_response(status_code, result)
            else:
                self._send_json_response(503, {"status": "error", "error": "Service not ready", "code": "SERVICE_NOT_READY"})

        else:
            self._send_json_response(404, {"status": "error", "error": "Not found", "code": "NOT_FOUND"})

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a separate thread.

    This prevents concurrent connections from blocking each other.
    LOAD-02: Fixes crash under concurrent load by using ThreadingMixIn.
    """
    daemon_threads = True  # Don't block shutdown waiting for threads


class AgentServer:
    """Manages the HTTP server for agent connections."""

    def __init__(self):
        self._server: Optional[ThreadedHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port = 4663
        self._running = False

        # Callbacks (replace Qt signals)
        self._on_started: Optional[Callable[[int], None]] = None
        self._on_stopped: Optional[Callable[[], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None

    def set_callbacks(
        self,
        on_started: Callable[[int], None] = None,
        on_stopped: Callable[[], None] = None,
        on_error: Callable[[str], None] = None
    ):
        """Set callback functions for server events."""
        if on_started:
            self._on_started = on_started
        if on_stopped:
            self._on_stopped = on_stopped
        if on_error:
            self._on_error = on_error

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._running and self._server is not None

    def set_signing_service(self, signing_service: "SigningService") -> None:
        """Set the signing service for handling requests."""
        global _signing_service
        _signing_service = signing_service

    def set_trading_service(self, trading_service: "TradingService") -> None:
        """Set the trading service for handling /trade requests."""
        global _trading_service
        _trading_service = trading_service

    def start(self, port: int = 4663, allow_lan: bool = False) -> bool:
        """
        Start the HTTP server on the specified port.

        Args:
            port: Port to listen on
            allow_lan: If True, bind to 0.0.0.0 (all interfaces). If False, localhost only.
        """
        if self._running:
            return True

        self._port = port
        bind_address = "0.0.0.0" if allow_lan else "127.0.0.1"

        try:
            self._server = ThreadedHTTPServer((bind_address, port), AgentRequestHandler)
            self._thread = threading.Thread(target=self._run_server, daemon=True)
            self._thread.start()
            self._running = True
            server_stats.reset()
            server_stats.start()
            rate_limiter.reset()  # LOAD-01: Reset rate limiter on server start
            if self._on_started:
                self._on_started(port)
            return True
        except OSError as e:
            if self._on_error:
                self._on_error(f"Failed to start server: {e}")
            return False

    def stop(self):
        """Stop the HTTP server."""
        if self._server:
            self._running = False
            self._server.shutdown()
            self._server = None
            self._thread = None
            if self._on_stopped:
                self._on_stopped()

    def _run_server(self):
        """Run the server in a background thread."""
        if self._server:
            self._server.serve_forever()


# Global server instance
agent_server = AgentServer()
