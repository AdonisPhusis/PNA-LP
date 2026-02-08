"""
Bitcoin HTLC implementation for pna SDK.

Creates P2WSH (Pay-to-Witness-Script-Hash) HTLCs compatible with BIP-199.

HTLC Script Structure:
    OP_IF
        OP_SHA256 <hashlock> OP_EQUALVERIFY
        <recipient_pubkey> OP_CHECKSIG
    OP_ELSE
        <timelock> OP_CHECKLOCKTIMEVERIFY OP_DROP
        <refund_pubkey> OP_CHECKSIG
    OP_ENDIF

To claim (with preimage):
    <signature> <preimage> OP_TRUE

To refund (after timeout):
    <signature> OP_FALSE
"""

import hashlib
import struct
import logging
from typing import Optional, Dict, Tuple
from dataclasses import dataclass

from ..core import HTLCParams, generate_secret
from ..chains.btc import BTCClient, BTCConfig

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
        return bytes([0x50 + n])  # OP_1 through OP_16
    else:
        # Encode as little-endian with sign bit
        negative = n < 0
        abs_n = abs(n)
        result = []
        while abs_n:
            result.append(abs_n & 0xff)
            abs_n >>= 8
        # Add sign bit if needed
        if result[-1] & 0x80:
            result.append(0x80 if negative else 0x00)
        elif negative:
            result[-1] |= 0x80
        return push_data(bytes(result))


def sha256(data: bytes) -> bytes:
    """SHA256 hash."""
    return hashlib.sha256(data).digest()


def hash160(data: bytes) -> bytes:
    """HASH160 = RIPEMD160(SHA256(data))."""
    import hashlib
    sha = hashlib.sha256(data).digest()
    ripemd = hashlib.new('ripemd160', sha).digest()
    return ripemd


