"""
Core types and interfaces for pna SDK.
"""

import hashlib
import secrets
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime


class SwapState(Enum):
    """Swap lifecycle states."""
    CREATED = "created"                 # Swap initiated, waiting for deposit
    DEPOSIT_PENDING = "deposit_pending" # Deposit TX seen, waiting confirmations
    DEPOSIT_CONFIRMED = "deposit_confirmed"  # Deposit confirmed
    HTLC_CREATED = "htlc_created"       # LP created counter-HTLC
    CLAIMED = "claimed"                 # User claimed, preimage revealed
    COMPLETED = "completed"             # LP claimed original deposit
    REFUNDED = "refunded"               # Timeout, funds returned
    FAILED = "failed"                   # Error state


class SwapDirection(Enum):
    """Swap direction relative to M1 rail."""
    TO_M1 = "to_m1"       # BTC/USDC -> M1
    FROM_M1 = "from_m1"   # M1 -> BTC/USDC
    THROUGH_M1 = "through_m1"  # BTC <-> USDC (via M1)


@dataclass
class HTLCParams:
    """HTLC parameters for any chain."""
    hashlock: str           # SHA256 hash (hex, 64 chars)
    timelock: int           # Absolute block height or timestamp
    amount: int             # Amount in smallest unit (sats, wei, etc.)
    recipient: str          # Who can claim with preimage
    refund_address: str     # Who can refund after timeout

    # Optional metadata
    chain: str = ""
    created_at: Optional[datetime] = None
    htlc_address: Optional[str] = None
    htlc_script: Optional[str] = None
    funding_txid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hashlock": self.hashlock,
            "timelock": self.timelock,
            "amount": self.amount,
            "recipient": self.recipient,
            "refund_address": self.refund_address,
            "chain": self.chain,
            "htlc_address": self.htlc_address,
            "funding_txid": self.funding_txid,
        }


@dataclass
class SwapQuote:
    """Quote for a swap."""
    quote_id: str
    from_asset: str
    to_asset: str
    from_amount: int        # In smallest units
    to_amount: int          # In smallest units
    rate: float             # Effective rate
    spread_percent: float
    route: str              # e.g., "BTC -> M1 -> USDC"
    expires_at: int         # Unix timestamp
    lp_id: str

    # Confirmation requirements
    confirmations_required: int = 1

    def is_valid(self) -> bool:
        """Check if quote is still valid."""
        import time
        return time.time() < self.expires_at


@dataclass
class SwapResult:
    """Result of a swap operation."""
    swap_id: str
    state: SwapState
    from_asset: str
    to_asset: str
    from_amount: int
    to_amount: int

    # HTLC info
    hashlock: str
    deposit_address: str
    deposit_txid: Optional[str] = None
    claim_txid: Optional[str] = None

    # Timing
    created_at: int = 0
    completed_at: Optional[int] = None

    # Error info
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "swap_id": self.swap_id,
            "state": self.state.value,
            "from_asset": self.from_asset,
            "to_asset": self.to_asset,
            "from_amount": self.from_amount,
            "to_amount": self.to_amount,
            "hashlock": self.hashlock,
            "deposit_address": self.deposit_address,
            "deposit_txid": self.deposit_txid,
            "claim_txid": self.claim_txid,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


# =============================================================================
# HTLC Utilities
# =============================================================================

def generate_secret() -> tuple[str, str]:
    """
    Generate a random secret and its SHA256 hashlock.

    Returns:
        (secret_hex, hashlock_hex)
    """
    secret = secrets.token_bytes(32)
    hashlock = hashlib.sha256(secret).digest()
    return secret.hex(), hashlock.hex()


def verify_preimage(preimage_hex: str, hashlock_hex: str) -> bool:
    """
    Verify that SHA256(preimage) == hashlock.

    Args:
        preimage_hex: 32-byte preimage as hex string
        hashlock_hex: Expected SHA256 hash as hex string

    Returns:
        True if valid
    """
    try:
        preimage = bytes.fromhex(preimage_hex)
        expected = bytes.fromhex(hashlock_hex)
        actual = hashlib.sha256(preimage).digest()
        return actual == expected
    except (ValueError, TypeError):
        return False


def sats_to_btc(sats: int) -> float:
    """Convert satoshis to BTC."""
    return sats / 100_000_000


def btc_to_sats(btc: float) -> int:
    """Convert BTC to satoshis."""
    return int(btc * 100_000_000)


def m1_to_display(m1: int) -> str:
    """Format M1 for display (M1 = sats, same unit)."""
    return f"{m1:,}"


# =============================================================================
# Constants
# =============================================================================

