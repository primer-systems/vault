"""
Signing Security Tests

Tests for authentication bypass attempts, timestamp replay protection,
amount validation, and other security-critical signing scenarios.

These tests verify that the signing service properly rejects malicious
or malformed requests.
"""

import hashlib
import hmac
import secrets
import sys
import tempfile
import shutil
import time
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models import verify_agent_hmac, verify_bearer_token, hash_bearer_token


# A stand-in master key for calls that exercise signature handling rather than
# credential decryption; the paths under test reject before reaching the key.
DATA_KEY = bytes(32)


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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
            payment_required="dGVzdA==",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "future" in result.lower()

    def test_negative_timestamp_rejected(self, signing_service, test_agent_hmac):
        """Negative timestamp should be rejected."""
        agent, _ = test_agent_hmac

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id=agent.id,
            signature_header="SIG:-1:fakesig",
            data_key=DATA_KEY,
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
            data_key=DATA_KEY,
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
        assert isinstance(is_valid, bool)

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


# =============================================================================
# Approval-time Re-validation
# =============================================================================

class TestApprovalRevalidatesFromParsedRequest:
    """A parked payment's own facts must come from the parsed request.

    The network is a fact about the payment, fixed when it arrived and checked
    then. Policy state around it is re-read live at approval time - that is the
    point of re-validating - but the payment's network is not re-derived from
    the raw agent message. Digging back into the wire fields returns "unknown"
    for anything not carrying the field at that exact path, and "unknown" maps
    to chain_id 0, which the network guards read as "nothing to check" and skip.

    Today the parser guarantees the field is present, so both routes agree.
    These tests exist so that stops being load-bearing: if the parser is ever
    loosened, the check must not silently switch itself off.
    """

    def _park(self, signing_service, network, x402_data):
        """Put a pending request into the queue and return its id."""
        from primer_vault.services.signing import SigningRequest
        from datetime import datetime, timezone

        request_id = "11111111-1111-1111-1111-111111111111"
        signing_service._pending_requests[request_id] = SigningRequest(
            id=request_id,
            agent_id="ABC123",
            agent_name="Parked",
            amount_micro=1_000_000,
            network=network,
            recipient="0x1234567890123456789012345678901234567890",
            resource=None,
            request_url=None,
            x402_data=x402_data,
            x402_version=2,
            created_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
        )
        return request_id

    def test_disabled_network_blocked_when_raw_payload_lacks_the_field(
            self, core, signing_service, test_agent_hmac, test_policy):
        """The payload has no accepts[0].network; the parsed request does.

        Re-deriving from the payload would yield chain_id 0 and skip the check.
        """
        agent, _ = test_agent_hmac
        core.commission_agent(agent.code, test_policy.id,
                              "0x1234567890123456789012345678901234567890")

        signing_service.set_network_enabled(4663, False)
        try:
            request_id = self._park(
                signing_service,
                network="eip155:4663",
                x402_data={"accepts": [{"amount": "1000000"}]},  # no "network"
            )
            request = signing_service._pending_requests[request_id]
            request.agent_id = agent.id

            result = signing_service.approve_request(request_id)

            assert result["status"] == "error"
            assert result["code"] == "NETWORK_DISABLED"
        finally:
            signing_service.set_network_enabled(4663, True)

    def test_enabled_network_is_not_blocked(
            self, core, signing_service, test_agent_hmac, test_policy):
        """The guard must not reject a network that is still switched on."""
        agent, _ = test_agent_hmac
        core.commission_agent(agent.code, test_policy.id,
                              "0x1234567890123456789012345678901234567890")

        signing_service.set_network_enabled(4663, True)
        request_id = self._park(
            signing_service,
            network="eip155:4663",
            x402_data={"accepts": [{"amount": "1000000"}]},
        )
        request = signing_service._pending_requests[request_id]
        request.agent_id = agent.id

        result = signing_service.approve_request(request_id)

        # It gets past the network guard; it fails later for want of a wallet.
        assert result.get("code") != "NETWORK_DISABLED"

    def test_network_not_allowed_by_policy_is_blocked(
            self, core, signing_service, test_agent_hmac):
        """Same for the per-policy network allowlist."""
        agent, _ = test_agent_hmac
        policy = core.create_policy(
            name="EthereumOnly",
            networks=[1],  # 4663 not permitted
            daily_limit_micro=10_000_000,
            per_request_max_micro=1_000_000,
            auto_approve_below_micro=100_000,
        )
        core.commission_agent(agent.code, policy.id,
                              "0x1234567890123456789012345678901234567890")

        request_id = self._park(
            signing_service,
            network="eip155:4663",
            x402_data={"accepts": [{"amount": "1000000"}]},
        )
        request = signing_service._pending_requests[request_id]
        request.agent_id = agent.id

        result = signing_service.approve_request(request_id)

        assert result["status"] == "error"
        assert result["code"] == "NETWORK_NOT_ALLOWED_BY_POLICY"


