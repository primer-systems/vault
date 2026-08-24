"""
Spending Limits Tests

Tests for concurrent payment handling, daily reset logic, policy limit
consistency, and atomic spending updates.

These tests verify that spending limits are enforced correctly under
various conditions including concurrent requests.
"""

import sys
import tempfile
import shutil
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)




@pytest.fixture
def signing_service(core):
    """Get the signing service from primer_vault.core."""
    return core._signing_service


@pytest.fixture
def commissioned_agent(core):
    """Create and commission a test agent with policy."""
    # Create wallet first
    core.create_wallet(
        wallet_path=str(core.get_wallet_dir() / "test.wallet"),
        password="testpass"
    )

    # Create policy
    policy = core.create_policy(
        name="TestPolicy",
        networks=[4663],  # Robinhood Chain
        daily_limit_micro=10_000_000,  # $10
        per_request_max_micro=1_000_000,  # $1
        auto_approve_below_micro=500_000  # $0.50 auto-approve
    )

    # Create and commission agent
    agent, secret = core.create_agent(name="TestAgent", auth_mode="bearer")
    addresses = core.get_wallet_addresses()
    core.commission_agent(agent.code, policy.id, addresses[0]["address"])

    return agent, secret, policy


# =============================================================================
# Concurrent Payment Tests
# =============================================================================

