"""
"Can one underpay and stick unconfirmed?
What does the user see if it does?"

A swap is signed, sent, and then Vault waits for the receipt. If the fee it was
built with is now below the base fee - or the RPC simply stops answering - the
wait times out. `execute_trade` already knows the transaction hash at that point
(`sh`), and the receipt-reverted branch already puts it in the TradeResult.

These tests ask whether that hash survives the two failure paths, because it is
the only thing that lets the user check whether the transaction they were told
"may still complete on-chain" actually did.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

from web3.exceptions import TimeExhausted

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.services.trading import TradingService

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20
SENT_HASH = "0x" + "7f" * 32

# 10 USDG in, so notional is $10 - well under every limit set below.
TRADE = {"token_in": USDG, "token_out": WETH, "amount_in": "10",
         "fee_tier": 500, "max_slippage_bps": 100}


class FakeAdapter:
    """A DEX that broadcasts, then fails the way the caller asks it to.

    `receipt` is either a receipt-shaped object to return, or an Exception
    instance to raise from wait_for_receipt.
    """

    def __init__(self, receipt):
        self.receipt = receipt
        self.sent = []

    def token_metadata(self, token):
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

    def build_swap_tx(self, *a, **k):
        return {"to": self.router_address()}

    def simulate_swap(self, *a, **k):
        return None

    def sign_and_send(self, tx, pkey, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return SENT_HASH

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt

    def amount_received(self, receipt, token_out, recipient):
        return None


def _service(monkeypatch, receipt):
    """A trading service wired to auto-approve a $10 trade. Returns
    (service, adapter, recorded) where `recorded` collects persisted rows."""
    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=5000.0,
                         auto_approve_below_usd=1000.0,
                         min_reserve_eth=0.0, max_slippage_percent=5.0,
                         max_price_impact_percent=1000.0)
    policy = SpendPolicy.create(name="P", networks=[4663], daily_limit_micro=0,
                                trading_rules=rules)
    policy.id = "P1"

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status="active",
        wallet_address=AGENT_ADDR, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None,
        last_trading_reset_at="", reset_daily_trading_volume=lambda: None,
        add_trading_volume=lambda usd: None)

    recorded = []
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: recorded.append(tx))

    entry = SimpleNamespace(id="A001", address=AGENT_ADDR, is_hardware=False)
    wallet = SimpleNamespace(
        data_key=bytes(32),
        get_address_by_address=lambda a: entry if a.lower() == AGENT_ADDR.lower() else None,
        get_private_key=lambda _id: bytes(32))

    adapter = FakeAdapter(receipt)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd", lambda *a, **k: 2000.0)
    return svc, adapter, recorded


# ---------------------------------------------------------------------------
# The transaction stuck unconfirmed
# ---------------------------------------------------------------------------

class TestReceiptTimedOut:
    """The swap is live in the mempool; Vault gave up waiting for it."""

    def test_the_hash_is_reported_to_the_caller(self, monkeypatch):
        """Vault tells the caller the trade may still complete on-chain. The
        hash is the only thing that lets them go and look."""
        svc, adapter, _ = _service(
            monkeypatch, TimeExhausted("not mined within 120 seconds"))

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert adapter.sent, "the swap was broadcast"
        assert result["status"] == "failed"
        assert "may still complete on-chain" in result["reason"]
        assert result["tx_hash"] == SENT_HASH, (
            "told to check a transaction, but not given its hash")

    def test_the_hash_is_kept_in_the_history(self, monkeypatch):
        svc, adapter, recorded = _service(
            monkeypatch, TimeExhausted("not mined within 120 seconds"))

        svc.handle_trade_request("A1", dict(TRADE))

        assert recorded, "the trade was written to history"
        assert recorded[-1].tx_hash == SENT_HASH, (
            "a broadcast transaction left no hash in the history")


# ---------------------------------------------------------------------------
# The transaction confirmed, and reverted
# ---------------------------------------------------------------------------

class TestSwapReverted:
    """Mined with status 0: the gas is spent and the hash is known for certain."""

    def test_the_hash_reaches_the_caller(self, monkeypatch):
        """This half already works - it is here to show the two paths apart."""
        svc, _, _ = _service(monkeypatch, SimpleNamespace(status=0))

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert result["status"] == "failed"
        assert result["tx_hash"] == SENT_HASH

    def test_the_hash_is_kept_in_the_history(self, monkeypatch):
        svc, _, recorded = _service(monkeypatch, SimpleNamespace(status=0))

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert recorded, "the trade was written to history"
        assert recorded[-1].tx_hash == result["tx_hash"], (
            "the result carries the hash but the history drops it")
