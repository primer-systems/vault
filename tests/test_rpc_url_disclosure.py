"""The configured RPC endpoint does not leave the machine in an API response.

Hosted nodes put the account's API key in the URL path, so the endpoint is a
credential rather than an address. Connection and HTTP errors raised against it
name the full URL, so any path that returns an underlying exception's text to a
caller must scrub the endpoint first. Covered here: /trade and /receipt/{id}.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Port 9 (discard) with nothing listening: the call fails without touching the
# network, and the failure carries the URL.
SECRET_RPC = "http://127.0.0.1:9/v2/SUPER-SECRET-RPC-KEY"
SECRET = "SUPER-SECRET-RPC-KEY"


@pytest.fixture
def temp_data_dir(tmp_path):
    """The conftest `core` fixture wants this; each test file supplies its own."""
    return tmp_path


@pytest.fixture
def core_with_secret_rpc(core):
    core.settings_manager.set_rpc_endpoint(4663, SECRET_RPC)
    return core


def _address(core):
    addresses = core.get_wallet_addresses()
    if not addresses:
        seed = core.create_seed(word_count=12)
        core.add_address_from_seed(seed["seed_id"], 0, "A")
        addresses = core.get_wallet_addresses()
    return addresses[0]["address"]


class TestTradeResponse:
    """The calling agent holds a spending credential, not a licence to read the
    user's node subscription."""

    def test_a_failed_quote_does_not_hand_the_agent_the_rpc_url(self, core_with_secret_rpc):
        from primer_vault.models.policy import TradingRules

        core = core_with_secret_rpc
        policy = core.create_policy(
            name="P", networks=[4663], daily_limit_micro=10_000_000,
            trading_rules=TradingRules(enabled=True, per_trade_max_usd=1000.0,
                                       daily_volume_limit_usd=1000.0))
        agent, token = core.create_agent(name="Bot", auth_mode="bearer")
        core.commission_agent(agent.code, policy.id, _address(core))

        result = core._trading_service.handle_trade_request(
            agent.id,
            {"token_in": "0x" + "11" * 20, "token_out": "0x" + "22" * 20,
             "amount_in": "1", "fee_tier": 3000, "max_slippage_bps": 100,
             "chain_id": 4663},
            signature=token,
        )

        assert SECRET not in json.dumps(result), (
            "the /trade response carries the configured RPC URL: "
            + str(result.get("reason"))[:300]
        )


class TestReceiptEndpoint:
    """/receipt/{id} authenticates nobody - it asks only for the transaction
    id - so anything it publishes is published to whoever holds that id."""

    def test_an_unverifiable_settlement_does_not_publish_the_rpc_url(
            self, core_with_secret_rpc):
        from primer_vault.models.transaction import Transaction

        core = core_with_secret_rpc
        policy = core.create_policy(name="P", networks=[4663],
                                    daily_limit_micro=10_000_000)
        agent, _token = core.create_agent(name="Bot", auth_mode="bearer")
        address = _address(core)
        core.commission_agent(agent.code, policy.id, address)

        tx = Transaction.create(
            agent_id=agent.id, agent_name=agent.name, agent_code=agent.code,
            amount_micro=1_000, recipient="0x" + "33" * 20,
            network="eip155:4663", resource="https://api.example/thing",
            request_url="https://api.example/thing",
            x402_data={}, wallet_address=address, wallet_id="A001")
        tx.mark_signed(address, "A001", True)
        tx.mark_settled("0x" + "44" * 32)
        core._policy_store.add_transaction(tx)

        # The node cannot be reached, so verification records why.
        core._signing_service._verify_transaction_sync(tx)

        receipt = core._signing_service.get_receipt(tx.id)

        assert SECRET not in json.dumps(receipt), (
            "the AP2 receipt carries the configured RPC URL: "
            + str(receipt.get("settlement", {}).get("verification"))[:300]
        )
