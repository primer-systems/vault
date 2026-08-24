"""DexAdapterV3 transaction building, run against the real implementation.

The trading tests elsewhere substitute a fake adapter, which is the right choice
for testing policy and approval flow but means the real build_* methods go
unexercised. What they depend on is web3's ABI encoding, whose spelling is tied
to the major version pinned in pyproject.toml - so a dependency bump can break
transaction building without a single policy test noticing.

These tests drive the genuine adapter against a canned JSON-RPC provider, so the
encoding and transaction scaffolding run for real without touching a network.
"""

import pytest
from web3 import Web3

from primer_vault.networks import DEX, DEX_V4, TOKENS
from primer_vault.services.dex import DexAdapterV3


CHAIN_ID = 4663
USDG = TOKENS["USDG"].addresses[CHAIN_ID]
WETH = DEX[CHAIN_ID].weth
ROUTER = Web3.to_checksum_address(DEX[CHAIN_ID].swap_router)
USER = Web3.to_checksum_address("0x742d35cc6634c0532925a3b844bc9e7595f0beb0")

# Enough of the chain's read surface for web3 to fill transaction defaults.
# Real JSON-RPC method names, so this does not depend on web3 internals.
CANNED_RPC = {
    "eth_getTransactionCount": "0x7",
    "eth_chainId": hex(CHAIN_ID),
    "eth_gasPrice": "0x3b9aca00",
    "eth_estimateGas": "0x3d090",
    "eth_maxPriorityFeePerGas": "0x3b9aca00",
    "eth_getBlockByNumber": {
        "number": "0x1",
        "baseFeePerGas": "0x3b9aca00",
        "gasLimit": "0x1c9c380",
    },
}


@pytest.fixture
def adapter():
    """A real DexAdapterV3 whose provider answers from CANNED_RPC."""
    a = DexAdapterV3("http://127.0.0.1:1", DEX[CHAIN_ID])

    def make_request(method, params, *args, **kwargs):
        name = str(method)
        if name not in CANNED_RPC:
            raise AssertionError(f"unexpected RPC call: {name}")
        return {"jsonrpc": "2.0", "id": 1, "result": CANNED_RPC[name]}

    a.w3.provider.make_request = make_request
    return a


class TestSwapToEthTx:
    """The multicall path: swap token -> WETH, then unwrap WETH -> native ETH."""

    def test_builds_a_multicall_of_swap_then_unwrap(self, adapter):
        tx = adapter.build_swap_to_eth_tx(USDG, 500, USER, 10_000_000, 9_000_000, USER)

        assert tx["to"] == ROUTER
        assert tx["chainId"] == CHAIN_ID
        assert tx["value"] == 0

        outer, args = adapter.router.decode_function_input(tx["data"])
        assert outer.fn_name == "multicall"

        inner = [adapter.router.decode_function_input(blob) for blob in args["data"]]
        assert [fn.fn_name for fn, _ in inner] == ["exactInputSingle", "unwrapWETH9"]

    def test_weth_goes_to_the_router_and_eth_to_the_user(self, adapter):
        """The two recipients differ, and that is the point of the multicall.

        The swap leg must leave WETH with the router so unwrapWETH9 has a balance
        to convert; only the unwrap leg pays out to the user. Sending the swap
        output straight to the user would leave the unwrap with nothing.
        """
        tx = adapter.build_swap_to_eth_tx(USDG, 500, USER, 10_000_000, 9_000_000, USER)
        _, args = adapter.router.decode_function_input(tx["data"])
        (_, swap), (_, unwrap) = [
            adapter.router.decode_function_input(blob) for blob in args["data"]
        ]

        assert swap["params"]["recipient"] == ROUTER
        assert unwrap["recipient"] == USER

        # The swap sells into WETH, not the caller's requested native ETH.
        assert swap["params"]["tokenIn"] == Web3.to_checksum_address(USDG)
        assert swap["params"]["tokenOut"] == Web3.to_checksum_address(WETH)

        # The agent's slippage floor is applied on both legs.
        assert swap["params"]["amountOutMinimum"] == 9_000_000
        assert unwrap["amountMinimum"] == 9_000_000


class TestOtherBuildPaths:
    """The remaining real build_* methods, which the fake adapters also stub."""

    def test_build_swap_tx(self, adapter):
        tx = adapter.build_swap_tx(USDG, WETH, 500, USER, 10_000_000, 9_000_000, USER)
        fn, _ = adapter.router.decode_function_input(tx["data"])
        assert fn.fn_name == "exactInputSingle"
        assert tx["value"] == 0

    def test_build_swap_tx_carries_eth_value_for_native_input(self, adapter):
        tx = adapter.build_swap_tx(
            WETH, USDG, 500, USER, 10**18, 1, USER, eth_value=10**18)
        assert tx["value"] == 10**18

    def test_build_approve_tx(self, adapter):
        tx = adapter.build_approve_tx(USDG, ROUTER, 10_000_000, USER)
        assert tx["to"] == Web3.to_checksum_address(USDG)
        assert tx["value"] == 0

    def test_build_wrap_tx_sends_the_eth_as_value(self, adapter):
        tx = adapter.build_wrap_tx(10**18, USER)
        assert tx["to"] == Web3.to_checksum_address(WETH)
        assert tx["value"] == 10**18

    def test_build_unwrap_tx_sends_no_value(self, adapter):
        tx = adapter.build_unwrap_tx(10**18, USER)
        assert tx["to"] == Web3.to_checksum_address(WETH)
        assert tx["value"] == 0


class TestConfiguredAddresses:
    """Every address Vault ships must be EIP-55 checksummed.

    These twelve values are what a careful user checks by hand against a block
    explorer or Uniswap's deployment list before trusting the app with funds.
    A checksummed address carries its own typo detection; a lowercase or
    mixed-but-wrong one does not, and reads as unverified.
    """

    @pytest.mark.parametrize("label, address", [
        ("v3.factory", DEX[CHAIN_ID].factory),
        ("v3.quoter_v2", DEX[CHAIN_ID].quoter_v2),
        ("v3.swap_router", DEX[CHAIN_ID].swap_router),
        ("v3.weth", DEX[CHAIN_ID].weth),
        ("v4.pool_manager", DEX_V4[CHAIN_ID].pool_manager),
        ("v4.position_manager", DEX_V4[CHAIN_ID].position_manager),
        ("v4.state_view", DEX_V4[CHAIN_ID].state_view),
        ("v4.quoter", DEX_V4[CHAIN_ID].quoter),
        ("v4.universal_router", DEX_V4[CHAIN_ID].universal_router),
        ("v4.permit2", DEX_V4[CHAIN_ID].permit2),
        ("v4.weth", DEX_V4[CHAIN_ID].weth),
        ("USDG", TOKENS["USDG"].addresses[CHAIN_ID]),
    ])
    def test_address_is_checksummed(self, label, address):
        assert address == Web3.to_checksum_address(address), (
            f"{label} is not EIP-55 checksummed")
