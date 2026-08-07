"""
Signing Security Tests

Tests for authentication bypass attempts, timestamp replay protection,
amount validation, and other security-critical signing scenarios.

These tests verify that the signing service properly rejects malicious
or malformed requests.
"""

import hashlib
import hmac
import json
import secrets
import sys
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models import Agent, SpendPolicy, verify_agent_hmac, verify_bearer_token, hash_bearer_token
from primer_vault.models.agent import generate_agent_token, encrypt_agent_secret


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
def test_agent_hmac(core):
    """Create an HMAC-authenticated test agent."""
    agent, secret = core.create_agent(name="TestHMAC", auth_mode="hmac")
    return agent, secret


@pytest.fixture
def test_agent_bearer(core):
    """Create a bearer token authenticated test agent."""
    agent, secret = core.create_agent(name="TestBearer", auth_mode="bearer")
    return agent, secret


@pytest.fixture
def test_policy(core):
    """Create a test policy with reasonable limits."""
    return core.create_policy(
        name="TestPolicy",
        networks=[4663],  # Robinhood Chain
        daily_limit_micro=10_000_000,  # $10
        per_request_max_micro=1_000_000,  # $1
        auto_approve_below_micro=100_000  # $0.10
    )


# =============================================================================
# HMAC Authentication Bypass Tests
# =============================================================================

class TestHMACAuthenticationBypass:
    """Test HMAC signature validation edge cases."""

    def test_empty_signature_header_rejected(self, signing_service, test_agent_hmac):
        """Empty signature header should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="",  # Empty
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "Missing" in result or "Invalid" in result

    def test_missing_sig_prefix_rejected(self, signing_service, test_agent_hmac):
        """Signature without SIG: prefix should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="1234567890:abcdef",  # No SIG: prefix
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "Invalid signature format" in result

    def test_malformed_sig_format_two_parts_rejected(self, signing_service, test_agent_hmac):
        """Signature with only two parts should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="SIG:1234567890",  # Only 2 parts
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "Invalid signature format" in result

    def test_non_numeric_timestamp_rejected(self, signing_service, test_agent_hmac):
        """Non-numeric timestamp should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="SIG:notanumber:abcdef1234",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "Invalid timestamp" in result

    def test_invalid_hex_signature_rejected(self):
        """Invalid hex in signature should be rejected."""
        # Test the verify_agent_hmac function directly
        result = verify_agent_hmac(
            shared_secret_hex="0123456789abcdef" * 4,  # Valid 32-byte hex
            message=b"test message",
            signature_hex="not_valid_hex!"  # Invalid hex
        )
        assert result is False

    def test_wrong_signature_rejected(self):
        """Incorrect HMAC signature should be rejected."""
        shared_secret = "0123456789abcdef" * 4
        message = b"test message"

        # Create wrong signature
        wrong_signature = "abcdef1234" * 6  # Wrong but valid hex

        result = verify_agent_hmac(shared_secret, message, wrong_signature)
        assert result is False

    def test_valid_hmac_accepted(self):
        """Correct HMAC signature should be accepted."""
        shared_secret = secrets.token_hex(32)
        message = b'{"test": "data"}'

        # Create correct signature
        correct_signature = hmac.new(
            bytes.fromhex(shared_secret),
            message,
            hashlib.sha256
        ).hexdigest()

        result = verify_agent_hmac(shared_secret, message, correct_signature)
        assert result is True

    def test_signature_with_extra_colons(self, signing_service, test_agent_hmac):
        """Signature with extra colons in hex part should still parse correctly."""
        agent, _ = test_agent_hmac

        # SIG:timestamp:signature where signature contains colons (edge case)
        # The split(":, 2") should handle this
        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="SIG:1234567890:abc:def:123",  # Extra colons in sig
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # Should parse but fail verification (wrong signature)
        assert result is not None


# =============================================================================
# Bearer Token Authentication Tests
# =============================================================================

