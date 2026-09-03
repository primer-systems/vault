"""
Vault - The central coordinator for all operations.

This class owns all state and logic. The daemon runs one instance.
GUI/Console connect to it via the admin API.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

from .events import EventBus, Event, EventType
from .interfaces import ApprovalHandler, HeadlessApprovalHandler
from .settings import SettingsManager, AppSettings

if TYPE_CHECKING:
    from ..models import PolicyStore, Agent, SpendPolicy, Transaction, TradingRules
    from ..models.policy import DefiRules
    from ..wallet import VaultWallet

logger = logging.getLogger(__name__)

#: Daily spend a policy gets when the caller does not name one. Matches the
#: default the CLI documents for `policy create --day`, so the Admin API, the
#: GUI and the CLI all land on the same number.
DEFAULT_DAILY_LIMIT_MICRO = 100_000_000  # 100 USDG


class Vault:
    """
    Central coordinator for Vault.

    Owns:
    - PolicyStore (agents, policies, transactions)
    - the unlocked VaultWallet, and the path it came from
    - SigningService (payment signing)
    - TradingService (swaps)
    - AgentServer (HTTP server for agents)
    - EventBus (notifications)

    Provides a clean API for all operations.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        approval_handler: Optional[ApprovalHandler] = None
    ):
        """
        Initialize Vault.

        Args:
            data_dir: Directory for data files. Defaults to get_app_dir():
                beside the executable for a frozen build, the platform-standard
                location for a pip install.
            approval_handler: Handler for manual approvals. If None, uses HeadlessApprovalHandler.
        """
        from ..models import PolicyStore
        from ..services.defi import DefiService
        from ..services.signing import SigningService
        from ..services.trading import TradingService
        from ..services.server import AgentServer
        from ..utils import get_app_dir
        from ..instance_lock import InstanceLock

        self._data_dir = data_dir or get_app_dir()

        # One live core per data directory. Every save below writes a whole
        # file from this process's memory, so a second core on the same
        # directory would silently erase this one's records - including spend
        # against daily limits, and the wallet itself. Acquired here, before
        # anything touches a file, so every entry point is covered; raises
        # InstanceAlreadyRunning, which each composition root surfaces
        # in their own way. The OS drops the lock on process death, so a
        # crashed instance never blocks the next start.
        self._instance_lock = InstanceLock(self._data_dir)
        self._instance_lock.acquire()

        self._event_bus = EventBus()

        # Initialize settings manager with file watching. A failed save is
        # surfaced as an error activity: the change stays live in memory, so
        # the person who made it is the one who must hear it will not survive
        # a restart.
        self._settings_manager = SettingsManager(
            self._data_dir,
            on_change=self._on_settings_changed,
            on_save_error=lambda msg: self._event_bus.emit_activity(msg, True)
        )

        # Initialize stores
        self._policy_store = PolicyStore(self._data_dir)

        # Approval handler (the desktop registers its own dialog-backed one)
        self._approval_handler = approval_handler or HeadlessApprovalHandler(self)

        # Initialize signing service
        self._signing_service = SigningService()
        self._signing_service.set_stores(self._policy_store)
        self._signing_service.set_wallet_provider(self.get_wallet_for_address)
        self._signing_service.set_wallet_status_checker(self.is_wallet_unlocked)
        self._signing_service.set_rpc_resolver(self.get_rpc_url)
        self._signing_service.set_balance_provider(self.get_agent_balances)
        self._setup_signing_callbacks()

        # Apply persisted settings to signing service
        self._apply_settings_to_signing_service()

        # Initialize trading service — reuses the same stores, wallet provider,
        # and agent auth (verify_agent_signature) as the payment path.
        self._trading_service = TradingService()
        self._trading_service.set_stores(self._policy_store)
        self._trading_service.set_wallet_provider(self.get_wallet_for_address)
        self._trading_service.set_wallet_status_checker(self.is_wallet_unlocked)
        self._trading_service.set_auth_verifier(self._signing_service.verify_agent_signature)
        self._trading_service.set_rpc_resolver(self.get_rpc_url)
        self._signing_service.set_reserved_volume_provider(
            self._trading_service.reserved_volume_for)
        self._trading_service.set_callbacks(
            on_approval_needed=self._on_trade_approval_needed,
            on_activity=lambda msg, is_error=False, detail=None: self._event_bus.emit_activity(msg, is_error, detail),
            on_trade_executed=lambda address: self._event_bus.emit_trade_executed(address),
        )

        # Initialize DeFi service — the third lane, wired from the same stores,
        # wallet provider and agent auth as the other two.
        self._defi_service = DefiService()
        self._defi_service.set_stores(self._policy_store)
        self._defi_service.set_wallet_provider(self.get_wallet_for_address)
        self._defi_service.set_wallet_status_checker(self.is_wallet_unlocked)
        self._defi_service.set_auth_verifier(self._signing_service.verify_agent_signature)
        self._defi_service.set_rpc_resolver(self.get_rpc_url)
        self._defi_service.set_callbacks(
            on_approval_needed=self._on_position_approval_needed,
            on_activity=lambda msg, is_error=False, detail=None: self._event_bus.emit_activity(msg, is_error, detail),
            on_position_changed=lambda address: self._event_bus.emit_trade_executed(address),
        )

        self._signing_service.set_defi_summary_provider(
            self._defi_service.mandate_summary)

        # Only started once agents can actually reach /mandate - see
        # start_server(). A Vault built but never serving (every test that
        # exercises the core directly, without a running agent server) has no
        # reason to run a background chain-scanning thread at all.
        self._defi_venue_warmer_started = False

        # Initialize agent server
        self._agent_server = AgentServer()
        self._agent_server.set_signing_service(self._signing_service)
        self._agent_server.set_trading_service(self._trading_service)
        self._agent_server.set_defi_service(self._defi_service)

        # Server state
        self._server_running = False
        self._server_port = self._settings_manager.get_default_port()
        self._allow_lan = self._settings_manager.get_allow_lan()

        # Wallet state
        self._unlocked_wallet: Optional["VaultWallet"] = None
        self._wallet_path: Optional[str] = None

        # Restore wallet path from persisted state so standalone processes
        # (e.g. CLI after daemon killed) know which wallet was last active.
        self._restore_wallet_path()

        logger.info(f"Vault initialized with data_dir: {self._data_dir}")

    def _setup_signing_callbacks(self):
        """Wire SigningService callbacks to EventBus and approval handler."""
        def on_approval_needed(request):
            self._event_bus.emit(Event(
                type=EventType.APPROVAL_NEEDED,
                data={
                    "request_id": request.id,
                    "agent_id": request.agent_id,
                    "agent_name": request.agent_name,
                    "amount_micro": request.amount_micro,
                    "network": request.network,
                    "recipient": request.recipient,
                    "resource": request.resource,
                    "request_url": request.request_url
                }
            ))
            self._approval_handler.request_approval(request)

        def on_request_signed(agent_name, agent_id, wallet_id, amount_micro):
            self._event_bus.emit(Event(
                type=EventType.TRANSACTION_CREATED,
                data={
                    "agent_name": agent_name,
                    "agent_id": agent_id,
                    "wallet_id": wallet_id,
                    "amount_micro": amount_micro
                }
            ))

        def on_request_rejected(agent_id, reason):
            self._event_bus.emit_activity(f"Rejected: {agent_id} - {reason}", is_error=True)

        def on_activity(message, is_error):
            self._event_bus.emit_activity(message, is_error)

        def on_transaction_updated(transaction_id):
            self._event_bus.emit(Event(
                type=EventType.TRANSACTION_UPDATED,
                data={"transaction_id": transaction_id}
            ))

        self._signing_service.set_callbacks(
            on_approval_needed=on_approval_needed,
            on_request_signed=on_request_signed,
            on_request_rejected=on_request_rejected,
            on_activity=on_activity,
            on_transaction_updated=on_transaction_updated
        )

    def _on_trade_approval_needed(self, request, quote):
        """A trade is awaiting user approval. The trade sits in the trading
        service's pending set until approve_trade/reject_trade is called.

        Note: Activity message is already emitted by TradingService._activity().
        This callback triggers the GUI approval dialog.
        """
        # Trigger trade approval dialog if handler supports it
        if hasattr(self._approval_handler, 'request_trade_approval'):
            self._approval_handler.request_trade_approval(request, quote)

    def approve_trade(self, request_id: str) -> dict:
        """Approve and execute a pending trade (used by GUI/CLI)."""
        return self._trading_service.approve_trade(request_id)

    def reject_trade(self, request_id: str, reason: str = "Rejected by user") -> dict:
        """Reject a pending trade (used by GUI/CLI)."""
        return self._trading_service.reject_trade(request_id, reason)

    def get_pending_trades(self) -> list:
        """Pending (request, quote) pairs awaiting approval."""
        return self._trading_service.get_pending_trades()

    def _on_position_approval_needed(self, request, quote):
        """A lending operation is awaiting user approval.

        The request sits in the DeFi service's pending set until
        approve_position/reject_position is called. The activity message is
        already emitted by DefiService._activity(); this only raises the dialog.

        Asked of the handler rather than assumed, the way trade approval is: an
        interface that has not grown the dialog yet still runs, and the request
        waits for the terminal or expires, rather than the callback raising.
        """
        if hasattr(self._approval_handler, 'request_position_approval'):
            self._approval_handler.request_position_approval(request, quote)

    def approve_position(self, request_id: str) -> dict:
        """Approve and execute a pending lending operation (used by GUI/CLI)."""
        return self._defi_service.approve_position(request_id)

    def reject_position(self, request_id: str, reason: str = "Rejected by user") -> dict:
        """Reject a pending lending operation (used by GUI/CLI)."""
        return self._defi_service.reject_position(request_id, reason)

    def get_pending_positions(self) -> list:
        """Pending (request, quote) pairs awaiting approval."""
        return self._defi_service.get_pending_positions()

    def get_defi_venues(self, curators: list) -> list:
        """Lending venues the given curators stand behind, read from chain.

        Exposed on Vault rather than reached for through the service, so the
        policy editor in either edition can show a user what their curator list
        actually resolves to before they save it.
        """
        from ..networks import DEFAULT_NETWORK
        return self._defi_service.venues(DEFAULT_NETWORK, curators, force=True)

    def _start_defi_venue_warmer(self) -> None:
        """Keep the DeFi venue cache populated in the background.

        Without this, the venue cache is only ever filled by whichever
        request happens to arrive first with a cold cache - and resolving it
        is a real chain scan, not something a caller like /mandate should be
        made to wait on (see services/defi.py's VENUE_CACHE_SECONDS). A daemon
        thread that resolves once at startup and again on the same interval
        the cache expires means no request ever pays that cost.

        Best-effort only: an error here is logged and retried next cycle, and
        must never be allowed to affect startup or any request in flight.
        """
        from ..networks import DEFAULT_NETWORK
        from ..services.defi import VENUE_CACHE_SECONDS

        def warm_once() -> None:
            try:
                curator_sets = set()
                for policy in self._policy_store.get_all_policies():
                    rules = getattr(policy, "defi_rules", None)
                    if rules and rules.enabled and rules.morpho_curators:
                        curator_sets.add(tuple(sorted(
                            c.lower() for c in rules.morpho_curators)))
                for curators in curator_sets:
                    self._defi_service.venues(
                        DEFAULT_NETWORK, list(curators), force=True)
            except Exception:
                logger.exception("DeFi venue warm-up failed; will retry")

        def loop() -> None:
            while True:
                warm_once()
                time.sleep(VENUE_CACHE_SECONDS)

        threading.Thread(target=loop, daemon=True,
                         name="defi-venue-warmer").start()

    def _apply_settings_to_signing_service(self) -> None:
        """Apply persisted settings to the signing service."""
        settings = self._settings_manager.settings

        # Apply signing settings
        self._signing_service.set_verify_settlements(settings.signing.verify_settlements)
        self._signing_service.set_max_request_age(settings.signing.max_request_age_seconds)

        # Apply network settings
        for chain_id_str, enabled in settings.signing.enabled_networks.items():
            try:
                chain_id = int(chain_id_str)
                self._signing_service.set_network_enabled(chain_id, enabled)
            except ValueError:
                logger.warning(f"Invalid chain ID in settings: {chain_id_str}")

    def _apply_rate_limit(self) -> None:
        """Push the configured per-caller ceiling onto the live rate limiter.

        The limiter is a module-level object shared by every request handler, so
        it has to be told; it has no way to read settings itself. Applied on
        every server start and whenever the setting changes, which covers the
        GUI, the CLI and the daemon without any of them having to remember.
        """
        from ..services.server import rate_limiter
        rate_limiter.configure(self._settings_manager.get_rate_limit())

    def _on_settings_changed(self, settings: AppSettings) -> None:
        """Handle settings change from file watcher."""
        logger.info("Settings changed, applying to signing service")
        self._apply_settings_to_signing_service()

        # Update local server settings cache
        self._server_port = settings.server.default_port
        self._allow_lan = settings.server.allow_lan
        self._apply_rate_limit()

        # Emit settings changed event
        self._event_bus.emit(Event(
            type=EventType.SETTINGS_CHANGED,
            data=settings.to_dict()
        ))

    @property
    def data_dir(self) -> Path:
        """Get the data directory for this instance."""
        return self._data_dir

    def release_instance_lock(self) -> None:
        """Release this process's exclusive claim on the data directory.

        For orderly shutdowns (the daemon calls this) and for tests. Not
        required for correctness: the OS releases the lock when the process
        exits, however it exits.
        """
        self._instance_lock.release()

    @property
    def settings_manager(self) -> SettingsManager:
        """Get the settings manager for direct access."""
        return self._settings_manager

    def get_rpc_url(self, chain_id: int) -> Optional[str]:
        """The RPC endpoint to use for a chain: the user's, or the built-in one.

        Every path that talks to a chain resolves its endpoint here. The setting
        was previously read only where balances are displayed, so a custom
        endpoint moved the balance pane and left quoting, trading, sending and
        on-chain verification pointed at the default - the opposite of what
        someone running their own node is asking for, and silent about it.

        Returns None only for a chain that has no configuration at all, which
        every caller already has to handle.
        """
        from ..networks import NETWORKS

        custom = self._settings_manager.get_rpc_endpoint(chain_id)
        if custom:
            return custom
        network = NETWORKS.get(chain_id)
        return network.rpc_url if network else None

    def get_agent_balances(self, address: str) -> list:
        """Native + token balances for `address` on the default chain, using the
        user's configured RPC. Backs the agent /balances endpoint. Raises on a
        fetch failure; the endpoint turns that into a graceful reply."""
        from ..networks import MultiNetworkBalanceFetcher, DEFAULT_NETWORK

        rpc = self.get_rpc_url(DEFAULT_NETWORK)
        custom_rpcs = {DEFAULT_NETWORK: rpc} if rpc else {}
        fetcher = MultiNetworkBalanceFetcher(
            networks=[DEFAULT_NETWORK], custom_rpcs=custom_rpcs)
        return fetcher.get_all_balances(address).get(DEFAULT_NETWORK, [])

    @property
    def event_bus(self) -> EventBus:
        """Get the event bus for subscribing to events."""
        return self._event_bus

    @property
    def policy_store(self) -> "PolicyStore":
        """Get the policy store (read-only access for queries)."""
        return self._policy_store

    # -------------------------------------------------------------------------
    # Agent Operations
    # -------------------------------------------------------------------------

    def get_all_agents(self) -> list["Agent"]:
        """Get all registered agents."""
        return self._policy_store.get_all_agents()

    def get_agent_by_code(self, code: str) -> Optional["Agent"]:
        """Get an agent by internal code (UUID)."""
        return self._policy_store.get_agent_by_code(code)

    def get_agent_by_id(self, agent_id: str) -> Optional["Agent"]:
        """Get an agent by its short ID (user-facing identifier)."""
        return self._policy_store.get_agent_by_id(agent_id)

    def create_agent(self, name: str, auth_mode: str = "hmac") -> tuple["Agent", str]:
        """
        Create a new agent.

        Args:
            name: Display name for the agent
            auth_mode: Authentication mode ("hmac" or "bearer")

        Returns:
            Tuple of (agent, secret/token)
            For HMAC: secret is the shared secret to give to agent
            For Bearer: secret is the bearer token to give to agent

        Raises:
            ValueError: if no wallet is open
        """
        from ..models import Agent
        from ..models.agent import (
            generate_agent_id, generate_agent_token,
            encrypt_agent_secret, hash_bearer_token
        )

        # An agent belongs to a wallet: its credential is encrypted under that
        # wallet's master key, and it cannot be commissioned or sign without an
        # address from it. So the wallet is a precondition, enforced here rather
        # than in each interface - the GUI, the CLI and the Admin API all arrive
        # through this method.
        if not self.is_wallet_unlocked():
            raise ValueError(
                "A wallet must be open before registering an agent. "
                "Agent credentials are encrypted with the wallet's key."
            )

        # Generate the token (same format for both modes)
        agent_token, shared_secret = generate_agent_token()

        # The short id is drawn from ~17.5M values with no uniqueness
        # guarantee. Lookups return the first match, so a colliding second
        # agent could never authenticate. Regenerate until unused.
        def _unused_agent_id() -> str:
            while True:
                candidate = generate_agent_id()
                if self._policy_store.get_agent_by_id(candidate) is None:
                    return candidate

        if auth_mode == "hmac":
            # HMAC mode: encrypt the shared secret under the wallet's master key.
            # Pre-generate agent ID for AAD (binds ciphertext to this specific agent)
            agent_id = _unused_agent_id()
            encrypted, iv, tag = encrypt_agent_secret(
                shared_secret, self._unlocked_wallet.data_key, agent_id)

            agent = Agent.create(
                name=name,
                encrypted_auth_key=encrypted,
                auth_key_iv=iv,
                auth_key_tag=tag,
                auth_mode="hmac",
                agent_id=agent_id
            )
        else:
            # Bearer mode: store hash of the token (no encryption needed)
            token_hash = hash_bearer_token(agent_token)
            agent = Agent.create(
                name=name,
                encrypted_auth_key=token_hash,
                auth_mode="bearer",
                agent_id=_unused_agent_id()
            )

        self._policy_store.add_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_CREATED,
            data={"agent_id": agent.id, "agent_code": agent.code, "name": name}
        ))
        self._event_bus.emit_activity(f"Created agent: {name} ({agent.id})")

        return agent, agent_token

    def get_agent_credentials(self, agent_code: str) -> tuple[str, str | None, str]:
        """
        Get agent credentials for display.

        For HMAC mode: returns (agent_id, decrypted_secret, auth_mode) if wallet unlocked
        For Bearer mode: returns (agent_id, None, auth_mode) - token is hashed, unrecoverable

        Args:
            agent_code: The agent's internal code (UUID)

        Returns:
            Tuple of (agent_id, token_or_none, auth_mode)

        Raises:
            ValueError if agent not found
        """
        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        if agent.auth_mode == "hmac":
            if not self.is_wallet_unlocked():
                return (agent.id, None, agent.auth_mode)
            secret_hex = agent.decrypt_auth_key(self._unlocked_wallet.data_key)
            return (agent.id, f"AT_{secret_hex}", agent.auth_mode)

        return (agent.id, None, agent.auth_mode)

    def regenerate_agent_token(self, agent_code: str) -> str:
        """
        Regenerate authentication token for a Bearer mode agent.

        Creates a new token, hashes it, and updates the agent's stored auth_key.
        The old token becomes invalid immediately.

        Args:
            agent_code: The agent's internal code (UUID)

        Returns:
            The new plain token (for display to user)

        Raises:
            ValueError if agent not found or not in Bearer mode
        """
        from ..models.agent import generate_agent_token, hash_bearer_token

        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        if agent.auth_mode != "bearer":
            raise ValueError("Token regeneration only supported for Bearer mode agents")

        # Generate new token
        new_token, _ = generate_agent_token()
        token_hash = hash_bearer_token(new_token)

        # Update agent
        agent.auth_key = token_hash
        self._policy_store.update_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_UPDATED,
            data={"agent_id": agent.id, "action": "token_regenerated"}
        ))
        self._event_bus.emit_activity(f"Regenerated token for agent: {agent.name}")

        return new_token

    def commission_agent(
        self,
        agent_code: str,
        policy_id: str,
        wallet_address: str,
        intent_mandate: Optional[dict] = None
    ) -> bool:
        """
        Commission an agent with a policy and wallet.

        Args:
            agent_code: The agent's internal code (UUID) to commission
            policy_id: The policy to assign
            wallet_address: The wallet address to use for signing
            intent_mandate: Optional intent mandate to attach to agent

        Returns:
            True if successful
        """
        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        policy = self._policy_store.get_policy(policy_id)
        if not policy:
            raise ValueError(f"Policy not found: {policy_id}")

        agent.commission(policy_id, wallet_address)

        # Attach intent mandate if provided
        if intent_mandate:
            agent.intent_mandate = intent_mandate

        self._policy_store.update_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_UPDATED,
            data={"agent_id": agent.id, "status": "active"}
        ))
        self._event_bus.emit_activity(f"Commissioned agent: {agent.name}")

        return True

    def update_agent(self, agent: "Agent") -> bool:
        """
        Update an existing agent.

        Use this for general agent edits (name, policy reassignment, etc).
        For commissioning a new agent, use commission_agent() instead.

        Args:
            agent: The agent object with updated fields

        Returns:
            True if successful
        """
        existing = self._policy_store.get_agent_by_code(agent.code)
        if not existing:
            raise ValueError(f"Agent not found: {agent.code}")

        self._policy_store.update_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_UPDATED,
            data={"agent_id": agent.id, "agent_code": agent.code, "status": agent.status}
        ))
        self._event_bus.emit_activity(f"Updated agent: {agent.name}")

        return True

    def generate_intent_mandate(
        self,
        agent_code: str,
        policy_id: str,
        wallet_address: str,
        sign: bool = False
    ) -> dict:
        """
        Generate an IntentMandate document for an agent.

        The IntentMandate documents the user's authorization for an agent
        to make payments within the specified policy limits.

        Args:
            agent_code: The agent's code
            policy_id: The policy ID to document
            wallet_address: The wallet address for payments
            sign: Whether to sign the mandate (requires unlocked wallet)

        Returns:
            IntentMandate document as a dict
        """
        from ..models import generate_intent_mandate

        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        policy = self._policy_store.get_policy(policy_id)
        if not policy:
            raise ValueError(f"Policy not found: {policy_id}")

        # Get signing key if requested and wallet is unlocked
        signer_key = None
        if sign and self._unlocked_wallet:
            addr_entry = self._unlocked_wallet.get_address_by_address(wallet_address)
            if addr_entry:
                try:
                    signer_key = self._unlocked_wallet.get_private_key(addr_entry.id)
                except Exception as e:
                    logger.warning(f"Failed to get private key for signing mandate: {e}")

        return generate_intent_mandate(
            agent=agent,
            policy=policy,
            wallet_address=wallet_address,
            signer_private_key=signer_key
        )

    def set_agent_mandate(self, agent_code: str, mandate: dict) -> bool:
        """
        Set or update an agent's intent mandate.

        Args:
            agent_code: The agent's code
            mandate: The IntentMandate document to assign

        Returns:
            True if successful
        """
        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        agent.intent_mandate = mandate
        self._policy_store.update_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_UPDATED,
            data={"agent_id": agent.id, "agent_code": agent.code, "mandate_updated": True}
        ))
        self._event_bus.emit_activity(f"Updated mandate for agent: {agent.name}")

        return True

    def upload_mandate_to_registry(self, mandate: dict) -> dict:
        """
        Upload an intent mandate to the AP2 registry.

        Args:
            mandate: The intent mandate document

        Returns:
            Dict with success status, mandate_id, and viewer_url on success,
            or error message on failure
        """
        import requests

        registry_url = "https://ap2.primer.systems"

        try:
            response = requests.post(
                f"{registry_url}/api/mandates",
                json=mandate,
                headers={"Content-Type": "application/json"},
                timeout=10
            )

            if response.status_code in (200, 201):
                result = response.json()
                mandate_id = result.get("id", mandate.get("id"))
                viewer_url = f"{registry_url}/mandate.html?id={mandate_id}"

                self._event_bus.emit_activity(f"Uploaded mandate to registry: {mandate_id}")
                return {
                    "success": True,
                    "mandate_id": mandate_id,
                    "viewer_url": viewer_url
                }
            else:
                return {
                    "success": False,
                    "error": f"Registry returned status {response.status_code}"
                }

        except requests.exceptions.ConnectionError:
            return {"success": False, "error": "Could not connect to registry"}
        except requests.exceptions.Timeout:
            return {"success": False, "error": "Registry upload timed out"}
        except Exception as e:
            logger.error(f"Failed to upload mandate to registry: {e}")
            return {"success": False, "error": str(e)}

    def suspend_agent(self, agent_code: str) -> bool:
        """Suspend an agent."""
        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        agent.suspend()
        self._policy_store.update_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_UPDATED,
            data={"agent_id": agent.id, "status": "suspended"}
        ))
        self._event_bus.emit_activity(f"Suspended agent: {agent.name}")

        return True

    def activate_agent(self, agent_code: str) -> bool:
        """Reactivate a suspended agent."""
        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        agent.activate()
        self._policy_store.update_agent(agent)

        self._event_bus.emit(Event(
            type=EventType.AGENT_UPDATED,
            data={"agent_id": agent.id, "status": "active"}
        ))
        self._event_bus.emit_activity(f"Activated agent: {agent.name}")

        return True

    def delete_agent(self, agent_code: str) -> bool:
        """Delete an agent."""
        agent = self._policy_store.get_agent_by_code(agent_code)
        if not agent:
            raise ValueError(f"Agent not found: {agent_code}")

        name = agent.name
        self._policy_store.delete_agent(agent_code)

        self._event_bus.emit(Event(
            type=EventType.AGENT_DELETED,
            data={"agent_id": agent.id}
        ))
        self._event_bus.emit_activity(f"Deleted agent: {name}")

        return True

    def decommission_agents_for_address(self, address: str) -> list[str]:
        """
        Decommission all agents using a specific wallet address.

        Args:
            address: The wallet address being removed

        Returns:
            List of decommissioned agent names
        """
        decommissioned = []
        # Use case-insensitive comparison for Ethereum addresses
        # (addresses can be mixed-case due to checksum encoding)
        address_lower = address.lower() if address else ""
        for agent in self._policy_store.get_all_agents():
            agent_addr_lower = agent.wallet_address.lower() if agent.wallet_address else ""
            if agent_addr_lower and agent_addr_lower == address_lower:
                agent.wallet_address = None
                agent.policy_id = None
                agent.status = "uncommissioned"
                self._policy_store.update_agent(agent)
                decommissioned.append(agent.name)

                self._event_bus.emit(Event(
                    type=EventType.AGENT_UPDATED,
                    data={"agent_id": agent.id, "status": "uncommissioned"}
                ))

        if decommissioned:
            self._event_bus.emit_activity(
                f"Decommissioned {len(decommissioned)} agent(s) for removed address"
            )

        return decommissioned

    # -------------------------------------------------------------------------
    # Policy Operations
    # -------------------------------------------------------------------------

    def get_all_policies(self) -> list["SpendPolicy"]:
        """Get all policies."""
        return self._policy_store.get_all_policies()

    def get_policy(self, policy_id: str) -> Optional["SpendPolicy"]:
        """Get a policy by ID."""
        return self._policy_store.get_policy(policy_id)

    def create_policy(
        self,
        name: str,
        daily_limit_micro: Optional[int] = None,
        per_request_max_micro: Optional[int] = None,
        auto_approve_below_micro: Optional[int] = None,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        networks: Optional[list[int]] = None,
        trading_rules: Optional["TradingRules"] = None,
        x402_enabled: bool = True,
        defi_rules: Optional["DefiRules"] = None
    ) -> "SpendPolicy":
        """Create a new spend policy.

        A daily limit is required by the model but optional in this signature,
        because the Admin API and the GUI both reach it with whatever the caller
        supplied. Omitting it lands on the same default the CLI documents rather
        than on None, which is not a limit and which the store cannot read back.

        Raises:
            ValueError: if a supplied limit is not a non-negative integer.
        """
        from ..models import SpendPolicy
        from ..networks import DEFAULT_NETWORK

        if daily_limit_micro is None:
            daily_limit_micro = DEFAULT_DAILY_LIMIT_MICRO
        if networks is None:
            networks = [DEFAULT_NETWORK]

        policy = SpendPolicy.create(
            name=name,
            daily_limit_micro=daily_limit_micro,
            per_request_max_micro=per_request_max_micro,
            auto_approve_below_micro=auto_approve_below_micro,
            allowed_domains=allowed_domains,
            blocked_domains=blocked_domains,
            networks=networks,
            trading_rules=trading_rules,
            x402_enabled=x402_enabled,
            defi_rules=defi_rules
        )
        self._policy_store.add_policy(policy)

        self._event_bus.emit(Event(
            type=EventType.POLICY_CREATED,
            data={"policy_id": policy.id, "name": name}
        ))
        self._event_bus.emit_activity(f"Created policy: {name}")

        return policy

    def update_policy(self, policy: "SpendPolicy") -> bool:
        """Update an existing policy."""
        self._policy_store.update_policy(policy)

        self._event_bus.emit(Event(
            type=EventType.POLICY_UPDATED,
            data={"policy_id": policy.id, "name": policy.name}
        ))

        return True

    def delete_policy(self, policy_id: str) -> list[str]:
        """
        Delete a policy.

        Returns list of agent names that were decommissioned.
        """
        policy = self._policy_store.get_policy(policy_id)
        if not policy:
            raise ValueError(f"Policy not found: {policy_id}")

        # Decommission agents using this policy
        decommissioned = []
        for agent in self._policy_store.get_all_agents():
            if agent.policy_id == policy_id:
                agent.wallet_address = None
                agent.policy_id = None
                agent.status = "uncommissioned"
                self._policy_store.update_agent(agent)
                decommissioned.append(agent.name)

        self._policy_store.delete_policy(policy_id)

        self._event_bus.emit(Event(
            type=EventType.POLICY_DELETED,
            data={"policy_id": policy_id, "decommissioned": decommissioned}
        ))
        self._event_bus.emit_activity(f"Deleted policy: {policy.name}")

        return decommissioned

    # -------------------------------------------------------------------------
    # Wallet Operations
    # -------------------------------------------------------------------------

    def _get_wallet_state_file(self) -> Path:
        return self._data_dir / "wallet_path.txt"

    def _restore_wallet_path(self) -> None:
        """On startup, restore _wallet_path from disk so standalone mode can detach."""
        state_file = self._get_wallet_state_file()
        try:
            if state_file.exists():
                path_str = state_file.read_text(encoding="utf-8").strip()
                if path_str and Path(path_str).exists():
                    self._wallet_path = path_str
        except Exception:
            pass

    def _persist_wallet_path(self, path: Optional[str]) -> None:
        """Write (or clear) the active wallet path to disk."""
        state_file = self._get_wallet_state_file()
        try:
            if path:
                state_file.write_text(path, encoding="utf-8")
            elif state_file.exists():
                state_file.unlink()
        except Exception:
            pass

    def lock_wallet(self) -> bool:
        """Lock the wallet (clear from memory)."""
        if self._unlocked_wallet:
            self._unlocked_wallet.lock()
            self._unlocked_wallet = None

            self._event_bus.emit(Event(type=EventType.WALLET_LOCKED, data={}))
            self._event_bus.emit_activity("Wallet locked")

        return True

    def is_wallet_unlocked(self) -> bool:
        """Check if wallet is currently unlocked."""
        return self._unlocked_wallet is not None

    def get_wallet_path(self) -> Optional[str]:
        """Return the active wallet file path, or None if no wallet is loaded/locked."""
        return self._wallet_path

    def get_wallet_addresses(self) -> list[dict]:
        """Get list of addresses in the unlocked wallet."""
        if not self._unlocked_wallet:
            return []
        return [
            {
                "id": addr.id,
                "address": addr.address,
                "name": addr.name,
                "seed_id": addr.seed_id,
                "index": addr.index,
                "wallet_type": addr.wallet_type,
                "is_hardware": addr.is_hardware,
                "device_path": addr.device_path,
                "device_label": addr.device_label,
            }
            for addr in self._unlocked_wallet.addresses
        ]

    def get_wallet_for_address(self, address: str) -> Optional["VaultWallet"]:
        """
        Get the unlocked wallet containing this address.

        This is the wallet provider function used by SigningService.
        """
        if not self._unlocked_wallet:
            return None
        # Check if this wallet has the address
        addr_entry = self._unlocked_wallet.get_address_by_address(address)
        if addr_entry:
            return self._unlocked_wallet
        return None

    def create_wallet(
        self,
        wallet_path: str,
        password: str,
        seed_phrase: Optional[str] = None,
        private_key: Optional[str] = None,
        derivation_path: str = "m/44'/60'/0'/0/{}",
        address_indices: Optional[list[int]] = None,
        address_names: Optional[dict[int, str]] = None,
        hardware_addresses: Optional[list[tuple]] = None,
        unlock: bool = True
    ) -> dict:
        """
        Create a new wallet file.

        Args:
            wallet_path: Path where wallet file will be saved
            password: Password to encrypt the wallet
            seed_phrase: Optional seed phrase. If None and no private_key, generates new seed.
            private_key: Optional private key (hex string). If provided, imports this key instead of seed.
            derivation_path: HD derivation path template (default: Ethereum)
            address_indices: List of address indices to derive (default: [0])
            address_names: Optional dict mapping index -> custom name
            hardware_addresses: Addresses from a hardware wallet as
                (path, address, path_type, name) tuples. Produces a wallet with
                no seed and no private key - the device holds every key.
            unlock: If True, sets this as the current unlocked wallet

        Returns:
            Dict with:
                - success: bool
                - seed_phrase: The seed phrase (if seed-based)
                - addresses: List of derived addresses
                - error: Error message if failed
        """
        from pathlib import Path
        from ..wallet import VaultWallet
        from eth_account.hdaccount import generate_mnemonic

        try:
            # Create wallet
            wallet = VaultWallet.create(password)

            if hardware_addresses:
                # Hardware-only wallet: no seed, no private key, nothing to back up.
                for path_str, address, path_type, name in hardware_addresses:
                    wallet.add_hardware_address(address, path_str, path_type, name)
                result_seed = None
            elif private_key:
                # Import private key
                wallet.add_imported_key(private_key)
                result_seed = None
            else:
                # Seed-based wallet
                if seed_phrase is None:
                    seed_phrase = generate_mnemonic(num_words=12, lang="english")

                # Default to first address
                if address_indices is None:
                    address_indices = [0]

                # Add seed and derive addresses
                seed_id = wallet.add_seed(seed_phrase, derivation_path)
                for idx in address_indices:
                    # Use custom name if provided; otherwise pass None so
                    # add_address_from_seed generates the default without validating it
                    if address_names and idx in address_names:
                        name = address_names[idx].replace("S001", seed_id)
                    else:
                        name = None
                    wallet.add_address_from_seed(seed_id, idx, name)

                result_seed = seed_phrase

            # Save wallet
            path = Path(wallet_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            wallet.save(path)

            # Optionally unlock (set as current wallet)
            if unlock:
                self._unlocked_wallet = wallet
                self._wallet_path = str(path)
                self._persist_wallet_path(str(path))

                self._event_bus.emit(Event(
                    type=EventType.WALLET_UNLOCKED,
                    data={"wallet_path": str(path)}
                ))

            self._event_bus.emit_activity(f"Created wallet: {path.name}")

            return {
                "success": True,
                "seed_phrase": result_seed,
                "addresses": [
                    {"id": addr.id, "address": addr.address, "name": addr.name}
                    for addr in wallet.addresses
                ]
            }

        except Exception as e:
            # debug, not error: the failure is returned below and the caller
            # surfaces it. In CLI mode (no log handler) an error-level line would
            # also print to stderr, doubling the message the user sees.
            logger.debug(f"Failed to create wallet: {e}")
            return {"success": False, "error": str(e)}

    def get_wallet_dir(self) -> Path:
        """Get the wallet directory (uses instance data_dir, not global)."""
        wallet_dir = self._data_dir / "wallets"
        wallet_dir.mkdir(parents=True, exist_ok=True)
        return wallet_dir

    def load_wallet(self, wallet_path: str, password: str) -> dict:
        """
        Load an existing wallet file.

        Args:
            wallet_path: Path to wallet file
            password: Wallet password (pass NO_PASSWORD_SENTINEL when opening
                a legacy unencrypted file; new wallets are always encrypted)

        Returns:
            Dict with success status and wallet info
        """
        from ..wallet import CorruptedWalletFile, UnsupportedWalletVersion, VaultWallet

        try:
            wallet = VaultWallet.load(wallet_path, password)
            self._unlocked_wallet = wallet
            self._wallet_path = wallet_path
            self._persist_wallet_path(wallet_path)

            self._event_bus.emit(Event(
                type=EventType.WALLET_UNLOCKED,
                data={"wallet_path": wallet_path}
            ))
            self._event_bus.emit_activity(f"Loaded wallet: {Path(wallet_path).name}")

            return {
                "success": True,
                "addresses": [
                    {"id": addr.id, "address": addr.address, "name": addr.name}
                    for addr in wallet.addresses
                ]
            }
        except UnsupportedWalletVersion as e:
            # Distinct from a bad password: no password will ever open this file,
            # so saying "wrong password" sends the user after the wrong problem.
            # The code carries that distinction to callers, because the message
            # cannot: an interface that tells these apart by searching the text
            # for "password" finds it in the damaged-file message too, which is
            # exactly how the unlock screen went on saying "Wrong password".
            return {"success": False, "error": str(e),
                    "code": "UNSUPPORTED_WALLET_VERSION"}
        except CorruptedWalletFile as e:
            # Also distinct, for the same reason - and the harm is worse: a
            # user told "Wrong password" for a damaged file retries passwords,
            # concludes they forgot theirs, and may abandon a recoverable
            # wallet. Name the real fault and every recovery path that exists.
            previous = Path(wallet_path).with_name(Path(wallet_path).name + ".previous")
            hint = (f" A copy saved just before the last change exists at "
                    f"{previous}." if previous.exists() else "")
            return {
                "success": False,
                "code": "WALLET_DAMAGED",
                "error": (f"This wallet file is damaged and cannot be read - "
                          f"{e}. Your password may well be correct; the file "
                          f"is the problem.{hint} Restore a backup or "
                          "re-import your seed phrase.")
            }
        except ValueError:
            return {"success": False, "error": "Wrong password",
                    "code": "WRONG_PASSWORD"}
        except FileNotFoundError:
            return {"success": False, "error": "Wallet file not found",
                    "code": "WALLET_NOT_FOUND"}
        except Exception as e:
            logger.error(f"Failed to load wallet: {e}")
            return {"success": False, "error": str(e)}

    def save_wallet(self) -> bool:
        """Save the current wallet to disk."""
        if not self._unlocked_wallet or not self._wallet_path:
            return False

        try:
            path = Path(self._wallet_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._unlocked_wallet.save(path)
            return True
        except Exception as e:
            logger.error(f"Failed to save wallet: {e}")
            return False

    def _wallet_save_failed(self, change: str, still_on_disk: bool = False) -> dict:
        """The honest answer when a wallet mutation could not be written.

        Every method that changes the wallet must return this instead of
        success when save_wallet() fails: a seed the user is told was created
        but never reached disk is gone at the next start - with any funds sent
        to it - and a seed the user is told was removed but is still in the
        file comes back. The in-memory change is deliberately left in place
        (reverting live key material has its own failure modes); the message
        says exactly what state disk and session are in.
        """
        if still_on_disk:
            detail = (f"{change}, but the wallet file on disk could NOT be "
                      "rewritten, so it is STILL in the file and will load "
                      "again the next time this wallet is opened.")
        else:
            detail = (f"{change}, but the wallet file on disk could NOT be "
                      "rewritten. It exists in this session only and will be "
                      "LOST when Vault closes. Do not send funds to it.")
        msg = (f"{detail} Free disk space or close any program holding the "
               "wallet file, then retry until Vault reports success.")
        self._event_bus.emit_activity(f"Wallet save FAILED: {detail}",
                                      is_error=True)
        return {"success": False, "error": msg, "code": "WALLET_SAVE_FAILED"}

    def detach_wallet(self) -> bool:
        """
        Detach (unload) the current wallet without deleting the file.

        Works whether the wallet is unlocked or locked (but file path is set).

        Returns:
            True if a wallet was detached, False if nothing was loaded.
        """
        if not self._unlocked_wallet and not self._wallet_path:
            return False

        if self._unlocked_wallet:
            self._unlocked_wallet.lock()
            self._unlocked_wallet = None

        self._wallet_path = None
        self._persist_wallet_path(None)
        self._event_bus.emit(Event(type=EventType.WALLET_LOCKED, data={}))
        self._event_bus.emit_activity("Wallet detached")
        return True

    def delete_wallet_file(self) -> bool:
        """
        Delete the current wallet file permanently.

        Returns:
            True if successful
        """
        if not self._wallet_path:
            return False

        wallet_path = Path(self._wallet_path)

        if self._unlocked_wallet:
            self._unlocked_wallet.lock()
            self._unlocked_wallet = None

        self._wallet_path = None

        try:
            if wallet_path.exists():
                wallet_path.unlink()
            # Deleting a wallet means deleting its keys - the automatic
            # previous-version copy must not outlive them.
            previous = wallet_path.with_name(wallet_path.name + ".previous")
            if previous.exists():
                previous.unlink()
            self._event_bus.emit(Event(type=EventType.WALLET_LOCKED, data={}))
            self._event_bus.emit_activity(f"Deleted wallet: {wallet_path.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete wallet file: {e}")
            return False

    def set_wallet_path(self, path: str) -> None:
        """Set the wallet path (for persistence)."""
        self._wallet_path = path

    def get_wallet(self) -> Optional["VaultWallet"]:
        """
        Get the current unlocked wallet object.

        Note: Primarily for display operations. For modifications, use core methods.
        """
        return self._unlocked_wallet

    def get_wallet_seeds(self) -> list[dict]:
        """Get list of seeds in the unlocked wallet."""
        if not self._unlocked_wallet:
            return []
        return [
            {"id": seed.id, "derivation_path": seed.derivation_path}
            for seed in self._unlocked_wallet.seeds
        ]

    def get_wallet_addresses_for_seed(self, seed_id: str) -> list[dict]:
        """Get addresses derived from a specific seed."""
        if not self._unlocked_wallet:
            return []
        addresses = self._unlocked_wallet.get_addresses_for_seed(seed_id)
        return [
            {"id": addr.id, "address": addr.address, "name": addr.name, "index": addr.index}
            for addr in addresses
        ]

    def add_seed(
        self,
        seed_phrase: str,
        derivation_path: str = "m/44'/60'/0'/0/{}"
    ) -> dict:
        """
        Add a seed phrase to the wallet.

        Args:
            seed_phrase: BIP-39 mnemonic
            derivation_path: HD derivation path template

        Returns:
            Dict with seed_id or error
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        try:
            seed_id = self._unlocked_wallet.add_seed(seed_phrase, derivation_path)
            if not self.save_wallet():
                return self._wallet_save_failed(f"Seed {seed_id} was added")

            self._event_bus.emit_activity(f"Added seed: {seed_id}")
            return {"success": True, "seed_id": seed_id}
        except Exception as e:
            logger.error(f"Failed to add seed: {e}")
            return {"success": False, "error": str(e)}

    def create_seed(
        self,
        word_count: int = 12,
        derivation_path: str = "m/44'/60'/0'/0/{}"
    ) -> dict:
        """
        Generate a new BIP-39 seed and add it to the wallet.

        Args:
            word_count: Number of words (12 or 24)
            derivation_path: HD derivation path template

        Returns:
            Dict with seed_id and seed_phrase, or error
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        if word_count not in (12, 24):
            return {"success": False, "error": "word_count must be 12 or 24"}

        try:
            from eth_account.hdaccount import generate_mnemonic

            seed_phrase = generate_mnemonic(num_words=word_count, lang="english")
            seed_id = self._unlocked_wallet.add_seed(seed_phrase, derivation_path)
            if not self.save_wallet():
                return self._wallet_save_failed(f"Seed {seed_id} was generated")

            self._event_bus.emit_activity(f"Created new seed: {seed_id}")
            return {"success": True, "seed_id": seed_id, "seed_phrase": seed_phrase}
        except Exception as e:
            logger.error(f"Failed to create seed: {e}")
            return {"success": False, "error": str(e)}

    def remove_seed(self, seed_id: str, remove_addresses: bool = True) -> dict:
        """
        Remove a seed and optionally its derived addresses.

        Args:
            seed_id: The seed ID to remove
            remove_addresses: If True, also remove all addresses from this seed

        Returns:
            Dict with removed address list
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        try:
            # Get addresses before removal (for agent cleanup)
            removed_addresses = []
            if remove_addresses:
                for addr in self._unlocked_wallet.get_addresses_for_seed(seed_id):
                    removed_addresses.append(addr.address)

            self._unlocked_wallet.remove_seed(seed_id, remove_addresses)
            if not self.save_wallet():
                return self._wallet_save_failed(
                    f"Seed {seed_id} was removed from this session",
                    still_on_disk=True)

            self._event_bus.emit_activity(f"Removed seed: {seed_id}")
            return {"success": True, "removed_addresses": removed_addresses}
        except Exception as e:
            logger.error(f"Failed to remove seed: {e}")
            return {"success": False, "error": str(e)}

    def add_address_from_seed(
        self,
        seed_id: str,
        index: int,
        name: Optional[str] = None
    ) -> dict:
        """
        Derive a new address from an existing seed.

        Args:
            seed_id: The seed to derive from
            index: Derivation index
            name: Optional display name

        Returns:
            Dict with address info
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        try:
            addr_id = self._unlocked_wallet.add_address_from_seed(seed_id, index, name)
            if not self.save_wallet():
                return self._wallet_save_failed(
                    f"An address was derived from {seed_id}")

            # Get the new address info
            addr = None
            for a in self._unlocked_wallet.addresses:
                if a.id == addr_id:
                    addr = a
                    break

            self._event_bus.emit_activity(f"Added address from {seed_id} #{index}")
            return {
                "success": True,
                "address_id": addr_id,
                "address": addr.address if addr else None,
                "name": addr.name if addr else None
            }
        except Exception as e:
            logger.error(f"Failed to add address: {e}")
            return {"success": False, "error": str(e)}

    def add_imported_key(self, private_key: str, name: Optional[str] = None) -> dict:
        """
        Import a private key directly.

        Args:
            private_key: Hex-encoded private key
            name: Optional display name

        Returns:
            Dict with address info
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        try:
            addr_id = self._unlocked_wallet.add_imported_key(private_key, name)
            if not self.save_wallet():
                return self._wallet_save_failed("The private key was imported")

            # Get the new address info
            addr = None
            for a in self._unlocked_wallet.addresses:
                if a.id == addr_id:
                    addr = a
                    break

            self._event_bus.emit_activity(f"Imported private key: {addr.address[:10]}..." if addr else "Imported key")
            return {
                "success": True,
                "address_id": addr_id,
                "address": addr.address if addr else None,
                "name": addr.name if addr else None
            }
        except Exception as e:
            logger.error(f"Failed to import key: {e}")
            return {"success": False, "error": str(e)}

    def add_hardware_address(
        self,
        address: str,
        path: str,
        path_type: str,
        name: Optional[str] = None
    ) -> dict:
        """
        Add a Ledger hardware wallet address.

        No private key is stored - the Ledger device holds the keys.

        Args:
            address: The 0x address from the Ledger
            path: Full derivation path (e.g., "m/44'/60'/0'/0/0")
            path_type: Path type ("ledger_live", "bip44", "legacy_mew", "custom")
            name: Optional display name

        Returns:
            Dict with address info
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        try:
            addr_id = self._unlocked_wallet.add_hardware_address(address, path, path_type, name)
            if not self.save_wallet():
                return self._wallet_save_failed("The Ledger address was added")

            # Get the new address info
            addr = None
            for a in self._unlocked_wallet.addresses:
                if a.id == addr_id:
                    addr = a
                    break

            self._event_bus.emit_activity(f"Added Ledger address: {addr.address[:10]}..." if addr else "Added Ledger address")
            return {
                "success": True,
                "address_id": addr_id,
                "address": addr.address if addr else None,
                "name": addr.name if addr else None
            }
        except Exception as e:
            logger.error(f"Failed to add Ledger address: {e}")
            return {"success": False, "error": str(e)}

    def remove_address(self, address_id: str) -> dict:
        """
        Remove an address from the wallet.

        Also decommissions any agents using this address (consistent with delete_policy behavior).

        Args:
            address_id: The address ID to remove

        Returns:
            Dict with the removed 0x address and list of decommissioned agent names
        """
        if not self._unlocked_wallet:
            return {"success": False, "error": "No wallet unlocked"}

        try:
            # Find the address first
            addr = None
            for a in self._unlocked_wallet.addresses:
                if a.id == address_id:
                    addr = a
                    break

            if not addr:
                return {"success": False, "error": "Address not found"}

            removed_address = addr.address

            # Decommission agents using this address BEFORE removing
            # (consistent with delete_policy behavior)
            decommissioned = self.decommission_agents_for_address(removed_address)

            self._unlocked_wallet.remove_address(address_id)
            if not self.save_wallet():
                return self._wallet_save_failed(
                    f"Address {removed_address[:10]}... was removed from this "
                    "session", still_on_disk=True)

            self._event_bus.emit_activity(f"Removed address: {removed_address[:10]}...")
            return {
                "success": True,
                "removed_address": removed_address,
                "decommissioned": decommissioned
            }
        except Exception as e:
            logger.error(f"Failed to remove address: {e}")
            return {"success": False, "error": str(e)}

    def rename_address(self, address_id: str, new_name: str) -> bool:
        """Rename an address."""
        if not self._unlocked_wallet:
            return False

        try:
            self._unlocked_wallet.rename_address(address_id, new_name)
            self.save_wallet()
            return True
        except Exception:
            return False

    def get_private_key_for_address(self, address_id: str) -> Optional[str]:
        """
        Get the private key for an address.

        Note: Used for user-initiated operations like withdrawals.
        The key should never be logged or transmitted.

        Args:
            address_id: The address ID

        Returns:
            Hex-encoded private key or None
        """
        if not self._unlocked_wallet:
            return None

        try:
            return self._unlocked_wallet.get_private_key(address_id)
        except Exception:
            return None

    def is_wallet_empty(self) -> bool:
        """Check if wallet has no addresses and no seeds."""
        if not self._unlocked_wallet:
            return True
        return len(self._unlocked_wallet.addresses) == 0 and len(self._unlocked_wallet.seeds) == 0

    # -------------------------------------------------------------------------
    # Approval Operations
    # -------------------------------------------------------------------------

    def set_approval_handler(self, handler: ApprovalHandler) -> None:
        """Set the approval handler (GUI provides its own)."""
        self._approval_handler = handler

    def set_hardware_sign_handler(self, handler: "Callable[[dict, str, str], str]") -> None:
        """Set the Ledger signing handler (GUI provides this for hardware wallet signing).

        The handler is called when a payment needs to be signed with a Ledger device.

        Args:
            handler: Callable(typed_data, device_path, expected_address) -> signature_hex
                - typed_data: EIP-712 typed data dict
                - device_path: Derivation path (e.g., "m/44'/60'/0'/0/0")
                - expected_address: Address to verify against
                Returns: Signature hex string
                Raises: Exception on error or cancellation
        """
        if self._signing_service:
            self._signing_service.set_callbacks(on_hardware_sign_needed=handler)

    def set_hardware_tx_sign_handler(self, handler: "Callable[[dict, str, str, str], str]") -> None:
        """Set the Ledger transaction signing handler (GUI provides this).

        Used by the trading path, which signs whole Ethereum transactions on the
        device rather than EIP-712 payment authorizations. A single trade may
        invoke this twice: once for the ERC-20 approval, once for the swap.

        Args:
            handler: Callable(tx_dict, device_path, expected_address, description)
                -> raw signed transaction hex
                - tx_dict: Unsigned transaction dict (to/value/gas/nonce/chainId/data)
                - device_path: Derivation path (e.g., "m/44'/60'/0'/0/0")
                - expected_address: Address to verify against before signing
                - description: Human-readable summary shown in the dialog
                Returns: Raw signed transaction hex, ready to broadcast
                Raises: Exception on error or cancellation
        """
        if self._trading_service:
            self._trading_service.set_hardware_tx_signer(handler)
        if self._defi_service:
            self._defi_service.set_hardware_tx_signer(handler)

    def approve_request(self, request_id: str) -> dict:
        """Approve a pending signing request."""
        if not self._signing_service:
            return {"status": "error", "error": "Signing service not initialized"}

        result = self._signing_service.approve_request(request_id)

        if result.get("status") == "success":
            self._event_bus.emit(Event(
                type=EventType.APPROVAL_RESOLVED,
                data={"request_id": request_id, "approved": True}
            ))
            self._approval_handler.on_approval_resolved(request_id, True)

        return result

    def reject_request(self, request_id: str, reason: str = "User rejected") -> dict:
        """Reject a pending signing request."""
        if not self._signing_service:
            return {"status": "error", "error": "Signing service not initialized"}

        result = self._signing_service.reject_request(request_id, reason)

        self._event_bus.emit(Event(
            type=EventType.APPROVAL_RESOLVED,
            data={"request_id": request_id, "approved": False, "reason": reason}
        ))
        self._approval_handler.on_approval_resolved(request_id, False, reason)

        return result

    def get_pending_requests(self) -> list:
        """Get all pending approval requests."""
        if not self._signing_service:
            return []
        return self._signing_service.get_pending_requests()

    # -------------------------------------------------------------------------
    # Signing Service Configuration
    # -------------------------------------------------------------------------

    def set_verify_settlements(self, enabled: bool) -> None:
        """
        Enable or disable settlement verification.

        When enabled, the signing service will verify transactions on-chain
        after they are submitted. Setting is persisted to disk.
        """
        # Persist to settings file (this also triggers signing service update via callback)
        self._settings_manager.set_verify_settlements(enabled)

    def get_verify_settlements(self) -> bool:
        """Get settlement verification setting."""
        return self._settings_manager.get_verify_settlements()

    def set_max_request_age(self, seconds: int) -> None:
        """
        Set the maximum age for signing requests (replay window).

        Requests older than this will be rejected to prevent replay attacks.
        Setting is persisted to disk.
        """
        # Persist to settings file (this also triggers signing service update via callback)
        self._settings_manager.set_max_request_age(seconds)

    def get_max_request_age(self) -> int:
        """Get the maximum request age in seconds."""
        return self._settings_manager.get_max_request_age()

    def verify_transaction(self, transaction: "Transaction") -> None:
        """
        Manually verify a transaction on-chain.

        Starts an async verification process. The transaction's verification_status
        will be updated and a TRANSACTION_UPDATED event emitted when complete.
        """
        if self._signing_service:
            self._signing_service.verify_transaction(transaction)

    def get_receipt(self, tx_id: str) -> dict:
        """
        Get the AP2-formatted receipt for a transaction.

        Returns a dict with receipt data or error information.
        """
        if not self._signing_service:
            return {"error": "Signing service not initialized"}
        return self._signing_service.get_receipt(tx_id)

    # -------------------------------------------------------------------------
    # Transaction Operations
    # -------------------------------------------------------------------------

    def get_recent_transactions(self, limit: int = 100) -> list["Transaction"]:
        """Get recent transactions."""
        return self._policy_store.get_recent_transactions(limit)

    def get_transaction(self, tx_id: str) -> Optional["Transaction"]:
        """Get a transaction by ID."""
        return self._policy_store.get_transaction(tx_id)

    def clear_transactions(self) -> int:
        """Clear all transactions. Returns count cleared."""
        return self._policy_store.clear_transactions()

    # -------------------------------------------------------------------------
    # Server Operations
    # -------------------------------------------------------------------------

    def start_server(self, port: int = 4663, allow_lan: bool = False) -> bool:
        """Start the agent HTTP server."""
        if self._agent_server.is_running:
            return True

        self._apply_rate_limit()
        success = self._agent_server.start(port, allow_lan)
        if success:
            self._server_running = True
            self._server_port = port
            self._allow_lan = allow_lan
            if not self._defi_venue_warmer_started:
                self._defi_venue_warmer_started = True
                self._start_defi_venue_warmer()
            self._event_bus.emit(Event(
                type=EventType.SERVER_STARTED,
                data={"port": port, "allow_lan": allow_lan}
            ))
            self._event_bus.emit_activity(f"Server started on port {port}")
        return success

    def stop_server(self) -> bool:
        """Stop the agent HTTP server."""
        if not self._agent_server.is_running:
            return True

        self._agent_server.stop()
        self._server_running = False
        self._event_bus.emit(Event(type=EventType.SERVER_STOPPED, data={}))
        self._event_bus.emit_activity("Server stopped")
        return True

    def is_server_running(self) -> bool:
        """Check if server is running."""
        return self._agent_server.is_running

    @property
    def server_port(self) -> int:
        """Get the current server port."""
        return self._agent_server.port

    def set_server_callbacks(
        self,
        on_started: Optional[Callable[[int], None]] = None,
        on_stopped: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None
    ) -> None:
        """
        Set server event callbacks.

        Args:
            on_started: Called when server starts, receives port number
            on_stopped: Called when server stops
            on_error: Called on server error, receives error message
        """
        self._agent_server.set_callbacks(
            on_started=on_started,
            on_stopped=on_stopped,
            on_error=on_error
        )

    # -------------------------------------------------------------------------
    # Network Settings
    # -------------------------------------------------------------------------

    def set_network_enabled(self, chain_id: int, enabled: bool) -> None:
        """Enable or disable a network globally. Setting is persisted to disk."""
        # Persist to settings file (this also triggers signing service update via callback)
        self._settings_manager.set_network_enabled(chain_id, enabled)

    def is_network_enabled(self, chain_id: int) -> bool:
        """Check if a network is enabled."""
        return self._settings_manager.is_network_enabled(chain_id)

    def get_enabled_networks(self) -> dict[int, bool]:
        """Get all network enabled states."""
        return self._settings_manager.get_enabled_networks()
