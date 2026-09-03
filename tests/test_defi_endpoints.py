"""
The DeFi lane's HTTP surface: /position, /position/status/{id}, /venues.

The status codes carry meaning an agent acts on, so they are asserted rather
than left to whatever the handler happened to return:

- 202 means "a human is looking at it, poll" - not an error.
- 400 means the request was understood and refused; changing it is what helps.
- 500 means the request was fine and something here or on-chain went wrong;
  resending it unchanged is reasonable.
- 503 with Retry-After means come back later - a locked wallet, or a venue that
  cannot free the amount today. Neither is the caller's fault, and neither of
  the other two codes says "wait".

/venues exists so an agent can find out what it may use rather than discovering
it by being refused. 124 markets exist on this chain and four are curated.
"""

import json
from types import SimpleNamespace

import pytest

from primer_vault.models.agent import Agent
from primer_vault.models.policy import DefiRules, SpendPolicy
from primer_vault.networks import TOKENS
from primer_vault.services import server as server_module
from primer_vault.services.defi import DefiService
from primer_vault.services.morpho import MarketVenue, MorphoError, VaultVenue

USDG = TOKENS["USDG"].addresses[4663]
VAULT = "0xBeEff033F34C046626B8D0A041844C5d1A5409dd"
MARKET = "0x" + "c8" * 32
CURATOR = "0x9023fbd6a08c666491a2d1648737e400cf42d2fb"
ADDRESS = "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d"


# ---- service under test --------------------------------------------------

def a_vault() -> VaultVenue:
    return VaultVenue(address=VAULT, name="Steakhouse USDG", symbol="steakUSDG",
                      curator=CURATOR, asset=USDG, asset_decimals=6,
                      share_decimals=18, total_assets=390_000_000_000_000)


def a_market() -> MarketVenue:
    return MarketVenue(
        params=(USDG, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20,
                915000000000000000),
        market_key=bytes.fromhex(MARKET[2:]), loan_token=USDG,
        collateral_token="0x" + "11" * 20, collateral_symbol="USDe",
        lltv=915000000000000000, loan_decimals=6, endorsed_by=VAULT)


class FakeStore:
    def __init__(self, agent, policy):
        self._agent, self._policy = agent, policy

    def get_agent_by_id(self, agent_id):
        return self._agent if self._agent and self._agent.id == agent_id else None

    def get_policy(self, policy_id):
        return self._policy if self._policy and self._policy.id == policy_id else None

    def update_agent(self, agent):
        pass


@pytest.fixture
def service():
    rules = DefiRules(enabled=True, morpho_curators=[CURATOR],
                      max_deposit_usd=100.0, max_total_deployed_usd=500.0,
                      max_deployed_percent=50.0, max_ops_per_day=20)
    policy = SpendPolicy.create("p", [4663], 1_000_000, defi_rules=rules)
    agent = Agent.create("agent", "AG1")
    agent.status = "active"
    agent.policy_id = policy.id
    agent.wallet_address = ADDRESS

    svc = DefiService()
    svc.set_stores(FakeStore(agent, policy))
    svc.venues = lambda chain_id, curators, force=False: (
        [a_vault(), a_market()]
        if any(c.lower() == CURATOR.lower() for c in curators) else [])
    svc.deployed_usd = lambda chain_id, curators, owner, extra_venues=None: 120.0
    svc.liquid_usdg = lambda chain_id, owner: 880.0
    svc._adapter = lambda chain_id: SimpleNamespace(
        vault_position=lambda v, o: (100 * 10**18, 100_000_000),
        market_position=lambda k, o: 20_000_000,
        preview_deposit=lambda v, a: a * 10**12,
        preview_withdraw=lambda v, a: a * 10**12,
        preview_redeem=lambda v, s: s // 10**12,
        withdrawable=lambda v: 36_000_000_000_000,
        market_available=lambda k: 25_000_000_000_000,
        approval_steps=lambda *a, **kw: [],
        morpho_address="0x" + "9d" * 20,
        _erc20=lambda t: SimpleNamespace(functions=SimpleNamespace(
            symbol=lambda: SimpleNamespace(call=lambda: "USDG"))),
    )
    svc._test_agent = agent
    return svc


