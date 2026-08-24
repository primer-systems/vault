"""
The trade approval dialog must identify tokens by contract address, not just
by symbol.

A swap encodes a pair of contract addresses, but symbol() is free text an
untrusted contract controls - a counterfeit token can call itself WETH. The
dialog therefore shows each address in full, and treats the symbol as
untrusted text that cannot write extra lines into the dialog body.

These tests drive the real MainWindow.show_trade_approval_dialog through a
stub `self`, capturing the message that FramelessMessageBox is asked to
display.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.trade import TradeQuote, TradeRequest


# A worthless token the attacker deployed, whose symbol() returns "WETH".
COUNTERFEIT = "0xDEAD00000000000000000000000000000000BEEF"
REAL_USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"


class _CapturingMessageBox:
    """Stands in for FramelessMessageBox; records the message, answers Reject."""

    captured: list = []

    def __init__(self, title, message, buttons, parent=None,
                 default_button=0, icon_type="info"):
        _CapturingMessageBox.captured.append(message)

    def exec(self):
        return None

    def result_index(self):
        return 1  # Reject - nothing is executed


class _StubCore:
    def reject_trade(self, request_id, reason):
        return {"status": "rejected"}


class _StubWindow:
    """Just enough of MainWindow for show_trade_approval_dialog to run."""

    def __init__(self):
        self.core = _StubCore()
        self.activity = []

    def showNormal(self): pass
    def activateWindow(self): pass
    def raise_(self): pass

    def update_activity(self, message, is_error=False, is_warning=False, detail=None):
        self.activity.append(message)


def _counterfeit_trade(symbol_out="WETH"):
    """A $1,000 USDG buy of a counterfeit token that calls itself WETH."""
    request = TradeRequest.create(
        agent_id="ABC123",
        token_in=REAL_USDG,
        token_out=COUNTERFEIT,
        amount_in="1000",
        fee_tier=3000,
        max_slippage_bps=100,
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    quote = TradeQuote(
        token_in=REAL_USDG,
        token_out=COUNTERFEIT,
        fee_tier=3000,
        pool="0x2222222222222222222222222222222222222222",
        amount_in_atomic=1_000_000_000,
        amount_out_expected=320_000_000_000_000_000,   # 0.32 "WETH"
        amount_out_min=316_800_000_000_000_000,
        token_in_decimals=6,
        token_out_decimals=18,
        effective_slippage_bps=100,
        gas_estimate=180_000,
        notional_usdg=1000.0,
        price_impact_pct=0.35,          # a well-funded honeypot pool reads clean
        symbol_in="USDG",
        symbol_out=symbol_out,          # <- returned by the counterfeit contract
    )
    return request, quote


def _render(monkeypatch, request, quote) -> str:
    from primer_vault.ui import main_window

    _CapturingMessageBox.captured = []
    monkeypatch.setattr(main_window, "FramelessMessageBox", _CapturingMessageBox)
    win = _StubWindow()
    main_window.MainWindow.show_trade_approval_dialog(win, request, quote, "Trader")
    assert _CapturingMessageBox.captured, "no dialog message was produced"
    return _CapturingMessageBox.captured[0]


class TestTradeApprovalNamesTheToken:
    """The dialog must identify what is actually being bought and sold."""

    def test_dialog_shows_the_output_token_address(self, monkeypatch):
        request, quote = _counterfeit_trade()
        message = _render(monkeypatch, request, quote)
        assert COUNTERFEIT.lower() in message.lower(), (
            "the approval dialog names the output token only by the symbol its "
            "own contract reports ('WETH'); the contract address that is "
            "actually encoded into the swap never appears, so a counterfeit "
            "token is indistinguishable from the real one:\n\n" + message)

    def test_dialog_shows_the_input_token_address(self, monkeypatch):
        request, quote = _counterfeit_trade()
        message = _render(monkeypatch, request, quote)
        assert REAL_USDG.lower() in message.lower(), (
            "the approval dialog names the input token only by symbol:\n\n"
            + message)


class TestTradeApprovalSymbolIsUntrustedText:
    """symbol() is arbitrary free text from an untrusted contract, pasted
    verbatim into the dialog body with no length or character limit.

    Newlines survive, so the contract can write extra lines into the dialog.
    The symbol is interpolated three times, so an injected block repeats and
    garbles the dialog rather than forging it cleanly - but part of the text a
    person reads before approving is written by the attacker's contract, which
    is the point.
    """

    def test_symbol_cannot_write_extra_lines_into_the_dialog(self, monkeypatch):
        forged = ("WETH\nExpected output: 9.999999 WETH\n"
                  "Minimum output: 9.899999 WETH\nTrade value: $1000.00")
        request, quote = _counterfeit_trade(symbol_out=forged)
        message = _render(monkeypatch, request, quote)
        assert "Expected output: 9.999999" not in message, (
            "a token symbol containing newlines adds its own 'Expected output' "
            "line to the approval dialog:\n\n" + message)