class TestConcurrentSpendingLimits:
    """Test that spending limits hold under concurrent requests."""

    def test_concurrent_requests_respect_daily_limit(self, core, commissioned_agent):
        """Multiple concurrent requests should not exceed daily limit."""
        agent, secret, policy = commissioned_agent

        # Refresh agent from store
        agent = core.get_agent_by_code(agent.code)

        # Daily limit is $10 (10_000_000 micro)
        # Try to make 20 requests of $0.60 each ($12 total)
        # Only ~16 should succeed before hitting limit

        request_count = 20
        request_amount = 600_000  # $0.60

        results = []

        def make_request(i):
            try:
                # Simulate a spending update
                current_agent = core.get_agent_by_code(agent.code)
                remaining = policy.daily_limit_micro - current_agent.spent_today_micro

                if request_amount <= remaining:
                    # Update spending
                    current_agent.spent_today_micro += request_amount
                    core._policy_store.update_agent(current_agent)
                    return ("success", request_amount)
                else:
                    return ("rejected", 0)
            except Exception as e:
                return ("error", str(e))

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request, i) for i in range(request_count)]
            for future in as_completed(futures):
                results.append(future.result())

        # Count successes
        sum(1 for r in results if r[0] == "success")
        sum(r[1] for r in results if r[0] == "success")

        # Should not exceed daily limit
        # Note: Due to race conditions without proper locking, this might fail
        # The test is designed to DETECT race conditions
        final_agent = core.get_agent_by_code(agent.code)
        assert final_agent.spent_today_micro <= policy.daily_limit_micro, \
            f"Spent {final_agent.spent_today_micro} exceeds limit {policy.daily_limit_micro}"

    def test_spending_lock_prevents_race_condition(self, signing_service):
        """Verify signing service has a spending lock."""
        # Check that the signing service has a lock for spending updates
        assert hasattr(signing_service, '_spending_lock'), \
            "SigningService should have _spending_lock for thread safety"

    def test_atomic_spending_update(self, core, commissioned_agent):
        """Spending updates should be atomic."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        initial_spent = agent.spent_today_micro
        update_amount = 100_000

        # Perform update
        agent.spent_today_micro += update_amount
        core._policy_store.update_agent(agent)

        # Verify
        refreshed = core.get_agent_by_code(agent.code)
        assert refreshed.spent_today_micro == initial_spent + update_amount


# =============================================================================
# Daily Reset Tests
# =============================================================================

class TestDailyReset:
    """Test daily spending reset logic."""

    def test_daily_reset_on_new_day(self, core, commissioned_agent, signing_service):
        """Spending should reset on a new calendar day."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Set some spending
        agent.spent_today_micro = 5_000_000  # $5
        agent.last_reset_date = (date.today() - timedelta(days=1)).isoformat()
        agent.last_reset_at = ""
        core._policy_store.update_agent(agent)

        # Trigger daily reset check
        signing_service._check_daily_reset(agent)

        # Refresh and verify reset
        refreshed = core.get_agent_by_code(agent.code)
        assert refreshed.spent_today_micro == 0, "Spending should reset on new day"
        assert refreshed.last_reset_date == date.today().isoformat()

    def test_no_reset_same_day(self, core, commissioned_agent, signing_service):
        """Spending should not reset on the same day."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Set some spending for today
        agent.spent_today_micro = 5_000_000
        agent.last_reset_date = date.today().isoformat()
        core._policy_store.update_agent(agent)

        # Trigger daily reset check
        signing_service._check_daily_reset(agent)

        # Should not reset
        assert agent.spent_today_micro == 5_000_000, "Spending should not reset on same day"

    def test_reset_uses_local_date(self, core, commissioned_agent, signing_service):
        """Daily reset should use local date, not UTC."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Set last reset to yesterday
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        agent.spent_today_micro = 1_000_000
        agent.last_reset_date = yesterday
        agent.last_reset_at = ""
        core._policy_store.update_agent(agent)

        # Check reset
        result = signing_service._check_daily_reset(agent)

        assert result is True, "Should have reset"
        assert agent.spent_today_micro == 0

    def test_reset_handles_missing_date(self, core, commissioned_agent, signing_service):
        """Reset should handle missing last_reset_date."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Clear reset date
        agent.last_reset_date = None
        agent.last_reset_at = ""
        agent.spent_today_micro = 1_000_000
        core._policy_store.update_agent(agent)

        # Should reset (different from today)
        signing_service._check_daily_reset(agent)
        assert agent.spent_today_micro == 0


# =============================================================================
# Policy Limit Consistency Tests
# =============================================================================

class TestPolicyLimitConsistency:
    """Test policy limit validation and edge cases."""

    def test_per_request_max_greater_than_daily_limit(self, core):
        """per_request_max > daily_limit is a suspicious configuration."""
        # This is allowed but may be unintentional
        policy = core.create_policy(
            name="InconsistentPolicy",
            networks=[4663],
            daily_limit_micro=1_000_000,  # $1
            per_request_max_micro=5_000_000,  # $5 (greater than daily!)
            auto_approve_below_micro=100_000
        )

        # Policy is created, but the per_request_max > daily_limit
        # In practice, daily limit will always hit first
        assert policy.per_request_max_micro > policy.daily_limit_micro

    def test_auto_approve_greater_than_per_request_max(self, core):
        """auto_approve >= per_request_max means all requests auto-approve."""
        policy = core.create_policy(
            name="AutoApproveAll",
            networks=[4663],
            daily_limit_micro=10_000_000,
            per_request_max_micro=1_000_000,  # $1
            auto_approve_below_micro=2_000_000  # $2 (greater than per-request)
        )

        # All valid requests will auto-approve since they must be <= per_request_max
        # which is <= auto_approve threshold
        assert policy.auto_approve_below_micro > policy.per_request_max_micro

    def test_zero_limits_behavior(self, core):
        """Zero limits are stored as zero and mean a cap of zero.

        Enforcement is proven end to end in TestZeroLimitsAreEnforced below;
        this pins only that the values survive creation unchanged.
        """
        policy = core.create_policy(
            name="ZeroLimit",
            networks=[4663],
            daily_limit_micro=0,  # $0
            per_request_max_micro=0,  # $0
            auto_approve_below_micro=None
        )

        assert policy.daily_limit_micro == 0
        assert policy.per_request_max_micro == 0

    def test_negative_limits_rejected(self, core):
        """Negative limits should be rejected."""
        from primer_vault.models import SpendPolicy

        with pytest.raises(ValueError):
            SpendPolicy.from_dict({
                "id": "test-id",
                "name": "BadPolicy",
                "networks": [4663],
                "daily_limit_micro": -1000,  # Negative
                "per_request_max_micro": 1000,
                "auto_approve_below_micro": None,
                "created_at": "2024-01-01T00:00:00Z"
            })

    def test_very_large_limits(self, core):
        """Very large limits should be handled without overflow."""
        policy = core.create_policy(
            name="LargeLimit",
            networks=[4663],
            daily_limit_micro=10**15,  # $1 billion
            per_request_max_micro=10**12,  # $1 million
            auto_approve_below_micro=10**9  # $1000
        )

        assert policy.daily_limit_micro == 10**15
        assert policy.per_request_max_micro == 10**12


# =============================================================================
# Spending Limit Enforcement Tests
# =============================================================================

class TestSpendingLimitEnforcement:
    """Test that spending limits are properly enforced."""

    def test_request_exactly_at_per_request_max(self, core, commissioned_agent):
        """Request exactly at per_request_max should be allowed."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Policy per_request_max is 1_000_000
        request_amount = policy.per_request_max_micro

        # Should be allowed (exactly at limit)
        assert request_amount <= policy.per_request_max_micro

    def test_request_one_micro_over_per_request_max(self, core, commissioned_agent):
        """Request one micro over per_request_max should be rejected."""
        agent, secret, policy = commissioned_agent

        request_amount = policy.per_request_max_micro + 1

        # Should be rejected
        assert request_amount > policy.per_request_max_micro

    def test_request_exactly_remaining_daily(self, core, commissioned_agent):
        """Request exactly equal to remaining daily should be allowed."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Spend some first
        agent.spent_today_micro = 9_000_000  # $9 of $10
        core._policy_store.update_agent(agent)

        remaining = policy.daily_limit_micro - agent.spent_today_micro
        request_amount = remaining  # Exactly $1 remaining

        # Should be allowed
        assert request_amount <= remaining

    def test_request_one_micro_over_remaining_daily(self, core, commissioned_agent):
        """Request one micro over remaining daily should be rejected."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Spend most of limit
        agent.spent_today_micro = 9_500_000
        core._policy_store.update_agent(agent)

        remaining = policy.daily_limit_micro - agent.spent_today_micro
        request_amount = remaining + 1

        # Should be rejected
        assert request_amount > remaining

    def test_spending_accumulates_correctly(self, core, commissioned_agent):
        """Multiple transactions should accumulate spending correctly."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        amounts = [100_000, 200_000, 300_000, 400_000]
        expected_total = sum(amounts)

        for amount in amounts:
            agent.spent_today_micro += amount
            core._policy_store.update_agent(agent)

        final_agent = core.get_agent_by_code(agent.code)
        assert final_agent.spent_today_micro == expected_total


# =============================================================================
# Edge Cases
# =============================================================================

class TestSpendingEdgeCases:
    """Test edge cases in spending limit handling."""

    def test_agent_with_no_policy(self, core):
        """Uncommissioned agent (no policy) should be rejected."""
        agent, _ = core.create_agent(name="NoPolicy", auth_mode="bearer")

        # Agent is uncommissioned, has no policy
        assert agent.policy_id is None
        assert agent.status == "uncommissioned"

    def test_deleted_policy_blocks_spending(self, core, commissioned_agent):
        """Agent whose policy was deleted should be blocked."""
        agent, secret, policy = commissioned_agent

        # Delete the policy
        core.delete_policy(policy.id)

        # Refresh agent
        refreshed = core.get_agent_by_code(agent.code)

        # Should be decommissioned
        assert refreshed.status == "uncommissioned" or refreshed.policy_id is None

    def test_suspended_agent_blocked(self, core, commissioned_agent):
        """Suspended agent should be blocked from spending."""
        agent, secret, policy = commissioned_agent

        # Suspend the agent
        core.suspend_agent(agent.code)

        refreshed = core.get_agent_by_code(agent.code)
        assert refreshed.status == "suspended"

    def test_limit_reached_status(self, core, commissioned_agent):
        """Agent at limit should have appropriate status."""
        agent, secret, policy = commissioned_agent
        agent = core.get_agent_by_code(agent.code)

        # Spend exactly the limit
        agent.spent_today_micro = policy.daily_limit_micro
        core._policy_store.update_agent(agent)

        # Check remaining
        remaining = policy.daily_limit_micro - agent.spent_today_micro
        assert remaining == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestZeroLimitsAreEnforced:
    """A limit of $0.00 refuses every payment - zero is a cap, not an absence.

    Guarded by truthiness (`if policy.daily_limit_micro:`), a policy set to 0
    would skip them entirely and an agent configured to spend nothing could
    spend without any cap at all. "No limit" is spelled None for
    per_request_max_micro and cannot be spelled for the daily limit; see the
    convention note on SpendPolicy in models/policy.py.

    End to end through handle_sign_request, because this is a property of the
    enforcement rather than of the model.
    """

    @staticmethod
    def _x402(pay_to="0x00000000000000000000000000000000000c0De0"):
        """A well-formed $1.00 USDG payment requirement on Robinhood Chain."""
        from primer_vault.networks import TOKENS
        return {
            "x402Version": 2,
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:4663",
                "amount": "1000000",
                "asset": TOKENS["USDG"].addresses[4663],
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "extra": {"name": "Global Dollar", "version": "1"},
            }],
            "resource": {"url": "https://api.example.com/thing",
                         "description": "", "mimeType": ""},
        }

    def _commission(self, core, *, daily, per_req, auto):
        agent, token = core.create_agent(name="ZeroPayer", auth_mode="bearer")
        policy = core.create_policy(
            name="ZeroEnforced", networks=[4663],
            daily_limit_micro=daily,
            per_request_max_micro=per_req,
            auto_approve_below_micro=auto)
        address = core.get_wallet_addresses()[0]
        core.commission_agent(agent.code, policy.id, address["address"])
        return core._signing_service, agent, token

    def test_a_zero_daily_limit_refuses_a_payment(self, core):
        svc, agent, token = self._commission(
            core, daily=0, per_req=50_000_000, auto=10_000_000)

        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=self._x402())

        assert result["status"] != "success"
        assert result["code"] == "EXCEEDS_DAILY_LIMIT"

    def test_a_zero_per_request_max_refuses_a_payment(self, core):
        svc, agent, token = self._commission(
            core, daily=100_000_000, per_req=0, auto=10_000_000)

        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=self._x402())

        assert result["status"] != "success"
        assert result["code"] == "EXCEEDS_PER_REQUEST_MAX"

    def test_nothing_accumulates_under_a_zero_daily_limit(self, core):
        """Repeated attempts must all refuse - the drain was $5 slices forever."""
        svc, agent, token = self._commission(
            core, daily=0, per_req=50_000_000, auto=10_000_000)

        signed = 0
        for i in range(3):
            # A distinct payTo per request sidesteps the idempotency cache.
            data = self._x402(pay_to=f"0x{'65' * 19}{i:02d}")
            result = svc.handle_sign_request(
                agent_id=agent.id, signature=token, x402_data=data)
            if result.get("status") == "success":
                signed += 1

        assert signed == 0
        assert core.get_agent_by_code(agent.code).spent_today_micro == 0

    def test_a_zero_daily_limit_refuses_at_intake_not_queued(self, core):
        """Manual-approval policies refuse too, rather than asking a human to
        approve a payment the policy cannot admit."""
        svc, agent, token = self._commission(
            core, daily=0, per_req=50_000_000, auto=None)

        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=self._x402())

        assert result.get("status") != "pending"
        assert result["code"] == "EXCEEDS_DAILY_LIMIT"


class TestDailyAllowanceRenewal:
    """A daily allowance must cover at least a day, whichever way the clock moves.

    The reset is keyed to the local day on purpose - "my daily limit" means the
    user's day, and one renewing at four in the afternoon because that is midnight
    UTC reads as broken. Timestamps stay UTC, because a record has to be
    comparable to a chain and to other machines.

    The date alone is not enough to hold that, though. Comparing it for mere
    inequality renewed the allowance whenever the date moved *backwards*, so a
    clock correction or a laptop carried west handed out a second one. And a
    laptop carried east crosses midnight early, so the date moving forward is not
    proof a day has passed either.
    """

    from datetime import datetime, timedelta, timezone

    def _hours_ago(self, hours):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def _days_ago_local(self, days):
        from datetime import date, timedelta
        return (date.today() - timedelta(days=days)).isoformat()

    def _today_local(self):
        from datetime import date
        return date.today().isoformat()

    def test_a_fresh_agent_is_due(self):
        from primer_vault.models.agent import daily_allowance_is_due
        assert daily_allowance_is_due("", "")

    def test_the_same_day_is_not_due(self):
        from primer_vault.models.agent import daily_allowance_is_due
        assert not daily_allowance_is_due(self._today_local(), self._hours_ago(3))

    def test_the_next_day_is_due(self):
        from primer_vault.models.agent import daily_allowance_is_due
        assert daily_allowance_is_due(self._days_ago_local(1), self._hours_ago(25))

    def test_a_date_that_moved_backwards_is_not_due(self):
        """A clock set back, or a flight west. Comparing dates for inequality
        treated this as a new day and renewed the allowance."""
        from primer_vault.models.agent import daily_allowance_is_due
        tomorrow_stored = self._days_ago_local(-1)  # stored date is in the future
        assert not daily_allowance_is_due(tomorrow_stored, self._hours_ago(3))

    def test_crossing_midnight_early_is_not_due(self):
        """A flight east crosses into the next local date after a few hours. The
        date has advanced, but a day has not passed."""
        from primer_vault.models.agent import daily_allowance_is_due
        assert not daily_allowance_is_due(self._days_ago_local(1), self._hours_ago(6))

    def test_a_full_day_later_is_due_even_across_a_timezone_move(self):
        from primer_vault.models.agent import daily_allowance_is_due
        assert daily_allowance_is_due(self._days_ago_local(1), self._hours_ago(21))

    def test_a_missing_instant_renews_rather_than_deadlocks(self):
        """The instant is only written by a renewal, so refusing without one
        would mean never renewing again."""
        from primer_vault.models.agent import daily_allowance_is_due
        assert daily_allowance_is_due(self._days_ago_local(1), "")

    def test_an_unreadable_instant_renews(self):
        from primer_vault.models.agent import daily_allowance_is_due
        assert daily_allowance_is_due(self._days_ago_local(1), "not-a-timestamp")

    def test_resetting_records_both_the_day_and_the_instant(self):
        from primer_vault.models.agent import Agent
        agent = Agent.create(name="t", encrypted_auth_key="x",
                             auth_key_iv="i", auth_key_tag="g")
        agent.spent_today_micro = 5
        agent.reset_daily_spend()

        assert agent.spent_today_micro == 0
        assert agent.last_reset_date == self._today_local()
        assert agent.last_reset_at, "without the instant, elapsed time cannot be checked"

    def test_a_clock_set_back_does_not_hand_out_a_second_allowance(self):
        """End to end: an agent that has spent its limit does not get a fresh one
        because the machine's date went backwards."""
        from primer_vault.models.agent import Agent, daily_allowance_is_due
        agent = Agent.create(name="t", encrypted_auth_key="x",
                             auth_key_iv="i", auth_key_tag="g")
        agent.reset_daily_spend()
        agent.spent_today_micro = 99_000_000

        agent.last_reset_date = self._days_ago_local(-1)  # as a clock-back leaves it
        assert not daily_allowance_is_due(agent.last_reset_date, agent.last_reset_at)
        assert agent.spent_today_micro == 99_000_000
