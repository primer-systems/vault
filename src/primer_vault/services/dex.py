"""
DEX adapters — Uniswap v3/v4 interaction for the trading engine.

Qt-free; used by the trading service in GUI, CLI, and headless modes.

DexAdapter is an abstract base class; DexAdapterV3 (this module) and DexAdapterV4
(dex_v4.py) implement it. Read paths (metadata, quote, simulate) are safe to call
against the live chain; only build_swap_tx/build_approve_tx produce transactions,
and those are still unsigned until the signing service handles them.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

from web3 import Web3

from ..networks import DexConfig, ETH_ADDRESS, is_native_eth, is_wrap_trade, is_unwrap_trade


ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"type": "address"}, {"type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"type": "address"}, {"type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "type": "function"},
]

FACTORY_ABI = [
    {"inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}],
     "name": "getPool", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]

QUOTER_V2_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}],
     "name": "quoteExactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                 {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]

# SwapRouter02: ExactInputSingleParams has NO deadline field (unlike SwapRouter v1).
SWAP_ROUTER_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}],
     "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}],
     "stateMutability": "payable", "type": "function"},
    # multicall bundles multiple calls atomically (for swap + unwrap)
    {"inputs": [{"name": "data", "type": "bytes[]"}],
     "name": "multicall", "outputs": [{"name": "results", "type": "bytes[]"}],
     "stateMutability": "payable", "type": "function"},
    # unwrapWETH9 converts router's WETH balance to ETH and sends to recipient
    {"inputs": [{"name": "amountMinimum", "type": "uint256"}, {"name": "recipient", "type": "address"}],
     "name": "unwrapWETH9", "outputs": [],
     "stateMutability": "payable", "type": "function"},
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# WETH9 contract for wrap/unwrap operations
WETH9_ABI = [
    # deposit: payable, wraps msg.value ETH into WETH
    {"inputs": [], "name": "deposit", "outputs": [],
     "stateMutability": "payable", "type": "function"},
    # withdraw: unwraps WETH to ETH and sends to caller
    {"inputs": [{"name": "wad", "type": "uint256"}],
     "name": "withdraw", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]

# Native ETH metadata (no contract call needed)
ETH_METADATA = {
    "address": ETH_ADDRESS,
    "symbol": "ETH",
    "name": "Ether",
    "decimals": 18,
}


def to_atomic(amount: str, decimals: int) -> int:
    """Convert a human-decimal amount string to atomic integer units."""
    return int((Decimal(str(amount)) * (Decimal(10) ** decimals)).to_integral_value())


def from_atomic(amount: int, decimals: int) -> Decimal:
    """Convert atomic integer units to a human Decimal."""
    return Decimal(amount) / (Decimal(10) ** decimals)


class DexError(Exception):
    """DEX interaction failed (no pool, revert, RPC error)."""


class DexAdapter(ABC):
    """Abstract base class for DEX adapters (V3, V4).

    Concrete implementations handle version-specific logic (pool lookup, quoting,
    swap encoding). Common functionality (ERC-20 metadata, signing) lives here.
    """

    def __init__(self, rpc_url: str, weth: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._weth = weth

    @property
    def weth(self) -> str:
        """Return the WETH address for this chain."""
        return self._weth

    # ---- ERC-20 metadata (shared) ----------------------------------------

    def _erc20(self, token: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

    def token_metadata(self, token: str) -> dict:
        """Return {symbol, name, decimals} for a token.

        For native ETH (address(0) or 'ETH'), returns static metadata without RPC call.
        """
        if is_native_eth(token):
            return dict(ETH_METADATA)
        c = self._erc20(token)
        try:
            return {
                "address": Web3.to_checksum_address(token),
                "symbol": c.functions.symbol().call(),
                "name": c.functions.name().call(),
                "decimals": int(c.functions.decimals().call()),
            }
        except Exception as e:
            raise DexError(f"could not read token metadata for {token}: {e}") from e

    def balance_of(self, token: str, owner: str) -> int:
        return self._erc20(token).functions.balanceOf(Web3.to_checksum_address(owner)).call()

    def native_balance(self, owner: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(owner))

    def allowance(self, token: str, owner: str, spender: str) -> int:
        return self._erc20(token).functions.allowance(
            Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)).call()

    def dex_chain_id(self) -> int:
        return self.w3.eth.chain_id

    # ---- Signing + submission (shared) -----------------------------------

    def sign_and_send(self, tx: dict, private_key: str) -> str:
        """Sign a built transaction and broadcast it. Returns the tx hash hex."""
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        signed = self.w3.eth.account.sign_transaction(tx, private_key)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:  # eth-account < 0.13 used camelCase
            raw = signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        return tx_hash.hex()

    def wait_for_receipt(self, tx_hash: str, timeout: float = 120.0):
        """Block until the transaction is mined; returns the receipt."""
        return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)

    # ---- Abstract methods (version-specific) -----------------------------

    @abstractmethod
    def find_pool(self, token_in: str, token_out: str, fee: int,
                  tick_spacing: Optional[int] = None, hooks: Optional[str] = None) -> Optional[str]:
        """Return pool address/identifier, or None if it doesn't exist.

        V3: Uses factory.getPool(tokenIn, tokenOut, fee).
        V4: Uses PoolKey(currency0, currency1, fee, tickSpacing, hooks).
        """
        pass

    @abstractmethod
    def quote_exact_input_single(self, token_in: str, token_out: str,
                                 amount_in_atomic: int, fee: int,
                                 tick_spacing: Optional[int] = None,
                                 hooks: Optional[str] = None) -> dict:
        """Independent re-quote. Returns {amount_out, gas_estimate, ...}.

        Raises DexError on revert (e.g. no liquidity).
        """
        pass

    @abstractmethod
    def simulate_swap(self, token_in: str, token_out: str, fee: int, recipient: str,
                      amount_in_atomic: int, amount_out_min: int, sender: str,
                      eth_value: int = 0, tick_spacing: Optional[int] = None,
                      hooks: Optional[str] = None) -> int:
        """Dry-run the swap via eth_call. Returns simulated amountOut or raises DexError."""
        pass

    @abstractmethod
    def build_swap_tx(self, token_in: str, token_out: str, fee: int, recipient: str,
                      amount_in_atomic: int, amount_out_min: int, sender: str,
                      gas: Optional[int] = None, eth_value: int = 0,
                      tick_spacing: Optional[int] = None,
                      hooks: Optional[str] = None) -> dict:
        """Build an unsigned swap transaction dict."""
        pass

    @abstractmethod
    def build_approve_tx(self, token: str, spender: str, amount: int, sender: str,
                         gas: Optional[int] = None) -> dict:
        """Build an unsigned ERC-20 approve transaction dict."""
        pass

    @abstractmethod
    def build_wrap_tx(self, amount: int, sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned ETH -> WETH wrap transaction."""
        pass

    @abstractmethod
    def build_unwrap_tx(self, amount: int, sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned WETH -> ETH unwrap transaction."""
        pass

    @abstractmethod
    def router_address(self) -> str:
        """Return the router/swap contract address for approvals."""
        pass


class DexAdapterV3(DexAdapter):
    """Uniswap v3 read/build operations for one chain."""

    def __init__(self, rpc_url: str, dex: DexConfig):
        super().__init__(rpc_url, dex.weth)
        self.dex = dex
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(dex.factory), abi=FACTORY_ABI)
        self.quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(dex.quoter_v2), abi=QUOTER_V2_ABI)
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(dex.swap_router), abi=SWAP_ROUTER_ABI)

    def router_address(self) -> str:
        """Return the SwapRouter02 address for approvals."""
        return self.dex.swap_router

    # ---- Pool + quote ---------------------------------------------------

    def find_pool(self, token_in: str, token_out: str, fee: int,
                  tick_spacing: Optional[int] = None, hooks: Optional[str] = None) -> Optional[str]:
        """Return the pool address for the pair+fee, or None if it doesn't exist."""
        pool = self.factory.functions.getPool(
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out), int(fee)).call()
        if int(pool, 16) == 0:
            return None
        return pool

    def quote_exact_input_single(self, token_in: str, token_out: str,
                                 amount_in_atomic: int, fee: int,
                                 tick_spacing: Optional[int] = None,
                                 hooks: Optional[str] = None) -> dict:
        """Independent re-quote via QuoterV2. Returns {amount_out, sqrt_after,
        gas_estimate}. Raises DexError on revert (e.g. no liquidity)."""
        params = (Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
                  int(amount_in_atomic), int(fee), 0)
        try:
            out = self.quoter.functions.quoteExactInputSingle(params).call()
        except Exception as e:
            raise DexError(f"quote failed ({token_in}->{token_out} fee {fee}): {e}") from e
        return {"amount_out": int(out[0]), "sqrt_after": int(out[1]),
                "ticks_crossed": int(out[2]), "gas_estimate": int(out[3])}

    # ---- Execution (build + simulate) -----------------------------------

    def _swap_params(self, token_in, token_out, fee, recipient, amount_in, amount_out_min):
        return (Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
                int(fee), Web3.to_checksum_address(recipient),
                int(amount_in), int(amount_out_min), 0)

    def simulate_swap(self, token_in: str, token_out: str, fee: int, recipient: str,
                      amount_in_atomic: int, amount_out_min: int, sender: str,
                      eth_value: int = 0, tick_spacing: Optional[int] = None,
                      hooks: Optional[str] = None) -> int:
        """Dry-run the swap via eth_call from `sender`. Returns the simulated
        amountOut, or raises DexError if it would revert.

        Args:
            eth_value: For ETH input swaps, set this to amount_in_atomic.
            tick_spacing, hooks: Ignored for V3.
        """
        params = self._swap_params(token_in, token_out, fee, recipient,
                                   amount_in_atomic, amount_out_min)
        try:
            return int(self.router.functions.exactInputSingle(params).call(
                {"from": Web3.to_checksum_address(sender), "value": int(eth_value)}))
        except Exception as e:
            raise DexError(f"swap simulation reverted: {e}") from e

    def build_swap_tx(self, token_in: str, token_out: str, fee: int, recipient: str,
                      amount_in_atomic: int, amount_out_min: int, sender: str,
                      gas: Optional[int] = None, eth_value: int = 0,
                      tick_spacing: Optional[int] = None,
                      hooks: Optional[str] = None) -> dict:
        """Build an unsigned exactInputSingle transaction dict (for the signing
        service to sign and submit).

        Args:
            eth_value: For ETH input swaps, set this to amount_in_atomic. The router
                       is payable and will wrap the ETH to WETH internally.
            tick_spacing, hooks: Ignored for V3.
        """
        params = self._swap_params(token_in, token_out, fee, recipient,
                                   amount_in_atomic, amount_out_min)
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": int(eth_value),
        }
        if gas is not None:
            tx["gas"] = int(gas)
        return self.router.functions.exactInputSingle(params).build_transaction(tx)

    def build_approve_tx(self, token: str, spender: str, amount: int, sender: str,
                         gas: Optional[int] = None) -> dict:
        """Build an unsigned ERC-20 approve transaction dict."""
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": 0,
        }
        if gas is not None:
            tx["gas"] = int(gas)
        return self._erc20(token).functions.approve(
            Web3.to_checksum_address(spender), int(amount)).build_transaction(tx)

    def build_swap_to_eth_tx(self, token_in: str, fee: int, recipient: str,
                             amount_in_atomic: int, amount_out_min: int, sender: str,
                             gas: Optional[int] = None) -> dict:
        """Build a multicall tx that swaps token -> WETH, then unwraps WETH -> ETH.

        Used when the agent wants native ETH as output. The swap sends WETH to the
        router itself, then unwrapWETH9 sends ETH to the final recipient.
        """
        weth = self.dex.weth
        router_addr = Web3.to_checksum_address(self.dex.swap_router)

        # 1. Encode exactInputSingle: token -> WETH, recipient = router (not user)
        swap_params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(weth),
            int(fee),
            router_addr,  # WETH goes to router, not final recipient
            int(amount_in_atomic),
            int(amount_out_min),
            0,  # sqrtPriceLimitX96
        )
        swap_data = self.router.encodeABI(fn_name="exactInputSingle", args=[swap_params])

        # 2. Encode unwrapWETH9: send ETH to final recipient
        unwrap_data = self.router.encodeABI(
            fn_name="unwrapWETH9",
            args=[int(amount_out_min), Web3.to_checksum_address(recipient)]
        )

        # 3. Bundle in multicall
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": 0,
        }
        if gas is not None:
            tx["gas"] = int(gas)

        return self.router.functions.multicall([swap_data, unwrap_data]).build_transaction(tx)

    # ---- wrap/unwrap (direct WETH9 interaction) -------------------------

    def _weth9(self):
        """Return a contract instance for the WETH9 contract."""
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.weth), abi=WETH9_ABI)

    def build_wrap_tx(self, amount: int, sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned WETH9.deposit() transaction (ETH -> WETH).

        Wrapping is a 1:1 conversion with no slippage. The WETH is credited to the sender.
        """
        sender = Web3.to_checksum_address(sender)
        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": int(amount),  # ETH to wrap
        }
        if gas is not None:
            tx["gas"] = int(gas)
        return self._weth9().functions.deposit().build_transaction(tx)

    def build_unwrap_tx(self, amount: int, sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned WETH9.withdraw() transaction (WETH -> ETH).

        Unwrapping is a 1:1 conversion with no slippage. The ETH is sent to the sender.
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
        return self._weth9().functions.withdraw(int(amount)).build_transaction(tx)
