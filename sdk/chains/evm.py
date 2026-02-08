"""
EVM RPC Client for pna SDK.

Supports Ethereum-compatible chains (Base, Ethereum, etc.) for USDC swaps.
"""

import json
import logging
import subprocess
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Common USDC contract addresses
USDC_CONTRACTS = {
    "base_sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base_mainnet": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "ethereum": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
}

# RPC endpoints
RPC_ENDPOINTS = {
    "base_sepolia": "https://sepolia.base.org",
    "base_mainnet": "https://mainnet.base.org",
}


@dataclass
class EVMConfig:
    """EVM chain configuration."""
    network: str = "base_sepolia"
    rpc_url: str = "https://sepolia.base.org"
    chain_id: int = 84532  # Base Sepolia
    usdc_contract: str = ""
    private_key: str = ""  # For signing (optional)


class EVMClient:
    """
    EVM RPC client using curl for simplicity.

    For production, use web3.py or ethers.
    """

    def __init__(self, config: EVMConfig):
        self.config = config
        self.rpc_url = config.rpc_url or RPC_ENDPOINTS.get(config.network, "")
        self.usdc_contract = config.usdc_contract or USDC_CONTRACTS.get(config.network, "")

    def _call_rpc(self, method: str, params: List = None, timeout: int = 15) -> Any:
        """Make JSON-RPC call via curl."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": 1
        }

        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST", self.rpc_url,
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload),
                 "--max-time", str(timeout)],
                capture_output=True,
                text=True,
                timeout=timeout + 5
            )

            if result.returncode != 0:
                raise RuntimeError(f"RPC call failed: {result.stderr}")

            data = json.loads(result.stdout)

            if "error" in data:
                raise RuntimeError(f"RPC error: {data['error']}")

            return data.get("result")

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"RPC timeout: {method}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {e}")

    # =========================================================================
    # Basic Operations
    # =========================================================================

    def get_block_number(self) -> int:
        """Get current block number."""
        result = self._call_rpc("eth_blockNumber")
        return int(result, 16) if result else 0

    def get_chain_id(self) -> int:
        """Get chain ID."""
        result = self._call_rpc("eth_chainId")
        return int(result, 16) if result else 0

    def get_gas_price(self) -> int:
        """Get current gas price in wei."""
        result = self._call_rpc("eth_gasPrice")
        return int(result, 16) if result else 0

    # =========================================================================
    # Balance Operations
    # =========================================================================

    def get_eth_balance(self, address: str) -> float:
        """Get ETH balance in ETH (not wei)."""
        result = self._call_rpc("eth_getBalance", [address, "latest"])
        if result:
            wei = int(result, 16)
            return wei / 1e18
        return 0.0

    def get_usdc_balance(self, address: str) -> float:
        """Get USDC token balance."""
        if not self.usdc_contract:
            return 0.0

        # balanceOf(address) = 0x70a08231 + padded address
        address_padded = address.lower().replace("0x", "").zfill(64)
        data = f"0x70a08231{address_padded}"

        result = self._call_rpc("eth_call", [
            {"to": self.usdc_contract, "data": data},
            "latest"
        ])

        if result and result != "0x":
            raw_balance = int(result, 16)
            # USDC has 6 decimals
            return raw_balance / 1e6
        return 0.0

    def get_token_balance(self, token_address: str, wallet_address: str,
                         decimals: int = 18) -> float:
        """Get ERC20 token balance."""
        address_padded = wallet_address.lower().replace("0x", "").zfill(64)
        data = f"0x70a08231{address_padded}"

        result = self._call_rpc("eth_call", [
            {"to": token_address, "data": data},
            "latest"
        ])

        if result and result != "0x":
            raw_balance = int(result, 16)
            return raw_balance / (10 ** decimals)
        return 0.0

    # =========================================================================
    # Transaction Operations
    # =========================================================================

    def get_transaction(self, txid: str) -> Optional[Dict]:
        """Get transaction by hash."""
        return self._call_rpc("eth_getTransactionByHash", [txid])

    def get_transaction_receipt(self, txid: str) -> Optional[Dict]:
        """Get transaction receipt."""
        return self._call_rpc("eth_getTransactionReceipt", [txid])

    def get_transaction_count(self, address: str) -> int:
        """Get nonce for address."""
        result = self._call_rpc("eth_getTransactionCount", [address, "latest"])
        return int(result, 16) if result else 0

    def send_raw_transaction(self, signed_tx: str) -> str:
        """Broadcast signed transaction."""
        return self._call_rpc("eth_sendRawTransaction", [signed_tx])

    def estimate_gas(self, tx: Dict) -> int:
        """Estimate gas for transaction."""
        result = self._call_rpc("eth_estimateGas", [tx])
        return int(result, 16) if result else 21000

    # =========================================================================
    # Contract Calls
    # =========================================================================

    def call(self, to: str, data: str, from_addr: str = None) -> str:
        """Make eth_call to contract."""
        params = {"to": to, "data": data}
        if from_addr:
            params["from"] = from_addr
        return self._call_rpc("eth_call", [params, "latest"])

    # =========================================================================
    # HTLC Contract Interaction (placeholder for future implementation)
    # =========================================================================

    def deploy_htlc_contract(self) -> str:
        """Deploy HTLC contract. Requires private key."""
        # TODO: Implement contract deployment
        raise NotImplementedError("Contract deployment not yet implemented")

    def create_htlc(self, htlc_contract: str, recipient: str,
                   hashlock: str, timelock: int, amount: int) -> str:
        """Create HTLC on EVM contract."""
        # TODO: Implement HTLC creation
        raise NotImplementedError("HTLC creation not yet implemented")

    def claim_htlc(self, htlc_contract: str, htlc_id: str, preimage: str) -> str:
        """Claim HTLC with preimage."""
        # TODO: Implement HTLC claim
        raise NotImplementedError("HTLC claim not yet implemented")

    def refund_htlc(self, htlc_contract: str, htlc_id: str) -> str:
        """Refund expired HTLC."""
        # TODO: Implement HTLC refund
        raise NotImplementedError("HTLC refund not yet implemented")

    # =========================================================================
    # Utility
    # =========================================================================

    def is_valid_address(self, address: str) -> bool:
        """Check if address is valid."""
        if not address:
            return False
        if not address.startswith("0x"):
            return False
        if len(address) != 42:
            return False
        try:
            int(address, 16)
            return True
        except ValueError:
            return False

    def to_checksum_address(self, address: str) -> str:
        """Convert to checksum address."""
        # Simple implementation - for production use web3.py
        import hashlib
        address = address.lower().replace("0x", "")
        hash_bytes = hashlib.keccak_256(address.encode()).hexdigest()

        checksum = "0x"
        for i, char in enumerate(address):
            if char in "0123456789":
                checksum += char
            elif int(hash_bytes[i], 16) >= 8:
                checksum += char.upper()
            else:
                checksum += char.lower()
        return checksum
