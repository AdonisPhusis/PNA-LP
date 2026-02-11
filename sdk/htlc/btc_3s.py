"""
Bitcoin HTLC with 3-secrets support for FlowSwap protocol.

Creates P2WSH HTLCs that require 3 independent preimages to claim.
This enables trustless multi-party atomic swaps.

HTLC Script Structure (3-secrets):
    OP_IF
        # Claim path: verify 3 secrets independently
        OP_SHA256 <H_user> OP_EQUALVERIFY
        OP_SHA256 <H_lp1>  OP_EQUALVERIFY
        OP_SHA256 <H_lp2>  OP_EQUALVERIFY
        <recipient_pubkey> OP_CHECKSIG
    OP_ELSE
        # Refund path: after timeout
        <timelock> OP_CHECKLOCKTIMEVERIFY OP_DROP
        <refund_pubkey> OP_CHECKSIG
    OP_ENDIF

Witness for claim (LIFO stack order):
    <signature> <S_lp2> <S_lp1> <S_user> <0x01> <witnessScript>

Witness for refund:
    <signature> <0x00> <witnessScript>
"""

import hashlib
import struct
import logging
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Bitcoin Script opcodes
OP_0 = 0x00
OP_FALSE = 0x00
OP_TRUE = 0x51
OP_1 = 0x51
OP_IF = 0x63
OP_ELSE = 0x67
OP_ENDIF = 0x68
OP_DROP = 0x75
OP_DUP = 0x76
OP_EQUALVERIFY = 0x88
OP_CHECKSIG = 0xac
OP_CHECKLOCKTIMEVERIFY = 0xb1
OP_SHA256 = 0xa8
OP_HASH160 = 0xa9


@dataclass
class HTLC3SParams:
    """Parameters for 3-secret HTLC."""
    H_user: str      # SHA256 hash of S_user (hex)
    H_lp1: str       # SHA256 hash of S_lp1 (hex)
    H_lp2: str       # SHA256 hash of S_lp2 (hex)
    recipient_pubkey: str  # Compressed pubkey for claim (hex)
    refund_pubkey: str     # Compressed pubkey for refund (hex)
    timelock: int          # Absolute block height


@dataclass
class HTLC3SSecrets:
    """The 3 secrets for claiming an HTLC."""
    S_user: str  # 32 bytes hex
    S_lp1: str   # 32 bytes hex
    S_lp2: str   # 32 bytes hex


def push_data(data: bytes) -> bytes:
    """Create push data opcode for Bitcoin script."""
    length = len(data)
    if length < 0x4c:
        return bytes([length]) + data
    elif length <= 0xff:
        return bytes([0x4c, length]) + data
    elif length <= 0xffff:
        return bytes([0x4d]) + struct.pack('<H', length) + data
    else:
        return bytes([0x4e]) + struct.pack('<I', length) + data


def push_int(n: int) -> bytes:
    """Push integer to script (for timelock)."""
    if n == 0:
        return bytes([OP_0])
    elif 1 <= n <= 16:
        return bytes([0x50 + n])
    else:
        negative = n < 0
        abs_n = abs(n)
        result = []
        while abs_n:
            result.append(abs_n & 0xff)
            abs_n >>= 8
        if result[-1] & 0x80:
            result.append(0x80 if negative else 0x00)
        elif negative:
            result[-1] |= 0x80
        return push_data(bytes(result))


def sha256(data: bytes) -> bytes:
    """SHA256 hash."""
    return hashlib.sha256(data).digest()


def generate_secret() -> Tuple[str, str]:
    """Generate a random 32-byte secret and its SHA256 hash."""
    import secrets
    secret = secrets.token_bytes(32)
    hashlock = sha256(secret)
    return secret.hex(), hashlock.hex()


def verify_preimage(secret_hex: str, hashlock_hex: str) -> bool:
    """Verify that SHA256(secret) == hashlock."""
    secret = bytes.fromhex(secret_hex)
    hashlock = bytes.fromhex(hashlock_hex)
    return sha256(secret) == hashlock


def _encode_compact_size(n: int) -> bytes:
    """Encode integer as Bitcoin compact size (varint)."""
    if n < 253:
        return struct.pack('<B', n)
    elif n < 0x10000:
        return struct.pack('<BH', 253, n)
    elif n < 0x100000000:
        return struct.pack('<BI', 254, n)
    else:
        return struct.pack('<BQ', 255, n)


