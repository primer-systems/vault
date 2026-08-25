"""The agent read surface: an agent can learn the address it signs from and
what that address holds, without keys.

  - /mandate now returns `wallet_address` (and `wallet_id`) in its live response,
    so an agent no longer has to infer its own address from a rejection message.
  - /balances returns the address plus on-chain balances, behind the same
    credential as /mandate, and degrades gracefully when the block explorer is
    down.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_data_dir():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bearer_agent(core):
    """An active bearer agent bound to a real address in the open wallet.
    Bearer so the tests can present a valid credential (the token itself)."""
    seed = core.create_seed(word_count=12)
    assert seed.get("success"), seed
    core.add_address_from_seed(seed["seed_id"], 0, "probe")
    address = core.get_wallet_addresses()[0]["address"]

    policy = core.create_policy(
        name="ReadProbe", networks=[4663], daily_limit_micro=10_000_000,
        per_request_max_micro=1_000_000, auto_approve_below_micro=100_000)
    agent, token = core.create_agent(name="ReadProbeAgent", auth_mode="bearer")
    core.commission_agent(agent.code, policy.id, address)
    return core.get_agent_by_id(agent.id), token, address


def test_mandate_returns_the_wallet_address(core, bearer_agent):
    agent, token, address = bearer_agent
    r = core._signing_service.handle_get_mandate(agent.id, token)
    assert r["status"] == "ok", r
    assert r["wallet_address"] == address
    assert r["wallet_id"], "expected the address-book id (A001-style) alongside"


def test_balances_returns_the_address_and_holdings(core, bearer_agent):
    agent, token, address = bearer_agent
    from primer_vault.networks import Balance

    core._signing_service.set_balance_provider(lambda _a: [
        Balance(symbol="ETH", name="Ether", raw=10 ** 18, decimals=18,
                formatted=1.0, is_native=True),
        Balance(symbol="USDG", name="Global Dollar", raw=5_000_000, decimals=6,
                formatted=5.0, token_address="0x" + "5f" * 20, is_native=False),
    ])

    r = core._signing_service.handle_get_balances(agent.id, token)
    assert r["status"] == "ok", r
    assert r["wallet_address"] == address
    assert {b["symbol"] for b in r["balances"]} == {"ETH", "USDG"}
    eth = next(b for b in r["balances"] if b["symbol"] == "ETH")
    assert eth["is_native"] and eth["raw"] == str(10 ** 18)  # raw is a string


def test_balances_survive_a_down_indexer(core, bearer_agent):
    agent, token, address = bearer_agent

    def boom(_a):
        raise RuntimeError("blockscout down")
    core._signing_service.set_balance_provider(boom)

    r = core._signing_service.handle_get_balances(agent.id, token)
    # Not an error: the agent still learns its address, and a down explorer is
    # not the agent's fault.
    assert r["status"] == "ok", r
    assert r["wallet_address"] == address
    assert r["balances"] is None
    assert "balances_error" in r


def test_balances_require_a_valid_credential(core, bearer_agent):
    agent, _token, _address = bearer_agent
    r = core._signing_service.handle_get_balances(agent.id, "AT_not_the_real_token")
    assert r["status"] == "error"
    assert r["code"] == "AUTH_FAILED", r
