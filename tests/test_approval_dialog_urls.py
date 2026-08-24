"""The payee's `resource` and the agent's `request_url` cannot forge lines in the
payment approval dialog.

Both are attacker-controlled strings that reach the dialog. Only their origin
(scheme and host) may be rendered, so neither can carry a newline and a second,
fabricated set of terms in beside the real ones.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from primer_vault.networks import TOKENS

USDG = TOKENS["USDG"].addresses[4663]
RHC = 4663

ATTACKER = "0x00000000000000000000000000000000DeadBeef"
DECOY = "0x1111111111111111111111111111111111111111"

# A URL every check accepts, followed by a second, fabricated terms block.
FORGED_TAIL = (
    "https://api.stripe.com/v1/charges\n"
    "\n"
    "Amount: 0.010000 USDG\n"
    "Network: eip155:4663\n"
    "Recipient:\n"
    "  " + DECOY[:22] + "\n"
    "  " + DECOY[22:] + "\n"
    "URL: https://api.stripe.com/v1/charges"
)


def offer(resource, amount="20000000"):
    """A single-offer 402 in USDG on RHC, with a caller-chosen resource."""
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:4663",
            "amount": amount,
            "asset": USDG,
            "payTo": ATTACKER,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": resource, "description": "", "mimeType": ""},
    }


@pytest.fixture
def signing_setup(tmp_path):
    """Core with an open wallet, a commissioned bearer agent, and a policy that
    auto-approves below $10 (mirrors tests/test_approval_dialog_injection.py)."""
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


def _queue(signing_setup, resource, request_url=None):
    """Submit an over-threshold payment and return the queued SigningRequest."""
    svc, agent, token, _ = signing_setup
    svc._on_approval_needed = None  # keep it queued, as the daemon does
    result = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=offer(resource),
        request_url=request_url)
    assert result["status"] == "pending", result
    pending = svc.get_pending_requests()
    assert len(pending) == 1
    return pending[0]


def _message_for(request):
    """The real dialog text, built by calling the real method."""
    from PyQt6.QtWidgets import QApplication
    from primer_vault.ui import main_window as mw

    QApplication.instance() or QApplication([])
    captured = {}

    class FakeBox:
        def __init__(self, title, message, buttons, parent=None,
                     default_button=0, icon_type="info"):
            captured["message"] = message

        def exec(self):
            return 0

        def result_index(self):
            return 1  # Reject, so nothing is signed

    class Stub:
        core = type("C", (), {
            "reject_request": staticmethod(lambda *a, **k: None)})()
        showNormal = activateWindow = raise_ = lambda self: None

        def update_activity(self, *a, **k):
            pass

        _wrapped_address = staticmethod(mw.MainWindow._wrapped_address)

    original = mw.FramelessMessageBox
    mw.FramelessMessageBox = FakeBox
    try:
        mw.MainWindow.show_approval_dialog(Stub(), request)
    finally:
        mw.FramelessMessageBox = original
    return captured["message"]


class TestPayeeResourceReachesTheDialog:

    def test_control_a_clean_resource_renders_one_terms_block(self, signing_setup):
        """Ground truth: an ordinary offer produces exactly one set of terms."""
        request = _queue(signing_setup, "https://api.example.com/thing")
        message = _message_for(request)
        assert message.count("Recipient:") == 1
        assert message.count("Amount:") == 1

    def test_dialog_shows_exactly_one_recipient_block(self, signing_setup):
        request = _queue(signing_setup, FORGED_TAIL)
        message = _message_for(request)
        assert message.count("Recipient:") == 1, (
            "the approval dialog renders more than one 'Recipient:' block; "
            "the extra one was written by the payee:\n---\n" + message + "\n---")

    def test_dialog_shows_exactly_one_amount_line(self, signing_setup):
        request = _queue(signing_setup, FORGED_TAIL)
        message = _message_for(request)
        assert message.count("Amount:") == 1, (
            "the approval dialog renders more than one 'Amount:' line; "
            "the extra one was written by the payee:\n---\n" + message + "\n---")

    def test_the_payment_still_signs_so_the_deception_is_not_self_limiting(
            self, signing_setup):
        """Unlike an injected payTo (which the address regex refuses at signing
        time), a forged resource does not stop the signature: approving the
        dialog signs the real 20 USDG to the real recipient."""
        svc, _, _, _ = signing_setup
        request = _queue(signing_setup, FORGED_TAIL)
        result = svc.approve_request(request.id)
        assert result["status"] == "success", result


class TestAgentRequestUrlReachesTheDialog:

    def test_dialog_shows_exactly_one_recipient_block(self, signing_setup):
        request = _queue(signing_setup, "https://api.example.com/thing",
                         request_url=FORGED_TAIL)
        message = _message_for(request)
        assert message.count("Recipient:") == 1, (
            "the approval dialog renders more than one 'Recipient:' block; "
            "the extra one was written by the agent:\n---\n" + message + "\n---")
