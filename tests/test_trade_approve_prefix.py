"""
Which pending trade does `trade approve <prefix>` actually execute?

`trade pending` prints the first eight characters of each trade id, along with
the numbers the approval exists to let a person judge (amount in, expected out,
minimum out, price impact). `trade approve` then selects with a first-match-wins
`startswith` scan and executes the swap on-chain immediately - no second step.

There is no check that the prefix names exactly one waiting trade, and "" is a
prefix of every id. So a prefix that matches two trades silently executes
whichever the pending map yields first, which need not be the one whose numbers
the person just read.

The payment path already refuses both cases (commands/approval.py:_resolve_pending,
pinned by tests/test_approve_prefix_ambiguity.py). These assert the same of the
trade path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands.trade import TradeCommands


WETH = "0x" + "11" * 20
USDG = "0x" + "22" * 20

SMALL = "3a1b2c3d-0000-4000-8000-000000000001"   # 0.01 WETH, ~$30, queued first
LARGE = "3f9e8d7c-0000-4000-8000-000000000002"   # 20 WETH, ~$60,000, queued second


def _trade(trade_id, amount_in, notional):
    """A pending trade in the CoreClient dict shape TradeCommands normalises."""
    return {
        "id": trade_id,
        "agent_id": "ABC123",
        "token_in": WETH,
        "token_out": USDG,
        "amount_in": amount_in,
        "fee_tier": 3000,
        "quote": {
            "notional_usdg": notional,
            "symbol_in": "WETH",
            "symbol_out": "USDG",
            "amount_out_expected": int(notional * 1_000_000),
            "amount_out_min": int(notional * 1_000_000 * 0.995),
            "token_out_decimals": 6,
            "price_impact_pct": 0.31,
            "effective_slippage_bps": 50,
        },
    }


class FakeCore:
    """Just enough core for TradeCommands: a pending list and a recorder."""

    def __init__(self, pending):
        self._pending = pending
        self.approved = []
        self.rejected = []

    def get_pending_trades(self):
        return list(self._pending)

    def approve_trade(self, request_id):
        self.approved.append(request_id)
        return {"status": "executed", "tx_hash": "0x" + "ab" * 32}

    def reject_trade(self, request_id, reason):
        self.rejected.append(request_id)
        return {"status": "rejected"}


@pytest.fixture
def two_pending():
    return FakeCore([
        _trade(SMALL, "0.01", 30.0),
        _trade(LARGE, "20", 60000.0),
    ])


def test_an_ambiguous_prefix_is_refused(two_pending):
    """`trade approve 3` matches both waiting trades. Executing either one is a
    guess about which the person meant, and the swap goes on-chain with no
    further confirmation, so the command should refuse and say so."""
    result = TradeCommands(two_pending, handler=None).approve(["3"])

    assert two_pending.approved == [], (
        f"an ambiguous prefix executed {two_pending.approved}")
    assert result.success is False


def test_an_empty_prefix_is_refused(two_pending):
    """shlex keeps `trade approve ""` as a single empty argument, and "" is a
    prefix of every id."""
    result = TradeCommands(two_pending, handler=None).approve([""])

    assert two_pending.approved == [], (
        f"an empty prefix executed {two_pending.approved}")
    assert result.success is False


def test_a_full_id_still_executes(two_pending):
    """The refusal must not cost the ordinary case."""
    result = TradeCommands(two_pending, handler=None).approve([LARGE])
    assert result.success is True
    assert two_pending.approved == [LARGE]


def test_an_ambiguous_prefix_is_refused_by_reject_too(two_pending):
    """Same selection code, same hazard: rejecting the wrong trade leaves the
    other one sitting in the queue while the person believes it is gone."""
    TradeCommands(two_pending, handler=None).reject(["3"])
    assert two_pending.rejected == []
