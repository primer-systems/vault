"""
Trading service — orchestrates an agent trade request end to end.

Mirrors SigningService (the x402 payment path): it resolves the agent, re-quotes
the trade independently, values it in USDG notional, and then either escalates to
a human approval or (once the policy engine is enabled) auto-executes under the
threshold. Qt-free, so GUI, CLI, and headless all share it.

Current phase: quote + valuation + manual-approval gate are complete and tested.
Execution (simulate → sign → submit) is implemented and gated behind approval;
exercising it end to end needs a funded, unlocked wallet. The policy validator is
a deliberate stub here (returns "manual approval") until the policy phase lands.
"""

import logging
from typing import Callable, Optional, TYPE_CHECKING

from ..models.trade import TradeRequest, TradeQuote, TradeResult
from ..models.transaction import Transaction, TYPE_TRADE, STATUS_RECEIVED, STATUS_SETTLED, STATUS_FAILED, STATUS_REJECTED
from ..networks import NETWORKS, get_dex, get_dex_v4, TOKENS, is_native_eth, is_wrap_trade, is_unwrap_trade
from .dex import DexAdapter, DexAdapterV3, DexError, to_atomic, from_atomic
from .dex_v4 import DexAdapterV4
from . import pricing

if TYPE_CHECKING:
    from ..models.agent import Agent

logger = logging.getLogger(__name__)


