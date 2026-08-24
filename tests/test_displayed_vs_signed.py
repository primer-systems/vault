"""
Displayed vs signed.

These tests pin the places where the value shown to (or recorded for) the user
must match the value that was actually signed. Cases exercised here:

1. Multi-offer x402: `_sign_payment` reads `accepts[0]` for the audit record's
   network (signing.py) and for the `accepted` block echoed to the
   merchant (signing.py), while the signature itself is built from the
   offer `_select_offer` picked - which need not be `accepts[0]`.

2. The console `pending` listing shows only the first 16 characters of the
   recipient, with no tail (commands/approval.py), although the recipient
   is the value the approval exists to let a human verify.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS

USDG = TOKENS["USDG"].addresses[4663]
RHC = 4663

# USDC on Base - a real, unsupported offer a multi-chain merchant would send.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

PAY_TO = "0x00000000000000000000000000000000000c0De0"


def multi_offer_x402(amount="1000000"):
    """A 402 offering [USDC on Base, USDG on RHC] - the exact shape the
    _select_offer comment in eip3009.py says must be supported."""
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": "999999999",  # deliberately different from the RHC offer
                "asset": USDC_BASE,
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 60,
                "extra": {"name": "USD Coin", "version": "2"},
            },
            {
                "scheme": "exact",
                "network": "eip155:4663",
                "amount": amount,
                "asset": USDG,
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 60,
                "extra": {"name": "Global Dollar", "version": "1"},
            },
        ],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


@pytest.fixture
def signing_setup(tmp_path):
    """Core with an open wallet, a commissioned bearer agent, and a policy that
    auto-approves below $10 (mirrors tests/test_payment_asset.py)."""
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
    return core._signing_service, agent, token, address["address"]


def _sign_multi_offer(signing_setup):
    svc, agent, token, wallet_address = signing_setup
    result = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=multi_offer_x402())
    assert result["status"] == "success", result
    return svc, result, wallet_address


class TestMultiOfferAuditRecord:
    """The transaction record must name the chain that was signed."""

    def test_recorded_network_is_the_signed_offers_network(self, signing_setup):
        svc, result, _ = _sign_multi_offer(signing_setup)
        tx = svc._policy_store.get_transaction(result["transaction_id"])
        # The amount comes from the selected (RHC) offer - this should pass.
        assert tx.amount_micro == 1_000_000
        # The network must too. Current code records accepts[0].network
        # ("eip155:8453"), the offer that was NOT signed.
        assert tx.network == "eip155:4663", (
            f"transaction record says the payment is on {tx.network!r}, "
            f"but the authorization was signed for chain 4663")


class TestMultiOfferEchoedExtra:
    """The `accepted.extra` echoed to the merchant must be the
    domain the signature was actually produced with - the merchant uses it to
    reconstruct the verification domain (eip3009.py)."""

    def test_signature_domain_is_the_selected_offers(self, signing_setup):
        """Establish ground truth first: the signature really is made with the
        selected (RHC/USDG) offer's domain. This should pass."""
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        svc, result, wallet_address = _sign_multi_offer(signing_setup)
        payment = json.loads(base64.b64decode(result["header_value"]))
        auth = payment["payload"]["authorization"]
        sig = payment["payload"]["signature"]

        def recover(domain_name, domain_version):
            typed = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "TransferWithAuthorization": [
                        {"name": "from", "type": "address"},
                        {"name": "to", "type": "address"},
                        {"name": "value", "type": "uint256"},
                        {"name": "validAfter", "type": "uint256"},
                        {"name": "validBefore", "type": "uint256"},
                        {"name": "nonce", "type": "bytes32"},
                    ],
                },
                "primaryType": "TransferWithAuthorization",
                "domain": {
                    "name": domain_name,
                    "version": domain_version,
                    "chainId": RHC,
                    "verifyingContract": USDG,
                },
                "message": {
                    "from": auth["from"],
                    "to": auth["to"],
                    "value": int(auth["value"]),
                    "validAfter": auth["validAfter"],
                    "validBefore": auth["validBefore"],
                    "nonce": bytes.fromhex(auth["nonce"][2:]),
                },
            }
            return Account.recover_message(
                encode_typed_data(full_message=typed), signature=sig)

        # Signed under the RHC offer's domain...
        assert recover("Global Dollar", "1").lower() == wallet_address.lower()
        # ...and NOT under the Base offer's domain.
        assert recover("USD Coin", "2").lower() != wallet_address.lower()

    def test_echoed_extra_matches_the_signed_domain(self, signing_setup):
        """Current code echoes accepts[0].extra ("USD Coin"/"2") although the
        signature above was made with "Global Dollar"/"1"."""
        svc, result, _ = _sign_multi_offer(signing_setup)
        payment = json.loads(base64.b64decode(result["header_value"]))
        assert payment["accepted"]["extra"] == {
            "name": "Global Dollar", "version": "1"}, (
            "the extra echoed to the merchant is not the domain the "
            "signature was produced with; the merchant will reconstruct the "
            "wrong verification domain and reject the payment")


class TestConsolePendingRecipientDisplay:
    """The console `pending` list is an approval surface, and it
    shows only the first 16 characters of the recipient with no tail."""

    def test_pending_line_shows_enough_of_the_recipient_to_verify(self, signing_setup):
        from primer_vault.commands.approval import ApprovalCommands

        svc, agent, token, _ = signing_setup
        # The bare core auto-rejects anything needing approval; keep it queued
        # instead, the way the daemon does (daemon/app.py).
        svc._on_approval_needed = None
        # Above the $10 auto-approve threshold -> queued for approval.
        result = svc.handle_sign_request(
            agent_id=agent.id, signature=token,
            x402_data=multi_offer_x402(amount="20000000"))
        assert result["status"] == "pending", result

        class CoreShim:
            def get_pending_requests(self):
                return svc.get_pending_requests()

        out = ApprovalCommands(CoreShim(), None).pending([])
        line = next(l for l in out.output.splitlines() if "->" in l)
        # The last characters of the address are what a human checks against a
        # known-good copy; the current line ends at recipient[:16] + "...".
        assert PAY_TO[-8:] in line, (
            f"pending line shows only a prefix of the recipient: {line!r}")
