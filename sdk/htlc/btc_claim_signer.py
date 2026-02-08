#!/usr/bin/env python3
"""
BTC HTLC Claim Transaction Builder and Signer.

This module builds and signs claim transactions for 3-secret HTLCs.
The witness structure for claim is:
    <signature> <S_lp2> <S_lp1> <S_user> <01> <witnessScript>
"""

import hashlib
import struct
from typing import Tuple, Optional
import subprocess
import json

# Bitcoin opcodes
OP_0 = 0x00
OP_PUSHDATA1 = 0x4c
OP_1 = 0x51
OP_IF = 0x63
OP_ELSE = 0x67
OP_ENDIF = 0x68
OP_DROP = 0x75
OP_SHA256 = 0xa8
OP_EQUALVERIFY = 0x88
OP_CHECKSIG = 0xac
OP_CHECKSEQUENCEVERIFY = 0xb2

# Sighash types
SIGHASH_ALL = 0x01


def double_sha256(data: bytes) -> bytes:
    """Double SHA256 hash."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data))."""
    import hashlib
    return hashlib.new('ripemd160', hashlib.sha256(data).digest()).digest()


def var_int(n: int) -> bytes:
    """Encode variable length integer."""
    if n < 0xfd:
        return bytes([n])
    elif n <= 0xffff:
        return bytes([0xfd]) + struct.pack('<H', n)
    elif n <= 0xffffffff:
        return bytes([0xfe]) + struct.pack('<I', n)
    else:
        return bytes([0xff]) + struct.pack('<Q', n)


def push_data(data: bytes) -> bytes:
    """Create push data opcode."""
    n = len(data)
    if n < 0x4c:
        return bytes([n]) + data
    elif n <= 0xff:
        return bytes([0x4c, n]) + data
    elif n <= 0xffff:
        return bytes([0x4d]) + struct.pack('<H', n) + data
    else:
        return bytes([0x4e]) + struct.pack('<I', n) + data


def decode_wif(wif: str) -> Tuple[bytes, bool]:
    """Decode WIF to private key bytes."""
    import base58
    decoded = base58.b58decode_check(wif)

    if decoded[0] in (0x80, 0xef):  # Mainnet or Testnet
        if len(decoded) == 34 and decoded[-1] == 0x01:
            # Compressed
            return decoded[1:33], True
        else:
            # Uncompressed
            return decoded[1:33], False
    raise ValueError(f"Invalid WIF prefix: {decoded[0]}")


def privkey_to_pubkey(privkey: bytes, compressed: bool = True) -> bytes:
    """Derive public key from private key."""
    try:
        from ecdsa import SigningKey, SECP256k1
        sk = SigningKey.from_string(privkey, curve=SECP256k1)
        vk = sk.get_verifying_key()

        if compressed:
            # Compressed format: 02/03 + x
            prefix = b'\x02' if vk.pubkey.point.y() % 2 == 0 else b'\x03'
            return prefix + vk.pubkey.point.x().to_bytes(32, 'big')
        else:
            # Uncompressed format: 04 + x + y
            return b'\x04' + vk.pubkey.point.x().to_bytes(32, 'big') + vk.pubkey.point.y().to_bytes(32, 'big')
    except ImportError:
        raise ImportError("ecdsa library required: pip install ecdsa")


def sign_hash(privkey: bytes, sighash: bytes) -> bytes:
    """Sign a hash with private key (DER format)."""
    try:
        from ecdsa import SigningKey, SECP256k1
        from ecdsa.util import sigencode_der_canonize

        sk = SigningKey.from_string(privkey, curve=SECP256k1)
        signature = sk.sign_digest(sighash, sigencode=sigencode_der_canonize)
        return signature
    except ImportError:
        raise ImportError("ecdsa library required: pip install ecdsa")


