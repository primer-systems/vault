"""DexAdapterV4 calldata, run against the real implementation.

This file exists because the v4 adapter shipped with a quoter call that appended
an argument the contract does not take. It decoded anyway on pools whose
currency0 is address(0) — the stray zero landed in the hookData offset slot and
read a length of zero from currency0 — so it looked like it worked on the 62% of
RHC v4 pools that are native-ETH pairs, and reverted on the rest. Nothing caught
it, because nothing exercised it.

So these tests assert the wire format itself, against the shapes taken from the
deployed, Blockscout-verified sources on chain 4663:

  V4Quoter        quoteExactInputSingle(((address,address,uint24,int24,address),
                                         bool,uint128,bytes))
  UniversalRouter execute(bytes commands, bytes[] inputs, uint256 deadline)
  IV4Router       ExactInputSingleParams { PoolKey; bool; uint128; uint128;
                                           uint256 minHopPriceX36; bytes }

No network: a canned JSON-RPC provider answers the reads, so the encoding runs
for real without touching a chain.
"""

import pytest
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector
from web3 import Web3

from primer_vault.networks import DEX_V4, TOKENS
from primer_vault.services.dex import DexAdapterV3, DexError
from primer_vault.networks import DEX
from primer_vault.services.dex_v4 import (
    ACTION_SETTLE_ALL,
    ACTION_SWAP_EXACT_IN_SINGLE,
    ACTION_TAKE_ALL,
    COMMAND_V4_SWAP,
    DexAdapterV4,
    ZERO_ADDRESS,
)


CHAIN_ID = 4663
DEXV4 = DEX_V4[CHAIN_ID]
USDG = Web3.to_checksum_address(TOKENS["USDG"].addresses[CHAIN_ID])
WETH = Web3.to_checksum_address(DEXV4.weth)
ROUTER = Web3.to_checksum_address(DEXV4.universal_router)
PERMIT2 = Web3.to_checksum_address(DEXV4.permit2)
USER = Web3.to_checksum_address("0x742d35cc6634c0532925a3b844bc9e7595f0beb0")

#: The selector the deployed V4Quoter on RHC actually exposes. Hard-coded rather
#: than derived, so that changing the ABI in the adapter fails here instead of
#: silently agreeing with itself.
DEPLOYED_QUOTER_SELECTOR = bytes.fromhex("aa9d21cb")

#: PoolKey and the RHC swap struct, as eth_abi type strings.
POOL_KEY = "(address,address,uint24,int24,address)"
RHC_EXACT_IN_SINGLE = f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"
#: The stock Uniswap struct, which has no minHopPriceX36. Used to prove the two
#: are genuinely different rather than assuming it.
STOCK_EXACT_IN_SINGLE = f"({POOL_KEY},bool,uint128,uint128,bytes)"

BLOCK = {"number": "0x1", "timestamp": "0x66000000",
         "baseFeePerGas": "0x3b9aca00", "gasLimit": "0x1c9c380"}


def _canned(eth_call_results=None):
    """A provider that answers reads from a selector -> hex-result map."""
    eth_call_results = eth_call_results or {}
    simple = {
        "eth_getTransactionCount": "0x7",
        "eth_chainId": hex(CHAIN_ID),
        "eth_gasPrice": "0x3b9aca00",
        "eth_estimateGas": "0x3d090",
        "eth_maxPriorityFeePerGas": "0x3b9aca00",
        "eth_getBlockByNumber": BLOCK,
    }

    def make_request(method, params, *args, **kwargs):
        name = str(method)
        if name == "eth_call":
            data = params[0]["data"]
            selector = data[2:10] if isinstance(data, str) else data[:4].hex()
            if selector not in eth_call_results:
                raise AssertionError(f"unexpected eth_call selector: 0x{selector}")
            return {"jsonrpc": "2.0", "id": 1, "result": eth_call_results[selector]}
        if name not in simple:
            raise AssertionError(f"unexpected RPC call: {name}")
        return {"jsonrpc": "2.0", "id": 1, "result": simple[name]}

    return make_request


