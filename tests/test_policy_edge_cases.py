"""
Policy Edge Cases Tests

Tests for policy configuration validation, invalid configurations,
null field handling, and policy lifecycle edge cases.

These tests verify that policies are validated correctly and
edge cases are handled gracefully.
"""

import sys
import tempfile
import shutil
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models import SpendPolicy


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)




# =============================================================================
# Policy Validation Tests
# =============================================================================

class TestPolicyValidation:
    """Test policy field validation."""

    def test_negative_daily_limit_rejected(self):
        """Negative daily_limit_micro should be rejected."""
        with pytest.raises(ValueError) as exc_info:
            SpendPolicy.from_dict({
                "id": "test-id",
                "name": "BadPolicy",
                "networks": [4663],
                "daily_limit_micro": -1,
                "per_request_max_micro": 1000000,
                "auto_approve_below_micro": None,
                "created_at": "2024-01-01T00:00:00Z"
            })

        assert "daily_limit_micro" in str(exc_info.value).lower()

    def test_negative_per_request_max_rejected(self):
        """Negative per_request_max_micro should be rejected."""
        with pytest.raises(ValueError) as exc_info:
            SpendPolicy.from_dict({
                "id": "test-id",
                "name": "BadPolicy",
                "networks": [4663],
                "daily_limit_micro": 1000000,
                "per_request_max_micro": -1,
                "auto_approve_below_micro": None,
                "created_at": "2024-01-01T00:00:00Z"
            })

        assert "per_request_max_micro" in str(exc_info.value).lower()

    def test_negative_auto_approve_rejected(self):
        """Negative auto_approve_below_micro should be rejected."""
        with pytest.raises(ValueError) as exc_info:
            SpendPolicy.from_dict({
                "id": "test-id",
                "name": "BadPolicy",
                "networks": [4663],
                "daily_limit_micro": 1000000,
                "per_request_max_micro": 100000,
                "auto_approve_below_micro": -1,
                "created_at": "2024-01-01T00:00:00Z"
            })

        assert "auto_approve_below_micro" in str(exc_info.value).lower()

    def test_float_daily_limit_rejected(self):
        """Float daily_limit_micro should be rejected."""
        with pytest.raises((ValueError, TypeError)):
            SpendPolicy.from_dict({
                "id": "test-id",
                "name": "FloatPolicy",
                "networks": [4663],
                "daily_limit_micro": 1000000.5,  # Float
                "per_request_max_micro": 100000,
                "auto_approve_below_micro": None,
                "created_at": "2024-01-01T00:00:00Z"
            })

    def test_string_daily_limit_rejected(self):
        """String daily_limit_micro should be rejected."""
        with pytest.raises((ValueError, TypeError)):
            SpendPolicy.from_dict({
                "id": "test-id",
                "name": "StringPolicy",
                "networks": [4663],
                "daily_limit_micro": "1000000",  # String
                "per_request_max_micro": 100000,
                "auto_approve_below_micro": None,
                "created_at": "2024-01-01T00:00:00Z"
            })

    def test_zero_limits_accepted(self):
        """Zero limits should be accepted."""
        policy = SpendPolicy.from_dict({
            "id": "test-id",
            "name": "ZeroPolicy",
            "networks": [4663],
            "daily_limit_micro": 0,
            "per_request_max_micro": 0,
            "auto_approve_below_micro": 0,
            "created_at": "2024-01-01T00:00:00Z"
        })

        assert policy.daily_limit_micro == 0
        assert policy.per_request_max_micro == 0
        assert policy.auto_approve_below_micro == 0

    def test_very_large_limits_accepted(self):
        """Very large (but valid) limits should be accepted."""
        policy = SpendPolicy.from_dict({
            "id": "test-id",
            "name": "LargePolicy",
            "networks": [4663],
            "daily_limit_micro": 10**15,  # $1 billion
            "per_request_max_micro": 10**12,  # $1 million
            "auto_approve_below_micro": 10**9,  # $1000
            "created_at": "2024-01-01T00:00:00Z"
        })

        assert policy.daily_limit_micro == 10**15


