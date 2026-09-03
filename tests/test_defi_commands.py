"""
The Morpho lane from a prompt: `position`, `venues`, and the policy flags.

`commands/` is shared, so these are the terminal edition's only way to answer a
queued supply — it has no approval dialog. A person running Vault unattended
sees the request in the live feed and answers it here or not at all.

The prefix matching is the part that matters. `position approve` executes with
no second step, so a short or empty prefix must refuse rather than pick.
"""

from types import SimpleNamespace

import pytest

from primer_vault.commands.position import PositionCommands, VenuesCommands
from primer_vault.models.defi import PositionQuote, PositionRequest
from primer_vault.models.policy import DefiRules, SpendPolicy
from primer_vault.services.morpho import MarketVenue, VaultVenue

USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
VAULT = "0xBeEff033F34C046626B8D0A041844C5d1A5409dd"
MARKET = "0x" + "c8" * 32
CURATOR = "0x9023fbd6a08c666491a2d1648737e400cf42d2fb"


def a_quote(**kw) -> PositionQuote:
    base = dict(venue=VAULT, venue_kind="vault", protocol="morpho",
                action="supply", asset=USDG, asset_decimals=6,
                share_decimals=18, assets=25_000_000, shares=24_873_000_000,
                notional_usd=25.0, current_position_assets=10_000_000,
                venue_withdrawable=44_926_051_000_000,
                asset_symbol="USDG", venue_name="Steakhouse USDG",
                approvals_needed=1)
    base.update(kw)
    return PositionQuote(**base)


def a_pair(request_id="abc12345-0000-0000-0000-000000000000", **quote_kw):
    request = PositionRequest.create(
        agent_id="AG1", action=quote_kw.get("action", "supply"),
        venue=VAULT, venue_kind="vault", amount="25")
    request.id = request_id
    return request, a_quote(**quote_kw)


class FakeCore:
    def __init__(self, pending=None, approve=None, policies=None, venues=None):
        self._pending = pending or []
        self._approve = approve or {"status": "executed", "tx_hash": "0xdead"}
        self._policies = policies or []
        self._venues = venues or []
        self.rejected = []
        self.approved = []

    def get_pending_positions(self):
        return self._pending

    def approve_position(self, request_id):
        self.approved.append(request_id)
        return self._approve

    def reject_position(self, request_id, reason="x"):
        self.rejected.append((request_id, reason))
        return {"status": "rejected"}

    def get_all_policies(self):
        return self._policies

    def get_defi_venues(self, curators):
        return self._venues


def cmd(core) -> PositionCommands:
    return PositionCommands(core, handler=None)


# ============================================================
# position pending
# ============================================================


class TestPending:

    def test_an_empty_queue_says_so(self):
        assert "No pending" in cmd(FakeCore()).pending([]).output

    def test_it_shows_the_amount_the_venue_and_the_value(self):
        output = cmd(FakeCore(pending=[a_pair()])).pending([]).output

        assert "supply 25" in output
        assert "Steakhouse USDG" in output
        assert "~$25.00" in output

    def test_it_shows_the_venue_address_in_full(self):
        """Approving is the one moment a person authorises a destination, and a
        truncated address hides the half people actually compare."""
        output = cmd(FakeCore(pending=[a_pair()])).pending([]).output

        assert VAULT in output

    def test_it_shows_what_is_already_held_there(self):
        output = cmd(FakeCore(pending=[a_pair()])).pending([]).output

        assert "already held: 10" in output

    def test_it_shows_what_the_venue_could_return_now(self):
        """A position is not a balance - exit capacity moves with other
        people's borrowing and is worth seeing before approving a supply."""
        output = cmd(FakeCore(pending=[a_pair()])).pending([]).output

        assert "venue can return now" in output.lower()

    def test_it_says_when_approvals_come_first(self):
        output = cmd(FakeCore(pending=[a_pair()])).pending([]).output

        assert "approval(s) first" in output

    def test_an_unpriced_request_is_flagged_loudly(self):
        """No dollar figure means the limits could not be applied to it, and
        the person approving is entitled to know none of them were checked."""
        output = cmd(FakeCore(pending=[a_pair(notional_usd=None)])).pending([]).output

        assert "WARNING" in output
        assert "could NOT be checked" in output

    def test_a_withdrawal_reads_as_a_withdrawal(self):
        output = cmd(FakeCore(pending=[a_pair(action="withdraw")])).pending([]).output

        assert "withdraw 25" in output

    def test_a_missing_withdrawable_figure_is_simply_omitted(self):
        output = cmd(FakeCore(
            pending=[a_pair(venue_withdrawable=None)])).pending([]).output

        assert "venue can return now" not in output.lower()


