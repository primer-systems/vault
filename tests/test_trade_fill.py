"""What a trade actually delivered, as distinct from what it was quoted.

A quote is a prediction made before the swap runs; the fill is what the pool
actually paid out. They differ by whatever moved in between, up to the whole
slippage tolerance. Trade history must not record the quote in the field
labelled "received", or the ledger, the detail view and the CSV export all
present an estimate as a settled fact.

Both are recorded now. These tests cover the two halves of that: reading the fill
out of a real receipt, and the rule that the quote must never be substituted for
a fill that could not be read. Unknown is not the same as the prediction, and it
is not the same as zero.

Receipts here are hand-built from the real event signatures rather than captured
from a chain, so the topics and layouts are the ones a node emits.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from web3 import Web3

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import DEX, TOKENS
from primer_vault.services.dex import (
    TRANSFER_TOPIC, WITHDRAWAL_TOPIC, DexAdapterV3,
)

CHAIN_ID = 4663
USDG = Web3.to_checksum_address(TOKENS["USDG"].addresses[CHAIN_ID])
WETH = Web3.to_checksum_address(DEX[CHAIN_ID].weth)
ROUTER = Web3.to_checksum_address(DEX[CHAIN_ID].swap_router)
USER = Web3.to_checksum_address("0x742d35cc6634c0532925a3b844bc9e7595f0beb0")
POOL = Web3.to_checksum_address("0x" + "cc" * 20)
ETH = "0x0000000000000000000000000000000000000000"


@pytest.fixture
def adapter():
    """A real adapter. Nothing here touches the network - receipts are supplied."""
    return DexAdapterV3("http://127.0.0.1:1", DEX[CHAIN_ID])


def topic(address):
    return "0x" + Web3.to_checksum_address(address)[2:].lower().rjust(64, "0")


def word(value):
    return "0x" + format(value, "064x")


def transfer(token, sender, to, amount):
    """An ERC-20 Transfer log as a node emits it: two indexed parties, value in data."""
    return {"address": token,
            "topics": [TRANSFER_TOPIC, topic(sender), topic(to)],
            "data": word(amount)}


def withdrawal(weth, by, amount):
    """WETH9 Withdrawal: the unwrap that turns swapped WETH into native ETH."""
    return {"address": weth,
            "topics": [WITHDRAWAL_TOPIC, topic(by)],
            "data": word(amount)}


def receipt(*logs):
    return SimpleNamespace(status=1, logs=list(logs))


class TestErc20Output:
    """The common case: the swap ends in a token, so a Transfer records it."""

    def test_the_fill_is_read_from_the_transfer_to_the_user(self, adapter):
        r = receipt(transfer(USDG, USER, POOL, 10 * 10**6),      # the input leg
                    transfer(WETH, POOL, USER, 2_845_119_000_000_000))
        assert adapter.amount_received(r, WETH, USER) == 2_845_119_000_000_000

    def test_the_input_leg_is_not_mistaken_for_the_output(self, adapter):
        """Both legs are Transfers. Only one is addressed to the user in the
        output token, and reading the wrong one would report the amount spent as
        the amount received."""
        r = receipt(transfer(USDG, USER, POOL, 10 * 10**6),
                    transfer(WETH, POOL, USER, 2_845_119_000_000_000))
        assert adapter.amount_received(r, USDG, USER) is None

    def test_a_transfer_of_another_token_is_ignored(self, adapter):
        """A hook or a router rebate can move an unrelated token in the same tx."""
        other = Web3.to_checksum_address("0x" + "ab" * 20)
        r = receipt(transfer(other, POOL, USER, 999),
                    transfer(WETH, POOL, USER, 500))
        assert adapter.amount_received(r, WETH, USER) == 500

    def test_a_transfer_to_someone_else_is_ignored(self, adapter):
        """A protocol fee paid in the output token goes elsewhere in the same tx."""
        treasury = Web3.to_checksum_address("0x" + "fe" * 20)
        r = receipt(transfer(WETH, POOL, treasury, 7),
                    transfer(WETH, POOL, USER, 500))
        assert adapter.amount_received(r, WETH, USER) == 500

    def test_several_credits_to_the_user_are_summed(self, adapter):
        """A multi-hop route, or a token that splits its transfer, credits the
        recipient more than once. Taking the first would under-report the fill."""
        r = receipt(transfer(WETH, POOL, USER, 300),
                    transfer(WETH, POOL, USER, 200))
        assert adapter.amount_received(r, WETH, USER) == 500

    def test_a_receipt_with_no_matching_transfer_is_unknown(self, adapter):
        assert adapter.amount_received(receipt(), WETH, USER) is None

    def test_a_zero_transfer_reads_as_zero_not_unknown(self, adapter):
        """A pool that paid nothing is a fact, and a different one from a receipt
        that could not be read."""
        r = receipt(transfer(WETH, POOL, USER, 0))
        assert adapter.amount_received(r, WETH, USER) == 0

    def test_hexbytes_topics_are_handled(self, adapter):
        """web3 returns HexBytes, not strings. Both must read the same."""
        from hexbytes import HexBytes
        log = transfer(WETH, POOL, USER, 4321)
        log["topics"] = [HexBytes(t) for t in log["topics"]]
        log["data"] = HexBytes(log["data"])
        assert adapter.amount_received(receipt(log), WETH, USER) == 4321

    def test_a_lowercase_recipient_matches(self, adapter):
        """Addresses arrive checksummed, lower-case and mixed. All are the same
        address and all must match."""
        r = receipt(transfer(WETH, POOL, USER, 88))
        assert adapter.amount_received(r, WETH.lower(), USER.lower()) == 88


class TestNativeEthOutput:
    """ETH transfers emit no event, so the unwrap is the only record."""

    def test_the_fill_is_read_from_the_routers_unwrap(self, adapter):
        r = receipt(transfer(USDG, USER, POOL, 10 * 10**6),
                    transfer(WETH, POOL, ROUTER, 2_845_119_000_000_000),
                    withdrawal(WETH, ROUTER, 2_845_119_000_000_000))
        assert adapter.amount_received(r, ETH, USER) == 2_845_119_000_000_000

    def test_the_weth_sent_to_the_router_is_not_the_fill(self, adapter):
        """The intermediate WETH is addressed to the router, not the user, so
        asking for WETH on an ETH-output trade finds nothing for the user."""
        r = receipt(transfer(WETH, POOL, ROUTER, 500),
                    withdrawal(WETH, ROUTER, 500))
        assert adapter.amount_received(r, WETH, USER) is None

    def test_a_direct_unwrap_by_the_user_counts(self, adapter):
        """Plain WETH to ETH: the user calls withdraw themselves."""
        r = receipt(withdrawal(WETH, USER, 10**18))
        assert adapter.amount_received(r, ETH, USER) == 10**18

    def test_an_unwrap_by_a_stranger_does_not_count(self, adapter):
        stranger = Web3.to_checksum_address("0x" + "99" * 20)
        r = receipt(withdrawal(WETH, stranger, 10**18))
        assert adapter.amount_received(r, ETH, USER) is None

    def test_no_unwrap_at_all_is_unknown(self, adapter):
        """A v4 swap paying native ETH emits neither a Transfer nor a Withdrawal,
        so there is nothing to read and the honest answer is that we cannot
        tell."""
        assert adapter.amount_received(receipt(), ETH, USER) is None


class TestMalformedReceipts:
    """Bookkeeping must not turn a settled trade into a reported failure."""

    @pytest.mark.parametrize("bad", [
        SimpleNamespace(status=1),                               # no logs at all
        SimpleNamespace(status=1, logs=None),
        receipt({"address": WETH, "topics": [], "data": "0x"}),  # no topic0
        receipt({"address": WETH, "topics": [TRANSFER_TOPIC], "data": "0x"}),
        receipt({"topics": [TRANSFER_TOPIC, topic(POOL), topic(USER)],
                 "data": word(5)}),                              # no address
    ])
    def test_an_odd_receipt_reads_as_unknown_rather_than_raising(self, adapter, bad):
        assert adapter.amount_received(bad, WETH, USER) is None


class TestWhatGetsRecorded:
    """The measurement is only worth taking if the history keeps it."""

    def _record(self, filled, quoted):
        """Run a settled trade through the recorder and hand back its record."""
        from primer_vault.models.transaction import Transaction, STATUS_SETTLED
        from primer_vault.services.trading import TradingService

        svc = TradingService()
        tx = Transaction.create(
            agent_id="A1", agent_name="Bot", agent_code="BOT", amount_micro=0,
            recipient=USER, network="4663")
        tx.type = "trade"
        tx.symbol_out = "WETH"
        svc._policy_store = SimpleNamespace(
            update_transaction=lambda t: None,
            get_agent_by_id=lambda i: None)
        quote = SimpleNamespace(token_out_decimals=18, notional_usdg=None)
        request = SimpleNamespace(id="T1", agent_id="A1", wallet_address=None)
        svc._record_outcome(request, quote, tx, {
            "status": "executed", "tx_hash": "0x" + "ee" * 32,
            "amount_out": filled, "amount_out_quoted": quoted})
        assert tx.status == STATUS_SETTLED
        return tx

    def test_both_are_kept_and_the_fill_is_the_one_labelled_received(self):
        """The quote said 2.5 WETH, the pool paid 2.4. Both survive, and the
        field the history calls 'received' holds the one that arrived."""
        tx = self._record(filled=2_400_000_000_000_000_000,
                          quoted=2_500_000_000_000_000_000)
        assert tx.amount_out == "2.4"
        assert tx.amount_out_quoted == "2.5"

    def test_an_unreadable_fill_stays_empty_rather_than_borrowing_the_quote(self):
        """The defect this replaced, stated as a test: when the fill cannot be
        read, the record must say so. Writing the quote there is what made an
        estimate look like a settled fact."""
        tx = self._record(filled=None, quoted=2_500_000_000_000_000_000)
        assert tx.amount_out is None
        assert tx.amount_out_quoted == "2.5"

    def test_an_unreadable_fill_does_not_stop_the_trade_being_recorded(self):
        """Bookkeeping is not the trade. A swap that settled is settled whether
        or not its receipt could be parsed."""
        tx = self._record(filled=None, quoted=1)
        assert tx.tx_hash == "0x" + "ee" * 32

    def test_a_fill_of_zero_is_recorded_as_zero(self):
        """Distinct from unknown, and the more alarming of the two."""
        tx = self._record(filled=0, quoted=2_500_000_000_000_000_000)
        assert tx.amount_out == "0"
