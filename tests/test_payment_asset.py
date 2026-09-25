"""
x402 payment asset tests.

0.4 rewrite (2026-09-23): Vault no longer restricts x402 to one allowlisted
asset per chain - see Planning-private/vault/0.4/2026-09-23-multi-network-base.md.
x402 itself is asset-agnostic per request (the resource server names whatever
asset it wants), so Vault will sign for any asset on a supported chain.

The safety property this file protects has not changed, only the mechanism:
Vault denominates every spending limit in one reference stablecoin per chain
(USDG on RHC, USDC on Base) and reads the requested amount as micro-units of
it. A token with fewer decimals than assumed, or one with no real value,
would make a large payment count as a small one against the daily and
per-request limits if its raw amount were ever taken at face value.

Previously this was prevented by refusing anything that was not exactly the
allowlisted asset. Now it is prevented by pricing: the reference stablecoin
itself is 1:1 (no RPC call - _price_in_reference_micro reads its raw amount
directly, which is safe only because it IS the 6-decimal reference asset).
Any other asset must be priced via an on-chain quote before its amount is
trusted; a token that cannot be priced (no real liquidity - the case for an
attacker-deployed token with no genuine pool) is never assigned a value. It
is also never treated as free: it is routed to mandatory human approval and
excluded from limit accounting entirely (amount_micro=0), rather than
auto-processed at a guessed or wrong scale.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS
from primer_vault.services.signing import SigningService

USDG = TOKENS["USDG"].addresses[4663]
USDC_BASE = TOKENS["USDC"].addresses[8453]
RHC = 4663
BASE = 8453

# An attacker-deployed EIP-3009 token. Nothing stops anyone shipping one, and
# it has no real Uniswap liquidity - _quote_to_reference_micro correctly
# returns None for it in practice. Tests below monkeypatch that specific
# outcome rather than hit a real RPC, to stay fast and offline.
HOSTILE_TOKEN = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def x402(asset=USDG, amount="1000000", network="eip155:4663"):
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": network,
            "amount": amount,
            "asset": asset,
            "payTo": "0x00000000000000000000000000000000000c0De0",
            "maxTimeoutSeconds": 60,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


class TestReferenceStablecoinPricesDirectly:
    """The reference stablecoin needs no quote - its raw atomic amount IS the
    micro amount, since it's the 6-decimal asset limits are denominated in."""

    @pytest.fixture
    def service(self):
        return SigningService()

    def test_usdg_is_priced_1to1(self, service):
        assert service._price_in_reference_micro(RHC, USDG, "1000000") == 1_000_000

    def test_usdg_accepted_regardless_of_case(self, service):
        """Addresses arrive in whatever case the merchant chose to send."""
        assert service._price_in_reference_micro(RHC, USDG.lower(), "1000000") == 1_000_000
        assert service._price_in_reference_micro(RHC, USDG.upper(), "1000000") == 1_000_000

    def test_usdc_on_base_is_priced_1to1(self, service):
        """Base's reference stablecoin is USDC, not USDG - it must price the
        same way USDG does on RHC."""
        assert service._price_in_reference_micro(BASE, USDC_BASE, "2500000") == 2_500_000

    def test_amount_is_read_as_six_decimals(self, service):
        """1_000_000 atomic units == $1.00, which is only true at 6 decimals -
        both USDG and USDC are 6-decimal tokens."""
        assert service._price_in_reference_micro(RHC, USDG, "1000000") == 1_000_000

    def test_unreasonably_large_amount_is_rejected(self, service):
        with pytest.raises(ValueError):
            service._price_in_reference_micro(RHC, USDG, str(10**16))

    @pytest.mark.parametrize("amount", ["", None, "not-a-number", "-5", "0"])
    def test_malformed_or_nonpositive_amount_is_rejected(self, service, amount):
        with pytest.raises(ValueError):
            service._price_in_reference_micro(RHC, USDG, amount)


class TestUnpriceableAssetIsNeverGuessedAt:
    """A token _price_in_reference_micro can't value (no real quote route)
    must never be silently assigned a value - the caller must fail closed."""

    @pytest.fixture
    def service(self):
        svc = SigningService()
        # No real Uniswap liquidity for an attacker-deployed token - this is
        # what _quote_to_reference_micro returns in that case in practice.
        # Monkeypatched here to keep the test fast and offline rather than
        # hitting a real RPC/quoter.
        svc._quote_to_reference_micro = lambda *a, **k: None
        return svc

    def test_hostile_token_cannot_be_priced(self, service):
        assert service._price_in_reference_micro(RHC, HOSTILE_TOKEN, "1000000") is None

    def test_chain_without_a_reference_stablecoin_cannot_price_anything(self, service):
        """A chain with no REFERENCE_STABLECOIN entry (unsupported chain)
        has nothing to price against."""
        assert service._price_in_reference_micro(999999, USDG, "1000000") is None

    def test_a_low_decimal_token_is_never_taken_at_face_value(self, service):
        """A 2-decimal hostile token's raw '1000000' is NOT 1_000_000
        micro-units of anything real - pricing it (rather than assuming
        1:1) is exactly what prevents a mis-scaled amount from reaching the
        limit checks. Since it can't be priced, the answer is None, not a
        guessed number."""
        priced = service._price_in_reference_micro(RHC, HOSTILE_TOKEN, "1000000")
        assert priced is None


class TestSigningRoutesUnpriceableAssetsToApproval:
    """End to end through the request handler."""

    @pytest.fixture
    def service(self, tmp_path):
        from primer_vault.core import Vault
        data_dir = tmp_path / "data"
        (data_dir / "wallets").mkdir(parents=True)
        core = Vault(data_dir=data_dir)
        wallet_path = str(data_dir / "wallets" / "test.wallet")
        core.create_wallet(wallet_path, "testpass")
        core.load_wallet(wallet_path, "testpass")

        agent, token = core.create_agent(name="Payer", auth_mode="bearer")
        policy = core.create_policy(
            name="P", networks=[RHC], daily_limit_micro=100_000_000,
            per_request_max_micro=50_000_000, auto_approve_below_micro=10_000_000)
        address = core.get_wallet_addresses()[0]
        core.commission_agent(agent.code, policy.id, address["address"])
        svc = core._signing_service
        # See TestUnpriceableAssetIsNeverGuessedAt - avoid a real RPC call.
        svc._quote_to_reference_micro = lambda *a, **k: None
        return svc, agent, token

    def test_hostile_token_is_queued_for_approval_not_auto_processed(self, service):
        svc, agent, token = service
        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402(asset=HOSTILE_TOKEN))
        assert result["status"] == "pending"
        assert result["code"] == "APPROVAL_REQUIRED"

    def test_hostile_token_does_not_consume_the_daily_limit(self, service):
        """An unpriced payment must not move the agent's spend counter -
        it was never assigned a value to debit."""
        svc, agent, token = service
        before = svc._policy_store.get_agent_by_id(agent.id).spent_today_micro
        svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402(asset=HOSTILE_TOKEN))
        after = svc._policy_store.get_agent_by_id(agent.id).spent_today_micro
        assert after == before

    def test_usdg_is_not_blocked_and_can_auto_approve(self, service):
        """The reference-asset path must not be affected by the pricing
        change - it still auto-approves below threshold as before."""
        svc, agent, token = service
        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402())
        assert result.get("code") != "APPROVAL_REQUIRED"
        assert result["status"] == "success"
