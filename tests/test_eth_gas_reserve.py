"""The minimum-ETH floor survives a trade that itself spends ETH.

The policy's "Min ETH Balance" exists so there is always ETH left to pay gas
with. For an ERC-20 to ERC-20 swap only gas leaves the wallet, but a native-ETH
input swap or a wrap sends ETH as the trade's own value â€” so the floor must be
compared against the balance the trade would leave behind, not the balance
before it.
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

DECIMALS = {USDG.lower(): 6, WETH.lower(): 18, "eth": 18}
SYMBOLS = {USDG.lower(): "USDG", WETH.lower(): "WETH", "eth": "ETH"}

WEI = 10 ** 18


def _key(token):
    return "eth" if token.upper() == "ETH" else token.lower()


class FakeAdapter:
    """A deep, linear pool, and a wallet whose ETH actually goes down.

    `native_balance` is served from `self.eth_wei`, and every transaction that
    carries a value debits it - which is the only thing these tests need the
    chain to do.
    """

    def __init__(self, eth_wei, rate=2):
        self.eth_wei = eth_wei
        self.rate = rate
        self.sent = []

    # -- reads ----------------------------------------------------------
    def token_metadata(self, token):
        k = _key(token)
        return {"address": token, "decimals": DECIMALS[k],
                "symbol": SYMBOLS[k], "name": SYMBOLS[k]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x" + "cc" * 20

    def quote_exact_input_single(self, a, b, amount, fee, tick_spacing=None, hooks=None):
        return {"amount_out": int(amount * self.rate), "gas_estimate": 120000}

    def native_balance(self, addr):
        return self.eth_wei

    def allowance(self, token, owner, spender):
        return 10 ** 40

    def router_address(self):
        return "0x" + "dd" * 20

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    # -- builds ---------------------------------------------------------
    def build_swap_tx(self, token_in, token_out, fee, recipient, amount_in_atomic,
                      amount_out_min, sender, gas=None, eth_value=0, **k):
        return {"to": self.router_address(), "value": int(eth_value)}

    def build_swap_to_eth_tx(self, *a, **k):
        return {"to": self.router_address(), "value": 0}

    def build_wrap_tx(self, amount, sender, gas=None):
        return {"to": WETH, "value": int(amount)}

    def build_unwrap_tx(self, amount, sender, gas=None):
        return {"to": WETH, "value": 0}

    def simulate_swap(self, *a, **k):
        return None

    # -- send -----------------------------------------------------------
    def sign_and_send(self, tx, pkey, before_send=None):
        if before_send:
            before_send()
        self.eth_wei -= int(tx.get("value", 0))   # the ETH the trade sends
        self.sent.append(tx)
        return "0x" + f"{len(self.sent):02x}" * 32

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def amount_received(self, receipt, token_out, recipient):
        return None


def _service(monkeypatch, *, eth_wei, min_reserve_eth, per_trade=10_000.0,
             auto_below=10_000.0, eth_usd=2000.0):
    rules = TradingRules(
        enabled=True, per_trade_max_usd=per_trade,
        daily_volume_limit_usd=1_000_000.0, auto_approve_below_usd=auto_below,
        min_reserve_eth=min_reserve_eth, max_slippage_percent=50.0,
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

    adapter = FakeAdapter(eth_wei=eth_wei)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    monkeypatch.setattr("primer_vault.services.pricing.get_eth_usd",
                        lambda *a, **k: eth_usd)
    return svc, agent, adapter


# ---------------------------------------------------------------------------
# Control: the floor does work, when the wallet is already under it.
# ---------------------------------------------------------------------------

def test_control_a_wallet_already_under_the_floor_cannot_trade(monkeypatch):
    svc, _, adapter = _service(monkeypatch,
                               eth_wei=int(0.005 * WEI),   # 0.005 ETH
                               min_reserve_eth=0.01)
    result = svc.handle_trade_request("A1", {
        "token_in": "ETH", "token_out": USDG, "amount_in": "0.001",
        "fee_tier": 500, "max_slippage_bps": 100})
    assert result["status"] == "rejected"
    assert "below the policy minimum" in result["reason"]
    assert adapter.sent == []


# ---------------------------------------------------------------------------
# The finding: one allowed trade spends the floor away.
# ---------------------------------------------------------------------------

def test_an_eth_swap_may_not_spend_the_wallet_below_the_gas_floor(monkeypatch):
    """0.02 ETH in the wallet, a 0.01 ETH floor, and a 0.019 ETH swap.

    The pre-trade balance clears the floor, so the trade is allowed. It then
    sends 0.019 ETH as msg.value and leaves 0.001 ETH - a tenth of the reserve
    the user asked to keep for gas.
    """
    svc, _, adapter = _service(monkeypatch,
                               eth_wei=int(0.02 * WEI),
                               min_reserve_eth=0.01)

    result = svc.handle_trade_request("A1", {
        "token_in": "ETH", "token_out": USDG, "amount_in": "0.019",
        "fee_tier": 500, "max_slippage_bps": 100})

    left = adapter.eth_wei / WEI
    assert result["status"] != "executed" or left >= 0.01, (
        f"trade executed and left {left:.6f} ETH in the wallet, below the "
        f"0.010000 ETH floor the policy set to keep gas money available "
        f"(status={result['status']})")


def test_a_wrap_may_not_spend_the_wallet_below_the_gas_floor(monkeypatch):
    """The same thing by the shortest route: wrapping ETH to WETH.

    A wrap has no pool, no slippage and no price impact, so nothing else in the
    policy has anything to say about it - the ETH floor is the only check
    standing between an agent and the wallet's whole ETH balance.
    """
    svc, _, adapter = _service(monkeypatch,
                               eth_wei=int(0.02 * WEI),
                               min_reserve_eth=0.01)

    result = svc.handle_trade_request("A1", {
        "token_in": "ETH", "token_out": WETH, "amount_in": "0.0199",
        "fee_tier": 0, "max_slippage_bps": 0})

    left = adapter.eth_wei / WEI
    assert result["status"] != "executed" or left >= 0.01, (
        f"wrap executed and left {left:.6f} ETH in the wallet, below the "
        f"0.010000 ETH floor the policy set to keep gas money available "
        f"(status={result['status']})")
