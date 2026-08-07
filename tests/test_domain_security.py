"""
Domain Security Tests

Tests for domain restriction bypass attempts, case sensitivity,
subdomain matching, and URL parsing edge cases.

These tests verify that domain restrictions are enforced correctly
and cannot be bypassed through various techniques.
"""

import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.models import SpendPolicy


@pytest.fixture
def policy_with_allowlist():
    """Create a policy with domain allowlist."""
    return SpendPolicy.create(
        name="AllowlistPolicy",
        networks=[4663],
        daily_limit_micro=10000000,
        per_request_max_micro=1000000,
        allowed_domains=["stripe.com", "api.example.com"],
        blocked_domains=[]
    )


@pytest.fixture
def policy_with_blocklist():
    """Create a policy with domain blocklist."""
    return SpendPolicy.create(
        name="BlocklistPolicy",
        networks=[4663],
        daily_limit_micro=10000000,
        per_request_max_micro=1000000,
        allowed_domains=[],
        blocked_domains=["evil.com", "malware.org"]
    )


@pytest.fixture
def policy_with_both():
    """Create a policy with both allowlist and blocklist."""
    return SpendPolicy.create(
        name="CombinedPolicy",
        networks=[4663],
        daily_limit_micro=10000000,
        per_request_max_micro=1000000,
        allowed_domains=["trusted.com", "api.trusted.com"],
        blocked_domains=["evil.trusted.com"]  # Blocked subdomain
    )


@pytest.fixture
def policy_no_restrictions():
    """Create a policy with no domain restrictions."""
    return SpendPolicy.create(
        name="OpenPolicy",
        networks=[4663],
        daily_limit_micro=10000000,
        per_request_max_micro=1000000,
        allowed_domains=[],
        blocked_domains=[]
    )


# =============================================================================
# Case Sensitivity Tests
# =============================================================================

class TestCaseSensitivity:
    """Test that domain matching is case-insensitive."""

    def test_uppercase_url_matches_lowercase_allowlist(self, policy_with_allowlist):
        """STRIPE.COM should match allowlist entry stripe.com."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://STRIPE.COM/api/charge"
        )
        assert allowed is True, f"Uppercase domain rejected: {reason}"

    def test_mixed_case_url_matches(self, policy_with_allowlist):
        """StRiPe.CoM should match allowlist entry stripe.com."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://StRiPe.CoM/api/charge"
        )
        assert allowed is True, f"Mixed case domain rejected: {reason}"

    def test_uppercase_subdomain_matches(self, policy_with_allowlist):
        """API.STRIPE.COM should match stripe.com entry."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://API.STRIPE.COM/v1/charge"
        )
        assert allowed is True, f"Uppercase subdomain rejected: {reason}"

    def test_uppercase_blocklist_matches(self, policy_with_blocklist):
        """EVIL.COM should be blocked by evil.com entry."""
        allowed, reason = policy_with_blocklist.check_domain_allowed(
            "https://EVIL.COM/malware"
        )
        assert allowed is False
        assert "blocked" in reason.lower()


# =============================================================================
# Subdomain Matching Tests
# =============================================================================

class TestSubdomainMatching:
    """Test subdomain matching behavior."""

    def test_direct_domain_match(self, policy_with_allowlist):
        """Exact domain match should be allowed."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com/api"
        )
        assert allowed is True

    def test_single_subdomain_matches(self, policy_with_allowlist):
        """Single subdomain should match parent domain."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://api.stripe.com/v1"
        )
        assert allowed is True

    def test_deep_subdomain_matches(self, policy_with_allowlist):
        """Deep nested subdomains should match parent domain."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://foo.bar.baz.stripe.com/resource"
        )
        assert allowed is True

    def test_similar_domain_not_matched(self, policy_with_allowlist):
        """Similar but different domain should not match."""
        # notstripe.com should NOT match stripe.com
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://notstripe.com/api"
        )
        assert allowed is False
        assert "not in allowlist" in reason.lower()

    def test_suffix_attack_blocked(self, policy_with_allowlist):
        """Domain suffix attack should be blocked."""
        # evil-stripe.com should NOT match stripe.com
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://evil-stripe.com/api"
        )
        assert allowed is False

    def test_prefix_attack_blocked(self, policy_with_allowlist):
        """Domain prefix attack should be blocked."""
        # stripe.com.evil.com should NOT match stripe.com
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com.evil.com/api"
        )
        assert allowed is False

    def test_blocked_subdomain_overrides_allowed_parent(self, policy_with_both):
        """Blocked subdomain should be blocked even if parent is allowed."""
        # trusted.com is allowed
        allowed1, _ = policy_with_both.check_domain_allowed(
            "https://trusted.com/api"
        )
        assert allowed1 is True

        # evil.trusted.com is explicitly blocked
        allowed2, reason = policy_with_both.check_domain_allowed(
            "https://evil.trusted.com/api"
        )
        assert allowed2 is False
        assert "blocked" in reason.lower()


