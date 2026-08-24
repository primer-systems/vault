"""
Positive control for the x402 approval path: the value a person approves must
equal the value that gets signed.

The dialog is built from the SigningRequest's `amount_micro`, `network` and
`recipient`; the signature is built independently from a second parse of the
stored payload. These tests recover the signature and compare every field the
dialog rendered against the field that was signed, including a multi-offer 402
where the first offer is unsupported.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS

RHC = 4663
USDG = TOKENS["USDG"].addresses[RHC]
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x00000000000000000000000000000000000c0De0"

TYPES = {
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
}


def x402_payload(amount, *, multi_offer=False):
    """A 402 the merchant controls end to end."""
    rhc_offer = {
        "scheme": "exact",
        "network": "eip155:4663",
        "amount": amount,
        "asset": USDG,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "extra": {"name": "Global Dollar", "version": "1"},
    }
    accepts = [rhc_offer]
    if multi_offer:
        accepts.insert(0, {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "999999999",
            "asset": USDC_BASE,
            "payTo": "0x000000000000000000000000000000000000dEaD",
            "extra": {"name": "USD Coin", "version": "2"},
        })
    return {
        "x402Version": 2,
        "accepts": accepts,
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


@pytest.fixture
def signing_setup(tmp_path):
    """Core with an open wallet, a commissioned bearer agent, and a policy whose
    auto-approve threshold is low enough that every payment here escalates to
    the human approval dialog."""
    from primer_vault.core import Vault
    from primer_vault.core.interfaces import HeadlessApprovalHandler
    data_dir = tmp_path / "data"
    (data_dir / "wallets").mkdir(parents=True)
    core = Vault(data_dir=data_dir)
    # The same handler the daemon installs (daemon/app.py): requests wait for
    # a human instead of being auto-rejected.
    core.set_approval_handler(HeadlessApprovalHandler(core, auto_reject=False))
    wallet_path = str(data_dir / "wallets" / "test.wallet")
    core.create_wallet(wallet_path, "testpass")
    core.load_wallet(wallet_path, "testpass")

    agent, token = core.create_agent(name="Payer", auth_mode="bearer")
    policy = core.create_policy(
        name="P", networks=[RHC], daily_limit_micro=1_000_000_000,
        per_request_max_micro=500_000_000,
        auto_approve_below_micro=1)  # 1 micro-USDG: everything needs approval
    address = core.get_wallet_addresses()[0]
    core.commission_agent(agent.code, policy.id, address["address"])
    return core._signing_service, agent, token, address["address"]


def _approve_and_recover(signing_setup, payload):
    """Queue a payment, read what the dialog would show, approve it, and pull
    the signed fields back out of the signature."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    svc, agent, token, wallet_address = signing_setup

    queued = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=payload)
    assert queued["status"] == "pending", queued

    # Exactly what ui/main_window.py renders.
    (request,) = svc.get_pending_requests()
    displayed = {
        "amount": request.amount_micro / 1_000_000,
        "network": request.network,
        "recipient": request.recipient,
    }

    result = svc.approve_request(request.id)
    assert result["status"] == "success", result

    payment = json.loads(base64.b64decode(result["header_value"]))
    auth = payment["payload"]["authorization"]
    signature = payment["payload"]["signature"]

    # Recover under the domain the dialog implied: RHC's chain id, RHC's USDG.
    typed = {
        "types": TYPES,
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "Global Dollar", "version": "1",
                   "chainId": RHC, "verifyingContract": USDG},
        "message": {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": auth["validAfter"],
            "validBefore": auth["validBefore"],
            "nonce": bytes.fromhex(auth["nonce"][2:]),
        },
    }
    signer = Account.recover_message(
        encode_typed_data(full_message=typed), signature=signature)
    assert signer.lower() == wallet_address.lower(), (
        "the signature does not verify under the chain and token the dialog "
        "named, so the recovered fields below mean nothing")

    return displayed, typed["message"]


class TestApprovedValueIsTheSignedValue:

    def test_amount(self, signing_setup):
        displayed, signed = _approve_and_recover(
            signing_setup, x402_payload("1234567"))
        assert displayed["amount"] == 1.234567
        assert signed["value"] == 1_234_567, (
            f"dialog showed {displayed['amount']} USDG; "
            f"signature authorises {signed['value']} micro-USDG")

    def test_recipient(self, signing_setup):
        displayed, signed = _approve_and_recover(
            signing_setup, x402_payload("1000000"))
        assert displayed["recipient"].lower() == signed["to"].lower(), (
            f"dialog showed recipient {displayed['recipient']}; "
            f"signature authorises a transfer to {signed['to']}")

    def test_chain(self, signing_setup):
        displayed, _ = _approve_and_recover(
            signing_setup, x402_payload("1000000"))
        # Recovery above only succeeds under chainId 4663, so the signature is
        # bound to the chain the dialog named.
        assert displayed["network"] == "eip155:4663"

    def test_multi_offer_dialog_describes_the_offer_that_gets_signed(
            self, signing_setup):
        """A merchant offering an unsupported chain first must not be able to
        make the dialog describe one offer while the signature covers another."""
        displayed, signed = _approve_and_recover(
            signing_setup, x402_payload("2500000", multi_offer=True))
        assert displayed["amount"] == 2.5
        assert signed["value"] == 2_500_000
        assert displayed["recipient"].lower() == PAY_TO.lower()
        assert signed["to"].lower() == PAY_TO.lower()
        assert displayed["network"] == "eip155:4663"
