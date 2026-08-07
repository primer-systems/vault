"""
DEX adapter — Uniswap v4 interaction for the trading engine.

V4 uses a singleton PoolManager architecture. Pools are identified by PoolKey:
(currency0, currency1, fee, tickSpacing, hooks).

**RHC-SPECIFIC:** Robinhood Chain has a modified UniversalRouter with an extra
`minHopPriceX36` field in swap structs. Standard Uniswap SDK calldata will REVERT.
This adapter encodes the RHC-specific format.

RHC only supports exact-in swaps (the Bags hook reverts exact-out WETH swaps).
"""

from typing import Optional

from eth_abi import encode
from web3 import Web3

from ..networks import DexConfigV4, ETH_ADDRESS, is_native_eth
from .dex import DexAdapter, DexError, ERC20_ABI, WETH9_ABI, ETH_METADATA


# RHC V4Quoter uses a non-standard interface (selector 0xaa9d21cb) with flat tuple
# encoding instead of nested PoolKey struct. We encode calldata manually below.
RHC_QUOTER_SELECTOR = bytes.fromhex("aa9d21cb")

# UniversalRouter command constants
# V4_SWAP command (0x10) for single-hop exact input swap
COMMAND_V4_SWAP = 0x10

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _sort_tokens(token_a: str, token_b: str) -> tuple[str, str]:
    """Sort tokens for PoolKey (currency0 < currency1 by address)."""
    addr_a = Web3.to_checksum_address(token_a)
    addr_b = Web3.to_checksum_address(token_b)
    if int(addr_a, 16) < int(addr_b, 16):
        return addr_a, addr_b
    return addr_b, addr_a


