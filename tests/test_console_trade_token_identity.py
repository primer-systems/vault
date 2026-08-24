"""
The console trade listing must identify the tokens the way the GUI does.

`trade pending` / `trade approve <id>` is a first-class approval path and
`approve` executes immediately with no second confirmation, so the listing is
the only thing a console operator judges the swap on.

The GUI dialog shows each token address in full and treats symbol() as
untrusted text, because a swap encodes contract addresses and a counterfeit
token can call itself anything (main_window.py, pinned by
tests/test_trade_token_identity.py). The console listing renders each address
as `addr[:8]…addr[-4:]` (commands/trade.py) and passes symbol() straight
through (commands/trade.py).

These tests drive the real TradeCommands.pending against a stub core, in the
same shape as tests/test_console_trade_approval.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands.trade import TradeCommands
from primer_vault.models.trade import TradeQuote, TradeRequest


USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
# WETH as it is deployed on an OP-stack chain - the address an operator would
# recognise at a glance.
REAL_WETH = "0x4200000000000000000000000000000000000006"
# A worthless contract the attacker ground out with CREATE2 so that the first
# six and last four hex digits match REAL_WETH. `_short` renders both as
# "0x420000…0006".
COUNTERFEIT_WETH = "0x420000DeaDbeeF11111111111111111111110006"


def _pending_pair(symbol_out="WETH", token_out=COUNTERFEIT_WETH):
    """A trade selling 50 USDG for a token that presents itself as WETH."""
    request = TradeRequest.create(
        agent_id="ABC123",
        token_in=USDG,
        token_out=token_out,
        amount_in="50",
        fee_tier=3000,
        max_slippage_bps=100,
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    quote = TradeQuote(
        token_in=USDG,
        token_out=token_out,
        fee_tier=3000,
        pool="0x2222222222222222222222222222222222222222",
        amount_in_atomic=50_000_000,                    # 50 USDG, 6dp
        amount_out_expected=20_000_000_000_000_000,     # 0.02
        amount_out_min=19_800_000_000_000_000,
        token_in_decimals=6,
        token_out_decimals=18,
        effective_slippage_bps=100,
        gas_estimate=210_000,
        notional_usdg=50.0,
        price_impact_pct=0.4,
        symbol_in="USDG",
        symbol_out=symbol_out,
    )
    return request, quote


class _StubCore:
    def __init__(self, pairs):
        self._pairs = pairs

    def get_pending_trades(self):
        return self._pairs


def _listing(pairs) -> str:
    return TradeCommands(_StubCore(pairs), None).pending([]).output


class TestConsoleListingNamesTheContractsBeingSwapped:

    def test_counterfeit_and_real_token_are_distinguishable(self):
        """Two different contracts must not render as the same text.

        Compares only the swap description, since the request id differs
        between two separately-created requests.
        """
        def swap_text(token_out):
            # Everything the listing says about the swap, minus the request id
            # (which differs between two separately-created requests). The
            # tokens are identified across the block, not only the `->` line.
            out = _listing([_pending_pair(token_out=token_out)])
            return out.split("ABC123", 1)[1]

        counterfeit_line = swap_text(COUNTERFEIT_WETH)
        real_line = swap_text(REAL_WETH)
        assert counterfeit_line != real_line, (
            "the console shows the same line for a swap into "
            f"{REAL_WETH} and a swap into {COUNTERFEIT_WETH}; "
            "`trade approve` signs whichever one it really is")

    def test_full_output_token_address_is_shown(self):
        """The address the swap encodes must appear in full, as it does in the
        GUI dialog, so an operator can compare it end to end."""
        out = _listing([_pending_pair()])
        assert COUNTERFEIT_WETH.lower() in out.lower(), (
            "the console trade listing never shows the output token's full "
            f"address; it renders {COUNTERFEIT_WETH} as an abbreviation")


class TestConsoleListingTreatsSymbolsAsUntrusted:

    def test_symbol_cannot_write_extra_lines_into_the_listing(self):
        """symbol() is free text from a contract the attacker deployed."""
        forged = "WETH\n            minimum out: 9.900000 WETH"
        out = _listing([_pending_pair(symbol_out=forged)])
        assert "minimum out: 9.900000" not in out, (
            "a counterfeit token's symbol() wrote its own 'minimum out' line "
            "into the approval listing")
