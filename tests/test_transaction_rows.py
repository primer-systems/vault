"""
One History row per on-chain transaction.

An approval and the trade/lend it enables are two separate Transaction
records, each settled independently the moment its own receipt resolves -
not one record sharing a single tx_hash/status pair. These tests cover the
model's new `approve` type and display helpers, and the real incident this
was built to fix: an approval that settles while the leg after it fails must
not end up mis-attributed or unverifiable.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.transaction import Transaction, TYPE_APPROVE, STATUS_SETTLED
from primer_vault.models.trade import TradeRequest, TradeQuote
from primer_vault.models.defi import PositionRequest, PositionQuote
from primer_vault.services.trading import TradingService
from primer_vault.services.defi import DefiService
from primer_vault.services.signing import SigningService, VERIFY_PENDING
from primer_vault.networks import get_dex, TOKENS

USDG = TOKENS["USDG"].addresses[4663]
WETH = get_dex(4663).weth
VAULT = "0xBeEff033F34C046626B8D0A041844C5d1A5409dd"
ADDRESS = "0x65BC5555a25e24569E69BB0Af3f51c4EBdA42e6d"
WALLET = "0x00000000000000000000000000000000000000A1"


# ---------------------------------------------------------------------------
# 1. Model: create_approve, VALID_TYPES, display helpers
# ---------------------------------------------------------------------------

def test_approve_is_a_valid_type():
    assert TYPE_APPROVE in Transaction.VALID_TYPES


def test_create_approve_round_trips_through_dict():
    tx = Transaction.create_approve(
        agent_id="A1", agent_name="Bot", agent_code="code1",
        network="eip155:4663", asset=USDG, symbol="USDG",
        spender="0xSpender0000000000000000000000000000001",
        amount="25500000", wallet_address=WALLET, wallet_id="W001")

    assert tx.type == TYPE_APPROVE
    assert tx.approve_symbol == "USDG"
    assert tx.approve_spender == "0xSpender0000000000000000000000000000001"
    assert tx.amount_in == "25500000"
    # No value moves - never mistaken for a real transfer amount.
    assert tx.amount_micro == 0

    restored = Transaction.from_dict(tx.to_dict())
    assert restored.type == TYPE_APPROVE
    assert restored.approve_spender == tx.approve_spender
    assert restored.amount_in == tx.amount_in


def test_approve_row_moves_no_value_in_display():
    tx = Transaction.create_approve(
        agent_id="A1", agent_name="Bot", agent_code="code1",
        network="eip155:4663", asset=USDG, symbol="USDG",
        spender="0xSpender0000000000000000000000000000001",
        amount="25500000", wallet_address=WALLET)

    assert tx.display_amount() == "—"
    assert "Approve" in tx.display_activity()
    assert "USDG" in tx.display_activity()


def test_lend_supply_row_displays_correctly():
    """create_lend stores no explicit direction - the model infers it from
    which side of token_in/token_out is the venue. This was rendering wrong
    everywhere (CLI and both UI paths fell through to x402 formatting and
    showed $0.000000) before the display helpers existed."""
    tx = Transaction.create_lend(
        agent_id="A1", agent_name="Bot", agent_code="code1",
        network="eip155:4663", action="supply",
        venue="0xVenue0000000000000000000000000000000001",
        venue_name="Steakhouse USDG", asset=USDG, symbol="USDG",
        amount_in="25.5", wallet_address=WALLET)

    assert tx.lend_is_supply() is True
    assert tx.display_activity() == "Supply 25.5 USDG to Steakhouse USDG"
    assert tx.display_amount() == "25.5 USDG"


def test_lend_withdraw_row_displays_correctly():
    tx = Transaction.create_lend(
        agent_id="A1", agent_name="Bot", agent_code="code1",
        network="eip155:4663", action="withdraw",
        venue="0xVenue0000000000000000000000000000000001",
        venue_name="Steakhouse USDG", asset=USDG, symbol="USDG",
        amount_in="10.0", wallet_address=WALLET)

    assert tx.lend_is_supply() is False
    assert tx.display_activity() == "Withdraw 10.0 USDG from Steakhouse USDG"
    assert tx.display_amount() == "10.0 USDG"


def test_display_amount_covers_every_type():
    """Every type gets a real answer, not a KeyError or a raw string."""
    trade = Transaction.create_trade(
        agent_id="A1", agent_name="Bot", agent_code="c1", network="eip155:4663",
        token_in=USDG, token_out=WETH, symbol_in="USDG", symbol_out="WETH",
        amount_in="10", fee_tier=500, wallet_address=WALLET, wallet_id="W001")
    transfer = Transaction.create_transfer(
        agent_id="A1", agent_name="Bot", agent_code="c1", network="eip155:4663",
        recipient="0xRecipient00000000000000000000000000001",
        transfer_amount="1.5", wallet_address=WALLET, wallet_id="W001")
    payment = Transaction.create(
        agent_id="A1", agent_name="Bot", agent_code="c1", amount_micro=1_000_000,
        recipient="0xRecipient00000000000000000000000000001", network="eip155:4663")

    for tx in (trade, transfer, payment):
        assert tx.display_amount() != "—"
        assert tx.display_activity()


# ---------------------------------------------------------------------------
# 2. Trading lane: approval settles, the swap leg then fails - the approval's
#    own row must be settled with its real hash, independent of the swap.
# ---------------------------------------------------------------------------

class ApprovalSettlesThenSwapFailsAdapter:
    """The approval confirms cleanly; something raises while the swap is
    being built/simulated - mirrors the real incident (approval settles in
    under a second, the deposit/swap leg then errors)."""

    def __init__(self):
        self.sent = []

    def approval_steps(self, token, owner, amount, token_label=""):
        return [({"to": "0xRouter00000000000000000000000000000001",
                  "data": "0x095ea7b3"}, f"approve {token_label}")]

    def sign_and_send(self, tx, private_key, before_send=None):
        if before_send:
            before_send()
        self.sent.append(tx)
        return "0xapprovalhash"

    def wait_for_receipt(self, tx_hash, timeout=120.0):
        return SimpleNamespace(status=1)

    def simulate_swap(self, *args, **kwargs):
        raise RuntimeError("boom: node rejected the swap build")


def _service_with_added_transactions(monkeypatch, adapter):
    added = []
    agent = SimpleNamespace(id="A1", name="Bot", code="A1-code",
                            wallet_address=WALLET, policy_id="P1",
                            trading_volume_today_usd=0.0)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        add_transaction=lambda tx: added.append(tx),
        update_transaction=lambda tx: None)
    entry = SimpleNamespace(id="k1", is_hardware=False, device_path=None,
                            device_label="", address=WALLET)
    wallet = SimpleNamespace(
        get_address_by_address=lambda a: entry,
        get_private_key=lambda kid: b"\x11" * 32)
    svc = TradingService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    monkeypatch.setattr(svc, "_adapter", lambda chain_id, version="v3": adapter)
    return svc, added


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


def test_settled_approval_is_not_lost_when_the_swap_leg_then_fails():
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        adapter = ApprovalSettlesThenSwapFailsAdapter()
        svc, added = _service_with_added_transactions(monkeypatch, adapter)
        request = TradeRequest.create("A1", USDG, WETH, "10", 500, 100,
                                      wallet_address=WALLET)
        request.agent_id = "A1"

        result = svc.execute_trade(request, _swap_quote())

        assert result["status"] == "failed"
        # The approval settled - the failure was in building/simulating the
        # swap, which never got its own transaction sent.
        assert result["approval_tx_hash"] == "0xapprovalhash"

        approve_rows = [tx for tx in added if tx.type == "approve"]
        assert len(approve_rows) == 1
        approve_row = approve_rows[0]
        assert approve_row.status == STATUS_SETTLED
        assert approve_row.tx_hash == "0xapprovalhash"
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# 3. Verify: a settled approve row is verifiable even though the paired
#    action failed - this is the exact "Verifying..." hang from the real
#    incident. The bug was gating on the *operation's* status; each row now
#    carries its own, so an approve row that settled is never blocked by
#    whatever happened to the deposit/swap next to it.
# ---------------------------------------------------------------------------

def test_verify_proceeds_on_a_settled_approve_row():
    tx = Transaction.create_approve(
        agent_id="A1", agent_name="Bot", agent_code="c1",
        network="eip155:4663", asset=USDG, symbol="USDG",
        spender="0xSpender0000000000000000000000000000001",
        amount="10000000", wallet_address=WALLET)
    tx.mark_settled("0xapprovalhash")

    svc = SigningService()
    svc.verify_transaction(tx)

    # Reaching VERIFY_PENDING proves the status/tx_hash guard passed - before
    # this fix, an approval's hash never had its own settled row to check,
    # only a "failed" operation record the guard refused outright.
    assert tx.verification_status == VERIFY_PENDING


def test_verify_refuses_a_failed_row_as_before():
    """The guard itself is unchanged and correct - a genuinely failed
    operation still can't be verified. Only the approve row's independence
    from it is new."""
    tx = Transaction.create_lend(
        agent_id="A1", agent_name="Bot", agent_code="c1",
        network="eip155:4663", action="supply",
        venue="0xVenue0000000000000000000000000000000001",
        venue_name="Steakhouse USDG", asset=USDG, symbol="USDG",
        amount_in="1.0", wallet_address=WALLET)
    tx.mark_failed("deposit reverted")

    svc = SigningService()
    svc.verify_transaction(tx)

    assert tx.verification_status is None


# ---------------------------------------------------------------------------
# 4. A node's own rejection is not the same as "may still complete on-chain"
#    (the Morpho lane's version of the trading-lane case in
#    test_price_impact_and_stuck_approval.py)
# ---------------------------------------------------------------------------

def _defi_service_that_rejects_at_broadcast():
    """A DefiService where the deposit is signed fine but the node refuses to
    broadcast it - insufficient funds for gas, the real incident this covers.
    No approval step: the allowance is already in place, same as a retry
    after an earlier attempt already approved it."""
    from web3.exceptions import Web3RPCError

    agent = SimpleNamespace(
        id="A1", name="Bot", code="A1-code", wallet_address=ADDRESS,
        policy_id="P1", add_defi_op=lambda: None,
        remember_defi_venue=lambda v: None)
    store = SimpleNamespace(
        get_agent_by_id=lambda aid: agent if aid == "A1" else None,
        add_transaction=lambda tx: None,
        update_transaction=lambda tx: None,
        update_agent=lambda a: None)
    entry = SimpleNamespace(id="A001", address=ADDRESS, is_hardware=False)
    wallet = SimpleNamespace(
        get_address_by_address=lambda a: entry,
        get_private_key=lambda _id: bytes(32))

    def send_raw_transaction(raw):
        raise Web3RPCError(
            "insufficient funds for gas * price + value",
            rpc_response={"error": {"code": -32000,
                                    "message": "insufficient funds for gas * "
                                               "price + value"}})

    fake_adapter = SimpleNamespace(
        simulate=lambda tx: None,
        approval_steps=lambda *a, **kw: [],
        morpho_address="0x" + "9d" * 20,
        build_vault_deposit_tx=lambda venue, assets, sender, receiver: {"to": venue},
        w3=SimpleNamespace(eth=SimpleNamespace(
            account=SimpleNamespace(
                sign_transaction=lambda tx, pk: SimpleNamespace(raw_transaction=b"signed")),
            send_raw_transaction=send_raw_transaction)),
    )

    svc = DefiService()
    svc.set_stores(store)
    svc.set_wallet_provider(lambda addr: wallet)
    svc._adapter = lambda chain_id: fake_adapter
    return svc, agent


def test_a_node_rejection_is_reported_as_nothing_sent_for_lending():
    """The opposite failure mode from a stuck-approval or a receipt timeout:
    this one is not ambiguous. The node answered, and the answer was no."""
    svc, agent = _defi_service_that_rejects_at_broadcast()
    request = PositionRequest.create(
        "A1", "supply", VAULT, "vault", amount="1", chain_id=4663)
    request.wallet_address = ADDRESS
    quote = PositionQuote(
        venue=VAULT, venue_kind="vault", protocol="morpho", action="supply",
        asset=USDG, asset_decimals=6, share_decimals=18,
        assets=1_000_000, shares=994_938 * 10**12,
        asset_symbol="USDG", venue_name="Steakhouse USDG",
        notional_usd=1.0)

    result = svc.execute_position(request, quote)

    assert result["status"] == "failed"
    assert result["code"] == "RPC_REJECTED"
    assert "nothing was sent" in result["reason"].lower()
    assert "may still complete on-chain" not in result["reason"]
