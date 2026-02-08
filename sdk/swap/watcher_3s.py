"""
Watcher for 3-secret FlowSwap protocol.

Monitors Bitcoin for claim transactions, extracts secrets,
and claims on EVM (permissionless).

Flow:
1. User sends BTC to HTLC-1
2. LP1 claims HTLC-1, revealing (S_user, S_lp1, S_lp2) in witness
3. Watcher detects claim on Bitcoin
4. Watcher extracts 3 secrets from witness
5. Watcher calls EVM claim(htlcId, S_user, S_lp1, S_lp2)
6. User receives USDC (fixed recipient)

Key properties:
- Permissionless: Watcher pays gas, user gets funds
- Trustless: Watcher cannot steal (recipient fixed at HTLC creation)
- Atomic: Either all secrets revealed or none
"""

import time
import logging
import threading
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class WatchedSwap:
    """A swap being watched for completion."""
    swap_id: str
    btc_htlc_address: str
    btc_htlc_script: str      # Redeem script hex
    evm_htlc_id: str
    evm_contract: str
    H_user: str
    H_lp1: str
    H_lp2: str
    user_usdc_address: str    # Fixed recipient
    timelock_btc: int         # Block height
    timelock_evm: int         # Unix timestamp
    created_at: float = field(default_factory=time.time)

    # State
    btc_claimed: bool = False
    secrets_extracted: bool = False
    evm_claimed: bool = False
    S_user: Optional[str] = None
    S_lp1: Optional[str] = None
    S_lp2: Optional[str] = None
    btc_claim_txid: Optional[str] = None
    evm_claim_txhash: Optional[str] = None


@dataclass
class Watcher3SConfig:
    """Configuration for the 3S watcher."""
    # Polling intervals
    btc_poll_interval: int = 30   # seconds
    evm_poll_interval: int = 10   # seconds

    # Auto-actions
    auto_claim_evm: bool = True   # Automatically claim on EVM after BTC claim

    # Watcher's EVM private key (for paying gas)
    evm_private_key: Optional[str] = None


