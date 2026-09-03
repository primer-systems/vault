"""
The DeFi lane's policy rules and request model.

Two things here carry most of the weight.

`exposure_limit_usd` is the limit a daily cap cannot express. Getting it wrong
in the permissive direction means an agent accumulates a position no rule ever
looks at; getting the denominator wrong means an agent that obeyed the limit is
instantly over it.

`morpho_curators` is the allowlist. Empty means nothing is permitted, which is
the opposite of how the domain lists in the same file read, so it is asserted
rather than assumed.
"""

import math

import pytest

from primer_vault.models.defi import PositionRequest, PositionQuote, PositionResult
from primer_vault.models.policy import DefiRules, SpendPolicy

VAULT = "0x" + "be" * 20
MARKET = "0x" + "ab" * 32
CURATOR = "0x9023fbd6a08c666491a2d1648737e400cf42d2fb"


def rules(**kw) -> DefiRules:
    kw.setdefault("enabled", True)
    kw.setdefault("morpho_curators", [CURATOR])
    kw.setdefault("max_total_deployed_usd", 500.0)
    return DefiRules(**kw)


# ============================================================
# Exposure — the stock limit
# ============================================================


class TestExposureLimit:

    def test_the_absolute_limit_binds_when_it_is_lower(self):
        r = rules(max_total_deployed_usd=500.0, max_deployed_percent=50.0)

        assert r.exposure_limit_usd(total_usdg=2000.0) == 500.0

    def test_the_percentage_binds_when_it_is_lower(self):
        r = rules(max_total_deployed_usd=500.0, max_deployed_percent=50.0)

        assert r.exposure_limit_usd(total_usdg=400.0) == 200.0

    def test_either_limit_may_be_used_alone(self):
        assert rules(max_total_deployed_usd=250.0,
                     max_deployed_percent=None).exposure_limit_usd(10_000) == 250.0
        assert rules(max_total_deployed_usd=None,
                     max_deployed_percent=10.0).exposure_limit_usd(1_000) == 100.0

    def test_with_neither_limit_set_the_answer_is_zero_not_unlimited(self):
        """`validate` refuses this combination while enabled, so it is only
        reachable on a disabled policy - where everything is refused anyway.
        Zero is the safe answer; infinity would be a cap that permits everything.
        """
        r = DefiRules(enabled=False, max_total_deployed_usd=None,
                      max_deployed_percent=None)

        assert r.exposure_limit_usd(10_000.0) == 0.0

    def test_a_negative_balance_cannot_produce_a_negative_ceiling(self):
        r = rules(max_total_deployed_usd=None, max_deployed_percent=50.0)

        assert r.exposure_limit_usd(total_usdg=-100.0) == 0.0

    def test_holding_no_usdg_leaves_no_room_under_a_percentage_limit(self):
        r = rules(max_total_deployed_usd=None, max_deployed_percent=50.0)

        assert r.exposure_limit_usd(total_usdg=0.0) == 0.0

    def test_a_hundred_percent_is_allowed_and_means_all_of_it(self):
        r = rules(max_total_deployed_usd=None, max_deployed_percent=100.0)

        assert r.exposure_limit_usd(total_usdg=750.0) == 750.0


class TestTheDenominatorContract:
    """The percentage is of USDG held *plus* USDG already deployed.

    This test does not compute the denominator - the service does that, from
    chain - but it pins the arithmetic the service is relying on, because the
    self-defeating version is easy to write and looks reasonable.
    """

    def test_a_deployed_agent_stays_within_a_limit_it_already_obeyed(self):
        r = rules(max_total_deployed_usd=None, max_deployed_percent=50.0)
        # $1,000 to start, half of it deployed. Liquid is now $500, deployed $500.
        liquid, deployed = 500.0, 500.0

        correct = r.exposure_limit_usd(total_usdg=liquid + deployed)
        self_defeating = r.exposure_limit_usd(total_usdg=liquid)

        assert deployed <= correct, "an agent obeying the limit was put over it"
        assert deployed > self_defeating, (
            "liquid-only would have been fine here, so this test proves nothing")


# ============================================================
# Curators — the allowlist
# ============================================================


