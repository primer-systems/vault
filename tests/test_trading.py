"""
Trading engine tests — models, valuation math, and the service's quote/gate flow.

These use a mocked DEX adapter so the suite never touches the network. A separate
live check (scripts, run manually) exercises the real RHC path.
"""

from types import SimpleNamespace

import pytest

from primer_vault.models.trade import TradeRequest, MAX_SLIPPAGE_BPS
from primer_vault.services.dex import to_atomic, from_atomic, ETH_METADATA
from primer_vault.services import pricing
from primer_vault.services.trading import TradingService
from primer_vault.networks import get_dex, TOKENS, is_native_eth

USDG = TOKENS["USDG"].addresses[4663]
WETH = get_dex(4663).weth
TOKEN = "0x1111111111111111111111111111111111111111"  # arbitrary non-base token


# ---- models -------------------------------------------------------------

def test_valid_request_shape():
    r = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)
    ok, reason = r.validate_shape()
    assert ok, reason


@pytest.mark.parametrize("mut,expect", [
    ({"token_in": "0xnothex"}, "valid address"),
    ({"token_out": "notanaddress"}, "valid address"),
    ({"token_out": USDG, "token_in": USDG}, "same"),
    ({"fee_tier": 3000000}, "fee_tier"),
    ({"amount_in": "0"}, "positive"),
    ({"amount_in": "-5"}, "positive"),
    ({"amount_in": "abc"}, "not a number"),
    ({"max_slippage_bps": MAX_SLIPPAGE_BPS + 1}, "ceiling"),
    ({"max_slippage_bps": -1}, "non-negative"),
])
def test_invalid_request_shapes(mut, expect):
    base = dict(agent_id="A1", token_in=USDG, token_out=WETH, amount_in="10",
                fee_tier=500, max_slippage_bps=100)
    base.update(mut)
    r = TradeRequest.from_dict(base)
    ok, reason = r.validate_shape()
    assert not ok
    assert expect in reason


def test_atomic_roundtrip():
    assert to_atomic("10", 6) == 10_000_000
    assert to_atomic("1.5", 18) == 1_500_000_000_000_000_000
    assert from_atomic(10_000_000, 6) == 1 * 10  # Decimal(10)


def test_value_base_leg_usdg_and_weth():
    assert pricing.value_base_leg(USDG, 10_000_000, 6, USDG, WETH) == 10.0
    # 1 WETH at $2000 = $2000 notional
    assert pricing.value_base_leg(WETH, 10**18, 18, USDG, WETH, eth_usd=2000.0) == 2000.0
    with pytest.raises(pricing.PricingError):
        pricing.value_base_leg(TOKEN, 10**18, 18, USDG, WETH, eth_usd=2000.0)


# ---- service with a mocked adapter --------------------------------------

class FakeAdapter:
    """Deterministic stand-in for DexAdapter (no network)."""
    DEC = {USDG.lower(): 6, WETH.lower(): 18, TOKEN.lower(): 18}
    SYM = {USDG.lower(): "USDG", WETH.lower(): "WETH", TOKEN.lower(): "TKN"}

    def __init__(self, expected_out=5_000_000_000_000_000):
        self.expected_out = expected_out

    def token_metadata(self, token):
        # Mirror DexAdapter: native ETH resolves without a contract call.
        if is_native_eth(token):
            return dict(ETH_METADATA)
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x0000000000000000000000000000000000000Pool"

    def quote_exact_input_single(self, ti, to, amt, fee, tick_spacing=None, hooks=None):
        return {"amount_out": self.expected_out, "sqrt_after": 0,
                "ticks_crossed": 1, "gas_estimate": 90000}


def _make_policy():
    """Create a minimal policy that escalates all trades."""
    trading_rules = SimpleNamespace(
        enabled=True,
        per_trade_max_usd=1000.0,
        daily_volume_limit_usd=10000.0,
        max_slippage_percent=5.0,
        max_price_impact_percent=1000.0,  # not under test here
        min_reserve_eth=0.0,
        auto_approve_below_usd=None,  # No auto-approve -> escalates
    )
    return SimpleNamespace(trading_rules=trading_rules)


def _service(monkeypatch, adapter=None):
    adapter = adapter or FakeAdapter()
    policy = _make_policy()
    agent = SimpleNamespace(
        id="A1", name="Bot", wallet_address="0x00000000000000000000000000000000000000A1",
        policy_id="P1", trading_volume_today_usd=0.0, last_trading_reset_date=None, last_trading_reset_at="",
        reset_daily_trading_volume=lambda: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None)
    svc = TradingService()
    svc.set_stores(store)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc


