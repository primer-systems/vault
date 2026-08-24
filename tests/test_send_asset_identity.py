"""
The Send dialog must move the asset the user selected.

An ERC-20's symbol() is free text its own contract returns, and Vault's balance
list is auto-discovered from Blockscout, so anyone can put a token called "ETH"
into a user's asset list by transferring one to them. `Balance.is_native`
(networks.py) records which entry is really the chain's native coin.

`SendDialog._execute_send` chooses between a native transfer and an ERC-20
transfer by comparing that untrusted symbol against the network's native symbol
(dialogs.py), and `_send_native` then scales the amount by the selected
Balance's `decimals` (dialogs.py) rather than the chain's.

These tests drive the real dialog with a counterfeit "ETH" token selected and
check what actually gets signed.
"""

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

pytest.importorskip("PyQt6")

from primer_vault.networks import Balance
from primer_vault.wallet.crypto import AddressEntry

RHC = 4663
FROM = "0x1111111111111111111111111111111111111111"
TO = "0x00000000000000000000000000000000000c0De0"
# A worthless token the attacker deployed, whose symbol() returns "ETH".
COUNTERFEIT = "0xDEAD00000000000000000000000000000000BEEF"
# Any key; the transaction is signed offline and never broadcast.
PRIVKEY = bytes.fromhex("11" * 32)


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _real_eth_balance():
    return Balance(
        symbol="ETH", name="ETH", raw=2 * 10**18, decimals=18,
        formatted=2.0, token_address=None, is_native=True)


def _counterfeit_eth_balance(decimals=21, human="5"):
    """An ERC-20 that calls itself ETH, with a balance the attacker chose."""
    return Balance(
        symbol="ETH", name="Ether", raw=int(Decimal(human) * (10 ** decimals)),
        decimals=decimals, formatted=float(human),
        token_address=COUNTERFEIT, is_native=False)


def _make_dialog(monkeypatch, balances):
    from primer_vault.ui import dialogs

    # The gas fetch is a live network call and irrelevant here.
    monkeypatch.setattr(dialogs.SendDialog, "_start_gas_fetch", lambda self: None)

    entry = AddressEntry(id="A001", name="Main", address=FROM)
    dlg = dialogs.SendDialog(
        parent=None, addresses=[entry], balances_by_address={FROM: balances},
        get_private_key_fn=lambda _id: PRIVKEY, chain_id=RHC,
        preselect_from=FROM, rpc_url="http://127.0.0.1:1")
    return dialogs, dlg


def _select_counterfeit(dlg):
    """Pick the asset entry backed by the counterfeit contract."""
    for i in range(dlg.asset_combo.count()):
        bal = dlg.asset_combo.itemData(i)
        if bal is not None and bal.token_address == COUNTERFEIT:
            dlg.asset_combo.setCurrentIndex(i)
            dlg._on_asset_changed()
            return
    raise AssertionError("counterfeit asset was not offered in the dialog")