class TestTrustedCurators:

    def test_a_named_curator_is_trusted(self):
        assert rules().is_curator_trusted(CURATOR) is True

    def test_the_comparison_ignores_case(self):
        """`curator()` returns a checksummed address and a user pastes whatever
        they copied. A case-sensitive compare would silently trust nothing."""
        assert rules(morpho_curators=[CURATOR.lower()]).is_curator_trusted(
            CURATOR.upper().replace("0X", "0x")) is True

    def test_an_unnamed_curator_is_not_trusted(self):
        assert rules().is_curator_trusted("0x" + "99" * 20) is False

    def test_an_empty_list_trusts_nobody(self):
        """The opposite of `allowed_domains`, where empty means no restriction.
        A deposit names a contract, and defaulting to "any contract" is not a
        default anyone wants."""
        r = DefiRules(enabled=True, morpho_curators=[])

        assert r.is_curator_trusted(CURATOR) is False

    def test_an_empty_curator_string_is_never_trusted(self):
        assert rules().is_curator_trusted("") is False


# ============================================================
# Validation
# ============================================================


class TestValidate:

    def test_a_complete_ruleset_is_usable(self):
        assert rules(max_deployed_percent=50.0).validate() == (True, "")

    def test_enabled_with_no_venue_source_is_refused(self):
        """The wording is protocol-neutral on purpose: a second protocol adds
        its own allowlist beside the curator list, and this message has to stay
        true when it does."""
        ok, reason = DefiRules(enabled=True, max_total_deployed_usd=1.0).validate()

        assert ok is False
        assert "venue" in reason

    def test_a_curator_list_counts_as_a_venue_source(self):
        assert DefiRules(enabled=True, morpho_curators=[CURATOR]).any_venue_source()

    def test_no_allowlist_at_all_is_no_venue_source(self):
        assert DefiRules(enabled=True).any_venue_source() is False

    def test_enabled_with_no_exposure_limit_is_refused(self):
        ok, reason = DefiRules(enabled=True, morpho_curators=[CURATOR]).validate()

        assert ok is False
        assert "exposure" in reason or "percentage" in reason

    def test_a_disabled_ruleset_need_not_be_complete(self):
        """A half-filled policy has to be savable, or it cannot be filled in."""
        assert DefiRules(enabled=False).validate() == (True, "")


class TestFromDict:

    def test_a_round_trip_preserves_every_field(self):
        original = rules(max_deployed_percent=25.0, max_deposit_usd=50.0,
                         max_ops_per_day=7, auto_approve_below_usd=5.0)

        restored = DefiRules.from_dict(original.to_dict())

        assert restored == original

    def test_defaults_apply_to_an_empty_dict(self):
        r = DefiRules.from_dict({})

        assert r.enabled is False
        assert r.morpho_curators == []
        assert r.max_total_deployed_usd is None

    @pytest.mark.parametrize("field,value", [
        ("enabled", "yes"),
        ("morpho_curators", "0xabc"),
        ("max_deposit_usd", -1),
        ("max_total_deployed_usd", -1),
        ("max_deployed_percent", 101),
        ("max_deployed_percent", -1),
        ("max_ops_per_day", -1),
        ("max_ops_per_day", 1.5),
        ("auto_approve_below_usd", -1),
    ])
    def test_bad_values_are_refused(self, field, value):
        with pytest.raises(ValueError):
            DefiRules.from_dict({field: value})

    @pytest.mark.parametrize("field", [
        "max_deposit_usd", "max_total_deployed_usd", "max_deployed_percent",
        "auto_approve_below_usd",
    ])
    def test_a_non_finite_limit_is_refused(self, field):
        """It passes every >= 0 check and then makes each comparison against it
        False, which switches the cap off rather than setting it."""
        with pytest.raises(ValueError):
            DefiRules.from_dict({field: math.inf})
        with pytest.raises(ValueError):
            DefiRules.from_dict({field: math.nan})

    def test_a_boolean_is_not_accepted_as_a_number(self):
        """True == 1 in Python, so a bool sails through an isinstance check for
        int and becomes a limit of one dollar."""
        with pytest.raises(ValueError):
            DefiRules.from_dict({"max_deposit_usd": True})

    def test_an_empty_curator_entry_is_refused(self):
        with pytest.raises(ValueError):
            DefiRules.from_dict({"morpho_curators": ["  "]})

    def test_curator_entries_are_stripped(self):
        r = DefiRules.from_dict({"morpho_curators": [f"  {CURATOR}  "]})

        assert r.morpho_curators == [CURATOR]


# ============================================================
# Wiring into SpendPolicy
# ============================================================


