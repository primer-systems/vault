"""POST /mandate does not disclose the wallet's lock state.

When the wallet is locked an HMAC credential cannot be checked at all, because
verification needs the wallet's master key to decrypt the agent's secret.
Answering "wallet locked" would hand an unauthenticated caller a signal it
could poll, so /sign answers AUTH_FAILED either way. /mandate reaches the same
fork and must hold the same property.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_data_dir():
    """A throwaway data directory; the shared `core` fixture builds on it."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


BOGUS_HMAC_SIG = None  # built per-test, needs a fresh timestamp


def _bogus_sig() -> str:
    """A well-formed but wrong HMAC credential, with a current timestamp so it
    fails on the signature and not on the replay window."""
    return f"SIG:{int(time.time())}:{'00' * 32}"


@pytest.fixture
def commissioned_agent(core):
    """An active HMAC agent bound to a real address in the open wallet."""
    seed = core.create_seed(word_count=12)
    assert seed.get("success"), seed
    addr = core.add_address_from_seed(seed["seed_id"], 0, "probe")
    assert addr.get("success"), addr
    address = core.get_wallet_addresses()[0]["address"]

    policy = core.create_policy(
        name="MandateProbe",
        networks=[4663],
        daily_limit_micro=10_000_000,
        per_request_max_micro=1_000_000,
        auto_approve_below_micro=100_000,
    )
    agent, _secret = core.create_agent(name="MandateProbeAgent", auth_mode="hmac")
    core.commission_agent(agent.code, policy.id, address)
    return core.get_agent_by_id(agent.id)


class TestSignDoesNotRevealLockState:
    """The behaviour /sign already has, recorded so it cannot quietly change."""

    def test_sign_answers_the_same_locked_or_unlocked(self, core, commissioned_agent):
        svc = core._signing_service
        agent_id = commissioned_agent.id

        unlocked = svc.handle_sign_request(
            agent_id, _bogus_sig(), payment_required="dGVzdA==",
            request_url="https://example.com")
        core.lock_wallet()
        locked = svc.handle_sign_request(
            agent_id, _bogus_sig(), payment_required="dGVzdA==",
            request_url="https://example.com")

        assert unlocked.get("code") == "AUTH_FAILED"
        assert locked.get("code") == unlocked.get("code"), (
            "/sign must answer a bad credential identically whether or not the "
            f"wallet is unlocked; got {locked.get('code')} vs {unlocked.get('code')}")


class TestMandateDoesNotRevealLockState:
    """The same property, asked of /mandate."""

    def test_mandate_answers_the_same_locked_or_unlocked(self, core, commissioned_agent):
        svc = core._signing_service
        agent_id = commissioned_agent.id

        unlocked = svc.handle_get_mandate(agent_id, _bogus_sig())
        core.lock_wallet()
        locked = svc.handle_get_mandate(agent_id, _bogus_sig())

        assert unlocked.get("code") == "AUTH_FAILED", unlocked
        assert locked.get("code") == unlocked.get("code"), (
            "/mandate answers a bad credential differently depending on whether "
            "the wallet is unlocked, so an unauthenticated caller holding only an "
            "agent id can poll it to learn when the keys are in memory; got "
            f"{locked.get('code')} while unlocked gives {unlocked.get('code')}")


class TestOverTheWire:
    """The same difference, as an unauthenticated caller actually sees it.

    POST /mandate takes agent_id and signature from the body and passes both
    straight to handle_get_mandate (services/server.py), so this is the
    whole of what an attacker needs to send.
    """

    def test_http_status_differs_by_lock_state(self, core, commissioned_agent):
        import json
        import urllib.error
        import urllib.request

        from primer_vault.services.server import AgentServer

        server = AgentServer()
        server.set_signing_service(core._signing_service)
        port = 19913
        assert server.start(port=port, allow_lan=False)
        time.sleep(0.3)

        def ask() -> int:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/mandate", method="POST",
                data=json.dumps({
                    "agent_id": commissioned_agent.id,
                    "signature": _bogus_sig(),
                }).encode())
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                e.read()
                return e.code

        try:
            unlocked_status = ask()
            core.lock_wallet()
            locked_status = ask()
        finally:
            server.stop()

        assert unlocked_status == 401, unlocked_status
        assert locked_status == unlocked_status, (
            "An unauthenticated caller sees a different HTTP status depending on "
            f"whether the wallet is unlocked: {locked_status} when locked, "
            f"{unlocked_status} when unlocked.")
