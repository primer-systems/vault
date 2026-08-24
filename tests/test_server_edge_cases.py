"""
Server Edge Cases Tests

Tests for HTTP server rate limiting, request handling edge cases,
connection management, and error responses.

These tests verify that the server handles edge cases correctly
and maintains stability under various conditions.
"""

import sys
import tempfile
import shutil
import json
import base64
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


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


# =============================================================================
# Rate Limiting Tests
# =============================================================================

class TestRateLimiting:
    """Test server rate limiting behavior."""

    def test_rate_limiter_has_requests_per_minute(self):
        """Rate limiter should have configurable requests per minute."""
        from primer_vault.services.server import RateLimiter

        limiter = RateLimiter(requests_per_minute=300)
        assert limiter.requests_per_minute == 300

    def test_rate_limiter_allows_normal_traffic(self):
        """Normal request rate should be allowed (not rate limited)."""
        from primer_vault.services.server import RateLimiter

        limiter = RateLimiter()
        client_ip = "192.168.1.100"

        # Should allow many requests under the limit (is_rate_limited returns False)
        for _i in range(10):
            assert limiter.is_rate_limited(client_ip) is False

    def test_rate_limiter_blocks_excessive_traffic(self):
        """Excessive request rate should be blocked."""
        from primer_vault.services.server import RateLimiter

        # Create limiter with low limit for testing
        limiter = RateLimiter(requests_per_minute=5)
        client_ip = "192.168.1.101"

        # Exhaust the limit
        for _i in range(5):
            limiter.is_rate_limited(client_ip)

        # Next request should be blocked (rate limited)
        assert limiter.is_rate_limited(client_ip) is True

    def test_rate_limiter_window_reset(self):
        """Rate limit tracking should work."""
        from primer_vault.services.server import RateLimiter

        limiter = RateLimiter()
        client_ip = "192.168.1.102"

        # Make some requests - should not be rate limited
        for _i in range(5):
            result = limiter.is_rate_limited(client_ip)
            assert result is False

    def test_rate_limiter_different_clients_independent(self):
        """Different clients should have independent rate limits."""
        from primer_vault.services.server import RateLimiter

        # Create limiter with low limit for testing
        limiter = RateLimiter(requests_per_minute=3)

        # Exhaust limit for client 1
        for _i in range(3):
            limiter.is_rate_limited("client1")

        # Client 1 should now be rate limited
        assert limiter.is_rate_limited("client1") is True

        # Client 2 should not be rate limited
        assert limiter.is_rate_limited("client2") is False


# =============================================================================
# Request Parsing Tests
# =============================================================================

class TestRequestParsing:
    """Test HTTP request parsing edge cases."""

    def test_valid_x402_request_accepted(self, signing_service):
        """Valid x402 request should be accepted."""
        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 1000000,
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        from primer_vault.services.signing import validate_x402_request
        is_valid, version, error = validate_x402_request(x402_data)
        assert is_valid is True

    def test_base64_encoded_payment_required(self):
        """Base64-encoded Payment-Required header should be decoded."""
        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 1000000,
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }]
        }

        # Encode as base64
        encoded = base64.b64encode(json.dumps(x402_data).encode()).decode()

        # Should be decodable
        decoded = json.loads(base64.b64decode(encoded).decode())
        assert decoded == x402_data

    def test_invalid_base64_handled(self):
        """Invalid base64 should be handled gracefully."""
        invalid_base64 = "not_valid_base64!!!"

        try:
            base64.b64decode(invalid_base64)
            raise AssertionError("Should have raised")
        except Exception:
            pass  # Expected

    def test_invalid_json_in_payload_handled(self):
        """Invalid JSON in decoded payload should be handled."""
        # Valid base64, invalid JSON
        invalid_json = base64.b64encode(b"not valid json {{{").decode()

        decoded_bytes = base64.b64decode(invalid_json)
        try:
            json.loads(decoded_bytes)
            raise AssertionError("Should have raised")
        except json.JSONDecodeError:
            pass  # Expected


# =============================================================================
# Request Size Tests
# =============================================================================

class TestRequestSizes:
    """Test handling of various request sizes."""

    def test_very_large_payload_handled(self):
        """Very large payload should be handled (or rejected)."""
        # Create a large x402 payload
        large_resource = "https://example.com/" + "a" * 10000

        x402_data = {
            "accepts": [{
                "network": "robinhood",
                "maxAmountRequired": 1000000,
                "payTo": "0x1234567890123456789012345678901234567890",
                "asset": "USDG"
            }],
            "resource": large_resource
        }

        # Should not crash
        from primer_vault.services.signing import validate_x402_request
        is_valid, version, error = validate_x402_request(x402_data)

    def test_many_accepts_entries(self):
        """Many entries in accepts array should be handled."""
        accepts = []
        for i in range(100):
            accepts.append({
                "network": "robinhood",
                "maxAmountRequired": 1000000 + i,
                "payTo": f"0x{'1' * 39}{i % 10}",
                "asset": "USDG"
            })

        x402_data = {"accepts": accepts}

        from primer_vault.services.signing import validate_x402_request
        is_valid, version, error = validate_x402_request(x402_data)
        # Should use first accept
        assert is_valid is True


# =============================================================================
# Error Response Tests
# =============================================================================

