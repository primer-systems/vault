"""Trades that cannot be valued in USD are still measured against the policy.

Two properties:

1. The slippage, price-impact and ETH-floor checks do not need a notional, so a
   trade that cannot be valued must still be measured against them rather than
   escalating straight past them.
2. When the output leg is the base asset, the notional is the value of what the
   trade receives; what it spends must still be bounded.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.services import pricing
from primer_vault.services.trading import TradingService

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
FOO = "0x" + "f0" * 20          # an ordinary ERC-20, neither USDG nor WETH
AGENT_ADDR = "0x" + "a1" * 20

DECIMALS = {USDG.lower(): 6, WETH.lower(): 18, FOO.lower(): 18}
SYMBOLS = {USDG.lower(): "USDG", WETH.lower(): "WETH", FOO.lower(): "FOO"}


class FakeAdapter:
    """A DEX with a linear (i.e. infinitely deep) pool at a fixed rate.

    `rate` is atomic-out per atomic-in. Linearity is the point for test 2: a deep
    pool has no price impact whatever its price, so the probe quote and the real
    quote agree and the impact check reads ~fee.
    """

    def __init__(self, rate=2):
        self.rate = rate
        self.sent = []
        self.swaps = []          # (amount_in_atomic, amount_out_min)

    def token_metadata(self, token):
        return {"address": token, "decimals": DECIMALS[token.lower()],
                "symbol": SYMBOLS[token.lower()], "name": SYMBOLS[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        return {"amount_out": int(amount * self.rate), "gas_estimate": 120000}

    def native_balance(self, addr):
        return 10 ** 18

    def allowance(self, token, owner, spender):
        return 10 ** 40

    def router_address(self):
        return "0x" + "dd" * 20

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    def build_swap_tx(self, token_in, token_out, fee, recipient, amount_in_atomic,
                      amount_out_min, sender, **k):
        self.swaps.append((amount_in_atomic, amount_out_min))
        return {"to": self.router_address()}

    def build_swap_to_eth_tx(self, *a, **k):
        return {"to": self.router_address()}

    def simulate_swap(self, *a, **k):
        return None

    def sign_and_send(self, tx, pkey, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0x" + f"{len(self.sent):02x}" * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        return None


def _service(monkeypatch, *, max_slippage_percent=3.0, max_impact=5.0,
             per_trade=100.0, auto_below=None, rate=2, eth_usd=2000.0):
    rules = TradingRules(
        enabled=True, per_trade_max_usd=per_trade, daily_volume_limit_usd=10_000.0,
        auto_approve_below_usd=auto_below, min_reserve_eth=0.0,
        max_slippage_percent=max_slippage_percent,
        max_price_impact_percent=max_impact)
    policy = SpendPolicy.create(name="P", networks=[4663], daily_limit_micro=0,
                                trading_rules=rules)
    policy.id = "P1"

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status="active",
        wallet_address=AGENT_ADDR, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None,
        last_trading_reset_at="", reset_daily_trading_volume=lambda: None)
    lock = threading.Lock()

    def add_volume(usd):
        with lock:
            agent.trading_volume_today_usd += usd
    agent.add_trading_volume = add_volume

    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None)

    entry = SimpleNamespace(id="A001", address=AGENT_ADDR, is_hardware=False)
    wallet = SimpleNamespace(
        data_key=bytes(32),
        get_address_by_address=lambda a: entry if a.lower() == AGENT_ADDR.lower() else None,
        get_private_key=lambda _id: bytes(32))

    adapter = FakeAdapter(rate=rate)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)

    if eth_usd is None:
        def _down(*a, **k):
            raise pricing.PricingError("ETH/USD reference unreachable")
        monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd", _down)
    else:
        monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd",
                            lambda *a, **k: eth_usd)
    return svc, agent, adapter


# ---------------------------------------------------------------------------
# 1. An unvaluable trade skips the policy checks that do not need a valuation
# ---------------------------------------------------------------------------

class TestUnvaluableTradeSkipsSlippageCap:

    def test_control_priced_feed_rejects_slippage_over_the_policy_cap(self, monkeypatch):
        """With the feed up, a 50% slippage request against a 3% cap is refused.

        This is the control: it establishes that the only thing changing in the
        next test is whether the trade could be valued.
        """
        svc, _, adapter = _service(monkeypatch, max_slippage_percent=3.0)
        result = svc.handle_trade_request("A1", {
            "token_in": WETH, "token_out": FOO, "amount_in": "0.001",
            "fee_tier": 500, "max_slippage_bps": 5000})
        assert result["status"] == "rejected"
        assert "slippage" in result["reason"].lower()
        assert adapter.sent == []

    def test_feed_down_the_same_trade_is_never_measured_against_the_cap(self, monkeypatch):
        """Feed down -> notional None -> escalate, before the 3% slippage check."""
        svc, _, adapter = _service(monkeypatch, max_slippage_percent=3.0,
                                   eth_usd=None)
        result = svc.handle_trade_request("A1", {
            "token_in": WETH, "token_out": FOO, "amount_in": "0.001",
            "fee_tier": 500, "max_slippage_bps": 5000})

        assert result["status"] == "rejected", (
            "a 50% slippage request against a 3% policy cap must be refused "
            f"whether or not it can be priced; got {result['status']} "
            f"({result.get('reason')})")

    def test_approving_it_sends_a_swap_with_a_floor_the_policy_forbids(self, monkeypatch):
        """The end of the same path: the user approves, and the swap goes out
        with amountOutMinimum 50% below the quote against a 3% policy cap."""
        svc, _, adapter = _service(monkeypatch, max_slippage_percent=3.0,
                                   eth_usd=None)
        result = svc.handle_trade_request("A1", {
            "token_in": WETH, "token_out": FOO, "amount_in": "0.001",
            "fee_tier": 500, "max_slippage_bps": 5000})
        if result["status"] != "pending":
            pytest.skip("did not escalate; the earlier test covers that case")

        approved = svc.approve_trade(result["request_id"])
        assert approved["status"] == "executed"
        amount_in, amount_out_min = adapter.swaps[-1]
        expected_out = amount_in * adapter.rate
        floor_pct = (1 - amount_out_min / expected_out) * 100

        assert floor_pct <= 3.0 + 1e-9, (
            f"swap sent with a {floor_pct:.1f}% slippage floor against a policy "
            f"cap of 3.0%")


class TestUnvaluableTradeSkipsEthFloor:
    """The same skip, on the other check that rejects rather than escalates."""

    def test_control_priced_feed_rejects_a_wallet_below_the_eth_floor(self, monkeypatch):
        svc, _, adapter = _service(monkeypatch, max_slippage_percent=50.0)
        svc._policy_store.get_policy("P1").trading_rules.min_reserve_eth = 5.0
        adapter.native_balance = lambda addr: 10 ** 18  # 1 ETH, under the 5 floor
        result = svc.handle_trade_request("A1", {
            "token_in": WETH, "token_out": FOO, "amount_in": "0.001",
            "fee_tier": 500, "max_slippage_bps": 100})
        assert result["status"] == "rejected"
        assert "below the policy minimum" in result["reason"]

    def test_feed_down_the_eth_floor_is_never_reached(self, monkeypatch):
        svc, _, adapter = _service(monkeypatch, max_slippage_percent=50.0,
                                   eth_usd=None)
        svc._policy_store.get_policy("P1").trading_rules.min_reserve_eth = 5.0
        adapter.native_balance = lambda addr: 10 ** 18  # 1 ETH, under the 5 floor
        result = svc.handle_trade_request("A1", {
            "token_in": WETH, "token_out": FOO, "amount_in": "0.001",
            "fee_tier": 500, "max_slippage_bps": 100})
        assert result["status"] == "rejected", (
            "trading is meant to halt below the ETH floor; with the feed down "
            f"the check is never reached and the trade got status "
            f"{result['status']}")


# ---------------------------------------------------------------------------
# 2. Selling a non-base token: the cap bounds what is received, not what is spent
# ---------------------------------------------------------------------------

class TestSizeCapOnASell:

    def test_a_huge_token_sale_into_a_cheap_pool_is_auto_approved(self, monkeypatch):
        """100,000 FOO sold for 1 USDG through a deep pool.

        rate: 1e18 atomic FOO -> 1e-2 atomic USDG, i.e. 100,000 FOO -> 1 USDG.
        The pool is linear, so the dust probe agrees with the real quote and the
        impact check reads the fee only. The notional is the USDG received: $1.
        """
        rate = 10 ** 6 / (100_000 * 10 ** 18)   # atomic USDG per atomic FOO
        svc, agent, adapter = _service(
            monkeypatch, per_trade=100.0, auto_below=50.0, max_impact=5.0,
            max_slippage_percent=50.0, rate=rate)

        result = svc.handle_trade_request("A1", {
            "token_in": FOO, "token_out": USDG, "amount_in": "100000",
            "fee_tier": 3000, "max_slippage_bps": 100})

        quote = result.get("quote") or {}
        assert result["status"] != "executed", (
            "100,000 FOO left the wallet for "
            f"{quote.get('amount_out_expected')} atomic USDG without a human "
            f"being asked; notional recorded as ${quote.get('notional_usdg')}, "
            f"impact {quote.get('price_impact_pct')}%, per-trade cap $100")