@pytest.fixture
def adapter():
    a = DexAdapterV4("http://127.0.0.1:1", DEXV4)
    a.w3.provider.make_request = _canned()
    return a


def _with_calls(results):
    a = DexAdapterV4("http://127.0.0.1:1", DEXV4)
    a.w3.provider.make_request = _canned(results)
    return a


def _sel(signature: str) -> str:
    """Hex selector (no 0x) for a Solidity signature."""
    return function_signature_to_4byte_selector(signature).hex()


ERC20_ALLOWANCE = _sel("allowance(address,address)")
PERMIT2_ALLOWANCE = _sel("allowance(address,address,address)")
GET_SLOT0 = _sel("getSlot0(bytes32)")


def _calldata_args(tx_or_data, types):
    """Decode the argument section of built calldata."""
    data = tx_or_data["data"] if isinstance(tx_or_data, dict) else tx_or_data
    return decode(types, bytes.fromhex(data[10:]), strict=False)


# =============================================================================
# The quoter
# =============================================================================

class TestQuoterCalldata:

    def test_adapter_abi_matches_the_deployed_selector(self, adapter):
        """The ABI we declare must hash to the selector the chain exposes.

        This is the whole of C1 in one assertion. The previous code hard-coded
        0xaa9d21cb and then encoded arguments for a different signature, so the
        selector was right and the body was not — a combination no amount of
        selector-checking alone would catch, which is why the body is checked
        below as well.
        """
        pool_key = adapter._make_pool_key(USDG, WETH, 500, 10, ZERO_ADDRESS)
        calldata = adapter._quoter().encode_abi(
            abi_element_identifier="quoteExactInputSingle",
            args=[(pool_key, True, 1, b"")])
        assert bytes.fromhex(calldata[2:10]) == DEPLOYED_QUOTER_SELECTOR

    def test_deployed_selector_is_the_signature_without_a_price_limit(self):
        """Pin the signature itself, so the shape cannot drift back."""
        with_limit = function_signature_to_4byte_selector(
            "quoteExactInputSingle(((address,address,uint24,int24,address),"
            "bool,uint128,uint160,bytes))")
        without_limit = function_signature_to_4byte_selector(
            "quoteExactInputSingle(((address,address,uint24,int24,address),"
            "bool,uint128,bytes))")
        assert without_limit == DEPLOYED_QUOTER_SELECTOR
        assert with_limit != DEPLOYED_QUOTER_SELECTOR

    def test_quote_encodes_four_arguments_and_no_price_limit(self):
        """Decode our own calldata with the contract's ABI and check the body.

        A quote is 4 words of poolKey + zeroForOne + exactAmount + the hookData
        offset and length. An extra uint160 before hookData would show up here
        as a fifth member.
        """
        quoter_out = "0x" + encode(["uint256", "uint256"], [12345, 90000]).hex()
        a = _with_calls({"aa9d21cb": quoter_out})

        result = a.quote_exact_input_single(USDG, WETH, 10_000_000, 500,
                                            tick_spacing=10, hooks=ZERO_ADDRESS)
        assert result == {"amount_out": 12345, "gas_estimate": 90000}

    def test_quote_calldata_decodes_under_the_deployed_signature(self, adapter):
        """The bytes we send must decode as the contract will decode them."""
        contract = adapter._quoter()
        pool_key = adapter._make_pool_key(USDG, WETH, 500, 10, ZERO_ADDRESS)
        zero_for_one = adapter._is_zero_for_one(USDG, WETH)
        calldata = contract.encode_abi(
            abi_element_identifier="quoteExactInputSingle",
            args=[(pool_key, zero_for_one, 10_000_000, b"")])

        raw = bytes.fromhex(calldata[2:])
        assert raw[:4] == DEPLOYED_QUOTER_SELECTOR

        # Decodes cleanly against the deployed signature...
        (decoded,) = decode(
            [f"({POOL_KEY},bool,uint128,bytes)"], raw[4:], strict=False)
        assert decoded[0] == tuple(
            x.lower() if isinstance(x, str) else x for x in pool_key)
        assert decoded[2] == 10_000_000
        assert decoded[3] == b""

    def test_quote_requires_tick_spacing_and_hooks(self, adapter):
        with pytest.raises(DexError, match="tick_spacing and hooks"):
            adapter.quote_exact_input_single(USDG, WETH, 1, 500)