def build_htlc_claim_witness(
    signature: bytes,
    s_user: bytes,
    s_lp1: bytes,
    s_lp2: bytes,
    witness_script: bytes
) -> bytes:
    """
    Build witness stack for HTLC claim.

    Witness: <sig> <S_lp2> <S_lp1> <S_user> <01> <witnessScript>
    """
    # Number of witness items
    witness = var_int(6)

    # Item 0: Signature with sighash type
    sig_with_hashtype = signature + bytes([SIGHASH_ALL])
    witness += var_int(len(sig_with_hashtype)) + sig_with_hashtype

    # Item 1: S_lp2
    witness += var_int(len(s_lp2)) + s_lp2

    # Item 2: S_lp1
    witness += var_int(len(s_lp1)) + s_lp1

    # Item 3: S_user
    witness += var_int(len(s_user)) + s_user

    # Item 4: OP_TRUE (for IF branch)
    witness += var_int(1) + bytes([0x01])

    # Item 5: Witness script
    witness += var_int(len(witness_script)) + witness_script

    return witness


def calculate_witness_v0_sighash(
    tx_version: int,
    tx_inputs: list,
    tx_outputs: list,
    input_index: int,
    witness_script: bytes,
    value: int,
    hashtype: int = SIGHASH_ALL
) -> bytes:
    """
    Calculate BIP143 sighash for witness v0.

    https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki
    """
    # 1. nVersion
    preimage = struct.pack('<I', tx_version)

    # 2. hashPrevouts
    prevouts = b''
    for inp in tx_inputs:
        prevouts += bytes.fromhex(inp['txid'])[::-1]  # Little-endian
        prevouts += struct.pack('<I', inp['vout'])
    preimage += double_sha256(prevouts)

    # 3. hashSequence
    sequences = b''
    for inp in tx_inputs:
        sequences += struct.pack('<I', inp.get('sequence', 0xffffffff))
    preimage += double_sha256(sequences)

    # 4. outpoint
    inp = tx_inputs[input_index]
    preimage += bytes.fromhex(inp['txid'])[::-1]
    preimage += struct.pack('<I', inp['vout'])

    # 5. scriptCode (witness script with length prefix)
    script_code = var_int(len(witness_script)) + witness_script
    preimage += script_code

    # 6. value
    preimage += struct.pack('<Q', value)

    # 7. nSequence
    preimage += struct.pack('<I', inp.get('sequence', 0xffffffff))

    # 8. hashOutputs
    outputs_data = b''
    for out in tx_outputs:
        outputs_data += struct.pack('<Q', out['value'])
        script = bytes.fromhex(out['scriptPubKey'])
        outputs_data += var_int(len(script)) + script
    preimage += double_sha256(outputs_data)

    # 9. nLockTime
    preimage += struct.pack('<I', 0)

    # 10. sighash type
    preimage += struct.pack('<I', hashtype)

    return double_sha256(preimage)


def build_signed_claim_tx(
    funding_txid: str,
    funding_vout: int,
    funding_value: int,  # satoshis
    recipient_address: str,
    witness_script: bytes,
    privkey: bytes,
    s_user: bytes,
    s_lp1: bytes,
    s_lp2: bytes,
    fee: int = 500
) -> str:
    """
    Build a fully signed claim transaction.

    Returns the signed transaction hex.
    """
    # Output value
    output_value = funding_value - fee

    # Decode recipient address to scriptPubKey
    if recipient_address.startswith('tb1q') or recipient_address.startswith('bc1q'):
        # P2WPKH
        import bech32
        hrp, data = bech32.decode(recipient_address[:2], recipient_address)
        if data is None:
            # Try full decode
            try:
                import base58
            except:
                pass
            hrp = 'tb' if recipient_address.startswith('tb') else 'bc'
            _, data = bech32.decode(hrp, recipient_address)

        witness_program = bytes(data[1:])  # Skip version
        output_script = bytes([0x00, len(witness_program)]) + witness_program
    else:
        raise ValueError(f"Unsupported address format: {recipient_address}")

    # Build transaction
    tx_inputs = [{
        'txid': funding_txid,
        'vout': funding_vout,
        'sequence': 0xffffffff
    }]

    tx_outputs = [{
        'value': output_value,
        'scriptPubKey': output_script.hex()
    }]

    # Calculate sighash
    sighash = calculate_witness_v0_sighash(
        tx_version=2,
        tx_inputs=tx_inputs,
        tx_outputs=tx_outputs,
        input_index=0,
        witness_script=witness_script,
        value=funding_value
    )

    # Sign
    signature = sign_hash(privkey, sighash)

    # Build witness
    witness = build_htlc_claim_witness(signature, s_user, s_lp1, s_lp2, witness_script)

    # Build final transaction
    # Version
    tx = struct.pack('<I', 2)

    # Marker and flag for witness
    tx += bytes([0x00, 0x01])

    # Input count
    tx += var_int(1)

    # Input
    tx += bytes.fromhex(funding_txid)[::-1]  # txid (little-endian)
    tx += struct.pack('<I', funding_vout)    # vout
    tx += bytes([0x00])                       # scriptSig (empty for witness)
    tx += struct.pack('<I', 0xffffffff)      # sequence

    # Output count
    tx += var_int(1)

    # Output
    tx += struct.pack('<Q', output_value)
    tx += var_int(len(output_script)) + output_script

    # Witness
    tx += witness

    # Locktime
    tx += struct.pack('<I', 0)

    return tx.hex()