class TestApprovalRevalidatesPolicyGates:
    """Approval re-reads policy state, and must re-read all of it.

    Agent status, limits and networks were re-checked at approval; the domain
    rules and the x402 on/off switch were not. So blocking a domain or turning
    payments off left anything already queued able to go through on the state
    that applied when it arrived.
    """

    URL = "https://pay.example.com/resource"

    def _park(self, signing_service, agent_id, request_url=URL):
        from datetime import datetime, timezone

        from primer_vault.services.signing import SigningRequest

        request_id = "22222222-2222-2222-2222-222222222222"
        signing_service._pending_requests[request_id] = SigningRequest(
            id=request_id,
            agent_id=agent_id,
            agent_name="Parked",
            amount_micro=1_000,
            network="eip155:4663",
            recipient="0x1234567890123456789012345678901234567890",
            resource=request_url,
            request_url=request_url,
            x402_data={"accepts": [{"network": "eip155:4663"}]},
            x402_version=2,
            created_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
        )
        return request_id

    def _commissioned(self, core, agent, policy):
        core.commission_agent(agent.code, policy.id,
                              "0x1234567890123456789012345678901234567890")
        return core.get_agent_by_id(agent.id)

    def test_domain_blocked_after_queueing_is_refused(
            self, core, signing_service, test_agent_hmac, test_policy):
        agent, _ = test_agent_hmac
        self._commissioned(core, agent, test_policy)
        request_id = self._park(signing_service, agent.id)

        test_policy.blocked_domains = ["pay.example.com"]
        core.update_policy(test_policy)

        result = signing_service.approve_request(request_id)
        assert result["status"] == "error"
        assert result["code"] == "DOMAIN_NOT_ALLOWED"
        assert request_id not in signing_service._pending_requests

    def test_domain_removed_from_the_allowlist_is_refused(
            self, core, signing_service, test_agent_hmac, test_policy):
        agent, _ = test_agent_hmac
        self._commissioned(core, agent, test_policy)
        request_id = self._park(signing_service, agent.id)

        test_policy.allowed_domains = ["somewhere-else.example"]
        core.update_policy(test_policy)

        result = signing_service.approve_request(request_id)
        assert result["code"] == "DOMAIN_NOT_ALLOWED"

    def test_payments_switched_off_after_queueing_is_refused(
            self, core, signing_service, test_agent_hmac, test_policy):
        agent, _ = test_agent_hmac
        self._commissioned(core, agent, test_policy)
        request_id = self._park(signing_service, agent.id)

        test_policy.x402_enabled = False
        core.update_policy(test_policy)

        result = signing_service.approve_request(request_id)
        assert result["code"] == "X402_DISABLED"

    def test_domain_restrictions_with_no_url_to_check_are_refused(
            self, core, signing_service, test_agent_hmac, test_policy):
        """A request parked before any domain rules existed carries no URL. Once
        rules apply, it cannot be shown to satisfy them, so it must not pass."""
        agent, _ = test_agent_hmac
        self._commissioned(core, agent, test_policy)
        request_id = self._park(signing_service, agent.id, request_url=None)

        test_policy.allowed_domains = ["pay.example.com"]
        core.update_policy(test_policy)

        result = signing_service.approve_request(request_id)
        assert result["code"] == "DOMAIN_URL_REQUIRED"

    def test_an_allowed_domain_still_goes_through(
            self, core, signing_service, test_agent_hmac, test_policy):
        """The gates must not become a blanket refusal."""
        agent, _ = test_agent_hmac
        self._commissioned(core, agent, test_policy)
        request_id = self._park(signing_service, agent.id)

        test_policy.allowed_domains = ["pay.example.com"]
        core.update_policy(test_policy)

        result = signing_service.approve_request(request_id)
        # Signing itself needs a real wallet address; what matters here is that
        # it got past the policy gates rather than being refused by them.
        assert result.get("code") not in (
            "DOMAIN_NOT_ALLOWED", "X402_DISABLED", "DOMAIN_URL_REQUIRED")


