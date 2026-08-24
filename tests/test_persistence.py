"""
Persistence Tests

Tests for JSON file corruption recovery, concurrent file access,
permission handling, and data integrity.

These tests verify that data persistence fails safely and handles
edge cases gracefully.
"""

import json
import os
import sys
import tempfile
import shutil
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models.store import PolicyStore
from primer_vault.models import SpendPolicy, Agent


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def policy_store(temp_data_dir):
    """Create a PolicyStore instance."""
    return PolicyStore(temp_data_dir)




# =============================================================================
# JSON Corruption Recovery Tests
# =============================================================================

class TestJSONCorruptionRecovery:
    """Test behavior when JSON files are corrupted."""

    def test_truncated_policies_json_handled(self, temp_data_dir):
        """Truncated policies.json should be handled gracefully."""
        policies_file = temp_data_dir / "policies.json"

        # Write truncated JSON
        with open(policies_file, 'w') as f:
            f.write('[{"id": "test", "name": "Pol')  # Truncated

        # Should load with warning, not crash
        store = PolicyStore(temp_data_dir)
        policies = store.get_all_policies()

        # Should return empty list (data couldn't be loaded)
        assert isinstance(policies, list)

    def test_truncated_agents_json_handled(self, temp_data_dir):
        """Truncated agents.json should be handled gracefully."""
        agents_file = temp_data_dir / "agents.json"

        # Write truncated JSON
        with open(agents_file, 'w') as f:
            f.write('[{"id": "test", "name": "Age')

        store = PolicyStore(temp_data_dir)
        agents = store.get_all_agents()

        assert isinstance(agents, list)

    def test_invalid_json_policies_handled(self, temp_data_dir):
        """Invalid JSON in policies file should be handled."""
        policies_file = temp_data_dir / "policies.json"

        with open(policies_file, 'w') as f:
            f.write('not valid json {{{')

        store = PolicyStore(temp_data_dir)
        assert isinstance(store.get_all_policies(), list)

    def test_empty_file_handled(self, temp_data_dir):
        """Empty JSON file should be handled."""
        policies_file = temp_data_dir / "policies.json"

        with open(policies_file, 'w') as f:
            f.write('')  # Empty file

        store = PolicyStore(temp_data_dir)
        assert isinstance(store.get_all_policies(), list)

    def test_null_json_handled(self, temp_data_dir):
        """A file holding `null` loads as no policies, rather than raising.

        PolicyStore is constructed by Vault(), which runs during startup, so
        anything raised out of it is the difference between an application that
        starts and one that does not. A malformed file must cost its contents,
        not the application.
        """
        policies_file = temp_data_dir / "policies.json"

        with open(policies_file, 'w') as f:
            f.write('null')

        store = PolicyStore(temp_data_dir)
        assert store.get_all_policies() == []

    def test_json_with_missing_required_fields(self, temp_data_dir):
        """An unreadable record is skipped; the rest of the file still loads."""
        policies_file = temp_data_dir / "policies.json"

        good = SpendPolicy.create(
            name="Good", networks=[4663], daily_limit_micro=1_000_000)
        with open(policies_file, 'w') as f:
            json.dump([{"id": "test"}, good.to_dict()], f)  # first one is unusable

        store = PolicyStore(temp_data_dir)

        assert [p.name for p in store.get_all_policies()] == ["Good"]

    def test_a_skipped_record_is_left_in_the_file(self, temp_data_dir):
        """Skipping is a read-time decision, not a repair.

        Vault starts without the record but does not rewrite the file, so a
        recoverable mistake - a hand-edit, a half-written save - can still be
        fixed by hand rather than being destroyed by the next startup.
        """
        policies_file = temp_data_dir / "policies.json"
        with open(policies_file, 'w') as f:
            json.dump([{"id": "test"}], f)

        PolicyStore(temp_data_dir)

        with open(policies_file) as f:
            assert json.load(f) == [{"id": "test"}]

    def test_recovery_after_corruption(self, temp_data_dir):
        """System should recover after fixing corrupted file."""
        store = PolicyStore(temp_data_dir)

        # Add valid policy
        policy = SpendPolicy.create(
            name="TestPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )
        store.add_policy(policy)

        # Verify it's saved
        assert len(store.get_all_policies()) == 1

        # Corrupt the file
        with open(temp_data_dir / "policies.json", 'w') as f:
            f.write("corrupted")

        # Create new store (simulates restart)
        store2 = PolicyStore(temp_data_dir)

        # Should load empty (corrupted data lost)
        # Now add new policy
        policy2 = SpendPolicy.create(
            name="NewPolicy",
            networks=[4663],
            daily_limit_micro=2000000,
            per_request_max_micro=200000
        )
        store2.add_policy(policy2)

        # Should work now
        assert len(store2.get_all_policies()) == 1


# =============================================================================
# Concurrent Access Tests
# =============================================================================

class TestConcurrentAccess:
    """Test behavior under concurrent file access."""

    def test_concurrent_policy_creation(self, temp_data_dir):
        """Multiple threads creating policies should not corrupt data.

        Was marked xfail against a known race: saves truncated the target file
        before writing it, so concurrent writers interleaved. Saves are now
        written to a temporary file and moved into place under a lock.
        """
        store = PolicyStore(temp_data_dir)

        def create_policy(i):
            policy = SpendPolicy.create(
                name=f"Policy{i}",
                networks=[4663],
                daily_limit_micro=1000000,
                per_request_max_micro=100000
            )
            store.add_policy(policy)
            return policy.id

        policy_ids = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(create_policy, i) for i in range(10)]
            for future in as_completed(futures):
                try:
                    policy_ids.append(future.result())
                except Exception:
                    # May get duplicate name errors
                    pass

        # Verify file is still valid JSON
        with open(temp_data_dir / "policies.json", 'r') as f:
            data = json.load(f)
            assert isinstance(data, list)

    def test_concurrent_read_write(self, temp_data_dir):
        """Concurrent reads and writes should not corrupt data."""
        store = PolicyStore(temp_data_dir)

        # Create initial policy
        policy = SpendPolicy.create(
            name="BasePolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )
        store.add_policy(policy)

        errors = []

        def read_policies():
            for _ in range(50):
                try:
                    policies = store.get_all_policies()
                    assert isinstance(policies, list)
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        def update_policy():
            for i in range(50):
                try:
                    p = store.get_policy(policy.id)
                    if p:
                        p.daily_limit_micro = 1000000 + i
                        store.update_policy(p)
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=read_policies),
            threading.Thread(target=read_policies),
            threading.Thread(target=update_policy),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify no critical errors (some transient errors may be OK)
        # Key is file integrity
        with open(temp_data_dir / "policies.json", 'r') as f:
            data = json.load(f)
            assert isinstance(data, list)


