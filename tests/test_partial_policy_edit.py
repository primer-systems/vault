"""
Can a rejected `policy edit` still change a limit?

`policy edit` applies each option to the live policy object as it parses the
command line. The object it mutates is the one held in PolicyStore, which is
the same object the signing service reads when it enforces a limit. So an
option that parses is in force the moment it is parsed - before the command has
finished, and whether or not the command succeeds.

These tests pin what should happen instead: a command that reports failure
should leave the limits exactly as they were.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.commands import CommandHandler


@pytest.fixture
def temp_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


class TestAFailedEditChangesNothing:

    def test_a_rejected_edit_leaves_the_daily_limit_alone(self, core):
        """`policy edit p --day 500 --txn oops` is refused for the typo.

        Nothing should have changed. The daily limit should still be $10.
        """
        policy = core.create_policy(
            name="standard",
            daily_limit_micro=10_000_000,       # $10.00
            per_request_max_micro=10_000_000,
        )
        handler = CommandHandler(core)

        result = handler.execute("policy edit standard --day 500 --txn oops")
        assert not result.success, "the typo should be refused"

        live = core.get_policy(policy.id)
        assert live.daily_limit_micro == 10_000_000, (
            f"the refused edit still raised the live daily limit to "
            f"${live.daily_limit_micro / 1_000_000:.2f}")

    def test_a_rejected_edit_does_not_switch_payments_back_on(self, core):
        """A policy with x402 off should stay off when the edit is refused."""
        policy = core.create_policy(
            name="paused",
            daily_limit_micro=10_000_000,
            per_request_max_micro=10_000_000,
            x402_enabled=False,
        )
        handler = CommandHandler(core)

        result = handler.execute("policy edit paused --x402 on --day nonsense")
        assert not result.success

        live = core.get_policy(policy.id)
        assert live.x402_enabled is False, (
            "the refused edit switched x402 payments back on for this session")

    def test_a_rejected_edit_cannot_reach_disk_later(self, core, temp_data_dir):
        """The phantom limit must not be written out by the next unrelated save."""
        policy = core.create_policy(
            name="standard",
            daily_limit_micro=10_000_000,
            per_request_max_micro=10_000_000,
        )
        handler = CommandHandler(core)

        handler.execute("policy edit standard --day 500 --txn oops")
        # Any later save rewrites the whole file from memory.
        core.create_policy(name="unrelated", daily_limit_micro=1_000_000)

        on_disk = json.loads(
            (core.data_dir / "policies.json").read_text(encoding="utf-8"))
        saved = [p for p in on_disk if p["id"] == policy.id][0]
        assert saved["daily_limit_micro"] == 10_000_000, (
            f"the refused edit was persisted: policies.json now says "
            f"${saved['daily_limit_micro'] / 1_000_000:.2f} a day")


def _x402(amount_micro, index=0):
    """A well-formed USDG payment requirement, as tests/test_policy_bypass.py
    builds one."""
    from primer_vault.networks import TOKENS
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:4663",
            "amount": str(amount_micro),
            "asset": TOKENS["USDG"].addresses[4663],
            "payTo": f"0x{'65' * 19}{index:02x}",
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


class TestTheMoneyFollows:

    def test_a_rejected_edit_does_not_let_an_agent_spend_more(self, core):
        """End to end: the refused edit must not buy the agent a bigger payment."""
        agent, token = core.create_agent(name="Payer", auth_mode="bearer")
        policy = core.create_policy(
            name="standard", networks=[4663],
            daily_limit_micro=10_000_000,        # $10.00 a day
            per_request_max_micro=10_000_000,    # $10.00 a payment
            auto_approve_below_micro=500_000_000)  # sign without asking
        address = core.get_wallet_addresses()[0]
        core.commission_agent(agent.code, policy.id, address["address"])

        handler = CommandHandler(core)
        refused = handler.execute(
            "policy edit standard --day 500 --txn 100 --auto oops")
        assert not refused.success

        # $50 - five times the daily limit the user set and never changed.
        result = core._signing_service.handle_sign_request(
            agent_id=agent.id, x402_data=_x402(50_000_000), signature=token)

        assert result["status"] != "success", (
            "the agent got a $50 payment signed under a $10 daily limit, "
            "because a policy edit that Vault refused had already been applied")
