"""
Vault Networks - Chain configurations and balance fetching via Blockscout API.

Robinhood Chain (RHC) mainnet only. Multi-network capable — add a NetworkConfig entry to support more.

Token discovery is automatic via Blockscout's V2 API - no hardcoded token lists needed.
"""

from dataclasses import dataclass, field
from typing import Optional
import json
import logging
import urllib.request
import urllib.error

from web3 import Web3

from .version import BLOCKSCOUT_USER_AGENT

logger = logging.getLogger(__name__)

# ============================================
# Network Configurations
# ============================================

@dataclass
class NetworkConfig:
    """Configuration for a blockchain network."""
    chain_id: int
    name: str
    display_name: str
    rpc_url: str
    explorer_url: str
    blockscout_api: str  # Blockscout V2 API base URL
    is_testnet: bool
    native_symbol: str
    native_decimals: int = 18
    aliases: list[str] = field(default_factory=list)


    @property
    def caip(self) -> str:
        """CAIP-2 network identifier, e.g. 'eip155:4663'. Derived from chain_id."""
        return f"eip155:{self.chain_id}"


# Supported networks. Multi-network capable — add a NetworkConfig entry to support more.
NETWORKS = {
    # === ROBINHOOD CHAIN ===
    4663: NetworkConfig(
        chain_id=4663,
        name="robinhood",
        display_name="Robinhood Chain",
        rpc_url="https://rpc.mainnet.chain.robinhood.com",
        explorer_url="https://robinhoodchain.blockscout.com",
        blockscout_api="https://robinhoodchain.blockscout.com/api/v2",
        is_testnet=False,
        native_symbol="ETH",
        aliases=["rhc", "robinhood-chain"],
    ),
}

# Default network
DEFAULT_NETWORK = 4663  # Robinhood Chain mainnet


# ============================================
# Derived name <-> CAIP-2 lookups
# ============================================

def _build_name_to_caip() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for cfg in NETWORKS.values():
        mapping[cfg.name] = cfg.caip
        for alias in cfg.aliases:
            mapping[alias] = cfg.caip
    return mapping


NAME_TO_CAIP: dict[str, str] = _build_name_to_caip()
CAIP_TO_NAME: dict[str, str] = {cfg.caip: cfg.name for cfg in NETWORKS.values()}


def name_to_caip(network: str) -> str:
    """Convert a v1 network name (or alias) to CAIP-2 form."""
    if network.startswith("eip155:"):
        return network
    return NAME_TO_CAIP.get(network.lower(), network)


def caip_to_name(network: str) -> str:
    """Convert a CAIP-2 network id to its canonical v1 name."""
    if not network.startswith("eip155:"):
        return network
    return CAIP_TO_NAME.get(network, network)


def resolve_network(network) -> Optional[NetworkConfig]:
    """Resolve a NetworkConfig from a chain id, v1 name/alias, or CAIP-2 id."""
    if isinstance(network, int):
        return NETWORKS.get(network)
    text = str(network)
    if text.startswith("eip155:"):
        try:
            return NETWORKS.get(int(text.split(":")[1]))
        except (ValueError, IndexError):
            return None
    caip = NAME_TO_CAIP.get(text.lower())
    if caip:
        return NETWORKS.get(int(caip.split(":")[1]))
    try:
        return NETWORKS.get(int(text))
    except ValueError:
        return None


# ============================================
# Token Configurations (for reference/Send dialog)
# ============================================

@dataclass
class TokenConfig:
    """Configuration for an ERC-20 token."""
    symbol: str
    name: str
    decimals: int
    addresses: dict[int, str]  # chain_id -> contract address


