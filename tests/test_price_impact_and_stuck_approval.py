"""Price-impact measurement and failed-approval reporting.

Each test is the smallest arrangement that exercises the behaviour.

1. The price-impact probe is sized in *atomic* units (amount_in // 10_000), so
   for a token with very few decimals a large, real trade falls under the probe
   floor and the check reports the pool fee instead of the actual impact.

2. When an approval transaction is broadcast but its receipt cannot be read
   (node timeout, dropped connection), the user is told that no transaction was
   created nor sent and that it is safe to try again.
"""

import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.trade import TradeRequest, TradeQuote
from primer_vault.services.trading import TradingService
from primer_vault.networks import get_dex, TOKENS, is_native_eth

USDG = TOKENS["USDG"].addresses[4663]
WETH = get_dex(4663).weth

#: A token that reports 0 decimals. One atomic unit is one whole token.
COARSE = "0x" + "33" * 20

WALLET = "0x00000000000000000000000000000000000000A1"


# ---------------------------------------------------------------------------
# 1. Price-impact probe vanishes for a low-decimal token
# ---------------------------------------------------------------------------

class CoarseTokenPool:
    """Constant-product pool: COARSE (0 decimals) against USDG (6 decimals).

    Reserves are chosen so a single COARSE token is worth about $49 at the
    pool's own marginal rate, and the pool holds only 100 of them - so selling
    9_999 of them drains it and fills at about 1% of that rate.
    """

    FEE = 0.003          # the 3000 tier
    X = 100              # COARSE reserve, atomic (= whole tokens, 0 decimals)
    Y = 5_000 * 10 ** 6  # USDG reserve, atomic (6 decimals)

    DEC = {COARSE.lower(): 0, USDG.lower(): 6, WETH.lower(): 18}
    SYM = {COARSE.lower(): "CRS", USDG.lower(): "USDG", WETH.lower(): "WETH"}

    def token_metadata(self, token):
        if is_native_eth(token):
            return {"address": token, "symbol": "ETH", "name": "Ether", "decimals": 18}
        return {"address": token, "symbol": self.SYM[token.lower()],
                "name": self.SYM[token.lower()], "decimals": self.DEC[token.lower()]}

    def find_pool(self, a, b, fee, tick_spacing=None, hooks=None):
        return "0x0000000000000000000000000000000000000Pool"

    def quote_exact_input_single(self, token_in, token_out, amount_in, fee,
                                 tick_spacing=None, hooks=None):
        paid = amount_in * (1 - self.FEE)
        out = int(self.Y * paid / (self.X + paid))
        return {"amount_out": out, "sqrt_after": 0, "ticks_crossed": 1,
                "gas_estimate": 90_000}


def _coarse_service(monkeypatch, auto_approve_below=5_000.0):
    adapter = CoarseTokenPool()
    rules = SimpleNamespace(
        enabled=True,
        per_trade_max_usd=10_000.0,
        daily_volume_limit_usd=100_000.0,
        max_slippage_percent=5.0,
        max_price_impact_percent=5.0,
        min_reserve_eth=0.0,
        auto_approve_below_usd=auto_approve_below,
    )
    policy = SimpleNamespace(id="P1", trading_rules=rules)
    agent = SimpleNamespace(
        id="A1", name="Bot", wallet_address=WALLET, policy_id="P1",
        trading_volume_today_usd=0.0, last_trading_reset_date=None,
        last_trading_reset_at="", reset_daily_trading_volume=lambda: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        get_policy=lambda pid: policy if pid == "P1" else None,
        update_agent=lambda a: None)
    svc = TradingService()
    svc.set_stores(store)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc, adapter, agent


def test_low_decimal_trade_reports_the_real_price_impact(monkeypatch):
    """A 9_999-token sale that fills at ~1% of the pool rate must not read as 0.3%."""
    svc, adapter, _ = _coarse_service(monkeypatch)
    request = TradeRequest.create("A1", COARSE, USDG, "9999", 3000, 100,
                                  wallet_address=WALLET)
    expected_out = adapter.quote_exact_input_single(
        COARSE, USDG, 9_999, 3000)["amount_out"]

    impact = svc._price_impact_pct(adapter, request, COARSE, USDG, 9_999, expected_out)

    assert impact is not None
    assert impact > 50, (
        f"fill is ~1% of the pool's own rate but impact reads {impact}%")