def _confirm_yes(monkeypatch, dialogs, seen):
    def fake_question(parent, title, message, default_no=True):
        seen["message"] = message
        return True  # the user approves what this message described

    monkeypatch.setattr(dialogs.FramelessMessageBox, "question",
                        staticmethod(fake_question))
    monkeypatch.setattr(dialogs.FramelessMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(dialogs.FramelessMessageBox, "critical",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(dialogs.FramelessMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))


def _fake_node(monkeypatch) -> list:
    """Replace the node and the signer, keeping the real signing arithmetic.

    Returns the list that every transaction dict handed to sign_transaction is
    recorded into. Nothing reaches a network.
    """
    import eth_account
    import web3
    from eth_account import Account as RealAccount

    real_web3 = web3.Web3
    signed_txs: list = []

    class RecordingAccount:
        def __init__(self, inner):
            self._inner = inner

        @property
        def address(self):
            return self._inner.address

        def sign_transaction(self, tx):
            signed_txs.append(dict(tx))
            return self._inner.sign_transaction(tx)

    class FakeAccount:
        @staticmethod
        def from_key(key):
            return RecordingAccount(RealAccount.from_key(key))

    class FakeHash:
        def hex(self):
            return "ab" * 32

    class FakeEth:
        gas_price = 10 ** 9

        def get_transaction_count(self, _addr):
            return 0

        def estimate_gas(self, _tx):
            return 21000

        def send_raw_transaction(self, _raw):
            return FakeHash()

    class FakeWeb3:
        """Only the node is faked; checksumming stays real."""

        to_checksum_address = staticmethod(real_web3.to_checksum_address)

        @staticmethod
        def HTTPProvider(*a, **k):
            return None

        def __init__(self, _provider):
            self.eth = FakeEth()

        def is_connected(self):
            return True

    monkeypatch.setattr(web3, "Web3", FakeWeb3)
    monkeypatch.setattr(eth_account, "Account", FakeAccount)
    return signed_txs


class TestSendMovesTheSelectedAsset:

    def test_counterfeit_eth_token_is_sent_as_a_token_not_as_native_eth(
            self, monkeypatch, qapp):
        """Selecting an ERC-20 must produce an ERC-20 transfer of that contract,
        whatever the contract calls itself."""
        dialogs, dlg = _make_dialog(
            monkeypatch, [_real_eth_balance(), _counterfeit_eth_balance()])
        _select_counterfeit(dlg)
        _fake_node(monkeypatch)

        routed = {}
        monkeypatch.setattr(
            dialogs.SendDialog, "_send_native",
            lambda self, *a, **k: routed.setdefault("path", "native"))
        monkeypatch.setattr(
            dialogs.SendDialog, "_send_erc20",
            lambda self, *a, **k: routed.setdefault("path", "erc20"))

        seen = {}
        _confirm_yes(monkeypatch, dialogs, seen)
        dlg.to_input.setText(TO)
        dlg.amount_input.setValue(0.001)
        dlg._execute_send()

        assert routed.get("path") == "erc20", (
            "the user selected an ERC-20 held at "
            f"{COUNTERFEIT}, but Vault took the native-transfer path and will "
            "move the chain's own ETH instead")


class TestSendSignsTheAmountItShowed:

    def test_genuine_eth_send_signs_the_confirmed_amount(self, monkeypatch, qapp):
        """Positive control: with only the real native balance present, the
        signed value is exactly what the confirmation showed. This passes."""
        dialogs, dlg = _make_dialog(monkeypatch, [_real_eth_balance()])
        signed_txs = _fake_node(monkeypatch)

        seen = {}
        _confirm_yes(monkeypatch, dialogs, seen)
        dlg.to_input.setText(TO)
        dlg.amount_input.setValue(0.001)
        dlg._execute_send()

        assert signed_txs, "nothing was signed"
        assert "0.001" in seen["message"]
        assert signed_txs[0]["value"] == 10 ** 15

    def test_counterfeit_selection_never_moves_native_eth(self, monkeypatch, qapp):
        """Selecting the counterfeit must not sign a native-ETH transfer at all,
        and certainly not the 1 ETH its decimals(21) would scale 0.001 into. The
        sibling test proves the ERC-20 path is taken instead."""
        dialogs, dlg = _make_dialog(
            monkeypatch, [_real_eth_balance(), _counterfeit_eth_balance()])
        _select_counterfeit(dlg)
        signed_txs = _fake_node(monkeypatch)

        seen = {}
        _confirm_yes(monkeypatch, dialogs, seen)
        dlg.to_input.setText(TO)
        dlg.amount_input.setValue(0.001)
        dlg._execute_send()

        assert "0.001" in seen["message"], seen["message"]
        # The bug was a native send of value = 0.001 * 10**21 = 1 ETH. No signed
        # transaction may carry native value now: the ERC-20 path moves the
        # token via calldata with value 0.
        moved = [tx.get("value", 0) for tx in signed_txs if tx.get("value", 0)]
        assert not moved, (
            f"selecting the counterfeit ERC-20 signed a native transfer of "
            f"{moved} wei; it must move the token, not the chain's ETH")
