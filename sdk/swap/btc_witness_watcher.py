#!/usr/bin/env python3
"""
BTC Witness Watcher - Extracts secrets from BTC HTLC claims.

This is the KEY component for atomicity:
- Monitors BTC mempool/blocks for claim transactions
- Extracts (S_user, S_lp1, S_lp2) from witness data
- Triggers EVM claim ONLY when secrets are revealed on-chain

RULE: Nobody knows all 3 secrets until BTC claim is broadcast.
"""

import hashlib
import struct
import json
import time
import subprocess
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass
from enum import Enum


class RevealSource(Enum):
    """Source of secret revelation."""
    NONE = "none"
    BTC_MEMPOOL = "btc_mempool"
    BTC_BLOCK = "btc_block"
    # NEVER allow this in production:
    # FILE = "file"  # ← This bypasses atomicity!


@dataclass
class RevealedSecrets:
    """Secrets extracted from BTC witness."""
    s_user: bytes
    s_lp1: bytes
    s_lp2: bytes
    source: RevealSource
    btc_txid: str
    btc_block_height: Optional[int] = None


def parse_witness_stack(witness_hex: str) -> List[bytes]:
    """
    Parse witness stack from hex.

    Returns list of witness items.
    """
    data = bytes.fromhex(witness_hex)
    pos = 0
    items = []

    # First byte is item count (varint)
    count = data[pos]
    pos += 1

    for _ in range(count):
        # Each item: length (varint) + data
        length = data[pos]
        pos += 1

        if length == 0xfd:
            length = struct.unpack('<H', data[pos:pos+2])[0]
            pos += 2
        elif length == 0xfe:
            length = struct.unpack('<I', data[pos:pos+4])[0]
            pos += 4

        item = data[pos:pos+length]
        pos += length
        items.append(item)

    return items


def extract_secrets_from_claim_witness(witness_items: List[bytes]) -> Optional[Tuple[bytes, bytes, bytes]]:
    """
    Extract 3 secrets from HTLC claim witness.

    Expected witness structure for claim:
        [0] <signature>
        [1] <S_lp2>
        [2] <S_lp1>
        [3] <S_user>
        [4] <01> (OP_TRUE for IF branch)
        [5] <witnessScript>

    Returns (S_user, S_lp1, S_lp2) or None if not a valid claim.
    """
    if len(witness_items) < 6:
        return None

    # Check item 4 is OP_TRUE (claim path)
    if witness_items[4] != b'\x01':
        return None

    # Secrets are at positions 1, 2, 3 (reversed order)
    s_lp2 = witness_items[1]
    s_lp1 = witness_items[2]
    s_user = witness_items[3]

    # Validate lengths (should be 32 bytes each)
    if len(s_user) != 32 or len(s_lp1) != 32 or len(s_lp2) != 32:
        return None

    return (s_user, s_lp1, s_lp2)


def verify_secrets_match_hashlocks(
    s_user: bytes,
    s_lp1: bytes,
    s_lp2: bytes,
    h_user: bytes,
    h_lp1: bytes,
    h_lp2: bytes
) -> bool:
    """Verify that secrets hash to expected hashlocks."""
    return (
        hashlib.sha256(s_user).digest() == h_user and
        hashlib.sha256(s_lp1).digest() == h_lp1 and
        hashlib.sha256(s_lp2).digest() == h_lp2
    )


