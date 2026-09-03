"""
DeFi service — orchestrates an agent lending request end to end.

Mirrors `TradingService`: it resolves and authenticates the agent, re-reads the
venue independently, values the operation in USD, and then either auto-executes
under the policy threshold or escalates to a human. Qt-free, so both editions
share it. The waiting and the allowance ledger come from `services/pending.py`,
which the trading lane uses too.

Three things differ from the trading lane, and each one is a decision rather
than an accident.

**The limit is on the position, not the day.** A daily volume cap bounds how
fast an agent deploys, not how much it has at risk: $500 a day for twenty days
trips nothing and leaves $10,000 exposed. So the reservation ledger here is
checked against `max_total_deployed_usd`, and what counts as already-committed
is read from the chain rather than accumulated locally. Yield accrues, so a
running deposits-minus-withdrawals total is wrong the moment it is written, and
it never sees a deposit made through Morpho's own interface with the same
wallet.

**Withdrawals are not money limits.** Taking a position back reduces risk, and
the proceeds cannot leave the wallet without passing the x402 or trading lane.
They count against the daily operation ceiling and nothing else. A breached
exposure limit stops new deposits; it never forces an unwind.

**Failure has two kinds.** A venue that cannot free an amount today may manage
tomorrow; asking for more than the position holds will never work. The first is
worth resending unchanged and the second is not, so they are reported
differently rather than both arriving as "failed".
"""

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional, TYPE_CHECKING

from web3.exceptions import Web3RPCError

from ..models.defi import PositionRequest, PositionQuote, PositionResult
from ..models.transaction import STATUS_REJECTED, Transaction
from ..networks import NETWORKS, DEFAULT_NETWORK, TOKENS, get_morpho
from ..wallet.ledger import LedgerError
from .dex import to_atomic
from .morpho import (
    InsufficientLiquidity, MarketVenue, MorphoAdapter, MorphoError, VaultVenue,
)
from .pending import PendingQueue, Reservations
from .server import server_stats

if TYPE_CHECKING:
    from ..models.agent import Agent

logger = logging.getLogger(__name__)

# How long a position request may wait for a human before it is abandoned.
# Matches the trading lane: a share price moves, and an approval given against
# numbers from hours ago is not the operation anyone looked at.
PENDING_POSITION_TTL_SECONDS = 900  # 15 minutes

# How many finished results to keep for agents to poll.
MAX_RESOLVED_POSITIONS = 500

# How many requests one agent may have waiting at once. Per-agent, so one noisy
# agent cannot crowd another's out of the queue or bury the person approving.
MAX_PENDING_POSITIONS_PER_AGENT = 20

# /venues describes every curated venue, and each description is its own
# handful of chain reads (position, withdrawable capacity) that do not depend
# on one another - so they run concurrently rather than one after another.
# Bounded, not "however many venues exist": a wide-open policy can resolve to
# far more venues than the ~4-7 curated ones, and an RPC node has its own
# limit on how many requests it will serve in parallel before every one of
# them slows down together.
VENUE_DESCRIBE_WORKERS = 8

# Decimals a market's supply shares are scaled by: none. Unlike a vault share,
# which is an ERC-20 with a `decimals()` of its own, a market share is a bare
# integer the singleton keeps against the market's totals. Named rather than
# written as a literal 0, because a zero passed to a decimals argument reads
# like an oversight.
MARKET_SHARE_DECIMALS = 0

# How long a resolved venue list stays usable before it is read again.
#
# Resolving costs a few dozen chain calls - every vault, its adapters, each
# market's parameters, cap and symbol - so doing it inside a request would put
# that on the critical path of every deposit. A curator adding a market is not
# an event anyone needs to see within seconds, and the cached list can only ever
# be a subset of what is permitted, never a superset: a venue that leaves the
# curated set is caught at execution because the curator is re-checked then.
# An hour, not five minutes: a curator change is rare (see MorphoAdapter's
# note that the role has no timelock, so it is a real if unusual event, not
# a never), and the background refresh in core/vault.py keeps this warm
# without any request ever paying to resolve it cold.
VENUE_CACHE_SECONDS = 3600


@dataclass
class PendingPosition:
    """The request a pending entry is holding."""
    request: "PositionRequest"
    quote: "PositionQuote"


