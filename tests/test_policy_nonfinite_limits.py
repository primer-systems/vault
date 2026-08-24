"""Trading limits must be finite numbers."""

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.policy import SpendPolicy, TradingRules
from primer_vault.models.store import PolicyStore
from primer_vault.services.trading import TradingService

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
AGENT_ADDR = "0x" + "a1" * 20

NAN = float("nan")


@pytest.fixture
def temp_data_dir(tmp_path):
    """The `core` fixture in conftest.py takes one; each test file supplies it."""
    return tmp_path


class FakeAdapter:
    def __init__(self):
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


def _service(monkeypatch, rules):
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
        update_agent=lambda a: None, add_transaction=lambda tx: None,
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


# A million USDG in one swap. $1,000,000 of notional.
HUGE = {"token_in": USDG, "token_out": WETH, "amount_in": "1000000",
        "fee_tier": 500, "max_slippage_bps": 100}


class TestANaNTradingLimitStillLimits:

    def test_a_nan_per_trade_max_refuses_a_million_dollar_swap(self, monkeypatch):
        # The daily cap is finite and generous, so only the per-trade cap is
        # under test here.
        rules = TradingRules(enabled=True, per_trade_max_usd=NAN,
                             daily_volume_limit_usd=10.0 ** 12,
                             auto_approve_below_usd=10 ** 12,
                             min_reserve_eth=0.0, max_slippage_percent=5.0,
                             max_price_impact_percent=1000.0)
        svc, agent, adapter = _service(monkeypatch, rules)
        result = svc.handle_trade_request("A1", dict(HUGE))
        assert result["status"] == "rejected", (
            f"a $1,000,000 swap passed a per-trade cap of NaN: {result}")

    def test_a_nan_daily_volume_limit_refuses_a_million_dollar_swap(self, monkeypatch):
        rules = TradingRules(enabled=True, per_trade_max_usd=10 ** 12,
                             daily_volume_limit_usd=NAN,
                             auto_approve_below_usd=10 ** 12,
                             min_reserve_eth=0.0, max_slippage_percent=5.0,
                             max_price_impact_percent=1000.0)
        svc, agent, adapter = _service(monkeypatch, rules)
        result = svc.handle_trade_request("A1", dict(HUGE))
        assert result["status"] == "rejected", (
            f"a $1,000,000 swap passed a daily volume cap of NaN: {result}")
        assert adapter.sent == [], "the swap was broadcast"

    def test_nan_limits_do_not_switch_the_whole_trading_policy_off(self, monkeypatch):
        """All five size/quality caps at once - what a NaN'd policy really costs."""
        rules = TradingRules(enabled=True, per_trade_max_usd=NAN,
                             daily_volume_limit_usd=NAN,
                             auto_approve_below_usd=10 ** 12,
                             min_reserve_eth=NAN, max_slippage_percent=NAN,
                             max_price_impact_percent=NAN)
        svc, agent, adapter = _service(monkeypatch, rules)
        results = [svc.handle_trade_request("A1", dict(HUGE)) for _ in range(5)]
        executed = [r for r in results if r["status"] == "executed"]
        assert not executed, (
            f"{len(executed)} unattended $1,000,000 swaps cleared a policy whose "
            f"caps are all NaN; agent volume today = "
            f"${agent.trading_volume_today_usd}")


class TestTheStoreRefusesANonFiniteLimit:

    def test_a_policy_file_with_a_nan_limit_is_skipped(self, tmp_path):
        """Q4: a hand-edited or corrupted policy file must fail closed.

        json.load accepts bare NaN and Infinity as a Python extension, so this
        survives the read; nothing downstream rejects it.
        """
        (tmp_path / "policies.json").write_text(json.dumps([{
            "id": "P1", "name": "trader", "networks": [4663],
            "daily_limit_micro": 1_000_000,
            "per_request_max_micro": None, "auto_approve_below_micro": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "allowed_domains": [], "blocked_domains": [],
            "trading_rules": {"enabled": True, "per_trade_max_usd": NAN,
                              "daily_volume_limit_usd": NAN,
                              "auto_approve_below_usd": None,
                              "min_reserve_eth": 0.0,
                              "max_slippage_percent": 3.0,
                              "max_price_impact_percent": 5.0},
            "x402_enabled": True,
        }]), encoding="utf-8")

        store = PolicyStore(tmp_path)
        assert store.get_all_policies() == [], (
            "a policy whose trading caps are NaN loaded as a usable policy; "
            "an agent commissioned to it trades with no size limit at all")

    def test_from_dict_refuses_infinity(self):
        with pytest.raises(ValueError):
            TradingRules.from_dict({"enabled": True,
                                    "daily_volume_limit_usd": float("inf")})


class TestTheCliRefusesANonFiniteLimit:

    def test_policy_create_rejects_nan(self, core):
        from primer_vault.commands.policy import PolicyCommands

        cmd = PolicyCommands(core, handler=None)
        result = cmd.execute(["create", "trader", "--trading",
                              "--trade-daily", "nan", "--trade-max", "nan"])
        assert not result.success, (
            "'policy create --trade-daily nan' was accepted; the policy it "
            "wrote has no daily trading cap")

    def test_policy_edit_rejects_nan(self, core):
        from primer_vault.commands.policy import PolicyCommands

        cmd = PolicyCommands(core, handler=None)
        assert cmd.execute(["create", "trader", "--trading",
                            "--trade-daily", "500"]).success
        result = cmd.execute(["edit", "trader", "--trade-daily", "nan"])
        assert not result.success, (
            "'policy edit --trade-daily nan' was accepted; the daily trading "
            "cap is now NaN and stops refusing anything")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
