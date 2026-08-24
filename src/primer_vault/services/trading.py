"""
Trading service — orchestrates an agent trade request end to end.

Mirrors SigningService (the x402 payment path): it resolves the agent, re-quotes
the trade independently, values it in USDG notional, and then either auto-executes
under the policy threshold or escalates to human approval. Qt-free, so GUI, CLI,
and headless all share it.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass, replace as dc_replace
from typing import Callable, Optional, TYPE_CHECKING

from ..models.agent import daily_allowance_is_due
from ..models.trade import TradeRequest, TradeQuote, TradeResult
from ..wallet.ledger import LedgerError
from ..models.transaction import Transaction, STATUS_SETTLED, STATUS_FAILED, STATUS_REJECTED
from ..networks import (NETWORKS, DEFAULT_NETWORK, get_dex, get_dex_v4, TOKENS,
                        is_native_eth, is_wrap_trade, is_unwrap_trade)
from .dex import DexAdapter, DexAdapterV3, DexError, to_atomic, from_atomic
from .server import server_stats
from .dex_v4 import DexAdapterV4
from . import pricing

if TYPE_CHECKING:
    from ..models.agent import Agent

logger = logging.getLogger(__name__)

# How long a trade may wait for a human before it is abandoned. A quote goes
# stale as the pool moves, and an approval given against a price from hours ago
# is not the trade anyone looked at. Expired trades are auto-rejected.
PENDING_TRADE_TTL_SECONDS = 900  # 15 minutes

# How many finished trade results to keep for agents to poll.
#
# A result is held so the caller that submitted a trade can come back and read
# the outcome; once it has, the entry is dead weight. Each carries a full quote,
# and a headless daemon runs for weeks, so the set needs a ceiling.
#
# Generous against real use - an agent polls within seconds of submitting - and
# small enough that the set cannot grow into a problem. Past the cap the oldest
# results are dropped, and a poll for one of those gets REQUEST_NOT_FOUND, the same
# answer it already gets for a request id Vault has never seen.
MAX_RESOLVED_TRADES = 500

# How many trades one agent may have waiting at once. Per-agent rather than
# global, so one noisy agent cannot crowd another's trades out of the queue or
# bury the person approving them.
MAX_PENDING_TRADES_PER_AGENT = 20


@dataclass
class PendingTrade:
    """A trade waiting for a human, with the deadline it waits until."""
    request: "TradeRequest"
    quote: "TradeQuote"
    deadline: float  # time.monotonic() value

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


class TradingService:
    """Handles trade requests from agents: quote, value, gate, execute."""

    def __init__(self):
        self._policy_store = None
        self._wallet_provider: Optional[Callable] = None
        self._wallet_status_checker: Optional[Callable] = None
        self._auth_verifier: Optional[Callable] = None
        self._rpc_resolver: Optional[Callable] = None
        self._on_approval_needed: Optional[Callable] = None
        self._on_activity: Optional[Callable] = None
        self._on_trade_executed: Optional[Callable] = None
        self._on_hardware_sign_tx: Optional[Callable] = None  # Hardware wallet tx signing
        self._pending: dict[str, PendingTrade] = {}
        self._resolved: dict[str, dict] = {}
        # Volume committed to trades that are in flight or awaiting approval but
        # not yet recorded against the agent. Held under _volume_lock.
        # request_id -> (agent_id, usd)
        self._reservations: dict[str, tuple[str, float]] = {}
        self._volume_lock = threading.Lock()
        # chain_id -> (rpc_url the adapter was built for, adapter)
        self._adapters_v3: dict[int, tuple[str, DexAdapterV3]] = {}
        self._adapters_v4: dict[int, tuple[str, DexAdapterV4]] = {}

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

    def set_rpc_resolver(self, resolver: Optional[Callable]):
        """Inject Vault.get_rpc_url so trades use the user's endpoint if they set one.

        Unset (in-process tests), the network's built-in endpoint is used.
        """
        self._rpc_resolver = resolver

    def set_callbacks(self, on_approval_needed: Optional[Callable] = None,
                      on_activity: Optional[Callable] = None,
                      on_trade_executed: Optional[Callable] = None):
        self._on_approval_needed = on_approval_needed
        self._on_activity = on_activity
        self._on_trade_executed = on_trade_executed

    def set_hardware_tx_signer(self, handler: Optional[Callable]):
        """Inject the hardware-wallet transaction signer (GUI provides this).

        Kept separate from set_callbacks() because that setter overwrites all of
        its arguments, and the Ledger handler is registered at a different point
        in startup than the trade approval callbacks.

        Args:
            handler: Callable(tx_dict, device_path, expected_address, description)
                -> signed raw transaction hex. Raises on rejection/disconnect.
        """
        self._on_hardware_sign_tx = handler

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

    def _rpc_url(self, chain_id: int) -> Optional[str]:
        """The endpoint to trade through: the user's if they set one.

        A resolver failure falls back to the built-in endpoint rather than
        stopping the trade - the setting is a preference, and a trade refused
        because a settings read raised would be a worse answer than one sent
        to the default.
        """
        if self._rpc_resolver is not None:
            try:
                url = self._rpc_resolver(chain_id)
                if url:
                    return url
            except Exception:
                logger.exception("RPC resolver failed for chain %s", chain_id)
        network = NETWORKS.get(chain_id)
        return network.rpc_url if network else None

    def _adapter(self, chain_id: int, version: str = "v3") -> DexAdapter:
        """Return a cached DexAdapter for the chain, built from network + DEX config.

        Cached against the endpoint it was built for, so changing the RPC in
        settings takes effect on the next trade instead of being served by an
        adapter still pointed at the old node for the life of the process.

        Args:
            chain_id: The chain ID.
            version: "v3" or "v4".
        """
        network = NETWORKS.get(chain_id)
        if network is None:
            raise DexError(f"no network configured for chain {chain_id}")

        rpc_url = self._rpc_url(chain_id)
        cache = self._adapters_v4 if version == "v4" else self._adapters_v3
        cached = cache.get(chain_id)
        if cached is not None and cached[0] == rpc_url:
            return cached[1]

        if version == "v4":
            dex = get_dex_v4(chain_id)
            if dex is None:
                raise DexError(f"no V4 DEX configured for chain {chain_id}")
            adapter = DexAdapterV4(rpc_url, dex)
        else:
            dex = get_dex(chain_id)
            if dex is None:
                raise DexError(f"no V3 DEX configured for chain {chain_id}")
            adapter = DexAdapterV3(rpc_url, dex)

        cache[chain_id] = (rpc_url, adapter)
        return adapter

    def _base_addresses(self, chain_id: int, version: str = "v3") -> tuple[str, str]:
        """(usdg_address, weth_address) for the chain."""
        if version == "v4":
            dex = get_dex_v4(chain_id)
        else:
            dex = get_dex(chain_id)
        usdg = TOKENS["USDG"].addresses.get(chain_id, "")
        return usdg, dex.weth if dex else ""

    # ---- daily volume accounting ----------------------------------------
    #
    # Volume is set aside at the moment the decision is made, and released if
    # the trade does not happen. Recording it only on success would leave two
    # gaps: simultaneous trades would all measure themselves against the same
    # starting total, and a queue of trades awaiting approval would commit
    # nothing until approved, so a stack of individually-allowed trades could
    # clear the daily cap together. What is checked has to include what is
    # already promised.

    def _reset_volume_if_due(self, agent) -> None:
        """Renew the agent's daily trading volume, under `_volume_lock` and only
        after re-confirming the renewal is still due.

        The check that decided to call this ran outside the lock and can be
        stale: two trades arriving as the day rolls over can both decide a reset
        is due, and if the second one's reset lands after the first has already
        reset, reserved and committed, it sets the counter back to zero and
        forgets a swap already on-chain - so the cap is then measured against a
        total that is short by that trade. Re-checking under the same lock every
        other reader and writer of the counter takes closes that window. The
        disk write is done after the lock is released.
        """
        with self._volume_lock:
            if not daily_allowance_is_due(
                    agent.last_trading_reset_date, agent.last_trading_reset_at):
                return
            agent.reset_daily_trading_volume()
        if self._policy_store:
            self._policy_store.update_agent(agent)

    def _check_and_reserve_volume(self, agent, request_id: str, usd: float,
                                  limit_usd: float,
                                  exclude_request_id: str = None) -> Optional[str]:
        """Take `usd` off the agent's remaining daily volume, or refuse.

        Both halves happen under one hold of the lock, and that is the whole
        point. Split across two acquisitions, two requests arriving together
        would each read the same remaining balance, each find room, and each
        proceed - carrying the agent past its daily limit by the size of the
        second trade. Locking each half correctly does not help; only the pair
        being indivisible does.

        The agent API serves requests concurrently, up to
        `server.MAX_WORKER_THREADS`, so "arriving together" is an ordinary event
        rather than a contrived one. The number is not quoted here because it has
        already drifted once - it read 16, which is the Admin API's cap, not this
        one.

        `exclude_request_id` leaves that request's own reservation out of the
        sum, so re-evaluating a trade at approval time does not count it against
        itself.

        Returns None if the volume was reserved, or a refusal reason if not.
        """
        with self._volume_lock:
            reserved = sum(u for rid, (aid, u) in self._reservations.items()
                           if aid == agent.id and rid != exclude_request_id)
            committed = agent.trading_volume_today_usd + reserved
            if committed + usd > limit_usd:
                remaining = max(0.0, limit_usd - committed)
                return (f"Trade ${usd:.2f} exceeds daily limit "
                        f"(${remaining:.2f} remaining of ${limit_usd:.2f})")
            self._reservations[request_id] = (agent.id, usd)
            return None

    def reserved_volume_for(self, agent_id: str) -> float:
        """USD this agent has promised to trades in flight or awaiting approval.

        Published so the agent can be told a remaining balance it can actually
        spend. Without it the figure counts only settled volume, so an agent with
        trades queued is told it has room the next request will refuse.
        """
        with self._volume_lock:
            return sum(u for aid, u in self._reservations.values() if aid == agent_id)

    def _release_volume(self, request_id: str) -> None:
        """Give back a reservation for a trade that did not happen."""
        with self._volume_lock:
            self._reservations.pop(request_id, None)

    def _commit_volume(self, request_id: str, agent, usd: float) -> None:
        """Turn a reservation into recorded volume, atomically."""
        with self._volume_lock:
            self._reservations.pop(request_id, None)
            agent.add_trading_volume(usd)
        if self._policy_store:
            self._policy_store.update_agent(agent)

    # ---- finished trades -------------------------------------------------

    @staticmethod
    def _count_refusal(result: dict) -> dict:
        """Count a trade refused before it ever became pending, and return it."""
        server_stats.trade_rejected += 1
        return result

    @staticmethod
    def _count_outcome(result: dict) -> None:
        """Add a finished trade to the session counters behind /status.

        Only terminal states count. `pending` is a trade waiting on the user and
        `simulated` never reaches a pool, so counting either would report work
        the server has not done.
        """
        status = result.get("status")
        if status == "executed":
            server_stats.traded += 1
        elif status in ("rejected", "failed"):
            server_stats.trade_rejected += 1

    def _remember_result(self, request_id: str, result: dict) -> dict:
        """Record a trade's outcome for the agent to poll, oldest dropped first.

        dict preserves insertion order, so the oldest keys are simply the first
        ones - no timestamp needed to know what to evict.
        """
        self._count_outcome(result)
        self._resolved[request_id] = result
        while len(self._resolved) > MAX_RESOLVED_TRADES:
            self._resolved.pop(next(iter(self._resolved)))
        return result

    # ---- pending expiry --------------------------------------------------

    def _expire_pending(self) -> None:
        """Auto-reject anything that has waited past its deadline.

        Called before every read or decision on the pending set, so an expired
        trade cannot be approved by a caller that happened to arrive first.
        """
        for request_id, entry in list(self._pending.items()):
            if not entry.expired:
                continue
            self._pending.pop(request_id, None)
            self._release_volume(request_id)
            reason = (f"Expired: not approved within "
                      f"{PENDING_TRADE_TTL_SECONDS // 60} minutes")
            tx = self._create_trade_transaction(entry.request, entry.quote)
            if tx and self._policy_store:
                tx.status = STATUS_REJECTED
                tx.reject_reason = reason
                self._policy_store.update_transaction(tx)
            self._remember_result(request_id, TradeResult.rejected(
                request_id, reason, entry.quote).to_dict())
            self._activity(f"Trade {request_id} expired without approval", is_error=True)

    # ---- price impact ----------------------------------------------------

    #: Fraction of the trade used to sample the pool's rate. Small enough that it
    #: barely moves the price, large enough not to vanish into integer rounding
    #: on a 6-decimal token.
    _IMPACT_PROBE_FRACTION = 10_000

    def _price_impact_pct(self, adapter, request: TradeRequest,
                          pool_token_in: str, pool_token_out: str,
                          amount_in_atomic: int, expected_out: int) -> Optional[float]:
        """How much worse this fill is than the pool's own rate, plus the fee.

        The agent chooses the pool, and every other rule is blind to that choice:
        slippage compares the pool against itself, and the notional is read off
        whichever leg is USDG or WETH - usually the input. So a pool too thin for
        the size quotes a near-worthless output, fills inside its slippage
        tolerance, and reports the size the agent asked for. Nothing looks at
        the rate.

        Measured by quoting a dust amount first. That establishes the rate
        without moving the price; the real trade is then compared against it. The
        tier's fee is added on top, so one number answers "how much is this trade
        paying away" whatever the cause.

        A round trip cannot do this. Impact in an AMM reverses - the price moves
        out on the way in and back on the way out - so buying and selling back
        costs about twice the fee no matter how thin the pool is, and the case
        this exists to catch would pass.

        Returns a percentage, or None if it could not be measured. None is
        unknown rather than fine: the policy check escalates on it.
        """
        if expected_out <= 0 or amount_in_atomic <= 0:
            return None

        # At least one atomic unit, so a low-decimal amount still samples the
        # pool rather than flooring the probe to zero.
        probe_in = max(1, amount_in_atomic // self._IMPACT_PROBE_FRACTION)
        if probe_in >= amount_in_atomic:
            # The trade is at the atomic floor: no smaller amount can be quoted
            # to establish the pool's near-spot rate, so the impact cannot be
            # measured. Unknown, not "just the fee" - the policy escalates on it.
            return None

        try:
            probe = adapter.quote_exact_input_single(
                pool_token_in, pool_token_out, probe_in, request.fee_tier,
                tick_spacing=request.tick_spacing, hooks=request.hooks)
        except DexError as e:
            logger.warning("Price impact unavailable for %s/%s at fee %s: %s",
                           pool_token_in, pool_token_out, request.fee_tier, e)
            return None

        probe_out = probe.get("amount_out", 0)
        if probe_out <= 0:
            return None

        # Rate per unit in, at dust size versus real size. Both quotes are net of
        # the fee, so the ratio cancels it and leaves impact alone - which is why
        # the fee is added back below rather than double-counted.
        spot_rate = probe_out / probe_in
        fill_rate = expected_out / amount_in_atomic
        impact = (1 - fill_rate / spot_rate) * 100

        return max(0.0, impact) + self._fee_percent(request.fee_tier)

    @staticmethod
    def _fee_percent(fee_tier: int) -> float:
        """A Uniswap fee tier as a percentage.

        Tiers are millionths: 500 -> 0.05%, 3000 -> 0.30%, 10000 -> 1.00%. Kept
        as one function because the conversion is needed in two places and they
        drifted once - one divided by 100, overstating the fee a hundredfold, so
        any trade too small to sample looked ruinously expensive and escalated.
        """
        return fee_tier / 10_000

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

        # Wrap/unwrap move ETH and WETH 1:1, so there is no price change - but the
        # amount moved still has a dollar value, and the policy caps are about how
        # much an agent may move. Value the WETH-equivalent leg so a wrap is judged
        # like any other trade. Leaving it unpriced made every wrap escalate for
        # want of a number, whatever the auto-approve threshold was set to.
        def wrap_notional():
            usdg_addr, _ = self._base_addresses(request.chain_id, version)
            try:
                return pricing.value_base_leg(
                    dex.weth, amount_in_atomic, meta_in["decimals"],
                    usdg_addr, dex.weth)
            except pricing.PricingError:
                return None  # unpriceable -> policy check escalates, as elsewhere

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
                price_impact_pct=0.0,  # 1:1, no pool and no impact
                notional_usdg=wrap_notional(),
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
                price_impact_pct=0.0,  # 1:1, no pool and no impact
                notional_usdg=wrap_notional(),
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

        price_impact = self._price_impact_pct(
            adapter, request, pool_token_in, pool_token_out,
            amount_in_atomic, expected_out)

        # Notional: value the INPUT leg only, and only when it is a base asset
        # Vault can price independently (USDG = $1, WETH via the ETH feed).
        #
        # The input leg is what leaves the wallet, so it is what the per-trade
        # cap must bound. Valuing the OUTPUT leg instead (the USDG/WETH received)
        # measures the wrong side: an unknown token has no price Vault can trust
        # - its only on-chain price is the pool being traded into, which an
        # attacker can create and rig - so a sale of a whole token balance into
        # a cheap pool would read as a tiny received amount and slip under the
        # cap. When the input cannot be trust-priced the notional is left None,
        # and an unvaluable trade escalates to a human rather than auto-executing.
        usdg, weth = self._base_addresses(request.chain_id, version)
        bases = {usdg.lower(), weth.lower()}
        # Native ETH is priced as WETH: same asset, same feed, same 18 decimals.
        # It arrives as "ETH"/address(0), which is neither base address, so
        # without this an ETH-input swap - a documented, common shape - would be
        # left unvalued and escape the per-trade and daily caps.
        base_in = weth if is_native_eth(request.token_in) else request.token_in
        notional = None
        if base_in.lower() in bases:
            try:
                notional = pricing.value_base_leg(
                    base_in, amount_in_atomic, meta_in["decimals"], usdg, weth)
            except pricing.PricingError:
                notional = None  # feed down; unvaluable trades escalate

        return TradeQuote(
            token_in=meta_in["address"], token_out=meta_out["address"],
            fee_tier=request.fee_tier, pool=pool,
            amount_in_atomic=amount_in_atomic,
            amount_out_expected=expected_out,
            amount_out_min=amount_out_min,
            token_in_decimals=meta_in["decimals"], token_out_decimals=meta_out["decimals"],
            effective_slippage_bps=effective_bps,
            gas_estimate=q["gas_estimate"], notional_usdg=notional,
            price_impact_pct=price_impact,
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

        # Who the caller is and which address it may use are settled by
        # authentication, not by the payload. Naming either in the request is
        # refused outright rather than quietly ignored, so a caller that tries
        # finds out instead of believing it worked.
        claimed = [f for f in ("agent_id", "wallet_address", "recipient")
                   if f in trade_data]
        if claimed:
            return {
                "status": "error", "code": "FIELD_NOT_PERMITTED",
                "error": (f"Trade requests may not set {', '.join(claimed)}. "
                          "Vault uses the agent and address the credentials belong to."),
            }

        trade_data = dict(trade_data)
        trade_data.setdefault("chain_id", DEFAULT_NETWORK)
        try:
            request = TradeRequest.from_dict(trade_data)
        except (KeyError, ValueError, TypeError) as e:
            return {"status": "error", "code": "BAD_REQUEST", "error": str(e)}

        request.agent_id = agent.id
        request.wallet_address = agent.wallet_address
        if not request.wallet_address:
            return {"status": "error", "code": "AGENT_NOT_COMMISSIONED",
                    "error": "Agent has no commissioned wallet address"}

        unsignable = self._signer_unavailable(request)
        if unsignable is not None:
            self._activity(f"Trade rejected for {agent_id}: {unsignable['error']}", is_error=True)
            return unsignable

        try:
            quote = self.prepare_trade(request)
        except (DexError, ValueError) as e:
            self._activity(f"Trade rejected for {agent_id}: {e}", is_error=True)
            # Refused at intake, so there is no pending request to resolve
            # later and _remember_result never sees it.
            return self._count_refusal(TradeResult.rejected(request.id, str(e)).to_dict())

        self._expire_pending()

        # Not a reject means the trade's volume is reserved, inside the same lock
        # hold that found room for it - unless the trade could not be valued, in
        # which case it escalated before the reservation. See _evaluate_policy.
        decision = self._evaluate_policy(agent, request, quote)
        if decision["action"] == "reject":
            self._activity(f"Trade rejected for {agent_id}: {decision['reason']}", is_error=True)
            # Refused at intake, so there is no pending request to resolve
            # later and _remember_result never sees it.
            return self._count_refusal(TradeResult.rejected(request.id, decision["reason"], quote).to_dict())

        if decision["action"] == "auto":
            # Record transaction before execution (auto_approved=True)
            tx = self._create_trade_transaction(request, quote, auto_approved=True)

            result = self.execute_trade(request, quote)

            try:
                self._record_outcome(request, quote, tx, result)
                if result.get("status") == "executed" and quote.notional_usdg:
                    self._activity(f"Trade auto-approved: ${quote.notional_usdg:.2f}")
            except Exception:
                logger.exception("failed to record outcome of trade %s", request.id)
            return result

        # Default: escalate to human approval.
        waiting = sum(1 for e in self._pending.values()
                      if e.request.agent_id == agent.id)
        if waiting >= MAX_PENDING_TRADES_PER_AGENT:
            self._release_volume(request.id)
            reason = (f"{MAX_PENDING_TRADES_PER_AGENT} trades from this agent are "
                      f"already waiting for approval")
            self._activity(f"Trade rejected for {agent_id}: {reason}", is_error=True)
            # Refused at intake, so there is no pending request to resolve
            # later and _remember_result never sees it.
            return self._count_refusal(TradeResult.rejected(request.id, reason, quote).to_dict())

        self._pending[request.id] = PendingTrade(
            request=request, quote=quote,
            deadline=time.monotonic() + PENDING_TRADE_TTL_SECONDS)

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

    def _signer_unavailable(self, request: TradeRequest) -> Optional[dict]:
        """Refuse now if nothing could sign this trade, whatever the policy says.

        A hardware address needs the desktop app to drive the device. Without it
        the trade would quote, pass policy, take a request id, and only fail once
        a human had approved it - asking someone to authorise something that was
        never going to work.

        This does not reach for the device itself. Whether it is plugged in and
        unlocked is a question for the moment of signing, and one that cannot be
        settled in advance.
        """
        if not self._wallet_provider or not request.wallet_address:
            return None
        wallet = self._wallet_provider(request.wallet_address)
        if not wallet:
            return None  # locked or unknown; the usual paths report that
        entry = wallet.get_address_by_address(request.wallet_address)
        if entry is None or not entry.is_hardware:
            return None
        if self._on_hardware_sign_tx is not None:
            return None
        return {
            "status": "error", "code": "LEDGER_SIGN_NOT_AVAILABLE",
            "error": (f"This agent signs from a {entry.device_label} address, which "
                      "needs the Vault window open to confirm on the device. "
                      "Headless and CLI modes cannot prompt for one."),
        }

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

        # HMAC needs the unlocked wallet's master key to decrypt the agent secret.
        if not self._wallet_provider:
            return {"status": "error", "code": "NO_WALLET_PROVIDER", "error": "Wallet provider not set"}
        wallet = self._wallet_provider(getattr(agent, "wallet_address", None))
        if not wallet:
            if self._wallet_status_checker and self._wallet_status_checker():
                return {"status": "error", "code": "WALLET_ADDRESS_NOT_FOUND",
                        "error": "Agent's wallet address is not available in the unlocked wallet."}
            # Locked wallet, HMAC not yet verifiable: answer AUTH_FAILED rather
            # than WALLET_LOCKED, so an unauthenticated caller cannot read the
            # wallet's lock state by probing. (Bearer is verified above.)
            return {"status": "error", "code": "AUTH_FAILED",
                    "error": "Authentication failed"}
        err = self._auth_verifier(agent, agent_id, signature, wallet.data_key, signed_fields)
        return None if not err else {"status": "error", "code": "AUTH_FAILED", "error": err}

    # ---- pending trades: status / approve / reject ----------------------

    def get_trade_status(self, request_id: str) -> dict:
        """Status of a trade awaiting approval (polled by the agent)."""
        self._expire_pending()
        if request_id in self._pending:
            entry = self._pending[request_id]
            return {"status": "pending", "request_id": request_id,
                    "quote": entry.quote.to_dict(),
                    "expires_in_seconds": max(0, int(entry.deadline - time.monotonic()))}
        if request_id in self._resolved:
            return self._resolved[request_id]
        return {"status": "error", "code": "REQUEST_NOT_FOUND",
                "error": f"No trade request {request_id}"}

    def get_pending_trades(self) -> list:
        """Pending (request, quote) pairs, for the approval UI."""
        self._expire_pending()
        return [(e.request, e.quote) for e in self._pending.values()]

    def approve_trade(self, request_id: str) -> dict:
        """Approve a pending trade, re-check it, and execute it.

        A trade can sit here for minutes, so what was true when it arrived is
        re-established before any money moves:

          - the pool is quoted again, because the price has moved;
          - the policy is applied to that fresh value, because the limits or the
            agent's standing may have changed since;
          - the fresh quote must still clear the minimum the original request
            asked for. That minimum came from the agent's own slippage
            tolerance, so holding the new price against it is what makes that
            tolerance cover the whole wait rather than just the instant of
            execution.

        Anything that fails is rejected rather than executed on stale terms.
        """
        self._expire_pending()

        entry = self._pending.pop(request_id, None)
        if entry is None:
            if request_id in self._resolved:
                return self._resolved[request_id]
            return {"status": "error", "code": "REQUEST_NOT_FOUND",
                    "error": f"No pending trade {request_id}"}
        request, original = entry.request, entry.quote

        def refuse(reason, code="TRADE_NO_LONGER_VALID"):
            self._release_volume(request_id)
            tx = self._create_trade_transaction(request, original)
            if tx and self._policy_store:
                tx.status = STATUS_REJECTED
                tx.reject_reason = reason
                self._policy_store.update_transaction(tx)
            result = TradeResult.rejected(request_id, reason, original).to_dict()
            result["code"] = code
            self._remember_result(request_id, result)
            self._activity(f"Trade {request_id} not executed: {reason}", is_error=True)
            return result

        agent = (self._policy_store.get_agent_by_id(request.agent_id)
                 if self._policy_store else None)
        if agent is None:
            return refuse("Agent no longer exists", "UNKNOWN_AGENT")

        # Re-check the agent's standing: it may have been suspended while the
        # trade waited. Suspension is the stop button, and the payment lane
        # re-checks it at approval too - a queued swap must not slip past it.
        if getattr(agent, "status", None) == "suspended":
            return refuse("Agent is suspended", "AGENT_SUSPENDED")

        try:
            fresh = self.prepare_trade(request)
        except (DexError, ValueError) as e:
            return refuse(f"Could not re-quote the trade: {e}", "REQUOTE_FAILED")

        decision = self._evaluate_policy(agent, request, fresh,
                                         exclude_request_id=request_id)
        if decision["action"] == "reject":
            return refuse(decision["reason"], "POLICY_REJECTED")

        # The floor the agent asked for, held against the price now.
        if fresh.amount_out_expected < original.amount_out_min:
            return refuse(
                f"Price moved beyond the {original.effective_slippage_bps / 100:.2f}% "
                f"tolerance while awaiting approval; submit the trade again",
                "PRICE_MOVED")

        # Execute at today's price with whichever floor is higher. The original
        # floor covers the whole wait, so the agent's tolerance cannot be
        # stretched by the delay; the fresh floor matters when the price moved
        # UP while the trade waited - keeping the stale floor then would let
        # the swap fill far below the market it is actually trading into,
        # handing the gap to whoever orders transactions. The floor only ever
        # rises: a flat or adverse market keeps the original one.
        quote = dc_replace(fresh, amount_out_min=max(original.amount_out_min,
                                                     fresh.amount_out_min))

        # _evaluate_policy re-reserved against the fresh valuation just above. If
        # the trade cannot be valued now it never got that far, so the stale
        # reservation from intake has to go.
        if not quote.notional_usdg:
            self._release_volume(request_id)

        tx = self._create_trade_transaction(request, quote)
        result = self.execute_trade(request, quote)
        self._remember_result(request_id, result)

        # Everything below runs after the swap is already on-chain, so a failure
        # here must not be allowed to lose the record of it.
        try:
            self._record_outcome(request, quote, tx, result)
        except Exception:
            logger.exception("failed to record outcome of trade %s", request.id)

        return result

    def reject_trade(self, request_id: str, reason: str = "Rejected by user") -> dict:
        """Reject a pending trade (called by the app/user)."""
        self._expire_pending()

        entry = self._pending.pop(request_id, None)
        if entry is None:
            if request_id in self._resolved:
                return self._resolved[request_id]
            return {"status": "error", "code": "REQUEST_NOT_FOUND",
                    "error": f"No pending trade {request_id}"}
        request, quote = entry.request, entry.quote

        self._release_volume(request_id)

        # Record rejected trade in transaction history
        tx = self._create_trade_transaction(request, quote)
        if tx and self._policy_store:
            tx.status = STATUS_REJECTED
            tx.reject_reason = reason
            self._policy_store.update_transaction(tx)

        result = TradeResult.rejected(request.id, reason, quote).to_dict()
        self._remember_result(request_id, result)
        self._activity(f"Trade {request_id} rejected: {reason}")
        return result

    def _record_outcome(self, request: TradeRequest, quote: TradeQuote,
                        tx, result: dict) -> None:
        """Write down what happened to a trade: status, hash, and volume used.

        Called after the swap has been submitted, so it is the only place that
        knows the outcome. Both the auto and the approved path go through here,
        which is why volume is counted once and in one way.
        """
        executed = result.get("status") == "executed"

        if tx is not None and self._policy_store:
            if executed:
                tx.status = STATUS_SETTLED
                tx.tx_hash = result.get("tx_hash")
                # Both sides, and neither standing in for the other: the fill
                # stays None if the receipt could not be read.
                decimals = quote.token_out_decimals
                filled = result.get("amount_out")
                if filled is not None:
                    tx.amount_out = str(from_atomic(filled, decimals))
                quoted = result.get("amount_out_quoted")
                if quoted is not None:
                    tx.amount_out_quoted = str(from_atomic(quoted, decimals))
            else:
                tx.status = STATUS_FAILED
                tx.reject_reason = result.get("reason", "Trade failed")
                # The hash belongs to the broadcast, not the outcome: a swap
                # that reverted or timed out still has one, and it is the only
                # thing that lets the user check the chain rather than retry
                # blind. None when nothing reached the network.
                tx.tx_hash = result.get("tx_hash")
            self._policy_store.update_transaction(tx)

        # Volume is committed just before the send now (execute_trade), not here.
        # Anything still reserved at this point never got that far - refused, or
        # failed while the transaction was still being built or signed, none of
        # which touch the network - so give it back. A trade that reached the
        # send has nothing left to release, which is what keeps its allowance
        # spent even when the receipt was never read.
        self._release_volume(request.id)

        if not executed:
            return

        if request.wallet_address:
            self._trade_executed(request.wallet_address)

    def _create_trade_transaction(self, request: TradeRequest, quote: TradeQuote,
                                    auto_approved: bool = False) -> Optional[Transaction]:
        """Create and persist a Transaction record for a trade."""
        if not self._policy_store:
            return None

        agent = self._policy_store.get_agent_by_id(request.agent_id)
        if not agent:
            logger.warning(f"Agent not found for trade {request.id}: {request.agent_id}")
            return None

        # Resolve the address entry so the record names the key that signed,
        # the way the payment path does. Agent carries an address, not an id.
        wallet_address = request.wallet_address
        wallet_id = None
        if wallet_address and self._wallet_provider:
            wallet = self._wallet_provider(wallet_address)
            entry = wallet.get_address_by_address(wallet_address) if wallet else None
            wallet_id = entry.id if entry else None

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
                         quote: TradeQuote,
                         exclude_request_id: str = None) -> dict:
        """Policy decision: reject / auto-execute / escalate.

        Checks trading rules from the agent's policy:
        1. Trading enabled
        2. Per-trade max (USD)
        3. Max slippage (%)
        4. Price impact (%) - the rate, not just the size
        5. Min ETH balance floor
        6. Daily volume limit (USD) - checked and reserved in one step

        The daily-volume check comes last deliberately. It is the only one that
        also *takes* something, so every check that can refuse for free is
        settled first and a trade turned away for slippage or a low ETH balance
        never has to hand volume back.

        **On any non-reject return, volume is reserved - with one exception.** A
        trade that cannot be valued in USD escalates before the daily-volume step
        is reached, so nothing was set aside for it. `quote.notional_usdg is
        None` is how a caller tells the two apart, and releasing a reservation
        that was never made is harmless either way. Otherwise the caller owns
        releasing it if the trade does not go ahead.

        `exclude_request_id` leaves that request's own reservation out of the
        daily-volume sum, so re-evaluating a trade at approval time does not
        count it against itself.

        Returns {"action": "reject"|"auto"|"escalate", "reason": str}
        """

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

        # A non-finite limit (NaN/Infinity) makes every comparison below False,
        # which would silently switch the cap off. Refuse rather than trade
        # against a policy whose limits cannot be honoured.
        for _name, _val in (("per_trade_max_usd", rules.per_trade_max_usd),
                            ("daily_volume_limit_usd", rules.daily_volume_limit_usd),
                            ("max_slippage_percent", rules.max_slippage_percent),
                            ("max_price_impact_percent", rules.max_price_impact_percent),
                            ("min_reserve_eth", rules.min_reserve_eth)):
            if _val is None or not math.isfinite(_val):
                return {"action": "reject",
                        "reason": f"Policy limit {_name} is not a usable number"}

        # The agent's own deadline (unix seconds): do not execute a stale intent.
        # A free reject, before anything is reserved.
        if request.deadline is not None and time.time() >= request.deadline:
            return {"action": "reject", "reason": "Trade deadline has passed"}

        # Checks that do NOT need a dollar value run first, so that a trade
        # which cannot be priced (feed down, or an unpriceable input leg) still
        # has its slippage cap and ETH floor enforced. Only after these does an
        # unvaluable trade escalate - otherwise the escalation below would skip
        # them, letting an outage wave through a 50%-slippage swap the policy
        # forbids.

        # Max slippage (compare request slippage to policy max)
        request_slippage_pct = request.max_slippage_bps / 100.0  # Convert bps to %
        if request_slippage_pct > rules.max_slippage_percent:
            return {
                "action": "reject",
                "reason": f"Requested slippage {request_slippage_pct:.1f}% exceeds policy max {rules.max_slippage_percent:.1f}%"
            }

        notional = quote.notional_usdg

        # Per-trade max, when the trade can be valued - before the ETH-balance
        # read below, so an over-size trade is refused without an RPC round-trip.
        if notional is not None and notional > rules.per_trade_max_usd:
            return {
                "action": "reject",
                "reason": f"Trade ${notional:.2f} exceeds per-trade max ${rules.per_trade_max_usd:.2f}"
            }

        # Minimum ETH balance floor. Forward-looking, the way the daily-volume
        # and per-trade caps are: what has to clear the floor is the balance the
        # trade LEAVES, not the balance it starts from. A trade that pays in
        # native ETH (a wrap, or an ETH-input swap) sends that ETH out as
        # msg.value, so it is subtracted here; an ERC-20 -> ERC-20 swap sends no
        # ETH (only gas), so the amount is zero and the check is unchanged. Gas
        # itself is not reserved - the node prices it at build time - so this
        # keeps the floor's ETH past the traded amount, not past the gas, which
        # is the same treatment gas gets everywhere else. (Does not need a dollar
        # value, so it holds even for a trade that could not be priced.)
        if rules.min_reserve_eth > 0 and request.wallet_address:
            try:
                _adapter = self._adapter(request.chain_id, request.infer_version())
                _eth_balance = _adapter.native_balance(request.wallet_address) / 1e18
            except Exception as e:
                logger.warning(f"Could not read ETH balance: {e}")
                return {
                    "action": "reject",
                    "reason": (
                        "Could not read the wallet's ETH balance to check the minimum "
                        "ETH floor. Set the policy's minimum ETH to 0 to trade without "
                        "this check."
                    )
                }
            eth_out = (quote.amount_in_atomic / 1e18
                       if is_native_eth(request.token_in) else 0.0)
            eth_after = _eth_balance - eth_out
            if eth_after < rules.min_reserve_eth:
                if eth_out > 0:
                    reason = (
                        f"This trade would leave {eth_after:.6f} ETH, below the "
                        f"policy minimum of {rules.min_reserve_eth:.6f} ETH kept "
                        f"for gas"
                    )
                else:
                    reason = (
                        f"ETH balance {_eth_balance:.6f} is below the policy "
                        f"minimum of {rules.min_reserve_eth:.6f} ETH"
                    )
                return {"action": "reject", "reason": reason}

        # An unvaluable trade escalates - but only after the price-independent
        # caps above have had their say.
        if notional is None:
            return {"action": "escalate", "reason": "Unable to value trade in USD"}

        # 3. Price impact: the only check that looks at the rate, not the size.
        #
        #    The agent picks the pool, so this is where a bad pick is caught -
        #    a near-empty pool quotes a near-worthless output, and every other
        #    rule passes it. Escalated rather than rejected: a deliberately
        #    expensive trade is the user's call to make, not Vault's to forbid.
        #    Noted rather than returned: a trade held for approval must reserve
        #    its volume like any other, and the reservation is taken below. An
        #    escalation that returned here would queue promising nothing, so the
        #    daily total would not reflect what is waiting on the user.
        #    (The ETH floor and the slippage cap were already applied above, so
        #    they hold even for a trade that could not be valued.)
        impact = quote.price_impact_pct
        escalate_reason = None
        if impact is None:
            escalate_reason = "Could not measure this trade's price impact"
        elif impact > rules.max_price_impact_percent:
            escalate_reason = (f"Price impact {impact:.1f}% exceeds the policy limit "
                               f"of {rules.max_price_impact_percent:.1f}%")

        # 5. Daily volume limit (with calendar-day reset), checked and reserved
        #    together so two concurrent requests cannot both be told there is room.
        #    The reset is a cheap, possibly-stale pre-check here; the write itself
        #    re-confirms under the lock (see _reset_volume_if_due), so a renewal
        #    cannot land between another thread's commit and its read and erase
        #    volume already committed to a swap on-chain.
        if daily_allowance_is_due(
                agent.last_trading_reset_date, agent.last_trading_reset_at):
            self._reset_volume_if_due(agent)

        refusal = self._check_and_reserve_volume(
            agent, request.id, notional, rules.daily_volume_limit_usd,
            exclude_request_id=exclude_request_id)
        if refusal:
            return {"action": "reject", "reason": refusal}

        # Past this point the volume is spoken for, so every return below leaves
        # the caller responsible for releasing it if the trade does not happen.

        # 6. Anything the checks above flagged goes to the user, whatever the
        #    auto-approve threshold says - the threshold answers "is this small
        #    enough to wave through", not "is this a sensible trade".
        if escalate_reason:
            return {"action": "escalate", "reason": escalate_reason}

        # 7. Auto-approve threshold
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

        sender = request.wallet_address
        if not sender:
            return {"status": "error", "code": "NO_WALLET_ADDRESS",
                    "error": "No wallet address for trade"}

        # Approval can arrive long after intake and through a different route, so
        # the binding is re-checked here, immediately before a key is used.
        agent = self._policy_store.get_agent_by_id(request.agent_id) if self._policy_store else None
        if agent is None or sender.lower() != (agent.wallet_address or "").lower():
            return {"status": "error", "code": "ADDRESS_NOT_COMMISSIONED",
                    "error": "Trade address does not match the agent's commissioned address"}

        wallet = self._wallet_provider(sender)
        if not wallet:
            return {"status": "error", "code": "WALLET_LOCKED",
                    "error": "Wallet is locked. Unlock the wallet to execute trades."}
        addr_entry = wallet.get_address_by_address(sender)
        if not addr_entry:
            return {"status": "error", "code": "ADDRESS_NOT_FOUND",
                    "error": f"Address {sender} not found in the unlocked wallet."}

        # Build the submit function for this trade. Software addresses sign
        # locally; Ledger addresses are signed on the device via the GUI handler.
        # A trade can need two device confirmations (approval, then swap), so each
        # call passes a description that tells the user which one they're approving.
        if addr_entry.is_hardware:
            if not self._on_hardware_sign_tx:
                return {"status": "error", "code": "LEDGER_SIGN_NOT_AVAILABLE",
                        "error": "Ledger signing is unavailable. The Vault window must be "
                                 "open to confirm transactions on the device."}

            def submit(tx: dict, description: str, before_send=None) -> str:
                raw = self._on_hardware_sign_tx(
                    tx, addr_entry.device_path, sender, description)
                if not raw:
                    raise DexError("No signed transaction returned from the Ledger device")
                if before_send:
                    before_send()
                return adapter.send_raw(raw)
        else:
            private_key = wallet.get_private_key(addr_entry.id).hex()

            def submit(tx: dict, description: str, before_send=None) -> str:
                return adapter.sign_and_send(tx, private_key,
                                             before_send=before_send)

        # Volume counts from the moment a value-bearing transaction is sent, and
        # is never given back after that. Not from the receipt: a receipt Vault
        # could not read - RPC drop, timeout, sleeping laptop - says nothing
        # about whether the trade landed, and it usually did. Giving the
        # allowance back there let real on-chain volume exceed the daily cap.
        #
        # The boundary is the instant *before* the network call, after signing.
        # Committing after the send would leave a window - milliseconds, but
        # real - where the transaction is live on-chain and the spend is not
        # yet on disk; a crash there reboots to an agent whose allowance was
        # never debited, free to trade it again while the first trade settles.
        # Committing first inverts which way
        # a crash errs: allowance can be spent on a trade that never left, which
        # costs the agent one trade until the daily reset, instead of real value
        # moving twice.
        #
        # Signing sits on the safe side of that line: it touches no network, so a
        # failure there provably sent nothing and must not burn the allowance.
        # That is the whole reason submit() takes before_send rather than the
        # caller committing around it - only submit() knows where the network
        # call begins. Nothing is released once past it, including on a send that
        # raises: a timeout cannot distinguish "never arrived" from "arrived, and
        # the answer was lost", and treating the second as the first is exactly
        # the overspend this guards against.
        #
        # The approval steps below do not count: granting an allowance moves no
        # value. Only the wrap, the unwrap and the swap do.
        # Tracked separately from the volume itself: a trade the pricing feed
        # could not value has no notional to commit, but it was still sent, and
        # the failure message below has to say so.
        broadcast = False
        # Hash of the last transaction handed to the node - approval or swap -
        # so a failure can report it and say the trade may be live. Distinct
        # from `broadcast` below, which gates the volume commit and is set only
        # for value-bearing transactions.
        sent_hash = None

        def on_broadcast() -> None:
            """A value-bearing transaction for this trade is about to be sent."""
            nonlocal broadcast
            if broadcast:
                return
            broadcast = True
            if quote.notional_usdg:
                self._commit_volume(request.id, agent, quote.notional_usdg)

        try:
            # Handle wrap/unwrap separately (direct WETH9 calls, no swap)
            if quote.pool == "WRAP":
                wrap_tx = adapter.build_wrap_tx(quote.amount_in_atomic, sender)
                sh = submit(wrap_tx, f"Wrap {request.amount_in} ETH to WETH",
                            before_send=on_broadcast)
                sent_hash = sh
                self._activity(f"Trade {request.id}: wrap submitted ({sh})")
                src = adapter.wait_for_receipt(sh)
                if src.status != 1:
                    return TradeResult(request_id=request.id, status="failed",
                                       reason=f"wrap reverted (tx {sh})", tx_hash=sh, quote=quote).to_dict()
                self._activity(f"Trade {request.id} executed: {sh}")
                # Wrapping is 1:1 by definition, so the quote is the fill.
                return TradeResult(request_id=request.id, status="executed",
                                   tx_hash=sh, amount_out=quote.amount_out_expected,
                                   amount_out_quoted=quote.amount_out_expected,
                                   quote=quote).to_dict()

            if quote.pool == "UNWRAP":
                unwrap_tx = adapter.build_unwrap_tx(quote.amount_in_atomic, sender)
                sh = submit(unwrap_tx, f"Unwrap {request.amount_in} WETH to ETH",
                            before_send=on_broadcast)
                sent_hash = sh
                self._activity(f"Trade {request.id}: unwrap submitted ({sh})")
                src = adapter.wait_for_receipt(sh)
                if src.status != 1:
                    return TradeResult(request_id=request.id, status="failed",
                                       reason=f"unwrap reverted (tx {sh})", tx_hash=sh, quote=quote).to_dict()
                self._activity(f"Trade {request.id} executed: {sh}")
                # Unwrapping is 1:1 by definition, so the quote is the fill.
                return TradeResult(request_id=request.id, status="executed",
                                   tx_hash=sh, amount_out=quote.amount_out_expected,
                                   amount_out_quoted=quote.amount_out_expected,
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

            # 1. Authorise exactly amount_in, if it is not already authorised.
            #
            # How many transactions that takes is the adapter's business: v3's
            # router moves tokens itself and needs one approval, v4's pulls
            # through Permit2 and needs two. Asking the adapter keeps that
            # difference in one place, and keeps the step count the user is shown
            # honest for whichever version is running. Native ETH needs none.
            steps = adapter.approval_steps(
                request.token_in, sender, quote.amount_in_atomic,
                token_label=f"{request.amount_in} {quote.symbol_in}")
            total_steps = len(steps) + 1  # the approvals, then the swap
            for step_num, (approve_tx, what) in enumerate(steps, start=1):
                ah = submit(approve_tx, f"Step {step_num} of {total_steps}: {what}")
                sent_hash = ah
                self._activity(f"Trade {request.id}: approval submitted ({ah})")
                arc = adapter.wait_for_receipt(ah)
                if arc.status != 1:
                    return TradeResult(
                        request_id=request.id, status="failed",
                        reason=f"approval reverted: {what} (tx {ah})",
                        tx_hash=ah, quote=quote).to_dict()

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
            sh = submit(swap_tx, f"Step {total_steps} of {total_steps}: swap "
                                 f"{request.amount_in} {quote.symbol_in} "
                                 f"for {quote.symbol_out}",
                        before_send=on_broadcast)
            sent_hash = sh
            self._activity(f"Trade {request.id}: swap submitted ({sh})")
            src = adapter.wait_for_receipt(sh)
            if src.status != 1:
                return TradeResult(request_id=request.id, status="failed",
                                   reason=f"swap reverted (tx {sh})", tx_hash=sh, quote=quote).to_dict()

            self._activity(f"Trade {request.id} executed: {sh}")
            # The fill, not the quote. None if the receipt could not be read,
            # which the history shows as unknown rather than filling in the
            # prediction and calling it the result.
            filled = adapter.amount_received(src, request.token_out, sender)
            if filled is None:
                logger.warning(
                    "Trade %s: could not read the fill from receipt %s; history "
                    "will show the quote only", request.id, sh)
            return TradeResult(request_id=request.id, status="executed",
                               tx_hash=sh, amount_out=filled,
                               amount_out_quoted=quote.amount_out_expected,
                               quote=quote).to_dict()
        except LedgerError as e:
            # Device rejection/disconnect/locked. These messages are already
            # written for the user, so pass them through unchanged.
            self._activity(f"Trade {request.id} failed: {e}", True)
            return TradeResult(request_id=request.id, status="failed",
                               reason=str(e), quote=quote).to_dict()
        except DexError as e:
            # Whether anything is live on-chain decides what the reader should do
            # next, and this handler catches both sides of that line - a quote
            # that would not build, and a receipt that could not be read after the
            # swap was already sent. Only the second is dangerous to retry.
            #
            # `broadcast` is now set before the network call rather than after it
            # returns, so a send that itself failed lands here as "may be live".
            # That is the honest reading: a timeout or a dropped connection does
            # not tell us the node refused the transaction, and telling someone it
            # is safe to retry when it may already be mining is how a trade gets
            # made twice.
            reason = str(e)
            if broadcast or sent_hash is not None:
                reason = (f"{reason}. The transaction was signed and sent, so it "
                          "may still complete on-chain - check it before trying "
                          "again.")
            return TradeResult(request_id=request.id, status="failed",
                               reason=reason, tx_hash=sent_hash,
                               quote=quote).to_dict()
        except Exception:
            # An unexpected fault on this side. The detail goes to the log: a
            # library's exception text is not something a caller can act on, and
            # it describes Vault's internals rather than the caller's request.
            logger.exception("trade execution failed")
            if broadcast or sent_hash is not None:
                reason = ("Vault could not confirm this trade. The transaction was "
                          "signed and sent, so it may still complete on-chain - "
                          "check it before trying again. See the Vault log for the "
                          "cause.")
            else:
                reason = ("Vault could not build this trade. No transaction was "
                          "created nor sent, and it is safe to try again. See "
                          "the Vault log for the cause.")
            return TradeResult(
                request_id=request.id, status="failed", code="EXECUTION_ERROR",
                reason=reason, tx_hash=sent_hash, quote=quote).to_dict()