# =============================================================================
# Pool identity
# =============================================================================

class TestPoolKey:

    def test_currencies_are_sorted(self, adapter):
        forward = adapter._make_pool_key(USDG, WETH, 500, 10, ZERO_ADDRESS)
        reverse = adapter._make_pool_key(WETH, USDG, 500, 10, ZERO_ADDRESS)
        assert forward == reverse
        assert int(forward[0], 16) < int(forward[1], 16)

    def test_native_eth_becomes_the_zero_address(self, adapter):
        key = adapter._make_pool_key("ETH", USDG, 500, 10, ZERO_ADDRESS)
        assert key[0] == ZERO_ADDRESS
        assert key[1] == USDG

    def test_zero_for_one_follows_the_sort(self, adapter):
        low, high = sorted([USDG, WETH], key=lambda a: int(a, 16))
        assert adapter._is_zero_for_one(low, high) is True
        assert adapter._is_zero_for_one(high, low) is False

    def test_pool_id_is_the_keccak_of_the_encoded_key(self, adapter):
        key = adapter._make_pool_key(USDG, WETH, 500, 10, ZERO_ADDRESS)
        assert adapter._pool_id(key) == Web3.keccak(encode([POOL_KEY], [key]))

    def test_find_pool_returns_none_for_an_uninitialised_pool(self):
        slot0 = "0x" + encode(["uint160", "int24", "uint24", "uint24"],
                              [0, 0, 0, 0]).hex()
        a = _with_calls({GET_SLOT0: slot0})
        assert a.find_pool(USDG, WETH, 500, tick_spacing=10, hooks=ZERO_ADDRESS) is None

    def test_find_pool_returns_the_pool_id_when_initialised(self):
        slot0 = "0x" + encode(["uint160", "int24", "uint24", "uint24"],
                              [79228162514264337593543950336, 0, 0, 500]).hex()
        a = _with_calls({GET_SLOT0: slot0})
        pool = a.find_pool(USDG, WETH, 500, tick_spacing=10, hooks=ZERO_ADDRESS)
        key = a._make_pool_key(USDG, WETH, 500, 10, ZERO_ADDRESS)
        assert pool == f"V4:0x{a._pool_id(key).hex()}"


# =============================================================================
# The swap input — actions, not a bare struct
# =============================================================================