def extract_secrets_from_witness(tx_hex: str) -> dict:
    """
    Extract secrets from a claim transaction witness.

    Returns dict with S_user, S_lp1, S_lp2.
    """
    # Parse witness from transaction
    # This is a simplified parser - assumes standard claim format
    tx = bytes.fromhex(tx_hex)

    # Find witness section (after marker/flag)
    # Skip version (4) + marker (1) + flag (1) + input count + inputs + output count + outputs
    # For a proper implementation, we'd need a full tx parser

    # For now, return placeholder - the full implementation would parse
    # the witness stack and extract items 1, 2, 3 (S_lp2, S_lp1, S_user)

    return {
        'S_user': None,
        'S_lp1': None,
        'S_lp2': None,
        'note': 'Full implementation requires complete TX parser'
    }


# Bech32 encoding/decoding
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

class bech32:
    @staticmethod
    def decode(hrp, addr):
        if addr[:len(hrp)+1].lower() != hrp + '1':
            return None, None

        data_part = addr[len(hrp)+1:]
        values = []
        for c in data_part:
            if c not in CHARSET:
                return None, None
            values.append(CHARSET.index(c))

        # Convert from 5-bit to 8-bit
        acc = 0
        bits = 0
        result = []
        for v in values[:-6]:  # Exclude checksum
            acc = (acc << 5) | v
            bits += 5
            while bits >= 8:
                bits -= 8
                result.append((acc >> bits) & 0xff)

        return hrp, result


if __name__ == '__main__':
    # Test with MVP data
    print("BTC HTLC Claim Signer")
    print("=" * 60)

    # MVP test parameters
    FUNDING_TXID = "d1c656881e844e6f9ef916ef426abdf451cdd6851cd17dff8d27497c15b44262"
    FUNDING_VOUT = 0
    FUNDING_VALUE = 10000  # sats

    S_USER = bytes.fromhex("2d6ea06f845f2fe64b03305076e39fe4114a15d8f83904d24a399168cd78f9ac")
    S_LP1 = bytes.fromhex("c9f95172a736ed145f41b138fbc82eb80dc1492d28b201008e14d881cfac82d0")
    S_LP2 = bytes.fromhex("681549506bf20b237b5ba05385d624b9fe36962aefae8b6c0128d0cf0713ccec")

    HTLC_SCRIPT = bytes.fromhex("63a82013ccc7087668869e62146ea776614c6ce10811c926ad583bda3d4a40864e05c088a820bdb432bb6537578e70c37da156b1b38ff7b94fd0c8f194d24f51856fdd2a409d88a820ecfcb6c5a30a876e665a1b7ce99dc1d8a04f38790584dd56cf118e02af5f4df28821039b6d9375838d5d4ad49e5fe75e3c8820dadbd9e601da39caa08132d2ecb8e7d5ac670190b275210370eeb81b88d20c6a9d3cace87c73698998077bc0b4ddf31b10f901e3f79a4378ac68")

    ALICE_ADDRESS = "tb1qc4ayevq4g7j4de52x8lkcxeffe3kqms6etvcrl"

    print(f"Funding UTXO: {FUNDING_TXID}:{FUNDING_VOUT}")
    print(f"Value: {FUNDING_VALUE} sats")
    print(f"Recipient: {ALICE_ADDRESS}")
    print()
    print("To sign, provide Alice's WIF private key")
    print("Then broadcast the resulting transaction")
