#!/usr/bin/env python3
"""
Extract BTC claim_wif from Bitcoin Core descriptor wallet and save to btc.json.

Bitcoin Core descriptor wallets don't support dumpprivkey.
This script uses listdescriptors + BIP32 derivation to export the WIF.

Usage:
    python3 extract_btc_wif.py [--cli-path /path/to/bitcoin-cli] [--datadir /path]

Requires: python-bitcoinlib (pip install python-bitcoinlib)
"""

import json
import subprocess
import sys
import hashlib
import hmac
import struct
import re
from pathlib import Path


# secp256k1 curve order
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def btc_cli(args, cli_path="bitcoin-cli", datadir=None, wallet=None):
    """Call bitcoin-cli and return parsed JSON."""
    cmd = [cli_path, "-signet"]
    if datadir:
        cmd.append(f"-datadir={datadir}")
    if wallet:
        cmd.append(f"-rpcwallet={wallet}")
    cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"bitcoin-cli error: {result.stderr.strip()}")
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def point_from_privkey(privkey_bytes):
    """Get compressed public key from private key bytes."""
    from bitcoin import SelectParams
    from bitcoin.wallet import CBitcoinSecret
    SelectParams('signet')
    secret = CBitcoinSecret.from_secret_bytes(privkey_bytes)
    return bytes(secret.pub)


def privkey_to_wif(privkey_bytes, testnet=True):
    """Convert 32-byte private key to WIF format (compressed)."""
    version = b'\xef' if testnet else b'\x80'
    payload = version + privkey_bytes + b'\x01'  # 0x01 = compressed
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58_encode(payload + checksum)


def base58_encode(data):
    """Encode bytes to base58."""
    ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    num = int.from_bytes(data, 'big')
    result = ''
    while num > 0:
        num, remainder = divmod(num, 58)
        result = ALPHABET[remainder] + result
    for byte in data:
        if byte == 0:
            result = '1' + result
        else:
            break
    return result


