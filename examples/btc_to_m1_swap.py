#!/usr/bin/env python3
"""
Example: BTC -> M1 Atomic Swap

This demonstrates the full swap flow from the LP perspective:

1. User requests quote for BTC -> M1
2. LP generates HTLC with deposit address
3. User sends BTC to deposit address
4. LP confirms deposit, creates M1 HTLC
5. User claims M1 (reveals preimage)
6. LP claims BTC using revealed preimage

Usage:
    python btc_to_m1_swap.py
"""

import sys
import time
import logging
from pathlib import Path

# Add SDK to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sdk.core import btc_to_sats, sats_to_btc
from sdk.chains.btc import BTCClient, BTCConfig
from sdk.chains.m1 import M1Client, M1Config
from sdk.swap.executor import SwapExecutor, SwapConfig
from sdk.swap.watcher import SwapWatcher, WatcherConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)


def main():
    # =================================================================
    # 1. Initialize clients
    # =================================================================
    log.info("Initializing chain clients...")

    btc_config = BTCConfig(
        network="signet",
        cli_path=Path.home() / "bitcoin" / "bin" / "bitcoin-cli",
    )

    m1_config = M1Config(
        network="testnet",
        rpc_host="127.0.0.1",
        rpc_port=27172,
        rpc_user="lprpc",
        rpc_password="lppass2026",
        cli_path=Path.home() / "BATHRON" / "src" / "bathron-cli",
    )

    btc_client = BTCClient(btc_config)
    m1_client = M1Client(m1_config)

    # =================================================================
    # 2. Initialize swap executor
    # =================================================================
    log.info("Initializing swap executor...")

    swap_config = SwapConfig(
        spread_btc_m1_bid=0.5,  # 0.5% spread
        btc_confirmations=1,    # Fast for testing
        btc_timeout_blocks=6,   # Short timeout for testing
        m1_timeout_blocks=10,
    )

    executor = SwapExecutor(btc_client, m1_client, config=swap_config)

    # =================================================================
    # 3. Get quote
    # =================================================================
    amount_btc = 0.001  # 0.001 BTC = 100,000 sats
    amount_sats = btc_to_sats(amount_btc)

    log.info(f"Getting quote for {amount_btc} BTC -> M1...")

    quote = executor.get_quote("BTC", "M1", amount_sats)

    log.info(f"Quote received:")
    log.info(f"  From: {sats_to_btc(quote.from_amount)} BTC ({quote.from_amount} sats)")
    log.info(f"  To: {quote.to_amount} M1")
    log.info(f"  Rate: {quote.rate} (with {quote.spread_percent}% spread)")
    log.info(f"  Route: {quote.route}")
    log.info(f"  Valid until: {quote.expires_at}")

    # =================================================================
    # 4. Initiate swap
    # =================================================================
    # In production, these would be user-provided addresses
    user_m1_address = "yUserM1AddressHere..."  # Where user receives M1
    lp_btc_address = "tb1qLPAddressHere..."    # LP's BTC address for claiming

    log.info("Initiating swap...")

    # Note: This would fail without real addresses
    # swap = executor.initiate_swap(quote, user_m1_address, lp_btc_address)

    log.info("Swap would be initiated with:")
    log.info(f"  - User sends BTC to: [HTLC address would be generated]")
    log.info(f"  - User claims M1 from: [M1 HTLC would be created]")
    log.info(f"  - Hashlock: [Would be generated]")

    # =================================================================
    # 5. Monitor for completion (in production)
    # =================================================================
    log.info("")
    log.info("In production, the watcher would:")
    log.info("  1. Monitor BTC for deposit confirmation")
    log.info("  2. Create M1 HTLC after BTC confirmed")
    log.info("  3. Watch for user's M1 claim (preimage reveal)")
    log.info("  4. Auto-claim BTC using revealed preimage")

    # Example watcher setup:
    # watcher = SwapWatcher(executor)
    # watcher.on_deposit_confirmed = lambda s: log.info(f"Deposit confirmed: {s.swap_id}")
    # watcher.on_user_claimed = lambda s, p: log.info(f"User claimed, preimage: {p}")
    # watcher.start()

    log.info("")
    log.info("✓ SDK initialized successfully!")
    log.info("")
    log.info("To perform real swaps:")
    log.info("  1. Ensure BTC Signet node is running and synced")
    log.info("  2. Ensure BATHRON testnet node is running")
    log.info("  3. Fund LP wallets with BTC and M1")
    log.info("  4. Use the server.py API or call executor directly")


if __name__ == "__main__":
    main()
