"""Dust trades and the daily volume cap.

Every trade costs the wallet gas, whether it is worth $500 or one wei. The daily
cap is denominated in USD notional, and a one-wei wrap has a notional near zero,
so it barely moves the figure. The known limit is recorded in
docs/security.md ("Dust trades and gas").
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.services.trading import TradingService

WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
AGENT_ADDR = "0x" + "a1" * 20

ONE_WEI_WRAP = {"token_in": "ETH", "token_out": WETH,
                "amount_in": "0.000000000000000001",
                "fee_tier": 0, "max_slippage_bps": 0}


class FakeAdapter:
    """Counts every transaction that reaches the network."""

    def __init__(self):
        self.sent = []

    def token_metadata(self, token):
        if token.upper() == "ETH":
            return {"address": "0x" + "00" * 20, "decimals": 18, "symbol": "ETH"}
        return {"address": token, "decimals": 6 if token == USDG else 18,
                "symbol": "USDG" if token == USDG else "WETH"}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        return {"amount_out": int(amount * 2), "gas_estimate": 120000}

    def native_balance(self, addr):
        return 10 ** 18

    def allowance(self, token, owner, spender):
        return 10 ** 30

    def router_address(self):
        return "0x" + "dd" * 20

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    def build_wrap_tx(self, amount, sender, gas=None):
        return {"to": WETH, "value": amount}

    def build_unwrap_tx(self, amount, sender, gas=None):
        return {"to": WETH, "value": 0}

    def build_swap_tx(self, *a, **k):
        return {"to": self.router_address()}

    def simulate_swap(self, *a, **k):
        return None

    def sign_and_send(self, tx, pkey, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0x" + f"{len(self.sent) % 256:02x}" * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        return None


def _service(monkeypatch, *, daily_limit=50.0, auto_below=100.0):
    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=daily_limit,
                         auto_approve_below_usd=auto_below,
                         min_reserve_eth=0.0, max_slippage_percent=5.0,
                         max_price_impact_percent=50.0)
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

    adapter = FakeAdapter()
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd",
                        lambda *a, **k: 2000.0)
    return svc, agent, adapter


def test_a_one_wei_wrap_is_auto_executed(monkeypatch):
    """Groundwork: the dust wrap does auto-execute and does send a transaction."""
    svc, agent, adapter = _service(monkeypatch)
    result = svc.handle_trade_request("A1", dict(ONE_WEI_WRAP))
    assert result["status"] == "executed", result
    assert len(adapter.sent) == 1


@pytest.mark.xfail(strict=True, reason=(
    "Known limit, documented in docs/security.md (Dust trades and gas): the value "
    "caps do not bound the number of trades, so a near-zero-notional dust trade "
    "slips under them and costs only gas. Bounded by the minimum-ETH reserve and "
    "needs an auto-approve agent. A non-value control is planned."))
def test_the_daily_cap_bounds_how_many_transactions_an_agent_can_buy(monkeypatch):
    """A $50/day cap should not permit 500 auto-executed, gas-costing trades.

    Each wrap here is one wei of ETH: notional ~2e-15 dollars. If the cap is a
    real bound on what an agent can make the wallet spend, these cannot all go
    out on a $50 allowance.
    """
    svc, agent, adapter = _service(monkeypatch, daily_limit=50.0)
    for _ in range(500):
        svc.handle_trade_request("A1", dict(ONE_WEI_WRAP))
    assert len(adapter.sent) < 500, (
        f"{len(adapter.sent)} transactions were sent and the wallet paid gas for "
        f"every one; recorded daily volume is only "
        f"${agent.trading_volume_today_usd:.10f} of the $50.00 cap")
