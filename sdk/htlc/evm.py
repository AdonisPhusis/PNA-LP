"""
EVM HTLC SDK for P&A cross-chain swaps.

Interacts with the HashedTimelockERC20 contract on Base Sepolia.
Contract: 0xBCf3eeb42629143A1B29d9542fad0E54a04dBFD2
"""

import json
import logging
import subprocess
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

# Deployed HTLC contract on Base Sepolia
HTLC_CONTRACT_ADDRESS = "0xBCf3eeb42629143A1B29d9542fad0E54a04dBFD2"

# USDC on Base Sepolia (Circle's official testnet USDC)
USDC_CONTRACT_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# Base Sepolia RPC
RPC_URL = "https://sepolia.base.org"
CHAIN_ID = 84532

# Contract ABI (minimal - only functions we use)
HTLC_ABI = [
    {
        "name": "create",
        "type": "function",
        "inputs": [
            {"name": "receiver", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "hashlock", "type": "bytes32"},
            {"name": "timelock", "type": "uint256"}
        ],
        "outputs": [{"name": "htlcId", "type": "bytes32"}]
    },
    {
        "name": "withdraw",
        "type": "function",
        "inputs": [
            {"name": "htlcId", "type": "bytes32"},
            {"name": "preimage", "type": "bytes32"}
        ],
        "outputs": []
    },
    {
        "name": "refund",
        "type": "function",
        "inputs": [{"name": "htlcId", "type": "bytes32"}],
        "outputs": []
    },
    {
        "name": "getHTLC",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "htlcId", "type": "bytes32"}],
        "outputs": [
            {"name": "sender", "type": "address"},
            {"name": "receiver", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "hashlock", "type": "bytes32"},
            {"name": "timelock", "type": "uint256"},
            {"name": "withdrawn", "type": "bool"},
            {"name": "refunded", "type": "bool"},
            {"name": "preimage", "type": "bytes32"}
        ]
    },
    {
        "name": "canWithdraw",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "htlcId", "type": "bytes32"},
            {"name": "preimage", "type": "bytes32"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    },
    {
        "name": "canRefund",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "htlcId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}]
    }
]

# ERC20 approve ABI
ERC20_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "outputs": [{"name": "", "type": "uint256"}]
    }
]


@dataclass
class EVMHTLCResult:
    """Result from an HTLC operation."""
    success: bool
    htlc_id: Optional[str] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    data: Optional[Dict] = None