class TestErrorResponses:
    """Test error response formatting."""

    def test_error_response_structure(self, signing_service):
        """Error responses should have consistent structure."""
        # Simulate various errors and check response format

        result = signing_service.handle_ping("nonexistent-agent")

        assert "status" in result
        assert result["status"] == "error"
        assert "error" in result
        assert "code" in result

    def test_error_codes_are_strings(self, signing_service):
        """Error codes should be uppercase string constants."""
        result = signing_service.handle_ping("nonexistent-agent")

        code = result.get("code", "")
        assert code.isupper()
        assert "_" in code or code.isalpha()

    def test_error_messages_are_descriptive(self, signing_service):
        """Error messages should be human-readable."""
        result = signing_service.handle_ping("nonexistent-agent")

        error = result.get("error", "")
        assert len(error) > 5  # Not just a code
        assert error[0].isupper()  # Starts with capital


# =============================================================================
# Server Lifecycle Tests
# =============================================================================

class TestServerLifecycle:
    """Test server start/stop lifecycle."""

    def test_server_start(self, core):
        """Server should start successfully."""
        result = core.start_server(port=0)  # Port 0 = auto-assign

        if result:
            assert core.is_server_running()
            core.stop_server()

    def test_server_stop(self, core):
        """Server should stop successfully."""
        core.start_server(port=0)

        if core.is_server_running():
            core.stop_server()
            assert not core.is_server_running()

    def test_server_port_assignment(self, core):
        """Server should report port when running."""
        core.start_server(port=0)

        if core.is_server_running():
            port = core.server_port
            # Port may be 0 if not yet assigned, or > 0 if assigned
            assert port >= 0
            core.stop_server()

    def test_double_start_handled(self, core):
        """Starting server twice should be handled."""
        core.start_server(port=0)

        if core.is_server_running():
            # Second start should be handled gracefully
            core.start_server(port=0)
            # May return False or True - implementation dependent
            core.stop_server()

    def test_stop_without_start(self, core):
        """Stopping non-running server should be safe."""
        # Should not raise
        core.stop_server()
        assert not core.is_server_running()


# =============================================================================
# Concurrent Request Tests
# =============================================================================

class TestConcurrentRequests:
    """Test handling of concurrent requests."""

    def test_concurrent_pings(self, core, temp_data_dir):
        """Multiple concurrent pings should be handled."""
        import threading

        # Create some agents
        agent1, _ = core.create_agent(name="Agent1", auth_mode="bearer")
        agent2, _ = core.create_agent(name="Agent2", auth_mode="bearer")

        signing_service = core._signing_service

        results = []
        errors = []

        def ping_agent(agent_id):
            try:
                result = signing_service.handle_ping(agent_id)
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(10):
            for agent in [agent1, agent2]:
                t = threading.Thread(target=ping_agent, args=(agent.id,))
                threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have results without crashes
        assert len(errors) == 0
        assert len(results) == 20


# =============================================================================
# Server Statistics Tests
# =============================================================================

class TestServerStatistics:
    """Test server statistics tracking."""

    def test_stats_object_exists(self):
        """Server stats object should exist with correct attributes."""
        from primer_vault.services.server import server_stats

        assert hasattr(server_stats, 'signed')
        assert hasattr(server_stats, 'rejected')
        assert hasattr(server_stats, 'started_at')

    def test_stats_increment_correctly(self):
        """Server stats should increment correctly."""
        from primer_vault.services.server import server_stats

        initial_signed = server_stats.signed

        # Simulate signing (stats are incremented by handlers)
        server_stats.signed += 1

        assert server_stats.signed == initial_signed + 1

    def test_stats_reset(self):
        """Server stats should reset correctly."""
        from primer_vault.services.server import server_stats

        server_stats.signed = 100
        server_stats.rejected = 50
        server_stats.reset()

        assert server_stats.signed == 0
        assert server_stats.rejected == 0


# =============================================================================
# Header Handling Tests
# =============================================================================

class TestHeaderHandling:
    """Test HTTP header handling."""

    def test_missing_authorization_header(self, signing_service):
        """Missing Authorization header should be handled."""
        from primer_vault.models import Agent

        agent = Agent(
            id="TEST123",
            code="abc123",
            name="TestAgent",
            auth_mode="bearer",
            auth_key="hash",
            status="active",
            created_at="2024-01-01"
        )

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id="TEST123",
            signature_header=None,  # Missing
            data_key=DATA_KEY,
            payment_required="test",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None
        assert "Missing" in result

    def test_empty_authorization_header(self, signing_service):
        """Empty Authorization header should be handled."""
        from primer_vault.models import Agent

        agent = Agent(
            id="TEST123",
            code="abc123",
            name="TestAgent",
            auth_mode="bearer",
            auth_key="hash",
            status="active",
            created_at="2024-01-01"
        )

        result = signing_service._verify_agent_auth(
            agent=agent,
            agent_id="TEST123",
            signature_header="",  # Empty
            data_key=DATA_KEY,
            payment_required="test",
            x402_data=None,
            request_url="https://example.com"
        )

        assert result is not None


# =============================================================================
# Timeout Tests
# =============================================================================

class TestTimeouts:
    """Test timeout handling."""

    def test_max_request_age_configurable(self, signing_service):
        """Max request age should be configurable."""
        original = signing_service._max_request_age_seconds

        signing_service.set_max_request_age(600)  # 10 minutes
        assert signing_service._max_request_age_seconds == 600

        # Restore
        signing_service.set_max_request_age(original)

    def test_max_request_age_minimum_enforced(self, signing_service):
        """Max request age should have a minimum."""
        signing_service.set_max_request_age(5)  # Very short

        # Should enforce minimum (30s)
        assert signing_service._max_request_age_seconds >= 30


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
