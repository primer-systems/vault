"""
The console trade listing must show the terms `trade approve` will sign.

`trade pending` / `trade approve <id>` is a first-class approval path, and
approving executes immediately with no second confirmation. What the swap
enforces on-chain is amount_out_min; the listing must therefore show the
minimum output, expected output, slippage tolerance and price impact - the
same numbers the GUI dialog renders before the identical call.

These tests drive the real TradeCommands.pending against a stub core.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands.trade import TradeCommands
from primer_vault.models.trade import TradeQuote, TradeRequest


USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
# A thin-pool token the agent picked; the quote for it is terrible.
THIN = "0xDEAD00000000000000000000000000000000BEEF"


def _pending_pair():
    """A trade that escalated because its price impact blew the policy limit.

    50 USDG in; the pool returns barely any of the other token, and the agent's
    own 50% slippage tolerance sets the floor that will be signed.
    """
    request = TradeRequest.create(
        agent_id="ABC123",
        token_in=USDG,
        token_out=THIN,
        amount_in="50",
        fee_tier=3000,
        max_slippage_bps=5000,          # 50%
        wallet_address="0x1111111111111111111111111111111111111111",
    )
    quote = TradeQuote(
        token_in=USDG,
        token_out=THIN,
        fee_tier=3000,
        pool="0x2222222222222222222222222222222222222222",
        amount_in_atomic=50_000_000,                     # 50 USDG, 6dp
        amount_out_expected=3_000_000_000_000_000_000,   # 3.0 THIN
        amount_out_min=1_500_000_000_000_000_000,        # 1.5 THIN - the signed floor
        token_in_decimals=6,
        token_out_decimals=18,
        effective_slippage_bps=5000,
        gas_estimate=210_000,
        notional_usdg=50.0,
        price_impact_pct=47.3,           # why it escalated
        symbol_in="USDG",
        symbol_out="THIN",
    )
    return request, quote


class _StubCore:
    def __init__(self, pairs):
        self._pairs = pairs

    def get_pending_trades(self):
        return self._pairs


def _listing():
    core = _StubCore([_pending_pair()])
    return TradeCommands(core, handler=None).pending([]).output


class TestConsoleTradeListingShowsWhatWillBeSigned:
    """Each number below is enforced by, or explains, the transaction that
    `trade approve` will sign. A person deciding from this listing alone must
    be able to read it."""

    def test_listing_shows_the_minimum_output(self):
        """amount_out_min is the one number the swap enforces on-chain."""
        shown = _listing()
        assert "1.5" in shown, (
            "console trade listing does not show the minimum output that will "
            f"be signed into the swap (amount_out_min = 1.5 THIN):\n{shown}")

    def test_listing_shows_the_expected_output(self):
        """3.0 THIN is what the pool is expected to return for the 50 USDG."""
        shown = _listing()
        assert "3.0" in shown, (
            "console trade listing does not show the expected output of the "
            f"swap (3.0 THIN):\n{shown}")

    def test_listing_shows_the_price_impact(self):
        """The reason this trade is waiting for a human at all."""
        shown = _listing()
        assert "47" in shown, (
            "console trade listing does not show the price impact (47.3%), "
            f"which is why this trade escalated:\n{shown}")

    def test_listing_shows_the_slippage_tolerance(self):
        shown = _listing()
        assert "50%" in shown or "5000" in shown, (
            "console trade listing does not show the slippage tolerance (50%) "
            f"that set the minimum output:\n{shown}")
