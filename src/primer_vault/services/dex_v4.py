"""
DEX adapter — Uniswap v4 interaction for the trading engine.

V4 uses a singleton PoolManager architecture. Pools are identified by PoolKey:
(currency0, currency1, fee, tickSpacing, hooks), and by the poolId that is the
keccak of that key.

**RHC-SPECIFIC — verified against the deployed, Blockscout-verified sources.**

Robinhood Chain's UniversalRouter (0x8876789976dEcBfCbBbe364623C63652db8C0904)
carries a modified `IV4Router.ExactInputSingleParams` with an extra
`uint256 minHopPriceX36` between `amountOutMinimum` and `hookData`:

    struct ExactInputSingleParams {
        PoolKey poolKey;
        bool    zeroForOne;
        uint128 amountIn;
        uint128 amountOutMinimum;
        uint256 minHopPriceX36;   // <-- RHC addition
        bytes   hookData;
    }

It is a per-hop minimum price (output/input, scaled by 1e36); zero disables the
check, which is what we send because `amountOutMinimum` already carries the
slippage floor the policy approved. Stock Uniswap SDK calldata omits this field
and will revert here. The router's own CalldataDecoder confirms the layout: it
requires at least 0x160 bytes, "11 * 0x20 -> 9 elements, bytes offset, and bytes
length 0", and 9 static elements is only reachable with minHopPriceX36 present.

Everything *else* on RHC is stock Uniswap, and previous revisions of this file
claimed otherwise:

- The V4Quoter is the standard contract. Its verified ABI is
  `quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))`
  — selector 0xaa9d21cb, with no sqrtPriceLimitX96. The ABI is declared once,
  below, and encoded by web3 rather than by hand: any extra word appended to
  this call lands where the contract reads the offset to `hookData` and breaks
  decoding on ERC-20/ERC-20 pools, so the exact struct shape is load-bearing.
- StateView is the standard contract (`getSlot0(bytes32)`, `getLiquidity(bytes32)`),
  so pool existence is answered by reading slot0 rather than by guessing with a
  probe quote.

RHC only supports exact-in swaps (the Bags hook reverts exact-out WETH swaps).
"""

import time
from typing import Optional

from eth_abi import encode
from web3 import Web3

from ..networks import DexConfigV4, is_native_eth
from .dex import DexAdapter, DexError, WETH9_ABI


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# ---- ABI fragments ---------------------------------------------------------
#
# Declared as ABI and encoded by web3, never as a hand-rolled selector plus a
# hand-built argument list. A mismatch between the two is invisible until the
# chain rejects it, and for the quoter it was not even visible then.

POOL_KEY_COMPONENTS = [
    {"name": "currency0", "type": "address"},
    {"name": "currency1", "type": "address"},
    {"name": "fee", "type": "uint24"},
    {"name": "tickSpacing", "type": "int24"},
    {"name": "hooks", "type": "address"},
]

