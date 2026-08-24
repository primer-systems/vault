"""A swap whose input leg is native ETH is valued against the trading limits.

Native ETH is written "ETH" or address(0) rather than as a token address, so it
must still be priced through the same path a wrap uses. Without a notional the
per-trade maximum is skipped, the daily-volume reservation is never reached, and
nothing is recorded at broadcast.
"""

from types import SimpleNamespace

import pytest

from primer_vault.models.trade import TradeRequest
from primer_vault.services import pricing
from primer_vault.services.dex import ETH_METADATA
from primer_vault.services.trading import TradingService
from primer_vault.networks import get_dex, TOKENS, is_native_eth

USDG = TOKENS["USDG"].addresses[4663]
WETH = get_dex(4663).weth
TOKEN = "0x1111111111111111111111111111111111111111"
AGENT_ADDR = "0x" + "a1" * 20
ETH_USD = 2000.0


class Adapter:
    DEC = {USDG.lower(): 6, WETH.lower(): 18, TOKEN.lower(): 18}
    SYM = {USDG.lower(): "USDG", WETH.lower(): "WETH", TOKEN.lower(): "TKN"}

    def __init__(self):
        self.sent = []

    def token_metadata(self, token):
        if is_native_eth(token):
            return dict(ETH_METADATA)
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, ti, to, amt, fee, tick_spacing=None, hooks=None):
        # 1 ETH -> 2000 USDG-ish; scale linearly so the dust probe behaves.
        return {"amount_out": amt * 2000 // 10 ** 12, "gas_estimate": 90000}

    def native_balance(self, addr):
        return 10 ** 20

    def allowance(self, token, owner, spender):
        return 10 ** 30

    def router_address(self):
        return "0x" + "dd" * 20

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    def build_swap_tx(self, *a, **k):
        return {"to": self.router_address()}

    def simulate_swap(self, *a, **k):
        return None

    def sign_and_send(self, tx, pkey, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0x" + ("%02x" % (len(self.sent) + 1)) * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        return None


def _service(monkeypatch, *, daily_limit=500.0, per_trade_max=1000.0,
             auto_below=None):
    rules = SimpleNamespace(
        enabled=True, per_trade_max_usd=per_trade_max,
        daily_volume_limit_usd=daily_limit, max_slippage_percent=5.0,
        max_price_impact_percent=1000.0, min_reserve_eth=0.0,
        auto_approve_below_usd=auto_below)
    policy = SimpleNamespace(trading_rules=rules, id="P1")

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status="active",
        wallet_address=AGENT_ADDR, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None,
        last_trading_reset_at="", reset_daily_trading_volume=lambda: None)

    def add_volume(usd):
        agent.trading_volume_today_usd += usd
    agent.add_trading_volume = add_volume

    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None, add_transaction=lambda tx: None,
        update_transaction=lambda tx: None)

    entry = SimpleNamespace(id="A001", address=AGENT_ADDR, is_hardware=False)
    wallet = SimpleNamespace(
        data_key=bytes(32),
        get_address_by_address=lambda a: entry if a.lower() == AGENT_ADDR.lower() else None,
        get_private_key=lambda _id: bytes(32))

    adapter = Adapter()
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    monkeypatch.setattr(pricing, "get_eth_usd", lambda *a, **k: ETH_USD)
    return svc, agent, adapter


class TestNativeEthInputIsValued:

    def test_an_eth_to_token_swap_carries_a_notional(self, monkeypatch):
        """The price feed is healthy and ETH is the one asset Vault prices from
        an off-chain reference. A 1 ETH swap is a $2000 trade."""
        svc, agent, adapter = _service(monkeypatch)
        q = svc.prepare_trade(
            TradeRequest.create("A1", "ETH", TOKEN, "1", 3000, 100,
                                wallet_address=AGENT_ADDR))
        assert q.notional_usdg is not None, (
            "an ETH-input swap was not valued, so neither the per-trade cap "
            "nor the daily volume cap can be applied to it")
        assert q.notional_usdg == pytest.approx(ETH_USD)

    def test_a_wrap_of_the_same_size_is_valued(self, monkeypatch):
        """Control: the wrap path prices the identical asset."""
        svc, agent, adapter = _service(monkeypatch)
        q = svc.prepare_trade(
            TradeRequest.create("A1", "ETH", WETH, "1", 0, 0,
                                wallet_address=AGENT_ADDR))
        assert q.pool == "WRAP"
        assert q.notional_usdg == pytest.approx(ETH_USD)

    def test_an_over_size_eth_swap_is_refused_by_the_per_trade_cap(self, monkeypatch):
        """$100 per-trade max; the agent asks to swap 1 ETH ($2000)."""
        svc, agent, adapter = _service(monkeypatch, per_trade_max=100.0)
        result = svc.handle_trade_request(
            "A1", {"token_in": "ETH", "token_out": TOKEN, "amount_in": "1",
                   "fee_tier": 3000, "max_slippage_bps": 100})
        assert result["status"] == "rejected", (
            "a $2000 ETH swap was not refused by a $100 per-trade cap; it "
            f"came back as {result['status']}")

    def test_eth_swaps_consume_the_daily_volume_allowance(self, monkeypatch):
        """$5000/day cap, each 1-ETH swap $2000 and small enough to auto-execute.
        The allowance admits two ($2000 + $2000), then the third would carry the
        agent to $6000 and is refused. That the cap counts ETH volume at all, and
        stops the run before all five, is the point: an unvalued ETH swap would
        record nothing and every one would go through."""
        svc, agent, adapter = _service(monkeypatch, daily_limit=5000.0,
                                       per_trade_max=10_000.0, auto_below=10_000.0)
        for _ in range(5):
            svc.handle_trade_request(
                "A1", {"token_in": "ETH", "token_out": TOKEN, "amount_in": "1",
                       "fee_tier": 3000, "max_slippage_bps": 100})

        assert agent.trading_volume_today_usd == pytest.approx(4000.0), (
            "%d ETH swaps reached the chain and recorded $%.2f of daily volume"
            % (len(adapter.sent), agent.trading_volume_today_usd))
        assert len(adapter.sent) == 2, (
            "%d swaps worth $%.0f reached the chain under a $5000 daily cap"
            % (len(adapter.sent), 2000.0 * len(adapter.sent)))


class TestAutoApproval:
    """ETH is a base asset Vault can price, so - like USDG and WETH - an ETH swap
    under the auto-approve threshold may execute without a human. Before it was
    valued it had no notional, so the threshold could never be met and every ETH
    swap escalated regardless of size."""

    def test_it_auto_executes_when_under_the_threshold(self, monkeypatch):
        """A $2000 ETH swap under a $1M auto-approve threshold executes straight
        through, the same as the equivalent WETH swap would."""
        svc, agent, adapter = _service(monkeypatch, daily_limit=1_000_000.0,
                                       per_trade_max=10_000.0, auto_below=1_000_000.0)
        r = svc.handle_trade_request(
            "A1", {"token_in": "ETH", "token_out": TOKEN, "amount_in": "1",
                   "fee_tier": 3000, "max_slippage_bps": 100})
        assert r["status"] == "executed", r
        assert len(adapter.sent) == 1

    def test_a_weth_input_swap_of_the_same_size_is_valued(self, monkeypatch):
        """Control: only *native* ETH is affected. WETH is in the base set."""
        svc, agent, adapter = _service(monkeypatch)
        q = svc.prepare_trade(
            TradeRequest.create("A1", WETH, TOKEN, "1", 3000, 100,
                                wallet_address=AGENT_ADDR))
        assert q.notional_usdg == pytest.approx(ETH_USD)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