# Fixed rate: 1 SAT = 1 M1
BTC_M1_RATE = 100_000_000  # M1 per BTC

# Default HTLC timeouts (in blocks)
HTLC_TIMEOUT_BTC = 144      # ~24 hours (10 min blocks)
HTLC_TIMEOUT_M1 = 1440      # ~24 hours (1 min blocks)
HTLC_TIMEOUT_EVM = 7200     # ~24 hours (12 sec blocks)

# Minimum confirmations by default
MIN_CONFIRMATIONS = {
    "BTC": 1,
    "M1": 1,
    "USDC": 1,
}


# =============================================================================
# FlowSwap 3-Secret Protocol
# =============================================================================

class FlowSwapState(Enum):
    """FlowSwap 3-secret swap lifecycle states.

    Anti-grief model: USER COMMITS FIRST.
    /init returns a PLAN only (off-chain). LP locks ONLY after user commit on-chain.

    Forward (BTC→USDC):
      AWAITING_BTC → BTC_FUNDED → LP_LOCKED → BTC_CLAIMED → COMPLETING → COMPLETED
    Reverse (USDC→BTC):
      AWAITING_USDC → USDC_FUNDED → LP_LOCKED → COMPLETING → COMPLETED
    """
    # Forward flow (BTC → USDC)
    AWAITING_BTC = "awaiting_btc"        # /init = PLAN only (no on-chain LP commitment)
    BTC_FUNDED = "btc_funded"            # User BTC detected on-chain, LP locking in progress
    LP_LOCKED = "lp_locked"              # LP HTLCs confirmed on-chain (USDC + M1)
    BTC_CLAIMED = "btc_claimed"          # Presign accepted, LP claimed BTC, secrets revealed
    # Reverse flow (USDC → BTC)
    AWAITING_USDC = "awaiting_usdc"      # /init = PLAN only (no on-chain LP commitment)
    USDC_FUNDED = "usdc_funded"          # User USDC HTLC detected on-chain, LP locking
    # LP_LOCKED reused for reverse direction
    # Per-leg routing (multi-LP)
    AWAITING_M1 = "awaiting_m1"          # LP_OUT waiting for M1 HTLC from LP_IN (per-leg)
    M1_LOCKED = "m1_locked"              # LP_IN locked M1, waiting for LP_OUT to lock USDC
    # Common terminal states
    COMPLETING = "completing"            # Claims in progress
    COMPLETED = "completed"              # All legs settled
    REFUNDED = "refunded"                # Timeout, funds returned
    FAILED = "failed"                    # Unrecoverable error
    EXPIRED = "expired"                  # Plan expired before user committed on-chain


# FlowSwap timelock constants (testnet)
# Forward (BTC→USDC): INVARIANT BTC < M1 < USDC
FLOWSWAP_TIMELOCK_BTC_BLOCKS = 6        # ~1h (6 * 600s = 3600s)
FLOWSWAP_TIMELOCK_M1_BLOCKS = 120       # ~2h (120 * 60s = 7200s)
FLOWSWAP_TIMELOCK_USDC_SECONDS = 14400  # 4h

# Reverse (USDC→BTC): INVARIANT USDC < M1 < BTC
FLOWSWAP_REV_TIMELOCK_USDC_SECONDS = 3600   # 1h (SHORTEST — user locks)
FLOWSWAP_REV_TIMELOCK_M1_BLOCKS = 120       # 2h (MEDIUM)
FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS = 24       # 4h (LONGEST — LP funds)

# Timelock cascade validation (per-leg safety invariant)
# Forward: T_btc < T_m1 < T_usdc (claimer must have time before next refund)
# Reverse: T_usdc < T_m1 < T_btc
# Minimum gap between adjacent legs (seconds):
TIMELOCK_CASCADE_MIN_GAP_SECONDS = 1800  # 30 min minimum gap


