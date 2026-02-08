// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title HashedTimelockERC20_3S
 * @notice HTLC with 3 independent hashlocks for FlowSwap protocol.
 *
 * Key features:
 * - 3 hashlocks: H_user, H_lp1, H_lp2
 * - Claim requires ALL 3 preimages
 * - Claim is PERMISSIONLESS: anyone can call, but funds go to fixed recipient
 * - Refund only after timelock expires
 *
 * Security properties:
 * - Atomicity: Either all 3 secrets revealed or none
 * - MEV-safe: Front-running claim doesn't steal funds (recipient fixed)
 * - Trustless: No admin functions, no upgrades
 */
contract HashedTimelockERC20_3S is ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct HTLC {
        address sender;      // Who locked the funds
        address recipient;   // Who receives on claim (FIXED, cannot change)
        address token;       // ERC20 token address
        uint256 amount;      // Token amount
        bytes32 H_user;      // SHA256 hash of S_user
        bytes32 H_lp1;       // SHA256 hash of S_lp1
        bytes32 H_lp2;       // SHA256 hash of S_lp2
        uint256 timelock;    // Unix timestamp until refund allowed
        bool claimed;        // Has been claimed
        bool refunded;       // Has been refunded
    }

    // htlcId => HTLC
    mapping(bytes32 => HTLC) public htlcs;

    // Events
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

    event HTLCClaimed(
        bytes32 indexed htlcId,
        address indexed claimer,      // Who called claim (for gas attribution)
        address indexed recipient,    // Who received funds
        bytes32 S_user,
        bytes32 S_lp1,
        bytes32 S_lp2
    );

    event HTLCRefunded(
        bytes32 indexed htlcId,
        address indexed sender
    );

    /**
     * @notice Create a new HTLC with 3 hashlocks.
     * @dev Caller must have approved this contract to spend `amount` of `token`.
     *
     * @param recipient Address that will receive funds on successful claim
     * @param token ERC20 token contract address
     * @param amount Amount of tokens to lock
     * @param H_user SHA256 hash of user's secret
     * @param H_lp1 SHA256 hash of LP1's secret
     * @param H_lp2 SHA256 hash of LP2's secret
     * @param timelock Unix timestamp until which refund is blocked
     * @return htlcId Unique identifier for this HTLC
     */
    function create(
        address recipient,
        address token,
        uint256 amount,
        bytes32 H_user,
        bytes32 H_lp1,
        bytes32 H_lp2,
        uint256 timelock
    ) external nonReentrant returns (bytes32 htlcId) {
        require(recipient != address(0), "Invalid recipient");
        require(token != address(0), "Invalid token");
        require(amount > 0, "Amount must be > 0");
        require(timelock > block.timestamp, "Timelock must be future");
        require(H_user != bytes32(0), "Invalid H_user");
        require(H_lp1 != bytes32(0), "Invalid H_lp1");
        require(H_lp2 != bytes32(0), "Invalid H_lp2");

        // Generate unique ID from all parameters
        htlcId = keccak256(abi.encodePacked(
            msg.sender,
            recipient,
            token,
            amount,
            H_user,
            H_lp1,
            H_lp2,
            timelock,
            block.timestamp
        ));

        require(htlcs[htlcId].sender == address(0), "HTLC exists");

        // Transfer tokens to this contract
        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);

        // Store HTLC
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

        emit HTLCCreated(
            htlcId,
            msg.sender,
            recipient,
            token,
            amount,
            H_user,
            H_lp1,
            H_lp2,
            timelock
        );

        return htlcId;
    }

    /**
     * @notice Claim HTLC by providing all 3 preimages.
     * @dev PERMISSIONLESS: Anyone can call this function.
     *      Funds ALWAYS go to the recipient set at creation.
     *      This is intentional - allows watchers to claim on user's behalf.
     *
     * @param htlcId HTLC identifier
     * @param S_user Preimage such that SHA256(S_user) == H_user
     * @param S_lp1 Preimage such that SHA256(S_lp1) == H_lp1
     * @param S_lp2 Preimage such that SHA256(S_lp2) == H_lp2
     */
    function claim(
        bytes32 htlcId,
        bytes32 S_user,
        bytes32 S_lp1,
        bytes32 S_lp2
    ) external nonReentrant {
        HTLC storage h = htlcs[htlcId];

        require(h.sender != address(0), "HTLC not found");
        require(!h.claimed, "Already claimed");
        require(!h.refunded, "Already refunded");
        require(block.timestamp < h.timelock, "HTLC expired");

        // Verify ALL 3 preimages independently
        // Using sha256 (not keccak256) for cross-chain compatibility with Bitcoin
        require(sha256(abi.encodePacked(S_user)) == h.H_user, "Invalid S_user");
        require(sha256(abi.encodePacked(S_lp1)) == h.H_lp1, "Invalid S_lp1");
        require(sha256(abi.encodePacked(S_lp2)) == h.H_lp2, "Invalid S_lp2");

        h.claimed = true;

        // Transfer to FIXED recipient (not msg.sender!)
        IERC20(h.token).safeTransfer(h.recipient, h.amount);

        emit HTLCClaimed(htlcId, msg.sender, h.recipient, S_user, S_lp1, S_lp2);
    }

    /**
     * @notice Refund expired HTLC to sender.
     * @param htlcId HTLC identifier
     */
    function refund(bytes32 htlcId) external nonReentrant {
        HTLC storage h = htlcs[htlcId];

        require(h.sender != address(0), "HTLC not found");
        require(!h.claimed, "Already claimed");
        require(!h.refunded, "Already refunded");
        require(block.timestamp >= h.timelock, "Not expired yet");

        h.refunded = true;

        IERC20(h.token).safeTransfer(h.sender, h.amount);

        emit HTLCRefunded(htlcId, h.sender);
    }

    /**
     * @notice Check if claim would succeed with given preimages.
     * @dev Useful for pre-validation before spending gas.
     */
    function canClaim(
        bytes32 htlcId,
        bytes32 S_user,
        bytes32 S_lp1,
        bytes32 S_lp2
    ) external view returns (bool) {
        HTLC storage h = htlcs[htlcId];

        if (h.sender == address(0)) return false;
        if (h.claimed || h.refunded) return false;
        if (block.timestamp >= h.timelock) return false;

        return (
            sha256(abi.encodePacked(S_user)) == h.H_user &&
            sha256(abi.encodePacked(S_lp1)) == h.H_lp1 &&
            sha256(abi.encodePacked(S_lp2)) == h.H_lp2
        );
    }

    /**
     * @notice Check if refund is possible.
     */
    function canRefund(bytes32 htlcId) external view returns (bool) {
        HTLC storage h = htlcs[htlcId];

        if (h.sender == address(0)) return false;
        if (h.claimed || h.refunded) return false;

        return block.timestamp >= h.timelock;
    }

    /**
     * @notice Get HTLC details.
     */
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
        return (
            h.sender,
            h.recipient,
            h.token,
            h.amount,
            h.H_user,
            h.H_lp1,
            h.H_lp2,
            h.timelock,
            h.claimed,
            h.refunded
        );
    }
}