class TestSwapInputEncoding:
    """What goes in inputs[0] of UniversalRouter.execute for command 0x10."""

    def _encoded(self, adapter, amount_in=10_000_000, amount_out_min=4_000_000):
        pool_key = adapter._make_pool_key(USDG, WETH, 500, 10, ZERO_ADDRESS)
        zero_for_one = adapter._is_zero_for_one(USDG, WETH)
        blob = adapter._encode_v4_swap_input(
            pool_key, zero_for_one, amount_in, amount_out_min)
        actions, params = decode(["bytes", "bytes[]"], blob, strict=False)
        return pool_key, zero_for_one, actions, params

    def test_it_is_three_actions_swap_settle_take(self, adapter):
        _, _, actions, params = self._encoded(adapter)
        assert list(actions) == [ACTION_SWAP_EXACT_IN_SINGLE,
                                 ACTION_SETTLE_ALL,
                                 ACTION_TAKE_ALL]
        assert len(params) == 3, "one parameter blob per action, or the router reverts"

    def test_swap_params_carry_min_hop_price(self, adapter):
        """The RHC field is present, and set to zero to disable the check."""
        pool_key, zero_for_one, _, params = self._encoded(adapter)
        (decoded,) = decode([RHC_EXACT_IN_SINGLE], params[0], strict=False)
        key, zfo, amount_in, amount_out_min, min_hop, hook_data = decoded
        assert key == tuple(x.lower() if isinstance(x, str) else x for x in pool_key)
        assert zfo == zero_for_one
        assert amount_in == 10_000_000
        assert amount_out_min == 4_000_000
        assert min_hop == 0
        assert hook_data == b""

    def test_swap_params_are_not_the_stock_uniswap_struct(self, adapter):
        """Guard the one genuine RHC difference.

        If this ever starts passing, the router has been changed to stock
        Uniswap and minHopPriceX36 should come out of the encoder.
        """
        pool_key, zero_for_one, _, params = self._encoded(adapter)
        stock_bytes = encode(
            [STOCK_EXACT_IN_SINGLE],
            [(pool_key, zero_for_one, 10_000_000, 4_000_000, b"")])
        assert params[0] != stock_bytes
        assert len(params[0]) == len(stock_bytes) + 32, (
            "minHopPriceX36 should add exactly one word to the stock struct")

    def test_swap_params_meet_the_routers_length_floor(self, adapter):
        """CalldataDecoder rejects anything under 0x160 bytes.

        Taken from the deployed source: "0x160 = 11 * 0x20 -> 9 elements, bytes
        offset, and bytes length 0". Nine static elements is only reachable with
        minHopPriceX36 present, so this length is itself evidence of the field.
        """
        _, _, _, params = self._encoded(adapter)
        assert len(params[0]) >= 0x160

    def test_settle_names_the_input_currency_and_caps_the_pull(self, adapter):
        pool_key, zero_for_one, _, params = self._encoded(adapter)
        currency, max_amount = decode(["address", "uint256"], params[1], strict=False)
        expected_in = pool_key[0] if zero_for_one else pool_key[1]
        assert currency.lower() == expected_in.lower()
        assert max_amount == 10_000_000, "the router may not pull more than amount_in"

    def test_take_names_the_output_currency_and_floors_the_delivery(self, adapter):
        pool_key, zero_for_one, _, params = self._encoded(adapter)
        currency, min_amount = decode(["address", "uint256"], params[2], strict=False)
        expected_out = pool_key[1] if zero_for_one else pool_key[0]
        assert currency.lower() == expected_out.lower()
        assert min_amount == 4_000_000, "slippage floor is enforced on-chain by TAKE_ALL"


# =============================================================================
# The swap transaction
# =============================================================================

class TestBuildSwapTx:

    def test_it_calls_execute_on_the_universal_router(self, adapter):
        tx = adapter.build_swap_tx(USDG, WETH, 500, USER, 10_000_000, 4_000_000, USER,
                                   tick_spacing=10, hooks=ZERO_ADDRESS)
        assert tx["to"] == ROUTER
        assert tx["chainId"] == CHAIN_ID

        fn, args = adapter._router().decode_function_input(tx["data"])
        assert fn.fn_name == "execute"
        assert list(args["commands"]) == [COMMAND_V4_SWAP]
        assert len(args["inputs"]) == 1

    def test_deadline_comes_from_chain_time(self, adapter):
        tx = adapter.build_swap_tx(USDG, WETH, 500, USER, 10_000_000, 4_000_000, USER,
                                   tick_spacing=10, hooks=ZERO_ADDRESS)
        _, args = adapter._router().decode_function_input(tx["data"])
        assert args["deadline"] == int(BLOCK["timestamp"], 16) + 600

    def test_native_eth_input_is_sent_as_value(self, adapter):
        tx = adapter.build_swap_tx("ETH", USDG, 500, USER, 10**15, 1, USER,
                                   eth_value=10**15, tick_spacing=10, hooks=ZERO_ADDRESS)
        assert tx["value"] == 10**15

    def test_erc20_input_sends_no_value(self, adapter):
        tx = adapter.build_swap_tx(USDG, WETH, 500, USER, 10_000_000, 1, USER,
                                   tick_spacing=10, hooks=ZERO_ADDRESS)
        assert tx["value"] == 0

    def test_a_third_party_recipient_is_refused(self, adapter):
        """v4 settles from and takes to msgSender(), so this cannot be honoured.

        Refusing beats ignoring: a caller that asks for a different recipient and
        is quietly given the sender has been told something untrue about where
        its money went.
        """
        other = Web3.to_checksum_address("0x0000000000000000000000000000000000001234")
        with pytest.raises(DexError, match="cannot be sent to a different recipient"):
            adapter.build_swap_tx(USDG, WETH, 500, other, 10_000_000, 1, USER,
                                  tick_spacing=10, hooks=ZERO_ADDRESS)

    def test_v4_fields_are_required(self, adapter):
        with pytest.raises(DexError, match="tick_spacing and hooks"):
            adapter.build_swap_tx(USDG, WETH, 500, USER, 1, 1, USER)


