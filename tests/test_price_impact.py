"""Price impact: the only check that looks at the rate rather than the size.

The agent names the pool it wants to trade through, and every other rule is
blind to that choice. Slippage compares the pool against itself, so a bad pool
passes. The notional is read off whichever leg is USDG or WETH - usually the
input - so a trade that returns almost nothing still reports the size the agent
asked for. Without this check a pool too thin for the trade fills at a fraction
of its worth and every rule reports compliance.

Measured by quoting a dust amount to learn the rate without moving the price,
comparing the real fill against it, and adding the tier's fee.

These drive real constant-product maths rather than a stub, because the property
being tested is arithmetic and a fixed-output stub cannot express it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.trade import TradeRequest
from primer_vault.services.dex import DexError
from primer_vault.services.trading import TradingService

TOKEN_IN = "0x" + "11" * 20
TOKEN_OUT = "0x" + "22" * 20
USDG_UNIT = 10 ** 6      # 6 decimals
WETH_UNIT = 10 ** 18


def pool(usdg_reserve, weth_reserve, fee_fraction):
    """A constant-product pool, quoted the way the real adapter quotes one."""
    x = usdg_reserve * USDG_UNIT
    y = weth_reserve * WETH_UNIT

    class Adapter:
        def quote_exact_input_single(self, token_in, token_out, amount_in, fee, **kwargs):
            paid = amount_in * (1 - fee_fraction)
            return {"amount_out": int(y * paid / (x + paid)), "gas_estimate": 0}

    return Adapter()


def impact_of(adapter, fee_tier, amount_in_units=1):
    """Measured impact for a trade of `amount_in_units` USDG through `adapter`."""
    svc = TradingService()
    amount_in = amount_in_units * USDG_UNIT
    request = TradeRequest.create("A1", TOKEN_IN, TOKEN_OUT, str(amount_in_units), fee_tier, 100)
    expected_out = adapter.quote_exact_input_single(
        TOKEN_IN, TOKEN_OUT, amount_in, fee_tier)["amount_out"]
    return svc._price_impact_pct(adapter, request, TOKEN_IN, TOKEN_OUT, amount_in, expected_out)


class TestDeepPools:
    """A pool with room for the trade costs its fee and nothing else."""

    @pytest.mark.parametrize("fee_tier, fee, expected_pct", [
        (100, 0.0001, 0.01),
        (500, 0.0005, 0.05),
        (3000, 0.003, 0.30),
        (10000, 0.01, 1.00),
    ])
    def test_impact_is_the_fee(self, fee_tier, fee, expected_pct):
        measured = impact_of(pool(1_000_000, 521.8, fee), fee_tier)
        assert measured == pytest.approx(expected_pct, abs=0.02)


class TestThinPools:
    """Depth, not the fee, is what makes a fill bad."""

    def test_a_pool_too_thin_for_the_trade_is_caught(self):
        # 0.01 USDG of depth against a 1 USDG trade.
        assert impact_of(pool(0.01, 0.0000052, 0.0001), 100) > 90

    def test_a_moderately_thin_pool_is_not_alarming(self):
        assert impact_of(pool(100, 0.05218, 0.0005), 500) < 5

    def test_impact_rises_with_trade_size_in_the_same_pool(self):
        p = pool(100, 0.05218, 0.0005)
        small = impact_of(p, 500, amount_in_units=1)
        large = impact_of(p, 500, amount_in_units=50)
        assert large > small * 5, "a bigger trade in the same pool must read worse"


class TestADeadFeeTier:
    """A fee-10000 pool thin enough to fill 99.4% worse, which every other rule
    passes."""

    def test_the_dead_tier_escalates_while_the_good_one_does_not(self):
        healthy = impact_of(pool(1_000_000, 521.8, 0.0001), 100)
        dead = impact_of(pool(0.01, 0.0000052, 0.01), 10000)

        assert healthy < 5, "a normal trade must not be escalated"
        assert dead > 5, "a pool this thin must be caught"


class TestUnmeasurable:
    """Unknown is not the same as fine."""

    def test_a_pool_that_will_not_quote_returns_none(self):
        class Broken:
            def quote_exact_input_single(self, *args, **kwargs):
                raise DexError("no liquidity")

        svc = TradingService()
        request = TradeRequest.create("A1", TOKEN_IN, TOKEN_OUT, "1", 500, 100)
        assert svc._price_impact_pct(
            Broken(), request, TOKEN_IN, TOKEN_OUT, USDG_UNIT, 500) is None

    def test_a_zero_output_quote_returns_none(self):
        svc = TradingService()
        request = TradeRequest.create("A1", TOKEN_IN, TOKEN_OUT, "1", 500, 100)
        assert svc._price_impact_pct(
            pool(1_000_000, 521.8, 0.0005), request, TOKEN_IN, TOKEN_OUT,
            USDG_UNIT, 0) is None


class TestPolicyGate:
    """The measurement is only useful if the decision uses it."""

    def _decide(self, impact, limit):
        from types import SimpleNamespace
        svc = TradingService()
        rules = SimpleNamespace(
            enabled=True, per_trade_max_usd=1000.0, daily_volume_limit_usd=10000.0,
            auto_approve_below_usd=1000.0, min_reserve_eth=0.0,
            max_slippage_percent=50.0, max_price_impact_percent=limit)
        policy = SimpleNamespace(id="p", trading_rules=rules)
        agent = SimpleNamespace(id="A1", policy_id="p", trading_volume_today_usd=0.0,
                                last_trading_reset_date="", last_trading_reset_at="", wallet_address=None,
                                reset_daily_trading_volume=lambda: None)
        svc._policy_store = SimpleNamespace(
            get_policy=lambda i: policy, update_agent=lambda a: None)
        request = TradeRequest.create("A1", TOKEN_IN, TOKEN_OUT, "1", 500, 100)
        quote = SimpleNamespace(notional_usdg=10.0, price_impact_pct=impact)
        return svc._evaluate_policy(agent, request, quote)

    def test_within_the_limit_auto_approves(self):
        assert self._decide(impact=1.0, limit=5.0)["action"] == "auto"

    def test_over_the_limit_escalates_rather_than_rejecting(self):
        """A deliberately expensive trade is the user's call, not Vault's."""
        decision = self._decide(impact=99.4, limit=5.0)
        assert decision["action"] == "escalate"
        assert "99.4" in decision["reason"] and "5.0" in decision["reason"]

    def test_unmeasurable_impact_escalates(self):
        decision = self._decide(impact=None, limit=5.0)
        assert decision["action"] == "escalate"

    def test_exactly_at_the_limit_is_allowed(self):
        assert self._decide(impact=5.0, limit=5.0)["action"] == "auto"