def base58_decode(s):
    """Decode base58 string to bytes."""
    ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    num = 0
    for char in s:
        num = num * 58 + ALPHABET.index(char)
    pad_size = 0
    for char in s:
        if char == '1':
            pad_size += 1
        else:
            break
    result = num.to_bytes((num.bit_length() + 7) // 8, 'big') if num else b''
    return b'\x00' * pad_size + result


def parse_tprv(tprv_str):
    """Parse tprv extended private key.
    Returns (chain_code, private_key_bytes).
    """
    raw = base58_decode(tprv_str)
    raw = raw[:-4]  # Remove checksum
    if len(raw) != 78:
        raise ValueError(f"Invalid tprv length: {len(raw)} (expected 78)")
    chain_code = raw[13:45]
    if raw[45] != 0:
        raise ValueError(f"Expected 0x00 prefix for private key, got {raw[45]:#x}")
    privkey = raw[46:78]
    return chain_code, privkey


def bip32_derive_child(parent_key, parent_chain, index, hardened=False):
    """Derive BIP32 child key (supports both hardened and non-hardened)."""
    if hardened:
        # Hardened: HMAC(chain, 0x00 + privkey + index_with_bit31)
        data = b'\x00' + parent_key + struct.pack('>I', index | 0x80000000)
    else:
        # Normal: HMAC(chain, pubkey + index)
        parent_pub = point_from_privkey(parent_key)
        data = parent_pub + struct.pack('>I', index)

    I = hmac.new(parent_chain, data, hashlib.sha512).digest()
    IL, IR = I[:32], I[32:]
    child_key_int = (int.from_bytes(IL, 'big') + int.from_bytes(parent_key, 'big')) % SECP256K1_N
    return child_key_int.to_bytes(32, 'big'), IR


def derive_full_path(master_key, master_chain, path_str):
    """Derive key from full BIP32 path like '84h/1h/0h/0/2'.

    Supports 'h' suffix for hardened derivation.
    """
    current_key = master_key
    current_chain = master_chain

    parts = path_str.strip('/').split('/')
    for part in parts:
        if part == '*':
            break  # wildcard — stop here
        hardened = part.endswith('h') or part.endswith("'")
        index = int(part.rstrip("h'"))
        current_key, current_chain = bip32_derive_child(
            current_key, current_chain, index, hardened=hardened
        )

    return current_key, current_chain


def parse_descriptor_path(desc_str):
    """Extract tprv and full derivation path from a descriptor string.

    Handles formats like:
        wpkh(tprv8ZgxMBic.../84h/1h/0h/0/*)#checksum
        wpkh([d34db33f/84h/1h/0h]tprv8FGo.../0/*)#checksum
    """
    # Extract tprv
    tprv_match = re.search(r'(tprv[a-km-zA-HJ-NP-Z1-9]+)', desc_str)
    if not tprv_match:
        return None, None

    tprv_str = tprv_match.group(1)

    # Extract path after tprv (everything between tprv.../ and )#)
    # This captures: /84h/1h/0h/0/* or /0/*
    after_tprv = desc_str[tprv_match.end():]
    path_match = re.match(r'/([\dh/\*\']+)', after_tprv)
    if not path_match:
        return tprv_str, ""

    return tprv_str, path_match.group(1)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract BTC WIF from descriptor wallet")
    parser.add_argument("--cli-path", default="bitcoin-cli", help="Path to bitcoin-cli")
    parser.add_argument("--datadir", default=None, help="Bitcoin datadir")
    parser.add_argument("--wallet", default="lp_wallet", help="Wallet name")
    args = parser.parse_args()

    # Load btc.json
    btc_json_path = Path.home() / ".BathronKey" / "btc.json"
    if not btc_json_path.exists():
        print(f"ERROR: {btc_json_path} not found")
        sys.exit(1)

    with open(btc_json_path) as f:
        btc_config = json.load(f)

    lp_address = btc_config.get("address", "")
    lp_pubkey = btc_config.get("pubkey", "")
    print(f"LP address: {lp_address}")
    print(f"LP pubkey:  {lp_pubkey}")

    if btc_config.get("claim_wif"):
        print("claim_wif already in btc.json — nothing to do")
        sys.exit(0)

    # Get address info to find HD path
    print("\n[1/4] Getting address info...")
    addr_info = btc_cli(
        ["getaddressinfo", lp_address],
        cli_path=args.cli_path, datadir=args.datadir, wallet=args.wallet
    )
    hd_path = addr_info.get("hdkeypath", "")
    print(f"  HD path: {hd_path}")

    if not hd_path:
        print("ERROR: No HD key path found for address")
        sys.exit(1)

    # Parse full path: m/84h/1h/0h/0/2
    full_path = hd_path.replace("'", "h").lstrip("m/")
    print(f"  Full derivation path: {full_path}")

    # Get descriptors with private keys
    print("\n[2/4] Getting wallet descriptors...")
    descs = btc_cli(
        ["listdescriptors", "true"],
        cli_path=args.cli_path, datadir=args.datadir, wallet=args.wallet
    )

    if not descs or not isinstance(descs, dict):
        print("ERROR: Cannot get descriptors")
        sys.exit(1)

    found_wif = None

    # Strategy 1: Find wpkh descriptor, derive using full path
    for desc_info in descs.get("descriptors", []):
        desc_str = desc_info.get("desc", "")

        # Only process wpkh descriptors (native segwit, not sh(wpkh))
        # sh(wpkh) = m/49', wpkh = m/84', we want m/84'
        if desc_str.startswith("sh("):
            continue
        if "wpkh(" not in desc_str:
            continue

        tprv_str, desc_path = parse_descriptor_path(desc_str)
        if not tprv_str:
            continue

        print(f"  Found wpkh descriptor, path suffix: /{desc_path}")

        try:
            chain_code, master_key = parse_tprv(tprv_str)

            # If descriptor has origin info [fp/84h/1h/0h], then tprv is the
            # master key and the path after tprv is just the non-hardened suffix.
            # We need to check: does the descriptor path include hardened levels?
            if 'h' in desc_path or "'" in desc_path:
                # Full path from master: derive everything
                # Replace * with the actual index from hdkeypath
                # desc_path might be: 84h/1h/0h/0/*
                # full_path is: 84h/1h/0h/0/2
                derive_path = full_path
                print(f"  Deriving full path from master: {derive_path}")
            else:
                # tprv is already at the hardened parent
                # desc_path is like: 0/* — just non-hardened levels
                # Parse non-hardened part from full_path
                parts = full_path.split('/')
                non_hardened = []
                for p in reversed(parts):
                    if 'h' in p or "'" in p:
                        break
                    non_hardened.insert(0, p)
                derive_path = '/'.join(non_hardened)
                print(f"  Deriving non-hardened from tprv: {derive_path}")

            current_key, current_chain = derive_full_path(
                master_key, chain_code, derive_path
            )

            derived_pub = point_from_privkey(current_key).hex()
            print(f"  Derived pubkey: {derived_pub[:20]}...")
            print(f"  Expected:       {lp_pubkey[:20]}...")

            if derived_pub == lp_pubkey:
                found_wif = privkey_to_wif(current_key, testnet=True)
                print(f"  MATCH!")
                break
            else:
                print(f"  No match, trying with [origin] prefix...")
                # Maybe the tprv already includes partial derivation
                # Try with just the non-hardened suffix
                parts = full_path.split('/')
                non_hardened = []
                for p in reversed(parts):
                    if 'h' in p or "'" in p:
                        break
                    non_hardened.insert(0, p)
                nh_path = '/'.join(non_hardened)
                if nh_path != derive_path:
                    current_key2, _ = derive_full_path(
                        master_key, chain_code, nh_path
                    )
                    derived_pub2 = point_from_privkey(current_key2).hex()
                    if derived_pub2 == lp_pubkey:
                        found_wif = privkey_to_wif(current_key2, testnet=True)
                        print(f"  MATCH with non-hardened path: {nh_path}")
                        break

        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            continue

    # Strategy 2: Brute-force all wpkh descriptors with both path interpretations
    if not found_wif:
        print("\n[3/4] Brute-force search...")
        for desc_info in descs.get("descriptors", []):
            desc_str = desc_info.get("desc", "")
            if "wpkh(" not in desc_str:
                continue

            tprv_str, desc_path = parse_descriptor_path(desc_str)
            if not tprv_str:
                continue

            try:
                chain_code, master_key = parse_tprv(tprv_str)
            except Exception:
                continue

            # Try full path from master
            try:
                final_key, _ = derive_full_path(master_key, chain_code, full_path)
                if point_from_privkey(final_key).hex() == lp_pubkey:
                    found_wif = privkey_to_wif(final_key, testnet=True)
                    print(f"  FOUND via full path derivation!")
                    break
            except Exception:
                pass

            # Try non-hardened suffix only (tprv at hardened parent)
            parts = full_path.split('/')
            non_hardened = []
            for p in reversed(parts):
                if 'h' in p or "'" in p:
                    break
                non_hardened.insert(0, p)
            nh_path = '/'.join(non_hardened)
            try:
                final_key, _ = derive_full_path(master_key, chain_code, nh_path)
                if point_from_privkey(final_key).hex() == lp_pubkey:
                    found_wif = privkey_to_wif(final_key, testnet=True)
                    print(f"  FOUND via non-hardened derivation!")
                    break
            except Exception:
                pass

            # Brute-force: try /0/N and /1/N for N in 0..50
            for branch in [0, 1]:
                for idx in range(50):
                    try:
                        k1, c1 = bip32_derive_child(master_key, chain_code, branch)
                        k2, _ = bip32_derive_child(k1, c1, idx)
                        if point_from_privkey(k2).hex() == lp_pubkey:
                            found_wif = privkey_to_wif(k2, testnet=True)
                            print(f"  FOUND at /{branch}/{idx}")
                            break
                    except Exception:
                        continue
                if found_wif:
                    break
            if found_wif:
                break

    if not found_wif:
        print("\nERROR: Could not find private key for LP pubkey")
        print("Try manually: bitcoin-cli -signet -rpcwallet=lp_wallet listdescriptors true")
        sys.exit(1)

    # Save to btc.json
    print(f"\n[4/4] Saving claim_wif to {btc_json_path}...")
    btc_config["claim_wif"] = found_wif
    with open(btc_json_path, 'w') as f:
        json.dump(btc_config, f, indent=2)

    # Verify by deriving pubkey from WIF
    from bitcoin import SelectParams
    from bitcoin.wallet import CBitcoinSecret
    SelectParams('signet')
    verify_key = CBitcoinSecret(found_wif)
    verify_pub = verify_key.pub.hex()
    if verify_pub == lp_pubkey:
        print(f"Verified! WIF pubkey matches LP pubkey")
    else:
        print(f"WARNING: WIF pubkey {verify_pub[:16]}... != LP pubkey {lp_pubkey[:16]}...")

    print(f"Done! claim_wif saved (starts with {found_wif[:4]}...)")


if __name__ == "__main__":
    main()
