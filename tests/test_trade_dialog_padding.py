"""An agent-supplied trade amount cannot push the terms out of the dialog.

`amount_in` is a string checked only by `Decimal(str(...))`, and Decimal ignores
surrounding whitespace â€” so "1" followed by many newlines is a valid amount.
The token addresses, the expected and minimum output, the price impact and the
"could not be priced" warning all sit below that line in a dialog with no
scroll area, and must stay visible.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.trade import TradeRequest

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
WETH = "0x4200000000000000000000000000000000000006"


def _request(amount_in):
    return TradeRequest.create(
        agent_id="A1", token_in=USDG, token_out=WETH,
        amount_in=amount_in, fee_tier=3000, max_slippage_bps=50,
        wallet_address="0x00000000000000000000000000000000000c0De0")


def test_control_a_plain_amount_is_accepted():
    ok, reason = _request("1.5").validate_shape()
    assert ok, reason


def test_amount_in_cannot_carry_blank_lines_into_the_dialog():
    padded = "1.5" + "\n" * 40
    request = _request(padded)
    ok, reason = request.validate_shape()
    assert not ok, (
        "an amount padded with " + str(padded.count("\n")) + " newlines was "
        "accepted; it is printed verbatim in the trade approval dialog "
        "(ui/main_window.py), pushing the token addresses, the minimum "
        "output and the price impact below it"
    )