class TradingService:
    """Handles trade requests from agents: quote, value, gate, execute."""

    def __init__(self):
        self._policy_store = None
        self._wallet_provider: Optional[Callable] = None
        self._wallet_status_checker: Optional[Callable] = None
        self._auth_verifier: Optional[Callable] = None
        self._on_approval_needed: Optional[Callable] = None
        self._on_activity: Optional[Callable] = None
        self._on_trade_executed: Optional[Callable] = None
        self._pending: dict[str, tuple[TradeRequest, TradeQuote]] = {}
        self._resolved: dict[str, dict] = {}
        self._adapters_v3: dict[int, DexAdapterV3] = {}
        self._adapters_v4: dict[int, DexAdapterV4] = {}

    # ---- wiring (mirrors SigningService) --------------------------------

    def set_stores(self, policy_store):
        self._policy_store = policy_store

    def set_wallet_provider(self, provider: Callable):
        self._wallet_provider = provider

    def set_wallet_status_checker(self, checker: Callable):
        self._wallet_status_checker = checker

    def set_auth_verifier(self, verifier: Callable):
        """Inject SigningService.verify_agent_signature so /trade authenticates the
        same way /sign does. When unset (e.g. in-process tests), auth is skipped."""
        self._auth_verifier = verifier

    def set_callbacks(self, on_approval_needed: Optional[Callable] = None,
                      on_activity: Optional[Callable] = None,
                      on_trade_executed: Optional[Callable] = None):
        self._on_approval_needed = on_approval_needed
        self._on_activity = on_activity
        self._on_trade_executed = on_trade_executed

    def _activity(self, message: str, is_error: bool = False, detail: str = None):
        """Emit an activity message.

        Args:
            message: Brief message for header display
            is_error: Whether this is an error
            detail: Optional detailed message for logs (defaults to message)
        """
        if self._on_activity:
            try:
                self._on_activity(message, is_error, detail)
            except Exception:
                logger.exception("trading activity callback failed")

    def _trade_executed(self, address: str):
        """Notify that a trade was executed for an address (triggers balance refresh)."""
        if self._on_trade_executed:
            try:
                self._on_trade_executed(address)
            except Exception:
                logger.exception("trade executed callback failed")

    def _adapter(self, chain_id: int, version: str = "v3") -> DexAdapter:
        """Return a cached DexAdapter for the chain, built from network + DEX config.

        Args:
            chain_id: The chain ID.
            version: "v3" or "v4".
        """
        network = NETWORKS.get(chain_id)
        if network is None:
            raise DexError(f"no network configured for chain {chain_id}")

        if version == "v4":
            if chain_id not in self._adapters_v4:
                dex = get_dex_v4(chain_id)
                if dex is None:
                    raise DexError(f"no V4 DEX configured for chain {chain_id}")
                self._adapters_v4[chain_id] = DexAdapterV4(network.rpc_url, dex)
            return self._adapters_v4[chain_id]
        else:
            if chain_id not in self._adapters_v3:
                dex = get_dex(chain_id)
                if dex is None:
                    raise DexError(f"no V3 DEX configured for chain {chain_id}")
                self._adapters_v3[chain_id] = DexAdapterV3(network.rpc_url, dex)
            return self._adapters_v3[chain_id]

    def _base_addresses(self, chain_id: int, version: str = "v3") -> tuple[str, str]:
        """(usdg_address, weth_address) for the chain."""
        if version == "v4":
            dex = get_dex_v4(chain_id)
        else:
            dex = get_dex(chain_id)
        usdg = TOKENS["USDG"].addresses.get(chain_id, "")
        return usdg, dex.weth if dex else ""

    # ---- quote + valuation (fully testable now) -------------------------

    def prepare_trade(self, request: TradeRequest) -> TradeQuote:
        """Re-quote and value a request. Raises DexError/ValueError with a reason
        if the trade can't be quoted (bad shape, no pool, no liquidity).

        Special cases:
        - ETH -> WETH (wrap): 1:1 conversion, no pool lookup
        - WETH -> ETH (unwrap): 1:1 conversion, no pool lookup

        V4 trades require tick_spacing and hooks to be specified.
        """
        ok, reason = request.validate_shape()
        if not ok:
            raise ValueError(reason)

        version = request.infer_version()
        adapter = self._adapter(request.chain_id, version)

        if version == "v4":
            dex = get_dex_v4(request.chain_id)
        else:
            dex = get_dex(request.chain_id)

        meta_in = adapter.token_metadata(request.token_in)
        meta_out = adapter.token_metadata(request.token_out)
        amount_in_atomic = to_atomic(request.amount_in, meta_in["decimals"])

        # Special case: wrap/unwrap (1:1 conversion, no pool needed)
        if is_wrap_trade(request.token_in, request.token_out, dex.weth):
            return TradeQuote(
                token_in=meta_in["address"], token_out=meta_out["address"],
                fee_tier=0, pool="WRAP",  # Special marker
                amount_in_atomic=amount_in_atomic,
                amount_out_expected=amount_in_atomic,  # 1:1
                amount_out_min=amount_in_atomic,       # No slippage
                token_in_decimals=meta_in["decimals"], token_out_decimals=meta_out["decimals"],
                effective_slippage_bps=0,
                gas_estimate=50000,  # Approximate gas for wrap
                notional_usdg=None,  # Wrap/unwrap has no notional value change
                symbol_in=meta_in.get("symbol"), symbol_out=meta_out.get("symbol"),
            )

        if is_unwrap_trade(request.token_in, request.token_out, dex.weth):
            return TradeQuote(
                token_in=meta_in["address"], token_out=meta_out["address"],
                fee_tier=0, pool="UNWRAP",  # Special marker
                amount_in_atomic=amount_in_atomic,
                amount_out_expected=amount_in_atomic,  # 1:1
                amount_out_min=amount_in_atomic,       # No slippage
                token_in_decimals=meta_in["decimals"], token_out_decimals=meta_out["decimals"],
                effective_slippage_bps=0,
                gas_estimate=30000,  # Approximate gas for unwrap
                notional_usdg=None,
                symbol_in=meta_in.get("symbol"), symbol_out=meta_out.get("symbol"),
            )

        # Native ETH support: V3 pools use WETH, so substitute for pool/quote lookups.
        # V4 can use native ETH directly (currency0 = address(0)), but this depends on
        # the specific pool - the agent must know which type of pool they're targeting.
        # For simplicity, we substitute WETH for V3; V4 passes tokens as-is to the adapter.
        eth_input = is_native_eth(request.token_in)
        eth_output = is_native_eth(request.token_out)

        if version == "v4":
            # V4: pass tokens as-is; adapter handles native ETH via currency0/currency1
            pool_token_in = request.token_in
            pool_token_out = request.token_out
        else:
            # V3: substitute WETH for native ETH
            pool_token_in = dex.weth if eth_input else request.token_in
            pool_token_out = dex.weth if eth_output else request.token_out

        pool = adapter.find_pool(pool_token_in, pool_token_out, request.fee_tier,
                                 tick_spacing=request.tick_spacing, hooks=request.hooks)
        if pool is None:
            raise DexError(
                f"no pool for {meta_in['symbol']}/{meta_out['symbol']} at fee {request.fee_tier}")

        q = adapter.quote_exact_input_single(
            pool_token_in, pool_token_out, amount_in_atomic, request.fee_tier,
            tick_spacing=request.tick_spacing, hooks=request.hooks)
        expected_out = q["amount_out"]

        # Effective slippage: the request's tolerance (policy will cap this later).
        effective_bps = request.max_slippage_bps
        amount_out_min = expected_out * (10000 - effective_bps) // 10000

        # Notional: value the base-asset leg (USDG or WETH) in USDG.
        usdg, weth = self._base_addresses(request.chain_id, version)
        bases = {usdg.lower(), weth.lower()}
        notional = None
        try:
            if request.token_in.lower() in bases:
                notional = pricing.value_base_leg(
                    request.token_in, amount_in_atomic, meta_in["decimals"], usdg, weth)
            elif request.token_out.lower() in bases:
                notional = pricing.value_base_leg(
                    request.token_out, expected_out, meta_out["decimals"], usdg, weth)
        except pricing.PricingError:
            notional = None  # valuation unavailable; policy size checks will handle

        return TradeQuote(
            token_in=meta_in["address"], token_out=meta_out["address"],
            fee_tier=request.fee_tier, pool=pool,
            amount_in_atomic=amount_in_atomic,
            amount_out_expected=expected_out,
            amount_out_min=amount_out_min,
            token_in_decimals=meta_in["decimals"], token_out_decimals=meta_out["decimals"],
            effective_slippage_bps=effective_bps,
            gas_estimate=q["gas_estimate"], notional_usdg=notional,
            symbol_in=meta_in.get("symbol"), symbol_out=meta_out.get("symbol"),
            dex_version=version,
            tick_spacing=request.tick_spacing,
            hooks=request.hooks,
        )

    # ---- request handling -----------------------------------------------

    def handle_trade_request(self, agent_id: str, trade_data: dict,
                             signature: Optional[str] = None) -> dict:
        """Entry point for a trade request. Resolves and authenticates the agent,
        then quotes/values the trade and gates it. Returns a JSON-able dict.

        `signature` is the agent's bearer token or SIG:ts:hex over
        ``{agent_id, timestamp, trade}``, verified the same way /sign verifies its
        payload (see set_auth_verifier).
        """
        agent = self._policy_store.get_agent_by_id(agent_id) if self._policy_store else None
        if agent is None:
            return {"status": "error", "code": "UNKNOWN_AGENT",
                    "error": f"No agent with id {agent_id}"}

        # Authenticate the same way /sign does, when the verifier is wired.
        if self._auth_verifier is not None:
            auth_err = self._authenticate(agent, agent_id, signature, trade_data)
            if auth_err is not None:
                return auth_err

        trade_data = dict(trade_data)
        trade_data.setdefault("agent_id", agent_id)
        trade_data.setdefault("chain_id", 4663)
        try:
            request = TradeRequest.from_dict(trade_data)
        except (KeyError, ValueError, TypeError) as e:
            return {"status": "error", "code": "BAD_REQUEST", "error": str(e)}

        if not request.recipient:
            request.recipient = getattr(agent, "wallet_address", None)

        try:
            quote = self.prepare_trade(request)
        except (DexError, ValueError) as e:
            self._activity(f"Trade rejected for {agent_id}: {e}", is_error=True)
            return TradeResult.rejected(request.id, str(e)).to_dict()

        decision = self._evaluate_policy(agent, request, quote)
        if decision["action"] == "reject":
            self._activity(f"Trade rejected for {agent_id}: {decision['reason']}", is_error=True)
            return TradeResult.rejected(request.id, decision["reason"], quote).to_dict()

        if decision["action"] == "auto":
            # Record transaction before execution (auto_approved=True)
            tx = self._create_trade_transaction(request, quote, auto_approved=True)

            result = self.execute_trade(request, quote)

            # Update transaction with result
            if tx and self._policy_store:
                if result.get("status") == "executed":
                    tx.status = STATUS_SETTLED
                    tx.tx_hash = result.get("tx_hash")
                    # Convert atomic amount_out to human-readable
                    amount_out_atomic = result.get("amount_out")
                    if amount_out_atomic is not None:
                        tx.amount_out = str(from_atomic(amount_out_atomic, quote.token_out_decimals))
                else:
                    tx.status = STATUS_FAILED
                    tx.reject_reason = result.get("reason", "Trade failed")
                self._policy_store.update_transaction(tx)

            # Track volume on successful auto-approved trades
            if result.get("status") == "executed":
                if quote.notional_usdg:
                    agent.add_trading_volume(quote.notional_usdg)
                    if self._policy_store:
                        self._policy_store.update_agent(agent)
                    self._activity(f"Trade auto-approved: ${quote.notional_usdg:.2f}")
                # Notify for balance refresh
                if request.recipient:
                    self._trade_executed(request.recipient)
            return result

        # Default: escalate to human approval.
        self._pending[request.id] = (request, quote)

        # Build activity messages: brief (symbols) for header, detailed (addresses) for logs
        sym_in = quote.symbol_in or quote.token_in[:10]
        sym_out = quote.symbol_out or quote.token_out[:10]
        notional = f" (~${quote.notional_usdg:.2f})" if quote.notional_usdg else ""

        brief = f"Trade pending: {agent_id} {request.amount_in} {sym_in} → {sym_out}{notional}"
        detail = (f"Trade pending: {agent_id} {request.amount_in} "
                  f"{sym_in} ({quote.token_in}) → {sym_out} ({quote.token_out}){notional}")
        self._activity(brief, detail=detail)
        if self._on_approval_needed:
            try:
                self._on_approval_needed(request, quote)
            except Exception:
                logger.exception("approval callback failed")
        result = TradeResult.pending(request.id, quote)
        return result.to_dict()

    def _authenticate(self, agent, agent_id: str, signature: Optional[str],
                      trade_data: dict) -> Optional[dict]:
        """Agent status + signature checks, mirroring handle_sign_request. Returns
        an error dict on failure, or None on success."""
        status = getattr(agent, "status", None)
        if status == "uncommissioned":
            return {"status": "error", "code": "AGENT_NOT_COMMISSIONED",
                    "error": "Agent not commissioned"}
        if status == "suspended":
            return {"status": "error", "code": "AGENT_SUSPENDED", "error": "Agent suspended"}

        signed_fields = {"trade": trade_data}
        # Bearer verifies before the wallet check (bearer needs no wallet).
        if getattr(agent, "auth_mode", "hmac") == "bearer":
            err = self._auth_verifier(agent, agent_id, signature, None, signed_fields)
            return None if not err else {"status": "error", "code": "AUTH_FAILED", "error": err}

        # HMAC needs the unlocked wallet's password to decrypt the agent secret.
        if not self._wallet_provider:
            return {"status": "error", "code": "NO_WALLET_PROVIDER", "error": "Wallet provider not set"}
        wallet = self._wallet_provider(getattr(agent, "wallet_address", None))
        if not wallet:
            if self._wallet_status_checker and self._wallet_status_checker():
                return {"status": "error", "code": "WALLET_ADDRESS_NOT_FOUND",
                        "error": "Agent's wallet address is not available in the unlocked wallet."}
            return {"status": "error", "code": "WALLET_LOCKED",
                    "error": "Wallet is locked. Unlock the wallet to enable trading."}
        err = self._auth_verifier(agent, agent_id, signature, wallet._password, signed_fields)
        return None if not err else {"status": "error", "code": "AUTH_FAILED", "error": err}

    # ---- pending trades: status / approve / reject ----------------------

    def get_trade_status(self, request_id: str) -> dict:
        """Status of a trade awaiting approval (polled by the agent)."""
        if request_id in self._pending:
            _, quote = self._pending[request_id]
            return {"status": "pending", "request_id": request_id, "quote": quote.to_dict()}
        if request_id in self._resolved:
            return self._resolved[request_id]
        return {"status": "error", "code": "UNKNOWN_REQUEST",
                "error": f"No trade request {request_id}"}

    def get_pending_trades(self) -> list:
        """Pending (request, quote) pairs, for the approval UI."""
        return list(self._pending.values())

    def approve_trade(self, request_id: str) -> dict:
        """Approve a pending trade and execute it (called by the app/user)."""
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return {"status": "error", "code": "UNKNOWN_REQUEST",
                    "error": f"No pending trade {request_id}"}
        request, quote = entry

        # Record trade in transaction history
        tx = self._create_trade_transaction(request, quote)

        result = self.execute_trade(request, quote)
        self._resolved[request_id] = result

        # Update transaction with result
        if tx and self._policy_store:
            if result.get("status") == "executed":
                tx.status = STATUS_SETTLED
                tx.tx_hash = result.get("tx_hash")
                # Convert atomic amount_out to human-readable
                amount_out_atomic = result.get("amount_out")
                if amount_out_atomic is not None:
                    tx.amount_out = str(from_atomic(amount_out_atomic, quote.token_out_decimals))
                # Track volume on successful manual trades
                if quote.notional_usdg:
                    agent = self._policy_store.get_agent(request.agent_id)
                    if agent:
                        agent.add_trading_volume(quote.notional_usdg)
                        self._policy_store.update_agent(agent)
            else:
                tx.status = STATUS_FAILED
                tx.reject_reason = result.get("reason", "Trade failed")
            self._policy_store.update_transaction(tx)

        # Notify for balance refresh on successful trades
        if result.get("status") == "executed" and request.recipient:
            self._trade_executed(request.recipient)

        return result

    def reject_trade(self, request_id: str, reason: str = "Rejected by user") -> dict:
        """Reject a pending trade (called by the app/user)."""
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return {"status": "error", "code": "UNKNOWN_REQUEST",
                    "error": f"No pending trade {request_id}"}
        request, quote = entry

        # Record rejected trade in transaction history
        tx = self._create_trade_transaction(request, quote)
        if tx and self._policy_store:
            tx.status = STATUS_REJECTED
            tx.reject_reason = reason
            self._policy_store.update_transaction(tx)

        result = TradeResult.rejected(request.id, reason, quote).to_dict()
        self._resolved[request_id] = result
        self._activity(f"Trade {request_id} rejected: {reason}")
        return result

    def _create_trade_transaction(self, request: TradeRequest, quote: TradeQuote,
                                    auto_approved: bool = False) -> Optional[Transaction]:
        """Create and persist a Transaction record for a trade."""
        if not self._policy_store:
            return None

        agent = self._policy_store.get_agent_by_id(request.agent_id)
        if not agent:
            logger.warning(f"Agent not found for trade {request.id}: {request.agent_id}")
            return None

        # Get wallet info
        wallet_address = request.recipient or getattr(agent, "wallet_address", None)
        wallet_id = getattr(agent, "wallet_id", None)

        tx = Transaction.create_trade(
            agent_id=agent.id,
            agent_name=agent.name,
            agent_code=agent.code,
            network=f"eip155:{request.chain_id}",
            token_in=request.token_in,
            token_out=request.token_out,
            symbol_in=quote.symbol_in or "",
            symbol_out=quote.symbol_out or "",
            amount_in=str(request.amount_in),
            fee_tier=request.fee_tier,
            wallet_address=wallet_address,
            wallet_id=wallet_id,
            slippage_bps=request.max_slippage_bps,
            pool=quote.pool,
            auto_approved=auto_approved,
        )

        self._policy_store.add_transaction(tx)
        return tx

    def _evaluate_policy(self, agent: "Agent", request: TradeRequest,
                         quote: TradeQuote) -> dict:
        """Policy decision: reject / auto-execute / escalate.

        Checks trading rules from the agent's policy:
        1. Trading enabled
        2. Per-trade max (USD)
        3. Daily volume limit (USD)
        4. Max slippage (%)
        5. Min ETH reserve

        Returns {"action": "reject"|"auto"|"escalate", "reason": str}
        """
        from datetime import date

        # Get policy
        if not self._policy_store or not agent.policy_id:
            return {"action": "reject", "reason": "Agent has no policy assigned"}

        policy = self._policy_store.get_policy(agent.policy_id)
        if not policy:
            return {"action": "reject", "reason": "Policy not found"}

        # Check trading rules exist and are enabled
        if not policy.trading_rules:
            return {"action": "reject", "reason": "Trading not configured for this policy"}

        rules = policy.trading_rules
        if not rules.enabled:
            return {"action": "reject", "reason": "Trading is disabled for this policy"}

        # Trade notional value (USD)
        notional = quote.notional_usdg
        if notional is None:
            # Can't value the trade - escalate for human review
            return {"action": "escalate", "reason": "Unable to value trade in USD"}

        # 1. Per-trade max
        if notional > rules.per_trade_max_usd:
            return {
                "action": "reject",
                "reason": f"Trade ${notional:.2f} exceeds per-trade max ${rules.per_trade_max_usd:.2f}"
            }

        # 2. Daily volume limit (with calendar-day reset)
        today = date.today().isoformat()
        if agent.last_trading_reset_date != today:
            agent.reset_daily_trading_volume()
            # Save the agent state
            if self._policy_store:
                self._policy_store.update_agent(agent)

        if agent.trading_volume_today_usd + notional > rules.daily_volume_limit_usd:
            remaining = rules.daily_volume_limit_usd - agent.trading_volume_today_usd
            return {
                "action": "reject",
                "reason": f"Trade ${notional:.2f} exceeds daily limit (${remaining:.2f} remaining of ${rules.daily_volume_limit_usd:.2f})"
            }

        # 3. Max slippage (compare request slippage to policy max)
        request_slippage_pct = request.max_slippage_bps / 100.0  # Convert bps to %
        if request_slippage_pct > rules.max_slippage_percent:
            return {
                "action": "reject",
                "reason": f"Requested slippage {request_slippage_pct:.1f}% exceeds policy max {rules.max_slippage_percent:.1f}%"
            }

        # 4. Min ETH reserve check
        if rules.min_reserve_eth > 0 and request.recipient:
            try:
                adapter = self._adapter(request.chain_id, request.infer_version())
                eth_balance_wei = adapter.native_balance(request.recipient)
                eth_balance = eth_balance_wei / 1e18  # Convert wei to ETH

                # Estimate ETH needed for this trade (gas)
                gas_cost_eth = (quote.gas_estimate * 50e9) / 1e18  # ~50 gwei gas price estimate

                if eth_balance - gas_cost_eth < rules.min_reserve_eth:
                    return {
                        "action": "reject",
                        "reason": f"Insufficient ETH reserve: {eth_balance:.6f} ETH (need {rules.min_reserve_eth:.6f} ETH + gas)"
                    }
            except Exception as e:
                # Can't check balance - log and escalate
                logger.warning(f"Could not check ETH reserve: {e}")

        # 5. Auto-approve threshold
        if rules.auto_approve_below_usd is not None and notional < rules.auto_approve_below_usd:
            return {"action": "auto", "reason": f"Auto-approved (${notional:.2f} < ${rules.auto_approve_below_usd:.2f})"}

        # Default: escalate to human approval
        return {"action": "escalate", "reason": ""}

    # ---- execution (needs a funded, unlocked wallet) --------------------

    def execute_trade(self, request: TradeRequest, quote: TradeQuote) -> dict:
        """Approve (if needed), simulate, then sign and submit the swap. Requires
        the wallet unlocked. Returns an executed/failed TradeResult dict.

        Approval is for exactly amount_in, so no lingering router allowance is left
        behind (safer than infinite approve; costs one extra tx per token).

        Special cases:
        - Native ETH input: No approval needed; swap tx sends ETH via msg.value.
        - Wrap (ETH->WETH): Direct WETH9.deposit(), no swap.
        - Unwrap (WETH->ETH): Direct WETH9.withdraw(), no swap.
        """
        if not self._wallet_provider:
            return {"status": "error", "code": "NO_WALLET", "error": "No wallet provider"}

        version = request.infer_version()
        adapter = self._adapter(request.chain_id, version)

        if version == "v4":
            dex = get_dex_v4(request.chain_id)
        else:
            dex = get_dex(request.chain_id)

        sender = request.recipient
        if not sender:
            return {"status": "error", "code": "NO_RECIPIENT", "error": "No wallet address for trade"}

        wallet = self._wallet_provider(sender)
        if not wallet:
            return {"status": "error", "code": "WALLET_LOCKED",
                    "error": "Wallet is locked. Unlock the wallet to execute trades."}
        addr_entry = wallet.get_address_by_address(sender)
        if not addr_entry:
            return {"status": "error", "code": "ADDRESS_NOT_FOUND",
                    "error": f"Address {sender} not found in the unlocked wallet."}
        private_key = wallet.get_private_key(addr_entry.id).hex()
        router = adapter.router_address()

        try:
            # Handle wrap/unwrap separately (direct WETH9 calls, no swap)
            if quote.pool == "WRAP":
                wrap_tx = adapter.build_wrap_tx(quote.amount_in_atomic, sender)
                sh = adapter.sign_and_send(wrap_tx, private_key)
                self._activity(f"Trade {request.id}: wrap submitted ({sh})")
                src = adapter.wait_for_receipt(sh)
                if src.status != 1:
                    return TradeResult(request_id=request.id, status="failed",
                                       reason=f"wrap reverted (tx {sh})", tx_hash=sh, quote=quote).to_dict()
                self._activity(f"Trade {request.id} executed: {sh}")
                return TradeResult(request_id=request.id, status="executed",
                                   tx_hash=sh, amount_out=quote.amount_out_expected,
                                   quote=quote).to_dict()

            if quote.pool == "UNWRAP":
                unwrap_tx = adapter.build_unwrap_tx(quote.amount_in_atomic, sender)
                sh = adapter.sign_and_send(unwrap_tx, private_key)
                self._activity(f"Trade {request.id}: unwrap submitted ({sh})")
                src = adapter.wait_for_receipt(sh)
                if src.status != 1:
                    return TradeResult(request_id=request.id, status="failed",
                                       reason=f"unwrap reverted (tx {sh})", tx_hash=sh, quote=quote).to_dict()
                self._activity(f"Trade {request.id} executed: {sh}")
                return TradeResult(request_id=request.id, status="executed",
                                   tx_hash=sh, amount_out=quote.amount_out_expected,
                                   quote=quote).to_dict()

            # Normal swap flow
            eth_input = is_native_eth(request.token_in)
            eth_output = is_native_eth(request.token_out)

            if version == "v4":
                # V4: pass tokens as-is; adapter handles native ETH
                swap_token_in = request.token_in
                swap_token_out = request.token_out
            else:
                # V3: substitute WETH for native ETH
                swap_token_in = dex.weth if eth_input else request.token_in
                swap_token_out = dex.weth if eth_output else request.token_out

            eth_value = quote.amount_in_atomic if eth_input else 0

            # 1. Approve exactly amount_in if needed (skip for native ETH input).
            if not eth_input:
                if adapter.allowance(request.token_in, sender, router) < quote.amount_in_atomic:
                    approve_tx = adapter.build_approve_tx(
                        request.token_in, router, quote.amount_in_atomic, sender)
                    ah = adapter.sign_and_send(approve_tx, private_key)
                    self._activity(f"Trade {request.id}: approval submitted ({ah})")
                    arc = adapter.wait_for_receipt(ah)
                    if arc.status != 1:
                        return TradeResult(request_id=request.id, status="failed",
                                           reason="token approval reverted", quote=quote).to_dict()

            # 2. Simulate now that allowance exists — rejects a bad fill before we
            #    spend gas on the swap (min-out enforces slippage on-chain too).
            #    For ETH output, simulate the swap to WETH (unwrap is 1:1, no slippage).
            adapter.simulate_swap(swap_token_in, swap_token_out, request.fee_tier,
                                  sender, quote.amount_in_atomic, quote.amount_out_min, sender,
                                  eth_value=eth_value, tick_spacing=request.tick_spacing,
                                  hooks=request.hooks)

            # 3. Submit the swap (or multicall for ETH output on V3).
            if eth_output and version == "v3":
                # V3 multicall: swap to WETH (held by router) + unwrap to ETH (sent to recipient)
                swap_tx = adapter.build_swap_to_eth_tx(
                    swap_token_in, request.fee_tier, sender,
                    quote.amount_in_atomic, quote.amount_out_min, sender)
            else:
                # V4 handles native ETH directly; V3 non-ETH-output also uses this path
                swap_tx = adapter.build_swap_tx(
                    swap_token_in, swap_token_out, request.fee_tier,
                    sender, quote.amount_in_atomic, quote.amount_out_min, sender,
                    eth_value=eth_value, tick_spacing=request.tick_spacing,
                    hooks=request.hooks)
            sh = adapter.sign_and_send(swap_tx, private_key)
            self._activity(f"Trade {request.id}: swap submitted ({sh})")
            src = adapter.wait_for_receipt(sh)
            if src.status != 1:
                return TradeResult(request_id=request.id, status="failed",
                                   reason=f"swap reverted (tx {sh})", tx_hash=sh, quote=quote).to_dict()

            self._activity(f"Trade {request.id} executed: {sh}")
            return TradeResult(request_id=request.id, status="executed",
                               tx_hash=sh, amount_out=quote.amount_out_expected,
                               quote=quote).to_dict()
        except DexError as e:
            return TradeResult(request_id=request.id, status="failed",
                               reason=str(e), quote=quote).to_dict()
        except Exception as e:
            logger.exception("trade execution failed")
            return TradeResult(request_id=request.id, status="failed",
                               reason=f"execution error: {e}", quote=quote).to_dict()
