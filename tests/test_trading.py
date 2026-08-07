"""
Trading engine tests — models, valuation math, and the service's quote/gate flow.

These use a mocked DEX adapter so the suite never touches the network. A separate
live check (scripts, run manually) exercises the real RHC path.
"""

from types import SimpleNamespace

import pytest

from primer_vault.models.trade import TradeRequest, MAX_SLIPPAGE_BPS
from primer_vault.services.dex import to_atomic, from_atomic
from primer_vault.services import pricing
from primer_vault.services.trading import TradingService
from primer_vault.networks import get_dex, TOKENS

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
        min_reserve_eth=0.0,
        auto_approve_below_usd=None,  # No auto-approve -> escalates
    )
    return SimpleNamespace(trading_rules=trading_rules)


def _service(monkeypatch, adapter=None):
    adapter = adapter or FakeAdapter()
    policy = _make_policy()
    agent = SimpleNamespace(
        id="A1", name="Bot", wallet_address="0x00000000000000000000000000000000000000A1",
        policy_id="P1", trading_volume_today_usd=0.0, last_trading_reset_date=None,
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
        wallet_id="W1",
        auth_key=hash_bearer_token(token),
        policy_id="P1", trading_volume_today_usd=0.0, last_trading_reset_date=None,
        reset_daily_trading_volume=lambda: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_agent=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_auth_verifier(SigningService().verify_agent_signature)  # the real verifier
    svc.set_wallet_provider(lambda addr: object())
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
