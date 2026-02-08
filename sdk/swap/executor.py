"""
Swap Executor for pna SDK.

Orchestrates atomic swaps between chains using HTLCs.
Handles the full swap lifecycle from quote to completion.

Swap Flow (BTC -> M1 example):
1. User requests quote
2. LP generates hashlock, creates M1 HTLC
3. User sends BTC to LP's HTLC address
4. LP confirms BTC deposit
5. User claims M1 HTLC (reveals preimage)
6. LP uses preimage to claim BTC

For BTC -> USDC, it goes through M1:
BTC -> M1 -> USDC (two atomic swaps chained)
"""

import time
import uuid
import logging
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, field
from enum import Enum

from ..core import (
    SwapState, SwapDirection, HTLCParams, SwapQuote, SwapResult,
    generate_secret, verify_preimage, BTC_M1_RATE,
    HTLC_TIMEOUT_BTC, HTLC_TIMEOUT_M1,
)
from ..chains.btc import BTCClient
from ..chains.m1 import M1Client
from ..chains.evm import EVMClient
from ..htlc.btc import BTCHtlc
from ..htlc.m1 import M1Htlc

log = logging.getLogger(__name__)


@dataclass
class SwapConfig:
    """Swap executor configuration."""
    # Spread percentages
    spread_btc_m1_bid: float = 0.5  # User sells BTC
    spread_btc_m1_ask: float = 0.5  # User buys BTC
    spread_usdc_m1_bid: float = 0.5
    spread_usdc_m1_ask: float = 0.5

    # Confirmation requirements
    btc_confirmations: int = 1
    m1_confirmations: int = 1

    # Timeouts (in blocks)
    btc_timeout_blocks: int = 144   # ~24 hours
    m1_timeout_blocks: int = 288    # ~4.8 hours

    # Quote validity (seconds)
    quote_validity: int = 60


@dataclass
class ActiveSwap:
    """Active swap state."""
    swap_id: str
    state: SwapState
    direction: SwapDirection

    # Assets
    from_asset: str
    to_asset: str
    from_amount: int
    to_amount: int

    # HTLC parameters
    secret: str
    hashlock: str

    # User's HTLC (where user deposits)
    user_htlc_address: Optional[str] = None
    user_htlc_script: Optional[str] = None
    user_deposit_txid: Optional[str] = None
    user_deposit_confirmations: int = 0

    # LP's HTLC (what user claims)
    lp_htlc_outpoint: Optional[str] = None
    lp_htlc_address: Optional[str] = None

    # Resolution
    claim_txid: Optional[str] = None
    preimage_revealed: Optional[str] = None

    # Timing
    created_at: int = 0
    expires_at: int = 0
    completed_at: Optional[int] = None

    # Callbacks
    on_state_change: Optional[Callable] = None