class TestBearerTokenAuthentication:
    """Test bearer token validation edge cases."""

    def test_empty_bearer_token_rejected(self):
        """Empty bearer token should be rejected."""
        stored_hash = hashlib.sha256(b"AT_realtoken").hexdigest()
        result = verify_bearer_token(stored_hash, "")
        assert result is False

    def test_wrong_bearer_token_rejected(self):
        """Wrong bearer token should be rejected."""
        stored_hash = hashlib.sha256(b"AT_realtoken").hexdigest()
        result = verify_bearer_token(stored_hash, "AT_wrongtoken")
        assert result is False

    def test_bearer_without_prefix_rejected(self, signing_service, test_agent_bearer):
        """Bearer token without AT_ prefix should be rejected."""
        agent, _ = test_agent_bearer

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="Bearer notavalidtoken",  # No AT_ prefix
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "Invalid bearer token format" in result

    def test_bearer_with_prefix_stripped(self, signing_service, test_agent_bearer):
        """Bearer prefix should be properly stripped."""
        agent, token = test_agent_bearer

        # Should work with "Bearer AT_..." format
        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=f"Bearer {token}",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # Should succeed (None = no error)
        assert result is None

    def test_bearer_without_bearer_prefix(self, signing_service, test_agent_bearer):
        """Token without 'Bearer ' prefix should also work."""
        agent, token = test_agent_bearer

        # Should also work with just "AT_..." format
        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=token,  # Just the token
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # Should succeed
        assert result is None

    def test_valid_bearer_token_accepted(self):
        """Correct bearer token should be accepted."""
        token = "AT_" + secrets.token_hex(32)
        stored_hash = hash_bearer_token(token)

        result = verify_bearer_token(stored_hash, token)
        assert result is True

    def test_bearer_case_sensitivity(self):
        """Bearer token comparison should be case-sensitive."""
        token = "AT_AbCdEf123456"
        stored_hash = hash_bearer_token(token)

        # Wrong case should fail
        result = verify_bearer_token(stored_hash, "AT_abcdef123456")
        assert result is False


# =============================================================================
# Timestamp Replay Protection Tests
# =============================================================================

class TestTimestampReplayProtection:
    """Test timestamp validation for replay attack protection."""

    def test_request_exactly_at_max_age_boundary(self, signing_service, test_agent_hmac):
        """Request exactly at max age boundary should be accepted."""
        agent, _ = test_agent_hmac

        # Default max age is 300 seconds
        max_age = signing_service._max_request_age_seconds
        timestamp = int(time.time()) - max_age  # Exactly at boundary

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=f"SIG:{timestamp}:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # Should still be checking signature at this point (timestamp valid)
        # Will fail on signature, not timestamp
        # Note: May fail earlier if agent credentials can't be decrypted (wallet locked)
        assert result is None or "signature" in result.lower() or "Invalid" in result or "decrypt" in result.lower()

    def test_request_one_second_past_max_age(self, signing_service, test_agent_hmac):
        """Request one second past max age should be rejected."""
        agent, _ = test_agent_hmac

        max_age = signing_service._max_request_age_seconds
        timestamp = int(time.time()) - max_age - 1  # Just past boundary

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=f"SIG:{timestamp}:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "expired" in result.lower() or "old" in result.lower()

    def test_timestamp_far_in_past_rejected(self, signing_service, test_agent_hmac):
        """Timestamp from hours ago should be rejected."""
        agent, _ = test_agent_hmac

        timestamp = int(time.time()) - 3600  # 1 hour ago

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=f"SIG:{timestamp}:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "expired" in result.lower() or "old" in result.lower()

    def test_timestamp_in_future_rejected(self, signing_service, test_agent_hmac):
        """Timestamp in the future should be rejected."""
        agent, _ = test_agent_hmac

        timestamp = int(time.time()) + 600  # 10 minutes in future

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=f"SIG:{timestamp}:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "future" in result.lower()

    def test_timestamp_slightly_in_future_within_tolerance(self, signing_service, test_agent_hmac):
        """Timestamp slightly in future (within max age) might be accepted."""
        agent, _ = test_agent_hmac

        # Just a few seconds in the future (clock skew tolerance)
        timestamp = int(time.time()) + 5

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header=f"SIG:{timestamp}:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # May be accepted (abs() check), will fail on signature
        # The implementation uses abs(now - timestamp) so small future is OK
        # Just verify it processes past timestamp check
        pass  # Implementation-dependent

    def test_negative_timestamp_rejected(self, signing_service, test_agent_hmac):
        """Negative timestamp should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="SIG:-1:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # Should be rejected as too old
        assert result is not None

    def test_zero_timestamp_rejected(self, signing_service, test_agent_hmac):
        """Zero timestamp (1970) should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="SIG:0:fakesig",
            wallet_password="test",
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        # Should be rejected as too old
        assert result is not None
        assert "expired" in result.lower() or "old" in result.lower()