# =============================================================================
# Network Configuration Tests
# =============================================================================

class TestNetworkConfiguration:
    """Test network configuration handling."""

    def test_empty_networks_list(self):
        """Empty networks list should be handled."""
        policy = SpendPolicy.create(
            name="NoNetworks",
            networks=[],  # Empty
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        assert policy.networks == []

    def test_none_networks_handled(self):
        """None networks should be converted to empty list."""
        policy = SpendPolicy.from_dict({
            "id": "test-id",
            "name": "NullNetworks",
            "networks": None,
            "daily_limit_micro": 1000000,
            "per_request_max_micro": 100000,
            "auto_approve_below_micro": None,
            "created_at": "2024-01-01T00:00:00Z"
        })

        assert policy.networks == []

    def test_multiple_networks(self):
        """Multiple networks should work."""
        policy = SpendPolicy.create(
            name="MultiNetwork",
            networks=[4663, 10, 42161],  # multiple chain ids (policy accepts a list)
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        assert len(policy.networks) == 3
        assert 4663 in policy.networks

    def test_duplicate_networks(self):
        """Duplicate networks in list should be allowed (implementation choice)."""
        policy = SpendPolicy.create(
            name="DuplicateNetworks",
            networks=[4663, 4663, 4663],  # Same network repeated
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        # May dedupe or keep duplicates - implementation dependent
        assert 4663 in policy.networks


# =============================================================================
# Null/None Field Tests
# =============================================================================

class TestNullFields:
    """Test handling of null/None fields."""

    def test_null_auto_approve(self):
        """Null auto_approve_below_micro should mean manual approval only."""
        policy = SpendPolicy.create(
            name="ManualOnly",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            auto_approve_below_micro=None
        )

        assert policy.auto_approve_below_micro is None

    def test_null_allowed_domains(self):
        """Null allowed_domains should be converted to empty list."""
        policy = SpendPolicy.create(
            name="NullDomains",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=None,
            blocked_domains=None
        )

        assert policy.allowed_domains == []
        assert policy.blocked_domains == []


# =============================================================================
# Policy Lifecycle Tests
# =============================================================================

class TestPolicyLifecycle:
    """Test policy lifecycle operations."""

    def test_create_policy_generates_id(self, core):
        """Creating policy should generate unique ID."""
        policy = core.create_policy(
            name="NewPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        assert policy.id is not None
        assert len(policy.id) > 0

    def test_create_policy_generates_timestamp(self, core):
        """Creating policy should set created_at."""
        policy = core.create_policy(
            name="TimestampPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        assert policy.created_at is not None

    def test_duplicate_policy_name_rejected(self, core):
        """Creating policy with duplicate name should be rejected."""
        core.create_policy(
            name="UniquePolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        with pytest.raises(ValueError) as exc_info:
            core.create_policy(
                name="UniquePolicy",  # Same name
                networks=[4663],
                daily_limit_micro=2000000,
                per_request_max_micro=200000
            )

        assert "already exists" in str(exc_info.value).lower()

    def test_update_policy_persists(self, core):
        """Updated policy values should persist."""
        policy = core.create_policy(
            name="UpdateTest",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        # Update
        policy.daily_limit_micro = 2000000
        core.update_policy(policy)

        # Retrieve
        retrieved = core.get_policy(policy.id)
        assert retrieved.daily_limit_micro == 2000000

    def test_delete_policy(self, core):
        """Deleting policy should remove it."""
        policy = core.create_policy(
            name="ToDelete",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000
        )

        policy_id = policy.id
        core.delete_policy(policy_id)

        assert core.get_policy(policy_id) is None


# =============================================================================
# Policy Formatting Tests
# =============================================================================

class TestPolicyFormatting:
    """Test policy value formatting."""

    def test_format_daily_limit(self):
        """Daily limit formatting should be correct."""
        policy = SpendPolicy.create(
            name="FormatTest",
            networks=[4663],
            daily_limit_micro=10_500_000,  # $10.50
            per_request_max_micro=100000
        )

        formatted = policy.format_daily_limit()
        assert "10.50" in formatted or "10.500000" in formatted
        assert "USDG" in formatted

    def test_format_per_request_max(self):
        """Per-request max formatting should be correct."""
        policy = SpendPolicy.create(
            name="FormatTest",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=500_000  # $0.50
        )

        formatted = policy.format_per_request_max()
        assert "0.50" in formatted or "0.500000" in formatted

    def test_format_auto_approve_none(self):
        """Auto-approve None should format as dash."""
        policy = SpendPolicy.create(
            name="FormatTest",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            auto_approve_below_micro=None
        )

        formatted = policy.format_auto_approve()
        assert formatted == "—" or formatted == "-"

    def test_format_domain_restrictions(self):
        """Domain restrictions should format correctly."""
        policy = SpendPolicy.create(
            name="DomainFormat",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["a.com", "b.com"],
            blocked_domains=["evil.com"]
        )

        formatted = policy.format_domain_restrictions()
        assert "allowed" in formatted.lower()
        assert "blocked" in formatted.lower()


# =============================================================================
# Policy Consistency Tests
# =============================================================================

class TestPolicyConsistency:
    """Test policy configuration consistency."""

    def test_per_request_max_greater_than_daily(self, core):
        """per_request_max > daily_limit should be allowed but questionable."""
        policy = core.create_policy(
            name="Inconsistent",
            networks=[4663],
            daily_limit_micro=100_000,  # $0.10
            per_request_max_micro=1_000_000  # $1.00 (10x daily!)
        )

        # This is allowed but makes little sense
        # Daily limit will always hit first
        assert policy.per_request_max_micro > policy.daily_limit_micro

    def test_auto_approve_equals_per_request_max(self, core):
        """auto_approve == per_request_max means all valid requests auto-approve."""
        policy = core.create_policy(
            name="AllAutoApprove",
            networks=[4663],
            daily_limit_micro=10_000_000,
            per_request_max_micro=1_000_000,
            auto_approve_below_micro=1_000_000  # Same as per-request
        )

        # All requests at or below per_request_max will auto-approve
        assert policy.auto_approve_below_micro == policy.per_request_max_micro

    def test_auto_approve_greater_than_per_request_max(self, core):
        """auto_approve > per_request_max means all valid requests auto-approve."""
        policy = core.create_policy(
            name="HighAutoApprove",
            networks=[4663],
            daily_limit_micro=10_000_000,
            per_request_max_micro=1_000_000,
            auto_approve_below_micro=5_000_000  # Higher than per-request
        )

        # Since valid requests must be <= per_request_max, all will auto-approve
        assert policy.auto_approve_below_micro > policy.per_request_max_micro


# =============================================================================
# Serialization Tests
# =============================================================================

class TestPolicySerialization:
    """Test policy to_dict/from_dict."""

    def test_round_trip_serialization(self):
        """Policy should survive to_dict/from_dict."""
        original = SpendPolicy.create(
            name="RoundTrip",
            networks=[4663],
            daily_limit_micro=5000000,
            per_request_max_micro=500000,
            auto_approve_below_micro=100000,
            allowed_domains=["stripe.com"],
            blocked_domains=["evil.com"]
        )

        data = original.to_dict()
        restored = SpendPolicy.from_dict(data)

        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.networks == original.networks
        assert restored.daily_limit_micro == original.daily_limit_micro
        assert restored.per_request_max_micro == original.per_request_max_micro
        assert restored.auto_approve_below_micro == original.auto_approve_below_micro
        assert restored.allowed_domains == original.allowed_domains
        assert restored.blocked_domains == original.blocked_domains

    def test_to_dict_returns_all_fields(self):
        """to_dict should include all fields."""
        policy = SpendPolicy.create(
            name="AllFields",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            auto_approve_below_micro=50000,
            allowed_domains=["example.com"],
            blocked_domains=["bad.com"]
        )

        data = policy.to_dict()

        required_fields = [
            "id", "name", "networks", "daily_limit_micro",
            "per_request_max_micro", "auto_approve_below_micro",
            "created_at", "allowed_domains", "blocked_domains"
        ]

        for field in required_fields:
            assert field in data, f"Missing field: {field}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
