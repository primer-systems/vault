"""
Morpho adapter — reads and unsigned transactions for the DeFi lane.

Mirrors `services/dex.py`: read paths are safe against the live chain, and the
build_* methods produce unsigned transactions that the DeFi service signs and
submits. Qt-free, so both editions share it.

Morpho is shaped nothing like Uniswap, and two differences drive this module.

**One singleton, unbounded venues.** Every lending market on the chain lives in
one contract, and anyone may create a market with any oracle they like. 124
exist on Robinhood Chain; four have depth and a curator behind them, and the
rest are empty, test, or junk. Nothing can be settled by configuration, so
`discover_vaults()` enumerates the vault factory and `resolve_venues()` keeps
whichever of them a trusted curator runs - both read live, because a curator is
a role that can be reassigned in one transaction.

**Positions persist.** A swap is over when it settles; a deposit sits there
earning, and its value moves. Nothing here caches a position or a share price -
every valuation is a fresh read, because a stale one is a wrong one.

On arithmetic: share counts are never computed here. `previewDeposit` and
`previewWithdraw` are asked instead, so this module's idea of a conversion
cannot disagree with the vault's. Amounts are atomic integers throughout - the
asset is 6dp and the share is 18dp, and those two scales are both just integers,
so the only safe discipline is never to let a float near them.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from web3 import Web3

from ..networks import MorphoConfig
from .dex import DexAdapter, ERC20_ABI, TRANSFER_TOPIC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ABIs — only what is actually called
# ---------------------------------------------------------------------------

VAULT_V2_ABI = [
    {"inputs": [], "name": "asset", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "curator", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalAssets", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "adaptersLength", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "adapters", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "absoluteCap", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "allocation", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "previewDeposit", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "previewWithdraw", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "previewRedeem", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "convertToAssets", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "canReceiveShares", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "canSendShares", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "canReceiveAssets", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "canSendAssets", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "address"}], "name": "deposit", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "address"}, {"type": "address"}], "name": "withdraw", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"type": "uint256"}, {"type": "address"}, {"type": "address"}], "name": "redeem", "outputs": [{"type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]

#: `CreateVaultV2(address,address,bytes32,address)` on the VaultV2Factory.
#: Every Vault V2 on the chain is created through it, so its log is the complete
#: list - the only way to find a vault a curator deployed after this release.
CREATE_VAULT_TOPIC = "0x341ce009267aa0d78cc12b34155e223904a51ed49d144beb6eb8be87813edb4e"

#: Blocks per `eth_getLogs` call while scanning the factory. Public nodes cap the
#: range, and the whole history is one pass.
_LOG_SCAN_STEP = 900_000

MARKET_ADAPTER_ABI = [
    {"inputs": [], "name": "marketIdsLength", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "uint256"}], "name": "marketIds", "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "morpho", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "parentVault", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]

_MARKET_PARAMS_TUPLE = {
    "components": [
        {"name": "loanToken", "type": "address"},
        {"name": "collateralToken", "type": "address"},
        {"name": "oracle", "type": "address"},
        {"name": "irm", "type": "address"},
        {"name": "lltv", "type": "uint256"},
    ],
    "name": "marketParams",
    "type": "tuple",
}

MORPHO_ABI = [
    {"inputs": [{"type": "bytes32"}], "name": "idToMarketParams",
     "outputs": [dict(_MARKET_PARAMS_TUPLE, name="")], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}], "name": "market", "outputs": [{"components": [
        {"name": "totalSupplyAssets", "type": "uint128"},
        {"name": "totalSupplyShares", "type": "uint128"},
        {"name": "totalBorrowAssets", "type": "uint128"},
        {"name": "totalBorrowShares", "type": "uint128"},
        {"name": "lastUpdate", "type": "uint128"},
        {"name": "fee", "type": "uint128"},
    ], "name": "", "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "bytes32"}, {"type": "address"}], "name": "position", "outputs": [{"components": [
        {"name": "supplyShares", "type": "uint256"},
        {"name": "borrowShares", "type": "uint128"},
        {"name": "collateral", "type": "uint128"},
    ], "name": "", "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [_MARKET_PARAMS_TUPLE, {"name": "assets", "type": "uint256"},
                {"name": "shares", "type": "uint256"}, {"name": "onBehalf", "type": "address"},
                {"name": "data", "type": "bytes"}],
     "name": "supply", "outputs": [{"type": "uint256"}, {"type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [_MARKET_PARAMS_TUPLE, {"name": "assets", "type": "uint256"},
                {"name": "shares", "type": "uint256"}, {"name": "onBehalf", "type": "address"},
                {"name": "receiver", "type": "address"}],
     "name": "withdraw", "outputs": [{"type": "uint256"}, {"type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [_MARKET_PARAMS_TUPLE], "name": "accrueInterest", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]


class MorphoError(Exception):
    """A Morpho interaction failed (bad venue, revert, RPC error).

    Redacted of any URL for the same reason `DexError` is: the message is built
    by interpolating a web3 exception that names the RPC endpoint, a hosted
    node's URL carries the account's API key, and this text reaches API
    responses.
    """

    def __init__(self, *args):
        from ..utils import redact_urls
        if args and isinstance(args[0], str):
            args = (redact_urls(args[0]),) + args[1:]
        super().__init__(*args)


#: Vault V2 reverts with four-byte custom errors, so a failure arrives as
#: `0xe65b7a77` and nothing else. Selectors are keccak of the signature, taken
#: from the errors declared in vault-v2's ErrorsLib and VaultV2; only the ones a
#: depositor or withdrawer can actually provoke are worth translating, because a
#: map of all thirty-nine would mostly be curator operations nobody here calls.
_REVERT_REASONS = {
    "0xe65b7a77": ("the deposit token has not been approved for this venue, or "
                   "the approval is for less than the amount"),
    "0x1eded19c": "the deposit token refused the transfer",
    "0xace2a47e": "the vault could not send the asset back",
    "0x2f0470fc": "the asset transfer out was refused",
    "0x861a96d6": "this address is not allowed to hold shares in this vault",
    "0x876736d1": "this address is not allowed to send shares",
    "0x8181c5ea": "this address is not allowed to receive the asset",
    "0x515b7cd9": "this address is not allowed to take the asset out",
    "0x4616e4af": "the vault is at its cap and cannot take more right now",
    "0x44e1772c": "the vault is at its cap and cannot take more right now",
    "0xa4875a49": "the vault is at its cap and cannot take more right now",
    "0x82b42900": "this address is not permitted to do that",
}

#: Solidity's arithmetic panic. Reached by asking to burn more shares than the
#: position holds, which subtracts past zero - so it reads as a corrupt-looking
#: internal error when it is really an ordinary "you do not have that much".
_PANIC_UNDERFLOW = "0x11"


def explain_revert(message: str) -> Optional[str]:
    """Turn a raw revert into something a person can act on, or None.

    Returns None when the failure is not one that is recognised, so the caller
    keeps the original text rather than replacing a specific error with a vague
    one.
    """
    lowered = message.lower()
    for selector, reason in _REVERT_REASONS.items():
        if selector in lowered:
            return reason
    if "panic error" in lowered and _PANIC_UNDERFLOW in lowered:
        return "that is more than the position holds"
    return None


class InsufficientLiquidity(MorphoError):
    """The venue could not source the amount asked for, right now.

    Kept apart from every other failure because it is the only one worth
    retrying unchanged. Asking for more than you hold is permanently wrong;
    asking for more than the vault can free today may work tomorrow, and a
    caller told to "try again" for the first case would loop forever.
    """


# ---------------------------------------------------------------------------
# Cap-id encoding
# ---------------------------------------------------------------------------
#
# Vault V2 stores a curator's caps under opaque bytes32 keys, and the adapter
# decides what those keys mean. From MorphoMarketV1AdapterV2:
#
#   ids[0] = keccak(abi.encode("this", adapter))
#   ids[1] = keccak(abi.encode("collateralToken", collateralToken))
#   ids[2] = keccak(abi.encode("this/marketParams", adapter, marketParams))
#
# The encoding is written out by hand rather than pulled from `eth_abi`, which
# is present only as a transitive dependency of web3. requirements.txt pins
# direct dependencies deliberately, and taking a new one for two fixed shapes
# would mean regenerating the lock. Both shapes are the same: a string, then
# some number of static 32-byte words. `test_morpho_ids` pins the result against
# ids read from the live chain, so a mistake here fails loudly rather than
# silently declaring every venue unendorsed.


def _word_address(value: str) -> bytes:
    return bytes(12) + bytes.fromhex(Web3.to_checksum_address(value)[2:])


def _word_uint(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def _encode_kind(kind: str, words: list[bytes]) -> bytes:
    """`abi.encode(string, ...static words)`.

    The string is dynamic, so the head holds an offset to it and every static
    word follows inline; the string's length and padded bytes go in the tail.
    """
    head_words = 1 + len(words)
    out = _word_uint(head_words * 32)
    for word in words:
        out += word
    raw = kind.encode()
    out += _word_uint(len(raw))
    out += raw + bytes((32 - len(raw) % 32) % 32)
    return out


def market_id(params: tuple) -> bytes:
    """The Morpho market id: keccak over the five packed parameter words.

    `MarketParamsLib.id()` hashes the struct's 160 bytes directly, so this is a
    plain concatenation with no offset or length prefix - unlike the cap ids
    above, which encode a string alongside.
    """
    loan, collateral, oracle, irm, lltv = params
    return Web3.keccak(
        _word_address(loan) + _word_address(collateral) + _word_address(oracle)
        + _word_address(irm) + _word_uint(lltv))


def adapter_cap_id(adapter: str) -> bytes:
    """The cap covering everything this adapter holds."""
    return Web3.keccak(_encode_kind("this", [_word_address(adapter)]))


def collateral_cap_id(collateral_token: str) -> bytes:
    """The cap covering every market sharing this collateral."""
    return Web3.keccak(_encode_kind("collateralToken", [_word_address(collateral_token)]))


def market_cap_id(adapter: str, params: tuple) -> bytes:
    """The cap covering this exact market, through this adapter.

    A non-zero `absoluteCap` under this id is what "the curator backs this
    market" means, and it is the only endorsement signal that is specific enough
    to act on - the other two are deliberately broader.
    """
    loan, collateral, oracle, irm, lltv = params
    return Web3.keccak(_encode_kind("this/marketParams", [
        _word_address(adapter), _word_address(loan), _word_address(collateral),
        _word_address(oracle), _word_address(irm), _word_uint(lltv),
    ]))


# ---------------------------------------------------------------------------
# Venues
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultVenue:
    """A Morpho vault the agent may supply to."""
    address: str
    name: str
    symbol: str
    curator: str
    asset: str
    asset_decimals: int
    share_decimals: int
    total_assets: int          # atomic, in asset units

    kind: str = "vault"

    @property
    def id(self) -> str:
        return self.address.lower()


@dataclass(frozen=True)
class MarketVenue:
    """A single Morpho Blue market the agent may supply to.

    `params` is the five-field struct the singleton wants for every call;
    `market_key` is its hash, which is what reads are keyed by. Both are carried
    because deriving one from the other on every call would be wasted hashing,
    and carrying only the hash would mean an extra chain read to act on it.
    """
    params: tuple
    market_key: bytes
    loan_token: str
    collateral_token: str
    collateral_symbol: str
    lltv: int
    loan_decimals: int
    endorsed_by: str           # the vault whose curator backs this market

    kind: str = "market"

    @property
    def id(self) -> str:
        return "0x" + self.market_key.hex()

    @property
    def lltv_percent(self) -> float:
        return self.lltv / 1e16


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MorphoAdapter:
    """Morpho read/build operations for one chain."""

    #: Bounds every call this adapter makes. Without it a slow or wedged RPC
    #: node blocks the caller indefinitely rather than failing in a way that
    #: can be caught and reported - the read paths already treat "chain
    #: unreadable" as a real, handled outcome, so this makes that reachable
    #: instead of it hanging first.
    _RPC_TIMEOUT_SECONDS = 15

    def __init__(self, rpc_url: str, config: MorphoConfig):
        self.w3 = Web3(Web3.HTTPProvider(
            rpc_url, request_kwargs={"timeout": self._RPC_TIMEOUT_SECONDS}))
        self.config = config
        self._morpho = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.morpho), abi=MORPHO_ABI)

    # ---- contracts -------------------------------------------------------

    def _vault(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=VAULT_V2_ABI)

    def _adapter(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=MARKET_ADAPTER_ABI)

    def _erc20(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=ERC20_ABI)

    @property
    def morpho_address(self) -> str:
        """The singleton. One approval here covers every market on the chain."""
        return Web3.to_checksum_address(self.config.morpho)

    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    # ---- discovery -------------------------------------------------------

    def discover_vaults(self) -> list[str]:
        """Every Vault V2 address on the chain, from the factory's own log.

        Slow - a full log scan, a minute or so against a public node - so this
        is a background refresh, never something a request waits on. What makes
        it worth doing is that a hardcoded list is wrong the moment a curator
        deploys another vault, and on this chain it already was: the two vaults
        originally shipped missed a third holding $48M.

        Returns every vault, of every curator. Filtering to the ones that matter
        is `resolve_venues`' job, because it is the curator check that decides
        and that check has to be a fresh read.
        """
        if not self.config.vault_factory:
            return []
        factory = Web3.to_checksum_address(self.config.vault_factory)
        found: list[str] = []
        seen: set[str] = set()
        try:
            head = self.w3.eth.block_number
        except Exception as e:
            raise MorphoError(f"could not reach the chain to list vaults: {e}") from e

        start = 0
        while start < head:
            end = min(start + _LOG_SCAN_STEP, head)
            try:
                logs = self.w3.eth.get_logs({
                    "address": factory, "topics": [CREATE_VAULT_TOPIC],
                    "fromBlock": start, "toBlock": end})
            except Exception as e:
                # One unreadable range must not lose the rest. A gap means a
                # vault is missed, which costs an agent access to it - not
                # access to something it should not have.
                logger.warning("vault scan failed for blocks %s-%s: %s", start, end, e)
                start = end
                continue
            for log in logs:
                # The event indexes three addresses; which is the vault is not
                # worth assuming, so every non-zero candidate is kept and the
                # ones that do not answer like a vault fall out below.
                for topic in log["topics"][1:]:
                    raw = topic.hex().replace("0x", "")
                    address = "0x" + raw[24:]
                    if int(address, 16) == 0:
                        continue
                    checksummed = Web3.to_checksum_address(address)
                    if checksummed not in seen:
                        seen.add(checksummed)
                        found.append(checksummed)
            start = end
        return found

    def curated_vaults(self, curators: list[str],
                       candidates: Optional[list[str]] = None) -> list[str]:
        """Of `candidates`, the vaults a trusted curator runs right now.

        `curator()` is read live for each. It is a mutable role with no timelock
        on these vaults, so a cached answer is a claim about the past.
        """
        trusted = {c.lower() for c in curators}
        if not trusted:
            return []
        keep = []
        for address in (candidates if candidates is not None
                        else self.discover_vaults()):
            try:
                if self.vault_curator(address).lower() in trusted:
                    keep.append(address)
            except MorphoError:
                continue  # not a vault, or unreadable; either way not ours
        return keep

    # ---- vault reads -----------------------------------------------------

    def vault_venue(self, address: str) -> VaultVenue:
        """Describe a vault, or raise if it does not answer like one."""
        v = self._vault(address)
        try:
            asset = v.functions.asset().call()
            return VaultVenue(
                address=Web3.to_checksum_address(address),
                name=v.functions.name().call(),
                symbol=v.functions.symbol().call(),
                curator=v.functions.curator().call(),
                asset=Web3.to_checksum_address(asset),
                asset_decimals=int(self._erc20(asset).functions.decimals().call()),
                share_decimals=int(v.functions.decimals().call()),
                total_assets=int(v.functions.totalAssets().call()),
            )
        except Exception as e:
            raise MorphoError(f"could not read vault {address}: {e}") from e

    def vault_curator(self, address: str) -> str:
        """Who curates this vault, read now.

        Deliberately not cached anywhere. `curator` is a mutable role - the
        owner can change it under timelock - so a trusted-curator check that
        consulted a value captured at startup would be answering a question
        about the past.
        """
        try:
            return self._vault(address).functions.curator().call()
        except Exception as e:
            raise MorphoError(f"could not read curator of {address}: {e}") from e

    def vault_position(self, address: str, owner: str) -> tuple[int, int]:
        """(shares held, what they are worth in asset units) — both atomic.

        The value is `previewRedeem`, not `convertToAssets`: the first is what
        the holder would actually receive, and it is the number a limit on
        exposure should be measured against.
        """
        v = self._vault(address)
        try:
            shares = int(v.functions.balanceOf(Web3.to_checksum_address(owner)).call())
            if shares == 0:
                return 0, 0
            return shares, int(v.functions.previewRedeem(shares).call())
        except Exception as e:
            raise MorphoError(f"could not read position in {address}: {e}") from e

    def preview_deposit(self, address: str, assets: int) -> int:
        """Shares this many asset units would mint. Asked, never computed."""
        try:
            return int(self._vault(address).functions.previewDeposit(int(assets)).call())
        except Exception as e:
            raise MorphoError(f"could not preview a deposit into {address}: {e}") from e

    def preview_withdraw(self, address: str, assets: int) -> int:
        """Shares that withdrawing this many asset units would burn."""
        try:
            return int(self._vault(address).functions.previewWithdraw(int(assets)).call())
        except Exception as e:
            raise MorphoError(f"could not preview a withdrawal from {address}: {e}") from e

    def preview_redeem(self, address: str, shares: int) -> int:
        """Asset units this many shares would return."""
        try:
            return int(self._vault(address).functions.previewRedeem(int(shares)).call())
        except Exception as e:
            raise MorphoError(f"could not preview a redemption from {address}: {e}") from e

    def gates_open(self, address: str, owner: str) -> bool:
        """Whether this address may hold shares and take assets back out.

        Vault V2 can be fitted with gates - a curator may point them at an
        allowlist contract, under timelock. Both RHC vaults have them set to the
        zero address today, meaning open to anyone, so this returns True. It is
        still asked before every deposit rather than assumed, because the day
        that changes is the day a deposit would otherwise revert with the money
        already approved.
        """
        v = self._vault(address)
        owner = Web3.to_checksum_address(owner)
        try:
            return bool(v.functions.canReceiveShares(owner).call()
                        and v.functions.canSendAssets(owner).call())
        except Exception as e:
            raise MorphoError(f"could not read the gates on {address}: {e}") from e

    # ---- liquidity -------------------------------------------------------

    def vault_market_keys(self, address: str) -> list[bytes]:
        """The markets this vault currently has an allocation in.

        Read from each adapter's own `marketIds`, which is the live set - the
        adapter drops a market from it when the allocation reaches zero. That
        makes this "endorsed and actually in use" rather than "capped at some
        point", which is the stricter of the two and the right one to offer.
        """
        keys: list[bytes] = []
        v = self._vault(address)
        try:
            for i in range(int(v.functions.adaptersLength().call())):
                adapter_address = v.functions.adapters(i).call()
                adapter = self._adapter(adapter_address)
                try:
                    count = int(adapter.functions.marketIdsLength().call())
                except Exception:
                    # Not a market adapter - Vault V2 can hold other kinds, and
                    # one that does not enumerate markets simply contributes
                    # none rather than failing the whole read.
                    logger.debug("adapter %s does not enumerate markets", adapter_address)
                    continue
                for j in range(count):
                    key = adapter.functions.marketIds(j).call()
                    if key not in keys:
                        keys.append(key)
        except MorphoError:
            raise
        except Exception as e:
            raise MorphoError(f"could not read the markets behind {address}: {e}") from e
        return keys

    def market_params(self, market_key: bytes) -> tuple:
        try:
            return tuple(self._morpho.functions.idToMarketParams(market_key).call())
        except Exception as e:
            raise MorphoError(f"could not read market {market_key.hex()}: {e}") from e

    def market_state(self, market_key: bytes) -> dict:
        try:
            m = self._morpho.functions.market(market_key).call()
        except Exception as e:
            raise MorphoError(f"could not read market {market_key.hex()}: {e}") from e
        return {
            "total_supply_assets": int(m[0]), "total_supply_shares": int(m[1]),
            "total_borrow_assets": int(m[2]), "total_borrow_shares": int(m[3]),
            "last_update": int(m[4]), "fee": int(m[5]),
        }

    def market_available(self, market_key: bytes) -> int:
        """What is not currently lent out — supplied minus borrowed.

        This is the whole of Morpho's "Total Liquidity" figure. It is plain
        arithmetic on one read, not something only an indexer can answer.
        """
        m = self.market_state(market_key)
        return max(0, m["total_supply_assets"] - m["total_borrow_assets"])

    def market_position(self, market_key: bytes, owner: str) -> int:
        """What `owner` has supplied to this market, in asset units."""
        return self.market_position_full(market_key, owner)[1]

    def market_position_full(self, market_key: bytes,
                             owner: str) -> tuple[int, int]:
        """(supply shares held, what they are worth in asset units) — atomic.

        Shaped like `vault_position` so a caller that needs the share count can
        have it without a second read. It needs one because a market position
        is not tokenised: there is no `balanceOf` to ask, and shares are the
        only denomination that names the position exactly.

        The asset figure is the market's own totals applied to the shares, which
        is the arithmetic the singleton does on withdrawal. It is an estimate in
        one respect: the totals read here do not include interest since the
        market last accrued, and `withdraw` accrues before it settles. So the
        position is worth this much or slightly more, never less - which is why
        a full exit goes by shares and not by this number.
        """
        try:
            pos = self._morpho.functions.position(
                market_key, Web3.to_checksum_address(owner)).call()
        except Exception as e:
            raise MorphoError(f"could not read a position in {market_key.hex()}: {e}") from e
        shares = int(pos[0])
        if shares == 0:
            return 0, 0
        m = self.market_state(market_key)
        if m["total_supply_shares"] == 0:
            return shares, 0
        return shares, shares * m["total_supply_assets"] // m["total_supply_shares"]

    def withdrawable(self, address: str) -> int:
        """How much could actually be taken out of this vault right now.

        `maxWithdraw()` is not usable for this. Vault V2 returns 0 from it
        unconditionally - measured against a holder sitting on 4.98M shares - so
        a caller that trusted it would refuse every withdrawal, and one that
        ignored it would find out by reverting after paying gas.

        The real answer is reconstructed the way Morpho's own interface does it:
        for each market behind the vault, the vault can free the smaller of what
        it has there and what is not already lent out; add whatever is sitting
        idle. Reconstruction has been checked against `totalAssets()` and agreed
        to within the interest accrued between the two reads.

        Note what this is not: a promise. It moves with other people's
        borrowing, and on RHC it currently sits near a tenth of deposits. Show
        it, do not bank on it.
        """
        venue = self.vault_venue(address)
        # Whatever is not lent out at all is available first.
        total = int(self._erc20(venue.asset).functions.balanceOf(
            Web3.to_checksum_address(address)).call())

        holders = self._market_holders(address)
        for key in self.vault_market_keys(address):
            # The position sits against the adapter that supplied it, and a
            # vault may run more than one, so a market's holding is the sum
            # across them.
            held = sum(self.market_position(key, holder) for holder in holders)
            total += min(held, self.market_available(key))
        return total

    def _market_holders(self, vault_address: str) -> list[str]:
        """The adapters that hold this vault's market positions.

        Positions are recorded against the adapter, not the vault, because the
        adapter is what calls `supply`.
        """
        v = self._vault(vault_address)
        return [v.functions.adapters(i).call()
                for i in range(int(v.functions.adaptersLength().call()))]

    # ---- venue resolution ------------------------------------------------

    def is_endorsed(self, vault_address: str, adapter_address: str,
                    params: tuple) -> bool:
        """Whether the vault's curator has put a cap behind this exact market."""
        try:
            cap = self._vault(vault_address).functions.absoluteCap(
                market_cap_id(adapter_address, params)).call()
        except Exception as e:
            raise MorphoError(
                f"could not read the cap for a market on {vault_address}: {e}") from e
        return int(cap) > 0

    def resolve_venues(self, curators: list[str],
                       candidate_vaults: Optional[list[str]] = None) -> list:
        """Every venue a trusted curator stands behind, read from the chain.

        This is the allowlist, and it is derived rather than configured. A
        curator address resolves to the vaults it curates and, through them, to
        the markets it has put a cap behind - which on RHC reproduces exactly
        the four markets Morpho's own interface shows, out of 124 that exist.

        `candidate_vaults` is the set to check, defaulting to the seed list in
        the network config. Callers that have run `discover_vaults()` pass the
        full set instead; either way a vault whose curator is not trusted is
        dropped here, and the check that drops it reads `curator()` now rather
        than trusting whatever produced the list.

        Costs a few dozen chain reads, so this is a periodic refresh and a
        startup step - not something to do on the way through a request.
        """
        trusted = {c.lower() for c in curators}
        if not trusted:
            return []
        venues: list = []
        # A market is one venue however many vaults endorse it. Both RHC vaults
        # back three of the same four markets, so without this the agent would
        # be offered the same market twice under different endorsements.
        seen_markets: set[bytes] = set()
        for address in (candidate_vaults if candidate_vaults is not None
                        else list(self.config.seed_vaults)):
            try:
                venue = self.vault_venue(address)
            except MorphoError:
                logger.warning("skipping unreadable vault %s", address)
                continue
            if venue.curator.lower() not in trusted:
                continue
            venues.append(venue)
            for market in self._markets_behind(venue):
                if market.market_key in seen_markets:
                    continue
                seen_markets.add(market.market_key)
                venues.append(market)
        return venues

    def _markets_behind(self, venue: VaultVenue) -> list[MarketVenue]:
        """The endorsed markets reachable through one vault."""
        found: list[MarketVenue] = []
        v = self._vault(venue.address)
        try:
            adapter_count = int(v.functions.adaptersLength().call())
        except Exception as e:
            raise MorphoError(f"could not read adapters of {venue.address}: {e}") from e

        for i in range(adapter_count):
            adapter_address = v.functions.adapters(i).call()
            adapter = self._adapter(adapter_address)
            try:
                count = int(adapter.functions.marketIdsLength().call())
            except Exception:
                continue
            for j in range(count):
                key = adapter.functions.marketIds(j).call()
                params = self.market_params(key)
                if not self.is_endorsed(venue.address, adapter_address, params):
                    continue
                loan, collateral = params[0], params[1]
                found.append(MarketVenue(
                    params=params, market_key=key,
                    loan_token=Web3.to_checksum_address(loan),
                    collateral_token=Web3.to_checksum_address(collateral),
                    collateral_symbol=self._symbol_or_address(collateral),
                    lltv=int(params[4]),
                    loan_decimals=int(self._erc20(loan).functions.decimals().call()),
                    endorsed_by=venue.address,
                ))
        return found

    def _symbol_or_address(self, token: str) -> str:
        """A token's symbol, falling back to its address.

        Plenty of the tokens on this chain do not answer `symbol()` - a market
        may name anything as collateral - and a venue listing that raised
        because one of them was rude would be worse than one that shows a hex
        string.
        """
        try:
            return self._erc20(token).functions.symbol().call()
        except Exception:
            return Web3.to_checksum_address(token)

    # ---- allowances ------------------------------------------------------

    def allowance(self, token: str, owner: str, spender: str) -> int:
        return int(self._erc20(token).functions.allowance(
            Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)).call())

    def build_approve_tx(self, token: str, spender: str, amount: int,
                         sender: str, gas: Optional[int] = None) -> dict:
        return self._erc20(token).functions.approve(
            Web3.to_checksum_address(spender), int(amount)
        ).build_transaction(self._tx_defaults(sender, gas))

    def approval_steps(self, token: str, owner: str, amount: int, spender: str,
                       label: str = "") -> list[tuple[dict, str]]:
        """Unsigned approvals needed before a supply can settle, in order.

        Shaped like `DexAdapter.approval_steps` so the service can count steps
        and describe them for a hardware prompt without knowing which lane it is
        driving. At most one: a vault pulls the asset itself, and a market pulls
        it through the singleton.

        An empty list means an existing allowance already covers the amount.
        """
        if self.allowance(token, owner, spender) >= amount:
            return []
        what = label or "the deposit token"
        return [(self.build_approve_tx(token, spender, amount, owner),
                 f"approve {what}")]

    # ---- unsigned transactions -------------------------------------------

    def _tx_defaults(self, sender: str, gas: Optional[int] = None) -> dict:
        """The fields every unsigned transaction here starts from.

        `gas` is optional and matters more than it looks. Left out, web3
        estimates it while building, which means asking the node to run the
        call - and a deposit built before its approval exists reverts with
        `TransferFromReverted` at that point, raising out of a *builder*. The
        trading lane never meets this because it approves first and builds the
        swap afterwards. This lane wants to rehearse the whole sequence before
        signing anything, so it needs to be able to build a transaction the
        chain would currently refuse. Passing a gas figure skips the estimate.
        """
        sender = Web3.to_checksum_address(sender)
        tx = {"from": sender,
              "nonce": self.w3.eth.get_transaction_count(sender),
              "chainId": self.w3.eth.chain_id,
              "value": 0}
        if gas is not None:
            tx["gas"] = int(gas)
        return tx

    def build_vault_deposit_tx(self, vault: str, assets: int, receiver: str,
                               sender: str, gas: Optional[int] = None) -> dict:
        return self._vault(vault).functions.deposit(
            int(assets), Web3.to_checksum_address(receiver)
        ).build_transaction(self._tx_defaults(sender, gas))

    def build_vault_withdraw_tx(self, vault: str, assets: int, receiver: str,
                                owner: str, sender: str,
                                gas: Optional[int] = None) -> dict:
        """Withdraw a stated number of asset units."""
        return self._vault(vault).functions.withdraw(
            int(assets), Web3.to_checksum_address(receiver),
            Web3.to_checksum_address(owner)
        ).build_transaction(self._tx_defaults(sender, gas))

    def build_vault_redeem_tx(self, vault: str, shares: int, receiver: str,
                              owner: str, sender: str,
                              gas: Optional[int] = None) -> dict:
        """Burn a stated number of shares.

        The right call for a full exit: withdrawing by asset amount leaves a
        dust share balance behind, because the amount was quoted a block before
        it settled and the share price moved underneath it.
        """
        return self._vault(vault).functions.redeem(
            int(shares), Web3.to_checksum_address(receiver),
            Web3.to_checksum_address(owner)
        ).build_transaction(self._tx_defaults(sender, gas))

    def build_market_supply_tx(self, params: tuple, assets: int,
                               on_behalf: str, sender: str,
                               gas: Optional[int] = None) -> dict:
        """Supply to a single market.

        `shares` is passed as 0: Morpho takes an amount in one denomination or
        the other and requires the unused one to be zero. Assets is the one the
        caller asked in.
        """
        return self._morpho.functions.supply(
            tuple(params), int(assets), 0, Web3.to_checksum_address(on_behalf), b""
        ).build_transaction(self._tx_defaults(sender, gas))

    def build_market_withdraw_tx(self, params: tuple, assets: int,
                                 on_behalf: str, receiver: str,
                                 sender: str, gas: Optional[int] = None,
                                 shares: int = 0) -> dict:
        """Withdraw from a market, in one denomination or the other.

        Morpho takes an amount in assets or in shares and requires the unused
        one to be zero, so exactly one of `assets` and `shares` may be non-zero.

        `shares` is the right one for a full exit. Morpho accrues interest
        inside `withdraw`, so a position is worth marginally more when it
        settles than when it was quoted; an asset-denominated exit is quoted
        against the earlier number and leaves that difference behind, and it
        cannot be arranged not to - the settling read does not exist yet when
        the amount has to be named. Naming the shares sidesteps the question.
        """
        if assets and shares:
            raise MorphoError(
                "a market withdrawal names assets or shares, not both")
        return self._morpho.functions.withdraw(
            tuple(params), int(assets), int(shares),
            Web3.to_checksum_address(on_behalf),
            Web3.to_checksum_address(receiver)
        ).build_transaction(self._tx_defaults(sender, gas))

    # ---- what a supply or withdrawal actually moved -----------------------

    def asset_moved(self, receipt, token: str, sender: str,
                     direction: str) -> Optional[int]:
        """Atomic units of `token` transferred between `sender` and the chain,
        from the receipt.

        A quote is a prediction and the settled transfer is the fact, and they
        can differ - a share-denominated withdrawal is quoted against an asset
        estimate that has moved by the time it settles, which is the whole
        reason that mode exists. `direction` is "out" for a supply (the asset
        leaves `sender`) or "in" for a withdrawal (the asset arrives at
        `sender`).

        Returns None when the transfer cannot be read rather than guessing.
        The caller must not substitute the quote for it: a quote recorded
        silently as the settled amount misreports an estimate as a fact.
        """
        try:
            token = Web3.to_checksum_address(token)
            wanted = DexAdapter._topic_address(sender)
            party_index = 1 if direction == "out" else 2
            total = 0
            found = False
            for log in DexAdapter._logs(receipt):
                topics = DexAdapter._topics(log)
                if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                    continue
                if DexAdapter._log_address(log) != token or topics[party_index] != wanted:
                    continue
                total += int(DexAdapter._log_data(log)[:66], 16)
                found = True
            return total if found else None
        except Exception:
            # Reading the transfer is bookkeeping, and the position has already
            # settled. An odd receipt shape must not turn a completed operation
            # into a reported failure.
            return None

    # ---- rehearsal -------------------------------------------------------

    def simulate(self, tx: dict, overrides: Optional[dict] = None) -> None:
        """Run a transaction without sending it. Raises if it would fail.

        The trading lane simulates before every swap for the same reason: a
        revert found here costs nothing, and one found on-chain costs gas and
        leaves the caller guessing.

        `overrides` allows rehearsing a deposit whose approval has not been
        granted yet - the node applies the state, answers, and forgets it - so
        the whole two-transaction sequence can be checked before the first one
        is signed.

        Raises `InsufficientLiquidity` when the venue simply cannot source the
        amount today, and `MorphoError` for everything else. The distinction is
        the point: only the first is worth retrying unchanged.
        """
        call = {k: v for k, v in tx.items()
                if k in ("from", "to", "data", "value", "gas")}
        try:
            if overrides:
                self.w3.eth.call(call, "latest", overrides)
            else:
                self.w3.eth.call(call)
        except Exception as e:
            message = str(e)
            if "insufficient liquidity" in message.lower():
                raise InsufficientLiquidity(
                    "the vault cannot free that much right now; a smaller "
                    "amount may work, or the same amount later") from e
            explained = explain_revert(message)
            if explained:
                raise MorphoError(f"this would fail on-chain: {explained}") from e
            raise MorphoError(f"this would fail on-chain: {message}") from e

    def estimate_gas(self, tx: dict, overrides: Optional[dict] = None) -> Optional[int]:
        """Best-effort gas estimate. None if the node will not give one."""
        call = {k: v for k, v in tx.items()
                if k in ("from", "to", "data", "value")}
        try:
            if overrides:
                return int(self.w3.eth.estimate_gas(call, "latest", overrides))
            return int(self.w3.eth.estimate_gas(call))
        except Exception as e:
            logger.debug("gas estimate unavailable: %s", e)
            return None
