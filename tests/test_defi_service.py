"""
DeFi service — venue gating, the exposure limit, and the quote/gate flow.

Uses a stand-in for MorphoAdapter so the suite never touches the network. The
live path is exercised by scripts run by hand.

Most of these guard one of three things:

- **A venue not backed by a trusted curator is unreachable.** 124 markets exist
  on the chain, four are curated, and the rest include a zero-address oracle and
  a market reporting two quadrillion supplied.
- **The exposure limit binds, and cannot be evaded by concurrency or by an RPC
  outage.** It is the only rule bounding what is actually at risk.
- **A withdrawal is never blocked by a money limit.** Refusing one traps funds.
"""

from types import SimpleNamespace

import pytest

from primer_vault.models.agent import Agent
from primer_vault.models.defi import PositionRequest
from primer_vault.models.policy import DefiRules, SpendPolicy
from primer_vault.networks import TOKENS
from primer_vault.services.defi import DefiService
from primer_vault.services.morpho import MarketVenue, MorphoError, VaultVenue

USDG = TOKENS["USDG"].addresses[4663]
VAULT = "0xBeEff033F34C046626B8D0A041844C5d1A5409dd"
MARKET = "0x" + "c8" * 32
CURATOR = "0x9023fbd6a08c666491a2d1648737e400cf42d2fb"
OTHER_TOKEN = "0x1111111111111111111111111111111111111111"
ADDRESS = "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d"


# ---- stand-ins ----------------------------------------------------------

def a_vault(asset=USDG, curator=CURATOR) -> VaultVenue:
    return VaultVenue(address=VAULT, name="Steakhouse USDG", symbol="steakUSDG",
                      curator=curator, asset=asset, asset_decimals=6,
                      share_decimals=18, total_assets=390_000_000_000_000)


def a_market(loan=USDG) -> MarketVenue:
    return MarketVenue(
        params=(loan, OTHER_TOKEN, "0x" + "22" * 20, "0x" + "33" * 20,
                915000000000000000),
        market_key=bytes.fromhex(MARKET[2:]), loan_token=loan,
        collateral_token=OTHER_TOKEN, collateral_symbol="USDe",
        lltv=915000000000000000, loan_decimals=6, endorsed_by=VAULT)


class FakeStore:
    def __init__(self, agent, policy):
        self._agent, self._policy = agent, policy
        self.saved_agents = []

    def get_agent_by_id(self, agent_id):
        return self._agent if self._agent and self._agent.id == agent_id else None

    def get_policy(self, policy_id):
        return self._policy if self._policy and self._policy.id == policy_id else None

    def update_agent(self, agent):
        self.saved_agents.append(agent)

    def add_transaction(self, tx):
        pass

    def update_transaction(self, tx):
        pass


def a_service(rules=None, venues=None, deployed=0.0, liquid=1000.0,
              position_assets=0) -> tuple:
    """A DefiService with the chain stubbed out. Returns (service, agent, store)."""
    rules = rules or DefiRules(enabled=True, morpho_curators=[CURATOR],
                               max_deposit_usd=100.0,
                               max_total_deployed_usd=500.0)
    policy = SpendPolicy.create("p", [4663], 1_000_000, defi_rules=rules)
    agent = Agent.create("agent", "AG1")
    agent.status = "active"
    agent.policy_id = policy.id
    agent.wallet_address = ADDRESS
    store = FakeStore(agent, policy)

    entry = SimpleNamespace(id="A001", address=ADDRESS, is_hardware=False)
    wallet = SimpleNamespace(
        get_address_by_address=lambda a: entry if a.lower() == ADDRESS.lower() else None,
        get_private_key=lambda _id: bytes(32))

    service = DefiService()
    service.set_stores(store)
    service.set_wallet_provider(lambda addr: wallet)

    resolved = venues if venues is not None else [a_vault(), a_market()]
    service.venues = lambda chain_id, curators, force=False: (
        resolved if any(c.lower() == CURATOR.lower() for c in curators) else [])
    service.deployed_usd = lambda chain_id, curators, owner, extra_venues=None: deployed
    service.liquid_usdg = lambda chain_id, owner: liquid

    service._adapter = lambda chain_id: SimpleNamespace(
        vault_position=lambda v, o: (position_assets * 10**12, position_assets),
        market_position=lambda k, o: position_assets,
        # Market shares are scaled against the market's own totals rather than
        # by decimals; a million to one is the ratio a fresh market starts at.
        market_position_full=lambda k, o: (position_assets * 10**6,
                                           position_assets),
        preview_deposit=lambda v, a: a * 994_938 // 1_000_000 * 10**12,
        preview_withdraw=lambda v, a: a * 994_938 // 1_000_000 * 10**12,
        preview_redeem=lambda v, s: s // 10**12,
        withdrawable=lambda v: 36_000_000_000_000,
        market_available=lambda k: 25_000_000_000_000,
        approval_steps=lambda *a, **kw: [({}, "approve")],
        morpho_address="0x" + "9d" * 20,
        _erc20=lambda t: SimpleNamespace(
            functions=SimpleNamespace(
                symbol=lambda: SimpleNamespace(call=lambda: "USDG"))),
    )
    return service, agent, store