def _call_rpc(method: str, params: list = None, timeout: int = 30) -> Any:
    """Make JSON-RPC call to Base Sepolia."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or [],
        "id": 1
    }

    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", RPC_URL,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload),
             "--max-time", str(timeout)],
            capture_output=True,
            text=True,
            timeout=timeout + 5
        )

        if result.returncode != 0:
            raise RuntimeError(f"RPC failed: {result.stderr}")

        data = json.loads(result.stdout)

        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")

        return data.get("result")

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"RPC timeout: {method}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON: {e}")


def _encode_function_call(func_name: str, params: list, abi: list) -> str:
    """Encode a function call for EVM."""
    from hashlib import sha3_256

    # Find function in ABI
    func_abi = None
    for item in abi:
        if item.get("name") == func_name:
            func_abi = item
            break

    if not func_abi:
        raise ValueError(f"Function {func_name} not found in ABI")

    # Build function signature
    input_types = [inp["type"] for inp in func_abi.get("inputs", [])]
    sig = f"{func_name}({','.join(input_types)})"

    # Use keccak256 for function selector (first 4 bytes)
    import hashlib
    # Note: sha3_256 in hashlib is NOT keccak - we need to use the keccak library or eth-hash
    try:
        from Crypto.Hash import keccak
        k = keccak.new(digest_bits=256)
        k.update(sig.encode())
        selector = k.hexdigest()[:8]
    except ImportError:
        # Fallback: use web3 if available
        try:
            from web3 import Web3
            selector = Web3.keccak(text=sig).hex()[2:10]
        except ImportError:
            # Last resort: precomputed selectors
            SELECTORS = {
                "create(address,address,uint256,bytes32,uint256)": "ab12a820",
                "withdraw(bytes32,bytes32)": "7d5aa844",
                "refund(bytes32)": "7249fbb6",
                "getHTLC(bytes32)": "3db03a54",
                "canWithdraw(bytes32,bytes32)": "4ae5ed5c",
                "canRefund(bytes32)": "b9f61ebc",
                "approve(address,uint256)": "095ea7b3",
                "allowance(address,address)": "dd62ed3e",
            }
            selector = SELECTORS.get(sig, "")
            if not selector:
                raise RuntimeError(f"Cannot compute selector for {sig}")

    # Encode parameters
    encoded_params = ""
    for i, (param, ptype) in enumerate(zip(params, input_types)):
        if ptype == "address":
            # Remove 0x and pad to 32 bytes
            addr = param.lower().replace("0x", "")
            encoded_params += addr.zfill(64)
        elif ptype == "uint256":
            # Convert to hex and pad to 32 bytes
            encoded_params += hex(int(param))[2:].zfill(64)
        elif ptype == "bytes32":
            # Remove 0x if present and ensure 32 bytes
            b32 = param.replace("0x", "")
            if len(b32) != 64:
                raise ValueError(f"bytes32 must be 64 hex chars, got {len(b32)}")
            encoded_params += b32
        elif ptype == "bool":
            encoded_params += ("1" if param else "0").zfill(64)
        else:
            raise ValueError(f"Unsupported type: {ptype}")

    return "0x" + selector + encoded_params


def get_htlc(htlc_id: str, contract: str = HTLC_CONTRACT_ADDRESS) -> Optional[Dict]:
    """
    Get HTLC details from contract.

    Args:
        htlc_id: The HTLC identifier (bytes32 hex)
        contract: HTLC contract address

    Returns:
        Dict with HTLC details or None if not found
    """
    try:
        data = _encode_function_call("getHTLC", [htlc_id], HTLC_ABI)

        result = _call_rpc("eth_call", [
            {"to": contract, "data": data},
            "latest"
        ])

        if not result or result == "0x" or len(result) < 66:
            return None

        # Decode response (9 fields, each 32 bytes)
        # sender, receiver, token, amount, hashlock, timelock, withdrawn, refunded, preimage
        raw = result[2:]  # Remove 0x

        if len(raw) < 576:  # 9 * 64 = 576
            return None

        sender = "0x" + raw[24:64]  # address is last 20 bytes of 32
        receiver = "0x" + raw[88:128]
        token = "0x" + raw[152:192]
        amount = int(raw[192:256], 16)
        hashlock = "0x" + raw[256:320]
        timelock = int(raw[320:384], 16)
        withdrawn = int(raw[384:448], 16) == 1
        refunded = int(raw[448:512], 16) == 1
        preimage = "0x" + raw[512:576]

        # Check if empty (sender == 0x0)
        if sender == "0x" + "0" * 40:
            return None

        return {
            "htlc_id": htlc_id,
            "sender": sender,
            "receiver": receiver,
            "token": token,
            "amount": amount,
            "amount_usdc": amount / 1e6,  # USDC has 6 decimals
            "hashlock": hashlock,
            "timelock": timelock,
            "timelock_datetime": datetime.fromtimestamp(timelock).isoformat() if timelock > 0 else None,
            "withdrawn": withdrawn,
            "refunded": refunded,
            "preimage": preimage if preimage != "0x" + "0" * 64 else None,
            "status": "withdrawn" if withdrawn else "refunded" if refunded else "active"
        }

    except Exception as e:
        log.error(f"Failed to get HTLC: {e}")
        return None


def can_withdraw(htlc_id: str, preimage: str, contract: str = HTLC_CONTRACT_ADDRESS) -> bool:
    """Check if HTLC can be withdrawn with given preimage."""
    try:
        data = _encode_function_call("canWithdraw", [htlc_id, preimage], HTLC_ABI)

        result = _call_rpc("eth_call", [
            {"to": contract, "data": data},
            "latest"
        ])

        if result and len(result) >= 66:
            return int(result, 16) == 1
        return False

    except Exception as e:
        log.error(f"canWithdraw check failed: {e}")
        return False


def can_refund(htlc_id: str, contract: str = HTLC_CONTRACT_ADDRESS) -> bool:
    """Check if HTLC can be refunded (timelock expired)."""
    try:
        data = _encode_function_call("canRefund", [htlc_id], HTLC_ABI)

        result = _call_rpc("eth_call", [
            {"to": contract, "data": data},
            "latest"
        ])

        if result and len(result) >= 66:
            return int(result, 16) == 1
        return False

    except Exception as e:
        log.error(f"canRefund check failed: {e}")
        return False


def create_htlc(
    receiver: str,
    amount_usdc: float,
    hashlock: str,
    timelock_seconds: int,
    private_key: str,
    token: str = USDC_CONTRACT_ADDRESS,
    contract: str = HTLC_CONTRACT_ADDRESS
) -> EVMHTLCResult:
    """
    Create a new USDC HTLC.

    Args:
        receiver: Address that can claim with preimage
        amount_usdc: Amount in USDC (human readable, e.g., 10.5)
        hashlock: SHA256 hash of the secret (bytes32 hex)
        timelock_seconds: How long until refund is possible
        private_key: LP's private key for signing
        token: ERC20 token address (default: USDC)
        contract: HTLC contract address

    Returns:
        EVMHTLCResult with htlc_id and tx_hash on success
    """
    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(RPC_URL))

        # Validate
        if not w3.is_connected():
            return EVMHTLCResult(success=False, error="Cannot connect to RPC")

        # Get sender address from private key
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        account = Account.from_key(private_key)
        sender = account.address

        # Convert amount to wei (USDC has 6 decimals)
        amount_wei = int(amount_usdc * 1e6)

        # Calculate timelock (current time + seconds)
        import time
        timelock = int(time.time()) + timelock_seconds

        # First, approve HTLC contract to spend USDC
        log.info(f"Approving {amount_usdc} USDC for HTLC contract...")

        # Check current allowance
        usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token),
            abi=ERC20_APPROVE_ABI
        )

        current_allowance = usdc_contract.functions.allowance(
            sender,
            Web3.to_checksum_address(contract)
        ).call()

        if current_allowance < amount_wei:
            # Approve spending - use a large amount to avoid repeated approvals
            # Use max uint256 for unlimited approval (common pattern for DEXes)
            MAX_UINT256 = 2**256 - 1
            approve_amount = MAX_UINT256

            nonce = w3.eth.get_transaction_count(sender, 'pending')
            gas_price = int(w3.eth.gas_price * 1.1)  # 10% buffer for replacement

            log.info(f"Current allowance: {current_allowance}, approving max amount")

            approve_tx = usdc_contract.functions.approve(
                Web3.to_checksum_address(contract),
                approve_amount
            ).build_transaction({
                'from': sender,
                'nonce': nonce,
                'gas': 100000,
                'gasPrice': gas_price,
                'chainId': CHAIN_ID
            })

            signed_approve = account.sign_transaction(approve_tx)
            approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)

            log.info(f"Approve TX: {approve_hash.hex()}")

            # Wait for approval
            receipt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=60)
            if receipt['status'] != 1:
                return EVMHTLCResult(success=False, error="Approval failed")

        # Verify current allowance after potential approval
        updated_allowance = usdc_contract.functions.allowance(
            sender,
            Web3.to_checksum_address(contract)
        ).call()
        log.info(f"Allowance after approval check: {updated_allowance}")

        if updated_allowance < amount_wei:
            return EVMHTLCResult(success=False, error=f"Insufficient allowance: {updated_allowance} < {amount_wei}")

        # Now create the HTLC
        log.info(f"Creating HTLC: {amount_usdc} USDC, receiver={receiver[:10]}...")

        htlc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract),
            abi=HTLC_ABI
        )

        # Ensure hashlock is bytes32
        if not hashlock.startswith("0x"):
            hashlock = "0x" + hashlock
        hashlock_bytes = bytes.fromhex(hashlock[2:])

        # Try to simulate the call first
        try:
            simulated_id = htlc_contract.functions.create(
                Web3.to_checksum_address(receiver),
                Web3.to_checksum_address(token),
                amount_wei,
                hashlock_bytes,
                timelock
            ).call({'from': sender})
            log.info(f"Simulation succeeded, expected htlc_id: 0x{simulated_id.hex()}")
        except Exception as sim_err:
            log.error(f"Simulation failed: {sim_err}")
            return EVMHTLCResult(success=False, error=f"Simulation failed: {sim_err}")

        # Get nonce including pending transactions
        nonce = w3.eth.get_transaction_count(sender, 'pending')
        gas_price = int(w3.eth.gas_price * 1.1)  # 10% buffer

        create_tx = htlc_contract.functions.create(
            Web3.to_checksum_address(receiver),
            Web3.to_checksum_address(token),
            amount_wei,
            hashlock_bytes,
            timelock
        ).build_transaction({
            'from': sender,
            'nonce': nonce,
            'gas': 300000,  # Increase gas limit
            'gasPrice': gas_price,
            'chainId': CHAIN_ID
        })

        signed_create = account.sign_transaction(create_tx)
        create_hash = w3.eth.send_raw_transaction(signed_create.raw_transaction)

        log.info(f"Create TX: {create_hash.hex()}")

        # Wait for confirmation
        receipt = w3.eth.wait_for_transaction_receipt(create_hash, timeout=120)

        if receipt['status'] != 1:
            return EVMHTLCResult(success=False, error="Create transaction failed", tx_hash=create_hash.hex())

        # Extract htlcId from HTLCCreated event
        # Event signature for HTLCCreated(bytes32 indexed,address indexed,address indexed,address,uint256,bytes32,uint256)
        # We need to find the log from the HTLC contract (not the USDC Transfer event)
        htlc_id = None
        htlc_contract_lower = contract.lower()

        for log_entry in receipt['logs']:
            # Check if this log is from our HTLC contract
            log_address = log_entry['address'].lower() if isinstance(log_entry['address'], str) else log_entry['address'].hex().lower()
            if log_address == htlc_contract_lower and len(log_entry['topics']) >= 2:
                # topics[0] = event signature
                # topics[1] = htlcId (indexed)
                topic1 = log_entry['topics'][1]
                htlc_id = topic1.hex() if hasattr(topic1, 'hex') else topic1
                # Remove 0x prefix if present for consistency
                if htlc_id.startswith('0x'):
                    htlc_id = htlc_id[2:]
                htlc_id = '0x' + htlc_id
                log.info(f"Extracted htlc_id from event: {htlc_id}")
                break

        if not htlc_id:
            # Fallback: use the simulated ID
            log.warning("Could not extract htlc_id from event, using simulated value")
            htlc_id = f"0x{simulated_id.hex()}"

        return EVMHTLCResult(
            success=True,
            htlc_id=htlc_id,
            tx_hash=create_hash.hex(),
            data={
                "receiver": receiver,
                "token": token,
                "amount_usdc": amount_usdc,
                "amount_wei": amount_wei,
                "hashlock": hashlock,
                "timelock": timelock,
                "sender": sender
            }
        )

    except ImportError as e:
        return EVMHTLCResult(success=False, error=f"Missing dependency: {e}. Install with: pip3 install web3 eth-account")
    except Exception as e:
        log.exception("Failed to create HTLC")
        return EVMHTLCResult(success=False, error=str(e))


def withdraw_htlc(
    htlc_id: str,
    preimage: str,
    private_key: str,
    contract: str = HTLC_CONTRACT_ADDRESS
) -> EVMHTLCResult:
    """
    Withdraw from HTLC using the preimage.

    Args:
        htlc_id: The HTLC identifier
        preimage: The secret that hashes to hashlock
        private_key: Receiver's private key
        contract: HTLC contract address

    Returns:
        EVMHTLCResult with tx_hash on success
    """
    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(RPC_URL))

        if not w3.is_connected():
            return EVMHTLCResult(success=False, error="Cannot connect to RPC")

        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        account = Account.from_key(private_key)

        # Ensure proper formatting
        if not htlc_id.startswith("0x"):
            htlc_id = "0x" + htlc_id
        if not preimage.startswith("0x"):
            preimage = "0x" + preimage

        htlc_id_bytes = bytes.fromhex(htlc_id[2:])
        preimage_bytes = bytes.fromhex(preimage[2:])

        htlc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract),
            abi=HTLC_ABI
        )

        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        gas_price = int(w3.eth.gas_price * 1.1)

        withdraw_tx = htlc_contract.functions.withdraw(
            htlc_id_bytes,
            preimage_bytes
        ).build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 150000,
            'gasPrice': gas_price,
            'chainId': CHAIN_ID
        })

        signed_tx = account.sign_transaction(withdraw_tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        log.info(f"Withdraw TX: {tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt['status'] != 1:
            return EVMHTLCResult(success=False, error="Withdraw failed", tx_hash=tx_hash.hex())

        return EVMHTLCResult(
            success=True,
            htlc_id=htlc_id,
            tx_hash=tx_hash.hex()
        )

    except ImportError as e:
        return EVMHTLCResult(success=False, error=f"Missing dependency: {e}")
    except Exception as e:
        log.exception("Failed to withdraw HTLC")
        return EVMHTLCResult(success=False, error=str(e))


def refund_htlc(
    htlc_id: str,
    private_key: str,
    contract: str = HTLC_CONTRACT_ADDRESS
) -> EVMHTLCResult:
    """
    Refund expired HTLC.

    Args:
        htlc_id: The HTLC identifier
        private_key: Sender's private key
        contract: HTLC contract address

    Returns:
        EVMHTLCResult with tx_hash on success
    """
    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(RPC_URL))

        if not w3.is_connected():
            return EVMHTLCResult(success=False, error="Cannot connect to RPC")

        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        account = Account.from_key(private_key)

        if not htlc_id.startswith("0x"):
            htlc_id = "0x" + htlc_id
        htlc_id_bytes = bytes.fromhex(htlc_id[2:])

        htlc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract),
            abi=HTLC_ABI
        )

        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        gas_price = int(w3.eth.gas_price * 1.1)

        refund_tx = htlc_contract.functions.refund(
            htlc_id_bytes
        ).build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 100000,
            'gasPrice': gas_price,
            'chainId': CHAIN_ID
        })

        signed_tx = account.sign_transaction(refund_tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        log.info(f"Refund TX: {tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt['status'] != 1:
            return EVMHTLCResult(success=False, error="Refund failed", tx_hash=tx_hash.hex())

        return EVMHTLCResult(
            success=True,
            htlc_id=htlc_id,
            tx_hash=tx_hash.hex()
        )

    except ImportError as e:
        return EVMHTLCResult(success=False, error=f"Missing dependency: {e}")
    except Exception as e:
        log.exception("Failed to refund HTLC")
        return EVMHTLCResult(success=False, error=str(e))


# Convenience function for checking balances
def get_usdc_balance(address: str, token: str = USDC_CONTRACT_ADDRESS) -> float:
    """Get USDC balance for an address."""
    try:
        # balanceOf(address) selector = 0x70a08231
        addr_padded = address.lower().replace("0x", "").zfill(64)
        data = f"0x70a08231{addr_padded}"

        result = _call_rpc("eth_call", [
            {"to": token, "data": data},
            "latest"
        ])

        if result and result != "0x":
            raw = int(result, 16)
            return raw / 1e6  # USDC has 6 decimals
        return 0.0

    except Exception as e:
        log.error(f"Failed to get USDC balance: {e}")
        return 0.0


def get_eth_balance(address: str) -> float:
    """Get ETH balance for an address."""
    try:
        result = _call_rpc("eth_getBalance", [address, "latest"])
        if result:
            return int(result, 16) / 1e18
        return 0.0
    except Exception as e:
        log.error(f"Failed to get ETH balance: {e}")
        return 0.0