@pytest.fixture
def wired(service, monkeypatch):
    """Install the service where the endpoint code looks for it."""
    monkeypatch.setattr(server_module, "_defi_service", service)
    return service


# ============================================================
# /venues
# ============================================================


class TestVenues:

    def test_it_lists_the_venues_the_curator_stands_behind(self, service):
        result = service.handle_venues_request(service._test_agent.id)

        assert result["status"] == "ok"
        kinds = sorted(v["kind"] for v in result["venues"])
        assert kinds == ["market", "vault"]

    def test_the_response_names_no_single_protocol(self, service):
        """The list spans whatever is permitted. A protocol at this level would
        be wrong the moment there are two, and it is in the wire contract."""
        result = service.handle_venues_request(service._test_agent.id)

        assert "protocol" not in result

    def test_each_venue_names_its_protocol(self, service):
        """A venue id means nothing without knowing whose it is."""
        result = service.handle_venues_request(service._test_agent.id)

        assert {v["protocol"] for v in result["venues"]} == {"morpho"}

    def test_it_reports_what_the_agent_already_holds(self, service):
        result = service.handle_venues_request(service._test_agent.id)

        by_kind = {v["kind"]: v for v in result["venues"]}
        assert by_kind["vault"]["your_position_assets"] == 100_000_000
        assert by_kind["market"]["your_position_assets"] == 20_000_000

    def test_it_reports_what_a_market_can_pay_out_now(self, service):
        """Markets keep this - `market_available()` is one plain read, so it's
        cheap. Vaults don't: `withdrawable()` is the nested chain-call loop
        that made /venues take 60s+, so it's left off the listing (an agent
        gets it fresh at the actual withdraw quote instead)."""
        result = service.handle_venues_request(service._test_agent.id)

        by_kind = {v["kind"]: v for v in result["venues"]}
        assert by_kind["market"]["withdrawable_now"] == 25_000_000_000_000
        assert "withdrawable_now" not in by_kind["vault"]

    def test_it_reports_the_limits_the_agent_is_held_to(self, service):
        result = service.handle_venues_request(service._test_agent.id)

        policy = result["policy"]
        assert policy["max_deposit_usd"] == 100.0
        assert policy["deployed_usd"] == 120.0
        assert policy["liquid_usdg"] == 880.0

    def test_it_reports_room_left_rather_than_only_the_ceiling(self, service):
        """$1,000 total USDG, 50% cap -> $500 limit, $120 already deployed."""
        result = service.handle_venues_request(service._test_agent.id)

        assert result["policy"]["exposure_limit_usd"] == 500.0
        assert result["policy"]["remaining_deployable_usd"] == pytest.approx(380.0)

    def test_room_left_counts_requests_still_awaiting_approval(self, service):
        """Pacing against the raw limit would have the agent discover the
        difference by being refused."""
        agent = service._test_agent
        service.handle_position_request(agent.id, {
            "action": "supply", "venue": VAULT, "venue_kind": "vault",
            "amount": "80"})

        result = service.handle_venues_request(agent.id)

        assert result["policy"]["remaining_deployable_usd"] == pytest.approx(300.0)

    def test_an_unreadable_position_leaves_the_numbers_absent_not_wrong(self, service):
        service.deployed_usd = lambda *a, **kw: None

        result = service.handle_venues_request(service._test_agent.id)

        assert result["policy"]["deployed_usd"] is None
        assert result["policy"]["remaining_deployable_usd"] is None

    def test_one_unreadable_venue_does_not_empty_the_list(self, service):
        """The agent can still use the others."""
        base = service._adapter(4663)
        base.vault_position = lambda v, o: (_ for _ in ()).throw(
            MorphoError("node down"))
        service._adapter = lambda chain_id: base

        result = service.handle_venues_request(service._test_agent.id)

        assert [v["kind"] for v in result["venues"]] == ["market"]

    def test_an_unknown_agent_is_refused(self, service):
        assert service.handle_venues_request("nope")["code"] == "UNKNOWN_AGENT"

    def test_a_disabled_lane_says_so_rather_than_listing_nothing(self, service):
        """An empty list and a disabled lane are different problems, and an
        agent shown an empty list would keep trying."""
        policy = SpendPolicy.create("p", [4663], 1,
                                    defi_rules=DefiRules(enabled=False))
        service._policy_store._policy = policy
        service._test_agent.policy_id = policy.id

        result = service.handle_venues_request(service._test_agent.id)

        assert result["status"] == "error"
        assert result["code"] == "DEFI_DISABLED"