class DefiService:
    """Handles lending requests from agents: read, value, gate, execute."""

    def __init__(self):
        self._policy_store = None
        self._wallet_provider: Optional[Callable] = None
        self._wallet_status_checker: Optional[Callable] = None
        self._auth_verifier: Optional[Callable] = None
        self._rpc_resolver: Optional[Callable] = None
        self._on_approval_needed: Optional[Callable] = None
        self._on_activity: Optional[Callable] = None
        self._on_position_changed: Optional[Callable] = None
        self._on_hardware_sign_tx: Optional[Callable] = None
        self._queue = PendingQueue(
            ttl_seconds=PENDING_POSITION_TTL_SECONDS,
            max_resolved=MAX_RESOLVED_POSITIONS,
            max_per_owner=MAX_PENDING_POSITIONS_PER_AGENT,
            on_expire=self._on_request_expired)
        # USD promised to operations in flight or awaiting approval but not yet
        # visible in the on-chain position.
        self._exposure = Reservations()
        # chain_id -> (rpc_url the adapter was built for, adapter)
        self._adapters: dict[int, tuple[str, MorphoAdapter]] = {}
        # chain_id -> (resolved_at, curator fingerprint, venues)
        self._venue_cache: dict[int, tuple[float, tuple, list]] = {}

    # ---- wiring (mirrors TradingService) ---------------------------------

    def set_stores(self, policy_store):
        self._policy_store = policy_store

    def set_wallet_provider(self, provider: Callable):
        self._wallet_provider = provider

    def set_wallet_status_checker(self, checker: Callable):
        self._wallet_status_checker = checker

    def set_auth_verifier(self, verifier: Callable):
        """Inject SigningService.verify_agent_signature so /position
        authenticates the same way /sign and /trade do."""
        self._auth_verifier = verifier

    def set_rpc_resolver(self, resolver: Optional[Callable]):
        self._rpc_resolver = resolver

    def set_callbacks(self, on_approval_needed: Optional[Callable] = None,
                      on_activity: Optional[Callable] = None,
                      on_position_changed: Optional[Callable] = None):
        self._on_approval_needed = on_approval_needed
        self._on_activity = on_activity
        self._on_position_changed = on_position_changed

    def set_hardware_tx_signer(self, handler: Optional[Callable]):
        """Inject the hardware-wallet transaction signer (the GUI provides it).

        Separate from set_callbacks() because that setter overwrites all of its
        arguments, and the Ledger handler is registered at a different point in
        startup.
        """
        self._on_hardware_sign_tx = handler

    # ---- read-through views onto the collaborators -----------------------

    @property
    def _pending(self) -> dict:
        return self._queue._pending

    @property
    def _resolved(self) -> dict:
        return self._queue._resolved

    @property
    def _reservations(self) -> dict:
        return self._exposure._entries

    # ---- plumbing --------------------------------------------------------

    def _activity(self, message: str, is_error: bool = False,
                  detail: str = None):
        if self._on_activity:
            try:
                self._on_activity(message, is_error, detail)
            except Exception:
                logger.exception("defi activity callback failed")

    def _position_changed(self, address: str):
        if self._on_position_changed:
            try:
                self._on_position_changed(address)
            except Exception:
                logger.exception("position changed callback failed")

    def _rpc_url(self, chain_id: int) -> Optional[str]:
        """The endpoint to read and send through: the user's if they set one.

        A resolver failure falls back to the built-in endpoint rather than
        refusing - the setting is a preference, and an operation refused because
        a settings read raised would be a worse answer than one sent to the
        default.
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

    def _adapter(self, chain_id: int) -> MorphoAdapter:
        """A cached MorphoAdapter for the chain, built from the network config.

        Cached against the endpoint it was built for, so changing the RPC in
        settings takes effect on the next request instead of being served by an
        adapter still pointed at the old node for the life of the process.
        """
        config = get_morpho(chain_id)
        if config is None:
            raise MorphoError(f"no Morpho deployment configured for chain {chain_id}")
        rpc_url = self._rpc_url(chain_id)
        cached = self._adapters.get(chain_id)
        if cached is not None and cached[0] == rpc_url:
            return cached[1]
        adapter = MorphoAdapter(rpc_url, config)
        self._adapters[chain_id] = (rpc_url, adapter)
        return adapter

    # ---- venues ----------------------------------------------------------

    def venues(self, chain_id: int, curators: list[str],
               force: bool = False) -> list:
        """Every venue the given curators stand behind, cached for an hour.

        The cache key includes the curator list, so editing the policy takes
        effect at once rather than after the timeout.
        """
        cached = self._cached_venues(chain_id, curators)
        if cached is not None and not force:
            return cached
        resolved = self._adapter(chain_id).resolve_venues(curators)
        fingerprint = self._fingerprint(curators)
        self._venue_cache[chain_id] = (time.monotonic(), fingerprint, resolved)
        return resolved

    @staticmethod
    def _fingerprint(curators: list[str]) -> tuple:
        return tuple(sorted(c.lower() for c in curators))

    def _cached_venues(self, chain_id: int, curators: list[str]) -> Optional[list]:
        """The venue list if a fresh-enough resolve is already cached, else
        None. Never resolves - a caller on the request path that would rather
        report "unknown" than pay for a cold chain scan uses this instead of
        `venues()`. The background warm-up in core/vault.py is what keeps this
        populated; see VENUE_CACHE_SECONDS.
        """
        cached = self._venue_cache.get(chain_id)
        if (cached is not None and cached[1] == self._fingerprint(curators)
                and time.monotonic() - cached[0] < VENUE_CACHE_SECONDS):
            return cached[2]
        return None

    def _find_venue(self, chain_id: int, rules, venue_id: str, venue_kind: str):
        """The venue the request names, or None if it may not be used.

        Restricted, it has to be one a trusted curator stands behind. Matched
        case-insensitively: an address arrives however the agent spelled it, and
        `curator()` answers checksummed.

        Unrestricted, any venue on the chain is fair game, so the answer is read
        straight from the address rather than looked up. Still a real read - an
        address that does not answer like a vault, or a market id the singleton
        does not know, resolves to nothing rather than to a broken venue.
        """
        wanted = venue_id.lower()
        if getattr(rules, "restrict_to_steakhouse", True):
            for venue in self.venues(chain_id, rules.morpho_curators):
                if venue.kind == venue_kind and venue.id.lower() == wanted:
                    return venue
            return None
        return self._venue_from_chain(chain_id, venue_id, venue_kind)

    def _venue_from_chain(self, chain_id: int, venue_id: str, venue_kind: str):
        """Build a venue from its bare address or market id. None if unreadable."""
        adapter = self._adapter(chain_id)
        try:
            if venue_kind == "vault":
                return adapter.vault_venue(venue_id)
            key = bytes.fromhex(venue_id.replace("0x", ""))
            params = adapter.market_params(key)
            if int(params[0], 16) == 0:
                return None  # the singleton does not know this id
            return MarketVenue(
                params=params, market_key=key,
                loan_token=params[0], collateral_token=params[1],
                collateral_symbol=adapter._symbol_or_address(params[1]),
                lltv=int(params[4]),
                loan_decimals=int(adapter._erc20(params[0]).functions.decimals().call()),
                endorsed_by="")
        except (MorphoError, ValueError) as e:
            logger.warning("could not read venue %s: %s", venue_id, e)
            return None

    # ---- exposure --------------------------------------------------------

    def _usdg_address(self, chain_id: int) -> Optional[str]:
        return TOKENS["USDG"].addresses.get(chain_id)

    def _remembered_venues(self, chain_id: int, agent) -> list:
        """Venues this agent has supplied to before, rebuilt from their ids.

        Unreadable ones are skipped rather than raising: a venue that has since
        broken should not stop the exposure sum, and its contribution is zero
        anyway.
        """
        out = []
        for venue_id in getattr(agent, "defi_venues", []) or []:
            kind = "vault" if len(venue_id) <= 42 else "market"
            venue = self._venue_from_chain(chain_id, venue_id, kind)
            if venue is not None:
                out.append(venue)
        return out

    def deployed_usd(self, chain_id: int, curators: list[str], owner: str,
                     extra_venues: Optional[list] = None,
                     cache_only: bool = False) -> Optional[float]:
        """What this address currently has deployed, in USD, read from chain.

        USDG is the only asset counted, and it is treated as $1 - the same
        assumption `pricing.value_base_leg` already makes. A venue denominated
        in anything else contributes nothing, which is deliberately conservative:
        a limit denominated in something Vault cannot price independently is a
        limit an attacker can move.

        Returns None if the chain could not be read. That is unknown, not zero -
        treating an unreadable position as an empty one would let every limit
        through during an RPC outage.

        `cache_only` skips a cold venue resolve rather than paying for one -
        for a caller like /mandate that needs a fast answer over an exact one.
        A cache miss reports None (unknown), same as an unreadable chain;
        the background warm-up in core/vault.py is what keeps the cache from
        missing in the first place.
        """
        adapter = self._adapter(chain_id)
        usdg = (self._usdg_address(chain_id) or "").lower()
        if cache_only:
            venue_list = self._cached_venues(chain_id, curators)
            if venue_list is None:
                return None
        else:
            venue_list = self.venues(chain_id, curators)

        seen: set[str] = set()
        priced: list = []
        for venue in list(venue_list) + list(extra_venues or []):
            # A venue reachable both ways must not be counted twice.
            if venue.id.lower() in seen:
                continue
            seen.add(venue.id.lower())
            if isinstance(venue, VaultVenue) and venue.asset.lower() == usdg:
                priced.append(venue)
            elif isinstance(venue, MarketVenue) and venue.loan_token.lower() == usdg:
                priced.append(venue)

        # One position read per USDG-denominated venue, independent of the
        # others - run concurrently rather than one after another. Any single
        # unreadable venue makes the whole total unknown (see docstring), so
        # the first MorphoError found wins regardless of which worker hit it.
        def position(venue) -> float:
            if isinstance(venue, VaultVenue):
                _shares, assets = adapter.vault_position(venue.address, owner)
                return assets / 10 ** venue.asset_decimals
            assets = adapter.market_position(venue.market_key, owner)
            return assets / 10 ** venue.loan_decimals

        if not priced:
            return 0.0
        try:
            with ThreadPoolExecutor(
                    max_workers=min(VENUE_DESCRIBE_WORKERS, len(priced))) as pool:
                return sum(pool.map(position, priced))
        except MorphoError as e:
            logger.warning("could not read deployed position for %s: %s", owner, e)
            return None

    def liquid_usdg(self, chain_id: int, owner: str) -> Optional[float]:
        """USDG sitting in the wallet, unspent. None if it could not be read."""
        usdg = self._usdg_address(chain_id)
        if not usdg:
            return None
        try:
            adapter = self._adapter(chain_id)
            raw = adapter._erc20(usdg).functions.balanceOf(
                adapter.w3.to_checksum_address(owner)).call()
        except Exception as e:
            logger.warning("could not read USDG balance for %s: %s", owner, e)
            return None
        return int(raw) / 10 ** TOKENS["USDG"].decimals

    def reserved_exposure_for(self, agent_id: str) -> float:
        """USD this agent has promised to operations not yet visible on chain.

        Published for the same reason the trading lane publishes its reserved
        volume: without it an agent with deposits queued is told it has room the
        next request will refuse.
        """
        return self._exposure.reserved_for(agent_id)

    # ---- finished requests -----------------------------------------------

    @staticmethod
    def _count_refusal(result: dict) -> dict:
        server_stats.trade_rejected += 1
        return result

    @staticmethod
    def _count_outcome(result: dict) -> None:
        """Add a finished operation to the session counters behind /status.

        Only terminal states count; `pending` is waiting on a person.
        """
        status = result.get("status")
        if status == "executed":
            server_stats.traded += 1
        elif status in ("rejected", "failed"):
            server_stats.trade_rejected += 1

    def _remember_result(self, request_id: str, result: dict) -> dict:
        self._count_outcome(result)
        return self._queue.remember(request_id, result)

    def _on_request_expired(self, entry) -> None:
        """Auto-reject one request that waited too long. Called by the queue."""
        request_id = entry.key
        held = entry.payload
        self._exposure.release(request_id)
        reason = (f"Expired: not approved within "
                  f"{PENDING_POSITION_TTL_SECONDS // 60} minutes")
        tx = self._create_lend_transaction(held.request, held.quote)
        if tx is not None and self._policy_store:
            tx.status = STATUS_REJECTED
            tx.reject_reason = reason
            self._policy_store.update_transaction(tx)
        self._remember_result(request_id, PositionResult.rejected(
            request_id, reason, held.quote, code="EXPIRED").to_dict())
        self._activity(f"Position request {request_id} expired without approval",
                       is_error=True)

    def _expire_pending(self) -> None:
        self._queue.sweep()

    # ---- quote -----------------------------------------------------------

    def prepare_position(self, request: PositionRequest, venue) -> PositionQuote:
        """Read the venue and work out exactly what this request would do.

        Raises MorphoError/ValueError with a reason if it cannot be quoted.

        Share counts come from the venue's own preview functions, never from
        arithmetic here, so this module's idea of a conversion cannot disagree
        with the vault's.
        """
        ok, reason = request.validate_shape()
        if not ok:
            raise ValueError(reason)

        adapter = self._adapter(request.chain_id)
        owner = request.wallet_address
        if not owner:
            raise ValueError("no wallet address for this request")

        if isinstance(venue, VaultVenue):
            return self._quote_vault(adapter, request, venue, owner)
        if isinstance(venue, MarketVenue):
            return self._quote_market(adapter, request, venue, owner)
        raise ValueError(f"unsupported venue kind: {getattr(venue, 'kind', venue)}")

    def _quote_vault(self, adapter: MorphoAdapter, request: PositionRequest,
                     venue: VaultVenue, owner: str) -> PositionQuote:
        held_shares, held_assets = adapter.vault_position(venue.address, owner)

        if request.is_supply:
            assets = to_atomic(request.amount, venue.asset_decimals)
            if assets <= 0:
                raise ValueError("amount rounds to nothing at this token's precision")
            shares = adapter.preview_deposit(venue.address, assets)
            spender = venue.address
        elif request.withdraw_all:
            # Shares, not assets. An asset-denominated exit is quoted a block
            # before it settles and leaves dust behind when the price moves.
            shares = held_shares
            if shares <= 0:
                raise ValueError("there is no position in this vault to withdraw")
            assets = adapter.preview_redeem(venue.address, shares)
            spender = None
        elif request.by_shares:
            # A stated number of shares. Vault shares are an ERC-20, so the
            # amount scales by the share's own decimals - 18, not the asset's 6.
            shares = to_atomic(request.amount, venue.share_decimals)
            if shares <= 0:
                raise ValueError("amount rounds to nothing at this share's precision")
            if shares > held_shares:
                raise ValueError("that is more shares than the position holds")
            assets = adapter.preview_redeem(venue.address, shares)
            spender = None
        else:
            assets = to_atomic(request.amount, venue.asset_decimals)
            if assets <= 0:
                raise ValueError("amount rounds to nothing at this token's precision")
            shares = adapter.preview_withdraw(venue.address, assets)
            spender = None

        approvals = 0
        if spender is not None:
            approvals = len(adapter.approval_steps(
                venue.asset, owner, assets, spender=spender))

        return PositionQuote(
            venue=venue.address, venue_kind="vault", protocol=request.protocol,
            action=request.action,
            asset=venue.asset, asset_decimals=venue.asset_decimals,
            share_decimals=venue.share_decimals,
            assets=assets, shares=shares, by_shares=request.by_shares,
            notional_usd=self._value_usd(venue.asset, assets,
                                         venue.asset_decimals, request.chain_id),
            current_position_assets=held_assets,
            venue_withdrawable=self._withdrawable_or_none(adapter, venue.address),
            asset_symbol=self._asset_symbol(adapter, venue.asset),
            venue_name=venue.name, curator=venue.curator,
            approvals_needed=approvals,
        )

    def _quote_market(self, adapter: MorphoAdapter, request: PositionRequest,
                      venue: MarketVenue, owner: str) -> PositionQuote:
        held_shares, held_assets = adapter.market_position_full(
            venue.market_key, owner)
        shares = 0

        if request.is_supply:
            assets = to_atomic(request.amount, venue.loan_decimals)
            if assets <= 0:
                raise ValueError("amount rounds to nothing at this token's precision")
            # One approval to the singleton covers every market on the chain.
            approvals = len(adapter.approval_steps(
                venue.loan_token, owner, assets, spender=adapter.morpho_address))
        elif request.withdraw_all:
            # Shares, for the same reason a vault exit uses them: the position
            # accrues interest inside the withdrawal, so the asset figure quoted
            # here is already behind by the time it settles.
            shares = held_shares
            if shares <= 0:
                raise ValueError("there is no position in this market to withdraw")
            assets = held_assets
            approvals = 0
        elif request.by_shares:
            # Market shares are not a token and carry no decimals - Morpho
            # scales them against the market's own totals - so the amount is
            # read as a plain integer count rather than scaled by anything.
            shares = to_atomic(request.amount, MARKET_SHARE_DECIMALS)
            if shares <= 0:
                raise ValueError("amount rounds to nothing as a share count")
            if shares > held_shares:
                raise ValueError("that is more shares than the position holds")
            # Proportional to what the whole position is worth, which is the
            # same conversion the singleton will do, on totals a block older.
            assets = held_assets * shares // held_shares if held_shares else 0
            approvals = 0
        else:
            assets = to_atomic(request.amount, venue.loan_decimals)
            if assets <= 0:
                raise ValueError("amount rounds to nothing at this token's precision")
            approvals = 0

        return PositionQuote(
            venue=venue.id, venue_kind="market", protocol=request.protocol,
            action=request.action,
            asset=venue.loan_token, asset_decimals=venue.loan_decimals,
            # A market position is not tokenised: its shares are a bare integer
            # the singleton keeps, with no decimals of their own. Zero says so,
            # rather than borrowing the asset's scale and implying a precision
            # the number does not have.
            share_decimals=MARKET_SHARE_DECIMALS,
            assets=assets, shares=shares, by_shares=request.by_shares,
            notional_usd=self._value_usd(venue.loan_token, assets,
                                         venue.loan_decimals, request.chain_id),
            current_position_assets=held_assets,
            venue_withdrawable=self._market_available_or_none(adapter, venue),
            asset_symbol=self._asset_symbol(adapter, venue.loan_token),
            venue_name=f"{venue.collateral_symbol} / {venue.lltv_percent:.1f}%",
            approvals_needed=approvals,
        )

    def _value_usd(self, asset: str, amount_atomic: int, decimals: int,
                   chain_id: int) -> Optional[float]:
        """Value an amount in USD, or None if it cannot be valued.

        Only USDG, and only at $1. Any other asset is unvaluable here, which
        escalates to a human rather than proceeding - the same rule the trading
        lane applies to a leg it cannot trust-price.
        """
        usdg = self._usdg_address(chain_id)
        if not usdg or asset.lower() != usdg.lower():
            return None
        return amount_atomic / 10 ** decimals

    @staticmethod
    def _withdrawable_or_none(adapter: MorphoAdapter, vault: str) -> Optional[int]:
        """Best-effort capacity read; None rather than raising.

        Informational only - shown to the caller so it can size a withdrawal -
        so a failure here must not refuse an otherwise valid operation.
        """
        try:
            return adapter.withdrawable(vault)
        except MorphoError as e:
            logger.debug("withdrawable unavailable for %s: %s", vault, e)
            return None

    @staticmethod
    def _market_available_or_none(adapter: MorphoAdapter,
                                  venue: MarketVenue) -> Optional[int]:
        try:
            return adapter.market_available(venue.market_key)
        except MorphoError:
            return None

    @staticmethod
    def _asset_symbol(adapter: MorphoAdapter, asset: str) -> Optional[str]:
        try:
            return adapter._erc20(asset).functions.symbol().call()
        except Exception:
            return None

    # ---- request handling ------------------------------------------------

    def handle_position_request(self, agent_id: str, data: dict,
                                signature: Optional[str] = None) -> dict:
        """Entry point for a lending request. Returns a JSON-able dict."""
        agent = (self._policy_store.get_agent_by_id(agent_id)
                 if self._policy_store else None)
        if agent is None:
            return {"status": "error", "code": "UNKNOWN_AGENT",
                    "error": f"No agent with id {agent_id}"}

        if self._auth_verifier is not None:
            auth_err = self._authenticate(agent, agent_id, signature, data)
            if auth_err is not None:
                return auth_err

        # Who the caller is and which address it may use are settled by
        # authentication, not by the payload. Naming either is refused outright
        # rather than quietly ignored, so a caller that tries finds out.
        claimed = [f for f in ("agent_id", "wallet_address", "receiver")
                   if f in data]
        if claimed:
            return {
                "status": "error", "code": "FIELD_NOT_PERMITTED",
                "error": (f"Position requests may not set {', '.join(claimed)}. "
                          "Vault uses the agent and address the credentials "
                          "belong to."),
            }

        data = dict(data)
        data.setdefault("chain_id", DEFAULT_NETWORK)
        try:
            request = PositionRequest.from_dict(data)
        except (KeyError, ValueError, TypeError) as e:
            return {"status": "error", "code": "BAD_REQUEST", "error": str(e)}

        request.agent_id = agent.id
        request.wallet_address = agent.wallet_address
        if not request.wallet_address:
            return {"status": "error", "code": "AGENT_NOT_COMMISSIONED",
                    "error": "Agent has no commissioned wallet address"}

        unsignable = self._signer_unavailable(request)
        if unsignable is not None:
            self._activity(f"Position rejected for {agent_id}: {unsignable['error']}",
                           is_error=True)
            return unsignable

        rules, error = self._rules_for(agent)
        if error is not None:
            return self._count_refusal(error)

        venue = self._find_venue(request.chain_id, rules,
                                 request.venue, request.venue_kind)
        if venue is None:
            reason = ("That venue is not one your policy permits. Ask for the "
                      "permitted venues before choosing one.")
            self._activity(f"Position rejected for {agent_id}: unknown venue "
                           f"{request.venue}", is_error=True)
            return self._count_refusal(PositionResult.rejected(
                request.id, reason, code="VENUE_NOT_PERMITTED").to_dict())

        try:
            quote = self.prepare_position(request, venue)
        except (MorphoError, ValueError) as e:
            self._activity(f"Position rejected for {agent_id}: {e}", is_error=True)
            return self._count_refusal(
                PositionResult.rejected(request.id, str(e)).to_dict())

        self._expire_pending()

        decision = self._evaluate_policy(agent, request, quote, rules)
        if decision["action"] == "reject":
            self._activity(f"Position rejected for {agent_id}: {decision['reason']}",
                           is_error=True)
            return self._count_refusal(PositionResult.rejected(
                request.id, decision["reason"], quote,
                code=decision.get("code")).to_dict())

        if decision["action"] == "auto":
            tx = self._create_lend_transaction(request, quote, auto_approved=True)
            result = self.execute_position(request, quote, auto_approved=True)
            try:
                self._settle_transaction(tx, result)
                self._record_outcome(request, quote, result)
            except Exception:
                logger.exception("failed to record outcome of position %s", request.id)
            return result

        if self._queue.is_full_for(agent.id):
            self._exposure.release(request.id)
            reason = (f"{MAX_PENDING_POSITIONS_PER_AGENT} requests from this agent "
                      f"are already waiting for approval")
            self._activity(f"Position rejected for {agent_id}: {reason}", is_error=True)
            return self._count_refusal(PositionResult.rejected(
                request.id, reason, quote, code="TOO_MANY_PENDING").to_dict())

        self._queue.add(request.id, agent.id,
                        PendingPosition(request=request, quote=quote))

        amount = self._format_amount(quote)
        verb = "Supply" if request.is_supply else "Withdraw"
        brief = (f"{verb} pending: {agent_id} {amount} "
                 f"{quote.asset_symbol or ''} → {quote.venue_name or quote.venue}")
        self._activity(brief, detail=f"{brief} (venue {quote.venue})")
        if self._on_approval_needed:
            try:
                self._on_approval_needed(request, quote)
            except Exception:
                logger.exception("approval callback failed")
        return PositionResult.pending(request.id, quote).to_dict()

    def mandate_summary(self, agent) -> dict:
        """The Morpho limits for /mandate, so an agent can check them up front.

        Same numbers /venues reports, without the venue list - an agent asking
        "what am I allowed to do" at the start of a task should not have to pay
        for a venue resolve to find out.
        """
        rules = getattr(
            self._policy_store.get_policy(agent.policy_id), "defi_rules", None
        ) if (self._policy_store and agent.policy_id) else None
        if rules is None or not rules.enabled:
            return {"enabled": False}

        chain_id = DEFAULT_NETWORK
        owner = agent.wallet_address
        deployed = liquid = limit = None
        if owner:
            # cache_only: /mandate must answer fast, so a cold curator scan
            # is skipped rather than paid for here - the background warm-up
            # in core/vault.py is what keeps the cache from missing. A miss
            # reports deployed_usd (and therefore exposure_limit_usd) as
            # unknown rather than blocking the request on a chain scan.
            deployed = self.deployed_usd(
                chain_id, rules.morpho_curators, owner,
                extra_venues=self._remembered_venues(chain_id, agent),
                cache_only=True)
            liquid = self.liquid_usdg(chain_id, owner)
            if deployed is not None and liquid is not None:
                limit = rules.exposure_limit_usd(liquid + deployed)

        if agent.defi_reset_due():
            self._reset_ops_if_due(agent)

        return {
            "enabled": True,
            "restricted_to_steakhouse": rules.restrict_to_steakhouse,
            "max_deposit_usd": rules.max_deposit_usd,
            "max_total_deployed_usd": rules.max_total_deployed_usd,
            "max_deployed_percent": rules.max_deployed_percent,
            "auto_approve_below_usd": rules.auto_approve_below_usd,
            "max_ops_per_day": rules.max_ops_per_day,
            "ops_today": agent.defi_ops_today,
            "ops_remaining_today": max(
                0, rules.max_ops_per_day - agent.defi_ops_today),
            "deployed_usd": deployed,
            "liquid_usdg": liquid,
            "exposure_limit_usd": limit,
            # Counts requests still awaiting approval, so an agent paces against
            # what it can actually spend rather than against the ceiling.
            "remaining_deployable_usd": (
                max(0.0, limit - deployed - self.reserved_exposure_for(agent.id))
                if limit is not None and deployed is not None else None),
        }

    def handle_venues_request(self, agent_id: str,
                              signature: Optional[str] = None) -> dict:
        """What this agent may actually deposit into, and what it holds there.

        Published because the alternative is an agent finding out by being
        refused. There are 124 markets on this chain and four are curated; an
        agent guessing addresses would be wrong almost every time, and each
        wrong guess costs a round trip and reads as a policy failure.

        Authenticated like everything else - the answer depends on the caller's
        policy, so it is not public.
        """
        agent = (self._policy_store.get_agent_by_id(agent_id)
                 if self._policy_store else None)
        if agent is None:
            return {"status": "error", "code": "UNKNOWN_AGENT",
                    "error": f"No agent with id {agent_id}"}

        if self._auth_verifier is not None:
            auth_err = self._authenticate(agent, agent_id, signature, {})
            if auth_err is not None:
                return auth_err

        rules, error = self._rules_for(agent)
        if error is not None:
            return {"status": "error", "code": error.get("code", "DEFI_DISABLED"),
                    "error": error.get("reason", "Morpho lending is not available")}

        chain_id = DEFAULT_NETWORK
        owner = agent.wallet_address
        adapter = self._adapter(chain_id)

        available = (self.venues(chain_id, rules.morpho_curators)
                     if rules.restrict_to_steakhouse
                     else self._remembered_venues(chain_id, agent))

        # Each description is its own handful of independent chain reads
        # (position, withdrawable capacity) - run them concurrently rather
        # than paying their latency one venue at a time. A worker's own
        # MorphoError must not sink the others', so each is caught here
        # rather than left to unwind the pool.
        def describe(venue):
            try:
                return self._describe_venue(adapter, venue, owner)
            except MorphoError as e:
                logger.warning("could not describe venue %s: %s", venue.id, e)
                return None

        if available:
            with ThreadPoolExecutor(
                    max_workers=min(VENUE_DESCRIBE_WORKERS, len(available))) as pool:
                listed = [d for d in pool.map(describe, available) if d is not None]
        else:
            listed = []

        deployed = self.deployed_usd(
            chain_id, rules.morpho_curators, owner,
            extra_venues=self._remembered_venues(chain_id, agent))
        liquid = self.liquid_usdg(chain_id, owner)
        limit = (rules.exposure_limit_usd(liquid + deployed)
                 if deployed is not None and liquid is not None else None)

        return {
            "status": "ok",
            # Unrestricted, the list is what this agent has used rather than
            # what it may use - there is no enumerable "everything on Morpho".
            "restricted": rules.restrict_to_steakhouse,
            # No protocol key at this level: the list spans whatever protocols
            # are permitted, and each venue names its own. One here would be
            # wrong the moment there are two, and it is in the wire contract, so
            # it is cheaper to leave out now than to remove later.
            "venues": listed,
            "policy": {
                "enabled": rules.enabled,
                "max_deposit_usd": rules.max_deposit_usd,
                "max_total_deployed_usd": rules.max_total_deployed_usd,
                "max_deployed_percent": rules.max_deployed_percent,
                "auto_approve_below_usd": rules.auto_approve_below_usd,
                "max_ops_per_day": rules.max_ops_per_day,
                "ops_today": agent.defi_ops_today,
                # What is left, counting requests still awaiting approval.
                # Pacing against the raw limit instead would have the agent
                # discover the difference by being refused.
                "deployed_usd": deployed,
                "liquid_usdg": liquid,
                "exposure_limit_usd": limit,
                "remaining_deployable_usd": (
                    max(0.0, limit - deployed - self.reserved_exposure_for(agent.id))
                    if limit is not None and deployed is not None else None),
            },
        }

    def _describe_venue(self, adapter: MorphoAdapter, venue, owner: str) -> dict:
        """One venue, as an agent needs to see it.

        No `withdrawable_now` for a vault: `MorphoAdapter.withdrawable()` is a
        nested markets x holders loop of chain calls, not O(1), and doing that
        once per curated vault is what made `/venues` take 60s+ end to end.
        The number is also stale within seconds regardless - it moves with
        other people's borrowing. An agent that needs it for a real decision
        gets it fresh, cheaply, at the one place it actually matters: the
        withdraw quote (`PositionQuote.venue_withdrawable`, set in
        `_quote_vault`/`_quote_market`) - a request that can't be filled today
        comes back as a retryable INSUFFICIENT_LIQUIDITY rather than silently
        wrong. Markets keep it here because `market_available()` is one plain
        read, not the expensive path.
        """
        if isinstance(venue, VaultVenue):
            _shares, held = adapter.vault_position(venue.address, owner) if owner \
                else (0, 0)
            return {
                "kind": "vault", "protocol": "morpho", "venue": venue.address,
                "name": venue.name, "symbol": venue.symbol,
                "asset": venue.asset, "asset_decimals": venue.asset_decimals,
                "curator": venue.curator,
                "total_assets": venue.total_assets,
                "your_position_assets": held,
            }
        held = adapter.market_position(venue.market_key, owner) if owner else 0
        return {
            "kind": "market", "protocol": "morpho", "venue": venue.id,
            "name": f"{venue.collateral_symbol} / {venue.lltv_percent:.1f}%",
            "collateral": venue.collateral_token,
            "collateral_symbol": venue.collateral_symbol,
            "asset": venue.loan_token, "asset_decimals": venue.loan_decimals,
            "lltv_percent": venue.lltv_percent,
            "endorsed_by": venue.endorsed_by,
            "your_position_assets": held,
            "withdrawable_now": self._market_available_or_none(adapter, venue),
        }

    def _create_lend_transaction(self, request: PositionRequest,
                                 quote: PositionQuote,
                                 auto_approved: bool = False):
        """Record the operation in history before it is attempted.

        Written first so a crash mid-flight leaves a trace rather than nothing,
        which is how the trading lane does it too.
        """
        if not self._policy_store:
            return None
        agent = self._policy_store.get_agent_by_id(request.agent_id)
        if not agent:
            logger.warning("agent not found for position %s", request.id)
            return None

        wallet_id = None
        if request.wallet_address and self._wallet_provider:
            wallet = self._wallet_provider(request.wallet_address)
            entry = (wallet.get_address_by_address(request.wallet_address)
                     if wallet else None)
            wallet_id = entry.id if entry else None

        tx = Transaction.create_lend(
            agent_id=agent.id, agent_name=agent.name, agent_code=agent.code,
            network=f"eip155:{request.chain_id}",
            action=request.action,
            venue=quote.venue,
            venue_name=quote.venue_name or quote.venue[:10],
            asset=quote.asset, symbol=quote.asset_symbol or "",
            amount_in=self._format_amount(quote),
            wallet_address=request.wallet_address, wallet_id=wallet_id,
            auto_approved=auto_approved,
        )
        self._policy_store.add_transaction(tx)
        return tx

    def _create_approve_transaction(self, request: PositionRequest, asset: str,
                                    symbol: str, spender: str, amount_atomic: int,
                                    auto_approved: bool = False):
        """Record one approval leg before it is attempted - same discipline as
        _create_lend_transaction, so a crash mid-flight still leaves a trace.

        Its own row, not folded into the supply/withdraw it enables: an
        approval is a real transaction with its own hash, and can settle even
        when the deposit it was for then fails.
        """
        if not self._policy_store:
            return None
        agent = self._policy_store.get_agent_by_id(request.agent_id)
        if not agent:
            logger.warning("agent not found for position %s", request.id)
            return None

        wallet_id = None
        if request.wallet_address and self._wallet_provider:
            wallet = self._wallet_provider(request.wallet_address)
            entry = (wallet.get_address_by_address(request.wallet_address)
                     if wallet else None)
            wallet_id = entry.id if entry else None

        tx = Transaction.create_approve(
            agent_id=agent.id, agent_name=agent.name, agent_code=agent.code,
            network=f"eip155:{request.chain_id}", asset=asset, symbol=symbol,
            spender=spender, amount=str(amount_atomic),
            wallet_address=request.wallet_address, wallet_id=wallet_id,
            auto_approved=auto_approved)
        self._policy_store.add_transaction(tx)
        return tx

    def _settle_transaction(self, tx, result: dict) -> None:
        """Mark a recorded operation with what actually happened."""
        if tx is None or not self._policy_store:
            return
        if result.get("status") == "executed":
            tx.mark_settled(result.get("tx_hash"))
        else:
            # A guard clause caught before execution (e.g. WALLET_LOCKED) uses
            # "error", not "reason" - both are read so that shape of failure
            # is not silently recorded as the generic fallback text.
            tx.mark_failed(result.get("reason") or result.get("error", "Operation failed"))
            # The hash belongs to the broadcast, not the outcome: something
            # that reverted still has one, and it is the only way to check
            # the chain rather than retry blind. This row is the
            # supply/withdraw's alone now - an approval's hash can no longer
            # land here, it settles its own row the moment it confirms.
            tx.tx_hash = result.get("tx_hash")
        self._policy_store.update_transaction(tx)

    @staticmethod
    def _format_amount(quote: PositionQuote) -> str:
        return f"{Decimal(quote.assets) / (10 ** quote.asset_decimals):f}"

    def _rules_for(self, agent) -> tuple:
        """(rules, error). The policy's DeFi ruleset, or why there isn't one."""
        if not self._policy_store or not agent.policy_id:
            return None, PositionResult.rejected(
                "", "Agent has no policy assigned", code="NO_POLICY").to_dict()
        policy = self._policy_store.get_policy(agent.policy_id)
        if not policy:
            return None, PositionResult.rejected(
                "", "Policy not found", code="NO_POLICY").to_dict()
        rules = getattr(policy, "defi_rules", None)
        if rules is None:
            return None, PositionResult.rejected(
                "", "Morpho lending is not configured for this policy",
                code="DEFI_NOT_CONFIGURED").to_dict()
        if not rules.enabled:
            return None, PositionResult.rejected(
                "", "Morpho lending is disabled for this policy",
                code="DEFI_DISABLED").to_dict()
        ok, reason = rules.validate()
        if not ok:
            return None, PositionResult.rejected(
                "", reason, code="POLICY_INCOMPLETE").to_dict()
        return rules, None

    def _signer_unavailable(self, request: PositionRequest) -> Optional[dict]:
        """Refuse now if nothing could sign this, whatever the policy says.

        A locked wallet and a hardware address with no driving app are the
        same shape of problem: without this check the request would read,
        pass policy, take an id, and only fail once a human had approved it -
        asking someone to authorise something that was never going to work,
        and leaving the caller to discover why only after that pointless
        round trip. Caught here instead, the caller finds out immediately and
        can retry once the wallet is unlocked.
        """
        if not self._wallet_provider or not request.wallet_address:
            return None
        wallet = self._wallet_provider(request.wallet_address)
        if not wallet:
            return {
                "status": "error", "code": "WALLET_LOCKED",
                "error": "Wallet is locked. Unlock it to run DeFi operations.",
            }
        entry = wallet.get_address_by_address(request.wallet_address)
        if entry is None or not entry.is_hardware:
            return None
        if self._on_hardware_sign_tx is not None:
            return None
        return {
            "status": "error", "code": "LEDGER_SIGN_NOT_AVAILABLE",
            "error": (f"This agent signs from a {entry.device_label} address, so "
                      "somebody has to confirm on the device before it can sign. "
                      "Nothing here is able to ask for that confirmation."),
        }

    def _authenticate(self, agent, agent_id: str, signature: Optional[str],
                      data: dict) -> Optional[dict]:
        """Agent status + signature checks, mirroring the other two lanes."""
        status = getattr(agent, "status", None)
        if status == "uncommissioned":
            return {"status": "error", "code": "AGENT_NOT_COMMISSIONED",
                    "error": "Agent not commissioned"}
        if status == "suspended":
            return {"status": "error", "code": "AGENT_SUSPENDED",
                    "error": "Agent suspended"}

        signed_fields = {"position": data}
        if getattr(agent, "auth_mode", "hmac") == "bearer":
            err = self._auth_verifier(agent, agent_id, signature, None, signed_fields)
            return None if not err else {"status": "error", "code": "AUTH_FAILED",
                                         "error": err}

        if not self._wallet_provider:
            return {"status": "error", "code": "NO_WALLET_PROVIDER",
                    "error": "Wallet provider not set"}
        wallet = self._wallet_provider(getattr(agent, "wallet_address", None))
        if not wallet:
            if self._wallet_status_checker and self._wallet_status_checker():
                return {"status": "error", "code": "WALLET_ADDRESS_NOT_FOUND",
                        "error": "Agent's wallet address is not available in the "
                                 "unlocked wallet."}
            # Locked wallet, HMAC not yet verifiable: answer AUTH_FAILED rather
            # than WALLET_LOCKED, so an unauthenticated caller cannot read the
            # wallet's lock state by probing.
            return {"status": "error", "code": "AUTH_FAILED",
                    "error": "Authentication failed"}
        err = self._auth_verifier(agent, agent_id, signature, wallet.data_key,
                                  signed_fields)
        return None if not err else {"status": "error", "code": "AUTH_FAILED",
                                     "error": err}

    # ---- policy ----------------------------------------------------------

    def _reset_ops_if_due(self, agent) -> None:
        """Renew the agent's daily operation count, under the ledger's lock and
        only after re-confirming the renewal is still due.

        Same reasoning as the trading lane's volume reset: the check that
        decided to call this ran outside the lock and can be stale, and a reset
        landing after another thread has already reset and committed would
        forget an operation already on-chain.
        """
        renewed = self._exposure.revalidate(
            still_due=agent.defi_reset_due,
            renew=agent.reset_daily_defi_ops)
        if renewed and self._policy_store:
            self._policy_store.update_agent(agent)

    def _evaluate_policy(self, agent: "Agent", request: PositionRequest,
                         quote: PositionQuote, rules,
                         exclude_request_id: str = None) -> dict:
        """Policy decision: reject / auto-execute / escalate.

        Order matters. Every check that can refuse for free runs before the
        exposure step, because that one also *takes* something - so an operation
        turned away for its size never has to hand a reservation back.

        **On any non-reject return for a supply, exposure is reserved**, unless
        the operation could not be valued, in which case it escalated before the
        reservation was taken. `quote.notional_usd is None` tells the two apart.
        Withdrawals never reserve.
        """
        if request.deadline is not None and time.time() >= request.deadline:
            return {"action": "reject", "reason": "Request deadline has passed",
                    "code": "DEADLINE_PASSED"}

        for name, value in (("max_deposit_usd", rules.max_deposit_usd),
                            ("max_total_deployed_usd", rules.max_total_deployed_usd),
                            ("max_deployed_percent", rules.max_deployed_percent)):
            if value is not None and not math.isfinite(value):
                return {"action": "reject", "code": "POLICY_UNUSABLE",
                        "reason": f"Policy limit {name} is not a usable number"}

        # The gas circuit breaker, and the only check a withdrawal meets. Free
        # to evaluate, so it runs before anything that reserves.
        if agent.defi_reset_due():
            self._reset_ops_if_due(agent)
        if agent.defi_ops_today >= rules.max_ops_per_day:
            return {
                "action": "reject", "code": "DAILY_OPS_EXCEEDED",
                "reason": (f"This agent has already made "
                           f"{agent.defi_ops_today} Morpho operations today, and "
                           f"the policy allows {rules.max_ops_per_day}"),
            }

        if request.is_withdraw:
            # Taking a position back reduces risk, and the proceeds cannot leave
            # the wallet without passing another lane. No money limit applies.
            if (rules.auto_approve_below_usd is not None
                    and quote.notional_usd is not None
                    and quote.notional_usd < rules.auto_approve_below_usd):
                return {"action": "auto", "reason": "Withdrawal under the "
                                                    "auto-approve threshold"}
            return {"action": "escalate", "reason": ""}

        notional = quote.notional_usd
        if notional is not None and notional > rules.max_deposit_usd:
            return {
                "action": "reject", "code": "PER_DEPOSIT_EXCEEDED",
                "reason": (f"Deposit ${notional:.2f} exceeds the per-deposit "
                           f"maximum of ${rules.max_deposit_usd:.2f}"),
            }

        # An unvaluable deposit escalates - after the free checks above, so an
        # outage cannot wave one past the operation ceiling.
        if notional is None:
            return {"action": "escalate",
                    "reason": "Unable to value this deposit in USD"}

        owner = request.wallet_address
        deployed = self.deployed_usd(
            request.chain_id, rules.morpho_curators, owner,
            extra_venues=self._remembered_venues(request.chain_id, agent))
        liquid = self.liquid_usdg(request.chain_id, owner)
        if deployed is None or liquid is None:
            # Unknown, not zero. Treating an unreadable position as empty would
            # let every limit through during an RPC outage. The same treatment
            # the trading lane gives an unreadable ETH balance.
            return {
                "action": "reject", "code": "EXPOSURE_UNREADABLE",
                "reason": ("Could not read this wallet's USDG position to check "
                           "the exposure limit. Nothing was deposited."),
            }

        limit = rules.exposure_limit_usd(total_usdg=liquid + deployed)
        shortfall = self._exposure.check_and_reserve(
            key=request.id, owner_id=agent.id, amount=notional, limit=limit,
            committed=deployed, exclude_key=exclude_request_id)
        if shortfall is not None:
            return {
                "action": "reject", "code": "EXPOSURE_EXCEEDED",
                "reason": (f"Depositing ${shortfall.amount:.2f} would exceed the "
                           f"exposure limit (${shortfall.remaining:.2f} of "
                           f"${shortfall.limit:.2f} remaining). Withdraw first, "
                           f"or raise the limit."),
            }

        # Past this point the exposure is spoken for, so every return below
        # leaves the caller responsible for releasing it if nothing happens.
        if (rules.auto_approve_below_usd is not None
                and notional < rules.auto_approve_below_usd):
            return {"action": "auto",
                    "reason": (f"Auto-approved (${notional:.2f} < "
                               f"${rules.auto_approve_below_usd:.2f})")}
        return {"action": "escalate", "reason": ""}

    # ---- pending: status / approve / reject ------------------------------

    def get_position_status(self, request_id: str) -> dict:
        """Status of a request awaiting approval (polled by the agent)."""
        self._expire_pending()
        entry = self._queue.get(request_id)
        if entry is not None:
            return {"status": "pending", "request_id": request_id,
                    "quote": entry.payload.quote.to_dict(),
                    "expires_in_seconds": self._queue.seconds_remaining(request_id)}
        if self._queue.has_resolved(request_id):
            return self._queue.resolved(request_id)
        return {"status": "error", "code": "REQUEST_NOT_FOUND",
                "error": f"No position request {request_id}"}

    def get_pending_positions(self) -> list:
        """Pending (request, quote) pairs, for the approval UI."""
        self._expire_pending()
        return [(e.payload.request, e.payload.quote) for e in self._queue.entries()]

    def approve_position(self, request_id: str) -> dict:
        """Approve a pending request, re-check it, and execute it.

        A request can sit here for minutes, so what was true when it arrived is
        re-established before anything moves: the venue is re-read, the curator
        is re-checked, and the policy is applied to the fresh numbers. The share
        price will have moved, and the agent's standing may have changed.
        """
        self._expire_pending()

        entry = self._queue.pop(request_id)
        if entry is None:
            if self._queue.has_resolved(request_id):
                return self._queue.resolved(request_id)
            return {"status": "error", "code": "REQUEST_NOT_FOUND",
                    "error": f"No pending position request {request_id}"}
        request, original = entry.payload.request, entry.payload.quote

        # Popped from pending, but nothing final exists yet: re-quoting and
        # execute_position below can take minutes (up to two sequential
        # on-chain waits). Without this, a status poll landing in that window
        # gets REQUEST_NOT_FOUND - indistinguishable from an id that never
        # existed - even though the request is very much alive and a human
        # just approved it. Every exit below (refuse, or the success path)
        # overwrites this the same way it always overwrote a resolved entry.
        self._queue.remember(request_id, {
            "status": "executing", "request_id": request_id,
            "quote": original.to_dict()})

        def refuse(reason, code="REQUEST_NO_LONGER_VALID"):
            self._exposure.release(request_id)
            tx = self._create_lend_transaction(request, original)
            if tx is not None and self._policy_store:
                tx.status = STATUS_REJECTED
                tx.reject_reason = reason
                self._policy_store.update_transaction(tx)
            result = PositionResult.rejected(request_id, reason, original,
                                             code=code).to_dict()
            self._remember_result(request_id, result)
            self._activity(f"Position {request_id} not executed: {reason}",
                           is_error=True)
            return result

        agent = (self._policy_store.get_agent_by_id(request.agent_id)
                 if self._policy_store else None)
        if agent is None:
            return refuse("Agent no longer exists", "UNKNOWN_AGENT")
        if getattr(agent, "status", None) == "suspended":
            return refuse("Agent is suspended", "AGENT_SUSPENDED")

        rules, error = self._rules_for(agent)
        if error is not None:
            return refuse(error.get("reason", "DeFi is no longer permitted"),
                          error.get("code", "DEFI_DISABLED"))

        # Re-resolve rather than trusting the venue found at intake: a curator
        # can be replaced under timelock, and a venue that has left the curated
        # set must not be reachable through a request that predates the change.
        venue = self._find_venue(request.chain_id, rules,
                                 request.venue, request.venue_kind)
        if venue is None:
            return refuse("That venue is no longer one your policy permits",
                          "VENUE_NOT_PERMITTED")

        try:
            fresh = self.prepare_position(request, venue)
        except (MorphoError, ValueError) as e:
            return refuse(f"Could not re-read the venue: {e}", "REQUOTE_FAILED")

        decision = self._evaluate_policy(agent, request, fresh, rules,
                                         exclude_request_id=request_id)
        if decision["action"] == "reject":
            return refuse(decision["reason"], decision.get("code", "POLICY_REJECTED"))

        # The intake reservation is replaced by one taken against the fresh
        # valuation. If the request cannot be valued now it never reached that
        # step, so the stale reservation has to go.
        if fresh.notional_usd is None:
            self._exposure.release(request_id)

        tx = self._create_lend_transaction(request, fresh)
        result = self.execute_position(request, fresh)
        self._remember_result(request_id, result)
        try:
            self._settle_transaction(tx, result)
            self._record_outcome(request, fresh, result)
        except Exception:
            logger.exception("failed to record outcome of position %s", request.id)
        return result

    def reject_position(self, request_id: str,
                        reason: str = "Rejected by user") -> dict:
        """Reject a pending request (called by the app/user)."""
        self._expire_pending()

        entry = self._queue.pop(request_id)
        if entry is None:
            if self._queue.has_resolved(request_id):
                return self._queue.resolved(request_id)
            return {"status": "error", "code": "REQUEST_NOT_FOUND",
                    "error": f"No pending position request {request_id}"}
        request = entry.payload.request

        self._exposure.release(request_id)
        tx = self._create_lend_transaction(request, entry.payload.quote)
        if tx is not None and self._policy_store:
            tx.status = STATUS_REJECTED
            tx.reject_reason = reason
            self._policy_store.update_transaction(tx)
        result = PositionResult.rejected(request_id, reason,
                                         entry.payload.quote).to_dict()
        self._remember_result(request_id, result)
        self._activity(f"Position {request_id} rejected: {reason}")
        return result

    def _record_outcome(self, request: PositionRequest, quote: PositionQuote,
                        result: dict) -> None:
        """Write down what happened. Called after the operation was submitted."""
        # Anything still reserved at this point never reached the send - refused,
        # or failed while the transaction was being built or signed, none of
        # which touch the network - so give it back. An operation that reached
        # the send has nothing left to release, which is what keeps its exposure
        # counted even when the receipt was never read.
        self._exposure.release(request.id)

        if result.get("status") == "executed" and request.wallet_address:
            self._position_changed(request.wallet_address)

    # ---- execution -------------------------------------------------------

    def execute_position(self, request: PositionRequest,
                         quote: PositionQuote,
                         auto_approved: bool = False) -> dict:
        """Approve if needed, rehearse, then sign and submit.

        Every transaction is simulated before it is sent. A revert found here
        costs nothing; one found on-chain costs gas and leaves the caller
        guessing.
        """
        if not self._wallet_provider:
            return {"status": "error", "code": "NO_WALLET",
                    "error": "No wallet provider"}

        adapter = self._adapter(request.chain_id)
        sender = request.wallet_address
        if not sender:
            return {"status": "error", "code": "NO_WALLET_ADDRESS",
                    "error": "No wallet address for this request"}

        # Approval can arrive long after intake and through a different route,
        # so the binding is re-checked immediately before a key is used.
        agent = (self._policy_store.get_agent_by_id(request.agent_id)
                 if self._policy_store else None)
        if agent is None or sender.lower() != (agent.wallet_address or "").lower():
            return {"status": "error", "code": "ADDRESS_NOT_COMMISSIONED",
                    "error": "Address does not match the agent's commissioned address"}

        wallet = self._wallet_provider(sender)
        if not wallet:
            return {"status": "error", "code": "WALLET_LOCKED",
                    "error": "Wallet is locked. Unlock it to run DeFi operations."}
        addr_entry = wallet.get_address_by_address(sender)
        if not addr_entry:
            return {"status": "error", "code": "ADDRESS_NOT_FOUND",
                    "error": f"Address {sender} not found in the unlocked wallet."}

        if addr_entry.is_hardware:
            if not self._on_hardware_sign_tx:
                return {"status": "error", "code": "LEDGER_SIGN_NOT_AVAILABLE",
                        "error": "Ledger signing is unavailable here."}

            def submit(tx: dict, description: str, before_send=None) -> str:
                raw = self._on_hardware_sign_tx(
                    tx, addr_entry.device_path, sender, description)
                if not raw:
                    raise MorphoError("No signed transaction returned from the device")
                if before_send:
                    before_send()
                return adapter.send_raw(raw) if hasattr(adapter, "send_raw") \
                    else adapter.w3.eth.send_raw_transaction(raw).hex()
        else:
            private_key = wallet.get_private_key(addr_entry.id).hex()

            def submit(tx: dict, description: str, before_send=None) -> str:
                signed = adapter.w3.eth.account.sign_transaction(tx, private_key)
                if before_send:
                    before_send()
                return adapter.w3.eth.send_raw_transaction(
                    signed.raw_transaction).hex()

        # Exposure counts from the moment a value-bearing transaction is sent,
        # and is never given back after that. Not from the receipt: a receipt
        # Vault could not read says nothing about whether the deposit landed,
        # and it usually did. The boundary is the instant *before* the network
        # call, after signing - committing after would leave a window where the
        # transaction is live and the spend is not yet recorded.
        #
        # An approval does not count. Granting an allowance moves no value.
        committed = False
        sent_hash = None
        # The settled approval's hash, if this attempt sent one - reported on
        # the PositionResult independently of whether the deposit/withdraw
        # then succeeded, so an agent polling /position/status can see it
        # even on a failure.
        approve_hash = None

        def on_broadcast() -> None:
            nonlocal committed
            if committed:
                return
            committed = True
            self._exposure.release(request.id)
            agent.add_defi_op()
            # Where the money went, so exposure can still be read back when the
            # venue is not in any curated list.
            if request.is_supply:
                agent.remember_defi_venue(quote.venue)
            if self._policy_store:
                self._policy_store.update_agent(agent)

        try:
            steps = []
            if request.is_supply:
                spender = (quote.venue if quote.venue_kind == "vault"
                           else adapter.morpho_address)
                steps = adapter.approval_steps(
                    quote.asset, sender, quote.assets, spender=spender,
                    label=f"{self._format_amount(quote)} {quote.asset_symbol or ''}")
            total_steps = len(steps) + 1

            for step_num, (approve_tx, what) in enumerate(steps, start=1):
                approve_row = self._create_approve_transaction(
                    request, asset=quote.asset, symbol=quote.asset_symbol or "",
                    spender=spender, amount_atomic=quote.assets,
                    auto_approved=auto_approved)
                adapter.simulate(approve_tx)
                ah = submit(approve_tx, f"Step {step_num} of {total_steps}: {what}")
                sent_hash = ah
                self._activity(f"Position {request.id}: approval submitted ({ah})")
                receipt = adapter.w3.eth.wait_for_transaction_receipt(ah, timeout=120)
                if receipt.status != 1:
                    if approve_row and self._policy_store:
                        approve_row.mark_failed(f"reverted: {what} (tx {ah})")
                        approve_row.tx_hash = ah
                        self._policy_store.update_transaction(approve_row)
                    return PositionResult.failed(
                        request.id, f"approval reverted: {what} (tx {ah})",
                        quote, tx_hash=ah, code="APPROVAL_REVERTED").to_dict()
                if approve_row and self._policy_store:
                    approve_row.mark_settled(ah)
                    self._policy_store.update_transaction(approve_row)
                approve_hash = ah

            main_tx = self._build_main_tx(adapter, request, quote, sender)
            # Rehearse with the allowance now real. A failure here has cost
            # nothing but the approval, and says why.
            adapter.simulate(main_tx)

            verb = "Supply" if request.is_supply else "Withdraw"
            sh = submit(main_tx,
                        f"Step {total_steps} of {total_steps}: {verb} "
                        f"{self._format_amount(quote)} {quote.asset_symbol or ''}",
                        before_send=on_broadcast)
            sent_hash = sh
            self._activity(f"Position {request.id}: {verb.lower()} submitted ({sh})")
            receipt = adapter.w3.eth.wait_for_transaction_receipt(sh, timeout=120)
            if receipt.status != 1:
                return PositionResult.failed(
                    request.id, f"{verb.lower()} reverted (tx {sh})", quote,
                    tx_hash=sh, code="REVERTED",
                    approval_tx_hash=approve_hash).to_dict()

            self._activity(f"Position {request.id} executed: {sh}")
            # The settled transfer, not the quote. None if the receipt could not
            # be read, which the history shows as unknown rather than filling in
            # the prediction and calling it the result.
            direction = "out" if request.is_supply else "in"
            moved = adapter.asset_moved(receipt, quote.asset, sender, direction)
            if moved is None:
                logger.warning(
                    "Position %s: could not read the settled amount from "
                    "receipt %s; history will show the quote only",
                    request.id, sh)
            return PositionResult(
                request_id=request.id, status="executed", tx_hash=sh,
                assets_moved=moved, assets_quoted=quote.assets,
                position_after=self._position_after(adapter, request, quote, sender),
                quote=quote, approval_tx_hash=approve_hash).to_dict()

        except InsufficientLiquidity as e:
            # The one failure worth resending unchanged.
            self._activity(f"Position {request.id} failed: {e}", True)
            return PositionResult.failed(
                request.id, str(e), quote, tx_hash=sent_hash,
                code="INSUFFICIENT_LIQUIDITY", retryable=True,
                approval_tx_hash=approve_hash).to_dict()
        except LedgerError as e:
            self._activity(f"Position {request.id} failed: {e}", True)
            return PositionResult.failed(request.id, str(e), quote,
                                         code="LEDGER_ERROR",
                                         approval_tx_hash=approve_hash).to_dict()
        except MorphoError as e:
            reason = str(e)
            if committed or sent_hash is not None:
                reason = (f"{reason}. The transaction was signed and sent, so it "
                          "may still complete on-chain - check it before trying "
                          "again.")
            return PositionResult.failed(request.id, reason, quote,
                                         tx_hash=sent_hash,
                                         code="EXECUTION_ERROR",
                                         approval_tx_hash=approve_hash).to_dict()
        except Web3RPCError as e:
            # The node evaluated this attempt and rejected it outright - not a
            # timeout or a dropped connection, where whether anything reached
            # the network is genuinely unknown. An RPC error response means it
            # did reach the network, and the network said no before ever
            # broadcasting it (insufficient funds for gas, a stale nonce,
            # underpriced gas). Unlike the generic branch below, this one can
            # say for certain that this attempt sent nothing.
            self._activity(f"Position {request.id} failed: {e}", True)
            return PositionResult.failed(
                request.id,
                f"Rejected before broadcast: {e}. Nothing was sent by this "
                "attempt - fix the cause and try again.",
                quote, code="RPC_REJECTED",
                approval_tx_hash=approve_hash).to_dict()
        except Exception:
            logger.exception("position execution failed")
            if committed or sent_hash is not None:
                reason = ("Vault could not confirm this operation. The transaction "
                          "was signed and sent, so it may still complete on-chain "
                          "- check it before trying again. See the Vault log.")
            else:
                reason = ("Vault could not build this operation. No transaction "
                          "was created nor sent, and it is safe to try again. "
                          "See the Vault log for the cause.")
            return PositionResult.failed(request.id, reason, quote,
                                         tx_hash=sent_hash,
                                         code="EXECUTION_ERROR",
                                         approval_tx_hash=approve_hash).to_dict()

    def _build_main_tx(self, adapter: MorphoAdapter, request: PositionRequest,
                       quote: PositionQuote, sender: str) -> dict:
        """The supply or withdrawal itself.

        Built against whichever denomination the caller named - `quote.by_shares`
        - and never against this module's conversion of it. The two numbers on
        the quote agree only for as long as the block they were read in.
        """
        if quote.venue_kind == "vault":
            if request.is_supply:
                return adapter.build_vault_deposit_tx(
                    quote.venue, quote.assets, sender, sender)
            if quote.by_shares:
                # Redeem burns a stated number of shares, so a full exit leaves
                # nothing behind and a partial one moves exactly what was asked.
                return adapter.build_vault_redeem_tx(
                    quote.venue, quote.shares, sender, sender, sender)
            return adapter.build_vault_withdraw_tx(
                quote.venue, quote.assets, sender, sender, sender)

        # The singleton wants the five-field struct, not the id. Read back from
        # the id the request named, which is what the quote was taken against.
        params = self._market_params(adapter, request.venue)
        if request.is_supply:
            return adapter.build_market_supply_tx(params, quote.assets, sender, sender)
        if quote.by_shares:
            # Assets must be zero when shares are named; Morpho takes one or the
            # other and rejects both.
            return adapter.build_market_withdraw_tx(
                params, 0, sender, sender, sender, shares=quote.shares)
        return adapter.build_market_withdraw_tx(
            params, quote.assets, sender, sender, sender)

    @staticmethod
    def _market_params(adapter: MorphoAdapter, market_id_hex: str) -> tuple:
        return adapter.market_params(bytes.fromhex(market_id_hex.replace("0x", "")))

    @staticmethod
    def _position_after(adapter: MorphoAdapter, request: PositionRequest,
                        quote: PositionQuote, owner: str) -> Optional[int]:
        """The position once the operation has landed, if it can be read."""
        try:
            if quote.venue_kind == "vault":
                _shares, assets = adapter.vault_position(quote.venue, owner)
                return assets
            return adapter.market_position(
                bytes.fromhex(quote.venue.replace("0x", "")), owner)
        except Exception:
            logger.debug("could not read the position after %s", request.id)
            return None
