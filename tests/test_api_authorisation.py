"""Callers that present no credential.

POST /callback answers the same whether or not the wallet is unlocked, so it
cannot be polled as a "the keys are in memory now" oracle. /sign and /mandate
hold the same property.

This file used to carry a second property about the Admin API refusing to open
on an unrecognised `admin_api_mode`. The Admin API was removed in 0.3 and that
setting no longer exists.
"""

import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_data_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _bogus_sig() -> str:
    """A well-formed but wrong HMAC credential, timestamped now so it fails on
    the signature rather than on the replay window."""
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
        name="CallbackProbe",
        networks=[4663],
        daily_limit_micro=10_000_000,
        per_request_max_micro=1_000_000,
        auto_approve_below_micro=100_000,
    )
    agent, _secret = core.create_agent(name="CallbackProbeAgent", auth_mode="hmac")
    core.commission_agent(agent.code, policy.id, address)
    return core.get_agent_by_id(agent.id)


class TestCallbackDoesNotRevealLockState:

    def test_callback_answers_the_same_locked_or_unlocked(self, core, commissioned_agent):
        svc = core._signing_service
        agent_id = commissioned_agent.id

        unlocked = svc.handle_callback(
            agent_id, "00000000-0000-4000-8000-000000000000", "settled",
            tx_hash="0x" + "11" * 32, signature=_bogus_sig())
        core.lock_wallet()
        locked = svc.handle_callback(
            agent_id, "00000000-0000-4000-8000-000000000000", "settled",
            tx_hash="0x" + "11" * 32, signature=_bogus_sig())

        assert locked.get("code") == unlocked.get("code"), (
            "/callback answers a bad credential differently depending on "
            "whether the wallet is unlocked, so a caller holding only an agent "
            "id can poll it to learn when the keys are in memory; got "
            f"{locked.get('code')} while unlocked gives {unlocked.get('code')}")


class TestCallbackOverTheWire:
    """The same difference, as an unauthenticated caller actually sees it.

    POST /callback takes agent_id and signature from the body and passes both
    straight to handle_callback (services/server.py).
    """

    def test_http_status_differs_by_lock_state(self, core, commissioned_agent):
        from primer_vault.services.server import AgentServer

        server = AgentServer()
        server.set_signing_service(core._signing_service)
        port = 19915
        assert server.start(port=port, allow_lan=False)
        time.sleep(0.3)

        def ask() -> int:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/callback", method="POST",
                data=json.dumps({
                    "agent_id": commissioned_agent.id,
                    "signature": _bogus_sig(),
                    "transaction_id": "00000000-0000-4000-8000-000000000000",
                    "event": "settled",
                    "tx_hash": "0x" + "11" * 32,
                }).encode())
            req.add_header("Content-Type", "application/json")
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
            from primer_vault.services import server as server_module
            server_module._signing_service = None

        assert locked_status == unlocked_status, (
            "an unauthenticated POST /callback returns "
            f"{locked_status} when the wallet is locked and {unlocked_status} "
            "when it is unlocked, which is a pollable signal that the keys are "
            "in memory")
