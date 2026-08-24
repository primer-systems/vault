"""
Two things a trading limit has to survive: being raced, and being made to wait.

Volume recorded only after a trade succeeded, and checked without a lock, lets
simultaneous trades all measure themselves against the same starting total and
all pass; a queue of trades awaiting approval that commits nothing until
approved lets a stack of individually-allowed trades clear the daily
cap together.

Separately, approving a trade re-used the quote from when it arrived. The price
had moved, the dialog showed an old number, and the protection against a bad fill
was calculated against a price nobody was trading at any more.
"""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.models.trade import TradeRequest
from primer_vault.services import trading as trading_module
from primer_vault.services.trading import PENDING_TRADE_TTL_SECONDS, TradingService

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20

# 10 USDG in, so notional is $10 a trade.
TRADE = {"token_in": USDG, "token_out": WETH, "amount_in": "10",
         "fee_tier": 500, "max_slippage_bps": 100}


class FakeAdapter:
    """A DEX whose price can be moved between calls."""

    def __init__(self):
        self.sent = []
        self.rate = 2          # amount_out = amount_in * rate
        self.quote_calls = 0
        self.submit_delay = 0.0

    def token_metadata(self, token):
        return {"address": token, "decimals": 6 if token == USDG else 18,
                "symbol": "USDG" if token == USDG else "WETH"}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        self.quote_calls += 1
        return {"amount_out": int(amount * self.rate), "gas_estimate": 120000}

    def native_balance(self, addr):
        return 10 ** 18

    def allowance(self, token, owner, spender):
        return 10 ** 30

    def router_address(self):
        return "0x" + "dd" * 20

    def approval_steps(self, token, owner, amount, token_label=""):
        """Mirror DexAdapter.approval_steps: nothing to approve if covered."""
        if self.allowance(token, owner, self.router_address()) >= amount:
            return []
        return [(self.build_approve_tx(token, self.router_address(), amount, owner),
                 f"approve {token_label or 'the input token'} for the router")]

    def build_swap_tx(self, *a, **k):
        return {"to": self.router_address()}

    def build_swap_to_eth_tx(self, *a, **k):
        return {"to": self.router_address()}

    def simulate_swap(self, *a, **k):
        return None

    def sign_and_send(self, tx, pkey, before_send=None):
        # The delay stands in for signing, which happens before before_send
        # fires - mirroring the real adapter, where the volume commit sits
        # between signing and the network call.
        if self.submit_delay:
            time.sleep(self.submit_delay)
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0x" + f"{len(self.sent):02x}" * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        """The fill, which a real adapter reads out of the receipt's logs.

        These fakes hand back a bare status, so there is nothing to read and the
        honest answer is "unknown" - the same answer the real adapter gives for a
        receipt it cannot parse. That makes every trade here exercise the branch
        where the fill is unavailable, which is worth having: a trade that
        settled must still be recorded as settled when its fill cannot be read.
        Reading a real receipt is covered in test_trade_fill.py.
        """
        return None


def _service(monkeypatch, *, auto=True, daily_limit=5000.0, per_trade=1000.0,
             enabled=True):
    rules = TradingRules(enabled=enabled, per_trade_max_usd=per_trade,
                         daily_volume_limit_usd=daily_limit,
                         auto_approve_below_usd=per_trade if auto else None,
                         min_reserve_eth=0.0, max_slippage_percent=5.0,
                         max_price_impact_percent=1000.0)  # not under test here
    policy = SpendPolicy.create(name="P", networks=[4663], daily_limit_micro=0,
                                trading_rules=rules)
    policy.id = "P1"

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status="active",
        wallet_address=AGENT_ADDR, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None, last_trading_reset_at="",
        reset_daily_trading_volume=lambda: None)
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
    monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd", lambda *a, **k: 2000.0)
    return svc, agent, adapter, policy, rules


# ---------------------------------------------------------------------------
# The daily limit holds when raced
# ---------------------------------------------------------------------------

class TestConcurrentTrades:

    def test_simultaneous_trades_cannot_exceed_the_daily_limit(self, monkeypatch):
        """Eight trades of $10 fired at once against a $50 cap. Before, all
        eight measured themselves against a total of zero and all went through.
        """
        svc, agent, adapter, _, _ = _service(monkeypatch, daily_limit=50.0)
        adapter.submit_delay = 0.02  # widen the window a race would live in

        start = threading.Barrier(8)
        results = []
        rlock = threading.Lock()

        def fire():
            start.wait()
            r = svc.handle_trade_request("A1", dict(TRADE))
            with rlock:
                results.append(r)

        threads = [threading.Thread(target=fire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        executed = [r for r in results if r.get("status") == "executed"]
        rejected = [r for r in results if r.get("status") == "rejected"]
        assert len(results) == 8
        assert len(executed) == 5, f"expected 5 x $10 under a $50 cap, got {len(executed)}"
        assert len(rejected) == 3
        assert len(adapter.sent) == 5, "a rejected trade must not reach the chain"
        assert agent.trading_volume_today_usd == pytest.approx(50.0)

    def test_nothing_is_left_reserved_once_the_dust_settles(self, monkeypatch):
        svc, _, _, _, _ = _service(monkeypatch, daily_limit=50.0)
        for _ in range(8):
            svc.handle_trade_request("A1", dict(TRADE))
        assert svc._reservations == {}, "a reservation leaked; the agent would be stuck"


# ---------------------------------------------------------------------------
# Queued trades count against the limit while they wait
# ---------------------------------------------------------------------------

class TestQueuedTradesAreCommitted:

    def test_pending_trades_consume_the_daily_allowance(self, monkeypatch):
        """Five $10 trades queued against a $50 cap; the sixth must be refused
        even though none has been approved yet."""
        svc, _, _, _, _ = _service(monkeypatch, auto=False, daily_limit=50.0)
        for i in range(5):
            assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "pending", i
        sixth = svc.handle_trade_request("A1", dict(TRADE))
        assert sixth["status"] == "rejected"
        assert "daily limit" in sixth["reason"]

    def test_rejecting_a_queued_trade_gives_the_allowance_back(self, monkeypatch):
        svc, _, _, _, _ = _service(monkeypatch, auto=False, daily_limit=50.0)
        ids = [svc.handle_trade_request("A1", dict(TRADE))["request_id"] for _ in range(5)]
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "rejected"

        svc.reject_trade(ids[0], "changed my mind")
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "pending"

    def test_a_failed_trade_gives_the_allowance_back(self, monkeypatch):
        svc, agent, adapter, _, _ = _service(monkeypatch, daily_limit=20.0)

        working = adapter.sign_and_send

        def boom(tx, pkey):
            raise trading_module.DexError("chain said no")
        adapter.sign_and_send = boom

        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "failed"
        assert svc._reservations == {}, "a failed trade must not hold the allowance"
        assert agent.trading_volume_today_usd == 0.0

        # The allowance is free again, so the next attempt fits under the cap.
        adapter.sign_and_send = working
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "executed"

    def test_approving_a_queued_trade_records_it_once(self, monkeypatch):
        svc, agent, _, _, _ = _service(monkeypatch, auto=False, daily_limit=50.0)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        svc.approve_trade(rid)
        assert agent.trading_volume_today_usd == pytest.approx(10.0)
        assert svc._reservations == {}


# ---------------------------------------------------------------------------
# Waiting too long
# ---------------------------------------------------------------------------

class TestExpiry:

    def _expire(self, svc, rid):
        svc._pending[rid].deadline = time.monotonic() - 1

    def test_the_window_is_fifteen_minutes(self):
        assert PENDING_TRADE_TTL_SECONDS == 900

    def test_an_expired_trade_cannot_be_approved(self, monkeypatch):
        svc, _, adapter, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        self._expire(svc, rid)

        result = svc.approve_trade(rid)
        assert result["status"] == "rejected"
        assert "Expired" in result["reason"]
        assert adapter.sent == []

    def test_an_expired_trade_reports_as_rejected_to_the_agent(self, monkeypatch):
        svc, _, _, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        self._expire(svc, rid)

        status = svc.get_trade_status(rid)
        assert status["status"] == "rejected"
        assert "Expired" in status["reason"]

    def test_expiry_gives_the_allowance_back(self, monkeypatch):
        svc, _, _, _, _ = _service(monkeypatch, auto=False, daily_limit=10.0)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "rejected"

        self._expire(svc, rid)
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "pending"

    def test_an_expired_trade_leaves_the_pending_list(self, monkeypatch):
        svc, _, _, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        self._expire(svc, rid)
        assert svc.get_pending_trades() == []

    def test_a_trade_still_inside_the_window_survives(self, monkeypatch):
        svc, _, _, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        assert svc.get_trade_status(rid)["status"] == "pending"
        assert 0 < svc.get_trade_status(rid)["expires_in_seconds"] <= PENDING_TRADE_TTL_SECONDS
        assert svc.approve_trade(rid)["status"] == "executed"


# ---------------------------------------------------------------------------
# The price at approval, not the price at request
# ---------------------------------------------------------------------------

class TestRequoteOnApproval:

    def test_the_pool_is_quoted_again_at_approval(self, monkeypatch):
        svc, _, adapter, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        before = adapter.quote_calls
        svc.approve_trade(rid)
        assert adapter.quote_calls > before, "approval used the stale quote"

    def test_a_price_move_beyond_the_tolerance_is_refused(self, monkeypatch):
        """The agent asked for 1% tolerance. A 10% fall means the fill it
        committed to is no longer available, so the trade is not executed."""
        svc, agent, adapter, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]

        adapter.rate = 1.8  # 10% worse than the quote the user saw
        result = svc.approve_trade(rid)

        assert result["status"] == "rejected"
        assert result["code"] == "PRICE_MOVED"
        assert adapter.sent == []
        assert agent.trading_volume_today_usd == 0.0
        assert svc._reservations == {}

    def test_a_price_move_inside_the_tolerance_still_executes(self, monkeypatch):
        svc, _, adapter, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]

        adapter.rate = 1.995  # 0.25% worse, inside the 1% the agent allowed
        result = svc.approve_trade(rid)

        assert result["status"] == "executed"
        assert len(adapter.sent) == 1

    def test_a_favourable_move_executes(self, monkeypatch):
        svc, _, adapter, _, _ = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]

        adapter.rate = 2.5  # price improved
        assert svc.approve_trade(rid)["status"] == "executed"

    def test_policy_is_re_applied_at_approval(self, monkeypatch):
        """Switching trading off while a trade waits must stop it."""
        svc, _, adapter, _, rules = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]

        rules.enabled = False
        result = svc.approve_trade(rid)

        assert result["status"] == "rejected"
        assert result["code"] == "POLICY_REJECTED"
        assert adapter.sent == []

    def test_a_tightened_per_trade_cap_is_re_applied(self, monkeypatch):
        svc, _, adapter, _, rules = _service(monkeypatch, auto=False)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]

        rules.per_trade_max_usd = 1.0  # tightened below this trade's $10
        result = svc.approve_trade(rid)

        assert result["status"] == "rejected"
        assert adapter.sent == []

    def test_the_trade_does_not_count_against_its_own_reservation(self, monkeypatch):
        """Re-checking the daily limit at approval must not count the trade's own
        set-aside amount twice, or a trade that exactly fills the cap would
        always refuse itself."""
        svc, _, adapter, _, _ = _service(monkeypatch, auto=False, daily_limit=10.0)
        rid = svc.handle_trade_request("A1", dict(TRADE))["request_id"]
        assert svc.approve_trade(rid)["status"] == "executed"
        assert len(adapter.sent) == 1


# ---------------------------------------------------------------------------
# Price impact is measured on one scale
# ---------------------------------------------------------------------------

class TestPriceImpactScale:
    """A fee tier is millionths: 500 is 0.05%, not 5%.

    Two places convert it, and they must agree - a mismatch judged one trade by
    a yardstick a hundred times harsher than another.
    """

    @pytest.mark.parametrize("tier,percent", [(500, 0.05), (3000, 0.30), (10000, 1.00)])
    def test_a_fee_tier_converts_to_its_real_percentage(self, tier, percent):
        assert TradingService._fee_percent(tier) == pytest.approx(percent)

    def test_a_small_atomic_trade_is_still_sampled_not_short_circuited(self, monkeypatch):
        """A low atomic count is not proof of a small value: for a low-decimal
        token it can be the whole holding. The probe floors to at least one unit
        and measures, rather than returning the fee unmeasured - so a real quote
        is asked for even below the probe fraction."""
        svc, _, adapter, _, _ = _service(monkeypatch)
        request = TradeRequest.create("A1", USDG, WETH, "0.005", 500, 100)

        called = {}
        real_quote = adapter.quote_exact_input_single

        def recording_quote(*a, **k):
            called["probe_in"] = a[2]  # the amount passed to the probe
            return real_quote(*a, **k)

        monkeypatch.setattr(adapter, "quote_exact_input_single", recording_quote)
        svc._price_impact_pct(adapter, request, USDG, WETH,
                              amount_in_atomic=5_000, expected_out=9_999)
        assert called.get("probe_in", 0) >= 1, (
            "a sub-fraction trade was not sampled - it short-circuited to the "
            "fee without asking the pool anything")


# ---------------------------------------------------------------------------
# Everything waiting has claimed its volume
# ---------------------------------------------------------------------------

class TestEscalationsReserve:
    """A trade held for approval reserves like any other.

    Price impact returning before the reserve would let a trade escalated for
    it queue promising nothing: the daily total would not reflect what was
    waiting, and the figure published to agents would overstate what they
    could spend.
    """

    def test_a_trade_escalated_for_price_impact_reserves_its_volume(self, monkeypatch):
        svc, _, _, _, rules = _service(monkeypatch, daily_limit=50.0)
        rules.max_price_impact_percent = 0.0  # force the escalation

        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "pending"
        assert sum(u for _, u in svc._reservations.values()) == pytest.approx(10.0)

    def test_queued_impact_escalations_consume_the_allowance(self, monkeypatch):
        svc, _, _, _, rules = _service(monkeypatch, daily_limit=50.0)
        rules.max_price_impact_percent = 0.0

        statuses = [svc.handle_trade_request("A1", dict(TRADE))["status"] for _ in range(8)]
        assert statuses.count("pending") == 5
        assert statuses.count("rejected") == 3

    def test_an_unmeasurable_impact_also_escalates(self, monkeypatch):
        """None means unknown, not fine."""
        svc, _, adapter, _, _ = _service(monkeypatch)
        monkeypatch.setattr(svc, "_price_impact_pct", lambda *a, **k: None)
        result = svc.handle_trade_request("A1", dict(TRADE))
        assert result["status"] == "pending"
        assert adapter.sent == []


# ---------------------------------------------------------------------------
# Volume counts from broadcast, not from the receipt
# ---------------------------------------------------------------------------

class TestBroadcastCommitsVolume:
    """A trade that reached the node has spent its volume, receipt or no receipt.

    Committed only after the receipt was read, an RPC that dropped after
    send_raw_transaction - or a wait that timed out - would report the trade
    "failed" and give the allowance back while the swap was live in the
    mempool, so the volume that actually landed on-chain could exceed the
    daily cap by the size of every trade whose receipt was lost.

    The boundary is submit() returning. A submit that raises never reached the
    node, and releasing there stays correct - that side is pinned by
    test_a_failed_trade_gives_the_allowance_back above.
    """

    @staticmethod
    def _break_receipts(adapter):
        def receipt_unavailable(tx_hash, timeout=120.0):
            raise trading_module.DexError("rpc connection lost after broadcast")
        adapter.wait_for_receipt = receipt_unavailable

    def test_a_broadcast_trade_keeps_its_allowance_when_the_receipt_is_lost(self, monkeypatch):
        svc, agent, adapter, _, _ = _service(monkeypatch, daily_limit=20.0)
        self._break_receipts(adapter)

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert len(adapter.sent) == 1, "the swap was broadcast before the failure"
        assert result["status"] == "failed"
        held = (agent.trading_volume_today_usd
                + sum(u for _, u in svc._reservations.values()))
        assert held >= 10.0, "a swap that reached the node must keep its volume"

    def test_a_lost_receipt_cannot_stretch_the_daily_cap_on_chain(self, monkeypatch):
        """$20 cap: at most $20 of swaps may ever reach the chain."""
        svc, _, adapter, _, _ = _service(monkeypatch, daily_limit=20.0)
        working = adapter.wait_for_receipt
        self._break_receipts(adapter)
        svc.handle_trade_request("A1", dict(TRADE))  # $10 broadcast, receipt lost

        adapter.wait_for_receipt = working
        svc.handle_trade_request("A1", dict(TRADE))
        svc.handle_trade_request("A1", dict(TRADE))

        on_chain_usd = len(adapter.sent) * 10
        assert on_chain_usd <= 20, (
            f"${on_chain_usd} of swaps broadcast against a $20 daily cap")

    def test_the_failure_says_the_swap_may_still_complete(self, monkeypatch):
        """The agent is told which kind of failure it got: this one is not
        safe to blindly retry."""
        svc, _, adapter, _, _ = _service(monkeypatch, daily_limit=20.0)
        self._break_receipts(adapter)

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert "broadcast" in result["reason"]


# ---------------------------------------------------------------------------
# The queue has a ceiling
# ---------------------------------------------------------------------------

class TestPendingTradeCap:

    def test_an_agent_cannot_queue_without_limit(self, monkeypatch):
        from primer_vault.services.trading import MAX_PENDING_TRADES_PER_AGENT
        svc, _, _, _, _ = _service(monkeypatch, auto=False, daily_limit=10_000_000.0)

        for i in range(MAX_PENDING_TRADES_PER_AGENT):
            assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "pending", i

        refused = svc.handle_trade_request("A1", dict(TRADE))
        assert refused["status"] == "rejected"
        assert "already waiting" in refused["reason"]

    def test_a_refused_queue_entry_does_not_hold_volume(self, monkeypatch):
        from primer_vault.services.trading import MAX_PENDING_TRADES_PER_AGENT
        svc, _, _, _, _ = _service(monkeypatch, auto=False, daily_limit=10_000_000.0)
        for _ in range(MAX_PENDING_TRADES_PER_AGENT):
            svc.handle_trade_request("A1", dict(TRADE))
        svc.handle_trade_request("A1", dict(TRADE))

        assert len(svc._reservations) == MAX_PENDING_TRADES_PER_AGENT

    def test_room_frees_up_when_one_is_dealt_with(self, monkeypatch):
        from primer_vault.services.trading import MAX_PENDING_TRADES_PER_AGENT
        svc, _, _, _, _ = _service(monkeypatch, auto=False, daily_limit=10_000_000.0)
        ids = [svc.handle_trade_request("A1", dict(TRADE))["request_id"]
               for _ in range(MAX_PENDING_TRADES_PER_AGENT)]
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "rejected"

        svc.reject_trade(ids[0], "no")
        assert svc.handle_trade_request("A1", dict(TRADE))["status"] == "pending"