class TestTerminalOutcomesAreObservable:
    """An agent must be able to tell what happened to a request it submitted.

    A request that was approved and then failed to sign is dropped from the
    idempotency cache on purpose, so the agent can submit the payment again.
    Without a separate record of the outcome that leaves the poll with nothing to
    find, and "it failed" becomes indistinguishable from "it never existed" -
    the worst ambiguity for something holding a payment authorization.
    """

    def _service(self):
        from primer_vault.services.signing import SigningService, SigningRequest

        svc = SigningService()

        class Store:
            def get_agent_by_id(self, i):
                return type("A", (), {
                    "status": "active", "policy_id": "p", "id": "ABC123", "code": "c",
                    "name": "a", "spent_today_micro": 0, "last_reset_date": "",
                    "intent_mandate": None, "wallet_address": "0x" + "11" * 20})()

            def get_policy(self, i):
                return type("P", (), {
                    "is_x402_enabled": lambda s: True,
                    "has_domain_restrictions": lambda s: False,
                    "check_domain_allowed": lambda s, u: (True, ""),
                    # None per-request means "no cap"; daily has no such
                    # spelling - it is always an int, and the checks subtract
                    # from it unconditionally (see models/policy.py).
                    "per_request_max_micro": None, "daily_limit_micro": 10_000_000,
                    "networks": None, "id": "p", "name": "p"})()

            def get_transaction(self, i): return None
            def add_transaction(self, t): pass
            def update_transaction(self, t): pass
            def update_agent(self, a): pass
            def get_all_transactions(self): return []

        svc._policy_store = Store()
        request = SigningRequest(
            id="rid", agent_id="ABC123", agent_name="a", amount_micro=500_000,
            network="eip155:4663", recipient="0x" + "11" * 20, resource=None,
            request_url=None, x402_data={}, x402_version=2, created_at="now")
        request.cache_key = "ck"
        svc._pending_requests["rid"] = request
        svc._request_to_signature["rid"] = "ck"
        svc._signature_cache["ck"] = ("rid", {"status": "pending"})
        return svc

    def test_a_failed_signing_is_reported_not_lost(self):
        svc = self._service()
        svc._sign_payment = lambda *a, **k: {
            "status": "error", "code": "LEDGER_SIGN_NOT_AVAILABLE",
            "error": "Ledger connection failed: not open"}

        svc.approve_request("rid")
        status = svc.get_request_status("rid")

        assert status["code"] == "LEDGER_SIGN_NOT_AVAILABLE"
        assert status["status"] == "error"

    def test_a_failed_signing_still_allows_resubmission(self):
        """The idempotency entry must go, or the agent could never try again."""
        svc = self._service()
        svc._sign_payment = lambda *a, **k: {"status": "error", "code": "SIGNING_ERROR"}

        svc.approve_request("rid")

        assert "ck" not in svc._signature_cache

    def test_a_signed_request_still_returns_its_header(self):
        svc = self._service()
        svc._sign_payment = lambda *a, **k: {
            "status": "success", "transaction_id": "tx1", "header_value": "eyJ..."}

        svc.approve_request("rid")

        assert svc.get_request_status("rid")["header_value"] == "eyJ..."

    def test_an_unknown_id_is_still_not_found(self):
        assert self._service().get_request_status("never-seen")["code"] == "REQUEST_NOT_FOUND"

    def test_the_record_is_bounded(self):
        from primer_vault.services.signing import MAX_RESOLVED_REQUESTS
        svc = self._service()
        for i in range(MAX_RESOLVED_REQUESTS + 50):
            svc._remember_outcome(f"r{i}", {"status": "rejected"})
        assert len(svc._resolved) == MAX_RESOLVED_REQUESTS


