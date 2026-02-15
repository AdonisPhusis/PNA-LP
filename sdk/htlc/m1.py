"""
M1 (BATHRON) HTLC implementation for pna SDK.

Uses BATHRON's native HTLC RPCs:
- htlc_create_m1: Lock M1 receipt in HTLC
- htlc_claim: Claim with preimage
- htlc_refund: Refund after timeout

M1 HTLCs are P2SH conditional scripts (BIP-199 compatible).
"""

import logging
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from ..core import HTLCParams, generate_secret
from ..chains.m1 import M1Client, M1Config

log = logging.getLogger(__name__)


@dataclass
class M1HTLCRecord:
    """M1 HTLC record from BATHRON."""
    outpoint: str           # txid:vout
    hashlock: str
    amount: int             # M1 units
    claim_address: str
    refund_address: str
    create_height: int
    expiry_height: int
    status: str             # active, claimed, refunded
    preimage: Optional[str] = None
    resolve_txid: Optional[str] = None


class M1Htlc:
    """
    M1 HTLC manager using BATHRON native RPCs.

    This is the simplest HTLC implementation as BATHRON
    handles all the script complexity internally.
    """

    def __init__(self, client: M1Client):
        self.client = client

    def generate_secret(self) -> Tuple[str, str]:
        """
        Generate new secret and hashlock.

        Returns:
            (secret, hashlock)
        """
        result = self.client.htlc_generate()
        if result:
            return result["secret"], result["hashlock"]

        # Fallback to local generation
        return generate_secret()

    def create_htlc(self, receipt_outpoint: str, hashlock: str,
                   claim_address: str, expiry_blocks: int = 288) -> Dict:
        """
        Create HTLC from M1 receipt.

        Args:
            receipt_outpoint: M1 receipt to lock (txid:vout)
            hashlock: SHA256 hash of preimage (64 hex chars)
            claim_address: BATHRON address that can claim
            expiry_blocks: Blocks until refund allowed (default 288 = ~4.8 hours)

        Returns:
            {
                "txid": "...",
                "htlc_outpoint": "txid:0",
                "htlc_address": "...",
                "amount": ...,
                "expiry_height": ...,
            }
        """
        log.info(f"Creating M1 HTLC: receipt={receipt_outpoint}, "
                f"hashlock={hashlock[:16]}..., claim={claim_address}")

        result = self.client.htlc_create_m1(
            receipt_outpoint, hashlock, claim_address, expiry_blocks
        )

        if not result:
            raise RuntimeError("HTLC creation failed")

        log.info(f"M1 HTLC created: txid={result.get('txid')}")
        return result

    def claim(self, htlc_outpoint: str, preimage: str) -> Dict:
        """
        Claim HTLC with preimage.

        Args:
            htlc_outpoint: HTLC to claim (txid:vout)
            preimage: 32-byte preimage (64 hex chars)

        Returns:
            {
                "txid": "...",
                "receipt_outpoint": "...",
            }
        """
        log.info(f"Claiming M1 HTLC: {htlc_outpoint}")

        result = self.client.htlc_claim(htlc_outpoint, preimage)

        if not result:
            raise RuntimeError("HTLC claim failed")

        log.info(f"M1 HTLC claimed: txid={result.get('txid')}")
        return result

    def refund(self, htlc_outpoint: str) -> Dict:
        """
        Refund expired HTLC.

        Args:
            htlc_outpoint: Expired HTLC to refund (txid:vout)

        Returns:
            {
                "txid": "...",
                "receipt_outpoint": "...",
            }
        """
        log.info(f"Refunding M1 HTLC: {htlc_outpoint}")

        result = self.client.htlc_refund(htlc_outpoint)

        if not result:
            raise RuntimeError("HTLC refund failed")

        log.info(f"M1 HTLC refunded: txid={result.get('txid')}")
        return result

    def get_htlc(self, htlc_outpoint: str) -> Optional[M1HTLCRecord]:
        """
        Get HTLC details.

        Args:
            htlc_outpoint: HTLC identifier (txid:vout)

        Returns:
            M1HTLCRecord or None
        """
        result = self.client.htlc_get(htlc_outpoint)
        if not result:
            return None

        return M1HTLCRecord(
            outpoint=htlc_outpoint,
            hashlock=result.get("hashlock", ""),
            amount=result.get("amount", 0),
            claim_address=result.get("claim_address", ""),
            refund_address=result.get("refund_address", ""),
            create_height=result.get("create_height", 0),
            expiry_height=result.get("expiry_height", 0),
            status=result.get("status", "unknown"),
            preimage=result.get("preimage"),
            resolve_txid=result.get("resolve_txid"),
        )

    def list_htlcs(self, status: str = None, hashlock: str = None) -> List[M1HTLCRecord]:
        """
        List HTLCs.

        Args:
            status: Filter by status (active, claimed, refunded)
            hashlock: Filter by hashlock

        Returns:
            List of M1HTLCRecord
        """
        results = self.client.htlc_list(status, hashlock)
        if not results:
            return []

        records = []
        for r in results:
            records.append(M1HTLCRecord(
                outpoint=r.get("outpoint", ""),
                hashlock=r.get("hashlock", ""),
                amount=r.get("amount", 0),
                claim_address=r.get("claim_address", ""),
                refund_address=r.get("refund_address", ""),
                create_height=r.get("create_height", 0),
                expiry_height=r.get("expiry_height", 0),
                status=r.get("status", "unknown"),
                preimage=r.get("preimage"),
                resolve_txid=r.get("resolve_txid"),
            ))
        return records

    def find_by_hashlock(self, hashlock: str) -> List[M1HTLCRecord]:
        """
        Find HTLCs by hashlock.

        Useful for cross-chain matching.
        """
        return self.list_htlcs(hashlock=hashlock)

    def verify_preimage(self, preimage: str, hashlock: str) -> bool:
        """Verify preimage matches hashlock."""
        return self.client.htlc_verify(preimage, hashlock)

    def extract_preimage_from_tx(self, txid: str) -> Optional[str]:
        """
        Extract preimage from a claim transaction.

        Useful for LP to learn preimage after user claims.
        """
        return self.client.htlc_extract_preimage(txid)

    def get_receipt_for_htlc(self, amount: int, address: str = None) -> Optional[str]:
        """
        Find a suitable M1 receipt for HTLC creation.

        Args:
            amount: Required amount
            address: Specific address (optional)

        Returns:
            Receipt outpoint (txid:vout) or None
        """
        receipts = self.client.list_m1_receipts()

        for r in receipts:
            r_sats = int(round(r.get("amount", 0) * 100_000_000))
            if r_sats >= amount:
                if address and r.get("address") != address:
                    continue
                return r.get("outpoint")

        return None

    def generate_htlc_params(self, amount: int, claim_address: str,
                            refund_address: str, expiry_blocks: int = 288
                            ) -> Tuple[HTLCParams, str]:
        """
        Generate HTLC parameters with new secret.

        Returns:
            (HTLCParams, secret)
        """
        secret, hashlock = self.generate_secret()

        current_height = self.client.get_block_count()
        expiry_height = current_height + expiry_blocks

        params = HTLCParams(
            hashlock=hashlock,
            timelock=expiry_height,
            amount=amount,
            recipient=claim_address,
            refund_address=refund_address,
            chain="M1",
        )

        return params, secret

    def ensure_receipt_available(self, amount: int) -> str:
        """
        Ensure an M1 receipt of sufficient amount is available.

        Will lock M0 -> M1 if needed.

        Args:
            amount: Required M1 amount

        Returns:
            Receipt outpoint

        Raises:
            RuntimeError if insufficient balance
        """
        # Check existing receipts
        receipt = self.get_receipt_for_htlc(amount)
        if receipt:
            return receipt

        # Check M0 balance and lock
        # BATHRON: 1 M0 = 1 sat, getbalance returns integer sats directly
        m0_data = self.client.get_balance()

        if isinstance(m0_data, dict):
            m0_balance = int(m0_data.get("m0", 0)) - int(m0_data.get("locked", 0))
        elif isinstance(m0_data, (int, float)):
            m0_balance = int(m0_data)
        else:
            m0_balance = 0

        log.info(f"M0 balance check: need {amount} sats, have {m0_balance} sats")

        if m0_balance < amount:
            raise RuntimeError(f"Insufficient balance. Need {amount}, have {m0_balance}")

        # BATHRON: 1 M0 = 1 sat, RPC lock expects integer sats directly
        log.info(f"Locking {amount} M0 -> M1 (sats)")
        result = self.client.lock(amount)

        if not result or not result.get("txid"):
            raise RuntimeError("Failed to lock M0 -> M1")

        # Return the new receipt
        # Note: May need to wait for confirmation
        # Receipt is at vout[1] (vout[0] is vault OP_TRUE)
        return f"{result['txid']}:1"
