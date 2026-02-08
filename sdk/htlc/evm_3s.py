"""
EVM HTLC with 3-secrets support for FlowSwap protocol.

Interacts with the HashedTimelockERC20_3S contract on Base Sepolia.

Key features:
- 3 hashlocks: H_user, H_lp1, H_lp2
- Claim is PERMISSIONLESS: anyone can call, funds go to fixed recipient
- Uses SHA256 for cross-chain compatibility with Bitcoin
"""

import json
import logging
import time
from typing import Optional, Dict, Tuple
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Contract deployment info (to be updated after deploy)
HTLC3S_CONTRACT_ADDRESS = "0x2493EaaaBa6B129962c8967AaEE6bF11D0277756"

# USDC on Base Sepolia
USDC_CONTRACT_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# Base Sepolia RPC
RPC_URL = "https://sepolia.base.org"
CHAIN_ID = 84532

# HTLC3S ABI (minimal)
HTLC3S_ABI = [
    {
        "name": "create",
        "type": "function",
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "H_user", "type": "bytes32"},
            {"name": "H_lp1", "type": "bytes32"},
            {"name": "H_lp2", "type": "bytes32"},
            {"name": "timelock", "type": "uint256"}
        ],
        "outputs": [{"name": "htlcId", "type": "bytes32"}]
    },
    {
        "name": "claim",
        "type": "function",
        "inputs": [
            {"name": "htlcId", "type": "bytes32"},
            {"name": "S_user", "type": "bytes32"},
            {"name": "S_lp1", "type": "bytes32"},
            {"name": "S_lp2", "type": "bytes32"}
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
        "name": "canClaim",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "htlcId", "type": "bytes32"},
            {"name": "S_user", "type": "bytes32"},
            {"name": "S_lp1", "type": "bytes32"},
            {"name": "S_lp2", "type": "bytes32"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    },
    {
        "name": "canRefund",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "htlcId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}]
    },
    {
        "name": "getHTLC",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "htlcId", "type": "bytes32"}],
        "outputs": [
            {"name": "sender", "type": "address"},
            {"name": "recipient", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "H_user", "type": "bytes32"},
            {"name": "H_lp1", "type": "bytes32"},
            {"name": "H_lp2", "type": "bytes32"},
            {"name": "timelock", "type": "uint256"},
            {"name": "claimed", "type": "bool"},
            {"name": "refunded", "type": "bool"}
        ]
    }
]