def ops_done_today(agent, count: int) -> None:
    """Give the agent a count that belongs to today.

    Setting `defi_ops_today` alone is not enough: an agent with no recorded
    reset has never had a DeFi day start, so the first request legitimately
    begins one and zeroes the count. Stamping the reset is what makes the count
    today's rather than a leftover from before the agent existed.
    """
    from datetime import date, datetime, timezone
    agent.defi_ops_today = count
    agent.last_defi_reset_date = date.today().isoformat()
    agent.last_defi_reset_at = datetime.now(timezone.utc).isoformat()


def request_body(**kw) -> dict:
    body = {"action": "supply", "venue": VAULT, "venue_kind": "vault",
            "amount": "10"}
    body.update(kw)
    return body


# ============================================================
# Venue gating
# ============================================================


class TestVenueGating:

    def test_a_curated_vault_is_reachable(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] in ("pending", "executed"), result

    def test_a_curated_market_is_reachable(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(venue=MARKET, venue_kind="market"))

        assert result["status"] in ("pending", "executed"), result

    def test_an_uncurated_venue_is_refused(self):
        """The 120 markets nobody stands behind must not be reachable."""
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(venue="0x" + "99" * 20))

        assert result["status"] == "rejected"
        assert result["code"] == "VENUE_NOT_PERMITTED"

    def test_a_venue_is_matched_regardless_of_address_case(self):
        """An agent sends whatever it copied; `curator()` answers checksummed."""
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(venue=VAULT.lower()))

        assert result["status"] != "rejected" or result.get("code") != "VENUE_NOT_PERMITTED"


class TestALockedWalletIsRefusedImmediately:
    """Without this, a request from a locked wallet would quote, pass policy,
    take a request id, sit `pending`, and only fail once a human had approved
    it - asking someone to authorise something that was never going to work,
    and leaving the agent to poll a request that could never succeed."""

    def test_locked_wallet_is_refused_at_intake_not_after_approval(self):
        service, agent, _ = a_service()
        service.set_wallet_provider(lambda addr: None)

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "error"
        assert result["code"] == "WALLET_LOCKED"

    def test_the_refusal_never_reaches_pending(self):
        """No approval request is left waiting on a decision that could
        never help - checked via the pending list, not just the response."""
        service, agent, _ = a_service()
        service.set_wallet_provider(lambda addr: None)

        service.handle_position_request(agent.id, request_body())

        assert service.get_pending_positions() == []

    def test_no_curators_means_no_venues(self):
        rules = DefiRules(enabled=True, morpho_curators=[],
                          max_total_deployed_usd=500.0)
        service, agent, _ = a_service(rules=rules)

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "rejected"
        assert result["code"] in ("POLICY_INCOMPLETE", "VENUE_NOT_PERMITTED")


# ============================================================
# What the caller may say about itself
# ============================================================


