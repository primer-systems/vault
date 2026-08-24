"""The Send dialog signs the quantity it displayed.

The confirmation shows the number the user typed; the ERC-20 transfer scales it
by the token's decimals. The quantity signed, measured in the token contract's
own decimals, must equal the quantity displayed â€” including when an indexer
reports decimals that disagree with the contract.
"""

import os
import sys
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from primer_vault.networks import Balance  # noqa: E402


# The token's real, on-chain decimals.
CONTRACT_DECIMALS = 6
# What the indexer reported for the same token (stale metadata / re-indexed proxy).
INDEXER_DECIMALS = 8

TOKEN_ADDRESS = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
FROM_ADDRESS = "0x1111111111111111111111111111111111111111"
TO_ADDRESS = "0x2222222222222222222222222222222222222222"

# The wallet really holds 1000 tokens: 1000 * 10**6 atomic units.
RAW_BALANCE = 1000 * 10 ** CONTRACT_DECIMALS


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


class _Entry:
    """Stand-in for AddressEntry."""
    id = "A001"
    name = "Main"
    address = FROM_ADDRESS
    is_hardware = False


class _Call:
    """A .call()-able that returns a fixed value, like a contract read."""

    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _Fn:
    """contract.functions.transfer(...) - records the atomic value it is given.

    Reports the token's real, on-chain decimals from decimals(), which is what
    the fixed Send path reads to scale the amount.
    """

    def __init__(self, sink):
        self._sink = sink

    def transfer(self, to, value):
        self._sink["to"] = to
        self._sink["value"] = value
        return self

    def decimals(self):
        return _Call(CONTRACT_DECIMALS)

    def build_transaction(self, overrides):
        tx = {"to": TOKEN_ADDRESS, "data": "0x", "value": 0}
        tx.update(overrides)
        return tx


class _Contract:
    def __init__(self, sink):
        self.functions = _Fn(sink)


class _Eth:
    def __init__(self, sink):
        self._sink = sink
        self.gas_price = 1_000_000_000

    def contract(self, address=None, abi=None):
        return _Contract(self._sink)

    def get_transaction_count(self, addr):
        return 0

    def estimate_gas(self, tx):
        return 65_000

    def send_raw_transaction(self, raw):
        class _H:
            def hex(self_inner):
                return "ab" * 32
        return _H()


class _W3:
    def __init__(self, sink):
        self.eth = _Eth(sink)

    @staticmethod
    def to_checksum_address(a):
        from web3 import Web3
        return Web3.to_checksum_address(a)


class _Signed:
    raw_transaction = b"\x00"


class _Account:
    def sign_transaction(self, tx):
        return _Signed()


class _Network:
    explorer_url = "https://example.invalid"
    native_symbol = "ETH"
    rpc_url = "https://rpc.invalid"


def test_send_dialog_signs_the_amount_it_displayed(qapp, monkeypatch):
    from primer_vault.ui import dialogs as dlg_mod

    # The dialog starts a background gas-price thread in __init__; in a unit test
    # it would connect to rpc.invalid and outlive the test, crashing the process
    # at teardown. Not part of what this test checks, so stub it out.
    monkeypatch.setattr(dlg_mod.SendDialog, "_start_gas_fetch", lambda self: None)

    balance = Balance(
        symbol="TKN",
        name="Test Token",
        raw=RAW_BALANCE,
        decimals=INDEXER_DECIMALS,            # what Blockscout said
        formatted=RAW_BALANCE / 10 ** INDEXER_DECIMALS,   # = 10.0
        token_address=TOKEN_ADDRESS,
        is_native=False,
    )

    dialog = dlg_mod.SendDialog(
        parent=None,
        addresses=[_Entry()],
        balances_by_address={FROM_ADDRESS: [balance]},
        get_private_key_fn=lambda _id: b"\x01" * 32,
        chain_id=4663,
        preselect_from=FROM_ADDRESS,
        preselect_asset="TKN",
        rpc_url="https://rpc.invalid",
    )

    # The largest amount the dialog will let the user ask for.
    displayed_amount = dialog.amount_input.maximum()
    assert displayed_amount == pytest.approx(10.0)

    # This is the string the "Confirm Send" box puts in front of the user
    # (ui/dialogs.py, _execute_send).
    shown = (f"{Decimal(str(displayed_amount)).quantize(Decimal('0.00000001')):,f}"
             .rstrip("0").rstrip("."))
    assert shown == "10"

    monkeypatch.setattr(dlg_mod.FramelessMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(dialog, "accept", lambda: None)

    sink = {}
    dialog._send_erc20(
        _W3(sink), _Account(), FROM_ADDRESS, TO_ADDRESS,
        displayed_amount, balance, _Network(),
    )

    # What actually leaves the wallet, in the token contract's own units.
    actually_sent = Decimal(sink["value"]) / (Decimal(10) ** CONTRACT_DECIMALS)

    assert actually_sent == Decimal(shown), (
        f"dialog showed {shown} TKN but the signed transfer moves "
        f"{actually_sent} TKN ({sink['value']} atomic units)"
    )
