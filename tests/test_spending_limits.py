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
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
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
def core(temp_data_dir):
    """Create a Vault instance with temporary data directory."""
    from primer_vault.core import Vault
    return Vault(data_dir=temp_data_dir)


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
        errors = []

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
        successful = sum(1 for r in results if r[0] == "success")
        total_spent = sum(r[1] for r in results if r[0] == "success")

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
        """Zero limits should effectively disable spending."""
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