# Known tokens - used by SendDialog for token lookup by symbol
# Balances are discovered automatically via Blockscout API
TOKENS = {
    "USDG": TokenConfig(
        symbol="USDG",
        name="Global Dollar",
        decimals=6,
        addresses={
            4663: "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        }
    ),
}

# ============================================
# DEX (Uniswap v3) Configuration
# ============================================

# Native ETH is represented by address(0) in trade requests.
# The router accepts ETH via msg.value and wraps it internally.
ETH_ADDRESS = "0x0000000000000000000000000000000000000000"


def is_native_eth(token: str) -> bool:
    """True if token represents native ETH (address(0) or 'ETH')."""
    if not token:
        return False
    return token.upper() == "ETH" or token.lower() == ETH_ADDRESS


def is_wrap_trade(token_in: str, token_out: str, weth: str) -> bool:
    """True if this is an ETH -> WETH wrap (not a swap)."""
    return is_native_eth(token_in) and token_out.lower() == weth.lower()


def is_unwrap_trade(token_in: str, token_out: str, weth: str) -> bool:
    """True if this is a WETH -> ETH unwrap (not a swap)."""
    return token_in.lower() == weth.lower() and is_native_eth(token_out)


@dataclass
class DexConfig:
    """Uniswap v3 deployment for a chain (Qt-free; shared by services + UI).

    Addresses verified on-chain against RHC (chain 4663): the router reports this
    factory and WETH9, and USDG/WETH pools exist at all standard fee tiers.
    """
    factory: str
    quoter_v2: str
    swap_router: str        # Uniswap SwapRouter02
    weth: str               # Wrapped ETH used by v3 pools (v4 uses native ETH)
    fee_tiers: tuple[int, ...] = (100, 500, 3000, 10000)


DEX = {
    4663: DexConfig(
        factory="0x1f7d7550B1b028f7571E69A784071F0205FD2EfA",
        quoter_v2="0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7",
        swap_router="0xCaf681a66D020601342297493863E78C959E5cb2",
        weth="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    ),
}


def get_dex(chain_id: int) -> Optional[DexConfig]:
    """Return the Uniswap v3 deployment for a chain, or None if unsupported."""
    return DEX.get(chain_id)


@dataclass
class DexConfigV4:
    """Uniswap v4 deployment for a chain.

    V4 uses a singleton PoolManager architecture. Pools are identified by PoolKey:
    (currency0, currency1, fee, tickSpacing, hooks).

    RHC (chain 4663) has a modified UniversalRouter: its
    `IV4Router.ExactInputSingleParams` carries an extra `uint256 minHopPriceX36`
    between `amountOutMinimum` and `hookData`, so standard Uniswap SDK swap
    calldata will REVERT. Confirmed against the router's verified source on
    Blockscout. The quoter, StateView, PoolManager and Permit2 are all stock
    Uniswap - only the router differs. See services/dex_v4.py.

    Addresses from: https://github.com/Uniswap/contracts/blob/main/deployments/4663.md
    """
    pool_manager: str           # Singleton pool manager
    position_manager: str       # NFT position manager
    state_view: str             # For reading pool state (slot0, liquidity)
    quoter: str                 # V4Quoter contract
    universal_router: str       # V4 router (RHC-modified with minHopPriceX36)
    permit2: str                # Token approval management
    weth: str                   # WETH address (still needed for WETH pools)


DEX_V4 = {
    4663: DexConfigV4(
        pool_manager="0x8366a39CC670B4001A1121B8F6A443A643e40951",
        position_manager="0x58daec3116aae6D93017bAAea7749052E8a04fA7",
        state_view="0xF3334192D15450CdD385c8B70e03f9A6bD9E673b",
        quoter="0x8Dc178eFB8111BB0973Dd9d722ebeFF267c98F94",
        universal_router="0x8876789976dEcBfCbBbe364623C63652db8C0904",
        permit2="0x000000000022D473030F116dDEE9F6B43aC78BA3",
        weth="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    ),
}


def get_dex_v4(chain_id: int) -> Optional[DexConfigV4]:
    """Return the Uniswap v4 deployment for a chain, or None if unsupported."""
    return DEX_V4.get(chain_id)


# ============================================================
# DeFi (Morpho) Configuration
# ============================================================


@dataclass
class MorphoConfig:
    """Morpho deployment for a chain (Qt-free; shared by services + UI).

    Morpho is one singleton for every lending market on the chain, which is the
    opposite shape to Uniswap: there is no factory to ask and no per-venue
    address to configure. A market is identified by the hash of its parameters,
    and anyone may create one - 124 exist on RHC, of which four have depth and a
    curator behind them.

    So the address list here is deliberately short, and the interesting question
    - *which* venues may be used - is not answered by configuration at all. It is
    read from the chain against `default_curators`; see `services/morpho.py`.

    Addresses from https://docs.morpho.org/getting-started/resources/addresses/
    and verified on-chain: the singleton reports this IRM as enabled, and the
    vaults below report `curator()` matching.
    """
    morpho: str                     # the Blue singleton — every market lives here
    adaptive_curve_irm: str         # the IRM 121 of 124 RHC markets use
    vault_factory: str = ""         # every Vault V2 on the chain came from here
    default_curators: tuple[str, ...] = ()   # who Vault trusts, out of the box
    #: A warm start, not an allowlist. Discovery enumerates the factory to find
    #: every vault a trusted curator runs, which takes a minute against a public
    #: node - too long to sit in front of the first request. These are the ones
    #: that were true when the release was cut, used immediately while discovery
    #: runs behind. Being stale is harmless: every entry is re-checked against
    #: `curator()` before it is offered, so a vault that changed hands drops out
    #: and one that is missing gets added.
    seed_vaults: tuple[str, ...] = ()


MORPHO = {
    4663: MorphoConfig(
        morpho="0x9D53d5E3bd5E8d4Cbfa6DB1ca238AEA02E651010",
        adaptive_curve_irm="0x2BD3d5965B26B51814AC95127B2b80dD6CcC0fa1",
        vault_factory="0x0FBad98595b0186dA120E41f77C102beb49f803c",
        # Steakhouse. Curator of seven vaults on this chain as of 2026-08-28,
        # and of every market those vaults lend into.
        default_curators=("0x9023fbd6a08c666491a2d1648737e400cf42d2fb",),
        seed_vaults=(
            "0xBeEff033F34C046626B8D0A041844C5d1A5409dd",  # Steakhouse USDG
            "0xbEeFF0fb1Dc19344A87b8479dAb60A2e16160737",  # Ethena x Steakhouse USDG
            "0xbeEfFF136E3684273e6aA75A1669B784B373A4FD",  # Steakhouse Turbo USDG
            "0xBEEff039907422219Fb367e525954DDC092854d9",  # Grove x Steakhouse USDG
            "0x2007B597b730546eb885aD7b589bEE2f5dc07052",  # Ethena USDG Turbo
            "0xB97a135C344862bbacbB585A3B0Db051698CF905",  # Ethena USDG Turbo
            "0xE9c34c8Fe2d8452807eA13148b3F52b91354eA04",
        ),
    ),
}


def get_morpho(chain_id: int) -> Optional[MorphoConfig]:
    """Return the Morpho deployment for a chain, or None if unsupported."""
    return MORPHO.get(chain_id)


# ============================================
# Balance Data
# ============================================

@dataclass
class Balance:
    """A token or native balance with metadata from Blockscout."""
    symbol: str
    name: str
    raw: int                              # Raw balance in smallest unit
    decimals: int
    formatted: float                      # Human-readable balance
    token_address: Optional[str] = None   # Contract address (None for native)
    icon_url: Optional[str] = None        # Logo URL from Blockscout/CoinGecko
    exchange_rate: Optional[float] = None # USD price per token
    usd_value: Optional[float] = None     # Balance * exchange_rate
    fetch_failed: bool = False            # True if API call failed
    is_native: bool = False               # True for ETH, False for ERC-20


# ============================================
# Blockscout API Client
# ============================================

class BlockscoutClient:
    """Client for Blockscout V2 API - auto-discovers all tokens held by an address."""

    def __init__(self, network: NetworkConfig, timeout: int = 10,
                 w3: Optional[Web3] = None):
        self.network = network
        self.api_base = network.blockscout_api
        self.timeout = timeout
        #: Native-balance fallback for when Blockscout itself is unreachable -
        #: an outage, or its User-Agent rule rejecting this request outright
        #: (see BLOCKSCOUT_USER_AGENT). Every node answers eth_getBalance
        #: directly, so this is not an extra external dependency - unlike
        #: token discovery below, which has no RPC equivalent.
        self.w3 = w3

    def _request(self, endpoint: str) -> dict:
        """Make an API request to Blockscout."""
        url = f"{self.api_base}{endpoint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": BLOCKSCOUT_USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Debug, not warning: Robinhood Chain's Blockscout answers 500 for an
            # address with no on-chain history - the normal state of a wallet you
            # just made - so the very first balance check of every new wallet
            # would otherwise print what reads as an error. The caller handles the
            # failure and shows the balance (or a contained "connection failed"),
            # so this line is for diagnostics, not the user.
            logger.debug(f"Blockscout API error {e.code}: {url}")
            raise
        except urllib.error.URLError as e:
            logger.debug(f"Blockscout connection error: {e.reason}")
            raise
        except json.JSONDecodeError as e:
            logger.debug(f"Blockscout JSON parse error: {e}")
            raise

    def get_native_balance(self, address: str) -> Balance:
        """Get native ETH balance via Blockscout address endpoint."""
        try:
            data = self._request(f"/addresses/{address}")
            # Blockscout returns coin_balance in wei as a string
            raw = int(data.get("coin_balance", "0") or "0")
            formatted = raw / (10 ** self.network.native_decimals)

            # Fetch ETH price for USD value (uses cached CoinGecko price)
            exchange_rate = None
            usd_value = None
            try:
                from .services.pricing import get_eth_usd
                exchange_rate = get_eth_usd()
                usd_value = formatted * exchange_rate
            except Exception as e:
                logger.debug(f"Could not fetch ETH price for balance display: {e}")

            return Balance(
                symbol=self.network.native_symbol,
                name=self.network.native_symbol,
                raw=raw,
                decimals=self.network.native_decimals,
                formatted=formatted,
                token_address=None,
                icon_url=None,  # Could add ETH icon URL here
                exchange_rate=exchange_rate,
                usd_value=usd_value,
                fetch_failed=False,
                is_native=True,
            )
        except urllib.error.HTTPError as e:
            # 404/500 means address not yet indexed - treat as 0 balance
            if e.code in (404, 500):
                return Balance(
                    symbol=self.network.native_symbol,
                    name=self.network.native_symbol,
                    raw=0,
                    decimals=self.network.native_decimals,
                    formatted=0.0,
                    token_address=None,
                    icon_url=None,
                    fetch_failed=False,
                    is_native=True,
                )
            logger.warning(f"Failed to fetch native balance: HTTP {e.code}")
            fallback = self._native_balance_via_rpc(address)
            if fallback is not None:
                return fallback
            return Balance(
                symbol=self.network.native_symbol,
                name=self.network.native_symbol,
                raw=0,
                decimals=self.network.native_decimals,
                formatted=0.0,
                token_address=None,
                icon_url=None,
                fetch_failed=True,
                is_native=True,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch native balance: {e}")
            fallback = self._native_balance_via_rpc(address)
            if fallback is not None:
                return fallback
            return Balance(
                symbol=self.network.native_symbol,
                name=self.network.native_symbol,
                raw=0,
                decimals=self.network.native_decimals,
                formatted=0.0,
                token_address=None,
                icon_url=None,
                fetch_failed=True,
                is_native=True,
            )

    def _native_balance_via_rpc(self, address: str) -> Optional["Balance"]:
        """Read native balance straight from the chain when Blockscout can't
        answer at all - a WAF challenge or an outage in front of its API,
        not an "address not indexed" 404/500. No USD value: the price lookup
        is unrelated to why Blockscout failed and stays best-effort only in
        the primary path, not duplicated here.
        """
        if self.w3 is None:
            return None
        try:
            raw = self.w3.eth.get_balance(Web3.to_checksum_address(address))
        except Exception as e:
            logger.debug(f"RPC balance fallback also failed: {e}")
            return None
        formatted = raw / (10 ** self.network.native_decimals)
        return Balance(
            symbol=self.network.native_symbol,
            name=self.network.native_symbol,
            raw=raw,
            decimals=self.network.native_decimals,
            formatted=formatted,
            token_address=None,
            icon_url=None,
            fetch_failed=False,
            is_native=True,
        )

    def get_token_balances(self, address: str) -> list[Balance]:
        """
        Get ALL ERC-20 token balances for an address via Blockscout.

        This is the main auto-discovery endpoint - returns every token
        the address holds, with metadata including icons and prices.
        """
        balances = []
        try:
            data = self._request(f"/addresses/{address}/token-balances")

            for item in data:
                token = item.get("token", {})
                value_str = item.get("value", "0")

                # Parse token metadata
                symbol = token.get("symbol", "???")
                name = token.get("name", symbol)
                decimals_str = token.get("decimals", "18")
                decimals = int(decimals_str) if decimals_str else 18
                token_address = token.get("address_hash", "")
                icon_url = token.get("icon_url")  # May be None
                exchange_rate_str = token.get("exchange_rate")

                # Parse balance
                try:
                    raw = int(value_str)
                except (ValueError, TypeError):
                    raw = 0
                formatted = raw / (10 ** decimals) if decimals > 0 else float(raw)

                # Parse exchange rate (USD price)
                exchange_rate = None
                usd_value = None
                if exchange_rate_str:
                    try:
                        exchange_rate = float(exchange_rate_str)
                        usd_value = formatted * exchange_rate
                    except (ValueError, TypeError):
                        pass

                balances.append(Balance(
                    symbol=symbol,
                    name=name,
                    raw=raw,
                    decimals=decimals,
                    formatted=formatted,
                    token_address=token_address,
                    icon_url=icon_url,
                    exchange_rate=exchange_rate,
                    usd_value=usd_value,
                    fetch_failed=False,
                    is_native=False,
                ))

        except urllib.error.HTTPError as e:
            # 404/500 means address not indexed - just no tokens
            if e.code not in (404, 500):
                logger.warning(f"Failed to fetch token balances: HTTP {e.code}")
        except Exception as e:
            logger.warning(f"Failed to fetch token balances: {e}")

        return balances

    def get_all_balances(self, address: str) -> list[Balance]:
        """
        Get all balances (native + all ERC-20 tokens) for an address.

        Returns native ETH first, then all discovered tokens.
        """
        balances = []

        # Native balance (ETH)
        native = self.get_native_balance(address)
        balances.append(native)

        # All ERC-20 tokens (auto-discovered)
        tokens = self.get_token_balances(address)
        balances.extend(tokens)

        return balances


# ============================================
# Balance Fetcher (uses Blockscout)
# ============================================

class BalanceFetcher:
    """Fetches balances from Blockscout API."""

    def __init__(self, network: NetworkConfig, rpc_url: Optional[str] = None):
        """
        Initialize balance fetcher.

        Args:
            network: Network configuration
            rpc_url: Optional custom RPC URL (overrides network.rpc_url if provided)
        """
        self.network = network
        effective_rpc = rpc_url if rpc_url else network.rpc_url
        self.w3 = Web3(Web3.HTTPProvider(effective_rpc))
        # Native balance falls back to this Web3 instance when Blockscout's
        # API itself is unreachable; see BlockscoutClient._native_balance_via_rpc.
        self.client = BlockscoutClient(network, w3=self.w3)

    @property
    def is_connected(self) -> bool:
        """Check if connected to the network."""
        try:
            return self.w3.is_connected()
        except Exception:
            return False

    def get_native_balance(self, address: str) -> Balance:
        """Get native token balance."""
        return self.client.get_native_balance(address)

    def get_all_balances(self, address: str) -> list[Balance]:
        """Get all balances (native + all tokens) via Blockscout."""
        return self.client.get_all_balances(address)


class MultiNetworkBalanceFetcher:
    """Fetches balances across multiple networks."""

    def __init__(
        self,
        networks: Optional[list[int]] = None,
        custom_rpcs: Optional[dict[int, str]] = None
    ):
        if networks is None:
            networks = list(NETWORKS.keys())

        custom_rpcs = custom_rpcs or {}

        self.fetchers = {
            chain_id: BalanceFetcher(NETWORKS[chain_id], custom_rpcs.get(chain_id))
            for chain_id in networks
            if chain_id in NETWORKS
        }

    def get_all_balances(self, address: str) -> dict[int, list[Balance]]:
        """Get balances across all networks."""
        results = {}
        for chain_id, fetcher in self.fetchers.items():
            results[chain_id] = fetcher.get_all_balances(address)
        return results


# ============================================
# Utility Functions
# ============================================

def get_network(chain_id: int) -> Optional[NetworkConfig]:
    """Get network config by chain ID."""
    return NETWORKS.get(chain_id)


def get_network_by_name(name: str) -> Optional[NetworkConfig]:
    """Get network config by name."""
    for network in NETWORKS.values():
        if network.name == name:
            return network
    return None


def format_address(address: str, chars: int = 4) -> str:
    """Format address as 0x1234...5678"""
    if len(address) <= chars * 2 + 2:
        return address
    return f"{address[:chars+2]}...{address[-chars:]}"