class TestSpendPolicyIntegration:

    def test_a_policy_carries_defi_rules_through_a_round_trip(self):
        policy = SpendPolicy.create("p", [4663], 1_000_000, defi_rules=rules())

        restored = SpendPolicy.from_dict(policy.to_dict())

        assert restored.defi_rules == policy.defi_rules
        assert restored.is_defi_enabled() is True

    def test_a_policy_written_before_this_lane_existed_still_loads(self):
        """Every policy on every existing install is one of these."""
        policy = SpendPolicy.create("p", [4663], 1_000_000)
        without = {k: v for k, v in policy.to_dict().items() if k != "defi_rules"}

        restored = SpendPolicy.from_dict(without)

        assert restored.defi_rules is None
        assert restored.is_defi_enabled() is False

    def test_defi_defaults_to_off_rather_than_on(self):
        assert SpendPolicy.create("p", [4663], 1).is_defi_enabled() is False

    def test_a_disabled_ruleset_reads_as_disabled(self):
        policy = SpendPolicy.create("p", [4663], 1,
                                    defi_rules=DefiRules(enabled=False))

        assert policy.is_defi_enabled() is False
        assert policy.format_defi_status() == "Disabled"

    def test_adding_defi_does_not_disturb_the_other_two_lanes(self):
        from primer_vault.models.policy import TradingRules
        policy = SpendPolicy.create(
            "p", [4663], 1_000_000, trading_rules=TradingRules(enabled=True),
            x402_enabled=True, defi_rules=rules())

        restored = SpendPolicy.from_dict(policy.to_dict())

        assert restored.is_trading_enabled() is True
        assert restored.is_x402_enabled() is True
        assert restored.is_defi_enabled() is True


class TestDisplayStrings:

    def test_both_halves_of_the_exposure_limit_are_shown(self):
        text = rules(max_total_deployed_usd=500.0,
                     max_deployed_percent=50.0).format_exposure_limit()

        assert "$500.00" in text and "50%" in text and "lower" in text

    def test_a_single_limit_is_shown_without_the_comparison(self):
        text = rules(max_deployed_percent=None).format_exposure_limit()

        assert "lower" not in text

    def test_no_limits_shows_a_dash_rather_than_an_empty_string(self):
        assert DefiRules(enabled=False).format_exposure_limit() == "—"

    def test_no_curators_says_so_plainly(self):
        assert "no venues" in DefiRules().format_curators().lower()

    def test_manual_approval_is_named_not_left_blank(self):
        assert rules(auto_approve_below_usd=None).format_auto_approve() == "Manual only"


# ============================================================
# PositionRequest
# ============================================================


def a_request(**kw) -> PositionRequest:
    kw.setdefault("agent_id", "ag")
    kw.setdefault("action", "supply")
    kw.setdefault("venue", VAULT)
    kw.setdefault("venue_kind", "vault")
    kw.setdefault("amount", "10")
    return PositionRequest.create(**kw)


class TestRequestShape:

    def test_a_vault_supply_is_valid(self):
        assert a_request().validate_shape() == (True, "")

    def test_a_market_withdrawal_is_valid(self):
        assert a_request(action="withdraw", venue=MARKET,
                         venue_kind="market").validate_shape() == (True, "")

    def test_a_full_exit_needs_no_amount(self):
        ok, _ = a_request(action="withdraw", amount="0",
                          withdraw_all=True).validate_shape()

        assert ok is True

    def test_a_full_exit_makes_no_sense_for_a_supply(self):
        ok, reason = a_request(action="supply", withdraw_all=True).validate_shape()

        assert ok is False
        assert "withdrawal" in reason

    def test_an_unknown_action_is_refused(self):
        ok, reason = a_request(action="borrow").validate_shape()

        assert ok is False
        assert "action" in reason

    def test_a_vault_address_is_not_a_market_id(self):
        """40 hex and 64 hex are distinguishable, but a request that names the
        wrong kind must be refused rather than guessed at."""
        ok, _ = a_request(venue=VAULT, venue_kind="market").validate_shape()
        assert ok is False

        ok, _ = a_request(venue=MARKET, venue_kind="vault").validate_shape()
        assert ok is False

    def test_an_unknown_protocol_is_refused(self):
        """A venue id means nothing without knowing whose it is."""
        ok, reason = a_request(protocol="aave").validate_shape()

        assert ok is False
        assert "protocol" in reason

    def test_omitting_the_protocol_means_the_only_one_there_is(self):
        """An agent written today names no protocol. When a second arrives, an
        unqualified request must keep meaning what it always meant."""
        request = PositionRequest.from_dict(
            {"action": "supply", "venue": VAULT, "venue_kind": "vault",
             "amount": "1"})

        assert request.protocol == "morpho"
        assert request.validate_shape() == (True, "")

    def test_an_unknown_venue_kind_is_refused(self):
        ok, reason = a_request(venue_kind="pool").validate_shape()

        assert ok is False
        assert "venue_kind" in reason

    @pytest.mark.parametrize("amount", ["inf", "-inf", "nan", "0", "-1", "abc", ""])
    def test_an_unusable_amount_is_refused(self, amount):
        ok, _ = a_request(amount=amount).validate_shape()

        assert ok is False, f"{amount!r} was accepted"

    def test_whitespace_in_the_amount_is_refused(self):
        """Decimal() ignores surrounding whitespace, and the padding then prints
        verbatim into the approval dialog, pushing the terms off screen."""
        ok, reason = a_request(amount="10\n\n\n").validate_shape()

        assert ok is False
        assert "whitespace" in reason

    def test_an_implausibly_large_amount_is_refused(self):
        ok, _ = a_request(amount="1e31").validate_shape()

        assert ok is False