def validate_timelock_cascade(direction: str = "forward") -> bool:
    """Validate that timelocks form a strict cascade.

    Forward (BTC→USDC): T_btc_s < T_m1_s < T_usdc_s
    Reverse (USDC→BTC): T_usdc_s < T_m1_s < T_btc_s

    Returns True if valid, raises ValueError if not.
    """
    btc_s = FLOWSWAP_TIMELOCK_BTC_BLOCKS * 600  # BTC block ~10min
    m1_s = FLOWSWAP_TIMELOCK_M1_BLOCKS * 60     # M1 block ~1min
    usdc_s = FLOWSWAP_TIMELOCK_USDC_SECONDS

    if direction == "forward":
        # User claims USDC first, LP claims M1 second, LP claims BTC third
        # So: T_btc < T_m1 < T_usdc (shorter → expires first → claims first)
        if not (btc_s < m1_s < usdc_s):
            raise ValueError(
                f"Forward timelock cascade violated: "
                f"T_btc={btc_s}s, T_m1={m1_s}s, T_usdc={usdc_s}s "
                f"(must be T_btc < T_m1 < T_usdc)"
            )
        if m1_s - btc_s < TIMELOCK_CASCADE_MIN_GAP_SECONDS:
            raise ValueError(
                f"Insufficient gap T_m1 - T_btc: {m1_s - btc_s}s "
                f"(min {TIMELOCK_CASCADE_MIN_GAP_SECONDS}s)"
            )
        if usdc_s - m1_s < TIMELOCK_CASCADE_MIN_GAP_SECONDS:
            raise ValueError(
                f"Insufficient gap T_usdc - T_m1: {usdc_s - m1_s}s "
                f"(min {TIMELOCK_CASCADE_MIN_GAP_SECONDS}s)"
            )
    else:
        # Reverse: USDC < M1 < BTC
        rev_usdc_s = FLOWSWAP_REV_TIMELOCK_USDC_SECONDS
        rev_m1_s = FLOWSWAP_REV_TIMELOCK_M1_BLOCKS * 60
        rev_btc_s = FLOWSWAP_REV_TIMELOCK_BTC_BLOCKS * 600

        if not (rev_usdc_s < rev_m1_s < rev_btc_s):
            raise ValueError(
                f"Reverse timelock cascade violated: "
                f"T_usdc={rev_usdc_s}s, T_m1={rev_m1_s}s, T_btc={rev_btc_s}s "
                f"(must be T_usdc < T_m1 < T_btc)"
            )
        if rev_m1_s - rev_usdc_s < TIMELOCK_CASCADE_MIN_GAP_SECONDS:
            raise ValueError(
                f"Insufficient gap T_m1 - T_usdc: {rev_m1_s - rev_usdc_s}s "
                f"(min {TIMELOCK_CASCADE_MIN_GAP_SECONDS}s)"
            )
        if rev_btc_s - rev_m1_s < TIMELOCK_CASCADE_MIN_GAP_SECONDS:
            raise ValueError(
                f"Insufficient gap T_btc - T_m1: {rev_btc_s - rev_m1_s}s "
                f"(min {TIMELOCK_CASCADE_MIN_GAP_SECONDS}s)"
            )
    return True


# =============================================================================
# Anti-Grief Policy Constants
# =============================================================================
# User-commits-first model: LP never locks before user's on-chain commitment.
# These constants govern rate limiting, plan expiry, and confirmation tiers.

PLAN_EXPIRY_SECONDS = 900               # Plan valid for 15 minutes after /init (Signet blocks ~10 min)
LP_LOCK_WINDOW_SECONDS = 300            # LP must lock within 5 min after user commit
MIN_SWAP_BTC_SATS = 500                  # Minimum 500 sats for testnet (limited M1 liquidity)
MIN_SWAP_USDC = 0.3                     # Minimum $0.30 USDC for testnet
MAX_CONCURRENT_SWAPS_PER_SESSION = 3    # Max pending plans per IP/session

# BTC confirmation tiers: (amount_sats_threshold, required_confirmations)
# LP locks only after required confirmations are met.
BTC_CONFIRMATION_TIERS = [
    (10_000_000, 0),    # < 0.1 BTC:  0-conf (LP takes risk, CLS model)
    (100_000_000, 1),   # < 1 BTC:    1 confirmation
    (float('inf'), 3),  # >= 1 BTC:   3 confirmations
]

# BTC claim confirmation gate (forward flow BTC->USDC only):
# LP can LOCK (engage) in 0-conf, but must NOT DELIVER (claim EVM USDC)
# until the LP's BTC claim TX has this many confirmations.
# This prevents RBF double-spend: user replaces funding TX after LP claims EVM.
BTC_CLAIM_MIN_CONFIRMATIONS = 1         # 1 conf = ~10 min on Signet
BTC_CLAIM_CONFIRMATION_TIMEOUT = 1800   # 30 min max wait before failing

# Completion timeout: max time a swap can stay in COMPLETING/BTC_CLAIMED state.
# If exceeded, watchdog marks FAILED. LP recovers via HTLC timelock refund.
COMPLETING_TIMEOUT_FORWARD = 2700       # 45 min (30 min BTC gate + 15 min margin)
COMPLETING_TIMEOUT_REVERSE = 1800       # 30 min (15 min BTC UTXO poll + 15 min margin)