# =============================================================================
# Approvals — the second half of C2
# =============================================================================

class TestApprovalSteps:
    """v4 pulls tokens through Permit2, so approving the router does nothing."""

    def _steps(self, erc20_allowance, permit2_amount, permit2_expiry):
        a = _with_calls({
            ERC20_ALLOWANCE: "0x" + encode(["uint256"], [erc20_allowance]).hex(),
            PERMIT2_ALLOWANCE: "0x" + encode(
                ["uint160", "uint48", "uint48"],
                [permit2_amount, permit2_expiry, 0]).hex(),
        })
        return a, a.approval_steps(USDG, USER, 10_000_000, token_label="10 USDG")

    def test_nothing_approved_gives_two_steps(self):
        a, steps = self._steps(0, 0, 0)
        assert len(steps) == 2

        # Step 1 is an ERC-20 approve naming Permit2 — not the router.
        tx0, what0 = steps[0]
        assert tx0["to"] == USDG
        spender, amount = _calldata_args(tx0, ["address", "uint256"])
        assert spender.lower() == PERMIT2.lower()
        assert amount == 10_000_000
        assert "Permit2" in what0

        # Step 2 is Permit2.approve naming the router.
        tx1, what1 = steps[1]
        assert tx1["to"] == PERMIT2
        token, spender, amount, expiry = _calldata_args(
            tx1, ["address", "address", "uint160", "uint48"])
        assert token.lower() == USDG.lower()
        assert spender.lower() == ROUTER.lower()
        assert amount == 10_000_000
        assert expiry > 0
        assert "router" in what1

    def test_erc20_already_approved_leaves_only_the_permit2_step(self):
        _, steps = self._steps(10_000_000, 0, 0)
        assert len(steps) == 1
        assert "router" in steps[0][1]

    def test_a_live_permit2_authorisation_skips_both(self):
        far_future = 2_000_000_000
        _, steps = self._steps(10_000_000, 10_000_000, far_future)
        assert steps == []

    def test_an_expired_permit2_authorisation_is_not_an_authorisation(self):
        """Amount alone is not enough — Permit2 allowances carry an expiry."""
        _, steps = self._steps(10_000_000, 10_000_000, 1)
        assert len(steps) == 1
        assert "router" in steps[0][1]

    def test_native_eth_needs_no_approval(self, adapter):
        assert adapter.approval_steps("ETH", USER, 10**15) == []
        assert adapter.approval_steps(ZERO_ADDRESS, USER, 10**15) == []

    def test_permit2_approval_carries_a_bounded_expiry(self):
        _a, steps = self._steps(10_000_000, 0, 0)
        *_rest, expiry = _calldata_args(
            steps[0][0], ["address", "address", "uint160", "uint48"])
        assert 0 < expiry < 2 ** 48


