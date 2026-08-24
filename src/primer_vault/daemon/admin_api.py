"""
Admin API - HTTP endpoints for GUI/Console to control the daemon.

This runs on localhost only (port 4664 by default).

Security modes:
- "gui_only" (DEFAULT): only the embedded GUI drives the API; everything else
  gets 403. This is the whole of the protection, because the Admin API has no
  authentication of its own.
- "open": any local process on this machine can use it — create agents and read
  back their tokens, commission them to a funded address, approve requests. With
  the wallet unlocked that is a complete drain path and no user interaction is
  required, so it is opt-in and stays that way.

Change it with `config set admin-api open`, `--admin-open` on a daemon, or the
Vault window under Settings > Security.
"""

import json
import logging
import os
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import TYPE_CHECKING
from urllib.parse import urlparse, parse_qs

from ..core.settings import ADMIN_API_MODE_GUI_ONLY, ADMIN_API_MODE_OPEN
from ..utils import is_browser_request, is_rebound_host

if TYPE_CHECKING:
    from ..core import Vault

logger = logging.getLogger(__name__)


# Largest request body accepted, matching the agent API. Admin payloads are
# small (a policy, an agent name), so this only bounds a caller that lies about
# how much it is about to send.
MAX_CONTENT_LENGTH = 1 * 1024 * 1024

# Ceiling on concurrently served connections. Callers are the CLI and the
# occasional local script, so this is generous and still bounds a runaway one.
MAX_WORKER_THREADS = 16

# How long a connection may go without sending anything before it is dropped.
# Without this a caller can announce a body and then never send it, holding its
# worker for as long as the process lives.
SOCKET_TIMEOUT_SECONDS = 30

# Failed wallet unlocks tolerated before the endpoint pauses, and for how long.
UNLOCK_MAX_FAILURES = 5
UNLOCK_LOCKOUT_SECONDS = 60


class _UnlockThrottle:
    """Counts consecutive failed unlocks and pauses the endpoint after a run.

    Deliberately global rather than per-IP: every caller here is on this machine,
    so an IP distinguishes nothing, and keying on one would let a caller reset
    its own budget. A successful unlock clears the count.

    This does not carry the security of the wallet - Argon2id does, at roughly
    280ms a guess. What it adds is a ceiling on unattended grinding and a line in
    the log, so an attempt to work through passwords is visible rather than
    silent.
    """

    def __init__(self):
        self._failures = 0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def blocked_for(self) -> int:
        """Seconds remaining before another attempt is accepted; 0 if allowed."""
        with self._lock:
            remaining = self._blocked_until - time.monotonic()
            return int(remaining) + 1 if remaining > 0 else 0

    def record_failure(self) -> int:
        """Count a failed attempt. Returns the consecutive failure count."""
        with self._lock:
            self._failures += 1
            if self._failures >= UNLOCK_MAX_FAILURES:
                self._blocked_until = time.monotonic() + UNLOCK_LOCKOUT_SECONDS
                self._failures = 0
            return self._failures

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._blocked_until = 0.0


_unlock_attempts = _UnlockThrottle()


class AdminRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for admin API."""

    # Reference to the Vault core (set by AdminAPIServer)
    core: "Vault" = None

    # Drop a connection that stalls rather than holding a worker on it.
    timeout = SOCKET_TIMEOUT_SECONDS

    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.debug(f"Admin API: {args[0]}")

    def _send_json(self, data: dict, status: int = 200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_error(self, message: str, code: str = "ERROR", status: int = 400):
        """Send error response."""
        self._send_json({"status": "error", "error": message, "code": code}, status)

    def _drain_request_body(self):
        """Read and discard any request body before replying with an error.

        Rejecting a request without consuming its body leaves the client still
        writing while we close the socket, which surfaces as a connection reset
        instead of the 403 we meant to send. Draining first makes the refusal
        legible to whoever asked.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _check_not_from_browser(self) -> bool:
        """Reject requests a web page told the browser to send.

        Returns True if the request should proceed, False if it was blocked.
        See utils.is_browser_request for why the Origin header decides this,
        and utils.is_rebound_host for the DNS-rebinding case, where the page
        has made itself same-origin with us and so sends no Origin at all.
        """
        if is_rebound_host(self.headers, self.server.server_address[0]):
            logger.warning(
                "Admin API rejected a request for host %r; this socket is "
                "loopback-only", self.headers.get("Host")
            )
            self._drain_request_body()
            self._send_json({
                "status": "error",
                "code": "FOREIGN_HOST_REJECTED",
                "error": "Requests for a host other than localhost are not accepted.",
            }, 403)
            return False

        origin = self.headers.get("Origin")
        if not is_browser_request(self.headers):
            return True

        logger.warning(f"Admin API rejected a browser-originated request from {origin}")
        self._drain_request_body()
        self._send_json({
            "status": "error",
            "code": "BROWSER_ORIGIN_REJECTED",
            "error": "Requests from web pages are not accepted by the Admin API.",
        }, 403)
        return False

    def _check_gui_only_access(self, path: str) -> bool:
        """Check if request is allowed given the current admin API mode.

        Returns True if the request should proceed, False if it was blocked.
        When blocked, sends a 403 response with a clear error message.

        In gui_only mode, only /status is allowed so CLI can detect a running
        daemon and understand why it can't proceed.
        """
        try:
            mode = self.core.settings_manager.get_admin_api_mode()
        except Exception as e:
            # Settings unreadable (e.g. still starting up). Assume the locked-down
            # mode rather than permitting the request: a check that cannot
            # establish whether something is allowed should answer no. /status
            # still passes below, so CLI instance detection keeps working - denying
            # that would make a second core start against the same data directory.
            logger.warning(f"Admin API mode unreadable, assuming gui_only: {e}")
            mode = ADMIN_API_MODE_GUI_ONLY

        # Only the exact "open" string opens the API. Anything else - including a
        # value this build does not recognise - is treated as locked down, so the
        # one control protecting the wallet fails closed rather than open.
        if mode == ADMIN_API_MODE_OPEN:
            return True

        # In gui_only mode, allow /status so CLI knows daemon is running
        if path == "/status":
            return True

        # Block everything else with a clear message
        self._drain_request_body()
        self._send_json({
            "status": "error",
            "code": "GUI_ONLY_MODE",
            "error": "Admin API is in GUI-only mode, so only the Vault window can "
                     "drive it. Manage Vault from that window - Settings > "
                     "Security there also lets you allow CLI access. A headless "
                     "server has no window: restart it with --admin-open.",
            "mode": "gui_only"
        }, 403)
        return False

    def _read_json_body(self) -> dict:
        """Read and parse the JSON body, refusing an implausible one.

        The declared length is checked before a single byte is read: it is the
        caller's claim, and reading first would mean allocating whatever it
        asked for. Raises ValueError, which the verb handlers already answer
        with 400.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError) as e:
            raise ValueError("Invalid Content-Length header") from e
        if content_length < 0:
            raise ValueError("Invalid Content-Length header")
        if content_length > MAX_CONTENT_LENGTH:
            raise ValueError(f"Payload too large (max {MAX_CONTENT_LENGTH} bytes)")
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode())

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_not_from_browser():
            return

        # Check gui_only mode (allows /status through for daemon detection)
        if not self._check_gui_only_access(path):
            return

        try:
            if path == "/status":
                self._handle_status()
            elif path == "/agents":
                self._handle_get_agents()
            elif path.startswith("/agents/"):
                agent_id = path.split("/")[2]
                self._handle_get_agent(agent_id)
            elif path == "/policies":
                self._handle_get_policies()
            elif path.startswith("/policies/"):
                policy_id = path.split("/")[2]
                self._handle_get_policy(policy_id)
            elif path == "/wallet/addresses":
                self._handle_get_addresses()
            elif path == "/wallet/seeds":
                self._handle_get_seeds()
            elif path.startswith("/wallet/seeds/") and path.endswith("/addresses"):
                seed_id = path.split("/")[3]
                self._handle_get_seed_addresses(seed_id)
            elif path == "/wallet/status":
                self._handle_wallet_status()
            elif path == "/transactions":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", [100])[0])
                self._handle_get_transactions(limit)
            elif path.startswith("/transactions/") and path.endswith("/receipt"):
                tx_id = path.split("/")[2]
                self._handle_get_receipt(tx_id)
            elif path == "/pending":
                self._handle_get_pending()
            elif path == "/trades/pending":
                self._handle_get_pending_trades()
            elif path == "/settings":
                self._handle_get_settings()
            else:
                self._send_error("Not found", "NOT_FOUND", 404)
        except Exception as e:
            # The detail goes to the log, not to the caller: an exception string
            # carries filesystem paths and internal structure, and the caller can
            # do nothing with it either way.
            logger.error(f"Admin API error: {e}", exc_info=True)
            self._send_error("Internal error - see the Vault log for details",
                             "INTERNAL_ERROR", 500)

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_not_from_browser():
            return

        # Check gui_only mode
        if not self._check_gui_only_access(path):
            return

        try:
            body = self._read_json_body()

            if path == "/agents":
                self._handle_create_agent(body)
            elif path.startswith("/agents/") and path.endswith("/commission"):
                agent_id = path.split("/")[2]
                self._handle_commission_agent(agent_id, body)
            elif path.startswith("/agents/") and path.endswith("/suspend"):
                agent_id = path.split("/")[2]
                self._handle_suspend_agent(agent_id)
            elif path.startswith("/agents/") and path.endswith("/activate"):
                agent_id = path.split("/")[2]
                self._handle_activate_agent(agent_id)
            elif path == "/policies":
                self._handle_create_policy(body)
            elif path == "/wallet/unlock":
                self._handle_unlock_wallet(body)
            elif path == "/wallet/lock":
                self._handle_lock_wallet()
            elif path == "/wallet/seeds":
                self._handle_create_seed(body)
            elif path == "/wallet/seeds/import":
                self._handle_import_seed(body)
            elif path.startswith("/wallet/seeds/") and path.endswith("/delete"):
                seed_id = path.split("/")[3]
                self._handle_delete_seed(seed_id, body)
            elif path == "/wallet/addresses/derive":
                self._handle_derive_address(body)
            elif path == "/wallet/addresses/import":
                self._handle_import_address(body)
            elif path.startswith("/wallet/addresses/") and path.endswith("/delete"):
                address_id = path.split("/")[3]
                self._handle_delete_address(address_id)
            elif path.startswith("/wallet/addresses/") and path.endswith("/rename"):
                address_id = path.split("/")[3]
                self._handle_rename_address(address_id, body)
            elif path.startswith("/wallet/addresses/") and path.endswith("/decommission"):
                address = path.split("/")[3]
                self._handle_decommission_for_address(address)
            elif path.startswith("/approve/"):
                request_id = path.split("/")[2]
                self._handle_approve(request_id)
            elif path.startswith("/reject/"):
                request_id = path.split("/")[2]
                reason = body.get("reason", "User rejected")
                self._handle_reject(request_id, reason)
            elif path.startswith("/trades/approve/"):
                request_id = path.split("/")[3]
                self._handle_approve_trade(request_id)
            elif path.startswith("/trades/reject/"):
                request_id = path.split("/")[3]
                reason = body.get("reason", "Rejected by user")
                self._handle_reject_trade(request_id, reason)
            elif path == "/server/start":
                self._handle_start_server(body)
            elif path == "/server/stop":
                self._handle_stop_server()
            elif path == "/history/clear":
                self._handle_clear_history()
            elif path.startswith("/transactions/") and path.endswith("/verify"):
                tx_id = path.split("/")[2]
                self._handle_verify_transaction(tx_id)
            elif path.startswith("/agents/") and path.endswith("/mandate/generate"):
                agent_id = path.split("/")[2]
                self._handle_generate_mandate(agent_id, body)
            elif path.startswith("/agents/") and path.endswith("/mandate"):
                agent_id = path.split("/")[2]
                self._handle_set_mandate(agent_id, body)
            elif path == "/mandate/upload":
                self._handle_upload_mandate(body)
            else:
                self._send_error("Not found", "NOT_FOUND", 404)
        except json.JSONDecodeError:
            self._send_error("Invalid JSON", "INVALID_JSON", 400)
        except ValueError as e:
            # A refused body (bad or oversized Content-Length) is the caller's
            # mistake, not ours - 400, not 500. Ordered after JSONDecodeError,
            # which is itself a ValueError.
            self._send_error(str(e), "BAD_REQUEST", 400)
        except Exception as e:
            # The detail goes to the log, not to the caller: an exception string
            # carries filesystem paths and internal structure, and the caller can
            # do nothing with it either way.
            logger.error(f"Admin API error: {e}", exc_info=True)
            self._send_error("Internal error - see the Vault log for details",
                             "INTERNAL_ERROR", 500)

    def do_PUT(self):
        """Handle PUT requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_not_from_browser():
            return

        # Check gui_only mode
        if not self._check_gui_only_access(path):
            return

        try:
            body = self._read_json_body()

            if path.startswith("/agents/") and len(path.split("/")) == 3:
                agent_id = path.split("/")[2]
                self._handle_update_agent(agent_id, body)
            elif path.startswith("/policies/"):
                policy_id = path.split("/")[2]
                self._handle_update_policy(policy_id, body)
            else:
                self._send_error("Not found", "NOT_FOUND", 404)
        except json.JSONDecodeError:
            self._send_error("Invalid JSON", "INVALID_JSON", 400)
        except ValueError as e:
            # A refused body (bad or oversized Content-Length) is the caller's
            # mistake, not ours - 400, not 500. Ordered after JSONDecodeError,
            # which is itself a ValueError.
            self._send_error(str(e), "BAD_REQUEST", 400)
        except Exception as e:
            # The detail goes to the log, not to the caller: an exception string
            # carries filesystem paths and internal structure, and the caller can
            # do nothing with it either way.
            logger.error(f"Admin API error: {e}", exc_info=True)
            self._send_error("Internal error - see the Vault log for details",
                             "INTERNAL_ERROR", 500)

    def do_PATCH(self):
        """Handle PATCH requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_not_from_browser():
            return

        # Check gui_only mode
        if not self._check_gui_only_access(path):
            return

        try:
            body = self._read_json_body()

            if path == "/settings":
                self._handle_patch_settings(body)
            else:
                self._send_error("Not found", "NOT_FOUND", 404)
        except json.JSONDecodeError:
            self._send_error("Invalid JSON", "INVALID_JSON", 400)
        except ValueError as e:
            # A refused body (bad or oversized Content-Length) is the caller's
            # mistake, not ours - 400, not 500. Ordered after JSONDecodeError,
            # which is itself a ValueError.
            self._send_error(str(e), "BAD_REQUEST", 400)
        except Exception as e:
            # The detail goes to the log, not to the caller: an exception string
            # carries filesystem paths and internal structure, and the caller can
            # do nothing with it either way.
            logger.error(f"Admin API error: {e}", exc_info=True)
            self._send_error("Internal error - see the Vault log for details",
                             "INTERNAL_ERROR", 500)

    def do_DELETE(self):
        """Handle DELETE requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._check_not_from_browser():
            return

        # Check gui_only mode
        if not self._check_gui_only_access(path):
            return

        try:
            if path.startswith("/agents/"):
                agent_id = path.split("/")[2]
                self._handle_delete_agent(agent_id)
            elif path.startswith("/policies/"):
                policy_id = path.split("/")[2]
                self._handle_delete_policy(policy_id)
            else:
                self._send_error("Not found", "NOT_FOUND", 404)
        except Exception as e:
            # The detail goes to the log, not to the caller: an exception string
            # carries filesystem paths and internal structure, and the caller can
            # do nothing with it either way.
            logger.error(f"Admin API error: {e}", exc_info=True)
            self._send_error("Internal error - see the Vault log for details",
                             "INTERNAL_ERROR", 500)

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def _handle_status(self):
        """Get daemon status.

        /status answers unauthenticated in the default gui_only mode so the CLI
        can detect a running instance. It must therefore not disclose anything
        an unauthenticated local caller should not watch - in particular the
        wallet lock state, which would be a "the keys are in memory now" signal.
        The CLI reads only server_running, server_port and data_dir; the
        lock/queue fields are added only in open mode, where the caller is
        already authorised to drain the wallet anyway.
        """
        running = self.core.is_server_running()
        status = {
            "status": "ok",
            "server_running": running,
            "server_port": self.core.server_port if running else None,
            "data_dir": str(self.core.data_dir),
        }
        try:
            open_mode = (self.core.settings_manager.get_admin_api_mode()
                         == ADMIN_API_MODE_OPEN)
        except Exception:
            open_mode = False
        if open_mode:
            status["wallet_unlocked"] = self.core.is_wallet_unlocked()
            status["pending_approvals"] = len(self.core.get_pending_requests())
            status["pending_trades"] = len(self.core.get_pending_trades())
        self._send_json(status)

    # -------------------------------------------------------------------------
    # Agents
    # -------------------------------------------------------------------------

    def _handle_get_agents(self):
        """Get all agents."""
        agents = self.core.get_all_agents()
        self._send_json({
            "agents": [self._agent_to_dict(a) for a in agents]
        })

    def _handle_get_agent(self, agent_id: str):
        """Get a specific agent."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return
        self._send_json(self._agent_to_dict(agent))

    def _handle_create_agent(self, body: dict):
        """Create a new agent."""
        name = body.get("name")
        if not name:
            self._send_error("Name is required", "MISSING_NAME", 400)
            return

        auth_mode = body.get("auth_mode", "hmac")
        try:
            agent, secret = self.core.create_agent(name, auth_mode)
        except ValueError as e:
            self._send_error(str(e), "WALLET_LOCKED", 409)
            return

        self._send_json({
            "agent": self._agent_to_dict(agent),
            "secret": secret  # Only returned on creation
        }, 201)

    def _handle_commission_agent(self, agent_id: str, body: dict):
        """Commission an agent."""
        # Look up agent by short ID to get internal code
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return

        policy_id = body.get("policy_id")
        wallet_address = body.get("wallet_address")
        intent_mandate = body.get("intent_mandate")

        if not all([policy_id, wallet_address]):
            self._send_error(
                "policy_id and wallet_address are required",
                "MISSING_FIELDS", 400
            )
            return

        try:
            self.core.commission_agent(agent.code, policy_id, wallet_address, intent_mandate)
            agent = self.core.get_agent_by_id(agent_id)  # Refresh after update
            self._send_json(self._agent_to_dict(agent))
        except ValueError as e:
            self._send_error(str(e), "INVALID_REQUEST", 400)

    def _handle_suspend_agent(self, agent_id: str):
        """Suspend an agent."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return

        try:
            self.core.suspend_agent(agent.code)
            agent = self.core.get_agent_by_id(agent_id)  # Refresh after update
            self._send_json(self._agent_to_dict(agent))
        except ValueError as e:
            self._send_error(str(e), "NOT_FOUND", 404)

    def _handle_activate_agent(self, agent_id: str):
        """Activate an agent."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return

        try:
            self.core.activate_agent(agent.code)
            agent = self.core.get_agent_by_id(agent_id)  # Refresh after update
            self._send_json(self._agent_to_dict(agent))
        except ValueError as e:
            self._send_error(str(e), "NOT_FOUND", 404)

    def _handle_delete_agent(self, agent_id: str):
        """Delete an agent."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return

        try:
            self.core.delete_agent(agent.code)
            self._send_json({"status": "deleted"})
        except ValueError as e:
            self._send_error(str(e), "NOT_FOUND", 404)

    def _agent_to_dict(self, agent) -> dict:
        """Convert agent to JSON-serializable dict."""
        return {
            "id": agent.id,
            "name": agent.name,
            "code": agent.code,
            "status": agent.status,
            "auth_mode": agent.auth_mode,
            "policy_id": agent.policy_id,
            "wallet_address": agent.wallet_address,
            "spent_today_micro": agent.spent_today_micro,
            "last_reset_date": agent.last_reset_date,
            "created_at": agent.created_at
        }

    # -------------------------------------------------------------------------
    # Policies
    # -------------------------------------------------------------------------

    def _handle_get_policies(self):
        """Get all policies."""
        policies = self.core.get_all_policies()
        self._send_json({
            "policies": [self._policy_to_dict(p) for p in policies]
        })

    def _handle_get_policy(self, policy_id: str):
        """Get a specific policy."""
        policy = self.core.get_policy(policy_id)
        if not policy:
            self._send_error("Policy not found", "NOT_FOUND", 404)
            return
        self._send_json(self._policy_to_dict(policy))

    def _handle_create_policy(self, body: dict):
        """Create a new policy."""
        from ..models.policy import TradingRules

        name = body.get("name")
        if not name:
            self._send_error("Name is required", "MISSING_NAME", 400)
            return

        # Parse trading_rules if provided
        trading_rules = None
        tr_data = body.get("trading_rules")
        if tr_data is not None:
            trading_rules = TradingRules.from_dict(tr_data)

        policy = self.core.create_policy(
            name=name,
            daily_limit_micro=body.get("daily_limit_micro"),
            per_request_max_micro=body.get("per_request_max_micro"),
            auto_approve_below_micro=body.get("auto_approve_below_micro"),
            allowed_domains=body.get("allowed_domains"),
            blocked_domains=body.get("blocked_domains"),
            networks=body.get("networks"),
            trading_rules=trading_rules,
            x402_enabled=body.get("x402_enabled", True)
        )
        self._send_json(self._policy_to_dict(policy), 201)

    def _handle_update_policy(self, policy_id: str, body: dict):
        """Update a policy."""
        from ..models.policy import TradingRules

        policy = self.core.get_policy(policy_id)
        if not policy:
            self._send_error("Policy not found", "NOT_FOUND", 404)
            return

        # Parse the one field that can raise (trading_rules) before applying
        # anything, so a malformed body cannot leave the policy half-edited (it
        # is the live store object the enforcement code reads).
        _UNSET = object()
        new_trading_rules = _UNSET
        if "trading_rules" in body:
            tr_data = body["trading_rules"]
            new_trading_rules = None if tr_data is None else TradingRules.from_dict(tr_data)

        if "name" in body:
            policy.name = body["name"]
        if "daily_limit_micro" in body:
            policy.daily_limit_micro = body["daily_limit_micro"]
        if "per_request_max_micro" in body:
            policy.per_request_max_micro = body["per_request_max_micro"]
        if "auto_approve_below_micro" in body:
            policy.auto_approve_below_micro = body["auto_approve_below_micro"]
        if "allowed_domains" in body:
            policy.allowed_domains = body["allowed_domains"]
        if "blocked_domains" in body:
            policy.blocked_domains = body["blocked_domains"]
        if "networks" in body:
            policy.networks = body["networks"]
        if new_trading_rules is not _UNSET:
            policy.trading_rules = new_trading_rules
        if "x402_enabled" in body:
            policy.x402_enabled = body["x402_enabled"]

        self.core.update_policy(policy)
        self._send_json(self._policy_to_dict(policy))

    def _handle_delete_policy(self, policy_id: str):
        """Delete a policy."""
        try:
            decommissioned = self.core.delete_policy(policy_id)
            self._send_json({
                "status": "deleted",
                "decommissioned_agents": decommissioned
            })
        except ValueError as e:
            self._send_error(str(e), "NOT_FOUND", 404)

    def _policy_to_dict(self, policy) -> dict:
        """Convert policy to JSON-serializable dict."""
        result = {
            "id": policy.id,
            "name": policy.name,
            "x402_enabled": policy.x402_enabled,
            "daily_limit_micro": policy.daily_limit_micro,
            "per_request_max_micro": policy.per_request_max_micro,
            "auto_approve_below_micro": policy.auto_approve_below_micro,
            "allowed_domains": policy.allowed_domains,
            "blocked_domains": policy.blocked_domains,
            "networks": policy.networks,
            "created_at": policy.created_at
        }
        if policy.trading_rules is not None:
            result["trading_rules"] = policy.trading_rules.to_dict()
        else:
            result["trading_rules"] = None
        return result

    # -------------------------------------------------------------------------
    # Wallet
    # -------------------------------------------------------------------------

    def _handle_wallet_status(self):
        """Get wallet status."""
        self._send_json({
            "unlocked": self.core.is_wallet_unlocked(),
            "addresses": self.core.get_wallet_addresses()
        })

    def _handle_get_addresses(self):
        """Get wallet addresses."""
        self._send_json({
            "addresses": self.core.get_wallet_addresses()
        })

    def _handle_unlock_wallet(self, body: dict):
        """Unlock the wallet.

        This is the one endpoint that takes a guess at a secret, so failures are
        counted and reported. Argon2id already makes each attempt cost about a
        quarter of a second, which is the real defence; the point of the backoff
        is that a process grinding away at the password cannot do so quietly.
        """
        wallet_path = body.get("wallet_path")
        password = body.get("password")

        if not wallet_path or password is None:
            self._send_error(
                "wallet_path and password are required",
                "MISSING_FIELDS", 400
            )
            return

        blocked_for = _unlock_attempts.blocked_for()
        if blocked_for:
            self._send_error(
                f"Too many failed unlock attempts. Try again in {blocked_for} seconds.",
                "TOO_MANY_ATTEMPTS", 429)
            return

        result = self.core.load_wallet(wallet_path, password)
        if result.get("success"):
            _unlock_attempts.record_success()
            self._send_json({
                "status": "unlocked",
                "addresses": result.get("addresses", [])
            })
        else:
            failures = _unlock_attempts.record_failure()
            logger.warning(
                "Admin API wallet unlock failed from %s (%d consecutive)",
                self.client_address[0], failures)
            self._send_error(result.get("error", "Unknown error"), "UNLOCK_FAILED", 401)

    def _handle_lock_wallet(self):
        """Lock the wallet."""
        self.core.lock_wallet()
        self._send_json({"status": "locked"})

    # -------------------------------------------------------------------------
    # Seeds
    # -------------------------------------------------------------------------

    def _handle_get_seeds(self):
        """List all seeds in wallet."""
        seeds = self.core.get_wallet_seeds()
        self._send_json({"seeds": seeds})

    def _handle_get_seed_addresses(self, seed_id: str):
        """List addresses derived from a seed."""
        addresses = self.core.get_wallet_addresses_for_seed(seed_id)
        self._send_json({"addresses": addresses})

    def _handle_create_seed(self, body: dict):
        """Generate a new BIP-39 seed."""
        word_count = body.get("word_count", 12)
        result = self.core.create_seed(word_count=word_count)
        if result.get("success"):
            self._send_json(result, 201)
        else:
            self._send_error(result.get("error", "Failed to create seed"), "CREATE_FAILED", 400)

    def _handle_import_seed(self, body: dict):
        """Import an existing BIP-39 seed phrase."""
        phrase = body.get("phrase")
        if not phrase:
            self._send_error("phrase is required", "MISSING_FIELDS", 400)
            return
        result = self.core.add_seed(phrase)
        if result.get("success"):
            self._send_json(result, 201)
        else:
            self._send_error(result.get("error", "Failed to import seed"), "IMPORT_FAILED", 400)

    def _handle_delete_seed(self, seed_id: str, body: dict):
        """Delete a seed (and optionally its addresses)."""
        remove_addresses = body.get("remove_addresses", True)
        result = self.core.remove_seed(seed_id, remove_addresses=remove_addresses)
        if result.get("success"):
            # Also decommission any agents using removed addresses
            decommissioned = []
            for address in result.get("removed_addresses", []):
                decommissioned.extend(self.core.decommission_agents_for_address(address))
            self._send_json({
                "success": True,
                "removed_addresses": result.get("removed_addresses", []),
                "decommissioned_agents": decommissioned,
            })
        else:
            self._send_error(result.get("error", "Failed to delete seed"), "DELETE_FAILED", 400)

    # -------------------------------------------------------------------------
    # Address management
    # -------------------------------------------------------------------------

    def _handle_derive_address(self, body: dict):
        """Derive a new address from a seed."""
        seed_id = body.get("seed_id")
        index = body.get("index")
        name = body.get("name")
        if not seed_id or index is None:
            self._send_error("seed_id and index are required", "MISSING_FIELDS", 400)
            return
        result = self.core.add_address_from_seed(seed_id, index, name)
        if result.get("success"):
            self._send_json(result, 201)
        else:
            self._send_error(result.get("error", "Failed to derive address"), "DERIVE_FAILED", 400)

    def _handle_import_address(self, body: dict):
        """Import a private key as a new address."""
        private_key = body.get("private_key")
        name = body.get("name")
        if not private_key:
            self._send_error("private_key is required", "MISSING_FIELDS", 400)
            return
        result = self.core.add_imported_key(private_key, name)
        if result.get("success"):
            self._send_json(result, 201)
        else:
            self._send_error(result.get("error", "Failed to import key"), "IMPORT_FAILED", 400)

    def _handle_delete_address(self, address_id: str):
        """Delete an address from the wallet."""
        result = self.core.remove_address(address_id)
        if result.get("success"):
            # Decommissioning is now handled inside remove_address()
            # Return 'decommissioned' to match core's return value (CLI expects this field name)
            self._send_json({
                "success": True,
                "removed_address": result.get("removed_address"),
                "decommissioned": result.get("decommissioned", []),
            })
        else:
            self._send_error(result.get("error", "Failed to delete address"), "DELETE_FAILED", 400)

    def _handle_rename_address(self, address_id: str, body: dict):
        """Rename an address."""
        name = body.get("name")
        if name is None:
            self._send_error("name is required", "MISSING_FIELDS", 400)
            return
        success = self.core.rename_address(address_id, name)
        if success:
            self._send_json({"success": True})
        else:
            self._send_error("Failed to rename address", "RENAME_FAILED", 400)

    def _handle_decommission_for_address(self, address: str):
        """Decommission all agents using a given address."""
        decommissioned = self.core.decommission_agents_for_address(address)
        self._send_json({"decommissioned_agents": decommissioned})

    # -------------------------------------------------------------------------
    # Transactions
    # -------------------------------------------------------------------------

    def _handle_get_transactions(self, limit: int):
        """Get recent transactions."""
        transactions = self.core.get_recent_transactions(limit)
        self._send_json({
            "transactions": [self._transaction_to_dict(t) for t in transactions]
        })

    def _transaction_to_dict(self, tx) -> dict:
        """Convert transaction to JSON-serializable dict."""
        # Get request_url and resource fields (Transaction model uses these names)
        request_url = getattr(tx, "request_url", None)
        resource = getattr(tx, "resource", None)
        tx_type = getattr(tx, "type", "x402")

        result = {
            "id": tx.id,
            "type": tx_type,
            "agent_id": tx.agent_id,
            "agent_name": tx.agent_name,
            "agent_code": tx.agent_code,
            "amount_micro": tx.amount_micro,
            "recipient": tx.recipient,
            "network": tx.network,
            "status": tx.status,
            "created_at": getattr(tx, "created_at", getattr(tx, "timestamp", None)),
            "timestamp": getattr(tx, "timestamp", getattr(tx, "created_at", None)),
            "request_url": request_url,
            "resource": resource,
            "resource_url": request_url or resource,  # Convenience: prefer request_url
            "signed_at": tx.signed_at,
            "tx_hash": tx.tx_hash,
            "auto_approved": tx.auto_approved,
            "reject_reason": getattr(tx, "reject_reason", None),
        }

        # Add trade-specific fields
        if tx_type == "trade":
            result.update({
                "token_in": getattr(tx, "token_in", None),
                "token_out": getattr(tx, "token_out", None),
                "symbol_in": getattr(tx, "symbol_in", None),
                "symbol_out": getattr(tx, "symbol_out", None),
                "amount_in": getattr(tx, "amount_in", None),
                "amount_out": getattr(tx, "amount_out", None),
                "fee_tier": getattr(tx, "fee_tier", None),
                "slippage_bps": getattr(tx, "slippage_bps", None),
                "pool": getattr(tx, "pool", None),
            })
        # Add transfer-specific fields
        elif tx_type == "transfer":
            result.update({
                "transfer_token": getattr(tx, "transfer_token", None),
                "transfer_symbol": getattr(tx, "transfer_symbol", None),
                "transfer_amount": getattr(tx, "transfer_amount", None),
            })

        return result

    # -------------------------------------------------------------------------
    # Approvals
    # -------------------------------------------------------------------------

    def _handle_get_pending(self):
        """Get pending approval requests."""
        pending = self.core.get_pending_requests()
        self._send_json({
            "pending": [self._request_to_dict(r) for r in pending]
        })

    def _handle_approve(self, request_id: str):
        """Approve a pending request."""
        result = self.core.approve_request(request_id)
        if result.get("status") == "success":
            self._send_json(result)
        else:
            self._send_error(
                result.get("error", "Approval failed"),
                result.get("code", "APPROVAL_FAILED"),
                400
            )

    def _handle_reject(self, request_id: str, reason: str):
        """Reject a pending request."""
        result = self.core.reject_request(request_id, reason)
        self._send_json(result)

    def _request_to_dict(self, request) -> dict:
        """Convert signing request to JSON-serializable dict."""
        return {
            "id": request.id,
            "agent_id": request.agent_id,
            "agent_name": request.agent_name,
            "amount_micro": request.amount_micro,
            "network": request.network,
            "recipient": request.recipient,
            "resource": request.resource,
            "request_url": request.request_url,
            "created_at": request.created_at,
            "status": request.status
        }

    # -------------------------------------------------------------------------
    # Trade Approvals
    # -------------------------------------------------------------------------

    def _handle_get_pending_trades(self):
        """Get pending trade requests."""
        pending = self.core.get_pending_trades()
        self._send_json({
            "pending": [self._trade_to_dict(req, quote) for req, quote in pending]
        })

    def _handle_approve_trade(self, request_id: str):
        """Approve a pending trade."""
        result = self.core.approve_trade(request_id)
        status = result.get("status")
        if status == "executed":
            self._send_json(result)
        elif status == "failed":
            self._send_error(
                result.get("reason", "Trade execution failed"),
                "TRADE_FAILED",
                400
            )
        else:
            self._send_error(
                result.get("reason", "Trade not found"),
                "NOT_FOUND",
                404
            )

    def _handle_reject_trade(self, request_id: str, reason: str):
        """Reject a pending trade."""
        result = self.core.reject_trade(request_id, reason)
        self._send_json(result)

    def _trade_to_dict(self, request, quote) -> dict:
        """Convert trade request and quote to JSON-serializable dict."""
        return {
            "id": request.id,
            "agent_id": request.agent_id,
            "token_in": request.token_in,
            "token_out": request.token_out,
            "amount_in": request.amount_in,
            "fee_tier": request.fee_tier,
            "max_slippage_bps": request.max_slippage_bps,
            "quote": {
                "amount_out_expected": quote.amount_out_expected,
                "amount_out_min": quote.amount_out_min,
                "notional_usdg": quote.notional_usdg,
                "price_impact_pct": quote.price_impact_pct,
                "pool": quote.pool,
                "gas_estimate": quote.gas_estimate,
                "symbol_in": quote.symbol_in,
                "symbol_out": quote.symbol_out,
            }
        }

    # -------------------------------------------------------------------------
    # Server
    # -------------------------------------------------------------------------

    def _handle_start_server(self, body: dict):
        """Start the agent server."""
        port = body.get("port", 4663)
        allow_lan = body.get("allow_lan", False)
        self.core.start_server(port, allow_lan)
        self._send_json({"status": "started", "port": port})

    def _handle_stop_server(self):
        """Stop the agent server."""
        self.core.stop_server()
        self._send_json({"status": "stopped"})

    # -------------------------------------------------------------------------
    # Agent update (generic field update for agent edit command)
    # -------------------------------------------------------------------------

    def _handle_update_agent(self, agent_id: str, body: dict):
        """Update agent fields (policy_id, wallet_address)."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return
        if "policy_id" in body:
            agent.policy_id = body["policy_id"]
        if "wallet_address" in body:
            agent.wallet_address = body["wallet_address"]
        try:
            self.core.update_agent(agent)
            agent = self.core.get_agent_by_id(agent_id)
            self._send_json(self._agent_to_dict(agent))
        except Exception as e:
            self._send_error(str(e), "UPDATE_FAILED", 400)

    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------

    def _handle_get_settings(self):
        """Get all settings."""
        sm = self.core.settings_manager
        settings = sm.settings
        self._send_json({
            "signing": {
                "verify_settlements": settings.signing.verify_settlements,
                "max_request_age_seconds": settings.signing.max_request_age_seconds,
                "enabled_networks": dict(settings.signing.enabled_networks),
            },
            "server": {
                "default_port": settings.server.default_port,
                "allow_lan": settings.server.allow_lan,
                "rate_limit_per_minute": settings.server.rate_limit_per_minute,
            },
            "display": {
                "default_network": settings.display.default_network,
            },
            "rpc": {
                "endpoints": dict(settings.rpc.endpoints),
            },
            "security": {
                "admin_api_mode": settings.security.admin_api_mode,
            },
        })

    def _handle_patch_settings(self, body: dict):
        """Update a single setting."""
        sm = self.core.settings_manager
        key = body.get("key")
        value = body.get("value")

        if key == "verify_settlements":
            sm.set_verify_settlements(bool(value))
        elif key == "max_request_age":
            sm.set_max_request_age(int(value))
        elif key == "network_enabled":
            sm.set_network_enabled(int(body["chain_id"]), bool(value))
        elif key == "default_port":
            sm.set_default_port(int(value))
        elif key == "allow_lan":
            sm.set_allow_lan(bool(value))
        elif key == "rate_limit":
            sm.set_rate_limit(int(value))
        elif key == "default_network":
            sm.set_default_network(int(value))
        elif key == "rpc_endpoint":
            sm.set_rpc_endpoint(int(body["chain_id"]), value)
        else:
            self._send_error(f"Unknown setting: {key}", "UNKNOWN_SETTING", 400)
            return

        self._send_json({"status": "ok"})

    # -------------------------------------------------------------------------
    # History
    # -------------------------------------------------------------------------

    def _handle_clear_history(self):
        """Clear all transaction history."""
        count = self.core.clear_transactions()
        self._send_json({"status": "cleared", "count": count})

    def _handle_get_receipt(self, tx_id: str):
        """Get AP2-formatted receipt for a transaction."""
        receipt = self.core.get_receipt(tx_id)
        self._send_json(receipt)

    def _handle_verify_transaction(self, tx_id: str):
        """Trigger on-chain verification of a transaction."""
        txs = self.core.get_recent_transactions(10000)
        tx = None
        for t in txs:
            if t.id.startswith(tx_id):
                tx = t
                break
        if not tx:
            self._send_error("Transaction not found", "NOT_FOUND", 404)
            return
        self.core.verify_transaction(tx)
        self._send_json({"status": "verification_started", "tx_id": tx.id})

    # -------------------------------------------------------------------------
    # Mandate
    # -------------------------------------------------------------------------

    def _handle_generate_mandate(self, agent_id: str, body: dict):
        """Generate (but don't store) an intent mandate for an agent."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return
        if not agent.policy_id or not agent.wallet_address:
            self._send_error("Agent is not commissioned", "NOT_COMMISSIONED", 400)
            return
        try:
            mandate = self.core.generate_intent_mandate(
                agent_code=agent.code,
                policy_id=agent.policy_id,
                wallet_address=agent.wallet_address,
                sign=body.get("sign", True)
            )
            self._send_json({"mandate": mandate})
        except Exception as e:
            self._send_error(str(e), "MANDATE_FAILED", 400)

    def _handle_set_mandate(self, agent_id: str, body: dict):
        """Store a mandate for an agent."""
        agent = self.core.get_agent_by_id(agent_id)
        if not agent:
            self._send_error("Agent not found", "NOT_FOUND", 404)
            return
        mandate = body.get("mandate")
        if not mandate:
            self._send_error("mandate is required", "MISSING_FIELDS", 400)
            return
        try:
            self.core.set_agent_mandate(agent.code, mandate)
            self._send_json({"status": "ok"})
        except Exception as e:
            self._send_error(str(e), "SET_MANDATE_FAILED", 400)

    def _handle_upload_mandate(self, body: dict):
        """Upload a mandate to the AP2 registry."""
        mandate = body.get("mandate")
        if not mandate:
            self._send_error("mandate is required", "MISSING_FIELDS", 400)
            return
        try:
            result = self.core.upload_mandate_to_registry(mandate)
            self._send_json(result)
        except Exception as e:
            self._send_error(str(e), "UPLOAD_FAILED", 400)


class ThreadedAdminServer(ThreadingMixIn, HTTPServer):
    """Admin API server, one thread per connection.

    Serving connections one at a time would mean any single caller could hold
    the API for as long as it liked - and since a caller may simply stop writing
    mid-request, "as long as it liked" has no upper bound without the socket
    timeout below. A CLI process asking a running GUI for its agent list has no
    way to recover from that, or even to explain it.

    Threads are capped rather than unbounded. Past the cap connections wait in
    the listen backlog, which a caller can cope with, instead of each claiming a
    thread. The cap is enforced with a semaphore rather than by setting
    `max_children`: that attribute belongs to ForkingMixIn; ThreadingMixIn never
    reads it, so setting it here looked like a ceiling and was not one.

    Address reuse is POSIX-only, deliberately. On POSIX, SO_REUSEADDR only
    permits rebinding past TIME_WAIT remnants after a restart - a second live
    bind still fails, which is what lets a bind failure mean something. On
    Windows the same flag means more: it lets a second socket bind a port that
    is actively in use, so two Vault processes could both "own" 4664 with
    connections split unpredictably between them, and neither would see an
    error. Windows instead gets SO_EXCLUSIVEADDRUSE, which also stops any other
    local process from binding over us with SO_REUSEADDR and stealing admin
    connections. (Windows does not need SO_REUSEADDR for restarts; asyncio
    makes the same platform split for the same reason.)
    """
    allow_reuse_address = os.name != "nt"
    daemon_threads = True  # Don't block shutdown waiting for threads

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_WORKER_THREADS)

    def process_request(self, request, client_address):
        """Claim a slot before handing the connection to a thread."""
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        """Give the slot back. Called once per handled connection, in the worker."""
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()

    def server_bind(self):
        if os.name == "nt":
            try:
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass  # non-Windows Python without the constant, or an odd stack
        super().server_bind()

    def handle_error(self, request, client_address):
        """Log a dropped connection instead of printing a traceback.

        A caller that disconnects mid-request is ordinary - a CLI interrupted
        with Ctrl-C does it every time - but socketserver's default prints a
        stack trace to stderr, which in the GUI's log looks like a crash.
        """
        logger.debug("Admin API connection from %s ended early", client_address,
                     exc_info=True)


class AdminAPIServer:
    """
    Admin API HTTP server.

    Runs on localhost only for security.
    """

    def __init__(self, core: "Vault", port: int = 4664):
        self._core = core
        self._port = port
        self._server: ThreadedAdminServer = None
        self._thread: threading.Thread = None

    def start(self):
        """Start the server in a background thread."""
        # Set core reference on handler class
        AdminRequestHandler.core = self._core

        # Bind to localhost only
        self._server = ThreadedAdminServer(("127.0.0.1", self._port), AdminRequestHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        logger.info(f"Admin API started on http://127.0.0.1:{self._port}")

    def stop(self):
        """Stop the server."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            logger.info("Admin API stopped")

    @property
    def port(self) -> int:
        return self._port