class TestRequestFromDict:

    def test_the_body_cannot_name_the_agent_or_the_address(self):
        """Both are facts the service knows from authenticating the caller.
        Reading them from the body is what let an agent act as someone else."""
        request = PositionRequest.from_dict({
            "action": "supply", "venue": VAULT, "venue_kind": "vault",
            "amount": "1", "agent_id": "somebody-else",
            "wallet_address": "0x" + "11" * 20,
        })

        assert request.agent_id == ""
        assert request.wallet_address is None

    def test_a_missing_amount_is_named_rather_than_defaulted(self):
        """Defaulting to zero turns a forgotten field into a request for
        nothing, which then reads as success."""
        with pytest.raises(ValueError, match="amount"):
            PositionRequest.from_dict(
                {"action": "supply", "venue": VAULT, "venue_kind": "vault"})

    def test_a_full_exit_may_omit_the_amount(self):
        request = PositionRequest.from_dict({
            "action": "withdraw", "venue": VAULT, "venue_kind": "vault",
            "withdraw_all": True})

        assert request.validate_shape() == (True, "")

    def test_missing_fields_are_named(self):
        with pytest.raises(ValueError, match="venue_kind"):
            PositionRequest.from_dict({"action": "supply", "venue": VAULT})

    def test_a_non_boolean_withdraw_all_is_refused(self):
        with pytest.raises(ValueError):
            PositionRequest.from_dict({
                "action": "withdraw", "venue": VAULT, "venue_kind": "vault",
                "withdraw_all": "yes"})


class TestResult:

    def test_a_rejection_carries_a_code_to_branch_on(self):
        result = PositionResult.rejected("r1", "nope", code="VENUE_NOT_PERMITTED")

        assert result.to_dict()["code"] == "VENUE_NOT_PERMITTED"
        assert result.status == "rejected"

    def test_a_failure_says_whether_retrying_is_sensible(self):
        """A venue that cannot free the amount today may manage tomorrow.
        Asking for more than the position holds will never work. Without the
        flag a caller cannot tell them apart, and one of them loops forever."""
        transient = PositionResult.failed("r1", "no liquidity", retryable=True)
        permanent = PositionResult.failed("r2", "more than you hold")

        assert transient.retryable is True
        assert permanent.retryable is False

    def test_a_quote_serialises_inside_a_result(self):
        quote = PositionQuote(venue=VAULT, venue_kind="vault", protocol="morpho",
                              action="supply",
                              asset="0x" + "cd" * 20, asset_decimals=6,
                              share_decimals=18, assets=1_000_000,
                              shares=994_938_507_073_380_375)

        as_dict = PositionResult.pending("r1", quote).to_dict()

        assert as_dict["quote"]["assets"] == 1_000_000
        assert as_dict["quote"]["shares"] == 994_938_507_073_380_375

    def test_both_denominations_survive_serialisation_as_integers(self):
        """Atomic values are 6dp and 18dp integers. A float anywhere in here
        loses the low digits of the share count silently."""
        quote = PositionQuote(venue=VAULT, venue_kind="vault", protocol="morpho",
                              action="withdraw",
                              asset="0x" + "cd" * 20, asset_decimals=6,
                              share_decimals=18, assets=5_168_957_020_000,
                              shares=5_142_743_110_000_000_000_000_000)

        as_dict = quote.to_dict()

        assert isinstance(as_dict["assets"], int)
        assert isinstance(as_dict["shares"], int)
        assert as_dict["shares"] == 5_142_743_110_000_000_000_000_000
