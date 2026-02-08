#!/usr/bin/env python3
"""
Deploy HTLC3S contract to Base Sepolia (fresh deployment)
"""
import json
import os
import sys

# Contract source - minimal HTLC3S
CONTRACT_SOURCE = '''
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

contract HTLC3S {
    struct HTLC {
        address sender;
        address recipient;
        address token;
        uint256 amount;
        bytes32 H_user;
        bytes32 H_lp1;
        bytes32 H_lp2;
        uint256 timelock;
        bool claimed;
        bool refunded;
    }

    mapping(bytes32 => HTLC) public htlcs;

    event HTLCCreated(
        bytes32 indexed htlcId,
        address indexed sender,
        address indexed recipient,
        address token,
        uint256 amount,
        bytes32 H_user,
        bytes32 H_lp1,
        bytes32 H_lp2,
        uint256 timelock
    );

    event HTLCClaimed(bytes32 indexed htlcId, bytes32 S_user, bytes32 S_lp1, bytes32 S_lp2);
    event HTLCRefunded(bytes32 indexed htlcId);

    function create(
        address recipient,
        address token,
        uint256 amount,
        bytes32 H_user,
        bytes32 H_lp1,
        bytes32 H_lp2,
        uint256 timelock
    ) external returns (bytes32 htlcId) {
        require(recipient != address(0), "Invalid recipient");
        require(token != address(0), "Invalid token");
        require(amount > 0, "Amount must be > 0");
        require(timelock > block.timestamp, "Timelock must be future");
        require(H_user != bytes32(0), "Invalid H_user");
        require(H_lp1 != bytes32(0), "Invalid H_lp1");
        require(H_lp2 != bytes32(0), "Invalid H_lp2");

        htlcId = keccak256(abi.encodePacked(
            msg.sender, recipient, token, amount,
            H_user, H_lp1, H_lp2, timelock, block.timestamp
        ));

        require(htlcs[htlcId].sender == address(0), "HTLC exists");

        require(IERC20(token).transferFrom(msg.sender, address(this), amount), "Transfer failed");

        htlcs[htlcId] = HTLC({
            sender: msg.sender,
            recipient: recipient,
            token: token,
            amount: amount,
            H_user: H_user,
            H_lp1: H_lp1,
            H_lp2: H_lp2,
            timelock: timelock,
            claimed: false,
            refunded: false
        });

        emit HTLCCreated(htlcId, msg.sender, recipient, token, amount, H_user, H_lp1, H_lp2, timelock);
        return htlcId;
    }

    function claim(bytes32 htlcId, bytes32 S_user, bytes32 S_lp1, bytes32 S_lp2) external {
        HTLC storage h = htlcs[htlcId];
        require(h.sender != address(0), "HTLC not found");
        require(!h.claimed, "Already claimed");
        require(!h.refunded, "Already refunded");
        require(sha256(abi.encodePacked(S_user)) == h.H_user, "Invalid S_user");
        require(sha256(abi.encodePacked(S_lp1)) == h.H_lp1, "Invalid S_lp1");
        require(sha256(abi.encodePacked(S_lp2)) == h.H_lp2, "Invalid S_lp2");

        h.claimed = true;
        require(IERC20(h.token).transfer(h.recipient, h.amount), "Transfer failed");

        emit HTLCClaimed(htlcId, S_user, S_lp1, S_lp2);
    }

    function refund(bytes32 htlcId) external {
        HTLC storage h = htlcs[htlcId];
        require(h.sender != address(0), "HTLC not found");
        require(!h.claimed, "Already claimed");
        require(!h.refunded, "Already refunded");
        require(block.timestamp >= h.timelock, "Not expired");

        h.refunded = true;
        require(IERC20(h.token).transfer(h.sender, h.amount), "Transfer failed");

        emit HTLCRefunded(htlcId);
    }

    function getHTLC(bytes32 htlcId) external view returns (
        address sender,
        address recipient,
        address token,
        uint256 amount,
        bytes32 H_user,
        bytes32 H_lp1,
        bytes32 H_lp2,
        uint256 timelock,
        bool claimed,
        bool refunded
    ) {
        HTLC storage h = htlcs[htlcId];
        return (h.sender, h.recipient, h.token, h.amount,
                h.H_user, h.H_lp1, h.H_lp2, h.timelock, h.claimed, h.refunded);
    }
}
'''

RPC_URL = 'https://sepolia.base.org'
CHAIN_ID = 84532

def main():
    from web3 import Web3
    import solcx

    # Install solc if needed
    try:
        solcx.set_solc_version('0.8.19')
    except:
        print('Installing solc 0.8.19...', file=sys.stderr)
        solcx.install_solc('0.8.19')
        solcx.set_solc_version('0.8.19')

    # Load Bob's key
    key_file = os.path.expanduser('~/.BathronKey/evm.json')
    with open(key_file) as f:
        keys = json.load(f)

    pk = keys.get('private_key') or keys.get('bob_private_key')
    if not pk:
        print(json.dumps({'error': 'No private key'}))
        return

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print(json.dumps({'error': 'Cannot connect to RPC'}))
        return

    account = w3.eth.account.from_key(pk)
    print(f'Deployer: {account.address}', file=sys.stderr)

    balance = w3.eth.get_balance(account.address)
    print(f'Balance: {w3.from_wei(balance, "ether")} ETH', file=sys.stderr)

    if balance < w3.to_wei(0.001, 'ether'):
        print(json.dumps({'error': 'Insufficient ETH'}))
        return

    # Compile
    print('Compiling...', file=sys.stderr)
    compiled = solcx.compile_source(
        CONTRACT_SOURCE,
        output_values=['abi', 'bin'],
        solc_version='0.8.19'
    )

    contract_id = '<stdin>:HTLC3S'
    contract_interface = compiled[contract_id]

    abi = contract_interface['abi']
    bytecode = contract_interface['bin']

    print(f'Bytecode size: {len(bytecode)//2} bytes', file=sys.stderr)

    # Verify expected selector is in bytecode
    expected_selector = w3.keccak(text='create(address,address,uint256,bytes32,bytes32,bytes32,uint256)')[:4].hex()
    print(f'Expected create selector: 0x{expected_selector}', file=sys.stderr)

    # Deploy
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.5)

    tx = Contract.constructor().build_transaction({
        'from': account.address,
        'nonce': nonce,
        'gas': 3000000,  # Increased for larger bytecode
        'gasPrice': gas_price,
        'chainId': CHAIN_ID
    })

    print('Deploying...', file=sys.stderr)
    signed = w3.eth.account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f'TX: {tx_hash.hex()}', file=sys.stderr)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status == 1:
        address = receipt.contractAddress
        print(f'Deployed: {address}', file=sys.stderr)

        # Verify selector in deployed bytecode
        code = w3.eth.get_code(address)
        if expected_selector in code.hex():
            print('Selector verified!', file=sys.stderr)
        else:
            print('WARNING: create selector not found in deployed bytecode!', file=sys.stderr)

        print(json.dumps({
            'address': address,
            'tx_hash': tx_hash.hex(),
            'deployer': account.address
        }))
    else:
        print(json.dumps({'error': 'Deployment failed'}))

if __name__ == '__main__':
    main()
