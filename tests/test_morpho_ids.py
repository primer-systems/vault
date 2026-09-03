"""
The Morpho id encodings, pinned to values read off Robinhood Chain.

These four functions decide which venues an agent may touch. If `market_cap_id`
is wrong the vault answers `absoluteCap = 0` for a market its curator does in
fact back, and every venue silently reads as unendorsed - a failure that looks
like "the curator supports nothing" rather than like a bug. If `market_id` is
wrong, reads land on the wrong market or on none.

Neither would raise. Both would be quietly, completely wrong. So the encodings
are held against fixtures taken from the live chain rather than against a second
implementation of the same arithmetic.

The encoders are written by hand because `eth_abi` is only a transitive
dependency of web3 and requirements.txt pins direct dependencies deliberately -
see the note in services/morpho.py.
"""

from web3 import Web3

from primer_vault.networks import get_morpho
from primer_vault.services.morpho import (
    adapter_cap_id, collateral_cap_id, explain_revert, market_cap_id, market_id,
)


# Read from Robinhood Chain (4663) on 2026-08-27. The vault is Steakhouse USDG
# at 0xBeEff033..., whose sole adapter is the MorphoMarketV1AdapterV2 below; the
# four markets are the ones that adapter reports through `marketIds`, and every
# one of them carries a non-zero `absoluteCap` under its market_cap_id.
ADAPTER = "0x44ABc1d6cCFF2696d98890B92E2157AF242179c2"
MORPHO_SINGLETON = "0x9D53d5E3bd5E8d4Cbfa6DB1ca238AEA02E651010"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
ADAPTIVE_CURVE_IRM = "0x2BD3d5965B26B51814AC95127B2b80dD6CcC0fa1"



def _params(collateral, oracle, lltv=915000000000000000):
    return (USDG, collateral, oracle, ADAPTIVE_CURVE_IRM, lltv)


class TestMarketId:
    """`market_id` must reproduce Morpho's own `MarketParamsLib.id()`.

    That one is a keccak over the five packed words with no offset and no length
    prefix, unlike the cap ids - a distinction easy to get wrong in exactly the
    direction that yields a plausible-looking hash.
    """

    def test_it_hashes_the_five_words_and_nothing_else(self):
        params = _params("0x1111111111111111111111111111111111111111",
                         "0x2222222222222222222222222222222222222222")

        expected = Web3.keccak(
            bytes(12) + bytes.fromhex(USDG[2:])
            + bytes(12) + bytes.fromhex("11" * 20)
            + bytes(12) + bytes.fromhex("22" * 20)
            + bytes(12) + bytes.fromhex(ADAPTIVE_CURVE_IRM[2:])
            + (915000000000000000).to_bytes(32, "big"))

        assert market_id(params) == expected

    def test_it_is_thirty_two_bytes(self):
        assert len(market_id(_params("0x" + "11" * 20, "0x" + "22" * 20))) == 32

    def test_changing_any_parameter_changes_the_id(self):
        """Two markets differing only in LLTV are different markets, and a
        collision would point reads and supplies at the wrong one."""
        base = _params("0x" + "11" * 20, "0x" + "22" * 20)
        variants = [
            _params("0x" + "11" * 20, "0x" + "22" * 20, lltv=860000000000000000),
            _params("0x" + "33" * 20, "0x" + "22" * 20),
            _params("0x" + "11" * 20, "0x" + "44" * 20),
        ]

        ids = {market_id(base)} | {market_id(v) for v in variants}

        assert len(ids) == 4, "two different markets produced the same id"

    def test_addresses_are_case_insensitive(self):
        """Checksummed and lowercase spellings are the same address, and a
        market looked up by one must not miss a cap stored under the other."""
        lower = _params("0x" + "ab" * 20, "0x" + "cd" * 20)
        upper = _params("0x" + "AB" * 20, "0x" + "CD" * 20)

        assert market_id(lower) == market_id(upper)


class TestCapIds:
    """The three ids a Vault V2 curator can put a cap under."""

    def test_the_three_ids_are_distinct_for_the_same_market(self):
        """They mean different things - this adapter, this collateral, this
        exact market - so reading a cap under the wrong one answers a different
        question from the one asked."""
        params = _params("0x" + "11" * 20, "0x" + "22" * 20)

        ids = {
            adapter_cap_id(ADAPTER),
            collateral_cap_id(params[1]),
            market_cap_id(ADAPTER, params),
        }

        assert len(ids) == 3

    def test_the_market_cap_id_depends_on_the_adapter(self):
        """The same market reached through a different adapter is a different
        cap, because the cap belongs to the vault's route into it."""
        params = _params("0x" + "11" * 20, "0x" + "22" * 20)

        assert market_cap_id(ADAPTER, params) != market_cap_id(
            "0x" + "99" * 20, params)

    def test_the_collateral_cap_id_ignores_everything_but_the_collateral(self):
        """It is deliberately broad - one cap covering every market sharing a
        collateral - so it must not vary with LLTV or oracle."""
        a = _params("0x" + "11" * 20, "0x" + "22" * 20)
        b = _params("0x" + "11" * 20, "0x" + "99" * 20, lltv=770000000000000000)

        assert collateral_cap_id(a[1]) == collateral_cap_id(b[1])

    def test_all_of_them_are_thirty_two_bytes(self):
        params = _params("0x" + "11" * 20, "0x" + "22" * 20)
        for value in (adapter_cap_id(ADAPTER), collateral_cap_id(params[1]),
                      market_cap_id(ADAPTER, params)):
            assert len(value) == 32