class TestRequestFields:

    @pytest.mark.parametrize("field", ["agent_id", "wallet_address", "receiver"])
    def test_the_body_may_not_name_the_caller_or_the_recipient(self, field):
        """Refused outright rather than ignored, so a caller that tries finds
        out instead of believing it worked."""
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(**{field: "0x" + "11" * 20}))

        assert result["code"] == "FIELD_NOT_PERMITTED"
        assert field in result["error"]

    def test_a_malformed_request_is_named_not_crashed_on(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(agent.id, {"action": "supply"})

        assert result["code"] == "BAD_REQUEST"

    def test_an_unknown_agent_is_refused(self):
        service, _, _ = a_service()

        result = service.handle_position_request("nope", request_body())

        assert result["code"] == "UNKNOWN_AGENT"


# ============================================================
# Policy gating
# ============================================================


class TestPolicyGating:

    def test_defi_disabled_refuses_everything(self):
        service, agent, _ = a_service(
            rules=DefiRules(enabled=False, morpho_curators=[CURATOR]))

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "rejected"
        assert result["code"] == "DEFI_DISABLED"

    def test_a_policy_with_no_defi_rules_refuses_everything(self):
        policy = SpendPolicy.create("p", [4663], 1_000_000)
        service, agent, store = a_service()
        store._policy = policy
        agent.policy_id = policy.id

        result = service.handle_position_request(agent.id, request_body())

        assert result["code"] == "DEFI_NOT_CONFIGURED"

    def test_a_deposit_over_the_per_deposit_cap_is_refused(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(amount="500"))

        assert result["status"] == "rejected"
        assert result["code"] == "PER_DEPOSIT_EXCEEDED"

    def test_a_deposit_that_would_breach_the_exposure_limit_is_refused(self):
        """$450 already deployed against a $500 ceiling leaves $50."""
        service, agent, _ = a_service(deployed=450.0, liquid=1000.0)

        result = service.handle_position_request(
            agent.id, request_body(amount="60"))

        assert result["status"] == "rejected"
        assert result["code"] == "EXPOSURE_EXCEEDED"

    def test_a_deposit_inside_the_exposure_limit_is_allowed(self):
        service, agent, _ = a_service(deployed=450.0, liquid=1000.0)

        result = service.handle_position_request(
            agent.id, request_body(amount="40"))

        assert result["status"] != "rejected", result

    def test_the_percentage_limit_binds_when_it_is_lower(self):
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=1000.0,
                          max_total_deployed_usd=10_000.0,
                          max_deployed_percent=10.0)
        # $1,000 total USDG -> 10% is $100.
        service, agent, _ = a_service(rules=rules, deployed=0.0, liquid=1000.0)

        result = service.handle_position_request(
            agent.id, request_body(amount="150"))

        assert result["code"] == "EXPOSURE_EXCEEDED"

    def test_an_unreadable_position_refuses_rather_than_assuming_zero(self):
        """Treating an unreadable position as empty would let every limit
        through during an RPC outage."""
        service, agent, _ = a_service()
        service.deployed_usd = lambda *a, **kw: None

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "rejected"
        assert result["code"] == "EXPOSURE_UNREADABLE"

    def test_an_unreadable_balance_refuses_too(self):
        service, agent, _ = a_service()
        service.liquid_usdg = lambda *a, **kw: None

        result = service.handle_position_request(agent.id, request_body())

        assert result["code"] == "EXPOSURE_UNREADABLE"

    def test_a_deposit_of_an_unpriceable_asset_escalates_rather_than_executing(self):
        """Vault only trust-prices USDG. Anything else goes to a human."""
        service, agent, _ = a_service(
            venues=[a_vault(asset=OTHER_TOKEN)],
            rules=DefiRules(enabled=True, morpho_curators=[CURATOR],
                            max_deposit_usd=100.0, max_total_deployed_usd=500.0,
                            auto_approve_below_usd=1000.0))

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "pending", (
            "an unvaluable deposit was auto-executed")


class TestDailyOperationCeiling:
    """The gas circuit breaker. Not a money limit - the money limits cannot see
    a loop that deposits and withdraws the same value all day."""

    def test_the_ceiling_refuses_once_reached(self):
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=100.0, max_total_deployed_usd=500.0,
                          max_ops_per_day=3)
        service, agent, _ = a_service(rules=rules)
        ops_done_today(agent, 3)

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "rejected"
        assert result["code"] == "DAILY_OPS_EXCEEDED"

    def test_a_new_day_clears_the_count(self):
        """The counter is a daily allowance, not a lifetime one."""
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=100.0, max_total_deployed_usd=500.0,
                          max_ops_per_day=3)
        service, agent, _ = a_service(rules=rules)
        # Three ops recorded, but from a day that has since rolled over.
        agent.defi_ops_today = 3
        agent.last_defi_reset_date = "2020-01-01"
        agent.last_defi_reset_at = "2020-01-01T00:00:00+00:00"

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "pending", result
        assert agent.defi_ops_today == 0

    def test_it_applies_to_withdrawals_too(self):
        """A loop needs both halves, so bounding only deposits bounds nothing."""
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=100.0, max_total_deployed_usd=500.0,
                          max_ops_per_day=1)
        service, agent, _ = a_service(rules=rules, position_assets=50_000_000)
        ops_done_today(agent, 1)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", amount="10"))

        assert result["code"] == "DAILY_OPS_EXCEEDED"

    def test_it_is_checked_before_anything_reserves(self):
        """A request refused for the op count must not leave a reservation
        behind, or the agent is charged exposure for something it never did."""
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=100.0, max_total_deployed_usd=500.0,
                          max_ops_per_day=0)
        service, agent, _ = a_service(rules=rules)

        service.handle_position_request(agent.id, request_body())

        assert service._reservations == {}


