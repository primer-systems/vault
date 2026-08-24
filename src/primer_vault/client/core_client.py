"""
CoreClient - HTTP client for the daemon admin API.

Used by a separate CLI process to reach a Vault core running in another process
(a GUI instance or the headless daemon). The GUI and its Console tab hold the
core object directly and do not go through this client.
"""

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)


@dataclass
class ClientConfig:
    """Configuration for CoreClient."""
    host: str = "127.0.0.1"
    admin_port: int = 4664
    timeout: float = 10.0
    retry_count: int = 3
    retry_delay: float = 0.5


class CoreClientError(Exception):
    """Error from CoreClient operations."""

    def __init__(self, message: str, code: str = "ERROR", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class GUIOnlyModeError(CoreClientError):
    """Raised when admin API is in GUI-only mode and rejects external access."""

    def __init__(self):
        super().__init__(
            "Vault is running in GUI-only mode, so the CLI cannot drive it. "
            "Manage it from the Vault window instead - Settings > Security there "
            "also lets you allow CLI access. A headless server has no window: "
            "restart it with --admin-open.",
            code="GUI_ONLY_MODE",
            status=403
        )


class CoreClient:
    """
    HTTP client for the daemon admin API.

    Provides a Python API that mirrors the Vault core methods.
    GUI/Console use this instead of direct core access.
    """

    def __init__(self, config: ClientConfig = None):
        self._config = config or ClientConfig()
        self._base_url = f"http://{self._config.host}:{self._config.admin_port}"
        self._event_handlers: dict[str, list[Callable]] = {}
        self._websocket_thread: Optional[threading.Thread] = None
        self._connected = False

    # -------------------------------------------------------------------------
    # Connection
    # -------------------------------------------------------------------------

    def is_daemon_running(self) -> bool:
        """Check if the daemon is running."""
        try:
            self._get("/status")
            return True
        except Exception:
            return False

    def wait_for_daemon(self, timeout: float = 30.0) -> bool:
        """
        Wait for the daemon to be ready.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if daemon is ready, False if timeout
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.is_daemon_running():
                return True
            time.sleep(0.5)
        return False

    def get_status(self) -> dict:
        """Get daemon status."""
        return self._get("/status")

    # -------------------------------------------------------------------------
    # Agents
    # -------------------------------------------------------------------------

    def get_agents(self) -> list[dict]:
        """Get all agents."""
        result = self._get("/agents")
        return result.get("agents", [])

    def get_agent(self, agent_id: str) -> Optional[dict]:
        """Get an agent by ID."""
        try:
            return self._get(f"/agents/{agent_id}")
        except CoreClientError as e:
            if e.code == "NOT_FOUND":
                return None
            raise

    # -------------------------------------------------------------------------
    # Policies
    # -------------------------------------------------------------------------

    def get_policies(self) -> list[dict]:
        """Get all policies."""
        result = self._get("/policies")
        return result.get("policies", [])

    # -------------------------------------------------------------------------
    # Wallet
    # -------------------------------------------------------------------------

    def get_wallet_status(self) -> dict:
        """Get wallet status."""
        return self._get("/wallet/status")


    def get_addresses(self) -> list[dict]:
        """Get wallet addresses."""
        result = self._get("/wallet/addresses")
        return result.get("addresses", [])

    def unlock_wallet(self, wallet_path: str, password: str) -> dict:
        """Unlock the wallet."""
        return self._post("/wallet/unlock", {
            "wallet_path": wallet_path,
            "password": password
        })

    # -------------------------------------------------------------------------
    # Transactions
    # -------------------------------------------------------------------------

    def get_transactions(self, limit: int = 100) -> list[dict]:
        """Get recent transactions."""
        result = self._get(f"/transactions?limit={limit}")
        return result.get("transactions", [])

    # -------------------------------------------------------------------------
    # Trade Approvals
    # -------------------------------------------------------------------------

    def get_pending_trades(self) -> list:
        """Get pending trade requests."""
        result = self._get("/trades/pending")
        return result.get("pending", [])

    def approve_trade(self, request_id: str) -> dict:
        """Approve a pending trade."""
        return self._post(f"/trades/approve/{request_id}", {})

    def reject_trade(self, request_id: str, reason: str = "Rejected by user") -> dict:
        """Reject a pending trade."""
        return self._post(f"/trades/reject/{request_id}", {"reason": reason})

    # -------------------------------------------------------------------------
    # Events
    # -------------------------------------------------------------------------

    def subscribe(self, event_type: str, handler: Callable[[dict], None]) -> None:
        """
        Subscribe to events from the daemon.

        Currently uses polling; GUI should use callbacks for real-time updates.
        """
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable[[dict], None]) -> None:
        """Unsubscribe from events."""
        if event_type in self._event_handlers:
            try:
                self._event_handlers[event_type].remove(handler)
            except ValueError:
                pass

    # -------------------------------------------------------------------------
    # HTTP helpers
    # -------------------------------------------------------------------------

    def _get(self, path: str) -> dict:
        """Make GET request."""
        return self._request("GET", path)

    def _post(self, path: str, data: dict) -> dict:
        """Make POST request."""
        return self._request("POST", path, data)

    def _put(self, path: str, data: dict) -> dict:
        """Make PUT request."""
        return self._request("PUT", path, data)

    def _patch(self, path: str, data: dict) -> dict:
        """Make PATCH request."""
        return self._request("PATCH", path, data)

    def _delete(self, path: str) -> dict:
        """Make DELETE request."""
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, data: dict = None) -> dict:
        """Make HTTP request to daemon."""
        url = f"{self._base_url}{path}"

        body = None
        if data is not None:
            body = json.dumps(data).encode()

        request = Request(url, data=body, method=method)
        request.add_header("Content-Type", "application/json")

        last_error = None
        for attempt in range(self._config.retry_count):
            try:
                with urlopen(request, timeout=self._config.timeout) as response:
                    return json.loads(response.read().decode())

            except HTTPError as e:
                # Parse error response
                try:
                    error_body = json.loads(e.read().decode())
                    error_code = error_body.get("code", "HTTP_ERROR")
                    # Special handling for GUI-only mode
                    if error_code == "GUI_ONLY_MODE":
                        raise GUIOnlyModeError()
                    raise CoreClientError(
                        error_body.get("error", str(e)),
                        error_code,
                        e.code
                    )
                except json.JSONDecodeError:
                    raise CoreClientError(str(e), "HTTP_ERROR", e.code) from e

            except URLError as e:
                last_error = e
                if attempt < self._config.retry_count - 1:
                    time.sleep(self._config.retry_delay)
                    continue

            except socket.timeout as e:
                last_error = e
                if attempt < self._config.retry_count - 1:
                    time.sleep(self._config.retry_delay)
                    continue

        raise CoreClientError(
            f"Failed to connect to daemon: {last_error}",
            "CONNECTION_FAILED"
        )

    # -------------------------------------------------------------------------
    # Single-instance detection
    # -------------------------------------------------------------------------

    @classmethod
    def try_connect(
        cls,
        host: str = "127.0.0.1",
        port: int = 4664,
        data_dir=None,
    ) -> Optional["CoreClient"]:
        """
        Try to connect to a running Vault instance.

        If data_dir is provided, only connects if the running instance uses the
        same data directory (prevents cross-instance contamination when multiple
        Vault installations exist on the same machine).

        Returns a CoreClient if a matching instance is found, None otherwise.
        """
        from pathlib import Path
        client = cls(ClientConfig(host=host, admin_port=port, timeout=1.0, retry_count=1, retry_delay=0.0))
        if not client.is_daemon_running():
            return None
        if data_dir is not None:
            try:
                status = client._get("/status")
                remote_dir = status.get("data_dir")
                if remote_dir and Path(remote_dir).resolve() != Path(data_dir).resolve():
                    return None  # Different installation — don't connect
            except Exception:
                return None
        return client

    # -------------------------------------------------------------------------
    # Core interface - proxy objects
    # -------------------------------------------------------------------------

    def get_all_agents(self) -> list:
        """Get all agents as AgentProxy objects."""
        return [AgentProxy(d) for d in self.get_agents()]

    def get_agent_by_id(self, agent_id: str) -> Optional["AgentProxy"]:
        """Find agent by ID (or name — falls back to all-agents search)."""
        from urllib.parse import quote
        try:
            data = self._get(f"/agents/{quote(agent_id, safe='')}")
            return AgentProxy(data)
        except CoreClientError as e:
            if e.code == "NOT_FOUND":
                # Fall back to searching all agents by name
                for d in self.get_agents():
                    if d.get("name", "").lower() == agent_id.lower():
                        return AgentProxy(d)
                return None
            raise

    def get_all_policies(self) -> list:
        """Get all policies as PolicyProxy objects."""
        return [PolicyProxy(d) for d in self.get_policies()]

    def get_policy(self, policy_id: str) -> Optional["PolicyProxy"]:
        """Get a policy by ID."""
        try:
            data = self._get(f"/policies/{policy_id}")
            return PolicyProxy(data)
        except CoreClientError as e:
            if e.code == "NOT_FOUND":
                return None
            raise

    def create_agent(self, name: str, auth_mode: str = "hmac") -> tuple:
        """Create agent. Returns (AgentProxy, secret)."""
        result = self._post("/agents", {"name": name, "auth_mode": auth_mode})
        return AgentProxy(result["agent"]), result["secret"]

    def update_agent(self, agent: "AgentProxy") -> None:
        """Update agent fields via PUT /agents/{id}."""
        self._put(f"/agents/{agent.id}", {
            "policy_id": agent.policy_id,
            "wallet_address": agent.wallet_address,
        })

    def _agent_code_to_id(self, agent_code: str) -> str:
        """Find agent id from its code (for URL routing)."""
        for d in self.get_agents():
            if d.get("code") == agent_code or d.get("id") == agent_code:
                return d["id"]
        raise CoreClientError(f"Agent not found: {agent_code}", "NOT_FOUND", 404)

    def commission_agent(self, agent_code: str, policy_id: str, wallet_address: str,
                         intent_mandate: Optional[dict] = None) -> None:
        """Commission an agent."""
        agent_id = self._agent_code_to_id(agent_code)
        payload = {"policy_id": policy_id, "wallet_address": wallet_address}
        if intent_mandate:
            payload["intent_mandate"] = intent_mandate
        self._post(f"/agents/{agent_id}/commission", payload)

    def suspend_agent(self, agent_code: str) -> None:
        """Suspend an agent."""
        agent_id = self._agent_code_to_id(agent_code)
        self._post(f"/agents/{agent_id}/suspend", {})

    def activate_agent(self, agent_code: str) -> None:
        """Activate an agent."""
        agent_id = self._agent_code_to_id(agent_code)
        self._post(f"/agents/{agent_id}/activate", {})

    def delete_agent(self, agent_code: str) -> None:
        """Delete an agent."""
        agent_id = self._agent_code_to_id(agent_code)
        self._delete(f"/agents/{agent_id}")

    def create_policy(self, name: str, daily_limit_micro: Optional[int] = None,
                      per_request_max_micro: Optional[int] = None,
                      auto_approve_below_micro: Optional[int] = None,
                      allowed_domains: Optional[list] = None,
                      blocked_domains: Optional[list] = None,
                      networks: Optional[list] = None,
                      trading_rules=None,
                      x402_enabled: Optional[bool] = None) -> "PolicyProxy":
        """Create a policy. Returns PolicyProxy."""
        data = {"name": name}
        if daily_limit_micro is not None:
            data["daily_limit_micro"] = daily_limit_micro
        if per_request_max_micro is not None:
            data["per_request_max_micro"] = per_request_max_micro
        if auto_approve_below_micro is not None:
            data["auto_approve_below_micro"] = auto_approve_below_micro
        if allowed_domains is not None:
            data["allowed_domains"] = allowed_domains
        if blocked_domains is not None:
            data["blocked_domains"] = blocked_domains
        if networks is not None:
            data["networks"] = networks
        if trading_rules is not None:
            # Convert TradingRules object to dict if needed
            if hasattr(trading_rules, 'to_dict'):
                data["trading_rules"] = trading_rules.to_dict()
            else:
                data["trading_rules"] = trading_rules
        if x402_enabled is not None:
            data["x402_enabled"] = x402_enabled
        result = self._post("/policies", data)
        return PolicyProxy(result)

    def update_policy(self, policy: "PolicyProxy") -> None:
        """Update a policy."""
        data = {
            "name": policy.name,
            "daily_limit_micro": policy.daily_limit_micro,
            "per_request_max_micro": policy.per_request_max_micro,
            "auto_approve_below_micro": policy.auto_approve_below_micro,
            "allowed_domains": policy.allowed_domains,
            "blocked_domains": policy.blocked_domains,
            "networks": policy.networks,
            "x402_enabled": policy.x402_enabled,
        }
        tr = policy.trading_rules
        if tr is not None:
            data["trading_rules"] = tr.to_dict() if hasattr(tr, 'to_dict') else tr._data
        else:
            data["trading_rules"] = None
        self._put(f"/policies/{policy.id}", data)

    def delete_policy(self, policy_id: str) -> list:
        """Delete a policy. Returns list of decommissioned agent names."""
        result = self._delete(f"/policies/{policy_id}")
        return result.get("decommissioned_agents", [])

    def get_pending_requests(self) -> list:
        """Get pending approval requests as RequestProxy objects."""
        result = self._get("/pending")
        return [RequestProxy(d) for d in result.get("pending", [])]

    def approve_request(self, request_id: str) -> dict:
        """Approve a pending request."""
        return self._post(f"/approve/{request_id}", {})

    def reject_request(self, request_id: str, reason: str = "User rejected") -> dict:
        """Reject a pending request."""
        return self._post(f"/reject/{request_id}", {"reason": reason})

    def is_server_running(self) -> bool:
        """Check if agent server is running."""
        return self.get_status().get("server_running", False)

    @property
    def server_port(self) -> int:
        """Get agent server port."""
        return self.get_status().get("server_port") or 4663

    def start_server(self, port: int = 4663, allow_lan: bool = False) -> bool:
        """Start the agent server."""
        try:
            self._post("/server/start", {"port": port, "allow_lan": allow_lan})
            return True
        except CoreClientError:
            return False

    def stop_server(self) -> None:
        """Stop the agent server."""
        self._post("/server/stop", {})

    def is_wallet_unlocked(self) -> bool:
        """Check if wallet is unlocked."""
        return self.get_wallet_status().get("unlocked", False)

    def get_wallet_addresses(self) -> list:
        """Get wallet addresses."""
        return self._get("/wallet/addresses").get("addresses", [])

    def load_wallet(self, wallet_path: str, password: str) -> dict:
        """Unlock the wallet. Returns normalized dict with 'success' key."""
        try:
            result = self._post("/wallet/unlock", {"wallet_path": wallet_path, "password": password})
            # Admin API returns {"status": "unlocked", "addresses": [...]}.
            # Normalize to the direct-core shape: {"success": True, "addresses": [...]}
            return {"success": True, "addresses": result.get("addresses", [])}
        except CoreClientError as e:
            return {"success": False, "error": str(e)}

    def lock_wallet(self) -> None:
        """Lock the wallet."""
        self._post("/wallet/lock", {})

    def get_recent_transactions(self, limit: int = 100) -> list:
        """Get recent transactions as TransactionProxy objects."""
        result = self._get(f"/transactions?limit={limit}")
        return [TransactionProxy(d) for d in result.get("transactions", [])]

    def clear_transactions(self) -> int:
        """Clear all transaction history."""
        result = self._post("/history/clear", {})
        return result.get("count", 0)

    def is_network_enabled(self, chain_id: int) -> bool:
        """Check if a network is enabled."""
        return self.settings_manager.is_network_enabled(chain_id)

    @property
    def settings_manager(self) -> "SettingsManagerProxy":
        """Settings manager proxy."""
        if not hasattr(self, "_settings_manager_proxy"):
            self._settings_manager_proxy = SettingsManagerProxy(self)
        return self._settings_manager_proxy

    def get_wallet_dir(self):
        """Get the daemon's wallet directory (derived from its data_dir)."""
        from pathlib import Path
        status = self._get("/status")
        return Path(status["data_dir"]) / "wallets"

    # -------------------------------------------------------------------------
    # Seed management (proxied to daemon)
    # -------------------------------------------------------------------------

    def get_wallet_seeds(self) -> list:
        """List all seeds in the wallet."""
        result = self._get("/wallet/seeds")
        return result.get("seeds", [])

    def get_wallet_addresses_for_seed(self, seed_id: str) -> list:
        """List addresses derived from a seed."""
        from urllib.parse import quote
        result = self._get(f"/wallet/seeds/{quote(seed_id, safe='')}/addresses")
        return result.get("addresses", [])

    def create_seed(self, word_count: int = 12) -> dict:
        """Generate a new BIP-39 seed phrase."""
        return self._post("/wallet/seeds", {"word_count": word_count})

    def add_seed(self, phrase: str) -> dict:
        """Import an existing BIP-39 seed phrase."""
        return self._post("/wallet/seeds/import", {"phrase": phrase})

    def remove_seed(self, seed_id: str, remove_addresses: bool = True) -> dict:
        """Delete a seed and optionally its derived addresses."""
        from urllib.parse import quote
        return self._post(f"/wallet/seeds/{quote(seed_id, safe='')}/delete",
                          {"remove_addresses": remove_addresses})

    # -------------------------------------------------------------------------
    # Address management (proxied to daemon)
    # -------------------------------------------------------------------------

    def add_address_from_seed(self, seed_id: str, index: int, name: str = None) -> dict:
        """Derive a new address from a seed."""
        data = {"seed_id": seed_id, "index": index}
        if name is not None:
            data["name"] = name
        return self._post("/wallet/addresses/derive", data)

    def add_imported_key(self, private_key: str, name: str = None) -> dict:
        """Import a private key as a new address."""
        data = {"private_key": private_key}
        if name is not None:
            data["name"] = name
        return self._post("/wallet/addresses/import", data)

    def remove_address(self, address_id: str) -> dict:
        """Delete an address from the wallet."""
        from urllib.parse import quote
        return self._post(f"/wallet/addresses/{quote(address_id, safe='')}/delete", {})

    def rename_address(self, address_id: str, new_name: str) -> bool:
        """Rename an address."""
        from urllib.parse import quote
        result = self._post(f"/wallet/addresses/{quote(address_id, safe='')}/rename",
                            {"name": new_name})
        return result.get("success", False)

    def decommission_agents_for_address(self, address: str) -> list:
        """Decommission all agents using a given address."""
        from urllib.parse import quote
        result = self._post(f"/wallet/addresses/{quote(address, safe='')}/decommission", {})
        return result.get("decommissioned_agents", [])

    # -------------------------------------------------------------------------
    # Not supported in connected mode — require direct core access
    # -------------------------------------------------------------------------

    def detach_wallet(self):
        raise CoreClientError(
            "Wallet file management requires running without a GUI or daemon. "
            "Stop the running instance first, then run the command standalone.",
            "REMOTE_NOT_SUPPORTED")

    def create_wallet(self, *args, **kwargs):
        raise CoreClientError(
            "Wallet creation requires running without a GUI or daemon. "
            "Stop the running instance first, then run the command standalone.",
            "REMOTE_NOT_SUPPORTED")

    def get_wallet_path(self):
        raise CoreClientError(
            "Wallet file management requires running without a GUI or daemon.",
            "REMOTE_NOT_SUPPORTED")

    def delete_wallet_file(self):
        raise CoreClientError(
            "Wallet file management requires running without a GUI or daemon. "
            "Stop the running instance first, then run the command standalone.",
            "REMOTE_NOT_SUPPORTED")

    def get_private_key_for_address(self, *args, **kwargs):
        raise CoreClientError(
            "Private key export requires running without a GUI or daemon. "
            "Stop the running instance first, then run the command standalone.",
            "REMOTE_NOT_SUPPORTED")

    def generate_intent_mandate(self, agent_code: str, policy_id: str,
                                wallet_address: str, sign: bool = True) -> dict:
        """Generate a signed intent mandate for an agent (signing done on daemon)."""
        agent_id = self._agent_code_to_id(agent_code)
        result = self._post(f"/agents/{agent_id}/mandate/generate",
                            {"policy_id": policy_id, "wallet_address": wallet_address, "sign": sign})
        return result.get("mandate", result)

    def set_agent_mandate(self, agent_code: str, mandate: dict) -> None:
        """Store a mandate for an agent."""
        agent_id = self._agent_code_to_id(agent_code)
        self._post(f"/agents/{agent_id}/mandate", {"mandate": mandate})

    def upload_mandate_to_registry(self, mandate: dict) -> dict:
        """Upload a mandate to the AP2 registry."""
        return self._post("/mandate/upload", {"mandate": mandate})

    def verify_transaction(self, tx) -> None:
        """Trigger on-chain verification of a transaction."""
        tx_id = tx.id if hasattr(tx, "id") else str(tx)
        from urllib.parse import quote
        self._post(f"/transactions/{quote(tx_id, safe='')}/verify", {})

    def get_receipt(self, tx_id: str) -> dict:
        """Get AP2-formatted receipt for a transaction."""
        from urllib.parse import quote
        return self._get(f"/transactions/{quote(tx_id, safe='')}/receipt")


# =============================================================================
# Proxy objects — wrap HTTP response dicts as attribute-accessible objects
# =============================================================================

class AgentProxy:
    """Wraps an agent dict from the admin API as a mutable attribute-accessible object."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def id(self) -> str:
        return self._data.get("id", "")

    @property
    def name(self) -> str:
        return self._data.get("name", "")

    @property
    def status(self) -> str:
        return self._data.get("status", "")

    @property
    def code(self) -> str:
        return self._data.get("code", "")

    @property
    def auth_mode(self) -> str:
        return self._data.get("auth_mode", "hmac")

    @property
    def policy_id(self) -> Optional[str]:
        return self._data.get("policy_id")

    @policy_id.setter
    def policy_id(self, value: Optional[str]):
        self._data["policy_id"] = value

    @property
    def wallet_address(self) -> Optional[str]:
        return self._data.get("wallet_address")

    @wallet_address.setter
    def wallet_address(self, value: Optional[str]):
        self._data["wallet_address"] = value

    @property
    def created_at(self) -> Optional[str]:
        return self._data.get("created_at")

    @property
    def spent_today_micro(self) -> int:
        return self._data.get("spent_today_micro", 0)


class PolicyProxy:
    """Wraps a policy dict from the admin API as a mutable attribute-accessible object."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def id(self) -> str:
        return self._data.get("id", "")

    @property
    def name(self) -> str:
        return self._data.get("name", "")

    @name.setter
    def name(self, value: str):
        self._data["name"] = value

    @property
    def daily_limit_micro(self) -> int:
        return self._data.get("daily_limit_micro", 0)

    @daily_limit_micro.setter
    def daily_limit_micro(self, value: int):
        self._data["daily_limit_micro"] = value

    @property
    def per_request_max_micro(self) -> Optional[int]:
        return self._data.get("per_request_max_micro")

    @per_request_max_micro.setter
    def per_request_max_micro(self, value: Optional[int]):
        self._data["per_request_max_micro"] = value

    @property
    def auto_approve_below_micro(self) -> Optional[int]:
        return self._data.get("auto_approve_below_micro")

    @auto_approve_below_micro.setter
    def auto_approve_below_micro(self, value: Optional[int]):
        self._data["auto_approve_below_micro"] = value

    @property
    def allowed_domains(self) -> list:
        return self._data.get("allowed_domains") or []

    @allowed_domains.setter
    def allowed_domains(self, value: list):
        self._data["allowed_domains"] = value

    @property
    def blocked_domains(self) -> list:
        return self._data.get("blocked_domains") or []

    @blocked_domains.setter
    def blocked_domains(self, value: list):
        self._data["blocked_domains"] = value

    @property
    def networks(self) -> list:
        return self._data.get("networks") or []

    @networks.setter
    def networks(self, value: list):
        self._data["networks"] = value

    @property
    def trading_rules(self):
        """Get trading rules as a TradingRulesProxy or None."""
        tr = self._data.get("trading_rules")
        if tr is None:
            return None
        return TradingRulesProxy(tr)

    @trading_rules.setter
    def trading_rules(self, value):
        """Set trading rules (accepts TradingRules, dict, TradingRulesProxy, or None)."""
        if value is None:
            self._data["trading_rules"] = None
        elif hasattr(value, 'to_dict'):
            self._data["trading_rules"] = value.to_dict()
        elif hasattr(value, '_data'):
            self._data["trading_rules"] = value._data
        else:
            self._data["trading_rules"] = value

    def is_trading_enabled(self) -> bool:
        """Check if trading is enabled for this policy."""
        tr = self._data.get("trading_rules")
        return tr is not None and tr.get("enabled", False)

    @property
    def x402_enabled(self) -> bool:
        return self._data.get("x402_enabled", True)

    @x402_enabled.setter
    def x402_enabled(self, value: bool):
        self._data["x402_enabled"] = value

    def is_x402_enabled(self) -> bool:
        """Check if x402 payments are enabled for this policy."""
        return self.x402_enabled

    @property
    def created_at(self) -> Optional[str]:
        return self._data.get("created_at")


class TradingRulesProxy:
    """Wraps a trading_rules dict as a mutable attribute-accessible object."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def enabled(self) -> bool:
        return self._data.get("enabled", False)

    @enabled.setter
    def enabled(self, value: bool):
        self._data["enabled"] = value

    @property
    def per_trade_max_usd(self) -> float:
        return self._data.get("per_trade_max_usd", 100.0)

    @per_trade_max_usd.setter
    def per_trade_max_usd(self, value: float):
        self._data["per_trade_max_usd"] = value

    @property
    def daily_volume_limit_usd(self) -> float:
        return self._data.get("daily_volume_limit_usd", 500.0)

    @daily_volume_limit_usd.setter
    def daily_volume_limit_usd(self, value: float):
        self._data["daily_volume_limit_usd"] = value

    @property
    def auto_approve_below_usd(self) -> Optional[float]:
        return self._data.get("auto_approve_below_usd")

    @auto_approve_below_usd.setter
    def auto_approve_below_usd(self, value: Optional[float]):
        self._data["auto_approve_below_usd"] = value

    @property
    def min_reserve_eth(self) -> float:
        return self._data.get("min_reserve_eth", 0.0001)

    @min_reserve_eth.setter
    def min_reserve_eth(self, value: float):
        self._data["min_reserve_eth"] = value

    @property
    def max_slippage_percent(self) -> float:
        return self._data.get("max_slippage_percent", 3.0)

    @max_slippage_percent.setter
    def max_slippage_percent(self, value: float):
        self._data["max_slippage_percent"] = value

    @property
    def max_price_impact_percent(self) -> float:
        return self._data.get("max_price_impact_percent", 5.0)

    @max_price_impact_percent.setter
    def max_price_impact_percent(self, value: float):
        self._data["max_price_impact_percent"] = value

    def to_dict(self) -> dict:
        return self._data.copy()


class RequestProxy:
    """Wraps a pending request dict from the admin API."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def id(self) -> str:
        return self._data.get("id", "")

    @property
    def agent_id(self) -> str:
        return self._data.get("agent_id", "")

    @property
    def agent_name(self) -> str:
        return self._data.get("agent_name", "")

    @property
    def amount_micro(self) -> int:
        return self._data.get("amount_micro", 0)

    @property
    def network(self) -> str:
        return self._data.get("network", "")

    @property
    def recipient(self) -> str:
        return self._data.get("recipient", "")

    @property
    def resource(self) -> Optional[str]:
        return self._data.get("resource")

    @property
    def request_url(self) -> Optional[str]:
        return self._data.get("request_url")

    @property
    def created_at(self) -> Optional[str]:
        return self._data.get("created_at")

    @property
    def status(self) -> str:
        return self._data.get("status", "")


class TransactionProxy:
    """Wraps a transaction dict from the admin API."""

    def __init__(self, data: dict):
        self._data = data

    @property
    def id(self) -> str:
        return self._data.get("id", "")

    @property
    def agent_name(self) -> Optional[str]:
        return self._data.get("agent_name")

    @property
    def amount_micro(self) -> int:
        return self._data.get("amount_micro", 0)

    @property
    def status(self) -> str:
        return self._data.get("status", "")

    @property
    def recipient(self) -> Optional[str]:
        return self._data.get("recipient")

    @property
    def network(self) -> Optional[str]:
        return self._data.get("network")

    @property
    def timestamp(self) -> Optional[str]:
        return self._data.get("timestamp") or self._data.get("created_at")

    @property
    def request_url(self) -> Optional[str]:
        return self._data.get("request_url")

    @property
    def resource(self) -> Optional[str]:
        return self._data.get("resource")

    @property
    def resource_url(self) -> Optional[str]:
        # Computed field - prefer request_url over resource
        return self._data.get("resource_url") or self._data.get("request_url") or self._data.get("resource")

    @property
    def created_at(self) -> Optional[str]:
        return self._data.get("created_at")

    @property
    def tx_hash(self) -> Optional[str]:
        return self._data.get("tx_hash")

    @property
    def wallet_address(self) -> Optional[str]:
        return self._data.get("wallet_address")


# =============================================================================
# Settings proxy — maps core.settings_manager interface to /settings API
# =============================================================================

class _SettingsData:
    """Nested settings data object, matches SettingsManager.settings structure."""

    class _Signing:
        def __init__(self, d: dict):
            self.verify_settlements = d.get("verify_settlements", True)
            self.max_request_age_seconds = d.get("max_request_age_seconds", 300)
            self.enabled_networks = d.get("enabled_networks", {})

    class _Server:
        def __init__(self, d: dict):
            self.default_port = d.get("default_port", 4663)
            self.allow_lan = d.get("allow_lan", False)
            self.rate_limit_per_minute = d.get("rate_limit_per_minute", 300)

    class _Display:
        def __init__(self, d: dict):
            self.default_network = d.get("default_network", 4663)

    class _Rpc:
        def __init__(self, d: dict):
            self.endpoints = d.get("endpoints", {})

    class _Security:
        def __init__(self, d: dict):
            self.admin_api_mode = d.get("admin_api_mode", "gui_only")

    def __init__(self, data: dict):
        self.signing = self._Signing(data.get("signing", {}))
        self.server = self._Server(data.get("server", {}))
        self.display = self._Display(data.get("display", {}))
        self.rpc = self._Rpc(data.get("rpc", {}))
        self.security = self._Security(data.get("security", {}))


class SettingsManagerProxy:
    """
    Proxy for core.settings_manager.

    Maps SettingsManager getter/setter methods to /settings HTTP endpoints.
    """

    def __init__(self, client: CoreClient):
        self._client = client

    @property
    def settings(self) -> _SettingsData:
        """Return current settings as a nested data object."""
        data = self._client._get("/settings")
        return _SettingsData(data)

    def get_verify_settlements(self) -> bool:
        return self.settings.signing.verify_settlements

    def get_max_request_age(self) -> int:
        return self.settings.signing.max_request_age_seconds

    def get_default_port(self) -> int:
        return self.settings.server.default_port

    def get_allow_lan(self) -> bool:
        return self.settings.server.allow_lan

    def get_default_network(self) -> int:
        return self.settings.display.default_network

    def is_network_enabled(self, chain_id: int) -> bool:
        networks = self.settings.signing.enabled_networks
        return networks.get(str(chain_id), True)

    def get_rpc_endpoint(self, chain_id: int) -> Optional[str]:
        return self.settings.rpc.endpoints.get(str(chain_id))

    def set_verify_settlements(self, value: bool) -> None:
        self._client._patch("/settings", {"key": "verify_settlements", "value": value})

    def set_max_request_age(self, seconds: int) -> None:
        self._client._patch("/settings", {"key": "max_request_age", "value": seconds})

    def set_network_enabled(self, chain_id: int, enabled: bool) -> None:
        self._client._patch("/settings", {"key": "network_enabled", "chain_id": chain_id, "value": enabled})

    def set_default_port(self, port: int) -> None:
        self._client._patch("/settings", {"key": "default_port", "value": port})

    def set_allow_lan(self, value: bool) -> None:
        self._client._patch("/settings", {"key": "allow_lan", "value": value})

    def set_default_network(self, chain_id: int) -> None:
        self._client._patch("/settings", {"key": "default_network", "value": chain_id})

    def set_rpc_endpoint(self, chain_id: int, url: Optional[str]) -> None:
        self._client._patch("/settings", {"key": "rpc_endpoint", "chain_id": chain_id, "value": url})

    def get_admin_api_mode(self) -> str:
        return self.settings.security.admin_api_mode

    def set_admin_api_mode(self, mode: str) -> None:
        # Deliberately not proxied. Opening the Admin API is what lets a local
        # process drive a running instance, so it must not be doable *through* a
        # running instance over that same API - that would defeat the gui_only
        # default. It is changed from the Vault window (Settings > Security), or
        # for a headless daemon by restarting with --admin-open.
        raise CoreClientError(
            "The Admin API mode cannot be changed from the CLI while Vault is "
            "running. Change it in the Vault window under Settings > Security, "
            "or restart a headless daemon with --admin-open.",
            code="GUI_ONLY_MODE",
        )