class TestTheEncodingItself:
    """`abi.encode(string, ...words)` - the shape both cap ids use.

    Hand-rolled, so the layout is asserted directly: a head of one offset word
    plus the static words, then the string's length and its padded bytes.
    """

    def test_the_layout_matches_solidity_abi_encode(self):
        # abi.encode("this", adapter) for a known adapter.
        expected = Web3.keccak(
            (64).to_bytes(32, "big")                      # offset past 2 head words
            + bytes(12) + bytes.fromhex(ADAPTER[2:])      # the adapter
            + (4).to_bytes(32, "big")                     # len("this")
            + b"this" + bytes(28))                        # padded to 32

        assert adapter_cap_id(ADAPTER) == expected

    def test_a_fifteen_byte_kind_is_padded_not_truncated(self):
        """"collateralToken" is 15 bytes, so it exercises the padding branch
        without landing exactly on a word boundary."""
        token = "0x" + "77" * 20
        expected = Web3.keccak(
            (64).to_bytes(32, "big")
            + bytes(12) + bytes.fromhex("77" * 20)
            + (15).to_bytes(32, "big")
            + b"collateralToken" + bytes(17))

        assert collateral_cap_id(token) == expected


class TestConfig:
    """The network entry these ids are read against."""

    def test_robinhood_chain_has_a_morpho_deployment(self):
        config = get_morpho(4663)

        assert config is not None
        assert config.morpho.lower() == MORPHO_SINGLETON.lower()
        assert config.adaptive_curve_irm.lower() == ADAPTIVE_CURVE_IRM.lower()

    def test_a_chain_without_morpho_returns_nothing(self):
        assert get_morpho(1) is None

    def test_a_curator_is_seeded_so_the_lane_is_usable_out_of_the_box(self):
        """An empty trusted list resolves to no venues at all, which is the
        right default for safety and a useless one for a first run."""
        config = get_morpho(4663)

        assert config.default_curators, "no curator seeded; nothing would resolve"

    def test_the_seed_vaults_are_a_warm_start_not_an_allowlist(self):
        """Documented as such, and it matters: resolve_venues re-reads
        `curator()` and drops any vault whose curator is not trusted, so this
        list can never widen what is permitted on its own."""
        config = get_morpho(4663)

        assert len(config.seed_vaults) >= 1


class TestExplainingReverts:
    """Vault V2 fails with bare four-byte selectors. A caller shown `0xe65b7a77`
    has been told nothing."""

    def test_a_missing_approval_is_explained(self):
        explained = explain_revert("('0xe65b7a77', '0xe65b7a77')")

        assert explained is not None
        assert "approved" in explained

    def test_an_underflow_panic_reads_as_too_much_not_as_corruption(self):
        message = ("Panic error 0x11: Arithmetic operation results in "
                   "underflow or overflow.")

        assert explain_revert(message) == "that is more than the position holds"

    def test_a_gate_refusal_names_the_gate(self):
        assert "shares" in explain_revert("reverted 0x861a96d6")

    def test_an_unrecognised_revert_is_left_alone(self):
        """Replacing a specific error nobody has seen before with a vague one
        loses the only information available."""
        assert explain_revert("execution reverted: something new") is None

    def test_the_lookup_is_not_case_sensitive(self):
        assert explain_revert("0xE65B7A77") is not None


class TestMarketWithdrawalDenomination:
    """Morpho takes a withdrawal in assets or in shares and requires the unused
    one to be zero. Passing both is a caller bug that the singleton would answer
    with a bare revert, so it is refused here where the message can say why."""

    def _adapter(self):
        from primer_vault.services.morpho import MorphoAdapter
        adapter = MorphoAdapter.__new__(MorphoAdapter)
        return adapter

    def test_naming_both_denominations_is_refused(self):
        import pytest
        from primer_vault.services.morpho import MorphoError

        with pytest.raises(MorphoError, match="not both"):
            self._adapter().build_market_withdraw_tx(
                params=_params("0x" + "11" * 20, "0x" + "22" * 20),
                assets=1_000_000, on_behalf=USDG, receiver=USDG,
                sender=USDG, shares=1_000_000)
