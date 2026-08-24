"""The trading-volume daily reset holds the lock that guards the counter.

Two /trade requests from the same agent arriving as the local date rolls over
must not interleave so that a reset lands after a commit and erases it â€” which
would leave the daily cap applied against a counter that has forgotten a swap
already on-chain.

The interleave is forced rather than waited for: what matters is that the
window is closed, not how often ordinary scheduling would land in it.
"""

import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.agent import Agent
from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.services.trading import TradingService

USDG_ADDR = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH_ADDR = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20

#: Two of these against a $500/day cap is $800 - over the cap by $300.
TRADE = {"token_in": USDG_ADDR, "token_out": WETH_ADDR, "amount_in": "400",
         "fee_tier": 500, "max_slippage_bps": 100}


class _Adapter:
    """Enough of a DEX adapter to quote and 'send' a swap. Records sends."""

    def __init__(self):
        self.sent = []

    def token_metadata(self, token):
        return {"address": token, "decimals": 6 if token == USDG_ADDR else 18,
                "symbol": "USDG" if token == USDG_ADDR else "WETH"}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        return {"amount_out": int(amount * 2), "gas_estimate": 120000}

    def native_balance(self, addr):
        return 10 ** 18

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    def build_swap_tx(self, *a, **k):
        return {"to": "0x" + "dd" * 20}

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


def _service(monkeypatch):
    """A trading service that auto-executes, with a $500/day volume cap and an
    agent whose trading allowance is due to renew."""
    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=500.0,
                         auto_approve_below_usd=10_000.0,  # auto-execute
                         min_reserve_eth=0.0, max_slippage_percent=5.0,
                         max_price_impact_percent=1000.0)
    policy = SpendPolicy.create(name="P", networks=[4663], daily_limit_micro=0,
                                trading_rules=rules)
    policy.id = "P1"

    agent = Agent.create(name="Bot", encrypted_auth_key="x", auth_mode="bearer",
                         agent_id="A1")
    agent.commission("P1", AGENT_ADDR)
    # Yesterday's date, more than 20 hours ago: the allowance is due.
    agent.last_trading_reset_date = (date.today() - timedelta(days=1)).isoformat()
    agent.last_trading_reset_at = (
        datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    agent.trading_volume_today_usd = 0.0

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

    adapter = _Adapter()
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd",
                        lambda *a, **k: 2000.0)
    return svc, agent, adapter


def test_a_lagging_reset_erases_a_committed_swaps_volume(monkeypatch):
    """Two trades arriving as the trading day rolls over.

    Thread B decides the allowance is due, then stalls before writing the
    reset. Trade A runs to completion: it resets, reserves $400 and commits it
    at broadcast. B then resumes and performs the reset it decided on long ago,
    wiping A's $400. B's own $400 now fits under the $500 cap, so both swaps
    reach the chain: $800 of volume under a $500 daily limit.
    """
    svc, agent, adapter = _service(monkeypatch)

    import primer_vault.services.trading as trading_mod
    real_is_due = trading_mod.daily_allowance_is_due

    b_decided = threading.Event()
    a_committed = threading.Event()

    def gated_is_due(last_date, last_at):
        answer = real_is_due(last_date, last_at)
        if threading.current_thread().name == "laggard" and answer:
            # B has decided the allowance is due and has written nothing yet.
            b_decided.set()
            a_committed.wait(timeout=10)
        return answer

    monkeypatch.setattr(trading_mod, "daily_allowance_is_due", gated_is_due)

    result_b = {}
    b = threading.Thread(
        target=lambda: result_b.update(svc.handle_trade_request("A1", dict(TRADE))),
        name="laggard")
    b.start()
    assert b_decided.wait(timeout=10), "the second trade never reached the gate"

    result_a = svc.handle_trade_request("A1", dict(TRADE))
    assert result_a["status"] == "executed", result_a
    volume_after_a = agent.trading_volume_today_usd
    assert volume_after_a == pytest.approx(400.0), volume_after_a

    a_committed.set()
    b.join(timeout=10)

    executed = [r for r in (result_a, result_b) if r.get("status") == "executed"]
    total_sent = 400.0 * len(adapter.sent)

    assert total_sent <= 500.0, (
        "%d swaps worth $%.2f reached the chain under a $500.00 daily volume "
        "cap; the agent's recorded volume afterwards is $%.2f"
        % (len(adapter.sent), total_sent, agent.trading_volume_today_usd))
    assert len(executed) == 1
