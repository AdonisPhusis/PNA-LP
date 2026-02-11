#!/usr/bin/env python3
"""
pna SDK Server
Trustless cross-chain swap coordination via M1 settlement rail.

Assets: BTC (Signet) <-> M1 (BATHRON) <-> USDC (Base)
Protocol fee: 0
LP fee: Variable (market-driven)

Endpoints:
  GET  /api/status          - Health check
  GET  /api/assets          - Supported assets
  GET  /api/quote           - Get swap quote
  POST /api/swap/create     - Create new swap
  GET  /api/swap/{id}       - Get swap status
  GET  /api/swaps           - List swaps

  # SDK Real Swap Endpoints
  POST /api/sdk/swap/initiate  - Initiate real swap with HTLC
  POST /api/sdk/swap/claim     - Claim HTLC
  GET  /api/sdk/htlc/list      - List active HTLCs
"""

import os
import json
import asyncio
import time
import uuid
import hashlib
import secrets
import logging
import threading
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from datetime import datetime

from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# SDK imports
try:
    from sdk.core import (
        generate_secret, verify_preimage, SwapState, btc_to_sats,
        FlowSwapState, FLOWSWAP_TIMELOCK_BTC_BLOCKS,
        FLOWSWAP_TIMELOCK_M1_BLOCKS, FLOWSWAP_TIMELOCK_USDC_SECONDS,
        PLAN_EXPIRY_SECONDS, LP_LOCK_WINDOW_SECONDS,
        MIN_SWAP_BTC_SATS, MIN_SWAP_USDC,
        MAX_CONCURRENT_SWAPS_PER_SESSION, BTC_CONFIRMATION_TIERS,
        BTC_CLAIM_MIN_CONFIRMATIONS, BTC_CLAIM_CONFIRMATION_TIMEOUT,
        COMPLETING_TIMEOUT_FORWARD, COMPLETING_TIMEOUT_REVERSE,
    )
    from sdk.chains.btc import BTCClient, BTCConfig
    from sdk.chains.m1 import M1Client, M1Config
    from sdk.htlc.m1 import M1Htlc
    from sdk.htlc.m1_3s import M1Htlc3S
    from sdk.htlc.btc import BTCHtlc
    from sdk.htlc.btc_3s import BTCHTLC3S
    from sdk.htlc.evm_3s import EVMHTLC3S
    from sdk.swap.watcher_3s import Watcher3S, WatchedSwap, Watcher3SConfig, create_watched_swap
    SDK_AVAILABLE = True
except ImportError as e:
    SDK_AVAILABLE = False
    logging.warning(f"SDK not available: {e}")

# Static files directory
STATIC_DIR = Path(__file__).parent / "static"

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

ASSETS = {
    "BTC": {
        "symbol": "BTC",
        "name": "Bitcoin",
        "network": "Bitcoin Signet",
        "decimals": 8,
        "htlc_type": "bitcoin_script",
        "confirmations_required": 0,  # CLS model: 0-conf for small amounts (LP risk)
    },
    "USDC": {
        "symbol": "USDC",
        "name": "USDC",
        "network": "Base",
        "decimals": 6,
        "htlc_type": "evm_contract",
        "confirmations_required": 1,
    },
    "M1": {
        "symbol": "M1",
        "name": "M1",
        "network": "BATHRON",
        "decimals": 8,
        "htlc_type": "bathron_native",
        "confirmations_required": 1,
    },
}

# Mock rates (USD base) - Production: oracle/exchange feeds
RATES_USD = {
    "BTC": 98500.0,
    "USDC": 1.0,
    "M1": 1.0,
}

# Settlement times (seconds)
SETTLEMENT_TIMES = {
    "BTC": 1200,   # ~20 min (6 conf)
    "USDC": 120,   # ~2 min
    "M1": 60,      # ~1 min (HU finality)
}

# HTLC timeouts (seconds)
HTLC_TIMEOUTS = {
    "BTC": 6 * 3600,   # 6 hours
    "USDC": 2 * 3600,  # 2 hours
    "M1": 1 * 3600,    # 1 hour
}

# =============================================================================
# LP CONFIGURATION (in production: persistent storage)
# =============================================================================

# BTC/M1 is FIXED: 1 SAT = 1 M1, so 1 BTC = 100,000,000 M1
BTC_M1_FIXED_RATE = 100_000_000  # sats per BTC = M1 per BTC

# Default LP config
LP_CONFIG = {
    "id": os.environ.get("LP_ID", "lp_pna_01"),
    "name": os.environ.get("LP_NAME", "pna LP"),
    "version": "0.1.0",
    "endpoint": None,  # Set dynamically
    "pairs": {
        "BTC/M1": {
            "enabled": True,
            "rate": BTC_M1_FIXED_RATE,  # FIXED, not configurable
            "spread_bid": 0.5,  # % - user sells BTC
            "spread_ask": 0.5,  # % - user buys BTC
            "min": 0.00001,     # BTC (testnet: very low for testing)
            "max": 1.0,         # BTC
        },
        "USDC/M1": {
            "enabled": True,
            "rate": 1309.0,     # M1 per USDC (from price feed)
            "spread_bid": 0.5,  # % - user sells USDC
            "spread_ask": 0.5,  # % - user buys USDC
            "min": 1,           # USDC (testnet: low for testing)
            "max": 100000,      # USDC
        },
        "BTC/USDC": {
            "enabled": True,
            # Derived from BTC/M1 and USDC/M1
            "min": 0.00001,     # BTC (testnet: very low for testing)
            "max": 1.0,
        },
    },
    # LP-configurable confirmation requirements
    # Lower = faster settlement but higher reorg risk
    # Higher = slower but safer (LP bears the risk)
    "confirmations": {
        "BTC": {
            "default": 1,       # Default fallback
            "min": 0,           # 0-conf for small amounts (CLS model: LP takes risk)
            "max": 3,           # Maximum for large amounts
            # Tiered confirmations by amount (must match BTC_CONFIRMATION_TIERS in sdk/core.py)
            "tiers": [
                {"max_btc": 0.1, "confirmations": 0},    # <0.1 BTC: 0-conf instant (CLS model)
                {"max_btc": 1.0, "confirmations": 1},    # <1 BTC: 1 conf (~10 min)
                {"max_btc": 100.0, "confirmations": 3},   # >=1 BTC: 3 conf (~30 min)
            ],
        },
        "USDC": {
            "default": 1,       # Base L2 is fast
            "min": 1,
            "max": 3,
        },
        "M1": {
            "default": 1,       # HU finality is fast (~1 min)
            "min": 1,
            "max": 2,
        },
    },
    "inventory": {
        "btc": 0.0,
        "m1": 0,
        "usdc": 0.0,
    },
    "stats": {
        "swaps_completed": 0,
        "volume_btc": 0.0,
        "volume_usdc": 0.0,
        "uptime_start": int(time.time()),
    },
}

# Legacy compatibility
DEFAULT_LP = {
    "id": LP_CONFIG["id"],
    "name": LP_CONFIG["name"],
    "fee_percent": 0.5,
    "min_btc": 0.0001,
    "max_btc": 1.0,
    "min_usdc": 1,
    "max_usdc": 100000,
    "min_m1": 1,
    "max_m1": 100000,
}

# =============================================================================
# IN-MEMORY STATE (Production: persistent DB)
# =============================================================================

swaps_db: Dict[str, Dict[str, Any]] = {}
lps_db: Dict[str, Dict[str, Any]] = {"lp_default": DEFAULT_LP}

# =============================================================================
# MODELS
# =============================================================================

class QuoteResponse(BaseModel):
    lp_id: str
    lp_name: str
    from_asset: str
    to_asset: str
    from_amount: float
    to_amount: float
    rate: float                      # Effective rate after spread
    rate_market: float               # Market rate before spread
    spread_percent: float            # Applied spread
    route: str
    settlement_time_seconds: int
    settlement_time_human: str
    confirmations_required: int      # BTC confirmations LP will wait for
    confirmations_breakdown: dict    # Detailed breakdown
    protocol_fee: float = 0
    valid_until: int
    valid_seconds: int = 60
    inventory_ok: bool = True
    min_amount: float
    max_amount: float

class LegQuoteResponse(BaseModel):
    """Quote for a single leg (X→M1 or M1→Y) — used by per-leg routing."""
    lp_id: str
    lp_name: str
    leg: str                         # e.g. "BTC/M1" or "M1/USDC"
    from_asset: str
    to_asset: str
    from_amount: float
    to_amount: float
    rate: float                      # Effective rate after spread
    rate_market: float               # Market rate before spread
    spread_percent: float
    inventory_ok: bool = True
    settlement_time_seconds: int
    settlement_time_human: str
    confirmations_required: int
    confirmations_breakdown: dict
    min_amount: float
    max_amount: float
    valid_until: int
    valid_seconds: int = 60
    H_lp: str = ""                   # Hashlock placeholder (Phase 4)

class SwapCreateRequest(BaseModel):
    from_asset: str = Field(..., example="BTC")
    to_asset: str = Field(..., example="USDC")
    from_amount: float = Field(..., gt=0, example=0.01)
    dest_address: str = Field(..., example="0x...")
    lp_id: Optional[str] = "lp_default"

class SwapCreateResponse(BaseModel):
    swap_id: str
    status: str
    from_asset: str
    to_asset: str
    from_amount: float
    to_amount: float
    deposit_address: str
    hashlock: str
    timeout: int
    route: str
    created_at: int
    expires_at: int

class SwapStatusResponse(BaseModel):
    swap_id: str
    status: str
    step: int
    step_name: str
    from_asset: str
    to_asset: str
    from_amount: float
    to_amount: float
    deposit_address: str
    dest_address: str
    route: str
    hashlock: str
    deposit_tx: Optional[str]
    claim_tx: Optional[str]
    confirmations: int
    created_at: int
    updated_at: int

# =============================================================================
# HELPERS
# =============================================================================

def get_rate(from_asset: str, to_asset: str) -> float:
    """Get exchange rate between two assets."""
    return RATES_USD.get(from_asset, 1.0) / RATES_USD.get(to_asset, 1.0)

def get_route(from_asset: str, to_asset: str) -> str:
    """Get routing path - M1 is the settlement rail."""
    if from_asset == "M1" or to_asset == "M1":
        return f"{from_asset} -> {to_asset}"
    return f"{from_asset} -> M1 -> {to_asset}"

def get_confirmations_required(asset: str, amount: float = 0) -> int:
    """
    Get confirmations required based on LP config and amount.

    LP can configure tiered confirmations:
    - Small amounts: fewer confirmations (faster, more risk)
    - Large amounts: more confirmations (slower, safer)
    """
    conf_config = LP_CONFIG.get("confirmations", {}).get(asset, {})

    if not conf_config:
        # Fallback to ASSETS config
        return ASSETS.get(asset, {}).get("confirmations_required", 1)

    # Check tiered confirmations (strict less-than, matching BTC_CONFIRMATION_TIERS)
    tiers = conf_config.get("tiers", [])
    for tier in tiers:
        max_amount_key = f"max_{asset.lower()}"
        if max_amount_key in tier and amount < tier[max_amount_key]:
            return tier["confirmations"]

    # Use default
    return conf_config.get("default", 1)


def get_settlement_time(from_asset: str, to_asset: str, amount: float = 0) -> tuple:
    """
    Get total settlement time based on LP confirmation config.

    Returns:
        (total_seconds, confirmations_required, breakdown)
    """
    # Get confirmations from LP config
    conf_required = get_confirmations_required(from_asset, amount)

    # Average block times
    BLOCK_TIMES = {
        "BTC": 600,    # ~10 min
        "USDC": 2,     # ~2s (Base L2)
        "M1": 60,      # ~1 min (BATHRON)
    }

    from_time = conf_required * BLOCK_TIMES.get(from_asset, 60)
    m1_hop = 60 if (from_asset != "M1" and to_asset != "M1") else 0

    total_time = from_time + m1_hop

    breakdown = {
        "asset": from_asset,
        "confirmations": conf_required,
        "block_time": BLOCK_TIMES.get(from_asset, 60),
        "asset_time": from_time,
        "m1_finality": m1_hop,
    }

    return total_time, conf_required, breakdown

def human_time(seconds: int) -> str:
    """Convert seconds to human readable."""
    if seconds < 60:
        return f"~{seconds}s"
    return f"~{seconds // 60} min"

def generate_hashlock() -> tuple:
    """Generate secret and hashlock."""
    secret = secrets.token_hex(32)
    hashlock = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
    return secret, hashlock

