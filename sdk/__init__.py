"""
pna SDK - Trustless Cross-Chain Swap Library

Enables atomic swaps between BTC, M1, and EVM chains using HTLCs.
M1 serves as the settlement rail with ~1 min finality.

Usage:
    from sdk import SwapExecutor, BTCClient, M1Client
    from sdk import generate_secret, verify_preimage

    # Initialize clients
    btc = BTCClient(config)
    m1 = M1Client(config)

    # Create executor
    executor = SwapExecutor(btc, m1)

    # Get quote and execute
    quote = executor.get_quote("BTC", "M1", 100000)  # sats
    swap = executor.initiate_swap(quote, user_addr, lp_addr)
"""

from .core import (
    SwapState,
    SwapDirection,
    HTLCParams,
    SwapQuote,
    SwapResult,
    generate_secret,
    verify_preimage,
    btc_to_sats,
    sats_to_btc,
    BTC_M1_RATE,
)

from .chains.btc import BTCClient, BTCConfig
from .chains.m1 import M1Client, M1Config
from .chains.evm import EVMClient, EVMConfig

from .htlc.btc import BTCHtlc
from .htlc.m1 import M1Htlc

from .swap.executor import SwapExecutor, SwapConfig, ActiveSwap
from .swap.watcher import SwapWatcher, SwapMonitor, WatcherConfig

__version__ = "0.1.0"
__all__ = [
    # Core types
    "SwapState",
    "SwapDirection",
    "HTLCParams",
    "SwapQuote",
    "SwapResult",
    # Utilities
    "generate_secret",
    "verify_preimage",
    "btc_to_sats",
    "sats_to_btc",
    "BTC_M1_RATE",
    # Clients
    "BTCClient",
    "BTCConfig",
    "M1Client",
    "M1Config",
    "EVMClient",
    "EVMConfig",
    # HTLC
    "BTCHtlc",
    "M1Htlc",
    # Swap
    "SwapExecutor",
    "SwapConfig",
    "ActiveSwap",
    "SwapWatcher",
    "SwapMonitor",
    "WatcherConfig",
]
