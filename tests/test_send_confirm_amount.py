"""
The Send confirmation must show exactly the amount that will move.

The confirmation renders the same 8-decimal quantized value the transfer
computes: full precision (no rounding to significant digits) and never in
scientific notation.
"""

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


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _make_dialog(monkeypatch, qapp, symbol, decimals, balance_human):
    from primer_vault.ui import dialogs

    # The gas fetch is a live network call and irrelevant here.
    monkeypatch.setattr(dialogs.SendDialog, "_start_gas_fetch", lambda self: None)

    entry = AddressEntry(id="A001", name="Main", address=FROM)
    bal = Balance(symbol=symbol, name=symbol, raw=int(
        Decimal(balance_human) * (10 ** decimals)), decimals=decimals,
        formatted=float(balance_human), token_address=None if symbol == "ETH" else TO,
        is_native=(symbol == "ETH"))
    dlg = dialogs.SendDialog(
        parent=None, addresses=[entry], balances_by_address={FROM: [bal]},
        get_private_key_fn=lambda _id: None, chain_id=RHC,
        preselect_from=FROM, preselect_asset=symbol, rpc_url="http://127.0.0.1:1")
    return dialogs, dlg


def _confirm_text(monkeypatch, dialogs, dlg, amount) -> str:
    """Run the real _execute_send up to the confirmation, then cancel."""
    seen = {}

    def fake_question(parent, title, message, default_no=True):
        seen["message"] = message
        return False  # cancel - nothing is signed or sent

    monkeypatch.setattr(dialogs.FramelessMessageBox, "question",
                        staticmethod(fake_question))
    dlg.to_input.setText(TO)
    dlg.amount_input.setValue(amount)
    dlg._execute_send()
    assert "message" in seen, "the confirmation dialog was never shown"
    return seen["message"]


class TestSendConfirmationShowsTheAmountThatWillMove:

    def test_eight_decimal_eth_amount_is_shown_in_full(self, monkeypatch, qapp):
        """A precise ETH amount the spin box accepts and the transfer will send
        in full, rounded to 8 significant digits in the confirmation."""
        dialogs, dlg = _make_dialog(monkeypatch, qapp, "ETH", 18, "2000")
        amount = 1234.56789012
        message = _confirm_text(monkeypatch, dialogs, dlg, amount)
        shown = message.splitlines()[0]
        # What the transfer will actually move (dialogs.py).
        will_send = Decimal(str(dlg.amount_input.value())).quantize(
            Decimal("0.00000001"))
        assert str(will_send) in shown.replace(",", ""), (
            f"confirmation reads {shown!r} but the transfer will move "
            f"{will_send} ETH")

    def test_large_token_amount_is_not_shown_in_scientific_notation(
            self, monkeypatch, qapp):
        """A large balance renders as an exponent in the one dialog whose job
        is to let a person read the amount before authorising it."""
        dialogs, dlg = _make_dialog(monkeypatch, qapp, "MEME", 18, "900000000")
        message = _confirm_text(monkeypatch, dialogs, dlg, 500000000.0)
        shown = message.splitlines()[0]
        assert "e+" not in shown.lower(), (
            f"send confirmation shows the amount in scientific notation: {shown!r}")