# ERC20 ABI for approve
ERC20_ABI = [
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
class HTLC3SResult:
    """Result from a 3S HTLC operation."""
    success: bool
    htlc_id: Optional[str] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    data: Optional[Dict] = None


@dataclass
class HTLC3SInfo:
    """Information about a 3S HTLC."""
    htlc_id: str
    sender: str
    recipient: str
    token: str
    amount: int
    amount_usdc: float
    H_user: str
    H_lp1: str
    H_lp2: str
    timelock: int
    claimed: bool
    refunded: bool
    status: str  # "active", "claimed", "refunded", "expired"


class EVMHTLC3S:
    """
    EVM HTLC manager with 3-secret support.

    Interacts with HashedTimelockERC20_3S contract.
    """

    def __init__(
        self,
        contract_address: str = None,
        rpc_url: str = RPC_URL,
        chain_id: int = CHAIN_ID
    ):
        """
        Initialize EVM HTLC 3S client.

        Args:
            contract_address: HTLC3S contract address (required)
            rpc_url: Ethereum JSON-RPC URL
            chain_id: Chain ID
        """
        self.contract_address = contract_address or HTLC3S_CONTRACT_ADDRESS
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self._web3 = None

    @property
    def web3(self):
        """Lazy-load web3 instance."""
        if self._web3 is None:
            from web3 import Web3
            self._web3 = Web3(Web3.HTTPProvider(self.rpc_url))
        return self._web3

    def get_htlc(self, htlc_id: str) -> Optional[HTLC3SInfo]:
        """
        Get HTLC details.

        Args:
            htlc_id: HTLC identifier (bytes32 hex)

        Returns:
            HTLC3SInfo or None if not found
        """
        from web3 import Web3

        try:
            contract = self.web3.eth.contract(
                address=Web3.to_checksum_address(self.contract_address),
                abi=HTLC3S_ABI
            )

            if not htlc_id.startswith("0x"):
                htlc_id = "0x" + htlc_id

            result = contract.functions.getHTLC(bytes.fromhex(htlc_id[2:])).call()

            sender, recipient, token, amount, H_user, H_lp1, H_lp2, timelock, claimed, refunded = result

            # Check if HTLC exists
            if sender == "0x" + "0" * 40:
                return None

            # Determine status
            now = int(time.time())
            if claimed:
                status = "claimed"
            elif refunded:
                status = "refunded"
            elif now >= timelock:
                status = "expired"
            else:
                status = "active"

            return HTLC3SInfo(
                htlc_id=htlc_id,
                sender=sender,
                recipient=recipient,
                token=token,
                amount=amount,
                amount_usdc=amount / 1e6,
                H_user="0x" + H_user.hex(),
                H_lp1="0x" + H_lp1.hex(),
                H_lp2="0x" + H_lp2.hex(),
                timelock=timelock,
                claimed=claimed,
                refunded=refunded,
                status=status,
            )

        except Exception as e:
            log.error(f"Failed to get HTLC: {e}")
            return None

    def can_claim(
        self,
        htlc_id: str,
        S_user: str,
        S_lp1: str,
        S_lp2: str
    ) -> bool:
        """
        Check if claim would succeed with given secrets.

        Args:
            htlc_id: HTLC identifier
            S_user: User's secret (32 bytes hex)
            S_lp1: LP1's secret (32 bytes hex)
            S_lp2: LP2's secret (32 bytes hex)

        Returns:
            True if claim would succeed
        """
        from web3 import Web3

        try:
            contract = self.web3.eth.contract(
                address=Web3.to_checksum_address(self.contract_address),
                abi=HTLC3S_ABI
            )

            # Normalize inputs
            if not htlc_id.startswith("0x"):
                htlc_id = "0x" + htlc_id
            if not S_user.startswith("0x"):
                S_user = "0x" + S_user
            if not S_lp1.startswith("0x"):
                S_lp1 = "0x" + S_lp1
            if not S_lp2.startswith("0x"):
                S_lp2 = "0x" + S_lp2

            return contract.functions.canClaim(
                bytes.fromhex(htlc_id[2:]),
                bytes.fromhex(S_user[2:]),
                bytes.fromhex(S_lp1[2:]),
                bytes.fromhex(S_lp2[2:])
            ).call()

        except Exception as e:
            log.error(f"canClaim check failed: {e}")
            return False

    def create_htlc(
        self,
        recipient: str,
        amount_usdc: float,
        H_user: str,
        H_lp1: str,
        H_lp2: str,
        timelock_seconds: int,
        private_key: str,
        token: str = USDC_CONTRACT_ADDRESS
    ) -> HTLC3SResult:
        """
        Create a new 3S HTLC.

        Args:
            recipient: Address that receives on claim (FIXED)
            amount_usdc: Amount in USDC (human readable)
            H_user: SHA256 hash of user's secret
            H_lp1: SHA256 hash of LP1's secret
            H_lp2: SHA256 hash of LP2's secret
            timelock_seconds: Seconds until refund allowed
            private_key: Sender's private key
            token: ERC20 token address (default USDC)

        Returns:
            HTLC3SResult with htlc_id on success
        """
        from web3 import Web3
        from eth_account import Account

        try:
            if not self.contract_address:
                return HTLC3SResult(success=False, error="Contract address not set")

            w3 = self.web3
            if not w3.is_connected():
                return HTLC3SResult(success=False, error="Cannot connect to RPC")

            # Get sender from private key
            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            account = Account.from_key(private_key)
            sender = account.address

            # Convert amount (USDC has 6 decimals)
            amount_wei = int(amount_usdc * 1e6)

            # Calculate timelock
            timelock = int(time.time()) + timelock_seconds

            # Normalize hashlocks
            if not H_user.startswith("0x"):
                H_user = "0x" + H_user
            if not H_lp1.startswith("0x"):
                H_lp1 = "0x" + H_lp1
            if not H_lp2.startswith("0x"):
                H_lp2 = "0x" + H_lp2

            # Approve token spending
            log.info(f"Checking allowance for {amount_usdc} USDC...")
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(token),
                abi=ERC20_ABI
            )

            allowance = usdc.functions.allowance(
                sender,
                Web3.to_checksum_address(self.contract_address)
            ).call()

            if allowance < amount_wei:
                log.info(f"Approving USDC spending...")
                nonce = w3.eth.get_transaction_count(sender, 'pending')
                gas_price = int(w3.eth.gas_price * 1.1)

                approve_tx = usdc.functions.approve(
                    Web3.to_checksum_address(self.contract_address),
                    2**256 - 1  # Max approval
                ).build_transaction({
                    'from': sender,
                    'nonce': nonce,
                    'gas': 100000,
                    'gasPrice': gas_price,
                    'chainId': self.chain_id
                })

                signed = account.sign_transaction(approve_tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

                if receipt['status'] != 1:
                    return HTLC3SResult(success=False, error="Approval failed")

            # Create HTLC
            log.info(f"Creating 3S HTLC: {amount_usdc} USDC to {recipient[:10]}...")

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(self.contract_address),
                abi=HTLC3S_ABI
            )

            nonce = w3.eth.get_transaction_count(sender, 'pending')
            gas_price = int(w3.eth.gas_price * 1.1)

            create_tx = contract.functions.create(
                Web3.to_checksum_address(recipient),
                Web3.to_checksum_address(token),
                amount_wei,
                bytes.fromhex(H_user[2:]),
                bytes.fromhex(H_lp1[2:]),
                bytes.fromhex(H_lp2[2:]),
                timelock
            ).build_transaction({
                'from': sender,
                'nonce': nonce,
                'gas': 350000,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            })

            signed = account.sign_transaction(create_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

            log.info(f"Create TX: {tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt['status'] != 1:
                return HTLC3SResult(
                    success=False,
                    error="Create failed",
                    tx_hash=tx_hash.hex()
                )

            # Extract htlcId from logs
            htlc_id = None
            contract_lower = self.contract_address.lower()

            for log_entry in receipt['logs']:
                log_addr = log_entry['address'].lower()
                if log_addr == contract_lower and len(log_entry['topics']) >= 2:
                    topic = log_entry['topics'][1]
                    htlc_id = "0x" + (topic.hex() if hasattr(topic, 'hex') else topic)
                    break

            if not htlc_id:
                return HTLC3SResult(
                    success=False,
                    error="Could not extract htlcId",
                    tx_hash=tx_hash.hex()
                )

            return HTLC3SResult(
                success=True,
                htlc_id=htlc_id,
                tx_hash=tx_hash.hex(),
                data={
                    "sender": sender,
                    "recipient": recipient,
                    "token": token,
                    "amount_usdc": amount_usdc,
                    "H_user": H_user,
                    "H_lp1": H_lp1,
                    "H_lp2": H_lp2,
                    "timelock": timelock,
                }
            )

        except ImportError as e:
            return HTLC3SResult(
                success=False,
                error=f"Missing dependency: {e}. Install: pip3 install web3 eth-account"
            )
        except Exception as e:
            log.exception("Failed to create HTLC")
            return HTLC3SResult(success=False, error=str(e))

    def claim_htlc(
        self,
        htlc_id: str,
        S_user: str,
        S_lp1: str,
        S_lp2: str,
        private_key: str
    ) -> HTLC3SResult:
        """
        Claim HTLC with 3 preimages.

        PERMISSIONLESS: Anyone can call, funds go to fixed recipient.

        Args:
            htlc_id: HTLC identifier
            S_user: User's secret
            S_lp1: LP1's secret
            S_lp2: LP2's secret
            private_key: Caller's private key (pays gas, any address OK)

        Returns:
            HTLC3SResult with tx_hash on success
        """
        from web3 import Web3
        from eth_account import Account

        try:
            w3 = self.web3
            if not w3.is_connected():
                return HTLC3SResult(success=False, error="Cannot connect to RPC")

            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            account = Account.from_key(private_key)

            # Normalize inputs
            if not htlc_id.startswith("0x"):
                htlc_id = "0x" + htlc_id
            if not S_user.startswith("0x"):
                S_user = "0x" + S_user
            if not S_lp1.startswith("0x"):
                S_lp1 = "0x" + S_lp1
            if not S_lp2.startswith("0x"):
                S_lp2 = "0x" + S_lp2

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(self.contract_address),
                abi=HTLC3S_ABI
            )

            nonce = w3.eth.get_transaction_count(account.address, 'pending')
            gas_price = int(w3.eth.gas_price * 1.1)

            claim_tx = contract.functions.claim(
                bytes.fromhex(htlc_id[2:]),
                bytes.fromhex(S_user[2:]),
                bytes.fromhex(S_lp1[2:]),
                bytes.fromhex(S_lp2[2:])
            ).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gas': 200000,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            })

            signed = account.sign_transaction(claim_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

            log.info(f"Claim TX: {tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt['status'] != 1:
                return HTLC3SResult(
                    success=False,
                    error="Claim failed",
                    tx_hash=tx_hash.hex()
                )

            return HTLC3SResult(
                success=True,
                htlc_id=htlc_id,
                tx_hash=tx_hash.hex()
            )

        except ImportError as e:
            return HTLC3SResult(success=False, error=f"Missing dependency: {e}")
        except Exception as e:
            log.exception("Failed to claim HTLC")
            return HTLC3SResult(success=False, error=str(e))

    def refund_htlc(self, htlc_id: str, private_key: str) -> HTLC3SResult:
        """
        Refund expired HTLC.

        Args:
            htlc_id: HTLC identifier
            private_key: Sender's private key

        Returns:
            HTLC3SResult with tx_hash on success
        """
        from web3 import Web3
        from eth_account import Account

        try:
            w3 = self.web3
            if not w3.is_connected():
                return HTLC3SResult(success=False, error="Cannot connect to RPC")

            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            account = Account.from_key(private_key)

            if not htlc_id.startswith("0x"):
                htlc_id = "0x" + htlc_id

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(self.contract_address),
                abi=HTLC3S_ABI
            )

            nonce = w3.eth.get_transaction_count(account.address, 'pending')
            gas_price = int(w3.eth.gas_price * 1.1)

            refund_tx = contract.functions.refund(
                bytes.fromhex(htlc_id[2:])
            ).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gas': 150000,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            })

            signed = account.sign_transaction(refund_tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

            log.info(f"Refund TX: {tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt['status'] != 1:
                return HTLC3SResult(
                    success=False,
                    error="Refund failed",
                    tx_hash=tx_hash.hex()
                )

            return HTLC3SResult(
                success=True,
                htlc_id=htlc_id,
                tx_hash=tx_hash.hex()
            )

        except ImportError as e:
            return HTLC3SResult(success=False, error=f"Missing dependency: {e}")
        except Exception as e:
            log.exception("Failed to refund HTLC")
            return HTLC3SResult(success=False, error=str(e))


def deploy_htlc3s_contract(private_key: str, rpc_url: str = RPC_URL) -> Tuple[str, str]:
    """
    Deploy HTLC3S contract.

    Args:
        private_key: Deployer's private key
        rpc_url: Ethereum RPC URL

    Returns:
        (contract_address, tx_hash)
    """
    from web3 import Web3
    from eth_account import Account
    import solcx

    log.info("Compiling HTLC3S contract...")

    # Install solc if needed
    solcx.install_solc('0.8.20')

    # Read contract source
    import os
    contract_path = os.path.join(
        os.path.dirname(__file__),
        '..', '..', 'contracts', 'HTLC3S.sol'
    )

    with open(contract_path, 'r') as f:
        source = f.read()

    # Compile
    compiled = solcx.compile_source(
        source,
        output_values=['abi', 'bin'],
        solc_version='0.8.20',
        import_remappings=[
            '@openzeppelin/contracts/=node_modules/@openzeppelin/contracts/'
        ]
    )

    contract_id = '<stdin>:HashedTimelockERC20_3S'
    contract_interface = compiled[contract_id]
    bytecode = contract_interface['bin']
    abi = contract_interface['abi']

    # Deploy
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    account = Account.from_key(private_key)

    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    nonce = w3.eth.get_transaction_count(account.address, 'pending')
    gas_price = int(w3.eth.gas_price * 1.1)

    tx = Contract.constructor().build_transaction({
        'from': account.address,
        'nonce': nonce,
        'gas': 2000000,
        'gasPrice': gas_price,
        'chainId': CHAIN_ID
    })

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    log.info(f"Deploy TX: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt['status'] != 1:
        raise RuntimeError(f"Deploy failed: {receipt}")

    contract_address = receipt['contractAddress']
    log.info(f"HTLC3S deployed at: {contract_address}")

    return contract_address, tx_hash.hex()
