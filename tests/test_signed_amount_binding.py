"""
Is the amount the user approved bound to the amount that gets signed?

The x402 payment amount is computed twice from the same payload:

  - signing.py parses it at intake. That number is what the approval
    dialog shows, what the per-request and daily limits are applied to, what the
    daily spend is debited by, and what the transaction record says.
  - signing.py parses it AGAIN inside _sign_payment, and it is that second
    parse (eip3009.py) that becomes the `value` field of the EIP-712
    TransferWithAuthorization the wallet key signs.

The first number is passed into _sign_payment as `amount_micro` and is never
compared with the second. Nothing asserts the two agree.

Today they always do: parse_x402 is a pure function of the payload dict, the
dict is unchanged between the two calls, and both callers of _sign_payment pass
the amount_micro that came from the same parse. This test is not reachable from
any current caller - it demonstrates the missing binding directly, by handing
_sign_payment a checked amount that differs from the payload's.
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
PAY_TO = "0x00000000000000000000000000000000000c0De0"


def x402(amount):
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": f"eip155:{RHC}",
            "amount": str(amount),
            "asset": USDG,
            "payTo": PAY_TO,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


@pytest.fixture
def signing_setup(tmp_path):
    """Core with an open wallet, a commissioned bearer agent, and a policy."""
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
    return core, core._signing_service, agent, token


def test_a_checked_amount_that_differs_from_the_payload_is_refused(signing_setup):
    """_sign_payment is handed the amount that passed the limits and was
    recorded ($0.000001), and a payload that says $20. It must refuse rather
    than sign a value it never checked."""
    core, svc, agent, _token = signing_setup
    policy = core.get_policy(agent.policy_id)

    checked_micro = 1  # what the dialog showed and the limits were applied to
    payload = x402(20_000_000)  # what the payload actually asks for

    result = svc._sign_payment(agent, policy, payload, checked_micro,
                               x402_version=2, auto_approved=True)

    assert result["status"] == "error" and result.get("code") == "AMOUNT_MISMATCH", (
        f"a payload amount that differs from the checked amount was not "
        f"refused: {result}")


def test_a_matching_amount_still_signs_normally(signing_setup):
    """The guard is inert in normal operation: when the checked amount matches
    the payload (as it always does from a single parse), signing proceeds and
    the recorded amount equals the signed value."""
    core, svc, agent, _token = signing_setup
    policy = core.get_policy(agent.policy_id)

    result = svc._sign_payment(agent, policy, x402(20_000_000), 20_000_000,
                               x402_version=2, auto_approved=True)
    assert result["status"] == "success", result

    payment = json.loads(base64.b64decode(result["header_value"]))
    signed_value = int(payment["payload"]["authorization"]["value"])
    tx = svc._policy_store.get_transaction(result["transaction_id"])

    assert signed_value == 20_000_000
    assert tx.amount_micro == signed_value, (
        f"the transaction record says {tx.amount_micro} micro-USDG but the "
        f"authorization signs {signed_value}")