class TestWithdrawals:

    def test_a_withdrawal_is_not_subject_to_the_per_deposit_cap(self):
        """Refusing a withdrawal traps funds. The proceeds cannot leave the
        wallet without passing another lane anyway."""
        service, agent, _ = a_service(position_assets=900_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", amount="500"))

        assert result["status"] != "rejected", result

    def test_a_withdrawal_is_not_subject_to_the_exposure_limit(self):
        service, agent, _ = a_service(deployed=10_000.0,
                                      position_assets=900_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", amount="500"))

        assert result["status"] != "rejected", result

    def test_a_withdrawal_reserves_no_exposure(self):
        service, agent, _ = a_service(position_assets=900_000_000)

        service.handle_position_request(
            agent.id, request_body(action="withdraw", amount="100"))

        assert service._reservations == {}

    def test_a_full_exit_is_denominated_in_shares(self):
        """An asset-denominated exit is quoted a block before it settles and
        leaves dust behind when the share price moves."""
        service, agent, _ = a_service(position_assets=5_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", withdraw_all=True,
                                   amount="0"))

        assert result["status"] == "pending", result
        assert result["quote"]["shares"] > 0

    def test_a_full_exit_from_an_empty_position_is_refused(self):
        service, agent, _ = a_service(position_assets=0)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", withdraw_all=True,
                                   amount="0"))

        assert result["status"] == "rejected"
        assert "no position" in result["reason"]


# ============================================================
# Denomination
# ============================================================


class TestWithdrawingInShares:
    """Morpho takes a withdrawal in assets or in shares, so Vault does too.

    The two are not interchangeable. Assets is "give me $10 back". Shares names
    the position itself, and is the only denomination that can name a fraction
    of it exactly - the share price moves between the quote and the settlement,
    so an asset figure is a prediction and a share count is not.
    """

    def test_a_vault_withdrawal_scales_by_the_shares_own_decimals(self):
        """Vault shares are an ERC-20 at 18dp, not the asset's 6. Reading the
        amount at the asset's scale would burn a millionth of what was asked."""
        service, agent, _ = a_service(position_assets=5_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", denomination="shares",
                                   amount="2"))

        assert result["status"] == "pending", result
        assert result["quote"]["shares"] == 2 * 10**18
        assert result["quote"]["by_shares"] is True

    def test_a_market_withdrawal_reads_shares_as_a_plain_count(self):
        """A market share is not a token and has no decimals of its own - the
        singleton keeps a bare integer against the market's totals."""
        service, agent, _ = a_service(position_assets=5_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", venue=MARKET,
                                   venue_kind="market", denomination="shares",
                                   amount="1000000"))

        assert result["status"] == "pending", result
        assert result["quote"]["shares"] == 1_000_000
        assert result["quote"]["share_decimals"] == 0

    def test_more_shares_than_the_position_holds_is_refused_before_signing(self):
        """It would revert on-chain as an arithmetic underflow, which reads as
        a corrupt internal error rather than as "you do not have that much"."""
        service, agent, _ = a_service(position_assets=1_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", denomination="shares",
                                   amount="999999"))

        assert result["status"] == "rejected"
        assert "more shares than the position holds" in result["reason"]

    def test_a_supply_may_not_be_denominated_in_shares(self):
        """There are none to hand over before the deposit that mints them."""
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(denomination="shares", amount="10"))

        assert result["status"] == "rejected"
        assert "always denominated in assets" in result["reason"]

    def test_an_unknown_denomination_is_refused(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", denomination="dollars",
                                   amount="10"))

        assert result["status"] == "rejected"
        assert "denomination" in result["reason"]

    def test_a_request_that_names_no_denomination_still_means_assets(self):
        """Every request written before shares were accepted meant assets, and
        has to keep meaning that."""
        service, agent, _ = a_service(position_assets=5_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", amount="1"))

        assert result["status"] == "pending", result
        assert result["quote"]["by_shares"] is False
        assert result["quote"]["assets"] == 1_000_000

    def test_a_full_exit_is_a_share_withdrawal_whatever_was_asked_for(self):
        """`withdraw_all` and `denomination: shares` are one idea, not two -
        so the build path has a single condition to answer rather than two that
        could disagree."""
        service, agent, _ = a_service(position_assets=5_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", withdraw_all=True,
                                   amount="0"))

        assert result["quote"]["by_shares"] is True

    def test_a_market_full_exit_names_shares_not_the_asset_figure(self):
        """The market accrues interest inside the withdrawal, so the asset
        figure quoted here is already behind by the time it settles. Naming it
        would strand the difference."""
        service, agent, _ = a_service(position_assets=5_000_000)

        result = service.handle_position_request(
            agent.id, request_body(action="withdraw", venue=MARKET,
                                   venue_kind="market", withdraw_all=True,
                                   amount="0"))

        assert result["status"] == "pending", result
        assert result["quote"]["by_shares"] is True
        assert result["quote"]["shares"] == 5_000_000 * 10**6


