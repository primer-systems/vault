"""
Minimum ETH balance floor tests.

A trading policy can set `min_reserve_eth`: trading halts while the wallet's ETH
balance is below that figure, so the user keeps gas money to work with. The check
compares the current balance against the floor - it does not model what the trade
will cost.

The existing trading tests set `min_reserve_eth=0.0`, which switches this off, so
without this file the whole branch is unexercised.
"""

from types import SimpleNamespace

import pytest

from primer_vault.models.trade import TradeRequest
from primer_vault.services.dex import ETH_METADATA
from primer_vault.services.trading import TradingService
from primer_vault.networks import get_dex, TOKENS, is_native_eth

USDG = TOKENS["USDG"].addresses[4663]
WETH = get_dex(4663).weth

WALLET = "0x00000000000000000000000000000000000000A1"
FLOOR = 0.01  # ETH


def eth(amount: float) -> int:
    """Human ETH -> wei."""
    return int(amount * 1e18)


class FakeAdapter:
    """Deterministic stand-in for DexAdapter. `balance` is in wei.

    Passing an Exception instance for `balance` makes native_balance raise it,
    which is how the RPC-failure path is driven.
    """

    DEC = {USDG.lower(): 6, WETH.lower(): 18}
    SYM = {USDG.lower(): "USDG", WETH.lower(): "WETH"}

    def __init__(self, balance=None):
        self.balance = eth(1.0) if balance is None else balance
        self.balance_calls = 0

    def token_metadata(self, token):
        # Mirror DexAdapter: native ETH resolves without a contract call.
        if is_native_eth(token):
            return dict(ETH_METADATA)
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x0000000000000000000000000000000000000Pool"

    def quote_exact_input_single(self, ti, to, amt, fee, tick_spacing=None, hooks=None):
        return {"amount_out": 5_000_000_000_000_000, "sqrt_after": 0,
                "ticks_crossed": 1, "gas_estimate": 90000}

    def native_balance(self, owner):
        self.balance_calls += 1
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance


def _service(monkeypatch, adapter, floor=FLOOR, auto_approve=None):
    rules = SimpleNamespace(
        enabled=True,
        per_trade_max_usd=1000.0,
        daily_volume_limit_usd=10000.0,
        max_slippage_percent=5.0,
            max_price_impact_percent=1000.0,  # not under test here
        min_reserve_eth=floor,
        auto_approve_below_usd=auto_approve,
    )
    policy = SimpleNamespace(trading_rules=rules)
    agent = SimpleNamespace(
        id="A1", name="Bot", wallet_address=WALLET, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None, last_trading_reset_at="",
        reset_daily_trading_volume=lambda: None,
        add_trading_volume=lambda v: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None)
    svc = TradingService()
    svc.set_stores(store)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc, agent


def _decide(monkeypatch, balance, floor=FLOOR, auto_approve=None,
            token_in=USDG, amount_in="10"):
    """Run a trade through the policy gate. Returns the decision dict."""
    adapter = FakeAdapter(balance=balance)
    svc, agent = _service(monkeypatch, adapter, floor=floor, auto_approve=auto_approve)
    request = TradeRequest.create("A1", token_in, WETH, amount_in, 500, 100)
    request.wallet_address = WALLET
    quote = svc.prepare_trade(request)
    return svc._evaluate_policy(agent, request, quote), adapter


# -------------------------------------------------------------------------
# Balance vs floor
# -------------------------------------------------------------------------

class TestBalanceAgainstFloor:

    def test_balance_above_floor_is_allowed(self, monkeypatch):
        decision, _ = _decide(monkeypatch, balance=eth(1.0))
        assert decision["action"] != "reject"

    def test_balance_below_floor_is_rejected(self, monkeypatch):
        decision, _ = _decide(monkeypatch, balance=eth(0.005))
        assert decision["action"] == "reject"
        assert "below the policy minimum" in decision["reason"]

    def test_balance_exactly_at_floor_is_allowed(self, monkeypatch):
        """The floor is a minimum to hold, not a level to stay above."""
        decision, _ = _decide(monkeypatch, balance=eth(FLOOR))
        assert decision["action"] != "reject"

    def test_balance_marginally_under_floor_is_rejected(self, monkeypatch):
        """The comparison happens in float ETH, not wei, so a wei-level margin
        is below the resolution of the conversion. This uses a margin a user
        could actually observe."""
        decision, _ = _decide(monkeypatch, balance=eth(FLOOR - 0.0001))
        assert decision["action"] == "reject"

    def test_zero_balance_is_rejected(self, monkeypatch):
        decision, _ = _decide(monkeypatch, balance=0)
        assert decision["action"] == "reject"

    def test_reason_names_balance_and_floor(self, monkeypatch):
        """The message has to tell the user which number to move."""
        decision, _ = _decide(monkeypatch, balance=eth(0.004), floor=0.02)
        assert "0.004000" in decision["reason"]
        assert "0.020000" in decision["reason"]


# -------------------------------------------------------------------------
# The check is on current balance only
# -------------------------------------------------------------------------

