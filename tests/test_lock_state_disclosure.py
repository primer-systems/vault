"""The agent API does not disclose the wallet's lock state.

Whether the keys are in memory is treated as sensitive: the Admin API's /status
withholds `wallet_unlocked` for that reason. The agent API is reachable by the
same callers, and by LAN hosts when the server is started with allow_lan, so a
caller presenting an agent id and a wrong credential must not be able to tell
the two states apart there either.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_data_dir(tmp_path):
    """The conftest `core` fixture wants this; each test file supplies its own."""
    return tmp_path


NEVER_VALID = "SIG:{ts}:" + "00" * 32


def _commissioned(core, auth_mode):
    """An agent commissioned against an address of the open wallet."""
    policy = core.create_policy(
        name=f"P-{auth_mode}",
        networks=[4663],
        daily_limit_micro=10_000_000,
        per_request_max_micro=1_000_000,
    )
    agent, _secret = core.create_agent(name=f"Bot-{auth_mode}", auth_mode=auth_mode)
    addresses = core.get_wallet_addresses()
    if not addresses:
        seed = core.create_seed(word_count=12)
        core.add_address_from_seed(seed["seed_id"], 0, "A")
        addresses = core.get_wallet_addresses()
    core.commission_agent(agent.code, policy.id, addresses[0]["address"])
    return agent


def _sign_reply(core, agent_id):
    """What /sign answers a caller holding a credential that can never verify."""
    return core._signing_service.handle_sign_request(
        agent_id,
        NEVER_VALID.format(ts=int(time.time())),
        x402_data={},
    )


def _trade_reply(core, agent_id):
    """What /trade answers the same caller."""
    return core._trading_service.handle_trade_request(
        agent_id,
        {"token_in": "0x" + "11" * 20, "token_out": "0x" + "22" * 20,
         "amount_in": "1000"},
        signature=NEVER_VALID.format(ts=int(time.time())),
    )


class TestWrongCredentialAnswersTheSameEitherWay:
    """A caller with no valid credential must not be able to watch the lock."""

    def test_sign_hmac(self, core):
        agent = _commissioned(core, "hmac")

        unlocked = _sign_reply(core, agent.id)
        core.lock_wallet()
        locked = _sign_reply(core, agent.id)

        assert locked["code"] == unlocked["code"], (
            f"unlocked -> {unlocked['code']}, locked -> {locked['code']}: "
            "an unauthenticated caller can tell the wallet's lock state apart"
        )

    def test_trade_hmac(self, core):
        agent = _commissioned(core, "hmac")

        unlocked = _trade_reply(core, agent.id)
        core.lock_wallet()
        locked = _trade_reply(core, agent.id)

        assert locked["code"] == unlocked["code"], (
            f"unlocked -> {unlocked['code']}, locked -> {locked['code']}: "
            "an unauthenticated caller can tell the wallet's lock state apart"
        )


class TestBearerAgentsDoNotLeakIt:
    """Contrast, not a bug hunt: bearer verifies before the wallet is consulted,
    so the same probe learns nothing. This is the behaviour the HMAC path above
    departs from, and it is why the two tests differ."""

    def test_sign_bearer(self, core):
        agent = _commissioned(core, "bearer")

        unlocked = _sign_reply(core, agent.id)
        core.lock_wallet()
        locked = _sign_reply(core, agent.id)

        assert locked["code"] == unlocked["code"] == "AUTH_FAILED"
