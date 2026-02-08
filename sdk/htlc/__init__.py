"""
HTLC (Hash Time-Locked Contract) implementations for each chain.

HTLCs enable trustless atomic swaps by ensuring:
1. Funds can only be claimed with knowledge of a secret (preimage)
2. Funds can be refunded after a timeout if not claimed

Each chain has its own HTLC implementation:
- BTC: Native Bitcoin Script (P2WSH)
- M1: BATHRON native via RPC
- EVM: Solidity smart contract

FlowSwap 3-Secret Support:
- btc_3s: BTC HTLC with 3 independent hashlocks
- evm_3s: EVM HTLC3S contract for Base Sepolia
- M1: Native HTLC3S via bathron-cli htlc3s_* commands
"""

from .btc import BTCHtlc
from .m1 import M1Htlc
from .m1_3s import M1Htlc3S, M1HTLC3SRecord
from .btc_3s import BTCHTLC3S, HTLC3SParams, HTLC3SSecrets, create_3s_hashlocks, verify_3s_secrets
from .evm_3s import EVMHTLC3S

__all__ = [
    # Standard 1-secret HTLCs
    "BTCHtlc",
    "M1Htlc",
    # FlowSwap 3-secret HTLCs
    "M1Htlc3S",
    "M1HTLC3SRecord",
    "BTCHTLC3S",
    "HTLC3SParams",
    "HTLC3SSecrets",
    "create_3s_hashlocks",
    "verify_3s_secrets",
    "EVMHTLC3S",
]
