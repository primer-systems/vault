"""
Core Operations Functional Tests

Tests for Core agent and policy operations.
These test the actual functionality, not just architecture.

IMPORTANT: These tests call REAL Core methods with REAL signatures.
Any API changes require updating these tests.
"""

import tempfile
import shutil
from pathlib import Path

import pytest


# We need to add src to path for imports
import sys
SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)




@pytest.fixture
def event_collector(core):
    """Collect all events emitted by Core."""
    from primer_vault.core.events import EventType

    events = []

    def collect(event):
        events.append(event)

    # Subscribe to all event types
    for event_type in EventType:
        core.event_bus.subscribe(event_type, collect)

    return events


class TestAgentOperations:
    """Test Core agent CRUD operations."""

    def test_create_agent(self, core, event_collector):
        """Creating an agent should return agent and secret, emit event."""
        agent, secret = core.create_agent(
            name="TestAgent",
            auth_mode="bearer"
        )

        assert agent is not None
        assert agent.name == "TestAgent"
        assert agent.auth_mode == "bearer"
        assert secret is not None
        assert len(secret) > 0

        # Check event was emitted
        agent_events = [e for e in event_collector if 'AGENT' in str(e.type)]
        assert len(agent_events) >= 1

    def test_create_agent_generates_unique_ids(self, core):
        """Each agent should get a unique ID and code."""
        agent1, _ = core.create_agent(name="Agent1", auth_mode="bearer")
        agent2, _ = core.create_agent(name="Agent2", auth_mode="bearer")

        assert agent1.id != agent2.id
        assert agent1.code != agent2.code

    def test_get_agent_by_code(self, core):
        """Should be able to retrieve agent by code."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")

        # Correct method name: get_agent_by_code
        retrieved = core.get_agent_by_code(agent.code)
        assert retrieved is not None
        assert retrieved.id == agent.id
        assert retrieved.name == agent.name

    def test_get_agent_by_id(self, core):
        """Should be able to retrieve agent by short ID."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")

        # Correct method name: get_agent_by_id
        retrieved = core.get_agent_by_id(agent.id)
        assert retrieved is not None
        assert retrieved.code == agent.code
        assert retrieved.name == agent.name

    def test_get_all_agents(self, core):
        """Should retrieve all agents."""
        core.create_agent(name="Agent1", auth_mode="bearer")
        core.create_agent(name="Agent2", auth_mode="bearer")
        core.create_agent(name="Agent3", auth_mode="bearer")

        agents = core.get_all_agents()
        assert len(agents) == 3

    def test_suspend_agent(self, core, event_collector):
        """Suspending an agent should change status and emit event."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")

        result = core.suspend_agent(agent.code)
        assert result is True

        # Re-fetch to check status
        updated = core.get_agent_by_code(agent.code)
        assert updated.status == "suspended"

        # Check event
        suspend_events = [
            e for e in event_collector
            if 'AGENT_UPDATED' in str(e.type)
        ]
        assert len(suspend_events) >= 1

    def test_activate_agent(self, core):
        """Activating a suspended agent should change status."""
        # Agent must be commissioned (have a policy) to be activated
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)
        core.commission_agent(
            agent_code=agent.code,
            policy_id=policy.id,
            wallet_address="0x1234567890123456789012345678901234567890"
        )
        core.suspend_agent(agent.code)

        result = core.activate_agent(agent.code)
        assert result is True

        updated = core.get_agent_by_code(agent.code)
        assert updated.status == "active"

    def test_delete_agent(self, core, event_collector):
        """Deleting an agent should remove it and emit event."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")
        code = agent.code

        result = core.delete_agent(code)
        assert result is True

        # Should not be retrievable
        deleted = core.get_agent_by_code(code)
        assert deleted is None

        # Check event
        delete_events = [e for e in event_collector if 'AGENT_DELETED' in str(e.type)]
        assert len(delete_events) >= 1

    def test_delete_nonexistent_agent_raises(self, core):
        """Deleting a non-existent agent should raise ValueError."""
        with pytest.raises(ValueError):
            core.delete_agent("NONEXISTENT")

    def test_suspend_nonexistent_agent_raises(self, core):
        """Suspending a non-existent agent should raise ValueError."""
        with pytest.raises(ValueError):
            core.suspend_agent("NONEXISTENT")