def test_low_decimal_trade_is_not_auto_approved(monkeypatch):
    """The same trade must not be waved through under the auto-approve threshold."""
    svc, adapter, agent = _coarse_service(monkeypatch)
    request = TradeRequest.create("A1", COARSE, USDG, "9999", 3000, 100,
                                  wallet_address=WALLET)
    request.agent_id = "A1"

    quote = svc.prepare_trade(request)
    decision = svc._evaluate_policy(agent, request, quote)

    assert decision["action"] != "auto", (
        f"9_999 COARSE sold for ${quote.notional_usdg:.2f} was auto-approved; "
        f"impact reported as {quote.price_impact_pct}%")


# ---------------------------------------------------------------------------
# 2. A broadcast approval whose receipt cannot be read is reported as "nothing sent"
# ---------------------------------------------------------------------------

class StuckApprovalAdapter:
    """Signs and sends the approval, then never sees its receipt."""

    def __init__(self):
        self.sent = []

    def approval_steps(self, token, owner, amount, token_label=""):
        return [({"to": token, "data": "0x095ea7b3"}, f"approve {token_label}")]

    def sign_and_send(self, tx, private_key, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0xapprovalhash"

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        raise TimeoutError(f"Transaction {tx_hash} is not in the chain after {timeout} seconds")

    def simulate_swap(self, *args, **kwargs):
        raise AssertionError("must not be reached: the approval never confirmed")

    def build_swap_tx(self, *args, **kwargs):
        raise AssertionError("must not be reached: the approval never confirmed")


def _execution_service(monkeypatch, adapter):
    agent = SimpleNamespace(id="A1", name="Bot", code="A1-code", wallet_address=WALLET,
                            policy_id="P1", trading_volume_today_usd=0.0)
    agent.add_trading_volume = lambda usd: setattr(
        agent, "trading_volume_today_usd", agent.trading_volume_today_usd + usd)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None,
        update_agent=lambda a: None)
    entry = SimpleNamespace(id="k1", is_hardware=False, device_path=None,
                            device_label="", address=WALLET)
    wallet = SimpleNamespace(
        get_address_by_address=lambda a: entry,
        get_private_key=lambda kid: b"\x11" * 32)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc


def _swap_quote():
    return TradeQuote(
        token_in=USDG, token_out=WETH, fee_tier=500,
        pool="0x0000000000000000000000000000000000000Pool",
        amount_in_atomic=10_000_000, amount_out_expected=5 * 10 ** 15,
        amount_out_min=4_950_000_000_000_000,
        token_in_decimals=6, token_out_decimals=18,
        effective_slippage_bps=100, gas_estimate=90_000,
        notional_usdg=10.0, price_impact_pct=0.05,
        symbol_in="USDG", symbol_out="WETH")


def test_stuck_approval_is_not_reported_as_nothing_sent(monkeypatch):
    adapter = StuckApprovalAdapter()
    svc = _execution_service(monkeypatch, adapter)
    request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100,
                                  wallet_address=WALLET)
    request.agent_id = "A1"

    result = svc.execute_trade(request, _swap_quote())

    assert adapter.sent, "the approval was broadcast"
    assert result["status"] == "failed"
    reason = result["reason"]
    assert "nor sent" not in reason, (
        f"an approval was broadcast but the user is told: {reason!r}")


# ---------------------------------------------------------------------------
# 3. A node's own rejection is not the same as "may still complete on-chain"
# ---------------------------------------------------------------------------

class RpcRejectsBeforeBroadcastAdapter:
    """No approval needed; the node itself refuses the swap - insufficient
    funds for gas, the real failure this class of exception represents."""

    def approval_steps(self, token, owner, amount, token_label=""):
        return []

    def simulate_swap(self, *args, **kwargs):
        return None

    def build_swap_tx(self, *args, **kwargs):
        return {"to": "0xRouter00000000000000000000000000000001"}

    def sign_and_send(self, tx, private_key, before_send=None):
        from web3.exceptions import Web3RPCError
        if before_send:
            before_send()
        raise Web3RPCError(
            "insufficient funds for gas * price + value",
            rpc_response={"error": {"code": -32000,
                                    "message": "insufficient funds for gas * "
                                               "price + value"}})


def test_a_node_rejection_is_reported_as_nothing_sent(monkeypatch):
    """The opposite failure mode from the stuck-approval case above: this one
    is NOT ambiguous. The node answered, and the answer was no."""
    adapter = RpcRejectsBeforeBroadcastAdapter()
    svc = _execution_service(monkeypatch, adapter)
    request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100,
                                  wallet_address=WALLET)
    request.agent_id = "A1"

    result = svc.execute_trade(request, _swap_quote())

    assert result["status"] == "failed"
    assert result["code"] == "RPC_REJECTED"
    assert "nothing was sent" in result["reason"].lower()
    assert "may still complete on-chain" not in result["reason"]