class Watcher3S:
    """
    Monitors Bitcoin claims and executes EVM claims.

    This is the "Send & Close" enabler - user can leave after
    sending BTC, and watcher completes the swap.
    """

    def __init__(
        self,
        btc_client,               # BTCClient
        evm_htlc,                 # EVMHTLC3S
        config: Watcher3SConfig = None
    ):
        """
        Initialize the 3S watcher.

        Args:
            btc_client: Bitcoin RPC client
            evm_htlc: EVM HTLC 3S client
            config: Watcher configuration
        """
        self.btc = btc_client
        self.evm = evm_htlc
        self.config = config or Watcher3SConfig()

        # Watched swaps
        self._swaps: Dict[str, WatchedSwap] = {}
        self._lock = threading.Lock()

        # Callbacks
        self.on_secrets_extracted: Optional[Callable[[WatchedSwap], None]] = None
        self.on_evm_claimed: Optional[Callable[[WatchedSwap], None]] = None
        self.on_swap_failed: Optional[Callable[[WatchedSwap, str], None]] = None

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def add_swap(self, swap: WatchedSwap):
        """Add a swap to watch."""
        with self._lock:
            self._swaps[swap.swap_id] = swap
            log.info(f"Watching swap {swap.swap_id}, BTC HTLC: {swap.btc_htlc_address[:20]}...")

    def remove_swap(self, swap_id: str):
        """Stop watching a swap."""
        with self._lock:
            if swap_id in self._swaps:
                del self._swaps[swap_id]

    def get_swap(self, swap_id: str) -> Optional[WatchedSwap]:
        """Get swap by ID."""
        with self._lock:
            return self._swaps.get(swap_id)

    def start(self):
        """Start watching in background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        log.info("Watcher 3S started")

    def stop(self):
        """Stop watching."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Watcher 3S stopped")

    def _watch_loop(self):
        """Main watch loop."""
        last_btc_check = 0

        while self._running:
            now = time.time()

            try:
                # Check Bitcoin
                if now - last_btc_check >= self.config.btc_poll_interval:
                    self._check_btc_claims()
                    last_btc_check = now

            except Exception as e:
                log.error(f"Watcher error: {e}")

            time.sleep(1)

    def _check_btc_claims(self):
        """Check for BTC HTLC claims."""
        with self._lock:
            swaps = list(self._swaps.values())

        for swap in swaps:
            if swap.btc_claimed:
                continue

            try:
                secrets = self._check_btc_htlc_claimed(swap)
                if secrets:
                    self._handle_btc_claimed(swap, secrets)
            except Exception as e:
                log.error(f"Error checking BTC claim for {swap.swap_id}: {e}")

    def _check_btc_htlc_claimed(self, swap: WatchedSwap) -> Optional[Dict]:
        """
        Check if BTC HTLC has been claimed and extract secrets.

        Returns:
            Dict with S_user, S_lp1, S_lp2 if claimed, None otherwise
        """
        import json

        try:
            # Check if HTLC output has been spent
            scan = self.btc._call(
                "scantxoutset", "start",
                json.dumps([f"addr({swap.btc_htlc_address})"])
            )

            if not scan or not scan.get("success"):
                return None

            # If no unspents, HTLC has been spent (claimed or refunded)
            unspents = scan.get("unspents", [])
            if unspents:
                # Still unspent, not claimed yet
                return None

            log.info(f"BTC HTLC spent for {swap.swap_id}, looking for claim TX...")

            # Find the spending transaction
            # We need to search recent blocks for a TX that spends our HTLC
            claim_tx = self._find_claim_transaction(swap)
            if not claim_tx:
                log.warning(f"Could not find claim TX for {swap.swap_id}")
                return None

            # Extract secrets from witness
            return self._extract_secrets_from_tx(claim_tx, swap)

        except Exception as e:
            log.error(f"Error checking BTC HTLC: {e}")
            return None

    def _find_claim_transaction(self, swap: WatchedSwap) -> Optional[Dict]:
        """
        Find the transaction that claimed the BTC HTLC.

        Searches recent blocks for a TX spending from the HTLC address.
        """
        try:
            current_height = self.btc.get_block_count()

            # Search last 6 blocks (should be enough for testnet)
            for height in range(current_height, max(0, current_height - 6), -1):
                block_hash = self.btc._call("getblockhash", height)
                block = self.btc._call("getblock", block_hash, 2)  # verbosity=2 for full TX

                for tx in block.get("tx", []):
                    for vin in tx.get("vin", []):
                        # Check if this input spends from our HTLC
                        # We'd need to look up the previous output
                        # For now, check witness structure
                        witness = vin.get("txinwitness", [])
                        if self._is_3s_claim_witness(witness):
                            # Verify it's spending our HTLC by checking script hash
                            if len(witness) >= 6:
                                script_hex = witness[-1]
                                if script_hex == swap.btc_htlc_script:
                                    swap.btc_claim_txid = tx["txid"]
                                    return tx

            # Also check mempool
            mempool_txids = self.btc._call("getrawmempool")
            for txid in mempool_txids[:100]:  # Limit scan
                tx = self.btc._call("getrawtransaction", txid, True)
                for vin in tx.get("vin", []):
                    witness = vin.get("txinwitness", [])
                    if self._is_3s_claim_witness(witness):
                        if len(witness) >= 6:
                            script_hex = witness[-1]
                            if script_hex == swap.btc_htlc_script:
                                swap.btc_claim_txid = tx["txid"]
                                return tx

            return None

        except Exception as e:
            log.error(f"Error finding claim TX: {e}")
            return None

    def _is_3s_claim_witness(self, witness: List[str]) -> bool:
        """
        Check if witness looks like a 3S HTLC claim.

        Expected: [sig, S_lp2, S_lp1, S_user, 0x01, script]
        """
        if len(witness) != 6:
            return False

        # Check branch selector is 0x01 (claim path)
        if witness[4] != "01":
            return False

        # Check secrets are 32 bytes each
        try:
            if len(bytes.fromhex(witness[1])) != 32:  # S_lp2
                return False
            if len(bytes.fromhex(witness[2])) != 32:  # S_lp1
                return False
            if len(bytes.fromhex(witness[3])) != 32:  # S_user
                return False
        except:
            return False

        return True

    def _extract_secrets_from_tx(self, tx: Dict, swap: WatchedSwap) -> Optional[Dict]:
        """
        Extract 3 secrets from claim transaction witness.

        Args:
            tx: Raw transaction dict
            swap: The swap being watched

        Returns:
            Dict with S_user, S_lp1, S_lp2 if valid, None otherwise
        """
        for vin in tx.get("vin", []):
            witness = vin.get("txinwitness", [])

            if not self._is_3s_claim_witness(witness):
                continue

            # Check if spending our script
            script_hex = witness[-1]
            if script_hex != swap.btc_htlc_script:
                continue

            # Extract secrets (canonical order in witness)
            # witness = [sig, S_lp2, S_lp1, S_user, 0x01, script]
            S_lp2 = witness[1]
            S_lp1 = witness[2]
            S_user = witness[3]

            # Verify against hashlocks
            import hashlib

            def sha256_hex(data_hex: str) -> str:
                return hashlib.sha256(bytes.fromhex(data_hex)).hexdigest()

            if sha256_hex(S_user) != swap.H_user.replace("0x", "").lower():
                log.warning(f"S_user doesn't match H_user")
                continue
            if sha256_hex(S_lp1) != swap.H_lp1.replace("0x", "").lower():
                log.warning(f"S_lp1 doesn't match H_lp1")
                continue
            if sha256_hex(S_lp2) != swap.H_lp2.replace("0x", "").lower():
                log.warning(f"S_lp2 doesn't match H_lp2")
                continue

            log.info(f"Extracted 3 secrets for {swap.swap_id}")
            log.info(f"  S_user: {S_user[:16]}...")
            log.info(f"  S_lp1:  {S_lp1[:16]}...")
            log.info(f"  S_lp2:  {S_lp2[:16]}...")

            return {
                "S_user": S_user,
                "S_lp1": S_lp1,
                "S_lp2": S_lp2,
            }

        return None

    def _handle_btc_claimed(self, swap: WatchedSwap, secrets: Dict):
        """Handle BTC HTLC being claimed - extract secrets and claim on EVM."""
        swap.btc_claimed = True
        swap.S_user = secrets["S_user"]
        swap.S_lp1 = secrets["S_lp1"]
        swap.S_lp2 = secrets["S_lp2"]
        swap.secrets_extracted = True

        log.info(f"BTC claimed for {swap.swap_id}, secrets extracted")

        if self.on_secrets_extracted:
            self.on_secrets_extracted(swap)

        # Auto-claim on EVM if enabled
        if self.config.auto_claim_evm:
            self._claim_evm(swap)

    def _claim_evm(self, swap: WatchedSwap):
        """Claim HTLC on EVM using extracted secrets."""
        if swap.evm_claimed:
            return

        if not self.config.evm_private_key:
            log.warning(f"No EVM private key configured, cannot auto-claim {swap.swap_id}")
            return

        log.info(f"Claiming EVM HTLC for {swap.swap_id}...")

        try:
            result = self.evm.claim_htlc(
                htlc_id=swap.evm_htlc_id,
                S_user=swap.S_user,
                S_lp1=swap.S_lp1,
                S_lp2=swap.S_lp2,
                private_key=self.config.evm_private_key
            )

            if result.success:
                swap.evm_claimed = True
                swap.evm_claim_txhash = result.tx_hash
                log.info(f"EVM claimed for {swap.swap_id}: {result.tx_hash}")

                if self.on_evm_claimed:
                    self.on_evm_claimed(swap)
            else:
                log.error(f"EVM claim failed for {swap.swap_id}: {result.error}")
                if self.on_swap_failed:
                    self.on_swap_failed(swap, result.error)

        except Exception as e:
            log.exception(f"Error claiming EVM for {swap.swap_id}")
            if self.on_swap_failed:
                self.on_swap_failed(swap, str(e))

    def claim_with_known_secrets(
        self,
        swap_id: str,
        S_user: str,
        S_lp1: str,
        S_lp2: str
    ) -> bool:
        """
        Manually trigger EVM claim with known secrets.

        Use this if secrets were obtained through other means
        (e.g., user provided S_user directly).

        Args:
            swap_id: Swap to claim
            S_user: User's secret
            S_lp1: LP1's secret
            S_lp2: LP2's secret

        Returns:
            True if claim succeeded
        """
        swap = self.get_swap(swap_id)
        if not swap:
            log.error(f"Swap {swap_id} not found")
            return False

        swap.S_user = S_user
        swap.S_lp1 = S_lp1
        swap.S_lp2 = S_lp2
        swap.secrets_extracted = True

        self._claim_evm(swap)
        return swap.evm_claimed

    def watch_single(
        self,
        swap: WatchedSwap,
        timeout: int = 3600
    ) -> WatchedSwap:
        """
        Watch a single swap until completion.

        Blocking call - use for CLI testing.

        Args:
            swap: Swap to watch
            timeout: Max seconds to wait

        Returns:
            Final swap state
        """
        self.add_swap(swap)
        start = time.time()

        while time.time() - start < timeout:
            current = self.get_swap(swap.swap_id)
            if not current:
                raise ValueError("Swap removed")

            if current.evm_claimed:
                log.info(f"Swap {swap.swap_id} completed!")
                return current

            # Manual check
            if not current.btc_claimed:
                secrets = self._check_btc_htlc_claimed(current)
                if secrets:
                    self._handle_btc_claimed(current, secrets)

            time.sleep(5)

        raise TimeoutError(f"Swap {swap.swap_id} did not complete in {timeout}s")


