"""
Pricing / valuation — value a trade in USDG notional for the policy limits.

Two prices are deliberately kept separate (see the trading plan):
  - Execution price: on-chain QuoterV2 (in services/dex.py), used for slippage.
  - Valuation price: an off-chain ETH/USD reference (here), used only to size
    ETH-legged trades against the USDG limits. ETH is deep and globally uniform,
    so a reputable off-chain source is safe and avoids thin-pool manipulation.

USDG is treated as $1. A trade's notional is the value of its base-asset leg.
This module is Qt-free, so it ships in both editions.
"""

import json
import logging
import math
import threading
import time
from decimal import Decimal
from typing import Optional
from urllib.request import urlopen, Request

from ..version import USER_AGENT

logger = logging.getLogger(__name__)

COINGECKO_ETH_USD = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
_CACHE_TTL_SECONDS = 60

#: How far past its TTL a cached price may be served when the reference cannot
#: be reached.
#:
#: This price is what enforces per_trade_max_usd and daily_volume_limit_usd, so
#: serving one that is too old quietly widens the caps the user set. The bound
#: has to be short, because the conditions that stop a free endpoint answering -
#: an outage, a rate limit, a laptop waking from sleep - overlap with a fast
#: market, where an old number is furthest from a true one.
#:
#: Past this bound, valuation fails rather than guesses. That is not a dead end:
#: the trading service treats an unvaluable trade as one it must not decide
#: alone and escalates it to the user. Asking a human is the honest answer to
#: "we do not know what this is worth".
_STALE_MAX_SECONDS = 900  # 15 minutes

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

    If the reference cannot be reached, a cached price is served for up to
    _STALE_MAX_SECONDS past its TTL, with a warning. Beyond that the price is
    treated as unknown rather than guessed at.

    Raises:
        PricingError: if the reference is unreachable and no price recent enough
            to rely on is held.
    """
    with _lock:
        cached = _cached("eth_usd")
    if cached is not None:
        return cached
    try:
        req = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        price = float(data["ethereum"]["usd"])
        # Finite, not merely positive. json.loads accepts NaN and Infinity as an
        # extension to the spec, float() takes both, and every comparison against
        # NaN is false - so it passes a "> 0" test and then passes every limit
        # check downstream, including the daily volume total it would be added
        # to and saved into. A limit that answers "not exceeded" to everything
        # has stopped being a limit. Same trap the amount_in parsing already
        # guards against in models/trade.py.
        if not math.isfinite(price) or price <= 0:
            raise PricingError(
                f"ETH/USD reference returned an unusable price: {price}")
    # OSError, not URLError: urlopen wraps connection failures in URLError, but a
    # timeout while reading the response body comes back as a bare TimeoutError.
    # URLError is an OSError subclass, so catching the parent covers both - and
    # anything that escapes here escapes get_eth_usd as something the trading
    # service does not catch, turning "ask the user" into a failed trade.
    except (OSError, KeyError, ValueError, TypeError) as e:
        with _lock:
            stale = _cache.get("eth_usd")
        if stale is not None:
            value, fetched_at = stale
            age = time.time() - fetched_at
            if age <= _STALE_MAX_SECONDS:
                logger.warning(
                    "ETH/USD reference unreachable (%s); using a price %.0fs old. "
                    "Trade valuations are based on it until the feed returns.",
                    e, age)
                return value
            raise PricingError(
                f"the ETH/USD reference is unreachable and the last price is "
                f"{age / 60:.0f} minutes old, past the {_STALE_MAX_SECONDS // 60}"
                f"-minute limit for relying on it") from e
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