class BTCHTLC3S:
    """
    Bitcoin HTLC manager with 3-secret support.

    Creates and manages HTLCs on Bitcoin using P2WSH scripts
    that require 3 independent preimages to claim.
    """

    def __init__(self, btc_client):
        """
        Initialize with a BTCClient instance.

        Args:
            btc_client: BTCClient from sdk/chains/btc.py
        """
        self.client = btc_client

    def create_htlc_script_3s(self, params: HTLC3SParams) -> bytes:
        """
        Create HTLC redeem script with 3 hashlocks.

        Script structure:
            OP_IF
                OP_SHA256 <H_user> OP_EQUALVERIFY
                OP_SHA256 <H_lp1>  OP_EQUALVERIFY
                OP_SHA256 <H_lp2>  OP_EQUALVERIFY
                <recipient_pubkey> OP_CHECKSIG
            OP_ELSE
                <timelock> OP_CHECKLOCKTIMEVERIFY OP_DROP
                <refund_pubkey> OP_CHECKSIG
            OP_ENDIF

        Args:
            params: HTLC3SParams with hashlocks and pubkeys

        Returns:
            Redeem script bytes
        """
        H_user = bytes.fromhex(params.H_user)
        H_lp1 = bytes.fromhex(params.H_lp1)
        H_lp2 = bytes.fromhex(params.H_lp2)
        recipient = bytes.fromhex(params.recipient_pubkey)
        refund = bytes.fromhex(params.refund_pubkey)

        # Validate
        if len(H_user) != 32 or len(H_lp1) != 32 or len(H_lp2) != 32:
            raise ValueError("Hashlocks must be 32 bytes each")
        if len(recipient) != 33 or len(refund) != 33:
            raise ValueError("Pubkeys must be 33 bytes (compressed)")

        # Build script
        script = bytes([OP_IF])

        # Claim path: verify 3 secrets in order (S_user, S_lp1, S_lp2)
        # Stack at entry: <S_lp2> <S_lp1> <S_user> (top to bottom after OP_TRUE)
        # We verify S_user first (top of stack after OP_IF consumes the 0x01)

        # Verify S_user
        script += bytes([OP_SHA256])
        script += push_data(H_user)
        script += bytes([OP_EQUALVERIFY])

        # Verify S_lp1
        script += bytes([OP_SHA256])
        script += push_data(H_lp1)
        script += bytes([OP_EQUALVERIFY])

        # Verify S_lp2
        script += bytes([OP_SHA256])
        script += push_data(H_lp2)
        script += bytes([OP_EQUALVERIFY])

        # Signature check
        script += push_data(recipient)
        script += bytes([OP_CHECKSIG])

        # Refund path
        script += bytes([OP_ELSE])
        script += push_int(params.timelock)
        script += bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP])
        script += push_data(refund)
        script += bytes([OP_CHECKSIG])

        script += bytes([OP_ENDIF])

        return script

    def script_to_p2wsh_address(self, script: bytes, network: str = "signet") -> str:
        """
        Convert redeem script to P2WSH bech32 address.

        Args:
            script: Redeem script bytes
            network: bitcoin network (signet, testnet, mainnet)

        Returns:
            Bech32 P2WSH address
        """
        witness_program = sha256(script)

        hrp = {
            "mainnet": "bc",
            "testnet": "tb",
            "signet": "tb",
        }.get(network, "tb")

        return self._encode_bech32(hrp, 0, witness_program)

    def _encode_bech32(self, hrp: str, version: int, program: bytes) -> str:
        """Encode bech32 address."""
        CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

        def bech32_polymod(values):
            GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
            chk = 1
            for v in values:
                b = chk >> 25
                chk = ((chk & 0x1ffffff) << 5) ^ v
                for i in range(5):
                    chk ^= GEN[i] if ((b >> i) & 1) else 0
            return chk

        def bech32_hrp_expand(hrp):
            return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

        def bech32_create_checksum(hrp, data):
            values = bech32_hrp_expand(hrp) + data
            polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
            return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]

        def convertbits(data, frombits, tobits, pad=True):
            acc = 0
            bits = 0
            ret = []
            maxv = (1 << tobits) - 1
            for value in data:
                acc = (acc << frombits) | value
                bits += frombits
                while bits >= tobits:
                    bits -= tobits
                    ret.append((acc >> bits) & maxv)
            if pad and bits:
                ret.append((acc << (tobits - bits)) & maxv)
            return ret

        data = [version] + convertbits(program, 8, 5)
        checksum = bech32_create_checksum(hrp, data)
        return hrp + "1" + "".join([CHARSET[d] for d in data + checksum])

    def create_htlc_3s(
        self,
        amount_sats: int,
        H_user: str,
        H_lp1: str,
        H_lp2: str,
        recipient_pubkey: str,
        refund_pubkey: str,
        timeout_blocks: int = 72  # ~12h at 10 min/block
    ) -> Dict:
        """
        Create a new 3-secret HTLC.

        Args:
            amount_sats: Amount in satoshis
            H_user: SHA256 hash of user's secret (hex)
            H_lp1: SHA256 hash of LP1's secret (hex)
            H_lp2: SHA256 hash of LP2's secret (hex)
            recipient_pubkey: Compressed pubkey for claim path (hex)
            refund_pubkey: Compressed pubkey for refund path (hex)
            timeout_blocks: Blocks until refund (default 72 = ~12h)

        Returns:
            {
                "htlc_address": "tb1...",
                "redeem_script": "hex...",
                "amount": sats,
                "timelock": block_height,
                "H_user": "hex...",
                "H_lp1": "hex...",
                "H_lp2": "hex...",
            }
        """
        current_height = self.client.get_block_count()
        timelock = current_height + timeout_blocks

        params = HTLC3SParams(
            H_user=H_user,
            H_lp1=H_lp1,
            H_lp2=H_lp2,
            recipient_pubkey=recipient_pubkey,
            refund_pubkey=refund_pubkey,
            timelock=timelock,
        )

        script = self.create_htlc_script_3s(params)
        network = getattr(self.client.config, 'network', 'signet')
        htlc_address = self.script_to_p2wsh_address(script, network)

        log.info(f"Created 3S-HTLC: {htlc_address}, timelock={timelock}")

        return {
            "htlc_address": htlc_address,
            "redeem_script": script.hex(),
            "amount": amount_sats,
            "timelock": timelock,
            "H_user": H_user,
            "H_lp1": H_lp1,
            "H_lp2": H_lp2,
            "recipient_pubkey": recipient_pubkey,
            "refund_pubkey": refund_pubkey,
        }

    def fund_htlc(self, htlc_address: str, amount_sats: int) -> str:
        """
        Fund an HTLC address from wallet.

        Args:
            htlc_address: P2WSH HTLC address
            amount_sats: Amount in satoshis

        Returns:
            Funding transaction ID
        """
        amount_btc = amount_sats / 100_000_000
        txid = self.client._call("sendtoaddress", htlc_address, f"{amount_btc:.8f}")
        log.info(f"Funded 3S-HTLC {htlc_address} with {amount_sats} sats, txid={txid}")
        return txid

    def check_htlc_funded(
        self,
        htlc_address: str,
        expected_amount: int,
        min_confirmations: int = 1
    ) -> Optional[Dict]:
        """
        Check if HTLC has been funded.

        Uses scantxoutset for confirmed UTXOs, and mempool scan for 0-conf.

        Args:
            htlc_address: HTLC address to check
            expected_amount: Expected amount in sats
            min_confirmations: Required confirmations

        Returns:
            UTXO info if funded, None otherwise
        """
        import json

        # 1. Check confirmed UTXO set
        try:
            scan_result = self.client._call(
                "scantxoutset", "start",
                json.dumps([f"addr({htlc_address})"])
            )

            if scan_result and scan_result.get("success"):
                current_height = scan_result.get("height", 0)
                for utxo in scan_result.get("unspents", []):
                    amount_sats = int(round(utxo["amount"] * 100_000_000))
                    utxo_height = utxo.get("height", current_height)
                    confirmations = current_height - utxo_height + 1 if utxo_height > 0 else 0

                    if amount_sats >= expected_amount and confirmations >= min_confirmations:
                        return {
                            "txid": utxo["txid"],
                            "vout": utxo["vout"],
                            "amount": amount_sats,
                            "confirmations": confirmations,
                        }
        except Exception as e:
            log.error(f"scantxoutset failed: {e}")

        # 2. If 0-conf accepted, scan mempool for unconfirmed TX to HTLC address
        if min_confirmations == 0:
            try:
                mempool_txids = self.client._call("getrawmempool")
                if mempool_txids:
                    for txid in mempool_txids:
                        try:
                            tx = self.client._call("getrawtransaction", txid, True)
                            if not tx:
                                continue
                            for vout in tx.get("vout", []):
                                spk = vout.get("scriptPubKey", {})
                                addr = spk.get("address", "")
                                if not addr:
                                    addrs = spk.get("addresses", [])
                                    addr = addrs[0] if addrs else ""
                                amount_sats = int(round(vout.get("value", 0) * 100_000_000))
                                if addr == htlc_address and amount_sats >= expected_amount:
                                    log.info(f"Found 0-conf TX in mempool: {txid} vout={vout['n']} "
                                             f"amount={amount_sats} sats")
                                    return {
                                        "txid": txid,
                                        "vout": vout["n"],
                                        "amount": amount_sats,
                                        "confirmations": 0,
                                    }
                        except Exception:
                            continue  # Skip TXs we can't decode
            except Exception as e:
                log.error(f"Mempool scan failed: {e}")

        return None

    def verify_tx_safe_for_0conf(
        self,
        txid: str,
        htlc_address: str,
        expected_amount_sats: int,
        min_fee_rate: float = 1.0,
    ) -> Dict:
        """
        Verify a BTC transaction is safe to accept at 0-conf.

        Checks:
        1. TX exists in mempool or blockchain
        2. NOT RBF-signaled (all inputs nSequence >= 0xFFFFFFFE)
        3. Fee rate >= min_fee_rate sat/vB
        4. Output goes to expected HTLC address with correct amount

        Args:
            txid: Transaction ID to verify
            htlc_address: Expected HTLC address
            expected_amount_sats: Expected amount in satoshis
            min_fee_rate: Minimum fee rate in sat/vB (default 1.0)

        Returns:
            Dict with: safe (bool), reason (str), details (dict)
        """
        import json

        result = {"safe": False, "reason": "", "details": {}}

        # 1. Get raw transaction
        try:
            tx = self.client._call("getrawtransaction", txid, True)
        except Exception as e:
            result["reason"] = f"TX not found: {e}"
            return result

        if not tx:
            result["reason"] = "TX not found"
            return result

        # 2. Check RBF: any input with nSequence < 0xFFFFFFFE signals RBF (BIP125)
        rbf_signaled = False
        for vin in tx.get("vin", []):
            seq = vin.get("sequence", 0xFFFFFFFF)
            if seq < 0xFFFFFFFE:
                rbf_signaled = True
                break

        if rbf_signaled:
            result["reason"] = "TX is RBF-signaled (replaceable). Rejected for 0-conf."
            result["details"]["rbf"] = True
            return result

        result["details"]["rbf"] = False

        # 3. Verify output to HTLC address with correct amount
        output_found = False
        output_vout = -1
        output_amount_sats = 0
        for vout in tx.get("vout", []):
            spk = vout.get("scriptPubKey", {})
            addr = spk.get("address", "")
            # Also check addresses array for older Bitcoin Core versions
            if not addr:
                addrs = spk.get("addresses", [])
                addr = addrs[0] if addrs else ""
            amount_sats = int(round(vout.get("value", 0) * 100_000_000))
            if addr == htlc_address and amount_sats >= expected_amount_sats:
                output_found = True
                output_vout = vout.get("n", 0)
                output_amount_sats = amount_sats
                break

        if not output_found:
            result["reason"] = f"No output to {htlc_address} with >= {expected_amount_sats} sats"
            return result

        result["details"]["output_vout"] = output_vout
        result["details"]["output_amount_sats"] = output_amount_sats

        # 4. Check fee rate via getmempoolentry (only works if TX is in mempool)
        confirmations = tx.get("confirmations", 0)
        if confirmations > 0:
            # Already confirmed — safe by definition
            result["safe"] = True
            result["reason"] = f"Already confirmed ({confirmations} confs)"
            result["details"]["confirmations"] = confirmations
            result["details"]["fee_rate"] = None
            return result

        try:
            mempool_entry = self.client._call("getmempoolentry", txid)
            if mempool_entry:
                fee_btc = mempool_entry.get("fees", {}).get("base", 0)
                if isinstance(fee_btc, str):
                    fee_btc = float(fee_btc)
                fee_sats = int(round(fee_btc * 100_000_000))
                vsize = mempool_entry.get("vsize", 1)
                fee_rate = fee_sats / vsize if vsize > 0 else 0

                result["details"]["fee_sats"] = fee_sats
                result["details"]["vsize"] = vsize
                result["details"]["fee_rate"] = round(fee_rate, 2)

                if fee_rate < min_fee_rate:
                    result["reason"] = (
                        f"Fee rate too low: {fee_rate:.1f} sat/vB "
                        f"(min {min_fee_rate} sat/vB). Risk of non-confirmation."
                    )
                    return result

                # Also check bip125-replaceable from mempool
                if mempool_entry.get("bip125-replaceable", False):
                    result["reason"] = "TX flagged bip125-replaceable by mempool. Rejected for 0-conf."
                    result["details"]["rbf"] = True
                    return result

        except Exception as e:
            # FAIL-CLOSED: if we can't verify fee/RBF from mempool, reject 0-conf
            log.warning(f"getmempoolentry failed for {txid}: {e}")
            result["reason"] = f"Cannot verify mempool entry (fee/RBF): {e}. Rejected for 0-conf safety."
            result["details"]["fee_rate"] = None
            return result

        # All checks passed
        result["safe"] = True
        result["reason"] = "TX passes 0-conf safety checks (non-RBF, adequate fee, correct output)"
        return result

    def build_claim_witness_3s(
        self,
        secrets: HTLC3SSecrets,
        signature: bytes,
        redeem_script: bytes
    ) -> List[bytes]:
        """
        Build witness stack for claiming a 3-secret HTLC.

        Witness order (bottom to top of stack):
            [0] signature
            [1] S_lp2 (32 bytes)
            [2] S_lp1 (32 bytes)
            [3] S_user (32 bytes)
            [4] 0x01 (OP_TRUE for IF branch)
            [5] witnessScript

        Args:
            secrets: HTLC3SSecrets with S_user, S_lp1, S_lp2
            signature: DER signature with SIGHASH byte
            redeem_script: The witness script

        Returns:
            List of witness stack elements
        """
        S_user = bytes.fromhex(secrets.S_user)
        S_lp1 = bytes.fromhex(secrets.S_lp1)
        S_lp2 = bytes.fromhex(secrets.S_lp2)

        if len(S_user) != 32 or len(S_lp1) != 32 or len(S_lp2) != 32:
            raise ValueError("All secrets must be 32 bytes")

        # Witness stack (order matters!)
        return [
            signature,          # [0]
            S_lp2,              # [1] - pushed first, verified last
            S_lp1,              # [2]
            S_user,             # [3] - pushed last, verified first
            bytes([0x01]),      # [4] - selects IF branch
            redeem_script,      # [5]
        ]

    def extract_secrets_from_witness(self, witness: List[bytes]) -> Optional[HTLC3SSecrets]:
        """
        Extract the 3 secrets from a claim transaction witness.

        Expected witness structure:
            [0] signature
            [1] S_lp2 (32 bytes)
            [2] S_lp1 (32 bytes)
            [3] S_user (32 bytes)
            [4] 0x01 (branch selector)
            [5] witnessScript

        Args:
            witness: List of witness stack elements

        Returns:
            HTLC3SSecrets if valid claim witness, None otherwise
        """
        if len(witness) < 6:
            log.warning(f"Witness too short: {len(witness)} elements")
            return None

        # Check if this is a claim (branch selector = 0x01)
        branch = witness[4]
        if branch != bytes([0x01]) and branch != b'\x01':
            log.debug("Not a claim witness (refund branch)")
            return None

        # Extract secrets
        S_lp2 = witness[1]
        S_lp1 = witness[2]
        S_user = witness[3]

        # Validate sizes
        if len(S_user) != 32 or len(S_lp1) != 32 or len(S_lp2) != 32:
            log.warning(f"Invalid secret sizes: {len(S_user)}, {len(S_lp1)}, {len(S_lp2)}")
            return None

        return HTLC3SSecrets(
            S_user=S_user.hex(),
            S_lp1=S_lp1.hex(),
            S_lp2=S_lp2.hex(),
        )

    def extract_secrets_from_txid(self, txid: str, vin_index: int = 0) -> Optional[HTLC3SSecrets]:
        """
        Extract 3 secrets from a claim transaction by txid.

        Args:
            txid: Transaction ID of the claim transaction
            vin_index: Input index (default 0)

        Returns:
            HTLC3SSecrets if found, None otherwise
        """
        try:
            raw_tx = self.client._call("getrawtransaction", txid, True)
            if not raw_tx:
                return None

            vin = raw_tx.get("vin", [])
            if vin_index >= len(vin):
                return None

            txinwitness = vin[vin_index].get("txinwitness", [])
            if not txinwitness:
                return None

            # Convert hex strings to bytes
            witness = [bytes.fromhex(w) for w in txinwitness]
            return self.extract_secrets_from_witness(witness)

        except Exception as e:
            log.error(f"Failed to extract secrets from {txid}: {e}")
            return None

    def presign_claim_3s(
        self,
        utxo: Dict,
        redeem_script: str,
        recipient_address: str,
        claim_privkey_wif: str,
        fee_rate_sat_vb: int = 2
    ) -> Dict:
        """
        Pre-sign BTC claim TX (before secrets are known).

        In segwit P2WSH, the sighash covers TX structure (inputs/outputs)
        but NOT the witness stack (secrets). So we can sign before knowing
        S_user, S_lp1, S_lp2 — they're assembled into the witness later.

        Args:
            utxo: UTXO dict with txid, vout, amount (sats)
            redeem_script: Redeem script hex
            recipient_address: Where to send claimed funds
            claim_privkey_wif: WIF private key for signing
            fee_rate_sat_vb: Fee rate in sat/vbyte

        Returns:
            {
                "raw_tx": unsigned TX hex,
                "signature": DER signature with SIGHASH byte (hex),
                "recipient_address": str,
                "utxo": dict,
                "redeem_script": str,
            }
        """
        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]

        log.info(f"Pre-signing 3S-HTLC claim: {txid}:{vout}")

        script_bytes = bytes.fromhex(redeem_script)

        # Estimate fee
        estimated_vsize = 180 + len(script_bytes) // 4
        fee_sats = estimated_vsize * fee_rate_sat_vb

        output_amount_sats = amount_sats - fee_sats
        if output_amount_sats <= 546:
            raise ValueError(f"Output {output_amount_sats} below dust")

        output_amount_btc = output_amount_sats / 100_000_000

        # Create raw transaction
        inputs = [{"txid": txid, "vout": vout}]
        outputs = {recipient_address: f"{output_amount_btc:.8f}"}

        raw_tx = self.client.create_raw_transaction(inputs, outputs)

        # Sign (sighash doesn't depend on witness secrets)
        try:
            from bitcoin import SelectParams
            from bitcoin.core import CMutableTransaction, CScript
            from bitcoin.core.script import (
                SignatureHash, SIGHASH_ALL, SIGVERSION_WITNESS_V0
            )
            from bitcoin.wallet import CBitcoinSecret

            SelectParams('signet')

            tx = CMutableTransaction.deserialize(bytes.fromhex(raw_tx))
            privkey = CBitcoinSecret(claim_privkey_wif)
            witness_script = CScript(script_bytes)

            sighash = SignatureHash(
                script=witness_script,
                txTo=tx,
                inIdx=0,
                hashtype=SIGHASH_ALL,
                amount=amount_sats,
                sigversion=SIGVERSION_WITNESS_V0
            )

            sig = privkey.sign(sighash) + bytes([SIGHASH_ALL])
            log.info(f"Pre-sign OK: sig={sig.hex()[:20]}...")

            return {
                "raw_tx": raw_tx,
                "signature": sig.hex(),
                "recipient_address": recipient_address,
                "utxo": utxo,
                "redeem_script": redeem_script,
            }

        except ImportError:
            raise NotImplementedError(
                "python-bitcoinlib required for 3S-HTLC pre-signing. "
                "Install with: pip install python-bitcoinlib"
            )

    def broadcast_presigned_claim_3s(
        self,
        presign_data: Dict,
        secrets: HTLC3SSecrets,
    ) -> str:
        """
        Assemble witness from pre-signed data + secrets and broadcast.

        Args:
            presign_data: Result from presign_claim_3s()
            secrets: HTLC3SSecrets with S_user, S_lp1, S_lp2

        Returns:
            Claim transaction ID
        """
        raw_tx = presign_data["raw_tx"]
        sig = bytes.fromhex(presign_data["signature"])
        redeem_script = presign_data["redeem_script"]
        script_bytes = bytes.fromhex(redeem_script)

        # Verify preimages match script hashlocks
        self._verify_preimages_match_script(secrets, script_bytes)

        # Build witness stack
        witness_stack = self.build_claim_witness_3s(secrets, sig, script_bytes)

        # Assemble segwit TX with witness
        from bitcoin import SelectParams
        from bitcoin.core import CMutableTransaction, CTxInWitness, CTxWitness, CScriptWitness, b2x

        SelectParams('signet')

        tx = CMutableTransaction.deserialize(bytes.fromhex(raw_tx))

        script_witness = CScriptWitness(witness_stack)
        txin_witness = CTxInWitness(script_witness)
        tx.wit = CTxWitness([txin_witness])

        signed_hex = b2x(tx.serialize())
        log.info(f"Assembled pre-signed claim: {signed_hex[:40]}...")

        # Broadcast
        claim_txid = self.client.send_raw_transaction(signed_hex)
        log.info(f"Pre-signed 3S-HTLC claimed: txid={claim_txid}")
        return claim_txid

    def claim_htlc_3s(
        self,
        utxo: Dict,
        redeem_script: str,
        secrets: HTLC3SSecrets,
        recipient_address: str,
        claim_privkey_wif: str,
        fee_rate_sat_vb: int = 2
    ) -> str:
        """
        Claim HTLC with 3 preimages.

        Args:
            utxo: UTXO dict with txid, vout, amount (sats)
            redeem_script: Redeem script hex
            secrets: HTLC3SSecrets with S_user, S_lp1, S_lp2
            recipient_address: Where to send claimed funds
            claim_privkey_wif: WIF private key for signing
            fee_rate_sat_vb: Fee rate in sat/vbyte

        Returns:
            Claim transaction ID
        """
        import json

        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]

        log.info(f"Claiming 3S-HTLC: {txid}:{vout}, amount={amount_sats} sats")

        # Verify preimages match hashlocks in script
        script_bytes = bytes.fromhex(redeem_script)
        self._verify_preimages_match_script(secrets, script_bytes)

        # Calculate P2WSH scriptPubKey
        witness_program = sha256(script_bytes)
        script_pubkey = bytes([OP_0, 0x20]) + witness_program

        # Estimate fee (3S witness is larger: ~110 + 3*32 + script_len)
        estimated_vsize = 180 + len(script_bytes) // 4
        fee_sats = estimated_vsize * fee_rate_sat_vb

        output_amount_sats = amount_sats - fee_sats
        if output_amount_sats <= 546:
            raise ValueError(f"Output {output_amount_sats} below dust")

        output_amount_btc = output_amount_sats / 100_000_000

        # Create raw transaction
        inputs = [{"txid": txid, "vout": vout}]
        outputs = {recipient_address: f"{output_amount_btc:.8f}"}

        raw_tx = self.client.create_raw_transaction(inputs, outputs)
        log.info(f"Created raw TX: {raw_tx[:40]}...")

        # Sign transaction
        # For 3S-HTLC, we need manual witness construction
        signed_tx = self._sign_claim_3s(
            raw_tx, utxo, redeem_script, secrets, claim_privkey_wif
        )

        # Broadcast
        claim_txid = self.client.send_raw_transaction(signed_tx)
        log.info(f"3S-HTLC claimed: txid={claim_txid}")

        return claim_txid

    def _verify_preimages_match_script(self, secrets: HTLC3SSecrets, script: bytes):
        """Verify that the provided secrets match the hashlocks in the script."""
        # Script structure:
        # [0] OP_IF
        # [1] OP_SHA256
        # [2] PUSH32 (0x20)
        # [3:35] H_user (32 bytes)
        # [35] OP_EQUALVERIFY
        # [36] OP_SHA256
        # [37] PUSH32 (0x20)
        # [38:70] H_lp1 (32 bytes)
        # [70] OP_EQUALVERIFY
        # [71] OP_SHA256
        # [72] PUSH32 (0x20)
        # [73:105] H_lp2 (32 bytes)
        # ...

        if len(script) < 105:
            raise ValueError("Script too short for 3S-HTLC")

        H_user_script = script[3:35]
        H_lp1_script = script[38:70]
        H_lp2_script = script[73:105]

        S_user = bytes.fromhex(secrets.S_user)
        S_lp1 = bytes.fromhex(secrets.S_lp1)
        S_lp2 = bytes.fromhex(secrets.S_lp2)

        if sha256(S_user) != H_user_script:
            raise ValueError("S_user does not match H_user in script")
        if sha256(S_lp1) != H_lp1_script:
            raise ValueError("S_lp1 does not match H_lp1 in script")
        if sha256(S_lp2) != H_lp2_script:
            raise ValueError("S_lp2 does not match H_lp2 in script")

        log.info("All 3 preimages verified against script hashlocks")

    def _sign_claim_3s(
        self,
        unsigned_tx: str,
        utxo: Dict,
        redeem_script: str,
        secrets: HTLC3SSecrets,
        privkey_wif: str
    ) -> str:
        """
        Sign a claim transaction for 3S-HTLC using python-bitcoinlib.

        Args:
            unsigned_tx: Raw unsigned transaction hex
            utxo: UTXO being spent
            redeem_script: Witness script hex
            secrets: The 3 secrets
            privkey_wif: WIF private key

        Returns:
            Signed transaction hex
        """
        try:
            from bitcoin import SelectParams
            from bitcoin.core import (
                CMutableTransaction, CScript, b2x
            )
            from bitcoin.core.script import (
                SignatureHash, SIGHASH_ALL, SIGVERSION_WITNESS_V0
            )
            from bitcoin.wallet import CBitcoinSecret

            SelectParams('signet')

            # Parse transaction
            tx = CMutableTransaction.deserialize(bytes.fromhex(unsigned_tx))

            # Get signing key
            privkey = CBitcoinSecret(privkey_wif)

            # Build witness script
            script_bytes = bytes.fromhex(redeem_script)
            witness_script = CScript(script_bytes)

            # Calculate sighash
            amount_sats = utxo["amount"]
            sighash = SignatureHash(
                script=witness_script,
                txTo=tx,
                inIdx=0,
                hashtype=SIGHASH_ALL,
                amount=amount_sats,
                sigversion=SIGVERSION_WITNESS_V0
            )

            # Sign
            sig = privkey.sign(sighash) + bytes([SIGHASH_ALL])

            # Build witness stack
            witness_stack = self.build_claim_witness_3s(
                secrets, sig, script_bytes
            )

            # Set witness - need to use mutable witness objects
            from bitcoin.core import CTxInWitness, CTxWitness, CScriptWitness

            # Create witness for the input
            script_witness = CScriptWitness(witness_stack)
            txin_witness = CTxInWitness(script_witness)

            # Create new witness data
            tx.wit = CTxWitness([txin_witness])

            signed_hex = b2x(tx.serialize())
            log.info(f"Signed 3S claim TX: {signed_hex[:40]}...")

            return signed_hex

        except ImportError:
            raise NotImplementedError(
                "python-bitcoinlib required for 3S-HTLC signing. "
                "Install with: pip install python-bitcoinlib"
            )

    def refund_htlc_3s(
        self,
        utxo: Dict,
        redeem_script: str,
        refund_address: str,
        refund_privkey_wif: str = "",
        timelock: int = 0,
        fee_rate_sat_vb: int = 2
    ) -> str:
        """
        Refund expired 3S-HTLC.

        Args:
            utxo: UTXO dict with txid, vout, amount (sats)
            redeem_script: Redeem script hex
            refund_address: Where to send refunded funds
            refund_privkey_wif: WIF private key for refund path
            timelock: Original timelock (must have passed)
            fee_rate_sat_vb: Fee rate

        Returns:
            Refund transaction ID
        """
        import json

        current_height = self.client.get_block_count()
        if current_height < timelock:
            raise ValueError(
                f"Cannot refund yet. Current {current_height}, "
                f"timelock {timelock}. Wait {timelock - current_height} blocks."
            )

        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]

        log.info(f"Refunding 3S-HTLC: {txid}:{vout}")

        script_bytes = bytes.fromhex(redeem_script)
        witness_program = sha256(script_bytes)
        script_pubkey = bytes([OP_0, 0x20]) + witness_program

        # Fee
        estimated_vsize = 120 + len(script_bytes) // 4
        fee_sats = estimated_vsize * fee_rate_sat_vb

        output_amount_sats = amount_sats - fee_sats
        if output_amount_sats <= 546:
            raise ValueError(f"Output {output_amount_sats} below dust")

        output_amount_btc = output_amount_sats / 100_000_000

        # Create with nLockTime
        inputs = [{"txid": txid, "vout": vout, "sequence": 0xFFFFFFFE}]
        outputs = {refund_address: f"{output_amount_btc:.8f}"}

        raw_tx = self.client._call(
            "createrawtransaction",
            json.dumps(inputs),
            json.dumps(outputs),
            timelock
        )

        # Sign refund (WIF direct or via Bitcoin Core wallet)
        if refund_privkey_wif:
            signed_tx = self._sign_refund_3s(
                raw_tx, utxo, redeem_script, refund_privkey_wif
            )
        else:
            signed_tx = self._sign_refund_3s_wallet(
                raw_tx, utxo, redeem_script
            )

        refund_txid = self.client.send_raw_transaction(signed_tx)
        log.info(f"3S-HTLC refunded: txid={refund_txid}")

        return refund_txid

    def _sign_refund_3s(
        self,
        unsigned_tx: str,
        utxo: Dict,
        redeem_script: str,
        privkey_wif: str
    ) -> str:
        """Sign refund transaction for 3S-HTLC.

        Uses python-bitcoinlib for sighash computation only,
        then builds the segwit TX manually to avoid immutable witness objects.
        """
        import struct

        from bitcoin import SelectParams
        from bitcoin.core import CMutableTransaction, CScript
        from bitcoin.core.script import (
            SignatureHash, SIGHASH_ALL, SIGVERSION_WITNESS_V0
        )
        from bitcoin.wallet import CBitcoinSecret

        SelectParams('signet')

        tx = CMutableTransaction.deserialize(bytes.fromhex(unsigned_tx))
        privkey = CBitcoinSecret(privkey_wif)

        script_bytes = bytes.fromhex(redeem_script)
        witness_script = CScript(script_bytes)

        amount_sats = utxo["amount"]
        sighash = SignatureHash(
            script=witness_script,
            txTo=tx,
            inIdx=0,
            hashtype=SIGHASH_ALL,
            amount=amount_sats,
            sigversion=SIGVERSION_WITNESS_V0
        )

        sig = privkey.sign(sighash) + bytes([SIGHASH_ALL])

        # Build segwit TX manually to avoid python-bitcoinlib immutable witness.
        # Non-segwit: [version(4)] [inputs+outputs] [locktime(4)]
        # Segwit:     [version(4)] [0x00 0x01] [inputs+outputs] [witness] [locktime(4)]
        unsigned_bytes = bytes.fromhex(unsigned_tx)
        version = unsigned_bytes[:4]
        locktime = unsigned_bytes[-4:]
        middle = unsigned_bytes[4:-4]

        # Witness for 1 input: [sig] [0x00 (ELSE)] [witnessScript]
        witness_stack = [sig, b'', script_bytes]
        witness_data = _encode_compact_size(len(witness_stack))
        for item in witness_stack:
            witness_data += _encode_compact_size(len(item)) + item

        signed_bytes = version + b'\x00\x01' + middle + witness_data + locktime
        return signed_bytes.hex()

    def _sign_refund_3s_wallet(
        self,
        unsigned_tx: str,
        utxo: Dict,
        redeem_script: str,
    ) -> str:
        """Sign refund using Bitcoin Core wallet (no WIF needed).

        Bitcoin Core handles P2WSH HTLC ELSE branch natively —
        signrawtransactionwithwallet produces the correct witness directly.
        """
        import json as _json

        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]
        amount_btc = amount_sats / 100_000_000

        script_bytes = bytes.fromhex(redeem_script)
        witness_program = sha256(script_bytes)
        script_pubkey_hex = (bytes([OP_0, 0x20]) + witness_program).hex()

        prevtxs = [{
            "txid": txid,
            "vout": vout,
            "scriptPubKey": script_pubkey_hex,
            "witnessScript": redeem_script,
            "amount": f"{amount_btc:.8f}",
        }]

        result = self.client._call(
            "signrawtransactionwithwallet",
            unsigned_tx,
            _json.dumps(prevtxs),
        )

        if not result:
            raise RuntimeError("signrawtransactionwithwallet returned nothing")

        if isinstance(result, str):
            result = _json.loads(result)

        if not result.get("complete"):
            errors = result.get("errors", [])
            raise RuntimeError(f"signrawtransactionwithwallet failed: {errors}")

        return result["hex"]