# ============================================================
# Prefix matching — approve executes with no second step
# ============================================================


class TestPrefixMatching:

    def test_an_empty_id_is_refused(self):
        core = FakeCore(pending=[a_pair()])

        result = cmd(core).approve([""])

        assert not result.success
        assert core.approved == []

    def test_no_arguments_is_refused(self):
        core = FakeCore(pending=[a_pair()])

        assert not cmd(core).approve([]).success
        assert core.approved == []

    def test_an_ambiguous_prefix_is_refused_rather_than_guessed(self):
        core = FakeCore(pending=[
            a_pair("abc11111-0000-0000-0000-000000000000"),
            a_pair("abc22222-0000-0000-0000-000000000000"),
        ])

        result = cmd(core).approve(["abc"])

        assert not result.success
        assert "matches 2" in result.error
        assert core.approved == [], "an ambiguous prefix moved money"

    def test_an_unknown_prefix_is_refused(self):
        core = FakeCore(pending=[a_pair()])

        assert not cmd(core).approve(["zzz"]).success
        assert core.approved == []

    def test_a_unique_prefix_is_enough(self):
        core = FakeCore(pending=[a_pair()])

        result = cmd(core).approve(["abc12345"])

        assert result.success
        assert core.approved == ["abc12345-0000-0000-0000-000000000000"]

    def test_reject_refuses_an_ambiguous_prefix_too(self):
        core = FakeCore(pending=[
            a_pair("abc11111-0000-0000-0000-000000000000"),
            a_pair("abc22222-0000-0000-0000-000000000000"),
        ])

        cmd(core).reject(["abc"])

        assert core.rejected == []


# ============================================================
# Outcomes
# ============================================================


class TestOutcomes:

    def test_an_executed_operation_reports_its_hash(self):
        core = FakeCore(pending=[a_pair()])

        output = cmd(core).approve(["abc12345"]).output

        assert "0xdead" in output

    def test_a_retryable_failure_says_it_is_worth_trying_again(self):
        """The difference between a venue short of liquidity today and a
        request that can never work. Without it the caller loops forever on
        one of them."""
        core = FakeCore(pending=[a_pair()], approve={
            "status": "failed", "reason": "no liquidity", "retryable": True})

        result = cmd(core).approve(["abc12345"])

        assert not result.success
        assert "trying again" in result.error

    def test_a_permanent_failure_does_not(self):
        core = FakeCore(pending=[a_pair()], approve={
            "status": "failed", "reason": "more than you hold"})

        result = cmd(core).approve(["abc12345"])

        assert not result.success
        assert "trying again" not in result.error

    def test_rejecting_passes_a_reason_through(self):
        core = FakeCore(pending=[a_pair()])

        cmd(core).reject(["abc12345", "not", "today"])

        assert core.rejected == [
            ("abc12345-0000-0000-0000-000000000000", "not today")]

    def test_rejecting_without_a_reason_still_records_one(self):
        core = FakeCore(pending=[a_pair()])

        cmd(core).reject(["abc12345"])

        assert core.rejected[0][1]


class TestHelp:

    def test_bare_position_shows_usage(self):
        assert "position pending" in cmd(FakeCore()).execute([]).output

    def test_an_unknown_subcommand_is_refused(self):
        assert not cmd(FakeCore()).execute(["borrow"]).success


# ============================================================
# venues
# ============================================================


def a_vault_venue():
    return VaultVenue(address=VAULT, name="Steakhouse USDG", symbol="steakUSDG",
                      curator=CURATOR, asset=USDG, asset_decimals=6,
                      share_decimals=18, total_assets=404_000_000_000_000)


def a_market_venue():
    return MarketVenue(
        params=(USDG, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20,
                915000000000000000),
        market_key=bytes.fromhex(MARKET[2:]), loan_token=USDG,
        collateral_token="0x" + "11" * 20, collateral_symbol="USDe",
        lltv=915000000000000000, loan_decimals=6, endorsed_by=VAULT)


def a_policy(**kw) -> SpendPolicy:
    kw.setdefault("enabled", True)
    kw.setdefault("morpho_curators", [CURATOR])
    kw.setdefault("max_total_deployed_usd", 500.0)
    return SpendPolicy.create("p", [4663], 1_000_000, defi_rules=DefiRules(**kw))