class TestTheTransactionNamesTheDenominationAsked:
    """The quote carries both numbers; only one of them may reach the chain.

    Everything above stops at the quote, and a quote that is right while the
    transaction is built against the other field would leave the whole
    denomination a presentational detail. So these assert the last step: which
    builder runs, and what it is handed.
    """

    @staticmethod
    def _recording_adapter():
        calls = []

        def record(name):
            def call(*args, **kwargs):
                calls.append((name, args, kwargs))
                return {"tx": name}
            return call

        adapter = SimpleNamespace(
            build_vault_deposit_tx=record("vault_deposit"),
            build_vault_withdraw_tx=record("vault_withdraw"),
            build_vault_redeem_tx=record("vault_redeem"),
            build_market_supply_tx=record("market_supply"),
            build_market_withdraw_tx=record("market_withdraw"),
            market_params=lambda key: ("p",),
        )
        return adapter, calls

    def _build(self, body, position_assets=5_000_000):
        service, agent, _ = a_service(position_assets=position_assets)
        result = service.handle_position_request(agent.id, request_body(**body))
        assert result["status"] == "pending", result

        request, quote = service.get_pending_positions()[0]
        adapter, calls = self._recording_adapter()
        service._build_main_tx(adapter, request, quote, ADDRESS)
        return calls[0], quote

    def test_a_share_denominated_vault_exit_redeems(self):
        (name, args, _), quote = self._build(
            {"action": "withdraw", "denomination": "shares", "amount": "2"})

        assert name == "vault_redeem"
        # The share count, not the asset figure the quote also carries.
        assert args[1] == quote.shares == 2 * 10**18

    def test_an_asset_denominated_vault_exit_withdraws(self):
        (name, args, _), _ = self._build(
            {"action": "withdraw", "amount": "1"})

        assert name == "vault_withdraw"
        assert args[1] == 1_000_000

    def test_a_share_denominated_market_exit_passes_shares_and_zero_assets(self):
        """Morpho takes one denomination or the other and rejects both, so the
        assets argument has to go to zero when shares are named."""
        (name, args, kwargs), _ = self._build(
            {"action": "withdraw", "venue": MARKET, "venue_kind": "market",
             "denomination": "shares", "amount": "1000000"})

        assert name == "market_withdraw"
        assert args[1] == 0
        assert kwargs["shares"] == 1_000_000

    def test_an_asset_denominated_market_exit_names_no_shares(self):
        (name, args, kwargs), _ = self._build(
            {"action": "withdraw", "venue": MARKET, "venue_kind": "market",
             "amount": "1"})

        assert name == "market_withdraw"
        assert args[1] == 1_000_000
        assert "shares" not in kwargs


# ============================================================
# Pending, approval and the allowance
# ============================================================