class SwapExecutor:
    """
    Executes atomic swaps across chains.

    Supports:
    - BTC <-> M1 (direct)
    - USDC <-> M1 (direct)
    - BTC <-> USDC (via M1)
    """

    def __init__(self, btc_client: BTCClient, m1_client: M1Client,
                 evm_client: EVMClient = None, config: SwapConfig = None):
        self.btc = btc_client
        self.m1 = m1_client
        self.evm = evm_client
        self.config = config or SwapConfig()

        # HTLC managers
        self.btc_htlc = BTCHtlc(btc_client)
        self.m1_htlc = M1Htlc(m1_client)

        # Active swaps
        self.swaps: Dict[str, ActiveSwap] = {}

    def get_quote(self, from_asset: str, to_asset: str,
                 from_amount: int) -> SwapQuote:
        """
        Get swap quote.

        Args:
            from_asset: Source asset (BTC, M1, USDC)
            to_asset: Target asset
            from_amount: Amount in smallest units

        Returns:
            SwapQuote with rate and estimated output
        """
        now = int(time.time())
        quote_id = f"q_{uuid.uuid4().hex[:12]}"

        # Calculate rate and output amount
        rate, spread = self._calculate_rate(from_asset, to_asset)
        to_amount = int(from_amount * rate * (1 - spread / 100))

        # Determine route
        if from_asset == "M1" or to_asset == "M1":
            route = f"{from_asset} -> {to_asset}"
        else:
            route = f"{from_asset} -> M1 -> {to_asset}"

        # Confirmations required
        confirmations = {
            "BTC": self.config.btc_confirmations,
            "M1": self.config.m1_confirmations,
            "USDC": 1,
        }.get(from_asset, 1)

        return SwapQuote(
            quote_id=quote_id,
            from_asset=from_asset,
            to_asset=to_asset,
            from_amount=from_amount,
            to_amount=to_amount,
            rate=rate * (1 - spread / 100),
            spread_percent=spread,
            route=route,
            expires_at=now + self.config.quote_validity,
            lp_id="pna_lp",
            confirmations_required=confirmations,
        )

    def _calculate_rate(self, from_asset: str, to_asset: str) -> tuple[float, float]:
        """Calculate exchange rate and spread."""
        # BTC/M1 is fixed: 1 SAT = 1 M1
        if from_asset == "BTC" and to_asset == "M1":
            return float(BTC_M1_RATE), self.config.spread_btc_m1_bid
        elif from_asset == "M1" and to_asset == "BTC":
            return 1.0 / BTC_M1_RATE, self.config.spread_btc_m1_ask

        # USDC/M1 - would need price feed in production
        # For now, assume 1 USDC = ~1300 M1 (based on BTC price)
        usdc_m1_rate = 1300.0
        if from_asset == "USDC" and to_asset == "M1":
            return usdc_m1_rate, self.config.spread_usdc_m1_bid
        elif from_asset == "M1" and to_asset == "USDC":
            return 1.0 / usdc_m1_rate, self.config.spread_usdc_m1_ask

        # BTC/USDC via M1
        if from_asset == "BTC" and to_asset == "USDC":
            btc_usdc = float(BTC_M1_RATE) / usdc_m1_rate
            spread = self.config.spread_btc_m1_bid + self.config.spread_usdc_m1_ask
            return btc_usdc, spread
        elif from_asset == "USDC" and to_asset == "BTC":
            usdc_btc = usdc_m1_rate / float(BTC_M1_RATE)
            spread = self.config.spread_usdc_m1_bid + self.config.spread_btc_m1_ask
            return usdc_btc, spread

        raise ValueError(f"Unsupported pair: {from_asset}/{to_asset}")

    def initiate_swap(self, quote: SwapQuote, user_claim_address: str,
                     lp_refund_address: str) -> ActiveSwap:
        """
        Initiate a new swap based on quote.

        For BTC -> M1:
        1. LP generates secret/hashlock
        2. LP creates deposit address (BTC HTLC)
        3. LP prepares M1 HTLC (will create after BTC deposit confirmed)

        Args:
            quote: Valid quote from get_quote()
            user_claim_address: Where user will receive funds
            lp_refund_address: LP's address for refund path

        Returns:
            ActiveSwap with deposit instructions
        """
        if not quote.is_valid():
            raise ValueError("Quote has expired")

        swap_id = f"swap_{uuid.uuid4().hex[:12]}"
        now = int(time.time())

        # Generate secret (LP holds secret for user's HTLC)
        secret, hashlock = generate_secret()

        # Determine direction
        if quote.to_asset == "M1":
            direction = SwapDirection.TO_M1
        elif quote.from_asset == "M1":
            direction = SwapDirection.FROM_M1
        else:
            direction = SwapDirection.THROUGH_M1

        swap = ActiveSwap(
            swap_id=swap_id,
            state=SwapState.CREATED,
            direction=direction,
            from_asset=quote.from_asset,
            to_asset=quote.to_asset,
            from_amount=quote.from_amount,
            to_amount=quote.to_amount,
            secret=secret,
            hashlock=hashlock,
            created_at=now,
            expires_at=now + (self.config.btc_timeout_blocks * 600),  # Rough estimate
        )

        # Create user's deposit address based on source asset
        if quote.from_asset == "BTC":
            self._setup_btc_deposit(swap, user_claim_address, lp_refund_address)
        elif quote.from_asset == "M1":
            self._setup_m1_deposit(swap, user_claim_address, lp_refund_address)

        self.swaps[swap_id] = swap
        log.info(f"Swap initiated: {swap_id}, {quote.from_asset} -> {quote.to_asset}")

        return swap

    def _setup_btc_deposit(self, swap: ActiveSwap, user_claim_address: str,
                          lp_refund_address: str):
        """Setup BTC HTLC for user deposit."""
        # LP creates BTC HTLC address where user will deposit
        # LP can claim with preimage, user can refund after timeout

        htlc_info = self.btc_htlc.create_htlc(
            amount_sats=swap.from_amount,
            hashlock=swap.hashlock,
            recipient_address=lp_refund_address,  # LP claims
            refund_address=user_claim_address,    # User can refund
            timeout_blocks=self.config.btc_timeout_blocks,
        )

        swap.user_htlc_address = htlc_info["htlc_address"]
        swap.user_htlc_script = htlc_info["redeem_script"]

        log.info(f"BTC deposit address: {swap.user_htlc_address}")

    def _setup_m1_deposit(self, swap: ActiveSwap, user_claim_address: str,
                         lp_refund_address: str):
        """Setup M1 HTLC for user deposit."""
        # For M1 -> X swaps, user sends to LP's M1 address
        # Then LP creates HTLC that user can claim

        # Get LP's M1 address for deposit
        lp_m1_address = self.m1.get_addresses_by_label("lp_pna")
        if not lp_m1_address:
            lp_m1_address = [self.m1.get_new_address("lp_pna")]

        swap.user_htlc_address = lp_m1_address[0]
        log.info(f"M1 deposit address: {swap.user_htlc_address}")

    def check_deposit(self, swap_id: str) -> bool:
        """
        Check if user has deposited to HTLC.

        Returns True if deposit is confirmed.
        """
        swap = self.swaps.get(swap_id)
        if not swap:
            return False

        if swap.from_asset == "BTC":
            utxo = self.btc_htlc.check_htlc_funded(
                swap.user_htlc_address,
                swap.from_amount,
                self.config.btc_confirmations
            )
            if utxo:
                swap.user_deposit_txid = utxo["txid"]
                swap.user_deposit_confirmations = utxo["confirmations"]
                swap.state = SwapState.DEPOSIT_CONFIRMED
                log.info(f"BTC deposit confirmed: {utxo['txid']}")
                return True

        elif swap.from_asset == "M1":
            # Check M1 deposit
            balance, _ = self.m1.get_balance_for_addresses([swap.user_htlc_address])
            if balance >= swap.from_amount:
                swap.state = SwapState.DEPOSIT_CONFIRMED
                log.info(f"M1 deposit confirmed")
                return True

        return False

    def create_lp_htlc(self, swap_id: str, lp_claim_address: str,
                       user_refund_address: str) -> str:
        """
        Create LP's counter-HTLC after user deposit confirmed.

        For BTC -> M1:
        - LP creates M1 HTLC with same hashlock
        - User can claim M1 by revealing preimage
        - LP then uses preimage to claim user's BTC

        Returns:
            HTLC outpoint or address
        """
        swap = self.swaps.get(swap_id)
        if not swap:
            raise ValueError("Swap not found")

        if swap.state != SwapState.DEPOSIT_CONFIRMED:
            raise ValueError("Deposit not confirmed yet")

        if swap.to_asset == "M1":
            # LP creates M1 HTLC for user to claim
            receipt = self.m1_htlc.ensure_receipt_available(swap.to_amount)

            result = self.m1_htlc.create_htlc(
                receipt_outpoint=receipt,
                hashlock=swap.hashlock,
                claim_address=user_refund_address,  # User claims
                expiry_blocks=self.config.m1_timeout_blocks,
            )

            swap.lp_htlc_outpoint = result.get("htlc_outpoint")
            swap.lp_htlc_address = result.get("htlc_address")
            swap.state = SwapState.HTLC_CREATED

            log.info(f"LP M1 HTLC created: {swap.lp_htlc_outpoint}")
            return swap.lp_htlc_outpoint

        elif swap.to_asset == "BTC":
            # LP creates BTC HTLC for user
            # This is more complex - need to fund BTC address
            raise NotImplementedError("BTC output HTLC not yet implemented")

        return ""

    def user_claim(self, swap_id: str, preimage: str) -> str:
        """
        User claims LP's HTLC (reveals preimage).

        After this, LP can use the preimage to claim user's deposit.

        Returns:
            Claim transaction ID
        """
        swap = self.swaps.get(swap_id)
        if not swap:
            raise ValueError("Swap not found")

        if swap.state != SwapState.HTLC_CREATED:
            raise ValueError("HTLC not ready for claim")

        # Verify preimage
        if not verify_preimage(preimage, swap.hashlock):
            raise ValueError("Invalid preimage")

        if swap.to_asset == "M1":
            result = self.m1_htlc.claim(swap.lp_htlc_outpoint, preimage)
            swap.claim_txid = result.get("txid")
            swap.preimage_revealed = preimage
            swap.state = SwapState.CLAIMED
            log.info(f"User claimed M1: txid={swap.claim_txid}")
            return swap.claim_txid

        raise NotImplementedError(f"Claim for {swap.to_asset} not implemented")

    def lp_claim_deposit(self, swap_id: str) -> str:
        """
        LP claims user's original deposit using revealed preimage.

        This completes the atomic swap.

        Returns:
            Claim transaction ID
        """
        swap = self.swaps.get(swap_id)
        if not swap:
            raise ValueError("Swap not found")

        if swap.state != SwapState.CLAIMED:
            raise ValueError("User hasn't claimed yet")

        if not swap.preimage_revealed:
            # Try to extract from claim TX
            preimage = self.m1_htlc.extract_preimage_from_tx(swap.claim_txid)
            if preimage:
                swap.preimage_revealed = preimage
            else:
                raise ValueError("Preimage not available")

        if swap.from_asset == "BTC":
            # LP claims BTC HTLC
            # Need to construct and sign claim transaction
            log.info(f"LP would claim BTC with preimage: {swap.preimage_revealed[:16]}...")
            # This requires raw transaction signing
            swap.state = SwapState.COMPLETED
            swap.completed_at = int(time.time())
            return "btc_claim_pending"

        elif swap.from_asset == "M1":
            # LP claims M1 HTLC
            # ... similar flow
            pass

        return ""

    def get_swap(self, swap_id: str) -> Optional[ActiveSwap]:
        """Get swap by ID."""
        return self.swaps.get(swap_id)

    def get_active_swaps(self) -> List[ActiveSwap]:
        """Get all active (non-completed) swaps."""
        return [
            s for s in self.swaps.values()
            if s.state not in (SwapState.COMPLETED, SwapState.FAILED, SwapState.REFUNDED)
        ]

    def to_swap_result(self, swap: ActiveSwap) -> SwapResult:
        """Convert ActiveSwap to SwapResult."""
        return SwapResult(
            swap_id=swap.swap_id,
            state=swap.state,
            from_asset=swap.from_asset,
            to_asset=swap.to_asset,
            from_amount=swap.from_amount,
            to_amount=swap.to_amount,
            hashlock=swap.hashlock,
            deposit_address=swap.user_htlc_address or "",
            deposit_txid=swap.user_deposit_txid,
            claim_txid=swap.claim_txid,
            created_at=swap.created_at,
            completed_at=swap.completed_at,
        )