V4_QUOTER_ABI = [
    {"inputs": [{"components": [
        {"components": POOL_KEY_COMPONENTS, "name": "poolKey", "type": "tuple"},
        {"name": "zeroForOne", "type": "bool"},
        {"name": "exactAmount", "type": "uint128"},
        {"name": "hookData", "type": "bytes"}], "name": "params", "type": "tuple"}],
     "name": "quoteExactInputSingle",
     "outputs": [{"name": "amountOut", "type": "uint256"},
                 {"name": "gasEstimate", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
]

STATE_VIEW_ABI = [
    {"inputs": [{"name": "poolId", "type": "bytes32"}], "name": "getSlot0",
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
                 {"name": "protocolFee", "type": "uint24"}, {"name": "lpFee", "type": "uint24"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "poolId", "type": "bytes32"}], "name": "getLiquidity",
     "outputs": [{"name": "liquidity", "type": "uint128"}],
     "stateMutability": "view", "type": "function"},
]

UNIVERSAL_ROUTER_ABI = [
    {"inputs": [{"name": "commands", "type": "bytes"},
                {"name": "inputs", "type": "bytes[]"},
                {"name": "deadline", "type": "uint256"}],
     "name": "execute", "outputs": [], "stateMutability": "payable", "type": "function"},
]

PERMIT2_ABI = [
    {"inputs": [{"name": "user", "type": "address"}, {"name": "token", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance",
     "outputs": [{"name": "amount", "type": "uint160"}, {"name": "expiration", "type": "uint48"},
                 {"name": "nonce", "type": "uint48"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "token", "type": "address"}, {"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint160"}, {"name": "expiration", "type": "uint48"}],
     "name": "approve", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

# ---- UniversalRouter command (Commands.sol) --------------------------------
COMMAND_V4_SWAP = 0x10

# ---- V4Router actions (v4-periphery Actions.sol) ---------------------------
# A swap is three actions, not one: perform the swap, pay what is owed, collect
# what is due. The router settles from and takes to msgSender(), which is the
# address that signed the transaction — so there is no recipient field to set,
# and no way for these to deliver the output anywhere other than the trader.
ACTION_SWAP_EXACT_IN_SINGLE = 0x06
ACTION_SETTLE_ALL = 0x0c
ACTION_TAKE_ALL = 0x0f

# eth_abi type strings for the action parameters.
_POOL_KEY_TYPE = "(address,address,uint24,int24,address)"
#: RHC's ExactInputSingleParams — note the uint256 minHopPriceX36 before hookData.
_EXACT_IN_SINGLE_TYPE = f"({_POOL_KEY_TYPE},bool,uint128,uint128,uint256,bytes)"

#: Sent as minHopPriceX36. Zero disables the router's per-hop price check;
#: amountOutMinimum already enforces the slippage the policy approved, and it is
#: checked on the whole swap rather than per hop.
_NO_MIN_HOP_PRICE = 0

#: How long a Permit2 allowance stays valid. Long enough to outlive the swap's
#: own deadline, short enough that an unused authorisation lapses on its own
#: rather than sitting on the wallet indefinitely.
PERMIT2_EXPIRATION_SECONDS = 1800

#: How long the swap itself may sit unmined before the router refuses it.
SWAP_DEADLINE_SECONDS = 600


def _sort_tokens(token_a: str, token_b: str) -> tuple[str, str]:
    """Sort tokens for PoolKey (currency0 < currency1 by address)."""
    addr_a = Web3.to_checksum_address(token_a)
    addr_b = Web3.to_checksum_address(token_b)
    if int(addr_a, 16) < int(addr_b, 16):
        return addr_a, addr_b
    return addr_b, addr_a


def _as_currency(token: str) -> str:
    """Normalise a token to its v4 Currency address (native ETH is address(0))."""
    return ZERO_ADDRESS if is_native_eth(token) else Web3.to_checksum_address(token)


class DexAdapterV4(DexAdapter):
    """Uniswap v4 read/build operations for one chain.

    RHC-specific only in the `minHopPriceX36` field of the swap struct; the
    quoter, StateView, PoolManager and Permit2 are all stock Uniswap.
    """

    def __init__(self, rpc_url: str, dex: DexConfigV4):
        super().__init__(rpc_url, dex.weth)
        self.dex = dex

    def router_address(self) -> str:
        """Return the UniversalRouter address.

        Note this is *not* the address an ERC-20 approval should name — the
        router pulls tokens through Permit2. See approval_steps().
        """
        return self.dex.universal_router

    # ---- contract handles -------------------------------------------------

    def _quoter(self):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.quoter), abi=V4_QUOTER_ABI)

    def _state_view(self):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.state_view), abi=STATE_VIEW_ABI)

    def _router(self):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.universal_router),
            abi=UNIVERSAL_ROUTER_ABI)

    def _permit2(self):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.permit2), abi=PERMIT2_ABI)

    def _weth9(self):
        """Return a contract instance for the WETH9 contract."""
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(self.dex.weth), abi=WETH9_ABI)

    # ---- pool key ---------------------------------------------------------

    def _make_pool_key(
        self, token_in: str, token_out: str, fee: int, tick_spacing: int, hooks: str
    ) -> tuple:
        """Create a PoolKey tuple with currencies sorted as v4 requires."""
        currency0, currency1 = _sort_tokens(_as_currency(token_in), _as_currency(token_out))
        return (
            currency0,
            currency1,
            int(fee),
            int(tick_spacing),
            Web3.to_checksum_address(hooks),
        )

    @staticmethod
    def _pool_id(pool_key: tuple) -> bytes:
        """The v4 poolId: keccak of the abi-encoded PoolKey.

        Every member of PoolKey is static, so the encoding is just the five
        words — the same bytes the PoolManager hashes.
        """
        return Web3.keccak(encode([_POOL_KEY_TYPE], [pool_key]))

    def _is_zero_for_one(self, token_in: str, token_out: str) -> bool:
        """True if the input currency is currency0 (selling token0 for token1)."""
        token_in_addr = _as_currency(token_in)
        currency0, _ = _sort_tokens(token_in_addr, _as_currency(token_out))
        return currency0.lower() == token_in_addr.lower()

    def _require_v4_fields(self, tick_spacing: Optional[int], hooks: Optional[str], what: str):
        if tick_spacing is None or hooks is None:
            raise DexError(
                f"V4 {what} requires tick_spacing and hooks — they are part of the "
                f"pool's identity, not optional tuning")

    # ---- Pool lookup via StateView ----------------------------------------

    def find_pool(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        tick_spacing: Optional[int] = None,
        hooks: Optional[str] = None,
    ) -> Optional[str]:
        """Return the pool identifier, or None if the pool is not initialised.

        Answered by reading slot0 for the poolId. An uninitialised pool has
        sqrtPriceX96 == 0.

        This deliberately does not judge liquidity. A pool that exists but is too
        thin to fill is a different failure with a different remedy, and the
        quote below reports it as one — whereas folding both into "no pool" sends
        the caller looking for a pool that is right there.
        """
        self._require_v4_fields(tick_spacing, hooks, "find_pool")

        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        pool_id = self._pool_id(pool_key)

        try:
            slot0 = self._state_view().functions.getSlot0(pool_id).call()
        except Exception as e:
            raise DexError(f"could not read v4 pool state for {pool_id.hex()}: {e}") from e

        if int(slot0[0]) == 0:
            return None
        return f"V4:0x{pool_id.hex()}"

    def pool_liquidity(self, token_in: str, token_out: str, fee: int,
                       tick_spacing: int, hooks: str) -> int:
        """Active liquidity for a v4 pool. Useful for diagnostics."""
        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        return int(self._state_view().functions.getLiquidity(self._pool_id(pool_key)).call())

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
        """Independent re-quote via the standard V4Quoter.

        Returns {amount_out, gas_estimate}. Raises DexError on revert, which for
        an initialised pool means there is not enough liquidity to fill.
        """
        self._require_v4_fields(tick_spacing, hooks, "quote")

        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        zero_for_one = self._is_zero_for_one(token_in, token_out)

        params = (pool_key, zero_for_one, int(amount_in_atomic), b"")
        try:
            amount_out, gas_estimate = self._quoter().functions.quoteExactInputSingle(
                params).call()
        except Exception as e:
            raise DexError(
                f"V4 quote failed ({token_in}->{token_out} fee {fee} "
                f"tickSpacing {tick_spacing}): {e}"
            ) from e

        return {"amount_out": int(amount_out), "gas_estimate": int(gas_estimate)}

    # ---- Swap encoding (RHC UniversalRouter) ------------------------------

    def _encode_v4_swap_input(
        self,
        pool_key: tuple,
        zero_for_one: bool,
        amount_in: int,
        amount_out_min: int,
        hook_data: bytes = b"",
    ) -> bytes:
        """Encode the V4_SWAP input: abi.encode(bytes actions, bytes[] params).

        Three actions, and all three are required:

        - SWAP_EXACT_IN_SINGLE performs the swap and leaves a debt in the input
          currency and a credit in the output currency on the PoolManager.
        - SETTLE_ALL pays the debt. The router pulls the input from msgSender()
          — via Permit2 for an ERC-20, or from msg.value for native ETH.
        - TAKE_ALL collects the credit and sends it to msgSender().

        Encoding only the swap leaves the deltas unresolved and the transaction
        reverts; there is no recipient field anywhere in this because both the
        payer and the payee are the transaction's sender by construction.

        SETTLE_ALL's second argument is the most the router may pull, and
        TAKE_ALL's is the least it must deliver — so the amounts the policy
        approved are enforced on both legs, on-chain, independently of the quote.
        """
        currency_in = pool_key[0] if zero_for_one else pool_key[1]
        currency_out = pool_key[1] if zero_for_one else pool_key[0]

        actions = bytes([ACTION_SWAP_EXACT_IN_SINGLE, ACTION_SETTLE_ALL, ACTION_TAKE_ALL])

        swap_params = encode(
            [_EXACT_IN_SINGLE_TYPE],
            [(pool_key, bool(zero_for_one), int(amount_in), int(amount_out_min),
              _NO_MIN_HOP_PRICE, hook_data)],
        )
        settle_params = encode(["address", "uint256"], [currency_in, int(amount_in)])
        take_params = encode(["address", "uint256"], [currency_out, int(amount_out_min)])

        return encode(["bytes", "bytes[]"], [actions, [swap_params, settle_params, take_params]])

    def _swap_call_args(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        amount_in_atomic: int,
        amount_out_min: int,
        tick_spacing: int,
        hooks: str,
    ) -> tuple[bytes, list[bytes], int]:
        """Build (commands, inputs, deadline) for UniversalRouter.execute."""
        pool_key = self._make_pool_key(token_in, token_out, fee, tick_spacing, hooks)
        zero_for_one = self._is_zero_for_one(token_in, token_out)

        commands = bytes([COMMAND_V4_SWAP])
        inputs = [self._encode_v4_swap_input(
            pool_key, zero_for_one, amount_in_atomic, amount_out_min)]
        deadline = self._deadline()
        return commands, inputs, deadline

    def _deadline(self) -> int:
        """Swap deadline, taken from chain time rather than local time.

        The router compares this against the block timestamp, so a machine with a
        skewed clock would otherwise submit trades that are already expired.
        """
        try:
            now = int(self.w3.eth.get_block("latest")["timestamp"])
        except Exception:
            now = int(time.time())
        return now + SWAP_DEADLINE_SECONDS

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
        """Dry-run the real swap via eth_call from `sender`.

        This runs the same calldata the swap will use, against the router, with
        the sender's actual allowances and balances — so it catches a missing
        Permit2 authorisation, a hook that rejects the trade, and a fill below
        amountOutMinimum, none of which a quote can see.

        Requires the approvals to be in place already, which is why the trading
        service simulates after approving rather than before.

        Returns the quoted output (execute() itself returns nothing), or raises
        DexError if the swap would revert.
        """
        self._require_v4_fields(tick_spacing, hooks, "simulate_swap")

        commands, inputs, deadline = self._swap_call_args(
            token_in, token_out, fee, amount_in_atomic, amount_out_min, tick_spacing, hooks)

        try:
            self._router().functions.execute(commands, inputs, deadline).call(
                {"from": Web3.to_checksum_address(sender), "value": int(eth_value)})
        except Exception as e:
            raise DexError(f"V4 swap simulation reverted: {e}") from e

        quote = self.quote_exact_input_single(
            token_in, token_out, amount_in_atomic, fee, tick_spacing, hooks)
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
        """Build an unsigned V4 swap via UniversalRouter.execute.

        `recipient` is accepted for interface parity with V3 and must be the
        sender: v4's SETTLE_ALL/TAKE_ALL both act on msgSender(), so the output
        cannot be routed elsewhere. Passing a different address is refused rather
        than silently ignored.
        """
        self._require_v4_fields(tick_spacing, hooks, "build_swap_tx")

        sender = Web3.to_checksum_address(sender)
        if recipient and Web3.to_checksum_address(recipient) != sender:
            raise DexError(
                "V4 swaps settle and take against the transaction sender, so the "
                "output cannot be sent to a different recipient")

        commands, inputs, deadline = self._swap_call_args(
            token_in, token_out, fee, amount_in_atomic, amount_out_min, tick_spacing, hooks)

        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": int(eth_value),
        }
        if gas is not None:
            tx["gas"] = int(gas)

        return self._router().functions.execute(
            commands, inputs, deadline).build_transaction(tx)

    # ---- Approvals (ERC-20 -> Permit2 -> UniversalRouter) ------------------

    def approval_steps(self, token: str, owner: str, amount: int,
                       token_label: str = "") -> list[tuple[dict, str]]:
        """Unsigned approvals needed before this swap can settle, in order.

        V4's UniversalRouter does not pull tokens itself — it calls Permit2,
        which holds its own allowance ledger. So an ERC-20 input needs up to two
        authorisations, and approving the router directly (as V3 does) authorises
        nothing:

          1. ERC-20 approve(Permit2, amount)  — lets Permit2 move the token
          2. Permit2 approve(token, router, amount, expiry) — lets the router ask

        Each is skipped when the existing allowance already covers the amount, so
        a second trade in the same session usually needs neither. Both are for
        exactly this trade's amount, and the Permit2 authorisation carries a short
        expiry, so nothing durable is left behind.

        Native ETH needs no approval at all — it arrives as msg.value.
        """
        if is_native_eth(token):
            return []

        steps: list[tuple[dict, str]] = []
        owner = Web3.to_checksum_address(owner)
        token = Web3.to_checksum_address(token)
        permit2_addr = Web3.to_checksum_address(self.dex.permit2)
        router_addr = Web3.to_checksum_address(self.dex.universal_router)
        label = token_label or "the input token"

        # 1. Does Permit2 have an ERC-20 allowance to move this token?
        if self.allowance(token, owner, permit2_addr) < amount:
            steps.append((
                self.build_approve_tx(token, permit2_addr, amount, owner),
                f"approve {label} for Permit2",
            ))

        # 2. Does the router have a live Permit2 authorisation for this token?
        try:
            allowed, expiration, _nonce = self._permit2().functions.allowance(
                owner, token, router_addr).call()
        except Exception as e:
            raise DexError(f"could not read Permit2 allowance: {e}") from e

        # An expiry that has already passed is no authorisation, whatever the
        # amount beside it says.
        now = int(time.time())
        if int(allowed) < amount or int(expiration) <= now:
            steps.append((
                self.build_permit2_approve_tx(token, router_addr, amount, owner),
                f"authorise the Uniswap router to spend {label}",
            ))

        return steps

    def build_permit2_approve_tx(self, token: str, spender: str, amount: int,
                                 sender: str, gas: Optional[int] = None) -> dict:
        """Build an unsigned Permit2 approve(token, spender, amount, expiration)."""
        sender = Web3.to_checksum_address(sender)

        # Permit2 stores the amount as uint160 and the expiry as uint48.
        if int(amount) >= 2 ** 160:
            raise DexError("amount is too large for a Permit2 allowance")
        expiration = int(time.time()) + PERMIT2_EXPIRATION_SECONDS
        if expiration >= 2 ** 48:
            raise DexError("Permit2 expiration overflows uint48")

        tx = {
            "from": sender,
            "nonce": self.w3.eth.get_transaction_count(sender),
            "chainId": self.dex_chain_id(),
            "value": 0,
        }
        if gas is not None:
            tx["gas"] = int(gas)

        return self._permit2().functions.approve(
            Web3.to_checksum_address(token),
            Web3.to_checksum_address(spender),
            int(amount),
            expiration,
        ).build_transaction(tx)

    def build_approve_tx(
        self, token: str, spender: str, amount: int, sender: str, gas: Optional[int] = None
    ) -> dict:
        """Build an unsigned ERC-20 approve transaction.

        For v4 the spender that matters is Permit2, not the router — see
        approval_steps(), which is what the trading service calls.
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
        return self._erc20(token).functions.approve(
            Web3.to_checksum_address(spender), int(amount)).build_transaction(tx)

    # ---- Wrap/Unwrap (same as V3, uses WETH9) -----------------------------

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