def create_watched_swap(
    swap_id: str,
    btc_htlc: Dict,      # Result from BTCHTLC3S.create_htlc_3s()
    evm_htlc_id: str,    # From EVMHTLC3S.create_htlc()
    evm_contract: str,   # HTLC3S contract address
    user_usdc_address: str
) -> WatchedSwap:
    """
    Create a WatchedSwap from HTLC creation results.

    Args:
        swap_id: Unique swap identifier
        btc_htlc: Result from BTC HTLC creation
        evm_htlc_id: EVM HTLC ID
        evm_contract: EVM contract address
        user_usdc_address: User's USDC recipient address

    Returns:
        WatchedSwap ready to be added to watcher
    """
    return WatchedSwap(
        swap_id=swap_id,
        btc_htlc_address=btc_htlc["htlc_address"],
        btc_htlc_script=btc_htlc["redeem_script"],
        evm_htlc_id=evm_htlc_id,
        evm_contract=evm_contract,
        H_user=btc_htlc["H_user"],
        H_lp1=btc_htlc["H_lp1"],
        H_lp2=btc_htlc["H_lp2"],
        user_usdc_address=user_usdc_address,
        timelock_btc=btc_htlc["timelock"],
        timelock_evm=0,  # Set from EVM HTLC info
    )