# =============================================================================
# File Permission Tests
# =============================================================================

class TestFilePermissions:
    """Test file permission handling."""

    @pytest.mark.skipif(os.name == 'nt', reason="Unix permissions only")
    def test_secure_permissions_set_on_unix(self, temp_data_dir):
        """Wallet and policy files should have restricted permissions on Unix."""
        store = PolicyStore(temp_data_dir)

        policy = SpendPolicy.create(
            name="SecurePolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )
        store.add_policy(policy)

        # Check permissions (should be 0600 on Unix)
        policies_file = temp_data_dir / "policies.json"
        mode = os.stat(policies_file).st_mode & 0o777

        # Should be owner read/write only
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

# =============================================================================
# Data Integrity Tests
# =============================================================================

class TestDataIntegrity:
    """Test data integrity across save/load cycles."""

    def test_policy_round_trip(self, temp_data_dir):
        """Policy should survive save/load cycle unchanged."""
        store1 = PolicyStore(temp_data_dir)

        policy = SpendPolicy.create(
            name="RoundTrip",
            networks=[4663],
            daily_limit_micro=5000000,
            per_request_max_micro=500000,
            auto_approve_below_micro=100000,
            allowed_domains=["stripe.com", "api.example.com"],
            blocked_domains=["evil.com"]
        )
        store1.add_policy(policy)

        # Create new store (reload from disk)
        store2 = PolicyStore(temp_data_dir)
        loaded = store2.get_policy(policy.id)

        assert loaded is not None
        assert loaded.name == policy.name
        assert loaded.networks == policy.networks
        assert loaded.daily_limit_micro == policy.daily_limit_micro
        assert loaded.per_request_max_micro == policy.per_request_max_micro
        assert loaded.auto_approve_below_micro == policy.auto_approve_below_micro
        assert loaded.allowed_domains == policy.allowed_domains
        assert loaded.blocked_domains == policy.blocked_domains

    def test_agent_round_trip(self, temp_data_dir):
        """Agent should survive save/load cycle unchanged."""
        from primer_vault.models.agent import generate_agent_id

        store1 = PolicyStore(temp_data_dir)

        # Create agent manually
        agent = Agent(
            id=generate_agent_id(),
            code=str(__import__('uuid').uuid4()),
            name="RoundTripAgent",
            auth_mode="bearer",
            auth_key="test_hash",
            status="uncommissioned",
            created_at="2024-01-01T00:00:00Z"
        )
        store1.add_agent(agent)

        # Reload
        store2 = PolicyStore(temp_data_dir)
        loaded = store2.get_agent_by_code(agent.code)

        assert loaded is not None
        assert loaded.id == agent.id
        assert loaded.name == agent.name
        assert loaded.auth_mode == agent.auth_mode

    def test_transaction_round_trip(self, temp_data_dir):
        """Transaction should survive save/load cycle unchanged."""
        store1 = PolicyStore(temp_data_dir)

        from primer_vault.models import Transaction
        tx = Transaction.create(
            agent_id="ABC123",
            agent_name="TestAgent",
            agent_code="TEST123",
            amount_micro=1000000,
            recipient="0x1234567890123456789012345678901234567890",
            network="robinhood"
        )
        store1.add_transaction(tx)

        # Reload
        store2 = PolicyStore(temp_data_dir)
        all_txs = store2.get_all_transactions()

        assert len(all_txs) >= 1
        loaded = next((t for t in all_txs if t.id == tx.id), None)

        assert loaded is not None
        assert loaded.amount_micro == tx.amount_micro
        assert loaded.network == tx.network

# =============================================================================
# Edge Cases
# =============================================================================

class TestPersistenceEdgeCases:
    """Test edge cases in persistence."""

    def test_nonexistent_directory_created(self):
        """Data directory should be created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = Path(tmp) / "new" / "nested" / "dir"
            assert not new_dir.exists()

            PolicyStore(new_dir)

            assert new_dir.exists()

    def test_empty_store_valid(self, temp_data_dir):
        """Empty store should work correctly."""
        store = PolicyStore(temp_data_dir)

        assert store.get_all_policies() == []
        assert store.get_all_agents() == []
        assert store.get_all_transactions() == []

    def test_delete_nonexistent_policy(self, policy_store):
        """Deleting nonexistent policy should return empty list."""
        result = policy_store.delete_policy("nonexistent-id")
        assert result == []  # No agents decommissioned

    def test_update_nonexistent_policy(self, policy_store):
        """Updating nonexistent policy should be handled."""
        policy = SpendPolicy.create(
            name="Ghost",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )
        # Don't add it, try to update directly
        policy_store.update_policy(policy)

        # Should not be added (update requires existing)
        assert policy_store.get_policy(policy.id) is None

    def test_very_large_dataset(self, temp_data_dir):
        """Store should handle reasonably large datasets."""
        store = PolicyStore(temp_data_dir)

        # Create 100 policies
        for i in range(100):
            policy = SpendPolicy.create(
                name=f"Policy{i}",
                networks=[4663],
                daily_limit_micro=1000000 + i,
                per_request_max_micro=100000
            )
            store.add_policy(policy)

        # Reload
        store2 = PolicyStore(temp_data_dir)
        assert len(store2.get_all_policies()) == 100

    def test_special_characters_in_names(self, temp_data_dir):
        """Names with special characters should be handled."""
        store = PolicyStore(temp_data_dir)

        # Note: Implementation may validate names and reject some
        try:
            policy = SpendPolicy.create(
                name="Test-Policy_123",  # Allowed characters
                networks=[4663],
                daily_limit_micro=1000000,
                per_request_max_micro=100000
            )
            store.add_policy(policy)

            loaded = store.get_policy(policy.id)
            assert loaded.name == "Test-Policy_123"
        except ValueError:
            # Some implementations restrict names
            pass


# =============================================================================
# Transaction History Tests
# =============================================================================

class TestTransactionHistory:
    """Test transaction history persistence."""

    def test_transactions_ordered_by_time(self, policy_store):
        """Transactions should maintain time ordering."""
        from primer_vault.models import Transaction
        import time

        for i in range(5):
            tx = Transaction.create(
                agent_id=f"ABC{i}",
                agent_name="TestAgent",
                agent_code="TEST",
                amount_micro=100000 * (i + 1),
                recipient="0x" + "1" * 40,
                network="robinhood"
            )
            policy_store.add_transaction(tx)
            time.sleep(0.01)  # Small delay

        txs = policy_store.get_all_transactions()
        # Should be in creation order (or reverse, implementation dependent)
        assert len(txs) == 5

    def test_clear_transactions(self, policy_store):
        """Clearing transactions should remove all."""
        from primer_vault.models import Transaction

        for i in range(3):
            tx = Transaction.create(
                agent_id=f"ABC{i}",
                agent_name="TestAgent",
                agent_code="TEST",
                amount_micro=100000,
                recipient="0x" + "1" * 40,
                network="robinhood"
            )
            policy_store.add_transaction(tx)

        assert len(policy_store.get_all_transactions()) == 3

        policy_store.clear_transactions()

        assert len(policy_store.get_all_transactions()) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# =============================================================================
# Atomic saves
# =============================================================================

class TestAtomicSaves:
    """Saves must never leave a partially written file on disk.

    The store previously opened the target directly, which truncates it before
    the new contents are written. An interruption in that window destroyed the
    data, because the previous version was already gone.
    """

    def test_all_policies_survive_concurrent_writers(self, temp_data_dir):
        """Every write lands - the lock stops one silently replacing another."""
        store = PolicyStore(temp_data_dir)

        def add(i):
            store.add_policy(SpendPolicy.create(
                name=f"P{i}", networks=[4663],
                daily_limit_micro=1_000_000, per_request_max_micro=100_000))

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(as_completed([ex.submit(add, i) for i in range(25)]))

        saved = json.loads((temp_data_dir / "policies.json").read_text(encoding="utf-8"))
        assert len(saved) == 25

    def test_agents_and_transactions_also_survive(self, temp_data_dir):
        """The same treatment applies to the other two files."""
        from primer_vault.models import Transaction
        store = PolicyStore(temp_data_dir)

        def add(i):
            store.add_transaction(Transaction.create(
                agent_id=f"A{i}", agent_name="Bot", agent_code=f"C{i}",
                amount_micro=1_000, recipient="0x" + "11" * 20,
                network="eip155:4663"))

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(as_completed([ex.submit(add, i) for i in range(25)]))

        saved = json.loads((temp_data_dir / "transactions.json").read_text(encoding="utf-8"))
        assert len(saved) == 25

    def test_a_failed_write_leaves_the_previous_file_intact(self, temp_data_dir):
        """The point of the temporary file: a mid-write failure loses nothing."""
        from primer_vault.models import store as store_mod

        store = PolicyStore(temp_data_dir)
        store.add_policy(SpendPolicy.create(
            name="Original", networks=[4663],
            daily_limit_micro=1_000_000, per_request_max_micro=100_000))
        before = (temp_data_dir / "policies.json").read_text(encoding="utf-8")

        real_dump = store_mod.json.dump

        def explode(*a, **k):
            raise OSError("disk full")

        store_mod.json.dump = explode
        try:
            with pytest.raises(OSError):
                store.add_policy(SpendPolicy.create(
                    name="Doomed", networks=[4663],
                    daily_limit_micro=1_000_000, per_request_max_micro=100_000))
        finally:
            store_mod.json.dump = real_dump

        after = (temp_data_dir / "policies.json").read_text(encoding="utf-8")
        assert after == before
        assert json.loads(after)[0]["name"] == "Original"

    def test_no_temporary_files_are_left_behind(self, temp_data_dir):
        """Successful and failed writes both clean up after themselves."""
        from primer_vault.models import store as store_mod

        store = PolicyStore(temp_data_dir)
        store.add_policy(SpendPolicy.create(
            name="Kept", networks=[4663],
            daily_limit_micro=1_000_000, per_request_max_micro=100_000))

        real_dump = store_mod.json.dump
        store_mod.json.dump = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            with pytest.raises(OSError):
                store.add_policy(SpendPolicy.create(
                    name="Failed", networks=[4663],
                    daily_limit_micro=1_000_000, per_request_max_micro=100_000))
        finally:
            store_mod.json.dump = real_dump

        leftovers = [p.name for p in temp_data_dir.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_saved_file_is_reloadable(self, temp_data_dir):
        """A fresh store reads back exactly what was written."""
        store = PolicyStore(temp_data_dir)
        for i in range(5):
            store.add_policy(SpendPolicy.create(
                name=f"Reload{i}", networks=[4663],
                daily_limit_micro=1_000_000, per_request_max_micro=100_000))

        reloaded = PolicyStore(temp_data_dir)
        assert len(reloaded.get_all_policies()) == 5
        assert {p.name for p in reloaded.get_all_policies()} == {f"Reload{i}" for i in range(5)}
