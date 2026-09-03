"""
Which agent a trade belongs to, and which address it may spend from.

Both facts are settled by authenticating the caller, never read from the request
body - otherwise an agent could name a different address to trade from, and a
different agent to charge the volume to.

Also covers approving a trade by hand, which crashed on a misspelled lookup
after the swap had already gone through on-chain - so the trade happened, the
record was never finished, and nothing counted against the daily limit.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.models.trade import TradeRequest
from primer_vault.services.trading import TradingService

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20
OTHER_ADDR = "0x" + "b2" * 20

TRADE = {"token_in": USDG, "token_out": WETH, "amount_in": "10",
         "fee_tier": 500, "max_slippage_bps": 100}


class FakeAdapter:
    """Enough of a DEX to quote and execute without a chain."""

    def __init__(self):
        self.sent = []

    def token_metadata(self, token):
        return {"address": token, "decimals": 6 if token == USDG else 18,
                "symbol": "USDG" if token == USDG else "WETH"}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        return {"amount_out": amount * 2, "gas_estimate": 120000}

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
        # Mirror the real adapter's ordering: before_send marks the instant
        # after signing and before the network call, which is where the
        # trading lane commits daily volume.
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0x" + "ee" * 32

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


def _service(monkeypatch, *, auto=True, volume_used=0.0):
    """A trading service for one agent commissioned to AGENT_ADDR."""
    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=5000.0,
                         auto_approve_below_usd=1000.0 if auto else None,
                         min_reserve_eth=0.0, max_slippage_percent=5.0,
                         max_price_impact_percent=1000.0)  # not under test here
    policy = SpendPolicy.create(name="P", networks=[4663], daily_limit_micro=0,
                                trading_rules=rules)
    policy.id = "P1"

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status="active",
        wallet_address=AGENT_ADDR, policy_id="P1",
        trading_volume_today_usd=volume_used, last_trading_reset_date=None, last_trading_reset_at="",
        reset_daily_trading_volume=lambda: None)

    def add_volume(usd):
        agent.trading_volume_today_usd += usd
    agent.add_trading_volume = add_volume

    saved = {"agents": [], "txs": []}
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: saved["agents"].append(a),
        add_transaction=lambda tx: saved["txs"].append(tx),
        update_transaction=lambda tx: saved["txs"].append(tx))

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
    return svc, agent, adapter, saved


# ---------------------------------------------------------------------------
# The agent does not choose its address or its identity
# ---------------------------------------------------------------------------

class TestRequestCannotClaimIdentity:

    @pytest.mark.parametrize("field,value", [
        ("wallet_address", OTHER_ADDR),
        ("recipient", OTHER_ADDR),
        ("agent_id", "OTHER1"),
    ])
    def test_naming_an_authoritative_field_is_refused(self, monkeypatch, field, value):
        svc, _, _, _ = _service(monkeypatch)
        resp = svc.handle_trade_request("A1", {**TRADE, field: value})
        assert resp["code"] == "FIELD_NOT_PERMITTED"
        assert field in resp["error"]

    def test_refusal_happens_before_anything_executes(self, monkeypatch):
        svc, _, adapter, saved = _service(monkeypatch)
        svc.handle_trade_request("A1", {**TRADE, "recipient": OTHER_ADDR})
        assert adapter.sent == []
        assert saved["txs"] == []

    def test_the_body_cannot_supply_an_address_even_by_another_name(self):
        """from_dict is the only way a body becomes a request, so the address
        and the agent must not be reachable from it at all."""
        req = TradeRequest.from_dict({**TRADE, "wallet_address": OTHER_ADDR,
                                     "recipient": OTHER_ADDR, "agent_id": "OTHER1"})
        assert req.wallet_address is None
        assert req.agent_id == ""

    def test_the_commissioned_address_is_the_one_used(self, monkeypatch):
        svc, _, _, saved = _service(monkeypatch)
        resp = svc.handle_trade_request("A1", dict(TRADE))
        assert resp["status"] == "executed"
        assert saved["txs"][0].wallet_address == AGENT_ADDR

    def test_identity_comes_from_the_credentials(self, monkeypatch):
        """The volume must land on the agent that authenticated, which is the
        half of this that let a daily cap be sidestepped entirely."""
        svc, agent, _, _ = _service(monkeypatch)
        svc.handle_trade_request("A1", dict(TRADE))
        assert agent.trading_volume_today_usd > 0


class TestSigningRechecksTheBinding:
    """Approval can arrive much later and by another route, so the address is
    re-checked immediately before a key is used, not only at intake."""

    def test_mismatched_address_is_refused_at_execution(self, monkeypatch):
        svc, _, adapter, _ = _service(monkeypatch)
        request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)
        request.wallet_address = AGENT_ADDR
        quote = svc.prepare_trade(request)

        request.wallet_address = OTHER_ADDR  # as a later tamper would leave it
        result = svc.execute_trade(request, quote)

        assert result["code"] == "ADDRESS_NOT_COMMISSIONED"
        assert adapter.sent == []

    def test_unknown_agent_is_refused_at_execution(self, monkeypatch):
        svc, _, adapter, _ = _service(monkeypatch)
        request = TradeRequest.create("GHOST", USDG, WETH, "10", 500, 100)
        request.wallet_address = AGENT_ADDR
        quote = svc.prepare_trade(request)

        result = svc.execute_trade(request, quote)
        assert result["code"] == "ADDRESS_NOT_COMMISSIONED"
        assert adapter.sent == []


# ---------------------------------------------------------------------------
# Approving by hand
# ---------------------------------------------------------------------------

class TestManualApproval:

    def _pending(self, monkeypatch):
        svc, agent, adapter, saved = _service(monkeypatch, auto=False)
        resp = svc.handle_trade_request("A1", dict(TRADE))
        assert resp["status"] == "pending"
        return svc, agent, adapter, saved, resp["request_id"]

    def test_approving_executes_the_trade(self, monkeypatch):
        svc, _, adapter, _, rid = self._pending(monkeypatch)
        result = svc.approve_trade(rid)
        assert result["status"] == "executed"
        assert adapter.sent, "no transaction was submitted"

    def test_approving_counts_the_volume(self, monkeypatch):
        """The lookup that crashed sat between the swap and this, so the trade
        settled on-chain while the daily total stayed at zero."""
        svc, agent, _, _, rid = self._pending(monkeypatch)
        svc.approve_trade(rid)
        assert agent.trading_volume_today_usd == pytest.approx(10.0)

    def test_approving_records_the_hash(self, monkeypatch):
        svc, _, _, saved, rid = self._pending(monkeypatch)
        result = svc.approve_trade(rid)
        recorded = [t for t in saved["txs"] if getattr(t, "tx_hash", None)]
        assert recorded and recorded[-1].tx_hash == result["tx_hash"]

    def test_the_request_is_no_longer_pending(self, monkeypatch):
        svc, _, _, _, rid = self._pending(monkeypatch)
        svc.approve_trade(rid)
        assert svc.get_trade_status(rid)["status"] == "executed"

    def test_approving_twice_does_not_execute_twice(self, monkeypatch):
        """A double click must not place a second swap. The repeat returns what
        happened the first time rather than an error, so the caller can tell the
        difference between 'already done' and 'never existed'."""
        svc, agent, adapter, _, rid = self._pending(monkeypatch)
        first = svc.approve_trade(rid)
        sent_after_first = len(adapter.sent)
        volume_after_first = agent.trading_volume_today_usd

        second = svc.approve_trade(rid)
        assert second["tx_hash"] == first["tx_hash"]
        assert len(adapter.sent) == sent_after_first
        assert agent.trading_volume_today_usd == volume_after_first

    def test_rejecting_does_not_execute(self, monkeypatch):
        svc, agent, adapter, _, rid = self._pending(monkeypatch)
        result = svc.reject_trade(rid, "no thanks")
        assert result["status"] == "rejected"
        assert adapter.sent == []
        assert agent.trading_volume_today_usd == 0.0

    def test_a_status_poll_during_execution_never_reads_not_found(self, monkeypatch):
        """approve_trade pops the request from the pending queue before
        execute_trade runs, which can take real time on a live chain. A poll
        landing in that window must not read REQUEST_NOT_FOUND -
        indistinguishable from an id that never existed - for a trade a
        human just approved.
        """
        svc, _, _, _, rid = self._pending(monkeypatch)
        seen_mid_flight = {}
        real_execute = svc.execute_trade

        def spying_execute(*args, **kwargs):
            seen_mid_flight["status"] = svc.get_trade_status(rid)["status"]
            return real_execute(*args, **kwargs)

        monkeypatch.setattr(svc, "execute_trade", spying_execute)
        result = svc.approve_trade(rid)

        assert seen_mid_flight["status"] == "executing"
        assert svc.get_trade_status(rid)["status"] == result["status"]

    def test_a_bookkeeping_failure_cannot_lose_a_completed_swap(self, monkeypatch):
        """Everything after the swap runs when the money has already moved, so
        it must not be able to throw away the fact that it did."""
        svc, _, adapter, _, rid = self._pending(monkeypatch)
        monkeypatch.setattr(svc, "_record_outcome",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        result = svc.approve_trade(rid)
        assert result["status"] == "executed"
        assert result["tx_hash"]
        assert svc.get_trade_status(rid)["status"] == "executed"


class TestVolumeIsCommittedBeforeTheSend:
    """The daily volume must be on disk before the transaction is sent.

    Written just after the send returned, the gap is
    milliseconds, but a crash inside it reboots to
    an agent whose allowance was never debited while its trade settles
    on-chain - so the same allowance can be spent twice, and nothing
    reconciles against the chain afterwards. Committing first inverts which
    way a crash errs: an unsent trade can cost allowance until the daily
    reset, which is recoverable, rather than value moving twice, which is not.

    Signing stays on the safe side of the line: it touches no network, so a
    failure there must not burn allowance.
    """

    def test_the_spend_is_recorded_before_the_transaction_is_sent(self, monkeypatch):
        svc, agent, adapter, _ = _service(monkeypatch)

        volume_when_sent = []
        real_send = adapter.sign_and_send

        def watch(tx, pkey, before_send=None):
            def record_then_watch():
                before_send()
                volume_when_sent.append(agent.trading_volume_today_usd)
            return real_send(tx, pkey,
                             before_send=record_then_watch if before_send else None)

        monkeypatch.setattr(adapter, "sign_and_send", watch)

        svc.handle_trade_request("A1", dict(TRADE))

        assert volume_when_sent == [pytest.approx(10.0)], (
            "volume was not committed before the network call")

    def test_a_signing_failure_does_not_burn_the_allowance(self, monkeypatch):
        """Signing reaches no network, so nothing can be live - the agent must
        keep its allowance."""
        svc, agent, adapter, _ = _service(monkeypatch)

        def fail_before_sending(tx, pkey, before_send=None):
            raise RuntimeError("could not sign")

        monkeypatch.setattr(adapter, "sign_and_send", fail_before_sending)

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert result["status"] != "executed"
        assert agent.trading_volume_today_usd == 0.0
        assert adapter.sent == []

    def test_a_failed_send_keeps_the_allowance_spent(self, monkeypatch):
        """Past the network call there is no proof the node refused it: a
        timeout cannot tell 'never arrived' from 'arrived, answer lost'. The
        allowance stays spent, and the caller is told it may be live."""
        svc, agent, adapter, _ = _service(monkeypatch)

        def send_then_fail(tx, pkey, before_send=None):
            if before_send:
                before_send()
            raise RuntimeError("connection reset")

        monkeypatch.setattr(adapter, "sign_and_send", send_then_fail)

        result = svc.handle_trade_request("A1", dict(TRADE))

        assert result["status"] != "executed"
        assert agent.trading_volume_today_usd == pytest.approx(10.0), (
            "allowance was given back after the transaction may have gone out")
        assert "may still complete on-chain" in result.get("reason", "")

    def test_an_approval_step_does_not_commit_volume(self, monkeypatch):
        """Granting an ERC-20 allowance moves no value, so it must not count
        against the daily volume - only the wrap, unwrap and swap do."""
        svc, agent, adapter, _ = _service(monkeypatch)
        # No existing allowance, so the trade needs an approval step first.
        monkeypatch.setattr(adapter, "allowance", lambda *a, **k: 0)
        monkeypatch.setattr(adapter, "build_approve_tx",
                            lambda *a, **k: {"to": adapter.router_address()},
                            raising=False)

        volume_at_each_send = []
        real_send = adapter.sign_and_send

        def watch(tx, pkey, before_send=None):
            result = real_send(tx, pkey, before_send=before_send)
            volume_at_each_send.append(agent.trading_volume_today_usd)
            return result

        monkeypatch.setattr(adapter, "sign_and_send", watch)

        svc.handle_trade_request("A1", dict(TRADE))

        # Two sends: the approval, then the swap. Volume is still zero after
        # the approval and only counts once the swap goes out.
        assert volume_at_each_send == [0.0, pytest.approx(10.0)]


class TestTransactionRecord:

    def test_record_names_the_address_that_signed(self, monkeypatch):
        svc, _, _, saved = _service(monkeypatch)
        svc.handle_trade_request("A1", dict(TRADE))
        tx = saved["txs"][0]
        assert tx.wallet_address == AGENT_ADDR
        assert tx.wallet_id == "A001", "wallet id was always None before"


class TestSignerAvailability:
    """A trade nothing can sign is refused before a human is asked to approve it.

    A hardware address needs the desktop app to drive the device. Without it the
    trade would quote, pass policy, take a request id and return `pending` - then
    fail after someone had authorised it. Asking for an approval that could never
    have been honoured is worse than refusing in the first round trip.
    """

    def _service_with(self, is_hardware, tx_signer):
        from types import SimpleNamespace
        from primer_vault.services.trading import TradingService

        svc = TradingService()
        entry = SimpleNamespace(is_hardware=is_hardware, device_label="Ledger", id="L001")
        wallet = SimpleNamespace(get_address_by_address=lambda a: entry)
        svc.set_wallet_provider(lambda address: wallet)
        svc.set_hardware_tx_signer(tx_signer)
        return svc

    def _request(self):
        from primer_vault.models.trade import TradeRequest
        r = TradeRequest.create("A1", USDG, WETH, "10", 500, 100)
        r.wallet_address = "0x" + "11" * 20
        return r

    def test_hardware_address_without_a_signer_is_refused(self):
        svc = self._service_with(is_hardware=True, tx_signer=None)
        result = svc._signer_unavailable(self._request())
        assert result is not None
        assert result["code"] == "LEDGER_SIGN_NOT_AVAILABLE"
        assert "Ledger" in result["error"]

    def test_hardware_address_with_a_signer_proceeds(self):
        svc = self._service_with(is_hardware=True, tx_signer=lambda *a: "0xsigned")
        assert svc._signer_unavailable(self._request()) is None

    def test_software_address_is_unaffected(self):
        """Software keys sign in-process; the desktop app is irrelevant to them."""
        svc = self._service_with(is_hardware=False, tx_signer=None)
        assert svc._signer_unavailable(self._request()) is None

    def test_a_locked_wallet_is_refused_here_too(self):
        """Same shape of problem as a hardware signer with nothing to drive
        it: without this, the trade would quote, pass policy, take a request
        id, wait for a human to approve it, and only then fail - because
        nothing checked whether anything could sign it in the first place."""
        from primer_vault.services.trading import TradingService
        svc = TradingService()
        svc.set_wallet_provider(lambda address: None)
        svc.set_hardware_tx_signer(None)
        result = svc._signer_unavailable(self._request())
        assert result is not None
        assert result["code"] == "WALLET_LOCKED"

    def test_no_wallet_provider_at_all_is_left_to_the_usual_path(self):
        """No provider configured means Vault cannot even ask - a different,
        earlier problem than a wallet that exists but is locked."""
        from primer_vault.services.trading import TradingService
        svc = TradingService()
        assert svc._signer_unavailable(self._request()) is None