class TestPendingFlow:

    def test_an_escalated_request_is_pollable(self):
        service, agent, _ = a_service()

        submitted = service.handle_position_request(agent.id, request_body())
        status = service.get_position_status(submitted["request_id"])

        assert status["status"] == "pending"
        assert 0 < status["expires_in_seconds"] <= 900

    def test_an_unknown_request_id_is_not_found(self):
        service, _, _ = a_service()

        assert service.get_position_status("nope")["code"] == "REQUEST_NOT_FOUND"

    def test_rejecting_releases_the_reservation(self):
        """A held allowance that is never returned leaves the agent stuck."""
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())
        assert service._reservations != {}

        service.reject_position(submitted["request_id"], "no thanks")

        assert service._reservations == {}

    def test_a_rejected_request_reports_the_reason(self):
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())

        result = service.reject_position(submitted["request_id"], "not today")

        assert result["status"] == "rejected"
        assert result["reason"] == "not today"

    def test_a_resolved_request_keeps_answering_after_it_leaves_the_queue(self):
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())
        service.reject_position(submitted["request_id"])

        assert service.get_position_status(
            submitted["request_id"])["status"] == "rejected"

    def test_a_status_poll_during_execution_never_reads_not_found(self):
        """approve_position pops the request from the pending queue before
        execute_position runs, which can take real time (up to two
        sequential on-chain waits). A poll landing in that window must not
        read REQUEST_NOT_FOUND - indistinguishable from an id that never
        existed - for a request a human just approved.
        """
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())
        request_id = submitted["request_id"]

        seen_mid_flight = {}

        real_execute = service.execute_position

        def spying_execute(*args, **kwargs):
            # Called from inside approve_position, after the pop and before
            # the real result is written - exactly the window this guards.
            seen_mid_flight["status"] = service.get_position_status(
                request_id)["status"]
            return real_execute(*args, **kwargs)

        service.execute_position = spying_execute
        result = service.approve_position(request_id)

        assert seen_mid_flight["status"] == "executing"
        assert result["status"] in ("executed", "failed")
        # The placeholder must not linger once the real result exists.
        assert service.get_position_status(request_id)["status"] == result["status"]

    def test_a_queued_deposit_counts_against_the_next_one(self):
        """Otherwise a stack of individually-allowed deposits clears the
        exposure limit together the moment they are approved."""
        service, agent, _ = a_service(deployed=0.0, liquid=1000.0)

        first = service.handle_position_request(agent.id, request_body(amount="60"))
        second = service.handle_position_request(agent.id, request_body(amount="60"))
        third = service.handle_position_request(agent.id, request_body(amount="60"))
        # 500 limit, three at 60 fit; the ninth would not.
        for _ in range(6):
            service.handle_position_request(agent.id, request_body(amount="60"))
        last = service.handle_position_request(agent.id, request_body(amount="60"))

        assert first["status"] == "pending"
        assert second["status"] == "pending"
        assert third["status"] == "pending"
        assert last["code"] == "EXPOSURE_EXCEEDED"

    def test_reserved_exposure_is_published_so_an_agent_can_pace_itself(self):
        service, agent, _ = a_service()
        service.handle_position_request(agent.id, request_body(amount="40"))

        assert service.reserved_exposure_for(agent.id) == pytest.approx(40.0)

    def test_a_suspended_agent_cannot_have_its_request_approved(self):
        """Suspension is the stop button; a queued request must not slip past."""
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())
        agent.status = "suspended"

        result = service.approve_position(submitted["request_id"])

        assert result["status"] == "rejected"
        assert result["code"] == "AGENT_SUSPENDED"
        assert service._reservations == {}

    def test_a_venue_that_leaves_the_curated_set_cannot_be_approved(self):
        """A curator can be replaced under timelock. A request that predates the
        change must not still reach the venue."""
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())
        service.venues = lambda chain_id, curators, force=False: []

        result = service.approve_position(submitted["request_id"])

        assert result["code"] == "VENUE_NOT_PERMITTED"
        assert service._reservations == {}

    def test_approving_an_unknown_request_is_not_found(self):
        service, _, _ = a_service()

        assert service.approve_position("nope")["code"] == "REQUEST_NOT_FOUND"

    def test_the_per_agent_pending_cap_holds(self):
        """One noisy agent must not bury the person approving."""
        from primer_vault.services.defi import MAX_PENDING_POSITIONS_PER_AGENT
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=1.0,
                          max_total_deployed_usd=1_000_000.0)
        service, agent, _ = a_service(rules=rules)

        for _ in range(MAX_PENDING_POSITIONS_PER_AGENT):
            service.handle_position_request(agent.id, request_body(amount="1"))
        overflow = service.handle_position_request(agent.id, request_body(amount="1"))

        assert overflow["code"] == "TOO_MANY_PENDING"

    def test_an_overflowing_request_does_not_keep_its_reservation(self):
        from primer_vault.services.defi import MAX_PENDING_POSITIONS_PER_AGENT
        rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                          max_deposit_usd=1.0,
                          max_total_deployed_usd=1_000_000.0)
        service, agent, _ = a_service(rules=rules)
        for _ in range(MAX_PENDING_POSITIONS_PER_AGENT):
            service.handle_position_request(agent.id, request_body(amount="1"))

        service.handle_position_request(agent.id, request_body(amount="1"))

        assert len(service._reservations) == MAX_PENDING_POSITIONS_PER_AGENT


class TestExpiry:

    def test_a_request_left_too_long_is_abandoned_and_gives_its_allowance_back(self):
        import time as _time
        service, agent, _ = a_service()
        submitted = service.handle_position_request(agent.id, request_body())
        request_id = submitted["request_id"]
        service._pending[request_id].deadline = _time.monotonic() - 1

        status = service.get_position_status(request_id)

        assert status["status"] == "rejected"
        assert status["code"] == "EXPIRED"
        assert service._reservations == {}


class TestQuoteContents:

    def test_a_quote_reports_the_position_the_agent_already_holds(self):
        service, agent, _ = a_service(position_assets=25_000_000)

        result = service.handle_position_request(agent.id, request_body())

        assert result["quote"]["current_position_assets"] == 25_000_000

    def test_a_quote_reports_what_the_venue_could_return_now(self):
        """Shown so a caller can size a withdrawal. It moves with other people's
        borrowing, so it is reported rather than relied on."""
        service, agent, _ = a_service()

        result = service.handle_position_request(agent.id, request_body())

        assert result["quote"]["venue_withdrawable"] == 36_000_000_000_000

    def test_a_capacity_read_that_fails_does_not_refuse_the_operation(self):
        service, agent, _ = a_service()
        base = service._adapter(4663)

        def failing(chain_id):
            base.withdrawable = lambda v: (_ for _ in ()).throw(
                MorphoError("node down"))
            return base

        service._adapter = failing

        result = service.handle_position_request(agent.id, request_body())

        assert result["status"] == "pending", result
        assert result["quote"]["venue_withdrawable"] is None

    def test_amounts_are_integers_at_both_scales(self):
        service, agent, _ = a_service()

        quote = service.handle_position_request(agent.id, request_body())["quote"]

        assert isinstance(quote["assets"], int)
        assert isinstance(quote["shares"], int)
        assert quote["assets"] == 10_000_000  # 10 USDG at 6dp

    def test_an_amount_below_the_tokens_precision_is_refused(self):
        """Rounding to zero would submit an operation that moves nothing."""
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(amount="0.0000001"))

        assert result["status"] == "rejected"


