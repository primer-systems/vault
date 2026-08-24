"""Values chosen by the payee cannot forge lines in the payment approval dialog.

The `network` string in an x402 offer is written by whoever served the 402. It
must be rebuilt from the parsed chain id before it is displayed, so it cannot
carry a newline and a second, fabricated set of terms into the dialog the user
reads.
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

# A chain id every check will accept, followed by lines of the payee's choosing.
# int("4663") is all `parse_caip_network` ever reads out of this.
INJECTED_NETWORK = (
    "eip155:4663:\n"
    "Recipient:\n"
    "  " + DECOY[:22] + "\n"
    "  " + DECOY[22:] + "\n"
    "URL: https://api.stripe.com/v1/charges\n"
    + "\n" * 40
    + "Network"
)


def offer(network, amount="20000000"):
    """A single-offer 402 in USDG on RHC, with a caller-chosen network string."""
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": network,
            "amount": amount,
            "asset": USDG,
            "payTo": ATTACKER,
            "extra": {"name": "Global Dollar", "version": "1"},
        }],
        "resource": {"url": "https://api.example.com/thing",
                     "description": "", "mimeType": ""},
    }


@pytest.fixture
def signing_setup(tmp_path):
    """Core with an open wallet, a commissioned bearer agent, and a policy that
    auto-approves below $10 (mirrors tests/test_displayed_vs_signed.py)."""
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


def _queue(signing_setup, network):
    """Submit an over-threshold payment and return the queued SigningRequest."""
    svc, agent, token, _ = signing_setup
    svc._on_approval_needed = None  # keep it queued, as the daemon does
    result = svc.handle_sign_request(
        agent_id=agent.id, signature=token, x402_data=offer(network))
    assert result["status"] == "pending", result
    pending = svc.get_pending_requests()
    assert len(pending) == 1
    return pending[0]


class TestNetworkStringReachesTheDialogUnchecked:

    def test_control_a_clean_offer_is_accepted(self, signing_setup):
        """Ground truth: the ordinary form of this offer queues for approval."""
        request = _queue(signing_setup, "eip155:4663")
        assert request.network == "eip155:4663"
        assert request.recipient == ATTACKER

    def test_queued_request_carries_a_clean_chain_identifier(self, signing_setup):
        """The value the dialog prints after "Network:" must be a chain id and
        nothing else."""
        request = _queue(signing_setup, INJECTED_NETWORK)
        assert request.network == "eip155:4663", (
            "the payee's network string reached the approval request "
            "unnormalised: " + repr(request.network))


class TestApprovalDialogText:
    """Build the real dialog text by calling the real method."""

    @staticmethod
    def _message_for(request):
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

    def test_control_clean_offer_renders_one_recipient(self, signing_setup):
        request = _queue(signing_setup, "eip155:4663")
        message = self._message_for(request)
        assert message.count("Recipient:") == 1
        assert ATTACKER[2:] in message.replace("\n", "").replace(" ", "")

    def test_dialog_shows_exactly_one_recipient_block(self, signing_setup):
        request = _queue(signing_setup, INJECTED_NETWORK)
        message = self._message_for(request)
        assert message.count("Recipient:") == 1, (
            "the approval dialog renders more than one 'Recipient:' block; "
            "the extra one was written by the payee:\n---\n" + message + "\n---")

    def test_recipient_is_not_pushed_down_the_dialog_by_the_payee(self, signing_setup):
        """The dialog is a plain QLabel with no scroll area (ui/theme.py).
        A payee must not be able to insert lines above the true recipient."""
        request = _queue(signing_setup, INJECTED_NETWORK)
        message = self._message_for(request)
        # The true recipient block is the last one in the message.
        head = message[:message.rindex("Recipient:")]
        assert head.count("\n") <= 6, (
            "the payee inserted " + str(head.count("\n")) +
            " lines above the true recipient block")
