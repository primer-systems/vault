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

from ..networks import DexConfig, ETH_ADDRESS, is_native_eth


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

#: keccak256("Transfer(address,address,uint256)") - every ERC-20 emits this, so
#: the tokens a swap actually delivered can be read from its receipt without
#: knowing which router or pool moved them.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: keccak256("Withdrawal(address,uint256)") - WETH9 emits this when unwrapping.
#: Native ETH leaves no Transfer behind, so a swap ending in ETH is read from the
#: unwrap instead.
WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"

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
    """DEX interaction failed (no pool, revert, RPC error).

    The message is redacted of any URL: it is built by interpolating the
    underlying web3/requests exception, which names the RPC endpoint - and a
    hosted node's URL carries the account's API key. This message reaches API
    responses (a failed trade's `reason`), so the URL must not survive in it.
    """

    def __init__(self, *args):
        from ..utils import redact_urls
        if args and isinstance(args[0], str):
            args = (redact_urls(args[0]),) + args[1:]
        super().__init__(*args)


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

    def sign_tx(self, tx: dict, private_key: str):
        """Sign a built transaction locally. Returns the raw signed bytes.

        Touches no network, so a failure here proves nothing was sent.
        """
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        signed = self.w3.eth.account.sign_transaction(tx, private_key)
        return signed.raw_transaction

    def sign_and_send(self, tx: dict, private_key: str, before_send=None) -> str:
        """Sign a built transaction and broadcast it. Returns the tx hash hex.

        `before_send` runs in the moment between signing and the network call,
        and exists because that instant is the only safe place for a caller to
        record that value is about to move. The adapter is what knows where the
        boundary is - a caller wrapping this method cannot tell a signing
        failure from a send failure, and the difference decides whether a
        transaction may be live. The trading lane commits daily volume here.

        Anything raised by `before_send` propagates and nothing is sent, which
        is the right way round: a caller that could not record the spend should
        not spend.
        """
        raw = self.sign_tx(tx, private_key)
        if before_send:
            before_send()
        return self.send_raw(raw)

    def send_raw(self, raw_tx) -> str:
        """Broadcast an already-signed raw transaction. Returns the tx hash hex.

        Used by the hardware-wallet path, where signing happens on the device and
        Vault only ever sees the signed bytes.

        Args:
            raw_tx: RLP-encoded signed transaction, as bytes or a 0x-hex string.
        """
        tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
        return tx_hash.hex()

    def wait_for_receipt(self, tx_hash: str, timeout: float = 120.0):
        """Block until the transaction is mined; returns the receipt."""
        return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)

    # ---- what a swap actually delivered ----------------------------------

    def amount_received(self, receipt, token_out: str,
                        recipient: str) -> Optional[int]:
        """Atomic units of `token_out` that reached `recipient`, from the receipt.

        A quote is a prediction and the fill is the fact, and they differ by
        whatever the pool did between the two. Trade history records both, so
        this reads the second one.

        Returns None when the fill cannot be read rather than guessing. The
        caller must not substitute the quote for it: a quote recorded silently
        as a fill misreports an estimate as a settled fact, and "could not be
        read" is the honest answer.
        """
        try:
            if is_native_eth(token_out):
                return self._native_received(receipt, recipient)
            return self._erc20_received(receipt, token_out, recipient)
        except Exception:
            # Reading the fill is bookkeeping, and the trade has already
            # settled. An odd receipt shape must not turn a completed swap into
            # a reported failure.
            return None

    def _erc20_received(self, receipt, token: str, recipient: str) -> Optional[int]:
        """Sum the ERC-20 Transfers of `token` into `recipient` in this receipt.

        Summed rather than taken from the first match because a swap routed
        through more than one hop, or a token that charges a transfer fee, can
        credit the recipient more than once. Anything not addressed to the
        recipient - the input leg, intermediate hops, fee transfers - is skipped
        by the address filter.
        """
        token = Web3.to_checksum_address(token)
        wanted = self._topic_address(recipient)
        total = 0
        found = False
        for log in self._logs(receipt):
            topics = self._topics(log)
            if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                continue
            if self._log_address(log) != token or topics[2] != wanted:
                continue
            total += int(self._log_data(log)[:66], 16)
            found = True
        return total if found else None

    def _native_received(self, receipt, recipient: str) -> Optional[int]:
        """ETH delivered by a swap. Version-specific; unknown unless overridden."""
        return None

    # ---- receipt field access --------------------------------------------
    #
    # web3 returns AttributeDicts of HexBytes; tests and other callers may hand
    # over plain dicts of strings. These normalise both to lower-case hex so the
    # comparisons above have one shape to deal with.

    @staticmethod
    def _logs(receipt):
        logs = receipt.get("logs") if isinstance(receipt, dict) else getattr(receipt, "logs", None)
        return logs or []

    @staticmethod
    def _hex(value) -> str:
        if isinstance(value, (bytes, bytearray)):
            return "0x" + bytes(value).hex()
        text = str(value).lower()
        return text if text.startswith("0x") else "0x" + text

    @classmethod
    def _topics(cls, log) -> list:
        topics = log.get("topics") if isinstance(log, dict) else getattr(log, "topics", None)
        return [cls._hex(t) for t in (topics or [])]

    @classmethod
    def _log_address(cls, log) -> str:
        addr = log.get("address") if isinstance(log, dict) else getattr(log, "address", None)
        return Web3.to_checksum_address(addr) if addr else ""

    @classmethod
    def _log_data(cls, log) -> str:
        data = log.get("data") if isinstance(log, dict) else getattr(log, "data", None)
        return cls._hex(data if data is not None else "0x")

    @staticmethod
    def _topic_address(address: str) -> str:
        """An address as it appears in an indexed topic: 32 bytes, left-padded."""
        return "0x" + Web3.to_checksum_address(address)[2:].lower().rjust(64, "0")

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

    # ---- Approvals (shared shape, version-specific content) --------------

    def approval_steps(self, token: str, owner: str, amount: int,
                       token_label: str = "") -> list[tuple[dict, str]]:
        """Unsigned approvals this adapter needs before a swap can settle, in order.

        Returned as a list because the number is a property of the DEX version,
        not of the trade: v3's router pulls tokens itself and needs one approval,
        v4's router pulls through Permit2 and needs two. The trading service
        submits whatever it is handed and counts the steps for the user, so
        neither it nor the Ledger prompt has to know which version it is driving.

        An empty list means nothing needs approving — native ETH, or an existing
        allowance that already covers the amount.
        """
        if is_native_eth(token):
            return []
        spender = self.router_address()
        if self.allowance(token, owner, spender) >= amount:
            return []
        label = token_label or "the input token"
        return [(self.build_approve_tx(token, spender, amount, owner),
                 f"approve {label} for the router")]

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
        swap_data = self.router.encode_abi(
            abi_element_identifier="exactInputSingle", args=[swap_params])

        # 2. Encode unwrapWETH9: send ETH to final recipient
        unwrap_data = self.router.encode_abi(
            abi_element_identifier="unwrapWETH9",
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

    def _native_received(self, receipt, recipient: str) -> Optional[int]:
        """ETH delivered, read from the WETH9 unwrap that produced it.

        A swap ending in native ETH is a multicall: the pool sends WETH to the
        router, then unwrapWETH9 turns it into ETH for the user. ETH transfers
        emit nothing, so the Withdrawal event is the only record of the amount -
        and the wad it carries is exactly what the recipient was paid.

        Withdrawals by the recipient itself count too, which is the plain
        WETH-to-ETH unwrap taking the same route.
        """
        sources = {self._topic_address(self.dex.swap_router),
                   self._topic_address(recipient)}
        weth = Web3.to_checksum_address(self.dex.weth)
        total = 0
        found = False
        for log in self._logs(receipt):
            topics = self._topics(log)
            if len(topics) < 2 or topics[0] != WITHDRAWAL_TOPIC:
                continue
            if self._log_address(log) != weth or topics[1] not in sources:
                continue
            total += int(self._log_data(log)[:66], 16)
            found = True
        return total if found else None

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