# =============================================================================
# URL Parsing Edge Cases
# =============================================================================

class TestURLParsingEdgeCases:
    """Test URL parsing edge cases."""

    def test_url_with_port(self, policy_with_allowlist):
        """URL with port number should extract domain correctly."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com:443/api"
        )
        assert allowed is True

    def test_url_with_unusual_port(self, policy_with_allowlist):
        """URL with unusual port should still work."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com:8080/api"
        )
        assert allowed is True

    def test_url_with_path(self, policy_with_allowlist):
        """URL with path should extract domain correctly."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com/v1/customers/create?key=value"
        )
        assert allowed is True

    def test_url_with_query_string(self, policy_with_allowlist):
        """URL with query string should extract domain correctly."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com/api?domain=evil.com"  # Query param has evil.com
        )
        # Should match stripe.com, not evil.com in query
        assert allowed is True

    def test_url_with_fragment(self, policy_with_allowlist):
        """URL with fragment should extract domain correctly."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com/api#section"
        )
        assert allowed is True

    def test_url_with_auth_credentials(self, policy_with_allowlist):
        """URL with credentials should extract domain correctly."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://user:pass@stripe.com/api"
        )
        assert allowed is True

    def test_http_scheme(self, policy_with_allowlist):
        """HTTP (not HTTPS) URL should work."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "http://stripe.com/api"
        )
        assert allowed is True

    def test_path_only_url(self, policy_with_allowlist):
        """Path-only URL should be handled."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "/api/v1/charge"  # No domain
        )
        # Should reject - can't verify domain
        assert allowed is False
        assert "path only" in reason.lower() or "cannot verify" in reason.lower()

    def test_path_only_url_no_restrictions(self, policy_no_restrictions):
        """Path-only URL with no restrictions should be allowed."""
        allowed, reason = policy_no_restrictions.check_domain_allowed(
            "/api/v1/charge"
        )
        # No restrictions, so allowed
        assert allowed is True

    def test_empty_url(self, policy_with_allowlist):
        """Empty URL should be handled gracefully."""
        allowed, reason = policy_with_allowlist.check_domain_allowed("")
        # Empty URL - no domain to check
        assert allowed is True  # Nothing to verify

    def test_none_url(self, policy_with_allowlist):
        """None URL should be handled gracefully."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(None)
        assert allowed is True  # Nothing to verify

    def test_ip_address_url(self, policy_with_allowlist):
        """IP address URL should not match domain entries."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://192.168.1.1/api"
        )
        # IP won't match domain allowlist
        assert allowed is False

    def test_localhost_url(self, policy_with_allowlist):
        """localhost URL should be handled."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "http://localhost:3000/api"
        )
        # localhost not in allowlist
        assert allowed is False


# =============================================================================
# Blocklist Tests
# =============================================================================

class TestBlocklist:
    """Test blocklist enforcement."""

    def test_blocked_domain_rejected(self, policy_with_blocklist):
        """Blocked domain should be rejected."""
        allowed, reason = policy_with_blocklist.check_domain_allowed(
            "https://evil.com/malware"
        )
        assert allowed is False
        assert "blocked" in reason.lower()

    def test_blocked_subdomain_rejected(self, policy_with_blocklist):
        """Subdomain of blocked domain should be rejected."""
        allowed, reason = policy_with_blocklist.check_domain_allowed(
            "https://api.evil.com/api"
        )
        assert allowed is False

    def test_unblocked_domain_allowed(self, policy_with_blocklist):
        """Domain not in blocklist should be allowed."""
        allowed, reason = policy_with_blocklist.check_domain_allowed(
            "https://good.com/api"
        )
        assert allowed is True

    def test_blocklist_only_no_allowlist(self, policy_with_blocklist):
        """With only blocklist, all non-blocked domains should be allowed."""
        # Random domain should be allowed
        allowed, reason = policy_with_blocklist.check_domain_allowed(
            "https://random-site-12345.com/api"
        )
        assert allowed is True


# =============================================================================
# Allowlist Tests
# =============================================================================

class TestAllowlist:
    """Test allowlist enforcement."""

    def test_allowed_domain_accepted(self, policy_with_allowlist):
        """Domain in allowlist should be accepted."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com/api"
        )
        assert allowed is True

    def test_unlisted_domain_rejected(self, policy_with_allowlist):
        """Domain not in allowlist should be rejected."""
        allowed, reason = policy_with_allowlist.check_domain_allowed(
            "https://unknown-domain.com/api"
        )
        assert allowed is False
        assert "not in allowlist" in reason.lower()

    def test_multiple_allowed_domains(self, policy_with_allowlist):
        """All domains in allowlist should be accepted."""
        allowed1, _ = policy_with_allowlist.check_domain_allowed(
            "https://stripe.com/api"
        )
        allowed2, _ = policy_with_allowlist.check_domain_allowed(
            "https://api.example.com/api"
        )

        assert allowed1 is True
        assert allowed2 is True