# Convenience functions for standalone use

def create_3s_hashlocks() -> Tuple[HTLC3SSecrets, Dict[str, str]]:
    """
    Generate 3 secrets and their hashlocks.

    Returns:
        (secrets, hashlocks) where:
        - secrets: HTLC3SSecrets with S_user, S_lp1, S_lp2
        - hashlocks: dict with H_user, H_lp1, H_lp2
    """
    S_user, H_user = generate_secret()
    S_lp1, H_lp1 = generate_secret()
    S_lp2, H_lp2 = generate_secret()

    secrets = HTLC3SSecrets(S_user=S_user, S_lp1=S_lp1, S_lp2=S_lp2)
    hashlocks = {"H_user": H_user, "H_lp1": H_lp1, "H_lp2": H_lp2}

    return secrets, hashlocks


def verify_3s_secrets(secrets: HTLC3SSecrets, hashlocks: Dict[str, str]) -> bool:
    """
    Verify that secrets match hashlocks.

    Returns:
        True if all 3 secrets are valid
    """
    return (
        verify_preimage(secrets.S_user, hashlocks["H_user"]) and
        verify_preimage(secrets.S_lp1, hashlocks["H_lp1"]) and
        verify_preimage(secrets.S_lp2, hashlocks["H_lp2"])
    )