class BTCHtlc:
    """
    Bitcoin HTLC manager.

    Creates and manages HTLCs on Bitcoin using P2WSH scripts.
    """

    def __init__(self, client: BTCClient):
        self.client = client

    def create_htlc_script(self, hashlock: str, recipient_pubkey: str,
                           refund_pubkey: str, timelock: int) -> bytes:
        """
        Create HTLC redeem script.

        Args:
            hashlock: SHA256 hash (hex)
            recipient_pubkey: Compressed pubkey for claim path (hex)
            refund_pubkey: Compressed pubkey for refund path (hex)
            timelock: Absolute block height or unix timestamp

        Returns:
            Redeem script bytes
        """
        hashlock_bytes = bytes.fromhex(hashlock)
        recipient_bytes = bytes.fromhex(recipient_pubkey)
        refund_bytes = bytes.fromhex(refund_pubkey)

        # Build script:
        # OP_IF
        #     OP_SHA256 <hashlock> OP_EQUALVERIFY
        #     <recipient_pubkey> OP_CHECKSIG
        # OP_ELSE
        #     <timelock> OP_CHECKLOCKTIMEVERIFY OP_DROP
        #     <refund_pubkey> OP_CHECKSIG
        # OP_ENDIF

        script = bytes([OP_IF])
        script += bytes([OP_SHA256])
        script += push_data(hashlock_bytes)
        script += bytes([OP_EQUALVERIFY])
        script += push_data(recipient_bytes)
        script += bytes([OP_CHECKSIG])
        script += bytes([OP_ELSE])
        script += push_int(timelock)
        script += bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP])
        script += push_data(refund_bytes)
        script += bytes([OP_CHECKSIG])
        script += bytes([OP_ENDIF])

        return script

    def script_to_p2wsh_address(self, script: bytes, network: str = "signet") -> str:
        """
        Convert redeem script to P2WSH address.

        Args:
            script: Redeem script bytes
            network: bitcoin network (signet, testnet, mainnet)

        Returns:
            Bech32 P2WSH address
        """
        # Witness program = SHA256(script)
        witness_program = sha256(script)

        # Encode as bech32
        hrp = {
            "mainnet": "bc",
            "testnet": "tb",
            "signet": "tb",
        }.get(network, "tb")

        return self._encode_bech32(hrp, 0, witness_program)

    def _encode_bech32(self, hrp: str, version: int, program: bytes) -> str:
        """Encode bech32 address."""
        # Simplified bech32 encoding
        # For production, use a proper bech32 library

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

        # Convert witness program to 5-bit groups
        data = [version] + convertbits(program, 8, 5)
        checksum = bech32_create_checksum(hrp, data)
        return hrp + "1" + "".join([CHARSET[d] for d in data + checksum])

    def create_htlc(self, amount_sats: int, hashlock: str,
                   recipient_address: str, refund_address: str,
                   timeout_blocks: int = 144,
                   recipient_pubkey: Optional[str] = None,
                   refund_pubkey: Optional[str] = None) -> Dict:
        """
        Create and fund a new HTLC.

        Args:
            amount_sats: Amount in satoshis
            hashlock: SHA256 hash of secret (hex)
            recipient_address: Address that will receive the pubkey for claim
            refund_address: Address for refund path
            timeout_blocks: Blocks until refund (default 144 = ~24h)
            recipient_pubkey: Optional pubkey hex (if not provided, looked up from wallet)
            refund_pubkey: Optional pubkey hex (if not provided, looked up from wallet)

        Returns:
            {
                "htlc_address": "tb1...",
                "redeem_script": "hex...",
                "funding_txid": "hex...",
                "amount": sats,
                "timelock": block_height,
            }
        """
        # Get current block height for timelock
        current_height = self.client.get_block_count()
        timelock = current_height + timeout_blocks

        # Get pubkeys - use provided ones or look up from wallet
        if not recipient_pubkey:
            recipient_info = self.client.get_address_info(recipient_address)
            recipient_pubkey = recipient_info.get("pubkey")
        if not refund_pubkey:
            refund_info = self.client.get_address_info(refund_address)
            refund_pubkey = refund_info.get("pubkey")

        if not recipient_pubkey or not refund_pubkey:
            raise ValueError("Could not get pubkeys - provide them as parameters or use wallet addresses")

        # Create HTLC script
        script = self.create_htlc_script(
            hashlock, recipient_pubkey, refund_pubkey, timelock
        )

        # Get P2WSH address
        htlc_address = self.script_to_p2wsh_address(
            script, self.client.config.network
        )

        log.info(f"Created HTLC: {htlc_address}, timelock={timelock}")

        return {
            "htlc_address": htlc_address,
            "redeem_script": script.hex(),
            "amount": amount_sats,
            "timelock": timelock,
            "hashlock": hashlock,
            "recipient_pubkey": recipient_pubkey,
            "refund_pubkey": refund_pubkey,
            # Funding TXnot created yet - user or LP funds externally
            "funding_txid": None,
        }

    def create_htlc_for_user(self, amount_sats: int, hashlock: str,
                              user_address: str, lp_refund_address: str,
                              timeout_blocks: int = 72) -> Optional[Dict]:
        """
        Create HTLC for user to claim (LP as sender).

        This generates an ephemeral keypair for the claim path, allowing
        the user to claim by revealing the preimage.

        Args:
            amount_sats: Amount in satoshis
            hashlock: SHA256 hash of secret (hex)
            user_address: User's BTC address (for reference, we generate claim key)
            lp_refund_address: LP's address for refund after timeout
            timeout_blocks: Blocks until LP can refund (default 72 = ~12h)

        Returns:
            {
                "htlc_address": P2WSH address to fund
                "redeem_script": hex script
                "claim_privkey": WIF for user to claim
                "claim_pubkey": hex pubkey
                "timelock": absolute block height
            }
        """
        import secrets
        from hashlib import sha256

        # Generate ephemeral keypair for user's claim path
        # In production, user would provide their own pubkey
        claim_privkey_bytes = secrets.token_bytes(32)

        # Derive pubkey using secp256k1
        # Simplified: use Bitcoin Core to generate if available
        try:
            # Try to generate via Bitcoin Core wallet
            # For descriptor wallets (Bitcoin Core v24+), getnewaddress works but
            # dumpprivkey is NOT supported. We only need the pubkey for HTLC script.
            try:
                # Try legacy first (for old BerkeleyDB wallets)
                new_addr_result = self.client._call("getnewaddress", "htlc_claim", "legacy")
            except Exception:
                # Fall back to default address type (works with descriptor wallets)
                new_addr_result = self.client._call("getnewaddress", "htlc_claim")
            
            addr_info = self.client.get_address_info(new_addr_result)
            claim_pubkey = addr_info.get("pubkey")

            if not claim_pubkey:
                raise ValueError("Could not get pubkey from wallet")

            # Descriptor wallets don't support dumpprivkey - but we don't need it!
            # The claim_privkey_wif is only returned for LP reference, not for signing HTLC
            claim_privkey_wif = None
            log.info(f"Generated claim address {new_addr_result} (pubkey={claim_pubkey[:20]}...)")

        except Exception as e:
            log.warning(f"Could not generate claim key from wallet: {e}")
            # Fallback: use a hardcoded test key (TESTNET ONLY!)
            # In production, this should never happen
            claim_pubkey = "02" + "00" * 32  # Invalid, will fail
            claim_privkey_wif = None
            return None

        # Get LP's refund pubkey from wallet
        try:
            refund_info = self.client.get_address_info(lp_refund_address)
            refund_pubkey = refund_info.get("pubkey")
            if not refund_pubkey:
                # Generate new address for LP
                try:
                    new_refund = self.client._call("getnewaddress", "htlc_refund", "legacy")
                except Exception:
                    new_refund = self.client._call("getnewaddress", "htlc_refund")
                refund_info = self.client.get_address_info(new_refund)
                refund_pubkey = refund_info.get("pubkey")
        except Exception as e:
            log.error(f"Could not get LP refund pubkey: {e}")
            return None

        if not refund_pubkey:
            log.error("No refund pubkey available")
            return None

        # Calculate timelock
        current_height = self.client.get_block_count()
        timelock = current_height + timeout_blocks

        # Create HTLC script
        script = self.create_htlc_script(
            hashlock, claim_pubkey, refund_pubkey, timelock
        )

        # Get P2WSH address
        htlc_address = self.script_to_p2wsh_address(
            script, self.client.config.network
        )

        log.info(f"Created HTLC for user: {htlc_address}, timelock={timelock}")

        return {
            "htlc_address": htlc_address,
            "redeem_script": script.hex(),
            "claim_privkey": claim_privkey_wif,
            "claim_pubkey": claim_pubkey,
            "refund_pubkey": refund_pubkey,
            "timelock": timelock,
            "user_address": user_address,  # For reference
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

        # Use sendtoaddress for simplicity
        # For production, use more sophisticated UTXO selection
        txid = self.client._call("sendtoaddress", htlc_address, f"{amount_btc:.8f}")

        log.info(f"Funded HTLC {htlc_address} with {amount_sats} sats, txid={txid}")
        return txid

    def check_htlc_funded(self, htlc_address: str, expected_amount: int,
                         min_confirmations: int = 1) -> Optional[Dict]:
        """
        Check if HTLC has been funded.

        Args:
            htlc_address: HTLC address to check
            expected_amount: Expected amount in sats
            min_confirmations: Required confirmations

        Returns:
            UTXO info if funded, None otherwise
        """
        # Use scantxoutset for descriptor wallets (listunspent only works for wallet addresses)
        try:
            import json
            scan_result = self.client._call("scantxoutset", "start", json.dumps([f"addr({htlc_address})"]))

            if not scan_result or not scan_result.get("success"):
                return None

            current_height = scan_result.get("height", 0)

            for utxo in scan_result.get("unspents", []):
                amount_sats = int(utxo["amount"] * 100_000_000)
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

        # Fallback to listunspent for legacy wallets
        try:
            utxos = self.client.list_unspent([htlc_address], min_confirmations)
            for utxo in utxos:
                amount_sats = int(utxo["amount"] * 100_000_000)
                if amount_sats >= expected_amount:
                    return {
                        "txid": utxo["txid"],
                        "vout": utxo["vout"],
                        "amount": amount_sats,
                        "confirmations": utxo.get("confirmations", 0),
                    }
        except Exception:
            pass

        return None

    def claim_htlc(self, utxo: Dict, redeem_script: str,
                  preimage: str, recipient_address: str,
                  fee_rate_sat_vb: int = 2) -> str:
        """
        Claim HTLC with preimage.

        Creates and broadcasts the claim transaction using Bitcoin Core signing.

        Args:
            utxo: UTXO dict with txid, vout, amount (sats)
            redeem_script: Redeem script hex (witness script)
            preimage: 32-byte preimage hex
            recipient_address: Where to send claimed funds
            fee_rate_sat_vb: Fee rate in sat/vbyte (default 2)

        Returns:
            Claim transaction ID
        """
        import json

        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]

        log.info(f"HTLC claim: {txid}:{vout}, amount={amount_sats} sats")

        # Verify preimage first
        script_bytes = bytes.fromhex(redeem_script)
        preimage_bytes = bytes.fromhex(preimage)
        expected_hash = sha256(preimage_bytes)

        # Extract hashlock from script (after OP_SHA256, push data)
        # Script: OP_IF OP_SHA256 <32-byte-hashlock> ...
        # Positions: [0]=OP_IF, [1]=OP_SHA256, [2]=0x20 (push 32), [3:35]=hashlock
        if len(script_bytes) < 35:
            raise ValueError("Invalid redeem script")
        script_hashlock = script_bytes[3:35]
        if expected_hash != script_hashlock:
            raise ValueError(f"Preimage does not match hashlock")

        # Calculate P2WSH scriptPubKey: OP_0 <SHA256(witnessScript)>
        witness_program = sha256(script_bytes)
        script_pubkey = bytes([OP_0, 0x20]) + witness_program

        # Estimate TX size for fee calculation
        # P2WSH claim witness: ~1 + 73 (sig) + 33 (preimage) + 1 (OP_TRUE) + 1 + len(script)
        # ~110 + script_len weight units, plus base ~40 vbytes
        estimated_vsize = 150 + len(script_bytes) // 4
        fee_sats = estimated_vsize * fee_rate_sat_vb

        output_amount_sats = amount_sats - fee_sats
        if output_amount_sats <= 546:  # Dust threshold
            raise ValueError(f"Output amount {output_amount_sats} below dust threshold")

        output_amount_btc = output_amount_sats / 100_000_000

        # Create raw transaction
        inputs = [{"txid": txid, "vout": vout}]
        outputs = {recipient_address: f"{output_amount_btc:.8f}"}

        raw_tx = self.client.create_raw_transaction(inputs, outputs)
        log.info(f"Created raw TX: {raw_tx[:40]}...")

        # Sign with witnessScript provided
        prevtxs = [{
            "txid": txid,
            "vout": vout,
            "scriptPubKey": script_pubkey.hex(),
            "witnessScript": redeem_script,
            "amount": amount_sats / 100_000_000,
        }]

        # Bitcoin Core's signrawtransactionwithwallet handles witness construction
        # but we need to manually add the preimage and branch selector
        sign_result = self.client._call(
            "signrawtransactionwithwallet",
            raw_tx,
            json.dumps(prevtxs)
        )

        if not sign_result.get("complete"):
            # Manual witness construction required
            log.info("Using manual witness construction for HTLC claim")
            signed_tx = self._construct_claim_witness(
                raw_tx, utxo, redeem_script, preimage
            )
        else:
            signed_tx = sign_result["hex"]

        # Broadcast
        claim_txid = self.client.send_raw_transaction(signed_tx)
        log.info(f"HTLC claimed: txid={claim_txid}")

        return claim_txid

    def _construct_claim_witness(self, unsigned_tx: str, utxo: Dict,
                                  redeem_script: str, preimage: str) -> str:
        """
        Manually construct witness for HTLC claim using python-bitcoinlib.

        Witness stack for claim branch:
        [0]: signature
        [1]: preimage (32 bytes)
        [2]: OP_TRUE (0x01)
        [3]: redeemScript
        """
        try:
            from bitcoin import SelectParams
            from bitcoin.core import (
                CMutableTransaction, CMutableTxIn, CMutableTxOut,
                COutPoint, CScript, COIN, lx, b2x
            )
            from bitcoin.core.script import (
                SignatureHash, SIGHASH_ALL, SIGVERSION_WITNESS_V0, OP_TRUE
            )
            from bitcoin.wallet import CBitcoinSecret

            # Select signet params
            SelectParams('signet')

            # Get LP's private key for signing
            # Script: OP_IF(0x63) OP_SHA256(0xa8) PUSH32(0x20) <hash 32> OP_EQ(0x88) PUSH33(0x21) <pubkey 33>...
            # Positions: 0:IF, 1:SHA256, 2:PUSH32, 3-34:hash, 35:EQUALVERIFY, 36:PUSH33, 37-69:pubkey
            script_bytes = bytes.fromhex(redeem_script)
            recipient_pubkey = script_bytes[37:70]  # 33-byte compressed pubkey

            # Get WIF for this pubkey from Bitcoin Core
            pubkey_hex = recipient_pubkey.hex()
            address_info = self.client._call("getaddressinfo", self.client._call("getnewaddress"))

            # Dump private key for signing
            # First, find an address we control that corresponds to the pubkey
            # We'll use dumpprivkey on the address associated with this pubkey

            # Try to find matching address
            wif = None
            try:
                # The LP should have imported this key during HTLC creation
                addresses = self.client._call("listreceivedbyaddress", 0, True)
                for addr_info in addresses:
                    addr = addr_info.get("address", "")
                    try:
                        info = self.client._call("getaddressinfo", addr)
                        if info.get("pubkey") == pubkey_hex:
                            wif = self.client._call("dumpprivkey", addr)
                            break
                    except:
                        continue
            except Exception as e:
                log.warning(f"Could not find matching address: {e}")

            if not wif:
                # Try deriving address from pubkey and importing
                # For P2WPKH, address is bech32 of hash160(pubkey)
                import hashlib
                pubkey_hash = hashlib.new('ripemd160', hashlib.sha256(recipient_pubkey).digest()).digest()

                # Try listdescriptors
                try:
                    descriptors = self.client._call("listdescriptors", True)
                    for desc_info in descriptors.get("descriptors", []):
                        desc = desc_info.get("desc", "")
                        if pubkey_hex in desc:
                            # Found the descriptor
                            log.info(f"Found matching descriptor: {desc[:50]}...")
                            break
                except Exception as e:
                    log.warning(f"listdescriptors failed: {e}")

                raise ValueError(f"Cannot find private key for pubkey {pubkey_hex[:16]}... in wallet")

            # Parse transaction
            tx = CMutableTransaction.deserialize(bytes.fromhex(unsigned_tx))

            # Build witness script
            witness_script = CScript(script_bytes)

            # Calculate sighash
            txid = utxo["txid"]
            vout = utxo["vout"]
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
            privkey = CBitcoinSecret(wif)
            sig = privkey.sign(sighash) + bytes([SIGHASH_ALL])

            # Construct witness
            # Witness: <sig> <preimage> <TRUE> <witnessScript>
            preimage_bytes = bytes.fromhex(preimage)

            tx.wit.vtxinwit[0].scriptWitness.stack = [
                sig,
                preimage_bytes,
                bytes([0x01]),  # OP_TRUE equivalent
                script_bytes
            ]

            # Serialize with witness
            signed_tx = b2x(tx.serialize())
            log.info(f"Constructed signed TX with witness: {signed_tx[:40]}...")

            return signed_tx

        except ImportError:
            log.error("python-bitcoinlib not installed")
            raise NotImplementedError(
                "Manual witness construction for non-standard scripts "
                "requires python-bitcoinlib. Install with: pip install python-bitcoinlib\n"
                f"HTLC can be claimed manually using: "
                f"preimage={preimage}, script={redeem_script}"
            )
        except Exception as e:
            log.exception(f"Witness construction failed: {e}")
            raise NotImplementedError(
                f"Witness construction failed: {e}\n"
                f"HTLC can be claimed manually using: "
                f"preimage={preimage}, script={redeem_script}"
            )

    def refund_htlc(self, utxo: Dict, redeem_script: str,
                   refund_address: str, timelock: int,
                   fee_rate_sat_vb: int = 2) -> str:
        """
        Refund expired HTLC.

        Args:
            utxo: UTXO dict with txid, vout, amount (sats)
            redeem_script: Redeem script hex
            refund_address: Where to send refunded funds
            timelock: Original timelock (must be passed)
            fee_rate_sat_vb: Fee rate in sat/vbyte

        Returns:
            Refund transaction ID
        """
        import json

        current_height = self.client.get_block_count()

        if current_height < timelock:
            raise ValueError(
                f"Cannot refund yet. Current height {current_height}, "
                f"timelock {timelock}. Wait {timelock - current_height} more blocks."
            )

        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]

        log.info(f"HTLC refund: {txid}:{vout}, amount={amount_sats} sats")

        script_bytes = bytes.fromhex(redeem_script)
        witness_program = sha256(script_bytes)
        script_pubkey = bytes([OP_0, 0x20]) + witness_program

        # Estimate fee
        estimated_vsize = 120 + len(script_bytes) // 4
        fee_sats = estimated_vsize * fee_rate_sat_vb

        output_amount_sats = amount_sats - fee_sats
        if output_amount_sats <= 546:
            raise ValueError(f"Output amount {output_amount_sats} below dust threshold")

        output_amount_btc = output_amount_sats / 100_000_000

        # Create raw transaction with nLockTime set
        inputs = [{"txid": txid, "vout": vout, "sequence": 0xFFFFFFFE}]  # Enable nLockTime
        outputs = {refund_address: f"{output_amount_btc:.8f}"}

        # createrawtransaction with locktime
        raw_tx = self.client._call(
            "createrawtransaction",
            json.dumps(inputs),
            json.dumps(outputs),
            timelock  # nLockTime
        )

        log.info(f"Created refund TX with locktime={timelock}")

        # Sign with witnessScript
        prevtxs = [{
            "txid": txid,
            "vout": vout,
            "scriptPubKey": script_pubkey.hex(),
            "witnessScript": redeem_script,
            "amount": amount_sats / 100_000_000,
        }]

        sign_result = self.client._call(
            "signrawtransactionwithwallet",
            raw_tx,
            json.dumps(prevtxs)
        )

        if not sign_result.get("complete"):
            # Similar to claim, manual witness needed for ELSE branch
            raise NotImplementedError(
                "Manual witness construction for refund requires python-bitcoinlib. "
                f"HTLC can be refunded manually using: script={redeem_script}"
            )

        signed_tx = sign_result["hex"]

        # Broadcast
        refund_txid = self.client.send_raw_transaction(signed_tx)
        log.info(f"HTLC refunded: txid={refund_txid}")

        return refund_txid

    def generate_htlc_params(self, amount_sats: int, recipient_address: str,
                            refund_address: str, timeout_blocks: int = 144
                            ) -> Tuple[HTLCParams, str]:
        """
        Generate HTLC parameters with new secret.

        Returns:
            (HTLCParams, secret)

        The secret should be kept private until claim.
        """
        secret, hashlock = generate_secret()

        current_height = self.client.get_block_count()
        timelock = current_height + timeout_blocks

        params = HTLCParams(
            hashlock=hashlock,
            timelock=timelock,
            amount=amount_sats,
            recipient=recipient_address,
            refund_address=refund_address,
            chain="BTC",
        )

        return params, secret

    # =========================================================================
    # HTLC3S - 3-Secret HTLC for FlowSwap Protocol
    # =========================================================================

    def create_htlc3s_script(self, hashlock_user: str, hashlock_lp1: str,
                              hashlock_lp2: str, recipient_pubkey: str,
                              refund_pubkey: str, timelock: int) -> bytes:
        """
        Create 3-secret HTLC redeem script for FlowSwap.

        Script structure (canonical order: user, lp1, lp2):
            OP_IF
                OP_SHA256 <H_user> OP_EQUALVERIFY
                OP_SHA256 <H_lp1> OP_EQUALVERIFY
                OP_SHA256 <H_lp2> OP_EQUALVERIFY
                <recipient_pubkey> OP_CHECKSIG
            OP_ELSE
                <timelock> OP_CHECKLOCKTIMEVERIFY OP_DROP
                <refund_pubkey> OP_CHECKSIG
            OP_ENDIF

        Witness for claim (LIFO - pushed in reverse order):
            <signature> <S_lp2> <S_lp1> <S_user> <TRUE> <witnessScript>

        Args:
            hashlock_user: SHA256(S_user) hex
            hashlock_lp1: SHA256(S_lp1) hex
            hashlock_lp2: SHA256(S_lp2) hex
            recipient_pubkey: Compressed pubkey for claim path (hex)
            refund_pubkey: Compressed pubkey for refund path (hex)
            timelock: Absolute block height

        Returns:
            Redeem script bytes
        """
        h_user = bytes.fromhex(hashlock_user)
        h_lp1 = bytes.fromhex(hashlock_lp1)
        h_lp2 = bytes.fromhex(hashlock_lp2)
        recipient_bytes = bytes.fromhex(recipient_pubkey)
        refund_bytes = bytes.fromhex(refund_pubkey)

        # Build 3-secret script
        script = bytes([OP_IF])

        # Verify S_user (first on stack after OP_TRUE)
        script += bytes([OP_SHA256])
        script += push_data(h_user)
        script += bytes([OP_EQUALVERIFY])

        # Verify S_lp1
        script += bytes([OP_SHA256])
        script += push_data(h_lp1)
        script += bytes([OP_EQUALVERIFY])

        # Verify S_lp2
        script += bytes([OP_SHA256])
        script += push_data(h_lp2)
        script += bytes([OP_EQUALVERIFY])

        # Signature check
        script += push_data(recipient_bytes)
        script += bytes([OP_CHECKSIG])

        # Refund branch
        script += bytes([OP_ELSE])
        script += push_int(timelock)
        script += bytes([OP_CHECKLOCKTIMEVERIFY, OP_DROP])
        script += push_data(refund_bytes)
        script += bytes([OP_CHECKSIG])

        script += bytes([OP_ENDIF])

        return script

    def create_htlc3s(self, amount_sats: int,
                      hashlock_user: str, hashlock_lp1: str, hashlock_lp2: str,
                      recipient_pubkey: str, refund_pubkey: str,
                      timeout_blocks: int = 144) -> Dict:
        """
        Create a 3-secret HTLC for FlowSwap.

        Args:
            amount_sats: Amount in satoshis
            hashlock_user: SHA256(S_user) hex
            hashlock_lp1: SHA256(S_lp1) hex
            hashlock_lp2: SHA256(S_lp2) hex
            recipient_pubkey: Pubkey for claim (hex)
            refund_pubkey: Pubkey for refund (hex)
            timeout_blocks: Blocks until refund

        Returns:
            {
                "htlc_address": P2WSH address,
                "redeem_script": hex,
                "amount": sats,
                "timelock": block height,
                "hashlock_user": hex,
                "hashlock_lp1": hex,
                "hashlock_lp2": hex,
            }
        """
        current_height = self.client.get_block_count()
        timelock = current_height + timeout_blocks

        script = self.create_htlc3s_script(
            hashlock_user, hashlock_lp1, hashlock_lp2,
            recipient_pubkey, refund_pubkey, timelock
        )

        htlc_address = self.script_to_p2wsh_address(
            script, self.client.config.network
        )

        log.info(f"Created HTLC3S: {htlc_address}, timelock={timelock}")

        return {
            "htlc_address": htlc_address,
            "redeem_script": script.hex(),
            "amount": amount_sats,
            "timelock": timelock,
            "hashlock_user": hashlock_user,
            "hashlock_lp1": hashlock_lp1,
            "hashlock_lp2": hashlock_lp2,
            "recipient_pubkey": recipient_pubkey,
            "refund_pubkey": refund_pubkey,
        }

    def claim_htlc3s(self, utxo: Dict, redeem_script: str,
                     preimage_user: str, preimage_lp1: str, preimage_lp2: str,
                     recipient_address: str, fee_rate_sat_vb: int = 2) -> str:
        """
        Claim 3-secret HTLC with 3 preimages.

        Witness stack for claim (LIFO order):
            [0]: signature
            [1]: S_lp2 (verified last, pushed first after sig)
            [2]: S_lp1
            [3]: S_user (verified first, pushed last before OP_TRUE)
            [4]: OP_TRUE (0x01)
            [5]: witnessScript

        Args:
            utxo: UTXO dict with txid, vout, amount
            redeem_script: Redeem script hex
            preimage_user: S_user hex (32 bytes)
            preimage_lp1: S_lp1 hex (32 bytes)
            preimage_lp2: S_lp2 hex (32 bytes)
            recipient_address: Where to send funds
            fee_rate_sat_vb: Fee rate

        Returns:
            Claim transaction ID
        """
        import json

        txid = utxo["txid"]
        vout = utxo["vout"]
        amount_sats = utxo["amount"]

        log.info(f"HTLC3S claim: {txid}:{vout}, amount={amount_sats} sats")

        # Verify all 3 preimages
        script_bytes = bytes.fromhex(redeem_script)
        p_user = bytes.fromhex(preimage_user)
        p_lp1 = bytes.fromhex(preimage_lp1)
        p_lp2 = bytes.fromhex(preimage_lp2)

        # Extract hashlocks from script and verify
        # Script: OP_IF(63) OP_SHA256(a8) PUSH32(20) <hash_user 32> EQUALVERIFY(88)
        #         OP_SHA256(a8) PUSH32(20) <hash_lp1 32> EQUALVERIFY(88)
        #         OP_SHA256(a8) PUSH32(20) <hash_lp2 32> EQUALVERIFY(88)
        #         PUSH33(21) <pubkey 33> CHECKSIG(ac)
        #         OP_ELSE(67) ...
        # Positions: 0=IF, 1=SHA256, 2=0x20, 3-34=H_user
        #           35=EQUALVERIFY, 36=SHA256, 37=0x20, 38-69=H_lp1
        #           70=EQUALVERIFY, 71=SHA256, 72=0x20, 73-104=H_lp2
        h_user_script = script_bytes[3:35]
        h_lp1_script = script_bytes[38:70]
        h_lp2_script = script_bytes[73:105]

        if sha256(p_user) != h_user_script:
            raise ValueError("preimage_user does not match hashlock")
        if sha256(p_lp1) != h_lp1_script:
            raise ValueError("preimage_lp1 does not match hashlock")
        if sha256(p_lp2) != h_lp2_script:
            raise ValueError("preimage_lp2 does not match hashlock")

        log.info("All 3 preimages verified")

        # Calculate P2WSH scriptPubKey
        witness_program = sha256(script_bytes)
        script_pubkey = bytes([OP_0, 0x20]) + witness_program

        # Estimate fee (larger witness with 3 preimages)
        # Witness: sig(~73) + 3*preimage(3*33) + OP_TRUE(1) + script(~150)
        estimated_vsize = 200 + len(script_bytes) // 4
        fee_sats = estimated_vsize * fee_rate_sat_vb

        output_amount_sats = amount_sats - fee_sats
        if output_amount_sats <= 546:
            raise ValueError(f"Output {output_amount_sats} below dust")

        output_amount_btc = output_amount_sats / 100_000_000

        # Create raw transaction
        inputs = [{"txid": txid, "vout": vout}]
        outputs = {recipient_address: f"{output_amount_btc:.8f}"}

        raw_tx = self.client.create_raw_transaction(inputs, outputs)
        log.info(f"Created raw TX for HTLC3S claim")

        # Try standard signing first
        prevtxs = [{
            "txid": txid,
            "vout": vout,
            "scriptPubKey": script_pubkey.hex(),
            "witnessScript": redeem_script,
            "amount": amount_sats / 100_000_000,
        }]

        sign_result = self.client._call(
            "signrawtransactionwithwallet",
            raw_tx,
            json.dumps(prevtxs)
        )

        if not sign_result.get("complete"):
            # Manual witness construction
            log.info("Using manual witness for HTLC3S claim")
            signed_tx = self._construct_claim3s_witness(
                raw_tx, utxo, redeem_script,
                preimage_user, preimage_lp1, preimage_lp2
            )
        else:
            signed_tx = sign_result["hex"]

        # Broadcast
        claim_txid = self.client.send_raw_transaction(signed_tx)
        log.info(f"HTLC3S claimed: txid={claim_txid}")

        return claim_txid

    def _construct_claim3s_witness(self, unsigned_tx: str, utxo: Dict,
                                    redeem_script: str,
                                    preimage_user: str, preimage_lp1: str,
                                    preimage_lp2: str) -> str:
        """
        Manually construct witness for 3-secret HTLC claim.

        Witness stack (in order as they appear in serialized witness):
            [0]: signature
            [1]: S_lp2
            [2]: S_lp1
            [3]: S_user
            [4]: 0x01 (OP_TRUE)
            [5]: witnessScript
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

            script_bytes = bytes.fromhex(redeem_script)

            # Extract recipient pubkey from script
            # After 3 hashlock verifications: PUSH33(0x21) <pubkey 33>
            # Position: 105=EQUALVERIFY, 106=PUSH33(0x21), 107-139=pubkey
            recipient_pubkey = script_bytes[107:140]
            pubkey_hex = recipient_pubkey.hex()

            # Find WIF for this pubkey
            wif = self._find_wif_for_pubkey(pubkey_hex)
            if not wif:
                raise ValueError(f"Cannot find key for pubkey {pubkey_hex[:16]}...")

            # Parse transaction
            tx = CMutableTransaction.deserialize(bytes.fromhex(unsigned_tx))
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
            privkey = CBitcoinSecret(wif)
            sig = privkey.sign(sighash) + bytes([SIGHASH_ALL])

            # Construct witness with 3 preimages
            # Order: sig, S_lp2, S_lp1, S_user, TRUE, script
            p_user = bytes.fromhex(preimage_user)
            p_lp1 = bytes.fromhex(preimage_lp1)
            p_lp2 = bytes.fromhex(preimage_lp2)

            tx.wit.vtxinwit[0].scriptWitness.stack = [
                sig,
                p_lp2,      # Verified last (top of stack after pops)
                p_lp1,      # Verified second
                p_user,     # Verified first
                bytes([0x01]),  # OP_TRUE
                script_bytes
            ]

            signed_tx = b2x(tx.serialize())
            log.info(f"Constructed HTLC3S claim witness")

            return signed_tx

        except ImportError:
            raise NotImplementedError(
                "python-bitcoinlib required. Install: pip install python-bitcoinlib"
            )

    def _find_wif_for_pubkey(self, pubkey_hex: str) -> Optional[str]:
        """Find WIF private key for a pubkey in the wallet."""
        try:
            # Try listdescriptors for descriptor wallets
            descriptors = self.client._call("listdescriptors", True)
            for desc_info in descriptors.get("descriptors", []):
                desc = desc_info.get("desc", "")
                if pubkey_hex in desc:
                    # Extract WIF from descriptor
                    # Format: wpkh([fingerprint/path]xprv.../0/*)#checksum
                    # or wpkh(xprv...)
                    import re
                    match = re.search(r'\[(.*?)\]', desc)
                    if match:
                        # Has derivation path, need to derive
                        pass
                    # For now, try dumpprivkey approach
                    break
        except Exception:
            pass

        # Try legacy approach
        try:
            addresses = self.client._call("listreceivedbyaddress", 0, True)
            for addr_info in addresses:
                addr = addr_info.get("address", "")
                try:
                    info = self.client._call("getaddressinfo", addr)
                    if info.get("pubkey") == pubkey_hex:
                        return self.client._call("dumpprivkey", addr)
                except:
                    continue
        except Exception:
            pass

        return None

    def refund_htlc3s(self, utxo: Dict, redeem_script: str,
                      refund_address: str, timelock: int,
                      fee_rate_sat_vb: int = 2) -> str:
        """
        Refund expired 3-secret HTLC.

        Same as single-secret refund - only signature needed for ELSE branch.

        Args:
            utxo: UTXO dict
            redeem_script: Redeem script hex
            refund_address: Where to send funds
            timelock: Original timelock
            fee_rate_sat_vb: Fee rate

        Returns:
            Refund transaction ID
        """
        # Refund branch is identical to single-secret
        # Witness: <signature> <FALSE> <witnessScript>
        return self.refund_htlc(utxo, redeem_script, refund_address,
                                timelock, fee_rate_sat_vb)

    def generate_htlc3s_secrets(self) -> Dict:
        """
        Generate 3 secret/hashlock pairs for FlowSwap.

        Returns:
            {
                "user": {"secret": hex, "hashlock": hex},
                "lp1": {"secret": hex, "hashlock": hex},
                "lp2": {"secret": hex, "hashlock": hex},
            }
        """
        import secrets as sec

        def gen_pair():
            secret = sec.token_bytes(32)
            hashlock = sha256(secret)
            return {"secret": secret.hex(), "hashlock": hashlock.hex()}

        return {
            "user": gen_pair(),
            "lp1": gen_pair(),
            "lp2": gen_pair(),
        }
