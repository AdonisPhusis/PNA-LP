#!/usr/bin/env python3
"""
Atomic USDC Claim - Gated on BTC witness reveal.

This is the PROPER way to claim USDC:
1. Wait for BTC claim transaction
2. Extract secrets from BTC witness
3. Only THEN call EVM claim

NEVER claim using secrets from a file - that breaks atomicity!
"""

import json
import os
import time
from typing import Optional
from web3 import Web3
from eth_account import Account

# Import the watcher
from btc_witness_watcher import BTCWitnessWatcher, RevealedSecrets, gate_evm_claim, RevealSource


# EVM Config
RPC_URL = "https://sepolia.base.org"
CHAIN_ID = 84532
HTLC3S_ADDRESS = "0x667E9bDC368F0aC2abff69F5963714e3656d2d9D"

HTLC3S_ABI = [
    {"inputs":[
        {"name":"id","type":"bytes32"},
        {"name":"secretUser","type":"bytes32"},
        {"name":"secretLp1","type":"bytes32"},
        {"name":"secretLp2","type":"bytes32"}
    ],"name":"claim","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"id","type":"bytes32"}],
     "name":"swaps","outputs":[
        {"name":"sender","type":"address"},
        {"name":"recipient","type":"address"},
        {"name":"token","type":"address"},
        {"name":"amount","type":"uint256"},
        {"name":"hashUser","type":"bytes32"},
        {"name":"hashLp1","type":"bytes32"},
        {"name":"hashLp2","type":"bytes32"},
        {"name":"timelock","type":"uint256"},
        {"name":"claimed","type":"bool"},
        {"name":"refunded","type":"bool"}
    ],"stateMutability":"view","type":"function"}
]


def claim_usdc_atomic(
    swap_id: bytes,
    revealed: RevealedSecrets,
    claimer_private_key: str
) -> Optional[str]:
    """
    Claim USDC from HTLC3S using secrets revealed on BTC chain.

    This function GATES on the reveal source - it will REFUSE to claim
    if secrets didn't come from BTC witness.

    Args:
        swap_id: The HTLC swap ID
        revealed: Secrets extracted from BTC witness
        claimer_private_key: Any wallet can call (permissionless)

    Returns:
        Transaction hash if successful, None if gated/failed
    """
    # CRITICAL: Gate on reveal source
    if not gate_evm_claim(revealed):
        print("❌ CLAIM BLOCKED - Atomicity violation!")
        print("   Secrets must come from BTC chain, not from file/API")
        return None

    print("=" * 60)
    print("ATOMIC USDC CLAIM")
    print("=" * 60)
    print(f"BTC Reveal TX: {revealed.btc_txid}")
    print(f"Reveal Source: {revealed.source.value}")
    print()

    # Connect to EVM
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    account = Account.from_key(claimer_private_key)

    print(f"Claimer: {account.address}")
    print(f"(Permissionless - funds go to fixed recipient)")
    print()

    htlc = w3.eth.contract(address=HTLC3S_ADDRESS, abi=HTLC3S_ABI)

    # Check swap status
    swap_data = htlc.functions.swaps(swap_id).call()
    if swap_data[8]:  # claimed
        print("⚠️  Already claimed!")
        return None

    print(f"HTLC Status:")
    print(f"  Recipient: {swap_data[1]}")
    print(f"  Amount: {swap_data[3] / 10**6} USDC")
    print()

    # Build and send claim transaction
    print("Sending claim transaction...")

    nonce = w3.eth.get_transaction_count(account.address)
    claim_tx = htlc.functions.claim(
        swap_id,
        revealed.s_user,
        revealed.s_lp1,
        revealed.s_lp2
    ).build_transaction({
        'chainId': CHAIN_ID,
        'gas': 200000,
        'gasPrice': w3.eth.gas_price,
        'nonce': nonce,
    })

    signed = w3.eth.account.sign_transaction(claim_tx, claimer_private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"TX: {tx_hash.hex()}")
    print("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt['status'] == 1:
        print()
        print("=" * 60)
        print("✅ USDC CLAIMED (ATOMICALLY)")
        print("=" * 60)
        print(f"EVM TX: https://sepolia.basescan.org/tx/{tx_hash.hex()}")
        print(f"BTC TX: {revealed.btc_txid}")
        print()
        print("Atomicity proven:")
        print("  - Secrets extracted from BTC witness")
        print("  - EVM claim gated on BTC reveal")
        print("  - No secrets were shared off-chain")
        return tx_hash.hex()
    else:
        print("❌ Claim failed!")
        return None


def run_atomic_swap_watcher(
    btc_htlc_address: str,
    h_user: bytes,
    h_lp1: bytes,
    h_lp2: bytes,
    evm_swap_id: bytes,
    claimer_key: str,
    timeout: int = 3600
):
    """
    Run the complete atomic swap watcher.

    This watches BTC for the claim, extracts secrets, and claims EVM.
    """
    print("=" * 60)
    print("ATOMIC SWAP WATCHER")
    print("=" * 60)
    print()
    print(f"BTC HTLC: {btc_htlc_address}")
    print(f"EVM Swap ID: {evm_swap_id.hex()}")
    print()
    print("Waiting for BTC claim transaction...")
    print("(Will extract secrets from witness and claim EVM)")
    print()

    # Initialize watcher
    watcher = BTCWitnessWatcher()

    # Track the HTLC
    watcher.track_htlc(btc_htlc_address, h_user, h_lp1, h_lp2)

    # Wait for reveal
    revealed = watcher.wait_for_reveal(btc_htlc_address, timeout=timeout)

    if revealed:
        print()
        print("Secrets revealed! Proceeding to claim EVM...")
        print()
        return claim_usdc_atomic(evm_swap_id, revealed, claimer_key)
    else:
        print("⏰ Timeout waiting for BTC claim")
        return None


if __name__ == '__main__':
    print("Atomic USDC Claim Module")
    print("=" * 60)
    print()
    print("This module ensures atomicity by:")
    print("1. Only accepting secrets from BTC witness")
    print("2. Gating EVM claim on BTC reveal")
    print()
    print("Usage:")
    print("  from atomic_claim import run_atomic_swap_watcher")
    print("  run_atomic_swap_watcher(btc_addr, h_user, h_lp1, h_lp2, evm_id, key)")
