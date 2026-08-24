"""
Attempts to make an agent spend more than the user authorised.

Routes exercised: racing the daily limit end to end through the signing
service, crashing between the check and the record, handing the app a policy
file it cannot read, and the price-feed outage during which trades cannot be
valued against the caps.

Note on the existing suite: tests/test_spending_limits.py has
`test_concurrent_requests_respect_daily_limit`, but it does its own
read-modify-write against the store rather than calling the signing service,
so the payment lane's own locking was not exercised there. These go through
the real entry points.
"""

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS


@pytest.fixture
def temp_data_dir():
    """A throwaway data directory, as the other suites define it."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _x402(amount_micro, index=0):
    """A well-formed USDG payment requirement on Robinhood Chain.

    A distinct payTo per request keeps each one out of the idempotency cache,
    so every call is processed fresh - which is what a race needs.
    """
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:4663",
            "amount": str(amount_micro),
            "asset": TOKENS["USDG"].addresses[4663],
            "payTo": f"0x{'65' * 19}{index:02x}",
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


def _commission(core, *, daily, per_req, auto, name="Payer"):
    agent, token = core.create_agent(name=name, auth_mode="bearer")
    policy = core.create_policy(
        name=f"P-{name}", networks=[4663],
        daily_limit_micro=daily, per_request_max_micro=per_req,
        auto_approve_below_micro=auto)
    address = core.get_wallet_addresses()[0]
    core.commission_agent(agent.code, policy.id, address["address"])
    return core._signing_service, agent, token, policy


# ---------------------------------------------------------------------------
# Q2: can two requests arriving together each pass a check they would fail if
#     serialised?
# ---------------------------------------------------------------------------

class TestConcurrentPaymentsCannotExceedTheDailyLimit:

    def test_twelve_simultaneous_payments_against_a_ten_dollar_cap(self, core):
        """Twelve $1 payments fired at once under a $10/day cap. At most ten
        may be signed, and the recorded spend may not exceed the cap."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=1_000_000, auto=10_000_000)

        start = threading.Barrier(12)
        results = []
        rlock = threading.Lock()

        def fire(i):
            start.wait()
            r = svc.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=_x402(1_000_000, i))
            with rlock:
                results.append(r)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        signed = [r for r in results if r.get("status") == "success"]
        assert len(results) == 12
        assert len(signed) <= 10, (
            f"{len(signed)} payments of $1.00 signed under a $10.00 daily cap")

        final = core.get_agent_by_code(agent.code)
        assert final.spent_today_micro <= policy.daily_limit_micro, (
            f"recorded spend {final.spent_today_micro} exceeds the cap "
            f"{policy.daily_limit_micro}")
        assert final.spent_today_micro == len(signed) * 1_000_000, (
            "signed payments and recorded spend disagree")

    def test_a_race_at_the_very_edge_of_the_cap(self, core):
        """Two $1 payments fired together with only $1 of allowance left.
        Exactly one may be signed."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=1_000_000, auto=10_000_000,
            name="Edge")

        live = core.get_agent_by_code(agent.code)
        live.spent_today_micro = 9_000_000
        core._policy_store.update_agent(live)

        start = threading.Barrier(2)
        results = []
        rlock = threading.Lock()

        def fire(i):
            start.wait()
            r = svc.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=_x402(1_000_000, i))
            with rlock:
                results.append(r)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        signed = [r for r in results if r.get("status") == "success"]
        assert len(signed) == 1, f"{len(signed)} signed with $1.00 of room"
        assert core.get_agent_by_code(agent.code).spent_today_micro == 10_000_000


# ---------------------------------------------------------------------------
# Q1: the spend is recorded relative to the header being handed out
# ---------------------------------------------------------------------------

class TestTheSpendIsOnDiskBeforeTheAgentHasTheHeader:

    def test_a_signed_payment_is_recorded_on_disk_when_sign_returns(self, core, tmp_path):
        """A crash the instant the agent receives its payment header must not
        reboot to an agent whose allowance was never debited. Read the store
        back off disk, not from memory."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Durable")

        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=_x402(2_000_000))
        assert result["status"] == "success", result
        assert result.get("header_value")

        on_disk = json.loads(
            (Path(core.data_dir) / "agents.json").read_text(encoding="utf-8"))
        record = next(a for a in on_disk if a["code"] == agent.code)
        assert record["spent_today_micro"] == 2_000_000, (
            "the header was handed out before the spend reached disk")

    def test_a_failure_to_record_the_spend_withholds_the_header(self, core, monkeypatch):
        """If the spend cannot be written down, no payment authorization may be
        handed out - otherwise a disk error becomes free money."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Unwritable")

        def refuse(_agent):
            raise OSError("disk full")
        monkeypatch.setattr(core._policy_store, "update_agent", refuse)

        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=_x402(2_000_000))

        assert result.get("status") != "success", result
        assert not result.get("header_value"), (
            "a payment authorization was returned although the spend was "
            "never recorded")


# ---------------------------------------------------------------------------
# Q4: a policy file that cannot be read
# ---------------------------------------------------------------------------

class TestAnUnreadablePolicyFileFailsClosed:

    def _restart(self, data_dir):
        from primer_vault.core import Vault
        return Vault(data_dir=data_dir)

    def test_a_truncated_policy_file_refuses_payments(self, core, temp_data_dir):
        """Half a policies.json, as a power loss might leave it."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Truncated")
        core.release_instance_lock()

        path = Path(temp_data_dir) / "policies.json"
        text = path.read_text(encoding="utf-8")
        path.write_text(text[:len(text) // 2], encoding="utf-8")

        restarted = self._restart(temp_data_dir)
        try:
            wallet_path = str(Path(temp_data_dir) / "wallets" / "test.wallet")
            restarted.load_wallet(wallet_path, "testpass")
            result = restarted._signing_service.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=_x402(1_000_000))
            assert result["status"] != "success", result
            assert result["code"] == "POLICY_NOT_FOUND"
        finally:
            restarted.release_instance_lock()

    def test_a_policy_with_a_corrupted_limit_refuses_payments(self, core, temp_data_dir):
        """A hand-edit that makes the daily limit unreadable must not be read
        as "no limit"."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Mangled")
        core.release_instance_lock()

        path = Path(temp_data_dir) / "policies.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data:
            entry["daily_limit_micro"] = "lots"
        path.write_text(json.dumps(data), encoding="utf-8")

        restarted = self._restart(temp_data_dir)
        try:
            wallet_path = str(Path(temp_data_dir) / "wallets" / "test.wallet")
            restarted.load_wallet(wallet_path, "testpass")
            result = restarted._signing_service.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=_x402(1_000_000))
            assert result["status"] != "success", result
            assert result["code"] == "POLICY_NOT_FOUND"
        finally:
            restarted.release_instance_lock()

    def test_a_policy_missing_its_daily_limit_refuses_payments(self, core, temp_data_dir):
        """The field deleted outright, rather than made unreadable."""
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Missing")
        core.release_instance_lock()

        path = Path(temp_data_dir) / "policies.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data:
            entry.pop("daily_limit_micro", None)
        path.write_text(json.dumps(data), encoding="utf-8")

        restarted = self._restart(temp_data_dir)
        try:
            wallet_path = str(Path(temp_data_dir) / "wallets" / "test.wallet")
            restarted.load_wallet(wallet_path, "testpass")
            result = restarted._signing_service.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=_x402(1_000_000))
            assert result["status"] != "success", result
            assert result["code"] == "POLICY_NOT_FOUND"
        finally:
            restarted.release_instance_lock()


# ---------------------------------------------------------------------------
# Q3: what survives a restart
# ---------------------------------------------------------------------------

class TestSpendSurvivesRestartAndPolicyEdits:

    def test_spend_is_not_forgotten_across_a_restart(self, core, temp_data_dir):
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Restart")
        assert svc.handle_sign_request(
            agent_id=agent.id, signature=token,
            x402_data=_x402(4_000_000))["status"] == "success"
        core.release_instance_lock()

        from primer_vault.core import Vault
        restarted = Vault(data_dir=temp_data_dir)
        try:
            assert restarted.get_agent_by_code(
                agent.code).spent_today_micro == 4_000_000
        finally:
            restarted.release_instance_lock()

    def test_editing_a_policy_does_not_forgive_todays_spend(self, core):
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=5_000_000, auto=10_000_000,
            name="Edited")
        assert svc.handle_sign_request(
            agent_id=agent.id, signature=token,
            x402_data=_x402(4_000_000))["status"] == "success"

        policy.daily_limit_micro = 20_000_000
        core.update_policy(policy)

        assert core.get_agent_by_code(agent.code).spent_today_micro == 4_000_000

    def test_reactivating_a_limit_reached_agent_does_not_refill_it(self, core):
        """`activate()` clears the status; it must not clear the spend."""
        svc, agent, token, policy = _commission(
            core, daily=4_000_000, per_req=5_000_000, auto=10_000_000,
            name="Reactivated")
        assert svc.handle_sign_request(
            agent_id=agent.id, signature=token,
            x402_data=_x402(4_000_000))["status"] == "success"
        assert core.get_agent_by_code(agent.code).status == "limit_reached"

        core.activate_agent(agent.code)

        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=_x402(1_000_000, 9))
        assert result["status"] != "success", result
        assert result["code"] == "EXCEEDS_DAILY_LIMIT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Q5: the trading limits are denominated in fiat. What happens when the
#     ETH/USD reference cannot be reached?
# ---------------------------------------------------------------------------

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
OTHER = "0x" + "b2" * 20
AGENT_ADDR = "0x" + "a1" * 20


def _unvaluable_service(monkeypatch, daily_limit=20.0):
    """A trading service whose ETH/USD reference is unreachable.

    The trade is WETH -> a token that is not a base asset, so its notional can
    only be found by pricing the WETH leg - which is exactly what the reference
    is for.
    """
    import threading as _th
    from types import SimpleNamespace
    from primer_vault.models.policy import SpendPolicy, TradingRules
    from primer_vault.services import pricing
    from primer_vault.services.trading import TradingService

    class Adapter:
        def __init__(self):
            self.sent = []

        def token_metadata(self, token):
            return {"address": token, "decimals": 18, "symbol": "TOK"}

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
            return "0x" + f"{len(self.sent):02x}" * 32

        def wait_for_receipt(self, tx_hash, timeout=120.0):
            return SimpleNamespace(status=1)

        def amount_received(self, receipt, token_out, recipient):
            return None

    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=daily_limit,
                         auto_approve_below_usd=None, min_reserve_eth=0.0,
                         max_slippage_percent=5.0, max_price_impact_percent=1000.0)
    policy = SpendPolicy.create(name="P", networks=[4663], daily_limit_micro=0,
                                trading_rules=rules)
    policy.id = "P1"

    agent = SimpleNamespace(
        id="A1", name="Bot", code="BOT", auth_mode="bearer", status="active",
        wallet_address=AGENT_ADDR, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None,
        last_trading_reset_at="", reset_daily_trading_volume=lambda: None)
    lock = _th.Lock()

    def add_volume(usd):
        with lock:
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
    monkeypatch.setattr(svc, "_base_addresses",
                        lambda chain_id, version="v3": (USDG, WETH))

    def unreachable(*a, **k):
        raise pricing.PricingError("the ETH/USD reference is unreachable")
    monkeypatch.setattr(pricing, "get_eth_usd", unreachable)

    return svc, agent, adapter


class TestTheDailyVolumeCapWhenThePriceFeedIsDown:

    TRADE = {"token_in": WETH, "token_out": OTHER, "amount_in": "1",
             "fee_tier": 500, "max_slippage_bps": 100}

    def test_an_unvaluable_trade_goes_to_the_user_rather_than_through(self, monkeypatch):
        """First: confirm it escalates rather than auto-executing."""
        svc, agent, adapter = _unvaluable_service(monkeypatch)
        result = svc.handle_trade_request("A1", dict(self.TRADE))
        assert result["status"] == "pending"
        assert adapter.sent == [], "an unvaluable trade reached the chain unattended"

    def test_an_approved_unvaluable_trade_consumes_no_daily_allowance(self):
        """Characterises the deliberate behaviour, so it is pinned somewhere.

        A trade Vault cannot price escalates before the daily-volume step, so
        nothing is reserved (services/trading.py), and the commit at
        broadcast is conditional on a notional (services/trading.py).
        Neither the per-trade nor the daily cap therefore applies to it.

        This is intended: the GUI approval dialog says so outright
        (ui/main_window.py). What it means in aggregate is that while
        the ETH/USD reference is unreachable, the configured daily volume cap
        provides no backstop at all - each trade rests solely on the user's
        approval of it.
        """

    def test_ten_approved_unvaluable_trades_record_nothing(self, monkeypatch):
        svc, agent, adapter = _unvaluable_service(monkeypatch, daily_limit=20.0)

        for _ in range(10):
            r = svc.handle_trade_request("A1", dict(self.TRADE))
            assert r["status"] == "pending"
            svc.approve_trade(r["request_id"])

        assert len(adapter.sent) == 10, "each approved trade was broadcast"
        assert agent.trading_volume_today_usd == 0.0, (
            "an unvaluable trade recorded volume")
        assert svc._reservations == {}, "a reservation leaked"


# ---------------------------------------------------------------------------
# Q5 / Q7: the console approval path and a trade Vault could not price
#
# When the ETH/USD reference is unreachable, a trade escalates with no notional,
# and - as the test above shows - an approved one records no volume, so neither
# the per-trade nor the daily cap applies to it. The GUI dialog says this in so
# many words (ui/main_window.py). The console listing does not.
# ---------------------------------------------------------------------------

class TestConsoleSaysWhenTheLimitsWereNotChecked:

    @staticmethod
    def _listing():
        from primer_vault.commands.trade import TradeCommands
        from primer_vault.models.trade import TradeQuote, TradeRequest

        request = TradeRequest.create(
            agent_id="ABC123", token_in=WETH, token_out=OTHER,
            amount_in="1", fee_tier=3000, max_slippage_bps=100,
            wallet_address="0x1111111111111111111111111111111111111111")
        quote = TradeQuote(
            token_in=WETH, token_out=OTHER, fee_tier=3000,
            pool="0x2222222222222222222222222222222222222222",
            amount_in_atomic=10 ** 18,
            amount_out_expected=2 * 10 ** 18,
            amount_out_min=int(1.98 * 10 ** 18),
            token_in_decimals=18, token_out_decimals=18,
            effective_slippage_bps=100, gas_estimate=210_000,
            notional_usdg=None,          # the price feed was unreachable
            price_impact_pct=0.3,
            symbol_in="WETH", symbol_out="TOK")

        class _StubCore:
            def get_pending_trades(self):
                return [(request, quote)]

        return TradeCommands(_StubCore(), handler=None).pending([]).output

    def test_the_listing_says_the_limits_could_not_be_applied(self):
        """`trade approve` executes with no further confirmation, so this
        listing is the whole of what a console user sees before the swap is
        signed. The GUI tells them the caps were not checked; this must too."""
        shown = self._listing()
        lowered = shown.lower()
        assert ("not be checked" in lowered
                or "not be priced" in lowered
                or "not be valued" in lowered
                or "limits" in lowered), (
            "the console listing for a trade Vault could not price does not "
            "say that the per-trade and daily limits were not applied to it; "
            f"it shows only '?' for the value:\n{shown}")