def test_prepare_trade_slippage_and_notional(monkeypatch):
    svc = _service(monkeypatch, FakeAdapter(expected_out=1_000_000))
    r = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)  # 1% slippage, USDG in
    q = svc.prepare_trade(r)
    assert q.amount_in_atomic == 10_000_000
    assert q.amount_out_expected == 1_000_000
    assert q.amount_out_min == 990_000          # 1% below expected
    assert q.notional_usdg == 10.0              # valued off the USDG leg
    assert q.effective_slippage_bps == 100


def test_handle_trade_request_escalates(monkeypatch):
    svc = _service(monkeypatch)
    fired = []
    svc.set_callbacks(on_approval_needed=lambda req, q: fired.append(req))
    resp = svc.handle_trade_request("A1", {"token_in": USDG, "token_out": WETH,
                                           "amount_in": "10", "fee_tier": 500,
                                           "max_slippage_bps": 100})
    assert resp["status"] == "pending"
    assert resp["quote"]["amount_out_min"] > 0
    assert len(fired) == 1


def test_handle_trade_request_unknown_agent(monkeypatch):
    svc = _service(monkeypatch)
    resp = svc.handle_trade_request("NOPE", {"token_in": USDG, "token_out": WETH,
                                             "amount_in": "10", "fee_tier": 500,
                                             "max_slippage_bps": 100})
    assert resp["code"] == "UNKNOWN_AGENT"


def test_handle_trade_request_rejects_bad_shape(monkeypatch):
    svc = _service(monkeypatch)
    resp = svc.handle_trade_request("A1", {"token_in": USDG, "token_out": USDG,
                                           "amount_in": "10", "fee_tier": 500,
                                           "max_slippage_bps": 100})
    assert resp["status"] == "rejected"
    assert "same" in resp["reason"]


# ---- auth (real verify_agent_signature through the service) --------------

from primer_vault.models import hash_bearer_token          # noqa: E402
from primer_vault.services.signing import SigningService   # noqa: E402

TRADE = {"token_in": USDG, "token_out": WETH, "amount_in": "10",
         "fee_tier": 500, "max_slippage_bps": 100}


def _authed_service(monkeypatch, token="AT_secrettoken123456", status="active"):
    policy = _make_policy()
    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status=status,
        wallet_address="0x00000000000000000000000000000000000000A1",
        auth_key=hash_bearer_token(token),
        policy_id="P1", trading_volume_today_usd=0.0, last_trading_reset_date=None, last_trading_reset_at="",
        reset_daily_trading_volume=lambda: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_auth_verifier(SigningService().verify_agent_signature)  # the real verifier
    wallet = SimpleNamespace(
        data_key=bytes(32),
        get_address_by_address=lambda a: SimpleNamespace(
            id="A001", address=agent.wallet_address, is_hardware=False),
        get_private_key=lambda _id: bytes(32))
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": FakeAdapter())
    return svc, token


def test_trade_auth_bearer_ok(monkeypatch):
    svc, token = _authed_service(monkeypatch)
    resp = svc.handle_trade_request("A1", dict(TRADE), signature=token)
    assert resp["status"] == "pending"


def test_trade_auth_bad_token(monkeypatch):
    svc, _ = _authed_service(monkeypatch)
    resp = svc.handle_trade_request("A1", dict(TRADE), signature="AT_wrongtoken")
    assert resp["code"] == "AUTH_FAILED"


def test_trade_auth_missing_signature(monkeypatch):
    svc, _ = _authed_service(monkeypatch)
    resp = svc.handle_trade_request("A1", dict(TRADE), signature=None)
    assert resp["code"] == "AUTH_FAILED"


def test_trade_auth_suspended_agent(monkeypatch):
    svc, token = _authed_service(monkeypatch, status="suspended")
    resp = svc.handle_trade_request("A1", dict(TRADE), signature=token)
    assert resp["code"] == "AGENT_SUSPENDED"


def test_approve_reject_flow(monkeypatch):
    svc, token = _authed_service(monkeypatch)
    resp = svc.handle_trade_request("A1", dict(TRADE), signature=token)
    rid = resp["request_id"]
    assert svc.get_trade_status(rid)["status"] == "pending"
    rejected = svc.reject_trade(rid, "no thanks")
    assert rejected["status"] == "rejected"
    assert svc.get_trade_status(rid)["status"] == "rejected"


# ---- wrap / unwrap valuation --------------------------------------------

