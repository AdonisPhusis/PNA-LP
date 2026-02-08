"""
Swap Watcher for pna SDK.

Monitors chains for:
- Deposit confirmations
- HTLC claims (preimage reveals)
- Timeouts for refunds

Runs as a background service to automate swap completion.
"""

import time
import logging
import threading
from typing import Dict, List, Callable, Optional
from dataclasses import dataclass

from ..core import SwapState
from ..chains.btc import BTCClient
from ..chains.m1 import M1Client
from .executor import SwapExecutor, ActiveSwap

log = logging.getLogger(__name__)


@dataclass
class WatcherConfig:
    """Watcher configuration."""
    poll_interval_btc: int = 30      # seconds
    poll_interval_m1: int = 10       # seconds
    poll_interval_evm: int = 5       # seconds
    auto_create_lp_htlc: bool = True # Auto-create LP HTLC on deposit
    auto_claim_lp: bool = True       # Auto-claim LP deposit after user claims


class SwapWatcher:
    """
    Background service that watches for swap events.

    Events:
    - on_deposit_confirmed: User's deposit is confirmed
    - on_user_claimed: User claimed LP's HTLC (preimage revealed)
    - on_swap_completed: Full swap completed
    - on_swap_expired: Swap timed out, needs refund
    """

    def __init__(self, executor: SwapExecutor, config: WatcherConfig = None):
        self.executor = executor
        self.config = config or WatcherConfig()

        # Callbacks
        self.on_deposit_confirmed: Optional[Callable[[ActiveSwap], None]] = None
        self.on_user_claimed: Optional[Callable[[ActiveSwap, str], None]] = None
        self.on_swap_completed: Optional[Callable[[ActiveSwap], None]] = None
        self.on_swap_expired: Optional[Callable[[ActiveSwap], None]] = None

        # State
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start watcher in background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        log.info("Swap watcher started")

    def stop(self):
        """Stop watcher."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Swap watcher stopped")

    def _watch_loop(self):
        """Main watch loop."""
        last_btc_check = 0
        last_m1_check = 0

        while self._running:
            now = time.time()

            try:
                # Check BTC
                if now - last_btc_check >= self.config.poll_interval_btc:
                    self._check_btc_deposits()
                    last_btc_check = now

                # Check M1
                if now - last_m1_check >= self.config.poll_interval_m1:
                    self._check_m1_htlcs()
                    last_m1_check = now

                # Check for expired swaps
                self._check_expirations()

            except Exception as e:
                log.error(f"Watcher error: {e}")

            time.sleep(1)

    def _check_btc_deposits(self):
        """Check for BTC deposits in active swaps."""
        for swap in self.executor.get_active_swaps():
            if swap.from_asset != "BTC":
                continue
            if swap.state != SwapState.CREATED:
                continue

            # Check if deposited
            if self.executor.check_deposit(swap.swap_id):
                log.info(f"BTC deposit confirmed for swap {swap.swap_id}")

                if self.on_deposit_confirmed:
                    self.on_deposit_confirmed(swap)

                # Auto-create LP HTLC
                if self.config.auto_create_lp_htlc and swap.to_asset == "M1":
                    try:
                        # Need user's claim address - should be stored in swap
                        # For now, use a placeholder
                        log.info(f"Auto-creating LP M1 HTLC for {swap.swap_id}")
                        # self.executor.create_lp_htlc(swap.swap_id, ...)
                    except Exception as e:
                        log.error(f"Failed to create LP HTLC: {e}")

    def _check_m1_htlcs(self):
        """Check M1 HTLCs for claims."""
        for swap in self.executor.get_active_swaps():
            if swap.state != SwapState.HTLC_CREATED:
                continue
            if not swap.lp_htlc_outpoint:
                continue

            # Check if user claimed
            htlc = self.executor.m1_htlc.get_htlc(swap.lp_htlc_outpoint)
            if htlc and htlc.status == "claimed":
                # Extract preimage
                preimage = htlc.preimage
                if preimage:
                    swap.preimage_revealed = preimage
                    swap.state = SwapState.CLAIMED
                    log.info(f"User claimed M1 HTLC, preimage: {preimage[:16]}...")

                    if self.on_user_claimed:
                        self.on_user_claimed(swap, preimage)

                    # Auto-claim LP deposit
                    if self.config.auto_claim_lp:
                        try:
                            self.executor.lp_claim_deposit(swap.swap_id)
                            if self.on_swap_completed:
                                self.on_swap_completed(swap)
                        except Exception as e:
                            log.error(f"Failed to claim LP deposit: {e}")

    def _check_expirations(self):
        """Check for expired swaps that need refund."""
        now = int(time.time())

        for swap in self.executor.get_active_swaps():
            if swap.expires_at and now > swap.expires_at:
                if swap.state in (SwapState.CREATED, SwapState.DEPOSIT_PENDING):
                    # User never deposited, just expire
                    swap.state = SwapState.FAILED
                    log.info(f"Swap {swap.swap_id} expired (no deposit)")

                elif swap.state == SwapState.DEPOSIT_CONFIRMED:
                    # User deposited but LP HTLC not claimed
                    # Need to refund user's deposit
                    swap.state = SwapState.REFUNDED
                    log.warning(f"Swap {swap.swap_id} needs refund")

                    if self.on_swap_expired:
                        self.on_swap_expired(swap)

    def watch_single_swap(self, swap_id: str, timeout: int = 3600) -> ActiveSwap:
        """
        Watch a single swap until completion or timeout.

        Blocking call - use for CLI or testing.

        Args:
            swap_id: Swap to watch
            timeout: Max seconds to wait

        Returns:
            Final swap state
        """
        start = time.time()

        while time.time() - start < timeout:
            swap = self.executor.get_swap(swap_id)
            if not swap:
                raise ValueError("Swap not found")

            if swap.state in (SwapState.COMPLETED, SwapState.FAILED, SwapState.REFUNDED):
                return swap

            # Check deposit
            if swap.state == SwapState.CREATED:
                self.executor.check_deposit(swap_id)

            # Check claim
            if swap.state == SwapState.HTLC_CREATED and swap.lp_htlc_outpoint:
                htlc = self.executor.m1_htlc.get_htlc(swap.lp_htlc_outpoint)
                if htlc and htlc.status == "claimed":
                    swap.preimage_revealed = htlc.preimage
                    swap.state = SwapState.CLAIMED

            # Check if we can complete
            if swap.state == SwapState.CLAIMED:
                try:
                    self.executor.lp_claim_deposit(swap_id)
                except Exception as e:
                    log.error(f"LP claim failed: {e}")

            time.sleep(5)

        raise TimeoutError(f"Swap {swap_id} did not complete in {timeout}s")


class SwapMonitor:
    """
    High-level swap monitoring with event emission.

    Integrates with the executor and provides callbacks for UI updates.
    """

    def __init__(self, executor: SwapExecutor):
        self.executor = executor
        self.watcher = SwapWatcher(executor)

        # Event handlers
        self._handlers: Dict[str, List[Callable]] = {
            "deposit": [],
            "claim": [],
            "complete": [],
            "expire": [],
        }

        # Wire up watcher callbacks
        self.watcher.on_deposit_confirmed = self._on_deposit
        self.watcher.on_user_claimed = self._on_claim
        self.watcher.on_swap_completed = self._on_complete
        self.watcher.on_swap_expired = self._on_expire

    def on(self, event: str, handler: Callable):
        """Register event handler."""
        if event in self._handlers:
            self._handlers[event].append(handler)

    def off(self, event: str, handler: Callable):
        """Remove event handler."""
        if event in self._handlers and handler in self._handlers[event]:
            self._handlers[event].remove(handler)

    def _emit(self, event: str, *args):
        """Emit event to handlers."""
        for handler in self._handlers.get(event, []):
            try:
                handler(*args)
            except Exception as e:
                log.error(f"Handler error for {event}: {e}")

    def _on_deposit(self, swap: ActiveSwap):
        self._emit("deposit", swap)

    def _on_claim(self, swap: ActiveSwap, preimage: str):
        self._emit("claim", swap, preimage)

    def _on_complete(self, swap: ActiveSwap):
        self._emit("complete", swap)

    def _on_expire(self, swap: ActiveSwap):
        self._emit("expire", swap)

    def start(self):
        """Start monitoring."""
        self.watcher.start()

    def stop(self):
        """Stop monitoring."""
        self.watcher.stop()