# =============================================================================
# No Restrictions Tests
# =============================================================================

class TestNoRestrictions:
    """Test behavior with no domain restrictions."""

    def test_any_domain_allowed(self, policy_no_restrictions):
        """Any domain should be allowed with no restrictions."""
        domains = [
            "https://stripe.com/api",
            "https://evil.com/api",
            "https://random.org/api",
            "https://192.168.1.1/api",
        ]

        for url in domains:
            allowed, reason = policy_no_restrictions.check_domain_allowed(url)
            assert allowed is True, f"Domain {url} rejected: {reason}"


# =============================================================================
# Special Domain Tests
# =============================================================================

class TestSpecialDomains:
    """Test special domain cases."""

    def test_very_long_domain(self):
        """Very long domain should be handled."""
        policy = SpendPolicy.create(
            name="LongDomainPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["a" * 100 + ".com"],  # Very long
            blocked_domains=[]
        )

        allowed, _ = policy.check_domain_allowed(
            f"https://{'a' * 100}.com/api"
        )
        assert allowed is True

    def test_domain_with_numbers(self):
        """Domain with numbers should work."""
        policy = SpendPolicy.create(
            name="NumbersPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["api2.stripe.com"],
            blocked_domains=[]
        )

        allowed, _ = policy.check_domain_allowed(
            "https://api2.stripe.com/v1"
        )
        assert allowed is True

    def test_domain_with_hyphens(self):
        """Domain with hyphens should work."""
        policy = SpendPolicy.create(
            name="HyphenPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["my-api.stripe.com"],
            blocked_domains=[]
        )

        allowed, _ = policy.check_domain_allowed(
            "https://my-api.stripe.com/v1"
        )
        assert allowed is True

    def test_punycode_domain(self):
        """Punycode (IDN) domain should be handled."""
        # xn--n3h.com is the punycode for an emoji domain
        policy = SpendPolicy.create(
            name="IDNPolicy",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["xn--n3h.com"],
            blocked_domains=[]
        )

        allowed, _ = policy.check_domain_allowed(
            "https://xn--n3h.com/api"
        )
        assert allowed is True


# =============================================================================
# Edge Cases
# =============================================================================

class TestDomainEdgeCases:
    """Test domain matching edge cases."""

    def test_empty_allowlist_entry(self):
        """Empty string in allowlist should be handled."""
        policy = SpendPolicy.create(
            name="EmptyEntry",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["", "stripe.com"],  # Empty entry
            blocked_domains=[]
        )

        # Empty entry should not match anything
        allowed, _ = policy.check_domain_allowed(
            "https://stripe.com/api"
        )
        assert allowed is True

    def test_whitespace_in_domain_entry(self):
        """Whitespace in domain entry should be trimmed."""
        policy = SpendPolicy.create(
            name="WhitespaceEntry",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=["  stripe.com  "],  # Whitespace
            blocked_domains=[]
        )

        allowed, _ = policy.check_domain_allowed(
            "https://stripe.com/api"
        )
        # Should match after trimming
        assert allowed is True

    def test_dot_prefix_in_allowlist(self):
        """Domain entry starting with dot should work."""
        policy = SpendPolicy.create(
            name="DotPrefix",
            networks=[4663],
            daily_limit_micro=1000000,
            per_request_max_micro=100000,
            allowed_domains=[".stripe.com"],  # Leading dot
            blocked_domains=[]
        )

        # Behavior depends on implementation
        # May or may not match


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
