#!/usr/bin/env python3
"""
PNA LP Registration — On-chain OP_RETURN TX

Registers (or unregisters) an LP on the BATHRON chain by sending a standard TX
with an OP_RETURN output containing: PNA|LP|01|<endpoint_url>

For Tier 1 (MN-backed), the TX must be sent from the operator's derived address.
Use --operator-pubkey to derive the address from the MN operator public key.

Usage:
    python3 register_lp.py --endpoint "http://57.131.33.152:8080"
    python3 register_lp.py --endpoint "http://57.131.33.152:8080" --operator-pubkey <66hex>
    python3 register_lp.py --unregister
    python3 register_lp.py --status
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


# =============================================================================
# CONFIG
# =============================================================================

PNA_PROTOCOL_PREFIX = "PNA|LP|01|"
PNA_UNREGISTER = "PNA|LP|01|UNREG"
MAX_OP_RETURN_DATA = 80  # MAX_OP_RETURN_RELAY(83) - 1(OP_RETURN) - 2(pushdata)
# BATHRON RPC uses raw satoshis (1 M0 = 1 satoshi internally)
# Fee ~200 sats is conservative for a standard TX with 1 input, 2 outputs
MIN_FEE_SATS = 20
BATHRON_KEY_DIR = Path.home() / ".BathronKey"
CLI_PATHS = [
    Path.home() / "bathron" / "bin" / "bathron-cli",
    Path.home() / "BATHRON" / "src" / "bathron-cli",
    Path.home() / "BATHRON-Core" / "src" / "bathron-cli",
    Path("/usr/local/bin/bathron-cli"),
    Path.home() / "bathron-cli",
]


# =============================================================================
# ADDRESS DERIVATION (operator pubkey -> P2PKH address)
# =============================================================================

_BASE58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
_TESTNET_PUBKEY_VERSION = 139  # base58Prefixes[PUBKEY_ADDRESS] from chainparams.cpp


def _base58_encode(data: bytes) -> str:
    """Base58 encode raw bytes."""
    n = int.from_bytes(data, 'big')
    result = ''
    while n > 0:
        n, r = divmod(n, 58)
        result = _BASE58_ALPHABET[r] + result
    for byte in data:
        if byte == 0:
            result = _BASE58_ALPHABET[0] + result
        else:
            break
    return result


def pubkey_to_address(pubkey_hex: str,
                      version: int = _TESTNET_PUBKEY_VERSION) -> str:
    """Derive P2PKH address from compressed ECDSA secp256k1 public key."""
    pubkey_bytes = bytes.fromhex(pubkey_hex)
    if len(pubkey_bytes) != 33:
        print(f"ERROR: Expected 33-byte compressed pubkey, got {len(pubkey_bytes)}")
        sys.exit(1)
    sha = hashlib.sha256(pubkey_bytes).digest()
    h160 = hashlib.new('ripemd160', sha).digest()
    versioned = bytes([version]) + h160
    checksum = hashlib.sha256(hashlib.sha256(versioned).digest()).digest()[:4]
    return _base58_encode(versioned + checksum)


# =============================================================================
# CLI HELPERS
# =============================================================================

def find_cli() -> Path:
    """Find bathron-cli binary."""
    for p in CLI_PATHS:
        if p.exists():
            return p
    print("ERROR: bathron-cli not found")
    sys.exit(1)


def rpc(cli: Path, method: str, *args, timeout: int = 30) -> any:
    """Call bathron-cli RPC."""
    cmd = [str(cli), "-testnet", method] + [str(a) for a in args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"RPC error ({method}): {result.stderr.strip()}")
        return None
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


# =============================================================================
# RAW TX MANIPULATION
# =============================================================================

def read_varint(data: bytes, pos: int) -> tuple:
    """Read a Bitcoin-style varint from data at position."""
    first = data[pos]
    if first < 0xfd:
        return first, pos + 1
    elif first == 0xfd:
        return int.from_bytes(data[pos+1:pos+3], 'little'), pos + 3
    elif first == 0xfe:
        return int.from_bytes(data[pos+1:pos+5], 'little'), pos + 5
    else:
        return int.from_bytes(data[pos+1:pos+9], 'little'), pos + 9


def encode_varint(n: int) -> bytes:
    """Encode integer as Bitcoin-style varint."""
    if n < 0xfd:
        return bytes([n])
    elif n <= 0xffff:
        return b'\xfd' + n.to_bytes(2, 'little')
    elif n <= 0xffffffff:
        return b'\xfe' + n.to_bytes(4, 'little')
    else:
        return b'\xff' + n.to_bytes(8, 'little')


def varint_size(n: int) -> int:
    """Get the encoded size of a varint value."""
    if n < 0xfd:
        return 1
    elif n <= 0xffff:
        return 3
    elif n <= 0xffffffff:
        return 5
    else:
        return 9


def insert_op_return(raw_hex: str, data: bytes) -> str:
    """Insert an OP_RETURN output into a raw transaction.

    Parses the raw TX hex, increments the vout count, and appends
    an OP_RETURN output (value=0) with the given data payload.
    """
    raw = bytes.fromhex(raw_hex)
    pos = 0

    # nVersion(2) + nType(2) = 4 bytes
    pos += 4

    # vin count
    vin_count, pos = read_varint(raw, pos)

    # Skip all vin entries
    for _ in range(vin_count):
        pos += 32  # prev txid
        pos += 4   # prev vout index
        script_len, pos = read_varint(raw, pos)
        pos += script_len  # scriptSig
        pos += 4   # sequence

    # Record vout count position
    vout_count_pos = pos
    vout_count, pos = read_varint(raw, pos)
    old_varint_len = varint_size(vout_count)

    # Skip all existing vout entries
    for _ in range(vout_count):
        pos += 8  # value (int64 LE)
        script_len, pos = read_varint(raw, pos)
        pos += script_len  # scriptPubKey

    vout_end_pos = pos

    # Build OP_RETURN scriptPubKey: OP_RETURN + push data
    script = bytes([0x6a])  # OP_RETURN
    if len(data) <= 75:
        script += bytes([len(data)]) + data
    else:
        script += bytes([0x4c, len(data)]) + data  # OP_PUSHDATA1

    # Build the new output: value=0 + script
    new_vout = (
        (0).to_bytes(8, 'little') +        # nValue = 0
        encode_varint(len(script)) +         # scriptPubKey length
        script                               # scriptPubKey
    )

    # Reconstruct TX: prefix + new vout count + existing vouts + new OP_RETURN vout + suffix
    new_raw = (
        raw[:vout_count_pos] +
        encode_varint(vout_count + 1) +
        raw[vout_count_pos + old_varint_len:vout_end_pos] +
        new_vout +
        raw[vout_end_pos:]
    )

    return new_raw.hex()


# =============================================================================
# REGISTRATION
# =============================================================================

def build_and_send_tx(cli: Path, op_return_data: bytes) -> str:
    """Build a TX with OP_RETURN, sign, and broadcast. Returns txid."""

    if len(op_return_data) > MAX_OP_RETURN_DATA:
        print(f"ERROR: OP_RETURN data too long ({len(op_return_data)} bytes, max {MAX_OP_RETURN_DATA})")
        sys.exit(1)

    # 1. Find a spendable UTXO
    utxos = rpc(cli, "listunspent", "1", "9999999")
    if not utxos:
        print("ERROR: No spendable UTXOs found. Fund this wallet first.")
        sys.exit(1)

    # Pick a UTXO with enough balance for the fee.
    # If --address was specified, prefer UTXOs at that address (for tier matching).
    # BATHRON amounts are in raw satoshis (integer)
    preferred_addr = getattr(build_and_send_tx, '_preferred_address', None)
    utxo = None
    fallback_utxo = None
    for u in utxos:
        if int(u["amount"]) >= MIN_FEE_SATS * 2:
            if preferred_addr and u.get("address") == preferred_addr:
                utxo = u
                break
            if fallback_utxo is None:
                fallback_utxo = u
    if utxo is None:
        utxo = fallback_utxo

    if not utxo:
        print(f"ERROR: No UTXO with balance >= {MIN_FEE_SATS * 2} sats")
        sys.exit(1)

    utxo_amount = int(utxo["amount"])
    print(f"Using UTXO: {utxo['txid']}:{utxo['vout']} ({utxo_amount} sats)")

    # 2. Get a change address
    change_addr = rpc(cli, "getnewaddress", "lp_register_change")
    if not change_addr:
        print("ERROR: Could not generate change address")
        sys.exit(1)

    change_amount = utxo_amount - MIN_FEE_SATS

    # 3. Create raw TX with just the change output
    # BATHRON createrawtransaction expects integer satoshi amounts
    inputs_json = json.dumps([{"txid": utxo["txid"], "vout": utxo["vout"]}])
    outputs_json = json.dumps({change_addr: change_amount})

    raw_hex = rpc(cli, "createrawtransaction", inputs_json, outputs_json)
    if not raw_hex:
        print("ERROR: createrawtransaction failed")
        sys.exit(1)

    # 4. Insert OP_RETURN output
    modified_hex = insert_op_return(raw_hex, op_return_data)

    # 5. Verify by decoding
    decoded = rpc(cli, "decoderawtransaction", modified_hex)
    if not decoded:
        print("ERROR: Modified TX failed to decode — raw TX manipulation error")
        sys.exit(1)

    # Verify OP_RETURN is present
    has_op_return = any(
        vout.get("scriptPubKey", {}).get("type") == "nulldata"
        for vout in decoded.get("vout", [])
    )
    if not has_op_return:
        print("ERROR: OP_RETURN output not found in decoded TX")
        sys.exit(1)

    print(f"TX built: {len(decoded['vout'])} outputs, OP_RETURN present")

    # 6. Sign
    signed = rpc(cli, "signrawtransaction", modified_hex)
    if not signed or not signed.get("complete"):
        print(f"ERROR: Signing failed: {signed}")
        sys.exit(1)

    # 7. Broadcast
    txid = rpc(cli, "sendrawtransaction", signed["hex"])
    if not txid:
        print("ERROR: Broadcast failed")
        sys.exit(1)

    return txid


def register_lp(cli: Path, endpoint: str):
    """Register LP on-chain."""
    # Validate URL format
    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        print(f"ERROR: Invalid endpoint URL. Must be http:// or https:// with hostname.")
        sys.exit(1)

    payload = f"{PNA_PROTOCOL_PREFIX}{endpoint}"
    data = payload.encode("utf-8")

    print(f"Registering LP: {endpoint}")
    print(f"OP_RETURN payload: {payload} ({len(data)} bytes)")

    txid = build_and_send_tx(cli, data)
    print(f"\nLP registered on-chain!")
    print(f"  TXID: {txid}")
    print(f"  Endpoint: {endpoint}")
    print(f"  Verify: bathron-cli -testnet getrawtransaction {txid} 1")


def unregister_lp(cli: Path):
    """Unregister LP on-chain."""
    data = PNA_UNREGISTER.encode("utf-8")

    print("Unregistering LP...")
    print(f"OP_RETURN payload: {PNA_UNREGISTER} ({len(data)} bytes)")

    txid = build_and_send_tx(cli, data)
    print(f"\nLP unregistered on-chain!")
    print(f"  TXID: {txid}")


def show_status(cli: Path):
    """Show wallet info relevant to LP registration."""
    # Wallet balance
    balance = rpc(cli, "getbalance")
    print(f"Wallet balance: {balance} sats")

    # Check if wallet.json exists
    wallet_file = BATHRON_KEY_DIR / "wallet.json"
    if wallet_file.exists():
        with open(wallet_file) as f:
            w = json.load(f)
        print(f"Wallet name: {w.get('name', '?')}")
        print(f"Address: {w.get('address', '?')}")
    else:
        print("WARNING: ~/.BathronKey/wallet.json not found")

    # Block height
    height = rpc(cli, "getblockcount")
    print(f"Chain height: {height}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="PNA LP Registration — Register/unregister LP on BATHRON chain"
    )
    parser.add_argument(
        "--endpoint", type=str,
        help="LP endpoint URL (e.g. http://57.131.33.152:8080)"
    )
    parser.add_argument(
        "--unregister", action="store_true",
        help="Unregister LP from the chain"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show wallet status"
    )
    parser.add_argument(
        "--operator-pubkey", type=str,
        help="MN operator public key (66 hex chars). Derives the operator "
             "address and sends registration TX from it (required for Tier 1)."
    )
    parser.add_argument(
        "--address", type=str,
        help="Prefer UTXO at this address (alternative to --operator-pubkey)"
    )
    args = parser.parse_args()

    cli = find_cli()
    print(f"Using CLI: {cli}\n")

    # Derive operator address from pubkey (takes priority over --address)
    if args.operator_pubkey:
        operator_addr = pubkey_to_address(args.operator_pubkey)
        print(f"Operator pubkey: {args.operator_pubkey}")
        print(f"Operator address: {operator_addr}")
        build_and_send_tx._preferred_address = operator_addr
    elif args.address:
        build_and_send_tx._preferred_address = args.address
        print(f"Preferred UTXO address: {args.address}")

    if args.status:
        show_status(cli)
    elif args.unregister:
        unregister_lp(cli)
    elif args.endpoint:
        # Validate URL length
        payload = f"{PNA_PROTOCOL_PREFIX}{args.endpoint}"
        if len(payload.encode("utf-8")) > MAX_OP_RETURN_DATA:
            max_url = MAX_OP_RETURN_DATA - len(PNA_PROTOCOL_PREFIX.encode("utf-8"))
            print(f"ERROR: URL too long. Max {max_url} chars for the endpoint URL.")
            sys.exit(1)
        register_lp(cli, args.endpoint)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