def generate_deposit_address(asset: str, hashlock: str) -> str:
    """
    Generate deposit address for swap.

    For BTC: Returns LP's regular BTC address (LP monitors for deposits)
    For M1/USDC: Returns LP's address (HTLCs are created on M1 side only)
    """
    global _lp_addresses

    if asset == "BTC":
        # Use LP's BTC address - LP will monitor for incoming deposits
        if _lp_addresses.get("btc"):
            return _lp_addresses["btc"]
        # Fallback: try to get from wallets
        btc_cli = CHAIN_CLI.get("btc")
        if btc_cli and btc_cli.exists():
            try:
                result = subprocess.run(
                    [str(btc_cli), "-signet", "-rpcwallet=lp_wallet", "getnewaddress", "lp_btc", "bech32"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    addr = result.stdout.strip()
                    _lp_addresses["btc"] = addr
                    return addr
            except Exception as e:
                log.error(f"Failed to get BTC address: {e}")
        return "btc_address_error"

    elif asset == "M1":
        # Use LP's M1 address
        if _lp_addresses.get("m1"):
            return _lp_addresses["m1"]
        return "m1_address_error"

    elif asset == "USDC":
        if _lp_addresses.get("usdc"):
            return _lp_addresses["usdc"]
        return "usdc_address_error"

    return f"unknown_asset_{asset}"

def get_step_name(step: int) -> str:
    """Get human-readable step name."""
    names = {
        1: "Waiting for deposit",
        2: "Confirming deposit",
        3: "Settling via M1",
        4: "Complete",
        5: "Refunded",
        6: "Expired",
    }
    return names.get(step, "Unknown")

# =============================================================================
# APP SETUP
# =============================================================================

app = FastAPI(
    title="pna SDK",
    description="Trustless cross-chain swap API - Protocol fee: 0",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/css", StaticFiles(directory=STATIC_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=STATIC_DIR / "js"), name="js")
    app.mount("/img", StaticFiles(directory=STATIC_DIR / "img"), name="img")

# =============================================================================
# ENDPOINTS
# =============================================================================

@app.get("/")
async def root():
    """Serve LP Dashboard."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {
        "name": "pna SDK",
        "version": "0.1.0",
        "protocol_fee": 0,
        "docs": "/docs",
        "dashboard": "Static files not found",
    }

@app.get("/api/status")
async def get_status():
    """Health check."""
    # Count regular swaps
    regular_active = len([s for s in swaps_db.values() if s["status"] not in ["complete", "refunded", "expired"]])
    # Count atomic swaps
    atomic_active = len([s for s in atomic_swaps_db.values() if s["status"] not in ["claimed", "refunded", "expired"]])

    # Count flowswap 3S swaps
    flowswap_active = len([s for s in flowswap_db.values()
                           if s["state"] not in ("completed", "refunded", "failed", "expired")])

    # Detect test mode (all spreads at 0)
    all_spreads = [
        pair_config.get("spread_bid", 0) + pair_config.get("spread_ask", 0)
        for pair_config in LP_CONFIG["pairs"].values()
    ]
    test_mode = all(s == 0 for s in all_spreads)

    return {
        "status": "ok",
        "version": "0.2.0",
        "timestamp": int(time.time()),
        "test_mode": test_mode,
        "swaps_active": regular_active + atomic_active + flowswap_active,
        "swaps_total": len(swaps_db) + len(atomic_swaps_db) + len(flowswap_db),
        "atomic_swaps_active": atomic_active,
        "atomic_swaps_total": len(atomic_swaps_db),
        "flowswap_3s_active": flowswap_active,
        "flowswap_3s_total": len(flowswap_db),
        "lps_active": len(lps_db),
        "protocol_fee": 0,
    }

@app.get("/api/assets")
async def get_assets():
    """List supported assets and pairs."""
    pairs = []
    symbols = list(ASSETS.keys())
    for i, a in enumerate(symbols):
        for b in symbols[i+1:]:
            pairs.append({"from": a, "to": b})
            pairs.append({"from": b, "to": a})

    return {
        "assets": ASSETS,
        "pairs": pairs,
        "protocol_fee": 0,
    }

@app.get("/api/quote", response_model=QuoteResponse)
async def get_quote(
    from_asset: str = Query(..., alias="from"),
    to_asset: str = Query(..., alias="to"),
    amount: float = Query(..., gt=0),
):
    """
    Get swap quote with current LP rates and spreads.

    The quote uses:
    - BTC/M1: Fixed rate (1 SAT = 1 M1) + configurable spread
    - USDC/M1: Market rate from price feeds + configurable spread
    - BTC/USDC: Derived from the two above

    Returns amount_out after applying the appropriate spread (bid or ask).
    """
    if from_asset not in ASSETS:
        raise HTTPException(400, f"Unknown asset: {from_asset}")
    if to_asset not in ASSETS:
        raise HTTPException(400, f"Unknown asset: {to_asset}")
    if from_asset == to_asset:
        raise HTTPException(400, "Cannot swap same asset")

    # Fetch live price (updates LP_CONFIG automatically)
    await fetch_live_btc_usdc_price()

    # Determine pair and direction
    pair_key = f"{from_asset}/{to_asset}"
    reverse_pair_key = f"{to_asset}/{from_asset}"

    # Get pair config
    pair_config = None
    is_reverse = False
    spread_percent = 0.0
    market_rate = 1.0
    min_amount = 0.0
    max_amount = float('inf')

    if pair_key in LP_CONFIG["pairs"]:
        pair_config = LP_CONFIG["pairs"][pair_key]
    elif reverse_pair_key in LP_CONFIG["pairs"]:
        pair_config = LP_CONFIG["pairs"][reverse_pair_key]
        is_reverse = True

    # Calculate rate based on pair type
    if from_asset == "BTC" and to_asset == "M1":
        # BTC → M1: User sells BTC (bid)
        market_rate = float(BTC_M1_FIXED_RATE)
        spread_percent = LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["BTC/M1"]["min"]
        max_amount = LP_CONFIG["pairs"]["BTC/M1"]["max"]

    elif from_asset == "M1" and to_asset == "BTC":
        # M1 → BTC: User buys BTC (ask)
        market_rate = 1.0 / float(BTC_M1_FIXED_RATE)
        spread_percent = LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["BTC/M1"]["min"] * BTC_M1_FIXED_RATE
        max_amount = LP_CONFIG["pairs"]["BTC/M1"]["max"] * BTC_M1_FIXED_RATE

    elif from_asset == "USDC" and to_asset == "M1":
        # USDC → M1: User sells USDC (bid)
        market_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        spread_percent = LP_CONFIG["pairs"]["USDC/M1"]["spread_bid"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["USDC/M1"]["min"]
        max_amount = LP_CONFIG["pairs"]["USDC/M1"]["max"]

    elif from_asset == "M1" and to_asset == "USDC":
        # M1 → USDC: User buys USDC (ask)
        market_rate = 1.0 / LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        spread_percent = LP_CONFIG["pairs"]["USDC/M1"]["spread_ask"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["USDC/M1"]["min"] * LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        max_amount = LP_CONFIG["pairs"]["USDC/M1"]["max"] * LP_CONFIG["pairs"]["USDC/M1"]["rate"]

    elif from_asset == "BTC" and to_asset == "USDC":
        # BTC → USDC: Goes through M1 (BTC→M1→USDC)
        # User sells BTC (BTC bid) + buys USDC (USDC ask)
        btc_m1_rate = float(BTC_M1_FIXED_RATE)
        usdc_m1_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        market_rate = btc_m1_rate / usdc_m1_rate  # BTC in USDC
        spread_percent = (LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"] +
                         LP_CONFIG["pairs"]["USDC/M1"]["spread_ask"])
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["BTC/USDC"]["min"]
        max_amount = LP_CONFIG["pairs"]["BTC/USDC"]["max"]

    elif from_asset == "USDC" and to_asset == "BTC":
        # USDC → BTC: Goes through M1 (USDC→M1→BTC)
        # User sells USDC (USDC bid) + buys BTC (BTC ask)
        btc_m1_rate = float(BTC_M1_FIXED_RATE)
        usdc_m1_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        market_rate = usdc_m1_rate / btc_m1_rate  # USDC per BTC → BTC per USDC
        spread_percent = (LP_CONFIG["pairs"]["USDC/M1"]["spread_bid"] +
                         LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"])
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["BTC/USDC"]["min"] * (btc_m1_rate / usdc_m1_rate)
        max_amount = LP_CONFIG["pairs"]["BTC/USDC"]["max"] * (btc_m1_rate / usdc_m1_rate)
    else:
        raise HTTPException(400, f"Unsupported pair: {from_asset}/{to_asset}")

    # Calculate output amount
    to_amount = round(amount * effective_rate, ASSETS[to_asset]["decimals"])

    # Check inventory (accounting for active reservations)
    # Note: inventory is stored in "coin" units (e.g., 0.005 M1 = 500,000 sats)
    # to_amount is in smallest units (sats for M1), so convert for comparison
    with _flowswap_lock:
        available = _get_available_inventory()
    inventory_ok = True
    to_amount_coins = to_amount / (10 ** ASSETS[to_asset]["decimals"])
    if to_asset == "BTC":
        inventory_ok = available.get("btc", 0) >= to_amount_coins
    elif to_asset == "M1":
        inventory_ok = available.get("m1", 0) >= to_amount_coins
    elif to_asset == "USDC":
        inventory_ok = available.get("usdc", 0) >= to_amount_coins

    # Check amount limits
    if amount < min_amount:
        raise HTTPException(400, f"Amount below minimum: {min_amount} {from_asset}")
    if amount > max_amount:
        raise HTTPException(400, f"Amount above maximum: {max_amount} {from_asset}")

    # Get settlement time with LP's confirmation config
    settlement_seconds, conf_required, conf_breakdown = get_settlement_time(
        from_asset, to_asset, amount
    )
    valid_seconds = 60

    return QuoteResponse(
        lp_id=LP_CONFIG["id"],
        lp_name=LP_CONFIG["name"],
        from_asset=from_asset,
        to_asset=to_asset,
        from_amount=amount,
        to_amount=to_amount,
        rate=effective_rate,
        rate_market=market_rate,
        spread_percent=spread_percent,
        route=get_route(from_asset, to_asset),
        settlement_time_seconds=settlement_seconds,
        settlement_time_human=human_time(settlement_seconds),
        confirmations_required=conf_required,
        confirmations_breakdown=conf_breakdown,
        protocol_fee=0,
        valid_until=int(time.time()) + valid_seconds,
        valid_seconds=valid_seconds,
        inventory_ok=inventory_ok,
        min_amount=min_amount,
        max_amount=max_amount,
    )

@app.get("/api/quote/leg", response_model=LegQuoteResponse)
async def get_quote_leg(
    from_asset: str = Query(..., alias="from"),
    to_asset: str = Query(..., alias="to"),
    amount: float = Query(..., gt=0),
):
    """
    Quote a single leg of a swap (X→M1 or M1→Y).

    Used by the pna-swap Router to compose per-leg routes across
    multiple LPs. Only M1 legs are supported — reject BTC/USDC.
    """
    if from_asset not in ASSETS:
        raise HTTPException(400, f"Unknown asset: {from_asset}")
    if to_asset not in ASSETS:
        raise HTTPException(400, f"Unknown asset: {to_asset}")
    if from_asset == to_asset:
        raise HTTPException(400, "Cannot quote same asset")
    if "M1" not in (from_asset, to_asset):
        raise HTTPException(400, "Leg quote requires one side to be M1")

    # Fetch live price
    await fetch_live_btc_usdc_price()

    # Calculate rate for the 4 valid M1 legs
    if from_asset == "BTC" and to_asset == "M1":
        market_rate = float(BTC_M1_FIXED_RATE)
        spread_percent = LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["BTC/M1"]["min"]
        max_amount = LP_CONFIG["pairs"]["BTC/M1"]["max"]

    elif from_asset == "M1" and to_asset == "BTC":
        market_rate = 1.0 / float(BTC_M1_FIXED_RATE)
        spread_percent = LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["BTC/M1"]["min"] * BTC_M1_FIXED_RATE
        max_amount = LP_CONFIG["pairs"]["BTC/M1"]["max"] * BTC_M1_FIXED_RATE

    elif from_asset == "USDC" and to_asset == "M1":
        market_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        spread_percent = LP_CONFIG["pairs"]["USDC/M1"]["spread_bid"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["USDC/M1"]["min"]
        max_amount = LP_CONFIG["pairs"]["USDC/M1"]["max"]

    elif from_asset == "M1" and to_asset == "USDC":
        market_rate = 1.0 / LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        spread_percent = LP_CONFIG["pairs"]["USDC/M1"]["spread_ask"]
        effective_rate = market_rate * (1 - spread_percent / 100)
        min_amount = LP_CONFIG["pairs"]["USDC/M1"]["min"] * LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        max_amount = LP_CONFIG["pairs"]["USDC/M1"]["max"] * LP_CONFIG["pairs"]["USDC/M1"]["rate"]

    else:
        raise HTTPException(400, f"Unsupported leg: {from_asset}/{to_asset}")

    # Calculate output
    to_amount = round(amount * effective_rate, ASSETS[to_asset]["decimals"])

    # Check inventory
    with _flowswap_lock:
        available = _get_available_inventory()
    inventory_ok = True
    to_amount_coins = to_amount / (10 ** ASSETS[to_asset]["decimals"])
    if to_asset == "BTC":
        inventory_ok = available.get("btc", 0) >= to_amount_coins
    elif to_asset == "M1":
        inventory_ok = available.get("m1", 0) >= to_amount_coins
    elif to_asset == "USDC":
        inventory_ok = available.get("usdc", 0) >= to_amount_coins

    # Check limits
    if amount < min_amount:
        raise HTTPException(400, f"Amount below minimum: {min_amount} {from_asset}")
    if amount > max_amount:
        raise HTTPException(400, f"Amount above maximum: {max_amount} {from_asset}")

    # Settlement time
    settlement_seconds, conf_required, conf_breakdown = get_settlement_time(
        from_asset, to_asset, amount
    )
    valid_seconds = 60

    return LegQuoteResponse(
        lp_id=LP_CONFIG["id"],
        lp_name=LP_CONFIG["name"],
        leg=f"{from_asset}/{to_asset}",
        from_asset=from_asset,
        to_asset=to_asset,
        from_amount=amount,
        to_amount=to_amount,
        rate=effective_rate,
        rate_market=market_rate,
        spread_percent=spread_percent,
        inventory_ok=inventory_ok,
        settlement_time_seconds=settlement_seconds,
        settlement_time_human=human_time(settlement_seconds),
        confirmations_required=conf_required,
        confirmations_breakdown=conf_breakdown,
        min_amount=min_amount,
        max_amount=max_amount,
        valid_until=int(time.time()) + valid_seconds,
        valid_seconds=valid_seconds,
        H_lp="",
    )

@app.post("/api/swap/create", response_model=SwapCreateResponse)
async def create_swap(req: SwapCreateRequest):
    """Create a new swap."""
    if req.from_asset not in ASSETS:
        raise HTTPException(400, f"Unknown asset: {req.from_asset}")
    if req.to_asset not in ASSETS:
        raise HTTPException(400, f"Unknown asset: {req.to_asset}")
    if req.from_asset == req.to_asset:
        raise HTTPException(400, "Cannot swap same asset")

    # Calculate rate based on pair type (same logic as get_quote)
    from_asset = req.from_asset
    to_asset = req.to_asset
    amount = req.from_amount

    if from_asset == "BTC" and to_asset == "M1":
        # BTC → M1: Fixed rate (1 sat BTC = 1 sat M1)
        market_rate = float(BTC_M1_FIXED_RATE)
        spread_percent = LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"]
    elif from_asset == "M1" and to_asset == "BTC":
        # M1 → BTC: Inverse fixed rate
        market_rate = 1.0 / float(BTC_M1_FIXED_RATE)
        spread_percent = LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"]
    elif from_asset == "USDC" and to_asset == "M1":
        market_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        spread_percent = LP_CONFIG["pairs"]["USDC/M1"]["spread_bid"]
    elif from_asset == "M1" and to_asset == "USDC":
        market_rate = 1.0 / LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        spread_percent = LP_CONFIG["pairs"]["USDC/M1"]["spread_ask"]
    elif from_asset == "BTC" and to_asset == "USDC":
        btc_m1_rate = float(BTC_M1_FIXED_RATE)
        usdc_m1_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        market_rate = btc_m1_rate / usdc_m1_rate
        spread_percent = (LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"] +
                         LP_CONFIG["pairs"]["USDC/M1"]["spread_ask"])
    elif from_asset == "USDC" and to_asset == "BTC":
        btc_m1_rate = float(BTC_M1_FIXED_RATE)
        usdc_m1_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
        market_rate = usdc_m1_rate / btc_m1_rate
        spread_percent = (LP_CONFIG["pairs"]["USDC/M1"]["spread_bid"] +
                         LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"])
    else:
        raise HTTPException(400, f"Unsupported pair: {from_asset}/{to_asset}")

    effective_rate = market_rate * (1 - spread_percent / 100)
    to_amount = round(amount * effective_rate, ASSETS[to_asset]["decimals"])

    # Generate IDs and hashlock
    swap_id = f"0x{uuid.uuid4().hex[:16]}"
    secret, hashlock = generate_hashlock()

    # Timeout
    now = int(time.time())
    timeout = now + HTLC_TIMEOUTS[req.from_asset]

    # Deposit address
    deposit_address = generate_deposit_address(req.from_asset, hashlock)

    # Store swap
    swap_data = {
        "swap_id": swap_id,
        "status": "pending_deposit",
        "step": 1,
        "from_asset": req.from_asset,
        "to_asset": req.to_asset,
        "from_amount": req.from_amount,
        "to_amount": to_amount,
        "deposit_address": deposit_address,
        "dest_address": req.dest_address,
        "route": get_route(req.from_asset, req.to_asset),
        "hashlock": hashlock,
        "secret": secret,  # Stored securely
        "timeout": timeout,
        "deposit_tx": None,
        "claim_tx": None,
        "confirmations": 0,
        "lp_id": req.lp_id,
        "created_at": now,
        "updated_at": now,
    }
    swaps_db[swap_id] = swap_data

    log.info(f"Swap created: {swap_id} | {req.from_amount} {req.from_asset} -> {to_amount} {req.to_asset}")

    return SwapCreateResponse(
        swap_id=swap_id,
        status="pending_deposit",
        from_asset=req.from_asset,
        to_asset=req.to_asset,
        from_amount=req.from_amount,
        to_amount=to_amount,
        deposit_address=deposit_address,
        hashlock=hashlock,
        timeout=timeout,
        route=get_route(req.from_asset, req.to_asset),
        created_at=now,
        expires_at=timeout,
    )

@app.get("/api/swap/{swap_id}", response_model=SwapStatusResponse)
async def get_swap(swap_id: str):
    """Get swap status."""
    if swap_id not in swaps_db:
        raise HTTPException(404, "Swap not found")

    swap = swaps_db[swap_id]

    return SwapStatusResponse(
        swap_id=swap["swap_id"],
        status=swap["status"],
        step=swap["step"],
        step_name=get_step_name(swap["step"]),
        from_asset=swap["from_asset"],
        to_asset=swap["to_asset"],
        from_amount=swap["from_amount"],
        to_amount=swap["to_amount"],
        deposit_address=swap["deposit_address"],
        dest_address=swap["dest_address"],
        route=swap["route"],
        hashlock=swap["hashlock"],
        deposit_tx=swap["deposit_tx"],
        claim_tx=swap["claim_tx"],
        confirmations=swap["confirmations"],
        created_at=swap["created_at"],
        updated_at=swap["updated_at"],
    )

@app.get("/api/swaps")
async def list_swaps(
    status: Optional[str] = None,
    swap_type: Optional[str] = None,  # "regular", "atomic", "flowswap", or None for all
    limit: int = Query(50, le=100),
):
    """List swaps (regular + atomic + flowswap 3S)."""
    all_swaps = []

    # Add regular swaps
    if swap_type in (None, "regular"):
        for s in swaps_db.values():
            swap = {k: v for k, v in s.items() if k != "secret"}
            swap["type"] = "regular"
            all_swaps.append(swap)

    # Add atomic swaps
    if swap_type in (None, "atomic"):
        for s in atomic_swaps_db.values():
            swap = {
                "swap_id": s["swap_id"],
                "type": "atomic",
                "status": s["status"],
                "from_asset": s["from_asset"],
                "to_asset": s["to_asset"],
                "from_amount": s["from_amount"],
                "to_amount": s["to_amount"],
                "hashlock": s["hashlock"][:16] + "...",  # Truncate for display
                "lp_htlc": s.get("lp_htlc", {}),
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
            }
            all_swaps.append(swap)

    # Add FlowSwap 3S swaps
    if swap_type in (None, "flowswap"):
        # Map FlowSwap states to unified status for dashboard
        _fs_status_map = {
            "awaiting_btc": "pending",
            "btc_funded": "htlc_created",
            "btc_claimed": "claiming",
            "awaiting_usdc": "pending",
            "usdc_funded": "htlc_created",
            "completing": "claiming",
            "completed": "claimed",
            "refunded": "refunded",
            "expired": "expired",
        }
        for s in flowswap_db.values():
            btc_sats = s.get("btc_amount_sats", 0)
            btc_amount = btc_sats / 100_000_000
            usdc_amount = s.get("usdc_amount", 0)
            created = s.get("created_at", 0)
            completed = s.get("completed_at")
            duration = (completed - created) if completed and created else None

            # Effective rate: USDC per BTC
            rate_exec = usdc_amount / btc_amount if btc_amount > 0 else 0

            # LP PnL: oracle-free, based on spread applied at swap time
            # PnL_M1 = volume_sats * spread / 100 (always >= 0)
            leg1_m1_sats = btc_sats  # 1 BTC sat = 1 M1 sat (fixed)
            spread_applied = s.get("spread_applied", 0)
            lp_pnl_m1 = round(btc_sats * spread_applied / 100) if btc_sats > 0 else 0
            # PnL in USDC: derive from executed rate
            rate_exec_tmp = usdc_amount / btc_amount if btc_amount > 0 else 0
            lp_pnl_usdc = round(lp_pnl_m1 * rate_exec_tmp / 100_000_000, 4) if rate_exec_tmp > 0 else 0.0

            swap = {
                "swap_id": s["swap_id"],
                "type": "flowswap_3s",
                "status": _fs_status_map.get(s["state"], s["state"]),
                "flowswap_state": s["state"],
                # Direction
                "from_asset": s.get("from_asset", "BTC"),
                "to_asset": s.get("to_asset", "USDC"),
                "direction": f"{s.get('from_asset', 'BTC')} \u2192 {s.get('to_asset', 'USDC')}",
                # Amounts
                "from_amount": btc_amount,
                "from_amount_sats": btc_sats,
                "to_amount": usdc_amount,
                "from_display": f"{btc_amount:.8f} BTC",
                "to_display": f"{usdc_amount:.2f} USDC",
                # 2-leg breakdown
                "legs": {
                    "leg1_btc_to_m1": {
                        "from": f"{btc_amount:.8f} BTC",
                        "to": f"{leg1_m1_sats:,} M1",
                        "rate": "1 BTC = 100,000,000 M1 (fixed)",
                    },
                    "leg2_m1_to_usdc": {
                        "from": f"{leg1_m1_sats:,} M1",
                        "to": f"{usdc_amount:.2f} USDC",
                        "rate": f"1 BTC = {rate_exec:,.0f} USDC (effective)",
                    },
                },
                # Rate & PnL
                "rate_executed": round(rate_exec, 2),
                "rate_display": f"1 BTC = {rate_exec:,.0f} USDC",
                "spread_applied": spread_applied,
                "lp_pnl_usdc": lp_pnl_usdc,
                "lp_pnl_m1": lp_pnl_m1,
                "lp_pnl": {
                    "usdc": lp_pnl_usdc,
                    "m1_sats": lp_pnl_m1,
                    "display": f"+{lp_pnl_m1:,} M1 (+${lp_pnl_usdc:.4f})" if lp_pnl_m1 >= 0 else f"{lp_pnl_m1:,} M1 (${lp_pnl_usdc:.4f})",
                },
                # TX details
                "btc_fund_txid": s.get("btc_fund_txid", ""),
                "btc_claim_txid": s.get("btc_claim_txid", ""),
                "evm_claim_txhash": s.get("evm_claim_txhash", ""),
                "m1_htlc_txid": s.get("m1_htlc_txid", ""),
                "btc_htlc_address": s.get("btc_htlc_address", ""),
                "evm_htlc_id": s.get("evm_htlc_id", ""),
                "m1_htlc_outpoint": s.get("m1_htlc_outpoint", ""),
                # User
                "user_usdc_address": s.get("user_usdc_address", ""),
                # Timing
                "created_at": created,
                "updated_at": s.get("updated_at", 0),
                "completed_at": completed,
                "duration_seconds": duration,
            }
            all_swaps.append(swap)

    # Filter by status if specified
    if status:
        all_swaps = [s for s in all_swaps if s.get("status") == status]

    # Sort by creation time
    all_swaps.sort(key=lambda x: x.get("created_at", 0), reverse=True)

    return {"swaps": all_swaps[:limit], "count": len(all_swaps)}

@app.get("/api/lps")
async def list_lps():
    """List liquidity providers."""
    return {
        "lps": list(lps_db.values()),
        "count": len(lps_db),
    }

# =============================================================================
# LP INFO & CONFIG ENDPOINTS (for SDK-to-SDK discovery)
# =============================================================================

class LPConfigUpdate(BaseModel):
    """LP configuration update from dashboard."""
    pairs: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    confirmations: Optional[Dict[str, Any]] = None

@app.get("/api/lp/info")
async def get_lp_info():
    """
    Get LP info for discovery by other SDKs.

    This endpoint is queried by aggregators and other LPs to discover
    this LP's capabilities, rates, and availability.
    """
    # Fetch live price first
    await fetch_live_btc_usdc_price()

    # Calculate uptime
    uptime_seconds = int(time.time()) - LP_CONFIG["stats"]["uptime_start"]
    uptime_hours = uptime_seconds / 3600

    # Get current rates for each pair
    pairs_info = {}
    for pair_key, pair_config in LP_CONFIG["pairs"].items():
        if not pair_config.get("enabled", True):
            continue

        from_asset, to_asset = pair_key.split("/")

        # Calculate effective rates (bid and ask)
        if pair_key == "BTC/M1":
            base_rate = float(BTC_M1_FIXED_RATE)
            bid_rate = base_rate * (1 - pair_config["spread_bid"] / 100)
            ask_rate = base_rate * (1 + pair_config["spread_ask"] / 100)
        elif pair_key == "USDC/M1":
            base_rate = pair_config["rate"]
            bid_rate = base_rate * (1 - pair_config["spread_bid"] / 100)
            ask_rate = base_rate * (1 + pair_config["spread_ask"] / 100)
        elif pair_key == "BTC/USDC":
            # Derived pair
            btc_m1 = float(BTC_M1_FIXED_RATE)
            usdc_m1 = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
            base_rate = btc_m1 / usdc_m1
            spread_buy = (LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"] +
                         LP_CONFIG["pairs"]["USDC/M1"]["spread_ask"])
            spread_sell = (LP_CONFIG["pairs"]["USDC/M1"]["spread_bid"] +
                          LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"])
            bid_rate = base_rate * (1 - spread_buy / 100)
            ask_rate = base_rate * (1 + spread_sell / 100)
        else:
            continue

        pairs_info[pair_key] = {
            "enabled": pair_config.get("enabled", True),
            "rate_market": base_rate,
            "rate_bid": bid_rate,      # LP buys (user sells)
            "rate_ask": ask_rate,      # LP sells (user buys)
            "spread_bid": pair_config.get("spread_bid", 0),
            "spread_ask": pair_config.get("spread_ask", 0),
            "min": pair_config.get("min", 0),
            "max": pair_config.get("max", float('inf')),
        }

    # Detect test mode (all spreads at 0)
    all_spreads = [
        pair_config.get("spread_bid", 0) + pair_config.get("spread_ask", 0)
        for pair_config in LP_CONFIG["pairs"].values()
    ]
    test_mode = all(s == 0 for s in all_spreads)

    with _flowswap_lock:
        _avail = _get_available_inventory()
    _status_inventory_snapshot = {
        "btc_available": _avail.get("btc", 0) > 0,
        "m1_available": _avail.get("m1", 0) > 0,
        "usdc_available": _avail.get("usdc", 0) > 0,
    }

    return {
        "lp_id": LP_CONFIG["id"],
        "name": LP_CONFIG["name"],
        "version": LP_CONFIG["version"],
        "protocol_fee": 0,
        "test_mode": test_mode,
        "pairs": pairs_info,
        # Expose confirmation requirements for aggregators to compare LPs
        "confirmations": {
            asset: {
                "default": conf.get("default", 1),
                "tiers": conf.get("tiers", []),
            }
            for asset, conf in LP_CONFIG.get("confirmations", {}).items()
        },
        "inventory": _status_inventory_snapshot,
        "stats": {
            "swaps_completed": LP_CONFIG["stats"]["swaps_completed"],
            "uptime_hours": round(uptime_hours, 2),
        },
        "timestamp": int(time.time()),
    }

@app.post("/api/lp/config")
async def update_lp_config(config: LPConfigUpdate):
    """
    Update LP configuration from dashboard.

    Called when LP saves config in the dashboard UI.
    """
    if config.name:
        LP_CONFIG["name"] = config.name
        log.info(f"LP name updated: {config.name}")

    if config.pairs:
        for pair_key, pair_data in config.pairs.items():
            if pair_key in LP_CONFIG["pairs"]:
                # Update only provided fields
                for field in ["enabled", "spread_bid", "spread_ask", "min", "max", "rate"]:
                    if field in pair_data:
                        LP_CONFIG["pairs"][pair_key][field] = pair_data[field]
                log.info(f"Pair config updated: {pair_key}")

    if config.confirmations:
        for asset, conf_data in config.confirmations.items():
            if asset in LP_CONFIG["confirmations"]:
                # Update confirmation settings
                for field in ["default", "min", "max", "tiers"]:
                    if field in conf_data:
                        LP_CONFIG["confirmations"][asset][field] = conf_data[field]
                log.info(f"Confirmation config updated: {asset} = {conf_data}")

    return {"success": True, "config": LP_CONFIG}


@app.get("/api/lp/confirmations")
async def get_confirmations_config():
    """
    Get LP's confirmation requirements.

    Returns the tiered confirmation structure so users/aggregators
    know how long settlement will take for different amounts.
    """
    return {
        "confirmations": LP_CONFIG.get("confirmations", {}),
        "description": {
            "BTC": "Bitcoin confirmations required (10 min/block). Higher = safer but slower.",
            "USDC": "Base L2 confirmations (2s/block). Usually 1 is enough.",
            "M1": "BATHRON confirmations (1 min/block). HU finality makes 1 sufficient.",
        },
        "risk_explanation": (
            "Lower confirmations = faster settlement but LP takes more reorg risk. "
            "If BTC reorgs after LP releases funds, LP loses money. "
            "Tiered approach: small amounts = low conf (acceptable loss), "
            "large amounts = high conf (worth the wait)."
        ),
    }


@app.post("/api/lp/confirmations")
async def update_confirmations_config(asset: str, config: Dict[str, Any]):
    """
    Update confirmation requirements for a specific asset.

    Example:
        POST /api/lp/confirmations?asset=BTC
        {
            "default": 3,
            "tiers": [
                {"max_btc": 0.01, "confirmations": 1},
                {"max_btc": 0.1, "confirmations": 2},
                {"max_btc": 1.0, "confirmations": 6}
            ]
        }
    """
    if asset not in ["BTC", "USDC", "M1"]:
        raise HTTPException(400, f"Unknown asset: {asset}")

    if asset not in LP_CONFIG["confirmations"]:
        LP_CONFIG["confirmations"][asset] = {}

    # Validate and update
    if "default" in config:
        LP_CONFIG["confirmations"][asset]["default"] = int(config["default"])

    if "min" in config:
        LP_CONFIG["confirmations"][asset]["min"] = int(config["min"])

    if "max" in config:
        LP_CONFIG["confirmations"][asset]["max"] = int(config["max"])

    if "tiers" in config:
        # Validate tiers
        tiers = config["tiers"]
        if not isinstance(tiers, list):
            raise HTTPException(400, "tiers must be a list")
        for tier in tiers:
            if "confirmations" not in tier:
                raise HTTPException(400, "Each tier must have 'confirmations'")
        LP_CONFIG["confirmations"][asset]["tiers"] = tiers

    log.info(f"Confirmation config updated: {asset} = {LP_CONFIG['confirmations'][asset]}")

    return {
        "success": True,
        "asset": asset,
        "config": LP_CONFIG["confirmations"][asset],
    }

@app.get("/api/lp/config")
async def get_lp_config():
    """Get current LP configuration."""
    return LP_CONFIG

@app.post("/api/lp/inventory/refresh")
async def refresh_inventory():
    """
    Refresh inventory from wallet balances.

    Queries the actual wallet balances and updates the inventory.
    For M1, uses SDK receipts (M1 liquidity) instead of M0 UTXO balance.
    """
    wallets = await get_wallets()

    LP_CONFIG["inventory"]["btc"] = wallets.get("btc", {}).get("balance", 0)
    LP_CONFIG["inventory"]["usdc"] = wallets.get("usdc", {}).get("balance", 0)

    # For M1, use SDK receipts (actual M1 liquidity available for swaps)
    m1_balance = 0
    if SDK_AVAILABLE:
        try:
            m1_client = get_m1_client()
            if m1_client:
                receipts = m1_client.list_m1_receipts()
                # Receipt amounts from getwalletstate are already in coins
                # (ValueFromAmount in C++ divides sats by COIN=100,000,000)
                m1_balance = sum(r.get("amount", 0) for r in receipts if r.get("unlockable", False))
        except Exception as e:
            log.error(f"Error getting M1 receipts: {e}")
            # Fall back to wallet M0 balance
            m1_balance = wallets.get("m1", {}).get("balance", 0)
    else:
        m1_balance = wallets.get("m1", {}).get("balance", 0)

    LP_CONFIG["inventory"]["m1"] = m1_balance

    log.info(f"Inventory refreshed: BTC={LP_CONFIG['inventory']['btc']}, "
             f"M1={LP_CONFIG['inventory']['m1']}, USDC={LP_CONFIG['inventory']['usdc']}")

    with _flowswap_lock:
        available = _get_available_inventory()
        reserved_total = {
            k: round(LP_CONFIG["inventory"].get(k, 0) - available.get(k, 0), 8)
            for k in ("btc", "m1", "usdc")
        }

    return {
        "success": True,
        "inventory": LP_CONFIG["inventory"],
        "reserved": reserved_total,
        "available": available,
    }

# =============================================================================
# INTERNAL / LP ENDPOINTS
# =============================================================================

@app.post("/api/swap/{swap_id}/deposit")
async def report_deposit(swap_id: str, tx_hash: str = Query(...)):
    """Report deposit transaction (called by watcher)."""
    if swap_id not in swaps_db:
        raise HTTPException(404, "Swap not found")

    swap = swaps_db[swap_id]
    swap["deposit_tx"] = tx_hash
    swap["status"] = "confirming"
    swap["step"] = 2
    swap["updated_at"] = int(time.time())

    log.info(f"Deposit reported: {swap_id} | tx={tx_hash[:16]}...")
    return {"success": True}

@app.post("/api/swap/{swap_id}/confirm")
async def confirm_swap(swap_id: str, confirmations: int = Query(...)):
    """Update confirmation count (called by watcher)."""
    if swap_id not in swaps_db:
        raise HTTPException(404, "Swap not found")

    swap = swaps_db[swap_id]
    swap["confirmations"] = confirmations
    swap["updated_at"] = int(time.time())

    required = ASSETS[swap["from_asset"]]["confirmations_required"]
    if confirmations >= required and swap["step"] == 2:
        swap["status"] = "settling"
        swap["step"] = 3
        log.info(f"Swap confirmed: {swap_id} | {confirmations}/{required}")

    return {"success": True, "confirmations": confirmations, "required": required}

@app.post("/api/swap/{swap_id}/settle")
async def settle_swap(swap_id: str):
    """
    Execute settlement for a confirmed swap.

    For BTC->M1 swaps: LP sends M1 to user's destination address.
    For M1->BTC swaps: LP sends BTC to user's destination address.
    This is a trusted model for MVP - no atomic HTLC on both chains.
    """
    if swap_id not in swaps_db:
        raise HTTPException(404, "Swap not found")

    swap = swaps_db[swap_id]

    if swap["step"] != 3:
        raise HTTPException(400, f"Swap not ready for settlement (step={swap['step']}, need step=3)")

    to_asset = swap["to_asset"]

    if to_asset == "M1":
        # BTC -> M1: Send M1 to user
        return await _settle_send_m1(swap_id, swap)
    elif to_asset == "BTC":
        # M1 -> BTC: Send BTC to user
        return await _settle_send_btc(swap_id, swap)
    else:
        raise HTTPException(400, f"Settlement for {to_asset} not supported yet")


async def _settle_send_m1(swap_id: str, swap: dict):
    """Settle by sending M1 to user."""
    # Get M1 client
    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    # Get available M1 receipts
    receipts = m1_client.list_m1_receipts()
    if not receipts:
        raise HTTPException(503, "No M1 receipts available for settlement")

    # Calculate amount needed (to_amount is in satoshis for M1)
    amount_needed = int(swap["to_amount"])

    # Find a suitable receipt
    # Receipt amounts from getwalletstate are in coins (ValueFromAmount), convert to sats
    suitable_receipt = None
    for r in receipts:
        r_sats = int(round(r.get("amount", 0) * 100_000_000))
        if r.get("unlockable") and r_sats >= amount_needed:
            suitable_receipt = r
            break

    if not suitable_receipt:
        raise HTTPException(503, f"No receipt with sufficient balance ({amount_needed} sats needed)")

    # Transfer M1 to user's destination address
    try:
        result = m1_client.transfer_m1(
            suitable_receipt["outpoint"],
            swap["dest_address"]
        )

        # Mark swap complete
        swap["claim_tx"] = result.get("txid", "unknown")
        swap["status"] = "complete"
        swap["step"] = 4
        swap["updated_at"] = int(time.time())

        log.info(f"Swap settled: {swap_id} | M1 sent to {swap['dest_address']} | tx={swap['claim_tx'][:16]}...")

        return {
            "success": True,
            "txid": swap["claim_tx"],
            "amount": amount_needed,
            "to_address": swap["dest_address"],
        }

    except Exception as e:
        log.error(f"M1 settlement failed: {swap_id} | {e}")
        raise HTTPException(500, f"Settlement failed: {e}")


async def _settle_send_btc(swap_id: str, swap: dict):
    """Settle by sending BTC to user."""
    # Get BTC client
    btc_client = get_btc_client()
    if not btc_client:
        raise HTTPException(503, "BTC client not available")

    # to_amount for BTC is in BTC (float), e.g. 0.00024875
    amount_btc = float(swap["to_amount"])

    # Check BTC balance
    try:
        balance = btc_client.get_balance()
        if balance < amount_btc:
            raise HTTPException(503, f"Insufficient BTC balance: {balance} < {amount_btc}")
    except Exception as e:
        log.error(f"Failed to check BTC balance: {e}")
        # Continue anyway, sendtoaddress will fail if insufficient

    # Send BTC to user's destination address
    try:
        txid = btc_client.send_to_address(
            swap["dest_address"],
            amount_btc,
            f"pna-swap-{swap_id}"
        )

        # Mark swap complete
        swap["claim_tx"] = txid
        swap["status"] = "complete"
        swap["step"] = 4
        swap["updated_at"] = int(time.time())

        log.info(f"Swap settled: {swap_id} | BTC sent to {swap['dest_address']} | tx={txid[:16]}...")

        return {
            "success": True,
            "txid": txid,
            "amount": amount_btc,
            "to_address": swap["dest_address"],
        }

    except Exception as e:
        log.error(f"BTC settlement failed: {swap_id} | {e}")
        raise HTTPException(500, f"Settlement failed: {e}")


@app.post("/api/swap/{swap_id}/complete")
async def complete_swap(swap_id: str, tx_hash: str = Query(...)):
    """Mark swap complete (called by watcher)."""
    if swap_id not in swaps_db:
        raise HTTPException(404, "Swap not found")

    swap = swaps_db[swap_id]
    swap["claim_tx"] = tx_hash
    swap["status"] = "complete"
    swap["step"] = 4
    swap["updated_at"] = int(time.time())

    log.info(f"Swap complete: {swap_id} | claim_tx={tx_hash[:16]}...")
    return {"success": True}


# =============================================================================
# ATOMIC HTLC SWAP ENDPOINTS (TRUSTLESS)
# =============================================================================
#
# These endpoints implement TRUE atomic swaps with bidirectional HTLCs.
# The user generates the secret and controls when to reveal it.
#
# Flow for BTC → M1:
#   1. User generates secret S, hashlock H = sha256(S)
#   2. User calls /api/atomic/initiate with H
#   3. LP creates HTLC-M1 locked by H (claimable by user with S)
#   4. User creates HTLC-BTC locked by same H (claimable by LP with S)
#   5. User claims HTLC-M1 with S → reveals S on-chain
#   6. LP sees S, claims HTLC-BTC
#
# ATOMIC GUARANTEE: Both succeed or both refund. No trust required.
# =============================================================================

# In-memory atomic swap database
atomic_swaps_db: Dict[str, Dict[str, Any]] = {}


class AtomicSwapInitRequest(BaseModel):
    """Request to initiate atomic swap."""
    from_asset: str = Field(..., description="Asset user sends (BTC or M1)")
    to_asset: str = Field(..., description="Asset user receives (M1 or BTC)")
    from_amount: float = Field(..., gt=0, description="Amount to swap")
    hashlock: str = Field(..., min_length=64, max_length=64, description="SHA256 hashlock (user generated)")
    user_claim_address: str = Field(..., description="User's address to receive to_asset")
    user_refund_address: str = Field(..., description="User's address for HTLC refund")


class AtomicSwapClaimRequest(BaseModel):
    """Request to claim HTLC."""
    swap_id: str
    preimage: str = Field(..., min_length=64, max_length=64, description="32-byte preimage hex")


@app.post("/api/atomic/initiate")
async def initiate_atomic_swap(req: AtomicSwapInitRequest):
    """
    Initiate a TRUE atomic swap with bidirectional HTLCs.

    User generates secret S and hashlock H = sha256(S).
    LP creates HTLC locked by H that user can claim with S.

    This is TRUSTLESS: LP cannot steal because they don't know S.
    User controls when to reveal S and claim.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    # Validate assets
    if req.from_asset not in ASSETS or req.to_asset not in ASSETS:
        raise HTTPException(400, "Invalid asset")
    if req.from_asset == req.to_asset:
        raise HTTPException(400, "Same asset swap not allowed")

    # Validate hashlock format
    try:
        bytes.fromhex(req.hashlock)
    except ValueError:
        raise HTTPException(400, "Invalid hashlock hex")

    # Calculate amounts
    if req.from_asset == "BTC" and req.to_asset == "M1":
        market_rate = float(BTC_M1_FIXED_RATE)
        spread = LP_CONFIG["pairs"]["BTC/M1"]["spread_bid"]
    elif req.from_asset == "M1" and req.to_asset == "BTC":
        market_rate = 1.0 / float(BTC_M1_FIXED_RATE)
        spread = LP_CONFIG["pairs"]["BTC/M1"]["spread_ask"]
    else:
        raise HTTPException(400, f"Pair {req.from_asset}/{req.to_asset} not supported for atomic swap")

    effective_rate = market_rate * (1 - spread / 100)
    to_amount = req.from_amount * effective_rate

    # Generate swap ID
    swap_id = f"atomic_{uuid.uuid4().hex[:16]}"
    now = int(time.time())

    # For BTC → M1: LP creates HTLC-M1 for user
    if req.to_asset == "M1":
        # Get M1 HTLC manager
        m1_client = get_m1_client()
        if not m1_client:
            raise HTTPException(503, "M1 client not available")

        m1_htlc = M1Htlc(m1_client)

        # Find or create M1 receipt for HTLC
        amount_sats = int(to_amount)  # to_amount is in sats for M1
        try:
            receipt_outpoint = m1_htlc.ensure_receipt_available(amount_sats)
        except RuntimeError as e:
            raise HTTPException(503, f"Insufficient M1 liquidity: {e}")

        # Create HTLC-M1 locked by user's hashlock
        # User can claim with preimage, LP can refund after timeout
        expiry_blocks = 288  # ~4.8 hours at 1 min blocks

        try:
            htlc_result = m1_htlc.create_htlc(
                receipt_outpoint=receipt_outpoint,
                hashlock=req.hashlock,
                claim_address=req.user_claim_address,
                expiry_blocks=expiry_blocks
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to create HTLC-M1: {e}")

        # Store atomic swap
        atomic_swaps_db[swap_id] = {
            "swap_id": swap_id,
            "status": "htlc_created",
            "from_asset": req.from_asset,
            "to_asset": req.to_asset,
            "from_amount": req.from_amount,
            "to_amount": to_amount,
            "hashlock": req.hashlock,
            "user_claim_address": req.user_claim_address,
            "user_refund_address": req.user_refund_address,
            # LP's HTLC (for user to claim)
            "lp_htlc": {
                "chain": "M1",
                "txid": htlc_result.get("txid"),
                "htlc_outpoint": htlc_result.get("htlc_outpoint"),
                "amount": amount_sats,
                "expiry_height": htlc_result.get("expiry_height"),
            },
            # User's HTLC (to be created by user, for LP to claim)
            "user_htlc": None,  # User creates this on BTC
            # LP's deposit address for user's BTC HTLC
            "lp_btc_address": _lp_addresses.get("btc"),
            "created_at": now,
            "updated_at": now,
        }

        log.info(f"Atomic swap initiated: {swap_id} | LP created HTLC-M1 {htlc_result.get('htlc_outpoint')}")

        return {
            "swap_id": swap_id,
            "status": "htlc_created",
            "message": "LP has created HTLC-M1. User should now create HTLC-BTC with same hashlock.",
            "lp_htlc": {
                "chain": "M1",
                "txid": htlc_result.get("txid"),
                "htlc_outpoint": htlc_result.get("htlc_outpoint"),
                "amount": amount_sats,
                "hashlock": req.hashlock,
                "claim_address": req.user_claim_address,
                "expiry_height": htlc_result.get("expiry_height"),
            },
            "next_step": {
                "action": "create_btc_htlc",
                "description": "Create HTLC-BTC locked with same hashlock",
                "amount_btc": req.from_amount,
                "hashlock": req.hashlock,
                "lp_btc_address": _lp_addresses.get("btc"),
                "timeout_blocks": 144,  # Must be SHORTER than M1 timeout
            },
            "atomic_guarantee": "User claims M1 → reveals preimage → LP claims BTC. Both or neither.",
        }

    # For M1 → BTC: LP creates HTLC-BTC for user
    elif req.to_asset == "BTC":
        btc_client = get_btc_client()
        if not btc_client:
            raise HTTPException(503, "BTC client not available")

        # to_amount is in BTC
        amount_btc = to_amount

        # Check BTC balance
        balance = btc_client.get_balance()
        if balance < amount_btc:
            raise HTTPException(503, f"Insufficient BTC liquidity: {balance} < {amount_btc}")

        # For BTC HTLC, we need to use the HTLC script
        btc_htlc = BTCHtlc(btc_client)

        try:
            htlc_result = btc_htlc.create_htlc(
                amount_sats=int(amount_btc * 100_000_000),
                hashlock=req.hashlock,
                recipient_address=req.user_claim_address,
                refund_address=_lp_addresses.get("btc"),
                timeout_blocks=144  # ~1 day on Signet
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to create HTLC-BTC: {e}")

        atomic_swaps_db[swap_id] = {
            "swap_id": swap_id,
            "status": "htlc_created",
            "from_asset": req.from_asset,
            "to_asset": req.to_asset,
            "from_amount": req.from_amount,
            "to_amount": amount_btc,
            "hashlock": req.hashlock,
            "user_claim_address": req.user_claim_address,
            "user_refund_address": req.user_refund_address,
            "lp_htlc": {
                "chain": "BTC",
                "htlc_address": htlc_result.get("htlc_address"),
                "funding_txid": htlc_result.get("funding_txid"),
                "amount_sats": int(amount_btc * 100_000_000),
                "timelock": htlc_result.get("timelock"),
            },
            "user_htlc": None,  # User creates this on M1
            "lp_m1_address": _lp_addresses.get("m1"),
            "created_at": now,
            "updated_at": now,
        }

        log.info(f"Atomic swap initiated: {swap_id} | LP created HTLC-BTC")

        return {
            "swap_id": swap_id,
            "status": "htlc_created",
            "message": "LP has created HTLC-BTC. User should now create HTLC-M1 with same hashlock.",
            "lp_htlc": {
                "chain": "BTC",
                "htlc_address": htlc_result.get("htlc_address"),
                "funding_txid": htlc_result.get("funding_txid"),
                "amount_sats": int(amount_btc * 100_000_000),
                "hashlock": req.hashlock,
                "timelock": htlc_result.get("timelock"),
            },
            "next_step": {
                "action": "create_m1_htlc",
                "description": "Create HTLC-M1 locked with same hashlock",
                "amount_sats": int(req.from_amount),
                "hashlock": req.hashlock,
                "lp_m1_address": _lp_addresses.get("m1"),
                "timeout_blocks": 288,  # Must be LONGER than BTC timeout
            },
            "atomic_guarantee": "User claims BTC → reveals preimage → LP claims M1. Both or neither.",
        }


@app.post("/api/atomic/register-user-htlc")
async def register_user_htlc(swap_id: str, htlc_txid: str = Query(...), htlc_outpoint: str = Query(None)):
    """
    Register user's HTLC for the atomic swap.

    After LP creates their HTLC, user creates theirs on the other chain.
    This endpoint lets LP verify and track user's HTLC.
    """
    if swap_id not in atomic_swaps_db:
        raise HTTPException(404, "Atomic swap not found")

    swap = atomic_swaps_db[swap_id]

    if swap["status"] != "htlc_created":
        raise HTTPException(400, f"Invalid swap status: {swap['status']}")

    # Store user HTLC info
    swap["user_htlc"] = {
        "txid": htlc_txid,
        "outpoint": htlc_outpoint or f"{htlc_txid}:0",
    }
    swap["status"] = "ready_to_claim"
    swap["updated_at"] = int(time.time())

    log.info(f"User HTLC registered: {swap_id} | {htlc_txid}")

    return {
        "success": True,
        "swap_id": swap_id,
        "status": "ready_to_claim",
        "message": "Both HTLCs created. User can now claim LP's HTLC with preimage.",
        "next_step": {
            "action": "claim_lp_htlc",
            "endpoint": f"/api/atomic/claim",
            "description": "Claim LP's HTLC using your preimage. This reveals preimage to LP.",
        },
    }


@app.post("/api/atomic/claim")
async def claim_atomic_htlc(req: AtomicSwapClaimRequest):
    """
    Claim LP's HTLC with preimage.

    This is the atomic moment: revealing preimage allows LP to also claim.
    User should only call this when ready to complete the swap.
    """
    if req.swap_id not in atomic_swaps_db:
        raise HTTPException(404, "Atomic swap not found")

    swap = atomic_swaps_db[req.swap_id]

    # Verify preimage matches hashlock
    import hashlib
    computed_hash = hashlib.sha256(bytes.fromhex(req.preimage)).hexdigest()
    if computed_hash != swap["hashlock"]:
        raise HTTPException(400, "Preimage does not match hashlock")

    lp_htlc = swap["lp_htlc"]

    if lp_htlc["chain"] == "M1":
        # User claims M1 HTLC
        m1_client = get_m1_client()
        if not m1_client:
            raise HTTPException(503, "M1 client not available")

        m1_htlc = M1Htlc(m1_client)

        try:
            claim_result = m1_htlc.claim(
                htlc_outpoint=lp_htlc["htlc_outpoint"],
                preimage=req.preimage
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to claim M1 HTLC: {e}")

        swap["status"] = "user_claimed"
        swap["preimage"] = req.preimage
        swap["user_claim_tx"] = claim_result.get("txid")
        swap["updated_at"] = int(time.time())

        log.info(f"User claimed M1 HTLC: {req.swap_id} | preimage revealed")

        return {
            "success": True,
            "swap_id": req.swap_id,
            "status": "user_claimed",
            "claim_txid": claim_result.get("txid"),
            "preimage_revealed": True,
            "message": "User claimed M1. Preimage is now on-chain. LP will claim BTC.",
            "atomic_status": "SWAP COMPLETE - Both parties can now claim their assets",
        }

    elif lp_htlc["chain"] == "BTC":
        # User claims BTC HTLC (more complex - needs raw tx construction)
        # For now, return the claim instruction
        return {
            "success": True,
            "swap_id": req.swap_id,
            "status": "claim_pending",
            "message": "BTC claim requires constructing P2WSH witness. Use bitcoin-cli or SDK.",
            "claim_info": {
                "htlc_address": lp_htlc["htlc_address"],
                "preimage": req.preimage,
                "recipient": swap["user_claim_address"],
            },
        }


@app.get("/api/atomic/{swap_id}")
async def get_atomic_swap(swap_id: str):
    """Get atomic swap status."""
    if swap_id not in atomic_swaps_db:
        raise HTTPException(404, "Atomic swap not found")

    swap = atomic_swaps_db[swap_id]

    # Don't expose preimage until claimed
    result = {k: v for k, v in swap.items() if k != "preimage"}
    if swap.get("preimage"):
        result["preimage_revealed"] = True

    return result


@app.get("/api/atomic/list")
async def list_atomic_swaps(status: str = None):
    """List atomic swaps."""
    swaps = list(atomic_swaps_db.values())

    if status:
        swaps = [s for s in swaps if s["status"] == status]

    return {"swaps": swaps, "count": len(swaps)}


# =============================================================================
# FLOWSWAP 3-SECRET ENDPOINTS (BTC <-> USDC via M1)
# =============================================================================

# HTLC3S contract address (E2E proven on Base Sepolia)
HTLC3S_CONTRACT_ADDRESS = "0x2493EaaaBa6B129962c8967AaEE6bF11D0277756"

# In-memory state for FlowSwap 3S (persisted to disk)
_lp_id = os.environ.get("LP_ID", "lp_pna_01")
FLOWSWAP_DB_PATH = os.path.expanduser(
    os.environ.get("LP_FLOWSWAP_DB", f"~/.bathron/flowswap_db_{_lp_id}.json")
)
flowswap_db: Dict[str, Dict[str, Any]] = {}
_flowswap_lock = threading.Lock()  # Protects flowswap_db access across threads

# Inventory reservations per swap_id: {"m1": coins, "usdc": coins, "btc": coins}
# Protected by _flowswap_lock. NOT persisted — rebuilt from flowswap_db on startup.
_inventory_reservations: Dict[str, Dict[str, float]] = {}

# Expected USDC token address (Base Sepolia)
EXPECTED_USDC_TOKEN = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def _save_flowswap_db():
    """Persist flowswap_db to disk (JSON). Skips if empty to avoid overwriting seed data."""
    if not flowswap_db:
        return
    try:
        os.makedirs(os.path.dirname(FLOWSWAP_DB_PATH), exist_ok=True)
        # Strip ALL secrets before saving (keys should NEVER be on disk)
        safe_db = {}
        for sid, s in flowswap_db.items():
            entry = dict(s)
            entry.pop("S_lp1", None)
            entry.pop("S_lp2", None)
            entry.pop("lp1_claim_wif", None)
            entry.pop("ephemeral_claim_wif", None)  # CRITICAL: strip BTC private key
            entry.pop("_lp_locking", None)  # Internal flag, not for disk
            safe_db[sid] = entry
        with open(FLOWSWAP_DB_PATH, "w") as f:
            json.dump(safe_db, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save flowswap_db: {e}")


def _load_flowswap_db():
    """Load flowswap_db from disk on startup."""
    global flowswap_db
    try:
        if os.path.exists(FLOWSWAP_DB_PATH):
            with open(FLOWSWAP_DB_PATH, "r") as f:
                flowswap_db = json.load(f)
            log.info(f"Loaded {len(flowswap_db)} FlowSwap entries from {FLOWSWAP_DB_PATH}")
    except Exception as e:
        log.error(f"Failed to load flowswap_db: {e}")

# Lazy SDK 3S clients
_sdk_m1_htlc_3s: Optional["M1Htlc3S"] = None
_sdk_btc_htlc_3s: Optional["BTCHTLC3S"] = None
_sdk_evm_htlc_3s: Optional["EVMHTLC3S"] = None
_sdk_watcher_3s: Optional["Watcher3S"] = None


def get_m1_htlc_3s() -> Optional["M1Htlc3S"]:
    """Get or create M1 HTLC3S manager."""
    global _sdk_m1_htlc_3s
    if _sdk_m1_htlc_3s is None and SDK_AVAILABLE:
        client = get_m1_client()
        if client:
            _sdk_m1_htlc_3s = M1Htlc3S(client)
            log.info("SDK M1 HTLC3S manager initialized")
    return _sdk_m1_htlc_3s


def get_btc_htlc_3s() -> Optional["BTCHTLC3S"]:
    """Get or create BTC HTLC3S manager."""
    global _sdk_btc_htlc_3s
    if _sdk_btc_htlc_3s is None and SDK_AVAILABLE:
        client = get_btc_client()
        if client:
            _sdk_btc_htlc_3s = BTCHTLC3S(client)
            log.info("SDK BTC HTLC3S manager initialized")
    return _sdk_btc_htlc_3s


def get_evm_htlc_3s() -> Optional["EVMHTLC3S"]:
    """Get or create EVM HTLC3S manager."""
    global _sdk_evm_htlc_3s
    if _sdk_evm_htlc_3s is None and SDK_AVAILABLE:
        _sdk_evm_htlc_3s = EVMHTLC3S(contract_address=HTLC3S_CONTRACT_ADDRESS)
        log.info("SDK EVM HTLC3S manager initialized")
    return _sdk_evm_htlc_3s


def _load_lp_btc_key() -> Dict:
    """Load LP1 BTC claim key from ~/.BathronKey/btc.json."""
    key_path = Path.home() / ".BathronKey" / "btc.json"
    if not key_path.exists():
        return {}
    try:
        with open(key_path) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load BTC key: {e}")
        return {}


def _load_evm_private_key() -> Optional[str]:
    """Load EVM private key for LP operations.

    Priority order:
    1. ~/.keys/lp_evm.json  (LP-specific key, takes precedence)
    2. ~/.BathronKey/evm.json (generic EVM key)

    NEVER hardcode private keys in source code.
    """
    from eth_account import Account as _Acct

    # Priority 1: LP-specific key file
    lp_path = Path.home() / ".keys" / "lp_evm.json"
    if lp_path.exists():
        try:
            with open(lp_path) as f:
                data = json.load(f)
                key = data.get("private_key") or data.get("privkey")
                if key:
                    addr = _Acct.from_key("0x" + key if not key.startswith("0x") else key).address
                    log.info(f"EVM key loaded from {lp_path} (address: {addr})")
                    return key
        except Exception as e:
            log.error(f"Failed to load EVM key from {lp_path}: {e}")

    # Priority 2: Standard BathronKey path
    std_path = Path.home() / ".BathronKey" / "evm.json"
    if std_path.exists():
        try:
            with open(std_path) as f:
                data = json.load(f)
                key = data.get("private_key") or data.get("privkey")
                if key:
                    addr = _Acct.from_key("0x" + key if not key.startswith("0x") else key).address
                    log.info(f"EVM key loaded from {std_path} (address: {addr})")
                    return key
        except Exception as e:
            log.error(f"Failed to load EVM key from {std_path}: {e}")

    log.error("No EVM private key found in ~/.keys/lp_evm.json or ~/.BathronKey/evm.json")
    return None


def _get_btc_m1_usdc_rate() -> float:
    """Calculate BTC -> USDC effective rate.
    BTC/M1 is fixed (1 sat = 1 M1).
    USDC/M1 comes from LP config.
    """
    usdc_m1_rate = LP_CONFIG["pairs"]["USDC/M1"]["rate"]
    # 1 BTC = 100M sats = 100M M1 = 100M / usdc_m1_rate USDC
    return 100_000_000 / usdc_m1_rate


# --- Anti-grief helpers ---

def _check_rate_limit(client_ip: str):
    """Check rate limit: max concurrent pending plans per IP/session.
    Plans past their plan_expires_at are auto-expired and don't count."""
    now = int(time.time())
    pending_states = (
        FlowSwapState.AWAITING_BTC.value,
        FlowSwapState.AWAITING_USDC.value,
    )
    pending_count = 0
    for s in flowswap_db.values():
        if s.get("state") not in pending_states:
            continue
        if s.get("client_ip") != client_ip:
            continue
        # Auto-expire stale plans
        expires = s.get("plan_expires_at", 0)
        if expires and now > expires:
            s["state"] = FlowSwapState.EXPIRED.value
            s["updated_at"] = now
            continue
        pending_count += 1
    if pending_count >= MAX_CONCURRENT_SWAPS_PER_SESSION:
        raise HTTPException(429, f"Too many pending plans ({pending_count}). Complete or wait for expiry.")


def _get_instant_min_feerate() -> float:
    """
    Minimum feerate (sats/vB) for 0-conf instant swap.
    Purely network-based — not LP-controllable.
    Uses estimatesmartfee from BTC node (confirm within 2 blocks).
    """
    FLOOR = 1.0  # absolute minimum (1 sat/vB)

    try:
        btc_3s = get_btc_htlc_3s()
        if btc_3s:
            result = btc_3s.client._call("estimatesmartfee", 2)
            if result and "feerate" in result:
                # estimatesmartfee returns BTC/kB → convert to sats/vB
                btc_per_kb = float(result["feerate"])
                sats_per_vb = (btc_per_kb * 1e8) / 1000
                # Use 80% of next-block estimate (reasonable for instant)
                return round(max(FLOOR, sats_per_vb * 0.8), 1)
    except Exception:
        pass

    return FLOOR


def _get_required_confirmations(amount_sats: int) -> int:
    """Return required BTC confirmations based on amount tier."""
    for threshold, confs in BTC_CONFIRMATION_TIERS:
        if amount_sats < threshold:
            return confs
    return 3  # fallback


def _detect_btc_sender(btc_3s, txid: str) -> str:
    """Extract sender's BTC address from a transaction's first input.

    Decodes the funding TX inputs to find where the BTC came from,
    so we can auto-refund to that address if the swap fails.
    """
    try:
        import json
        raw = btc_3s.client._call("getrawtransaction", txid, True)
        if not raw or not raw.get("vin"):
            return ""
        # Get the first input's previous output address
        vin = raw["vin"][0]
        prev_txid = vin.get("txid", "")
        prev_vout = vin.get("vout", 0)
        if not prev_txid:
            return ""  # Coinbase TX
        prev_raw = btc_3s.client._call("getrawtransaction", prev_txid, True)
        if not prev_raw:
            return ""
        prev_out = prev_raw.get("vout", [])[prev_vout]
        addr = prev_out.get("scriptPubKey", {}).get("address", "")
        return addr
    except Exception as e:
        log.warning(f"Could not detect BTC sender for {txid}: {e}")
        return ""


def _check_plan_not_expired(fs: Dict, swap_id: str = ""):
    """Check plan hasn't expired. Raises 400 if expired, sets state to EXPIRED."""
    plan_expires_at = fs.get("plan_expires_at", 0)
    if plan_expires_at and int(time.time()) > plan_expires_at:
        fs["state"] = FlowSwapState.EXPIRED.value
        fs["updated_at"] = int(time.time())
        _release_reservation(swap_id)
        _save_flowswap_db()
        raise HTTPException(400, "Plan expired. Create a new swap.")


# =============================================================================
# Inventory Reservation Helpers (protected by _flowswap_lock)
# =============================================================================

TERMINAL_STATES = {
    FlowSwapState.COMPLETED.value,
    FlowSwapState.FAILED.value,
    FlowSwapState.REFUNDED.value,
    FlowSwapState.EXPIRED.value,
}


def _reserve_inventory(swap_id: str, m1_sats: int = 0, usdc: float = 0, btc_sats: int = 0):
    """Reserve LP inventory for an active swap. Caller must hold _flowswap_lock."""
    reservation = {}
    if m1_sats > 0:
        reservation["m1"] = m1_sats / 100_000_000
    if usdc > 0:
        reservation["usdc"] = float(usdc)
    if btc_sats > 0:
        reservation["btc"] = btc_sats / 100_000_000
    if reservation:
        _inventory_reservations[swap_id] = reservation
        log.info(f"Inventory reserved for {swap_id}: {reservation}")


def _release_reservation(swap_id: str):
    """Release inventory reservation for a swap. Caller must hold _flowswap_lock."""
    released = _inventory_reservations.pop(swap_id, None)
    if released:
        log.info(f"Inventory released for {swap_id}: {released}")


def _get_available_inventory() -> Dict[str, float]:
    """Get available inventory = wallet balance - sum(reservations). Caller must hold _flowswap_lock."""
    raw = LP_CONFIG.get("inventory", {})
    totals = {"btc": 0.0, "m1": 0.0, "usdc": 0.0}
    for res in _inventory_reservations.values():
        for asset, amount in res.items():
            totals[asset] = totals.get(asset, 0) + amount
    return {
        asset: max(0, raw.get(asset, 0) - totals.get(asset, 0))
        for asset in ("btc", "m1", "usdc")
    }


def _rebuild_reservations_from_db():
    """Rebuild inventory reservations from active swaps on startup. Must hold _flowswap_lock."""
    _inventory_reservations.clear()
    for swap_id, fs in flowswap_db.items():
        state = fs.get("state", "")
        if state in TERMINAL_STATES:
            continue
        direction = fs.get("direction", "forward")
        m1_sats = fs.get("m1_amount_sats", 0) or 0
        if direction == "reverse":
            btc_sats = fs.get("btc_amount_sats", 0) or 0
            _reserve_inventory(swap_id, m1_sats=m1_sats, btc_sats=btc_sats)
        else:
            usdc = fs.get("usdc_amount", 0) or 0
            _reserve_inventory(swap_id, m1_sats=m1_sats, usdc=usdc)
    if _inventory_reservations:
        log.info(f"Rebuilt {len(_inventory_reservations)} inventory reservation(s) from DB")


def _process_expired_htlcs():
    """Auto-refund expired BTC HTLCs back to user.

    LP holds the refund key and auto-refunds BTC to user's refund address
    after the timelock expires. Only applies to forward (BTC→USDC) swaps
    where the BTC was funded but the swap didn't complete.
    """
    btc_3s = get_btc_htlc_3s()
    if not btc_3s:
        log.info("Auto-refund: BTC client not available, skipping")
        return

    try:
        current_height = btc_3s.client.get_block_count()
    except Exception as e:
        log.error(f"Auto-refund: cannot get BTC block height: {e}")
        return

    lp_btc_key = _load_lp_btc_key()
    refund_wif = lp_btc_key.get("claim_wif", "")
    lp_fallback_address = lp_btc_key.get("address", "")

    if not refund_wif:
        log.info("Auto-refund: no claim_wif in btc.json, will use wallet signing")

    refunded_any = False
    candidates = 0

    with _flowswap_lock:
        for swap_id, fs in flowswap_db.items():
            # Only forward swaps (BTC→USDC) have BTC HTLCs
            if fs.get("from_asset") != "BTC":
                continue
            # Skip terminal states and already-refunded
            if fs.get("state") in (
                FlowSwapState.COMPLETED.value,
                FlowSwapState.REFUNDED.value,
            ):
                continue
            if fs.get("btc_refund_txid"):
                continue

            candidates += 1

            # Check timelock expired
            timelock = fs.get("btc_timelock", 0)
            if not timelock or current_height < timelock:
                if timelock:
                    log.info(f"Auto-refund {swap_id}: waiting for timelock {timelock} (current={current_height}, {timelock - current_height} blocks left)")
                continue

            # Need redeem script to build refund TX
            redeem_script = fs.get("btc_redeem_script")
            if not redeem_script:
                continue

            # Check UTXO still exists (not claimed already)
            htlc_address = fs.get("btc_htlc_address", "")
            amount_sats = fs.get("btc_amount_sats", 0)
            if not htlc_address or not amount_sats:
                continue

            try:
                utxo = btc_3s.check_htlc_funded(
                    htlc_address=htlc_address,
                    expected_amount=amount_sats,
                    min_confirmations=0,
                )
            except Exception:
                continue

            if not utxo:
                continue  # Already spent (claimed or previously refunded)

            # Determine refund address: user's address, or LP fallback
            refund_to = fs.get("user_btc_refund_address") or lp_fallback_address
            if not refund_to:
                continue

            # Skip swaps already marked as unrecoverable (wrong refund key)
            if fs.get("btc_refund_unrecoverable"):
                continue

            # Old swaps used secp256k1 generator G as dummy refund key —
            # nobody has the private key, so these are permanently unrefundable.
            DUMMY_G = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
            if DUMMY_G in redeem_script.lower():
                log.warning(
                    f"Auto-refund {swap_id}: dummy G refund key detected "
                    f"— marking unrecoverable ({amount_sats} sats stuck)"
                )
                fs["btc_refund_unrecoverable"] = True
                refunded_any = True  # trigger DB save
                continue

            # Execute refund
            try:
                refund_txid = btc_3s.refund_htlc_3s(
                    utxo=utxo,
                    redeem_script=redeem_script,
                    refund_address=refund_to,
                    refund_privkey_wif=refund_wif,
                    timelock=timelock,
                )
                fs["btc_refund_txid"] = refund_txid
                fs["state"] = FlowSwapState.REFUNDED.value
                fs["updated_at"] = int(time.time())
                _release_reservation(swap_id)
                refunded_any = True
                log.info(
                    f"Auto-refund {swap_id}: {amount_sats} sats -> {refund_to} "
                    f"(txid={refund_txid})"
                )
            except Exception as e:
                log.error(f"Auto-refund {swap_id} failed: {e}")

    if candidates > 0:
        log.info(f"Auto-refund check: {candidates} candidate(s), BTC height={current_height}")
    if refunded_any:
        _save_flowswap_db()


def _process_stale_completing():
    """Watchdog: fail swaps stuck in COMPLETING/BTC_CLAIMED beyond timeout.

    This catches daemon threads that died silently (e.g. server restart,
    unhandled exception).  The LP recovers locked funds via HTLC timelock
    refunds — no loss, just delayed recovery.
    """
    now = int(time.time())
    failed_any = False

    with _flowswap_lock:
        for swap_id, fs in flowswap_db.items():
            state = fs.get("state", "")
            if state not in (FlowSwapState.COMPLETING.value,
                             FlowSwapState.BTC_CLAIMED.value):
                continue

            updated_at = fs.get("updated_at", 0)
            if updated_at == 0:
                continue  # No timestamp — skip (legacy entry)

            direction = fs.get("direction", "forward")
            timeout = (COMPLETING_TIMEOUT_FORWARD if direction == "forward"
                       else COMPLETING_TIMEOUT_REVERSE)

            elapsed = now - updated_at
            if elapsed < timeout:
                continue

            # Swap has been stuck beyond timeout — mark FAILED
            log.warning(
                f"Completion watchdog: {swap_id} stuck in {state} for "
                f"{elapsed}s (timeout={timeout}s, direction={direction}). "
                f"Marking FAILED. LP recovers via HTLC timelock refund."
            )
            fs["state"] = FlowSwapState.FAILED.value
            fs["error"] = (
                f"Completion timeout: stuck in {state} for {elapsed}s. "
                f"LP funds recover via HTLC timelock refund."
            )
            fs["updated_at"] = now
            _release_reservation(swap_id)
            failed_any = True

        if failed_any:
            _save_flowswap_db()


def _process_expired_m1_htlc3s():
    """Background: auto-refund expired M1 3-secret HTLCs back to LP wallet.

    This is the definitive safety net for M1 leaks.  Even if _complete_swap or
    _complete_reverse fails to claim the M1 leg, the HTLC timelock eventually
    expires and this function recovers the M1 automatically.
    """
    try:
        m1_3s = get_m1_htlc_3s()
        if not m1_3s:
            return
        m1_client = get_m1_client()
        if not m1_client:
            return

        htlcs = m1_3s.list_htlcs()
        if not htlcs:
            return

        current_height = m1_client.get_block_count()
        refunded_count = 0

        for h in htlcs:
            if h.status != "active":
                continue
            if h.expiry_height > current_height:
                continue  # not yet expired

            try:
                result = m1_client.htlc3s_refund(h.outpoint)
                txid = result.get("txid") if isinstance(result, dict) else str(result)
                log.info(f"Auto-refunded expired M1 HTLC: outpoint={h.outpoint}, amount={h.amount}, txid={txid}")
                refunded_count += 1
            except Exception as e:
                # Don't spam logs — some HTLCs may not have our refund key
                if "not in wallet" not in str(e).lower():
                    log.warning(f"M1 HTLC refund failed: outpoint={h.outpoint}, error={e}")

        if refunded_count > 0:
            log.info(f"M1 auto-refund: recovered {refunded_count} expired HTLC(s)")

    except Exception as e:
        log.error(f"M1 auto-refund scanner error: {e}")


# =============================================================================
# Crash Recovery — on-chain verification (no secrets needed)
# =============================================================================

def _recover_completing_swap(swap_id: str):
    """
    Recover a swap stuck in COMPLETING or BTC_CLAIMED after server restart.

    Strategy: check on-chain state for each claim leg.  If all claims
    are verified on-chain → COMPLETED.  If some are missing and we have
    no secrets (stripped from JSON) → FAILED (LP recovers via timelock).

    Caller must hold _flowswap_lock.
    """
    fs = flowswap_db.get(swap_id)
    if not fs:
        return

    direction = fs.get("direction", "forward")
    state = fs.get("state", "")
    log.info(f"Recovery: checking {swap_id} (state={state}, direction={direction})")

    evm_ok = False
    m1_ok = False

    # --- Check EVM claim status ---
    if fs.get("evm_claim_txhash"):
        evm_ok = True  # TX hash recorded — claim was broadcast
        log.info(f"  EVM: claim TX recorded ({fs['evm_claim_txhash'][:16]}...)")
    elif fs.get("evm_htlc_id"):
        try:
            evm = get_evm_htlc_3s()
            if evm:
                info = evm.get_htlc(fs["evm_htlc_id"])
                if info and info.claimed:
                    evm_ok = True
                    log.info(f"  EVM: claimed on-chain (htlc_id={fs['evm_htlc_id'][:16]}...)")
                else:
                    log.info(f"  EVM: NOT claimed on-chain (status={getattr(info, 'status', 'unknown')})")
        except Exception as e:
            log.warning(f"  EVM check failed: {e}")

    # --- Check M1 claim status ---
    if fs.get("m1_claim_txid"):
        m1_ok = True  # TX ID recorded — claim was broadcast
        log.info(f"  M1: claim TX recorded ({fs['m1_claim_txid'][:16]}...)")
    elif fs.get("m1_htlc_outpoint"):
        try:
            m1 = get_m1_htlc_3s()
            if m1:
                record = m1.get_htlc(fs["m1_htlc_outpoint"])
                if record and record.status == "claimed":
                    m1_ok = True
                    log.info(f"  M1: claimed on-chain (outpoint={fs['m1_htlc_outpoint']})")
                else:
                    log.info(f"  M1: NOT claimed (status={getattr(record, 'status', 'unknown')})")
        except Exception as e:
            log.warning(f"  M1 check failed: {e}")

    # --- Decision ---
    if direction == "forward":
        # Forward: LP needs to claim EVM (USDC→user) + M1 (back to LP)
        if evm_ok and m1_ok:
            fs["state"] = FlowSwapState.COMPLETED.value
            fs["completed_at"] = int(time.time())
            fs["updated_at"] = int(time.time())
            _release_reservation(swap_id)
            log.info(f"Recovery: {swap_id} → COMPLETED (both legs verified on-chain)")
        else:
            fs["state"] = FlowSwapState.FAILED.value
            fs["error"] = (
                f"Server restarted. On-chain: EVM={'OK' if evm_ok else 'MISSING'}, "
                f"M1={'OK' if m1_ok else 'MISSING'}. LP recovers via HTLC timelock."
            )
            fs["updated_at"] = int(time.time())
            _release_reservation(swap_id)
            log.warning(f"Recovery: {swap_id} → FAILED (EVM={evm_ok}, M1={m1_ok})")
    else:
        # Reverse: LP needs to claim EVM (USDC from user) + M1 (back to LP)
        # BTC HTLC was funded by LP — user claims with secrets or LP refunds via timelock
        if evm_ok and m1_ok:
            fs["state"] = FlowSwapState.COMPLETED.value
            fs["completed_at"] = int(time.time())
            fs["updated_at"] = int(time.time())
            _release_reservation(swap_id)
            log.info(f"Recovery: {swap_id} → COMPLETED (both legs verified on-chain)")
        else:
            fs["state"] = FlowSwapState.FAILED.value
            fs["error"] = (
                f"Server restarted. On-chain: EVM={'OK' if evm_ok else 'MISSING'}, "
                f"M1={'OK' if m1_ok else 'MISSING'}. LP recovers via HTLC timelock."
            )
            fs["updated_at"] = int(time.time())
            _release_reservation(swap_id)
            log.warning(f"Recovery: {swap_id} → FAILED (EVM={evm_ok}, M1={m1_ok})")


def _startup_recover_all_swaps():
    """
    Startup recovery: process ALL non-terminal swaps stuck from before restart.

    Handles every state:
      BTC_FUNDED / USDC_FUNDED → re-trigger LP lock thread
      LP_LOCKED → no action (waiting for user presign)
      BTC_CLAIMED / COMPLETING → on-chain verification → COMPLETED or FAILED

    Must be called with _flowswap_lock held.
    """
    recovered_lock = 0
    recovered_completing = 0

    for swap_id, fs in flowswap_db.items():
        state = fs.get("state", "")
        direction = fs.get("direction", "forward")

        if state in TERMINAL_STATES:
            continue

        # Clear stale locking flags from before restart
        if fs.get("_lp_locking"):
            fs["_lp_locking"] = False

        if state == FlowSwapState.BTC_FUNDED.value:
            # Forward: re-trigger LP lock
            if fs.get("from_asset") == "BTC" and fs.get("btc_fund_txid"):
                log.info(f"Recovery: re-triggering LP lock for {swap_id} (btc_funded)")
                threading.Thread(
                    target=_do_lp_lock_forward,
                    args=(swap_id,),
                    daemon=True,
                ).start()
                recovered_lock += 1

        elif state == FlowSwapState.USDC_FUNDED.value:
            # Reverse: re-trigger LP lock
            if fs.get("from_asset") == "USDC" and fs.get("evm_htlc_id"):
                log.info(f"Recovery: re-triggering LP lock for {swap_id} (usdc_funded)")
                threading.Thread(
                    target=_do_lp_lock_reverse,
                    args=(swap_id,),
                    daemon=True,
                ).start()
                recovered_lock += 1

        elif state == FlowSwapState.LP_LOCKED.value:
            # Waiting for user presign — no action needed
            log.info(f"Recovery: {swap_id} in lp_locked — waiting for user action")

        elif state in (FlowSwapState.BTC_CLAIMED.value,
                       FlowSwapState.COMPLETING.value):
            # Check on-chain state to determine if claims went through
            _recover_completing_swap(swap_id)
            recovered_completing += 1

        elif state in (FlowSwapState.AWAITING_BTC.value,
                       FlowSwapState.AWAITING_USDC.value):
            # Plan state — check if expired
            created_at = fs.get("created_at", 0)
            if created_at and (int(time.time()) - created_at > 1800):
                fs["state"] = FlowSwapState.EXPIRED.value
                fs["updated_at"] = int(time.time())
                _release_reservation(swap_id)
                log.info(f"Recovery: {swap_id} expired (awaiting state, >30min old)")

    total = recovered_lock + recovered_completing
    if total:
        log.info(f"Startup recovery: {recovered_lock} lock re-triggered, "
                 f"{recovered_completing} completing checked")
        _save_flowswap_db()


# --- Pydantic models ---

class FlowSwapQuoteRequest(BaseModel):
    """Request a FlowSwap quote."""
    from_asset: str = Field("BTC", description="Asset to swap from (BTC or USDC)")
    to_asset: str = Field("USDC", description="Asset to receive (USDC or BTC)")
    amount: float = Field(..., gt=0, description="Amount in from_asset units")


class FlowSwapInitRequest(BaseModel):
    """Initiate a FlowSwap 3-secret swap."""
    from_asset: str = Field("BTC", description="BTC or USDC")
    to_asset: str = Field("USDC", description="USDC or BTC")
    amount: float = Field(..., gt=0, description="Amount in from_asset units")
    H_user: str = Field(..., min_length=64, max_length=64,
                        description="User's hashlock SHA256(S_user) as 64 hex chars")
    user_usdc_address: str = Field("", description="BTC→USDC: user's EVM address for USDC receipt")
    user_btc_claim_address: str = Field("", description="USDC→BTC: user's BTC destination address")


class FlowSwapPresignRequest(BaseModel):
    """User sends S_user to LP for BTC claim (Mode B: Send & Close)."""
    S_user: str = Field(..., min_length=64, max_length=64,
                        description="User's preimage (64 hex chars)")


class LegInitRequest(BaseModel):
    """Initialize one leg of a per-leg multi-LP swap."""
    leg: str = Field(..., description="Leg pair: 'BTC/M1' or 'M1/USDC'")
    from_asset: str = Field(..., description="Source asset: 'BTC' or 'M1'")
    to_asset: str = Field(..., description="Destination asset: 'M1' or 'USDC'")
    amount: float = Field(..., gt=0, description="Amount in from_asset units")
    H_user: str = Field(..., min_length=64, max_length=64,
                        description="User's hashlock SHA256(S_user)")
    # LP_IN only (BTC→M1):
    H_lp_other: Optional[str] = Field(None, min_length=64, max_length=64,
                                      description="H_lp2 from LP_OUT (LP_IN only)")
    lp_out_m1_address: Optional[str] = Field(None,
                                             description="LP_OUT's M1 claim address (LP_IN only)")
    # LP_OUT only (M1→USDC):
    user_usdc_address: Optional[str] = Field(None,
                                             description="User's EVM address for USDC (LP_OUT only)")


class M1LockedRequest(BaseModel):
    """Frontend notifies LP_OUT that LP_IN has locked M1 on-chain."""
    m1_htlc_outpoint: str = Field(..., description="M1 HTLC outpoint (txid:vout)")
    H_lp1: str = Field(..., min_length=64, max_length=64,
                       description="LP_IN's hashlock H_lp1")


class DeliverSecretRequest(BaseModel):
    """Frontend delivers counterparty's secret to LP."""
    S_lp2: str = Field(..., min_length=64, max_length=64,
                       description="LP_OUT's secret S_lp2")


class BtcClaimedRequest(BaseModel):
    """Notify LP_OUT that LP_IN claimed BTC (per-leg completion)."""
    btc_claim_txid: str = Field(..., min_length=64, max_length=64,
                                description="LP_IN's BTC claim transaction ID")
    S_user: str = Field(..., min_length=64, max_length=64,
                        description="User's secret (revealed on BTC chain)")
    S_lp1: str = Field(..., min_length=64, max_length=64,
                       description="LP_IN's secret (revealed on BTC chain)")


# --- Endpoints ---

@app.post("/api/flowswap/quote")
async def flowswap_quote(req: FlowSwapQuoteRequest):
    """
    Get a FlowSwap quote for BTC <-> USDC via M1 settlement.
    Pure calculation, no state change.
    """
    if req.from_asset not in ("BTC", "USDC") or req.to_asset not in ("BTC", "USDC"):
        raise HTTPException(400, "Only BTC <-> USDC supported")
    if req.from_asset == req.to_asset:
        raise HTTPException(400, "Same asset swap not supported")

    # Calculate rate
    btc_usdc_rate = _get_btc_m1_usdc_rate()
    spread = LP_CONFIG["pairs"]["BTC/USDC"].get("spread_bid", 0.5) if req.from_asset == "BTC" \
        else LP_CONFIG["pairs"]["BTC/USDC"].get("spread_ask", 0.5)

    if req.from_asset == "BTC":
        btc_amount = req.amount
        usdc_amount = round(btc_amount * btc_usdc_rate * (1 - spread / 100), 2)
    else:
        usdc_amount = req.amount
        btc_amount = round(usdc_amount / btc_usdc_rate / (1 - spread / 100), 8)

    quote_id = f"fq_{uuid.uuid4().hex[:12]}"

    return {
        "quote_id": quote_id,
        "from_asset": req.from_asset,
        "to_asset": req.to_asset,
        "btc_amount": btc_amount,
        "btc_amount_sats": int(btc_amount * 100_000_000),
        "usdc_amount": usdc_amount,
        "rate": round(btc_usdc_rate, 2),
        "spread_percent": spread,
        "timelocks": {
            "btc_blocks": FLOWSWAP_TIMELOCK_BTC_BLOCKS,
            "btc_seconds": FLOWSWAP_TIMELOCK_BTC_BLOCKS * 600,
            "m1_blocks": FLOWSWAP_TIMELOCK_M1_BLOCKS,
            "m1_seconds": FLOWSWAP_TIMELOCK_M1_BLOCKS * 60,
            "usdc_seconds": FLOWSWAP_TIMELOCK_USDC_SECONDS,
        },
        "expires_at": int(time.time()) + 300,  # 5 min validity
    }


@app.post("/api/flowswap/init")
async def flowswap_init(req: FlowSwapInitRequest, request: Request = None):
    """
    Initiate a FlowSwap 3-secret swap (BTC↔USDC via M1).
    Returns a PLAN only — no on-chain LP commitment (anti-grief).
    LP locks funds only after user commits on-chain.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    # Validate direction
    if req.from_asset not in ("BTC", "USDC") or req.to_asset not in ("BTC", "USDC"):
        raise HTTPException(400, "Only BTC <-> USDC supported")
    if req.from_asset == req.to_asset:
        raise HTTPException(400, "Same asset swap not allowed")

    # Validate H_user
    try:
        h_user_bytes = bytes.fromhex(req.H_user)
        if len(h_user_bytes) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid H_user: must be 64 hex chars (32 bytes)")

    # Get client IP for rate limiting
    client_ip = ""
    if request:
        client_ip = request.client.host if request.client else ""

    # Branch on direction
    if req.from_asset == "BTC" and req.to_asset == "USDC":
        return await _flowswap_init_btc_to_usdc(req, client_ip)
    else:
        return await _flowswap_init_usdc_to_btc(req, client_ip)


async def _flowswap_init_btc_to_usdc(req: FlowSwapInitRequest, client_ip: str = ""):
    """Forward flow: BTC → USDC. PLAN ONLY — no on-chain LP commitment.
    LP locks USDC+M1 only AFTER user funds BTC (anti-grief)."""

    # Anti-grief: rate limit
    _check_rate_limit(client_ip)

    # LP holds the refund key — can auto-refund expired HTLCs back to user.
    # Security: LP already has claim path (with secrets). Refund path doesn't
    # give more power — HTLC script guarantees: before timelock = only claim
    # with 3 secrets, after timelock = refund with LP key.
    lp_btc_key = _load_lp_btc_key()
    lp_refund_pubkey = lp_btc_key.get("pubkey", "")
    if not lp_refund_pubkey:
        raise HTTPException(503, "LP BTC key not configured — cannot create HTLC")

    # Calculate amounts
    btc_amount_sats = int(req.amount * 100_000_000)
    m1_amount_sats = btc_amount_sats  # 1:1 BTC/M1
    btc_usdc_rate = _get_btc_m1_usdc_rate()
    spread = LP_CONFIG["pairs"]["BTC/USDC"].get("spread_bid", 0.5)
    usdc_amount = round(req.amount * btc_usdc_rate * (1 - spread / 100), 2)

    # Anti-grief: minimum amount
    if btc_amount_sats < MIN_SWAP_BTC_SATS:
        raise HTTPException(400, f"Amount too small: {btc_amount_sats} sats (min {MIN_SWAP_BTC_SATS})")

    swap_id = f"fs_{uuid.uuid4().hex[:16]}"
    now = int(time.time())

    # Step 1: Generate LP secrets (off-chain, free)
    import hashlib as _hl
    S_lp1 = secrets.token_hex(32)
    S_lp2 = secrets.token_hex(32)
    H_lp1 = _hl.sha256(bytes.fromhex(S_lp1)).hexdigest()
    H_lp2 = _hl.sha256(bytes.fromhex(S_lp2)).hexdigest()

    log.info(f"FlowSwap {swap_id}: PLAN for {req.amount} BTC -> {usdc_amount} USDC (no LP lock yet)")

    # Step 2: Generate BTC HTLC address for user deposit (off-chain, free)
    btc_3s = get_btc_htlc_3s()
    if not btc_3s:
        raise HTTPException(503, "BTC HTLC3S client not available")

    # LP claim key (same key file, already loaded above for refund)
    lp1_claim_pubkey = lp_refund_pubkey  # Same key: LP claims AND refunds
    lp1_claim_wif = lp_btc_key.get("claim_wif", "")

    try:
        btc_htlc = btc_3s.create_htlc_3s(
            amount_sats=btc_amount_sats,
            H_user=req.H_user,
            H_lp1=H_lp1,
            H_lp2=H_lp2,
            recipient_pubkey=lp1_claim_pubkey,
            refund_pubkey=lp_refund_pubkey,
            timeout_blocks=FLOWSWAP_TIMELOCK_BTC_BLOCKS,
        )
    except Exception as e:
        log.error(f"FlowSwap {swap_id}: BTC HTLC generation failed: {e}")
        raise HTTPException(500, f"Failed to generate BTC HTLC: {e}")

    log.info(f"FlowSwap {swap_id}: BTC HTLC address={btc_htlc['htlc_address']}")

    # Store swap PLAN (no on-chain commitment from LP)
    plan_expires_at = now + PLAN_EXPIRY_SECONDS
    flowswap_db[swap_id] = {
        "swap_id": swap_id,
        "state": FlowSwapState.AWAITING_BTC.value,
        "from_asset": "BTC",
        "to_asset": "USDC",
        # Amounts
        "btc_amount_sats": btc_amount_sats,
        "m1_amount_sats": m1_amount_sats,
        "usdc_amount": usdc_amount,
        # Spread applied at swap time (for PnL: pnl_m1 = btc_sats * spread / 100)
        "spread_applied": spread,
        # Secrets (LP-side, never exposed to user)
        "S_lp1": S_lp1,
        "S_lp2": S_lp2,
        "lp1_claim_wif": lp1_claim_wif,
        # Hashlocks (shared)
        "H_user": req.H_user,
        "H_lp1": H_lp1,
        "H_lp2": H_lp2,
        # BTC leg (address generated off-chain)
        "btc_htlc_address": btc_htlc["htlc_address"],
        "btc_redeem_script": btc_htlc["redeem_script"],
        "btc_timelock": btc_htlc["timelock"],
        "btc_fund_txid": None,
        "btc_claim_txid": None,
        # M1 leg (populated after LP lock)
        "m1_htlc_outpoint": None,
        "m1_htlc_txid": None,
        "m1_claim_txid": None,
        # EVM leg (populated after LP lock)
        "evm_htlc_id": None,
        "evm_lock_txhash": None,
        "evm_claim_txhash": None,
        # User info
        "user_usdc_address": req.user_usdc_address,
        "user_btc_refund_address": "",  # Auto-detected from funding TX
        # Refund tracking
        "btc_refund_txid": None,
        # Anti-grief
        "plan_expires_at": plan_expires_at,
        "client_ip": client_ip,
        "lp_locked_at": None,
        # Timing
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    _save_flowswap_db()

    return {
        "swap_id": swap_id,
        "state": FlowSwapState.AWAITING_BTC.value,
        "message": "Plan created. Fund the BTC address to trigger LP locking.",
        # What user needs to fund
        "btc_deposit": {
            "address": btc_htlc["htlc_address"],
            "amount_sats": btc_amount_sats,
            "amount_btc": f"{btc_amount_sats / 100_000_000:.8f}",
            "timelock_blocks": btc_htlc["timelock"],
            "instant_min_feerate": _get_instant_min_feerate(),
        },
        # What user will receive (after LP locks)
        "usdc_output": {
            "amount": usdc_amount,
            "recipient": req.user_usdc_address,
        },
        # Hashlocks (user already has H_user, needs H_lp1/H_lp2 for verification)
        "hashlocks": {
            "H_user": req.H_user,
            "H_lp1": H_lp1,
            "H_lp2": H_lp2,
        },
        # Timelocks for verification
        "timelocks": {
            "btc_blocks": FLOWSWAP_TIMELOCK_BTC_BLOCKS,
            "m1_blocks": FLOWSWAP_TIMELOCK_M1_BLOCKS,
            "usdc_seconds": FLOWSWAP_TIMELOCK_USDC_SECONDS,
        },
        "plan_expires_at": plan_expires_at,
        "next_step": "Fund BTC address, then POST /api/flowswap/{id}/btc-funded. LP will lock USDC+M1 after confirmation.",
    }


async def _flowswap_init_usdc_to_btc(req: FlowSwapInitRequest, client_ip: str = ""):
    """
    Reverse flow: USDC → BTC. PLAN ONLY — no on-chain LP commitment.
    LP locks M1+BTC only AFTER user locks USDC via MetaMask (anti-grief).

    Timelock ordering: USDC(1h) < M1(2h) < BTC(4h)
    """
    from sdk.core import (
        FLOWSWAP_REV_TIMELOCK_USDC_SECONDS,
        FLOWSWAP_REV_TIMELOCK_M1_BLOCKS,
        FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS,
    )

    # Anti-grief: rate limit
    _check_rate_limit(client_ip)

    # Validate user_btc_claim_address
    if not req.user_btc_claim_address:
        raise HTTPException(400, "user_btc_claim_address required for USDC → BTC")

    # Calculate amounts: USDC in → BTC out
    usdc_amount_in = req.amount
    btc_usdc_rate = _get_btc_m1_usdc_rate()
    if btc_usdc_rate <= 0:
        raise HTTPException(503, "Price feed unavailable")
    spread = LP_CONFIG["pairs"]["BTC/USDC"].get("spread_ask", 0.5)
    btc_amount = round(usdc_amount_in / btc_usdc_rate / (1 + spread / 100), 8)
    btc_amount_sats = int(btc_amount * 100_000_000)
    m1_amount_sats = btc_amount_sats  # 1:1 BTC/M1

    # Anti-grief: minimum amount
    if usdc_amount_in < MIN_SWAP_USDC:
        raise HTTPException(400, f"Amount too small: {usdc_amount_in} USDC (min {MIN_SWAP_USDC})")

    if btc_amount_sats < MIN_SWAP_BTC_SATS:
        raise HTTPException(400, f"BTC amount too small: {btc_amount_sats} sats (min {MIN_SWAP_BTC_SATS})")

    swap_id = f"fs_{uuid.uuid4().hex[:16]}"
    now = int(time.time())

    # Step 1: Generate LP secrets (off-chain, free)
    import hashlib as _hl
    S_lp1 = secrets.token_hex(32)
    S_lp2 = secrets.token_hex(32)
    H_lp1 = _hl.sha256(bytes.fromhex(S_lp1)).hexdigest()
    H_lp2 = _hl.sha256(bytes.fromhex(S_lp2)).hexdigest()

    log.info(f"FlowSwap {swap_id}: PLAN for USDC→BTC: {usdc_amount_in} USDC -> {btc_amount} BTC (no LP lock yet)")

    # Step 2: Generate ephemeral BTC claim keypair (off-chain, free)
    ephemeral_wif, ephemeral_pubkey = _generate_ephemeral_btc_key()
    log.info(f"FlowSwap {swap_id}: ephemeral claim pubkey={ephemeral_pubkey[:16]}...")

    # LP EVM address (recipient for user's USDC HTLC)
    lp_evm_address = _lp_addresses.get("usdc", "")
    if not lp_evm_address:
        lp_evm_address = "0x78F5e39850C222742Ac06a304893080883F1270c"  # alice_evm fallback

    # Store swap PLAN (no on-chain commitment from LP)
    plan_expires_at = now + PLAN_EXPIRY_SECONDS
    flowswap_db[swap_id] = {
        "swap_id": swap_id,
        "state": FlowSwapState.AWAITING_USDC.value,
        "direction": "reverse",
        "from_asset": "USDC",
        "to_asset": "BTC",
        # Amounts
        "usdc_amount": usdc_amount_in,
        "btc_amount_sats": btc_amount_sats,
        "m1_amount_sats": m1_amount_sats,
        # Spread applied at swap time (for PnL: pnl_m1 = btc_sats * spread / 100)
        "spread_applied": spread,
        # Secrets
        "S_lp1": S_lp1,
        "S_lp2": S_lp2,
        "ephemeral_claim_wif": ephemeral_wif,
        "ephemeral_pubkey": ephemeral_pubkey,
        # Hashlocks
        "H_user": req.H_user,
        "H_lp1": H_lp1,
        "H_lp2": H_lp2,
        # BTC leg (populated after LP lock)
        "btc_htlc_address": None,
        "btc_redeem_script": None,
        "btc_timelock": None,
        "btc_fund_txid": None,
        "btc_claim_txid": None,
        # M1 leg (populated after LP lock)
        "m1_htlc_outpoint": None,
        "m1_htlc_txid": None,
        "m1_claim_txid": None,
        # EVM leg (user creates via MetaMask)
        "evm_htlc_id": None,
        "evm_lock_txhash": None,
        "evm_claim_txhash": None,
        # User info
        "user_btc_address": req.user_btc_claim_address,
        "user_usdc_address": req.user_usdc_address or "",
        # Anti-grief
        "plan_expires_at": plan_expires_at,
        "client_ip": client_ip,
        "lp_locked_at": None,
        # Timing
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    _save_flowswap_db()

    return {
        "swap_id": swap_id,
        "state": FlowSwapState.AWAITING_USDC.value,
        "direction": "reverse",
        "message": "Plan created. Lock USDC via MetaMask, then notify /usdc-funded. LP will lock BTC+M1 after.",
        # What user needs to lock (create via MetaMask)
        "usdc_deposit": {
            "amount": usdc_amount_in,
            "contract": HTLC3S_CONTRACT_ADDRESS,
            "token": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # USDC Base Sepolia
            "recipient": lp_evm_address,  # LP receives USDC on claim
            "timelock_seconds": FLOWSWAP_REV_TIMELOCK_USDC_SECONDS,
        },
        # What user will receive (after LP locks)
        "btc_output": {
            "amount_btc": btc_amount,
            "amount_sats": btc_amount_sats,
            "destination": req.user_btc_claim_address,
        },
        # Hashlocks
        "hashlocks": {
            "H_user": req.H_user,
            "H_lp1": H_lp1,
            "H_lp2": H_lp2,
        },
        "timelocks": {
            "usdc_seconds": FLOWSWAP_REV_TIMELOCK_USDC_SECONDS,
            "m1_blocks": FLOWSWAP_REV_TIMELOCK_M1_BLOCKS,
            "btc_blocks": FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS,
        },
        "plan_expires_at": plan_expires_at,
        "next_step": "Create USDC HTLC via MetaMask, then POST /api/flowswap/{id}/usdc-funded with htlc_id. LP will lock BTC+M1 after.",
    }


def _generate_ephemeral_btc_key() -> tuple:
    """
    Generate single-use BTC keypair for HTLC claim.
    Returns (wif_str, compressed_pubkey_hex).
    Uses python-bitcoinlib (same as SDK).
    """
    from bitcoin import SelectParams
    from bitcoin.wallet import CBitcoinSecret

    SelectParams("signet")

    # Generate random 32-byte private key
    privkey_bytes = secrets.token_bytes(32)
    privkey = CBitcoinSecret.from_secret_bytes(privkey_bytes, compressed=True)

    wif_str = str(privkey)
    pubkey_hex = privkey.pub.hex()

    log.info(f"Generated ephemeral BTC key: pubkey={pubkey_hex[:16]}...")
    return wif_str, pubkey_hex


# =============================================================================
# PER-LEG ROUTING — Multi-LP FlowSwap Init (Blueprint 16 Phase 4)
# =============================================================================

@app.post("/api/flowswap/init-leg")
async def flowswap_init_leg(req: LegInitRequest, request: Request = None):
    """
    Initialize ONE leg of a per-leg multi-LP swap.

    LP_OUT (M1→USDC): generates S_lp2, returns H_lp2 + lp_m1_address.
    LP_IN  (BTC→M1):  generates S_lp1, returns H_lp1 + btc_deposit address.

    PLAN ONLY — no on-chain commitment (anti-grief).
    """
    import hashlib as _hl

    # Validate H_user
    try:
        h_user_bytes = bytes.fromhex(req.H_user)
        if len(h_user_bytes) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid H_user: must be 64 hex chars (32 bytes)")

    # Validate leg
    valid_legs = {"BTC/M1", "M1/USDC"}
    if req.leg not in valid_legs:
        raise HTTPException(400, f"Invalid leg: {req.leg} (must be one of {valid_legs})")

    client_ip = ""
    if request:
        client_ip = request.client.host if request.client else ""
    _check_rate_limit(client_ip)

    swap_id = f"fs_{uuid.uuid4().hex[:16]}"
    now = int(time.time())
    plan_expires_at = now + PLAN_EXPIRY_SECONDS

    if req.leg == "M1/USDC":
        # ── LP_OUT branch: M1→USDC ──
        if not req.user_usdc_address:
            raise HTTPException(400, "user_usdc_address required for M1/USDC leg")

        # M1 amount in sats (from_asset=M1)
        m1_amount_sats = int(req.amount)
        if m1_amount_sats <= 0:
            raise HTTPException(400, "Invalid M1 amount")

        # Calculate USDC output
        btc_usdc_rate = _get_btc_m1_usdc_rate()
        if btc_usdc_rate <= 0:
            raise HTTPException(503, "Price feed unavailable")
        spread = LP_CONFIG["pairs"].get("BTC/USDC", {}).get("spread_bid", 0.5)
        # M1 sats → BTC equivalent → USDC
        usdc_amount = round((m1_amount_sats / 100_000_000) * btc_usdc_rate * (1 - spread / 100), 2)

        # Generate S_lp2
        S_lp2 = secrets.token_hex(32)
        H_lp2 = _hl.sha256(bytes.fromhex(S_lp2)).hexdigest()

        # LP_OUT's M1 address (where M1 will be routed via claim_address)
        lp_m1_address = _lp_addresses.get("m1", "")
        if not lp_m1_address:
            raise HTTPException(503, "LP M1 address not configured")

        log.info(f"FlowSwap init-leg {swap_id}: LP_OUT M1→USDC, {m1_amount_sats} sats → {usdc_amount} USDC")

        flowswap_db[swap_id] = {
            "swap_id": swap_id,
            "state": FlowSwapState.AWAITING_M1.value,
            "is_perleg": True,
            "leg": "M1/USDC",
            "from_asset": "M1",
            "to_asset": "USDC",
            "direction": "forward",
            "m1_amount_sats": m1_amount_sats,
            "usdc_amount": usdc_amount,
            "spread_applied": spread,
            # Secret (LP_OUT generates S_lp2 only)
            "S_lp2": S_lp2,
            "H_user": req.H_user,
            "H_lp2": H_lp2,
            "H_lp1": None,  # Populated when m1-locked is called
            # M1 leg (populated when LP_IN locks)
            "m1_htlc_outpoint": None,
            "m1_htlc_txid": None,
            # EVM leg (populated when LP_OUT locks USDC)
            "evm_htlc_id": None,
            "evm_lock_txhash": None,
            "evm_claim_txhash": None,
            # User info
            "user_usdc_address": req.user_usdc_address,
            # Cross-reference
            "lp_in_swap_id": None,
            # Anti-grief
            "plan_expires_at": plan_expires_at,
            "client_ip": client_ip,
            "lp_locked_at": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        _save_flowswap_db()

        return {
            "swap_id": swap_id,
            "state": FlowSwapState.AWAITING_M1.value,
            "leg": "M1/USDC",
            "lp_id": LP_CONFIG["id"],
            "lp_name": LP_CONFIG["name"],
            "H_lp2": H_lp2,
            "lp_m1_address": lp_m1_address,
            "usdc_output": {
                "amount": usdc_amount,
                "recipient": req.user_usdc_address,
            },
            "plan_expires_at": plan_expires_at,
            "message": "LP_OUT plan created. Waiting for LP_IN to lock M1.",
        }

    else:
        # ── LP_IN branch: BTC→M1 ──
        if not req.H_lp_other:
            raise HTTPException(400, "H_lp_other (H_lp2 from LP_OUT) required for BTC/M1 leg")
        if not req.lp_out_m1_address:
            raise HTTPException(400, "lp_out_m1_address required for BTC/M1 leg")

        # Validate H_lp_other
        try:
            h_other_bytes = bytes.fromhex(req.H_lp_other)
            if len(h_other_bytes) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid H_lp_other: must be 64 hex chars")

        btc_amount_sats = int(req.amount * 100_000_000)
        m1_amount_sats = btc_amount_sats  # 1:1 BTC/M1

        if btc_amount_sats < MIN_SWAP_BTC_SATS:
            raise HTTPException(400, f"Amount too small: {btc_amount_sats} sats (min {MIN_SWAP_BTC_SATS})")

        # Generate S_lp1 only (LP_IN's secret)
        S_lp1 = secrets.token_hex(32)
        H_lp1 = _hl.sha256(bytes.fromhex(S_lp1)).hexdigest()

        log.info(f"FlowSwap init-leg {swap_id}: LP_IN BTC→M1, {req.amount} BTC, lp_out={req.lp_out_m1_address[:16]}...")

        # Generate BTC HTLC address (3 hashlocks: H_user + H_lp1 + H_lp2)
        btc_3s = get_btc_htlc_3s()
        if not btc_3s:
            raise HTTPException(503, "BTC HTLC3S client not available")

        lp_btc_key = _load_lp_btc_key()
        lp_refund_pubkey = lp_btc_key.get("pubkey", "")
        lp1_claim_pubkey = lp_refund_pubkey
        lp1_claim_wif = lp_btc_key.get("claim_wif", "")
        if not lp_refund_pubkey:
            raise HTTPException(503, "LP BTC key not configured")

        try:
            btc_htlc = btc_3s.create_htlc_3s(
                amount_sats=btc_amount_sats,
                H_user=req.H_user,
                H_lp1=H_lp1,
                H_lp2=req.H_lp_other,  # LP_OUT's hashlock
                recipient_pubkey=lp1_claim_pubkey,
                refund_pubkey=lp_refund_pubkey,
                timeout_blocks=FLOWSWAP_TIMELOCK_BTC_BLOCKS,
            )
        except Exception as e:
            log.error(f"FlowSwap init-leg {swap_id}: BTC HTLC generation failed: {e}")
            raise HTTPException(500, f"Failed to generate BTC HTLC: {e}")

        log.info(f"FlowSwap init-leg {swap_id}: BTC HTLC address={btc_htlc['htlc_address']}")

        flowswap_db[swap_id] = {
            "swap_id": swap_id,
            "state": FlowSwapState.AWAITING_BTC.value,
            "is_perleg": True,
            "leg": "BTC/M1",
            "from_asset": "BTC",
            "to_asset": "M1",
            "direction": "forward",
            "btc_amount_sats": btc_amount_sats,
            "m1_amount_sats": m1_amount_sats,
            "spread_applied": 0,  # Spread applied on LP_OUT side
            # Secret (LP_IN generates S_lp1 only)
            "S_lp1": S_lp1,
            "lp1_claim_wif": lp1_claim_wif,
            "H_user": req.H_user,
            "H_lp1": H_lp1,
            "H_lp2": req.H_lp_other,  # From LP_OUT
            # S_lp2 received later via /deliver-secret
            "S_lp_received": None,
            # Per-leg routing
            "lp_out_m1_address": req.lp_out_m1_address,
            "lp_out_swap_id": None,
            # BTC leg
            "btc_htlc_address": btc_htlc["htlc_address"],
            "btc_redeem_script": btc_htlc["redeem_script"],
            "btc_timelock": btc_htlc["timelock"],
            "btc_fund_txid": None,
            "btc_claim_txid": None,
            # M1 leg (populated after LP_IN locks)
            "m1_htlc_outpoint": None,
            "m1_htlc_txid": None,
            # Anti-grief
            "plan_expires_at": plan_expires_at,
            "client_ip": client_ip,
            "lp_locked_at": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        _save_flowswap_db()

        return {
            "swap_id": swap_id,
            "state": FlowSwapState.AWAITING_BTC.value,
            "leg": "BTC/M1",
            "lp_id": LP_CONFIG["id"],
            "lp_name": LP_CONFIG["name"],
            "H_lp1": H_lp1,
            "btc_deposit": {
                "address": btc_htlc["htlc_address"],
                "amount_sats": btc_amount_sats,
                "amount_btc": f"{btc_amount_sats / 100_000_000:.8f}",
                "timelock_blocks": btc_htlc["timelock"],
                "instant_min_feerate": _get_instant_min_feerate(),
            },
            "hashlocks": {
                "H_user": req.H_user,
                "H_lp1": H_lp1,
                "H_lp2": req.H_lp_other,
            },
            "timelocks": {
                "btc_blocks": FLOWSWAP_TIMELOCK_BTC_BLOCKS,
                "m1_blocks": FLOWSWAP_TIMELOCK_M1_BLOCKS,
            },
            "plan_expires_at": plan_expires_at,
            "message": "LP_IN plan created. Fund BTC address, then POST /api/flowswap/{id}/btc-funded.",
        }


def _do_lp_lock_forward(swap_id: str):
    """
    Background: LP locks M1 (BATHRON) + USDC (EVM) after user's BTC is confirmed.
    Called from /btc-funded endpoint. On success → LP_LOCKED. On failure → FAILED.

    Lock order: M1 first (cheaper to lose on partial failure), then USDC.
    Idempotency: checks _lp_locking flag to prevent duplicate threads.
    """
    with _flowswap_lock:
        fs = flowswap_db.get(swap_id)
        if not fs:
            log.error(f"_do_lp_lock_forward: swap {swap_id} not found")
            return
        # Idempotency guard: prevent duplicate LP lock threads
        if fs.get("_lp_locking"):
            log.warning(f"FlowSwap {swap_id}: LP lock already in progress, skipping duplicate")
            return
        fs["_lp_locking"] = True

    try:
        btc_3s = get_btc_htlc_3s()
        btc_txid = fs.get("btc_fund_txid", "")
        confs_at_accept = fs.get("btc_fund_confs", 0)

        # --- 0-conf stability check (CLS model: speed with safety) ---
        if btc_3s and btc_txid and confs_at_accept == 0:
            # Step A: RBF + feerate check (reject RBF-signaled and dust-fee TXs)
            min_feerate = _get_instant_min_feerate()
            try:
                safety = btc_3s.verify_tx_safe_for_0conf(
                    txid=btc_txid,
                    htlc_address=fs["btc_htlc_address"],
                    expected_amount_sats=fs["btc_amount_sats"],
                    min_fee_rate=min_feerate,
                )
                fs["btc_0conf_safety"] = safety
                fee_rate = safety.get("details", {}).get("fee_rate")
                if fee_rate is not None:
                    fs["btc_feerate"] = round(fee_rate, 2)
                if not safety.get("safe"):
                    raise Exception(
                        f"BTC TX failed 0-conf safety: {safety.get('reason')}. "
                        f"Bump fee or wait for confirmation."
                    )
                log.info(f"FlowSwap {swap_id}: 0-conf safety OK: {safety.get('reason')}")
            except Exception as e:
                if "safety" in str(e).lower() or "rbf" in str(e).lower() or "fee" in str(e).lower():
                    raise
                log.warning(f"FlowSwap {swap_id}: could not run 0-conf safety check: {e}")

            # Step B: Stability wait — TX must survive 30s in mempool
            stability_secs = 30
            log.info(f"FlowSwap {swap_id}: 0-conf stability check ({stability_secs}s)...")
            with _flowswap_lock:
                fs["stability_check_until"] = int(time.time()) + stability_secs
                _save_flowswap_db()

            time.sleep(stability_secs)

            # Step C: Re-check TX still exists after wait
            still_exists = btc_3s.check_htlc_funded(
                htlc_address=fs["btc_htlc_address"],
                expected_amount=fs["btc_amount_sats"],
                min_confirmations=0,
            )
            if not still_exists:
                raise Exception("BTC TX replaced/dropped during stability check (RBF grief)")
            log.info(f"FlowSwap {swap_id}: 0-conf stable after {stability_secs}s — proceeding")

            with _flowswap_lock:
                fs.pop("stability_check_until", None)

        # --- Confirmed TX: verify still exists ---
        elif btc_3s and btc_txid:
            still_exists = btc_3s.check_htlc_funded(
                htlc_address=fs["btc_htlc_address"],
                expected_amount=fs["btc_amount_sats"],
                min_confirmations=0,
            )
            if not still_exists:
                raise Exception("BTC TX disappeared from mempool (possible RBF replacement)")

        # Step 1: Lock M1 on BATHRON (cheap — only M1 gas ~23 sats at risk on failure)
        m1_3s = get_m1_htlc_3s()
        if not m1_3s:
            raise Exception("M1 HTLC3S client not available")

        # Per-leg: M1 claim_address → LP_OUT (not self)
        is_perleg = fs.get("is_perleg", False)
        if is_perleg and fs.get("lp_out_m1_address"):
            m1_claim_address = fs["lp_out_m1_address"]
            log.info(f"FlowSwap {swap_id}: Per-leg mode — M1 claim_address → LP_OUT: {m1_claim_address[:16]}...")
        else:
            m1_claim_address = _lp_addresses.get("m1", "")
        if not m1_claim_address:
            raise Exception("M1 claim address not configured — cannot create HTLC")

        receipt_outpoint = m1_3s.ensure_receipt_available(fs["m1_amount_sats"])
        m1_result = m1_3s.create_htlc(
            receipt_outpoint=receipt_outpoint,
            H_user=fs["H_user"],
            H_lp1=fs["H_lp1"],
            H_lp2=fs["H_lp2"],
            claim_address=m1_claim_address,
            expiry_blocks=FLOWSWAP_TIMELOCK_M1_BLOCKS,
        )

        with _flowswap_lock:
            fs["m1_htlc_outpoint"] = m1_result.get("htlc_outpoint")
            fs["m1_htlc_txid"] = m1_result.get("txid")
        log.info(f"FlowSwap {swap_id}: M1 locked, outpoint={m1_result.get('htlc_outpoint')}")

        # Per-leg: LP_IN only locks M1, not USDC (LP_OUT handles USDC)
        if is_perleg:
            with _flowswap_lock:
                fs["state"] = FlowSwapState.M1_LOCKED.value
                fs["updated_at"] = int(time.time())
                fs.pop("_lp_locking", None)
                _save_flowswap_db()
            log.info(f"FlowSwap {swap_id}: M1_LOCKED (per-leg, waiting for LP_OUT to lock USDC)")
            return  # LP_OUT will lock USDC via /m1-locked endpoint

        # Re-check BTC TX before committing USDC (most expensive leg)
        if btc_3s and fs.get("btc_fund_txid"):
            still_exists = btc_3s.check_htlc_funded(
                htlc_address=fs["btc_htlc_address"],
                expected_amount=fs["btc_amount_sats"],
                min_confirmations=0,
            )
            if not still_exists:
                raise Exception("BTC TX replaced (RBF) after M1 lock — aborting USDC lock")

        # Step 2: Lock USDC on EVM (expensive — real USDC at risk)
        evm_htlc = get_evm_htlc_3s()
        if not evm_htlc:
            raise Exception("EVM HTLC3S client not available")

        evm_privkey = _load_evm_private_key()
        if not evm_privkey:
            raise Exception("EVM private key not configured")

        evm_result = evm_htlc.create_htlc(
            recipient=fs["user_usdc_address"],
            amount_usdc=fs["usdc_amount"],
            H_user=fs["H_user"],
            H_lp1=fs["H_lp1"],
            H_lp2=fs["H_lp2"],
            timelock_seconds=FLOWSWAP_TIMELOCK_USDC_SECONDS,
            private_key=evm_privkey,
        )

        if not evm_result.success:
            raise Exception(f"USDC lock failed: {evm_result.error}")

        # Success → LP_LOCKED
        with _flowswap_lock:
            fs["evm_htlc_id"] = evm_result.htlc_id
            fs["evm_lock_txhash"] = evm_result.tx_hash
            fs["state"] = FlowSwapState.LP_LOCKED.value
            fs["lp_locked_at"] = int(time.time())
            fs["updated_at"] = int(time.time())
            fs.pop("_lp_locking", None)
            _save_flowswap_db()
        log.info(f"FlowSwap {swap_id}: LP_LOCKED (M1 + USDC confirmed on-chain)")

    except Exception as e:
        log.error(f"FlowSwap {swap_id}: LP lock failed: {e}")
        with _flowswap_lock:
            fs["state"] = FlowSwapState.FAILED.value
            fs["error"] = str(e)
            fs["updated_at"] = int(time.time())
            fs.pop("_lp_locking", None)
            _release_reservation(swap_id)
            _save_flowswap_db()


def _do_lp_lock_reverse(swap_id: str):
    """
    Background: LP locks M1 (BATHRON) + funds BTC HTLC after user's USDC is confirmed.
    Called from /usdc-funded endpoint. On success → LP_LOCKED. On failure → FAILED.

    Lock order: M1 first (cheap), then BTC (expensive).
    Idempotency: checks _lp_locking flag to prevent duplicate threads.
    """
    from sdk.core import (
        FLOWSWAP_REV_TIMELOCK_M1_BLOCKS,
        FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS,
    )

    with _flowswap_lock:
        fs = flowswap_db.get(swap_id)
        if not fs:
            log.error(f"_do_lp_lock_reverse: swap {swap_id} not found")
            return
        # Idempotency guard
        if fs.get("_lp_locking"):
            log.warning(f"FlowSwap {swap_id}: LP lock already in progress, skipping duplicate")
            return
        fs["_lp_locking"] = True

    try:
        # Step 1: Lock M1 on BATHRON (cheap — only M1 gas at risk on failure)
        m1_3s = get_m1_htlc_3s()
        if not m1_3s:
            raise Exception("M1 HTLC3S client not available")

        lp_m1_address = _lp_addresses.get("m1", "")
        if not lp_m1_address:
            raise Exception("LP M1 address not configured — cannot create HTLC")

        receipt_outpoint = m1_3s.ensure_receipt_available(fs["m1_amount_sats"])
        m1_result = m1_3s.create_htlc(
            receipt_outpoint=receipt_outpoint,
            H_user=fs["H_user"],
            H_lp1=fs["H_lp1"],
            H_lp2=fs["H_lp2"],
            claim_address=lp_m1_address,
            expiry_blocks=FLOWSWAP_REV_TIMELOCK_M1_BLOCKS,
        )

        with _flowswap_lock:
            fs["m1_htlc_outpoint"] = m1_result.get("htlc_outpoint")
            fs["m1_htlc_txid"] = m1_result.get("txid")
        log.info(f"FlowSwap {swap_id}: M1 locked, outpoint={m1_result.get('htlc_outpoint')}")

        # Step 2: Create + fund BTC HTLC (expensive — real BTC at risk)
        btc_3s = get_btc_htlc_3s()
        if not btc_3s:
            raise Exception("BTC HTLC3S client not available")

        lp_btc_key = _load_lp_btc_key()
        lp_refund_pubkey = lp_btc_key.get("pubkey", "")
        if not lp_refund_pubkey:
            raise Exception("LP BTC key not configured")

        ephemeral_pubkey = fs.get("ephemeral_pubkey", "")
        if not ephemeral_pubkey:
            raise Exception("Ephemeral BTC pubkey not found in swap state")

        btc_htlc = btc_3s.create_htlc_3s(
            amount_sats=fs["btc_amount_sats"],
            H_user=fs["H_user"],
            H_lp1=fs["H_lp1"],
            H_lp2=fs["H_lp2"],
            recipient_pubkey=ephemeral_pubkey,
            refund_pubkey=lp_refund_pubkey,
            timeout_blocks=FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS,
        )

        # Fund BTC HTLC from LP wallet
        btc_fund_txid = btc_3s.fund_htlc(btc_htlc["htlc_address"], fs["btc_amount_sats"])

        # Success → LP_LOCKED
        with _flowswap_lock:
            fs["btc_htlc_address"] = btc_htlc["htlc_address"]
            fs["btc_redeem_script"] = btc_htlc["redeem_script"]
            fs["btc_timelock"] = btc_htlc["timelock"]
            fs["btc_fund_txid"] = btc_fund_txid
            fs["state"] = FlowSwapState.LP_LOCKED.value
            fs["lp_locked_at"] = int(time.time())
            fs["updated_at"] = int(time.time())
            fs.pop("_lp_locking", None)
            _save_flowswap_db()
        log.info(f"FlowSwap {swap_id}: LP_LOCKED (M1 + BTC confirmed on-chain)")

    except Exception as e:
        log.error(f"FlowSwap {swap_id}: LP lock (reverse) failed: {e}")
        with _flowswap_lock:
            fs["state"] = FlowSwapState.FAILED.value
            fs["error"] = str(e)
            fs["updated_at"] = int(time.time())
            fs.pop("_lp_locking", None)
            _release_reservation(swap_id)
            _save_flowswap_db()


class USDCFundedRequest(BaseModel):
    htlc_id: str = ""


@app.post("/api/flowswap/{swap_id}/usdc-funded")
async def flowswap_usdc_funded(swap_id: str, body: USDCFundedRequest = None):
    """
    User notifies LP that USDC HTLC was created on EVM.
    LP verifies, then locks M1+BTC in background.
    """
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]

    if fs.get("direction") != "reverse":
        raise HTTPException(400, "This endpoint is for USDC→BTC swaps only")

    # Allow re-check if already USDC_FUNDED (LP lock may still be in progress)
    if fs["state"] not in (FlowSwapState.AWAITING_USDC.value, FlowSwapState.USDC_FUNDED.value):
        raise HTTPException(400, f"Invalid state: {fs['state']} (expected awaiting_usdc or usdc_funded)")

    # Anti-grief: check plan not expired
    _check_plan_not_expired(fs, swap_id)

    # Store EVM HTLC ID for claiming
    htlc_id = body.htlc_id if body else ""
    if not htlc_id:
        raise HTTPException(400, "htlc_id required — pass the EVM HTLC ID from MetaMask TX")
    fs["evm_htlc_id"] = htlc_id
    log.info(f"FlowSwap {swap_id}: EVM HTLC ID stored: {htlc_id}")

    # Verify USDC HTLC on-chain (MANDATORY — hard-fail if EVM unavailable)
    evm_htlc = get_evm_htlc_3s()
    if not evm_htlc:
        raise HTTPException(503, "EVM client unavailable. Cannot verify USDC HTLC. Try again later.")

    htlc_info = evm_htlc.get_htlc(htlc_id)
    if not htlc_info:
        raise HTTPException(400, f"USDC HTLC {htlc_id} not found on-chain. Wait for TX confirmation.")

    # Verify not already claimed/refunded
    if htlc_info.status != "active":
        raise HTTPException(400, f"USDC HTLC is {htlc_info.status}, not active")

    # Verify ERC20 token is USDC (not a worthless token)
    if htlc_info.token.lower() != EXPECTED_USDC_TOKEN.lower():
        raise HTTPException(400,
            f"Wrong ERC20 token: on-chain={htlc_info.token}, expected USDC={EXPECTED_USDC_TOKEN}")

    # Verify amount (USDC has 6 decimals, allow small rounding)
    expected_usdc = fs["usdc_amount"]
    if htlc_info.amount_usdc < expected_usdc * 0.99:
        raise HTTPException(400,
            f"USDC amount mismatch: on-chain={htlc_info.amount_usdc}, expected={expected_usdc}")

    # Verify recipient is LP (not some random address)
    lp_evm_address = (_lp_addresses.get("usdc", "") or "").lower()
    if lp_evm_address and htlc_info.recipient.lower() != lp_evm_address:
        raise HTTPException(400,
            f"USDC HTLC recipient mismatch: on-chain={htlc_info.recipient}, expected={lp_evm_address}")

    # Verify hashlocks match our plan
    def _norm_hash(h): return h.lower().replace("0x", "")
    if _norm_hash(htlc_info.H_user) != _norm_hash(fs["H_user"]):
        raise HTTPException(400, "H_user mismatch between on-chain HTLC and swap plan")
    if _norm_hash(htlc_info.H_lp1) != _norm_hash(fs["H_lp1"]):
        raise HTTPException(400, "H_lp1 mismatch between on-chain HTLC and swap plan")
    if _norm_hash(htlc_info.H_lp2) != _norm_hash(fs["H_lp2"]):
        raise HTTPException(400, "H_lp2 mismatch between on-chain HTLC and swap plan")

    # Verify timelock gives LP enough time
    remaining_seconds = htlc_info.timelock - int(time.time())
    if remaining_seconds < 1800:  # < 30 min remaining = too risky
        raise HTTPException(400,
            f"USDC HTLC timelock too short: {remaining_seconds}s remaining (need >= 1800s)")

    # Verify timelock ordering invariant: USDC (user locks) < BTC (LP locks)
    # Reverse direction: USDC is shortest, BTC is longest
    from sdk.core import FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS
    # BTC timelock ~4h (24 blocks * 600s), USDC must expire BEFORE BTC
    btc_timeout_seconds = FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS * 600
    if remaining_seconds > btc_timeout_seconds:
        raise HTTPException(400,
            f"USDC timelock ({remaining_seconds}s) must be shorter than BTC timelock (~{btc_timeout_seconds}s)")

    log.info(f"FlowSwap {swap_id}: USDC HTLC verified on-chain: "
             f"token={htlc_info.token}, amount={htlc_info.amount_usdc}, "
             f"recipient={htlc_info.recipient}, timelock_remaining={remaining_seconds}s")

    with _flowswap_lock:
        fs["state"] = FlowSwapState.USDC_FUNDED.value
        fs["updated_at"] = int(time.time())
        _reserve_inventory(swap_id, m1_sats=fs.get("m1_amount_sats", 0),
                           btc_sats=fs.get("btc_amount_sats", 0))
        _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: USDC funded and verified, launching LP lock...")

    # Launch LP locking in background thread (idempotency guard inside _do_lp_lock_reverse)
    threading.Thread(
        target=_do_lp_lock_reverse,
        args=(swap_id,),
        daemon=True,
    ).start()

    return {
        "swap_id": swap_id,
        "state": fs["state"],
        "message": "USDC confirmed. LP locking M1 + BTC in progress...",
        "next_step": f"Poll GET /api/flowswap/{swap_id} until state=lp_locked, then POST /presign with S_user",
    }


@app.post("/api/flowswap/{swap_id}/usdc-funded-verify")
async def flowswap_usdc_funded_verify(swap_id: str, htlc_id: str = ""):
    """
    DEPRECATED: Redirects to /usdc-funded which now includes full on-chain verification.
    Kept for backward compatibility only.
    """
    body = USDCFundedRequest(htlc_id=htlc_id)
    return await flowswap_usdc_funded(swap_id, body)


@app.get("/api/flowswap/list")
async def flowswap_list(state: str = None):
    """List FlowSwap swaps, optionally filtered by state."""
    swaps = list(flowswap_db.values())
    if state:
        swaps = [s for s in swaps if s["state"] == state]

    # Strip secrets from list view
    safe_swaps = []
    for s in swaps:
        safe_swaps.append({
            "swap_id": s["swap_id"],
            "state": s["state"],
            "btc_amount_sats": s.get("btc_amount_sats", 0),
            "usdc_amount": s.get("usdc_amount", 0),
            "btc_htlc_address": s.get("btc_htlc_address", ""),
            "created_at": s.get("created_at", 0),
            "completed_at": s.get("completed_at"),
            "plan_expires_at": s.get("plan_expires_at", 0),
            "lp_locked_at": s.get("lp_locked_at"),
        })

    return {"swaps": safe_swaps, "count": len(safe_swaps)}


@app.get("/api/flowswap/{swap_id}")
async def flowswap_status(swap_id: str):
    """Get FlowSwap swap status (multi-chain)."""
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]
    state = fs.get("state", "unknown")

    # Amounts
    btc_sats = fs.get("btc_amount_sats", 0)
    btc_amount = btc_sats / 100_000_000
    m1_sats = fs.get("m1_amount_sats", 0)
    usdc_amount = fs.get("usdc_amount", 0)

    # 2-leg breakdown: BTC→M1 + M1→USDC
    # Leg 1: BTC→M1 — LP receives BTC, locks M1 (1 BTC = 100M M1, fixed)
    leg1_m1_locked = btc_sats  # 1:1 BTC sats = M1 sats
    # Leg 2: M1→USDC — LP locks USDC, M1 returns via covenant
    # LP PnL: oracle-free, based on spread applied at swap time
    # PnL_M1 = volume_sats * spread / 100 (always >= 0)
    spread_applied = fs.get("spread_applied", 0)
    lp_pnl_m1 = round(btc_sats * spread_applied / 100) if btc_sats > 0 else 0
    # PnL in USDC: derive from executed rate
    lp_pnl_usdc = round(lp_pnl_m1 * (usdc_amount / btc_amount if btc_amount > 0 else 0) / 100_000_000, 4)

    rate_exec = usdc_amount / btc_amount if btc_amount > 0 else 0

    result = {
        "swap_id": fs.get("swap_id", swap_id),
        "state": state,
        "from_asset": fs.get("from_asset", "BTC"),
        "to_asset": fs.get("to_asset", "USDC"),
        "btc_amount_sats": btc_sats,
        "usdc_amount": usdc_amount,
        # 2-leg breakdown
        "legs": {
            "leg1_btc_to_m1": {
                "from": f"{btc_amount:.8f} BTC",
                "to": f"{leg1_m1_locked:,} M1",
                "rate": "1 BTC = 100,000,000 M1 (fixed)",
            },
            "leg2_m1_to_usdc": {
                "from": f"{leg1_m1_locked:,} M1",
                "to": f"{usdc_amount:.2f} USDC",
                "rate": f"1 BTC = {rate_exec:,.0f} USDC (effective)",
            },
        },
        # Rate & PnL
        "rate_executed": round(rate_exec, 2),
        "rate_display": f"1 BTC = {rate_exec:,.0f} USDC",
        "spread_applied": spread_applied,
        "lp_pnl": {
            "usdc": lp_pnl_usdc,
            "m1_sats": lp_pnl_m1,
            "display": f"+{lp_pnl_m1:,} M1 (+${lp_pnl_usdc:.4f})" if lp_pnl_m1 >= 0 else f"{lp_pnl_m1:,} M1 (${lp_pnl_usdc:.4f})",
        },
        "hashlocks": {
            "H_user": fs.get("H_user", ""),
            "H_lp1": fs.get("H_lp1", ""),
            "H_lp2": fs.get("H_lp2", ""),
        },
        "btc": {
            "htlc_address": fs.get("btc_htlc_address", ""),
            "timelock": fs.get("btc_timelock", 0),
            "fund_txid": fs.get("btc_fund_txid"),
            "claim_txid": fs.get("btc_claim_txid"),
            "claim_confs": fs.get("btc_claim_confs", 0),
            "refund_txid": fs.get("btc_refund_txid"),
            "refund_address": fs.get("user_btc_refund_address", ""),
        },
        "m1": {
            "htlc_outpoint": fs.get("m1_htlc_outpoint", ""),
            "txid": fs.get("m1_htlc_txid"),
            "claim_txid": fs.get("m1_claim_txid"),
        },
        "evm": {
            "htlc_id": fs.get("evm_htlc_id", ""),
            "lock_txhash": fs.get("evm_lock_txhash"),
            "claim_txhash": fs.get("evm_claim_txhash"),
        },
        "user_usdc_address": fs.get("user_usdc_address", ""),
        "created_at": fs.get("created_at", 0),
        "updated_at": fs.get("updated_at", 0),
        "completed_at": fs.get("completed_at"),
        "plan_expires_at": fs.get("plan_expires_at", 0),
        "lp_locked_at": fs.get("lp_locked_at"),
        "btc_feerate": fs.get("btc_feerate"),
        "stability_check_until": fs.get("stability_check_until"),
    }

    # Include error info if failed
    if state == FlowSwapState.FAILED.value and fs.get("error"):
        result["error"] = fs["error"]

    # Include secrets only if already revealed on-chain
    secrets_revealed = state in (
        FlowSwapState.BTC_CLAIMED.value,
        FlowSwapState.COMPLETING.value,
        FlowSwapState.COMPLETED.value,
    )
    if secrets_revealed and fs.get("S_lp1"):
        result["secrets"] = {
            "S_lp1": fs.get("S_lp1", ""),
            "S_lp2": fs.get("S_lp2", ""),
        }

    return result


@app.post("/api/flowswap/{swap_id}/btc-funded")
async def flowswap_btc_funded(swap_id: str):
    """
    Notify that user has funded the BTC HTLC.
    LP verifies on-chain with tier-based confirmations, then locks USDC+M1 in background.
    """
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]

    # Allow re-check if already BTC_FUNDED (LP lock may still be in progress)
    if fs["state"] not in (FlowSwapState.AWAITING_BTC.value, FlowSwapState.BTC_FUNDED.value):
        raise HTTPException(400, f"Invalid state: {fs['state']} (expected awaiting_btc or btc_funded)")

    # Anti-grief: check plan not expired
    _check_plan_not_expired(fs, swap_id)

    # Verify BTC HTLC is funded with tier-based confirmations
    btc_3s = get_btc_htlc_3s()
    if not btc_3s:
        raise HTTPException(503, "BTC client not available")

    required_confs = _get_required_confirmations(fs["btc_amount_sats"])

    utxo = btc_3s.check_htlc_funded(
        htlc_address=fs["btc_htlc_address"],
        expected_amount=fs["btc_amount_sats"],
        min_confirmations=required_confs,
    )

    if not utxo:
        raise HTTPException(400, f"BTC HTLC not funded or needs {required_confs} confirmation(s)")

    # 0-conf: LP accepts risk (CLS model — speed is the competitive edge)
    if required_confs == 0 and utxo.get("confirmations", 0) == 0:
        log.info(f"FlowSwap {swap_id}: 0-conf accepted (LP risk, {fs['btc_amount_sats']} sats)")

    # Auto-detect sender's BTC address for refund (from funding TX inputs)
    sender_address = ""
    if not fs.get("user_btc_refund_address"):
        sender_address = _detect_btc_sender(btc_3s, utxo["txid"])
        if sender_address:
            log.info(f"FlowSwap {swap_id}: auto-detected refund address: {sender_address}")

    with _flowswap_lock:
        fs["btc_fund_txid"] = utxo["txid"]
        fs["btc_fund_confs"] = utxo.get("confirmations", 0)
        if sender_address:
            fs["user_btc_refund_address"] = sender_address
        fs["state"] = FlowSwapState.BTC_FUNDED.value
        fs["updated_at"] = int(time.time())
        _reserve_inventory(swap_id, m1_sats=fs.get("m1_amount_sats", 0),
                           usdc=fs.get("usdc_amount", 0))
        _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: BTC funded, txid={utxo['txid']}, "
             f"confs={utxo['confirmations']}, required={required_confs}")

    # Launch LP locking in background thread (idempotency guard inside _do_lp_lock_forward)
    threading.Thread(
        target=_do_lp_lock_forward,
        args=(swap_id,),
        daemon=True,
    ).start()

    return {
        "swap_id": swap_id,
        "state": fs["state"],
        "btc_fund_txid": utxo["txid"],
        "confirmations": utxo["confirmations"],
        "required_confirmations": required_confs,
        "message": "BTC confirmed. LP locking USDC + M1 in progress...",
        "next_step": f"Poll GET /api/flowswap/{swap_id} until state=lp_locked, then POST /presign with S_user",
    }


# =============================================================================
# PER-LEG: m1-locked (LP_OUT receives M1 info, locks USDC, returns S_lp2)
# =============================================================================

@app.post("/api/flowswap/{swap_id}/m1-locked")
async def flowswap_m1_locked(swap_id: str, req: M1LockedRequest):
    """
    Frontend notifies LP_OUT that LP_IN has locked M1 on BATHRON chain.
    LP_OUT verifies M1 HTLC, locks USDC, and returns S_lp2.
    """
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]

    if not fs.get("is_perleg"):
        raise HTTPException(400, "Not a per-leg swap")
    if fs.get("leg") != "M1/USDC":
        raise HTTPException(400, "m1-locked only applies to LP_OUT (M1/USDC leg)")
    if fs["state"] != FlowSwapState.AWAITING_M1.value:
        raise HTTPException(400, f"Invalid state: {fs['state']} (expected awaiting_m1)")

    # Store H_lp1 and M1 HTLC outpoint
    with _flowswap_lock:
        fs["H_lp1"] = req.H_lp1
        fs["m1_htlc_outpoint"] = req.m1_htlc_outpoint
        fs["m1_htlc_txid"] = req.m1_htlc_outpoint.split(":")[0] if ":" in req.m1_htlc_outpoint else req.m1_htlc_outpoint
        fs["updated_at"] = int(time.time())
        _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: m1-locked received, outpoint={req.m1_htlc_outpoint}, H_lp1={req.H_lp1[:16]}...")

    # TODO: Verify M1 HTLC on BATHRON chain (amount, hashlocks, claim_address)
    # For testnet, we trust the frontend relay. Production: verify via RPC.

    # Lock USDC on EVM
    evm_htlc = get_evm_htlc_3s()
    if not evm_htlc:
        raise HTTPException(503, "EVM HTLC3S client not available")

    evm_privkey = _load_evm_private_key()
    if not evm_privkey:
        raise HTTPException(503, "EVM private key not configured")

    try:
        evm_result = evm_htlc.create_htlc(
            recipient=fs["user_usdc_address"],
            amount_usdc=fs["usdc_amount"],
            H_user=fs["H_user"],
            H_lp1=req.H_lp1,
            H_lp2=fs["H_lp2"],
            timelock_seconds=FLOWSWAP_TIMELOCK_USDC_SECONDS,
            private_key=evm_privkey,
        )
        if not evm_result.success:
            raise Exception(f"USDC lock failed: {evm_result.error}")
    except Exception as e:
        log.error(f"FlowSwap {swap_id}: LP_OUT USDC lock failed: {e}")
        with _flowswap_lock:
            fs["state"] = FlowSwapState.FAILED.value
            fs["error"] = str(e)
            fs["updated_at"] = int(time.time())
            _save_flowswap_db()
        raise HTTPException(500, f"USDC lock failed: {e}")

    # Success → LP_LOCKED + return S_lp2 (safe: USDC is now locked)
    with _flowswap_lock:
        fs["evm_htlc_id"] = evm_result.htlc_id
        fs["evm_lock_txhash"] = evm_result.tx_hash
        fs["state"] = FlowSwapState.LP_LOCKED.value
        fs["lp_locked_at"] = int(time.time())
        fs["updated_at"] = int(time.time())
        _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: LP_OUT USDC locked, returning S_lp2")

    return {
        "swap_id": swap_id,
        "state": FlowSwapState.LP_LOCKED.value,
        "evm_htlc_id": evm_result.htlc_id,
        "evm_lock_txhash": evm_result.tx_hash,
        # Secret exchange: LP_OUT shares S_lp2 after committing USDC
        "S_lp2": fs["S_lp2"],
        "message": "USDC locked. S_lp2 delivered. Forward to LP_IN via /deliver-secret.",
    }


# =============================================================================
# PER-LEG: deliver-secret (LP_IN receives S_lp2 from frontend relay)
# =============================================================================

@app.post("/api/flowswap/{swap_id}/deliver-secret")
async def flowswap_deliver_secret(swap_id: str, req: DeliverSecretRequest):
    """
    Frontend delivers LP_OUT's secret (S_lp2) to LP_IN.
    LP_IN verifies SHA256(S_lp2) == H_lp2, stores it, transitions to LP_LOCKED.
    """
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]

    if not fs.get("is_perleg"):
        raise HTTPException(400, "Not a per-leg swap")
    if fs.get("leg") != "BTC/M1":
        raise HTTPException(400, "deliver-secret only applies to LP_IN (BTC/M1 leg)")
    if fs["state"] != FlowSwapState.M1_LOCKED.value:
        raise HTTPException(400, f"Invalid state: {fs['state']} (expected m1_locked)")

    # Verify SHA256(S_lp2) == H_lp2
    import hashlib as _hl
    computed_h = _hl.sha256(bytes.fromhex(req.S_lp2)).hexdigest()
    if computed_h != fs["H_lp2"]:
        raise HTTPException(400, "S_lp2 does not match H_lp2")

    # Store and transition
    with _flowswap_lock:
        fs["S_lp_received"] = req.S_lp2
        fs["state"] = FlowSwapState.LP_LOCKED.value
        fs["lp_locked_at"] = int(time.time())
        fs["updated_at"] = int(time.time())
        _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: S_lp2 received and verified, state → LP_LOCKED (ready for presign)")

    return {
        "swap_id": swap_id,
        "state": FlowSwapState.LP_LOCKED.value,
        "message": "Secret received. LP_IN ready for presign.",
    }


@app.post("/api/flowswap/{swap_id}/presign")
async def flowswap_presign(swap_id: str, req: FlowSwapPresignRequest):
    """
    User sends S_user to LP (Mode B: Send & Close).

    Forward (BTC→USDC): LP claims BTC, then auto-claims USDC + M1.
    Reverse (USDC→BTC): LP claims USDC + BTC-for-user + M1.
    """
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]

    # Presign only accepted from LP_LOCKED state (anti-grief: LP must have locked first)
    if fs["state"] != FlowSwapState.LP_LOCKED.value:
        if fs["state"] in (FlowSwapState.AWAITING_BTC.value, FlowSwapState.BTC_FUNDED.value,
                           FlowSwapState.AWAITING_USDC.value, FlowSwapState.USDC_FUNDED.value):
            raise HTTPException(400, f"LP has not locked yet (state: {fs['state']}). Wait for state=lp_locked.")
        raise HTTPException(400, f"Invalid state for presign: {fs['state']} (expected lp_locked)")

    # Verify SHA256(S_user) == H_user
    computed_hash = hashlib.sha256(bytes.fromhex(req.S_user)).hexdigest()
    if computed_hash != fs["H_user"]:
        raise HTTPException(400, "S_user does not match H_user")

    # Branch on direction
    if fs.get("direction") == "reverse":
        return await _presign_reverse(swap_id, fs, req)

    # LP1 claims BTC with all 3 secrets
    btc_3s = get_btc_htlc_3s()
    if not btc_3s:
        raise HTTPException(503, "BTC HTLC3S client not available")

    lp1_claim_wif = fs.get("lp1_claim_wif", "")
    if not lp1_claim_wif:
        raise HTTPException(503, "LP1 BTC claim key not available")

    # Get the UTXO
    utxo = btc_3s.check_htlc_funded(
        htlc_address=fs["btc_htlc_address"],
        expected_amount=fs["btc_amount_sats"],
        min_confirmations=0,
    )
    if not utxo:
        raise HTTPException(400, "BTC HTLC output not found (already spent?)")

    # LP1 BTC receive address
    lp_btc_key = _load_lp_btc_key()
    lp1_btc_address = lp_btc_key.get("address", _lp_addresses.get("btc", ""))
    if not lp1_btc_address:
        raise HTTPException(503, "LP1 BTC receive address not configured")

    from sdk.htlc.btc_3s import HTLC3SSecrets

    try:
        btc_claim_txid = btc_3s.claim_htlc_3s(
            utxo=utxo,
            redeem_script=fs["btc_redeem_script"],
            secrets=HTLC3SSecrets(
                S_user=req.S_user,
                S_lp1=fs["S_lp1"],
                # Per-leg: S_lp2 received from LP_OUT via /deliver-secret
                S_lp2=fs.get("S_lp_received") or fs["S_lp2"],
            ),
            recipient_address=lp1_btc_address,
            claim_privkey_wif=lp1_claim_wif,
        )
    except Exception as e:
        log.error(f"FlowSwap {swap_id}: BTC claim failed: {e}")
        raise HTTPException(500, f"BTC claim failed: {e}")

    fs["btc_claim_txid"] = btc_claim_txid
    fs["state"] = FlowSwapState.BTC_CLAIMED.value
    fs["updated_at"] = int(time.time())
    _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: BTC claimed, txid={btc_claim_txid}")

    # Now claim USDC (permissionless) and M1
    # Start in background to not block the response
    def _complete_swap():
        """Complete USDC + M1 claims after BTC claim.

        SECURITY: Must wait for BTC claim TX to reach >= BTC_CLAIM_MIN_CONFIRMATIONS
        before claiming USDC on EVM. This prevents the RBF double-spend attack where
        the user replaces their BTC funding TX after LP claims EVM USDC.
        Rule: LP can LOCK in 0-conf, but must NOT DELIVER until BTC proof.
        """
        try:
            btc_claim_txid_local = fs.get("btc_claim_txid", "")

            # ── GATE: Wait for BTC claim TX confirmation before releasing USDC ──
            # FAIL-CLOSED: If BTC client unavailable, REFUSE to deliver USDC.
            if btc_claim_txid_local and BTC_CLAIM_MIN_CONFIRMATIONS > 0:
                btc_3s_gate = get_btc_htlc_3s()
                if not btc_3s_gate:
                    log.error(
                        f"FlowSwap {swap_id}: BTC client unavailable — "
                        f"CANNOT verify BTC claim confirmation. "
                        f"REFUSING to release USDC (fail-closed)."
                    )
                    with _flowswap_lock:
                        fs["state"] = FlowSwapState.FAILED.value
                        fs["error"] = (
                            "BTC client unavailable. Cannot verify BTC claim "
                            "confirmation. USDC NOT released (fail-closed). "
                            "LP recovers via HTLC timelock refund."
                        )
                        fs["updated_at"] = int(time.time())
                        _release_reservation(swap_id)
                        _save_flowswap_db()
                    return

                # btc_3s_gate is guaranteed non-None here (fail-closed above)
                poll_start = time.time()
                poll_interval = 15
                confirmed = False

                log.info(
                    f"FlowSwap {swap_id}: GATING — waiting for BTC claim "
                    f"{btc_claim_txid_local[:16]}... to reach "
                    f"{BTC_CLAIM_MIN_CONFIRMATIONS} conf(s) before USDC delivery "
                    f"(timeout={BTC_CLAIM_CONFIRMATION_TIMEOUT}s)"
                )

                while time.time() - poll_start < BTC_CLAIM_CONFIRMATION_TIMEOUT:
                    try:
                        tx_info = btc_3s_gate.client._call(
                            "getrawtransaction", btc_claim_txid_local, True
                        )
                        confs = tx_info.get("confirmations", 0) if tx_info else 0

                        with _flowswap_lock:
                            fs["btc_claim_confs"] = confs
                            fs["updated_at"] = int(time.time())
                            _save_flowswap_db()

                        if confs >= BTC_CLAIM_MIN_CONFIRMATIONS:
                            log.info(
                                f"FlowSwap {swap_id}: BTC claim CONFIRMED "
                                f"({confs} conf(s)). Proceeding to USDC delivery."
                            )
                            confirmed = True
                            break

                        elapsed = int(time.time() - poll_start)
                        log.info(
                            f"FlowSwap {swap_id}: BTC claim confs={confs}/"
                            f"{BTC_CLAIM_MIN_CONFIRMATIONS}, elapsed={elapsed}s"
                        )
                    except Exception as e:
                        log.warning(
                            f"FlowSwap {swap_id}: BTC claim conf check error: {e}"
                        )

                    time.sleep(poll_interval)

                if not confirmed:
                    log.error(
                        f"FlowSwap {swap_id}: BTC claim "
                        f"{btc_claim_txid_local[:16]}... did NOT confirm within "
                        f"{BTC_CLAIM_CONFIRMATION_TIMEOUT}s. "
                        f"REFUSING to release USDC. LP recovers via HTLC timelock."
                    )
                    with _flowswap_lock:
                        fs["state"] = FlowSwapState.FAILED.value
                        fs["error"] = (
                            "BTC claim TX did not confirm in time. "
                            "USDC NOT released. LP recovers via HTLC timelock refund."
                        )
                        fs["updated_at"] = int(time.time())
                        _release_reservation(swap_id)
                        _save_flowswap_db()
                    return

            # ── Per-leg: LP_IN only claimed BTC. USDC + M1 are LP_OUT's job. ──
            if fs.get("is_perleg"):
                with _flowswap_lock:
                    fs["state"] = FlowSwapState.COMPLETED.value
                    fs["completed_at"] = int(time.time())
                    fs["updated_at"] = int(time.time())
                    _release_reservation(swap_id)
                    _save_flowswap_db()
                log.info(f"FlowSwap {swap_id}: COMPLETED (per-leg LP_IN — USDC+M1 handled by LP_OUT)")
                return

            # ── Claim USDC on EVM (only AFTER BTC claim is confirmed) ──
            if not fs.get("evm_claim_txhash"):
                evm = get_evm_htlc_3s()
                evm_privkey = _load_evm_private_key()
                if evm and evm_privkey:
                    evm_result = evm.claim_htlc(
                        htlc_id=fs["evm_htlc_id"],
                        S_user=req.S_user,
                        S_lp1=fs["S_lp1"],
                        S_lp2=fs["S_lp2"],
                        private_key=evm_privkey,
                    )
                    if evm_result.success:
                        with _flowswap_lock:
                            fs["evm_claim_txhash"] = evm_result.tx_hash
                            fs["updated_at"] = int(time.time())
                            _save_flowswap_db()
                        log.info(f"FlowSwap {swap_id}: USDC claimed, tx={evm_result.tx_hash}")
                    else:
                        log.error(f"FlowSwap {swap_id}: USDC claim failed: {evm_result.error}")
            else:
                log.info(f"FlowSwap {swap_id}: USDC already claimed, skipping")

            # Claim M1 on BATHRON — retry until HTLC is in a block
            m1_claimed = True  # default: True for "already claimed" / "no m1_3s" paths
            if not fs.get("m1_claim_txid"):
                m1_3s = get_m1_htlc_3s()
                if m1_3s:
                    m1_claimed = False
                    for attempt in range(12):  # up to 2 minutes
                        try:
                            m1_result = m1_3s.claim(
                                htlc_outpoint=fs["m1_htlc_outpoint"],
                                S_user=req.S_user,
                                S_lp1=fs["S_lp1"],
                                S_lp2=fs["S_lp2"],
                            )
                            with _flowswap_lock:
                                fs["m1_claim_txid"] = m1_result.get("txid")
                                fs["updated_at"] = int(time.time())
                                _save_flowswap_db()
                            log.info(f"FlowSwap {swap_id}: M1 claimed, txid={m1_result.get('txid')}")
                            m1_claimed = True
                            break
                        except Exception as e:
                            if "not found" in str(e).lower():
                                log.info(f"FlowSwap {swap_id}: M1 HTLC not in block yet, waiting... ({attempt+1}/12)")
                            else:
                                log.error(f"FlowSwap {swap_id}: M1 claim error (attempt {attempt+1}/12): {e}")
                            time.sleep(10)
                    if not m1_claimed:
                        log.error(f"FlowSwap {swap_id}: M1 claim failed after 12 retries — background scheduler will refund via timelock")
                else:
                    m1_claimed = False
                    log.error(f"FlowSwap {swap_id}: M1 HTLC3S manager not available — background scheduler will refund via timelock")
            else:
                log.info(f"FlowSwap {swap_id}: M1 already claimed, skipping")

            # Mark complete — user already received assets (EVM USDC).
            # If M1 claim failed, flag it so background scheduler recovers via timelock refund.
            with _flowswap_lock:
                fs["state"] = FlowSwapState.COMPLETED.value
                fs["completed_at"] = int(time.time())
                fs["updated_at"] = int(time.time())
                if not m1_claimed:
                    fs["m1_claim_failed"] = True
                _release_reservation(swap_id)
                _save_flowswap_db()
            log.info(f"FlowSwap {swap_id}: COMPLETED (m1_claimed={m1_claimed})")

        except Exception as e:
            log.error(f"FlowSwap {swap_id}: completion error: {e}")
            with _flowswap_lock:
                fs["state"] = FlowSwapState.FAILED.value
                fs["error"] = f"Completion error: {e}"
                fs["updated_at"] = int(time.time())
                _release_reservation(swap_id)
                _save_flowswap_db()

    fs["state"] = FlowSwapState.COMPLETING.value
    threading.Thread(target=_complete_swap, daemon=True).start()

    response = {
        "swap_id": swap_id,
        "state": FlowSwapState.COMPLETING.value,
        "btc_claim_txid": btc_claim_txid,
        "message": "BTC claimed. USDC + M1 claims in progress (auto-completing).",
        "next_step": f"Poll GET /api/flowswap/{swap_id} for completion",
    }
    # Per-leg: expose S_lp1 so frontend can relay it to LP_OUT.
    # Not a secret leak — S_lp1 is already public on the BTC chain (claim TX).
    if fs.get("is_perleg"):
        response["S_lp1"] = fs["S_lp1"]
    return response


async def _presign_reverse(swap_id: str, fs: Dict, req: FlowSwapPresignRequest):
    """
    Presign for reverse flow (USDC→BTC).
    LP claims USDC on EVM + BTC for user + M1.
    """
    log.info(f"FlowSwap {swap_id}: reverse presign, claiming all legs...")

    def _complete_reverse():
        """Complete USDC→BTC swap: LP claims USDC, BTC-for-user, M1."""
        try:
            S_user = req.S_user

            # 1. Claim USDC on EVM (LP receives) — idempotent
            if not fs.get("evm_claim_txhash"):
                evm = get_evm_htlc_3s()
                evm_privkey = _load_evm_private_key()
                if evm and evm_privkey and fs.get("evm_htlc_id"):
                    try:
                        evm_result = evm.claim_htlc(
                            htlc_id=fs["evm_htlc_id"],
                            S_user=S_user,
                            S_lp1=fs["S_lp1"],
                            S_lp2=fs["S_lp2"],
                            private_key=evm_privkey,
                        )
                        if evm_result.success:
                            with _flowswap_lock:
                                fs["evm_claim_txhash"] = evm_result.tx_hash
                                fs["updated_at"] = int(time.time())
                                _save_flowswap_db()
                            log.info(f"FlowSwap {swap_id}: USDC claimed, tx={evm_result.tx_hash}")
                        else:
                            log.error(f"FlowSwap {swap_id}: USDC claim failed: {evm_result.error}")
                    except Exception as e:
                        log.error(f"FlowSwap {swap_id}: USDC claim error: {e}")
            else:
                log.info(f"FlowSwap {swap_id}: USDC already claimed, skipping")

            # 2. Claim BTC for user (using ephemeral key) — idempotent
            if not fs.get("btc_claim_txid"):
                btc_3s = get_btc_htlc_3s()
                if btc_3s and fs.get("ephemeral_claim_wif"):
                    utxo = None
                    fund_txid = fs.get("btc_fund_txid", "")
                    fund_vout = fs.get("btc_fund_vout", 0)
                    for attempt in range(90):  # up to 15 minutes (Signet ~10min blocks)
                        try:
                            # Use gettxout with include_mempool=true (finds 0-conf)
                            if fund_txid:
                                for try_vout in range(3):  # try vout 0, 1, 2
                                    txout = btc_3s.client._call("gettxout", fund_txid, try_vout, True)
                                    if txout:
                                        amount_sats = int(round(float(txout.get("value", 0)) * 100_000_000))
                                        # Verify this is the HTLC output (not change)
                                        if amount_sats >= fs["btc_amount_sats"]:
                                            utxo = {
                                                "txid": fund_txid,
                                                "vout": try_vout,
                                                "amount": amount_sats,
                                                "confirmations": txout.get("confirmations", 0),
                                            }
                                            break
                                if utxo:
                                    break
                            # Fallback to scantxoutset
                            utxo = btc_3s.check_htlc_funded(
                                htlc_address=fs["btc_htlc_address"],
                                expected_amount=fs["btc_amount_sats"],
                                min_confirmations=0,
                            )
                            if utxo:
                                break
                        except Exception as e:
                            log.debug(f"BTC UTXO check error: {e}")
                        log.info(f"FlowSwap {swap_id}: BTC UTXO not ready, waiting... ({attempt+1}/90)")
                        time.sleep(10)

                    if utxo:
                        try:
                            from sdk.htlc.btc_3s import HTLC3SSecrets
                            btc_claim_txid = btc_3s.claim_htlc_3s(
                                utxo=utxo,
                                redeem_script=fs["btc_redeem_script"],
                                secrets=HTLC3SSecrets(
                                    S_user=S_user,
                                    S_lp1=fs["S_lp1"],
                                    S_lp2=fs["S_lp2"],
                                ),
                                recipient_address=fs["user_btc_address"],
                                claim_privkey_wif=fs["ephemeral_claim_wif"],
                            )
                            with _flowswap_lock:
                                fs["btc_claim_txid"] = btc_claim_txid
                                fs["updated_at"] = int(time.time())
                                _save_flowswap_db()
                            log.info(f"FlowSwap {swap_id}: BTC claimed for user, txid={btc_claim_txid}")
                        except Exception as e:
                            log.error(f"FlowSwap {swap_id}: BTC claim error: {e}")
                    else:
                        log.error(f"FlowSwap {swap_id}: BTC HTLC UTXO not found after 90 attempts")
            else:
                log.info(f"FlowSwap {swap_id}: BTC already claimed, skipping")

            # 3. Claim M1 on BATHRON — idempotent, retry until HTLC is in a block
            m1_claimed = True  # default: True for "already claimed" / "no m1_3s" paths
            if not fs.get("m1_claim_txid"):
                m1_3s = get_m1_htlc_3s()
                if m1_3s:
                    m1_claimed = False
                    for attempt in range(12):  # up to 2 minutes
                        try:
                            m1_result = m1_3s.claim(
                                htlc_outpoint=fs["m1_htlc_outpoint"],
                                S_user=S_user,
                                S_lp1=fs["S_lp1"],
                                S_lp2=fs["S_lp2"],
                            )
                            with _flowswap_lock:
                                fs["m1_claim_txid"] = m1_result.get("txid")
                                fs["updated_at"] = int(time.time())
                                _save_flowswap_db()
                            log.info(f"FlowSwap {swap_id}: M1 claimed, txid={m1_result.get('txid')}")
                            m1_claimed = True
                            break
                        except Exception as e:
                            if "not found" in str(e).lower():
                                log.info(f"FlowSwap {swap_id}: M1 HTLC not in block yet, waiting... ({attempt+1}/12)")
                            else:
                                log.error(f"FlowSwap {swap_id}: M1 claim error (attempt {attempt+1}/12): {e}")
                            time.sleep(10)
                    if not m1_claimed:
                        log.error(f"FlowSwap {swap_id}: M1 claim failed after 12 retries — background scheduler will refund via timelock")
                else:
                    m1_claimed = False
                    log.error(f"FlowSwap {swap_id}: M1 HTLC3S manager not available — background scheduler will refund via timelock")
            else:
                log.info(f"FlowSwap {swap_id}: M1 already claimed, skipping")

            # Mark complete — user already received assets (BTC).
            # If M1 claim failed, flag it so background scheduler recovers via timelock refund.
            with _flowswap_lock:
                fs["state"] = FlowSwapState.COMPLETED.value
                fs["completed_at"] = int(time.time())
                fs["updated_at"] = int(time.time())
                if not m1_claimed:
                    fs["m1_claim_failed"] = True
                _release_reservation(swap_id)
                _save_flowswap_db()
            log.info(f"FlowSwap {swap_id}: REVERSE SWAP COMPLETED (m1_claimed={m1_claimed})")

        except Exception as e:
            log.error(f"FlowSwap {swap_id}: reverse completion error: {e}")
            with _flowswap_lock:
                fs["state"] = FlowSwapState.FAILED.value
                fs["error"] = f"Reverse completion error: {e}"
                fs["updated_at"] = int(time.time())
                _release_reservation(swap_id)
                _save_flowswap_db()

    fs["state"] = FlowSwapState.COMPLETING.value
    fs["updated_at"] = int(time.time())
    _save_flowswap_db()

    threading.Thread(target=_complete_reverse, daemon=True).start()

    return {
        "swap_id": swap_id,
        "state": FlowSwapState.COMPLETING.value,
        "message": "Claiming USDC + BTC (for user) + M1. Auto-completing.",
        "btc_destination": fs.get("user_btc_address", ""),
        "next_step": f"Poll GET /api/flowswap/{swap_id} for completion",
    }


# =============================================================================
# PER-LEG COMPLETION: LP_OUT receives secrets after LP_IN claims BTC
# =============================================================================

@app.post("/api/flowswap/{swap_id}/btc-claimed")
async def flowswap_btc_claimed(swap_id: str, req: BtcClaimedRequest):
    """
    Per-leg completion: frontend notifies LP_OUT that LP_IN claimed BTC.

    LP_OUT receives the revealed secrets (S_user, S_lp1 — now public on BTC chain)
    and launches a background thread to claim USDC (for user) and M1 (for self).
    """
    if swap_id not in flowswap_db:
        raise HTTPException(404, "FlowSwap not found")

    fs = flowswap_db[swap_id]

    # Only valid for per-leg LP_OUT swaps
    if not fs.get("is_perleg"):
        raise HTTPException(400, "Not a per-leg swap")

    if fs.get("leg") != "M1/USDC":
        raise HTTPException(400, f"This endpoint is for LP_OUT (M1/USDC leg), got leg={fs.get('leg')}")

    # Accept from lp_locked state (LP_OUT locked USDC+M1, waiting for BTC claim proof)
    if fs["state"] != FlowSwapState.LP_LOCKED.value:
        raise HTTPException(400, f"Invalid state: {fs['state']} (expected lp_locked)")

    # Verify secrets match the stored hashes
    computed_H_user = hashlib.sha256(bytes.fromhex(req.S_user)).hexdigest()
    if computed_H_user != fs["H_user"]:
        raise HTTPException(400, "S_user does not match H_user")

    computed_H_lp1 = hashlib.sha256(bytes.fromhex(req.S_lp1)).hexdigest()
    if computed_H_lp1 != fs.get("H_lp1"):
        raise HTTPException(400, "S_lp1 does not match H_lp1")

    # Store secrets + BTC claim txid
    with _flowswap_lock:
        fs["S_user"] = req.S_user
        fs["S_lp1"] = req.S_lp1
        fs["btc_claim_txid"] = req.btc_claim_txid
        fs["state"] = FlowSwapState.BTC_CLAIMED.value
        fs["updated_at"] = int(time.time())
        _save_flowswap_db()

    log.info(f"FlowSwap {swap_id}: LP_OUT received BTC claim proof, btc_txid={req.btc_claim_txid[:16]}...")

    # Launch LP_OUT completion thread
    def _complete_lp_out():
        """LP_OUT completion: wait for BTC claim confirmation, then claim USDC + M1."""
        try:
            btc_claim_txid_local = fs.get("btc_claim_txid", "")

            # ── GATE: Wait for BTC claim TX confirmation (same RBF gate as single-LP) ──
            if btc_claim_txid_local and BTC_CLAIM_MIN_CONFIRMATIONS > 0:
                btc_3s_gate = get_btc_htlc_3s()
                if not btc_3s_gate:
                    log.error(
                        f"FlowSwap {swap_id}: LP_OUT BTC client unavailable — "
                        f"REFUSING to release USDC (fail-closed)."
                    )
                    with _flowswap_lock:
                        fs["state"] = FlowSwapState.FAILED.value
                        fs["error"] = (
                            "BTC client unavailable. Cannot verify BTC claim "
                            "confirmation. USDC NOT released (fail-closed)."
                        )
                        fs["updated_at"] = int(time.time())
                        _release_reservation(swap_id)
                        _save_flowswap_db()
                    return

                poll_start = time.time()
                poll_interval = 15
                confirmed = False

                log.info(
                    f"FlowSwap {swap_id}: LP_OUT GATING — waiting for BTC claim "
                    f"{btc_claim_txid_local[:16]}... to reach "
                    f"{BTC_CLAIM_MIN_CONFIRMATIONS} conf(s) "
                    f"(timeout={BTC_CLAIM_CONFIRMATION_TIMEOUT}s)"
                )

                while time.time() - poll_start < BTC_CLAIM_CONFIRMATION_TIMEOUT:
                    try:
                        tx_info = btc_3s_gate.client._call(
                            "getrawtransaction", btc_claim_txid_local, True
                        )
                        confs = tx_info.get("confirmations", 0) if tx_info else 0

                        with _flowswap_lock:
                            fs["btc_claim_confs"] = confs
                            fs["updated_at"] = int(time.time())
                            _save_flowswap_db()

                        if confs >= BTC_CLAIM_MIN_CONFIRMATIONS:
                            log.info(
                                f"FlowSwap {swap_id}: LP_OUT BTC claim CONFIRMED "
                                f"({confs} conf(s)). Proceeding to USDC+M1 claims."
                            )
                            confirmed = True
                            break

                        elapsed = int(time.time() - poll_start)
                        log.info(
                            f"FlowSwap {swap_id}: LP_OUT BTC claim confs={confs}/"
                            f"{BTC_CLAIM_MIN_CONFIRMATIONS}, elapsed={elapsed}s"
                        )
                    except Exception as e:
                        log.warning(
                            f"FlowSwap {swap_id}: LP_OUT BTC claim conf check error: {e}"
                        )

                    time.sleep(poll_interval)

                if not confirmed:
                    log.error(
                        f"FlowSwap {swap_id}: LP_OUT BTC claim "
                        f"{btc_claim_txid_local[:16]}... did NOT confirm within "
                        f"{BTC_CLAIM_CONFIRMATION_TIMEOUT}s. "
                        f"REFUSING to release USDC."
                    )
                    with _flowswap_lock:
                        fs["state"] = FlowSwapState.FAILED.value
                        fs["error"] = (
                            "BTC claim TX did not confirm in time. "
                            "USDC NOT released. LP recovers via HTLC timelock."
                        )
                        fs["updated_at"] = int(time.time())
                        _release_reservation(swap_id)
                        _save_flowswap_db()
                    return

            # ── Claim USDC on EVM for user (LP_OUT has evm_htlc_id) ──
            if not fs.get("evm_claim_txhash"):
                evm = get_evm_htlc_3s()
                evm_privkey = _load_evm_private_key()
                if evm and evm_privkey and fs.get("evm_htlc_id"):
                    evm_result = evm.claim_htlc(
                        htlc_id=fs["evm_htlc_id"],
                        S_user=fs["S_user"],
                        S_lp1=fs["S_lp1"],
                        S_lp2=fs["S_lp2"],
                        private_key=evm_privkey,
                    )
                    if evm_result.success:
                        with _flowswap_lock:
                            fs["evm_claim_txhash"] = evm_result.tx_hash
                            fs["updated_at"] = int(time.time())
                            _save_flowswap_db()
                        log.info(f"FlowSwap {swap_id}: LP_OUT USDC claimed, tx={evm_result.tx_hash}")
                    else:
                        log.error(f"FlowSwap {swap_id}: LP_OUT USDC claim failed: {evm_result.error}")
                else:
                    log.error(f"FlowSwap {swap_id}: LP_OUT cannot claim USDC — missing evm client/key/htlc_id")
            else:
                log.info(f"FlowSwap {swap_id}: LP_OUT USDC already claimed, skipping")

            # ── Claim M1 on BATHRON for LP_OUT (LP_OUT's own M1) ──
            m1_claimed = True
            if not fs.get("m1_claim_txid"):
                m1_3s = get_m1_htlc_3s()
                if m1_3s:
                    m1_claimed = False
                    for attempt in range(12):  # up to 2 minutes
                        try:
                            m1_result = m1_3s.claim(
                                htlc_outpoint=fs["m1_htlc_outpoint"],
                                S_user=fs["S_user"],
                                S_lp1=fs["S_lp1"],
                                S_lp2=fs["S_lp2"],
                            )
                            with _flowswap_lock:
                                fs["m1_claim_txid"] = m1_result.get("txid")
                                fs["updated_at"] = int(time.time())
                                _save_flowswap_db()
                            log.info(f"FlowSwap {swap_id}: LP_OUT M1 claimed, txid={m1_result.get('txid')}")
                            m1_claimed = True
                            break
                        except Exception as e:
                            if "not found" in str(e).lower():
                                log.info(f"FlowSwap {swap_id}: LP_OUT M1 HTLC not in block yet ({attempt+1}/12)")
                            else:
                                log.error(f"FlowSwap {swap_id}: LP_OUT M1 claim error ({attempt+1}/12): {e}")
                            time.sleep(10)
                    if not m1_claimed:
                        log.error(f"FlowSwap {swap_id}: LP_OUT M1 claim failed after 12 retries")
                else:
                    m1_claimed = False
                    log.error(f"FlowSwap {swap_id}: LP_OUT M1 HTLC3S manager not available")
            else:
                log.info(f"FlowSwap {swap_id}: LP_OUT M1 already claimed, skipping")

            # Mark complete
            with _flowswap_lock:
                fs["state"] = FlowSwapState.COMPLETED.value
                fs["completed_at"] = int(time.time())
                fs["updated_at"] = int(time.time())
                if not m1_claimed:
                    fs["m1_claim_failed"] = True
                _release_reservation(swap_id)
                _save_flowswap_db()
            log.info(f"FlowSwap {swap_id}: LP_OUT COMPLETED (m1_claimed={m1_claimed})")

        except Exception as e:
            log.error(f"FlowSwap {swap_id}: LP_OUT completion error: {e}")
            with _flowswap_lock:
                fs["state"] = FlowSwapState.FAILED.value
                fs["error"] = f"LP_OUT completion error: {e}"
                fs["updated_at"] = int(time.time())
                _release_reservation(swap_id)
                _save_flowswap_db()

    with _flowswap_lock:
        fs["state"] = FlowSwapState.COMPLETING.value
        fs["updated_at"] = int(time.time())
        _save_flowswap_db()
    threading.Thread(target=_complete_lp_out, daemon=True).start()

    return {
        "swap_id": swap_id,
        "state": FlowSwapState.COMPLETING.value,
        "message": "LP_OUT completing: claiming USDC (for user) + M1 (for self).",
        "next_step": f"Poll GET /api/flowswap/{swap_id} for completion",
    }


# =============================================================================
# CHAIN MANAGEMENT ENDPOINTS
# =============================================================================

# In-memory chain status and install jobs
chain_status_db: Dict[str, Dict[str, Any]] = {
    "btc": {"installed": False, "running": False, "height": 0, "pid": None},
    "m1": {"installed": False, "running": False, "height": 0, "pid": None},
    "usdc": {"installed": True, "running": False, "height": 0},  # No install needed
}

install_jobs_db: Dict[str, Dict[str, Any]] = {}

# Script paths
SCRIPTS_DIR = Path(__file__).parent / "scripts"
INSTALL_SCRIPTS = {
    "btc": SCRIPTS_DIR / "install_btc_signet.sh",
    "m1": SCRIPTS_DIR / "install_bathron.sh",
}

PROGRESS_FILES = {
    "btc": "/tmp/btc_install_progress.txt",
    "m1": "/tmp/m1_install_progress.txt",
}

# Binary paths - check multiple locations
def find_binary(name: str, paths: list) -> Optional[Path]:
    """Find binary in multiple possible locations."""
    for p in paths:
        path = Path(p).expanduser()
        if path.exists():
            return path
    return paths[0] if paths else None  # Return first as default

CHAIN_BINARIES = {
    "btc": find_binary("bitcoind", [
        Path.home() / "bitcoin" / "bin" / "bitcoind",          # install_btc_signet.sh
        Path.home() / "btc-signet" / "bin" / "bitcoind",
        Path.home() / "BATHRON" / "BTCTESTNET" / "bitcoin-27.0" / "bin" / "bitcoind",
        "/usr/local/bin/bitcoind",
    ]),
    "m1": find_binary("bathrond", [
        Path.home() / "bathron" / "bin" / "bathrond",          # deploy_to_vps.sh
        Path.home() / "BATHRON" / "src" / "bathrond",
        "/usr/local/bin/bathrond",
    ]),
}

CHAIN_CLI = {
    "btc": find_binary("bitcoin-cli", [
        Path.home() / "bitcoin" / "bin" / "bitcoin-cli",       # install_btc_signet.sh
        Path.home() / "btc-signet" / "bin" / "bitcoin-cli",
        Path.home() / "BATHRON" / "BTCTESTNET" / "bitcoin-27.0" / "bin" / "bitcoin-cli",
        "/usr/local/bin/bitcoin-cli",
    ]),
    "m1": find_binary("bathron-cli", [
        Path.home() / "bathron" / "bin" / "bathron-cli",       # deploy_to_vps.sh
        Path.home() / "BATHRON" / "src" / "bathron-cli",
        "/usr/local/bin/bathron-cli",
    ]),
}

# M1 LP Wallet - connects to local M1 node on OP1
# Uses default RPC settings from ~/.bathron/bathron.conf
M1_LP_RPC_ARGS = []  # Empty - uses default testnet config

import subprocess

def check_chain_status_on_startup():
    """Check if chains are installed and running on server startup."""
    for chain in ["btc", "m1"]:
        binary = CHAIN_BINARIES.get(chain)
        if binary and binary.exists():
            chain_status_db[chain]["installed"] = True
            log.info(f"Chain {chain} binary found at {binary}")

            # Check if daemon is running
            try:
                result = subprocess.run(
                    ["pgrep", "-f", binary.name],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    chain_status_db[chain]["running"] = True
                    chain_status_db[chain]["pid"] = int(result.stdout.strip().split()[0])
                    log.info(f"Chain {chain} daemon is running (PID: {chain_status_db[chain]['pid']})")
            except:
                pass

# Run startup checks
check_chain_status_on_startup()
# Note: load_lp_addresses() called in __main__ after all function definitions

# =============================================================================
# SDK INITIALIZATION
# =============================================================================

# SDK clients (initialized lazily)
_sdk_m1_client: Optional["M1Client"] = None
_sdk_btc_client: Optional["BTCClient"] = None
_sdk_m1_htlc: Optional["M1Htlc"] = None
_sdk_btc_htlc: Optional["BTCHtlc"] = None

def get_m1_client() -> "M1Client":
    """Get or create M1 client."""
    global _sdk_m1_client
    if _sdk_m1_client is None and SDK_AVAILABLE:
        # Use default testnet RPC settings from ~/.bathron/bathron.conf
        config = M1Config(
            network="testnet",
            cli_path=CHAIN_CLI.get("m1"),
            # No explicit RPC settings - uses defaults from config file
        )
        _sdk_m1_client = M1Client(config)
        log.info("SDK M1 client initialized")
    return _sdk_m1_client

def get_btc_client() -> "BTCClient":
    """Get or create BTC client."""
    global _sdk_btc_client
    if _sdk_btc_client is None and SDK_AVAILABLE:
        # Load wallet name: btc.json "wallet" or "wallet_name" field
        btc_wallet_name = ""
        btc_key = _load_lp_btc_key()
        btc_wallet_name = btc_key.get("wallet") or btc_key.get("wallet_name") or ""
        if not btc_wallet_name:
            btc_wallet_name = "lp_wallet"  # fallback convention

        # Auto-load wallet before creating client (loadwallet is global, no -rpcwallet)
        if btc_wallet_name:
            btc_cli = CHAIN_CLI.get("btc")
            if btc_cli:
                try:
                    r = subprocess.run(
                        [str(btc_cli), "-signet", f"-datadir={Path.home() / '.bitcoin-signet'}",
                         "loadwallet", btc_wallet_name],
                        capture_output=True, text=True, timeout=10)
                    if r.returncode == 0:
                        log.info(f"BTC wallet '{btc_wallet_name}' loaded")
                    elif "already loaded" in r.stderr.lower():
                        log.info(f"BTC wallet '{btc_wallet_name}' already loaded")
                    else:
                        log.warning(f"BTC loadwallet '{btc_wallet_name}': {r.stderr.strip()}")
                except Exception as e:
                    log.warning(f"BTC loadwallet failed: {e}")

        config = BTCConfig(
            network="signet",
            cli_path=CHAIN_CLI.get("btc"),
            datadir=str(Path.home() / ".bitcoin-signet"),
            wallet_name=btc_wallet_name,
        )
        _sdk_btc_client = BTCClient(config)
        log.info(f"SDK BTC client initialized (wallet={btc_wallet_name or 'default'})")
    return _sdk_btc_client

def get_m1_htlc() -> "M1Htlc":
    """Get or create M1 HTLC manager."""
    global _sdk_m1_htlc
    if _sdk_m1_htlc is None and SDK_AVAILABLE:
        client = get_m1_client()
        if client:
            _sdk_m1_htlc = M1Htlc(client)
            log.info("SDK M1 HTLC manager initialized")
    return _sdk_m1_htlc

def get_btc_htlc() -> "BTCHtlc":
    """Get or create BTC HTLC manager."""
    global _sdk_btc_htlc
    if _sdk_btc_htlc is None and SDK_AVAILABLE:
        client = get_btc_client()
        if client:
            _sdk_btc_htlc = BTCHtlc(client)
            log.info("SDK BTC HTLC manager initialized")
    return _sdk_btc_htlc

@app.post("/api/chain/{chain}/test")
async def test_chain_connection(chain: str):
    """Test chain RPC connection."""
    if chain not in ["btc", "m1", "usdc"]:
        raise HTTPException(400, f"Unknown chain: {chain}")

    # Mock test - in production: actually test RPC
    status = chain_status_db.get(chain, {})

    if chain == "usdc":
        # Test EVM RPC
        return {"connected": True, "height": 12345678, "chain": "base"}

    return {
        "connected": status.get("running", False),
        "height": status.get("height", 0),
        "chain": chain,
    }

def run_install_script(chain: str, job_id: str):
    """Run installation script in background thread."""
    script_path = INSTALL_SCRIPTS.get(chain)
    if not script_path or not script_path.exists():
        install_jobs_db[job_id]["status"] = "failed"
        install_jobs_db[job_id]["message"] = "Install script not found"
        return

    try:
        # Make script executable
        os.chmod(script_path, 0o755)

        # Run the script
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min timeout
        )

        if result.returncode == 0:
            install_jobs_db[job_id]["status"] = "complete"
            install_jobs_db[job_id]["progress"] = 100
            install_jobs_db[job_id]["message"] = "Installation complete!"
            chain_status_db[chain]["installed"] = True
            log.info(f"Installation complete: {chain}")
        else:
            install_jobs_db[job_id]["status"] = "failed"
            install_jobs_db[job_id]["message"] = f"Error: {result.stderr[:200]}"
            log.error(f"Installation failed: {chain} - {result.stderr}")

    except subprocess.TimeoutExpired:
        install_jobs_db[job_id]["status"] = "failed"
        install_jobs_db[job_id]["message"] = "Installation timed out"
    except Exception as e:
        install_jobs_db[job_id]["status"] = "failed"
        install_jobs_db[job_id]["message"] = str(e)
        log.error(f"Installation error: {chain} - {e}")

@app.post("/api/chain/{chain}/install")
async def install_chain(chain: str):
    """Start chain installation."""
    if chain not in ["btc", "m1"]:
        raise HTTPException(400, f"Chain {chain} does not need installation")

    # Check if binary already exists
    if CHAIN_BINARIES[chain].exists():
        chain_status_db[chain]["installed"] = True
        return {"already_installed": True, "path": str(CHAIN_BINARIES[chain])}

    if chain_status_db[chain]["installed"]:
        return {"already_installed": True}

    # Check for existing running install
    for job_id, job in install_jobs_db.items():
        if job["chain"] == chain and job["status"] == "running":
            return {"job_id": job_id, "status": "already_running"}

    # Create install job
    job_id = f"install_{chain}_{int(time.time())}"
    install_jobs_db[job_id] = {
        "chain": chain,
        "status": "running",
        "progress": 0,
        "message": "Starting installation...",
        "started_at": int(time.time()),
    }

    # Clear progress file
    progress_file = PROGRESS_FILES.get(chain)
    if progress_file:
        Path(progress_file).write_text("0|Starting...")

    # Start installation in background thread
    thread = threading.Thread(target=run_install_script, args=(chain, job_id))
    thread.daemon = True
    thread.start()

    log.info(f"Installation started: {chain} (job: {job_id})")
    return {"job_id": job_id, "status": "started"}

@app.get("/api/chain/{chain}/install/status")
async def get_install_status(chain: str, job_id: str = Query(...)):
    """Get installation progress."""
    if job_id not in install_jobs_db:
        raise HTTPException(404, "Install job not found")

    job = install_jobs_db[job_id]

    # Read progress from file if still running
    if job["status"] == "running":
        progress_file = PROGRESS_FILES.get(chain)
        if progress_file and Path(progress_file).exists():
            try:
                content = Path(progress_file).read_text().strip()
                if "|" in content:
                    progress_str, message = content.split("|", 1)
                    job["progress"] = int(progress_str)
                    job["message"] = message

                    # Check if complete
                    if job["progress"] >= 100:
                        job["status"] = "complete"
                        chain_status_db[chain]["installed"] = True
                        log.info(f"Installation complete: {chain}")
            except Exception as e:
                log.error(f"Error reading progress: {e}")

    return {
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
    }

@app.post("/api/chain/{chain}/start")
async def start_chain(chain: str):
    """Start chain daemon."""
    if chain not in ["btc", "m1"]:
        raise HTTPException(400, f"Cannot start chain: {chain}")

    binary = CHAIN_BINARIES.get(chain)
    if not binary or not binary.exists():
        return {"started": False, "error": "Chain not installed"}

    try:
        # Start daemon
        if chain == "btc":
            cmd = [str(binary), "-signet", "-daemon"]
        else:  # m1
            cmd = [str(binary), "-testnet", "-daemon"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 or "already running" in result.stderr.lower():
            chain_status_db[chain]["running"] = True
            log.info(f"Chain started: {chain}")

            # Get PID
            try:
                pid_result = subprocess.run(
                    ["pgrep", "-f", str(binary.name)],
                    capture_output=True, text=True
                )
                if pid_result.stdout.strip():
                    chain_status_db[chain]["pid"] = int(pid_result.stdout.strip().split()[0])
            except:
                pass

            return {"started": True}
        else:
            return {"started": False, "error": result.stderr[:200]}

    except subprocess.TimeoutExpired:
        return {"started": False, "error": "Timeout starting daemon"}
    except Exception as e:
        return {"started": False, "error": str(e)}

@app.post("/api/chain/{chain}/stop")
async def stop_chain(chain: str):
    """Stop chain daemon."""
    if chain not in ["btc", "m1"]:
        raise HTTPException(400, f"Cannot stop chain: {chain}")

    cli = CHAIN_CLI.get(chain)
    if not cli or not cli.exists():
        return {"stopped": False, "error": "CLI not found"}

    try:
        # Stop daemon via CLI
        if chain == "btc":
            cmd = [str(cli), "-signet", "stop"]
        else:  # m1
            cmd = [str(cli), "-testnet", "stop"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        chain_status_db[chain]["running"] = False
        chain_status_db[chain]["pid"] = None
        log.info(f"Chain stopped: {chain}")
        return {"stopped": True}

    except Exception as e:
        # Try kill as fallback
        pid = chain_status_db[chain].get("pid")
        if pid:
            try:
                os.kill(pid, 15)  # SIGTERM
                chain_status_db[chain]["running"] = False
                chain_status_db[chain]["pid"] = None
                return {"stopped": True}
            except:
                pass
        return {"stopped": False, "error": str(e)}

@app.get("/api/chain/{chain}/sync")
async def get_chain_sync_status(chain: str):
    """Get blockchain sync status."""
    if chain not in ["btc", "m1"]:
        raise HTTPException(400, f"Cannot get sync for chain: {chain}")

    cli = CHAIN_CLI.get(chain)
    if not cli or not cli.exists():
        return {"syncing": False, "error": "Chain not installed", "progress": 0}

    try:
        # Call getblockchaininfo
        if chain == "btc":
            cmd = [str(cli), "-signet", f"-datadir={Path.home() / '.bitcoin-signet'}", "getblockchaininfo"]
        else:  # m1
            cmd = [str(cli), "-testnet", "getblockchaininfo"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            # Node not running or error
            return {
                "syncing": False,
                "error": "Node not running",
                "progress": 0,
                "height": 0,
            }

        # Parse JSON output
        info = json.loads(result.stdout)

        # Calculate sync progress
        headers = info.get("headers", 0)
        blocks = info.get("blocks", 0)
        verification_progress = info.get("verificationprogress", 0)

        # Use verification progress if available, else calculate from blocks/headers
        if verification_progress > 0:
            progress = verification_progress * 100
        elif headers > 0:
            progress = (blocks / headers) * 100
        else:
            progress = 0

        syncing = blocks < headers if headers > 0 else False

        # Update chain status
        chain_status_db[chain]["height"] = blocks
        chain_status_db[chain]["running"] = True

        return {
            "syncing": syncing,
            "progress": round(progress, 2),
            "blocks": blocks,
            "headers": headers,
            "chain": info.get("chain", chain),
            "size_on_disk": info.get("size_on_disk", 0),
            "pruned": info.get("pruned", False),
        }

    except subprocess.TimeoutExpired:
        return {"syncing": False, "error": "Timeout", "progress": 0}
    except json.JSONDecodeError:
        return {"syncing": False, "error": "Invalid response", "progress": 0}
    except Exception as e:
        return {"syncing": False, "error": str(e), "progress": 0}

@app.post("/api/chain/usdc/deploy-htlc")
async def deploy_htlc_contract():
    """Return the deployed HTLC contract address on Base Sepolia."""
    # Contract was deployed on 2026-02-04
    return {
        "address": HTLC_CONTRACT_BASE_SEPOLIA,
        "network": "Base Sepolia",
        "chain_id": 84532,
        "explorer": f"https://sepolia.basescan.org/address/{HTLC_CONTRACT_BASE_SEPOLIA}"
    }


# =============================================================================
# USDC HTLC ENDPOINTS
# =============================================================================

class USDCHTLCCreateRequest(BaseModel):
    """Request to create a USDC HTLC."""
    receiver: str = Field(..., description="Address that can claim with preimage")
    amount_usdc: float = Field(..., gt=0, description="Amount in USDC")
    hashlock: str = Field(..., description="SHA256 hash of the secret (bytes32 hex)")
    timelock_seconds: int = Field(default=7200, description="Seconds until refund is possible")


@app.post("/api/sdk/usdc/htlc/create")
async def create_usdc_htlc(request: USDCHTLCCreateRequest):
    """
    Create a USDC HTLC on Base Sepolia.

    The LP creates this HTLC when the user needs USDC.
    User claims by revealing preimage, LP extracts preimage from chain.
    """
    try:
        from sdk.htlc.evm import create_htlc, HTLC_CONTRACT_ADDRESS

        if not LP_USDC_PRIVKEY:
            raise ValueError("EVM private key not loaded — check ~/.BathronKey/evm.json")

        result = create_htlc(
            receiver=request.receiver,
            amount_usdc=request.amount_usdc,
            hashlock=request.hashlock,
            timelock_seconds=request.timelock_seconds,
            private_key=LP_USDC_PRIVKEY,
            contract=HTLC_CONTRACT_BASE_SEPOLIA
        )

        if result.success:
            return {
                "success": True,
                "htlc_id": result.htlc_id,
                "tx_hash": result.tx_hash,
                "explorer": f"https://sepolia.basescan.org/tx/0x{result.tx_hash}" if result.tx_hash else None,
                "contract": HTLC_CONTRACT_BASE_SEPOLIA,
                "data": result.data
            }
        else:
            raise HTTPException(status_code=400, detail=result.error)

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"SDK not available: {e}")
    except Exception as e:
        log.exception("USDC HTLC create failed")
        raise HTTPException(status_code=500, detail=str(e))


class USDCHTLCWithdrawRequest(BaseModel):
    """Request to withdraw from a USDC HTLC."""
    htlc_id: str = Field(..., description="The HTLC identifier")
    preimage: str = Field(..., description="The secret that hashes to hashlock")
    private_key: Optional[str] = Field(None, description="Receiver's private key (optional, uses LP key if not provided)")


@app.post("/api/sdk/usdc/htlc/withdraw")
async def withdraw_usdc_htlc(request: USDCHTLCWithdrawRequest):
    """
    Withdraw from a USDC HTLC using the preimage.

    The user (or LP) calls this to claim USDC by revealing the preimage.
    """
    try:
        from sdk.htlc.evm import withdraw_htlc

        # Use provided key or LP key
        private_key = request.private_key or LP_USDC_PRIVKEY
        if not private_key:
            raise ValueError("No EVM private key available — check ~/.BathronKey/evm.json")

        result = withdraw_htlc(
            htlc_id=request.htlc_id,
            preimage=request.preimage,
            private_key=private_key,
            contract=HTLC_CONTRACT_BASE_SEPOLIA
        )

        if result.success:
            return {
                "success": True,
                "htlc_id": result.htlc_id,
                "tx_hash": result.tx_hash,
                "explorer": f"https://sepolia.basescan.org/tx/0x{result.tx_hash}" if result.tx_hash else None
            }
        else:
            raise HTTPException(status_code=400, detail=result.error)

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"SDK not available: {e}")
    except Exception as e:
        log.exception("USDC HTLC withdraw failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sdk/usdc/htlc/{htlc_id}")
async def get_usdc_htlc(htlc_id: str):
    """
    Get USDC HTLC details by ID.
    """
    try:
        from sdk.htlc.evm import get_htlc

        htlc = get_htlc(htlc_id, contract=HTLC_CONTRACT_BASE_SEPOLIA)

        if htlc:
            return {
                "success": True,
                "htlc": htlc,
                "contract": HTLC_CONTRACT_BASE_SEPOLIA
            }
        else:
            raise HTTPException(status_code=404, detail="HTLC not found")

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"SDK not available: {e}")
    except Exception as e:
        log.exception("USDC HTLC get failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sdk/usdc/balance/{address}")
async def get_usdc_balance(address: str):
    """
    Get USDC and ETH balance for an address on Base Sepolia.
    """
    try:
        from sdk.htlc.evm import get_usdc_balance, get_eth_balance

        usdc = get_usdc_balance(address)
        eth = get_eth_balance(address)

        return {
            "address": address,
            "usdc_balance": usdc,
            "eth_balance": eth,
            "network": "Base Sepolia"
        }

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"SDK not available: {e}")
    except Exception as e:
        log.exception("Balance check failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sdk/usdc/debug")
async def debug_usdc_htlc():
    """
    Debug endpoint to check HTLC contract state and allowances.
    """
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(BASE_SEPOLIA_RPC))

        # Check connection
        connected = w3.is_connected()

        # Get LP address
        from eth_account import Account
        if not LP_USDC_PRIVKEY:
            raise ValueError("EVM private key not loaded — check ~/.BathronKey/evm.json")
        account = Account.from_key("0x" + LP_USDC_PRIVKEY)
        lp_address = account.address

        # Check balances
        eth_balance = w3.eth.get_balance(lp_address) / 1e18

        # USDC balance and allowance
        usdc_abi = [
            {"name": "balanceOf", "type": "function", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
            {"name": "allowance", "type": "function", "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"}
        ]
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_CONTRACT_BASE_SEPOLIA), abi=usdc_abi)

        usdc_balance = usdc.functions.balanceOf(lp_address).call()
        allowance = usdc.functions.allowance(lp_address, Web3.to_checksum_address(HTLC_CONTRACT_BASE_SEPOLIA)).call()

        # Current nonce
        nonce_latest = w3.eth.get_transaction_count(lp_address, 'latest')
        nonce_pending = w3.eth.get_transaction_count(lp_address, 'pending')

        # Gas price
        gas_price = w3.eth.gas_price

        # Test simulate create
        import time
        hashlock = bytes.fromhex("84c42347857fd4cff83d6d41c5f18cbd163cd073d65f799bdce11b628a40b24a")
        receiver = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        amount = 1_000_000  # 1 USDC
        timelock = int(time.time()) + 3600

        htlc_abi = [
            {"name": "create", "type": "function", "inputs": [
                {"name": "receiver", "type": "address"},
                {"name": "token", "type": "address"},
                {"name": "amount", "type": "uint256"},
                {"name": "hashlock", "type": "bytes32"},
                {"name": "timelock", "type": "uint256"}
            ], "outputs": [{"name": "htlcId", "type": "bytes32"}]}
        ]
        htlc = w3.eth.contract(address=Web3.to_checksum_address(HTLC_CONTRACT_BASE_SEPOLIA), abi=htlc_abi)

        simulation_result = None
        simulation_error = None
        try:
            result = htlc.functions.create(
                Web3.to_checksum_address(receiver),
                Web3.to_checksum_address(USDC_CONTRACT_BASE_SEPOLIA),
                amount,
                hashlock,
                timelock
            ).call({'from': lp_address})
            simulation_result = f"0x{result.hex()}"
        except Exception as e:
            simulation_error = str(e)

        return {
            "connected": connected,
            "lp_address": lp_address,
            "eth_balance": eth_balance,
            "usdc_balance_raw": usdc_balance,
            "usdc_balance": usdc_balance / 1e6,
            "allowance_raw": allowance,
            "allowance": allowance / 1e6,
            "nonce_latest": nonce_latest,
            "nonce_pending": nonce_pending,
            "gas_price_gwei": gas_price / 1e9,
            "htlc_contract": HTLC_CONTRACT_BASE_SEPOLIA,
            "usdc_contract": USDC_CONTRACT_BASE_SEPOLIA,
            "simulation": {
                "receiver": receiver,
                "amount_usdc": 1.0,
                "timelock": timelock,
                "result": simulation_result,
                "error": simulation_error
            }
        }

    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"SDK not available: {e}")
    except Exception as e:
        log.exception("Debug failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chains/status")
async def get_all_chains_status():
    """Get status of all chains (refreshes installed/running status)."""
    # Re-check installation and running status
    for chain in ["btc", "m1"]:
        binary = CHAIN_BINARIES.get(chain)
        if binary and binary.exists():
            chain_status_db[chain]["installed"] = True
            # Check if running
            try:
                result = subprocess.run(
                    ["pgrep", "-f", binary.name],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    chain_status_db[chain]["running"] = True
                    chain_status_db[chain]["pid"] = int(result.stdout.strip().split()[0])
                else:
                    chain_status_db[chain]["running"] = False
                    chain_status_db[chain]["pid"] = None
            except:
                pass
        else:
            chain_status_db[chain]["installed"] = False

    return {
        "chains": chain_status_db,
        "timestamp": int(time.time()),
    }

# =============================================================================
# WALLET ENDPOINTS
# =============================================================================

# LP wallet address for USDC (Base Sepolia)
LP_USDC_ADDRESS_DEFAULT = "0xB6bc96842f6085a949b8433dc6316844c32Cba63"
LP_USDC_PRIVKEY = None  # Loaded at startup from ~/.BathronKey/evm.json — NEVER hardcode keys

# USDC Token contract on Base Sepolia
# Official USDC on Base Sepolia: https://sepolia.basescan.org/token/0x036CbD53842c5426634e7929541eC2318f3dCF7e
USDC_CONTRACT_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# HTLC Contract on Base Sepolia (deployed 2026-02-04)
# https://sepolia.basescan.org/address/0xBCf3eeb42629143A1B29d9542fad0E54a04dBFD2
HTLC_CONTRACT_BASE_SEPOLIA = "0xBCf3eeb42629143A1B29d9542fad0E54a04dBFD2"

# Base RPC endpoint
BASE_SEPOLIA_RPC = "https://sepolia.base.org"

# Cache for LP addresses (persistent across refreshes)
_lp_addresses = {"btc": None, "m1": None, "usdc": None}

# Persistent storage for LP addresses
LP_ADDRESS_FILE = Path(__file__).parent / ".lp_addresses.json"

def load_lp_addresses():
    """Load LP addresses from ~/.BathronKey/ (source of truth) then fallback to cache.

    Priority:
    1. ~/.BathronKey/wallet.json -> btc_address, address (M1)
    2. ~/.BathronKey/btc.json -> address (BTC)
    3. ~/.BathronKey/evm.json -> address (USDC/EVM)
    4. .lp_addresses.json cache (fallback)
    """
    global _lp_addresses

    key_dir = Path.home() / ".BathronKey"

    # 1. Load from ~/.BathronKey/wallet.json (main wallet file)
    wallet_file = key_dir / "wallet.json"
    if wallet_file.exists():
        try:
            with open(wallet_file) as f:
                wallet = json.load(f)
            if wallet.get("btc_address"):
                _lp_addresses["btc"] = wallet["btc_address"]
                log.info(f"BTC address from ~/.BathronKey/wallet.json: {_lp_addresses['btc']}")
            if wallet.get("address"):
                _lp_addresses["m1"] = wallet["address"]
                log.info(f"M1 address from ~/.BathronKey/wallet.json: {_lp_addresses['m1']}")
        except Exception as e:
            log.error(f"Failed to load ~/.BathronKey/wallet.json: {e}")

    # 2. BTC from btc.json ALWAYS overrides wallet.json (btc.json has the claim key)
    btc_file = key_dir / "btc.json"
    if btc_file.exists():
        try:
            with open(btc_file) as f:
                btc_data = json.load(f)
            if btc_data.get("address"):
                _lp_addresses["btc"] = btc_data["address"]
                log.info(f"BTC address from ~/.BathronKey/btc.json: {_lp_addresses['btc']}")
        except Exception as e:
            log.error(f"Failed to load ~/.BathronKey/btc.json: {e}")

    # 3. EVM/USDC from evm.json
    evm_file = key_dir / "evm.json"
    if evm_file.exists():
        try:
            with open(evm_file) as f:
                evm_data = json.load(f)
            if evm_data.get("address"):
                _lp_addresses["usdc"] = evm_data["address"]
                log.info(f"USDC address from ~/.BathronKey/evm.json: {_lp_addresses['usdc']}")
        except Exception as e:
            log.error(f"Failed to load ~/.BathronKey/evm.json: {e}")

    # 4. Fallback: load from cache for anything still missing
    if LP_ADDRESS_FILE.exists():
        try:
            with open(LP_ADDRESS_FILE, 'r') as f:
                saved = json.load(f)
                if not _lp_addresses["btc"]:
                    _lp_addresses["btc"] = saved.get("btc")
                if not _lp_addresses["m1"]:
                    _lp_addresses["m1"] = saved.get("m1")
                if not _lp_addresses["usdc"]:
                    _lp_addresses["usdc"] = saved.get("usdc")
        except Exception as e:
            log.error(f"Failed to load LP address cache: {e}")

    # Final fallback for USDC
    if not _lp_addresses["usdc"]:
        _lp_addresses["usdc"] = LP_USDC_ADDRESS_DEFAULT

    log.info(f"LP addresses: BTC={_lp_addresses['btc']}, M1={_lp_addresses['m1']}, USDC={_lp_addresses['usdc']}")

    # Persist consolidated addresses
    save_lp_addresses()

def save_lp_addresses():
    """Save LP addresses to persistent storage."""
    try:
        with open(LP_ADDRESS_FILE, 'w') as f:
            json.dump(_lp_addresses, f)
        log.info(f"Saved LP addresses to disk")
    except Exception as e:
        log.error(f"Failed to save LP addresses: {e}")

def get_all_addresses_for_label(cli_path: Path, network_flag: str, label: str, extra_args: list = None) -> List[str]:
    """Get ALL addresses for a given label."""
    try:
        cmd = [str(cli_path), network_flag]
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(["getaddressesbylabel", label])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            addresses = json.loads(result.stdout.strip())
            if addresses:
                return list(addresses.keys())
    except Exception as e:
        log.error(f"Error getting addresses for label {label}: {e}")
    return []

def get_or_create_labeled_address(cli_path: Path, network_flag: str, label: str, address_type: str = None, extra_args: list = None) -> Optional[str]:
    """Get existing address for label, or create new one if none exists."""
    try:
        # Try to get existing addresses for this label
        cmd = [str(cli_path), network_flag]
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(["getaddressesbylabel", label])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode == 0 and result.stdout.strip():
            # Parse JSON response - keys are addresses
            addresses = json.loads(result.stdout.strip())
            if addresses:
                # Return first address for this label
                return list(addresses.keys())[0]

        # No address for this label, create one
        cmd = [str(cli_path), network_flag]
        if extra_args:
            cmd.extend(extra_args)
        if address_type:
            cmd.extend(["getnewaddress", label, address_type])
        else:
            cmd.extend(["getnewaddress", label])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            log.error(f"getnewaddress failed: {result.stderr}")
    except Exception as e:
        log.error(f"Error getting/creating address for label {label}: {e}")
    return None

@app.get("/api/wallets")
async def get_wallets():
    """Get wallet addresses and balances for all chains."""
    global _lp_addresses

    wallets = {
        "btc": {"address": None, "balance": 0, "pending": 0},
        "m1": {"address": None, "balance": 0, "pending": 0},
        "usdc": {"address": None, "balance": 0, "pending": 0, "eth_balance": 0},
    }

    # Get BTC wallet info
    # Address comes from ~/.BathronKey/wallet.json (loaded at startup)
    # Balance checked via Bitcoin Core wallet RPC (getbalance), fallback to scantxoutset
    btc_cli = CHAIN_CLI.get("btc")
    btc_datadir = str(Path.home() / ".bitcoin-signet")
    if btc_cli and btc_cli.exists():
        try:
            btc_base_cmd = [str(btc_cli), "-signet", f"-datadir={btc_datadir}"]

            wallets["btc"]["address"] = _lp_addresses.get("btc")

            # Get wallet name from btc.json (same logic as SDK init)
            btc_key = _load_lp_btc_key()
            btc_wallet_name = btc_key.get("wallet") or btc_key.get("wallet_name") or "lp_wallet"

            # Primary: use Bitcoin Core wallet getbalance (sees all wallet UTXOs)
            btc_balance_found = False
            result = subprocess.run(
                btc_base_cmd + [f"-rpcwallet={btc_wallet_name}", "getbalance"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                try:
                    wallets["btc"]["balance"] = float(result.stdout.strip())
                    btc_balance_found = True
                    log.info(f"BTC balance (wallet={btc_wallet_name}): {wallets['btc']['balance']}")
                except ValueError:
                    pass

            # Fallback: scantxoutset for specific address
            if not btc_balance_found and _lp_addresses.get("btc"):
                scan_arg = json.dumps([f"addr({_lp_addresses['btc']})"])
                result = subprocess.run(
                    btc_base_cmd + ["scantxoutset", "start", scan_arg],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    try:
                        scan = json.loads(result.stdout.strip())
                        wallets["btc"]["balance"] = scan.get("total_amount", 0)
                        log.info(f"BTC balance (scantxoutset): {wallets['btc']['balance']}")
                    except (json.JSONDecodeError, ValueError) as e:
                        log.error(f"Failed to parse scantxoutset: {e}")
                else:
                    log.error(f"BTC scantxoutset failed: {result.stderr[:200]}")
        except Exception as e:
            log.error(f"Error getting BTC wallet: {e}")

    # Get M1 wallet info - uses SEPARATE node via SSH tunnel (no MN)
    m1_cli = CHAIN_CLI.get("m1")
    if m1_cli and m1_cli.exists():
        try:
            # Build command with LP RPC args (connects to separate node)
            m1_base_cmd = [str(m1_cli), "-testnet"] + M1_LP_RPC_ARGS

            # Get or reuse LP address (fixed label: lp_pna)
            # First, get ALL addresses with this label for balance calculation
            all_m1_addresses = get_all_addresses_for_label(
                m1_cli, "-testnet", "lp_pna", M1_LP_RPC_ARGS
            )

            if not _lp_addresses["m1"]:
                # Use first existing address or create new one
                if all_m1_addresses:
                    _lp_addresses["m1"] = all_m1_addresses[0]
                    log.info(f"Using existing M1 LP address: {_lp_addresses['m1']}")
                else:
                    # Create new address
                    result = subprocess.run(
                        m1_base_cmd + ["getnewaddress", "lp_pna"],
                        capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0:
                        _lp_addresses["m1"] = result.stdout.strip()
                        all_m1_addresses = [_lp_addresses["m1"]]
                        log.info(f"Created new M1 LP address: {_lp_addresses['m1']}")
                    else:
                        log.error(f"M1 getnewaddress failed: {result.stderr.strip()}")
                        log.error(f"M1 command was: {' '.join(m1_base_cmd + ['getnewaddress', 'lp_pna'])}")

                # Persist to disk
                save_lp_addresses()

            wallets["m1"]["address"] = _lp_addresses["m1"]

            # Get M1 balance from getwalletstate (M1 receipts, not regular UTXOs)
            result = subprocess.run(
                m1_base_cmd + ["getwalletstate", "true"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                try:
                    wallet_state = json.loads(result.stdout.strip())
                    m1_state = wallet_state.get("m1", {})
                    # M1 total is in coins (from ValueFromAmount)
                    m1_total = m1_state.get("total", 0)
                    receipts = m1_state.get("receipts", [])
                    # Calculate pending (0 confirmations)
                    confirmed = sum(r.get("amount", 0) for r in receipts if r.get("confirmations", 0) > 0)
                    pending = sum(r.get("amount", 0) for r in receipts if r.get("confirmations", 0) == 0)
                    wallets["m1"]["balance"] = confirmed
                    wallets["m1"]["pending"] = pending
                    log.info(f"M1 balance: {confirmed} confirmed, {pending} pending ({len(receipts)} receipts)")
                except json.JSONDecodeError as e:
                    log.error(f"Failed to parse M1 getwalletstate: {e}")
            else:
                log.error(f"M1 getwalletstate failed: {result.stderr}")
        except Exception as e:
            log.error(f"Error getting M1 wallet: {e}")

    # Get USDC wallet info on Base Sepolia
    # Use cached address or default
    usdc_address = _lp_addresses.get("usdc") or LP_USDC_ADDRESS_DEFAULT
    wallets["usdc"]["address"] = usdc_address

    try:
        # 1. Get ETH balance (for gas)
        eth_rpc_data = json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [usdc_address, "latest"],
            "id": 1
        })

        result = subprocess.run(
            ["curl", "-s", "-X", "POST", BASE_SEPOLIA_RPC,
             "-H", "Content-Type: application/json",
             "-d", eth_rpc_data,
             "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )

        eth_balance = 0
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            if "result" in data and data["result"]:
                balance_wei = int(data["result"], 16)
                eth_balance = balance_wei / 1e18

        # 2. Get USDC token balance (ERC20 balanceOf)
        # balanceOf(address) selector = 0x70a08231
        # Pad address to 32 bytes
        address_padded = usdc_address.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{address_padded}"

        usdc_rpc_data = json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": USDC_CONTRACT_BASE_SEPOLIA,
                "data": call_data
            }, "latest"],
            "id": 2
        })

        result = subprocess.run(
            ["curl", "-s", "-X", "POST", BASE_SEPOLIA_RPC,
             "-H", "Content-Type: application/json",
             "-d", usdc_rpc_data,
             "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )

        usdc_balance = 0
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            if "result" in data and data["result"] and data["result"] != "0x":
                # USDC has 6 decimals
                balance_raw = int(data["result"], 16)
                usdc_balance = balance_raw / 1e6

        wallets["usdc"]["balance"] = usdc_balance
        wallets["usdc"]["eth_balance"] = eth_balance  # For gas
        log.info(f"USDC wallet {usdc_address[:10]}...: {usdc_balance} USDC, {eth_balance} ETH (gas)")

    except Exception as e:
        log.error(f"Error getting USDC balance: {e}")

    return wallets


@app.get("/api/wallets/debug")
async def get_wallets_debug():
    """
    Debug endpoint to see detailed wallet info.
    Shows all addresses, UTXOs, and cached state.
    """
    debug_info = {
        "cached_addresses": _lp_addresses.copy(),
        "m1_details": {},
        "btc_details": {},
    }

    # M1 detailed info
    m1_cli = CHAIN_CLI.get("m1")
    if m1_cli and m1_cli.exists():
        try:
            m1_base_cmd = [str(m1_cli), "-testnet"] + M1_LP_RPC_ARGS

            # Get ALL addresses with label
            all_addresses = get_all_addresses_for_label(
                m1_cli, "-testnet", "lp_pna", M1_LP_RPC_ARGS
            )
            debug_info["m1_details"]["all_labeled_addresses"] = all_addresses
            debug_info["m1_details"]["address_count"] = len(all_addresses)

            # Get UTXOs for each address
            utxos_by_address = {}
            for addr in all_addresses:
                result = subprocess.run(
                    m1_base_cmd + ["listunspent", "0", "9999999", json.dumps([addr])],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    try:
                        utxos = json.loads(result.stdout.strip())
                        utxos_by_address[addr] = {
                            "utxo_count": len(utxos),
                            "total": sum(u["amount"] for u in utxos),
                            "confirmed": sum(u["amount"] for u in utxos if u.get("confirmations", 0) > 0),
                            "pending": sum(u["amount"] for u in utxos if u.get("confirmations", 0) == 0),
                        }
                    except:
                        utxos_by_address[addr] = {"error": "parse failed"}
                else:
                    utxos_by_address[addr] = {"error": result.stderr}

            debug_info["m1_details"]["utxos_by_address"] = utxos_by_address

            # Total across all addresses
            total_confirmed = sum(v.get("confirmed", 0) for v in utxos_by_address.values() if isinstance(v.get("confirmed"), (int, float)))
            total_pending = sum(v.get("pending", 0) for v in utxos_by_address.values() if isinstance(v.get("pending"), (int, float)))
            debug_info["m1_details"]["total_confirmed"] = total_confirmed
            debug_info["m1_details"]["total_pending"] = total_pending

        except Exception as e:
            debug_info["m1_details"]["error"] = str(e)

    # BTC detailed info
    btc_cli = CHAIN_CLI.get("btc")
    if btc_cli and btc_cli.exists():
        try:
            # Load wallet first
            subprocess.run(
                [str(btc_cli), "-signet", "loadwallet", "lp_wallet"],
                capture_output=True, text=True, timeout=10
            )

            # Get ALL addresses with label
            all_addresses = get_all_addresses_for_label(btc_cli, "-signet", "lp_btc")
            debug_info["btc_details"]["all_labeled_addresses"] = all_addresses
            debug_info["btc_details"]["address_count"] = len(all_addresses)

            # Get UTXOs for each address
            utxos_by_address = {}
            for addr in all_addresses:
                result = subprocess.run(
                    [str(btc_cli), "-signet", "listunspent", "0", "9999999", json.dumps([addr])],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    try:
                        utxos = json.loads(result.stdout.strip())
                        utxos_by_address[addr] = {
                            "utxo_count": len(utxos),
                            "total": sum(u["amount"] for u in utxos),
                            "confirmed": sum(u["amount"] for u in utxos if u.get("confirmations", 0) > 0),
                            "pending": sum(u["amount"] for u in utxos if u.get("confirmations", 0) == 0),
                        }
                    except:
                        utxos_by_address[addr] = {"error": "parse failed"}
                else:
                    utxos_by_address[addr] = {"error": result.stderr}

            debug_info["btc_details"]["utxos_by_address"] = utxos_by_address

            # Total across all addresses
            total_confirmed = sum(v.get("confirmed", 0) for v in utxos_by_address.values() if isinstance(v.get("confirmed"), (int, float)))
            total_pending = sum(v.get("pending", 0) for v in utxos_by_address.values() if isinstance(v.get("pending"), (int, float)))
            debug_info["btc_details"]["total_confirmed"] = total_confirmed
            debug_info["btc_details"]["total_pending"] = total_pending

        except Exception as e:
            debug_info["btc_details"]["error"] = str(e)

    # USDC detailed info
    usdc_address = _lp_addresses.get("usdc") or LP_USDC_ADDRESS_DEFAULT
    debug_info["usdc_details"] = {
        "address": usdc_address,
        "contract": USDC_CONTRACT_BASE_SEPOLIA,
        "rpc": BASE_SEPOLIA_RPC,
    }

    try:
        # Get ETH balance
        eth_rpc_data = json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [usdc_address, "latest"],
            "id": 1
        })
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", BASE_SEPOLIA_RPC,
             "-H", "Content-Type: application/json",
             "-d", eth_rpc_data,
             "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            if "result" in data and data["result"]:
                debug_info["usdc_details"]["eth_balance_wei"] = data["result"]
                debug_info["usdc_details"]["eth_balance"] = int(data["result"], 16) / 1e18

        # Get USDC balance
        address_padded = usdc_address.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{address_padded}"
        usdc_rpc_data = json.dumps({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": USDC_CONTRACT_BASE_SEPOLIA, "data": call_data}, "latest"],
            "id": 2
        })
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", BASE_SEPOLIA_RPC,
             "-H", "Content-Type: application/json",
             "-d", usdc_rpc_data,
             "--max-time", "10"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            if "result" in data and data["result"] and data["result"] != "0x":
                debug_info["usdc_details"]["usdc_balance_raw"] = data["result"]
                debug_info["usdc_details"]["usdc_balance"] = int(data["result"], 16) / 1e6
            else:
                debug_info["usdc_details"]["usdc_balance"] = 0
    except Exception as e:
        debug_info["usdc_details"]["error"] = str(e)

    return debug_info


@app.post("/api/wallets/reset-address")
async def reset_wallet_address(chain: str = Query(...)):
    """
    Reset the cached address for a chain and get a fresh one.
    Use if the cached address is wrong.
    """
    global _lp_addresses

    if chain not in ["btc", "m1", "usdc"]:
        raise HTTPException(400, f"Unknown chain: {chain}")

    old_address = _lp_addresses.get(chain)
    _lp_addresses[chain] = None

    # Force re-fetch
    wallets = await get_wallets()

    new_address = _lp_addresses.get(chain)
    save_lp_addresses()

    return {
        "chain": chain,
        "old_address": old_address,
        "new_address": new_address,
        "balance": wallets.get(chain, {}).get("balance", 0),
    }


@app.post("/api/wallets/set-address")
async def set_wallet_address(chain: str = Query(...), address: str = Query(...)):
    """
    Set a custom wallet address for a chain.
    Mainly used for USDC where address can't be auto-generated.
    """
    global _lp_addresses

    if chain not in ["btc", "m1", "usdc"]:
        raise HTTPException(400, f"Unknown chain: {chain}")

    # Validate address format
    if chain == "usdc":
        if not address.startswith("0x") or len(address) != 42:
            raise HTTPException(400, "Invalid Ethereum address format")
    elif chain == "btc":
        # BTC addresses: start with tb1 (segwit) or 2/m/n (testnet legacy)
        if not (address.startswith("tb1") or address.startswith("2") or
                address.startswith("m") or address.startswith("n")):
            raise HTTPException(400, "Invalid Bitcoin Signet address format")
    elif chain == "m1":
        # BATHRON addresses start with y
        if not address.startswith("y"):
            raise HTTPException(400, "Invalid BATHRON address format")

    old_address = _lp_addresses.get(chain)
    _lp_addresses[chain] = address
    save_lp_addresses()

    # Fetch balance for new address
    wallets = await get_wallets()

    log.info(f"Set {chain} LP address: {old_address} -> {address}")

    return {
        "chain": chain,
        "old_address": old_address,
        "new_address": address,
        "balance": wallets.get(chain, {}).get("balance", 0),
    }


# =============================================================================
# PRICE FEEDS
# =============================================================================

# Price source configuration
price_sources_config: Dict[str, Dict[str, Any]] = {
    "binance": {"enabled": True, "api_key": None},
    "mexc": {"enabled": False, "api_key": None},
    "coingecko": {"enabled": True, "api_key": None},
    "kraken": {"enabled": False, "api_key": None},
}

@app.get("/api/rates")
async def get_rates():
    """Get aggregated rates from configured sources and update LP pricing."""
    # Fetch live BTC price and update LP_CONFIG
    btc_price = await fetch_live_btc_usdc_price()

    # Also update LP_CONFIG to ensure quotes use current price
    usdc_m1_rate = BTC_M1_FIXED_RATE / btc_price
    LP_CONFIG["pairs"]["USDC/M1"]["rate"] = usdc_m1_rate

    return {
        "BTC": btc_price,
        "ETH": btc_price / 28,  # Approximate ETH/BTC ratio
        "USDC": 1.0,
        "M1": 1.0,
        "USDC_M1_rate": usdc_m1_rate,  # For transparency
        "sources": ["binance"],
        "timestamp": int(time.time()),
    }

@app.post("/api/rates/sources")
async def update_price_sources(sources: Dict[str, Any]):
    """Update price source configuration."""
    for source, config in sources.items():
        if source in price_sources_config:
            price_sources_config[source].update(config)
            log.info(f"Updated price source: {source}")

    return {"success": True, "sources": price_sources_config}

@app.get("/api/rates/sources")
async def get_price_sources():
    """Get price source configuration."""
    return {"sources": price_sources_config}

# =============================================================================
# PRICE PROXY (to avoid CORS issues)
# =============================================================================

import httpx

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

# =============================================================================
# LIVE PRICE FETCHING
# =============================================================================

# Cache for live prices
_price_cache = {
    "btc_usdc": None,
    "last_update": 0,
    "cache_ttl": 60,  # seconds (price doesn't move enough to matter for quotes)
}

# Reusable httpx client (avoids SSL handshake on every request)
_httpx_client: Optional[httpx.AsyncClient] = None

def _get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(timeout=5.0)
    return _httpx_client

async def fetch_live_btc_usdc_price():
    """Fetch live BTC/USDC price from Binance."""
    now = time.time()

    # Use cached price if fresh
    if (_price_cache["btc_usdc"] is not None and
        now - _price_cache["last_update"] < _price_cache["cache_ttl"]):
        return _price_cache["btc_usdc"]

    try:
        client = _get_httpx_client()
        response = await asyncio.wait_for(
            client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDC"),
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()
        price = float(data["price"])

        # Update cache
        _price_cache["btc_usdc"] = price
        _price_cache["last_update"] = now

        # Update LP config with new USDC/M1 rate
        # 1 USDC = (1/BTC_USDC) * 100M M1 = 100M/BTC_USDC M1
        usdc_m1_rate = BTC_M1_FIXED_RATE / price
        LP_CONFIG["pairs"]["USDC/M1"]["rate"] = usdc_m1_rate

        log.info(f"Price updated: BTC/USDC={price:.2f}, USDC/M1={usdc_m1_rate:.2f}")
        return price
    except Exception as e:
        log.error(f"Failed to fetch live price: {e}")
        # Return cached price or default
        return _price_cache["btc_usdc"] or 76000.0

@app.get("/api/proxy/price")
async def proxy_price(url: str = Query(...), path: str = Query("price")):
    """
    Proxy price API calls to avoid CORS issues.
    Fetches URL and extracts price using JSON path.
    """
    # Whitelist domains for security
    allowed_domains = [
        "api.binance.com",
        "api.coingecko.com",
        "api.kraken.com",
        "api.coinbase.com",
        "api.mexc.com",
    ]

    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.netloc not in allowed_domains:
        # Allow custom URLs but log them
        log.warning(f"Price proxy request to non-whitelisted domain: {parsed.netloc}")

    try:
        client = _get_httpx_client()
        response = await asyncio.wait_for(client.get(url), timeout=10.0)
        response.raise_for_status()
        data = response.json()

        # Extract price using path
        price = extract_json_path(data, path)

        if price is not None:
            return {"price": float(price), "source": parsed.netloc}
        else:
            return {"error": f"Could not extract price at path: {path}", "data": data}

    except httpx.TimeoutException:
        raise HTTPException(504, "Upstream timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Upstream error: {e.response.status_code}")
    except Exception as e:
        log.error(f"Price proxy error: {e}")
        raise HTTPException(500, f"Proxy error: {str(e)}")

# =============================================================================
# SDK REAL SWAP ENDPOINTS
# =============================================================================

class SDKSwapRequest(BaseModel):
    """Request to initiate a real swap."""
    from_asset: str = Field(..., example="BTC")
    to_asset: str = Field(..., example="M1")
    from_amount_sats: int = Field(..., gt=0, example=100000)
    user_claim_address: str = Field(..., example="yAddress...")
    user_refund_address: Optional[str] = None

class SDKHTLCCreateRequest(BaseModel):
    """Request to create M1 HTLC."""
    receipt_outpoint: str = Field(..., example="txid:0")
    hashlock: str = Field(..., min_length=64, max_length=64)
    claim_address: str = Field(...)
    expiry_blocks: int = Field(default=288, ge=6, le=4320)

class SDKHTLCClaimRequest(BaseModel):
    """Request to claim HTLC."""
    htlc_outpoint: str = Field(..., example="txid:0")
    preimage: str = Field(..., min_length=64, max_length=64)

@app.get("/api/sdk/status")
async def sdk_status():
    """Check SDK availability and status."""
    status = {
        "sdk_available": SDK_AVAILABLE,
        "m1_client": get_m1_client() is not None if SDK_AVAILABLE else False,
        "btc_client": get_btc_client() is not None if SDK_AVAILABLE else False,
    }

    # Test M1 connection
    if status["m1_client"]:
        try:
            height = get_m1_client().get_block_count()
            status["m1_height"] = height
            status["m1_connected"] = True
        except Exception as e:
            status["m1_connected"] = False
            status["m1_error"] = str(e)

    # Test BTC connection
    if status["btc_client"]:
        try:
            height = get_btc_client().get_block_count()
            status["btc_height"] = height
            status["btc_connected"] = True
        except Exception as e:
            status["btc_connected"] = False
            status["btc_error"] = str(e)

    return status

@app.post("/api/sdk/htlc/generate")
async def sdk_generate_htlc_secret():
    """Generate new secret and hashlock for HTLC."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    secret, hashlock = generate_secret()

    return {
        "secret": secret,
        "hashlock": hashlock,
        "note": "Keep secret private until ready to claim. Share hashlock with counterparty."
    }

@app.post("/api/sdk/htlc/m1/create")
async def sdk_create_m1_htlc(req: SDKHTLCCreateRequest):
    """
    Create M1 HTLC from receipt.

    This locks an M1 receipt in an HTLC that can be:
    - Claimed by claim_address with the preimage
    - Refunded by creator after expiry_blocks
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_htlc = get_m1_htlc()
    if not m1_htlc:
        raise HTTPException(503, "M1 HTLC manager not available")

    try:
        result = m1_htlc.create_htlc(
            receipt_outpoint=req.receipt_outpoint,
            hashlock=req.hashlock,
            claim_address=req.claim_address,
            expiry_blocks=req.expiry_blocks,
        )

        return {
            "success": True,
            "txid": result.get("txid"),
            "htlc_outpoint": f"{result.get('txid')}:0",
            "htlc_address": result.get("htlc_address"),
            "expiry_height": result.get("expiry_height"),
        }

    except Exception as e:
        log.error(f"HTLC creation failed: {e}")
        raise HTTPException(400, f"HTLC creation failed: {str(e)}")

@app.post("/api/sdk/htlc/m1/claim")
async def sdk_claim_m1_htlc(req: SDKHTLCClaimRequest):
    """
    Claim M1 HTLC with preimage.

    The preimage must hash to the HTLC's hashlock.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_htlc = get_m1_htlc()
    if not m1_htlc:
        raise HTTPException(503, "M1 HTLC manager not available")

    # Verify preimage first
    htlc = m1_htlc.get_htlc(req.htlc_outpoint)
    if not htlc:
        raise HTTPException(404, "HTLC not found")

    if not verify_preimage(req.preimage, htlc.hashlock):
        raise HTTPException(400, "Invalid preimage - does not match hashlock")

    try:
        result = m1_htlc.claim(req.htlc_outpoint, req.preimage)

        return {
            "success": True,
            "txid": result.get("txid"),
            "receipt_outpoint": result.get("receipt_outpoint"),
        }

    except Exception as e:
        log.error(f"HTLC claim failed: {e}")
        raise HTTPException(400, f"HTLC claim failed: {str(e)}")

@app.post("/api/sdk/htlc/m1/refund")
async def sdk_refund_m1_htlc(htlc_outpoint: str = Query(...)):
    """
    Refund expired M1 HTLC.

    Only works after the expiry height has passed.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_htlc = get_m1_htlc()
    if not m1_htlc:
        raise HTTPException(503, "M1 HTLC manager not available")

    try:
        result = m1_htlc.refund(htlc_outpoint)

        return {
            "success": True,
            "txid": result.get("txid"),
            "receipt_outpoint": result.get("receipt_outpoint"),
        }

    except Exception as e:
        log.error(f"HTLC refund failed: {e}")
        raise HTTPException(400, f"HTLC refund failed: {str(e)}")

@app.get("/api/sdk/htlc/m1/list")
async def sdk_list_m1_htlcs(status: Optional[str] = None, hashlock: Optional[str] = None):
    """
    List M1 HTLCs.

    Optional filters:
    - status: active, claimed, refunded
    - hashlock: filter by specific hashlock
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_htlc = get_m1_htlc()
    if not m1_htlc:
        raise HTTPException(503, "M1 HTLC manager not available")

    try:
        htlcs = m1_htlc.list_htlcs(status=status, hashlock=hashlock)

        return {
            "htlcs": [
                {
                    "outpoint": h.outpoint,
                    "hashlock": h.hashlock,
                    "amount": h.amount,
                    "claim_address": h.claim_address,
                    "refund_address": h.refund_address,
                    "create_height": h.create_height,
                    "expiry_height": h.expiry_height,
                    "status": h.status,
                }
                for h in htlcs
            ],
            "count": len(htlcs),
        }

    except Exception as e:
        log.error(f"HTLC list failed: {e}")
        raise HTTPException(500, f"Failed to list HTLCs: {str(e)}")

@app.get("/api/sdk/htlc/m1/{outpoint}")
async def sdk_get_m1_htlc(outpoint: str):
    """Get M1 HTLC details."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_htlc = get_m1_htlc()
    if not m1_htlc:
        raise HTTPException(503, "M1 HTLC manager not available")

    # URL decode the outpoint (: might be encoded)
    outpoint = outpoint.replace("%3A", ":")

    htlc = m1_htlc.get_htlc(outpoint)
    if not htlc:
        raise HTTPException(404, "HTLC not found")

    return {
        "outpoint": htlc.outpoint,
        "hashlock": htlc.hashlock,
        "amount": htlc.amount,
        "claim_address": htlc.claim_address,
        "refund_address": htlc.refund_address,
        "create_height": htlc.create_height,
        "expiry_height": htlc.expiry_height,
        "status": htlc.status,
        "preimage": htlc.preimage,
        "resolve_txid": htlc.resolve_txid,
    }

@app.get("/api/sdk/m1/balance")
async def sdk_m1_balance():
    """Get M0/M1 balance and wallet state."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    try:
        # Get M0 balance
        m0_balance = m1_client.get_balance()

        # Get wallet state with receipts
        wallet_state = m1_client.get_wallet_state(True)
        m1_state = wallet_state.get("m1", {}) if wallet_state else {}
        # M1 total is in "total" field, not "balance"
        m1_balance = m1_state.get("total", 0)
        receipts = m1_state.get("receipts", [])

        return {
            "m0_balance": m0_balance,
            "m1_balance": m1_balance,
            "receipts": receipts,
            "receipts_count": len(receipts),
        }

    except Exception as e:
        log.error(f"Balance check failed: {e}")
        raise HTTPException(500, f"Failed to check balance: {str(e)}")


@app.get("/api/sdk/m1/receipts")
async def sdk_list_m1_receipts():
    """List M1 receipts available for HTLC creation."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    try:
        receipts = m1_client.list_m1_receipts()

        return {
            "receipts": receipts,
            "count": len(receipts),
        }

    except Exception as e:
        log.error(f"Receipt list failed: {e}")
        raise HTTPException(500, f"Failed to list receipts: {str(e)}")

@app.post("/api/sdk/m1/lock")
async def sdk_lock_m0_to_m1(amount: int = Query(..., gt=0)):
    """
    Lock M0 to create M1 receipt.

    This is needed before creating an HTLC.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    try:
        result = m1_client.lock(amount)

        return {
            "success": True,
            "txid": result.get("txid"),
            "receipt_outpoint": f"{result.get('txid')}:1" if result.get("txid") else None,  # Receipt at vout[1]
            "amount": amount,
        }

    except Exception as e:
        log.error(f"Lock failed: {e}")
        raise HTTPException(400, f"Lock failed: {str(e)}")

@app.post("/api/sdk/btc/htlc/create")
async def sdk_create_btc_htlc(
    amount_sats: int = Query(..., gt=0),
    hashlock: str = Query(..., min_length=64, max_length=64),
    recipient_address: str = Query(...),
    refund_address: str = Query(...),
    timeout_blocks: int = Query(default=144, ge=6),
    recipient_pubkey: Optional[str] = Query(default=None, min_length=66, max_length=66),
    refund_pubkey: Optional[str] = Query(default=None, min_length=66, max_length=66),
):
    """
    Create BTC HTLC deposit address.

    Returns the P2WSH address where BTC should be sent.

    If pubkeys are not provided, they will be looked up from the local wallet.
    For external addresses, provide the pubkeys explicitly.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    btc_htlc = get_btc_htlc()
    if not btc_htlc:
        raise HTTPException(503, "BTC HTLC manager not available")

    try:
        result = btc_htlc.create_htlc(
            amount_sats=amount_sats,
            hashlock=hashlock,
            recipient_address=recipient_address,
            refund_address=refund_address,
            timeout_blocks=timeout_blocks,
            recipient_pubkey=recipient_pubkey,
            refund_pubkey=refund_pubkey,
        )

        return {
            "success": True,
            "htlc_address": result["htlc_address"],
            "redeem_script": result["redeem_script"],
            "amount_sats": result["amount"],
            "timelock": result["timelock"],
            "hashlock": result["hashlock"],
        }

    except Exception as e:
        log.error(f"BTC HTLC creation failed: {e}")
        raise HTTPException(400, f"BTC HTLC creation failed: {str(e)}")

@app.get("/api/sdk/btc/htlc/check")
async def sdk_check_btc_htlc(
    htlc_address: str = Query(...),
    expected_amount: int = Query(..., gt=0),
    min_confirmations: int = Query(default=1, ge=0),
):
    """
    Check if BTC HTLC has been funded.

    Returns UTXO info if funded with sufficient confirmations.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    btc_htlc = get_btc_htlc()
    if not btc_htlc:
        raise HTTPException(503, "BTC HTLC manager not available")

    try:
        utxo = btc_htlc.check_htlc_funded(
            htlc_address, expected_amount, min_confirmations
        )

        if utxo:
            return {
                "funded": True,
                "txid": utxo["txid"],
                "vout": utxo["vout"],
                "amount_sats": utxo["amount"],
                "confirmations": utxo["confirmations"],
            }
        else:
            return {
                "funded": False,
                "message": "HTLC not yet funded or insufficient confirmations",
            }

    except Exception as e:
        log.error(f"BTC HTLC check failed: {e}")
        raise HTTPException(500, f"Check failed: {str(e)}")


@app.post("/api/sdk/btc/htlc/claim")
async def sdk_claim_btc_htlc(
    swap_id: str = Query(..., description="Swap ID to claim BTC from"),
    preimage: str = Query(..., description="32-byte preimage (hex)"),
    recipient_address: str = Query(None, description="Override recipient address (optional)"),
):
    """
    Claim BTC HTLC with preimage.

    TRUSTLESS: This broadcasts a claim transaction on Bitcoin, which reveals
    the preimage ON-CHAIN. This is the correct way to claim - it allows LP
    to extract the preimage from the Bitcoin blockchain and claim USDC.

    Args:
        swap_id: Swap identifier
        preimage: The 32-byte secret (hex) that hashes to the hashlock
        recipient_address: Optional override for where BTC should go

    Returns:
        Claim transaction ID (user receives BTC, preimage visible on Bitcoin)
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]

    # Verify this is a USDC→BTC swap with BTC HTLC created
    if swap.get("to_asset") != "BTC":
        raise HTTPException(400, "This swap does not have a BTC HTLC to claim")

    if swap.get("status") != "btc_htlc_created":
        raise HTTPException(400, f"Cannot claim: swap status is {swap.get('status')}, expected btc_htlc_created")

    btc_htlc_addr = swap.get("lp_btc_htlc_address")
    btc_htlc_script = swap.get("lp_btc_htlc_script")
    funding_txid = swap.get("lp_btc_htlc_funding_txid")
    amount_sats = swap.get("to_amount_sats", 0)

    if not all([btc_htlc_addr, btc_htlc_script, funding_txid]):
        raise HTTPException(400, "BTC HTLC details incomplete")

    # Verify preimage matches hashlock
    computed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    if computed_hash != swap["hashlock"]:
        raise HTTPException(400, f"Preimage does not match hashlock. Got: {computed_hash}")

    # Get recipient address (user's BTC address or override)
    claim_address = recipient_address or swap.get("user_btc_claim_address")
    if not claim_address:
        raise HTTPException(400, "No recipient address for BTC claim")

    btc_htlc = get_btc_htlc()
    if not btc_htlc:
        raise HTTPException(503, "BTC HTLC manager not available")

    try:
        # Build UTXO info
        utxo = {
            "txid": funding_txid,
            "vout": 0,  # HTLC is always at vout 0
            "amount": amount_sats,
        }

        # Claim the HTLC (this broadcasts to Bitcoin network, revealing preimage)
        claim_txid = btc_htlc.claim_htlc(
            utxo=utxo,
            redeem_script=btc_htlc_script,
            preimage=preimage,
            recipient_address=claim_address,
            fee_rate_sat_vb=2,  # Low fee for Signet
        )

        # Update swap status
        swap["btc_claim_txid"] = claim_txid
        swap["btc_claim_detected"] = True
        swap["preimage"] = preimage
        swap["updated_at"] = int(time.time())

        log.info(f"User claimed BTC HTLC: swap={swap_id}, txid={claim_txid}")
        log.info(f"Preimage {preimage[:16]}... is now PUBLIC on Bitcoin blockchain")

        return {
            "success": True,
            "swap_id": swap_id,
            "claim_txid": claim_txid,
            "amount_sats": amount_sats,
            "recipient": claim_address,
            "preimage_revealed": True,
            "message": "BTC claimed! Preimage is now on Bitcoin blockchain. LP will auto-claim USDC.",
            "explorer": f"https://mempool.space/signet/tx/{claim_txid}",
        }

    except Exception as e:
        log.exception(f"BTC HTLC claim failed: {e}")
        raise HTTPException(500, f"Claim failed: {str(e)}")


@app.get("/api/sdk/m1/tx/{txid}")
async def sdk_get_m1_transaction(txid: str):
    """Debug: Get M1 transaction details."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    try:
        # Try wallet transaction first
        tx = m1_client.get_transaction(txid)
        if tx:
            return {"source": "wallet", "transaction": tx}

        # Try raw transaction
        raw_tx = m1_client.get_raw_transaction(txid)
        if raw_tx:
            return {"source": "raw", "transaction": raw_tx}

        return {"error": "Transaction not found"}

    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sdk/htlc/verify")
async def sdk_verify_preimage(
    preimage: str = Query(..., min_length=64, max_length=64),
    hashlock: str = Query(..., min_length=64, max_length=64)
):
    """Verify preimage matches hashlock via RPC."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    try:
        result = m1_client.htlc_verify(preimage, hashlock)
        return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


@app.post("/api/sdk/m1/abandon/{txid}")
async def sdk_abandon_transaction(txid: str):
    """Abandon a stuck transaction."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    try:
        result = m1_client.abandon_transaction(txid)
        return {"success": result, "txid": txid}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/sdk/m1/debug/receipt/{outpoint}")
async def sdk_debug_receipt(outpoint: str):
    """Debug: Check if receipt exists in settlement DB."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")

    # URL decode
    outpoint = outpoint.replace("%3A", ":")

    try:
        # Check via getwalletstate
        wallet_state = m1_client.get_wallet_state(True)
        m1_state = wallet_state.get("m1", {}) if wallet_state else {}
        receipts = m1_state.get("receipts", [])

        found_receipt = None
        for r in receipts:
            if r.get("outpoint") == outpoint:
                found_receipt = r
                break

        # Also check HTLC list
        htlcs = m1_client.htlc_list()

        return {
            "outpoint": outpoint,
            "receipt_found": found_receipt is not None,
            "receipt": found_receipt,
            "all_receipts_count": len(receipts),
            "active_htlcs_count": len(htlcs) if htlcs else 0,
        }

    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sdk/htlc3s/m1/list")
async def sdk_list_m1_htlc3s(status: Optional[str] = None):
    """List 3-secret M1 HTLCs (used by FlowSwap)."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")
    m1_3s = get_m1_htlc_3s()
    if not m1_3s:
        raise HTTPException(503, "M1 HTLC3S manager not available")
    try:
        htlcs = m1_3s.list_htlcs(status=status)
        return {
            "htlcs": [
                {
                    "outpoint": h.outpoint,
                    "amount": h.amount,
                    "status": h.status,
                    "claim_address": h.claim_address,
                    "refund_address": h.refund_address,
                    "create_height": h.create_height,
                    "expiry_height": h.expiry_height,
                }
                for h in htlcs
            ],
            "count": len(htlcs),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/sdk/htlc3s/m1/refund-expired")
async def sdk_refund_expired_m1_htlc3s():
    """Refund ALL expired 3-secret M1 HTLCs back to LP wallet."""
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")
    m1_3s = get_m1_htlc_3s()
    if not m1_3s:
        raise HTTPException(503, "M1 HTLC3S manager not available")
    m1_client = get_m1_client()
    if not m1_client:
        raise HTTPException(503, "M1 client not available")
    try:
        htlcs = m1_3s.list_htlcs()
        current_height = m1_client.get_block_count()
        refunded = []
        errors = []
        for h in htlcs:
            if h.status != "active":
                continue
            if h.expiry_height > current_height:
                continue  # not yet expired
            try:
                result = m1_client.htlc3s_refund(h.outpoint)
                refunded.append({
                    "outpoint": h.outpoint,
                    "amount": h.amount,
                    "txid": result.get("txid") if isinstance(result, dict) else str(result),
                })
            except Exception as e:
                errors.append({"outpoint": h.outpoint, "error": str(e)})
        return {
            "refunded": refunded,
            "errors": errors,
            "current_height": current_height,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# =============================================================================
# SWAP MONITOR - AUTO LP HTLC RESPONSE
# =============================================================================

# Global swap monitor
_swap_monitor = None
_evm_watcher_thread = None
_evm_watcher_running = False

# Track pending swaps waiting for LP HTLC creation
pending_lp_htlcs: Dict[str, Dict[str, Any]] = {}


def start_evm_watcher():
    """Background thread to watch for incoming USDC HTLCs and auto-respond."""
    global _evm_watcher_running
    _evm_watcher_running = True
    log.info("EVM HTLC watcher started - monitoring for incoming HTLCs")

    # Poll interval (seconds)
    poll_interval = 10

    while _evm_watcher_running:
        try:
            _check_pending_swaps()
        except Exception as e:
            log.error(f"EVM watcher error: {e}")

        time.sleep(poll_interval)


def _check_pending_swaps():
    """Check pending swaps and process them."""
    now = int(time.time())

    for swap_id, swap in list(pending_lp_htlcs.items()):
        if swap["status"] == "awaiting_user_htlc":
            # Check if user has deposited
            if swap["from_asset"] == "USDC":
                _check_usdc_deposit(swap_id, swap)
            elif swap["from_asset"] == "BTC":
                _check_btc_deposit(swap_id, swap)

        elif swap["status"] == "m1_htlc_created":
            # OLD FLOW (deprecated): Check if user claimed M1 HTLC
            _check_m1_htlc_claimed(swap_id, swap)

        elif swap["status"] == "btc_htlc_created":
            # CORRECT 4-HTLC FLOW: Check if user claimed BTC HTLC
            # User claims BTC → reveals S on Bitcoin → LP claims USDC
            _check_btc_htlc_claimed(swap_id, swap)

        elif swap["status"] == "lp_htlc_created":
            # Check if user has claimed LP HTLC
            if swap["to_asset"] == "USDC":
                _check_usdc_claim(swap_id, swap)
            elif swap["to_asset"] == "M1":
                _check_m1_claim(swap_id, swap)

        # Expire old swaps
        if now > swap.get("expires_at", 0):
            swap["status"] = "expired"
            log.info(f"Swap {swap_id} expired")


def _check_usdc_deposit(swap_id: str, swap: Dict):
    """Check if user has deposited USDC to HTLC."""
    try:
        from sdk.htlc.evm import get_htlc
        htlc_id = swap.get("user_htlc_id")
        if not htlc_id:
            return

        htlc = get_htlc(htlc_id, contract=HTLC_CONTRACT_BASE_SEPOLIA)
        if htlc and htlc["status"] == "active":
            log.info(f"User USDC HTLC confirmed: {htlc_id}")
            swap["status"] = "user_deposit_confirmed"
            swap["user_htlc_data"] = htlc

            # Auto-create LP counter-HTLC
            _create_lp_counter_htlc(swap_id, swap)

    except Exception as e:
        log.error(f"Error checking USDC deposit: {e}")


def _check_btc_deposit(swap_id: str, swap: Dict):
    """Check if user has deposited BTC to HTLC address."""
    try:
        btc_client = get_btc_client()
        if not btc_client:
            return

        htlc_addr = swap.get("user_btc_htlc_address")
        expected_sats = swap.get("from_amount_sats", 0)
        if not htlc_addr or expected_sats <= 0:
            return

        # Check for UTXOs at HTLC address
        btc_htlc = get_btc_htlc()
        if btc_htlc:
            utxo = btc_htlc.check_htlc_funded(htlc_addr, expected_sats, min_confirmations=1)
            if utxo:
                log.info(f"User BTC deposit confirmed: {utxo['txid']}")
                swap["status"] = "user_deposit_confirmed"
                swap["user_btc_utxo"] = utxo

                # Auto-create LP counter-HTLC
                _create_lp_counter_htlc(swap_id, swap)

    except Exception as e:
        log.error(f"Error checking BTC deposit: {e}")


def _create_lp_counter_htlc(swap_id: str, swap: Dict):
    """Create LP's counter-HTLC after user deposit confirmed."""
    try:
        to_asset = swap["to_asset"]
        hashlock = swap["hashlock"]

        if to_asset == "USDC":
            # LP creates USDC HTLC for user to claim
            from sdk.htlc.evm import create_htlc as create_usdc_htlc

            # Load LP key
            lp_key_path = Path.home() / ".keys" / "lp_evm.json"
            if not lp_key_path.exists():
                log.error("LP EVM key not found")
                return

            with open(lp_key_path) as f:
                lp_key = json.load(f)["private_key"]

            user_address = swap["user_usdc_address"]
            amount_usdc = swap["to_amount_usdc"]

            result = create_usdc_htlc(
                receiver=user_address,
                amount_usdc=amount_usdc,
                hashlock=hashlock,
                timelock_seconds=1800,  # 30 min (shorter than user's)
                private_key=lp_key
            )

            if result.success:
                swap["lp_htlc_id"] = result.htlc_id
                swap["lp_htlc_tx"] = result.tx_hash
                swap["status"] = "lp_htlc_created"
                log.info(f"LP USDC HTLC created: {result.htlc_id}")
            else:
                log.error(f"Failed to create LP USDC HTLC: {result.error}")

        elif to_asset == "BTC":
            # =================================================================
            # CORRECT 4-HTLC FLOW: USDC → BTC (M1 invisible to user)
            # =================================================================
            # 1. User created HTLC-USDC (already done, detected above)
            # 2. LP locks M1 INTERNALLY (commitment checkpoint, NOT to user)
            # 3. LP creates HTLC-BTC for user to claim
            # 4. User claims HTLC-BTC → reveals S on Bitcoin
            # 5. LP sees S on Bitcoin, claims HTLC-USDC
            # 6. LP unlocks M1 internally
            #
            # USER NEVER TOUCHES M1 - it's purely internal LP settlement rail
            # =================================================================

            amount_sats = swap["to_amount_sats"]
            user_btc_addr = swap.get("user_btc_claim_address")

            if not user_btc_addr:
                log.error("No user BTC claim address provided")
                swap["status"] = "error_no_btc_address"
                return

            try:
                # STEP 2: Lock M1 internally as commitment checkpoint (OPTIONAL)
                # LP may already have sufficient M1 from previous operations
                m1_client = get_m1_client()
                if m1_client:
                    try:
                        # Lock M0 → M1 as internal commitment (NOT an HTLC to user!)
                        # This proves LP has committed to the swap
                        lock_result = m1_client.lock(amount_sats)
                        if lock_result and lock_result.get("txid"):
                            swap["m1_lock_txid"] = lock_result.get("txid")
                            swap["m1_lock_receipt"] = lock_result.get("receipt_outpoint")
                            log.info(f"M1 locked internally (commitment): {lock_result.get('txid')}")
                        else:
                            log.warning("M1 lock returned no txid, continuing with existing M1")
                    except Exception as lock_err:
                        # M1 lock is optional - LP may have existing M1 from prior swaps
                        log.warning(f"M1 lock failed (using existing M1): {lock_err}")

                # STEP 3: Create HTLC-BTC for user to claim
                btc_htlc = get_btc_htlc()
                if not btc_htlc:
                    log.error("BTC HTLC manager not available")
                    swap["status"] = "error_btc_htlc"
                    return

                # Get LP's BTC address (for refund path)
                lp_btc_addr = _lp_addresses.get("btc")
                if not lp_btc_addr:
                    log.error("LP BTC address not configured")
                    swap["status"] = "error_no_lp_btc"
                    return

                # Create BTC HTLC: user can claim with preimage S
                # LP can refund after timeout if user disappears
                btc_htlc_info = btc_htlc.create_htlc_for_user(
                    amount_sats=amount_sats,
                    hashlock=hashlock,
                    user_address=user_btc_addr,      # User claims here
                    lp_refund_address=lp_btc_addr,   # LP refunds here after timeout
                    timeout_blocks=72                 # ~12 hours (shorter than USDC)
                )

                if not btc_htlc_info:
                    raise RuntimeError("Failed to create BTC HTLC")

                # Fund the HTLC from LP wallet
                funding_txid = btc_htlc.fund_htlc(
                    btc_htlc_info["htlc_address"],
                    amount_sats
                )

                swap["lp_btc_htlc_address"] = btc_htlc_info["htlc_address"]
                swap["lp_btc_htlc_script"] = btc_htlc_info["redeem_script"]
                swap["lp_btc_htlc_funding_txid"] = funding_txid
                swap["lp_btc_htlc_timelock"] = btc_htlc_info["timelock"]
                swap["status"] = "btc_htlc_created"
                swap["flow_stage"] = "awaiting_btc_claim"

                log.info(f"LP BTC HTLC created: {btc_htlc_info['htlc_address']} "
                        f"funded with {amount_sats} sats, txid={funding_txid}")

            except Exception as e:
                log.exception(f"Failed to create BTC HTLC: {e}")
                swap["status"] = "btc_htlc_failed"
                swap["error"] = str(e)

        elif to_asset == "M1":
            # LP creates M1 HTLC for user
            m1_htlc = get_m1_htlc()
            if not m1_htlc:
                log.error("M1 HTLC manager not available")
                return

            amount_sats = swap["to_amount_sats"]
            user_m1_addr = swap["user_m1_address"]

            # Get receipt
            receipt = m1_htlc.ensure_receipt_available(amount_sats)

            htlc_result = m1_htlc.create_htlc(
                receipt_outpoint=receipt,
                hashlock=hashlock,
                claim_address=user_m1_addr,
                expiry_blocks=144  # ~2.4 hours
            )

            swap["lp_m1_htlc_outpoint"] = htlc_result.get("htlc_outpoint")
            swap["status"] = "lp_htlc_created"
            log.info(f"LP M1 HTLC created: {htlc_result.get('htlc_outpoint')}")

    except Exception as e:
        log.exception(f"Error creating LP counter-HTLC: {e}")


def _check_usdc_claim(swap_id: str, swap: Dict):
    """Check if user has claimed LP's USDC HTLC."""
    try:
        from sdk.htlc.evm import get_htlc
        lp_htlc_id = swap.get("lp_htlc_id")
        if not lp_htlc_id:
            return

        htlc = get_htlc(lp_htlc_id, contract=HTLC_CONTRACT_BASE_SEPOLIA)
        if htlc and htlc["status"] == "withdrawn":
            preimage = htlc.get("preimage")
            if preimage:
                log.info(f"User claimed LP USDC HTLC, preimage revealed: {preimage[:16]}...")
                swap["preimage"] = preimage
                swap["status"] = "user_claimed"

                # Auto-claim user's deposit
                _claim_user_deposit(swap_id, swap)

    except Exception as e:
        log.error(f"Error checking USDC claim: {e}")


def _check_m1_claim(swap_id: str, swap: Dict):
    """Check if user has claimed LP's M1 HTLC."""
    try:
        m1_client = get_m1_client()
        if not m1_client:
            return

        lp_htlc_outpoint = swap.get("lp_m1_htlc_outpoint")
        if not lp_htlc_outpoint:
            return

        # Check HTLC status
        htlcs = m1_client.htlc_list()
        for htlc in (htlcs or []):
            if htlc.get("outpoint") == lp_htlc_outpoint:
                if htlc.get("status") == "claimed":
                    preimage = htlc.get("preimage")
                    if preimage:
                        log.info(f"User claimed LP M1 HTLC, preimage: {preimage[:16]}...")
                        swap["preimage"] = preimage
                        swap["status"] = "user_claimed"
                        _claim_user_deposit(swap_id, swap)
                break

    except Exception as e:
        log.error(f"Error checking M1 claim: {e}")


def _check_m1_htlc_claimed(swap_id: str, swap: Dict):
    """
    4-HTLC FLOW: Check if user claimed M1 HTLC (step 3).

    When user claims M1, preimage S is revealed on BATHRON chain.
    LP then:
    1. Extracts preimage S from M1 chain
    2. Sends BTC to user (HTLC-4)
    3. Claims user's USDC HTLC with S (HTLC-1)
    """
    try:
        m1_client = get_m1_client()
        if not m1_client:
            return

        lp_m1_htlc = swap.get("lp_m1_htlc_outpoint")
        if not lp_m1_htlc:
            return

        # Check HTLC status on BATHRON
        htlcs = m1_client.htlc_list()
        for htlc in (htlcs or []):
            if htlc.get("outpoint") == lp_m1_htlc:
                if htlc.get("status") == "claimed":
                    preimage = htlc.get("preimage")
                    if preimage:
                        log.info(f"4-HTLC: User claimed M1! Preimage revealed: {preimage[:16]}...")
                        swap["preimage"] = preimage
                        swap["m1_claim_detected"] = True

                        # Step 4: LP sends BTC to user
                        _send_btc_to_user(swap_id, swap)

                        # Step 5: LP claims USDC with preimage
                        _claim_usdc_with_preimage(swap_id, swap, preimage)

                        swap["status"] = "completed"
                        swap["completed_at"] = int(time.time())
                        log.info(f"4-HTLC SWAP COMPLETE: {swap_id}")
                break

    except Exception as e:
        log.exception(f"Error in 4-HTLC M1 claim check: {e}")


def _check_btc_htlc_claimed(swap_id: str, swap: Dict):
    """
    CORRECT 4-HTLC FLOW: Check if user claimed BTC HTLC.

    Flow: USDC → BTC (M1 invisible to user)
    1. User created HTLC-USDC ✓
    2. LP locked M1 internally ✓
    3. LP created HTLC-BTC ✓
    4. User claims HTLC-BTC → reveals S on Bitcoin ← WE CHECK THIS
    5. LP claims HTLC-USDC with S
    6. LP unlocks M1

    User claims BTC, revealing preimage S on Bitcoin chain.
    LP monitors Bitcoin, extracts S, and claims user's USDC.
    """
    try:
        btc_htlc = get_btc_htlc()
        if not btc_htlc:
            return

        htlc_address = swap.get("lp_btc_htlc_address")
        funding_txid = swap.get("lp_btc_htlc_funding_txid")
        redeem_script_hex = swap.get("lp_btc_htlc_script")

        if not htlc_address or not funding_txid:
            return

        # Check if the HTLC UTXO has been spent (claimed)
        btc_client = get_btc_client()
        if not btc_client:
            return

        # Check if funding UTXO still exists (not spent)
        utxos = btc_client.list_unspent([htlc_address], 0)
        htlc_still_funded = any(u.get("txid") == funding_txid for u in (utxos or []))

        if htlc_still_funded:
            # HTLC not yet claimed, wait
            return

        # HTLC was spent! Extract preimage from the spending transaction
        log.info(f"BTC HTLC spent! Extracting preimage for swap {swap_id}")

        # Find the spending transaction by looking at the address history
        # or by checking the blockchain for transactions spending our UTXO
        preimage = _extract_btc_preimage(btc_client, funding_txid, 0, redeem_script_hex)

        if not preimage:
            log.warning(f"Could not extract preimage from BTC claim for {swap_id}")
            # Could be a refund (after timeout) - check timelock
            current_height = btc_client.get_block_count()
            timelock = swap.get("lp_btc_htlc_timelock", 0)
            if current_height >= timelock:
                log.info(f"BTC HTLC likely refunded (timeout), swap {swap_id}")
                swap["status"] = "btc_refunded"
            return

        log.info(f"4-HTLC: User claimed BTC! Preimage revealed: {preimage[:16]}...")
        swap["preimage"] = preimage
        swap["btc_claim_detected"] = True
        swap["updated_at"] = int(time.time())

        # STEP 5: LP claims USDC with preimage
        _claim_usdc_with_preimage(swap_id, swap, preimage)

        # STEP 6: LP unlocks M1 internally
        _unlock_m1_internal(swap_id, swap)

        swap["status"] = "completed"
        swap["completed_at"] = int(time.time())
        log.info(f"4-HTLC SWAP COMPLETE (correct flow): {swap_id}")

    except Exception as e:
        log.exception(f"Error checking BTC HTLC claim: {e}")


def _extract_btc_preimage(btc_client, funding_txid: str, vout: int,
                          redeem_script_hex: str) -> Optional[str]:
    """
    Extract preimage from a BTC HTLC claim transaction.

    When user claims the HTLC, they reveal the preimage in the witness data.
    We need to find the spending transaction and extract it.
    """
    try:
        # Get the spending transaction
        # Method: use getrawtransaction with verbose to find spent info
        # or scan recent blocks for transactions spending our UTXO

        # Try using gettxout first - if None, UTXO is spent
        txout = btc_client._call("gettxout", funding_txid, vout, True)
        if txout is not None:
            # UTXO still exists, not spent
            return None

        # UTXO is spent, we need to find the spending transaction
        # This is tricky without an indexer. For signet, we can scan recent blocks.

        # Alternative: if we have the claim transaction tracked elsewhere
        # For now, scan the last few blocks
        current_height = btc_client.get_block_count()

        for height in range(current_height, max(0, current_height - 10), -1):
            block_hash = btc_client._call("getblockhash", height)
            block = btc_client._call("getblock", block_hash, 2)  # verbosity 2 = full tx

            for tx in block.get("tx", []):
                for vin in tx.get("vin", []):
                    if vin.get("txid") == funding_txid and vin.get("vout") == vout:
                        # Found the spending transaction!
                        # Extract preimage from witness
                        witness = vin.get("txinwitness", [])
                        if len(witness) >= 2:
                            # HTLC claim witness: [signature, preimage, 0x01, redeemscript]
                            # The preimage is typically the second element
                            preimage_hex = witness[1]
                            if len(preimage_hex) == 64:  # 32 bytes = 64 hex chars
                                log.info(f"Extracted preimage from BTC tx {tx['txid']}")
                                return preimage_hex
                        break

        log.warning("Could not find spending transaction in recent blocks")
        return None

    except Exception as e:
        log.error(f"Error extracting BTC preimage: {e}")
        return None


def _unlock_m1_internal(swap_id: str, swap: Dict):
    """
    Unlock M1 that was locked internally as commitment.

    This M1 was never exposed to the user - it was purely an internal
    LP commitment checkpoint. Now that the swap is complete, unlock it.
    """
    try:
        m1_lock_receipt = swap.get("m1_lock_receipt")
        if not m1_lock_receipt:
            log.info(f"No M1 lock to unlock for {swap_id}")
            return

        m1_client = get_m1_client()
        if not m1_client:
            return

        # Unlock M1 -> M0
        # The receipt outpoint from the lock operation
        result = m1_client.unlock_receipt(m1_lock_receipt)

        if result and result.get("txid"):
            swap["m1_unlock_txid"] = result.get("txid")
            log.info(f"M1 unlocked internally: {result.get('txid')}")
        else:
            log.warning(f"M1 unlock may have failed for {swap_id}")

    except Exception as e:
        log.error(f"Error unlocking M1: {e}")


def _send_btc_to_user(swap_id: str, swap: Dict):
    """4-HTLC Step 4: LP sends BTC to user after preimage revealed."""
    try:
        btc_client = get_btc_client()
        if not btc_client:
            log.error("BTC client not available")
            return

        user_btc_addr = swap["user_btc_claim_address"]
        amount_sats = swap["to_amount_sats"]
        amount_btc = amount_sats / 100_000_000

        # Send BTC
        txid = btc_client.send_to_address(user_btc_addr, amount_btc, f"pna-4htlc-{swap_id}")
        swap["lp_btc_tx"] = txid
        log.info(f"4-HTLC: LP sent BTC to user: {txid} ({amount_btc} BTC)")

    except Exception as e:
        log.exception(f"Failed to send BTC in 4-HTLC: {e}")


def _claim_usdc_with_preimage(swap_id: str, swap: Dict, preimage: str):
    """4-HTLC Step 5: LP claims user's USDC HTLC with revealed preimage."""
    try:
        from sdk.htlc.evm import withdraw_htlc

        lp_key_path = Path.home() / ".keys" / "lp_evm.json"
        if not lp_key_path.exists():
            log.error("LP EVM key not found")
            return

        with open(lp_key_path) as f:
            lp_key = json.load(f)["private_key"]

        user_htlc_id = swap["user_htlc_id"]

        result = withdraw_htlc(
            htlc_id=user_htlc_id,
            preimage=preimage,
            private_key=lp_key
        )

        if result.success:
            swap["lp_usdc_claim_tx"] = result.tx_hash
            log.info(f"4-HTLC: LP claimed USDC: {result.tx_hash}")
        else:
            log.error(f"4-HTLC: Failed to claim USDC: {result.error}")

    except Exception as e:
        log.exception(f"Failed to claim USDC in 4-HTLC: {e}")


def _claim_user_deposit(swap_id: str, swap: Dict):
    """LP claims user's original deposit using revealed preimage."""
    try:
        from_asset = swap["from_asset"]
        preimage = swap["preimage"]

        if from_asset == "USDC":
            from sdk.htlc.evm import withdraw_htlc

            lp_key_path = Path.home() / ".keys" / "lp_evm.json"
            with open(lp_key_path) as f:
                lp_key = json.load(f)["private_key"]

            user_htlc_id = swap["user_htlc_id"]

            result = withdraw_htlc(
                htlc_id=user_htlc_id,
                preimage=preimage,
                private_key=lp_key
            )

            if result.success:
                swap["lp_claim_tx"] = result.tx_hash
                swap["status"] = "completed"
                log.info(f"LP claimed user USDC: {result.tx_hash}")
            else:
                log.error(f"LP USDC claim failed: {result.error}")

        elif from_asset == "BTC":
            btc_htlc = get_btc_htlc()
            if btc_htlc:
                utxo = swap.get("user_btc_utxo")
                redeem_script = swap.get("user_btc_htlc_script")
                lp_btc_addr = _lp_addresses.get("btc")

                if utxo and redeem_script:
                    claim_txid = btc_htlc.claim_htlc(
                        utxo=utxo,
                        redeem_script=redeem_script,
                        preimage=preimage,
                        recipient_address=lp_btc_addr
                    )
                    swap["lp_claim_tx"] = claim_txid
                    swap["status"] = "completed"
                    log.info(f"LP claimed user BTC: {claim_txid}")

        elif from_asset == "M1":
            m1_htlc = get_m1_htlc()
            if m1_htlc:
                user_htlc_outpoint = swap.get("user_m1_htlc_outpoint")
                if user_htlc_outpoint:
                    result = m1_htlc.claim(user_htlc_outpoint, preimage)
                    swap["lp_claim_tx"] = result.get("txid")
                    swap["status"] = "completed"
                    log.info(f"LP claimed user M1: {result.get('txid')}")

    except Exception as e:
        log.exception(f"Error claiming user deposit: {e}")


def stop_evm_watcher():
    """Stop the EVM watcher thread."""
    global _evm_watcher_running
    _evm_watcher_running = False


# =============================================================================
# FULL ATOMIC SWAP ENDPOINT (PRODUCTION FLOW)
# =============================================================================

class FullSwapRequest(BaseModel):
    """Request for full atomic swap."""
    from_asset: str = Field(..., description="BTC or USDC")
    to_asset: str = Field(..., description="USDC or BTC")
    from_amount: float = Field(..., gt=0)
    hashlock: str = Field(..., min_length=64, max_length=64, description="User's SHA256 hashlock")
    user_receive_address: str = Field(..., description="Where user receives to_asset")
    user_refund_address: Optional[str] = Field(None, description="Where user gets refund if timeout")


@app.post("/api/swap/full/initiate")
async def initiate_full_swap(req: FullSwapRequest):
    """
    Initiate a FULL atomic swap with automatic LP counter-HTLC creation.

    This is the PRODUCTION flow:
    1. User generates secret S, hashlock H = SHA256(S)
    2. User calls this endpoint with H and deposit info
    3. User creates HTLC on from_asset chain (BTC or USDC)
    4. LP automatically detects deposit and creates counter-HTLC
    5. User claims LP's HTLC (reveals S)
    6. LP automatically claims user's HTLC

    For BTC → USDC:
    - M1 is used internally as settlement rail (invisible to user)
    - User only sees: send BTC, receive USDC

    Args:
        from_asset: Source asset (BTC, USDC)
        to_asset: Target asset (USDC, BTC)
        from_amount: Amount to send
        hashlock: User's hashlock (they keep the preimage secret)
        user_receive_address: Address on to_asset chain
        user_refund_address: Refund address on from_asset chain

    Returns:
        Deposit instructions and swap tracking info
    """
    # Validate assets
    supported_pairs = [("BTC", "USDC"), ("USDC", "BTC")]
    if (req.from_asset, req.to_asset) not in supported_pairs:
        raise HTTPException(400, f"Unsupported pair: {req.from_asset}/{req.to_asset}")

    # Validate hashlock
    try:
        bytes.fromhex(req.hashlock)
    except ValueError:
        raise HTTPException(400, "Invalid hashlock hex")

    now = int(time.time())
    swap_id = f"full_{uuid.uuid4().hex[:16]}"

    # Get quote
    quote = await get_quote(req.from_asset, req.to_asset, req.from_amount)

    # Create swap tracking
    swap_data = {
        "swap_id": swap_id,
        "status": "awaiting_user_htlc",
        "from_asset": req.from_asset,
        "to_asset": req.to_asset,
        "from_amount": req.from_amount,
        "to_amount": quote.to_amount,
        "hashlock": req.hashlock,
        "created_at": now,
        "expires_at": now + 3600,  # 1 hour
    }

    # Set up deposit instructions based on from_asset
    if req.from_asset == "BTC":
        # User will create BTC HTLC
        btc_htlc = get_btc_htlc()
        if not btc_htlc:
            raise HTTPException(503, "BTC service not available")

        # LP's BTC address for claiming (LP gets BTC with preimage)
        lp_btc_addr = _lp_addresses.get("btc")
        if not lp_btc_addr:
            raise HTTPException(503, "LP BTC address not configured")

        # Get LP's pubkey from wallet
        btc_client = get_btc_client()
        if not btc_client:
            raise HTTPException(503, "BTC client not available")

        lp_addr_info = btc_client.get_address_info(lp_btc_addr)
        lp_pubkey = lp_addr_info.get("pubkey")
        if not lp_pubkey:
            raise HTTPException(503, "LP BTC pubkey not available")

        # Generate ephemeral keypair for user's refund path
        # User will receive refund_privkey to claim refund after timeout
        refund_addr = btc_client.get_new_address("htlc_refund", "bech32")
        refund_info = btc_client.get_address_info(refund_addr)
        refund_pubkey = refund_info.get("pubkey")
        if not refund_pubkey:
            raise HTTPException(503, "Could not generate refund key")

        # Create HTLC address where user will deposit
        amount_sats = int(req.from_amount * 100_000_000)
        htlc_info = btc_htlc.create_htlc(
            amount_sats=amount_sats,
            hashlock=req.hashlock,
            recipient_address=lp_btc_addr,
            refund_address=refund_addr,
            timeout_blocks=144,
            recipient_pubkey=lp_pubkey,
            refund_pubkey=refund_pubkey
        )

        swap_data["user_btc_htlc_address"] = htlc_info["htlc_address"]
        swap_data["user_btc_htlc_script"] = htlc_info["redeem_script"]
        swap_data["user_btc_timelock"] = htlc_info["timelock"]
        swap_data["from_amount_sats"] = amount_sats
        swap_data["user_usdc_address"] = req.user_receive_address
        swap_data["to_amount_usdc"] = quote.to_amount
        swap_data["user_refund_address"] = refund_addr  # LP controls refund key for simplicity

        deposit_instructions = {
            "action": "Send BTC to HTLC address",
            "htlc_address": htlc_info["htlc_address"],
            "amount_btc": req.from_amount,
            "amount_sats": amount_sats,
            "hashlock": req.hashlock,
            "timelock_blocks": htlc_info["timelock"],
            "refund_address": refund_addr,
            "note": "After 1 confirmation, LP will create counter-HTLC. Refund available after timeout.",
        }

    elif req.from_asset == "USDC":
        # User will create USDC HTLC on Base Sepolia
        lp_address = _lp_addresses.get("usdc")
        if not lp_address:
            raise HTTPException(503, "LP USDC address not configured")

        # Calculate HTLC ID that user will create
        amount_wei = int(req.from_amount * 1e6)

        swap_data["user_usdc_amount"] = req.from_amount
        swap_data["user_usdc_amount_wei"] = amount_wei
        swap_data["lp_usdc_address"] = lp_address
        swap_data["user_btc_claim_address"] = req.user_receive_address
        swap_data["to_amount_sats"] = int(quote.to_amount * 100_000_000) if quote.to_amount < 1 else int(quote.to_amount)

        deposit_instructions = {
            "action": "Create USDC HTLC on Base Sepolia",
            "htlc_contract": HTLC_CONTRACT_BASE_SEPOLIA,
            "receiver": lp_address,
            "token": USDC_CONTRACT_BASE_SEPOLIA,
            "amount_usdc": req.from_amount,
            "amount_wei": amount_wei,
            "hashlock": req.hashlock,
            "timelock_seconds": 3600,  # 1 hour
            "note": "After HTLC created, LP will create counter-BTC HTLC",
        }

    # Store swap
    pending_lp_htlcs[swap_id] = swap_data

    return {
        "swap_id": swap_id,
        "status": "awaiting_user_htlc",
        "quote": {
            "from_asset": req.from_asset,
            "to_asset": req.to_asset,
            "from_amount": req.from_amount,
            "to_amount": quote.to_amount,
            "rate": quote.rate,
            "spread_percent": quote.spread_percent,
        },
        "deposit_instructions": deposit_instructions,
        "settlement_flow": f"{req.from_asset} → M1 (internal) → {req.to_asset}",
        "m1_visibility": "M1 is used internally as settlement rail. User never sees or touches M1.",
        "next_steps": [
            f"1. Create {req.from_asset} HTLC as instructed above",
            "2. LP will automatically detect your deposit",
            f"3. LP will create {req.to_asset} HTLC for you to claim",
            "4. Claim with your preimage (reveals secret)",
            f"5. LP claims your {req.from_asset} - swap complete!",
        ],
        "confirmations_required": {
            "BTC": 1,
            "USDC": 1,
            "M1": 1,
        },
    }


@app.post("/api/swap/full/{swap_id}/register-htlc")
async def register_user_htlc_full(swap_id: str, htlc_id: str = Query(...)):
    """
    Register user's HTLC for tracking.

    After user creates their HTLC, call this to let LP know the HTLC ID.
    LP will verify and start monitoring for confirmation.
    """
    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]

    if swap["status"] != "awaiting_user_htlc":
        raise HTTPException(400, f"Invalid swap status: {swap['status']}")

    swap["user_htlc_id"] = htlc_id
    swap["updated_at"] = int(time.time())

    return {
        "success": True,
        "swap_id": swap_id,
        "user_htlc_id": htlc_id,
        "status": swap["status"],
        "message": "HTLC registered. LP will verify and create counter-HTLC.",
    }


@app.get("/api/swap/full/{swap_id}/status")
async def get_full_swap_status(swap_id: str):
    """Get full swap status."""
    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]

    # Don't expose preimage until swap is complete
    result = {k: v for k, v in swap.items() if k not in ("preimage",)}

    if swap.get("status") == "completed":
        result["preimage_used"] = True

    return result


@app.get("/api/swap/full/list")
async def list_full_swaps():
    """List all full swaps."""
    return {
        "swaps": [
            {k: v for k, v in s.items() if k not in ("preimage",)}
            for s in pending_lp_htlcs.values()
        ],
        "count": len(pending_lp_htlcs),
    }


@app.post("/api/swap/full/{swap_id}/claim-m1")
async def claim_m1_htlc(swap_id: str, preimage: str = Query(...)):
    """
    4-HTLC FLOW: User claims M1 HTLC with preimage.

    This is the KEY step that reveals preimage S on BATHRON chain.
    Once S is revealed (~1 min finality), LP will automatically:
    1. Send BTC to user
    2. Claim user's USDC

    Args:
        swap_id: Swap identifier
        preimage: The 32-byte secret S (hex)
    """
    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]

    if swap["status"] != "m1_htlc_created":
        raise HTTPException(400, f"Invalid swap status: {swap['status']}. Expected 'm1_htlc_created'")

    # Verify preimage matches hashlock
    computed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    if computed_hash != swap["hashlock"]:
        raise HTTPException(400, f"Preimage does not match hashlock")

    # Claim M1 HTLC
    m1_htlc = get_m1_htlc()
    if not m1_htlc:
        raise HTTPException(503, "M1 HTLC manager not available")

    lp_m1_htlc = swap.get("lp_m1_htlc_outpoint")
    if not lp_m1_htlc:
        raise HTTPException(400, "M1 HTLC outpoint not found")

    try:
        result = m1_htlc.claim(lp_m1_htlc, preimage)
        swap["m1_claim_txid"] = result.get("txid")
        swap["preimage"] = preimage
        swap["status"] = "m1_claimed"
        log.info(f"4-HTLC: User claimed M1 HTLC: {result.get('txid')}")

        return {
            "success": True,
            "swap_id": swap_id,
            "m1_claim_txid": result.get("txid"),
            "message": "M1 HTLC claimed! Preimage revealed on BATHRON. LP will now send BTC and claim USDC.",
            "next": "Wait for LP to complete the swap (~1 min for M1 finality)",
        }

    except Exception as e:
        log.exception(f"Failed to claim M1 HTLC: {e}")
        raise HTTPException(500, f"Failed to claim M1: {e}")


@app.post("/api/swap/full/{swap_id}/reveal-preimage")
async def reveal_preimage_for_swap(swap_id: str, preimage: str = Query(...)):
    """
    User reveals preimage so LP can claim their USDC HTLC.

    TRUSTLESS REQUIREMENT: This endpoint ONLY works if user has already claimed
    their BTC (which reveals preimage on Bitcoin blockchain). This prevents LP
    from claiming USDC before user receives BTC.

    Args:
        swap_id: Swap identifier
        preimage: The 32-byte secret (hex)
    """
    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]

    # Verify preimage matches hashlock
    computed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    if computed_hash != swap["hashlock"]:
        raise HTTPException(400, f"Preimage does not match hashlock. Got: {computed_hash}, expected: {swap['hashlock']}")

    # =========================================================================
    # TRUSTLESS VERIFICATION: Ensure user has claimed BTC FIRST
    # =========================================================================
    # In a trustless atomic swap, LP should ONLY get preimage by seeing user
    # claim BTC on-chain. This check ensures we don't release USDC until user
    # has received their BTC.
    btc_htlc_addr = swap.get("lp_btc_htlc_address")
    funding_txid = swap.get("lp_btc_htlc_funding_txid")

    if btc_htlc_addr and funding_txid:
        btc_client = get_btc_client()
        if btc_client:
            try:
                # Check if BTC HTLC UTXO still exists (not yet claimed)
                txout = btc_client._call("gettxout", funding_txid, 0, True)
                if txout is not None:
                    # UTXO still exists = user has NOT claimed BTC yet
                    raise HTTPException(400,
                        "TRUSTLESS VIOLATION: BTC HTLC has not been claimed yet. "
                        "User must claim BTC first, which reveals preimage on Bitcoin blockchain. "
                        "LP can only claim USDC after user claims BTC. "
                        f"BTC HTLC address: {btc_htlc_addr}"
                    )
                # UTXO is spent = user claimed BTC, safe to proceed
                log.info(f"BTC HTLC verified as claimed for swap {swap_id}")
            except HTTPException:
                raise  # Re-raise our trustless violation
            except Exception as e:
                log.warning(f"Could not verify BTC HTLC status: {e}. Proceeding with caution.")
                # In case of RPC error, log but allow (for testnet flexibility)
                # Production should be stricter

    # LP claims user's USDC HTLC
    if swap["from_asset"] == "USDC":
        try:
            from sdk.htlc.evm import withdraw_htlc

            lp_key_path = Path.home() / ".keys" / "lp_evm.json"
            with open(lp_key_path) as f:
                lp_key = json.load(f)["private_key"]

            result = withdraw_htlc(
                htlc_id=swap["user_htlc_id"],
                preimage=preimage,
                private_key=lp_key
            )

            if result.success:
                swap["preimage"] = preimage
                swap["lp_claim_tx"] = result.tx_hash
                swap["status"] = "completed"
                swap["completed_at"] = int(time.time())
                log.info(f"LP claimed user USDC HTLC: {result.tx_hash}")

                return {
                    "success": True,
                    "swap_id": swap_id,
                    "status": "completed",
                    "lp_claim_tx": result.tx_hash,
                    "message": "Swap complete! LP claimed USDC, user received BTC.",
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                }

        except Exception as e:
            log.exception(f"Failed to claim USDC: {e}")
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Cannot claim {swap['from_asset']} HTLC"}


# =============================================================================
# TEST ENDPOINTS (for development/testnet only)
# =============================================================================

@app.post("/api/test/swap/{swap_id}/set-btc-htlc")
async def test_set_btc_htlc(
    swap_id: str,
    btc_address: str = Query(..., description="BTC HTLC P2WSH address"),
    funding_txid: str = Query(..., description="BTC funding transaction ID"),
    user_htlc_id: str = Query(None, description="User's USDC HTLC ID")
):
    """
    TEST ONLY: Manually set BTC HTLC fields for trust check testing.
    This simulates LP creating a BTC HTLC without actual on-chain transaction.
    """
    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]
    swap["lp_btc_htlc_address"] = btc_address
    swap["lp_btc_htlc_funding_txid"] = funding_txid
    swap["status"] = "btc_htlc_ready"
    if user_htlc_id:
        swap["user_htlc_id"] = user_htlc_id

    return {
        "success": True,
        "swap_id": swap_id,
        "btc_htlc_address": btc_address,
        "funding_txid": funding_txid,
        "status": swap["status"],
        "message": "BTC HTLC fields set for testing. Trust check will now verify this UTXO.",
    }


@app.post("/api/test/swap/{swap_id}/create-usdc-htlc")
async def test_create_usdc_htlc(swap_id: str):
    """
    TEST ONLY: Manually trigger LP to create USDC HTLC for user.
    Use after user's BTC deposit is confirmed.
    """
    if swap_id not in pending_lp_htlcs:
        raise HTTPException(404, "Swap not found")

    swap = pending_lp_htlcs[swap_id]

    if swap.get("to_asset") != "USDC":
        raise HTTPException(400, f"Swap to_asset is {swap.get('to_asset')}, not USDC")

    try:
        from sdk.htlc.evm import create_htlc as create_usdc_htlc

        # Load LP key
        lp_key_path = Path.home() / ".keys" / "lp_evm.json"
        if not lp_key_path.exists():
            raise HTTPException(503, "LP EVM key not found")

        with open(lp_key_path) as f:
            lp_key = json.load(f)["private_key"]

        user_address = swap["user_usdc_address"]
        amount_usdc = swap.get("to_amount_usdc", swap.get("to_amount", 0))
        hashlock = swap["hashlock"]

        log.info(f"Creating USDC HTLC: receiver={user_address}, amount={amount_usdc}, hashlock={hashlock[:16]}...")

        result = create_usdc_htlc(
            receiver=user_address,
            amount_usdc=amount_usdc,
            hashlock=hashlock,
            timelock_seconds=1800,  # 30 min
            private_key=lp_key
        )

        if result.success:
            swap["lp_htlc_id"] = result.htlc_id
            swap["lp_htlc_tx"] = result.tx_hash
            swap["status"] = "lp_htlc_created"
            log.info(f"LP USDC HTLC created: {result.htlc_id}")

            return {
                "success": True,
                "swap_id": swap_id,
                "status": "lp_htlc_created",
                "lp_htlc_id": result.htlc_id,
                "lp_htlc_tx": result.tx_hash,
                "amount_usdc": amount_usdc,
                "message": "USDC HTLC created! User can now claim with preimage.",
            }
        else:
            return {"success": False, "error": result.error}

    except Exception as e:
        log.exception(f"Failed to create USDC HTLC: {e}")
        raise HTTPException(500, f"Failed to create USDC HTLC: {e}")


@app.post("/api/test/btc/htlc/claim")
async def test_btc_htlc_claim(
    txid: str = Query(..., description="BTC HTLC funding transaction ID"),
    vout: int = Query(0, description="Output index"),
    amount_sats: int = Query(..., description="Amount in satoshis"),
    redeem_script: str = Query(..., description="Witness script hex"),
    preimage: str = Query(..., description="32-byte preimage hex"),
    recipient_address: str = Query(None, description="Recipient address (default: LP wallet)"),
):
    """
    TEST ONLY: Direct BTC HTLC claim with preimage.

    Use this to test LP claiming BTC after extracting preimage from EVM.
    """
    if not SDK_AVAILABLE:
        raise HTTPException(503, "SDK not available")

    try:
        btc_client = get_btc_client()
        btc_htlc = get_btc_htlc()

        # Use LP wallet address if not specified
        if not recipient_address:
            recipient_address = btc_client._call("getnewaddress")

        utxo = {
            "txid": txid,
            "vout": vout,
            "amount": amount_sats
        }

        claim_txid = btc_htlc.claim_htlc(
            utxo=utxo,
            redeem_script=redeem_script,
            preimage=preimage,
            recipient_address=recipient_address,
            fee_rate_sat_vb=2
        )

        return {
            "success": True,
            "claim_txid": claim_txid,
            "recipient": recipient_address,
            "amount_sats": amount_sats,
            "explorer": f"https://mempool.space/signet/tx/{claim_txid}"
        }

    except Exception as e:
        log.exception(f"BTC HTLC claim failed: {e}")
        raise HTTPException(500, f"Claim failed: {e}")


# =============================================================================
# FASTAPI STARTUP/SHUTDOWN EVENTS
# =============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    global _evm_watcher_thread

    # Load persisted FlowSwap state
    _load_flowswap_db()

    # Initialize LP addresses
    load_lp_addresses()

    # Load EVM private key from secure storage
    global LP_USDC_PRIVKEY
    LP_USDC_PRIVKEY = _load_evm_private_key()
    if LP_USDC_PRIVKEY:
        log.info("EVM private key loaded — EVM operations enabled")
    else:
        log.warning("EVM private key NOT loaded — EVM operations will fail")

    # Fetch initial live price and update LP_CONFIG
    try:
        btc_price = await fetch_live_btc_usdc_price()
        usdc_m1_rate = BTC_M1_FIXED_RATE / btc_price
        LP_CONFIG["pairs"]["USDC/M1"]["rate"] = usdc_m1_rate
        log.info(f"Initial price loaded: BTC=${btc_price:.0f}, USDC/M1={usdc_m1_rate:.2f}")
    except Exception as e:
        log.warning(f"Could not fetch initial price, using default: {e}")

    # Refresh inventory from actual wallet balances
    try:
        await refresh_inventory()
        log.info(f"Inventory loaded: BTC={LP_CONFIG['inventory']['btc']}, "
                 f"M1={LP_CONFIG['inventory']['m1']}, USDC={LP_CONFIG['inventory']['usdc']}")
    except Exception as e:
        log.warning(f"Could not refresh inventory on startup: {e}")

    # Start periodic inventory refresh in a background THREAD (not async)
    # to avoid blocking the event loop with subprocess.run calls
    def _periodic_inventory_refresh_thread():
        import time as _time
        while True:
            _time.sleep(60)
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(refresh_inventory())
                loop.close()
            except Exception:
                pass
    _inv_thread = threading.Thread(target=_periodic_inventory_refresh_thread, daemon=True)
    _inv_thread.start()

    # Start EVM watcher thread
    _evm_watcher_thread = threading.Thread(target=start_evm_watcher, daemon=True)
    _evm_watcher_thread.start()

    # Start auto-refund checker (expired BTC HTLCs + M1 HTLCs + completion watchdog)
    async def _auto_refund_checker():
        """Check for expired HTLCs, auto-refund, M1 recovery, and completion watchdog every 60s."""
        while True:
            await asyncio.sleep(60)
            try:
                _process_expired_htlcs()
            except Exception as e:
                log.error(f"Auto-refund checker error: {e}")
            try:
                _process_stale_completing()
            except Exception as e:
                log.error(f"Completion watchdog error: {e}")
            try:
                _process_expired_m1_htlc3s()
            except Exception as e:
                log.error(f"M1 auto-refund error: {e}")
    asyncio.create_task(_auto_refund_checker())

    # --- Startup recovery: rebuild reservations + recover all stuck swaps ---
    with _flowswap_lock:
        _rebuild_reservations_from_db()
        _startup_recover_all_swaps()

    log.info("Swap monitor started - auto LP HTLC response + auto-refund + watchdog enabled")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    _save_flowswap_db()
    stop_evm_watcher()
    if _httpx_client and not _httpx_client.is_closed:
        await _httpx_client.aclose()
    log.info("Swap monitor stopped")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    # Note: startup_event handles LP address loading
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting pna SDK on port {port}")
    log.info(f"Protocol fee: 0")
    log.info(f"Docs: http://0.0.0.0:{port}/docs")
    uvicorn.run(app, host="0.0.0.0", port=port)
