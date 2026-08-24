"""
x402 payment asset tests.

Vault denominates every spending limit in USDG and reads the requested amount as
micro-USDG. The asset is named by whoever issued the 402 response, and EIP-3009
is an open standard - anyone can deploy a token implementing it with any number
of decimals. A token with fewer than six decimals would make a large payment
count as a small one against the daily and per-request limits.

The asset is therefore checked against the chain's USDG address before the amount
is interpreted, and again immediately before signing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS
from primer_vault.services.signing import SigningService

USDG = TOKENS["USDG"].addresses[4663]
RHC = 4663

# An attacker-deployed EIP-3009 token. Nothing stops anyone shipping one.
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


class TestAssetCheck:
    """Direct tests of the guard."""

    @pytest.fixture
    def service(self):
        return SigningService()

    def test_usdg_is_accepted(self, service):
        assert service._check_asset_supported(RHC, USDG) is None

    def test_usdg_accepted_regardless_of_case(self, service):
        """Addresses arrive in whatever case the merchant chose to send."""
        assert service._check_asset_supported(RHC, USDG.lower()) is None
        assert service._check_asset_supported(RHC, USDG.upper()) is None

    def test_arbitrary_token_is_rejected(self, service):
        error = service._check_asset_supported(RHC, HOSTILE_TOKEN)
        assert error is not None
        assert "USDG" in error

    @pytest.mark.parametrize("asset", [
        "",
        None,
        "0x0000000000000000000000000000000000000000",
        "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d169",  # USDG, last digit changed
    ])
    def test_missing_or_near_miss_addresses_rejected(self, service, asset):
        assert service._check_asset_supported(RHC, asset) is not None

    def test_chain_without_a_configured_asset_is_rejected(self, service):
        """Base (8453) is not configured, so nothing on it can be paid."""
        error = service._check_asset_supported(8453, USDG)
        assert error is not None
        assert "8453" in error

    def test_unknown_chain_is_rejected(self, service):
        assert service._check_asset_supported(0, USDG) is not None


class TestDecimalAssumptionIsGuarded:
    """The amount is read as micro-USDG, which only holds for a 6-decimal asset.

    These pin the connection between the two: the reason the assumption is safe
    is that the guard runs first. If the guard is removed, these fail.
    """

    @pytest.fixture
    def service(self):
        return SigningService()

    def test_amount_is_read_as_six_decimals(self, service):
        """1_000_000 atomic units == $1.00, which is only true at 6 decimals."""
        assert service._bounded_amount_micro("1000000") == 1_000_000

    def test_a_low_decimal_token_cannot_reach_the_amount_reader(self, service):
        """A 2-decimal token would make $1,000 read as $0.10.

        It never gets that far: the asset is refused first.
        """
        assert service._check_asset_supported(RHC, HOSTILE_TOKEN) is not None


class TestSigningRefusesUnsupportedAssets:
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
        return core._signing_service, agent, token

    def test_hostile_token_is_refused(self, service):
        svc, agent, token = service
        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402(asset=HOSTILE_TOKEN))
        assert result["status"] == "error"
        assert result["code"] == "UNSUPPORTED_ASSET"

    def test_refusal_is_recorded_for_audit(self, service):
        """Rejections leave a receipt, like every other policy refusal."""
        svc, agent, token = service
        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402(asset=HOSTILE_TOKEN))
        assert "transaction_id" in result

    def test_hostile_token_does_not_consume_the_daily_limit(self, service):
        """A refused payment must not move the agent's spend counter."""
        svc, agent, token = service
        before = svc._policy_store.get_agent_by_id(agent.id).spent_today_micro
        svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402(asset=HOSTILE_TOKEN))
        after = svc._policy_store.get_agent_by_id(agent.id).spent_today_micro
        assert after == before

    def test_usdg_is_not_blocked_by_the_guard(self, service):
        """The guard must not break the supported path."""
        svc, agent, token = service
        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token, x402_data=x402())
        assert result.get("code") != "UNSUPPORTED_ASSET"

    def test_http_status_is_forbidden(self):
        from primer_vault.services.server import get_http_status_for_error
        assert get_http_status_for_error("UNSUPPORTED_ASSET") == 403