class BTCWitnessWatcher:
    """
    Watches BTC chain for HTLC claim transactions and extracts secrets.
    """

    def __init__(self, btc_rpc_url: str = None, btc_cli_path: str = None):
        """
        Initialize watcher.

        Args:
            btc_rpc_url: Bitcoin RPC URL (for API-based access)
            btc_cli_path: Path to bitcoin-cli (for CLI-based access)
        """
        self.btc_rpc_url = btc_rpc_url
        self.btc_cli_path = btc_cli_path or "/home/ubuntu/bitcoin/bin/bitcoin-cli"
        self.btc_datadir = "/home/ubuntu/.bitcoin-signet"

        # Tracked HTLCs: htlc_address -> {hashlocks, callback}
        self.tracked_htlcs: Dict[str, dict] = {}

        # Revealed secrets: htlc_address -> RevealedSecrets
        self.revealed: Dict[str, RevealedSecrets] = {}

    def _run_cli(self, cmd: str) -> str:
        """Run bitcoin-cli command."""
        full_cmd = f"{self.btc_cli_path} -signet -datadir={self.btc_datadir} {cmd}"
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
        return result.stdout.strip()

    def track_htlc(
        self,
        htlc_address: str,
        h_user: bytes,
        h_lp1: bytes,
        h_lp2: bytes,
        callback: callable = None
    ):
        """
        Start tracking an HTLC for claim transactions.

        Args:
            htlc_address: The P2WSH HTLC address
            h_user, h_lp1, h_lp2: Expected hashlocks
            callback: Function to call when secrets are revealed
        """
        self.tracked_htlcs[htlc_address] = {
            'h_user': h_user,
            'h_lp1': h_lp1,
            'h_lp2': h_lp2,
            'callback': callback
        }
        print(f"[Watcher] Tracking HTLC: {htlc_address}")

    def check_mempool(self) -> List[RevealedSecrets]:
        """
        Check mempool for claim transactions.

        Returns list of newly revealed secrets.
        """
        revealed = []

        try:
            # Get raw mempool
            mempool = json.loads(self._run_cli("getrawmempool true"))

            for txid, tx_info in mempool.items():
                # Get full transaction
                raw_tx = self._run_cli(f"getrawtransaction {txid} true")
                if not raw_tx:
                    continue

                tx = json.loads(raw_tx)
                secrets = self._check_transaction(tx, txid, RevealSource.BTC_MEMPOOL)
                if secrets:
                    revealed.append(secrets)

        except Exception as e:
            print(f"[Watcher] Mempool check error: {e}")

        return revealed

    def check_block(self, block_height: int) -> List[RevealedSecrets]:
        """
        Check a specific block for claim transactions.

        Returns list of newly revealed secrets.
        """
        revealed = []

        try:
            block_hash = self._run_cli(f"getblockhash {block_height}")
            block = json.loads(self._run_cli(f"getblock {block_hash} 2"))

            for tx in block.get('tx', []):
                txid = tx['txid']
                secrets = self._check_transaction(tx, txid, RevealSource.BTC_BLOCK, block_height)
                if secrets:
                    revealed.append(secrets)

        except Exception as e:
            print(f"[Watcher] Block {block_height} check error: {e}")

        return revealed

    def _check_transaction(
        self,
        tx: dict,
        txid: str,
        source: RevealSource,
        block_height: int = None
    ) -> Optional[RevealedSecrets]:
        """
        Check if transaction is a claim for any tracked HTLC.
        """
        for vin in tx.get('vin', []):
            # Get the address being spent
            prev_txid = vin.get('txid')
            prev_vout = vin.get('vout')

            if not prev_txid:
                continue

            # Get previous tx to find the address
            try:
                prev_tx = json.loads(self._run_cli(f"getrawtransaction {prev_txid} true"))
                prev_output = prev_tx['vout'][prev_vout]
                address = prev_output['scriptPubKey'].get('address')
            except:
                continue

            if address not in self.tracked_htlcs:
                continue

            # This is spending our tracked HTLC!
            htlc_info = self.tracked_htlcs[address]

            # Extract witness
            witness = vin.get('txinwitness', [])
            if len(witness) < 6:
                continue

            # Parse witness items
            witness_items = [bytes.fromhex(w) for w in witness]
            secrets = extract_secrets_from_claim_witness(witness_items)

            if not secrets:
                continue

            s_user, s_lp1, s_lp2 = secrets

            # Verify secrets match hashlocks
            if not verify_secrets_match_hashlocks(
                s_user, s_lp1, s_lp2,
                htlc_info['h_user'], htlc_info['h_lp1'], htlc_info['h_lp2']
            ):
                print(f"[Watcher] Secrets don't match hashlocks for {address}")
                continue

            # Success! Secrets revealed on-chain
            revealed = RevealedSecrets(
                s_user=s_user,
                s_lp1=s_lp1,
                s_lp2=s_lp2,
                source=source,
                btc_txid=txid,
                btc_block_height=block_height
            )

            self.revealed[address] = revealed

            print(f"[Watcher] ✅ SECRETS REVEALED!")
            print(f"  Source: {source.value}")
            print(f"  BTC TXID: {txid}")
            print(f"  S_user: {s_user.hex()[:16]}...")
            print(f"  S_lp1:  {s_lp1.hex()[:16]}...")
            print(f"  S_lp2:  {s_lp2.hex()[:16]}...")

            # Call callback if set
            if htlc_info.get('callback'):
                htlc_info['callback'](revealed)

            return revealed

        return None

    def get_revealed_secrets(self, htlc_address: str) -> Optional[RevealedSecrets]:
        """
        Get revealed secrets for an HTLC.

        Returns None if secrets haven't been revealed on-chain yet.
        """
        return self.revealed.get(htlc_address)

    def wait_for_reveal(
        self,
        htlc_address: str,
        timeout: int = 3600,
        poll_interval: int = 10
    ) -> Optional[RevealedSecrets]:
        """
        Wait for secrets to be revealed on-chain.

        Args:
            htlc_address: HTLC address to watch
            timeout: Maximum time to wait (seconds)
            poll_interval: How often to check (seconds)

        Returns RevealedSecrets when found, None on timeout.
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check if already revealed
            if htlc_address in self.revealed:
                return self.revealed[htlc_address]

            # Check mempool
            self.check_mempool()

            if htlc_address in self.revealed:
                return self.revealed[htlc_address]

            # Check recent blocks
            try:
                tip_height = int(self._run_cli("getblockcount"))
                for h in range(max(0, tip_height - 6), tip_height + 1):
                    self.check_block(h)
                    if htlc_address in self.revealed:
                        return self.revealed[htlc_address]
            except:
                pass

            time.sleep(poll_interval)

        return None


def gate_evm_claim(revealed: RevealedSecrets) -> bool:
    """
    Gate function: Only allow EVM claim if secrets came from a CONFIRMED BTC block.

    This is THE CRITICAL CHECK for atomicity.
    BTC_MEMPOOL is NOT allowed: the user could RBF the funding TX after the
    LP broadcasts a BTC claim, making mempool secrets unreliable proof.
    Only secrets confirmed in a mined block are irrevocable.
    """
    allowed_sources = {RevealSource.BTC_BLOCK}

    if revealed.source not in allowed_sources:
        print(f"BLOCKED: Secrets not from confirmed BTC block (source: {revealed.source})")
        return False

    if not revealed.btc_txid:
        print("BLOCKED: No BTC txid provided")
        return False

    print(f"ALLOWED: Secrets confirmed in BTC block ({revealed.source.value})")
    print(f"   BTC TXID: {revealed.btc_txid}")
    return True


if __name__ == '__main__':
    print("BTC Witness Watcher")
    print("=" * 60)
    print()
    print("This watcher extracts secrets from BTC HTLC claim transactions.")
    print("It ensures atomicity by only allowing EVM claims after BTC reveal.")
    print()

    # Example usage
    watcher = BTCWitnessWatcher()

    # Track the MVP HTLC
    HTLC_ADDRESS = "tb1q959k2v75u5fx4kjgsr5gvq99hywt0kugq0q8kj70ff396yquexzshwxxtj"
    H_USER = bytes.fromhex("13ccc7087668869e62146ea776614c6ce10811c926ad583bda3d4a40864e05c0")
    H_LP1 = bytes.fromhex("bdb432bb6537578e70c37da156b1b38ff7b94fd0c8f194d24f51856fdd2a409d")
    H_LP2 = bytes.fromhex("ecfcb6c5a30a876e665a1b7ce99dc1d8a04f38790584dd56cf118e02af5f4df2")

    watcher.track_htlc(HTLC_ADDRESS, H_USER, H_LP1, H_LP2)

    print(f"Watching: {HTLC_ADDRESS}")
    print("Waiting for claim transaction...")
    print()
    print("Run this on a node with synced Signet to detect the claim.")