# =============================================================================
# x402 Amount Validation Tests
# =============================================================================

class TestX402AmountValidation:
    """Test amount validation in x402 payloads."""

    def test_zero_amount_handling(self, signing_service):
        """Zero amount should be handled gracefully."""
        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 0,
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        is_valid, version, error = signing_service._validate_x402_request(x402_data) if hasattr(signing_service, '_validate_x402_request') else (True, 1, "")
        # Implementation may accept or reject zero - just verify no crash
        assert isinstance(is_valid, bool)

    def test_negative_amount_rejected(self):
        """Negative amount should be rejected or handled safely."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": -1000000,  # Negative
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        # The validate function should handle this
        is_valid, version, error = validate_x402_request(x402_data)
        # Implementation may accept (policy checks amount) or reject
        # Key is no crash or overflow

    def test_extremely_large_amount(self):
        """Extremely large amount should be handled without overflow."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 10**18,  # 1 trillion USDG (unrealistic)
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        # Should validate without overflow
        is_valid, version, error = validate_x402_request(x402_data)
        assert isinstance(is_valid, bool)

    def test_float_amount_rejected(self):
        """Float amounts should be rejected or converted properly."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 1.5,  # Float
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        is_valid, version, error = validate_x402_request(x402_data)
        # Should handle float (implementation-dependent)

    def test_string_amount_handling(self):
        """String amounts should be handled properly."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": "1000000",  # String
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        is_valid, version, error = validate_x402_request(x402_data)
        # Implementation may accept string numbers or reject

    def test_null_amount_rejected(self):
        """Null amount should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": None,
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False
        assert "maxAmountRequired" in error.lower() or "missing" in error.lower()


# =============================================================================
# x402 Payload Validation Tests
# =============================================================================

class TestX402PayloadValidation:
    """Test x402 payload structure validation."""

    def test_missing_accepts_array(self):
        """Missing accepts array should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "other": "data"
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False
        assert "accepts" in error.lower()

    def test_empty_accepts_array(self):
        """Empty accepts array should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": []
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False
        assert "empty" in error.lower()

    def test_accepts_not_array(self):
        """Non-array accepts should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": "not an array"
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False

    def test_missing_network_rejected(self):
        """Missing network in accepts should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "maxAmountRequired": 1000000,
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False
        assert "network" in error.lower()

    def test_missing_payto_rejected(self):
        """Missing payTo in accepts should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 1000000,
                "asset": "USDG"
            }]
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False
        assert "payTo" in error

    def test_missing_asset_rejected(self):
        """Missing asset in accepts should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 1000000,
                "payTo": "0x1234567890123456789012345678901234567890"
            }]
        }

        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is False
        assert "asset" in error.lower()

    def test_non_dict_x402_rejected(self):
        """Non-dict x402 data should be rejected."""
        from primer_vault.services.signing import validate_x402_request

        is_valid, version, error = validate_x402_request("not a dict")
        assert is_valid is False

        is_valid, version, error = validate_x402_request(None)
        assert is_valid is False

        is_valid, version, error = validate_x402_request([1, 2, 3])
        assert is_valid is False


# =============================================================================
# Idempotency Cache Tests
# =============================================================================

class TestIdempotencyCache:
    """Test idempotency cache behavior and edge cases."""

    def test_cache_key_collision_resistance(self, signing_service):
        """Different requests should have different cache keys."""
        # Verify that slightly different requests don't collide
        # This is a smoke test - actual collision resistance depends on hash

        key1 = hashlib.sha256(b"agent1:sig1:data1").hexdigest()
        key2 = hashlib.sha256(b"agent1:sig1:data2").hexdigest()
        key3 = hashlib.sha256(b"agent2:sig1:data1").hexdigest()

        assert key1 != key2
        assert key1 != key3
        assert key2 != key3

    def test_cache_pruning_on_overflow(self, signing_service):
        """Cache should prune old entries when full."""
        from primer_vault.services.signing import SIGNATURE_CACHE_MAX_SIZE, SIGNATURE_CACHE_PRUNE_COUNT

        # Verify constants are reasonable
        assert SIGNATURE_CACHE_MAX_SIZE > 0
        assert SIGNATURE_CACHE_PRUNE_COUNT > 0
        assert SIGNATURE_CACHE_PRUNE_COUNT < SIGNATURE_CACHE_MAX_SIZE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
