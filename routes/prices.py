"""
Price feeds and proxy endpoints.

Extracted from server.py for modularity.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Price cache (module-level state)
# ---------------------------------------------------------------------------

_price_cache = {
    "btc_usdc": None,
    "last_update": 0,
    "cache_ttl": 60,
}

_httpx_client: Optional[httpx.AsyncClient] = None


def _get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(timeout=5.0)
    return _httpx_client


async def close_httpx_client():
    """Call on shutdown to clean up."""
    global _httpx_client
    if _httpx_client and not _httpx_client.is_closed:
        await _httpx_client.aclose()


# ---------------------------------------------------------------------------
# Callbacks set by server.py at init
# ---------------------------------------------------------------------------

# server.py sets this so we can update LP_CONFIG without importing it
_on_price_update = None  # callback(btc_price, usdc_m1_rate)
_btc_m1_fixed_rate = 100_000_000  # default, overridden by server.py

# API keys for authenticated access (loaded from ~/.BathronKey/api_keys.json)
_api_keys: Dict[str, str] = {}


def configure(btc_m1_fixed_rate: int, on_price_update=None,
              api_keys: Optional[Dict[str, str]] = None):
    """Configure price module. Called once at startup by server.py."""
    global _btc_m1_fixed_rate, _on_price_update, _api_keys
    _btc_m1_fixed_rate = btc_m1_fixed_rate
    _on_price_update = on_price_update
    if api_keys:
        _api_keys = {k: v for k, v in api_keys.items() if v}
        log.info(f"API keys configured: {[k for k in _api_keys]}")


def set_api_keys(keys: Dict[str, str]):
    """Update API keys at runtime (e.g. from dashboard POST)."""
    global _api_keys
    _api_keys.update({k: v for k, v in keys.items() if v})
    _api_keys = {k: v for k, v in _api_keys.items() if v}
    log.info(f"API keys updated: {[k for k in _api_keys]}")
    # Update price_sources_config visibility (boolean only)
    for source in price_sources_config:
        key_name = f"{source}_api_key"
        price_sources_config[source]["api_key"] = bool(_api_keys.get(key_name))


def get_api_keys_status() -> Dict[str, bool]:
    """Return which API keys are configured (never expose actual values)."""
    return {
        "binance": bool(_api_keys.get("binance_api_key")),
        "coingecko": bool(_api_keys.get("coingecko_api_key")),
        "kraken": bool(_api_keys.get("kraken_api_key")),
    }


def _get_headers_for_domain(domain: str) -> Dict[str, str]:
    """Return authentication headers for a given API domain."""
    headers: Dict[str, str] = {}
    if "binance.com" in domain and _api_keys.get("binance_api_key"):
        headers["X-MBX-APIKEY"] = _api_keys["binance_api_key"]
    elif "coingecko.com" in domain and _api_keys.get("coingecko_api_key"):
        headers["x-cg-pro-api-key"] = _api_keys["coingecko_api_key"]
    elif "kraken.com" in domain and _api_keys.get("kraken_api_key"):
        headers["API-Key"] = _api_keys["kraken_api_key"]
    return headers


# ---------------------------------------------------------------------------
# Core price fetch
# ---------------------------------------------------------------------------

async def fetch_live_btc_usdc_price() -> float:
    """Fetch live BTC/USDC price from Binance."""
    now = time.time()

    if (_price_cache["btc_usdc"] is not None and
            now - _price_cache["last_update"] < _price_cache["cache_ttl"]):
        return _price_cache["btc_usdc"]

    try:
        client = _get_httpx_client()
        response = await asyncio.wait_for(
            client.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDC",
                headers=_get_headers_for_domain("binance.com"),
            ),
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()
        price = float(data["price"])

        _price_cache["btc_usdc"] = price
        _price_cache["last_update"] = now

        usdc_m1_rate = _btc_m1_fixed_rate / price

        if _on_price_update:
            _on_price_update(price, usdc_m1_rate)

        log.info(f"Price updated: BTC/USDC={price:.2f}, USDC/M1={usdc_m1_rate:.2f}")
        return price
    except Exception as e:
        log.error(f"Failed to fetch live price: {e}")
        return _price_cache["btc_usdc"] or 76000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_json_path(data: dict, path: str):
    """Extract value from nested dict using dot notation path."""
    keys = path.replace('[', '.').replace(']', '').split('.')
    result = data
    for key in keys:
        if key.isdigit():
            result = result[int(key)]
        elif isinstance(result, dict):
            result = result.get(key)
        else:
            return None
        if result is None:
            return None
    return result


# ---------------------------------------------------------------------------
# Price source configuration
# ---------------------------------------------------------------------------

price_sources_config: Dict[str, Dict[str, Any]] = {
    "binance": {"enabled": True, "api_key": None},
    "mexc": {"enabled": False, "api_key": None},
    "coingecko": {"enabled": True, "api_key": None},
    "kraken": {"enabled": False, "api_key": None},
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/rates")
async def get_rates():
    """Get aggregated rates from configured sources and update LP pricing."""
    btc_price = await fetch_live_btc_usdc_price()
    usdc_m1_rate = _btc_m1_fixed_rate / btc_price

    return {
        "BTC": btc_price,
        "ETH": btc_price / 28,
        "USDC": 1.0,
        "M1": 1.0,
        "USDC_M1_rate": usdc_m1_rate,
        "sources": ["binance"],
        "timestamp": int(time.time()),
    }


@router.post("/api/rates/sources")
async def update_price_sources(sources: Dict[str, Any]):
    """Update price source configuration."""
    for source, config in sources.items():
        if source in price_sources_config:
            price_sources_config[source].update(config)
            log.info(f"Updated price source: {source}")
    return {"success": True, "sources": price_sources_config}


@router.get("/api/rates/sources")
async def get_price_sources():
    """Get price source configuration."""
    return {"sources": price_sources_config}


# ---------------------------------------------------------------------------
# Proxy URL cache — avoid upstream rate limits (CoinGecko 429 etc.)
# Cache: fresh (< TTL) → serve directly.  Stale (> TTL) → try upstream,
# on error fall back to stale data.  No data at all → propagate error.
# ---------------------------------------------------------------------------

_proxy_url_cache: Dict[str, Any] = {}   # { url: { "data": dict, "ts": float } }
_PROXY_CACHE_TTL = 60  # seconds — fresh window
_PROXY_STALE_TTL = 600  # seconds — serve stale on upstream error


@router.get("/api/proxy/price")
async def proxy_price(url: str = Query(...), path: str = Query("price")):
    """Proxy price API calls to avoid CORS issues. 60s cache + stale fallback."""
    allowed_domains = [
        "api.binance.com",
        "api.coingecko.com",
        "pro-api.coingecko.com",
        "api.kraken.com",
        "api.coinbase.com",
        "api.mexc.com",
    ]

    parsed = urlparse(url)

    # CoinGecko Pro: rewrite URL when API key is available
    if parsed.netloc == "api.coingecko.com" and _api_keys.get("coingecko_api_key"):
        url = url.replace("api.coingecko.com", "pro-api.coingecko.com")
        parsed = urlparse(url)

    if parsed.netloc not in allowed_domains:
        log.warning(f"Price proxy request to non-whitelisted domain: {parsed.netloc}")

    now = time.time()
    cached = _proxy_url_cache.get(url)

    # Fresh cache → serve directly
    if cached and (now - cached["ts"]) < _PROXY_CACHE_TTL:
        data = cached["data"]
    else:
        # Try upstream
        data = None
        try:
            client = _get_httpx_client()
            headers = _get_headers_for_domain(parsed.netloc)
            response = await asyncio.wait_for(
                client.get(url, headers=headers), timeout=10.0
            )
            response.raise_for_status()
            data = response.json()
            _proxy_url_cache[url] = {"data": data, "ts": now}
        except Exception as e:
            # Upstream failed — fall back to stale cache if available
            if cached and (now - cached["ts"]) < _PROXY_STALE_TTL:
                log.info(f"Upstream error ({e}), serving stale cache for {parsed.netloc}")
                data = cached["data"]
            else:
                # No cache at all — propagate error
                if isinstance(e, httpx.TimeoutException):
                    raise HTTPException(504, "Upstream timeout")
                elif isinstance(e, httpx.HTTPStatusError):
                    raise HTTPException(502, f"Upstream error: {e.response.status_code}")
                else:
                    log.error(f"Price proxy error: {e}")
                    raise HTTPException(500, f"Proxy error: {str(e)}")

    price = extract_json_path(data, path)

    if price is not None:
        return {"price": float(price), "source": parsed.netloc}
    else:
        return {"error": f"Could not extract price at path: {path}", "data": data}
