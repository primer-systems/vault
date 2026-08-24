"""
The on-chain slippage floor used when a queued trade
is approved.

approve_trade() re-quotes the pool, then executes with `amount_out_min` taken
from the quote made at *intake*, not from the fresh one
(services/trading.py). When the price moved in the user's favour during the
wait, that stale floor sits far below the current market, so the agent's declared
slippage tolerance is not enforced against the price the swap will actually
execute at.

These tests drive the service with a fake DEX whose rate can be changed between
the intake quote and the approval quote, and record the `amount_out_min` that
reaches the swap builder.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.services.trading import TradingService

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20

SLIPPAGE_BPS = 100  # 1.00%, what the agent asks for
TRADE = {"token_in": USDG, "token_out": WETH, "amount_in": "10",
         "fee_tier": 500, "max_slippage_bps": SLIPPAGE_BPS}


class RecordingAdapter:
    """A DEX whose rate can be moved, which records the min-out it is handed."""

    def __init__(self):
        self.rate = 2
        self.sent = []
        self.swap_min_out = None       # amount_out_min passed to build_swap_tx
        self.simulate_min_out = None   # amount_out_min passed to simulate_swap

    def token_metadata(self, token):
        return {"address": token, "decimals": 6 if token == USDG else 18,
                "symbol": "USDG" if token == USDG else "WETH"}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        return {"amount_out": int(amount * self.rate), "gas_estimate": 120000}

    def native_balance(self, addr):
        return 10 ** 18

    def allowance(self, token, owner, spender):
        return 10 ** 30

    def router_address(self):
        return "0x" + "dd" * 20

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    def simulate_swap(self, token_in, token_out, fee, recipient,
                      amount_in_atomic, amount_out_min, sender, **k):
        self.simulate_min_out = amount_out_min
        return None

    def build_swap_tx(self, token_in, token_out, fee, recipient,
                      amount_in_atomic, amount_out_min, sender, **k):
        self.swap_min_out = amount_out_min
        return {"to": self.router_address()}

    def sign_and_send(self, tx, pkey, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0x" + "ab" * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        return None


def _service(monkeypatch):
    """A service whose trades escalate for approval rather than auto-executing."""
    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=5000.0,
                         auto_approve_below_usd=None,   # everything escalates
                         min_reserve_eth=0.0, max_slippage_percent=5.0,
                         max_price_impact_percent=1000.0)
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

    adapter = RecordingAdapter()
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd",
                        lambda *a, **k: 2000.0)
    return svc, adapter


def _queue_then_approve(monkeypatch, rate_at_approval):
    """Queue a trade at rate 2, move the pool, approve. Returns (adapter, intake_min)."""
    svc, adapter = _service(monkeypatch)
    queued = svc.handle_trade_request("A1", dict(TRADE))
    assert queued["status"] == "pending", queued
    intake_min = queued["quote"]["amount_out_min"]

    adapter.rate = rate_at_approval
    result = svc.approve_trade(queued["request_id"])
    assert result["status"] == "executed", result
    return adapter, intake_min


class TestFloorAfterAFavourableMove:
    """The pool moved 50% in the user's favour while the trade sat waiting."""

    def test_the_executed_floor_is_the_higher_of_intake_and_fresh(self, monkeypatch):
        """After the fix: the floor only ever rises.

        10 USDG at 6 decimals is 10_000_000 atomic. At rate 2 the intake quote
        is 20_000_000 out, floored at 19_800_000 by the 1% tolerance. At
        approval the pool quotes 30_000_000, whose 1% floor is 29_700_000 -
        the higher of the two, so the one that must be sent.
        """
        adapter, intake_min = _queue_then_approve(monkeypatch, rate_at_approval=3)
        assert intake_min == 19_800_000
        assert adapter.swap_min_out == 29_700_000
        assert adapter.simulate_min_out == 29_700_000

    def test_the_floor_honours_the_tolerance_against_the_execution_price(self, monkeypatch):
        """The case that matters: a favourable move.

        At approval the pool quotes 30_000_000. The agent asked for 1%
        tolerance, so the swap should not be allowed to fill below 29_700_000.
        It is sent with a floor of 19_800_000 - 34% below the market it is about
        to trade into.
        """
        adapter, _ = _queue_then_approve(monkeypatch, rate_at_approval=3)
        fresh_expected = 30_000_000
        tolerated_floor = fresh_expected * (10_000 - SLIPPAGE_BPS) // 10_000
        assert adapter.swap_min_out >= tolerated_floor, (
            f"swap sent with amount_out_min={adapter.swap_min_out}, which allows a "
            f"fill {100 * (1 - adapter.swap_min_out / fresh_expected):.1f}% below the "
            f"price at execution; the agent authorised {SLIPPAGE_BPS / 100:.2f}%")

    def test_a_flat_market_is_unaffected(self, monkeypatch):
        """Control: with no price move the intake floor already is the right one,
        so this must pass both before and after any change."""
        adapter, intake_min = _queue_then_approve(monkeypatch, rate_at_approval=2)
        assert adapter.swap_min_out == intake_min == 19_800_000

    def test_an_adverse_move_inside_tolerance_keeps_the_intake_floor(self, monkeypatch):
        """Control: the protection the current code exists to give must survive.

        The pool fell to 1.99, inside the 1% the agent allowed. The floor must
        stay at the intake figure, not be relaxed to 1% below the new price.
        """
        adapter, intake_min = _queue_then_approve(monkeypatch, rate_at_approval=1.99)
        assert adapter.swap_min_out == intake_min == 19_800_000
