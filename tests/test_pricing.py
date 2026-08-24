"""The valuation reference: what a price has to be before a limit is checked
against it.

USD limits are the only thing standing between an agent and the whole wallet, and
a trade with an ETH leg cannot be checked against one until it has been valued.
So the question this file asks is not "is the price right" - Vault cannot know
that - but "is it a number a limit can be compared against at all".

That distinction matters because of one Python detail: json.loads accepts NaN and
Infinity as an extension to the JSON spec, float() takes both, and every
comparison against NaN is false. A NaN price passes a "greater than zero" test,
then passes every limit check downstream, and is added to the daily volume total
and saved. From then on the agent's total is unreadable and the daily limit
answers "not exceeded" to everything. A limit that always says no is not a limit.

Everything else - an unreachable host, a rate-limit page, a missing key - already
fails and is handled. Those are covered here too, because the value of the finite
check is that it closes the last gap in a set that was otherwise complete.
"""

import json
import sys
from pathlib import Path

import pytest
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.services import pricing
from primer_vault.services.pricing import PricingError, get_eth_usd


@pytest.fixture(autouse=True)
def clear_cache():
    """Each test starts with no cached price, so none of them leak into another."""
    pricing._cache.clear()
    yield
    pricing._cache.clear()


def serving(body: str):
    """A stand-in for the reference endpoint returning `body` verbatim."""
    class Response:
        def read(self):
            return body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda req, timeout=None: Response()


def price_from(body: str, monkeypatch):
    monkeypatch.setattr(pricing, "urlopen", serving(body))
    return get_eth_usd()


class TestUsablePrices:

    def test_a_normal_price_is_returned(self, monkeypatch):
        assert price_from('{"ethereum": {"usd": 3421.55}}', monkeypatch) == 3421.55

    def test_an_integer_price_is_returned(self, monkeypatch):
        assert price_from('{"ethereum": {"usd": 3000}}', monkeypatch) == 3000.0

    def test_the_price_is_cached_rather_than_refetched(self, monkeypatch):
        calls = []

        def counting(req, timeout=None):
            calls.append(1)
            return serving('{"ethereum": {"usd": 3000}}')(req)

        monkeypatch.setattr(pricing, "urlopen", counting)
        assert get_eth_usd() == 3000.0
        assert get_eth_usd() == 3000.0
        assert len(calls) == 1, "the second call should have come from the cache"


class TestPricesALimitCannotUse:
    """Each of these would otherwise be compared against a spending limit."""

    @pytest.mark.parametrize("literal", ["NaN", "-NaN"])
    def test_nan_is_refused(self, literal, monkeypatch):
        """NaN is greater than nothing and less than nothing, so it passes a check
        for a positive price and then fails to exceed any limit it is compared
        against.
        """
        with pytest.raises(PricingError):
            price_from('{"ethereum": {"usd": %s}}' % literal, monkeypatch)

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity"])
    def test_infinity_is_refused(self, literal, monkeypatch):
        with pytest.raises(PricingError):
            price_from('{"ethereum": {"usd": %s}}' % literal, monkeypatch)

    @pytest.mark.parametrize("literal", ["0", "-1", "-3000.5"])
    def test_a_non_positive_price_is_refused(self, literal, monkeypatch):
        with pytest.raises(PricingError):
            price_from('{"ethereum": {"usd": %s}}' % literal, monkeypatch)

    def test_nan_would_have_passed_the_old_check(self):
        """Why the check is `isfinite` and not a wider band.

        Held here rather than in a comment because it is the whole reason the
        line exists, and a future simplification back to `> 0` should fail.
        """
        nan = float(json.loads('{"v": NaN}')["v"])
        assert not (nan <= 0), "a bare positivity check does not catch NaN"
        assert not (nan > 1000), "and nothing NaN is compared against exceeds it"
        assert not (nan < 1000), "in either direction"


class TestUnusableResponses:
    """Malformed replies were already handled. They stay handled."""

    @pytest.mark.parametrize("body", [
        '{}',                                   # empty object
        '{"ethereum": {}}',                     # no usd key
        '{"error": "rate limited"}',            # the endpoint's own error shape
        '<html>maintenance</html>',             # not JSON at all
        '{"ethereum": {"usd": "3000"}}[',       # truncated / trailing junk
    ])
    def test_a_malformed_reply_raises(self, body, monkeypatch):
        with pytest.raises(PricingError):
            price_from(body, monkeypatch)

    @pytest.mark.parametrize("failure", [
        URLError("connection refused"),   # how urlopen reports a failed connection
        TimeoutError("timed out"),        # how a read timeout arrives - not a URLError
        ConnectionResetError("reset"),
    ])
    def test_an_unreachable_reference_raises_when_nothing_is_cached(self, failure,
                                                                    monkeypatch):
        """Every one of these must arrive as PricingError.

        The trading service catches PricingError and nothing else, so anything
        that escapes with its own type escapes the valuation step entirely - and
        a trade that should have been escalated for pricing fails instead.
        """
        def fail(req, timeout=None):
            raise failure

        monkeypatch.setattr(pricing, "urlopen", fail)
        with pytest.raises(PricingError):
            get_eth_usd()


class TestStaleFallback:
    """An unreachable reference is not the same as a bad one."""

    def test_a_recent_cached_price_is_served_when_the_reference_is_down(self, monkeypatch):
        monkeypatch.setattr(pricing, "urlopen", serving('{"ethereum": {"usd": 3000}}'))
        assert get_eth_usd() == 3000.0

        # Age the entry past its TTL but well inside the staleness bound.
        value, fetched_at = pricing._cache["eth_usd"]
        pricing._cache["eth_usd"] = (value, fetched_at - pricing._CACHE_TTL_SECONDS - 5)

        def refuse(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(pricing, "urlopen", refuse)
        assert get_eth_usd() == 3000.0

    def test_a_price_past_the_staleness_bound_is_not_served(self, monkeypatch):
        monkeypatch.setattr(pricing, "urlopen", serving('{"ethereum": {"usd": 3000}}'))
        get_eth_usd()

        value, fetched_at = pricing._cache["eth_usd"]
        pricing._cache["eth_usd"] = (value, fetched_at - pricing._STALE_MAX_SECONDS - 5)

        def refuse(req, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(pricing, "urlopen", refuse)
        with pytest.raises(PricingError):
            get_eth_usd()

    def test_a_bad_price_is_not_papered_over_by_a_cached_one(self, monkeypatch):
        """A reference that answers with nonsense is a different failure from one
        that does not answer, and must not be masked by a good earlier price."""
        monkeypatch.setattr(pricing, "urlopen", serving('{"ethereum": {"usd": 3000}}'))
        assert get_eth_usd() == 3000.0
        pricing._cache["eth_usd"] = (3000.0, pricing._cache["eth_usd"][1]
                                     - pricing._CACHE_TTL_SECONDS - 5)

        monkeypatch.setattr(pricing, "urlopen", serving('{"ethereum": {"usd": NaN}}'))
        with pytest.raises(PricingError):
            get_eth_usd()
