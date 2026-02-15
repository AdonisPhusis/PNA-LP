"""
M1 (BATHRON) HTLC3S implementation for FlowSwap protocol.

Uses BATHRON's native 3-secret HTLC RPCs:
- htlc3s_create: Lock M1 receipt in 3-secret HTLC
- htlc3s_claim: Claim with 3 preimages
- htlc3s_refund: Refund after timeout

Mirrors sdk/htlc/m1.py but for 3-secret FlowSwap operations.
"""

import logging
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from ..chains.m1 import M1Client

log = logging.getLogger(__name__)


@dataclass
class M1HTLC3SRecord:
    """M1 3-secret HTLC record from BATHRON."""
    outpoint: str               # txid:vout
    hashlock_user: str
    hashlock_lp1: str
    hashlock_lp2: str
    amount: int                 # M1 units (sats)
    claim_address: str
    refund_address: str
    create_height: int
    expiry_height: int
    status: str                 # active, claimed, refunded
    resolve_txid: Optional[str] = None
    has_covenant: bool = False
    covenant_dest_address: Optional[str] = None


class M1Htlc3S:
    """
    M1 HTLC3S manager using BATHRON native RPCs.

    Wraps htlc3s_create, htlc3s_claim, htlc3s_refund for
    the FlowSwap 3-secret protocol.
    """

    def __init__(self, client: M1Client):
        self.client = client

    def create_htlc(self, receipt_outpoint: str, H_user: str,
                    H_lp1: str, H_lp2: str, claim_address: str,
                    expiry_blocks: int = 120,
                    template_commitment: str = None,
                    covenant_dest_address: str = None) -> Dict:
        """
        Create 3-secret HTLC from M1 receipt.

        Args:
            receipt_outpoint: M1 receipt to lock (txid:vout)
            H_user: SHA256 hashlock for user (64 hex)
            H_lp1: SHA256 hashlock for LP1 (64 hex)
            H_lp2: SHA256 hashlock for LP2 (64 hex)
            claim_address: BATHRON address that can claim with 3 preimages
            expiry_blocks: Blocks until refund (default 120 = ~2h)
            template_commitment: C3 covenant hash (64 hex) for per-leg mode
            covenant_dest_address: LP_OUT address forced by covenant

        Returns:
            {"txid": "...", "htlc_outpoint": "txid:0", "amount": ..., "expiry_height": ...}
        """
        covenant_str = ""
        if template_commitment and covenant_dest_address:
            covenant_str = f", covenant → {covenant_dest_address}"
        log.info(f"Creating M1 HTLC3S: receipt={receipt_outpoint}, "
                 f"H_user={H_user[:16]}..., claim={claim_address}{covenant_str}")

        result = self.client.htlc3s_create(
            receipt_outpoint, H_user, H_lp1, H_lp2,
            claim_address, expiry_blocks,
            template_commitment=template_commitment,
            covenant_dest_address=covenant_dest_address
        )

        if not result:
            raise RuntimeError("HTLC3S creation failed")

        log.info(f"M1 HTLC3S created: txid={result.get('txid')}, "
                 f"has_covenant={result.get('has_covenant', False)}")
        return result

    def claim(self, htlc_outpoint: str, S_user: str,
              S_lp1: str, S_lp2: str) -> Dict:
        """
        Claim 3-secret HTLC with all 3 preimages.

        Args:
            htlc_outpoint: HTLC3S to claim (txid:vout)
            S_user: User's preimage (64 hex)
            S_lp1: LP1's preimage (64 hex)
            S_lp2: LP2's preimage (64 hex)

        Returns:
            {"txid": "...", "receipt_outpoint": "...", "amount": ...}
        """
        log.info(f"Claiming M1 HTLC3S: {htlc_outpoint}")

        result = self.client.htlc3s_claim(
            htlc_outpoint, S_user, S_lp1, S_lp2
        )

        if not result:
            raise RuntimeError("HTLC3S claim failed")

        log.info(f"M1 HTLC3S claimed: txid={result.get('txid')}")
        return result

    def refund(self, htlc_outpoint: str) -> Dict:
        """
        Refund expired 3-secret HTLC.

        Args:
            htlc_outpoint: Expired HTLC3S to refund (txid:vout)

        Returns:
            {"txid": "...", "receipt_outpoint": "...", "amount": ...}
        """
        log.info(f"Refunding M1 HTLC3S: {htlc_outpoint}")

        result = self.client.htlc3s_refund(htlc_outpoint)

        if not result:
            raise RuntimeError("HTLC3S refund failed")

        log.info(f"M1 HTLC3S refunded: txid={result.get('txid')}")
        return result

    def get_htlc(self, htlc_outpoint: str) -> Optional[M1HTLC3SRecord]:
        """Get 3S HTLC details."""
        result = self.client.htlc3s_get(htlc_outpoint)
        if not result:
            return None

        return M1HTLC3SRecord(
            outpoint=htlc_outpoint,
            hashlock_user=result.get("hashlock_user", ""),
            hashlock_lp1=result.get("hashlock_lp1", ""),
            hashlock_lp2=result.get("hashlock_lp2", ""),
            amount=result.get("amount", 0),
            claim_address=result.get("claim_address", ""),
            refund_address=result.get("refund_address", ""),
            create_height=result.get("create_height", 0),
            expiry_height=result.get("expiry_height", 0),
            status=result.get("status", "unknown"),
            resolve_txid=result.get("resolve_txid"),
            has_covenant=result.get("has_covenant", False),
            covenant_dest_address=result.get("covenant_dest_address"),
        )

    def list_htlcs(self, status: str = None) -> List[M1HTLC3SRecord]:
        """List 3S HTLCs."""
        results = self.client.htlc3s_list(status)
        if not results:
            return []

        records = []
        for r in results:
            records.append(M1HTLC3SRecord(
                outpoint=r.get("outpoint", ""),
                hashlock_user=r.get("hashlock_user", ""),
                hashlock_lp1=r.get("hashlock_lp1", ""),
                hashlock_lp2=r.get("hashlock_lp2", ""),
                amount=r.get("amount", 0),
                claim_address=r.get("claim_address", ""),
                refund_address=r.get("refund_address", ""),
                create_height=r.get("create_height", 0),
                expiry_height=r.get("expiry_height", 0),
                status=r.get("status", "unknown"),
                resolve_txid=r.get("resolve_txid"),
            ))
        return records

    def ensure_receipt_available(self, amount: int) -> str:
        """
        Ensure an M1 receipt of sufficient amount is available.
        Will lock M0 -> M1 if needed.

        Args:
            amount: Required M1 amount (sats)

        Returns:
            Receipt outpoint (txid:vout)

        Raises:
            RuntimeError if insufficient balance
        """
        # Check existing receipts (BATHRON: amount already in sats)
        receipts = self.client.list_m1_receipts()
        for r in receipts:
            r_sats = int(r.get("amount", 0))
            if r_sats >= amount:
                return r.get("outpoint")

        # Need to lock M0 → M1
        # BATHRON: 1 M0 = 1 sat, getbalance returns integer sats directly
        m0_data = self.client.get_balance()

        if isinstance(m0_data, dict):
            m0_balance = int(m0_data.get("m0", 0)) - int(m0_data.get("locked", 0))
        elif isinstance(m0_data, (int, float)):
            m0_balance = int(m0_data)
        else:
            m0_balance = 0

        log.info(f"M0 balance: need {amount} sats, have {m0_balance} sats")

        if m0_balance < amount:
            raise RuntimeError(f"Insufficient balance. Need {amount}, have {m0_balance}")

        # BATHRON: 1 M0 = 1 sat, RPC lock expects integer sats directly
        log.info(f"Locking {amount} M0 -> M1 (sats)")
        result = self.client.lock(amount)

        if not result or not result.get("txid"):
            raise RuntimeError("Failed to lock M0 -> M1")

        lock_txid = result['txid']
        expected_outpoint = f"{lock_txid}:1"

        # Wait for lock TX to be confirmed (mined into a block)
        import time as _time
        log.info(f"Waiting for lock TX {lock_txid[:16]}... to be confirmed")
        for attempt in range(12):  # ~120s max (12 x 10s)
            _time.sleep(10)
            receipts = self.client.list_m1_receipts()
            for r in receipts:
                if r.get("outpoint") == expected_outpoint:
                    log.info(f"Lock TX confirmed after {(attempt+1)*10}s")
                    return expected_outpoint
            log.info(f"Waiting... attempt {attempt+1}/12")

        raise RuntimeError(f"Lock TX {lock_txid} not confirmed after 120s")