class TestVenues:

    def test_it_says_so_when_no_policy_enables_the_lane(self):
        result = VenuesCommands(FakeCore(policies=[]), None).execute([])

        assert "No policy enables Morpho" in result.output

    def test_it_separates_vaults_from_markets(self):
        """They are alternative routes, not a container and its contents."""
        core = FakeCore(policies=[a_policy()],
                        venues=[a_vault_venue(), a_market_venue()])

        output = VenuesCommands(core, None).execute([]).output

        assert "Vaults" in output and "Markets" in output
        assert output.index("Vaults") < output.index("Markets")

    def test_a_vault_shows_its_name_address_and_size(self):
        core = FakeCore(policies=[a_policy()], venues=[a_vault_venue()])

        output = VenuesCommands(core, None).execute([]).output

        assert "Steakhouse USDG" in output
        assert VAULT in output
        assert "404000000" in output.replace(",", "")

    def test_a_market_shows_its_collateral_and_lltv(self):
        core = FakeCore(policies=[a_policy()], venues=[a_market_venue()])

        output = VenuesCommands(core, None).execute([]).output

        assert "USDe" in output
        assert "91.5%" in output

    def test_an_unrestricted_policy_is_called_out(self):
        """The listed venues are then not the limit, and saying nothing would
        imply they were."""
        core = FakeCore(
            policies=[a_policy(restrict_to_steakhouse=False)],
            venues=[a_vault_venue()])

        output = VenuesCommands(core, None).execute([]).output

        assert "any Morpho venue" in output

    def test_a_restricted_policy_is_not(self):
        core = FakeCore(policies=[a_policy()], venues=[a_vault_venue()])

        assert "any Morpho venue" not in VenuesCommands(core, None).execute([]).output

    def test_an_unreadable_chain_fails_rather_than_showing_an_empty_list(self):
        """An empty list reads as "nothing is permitted", which is a different
        and much more alarming statement than "the node did not answer"."""
        core = FakeCore(policies=[a_policy()])

        def boom(curators):
            raise RuntimeError("node down")

        core.get_defi_venues = boom

        result = VenuesCommands(core, None).execute([])

        assert not result.success
        assert "Could not read venues" in result.error


# ============================================================
# Policy flags
# ============================================================


class TestPolicyFlags:

    @pytest.fixture
    def commands(self):
        from primer_vault.commands.policy import PolicyCommands
        created = {}

        class Core:
            def get_all_policies(self):
                return []

            def create_policy(self, **kw):
                created.update(kw)
                return SimpleNamespace(name=kw["name"], id="pid")

        commands = PolicyCommands(Core(), handler=None)
        commands._created = created
        return commands

    def test_morpho_is_off_unless_asked_for(self, commands):
        commands._create(["plain"])

        assert commands._created["defi_rules"] is None

    def test_the_flag_enables_it_restricted_by_default(self, commands):
        commands._create(["lender", "--morpho"])

        rules = commands._created["defi_rules"]
        assert rules.enabled is True
        assert rules.restrict_to_steakhouse is True
        assert rules.morpho_curators, "the shipped curator was not applied"

    def test_no_restrict_opens_the_venue_gate(self, commands):
        commands._create(["lender", "--morpho", "--no-restrict"])

        assert commands._created["defi_rules"].restrict_to_steakhouse is False

    def test_the_limits_are_settable(self, commands):
        commands._create(["lender", "--morpho", "--morpho-max", "50",
                          "--morpho-total", "250", "--morpho-percent", "40",
                          "--morpho-ops", "5", "--morpho-auto", "2.5"])

        rules = commands._created["defi_rules"]
        assert rules.max_deposit_usd == 50.0
        assert rules.max_total_deployed_usd == 250.0
        assert rules.max_deployed_percent == 40.0
        assert rules.max_ops_per_day == 5
        assert rules.auto_approve_below_usd == 2.5

    @pytest.mark.parametrize("args", [
        ["--morpho", "--morpho-max", "abc"],
        ["--morpho", "--morpho-percent", "150"],
        ["--morpho", "--morpho-ops", "0"],
        ["--morpho", "--morpho-total", "nope"],
    ])
    def test_a_bad_value_is_refused_rather_than_defaulted(self, commands, args):
        result = commands._create(["lender"] + args)

        assert not result.success

    def test_an_unknown_morpho_flag_is_refused(self, commands):
        assert not commands._create(["lender", "--morpho-curator", "0xabc"]).success

    def test_the_help_documents_the_flags(self, commands):
        text = commands._create_help().output

        for flag in ("--morpho", "--morpho-max", "--morpho-total",
                     "--morpho-percent", "--morpho-ops", "--no-restrict"):
            assert flag in text, f"{flag} is not documented"
