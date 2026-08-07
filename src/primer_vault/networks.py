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
from pathlib import Path

from web3 import Web3

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
        quoter_v2="0x33e885ed0Ec9bF04eCFB19341582aadcB4c8a9E7",
        swap_router="0xcaf681a66d020601342297493863e78C959e5cb2",
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

    RHC (chain 4663) has a modified UniversalRouter with an extra `minHopPriceX36`
    field in swap structs - standard Uniswap SDK calldata will REVERT.

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
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        position_manager="0x58daec3116aae6d93017baaea7749052e8a04fa7",
        state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
        quoter="0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        universal_router="0x8876789976decbfcbbbe364623c63652db8c0904",
        permit2="0x000000000022d473030f116ddee9f6b43ac78ba3",
        weth="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    ),
}


def get_dex_v4(chain_id: int) -> Optional[DexConfigV4]:
    """Return the Uniswap v4 deployment for a chain, or None if unsupported."""
    return DEX_V4.get(chain_id)


# Minimal ERC-20 ABI for transfers (used by SendDialog)
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
]


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

    def __init__(self, network: NetworkConfig, timeout: int = 10):
        self.network = network
        self.api_base = network.blockscout_api
        self.timeout = timeout

    def _request(self, endpoint: str) -> dict:
        """Make an API request to Blockscout."""
        url = f"{self.api_base}{endpoint}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PrimerVault/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning(f"Blockscout API error {e.code}: {url}")
            raise
        except urllib.error.URLError as e:
            logger.warning(f"Blockscout connection error: {e.reason}")
            raise
        except json.JSONDecodeError as e:
            logger.warning(f"Blockscout JSON parse error: {e}")
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
        self.client = BlockscoutClient(network)
        # Keep Web3 for potential direct RPC fallback
        effective_rpc = rpc_url if rpc_url else network.rpc_url
        self.w3 = Web3(Web3.HTTPProvider(effective_rpc))

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