class TestNoTradeCostModelling:
    """The floor is a circuit breaker on the balance as it stands.

    It deliberately does not estimate gas or subtract the traded amount - those
    produced a fictitious gas figure and an unenforced floor on ETH-input trades
    respectively. These tests pin the simpler contract down.
    """

    def test_trade_size_does_not_affect_the_decision(self, monkeypatch):
        small, _ = _decide(monkeypatch, balance=eth(1.0), amount_in="1")
        large, _ = _decide(monkeypatch, balance=eth(1.0), amount_in="900")
        assert small["action"] == large["action"] != "reject"

    def test_eth_input_trade_judged_on_current_balance(self, monkeypatch):
        """An ETH-input trade is gated the same way: balance now, vs floor."""
        decision, _ = _decide(monkeypatch, balance=eth(1.0), token_in="ETH",
                              amount_in="0.5")
        assert decision["action"] != "reject"

    def test_balance_just_above_floor_allowed_regardless_of_trade_size(self, monkeypatch):
        """No phantom gas margin is deducted before the comparison."""
        decision, _ = _decide(monkeypatch, balance=eth(FLOOR) + 1, amount_in="500")
        assert decision["action"] != "reject"


# -------------------------------------------------------------------------
# Unreadable balance blocks
# -------------------------------------------------------------------------

class TestBalanceUnreadable:
    """If the balance cannot be read the floor cannot be honoured, so the trade
    is refused rather than waved through."""

    @pytest.mark.parametrize("error", [
        ConnectionError("RPC unreachable"),
        TimeoutError("read timed out"),
        ValueError("garbage response"),
        Exception("429 Too Many Requests"),
    ])
    def test_rpc_failure_rejects(self, monkeypatch, error):
        decision, _ = _decide(monkeypatch, balance=error)
        assert decision["action"] == "reject"
        assert "Could not read" in decision["reason"]

    def test_rpc_failure_rejects_even_under_auto_approve_threshold(self, monkeypatch):
        """A small trade that would otherwise auto-execute must not skip the
        floor."""
        decision, _ = _decide(monkeypatch, balance=ConnectionError("boom"),
                              auto_approve=10_000.0)
        assert decision["action"] == "reject"

    def test_failure_message_points_at_the_way_out(self, monkeypatch):
        decision, _ = _decide(monkeypatch, balance=ConnectionError("boom"))
        assert "0" in decision["reason"]
        assert "minimum ETH" in decision["reason"]


# -------------------------------------------------------------------------
# Disabling the check
# -------------------------------------------------------------------------

class TestFloorDisabled:

    def test_zero_floor_skips_the_check_entirely(self, monkeypatch):
        """Floor of 0 means no floor: no balance call, no rejection."""
        decision, adapter = _decide(monkeypatch, balance=eth(0.0), floor=0.0)
        assert decision["action"] != "reject"
        assert adapter.balance_calls == 0

    def test_zero_floor_survives_an_rpc_failure(self, monkeypatch):
        """With the check off, an unreachable RPC must not block trading."""
        decision, adapter = _decide(monkeypatch, balance=ConnectionError("boom"),
                                    floor=0.0)
        assert decision["action"] != "reject"
        assert adapter.balance_calls == 0

    def test_missing_recipient_skips_the_check(self, monkeypatch):
        """Nothing to look up a balance for."""
        adapter = FakeAdapter(balance=eth(0.0))
        svc, agent = _service(monkeypatch, adapter)
        request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)
        request.wallet_address = None
        quote = svc.prepare_trade(request)
        decision = svc._evaluate_policy(agent, request, quote)
        assert decision["action"] != "reject"
        assert adapter.balance_calls == 0


# -------------------------------------------------------------------------
# Ordering against the other policy checks
# -------------------------------------------------------------------------

class TestOrdering:

    def test_size_rejection_takes_precedence(self, monkeypatch):
        """A trade that breaks a cheaper check should not need an RPC call."""
        adapter = FakeAdapter(balance=eth(1.0))
        rules = SimpleNamespace(
            enabled=True,
            per_trade_max_usd=1.0,            # 10 USDG trade blows this
            daily_volume_limit_usd=10000.0,
            max_slippage_percent=5.0,
            max_price_impact_percent=1000.0,  # not under test here
            min_reserve_eth=FLOOR,
            auto_approve_below_usd=None,
        )
        policy = SimpleNamespace(trading_rules=rules)
        agent = SimpleNamespace(
            id="A1", name="Bot", wallet_address=WALLET, policy_id="P1",
            trading_volume_today_usd=0.0, last_trading_reset_date=None, last_trading_reset_at="",
            reset_daily_trading_volume=lambda: None,
            add_trading_volume=lambda v: None)
        store = SimpleNamespace(
            get_agent_by_id=lambda aid: agent,
            get_policy=lambda pid: policy,
            update_agent=lambda a: None)
        svc = TradingService()
        svc.set_stores(store)
        monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)

        request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)
        request.wallet_address = WALLET
        quote = svc.prepare_trade(request)
        decision = svc._evaluate_policy(agent, request, quote)

        assert decision["action"] == "reject"
        assert "per-trade max" in decision["reason"]
        assert adapter.balance_calls == 0