class DexAdapterV4(DexAdapter):
    """Uniswap v4 read/build operations for one chain.

    RHC-specific: Uses modified UniversalRouter encoding with minHopPriceX36 field.
    """

    def __init__(self, rpc_url: str, dex: DexConfigV4):
        super().__init__(rpc_url, dex.weth)
        self.dex = dex

    def router_address(self) -> str:
        """Return the UniversalRouter address for approvals."""
        return self.dex.universal_router

    def _make_pool_key(
        self, token_in: str, token_out: str, fee: int, tick_spacing: int, hooks: str
    ) -> tuple:
        """Create a PoolKey tuple with tokens sorted correctly."""
        # Normalize native ETH to address(0)
        token_in_addr = ZERO_ADDRESS if is_native_eth(token_in) else token_in
        token_out_addr = ZERO_ADDRESS if is_native_eth(token_out) else token_out

        currency0, currency1 = _sort_tokens(token_in_addr, token_out_addr)
        return (
            Web3.to_checksum_address(currency0),
            Web3.to_checksum_address(currency1),
            int(fee),
            int(tick_spacing),
            Web3.to_checksum_address(hooks),
        )

    def _is_zero_for_one(self, token_in: str, token_out: str) -> bool:
        """Determine swap direction based on token ordering."""
        token_in_addr = ZERO_ADDRESS if is_native_eth(token_in) else token_in
        token_out_addr = ZERO_ADDRESS if is_native_eth(token_out) else token_out

        currency0, _ = _sort_tokens(token_in_addr, token_out_addr)
        # zeroForOne = true if selling currency0
        return currency0.lower() == Web3.to_checksum_address(token_in_addr).lower()

    # ---- Pool lookup via quoter --------------------------------------------

    def find_pool(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        tick_spacing: Optional[int] = None,
        hooks: Optional[str] = None,
    ) -> Optional[str]:
        """Check if pool exists by attempting a minimal quote.

        RHC's StateView uses a non-standard interface, so we use the quoter
        instead. If the quote succeeds, the pool exists and has liquidity.
        """
        if tick_spacing is None or hooks is None:
            raise DexError("V4 find_pool requires tick_spacing and hooks")

        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        zero_for_one = self._is_zero_for_one(token_in, token_out)

        params = encode(
            ["address", "address", "uint24", "int24", "address", "bool", "uint128", "uint160", "bytes"],
            [
                pool_key[0],
                pool_key[1],
                pool_key[2],
                pool_key[3],
                pool_key[4],
                zero_for_one,
                10**10,  # small but non-zero amount to check pool exists
                0,
                b"",
            ],
        )
        calldata = RHC_QUOTER_SELECTOR + params

        try:
            result = self.w3.eth.call({
                "to": Web3.to_checksum_address(self.dex.quoter),
                "data": "0x" + calldata.hex(),
            })
            amount_out = int.from_bytes(result[:32], "big")
            if amount_out == 0:
                return None
            return f"V4:{fee}:{tick_spacing}:{hooks[:10]}"
        except Exception:
            return None

    # ---- Quoting via V4Quoter ---------------------------------------------

    def quote_exact_input_single(
        self,
        token_in: str,
        token_out: str,
        amount_in_atomic: int,
        fee: int,
        tick_spacing: Optional[int] = None,
        hooks: Optional[str] = None,
    ) -> dict:
        """Get quote via V4Quoter. Returns {amount_out, gas_estimate}.

        RHC's V4Quoter uses a non-standard interface with flat tuple encoding
        (selector 0xaa9d21cb) and returns (amountOut, sqrtPriceX96After).
        """
        if tick_spacing is None or hooks is None:
            raise DexError("V4 quote requires tick_spacing and hooks")

        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        zero_for_one = self._is_zero_for_one(token_in, token_out)

        params = encode(
            ["address", "address", "uint24", "int24", "address", "bool", "uint128", "uint160", "bytes"],
            [
                pool_key[0],  # currency0
                pool_key[1],  # currency1
                pool_key[2],  # fee
                pool_key[3],  # tickSpacing
                pool_key[4],  # hooks
                zero_for_one,
                int(amount_in_atomic),
                0,  # sqrtPriceLimitX96 (0 = no limit)
                b"",  # hookData
            ],
        )
        calldata = RHC_QUOTER_SELECTOR + params

        try:
            result = self.w3.eth.call({
                "to": Web3.to_checksum_address(self.dex.quoter),
                "data": "0x" + calldata.hex(),
            })
            amount_out = int.from_bytes(result[:32], "big")
            return {
                "amount_out": amount_out,
                "gas_estimate": 150000,
            }
        except Exception as e:
            raise DexError(
                f"V4 quote failed ({token_in}->{token_out} fee {fee}): {e}"
            ) from e

    # ---- Swap execution via UniversalRouter (RHC-specific) ----------------

    def _encode_v4_swap_calldata(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        tick_spacing: int,
        hooks: str,
        amount_in: int,
        amount_out_min: int,
    ) -> bytes:
        """Encode V4 swap params with RHC-specific minHopPriceX36 field.

        RHC's ExactInputSingleParams struct:
        - PoolKey poolKey
        - bool zeroForOne
        - uint128 amountIn
        - uint128 amountOutMinimum
        - uint160 sqrtPriceLimitX96
        - uint256 minHopPriceX36      <- RHC-specific (set to 0)
        - bytes hookData
        """
        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        zero_for_one = self._is_zero_for_one(token_in, token_out)

        # Encode the ExactInputSingleParams struct
        # Using eth_abi to encode the tuple
        encoded_params = encode(
            [
                "(address,address,uint24,int24,address)",  # PoolKey
                "bool",  # zeroForOne
                "uint128",  # amountIn
                "uint128",  # amountOutMinimum
                "uint160",  # sqrtPriceLimitX96
                "uint256",  # minHopPriceX36 (RHC-specific)
                "bytes",  # hookData
            ],
            [
                pool_key,
                zero_for_one,
                int(amount_in),
                int(amount_out_min),
                0,  # sqrtPriceLimitX96 (0 = no limit)
                0,  # minHopPriceX36 (RHC-specific, set to 0)
                b"",  # hookData
            ],
        )

        return encoded_params

    def simulate_swap(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        recipient: str,
        amount_in_atomic: int,
        amount_out_min: int,
        sender: str,
        eth_value: int = 0,
        tick_spacing: Optional[int] = None,
        hooks: Optional[str] = None,
    ) -> int:
        """Simulate V4 swap via quote (V4 doesn't support direct eth_call simulation)."""
        if tick_spacing is None or hooks is None:
            raise DexError("V4 simulate_swap requires tick_spacing and hooks")

        # For V4, we use the quoter for simulation since the router encoding is complex
        quote = self.quote_exact_input_single(
            token_in, token_out, amount_in_atomic, fee, tick_spacing, hooks
        )

        if quote["amount_out"] < amount_out_min:
            raise DexError(
                f"V4 simulation: output {quote['amount_out']} < min {amount_out_min}"
            )

        return quote["amount_out"]

    def build_swap_tx(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        recipient: str,
        amount_in_atomic: int,
        amount_out_min: int,
        sender: str,
        gas: Optional[int] = None,
        eth_value: int = 0,
        tick_spacing: Optional[int] = None,
        hooks: Optional[str] = None,
    ) -> dict:
        """Build an unsigned V4 swap via UniversalRouter.

        Uses RHC-specific encoding with minHopPriceX36 field.
        """
        if tick_spacing is None or hooks is None:
            raise DexError("V4 build_swap_tx requires tick_spacing and hooks")

        # Encode the swap parameters
        swap_params = self._encode_v4_swap_calldata(
            token_in, token_out, fee, tick_spacing, hooks, amount_in_atomic, amount_out_min
        )

        # UniversalRouter execute(bytes commands, bytes[] inputs, uint256 deadline)
        # For V4_SWAP command (0x10), the input is the encoded ExactInputSingleParams
        commands = bytes([COMMAND_V4_SWAP])
        inputs = [swap_params]

        # Build the raw transaction data
        # execute(bytes,bytes[],uint256) selector: 0x3593564c
        # But we need the full ABI to encode properly
        router_abi = [
            {
                "inputs": [
                    {"name": "commands", "type": "bytes"},
                    {"name": "inputs", "type": "bytes[]"},
                    {"name": "deadline", "type": "uint256"},
                ],
                "name": "execute",
                "outputs": [],
                "stateMutability": "payable",
                "type": "function",
            }
        ]

        router = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.universal_router), abi=router_abi
        )

        # Deadline: current block timestamp + 10 minutes
        deadline = self.w3.eth.get_block("latest")["timestamp"] + 600

        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": int(eth_value),
        }
        if gas is not None:
            tx["gas"] = int(gas)

        return router.functions.execute(commands, inputs, deadline).build_transaction(tx)

    # ---- Approvals --------------------------------------------------------

    def build_approve_tx(
        self, token: str, spender: str, amount: int, sender: str, gas: Optional[int] = None
    ) -> dict:
        """Build an unsigned ERC-20 approve transaction.

        Note: V4 typically uses Permit2, but for simplicity we support standard approve.
        The spender should be the UniversalRouter for V4 swaps.
        """
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": 0,
        }
        if gas is not None:
            tx["gas"] = int(gas)
        return (
            self._erc20(token)
            .functions.approve(Web3.to_checksum_address(spender), int(amount))
            .build_transaction(tx)
        )

    # ---- Wrap/Unwrap (same as V3, uses WETH9) -----------------------------

    def _weth9(self):
        """Return a contract instance for the WETH9 contract."""
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.weth), abi=WETH9_ABI
        )

    def build_wrap_tx(self, amount: int, sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned WETH9.deposit() transaction (ETH -> WETH)."""
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": int(amount),
        }
        if gas is not None:
            tx["gas"] = int(gas)
        return self._weth9().functions.deposit().build_transaction(tx)

    def build_unwrap_tx(self, amount: int, sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned WETH9.withdraw() transaction (WETH -> ETH)."""
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": 0,
        }
        if gas is not None:
            tx["gas"] = int(gas)
        return self._weth9().functions.withdraw(int(amount)).build_transaction(tx)
