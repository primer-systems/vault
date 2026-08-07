"""
Pricing / valuation — value a trade in USDG notional for the policy limits.

Two prices are deliberately kept separate (see the trading plan):
  - Execution price: on-chain QuoterV2 (in services/dex.py), used for slippage.
  - Valuation price: an off-chain ETH/USD reference (here), used only to size
    ETH-legged trades against the USDG limits. ETH is deep and globally uniform,
    so a reputable off-chain source is safe and avoids thin-pool manipulation.

USDG is treated as $1. A trade's notional is the value of its base-asset leg.
This module is Qt-free (GUI / CLI / headless).
"""

import json
import threading
import time
from decimal import Decimal
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

COINGECKO_ETH_USD = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
_CACHE_TTL_SECONDS = 60

_lock = threading.Lock()
_cache: dict[str, tuple[float, float]] = {}  # key -> (value, fetched_at)


class PricingError(Exception):
    """Could not obtain a valuation price."""


def _cached(key: str) -> Optional[float]:
    entry = _cache.get(key)
    if entry is None:
        return None
    value, fetched_at = entry
    if time.time() - fetched_at > _CACHE_TTL_SECONDS:
        return None
    return value


def get_eth_usd(timeout: float = 8.0, url: str = COINGECKO_ETH_USD) -> float:
    """Return the current ETH price in USD, cached for a minute.

    Raises PricingError if the reference is unreachable and nothing is cached.
    """
    with _lock:
        cached = _cached("eth_usd")
    if cached is not None:
        return cached
    try:
        req = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": "PrimerVault/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        price = float(data["ethereum"]["usd"])
        if price <= 0:
            raise PricingError("ETH/USD reference returned a non-positive price")
    except (URLError, KeyError, ValueError, TypeError) as e:
        # Fall back to a stale cached value if we have one, else fail.
        with _lock:
            stale = _cache.get("eth_usd")
        if stale is not None:
            return stale[0]
        raise PricingError(f"could not fetch ETH/USD reference: {e}") from e
    with _lock:
        _cache["eth_usd"] = (price, time.time())
    return price


def value_base_leg(base_token: str, amount_atomic: int, decimals: int,
                   usdg_addr: str, weth_addr: str,
                   eth_usd: Optional[float] = None) -> float:
    """Value a base-asset leg (USDG or WETH/ETH) in USDG notional.

    `base_token` must be USDG or WETH; the caller identifies which leg is the
    base asset. Returns a float USDG amount (USDG is treated as $1).
    """
    human = Decimal(amount_atomic) / (Decimal(10) ** decimals)
    if base_token.lower() == usdg_addr.lower():
        return float(human)
    if base_token.lower() == weth_addr.lower():
        price = eth_usd if eth_usd is not None else get_eth_usd()
        return float(human * Decimal(str(price)))
    raise PricingError(f"cannot value non-base leg as notional: {base_token}")