class TestRequestModelWiring:

    def test_the_service_sets_the_address_from_the_agent(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(agent.id, request_body())

        pending = service.get_pending_positions()
        assert pending, result
        assert pending[0][0].wallet_address == ADDRESS

    def test_an_uncommissioned_agent_is_refused(self):
        service, agent, _ = a_service()
        agent.wallet_address = None

        result = service.handle_position_request(agent.id, request_body())

        assert result["code"] == "AGENT_NOT_COMMISSIONED"

    def test_a_request_past_its_deadline_is_refused(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(deadline=1))

        assert result["status"] == "rejected"
        assert result["code"] == "DEADLINE_PASSED"


def test_position_request_defaults_to_the_configured_network():
    request = PositionRequest.from_dict(
        {"action": "supply", "venue": VAULT, "venue_kind": "vault",
         "amount": "1", "chain_id": 4663})

    assert request.chain_id == 4663


# ============================================================
# The restriction toggle
# ============================================================


class TestRestriction:
    """`restrict_to_steakhouse` is the one control a user actually sees."""

    def test_it_is_on_by_default(self):
        assert DefiRules().restrict_to_steakhouse is True

    def test_restricted_refuses_a_venue_no_curator_backs(self):
        service, agent, _ = a_service()

        result = service.handle_position_request(
            agent.id, request_body(venue="0x" + "99" * 20))

        assert result["code"] == "VENUE_NOT_PERMITTED"

    def test_unrestricted_reaches_a_venue_straight_from_the_chain(self):
        """No curated list to look in, so the venue is read from its address."""
        rules = DefiRules(enabled=True, restrict_to_steakhouse=False,
                          max_deposit_usd=100.0, max_total_deployed_usd=500.0)
        service, agent, _ = a_service(rules=rules)
        other = "0x" + "99" * 20
        service._venue_from_chain = lambda c, vid, kind: a_vault()

        result = service.handle_position_request(
            agent.id, request_body(venue=other))

        assert result["status"] != "rejected", result

    def test_unrestricted_still_refuses_an_address_that_is_not_a_venue(self):
        """Permissive is not credulous - an unreadable address resolves to
        nothing rather than to a broken venue."""
        rules = DefiRules(enabled=True, restrict_to_steakhouse=False,
                          max_deposit_usd=100.0, max_total_deployed_usd=500.0)
        service, agent, _ = a_service(rules=rules)
        service._venue_from_chain = lambda c, vid, kind: None

        result = service.handle_position_request(
            agent.id, request_body(venue="0x" + "99" * 20))

        assert result["code"] == "VENUE_NOT_PERMITTED"

    def test_unrestricted_needs_no_curator_to_be_a_valid_policy(self):
        rules = DefiRules(enabled=True, restrict_to_steakhouse=False,
                          morpho_curators=[], max_total_deployed_usd=1.0)

        assert rules.validate() == (True, "")

    def test_restricted_with_no_curator_is_an_invalid_policy(self):
        """It would refuse everything while looking enabled."""
        ok, reason = DefiRules(enabled=True, morpho_curators=[],
                               max_total_deployed_usd=1.0).validate()

        assert ok is False
        assert "Steakhouse" in reason


class TestRememberingWhereMoneyWent:
    """Exposure is read back off the chain, which means knowing where to look.

    The curated list answers that while the agent is restricted to it. With the
    restriction off there is no list, so without this the exposure cap would
    stop counting at exactly the moment the venue gate is opened.
    """

    def test_a_venue_is_recorded_the_first_time_only(self):
        agent = Agent.create("a", "AG1")

        assert agent.remember_defi_venue(VAULT) is True
        assert agent.remember_defi_venue(VAULT) is False
        assert agent.defi_venues == [VAULT]

    def test_the_same_venue_in_a_different_case_is_not_recorded_twice(self):
        """An address arrives however the agent spelled it; counted twice it
        would double the measured exposure."""
        agent = Agent.create("a", "AG1")
        agent.remember_defi_venue(VAULT)

        assert agent.remember_defi_venue(VAULT.lower()) is False
        assert len(agent.defi_venues) == 1

    def test_an_empty_venue_is_not_recorded(self):
        agent = Agent.create("a", "AG1")

        assert agent.remember_defi_venue("") is False
        assert agent.defi_venues == []

    def test_remembered_venues_are_added_to_the_exposure_sum(self):
        service, agent, _ = a_service()
        agent.defi_venues = [VAULT]
        service._venue_from_chain = lambda c, vid, kind: a_vault()

        assert [v.id for v in service._remembered_venues(4663, agent)] == [
            a_vault().id]

    def test_an_unreadable_remembered_venue_is_skipped_not_fatal(self):
        """A venue that has since broken must not stop the exposure sum; its
        contribution is zero anyway."""
        service, agent, _ = a_service()
        agent.defi_venues = [VAULT, MARKET]
        service._venue_from_chain = lambda c, vid, kind: (
            a_vault() if kind == "vault" else None)

        assert len(service._remembered_venues(4663, agent)) == 1

    def test_an_agent_written_before_this_existed_still_loads(self):
        agent = Agent.create("a", "AG1")
        stored = {k: v for k, v in agent.to_dict().items() if k != "defi_venues"}

        assert Agent.from_dict(stored).defi_venues == []

    def test_a_malformed_venue_list_is_refused(self):
        agent = Agent.create("a", "AG1")

        with pytest.raises(ValueError):
            Agent.from_dict({**agent.to_dict(), "defi_venues": [123]})


# ============================================================
# History
# ============================================================


class TestHistory:
    """Every operation leaves a record, whatever happens to it.

    Written before the attempt, so a crash mid-flight leaves a trace rather
    than nothing - the same order the trading lane uses.
    """

    def _store_with_history(self, **kw):
        service, agent, store = a_service(**kw)
        store.added = []
        store.updated = []
        store.add_transaction = store.added.append
        store.update_transaction = store.updated.append
        return service, agent, store

    def test_a_pending_request_writes_nothing_until_it_is_answered(self):
        """It has not happened yet, and a row for it would read as one that had."""
        service, agent, store = self._store_with_history()

        service.handle_position_request(agent.id, request_body())

        assert store.added == []

    def test_rejecting_records_the_refusal(self):
        service, agent, store = self._store_with_history()
        submitted = service.handle_position_request(agent.id, request_body())

        service.reject_position(submitted["request_id"], "no thanks")

        assert len(store.added) == 1
        assert store.updated[-1].status == "rejected"
        assert store.updated[-1].reject_reason == "no thanks"

    def test_expiry_records_the_refusal_too(self):
        import time as _time
        service, agent, store = self._store_with_history()
        submitted = service.handle_position_request(agent.id, request_body())
        service._pending[submitted["request_id"]].deadline = _time.monotonic() - 1

        service.get_position_status(submitted["request_id"])

        assert store.updated[-1].status == "rejected"
        assert "Expired" in store.updated[-1].reject_reason

    def test_a_supply_is_recorded_with_the_venue_as_the_destination(self):
        from primer_vault.models.transaction import Transaction

        tx = Transaction.create_lend(
            agent_id="a", agent_name="n", agent_code="C1",
            network="eip155:4663", action="supply", venue=VAULT,
            venue_name="Steakhouse USDG", asset=USDG, symbol="USDG",
            amount_in="25", wallet_address=ADDRESS)

        assert tx.type == "lend"
        assert tx.token_in == USDG and tx.token_out == VAULT
        assert tx.symbol_in == "USDG" and tx.symbol_out == "Steakhouse USDG"

    def test_a_withdrawal_records_the_same_pair_the_other_way_round(self):
        """The history table renders these as "out" and "in", so a withdrawal
        has to reverse them or it reads as another deposit."""
        from primer_vault.models.transaction import Transaction

        tx = Transaction.create_lend(
            agent_id="a", agent_name="n", agent_code="C1",
            network="eip155:4663", action="withdraw", venue=VAULT,
            venue_name="Steakhouse USDG", asset=USDG, symbol="USDG",
            amount_in="25", wallet_address=ADDRESS)

        assert tx.token_in == VAULT and tx.token_out == USDG
        assert tx.symbol_in == "Steakhouse USDG" and tx.symbol_out == "USDG"

    def test_a_lend_record_survives_a_round_trip(self):
        from primer_vault.models.transaction import Transaction

        tx = Transaction.create_lend(
            agent_id="a", agent_name="n", agent_code="C1",
            network="eip155:4663", action="supply", venue=VAULT,
            venue_name="Steakhouse USDG", asset=USDG, symbol="USDG",
            amount_in="25", wallet_address=ADDRESS)

        assert Transaction.from_dict(tx.to_dict()).type == "lend"
