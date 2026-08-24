"""Trade deadline enforcement and the price-impact probe floor.

1. A trade request may carry a `deadline` (models/trade.py, accepted from the
   agent payload at models/trade.py). Nothing ever reads it. A request whose
   deadline has already passed is quoted, gated and executed as if it had none.

2. The price-impact probe is `max(1, amount_in_atomic // 10_000)`. When the
   trade is one atomic unit, the probe IS the trade, so the fill is compared
   against itself and the impact reads as the pool fee alone.
"""

import time
from types import SimpleNamespace


from primer_vault.models.trade import TradeRequest
from primer_vault.services.trading import TradingService
from primer_vault.networks import get_dex, TOKENS, is_native_eth

USDG = TOKENS["USDG"].addresses[4663]
WETH = get_dex(4663).weth
WALLET = "0x00000000000000000000000000000000000000A1"

#: A token reporting 0 decimals: one atomic unit is one whole token.
COARSE = "0x" + "44" * 20


class DeepPool:
    """A healthy USDG/WETH pool: plenty of liquidity, ordinary rate."""

    DEC = {USDG.lower(): 6, WETH.lower(): 18}
    SYM = {USDG.lower(): "USDG", WETH.lower(): "WETH"}

    def token_metadata(self, token):
        if is_native_eth(token):
            return {"address": token, "symbol": "ETH", "name": "Ether", "decimals": 18}
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x0000000000000000000000000000000000000Pool"

    def quote_exact_input_single(self, ti, to, amt, fee, tick_spacing=None, hooks=None):
        # 1 USDG (1e6) -> 0.0005 WETH (5e14). Linear, so no impact at any size.
        return {"amount_out": amt * 500_000_000, "sqrt_after": 0,
                "ticks_crossed": 1, "gas_estimate": 90_000}

    def native_balance(self, owner):
        return 10 ** 18


class OneUnitPool:
    """Constant-product COARSE/USDG pool holding a single COARSE token.

    Selling one COARSE into it takes roughly half the USDG reserve, so the fill
    is about half the pool's own marginal rate: a real impact near 50%. The
    reserve is small enough that the proceeds land under the auto-approve
    threshold, which is the arrangement the impact check exists to stop.
    """

    FEE = 0.003
    X = 1                      # COARSE reserve, atomic (0 decimals)
    Y = 100 * 10 ** 6          # USDG reserve, atomic (6 decimals)

    DEC = {COARSE.lower(): 0, USDG.lower(): 6, WETH.lower(): 18}
    SYM = {COARSE.lower(): "CRS", USDG.lower(): "USDG", WETH.lower(): "WETH"}

    def token_metadata(self, token):
        if is_native_eth(token):
            return {"address": token, "symbol": "ETH", "name": "Ether", "decimals": 18}
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x0000000000000000000000000000000000000Pool"

    def quote_exact_input_single(self, ti, to, amount_in, fee,
                                 tick_spacing=None, hooks=None):
        paid = amount_in * (1 - self.FEE)
        out = int(self.Y * paid / (self.X + paid))
        return {"amount_out": out, "sqrt_after": 0, "ticks_crossed": 1,
                "gas_estimate": 90_000}

    def native_balance(self, owner):
        return 10 ** 18


def _service(monkeypatch, adapter, auto_approve=5_000.0):
    rules = SimpleNamespace(
        enabled=True,
        per_trade_max_usd=1_000_000.0,
        daily_volume_limit_usd=10_000_000.0,
        max_slippage_percent=5.0,
        max_price_impact_percent=5.0,
        min_reserve_eth=0.0,
        auto_approve_below_usd=auto_approve,
    )
    policy = SimpleNamespace(id="P1", trading_rules=rules)
    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", wallet_address=WALLET, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None,
        last_trading_reset_at="", reset_daily_trading_volume=lambda: None,
        add_trading_volume=lambda v: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None)
    svc = TradingService()
    svc.set_stores(store)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc, agent


# ---------------------------------------------------------------------------
# 1. An expired deadline is ignored
# ---------------------------------------------------------------------------

def test_expired_deadline_is_not_executable(monkeypatch):
    """An agent that says "not after T" must not have its trade run after T."""
    svc, agent = _service(monkeypatch, DeepPool())
    request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100,
                                  wallet_address=WALLET,
                                  deadline=int(time.time()) - 3600)
    request.agent_id = "A1"

    quote = svc.prepare_trade(request)
    decision = svc._evaluate_policy(agent, request, quote)

    assert decision["action"] == "reject", (
        f"a trade whose deadline passed an hour ago was decided "
        f"{decision['action']!r}: {decision['reason']!r}")


# ---------------------------------------------------------------------------
# 2. A one-atomic-unit trade is compared against itself
# ---------------------------------------------------------------------------

def test_single_atomic_unit_trade_impact_is_unmeasurable_not_just_the_fee(monkeypatch):
    """A one-atomic-unit trade cannot be probed below itself, so its impact is
    genuinely unmeasurable - the pool's near-spot rate needs a smaller quote
    that does not exist. It must report None (→ escalate), never the pool fee
    as if the fill were healthy."""
    svc, _ = _service(monkeypatch, OneUnitPool())
    adapter = OneUnitPool()
    request = TradeRequest.create("A1", COARSE, USDG, "1", 3000, 100,
                                  wallet_address=WALLET)
    expected_out = adapter.quote_exact_input_single(COARSE, USDG, 1, 3000)["amount_out"]

    impact = svc._price_impact_pct(adapter, request, COARSE, USDG, 1, expected_out)

    assert impact is None, (
        f"a 1-unit trade's impact cannot be measured, but it reported {impact}%")


def test_single_atomic_unit_trade_is_not_auto_approved(monkeypatch):
    """The same trade must not be waved through under the auto-approve threshold."""
    svc, agent = _service(monkeypatch, OneUnitPool())
    request = TradeRequest.create("A1", COARSE, USDG, "1", 3000, 100,
                                  wallet_address=WALLET)
    request.agent_id = "A1"

    quote = svc.prepare_trade(request)
    decision = svc._evaluate_policy(agent, request, quote)

    assert decision["action"] != "auto", (
        f"1 CRS sold for ${quote.notional_usdg:.2f} was auto-approved; "
        f"impact reported as {quote.price_impact_pct}%")