class TestPolicyOperations:
    """Test Core policy CRUD operations."""

    def test_create_policy(self, core, event_collector):
        """Creating a policy should return it and emit event."""
        # Correct parameter names: daily_limit_micro, etc.
        policy = core.create_policy(
            name="TestPolicy",
            daily_limit_micro=100_000_000,  # $100 in micro units
            per_request_max_micro=10_000_000,  # $10
            auto_approve_below_micro=5_000_000,  # $5
            networks=[4663]  # Robinhood Chain
        )

        assert policy is not None
        assert policy.name == "TestPolicy"
        assert policy.daily_limit_micro == 100_000_000
        assert policy.per_request_max_micro == 10_000_000
        assert policy.auto_approve_below_micro == 5_000_000

        # Check event
        policy_events = [e for e in event_collector if 'POLICY_CREATED' in str(e.type)]
        assert len(policy_events) >= 1

    def test_get_all_policies(self, core):
        """Should retrieve all policies."""
        core.create_policy(name="Policy1", daily_limit_micro=100_000_000)
        core.create_policy(name="Policy2", daily_limit_micro=200_000_000)

        policies = core.get_all_policies()
        assert len(policies) == 2

    def test_get_policy(self, core):
        """Should retrieve policy by ID."""
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)

        retrieved = core.get_policy(policy.id)
        assert retrieved is not None
        assert retrieved.name == "TestPolicy"

    def test_update_policy(self, core, event_collector):
        """Updating a policy should change values and emit event."""
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)

        # Clear events from creation
        event_collector.clear()

        # Update the policy object directly (like Console does)
        policy.name = "UpdatedPolicy"
        policy.daily_limit_micro = 200_000_000
        policy.per_request_max_micro = 20_000_000

        result = core.update_policy(policy)
        assert result is True

        updated = core.get_policy(policy.id)
        assert updated.name == "UpdatedPolicy"
        assert updated.daily_limit_micro == 200_000_000

        # Check event
        update_events = [e for e in event_collector if 'POLICY_UPDATED' in str(e.type)]
        assert len(update_events) >= 1

    def test_delete_policy(self, core, event_collector):
        """Deleting a policy should remove it and emit event."""
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)
        policy_id = policy.id

        # delete_policy returns list of decommissioned agents
        decommissioned = core.delete_policy(policy_id)
        assert isinstance(decommissioned, list)

        # Should not be retrievable
        deleted = core.get_policy(policy_id)
        assert deleted is None

        # Check event
        delete_events = [e for e in event_collector if 'POLICY_DELETED' in str(e.type)]
        assert len(delete_events) >= 1

    def test_delete_nonexistent_policy_raises(self, core):
        """Deleting a non-existent policy should raise ValueError."""
        with pytest.raises(ValueError):
            core.delete_policy("nonexistent-id")


class TestCommissionFlow:
    """Test the agent commissioning flow."""

    def test_commission_agent(self, core, event_collector):
        """Commissioning an agent should link it to policy and wallet."""
        # Create prerequisites
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)
        wallet_address = "0x1234567890123456789012345678901234567890"

        # Clear events
        event_collector.clear()

        result = core.commission_agent(
            agent_code=agent.code,
            policy_id=policy.id,
            wallet_address=wallet_address
        )

        assert result is True

        # Check agent is updated
        updated = core.get_agent_by_code(agent.code)
        assert updated.policy_id == policy.id
        assert updated.wallet_address == wallet_address
        assert updated.status == "active"

        # Check event
        update_events = [e for e in event_collector if 'AGENT_UPDATED' in str(e.type)]
        assert len(update_events) >= 1

    def test_commission_with_mandate(self, core):
        """Commissioning can include an intent mandate."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)
        wallet_address = "0x1234567890123456789012345678901234567890"

        mandate = {"type": "IntentMandate", "version": "1.0", "id": "test"}

        result = core.commission_agent(
            agent_code=agent.code,
            policy_id=policy.id,
            wallet_address=wallet_address,
            intent_mandate=mandate
        )

        assert result is True

        updated = core.get_agent_by_code(agent.code)
        assert updated.intent_mandate is not None
        assert updated.intent_mandate.get("id") == "test"

    def test_commission_nonexistent_agent_raises(self, core):
        """Commissioning a non-existent agent should raise ValueError."""
        policy = core.create_policy(name="TestPolicy", daily_limit_micro=100_000_000)

        with pytest.raises(ValueError):
            core.commission_agent(
                agent_code="NONEXISTENT",
                policy_id=policy.id,
                wallet_address="0x1234567890123456789012345678901234567890"
            )

    def test_commission_nonexistent_policy_raises(self, core):
        """Commissioning with non-existent policy should raise ValueError."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")

        with pytest.raises(ValueError):
            core.commission_agent(
                agent_code=agent.code,
                policy_id="nonexistent-id",
                wallet_address="0x1234567890123456789012345678901234567890"
            )


class TestMandateOperations:
    """Test intent mandate operations through Core."""

    def test_set_agent_mandate(self, core, event_collector):
        """Setting an agent's mandate should update it and emit event."""
        agent, _ = core.create_agent(name="TestAgent", auth_mode="bearer")

        event_collector.clear()

        mandate = {"type": "IntentMandate", "id": "test-mandate"}
        result = core.set_agent_mandate(agent.code, mandate)

        assert result is True

        updated = core.get_agent_by_code(agent.code)
        assert updated.intent_mandate is not None
        assert updated.intent_mandate.get("id") == "test-mandate"

        # Check event
        update_events = [e for e in event_collector if 'AGENT_UPDATED' in str(e.type)]
        assert len(update_events) >= 1

    def test_set_mandate_nonexistent_agent_raises(self, core):
        """Setting mandate on non-existent agent should raise ValueError."""
        mandate = {"type": "IntentMandate", "id": "test"}
        with pytest.raises(ValueError):
            core.set_agent_mandate("NONEXISTENT", mandate)


class TestEventBus:
    """Test EventBus functionality."""

    def test_emit_activity(self, core, event_collector):
        """emit_activity should create ACTIVITY events."""
        from primer_vault.core.events import EventType

        core.event_bus.emit_activity("Test message", is_error=False)

        activity_events = [e for e in event_collector if e.type == EventType.ACTIVITY]
        assert len(activity_events) == 1
        assert activity_events[0].data["message"] == "Test message"
        assert activity_events[0].data["is_error"] is False

    def test_multiple_subscribers(self, core):
        """Multiple subscribers should all receive events."""
        from primer_vault.core.events import EventType

        received = []

        def handler1(event):
            received.append(("h1", event))

        def handler2(event):
            received.append(("h2", event))

        core.event_bus.subscribe(EventType.ACTIVITY, handler1)
        core.event_bus.subscribe(EventType.ACTIVITY, handler2)

        core.event_bus.emit_activity("Test")

        assert len(received) == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
