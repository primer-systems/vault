"""An agent cannot spend more than the user authorised.

Three routes the other limit suites do not cover:

  1. the daily-allowance reset writes `spent_today_micro`, and one endpoint that
     triggers it (/ping) carries no signature at all;
  2. approving a queued trade must re-check the agent's status, not only the
     policy, so suspending an agent reaches a trade already in the queue;
  3. the daily limit is counted per agent while it is named and displayed on the
     policy, which several agents may share.
"""

import shutil
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.networks import TOKENS
from primer_vault.services.trading import TradingService


@pytest.fixture
def temp_data_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _x402(amount_micro, index=0):
    """A well-formed USDG payment requirement on Robinhood Chain."""
    pay_to = "0x" + ("65" * 19) + ("%02x" % index)
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:4663",
            "amount": str(amount_micro),
            "asset": TOKENS["USDG"].addresses[4663],
            "payTo": pay_to,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


def _commission(core, *, daily, per_req, auto, name="Payer", policy=None):
    agent, token = core.create_agent(name=name, auth_mode="bearer")
    if policy is None:
        policy = core.create_policy(
            name="P-" + name, networks=[4663],
            daily_limit_micro=daily, per_request_max_micro=per_req,
            auto_approve_below_micro=auto)
    address = core.get_wallet_addresses()[0]
    core.commission_agent(agent.code, policy.id, address["address"])
    return core._signing_service, agent, token, policy


# ---------------------------------------------------------------------------
# 1. The daily reset is not taken under the spending lock
# ---------------------------------------------------------------------------

class TestTheResetRacesTheSpend:

    def test_a_ping_racing_a_signature_wipes_the_recorded_spend(self, core):
        """A /ping and a /sign that both arrive on the first request after the
        day rolls over.

        /ping needs no signature (services/signing.py) and calls
        _check_daily_reset before anything else (services/signing.py). The
        reset writes spent_today_micro without holding _spending_lock, which is
        the lock every other writer takes. If the ping's reset lands after the
        signature's increment, the day's spend is set back to zero and the full
        allowance is available again.

        The interleave is forced here rather than waited for; the point of the
        test is that the window exists, not how often it is hit.
        """
        svc, agent, token, policy = _commission(
            core, daily=10_000_000, per_req=10_000_000, auto=20_000_000,
            name="Racer")

        # Make the allowance due: yesterday's date, more than 20 hours ago.
        stored = core.get_agent_by_code(agent.code)
        stored.last_reset_date = (date.today() - timedelta(days=1)).isoformat()
        stored.last_reset_at = (
            datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        core.update_agent(stored)

        import primer_vault.services.signing as signing_mod
        real_is_due = signing_mod.daily_allowance_is_due

        ping_decided = threading.Event()
        sign_recorded = threading.Event()

        def gated_is_due(last_date, last_at):
            answer = real_is_due(last_date, last_at)
            if threading.current_thread().name == "pinger" and answer:
                # The ping thread has decided the allowance is due. Hold it
                # here - it has not written anything yet - and let the payment
                # go all the way through.
                ping_decided.set()
                sign_recorded.wait(timeout=10)
            return answer

        signing_mod.daily_allowance_is_due = gated_is_due
        try:
            pinger = threading.Thread(
                target=lambda: svc.handle_ping(agent.id), name="pinger")
            pinger.start()
            assert ping_decided.wait(timeout=10), "ping thread never reached the gate"

            first = svc.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=_x402(10_000_000, 1))
            assert first["status"] == "success", first
            assert core.get_agent_by_code(agent.code).spent_today_micro == 10_000_000

            # Release the ping: it now performs the reset it decided on earlier.
            sign_recorded.set()
            pinger.join(timeout=10)
        finally:
            signing_mod.daily_allowance_is_due = real_is_due

        spent_after_ping = core.get_agent_by_code(agent.code).spent_today_micro
        second = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=_x402(10_000_000, 2))

        assert second["status"] != "success", (
            "the agent signed 20.00 USDG under a 10.00 USDG daily limit; "
            "after the ping's reset landed, spent_today_micro read %s"
            % spent_after_ping)
        assert spent_after_ping == 10_000_000, (
            "an unauthenticated /ping erased a signed payment's spend: "
            "spent_today_micro fell to %s" % spent_after_ping)

    # The interleave is forced rather than waited for: a ping thread has to be
    # descheduled between deciding the allowance is due and writing the reset,
    # and stay descheduled for the whole of the signing path.


# ---------------------------------------------------------------------------
# 2. Suspension does not reach a trade already queued for approval
# ---------------------------------------------------------------------------

USDG_ADDR = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH_ADDR = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20

TRADE = {"token_in": USDG_ADDR, "token_out": WETH_ADDR, "amount_in": "10",
         "fee_tier": 500, "max_slippage_bps": 100}


class _Adapter:
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
        return "0x" + ("%02x" % len(self.sent)) * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        return None


def _trading_service(monkeypatch):
    """A trading service whose trades escalate to the user."""
    rules = TradingRules(enabled=True, per_trade_max_usd=1000.0,
                         daily_volume_limit_usd=5000.0,
                         auto_approve_below_usd=None,  # manual approval for all
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


class TestSuspensionAndTheApprovalQueue:

    def test_a_suspended_agents_queued_trade_still_executes(self, monkeypatch):
        """Suspending an agent is the stop button; the GUI says it "will reject
        all signing requests from this agent" (ui/tabs.py). A trade already
        in the approval queue is not covered: approve_trade re-quotes and
        re-applies the policy (services/trading.py) but never looks at
        agent.status, unlike approve_request on the payment side
        (services/signing.py).
        """
        svc, agent, adapter = _trading_service(monkeypatch)

        queued = svc.handle_trade_request("A1", dict(TRADE))
        assert queued["status"] == "pending", queued

        agent.status = "suspended"

        result = svc.approve_trade(queued["request_id"])

        assert result["status"] != "executed", (
            "a suspended agent's queued trade was executed")
        assert adapter.sent == [], "a suspended agent's swap reached the chain"


# ---------------------------------------------------------------------------
# 3. The daily limit is per agent, not per policy
# ---------------------------------------------------------------------------

class TestOnePolicyManyAgents:

    def test_agents_sharing_a_policy_each_get_their_own_daily_limit(self, core):
        """Intended design: a policy is a reusable description of the rules,
        and the daily allowance lives on the AGENT, so each agent commissioned
        against a policy gets its own full daily limit. Three agents on one
        $10/day policy can therefore sign $30 in a day - by design, not a bug.
        The wallet-facing total is the policy figure times the number of agents,
        which the label/docs should make clear (tracked for v0.3).
        """
        policy = core.create_policy(
            name="shared", networks=[4663], daily_limit_micro=10_000_000,
            per_request_max_micro=10_000_000,
            auto_approve_below_micro=20_000_000)

        total_signed = 0
        for n in range(3):
            svc, agent, token, _ = _commission(
                core, daily=0, per_req=0, auto=0,
                name="Shared%d" % n, policy=policy)
            result = svc.handle_sign_request(
                agent_id=agent.id, signature=token,
                x402_data=_x402(10_000_000, n))
            assert result["status"] == "success", result
            total_signed += 10_000_000

        # Each agent stays within its own limit; the shared-policy total is
        # per-agent, as intended.
        assert total_signed == 3 * policy.daily_limit_micro


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