class TestWrapValuation:
    """ETH <-> WETH moves value 1:1, so there is no price change - but the amount
    moved still has a dollar value, and the policy caps govern how much an agent
    may move. A wrap carrying no notional would escalate whatever the
    auto-approve threshold was, and count nothing toward the daily cap.
    """

    ETH_USD = 2000.0

    def _quote(self, monkeypatch, token_in, token_out, amount, eth_usd=None):
        svc = _service(monkeypatch)
        monkeypatch.setattr(pricing, "get_eth_usd",
                            lambda: self.ETH_USD if eth_usd is None else eth_usd)
        r = TradeRequest.create("A1", token_in, token_out, amount, 0, 0)
        return svc.prepare_trade(r)

    def test_wrap_is_valued_at_the_eth_price(self, monkeypatch):
        q = self._quote(monkeypatch, "ETH", WETH, "0.5")
        assert q.pool == "WRAP"
        assert q.notional_usdg == pytest.approx(0.5 * self.ETH_USD)

    def test_unwrap_is_valued_at_the_eth_price(self, monkeypatch):
        q = self._quote(monkeypatch, WETH, "ETH", "2")
        assert q.pool == "UNWRAP"
        assert q.notional_usdg == pytest.approx(2 * self.ETH_USD)

    def test_wrap_is_still_one_to_one(self, monkeypatch):
        """Valuing it must not disturb the conversion or slippage."""
        q = self._quote(monkeypatch, "ETH", WETH, "0.5")
        assert q.amount_out_expected == q.amount_in_atomic
        assert q.amount_out_min == q.amount_in_atomic
        assert q.effective_slippage_bps == 0

    def test_unpriceable_wrap_falls_back_to_none(self, monkeypatch):
        """No ETH price -> no notional -> the policy gate escalates, as elsewhere."""
        def boom():
            raise pricing.PricingError("no price feed")
        svc = _service(monkeypatch)
        monkeypatch.setattr(pricing, "get_eth_usd", boom)
        r = TradeRequest.create("A1", "ETH", WETH, "0.5", 0, 0)
        assert svc.prepare_trade(r).notional_usdg is None


class TestWrapPolicyGate:
    """The point of pricing wraps: they now obey the same limits as any trade."""

    ETH_USD = 2000.0

    def _decide(self, monkeypatch, amount, per_trade_max=1000.0,
                auto_approve=None, volume_today=0.0, daily_limit=10000.0):
        adapter = FakeAdapter()
        rules = SimpleNamespace(
            enabled=True,
            per_trade_max_usd=per_trade_max,
            daily_volume_limit_usd=daily_limit,
            max_slippage_percent=5.0,
        max_price_impact_percent=1000.0,  # not under test here
            min_reserve_eth=0.0,
            auto_approve_below_usd=auto_approve,
        )
        policy = SimpleNamespace(trading_rules=rules)
        agent = SimpleNamespace(
            id="A1", name="Bot", wallet_address="0x" + "a1" * 20, policy_id="P1",
            trading_volume_today_usd=volume_today, last_trading_reset_date=None, last_trading_reset_at="",
            reset_daily_trading_volume=lambda: None,
            add_trading_volume=lambda v: None)
        store = SimpleNamespace(
            get_agent_by_id=lambda aid: agent,
            get_policy=lambda pid: policy,
            update_agent=lambda a: None)
        svc = TradingService()
        svc.set_stores(store)
        monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
        monkeypatch.setattr(pricing, "get_eth_usd", lambda: self.ETH_USD)

        r = TradeRequest.create("A1", "ETH", WETH, amount, 0, 0)
        return svc._evaluate_policy(agent, r, svc.prepare_trade(r))

    def test_small_wrap_auto_approves_under_the_threshold(self, monkeypatch):
        # 0.01 ETH = $20, threshold $100
        d = self._decide(monkeypatch, "0.01", auto_approve=100.0)
        assert d["action"] == "auto"

    def test_large_wrap_escalates_over_the_threshold(self, monkeypatch):
        # 1 ETH = $2000, threshold $100, per-trade cap high enough not to fire first
        d = self._decide(monkeypatch, "1", auto_approve=100.0, per_trade_max=5000.0)
        assert d["action"] == "escalate"

    def test_wrap_is_rejected_above_per_trade_max(self, monkeypatch):
        # 1 ETH = $2000, per-trade max $500
        d = self._decide(monkeypatch, "1", per_trade_max=500.0)
        assert d["action"] == "reject"
        assert "per-trade max" in d["reason"]

    def test_wrap_counts_toward_the_daily_volume_cap(self, monkeypatch):
        # $900 already used, 1 ETH = $2000, cap $1000; per-trade cap kept clear
        d = self._decide(monkeypatch, "1", volume_today=900.0, daily_limit=1000.0,
                         per_trade_max=5000.0)
        assert d["action"] == "reject"
        assert "daily limit" in d["reason"]

    def test_threshold_of_zero_still_catches_every_wrap(self, monkeypatch):
        """auto_approve of 0 means 'ask me about everything'."""
        d = self._decide(monkeypatch, "0.000001", auto_approve=0.0)
        assert d["action"] == "escalate"

    def test_no_threshold_still_catches_every_wrap(self, monkeypatch):
        d = self._decide(monkeypatch, "0.000001", auto_approve=None)
        assert d["action"] == "escalate"
