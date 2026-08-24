"""
Which pending payment does `approve <prefix>` actually sign?

`pending` prints the first eight characters of each request id and `approve`
accepts "a prefix". The selection is a first-match-wins scan over the pending
map, with no check that the prefix names exactly one request - so a prefix that
matches two waiting payments silently signs the older one, and an empty prefix
matches everything.

These assert the behaviour a person typing at the console is entitled to
assume.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands.approval import ApprovalCommands


class FakeRequest:
    def __init__(self, req_id, amount_micro, recipient):
        self.id = req_id
        self.agent_name = "bot"
        self.amount_micro = amount_micro
        self.network = "robinhood"
        self.recipient = recipient


class FakeCore:
    """Just enough core for ApprovalCommands: a pending list and a recorder."""

    def __init__(self, pending):
        self._pending = pending
        self.approved = []
        self.rejected = []

    def get_pending_requests(self):
        return list(self._pending)

    def approve_request(self, request_id):
        self.approved.append(request_id)
        return {"status": "success"}

    def reject_request(self, request_id, reason):
        self.rejected.append(request_id)
        return {"status": "rejected"}


SMALL = "3a1b2c3d-0000-4000-8000-000000000001"   # $0.01, queued first
LARGE = "3f9e8d7c-0000-4000-8000-000000000002"   # $500.00, queued second


@pytest.fixture
def two_pending():
    return FakeCore([
        FakeRequest(SMALL, 10_000, "0x" + "aa" * 20),
        FakeRequest(LARGE, 500_000_000, "0x" + "bb" * 20),
    ])


def test_an_ambiguous_prefix_is_refused(two_pending):
    """`approve 3` matches both waiting payments. Signing either one is a guess
    about which the person meant, so the command should refuse and say so."""
    result = ApprovalCommands(two_pending, handler=None).approve(["3"])

    assert two_pending.approved == [], (
        f"an ambiguous prefix signed {two_pending.approved}")
    assert result.success is False


def test_an_empty_prefix_is_refused(two_pending):
    """shlex keeps `approve ""` as a single empty argument, and "" is a prefix
    of every id."""
    result = ApprovalCommands(two_pending, handler=None).approve([""])

    assert two_pending.approved == [], (
        f"an empty prefix signed {two_pending.approved}")
    assert result.success is False


def test_a_full_id_still_approves(two_pending):
    """The refusal must not cost the ordinary case."""
    result = ApprovalCommands(two_pending, handler=None).approve([LARGE])
    assert result.success is True
    assert two_pending.approved == [LARGE]


def test_an_ambiguous_prefix_is_refused_by_reject_too(two_pending):
    """Same selection code, same hazard: rejecting the wrong request lets the
    other one keep sitting in the queue while the person believes it is gone."""
    ApprovalCommands(two_pending, handler=None).reject(["3"])
    assert two_pending.rejected == []