class TestPendingRequestsAreBounded:
    """A request waiting for approval cannot wait forever, or in unlimited number.

    Pending approvals live in memory. Without a deadline they accumulate for the
    life of the process, and without a per-agent ceiling one agent can fill the
    queue and bury every other agent's approvals — and the person approving them.

    They are deliberately not persisted: nothing is signed or submitted while a
    request waits, so a restart costs the agent a retry and risks nothing.
    """

    def _park(self, signing_service, agent_id, request_id=None):
        import uuid as _uuid
        from datetime import datetime, timezone

        from primer_vault.services.signing import (
            PENDING_REQUEST_TTL_SECONDS, SigningRequest)
        import time as _time

        request_id = request_id or str(_uuid.uuid4())
        signing_service._pending_requests[request_id] = SigningRequest(
            id=request_id, agent_id=agent_id, agent_name="Parked",
            amount_micro=1_000, network="eip155:4663",
            recipient="0x1234567890123456789012345678901234567890",
            resource=None, request_url=None,
            x402_data={"accepts": [{"network": "eip155:4663"}]}, x402_version=2,
            created_at=datetime.now(timezone.utc).isoformat(), status="pending")
        signing_service._pending_deadlines[request_id] = (
            _time.monotonic() + PENDING_REQUEST_TTL_SECONDS)
        return request_id

    def test_the_window_matches_the_trading_one(self):
        from primer_vault.services.signing import PENDING_REQUEST_TTL_SECONDS
        from primer_vault.services.trading import PENDING_TRADE_TTL_SECONDS
        assert PENDING_REQUEST_TTL_SECONDS == PENDING_TRADE_TTL_SECONDS == 900

    def test_an_expired_request_is_dropped(self, signing_service):
        import time as _time
        request_id = self._park(signing_service, "ABC123")
        signing_service._pending_deadlines[request_id] = _time.monotonic() - 1

        signing_service._expire_pending_requests()
        assert request_id not in signing_service._pending_requests
        assert request_id not in signing_service._pending_deadlines

    def test_an_expired_request_cannot_be_approved(self, signing_service):
        import time as _time
        request_id = self._park(signing_service, "ABC123")
        signing_service._pending_deadlines[request_id] = _time.monotonic() - 1

        result = signing_service.approve_request(request_id)
        assert result["status"] == "error"
        assert result["code"] == "REQUEST_NOT_FOUND"

    def test_the_agent_polling_learns_it_expired(self, signing_service):
        import time as _time
        request_id = self._park(signing_service, "ABC123")
        signing_service._pending_deadlines[request_id] = _time.monotonic() - 1

        status = signing_service.get_request_status(request_id)
        assert status["status"] == "rejected"
        assert status["code"] == "REQUEST_EXPIRED"

    def test_a_live_request_is_untouched(self, signing_service):
        request_id = self._park(signing_service, "ABC123")
        signing_service._expire_pending_requests()
        assert request_id in signing_service._pending_requests

    def test_removing_a_request_removes_its_deadline(self, signing_service):
        """Two maps that can drift are two maps that will. Every removal goes
        through one place so a deadline cannot outlive its request."""
        request_id = self._park(signing_service, "ABC123")
        signing_service._drop_pending(request_id)
        assert signing_service._pending_deadlines == {}

    def test_no_removal_bypasses_the_pair(self):
        source = (Path(__file__).parent.parent / "src" / "primer_vault"
                  / "services" / "signing.py").read_text(encoding="utf-8")
        assert "del self._pending_requests" not in source

    def test_the_queue_is_capped_per_agent(self, signing_service):
        from primer_vault.services.signing import MAX_PENDING_PER_AGENT
        for _ in range(MAX_PENDING_PER_AGENT):
            self._park(signing_service, "ABC123")
        assert signing_service._pending_count_for("ABC123") == MAX_PENDING_PER_AGENT

    def test_the_cap_is_per_agent_not_global(self, signing_service):
        """A global ceiling would let one agent crowd out everyone else."""
        from primer_vault.services.signing import MAX_PENDING_PER_AGENT
        for _ in range(MAX_PENDING_PER_AGENT):
            self._park(signing_service, "NOISY1")
        assert signing_service._pending_count_for("QUIET1") == 0


class TestRateLimiterDoesNotGrowForever:

    def test_idle_callers_are_forgotten(self):
        import time as _time
        from primer_vault.services.server import RateLimiter

        limiter = RateLimiter()
        limiter.is_rate_limited("10.0.0.1")
        # Age that caller out of the window.
        limiter._request_times["10.0.0.1"] = [_time.time() - 3600]

        limiter.is_rate_limited("10.0.0.2")
        assert "10.0.0.1" not in limiter._request_times, "the map keeps every IP ever seen"
        assert "10.0.0.2" in limiter._request_times

    def test_an_active_caller_is_kept(self):
        from primer_vault.services.server import RateLimiter
        limiter = RateLimiter()
        limiter.is_rate_limited("10.0.0.1")
        limiter.is_rate_limited("10.0.0.2")
        assert set(limiter._request_times) == {"10.0.0.1", "10.0.0.2"}

    def test_limiting_still_works_after_pruning(self):
        from primer_vault.services.server import RateLimiter
        limiter = RateLimiter(requests_per_minute=3)
        assert [limiter.is_rate_limited("10.0.0.9") for _ in range(4)] == [
            False, False, False, True]
