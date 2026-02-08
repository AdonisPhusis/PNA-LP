#!/usr/bin/env python3
"""
Generate EVM wallet and save keys securely.
"""

import secrets
import json
import os
import sys

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def keccak256(data: bytes) -> bytes:
    """Keccak256 hash."""
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()

def private_key_to_address(private_key_hex: str) -> str:
    """Derive Ethereum address from private key."""
    # Remove 0x prefix if present
    if private_key_hex.startswith('0x'):
        private_key_hex = private_key_hex[2:]

    private_key_bytes = bytes.fromhex(private_key_hex)

    # Use ecdsa to get public key
    from ecdsa import SigningKey, SECP256k1
    sk = SigningKey.from_string(private_key_bytes, curve=SECP256k1)
    vk = sk.get_verifying_key()

    # Public key is 64 bytes (uncompressed without 04 prefix)
    public_key_bytes = vk.to_string()

    # Address is last 20 bytes of keccak256(public_key)
    address_bytes = keccak256(public_key_bytes)[-20:]
    address = '0x' + address_bytes.hex()

    return address

def main():
    print("=" * 60)
    print("EVM WALLET GENERATOR FOR P&A LP")
    print("=" * 60)
    print()

    # Check dependencies
    try:
        from ecdsa import SigningKey, SECP256k1
        from Crypto.Hash import keccak
    except ImportError:
        print("Installing dependencies...")
        os.system("pip3 install ecdsa pycryptodome --break-system-packages -q")
        from ecdsa import SigningKey, SECP256k1
        from Crypto.Hash import keccak

    # Generate random private key
    private_key = '0x' + secrets.token_hex(32)

    # Derive address
    address = private_key_to_address(private_key)

    print(f"Address:     {address}")
    print(f"Private Key: {private_key}")
    print()

    # Save to file
    keys_file = os.path.join(os.path.dirname(__file__), "evm_wallet.json")
    wallet_data = {
        "network": "base_sepolia",
        "address": address,
        "private_key": private_key,
        "note": "LP EVM wallet for USDC HTLC swaps"
    }

    with open(keys_file, 'w') as f:
        json.dump(wallet_data, f, indent=2)
    os.chmod(keys_file, 0o600)  # Restrict permissions

    print(f"Saved to: {keys_file}")
    print()
    print("=" * 60)
    print("NEXT STEPS:")
    print("=" * 60)
    print(f"1. Get Base Sepolia ETH: https://www.alchemy.com/faucets/base-sepolia")
    print(f"   Address: {address}")
    print()
    print(f"2. Get USDC: https://faucet.circle.com/")
    print(f"   Select Base Sepolia, address: {address}")
    print()
    print("3. Deploy HTLC contract:")
    print(f"   python3 deploy_htlc.py -k {private_key}")
    print()

    return address, private_key

if __name__ == "__main__":
    main()