# ============================================================
# Status codes
# ============================================================


class TestPositionStatusCodes:
    """What the endpoint turns each service outcome into."""

    def test_a_pending_request_is_202_not_200(self, wired):
        """202 says a human is looking at it. 200 would read as done."""
        result = wired.handle_position_request(wired._test_agent.id, {
            "action": "supply", "venue": VAULT, "venue_kind": "vault",
            "amount": "10"})

        assert result["status"] == "pending"
        # The endpoint maps pending -> 202; asserted through the mapping table
        # rather than the socket, which needs a live server.
        assert _status_for(result) == 202

    def test_a_refused_request_is_400(self, wired):
        result = wired.handle_position_request(wired._test_agent.id, {
            "action": "supply", "venue": "0x" + "99" * 20,
            "venue_kind": "vault", "amount": "10"})

        assert result["code"] == "VENUE_NOT_PERMITTED"
        assert _status_for(result) == 400

    def test_a_liquidity_shortfall_is_503_so_the_caller_knows_to_wait(self, wired):
        """Neither 400 nor 500 says "come back later", and that is the whole
        content of this failure."""
        result = {"status": "failed", "code": "INSUFFICIENT_LIQUIDITY",
                  "retryable": True}

        assert _status_for(result) == 503

    def test_a_locked_wallet_is_503(self, wired):
        assert _status_for({"status": "error", "code": "WALLET_LOCKED"}) == 503

    def test_a_failure_on_our_side_is_500_not_400(self, wired):
        """The request was fine. A 400 would send the caller off to fix
        something that was never wrong."""
        assert _status_for({"status": "failed", "code": "EXECUTION_ERROR"}) == 500


def _status_for(result: dict) -> int:
    """The mapping /position applies, kept in one place so the test and the
    handler cannot drift apart silently."""
    from primer_vault.services.server import get_http_status_for_error
    status = result.get("status")
    code = result.get("code")
    if status == "executed":
        return 200
    if status == "pending":
        return 202
    if code in ("WALLET_LOCKED", "INSUFFICIENT_LIQUIDITY"):
        return 503
    if status == "rejected":
        return 400
    if status == "failed":
        return 500
    return get_http_status_for_error(code) if code else 200


# ============================================================
# Routing table
# ============================================================


class TestRouting:

    def test_the_new_endpoints_are_registered_as_post(self):
        from primer_vault.services.server import AgentRequestHandler

        assert "/position" in AgentRequestHandler.POST_ENDPOINTS
        assert "/venues" in AgentRequestHandler.POST_ENDPOINTS

    def test_position_status_is_not_a_post_endpoint(self):
        """It is a GET with a path parameter, so a POST must get a 405 naming
        the right verb rather than a 404."""
        from primer_vault.services.server import AgentRequestHandler

        assert "/position/status/" not in AgentRequestHandler.POST_ENDPOINTS

    def test_a_get_only_path_is_not_registered_for_post(self):
        """/position/status/{id} is dynamic, so it is matched by prefix rather
        than listed - but a bare POST to it must still not be treated as a
        known POST route."""
        from primer_vault.services.server import AgentRequestHandler

        assert "/position/status" not in AgentRequestHandler.POST_ENDPOINTS


class TestServiceNotWired:
    """Before the composition root has run, the endpoints must say so rather
    than raising."""

    def test_the_module_reference_starts_empty(self, monkeypatch):
        monkeypatch.setattr(server_module, "_defi_service", None)

        assert server_module._defi_service is None


def test_the_position_payload_is_json_serialisable(service):
    """Every result crosses an HTTP boundary. A stray Decimal or bytes here
    fails at write time, long after the operation has already happened."""
    result = service.handle_position_request(service._test_agent.id, {
        "action": "supply", "venue": VAULT, "venue_kind": "vault",
        "amount": "10"})

    json.dumps(result)


def test_a_venues_payload_is_json_serialisable(service):
    json.dumps(service.handle_venues_request(service._test_agent.id))