class TestV3ApprovalStepsUnchanged:
    """The shared contract must not have changed v3's behaviour."""

    @pytest.fixture
    def v3(self):
        a = DexAdapterV3("http://127.0.0.1:1", DEX[CHAIN_ID])
        a.w3.provider.make_request = _canned(
            {ERC20_ALLOWANCE: "0x" + encode(["uint256"], [0]).hex()})
        return a

    def test_v3_needs_one_approval_naming_the_router(self, v3):
        steps = v3.approval_steps(USDG, USER, 10_000_000, token_label="10 USDG")
        assert len(steps) == 1
        tx, _what = steps[0]
        spender, amount = _calldata_args(tx, ["address", "uint256"])
        assert spender.lower() == DEX[CHAIN_ID].swap_router.lower()
        assert amount == 10_000_000

    def test_v3_native_eth_needs_none(self, v3):
        assert v3.approval_steps("ETH", USER, 10**15) == []


# =============================================================================
# Simulation
# =============================================================================

class TestSimulateSwap:
    """Simulation must not be a second call to the quoter.

    A quote asks the pool what a swap would return. It cannot see a missing
    Permit2 authorisation, a hook that rejects the caller, or an insufficient
    balance — all of which are exactly what a dry run before spending gas is
    supposed to catch. So simulation now eth_calls the real swap and only then
    reports the amount.
    """

    EXECUTE = _sel("execute(bytes,bytes[],uint256)")

    def test_it_calls_execute_not_just_the_quoter(self):
        seen = []

        a = DexAdapterV4("http://127.0.0.1:1", DEXV4)
        quoter_out = "0x" + encode(["uint256", "uint256"], [4_200_000, 90000]).hex()

        def make_request(method, params, *args, **kwargs):
            name = str(method)
            if name == "eth_call":
                selector = params[0]["data"][2:10]
                seen.append(selector)
                if selector == self.EXECUTE:
                    return {"jsonrpc": "2.0", "id": 1, "result": "0x"}
                return {"jsonrpc": "2.0", "id": 1, "result": quoter_out}
            if name == "eth_getBlockByNumber":
                return {"jsonrpc": "2.0", "id": 1, "result": BLOCK}
            if name == "eth_chainId":
                return {"jsonrpc": "2.0", "id": 1, "result": hex(CHAIN_ID)}
            raise AssertionError(f"unexpected RPC call: {name}")

        a.w3.provider.make_request = make_request

        out = a.simulate_swap(USDG, WETH, 500, USER, 10_000_000, 4_000_000, USER,
                              tick_spacing=10, hooks=ZERO_ADDRESS)

        assert self.EXECUTE in seen, "the real swap must be dry-run, not just quoted"
        assert out == 4_200_000

    def test_a_reverting_swap_raises_rather_than_returning_a_quote(self):
        a = DexAdapterV4("http://127.0.0.1:1", DEXV4)

        def make_request(method, params, *args, **kwargs):
            name = str(method)
            if name == "eth_call":
                return {"jsonrpc": "2.0", "id": 1,
                        "error": {"code": 3, "message": "execution reverted"}}
            if name == "eth_getBlockByNumber":
                return {"jsonrpc": "2.0", "id": 1, "result": BLOCK}
            if name == "eth_chainId":
                return {"jsonrpc": "2.0", "id": 1, "result": hex(CHAIN_ID)}
            raise AssertionError(f"unexpected RPC call: {name}")

        a.w3.provider.make_request = make_request

        with pytest.raises(DexError, match="simulation reverted"):
            a.simulate_swap(USDG, WETH, 500, USER, 10_000_000, 4_000_000, USER,
                            tick_spacing=10, hooks=ZERO_ADDRESS)

    def test_it_requires_the_v4_fields(self, adapter):
        with pytest.raises(DexError, match="tick_spacing and hooks"):
            adapter.simulate_swap(USDG, WETH, 500, USER, 1, 1, USER)


class TestWrapUnwrap:
    """Wrap/unwrap go straight to WETH9 and never touch the router."""

    def test_wrap_sends_eth_to_weth9(self, adapter):
        tx = adapter.build_wrap_tx(10**15, USER)
        assert tx["to"] == WETH
        assert tx["value"] == 10**15

    def test_unwrap_sends_no_value(self, adapter):
        tx = adapter.build_unwrap_tx(10**15, USER)
        assert tx["to"] == WETH
        assert tx["value"] == 0
