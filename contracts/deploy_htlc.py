#!/usr/bin/env python3
"""
Deploy HashedTimelockERC20 contract to Base Sepolia.

Usage:
    python deploy_htlc.py --private-key <KEY> [--rpc-url <URL>]

Or set environment variables:
    DEPLOYER_PRIVATE_KEY=0x...
    BASE_SEPOLIA_RPC=https://sepolia.base.org
"""

import argparse
import json
import os
import sys

try:
    from web3 import Web3
    from solcx import compile_standard, install_solc
except ImportError:
    print("Installing dependencies...")
    os.system("pip install web3 py-solc-x")
    from web3 import Web3
    from solcx import compile_standard, install_solc

# Base Sepolia config
BASE_SEPOLIA_RPC = "https://sepolia.base.org"
BASE_SEPOLIA_CHAIN_ID = 84532

# Contract source
CONTRACT_FILE = os.path.join(os.path.dirname(__file__), "HashedTimelockERC20.sol")


def compile_contract():
    """Compile the Solidity contract."""
    print("[1/4] Compiling contract...")

    # Install solc if needed
    try:
        install_solc("0.8.19")
    except Exception:
        pass  # Already installed

    with open(CONTRACT_FILE, "r") as f:
        source = f.read()

    compiled = compile_standard({
        "language": "Solidity",
        "sources": {
            "HashedTimelockERC20.sol": {"content": source}
        },
        "settings": {
            "outputSelection": {
                "*": {
                    "*": ["abi", "metadata", "evm.bytecode", "evm.sourceMap"]
                }
            },
            "optimizer": {
                "enabled": True,
                "runs": 200
            }
        }
    }, solc_version="0.8.19")

    contract_data = compiled["contracts"]["HashedTimelockERC20.sol"]["HashedTimelockERC20"]
    return contract_data["abi"], contract_data["evm"]["bytecode"]["object"]


def deploy_contract(private_key: str, rpc_url: str):
    """Deploy the contract."""

    # Compile
    abi, bytecode = compile_contract()

    # Connect to network
    print(f"[2/4] Connecting to {rpc_url}...")
    w3 = Web3(Web3.HTTPProvider(rpc_url))

    if not w3.is_connected():
        print("ERROR: Could not connect to RPC")
        sys.exit(1)

    # Get account from private key
    account = w3.eth.account.from_key(private_key)
    print(f"[3/4] Deploying from: {account.address}")

    # Check balance
    balance = w3.eth.get_balance(account.address)
    balance_eth = w3.from_wei(balance, 'ether')
    print(f"       Balance: {balance_eth} ETH")

    if balance == 0:
        print("ERROR: No ETH for gas. Get some from https://www.alchemy.com/faucets/base-sepolia")
        sys.exit(1)

    # Create contract
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    # Build transaction
    nonce = w3.eth.get_transaction_count(account.address)

    tx = Contract.constructor().build_transaction({
        'from': account.address,
        'nonce': nonce,
        'gas': 2000000,
        'gasPrice': w3.eth.gas_price,
        'chainId': BASE_SEPOLIA_CHAIN_ID,
    })

    # Sign and send
    print("[4/4] Deploying contract...")
    signed_tx = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

    print(f"       TX Hash: {tx_hash.hex()}")
    print("       Waiting for confirmation...")

    # Wait for receipt
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status == 1:
        contract_address = receipt.contractAddress
        print("")
        print("=" * 60)
        print("SUCCESS! Contract deployed!")
        print("=" * 60)
        print(f"  Address:  {contract_address}")
        print(f"  TX Hash:  {tx_hash.hex()}")
        print(f"  Block:    {receipt.blockNumber}")
        print(f"  Gas Used: {receipt.gasUsed}")
        print("")
        print(f"Explorer: https://sepolia.basescan.org/address/{contract_address}")
        print("")

        # Save deployment info
        deployment_info = {
            "network": "base-sepolia",
            "chainId": BASE_SEPOLIA_CHAIN_ID,
            "address": contract_address,
            "txHash": tx_hash.hex(),
            "blockNumber": receipt.blockNumber,
            "deployer": account.address,
            "abi": abi
        }

        output_file = os.path.join(os.path.dirname(__file__), "deployment.json")
        with open(output_file, "w") as f:
            json.dump(deployment_info, f, indent=2)
        print(f"Deployment info saved to: {output_file}")

        return contract_address
    else:
        print("ERROR: Deployment failed!")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Deploy HTLC contract to Base Sepolia")
    parser.add_argument("--private-key", "-k", help="Deployer private key (or set DEPLOYER_PRIVATE_KEY)")
    parser.add_argument("--rpc-url", "-r", default=BASE_SEPOLIA_RPC, help="RPC URL")
    args = parser.parse_args()

    # Get private key
    private_key = args.private_key or os.environ.get("DEPLOYER_PRIVATE_KEY")

    if not private_key:
        print("ERROR: Private key required")
        print("  Use --private-key <KEY> or set DEPLOYER_PRIVATE_KEY environment variable")
        sys.exit(1)

    # Ensure 0x prefix
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    deploy_contract(private_key, args.rpc_url)


if __name__ == "__main__":
    main()
