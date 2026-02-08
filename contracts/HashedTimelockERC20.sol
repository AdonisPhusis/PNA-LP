// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title Hashed Timelock Contract for ERC20 tokens (HTLC)
 * @notice Enables atomic swaps using hash time-locked contracts
 * @dev Used by P&A for trustless BTC <-> USDC swaps via M1 settlement
 */

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract HashedTimelockERC20 {

    // =========================================================================
    // EVENTS
    // =========================================================================

    event HTLCCreated(
        bytes32 indexed htlcId,
        address indexed sender,
        address indexed receiver,
        address token,
        uint256 amount,
        bytes32 hashlock,
        uint256 timelock
    );

    event HTLCWithdrawn(bytes32 indexed htlcId, bytes32 preimage);
    event HTLCRefunded(bytes32 indexed htlcId);

    // =========================================================================
    // STATE
    // =========================================================================

    struct HTLC {
        address sender;
        address receiver;
        address token;
        uint256 amount;
        bytes32 hashlock;    // SHA256 hash of the preimage
        uint256 timelock;    // Block timestamp after which refund is allowed
        bool withdrawn;
        bool refunded;
        bytes32 preimage;    // Stored when withdrawn (for LP to extract)
    }

    // htlcId => HTLC
    mapping(bytes32 => HTLC) public htlcs;

    // Counter for unique HTLC IDs
    uint256 private htlcCounter;

    // =========================================================================
    // MODIFIERS
    // =========================================================================

    modifier htlcExists(bytes32 htlcId) {
        require(htlcs[htlcId].sender != address(0), "HTLC does not exist");
        _;
    }

    modifier hashlockMatches(bytes32 htlcId, bytes32 preimage) {
        require(
            htlcs[htlcId].hashlock == sha256(abi.encodePacked(preimage)),
            "Invalid preimage"
        );
        _;
    }

    modifier withdrawable(bytes32 htlcId) {
        require(!htlcs[htlcId].withdrawn, "Already withdrawn");
        require(!htlcs[htlcId].refunded, "Already refunded");
        _;
    }

    modifier refundable(bytes32 htlcId) {
        require(!htlcs[htlcId].withdrawn, "Already withdrawn");
        require(!htlcs[htlcId].refunded, "Already refunded");
        require(block.timestamp >= htlcs[htlcId].timelock, "Timelock not expired");
        _;
    }

    // =========================================================================
    // MAIN FUNCTIONS
    // =========================================================================

    /**
     * @notice Create a new HTLC
     * @param receiver Address that can withdraw with the preimage
     * @param token ERC20 token address (e.g., USDC)
     * @param amount Amount of tokens to lock
     * @param hashlock SHA256 hash of the secret preimage
     * @param timelock Unix timestamp after which sender can refund
     * @return htlcId Unique identifier for this HTLC
     */
    function create(
        address receiver,
        address token,
        uint256 amount,
        bytes32 hashlock,
        uint256 timelock
    ) external returns (bytes32 htlcId) {
        require(receiver != address(0), "Invalid receiver");
        require(token != address(0), "Invalid token");
        require(amount > 0, "Amount must be > 0");
        require(timelock > block.timestamp, "Timelock must be in future");
        require(hashlock != bytes32(0), "Invalid hashlock");

        // Generate unique HTLC ID
        htlcId = keccak256(abi.encodePacked(
            msg.sender,
            receiver,
            token,
            amount,
            hashlock,
            timelock,
            htlcCounter++
        ));

        // Ensure no collision
        require(htlcs[htlcId].sender == address(0), "HTLC already exists");

        // Transfer tokens to this contract
        require(
            IERC20(token).transferFrom(msg.sender, address(this), amount),
            "Token transfer failed"
        );

        // Store HTLC
        htlcs[htlcId] = HTLC({
            sender: msg.sender,
            receiver: receiver,
            token: token,
            amount: amount,
            hashlock: hashlock,
            timelock: timelock,
            withdrawn: false,
            refunded: false,
            preimage: bytes32(0)
        });

        emit HTLCCreated(
            htlcId,
            msg.sender,
            receiver,
            token,
            amount,
            hashlock,
            timelock
        );

        return htlcId;
    }

    /**
     * @notice Withdraw tokens by providing the preimage
     * @dev Anyone can call this, but tokens go to the receiver
     * @param htlcId The HTLC identifier
     * @param preimage The secret that hashes to the hashlock
     */
    function withdraw(bytes32 htlcId, bytes32 preimage)
        external
        htlcExists(htlcId)
        hashlockMatches(htlcId, preimage)
        withdrawable(htlcId)
    {
        HTLC storage htlc = htlcs[htlcId];

        htlc.withdrawn = true;
        htlc.preimage = preimage;  // Store for extraction

        require(
            IERC20(htlc.token).transfer(htlc.receiver, htlc.amount),
            "Token transfer failed"
        );

        emit HTLCWithdrawn(htlcId, preimage);
    }

    /**
     * @notice Refund tokens after timelock expires
     * @param htlcId The HTLC identifier
     */
    function refund(bytes32 htlcId)
        external
        htlcExists(htlcId)
        refundable(htlcId)
    {
        HTLC storage htlc = htlcs[htlcId];

        htlc.refunded = true;

        require(
            IERC20(htlc.token).transfer(htlc.sender, htlc.amount),
            "Token transfer failed"
        );

        emit HTLCRefunded(htlcId);
    }

    // =========================================================================
    // VIEW FUNCTIONS
    // =========================================================================

    /**
     * @notice Get HTLC details
     */
    function getHTLC(bytes32 htlcId) external view returns (
        address sender,
        address receiver,
        address token,
        uint256 amount,
        bytes32 hashlock,
        uint256 timelock,
        bool withdrawn,
        bool refunded,
        bytes32 preimage
    ) {
        HTLC storage htlc = htlcs[htlcId];
        return (
            htlc.sender,
            htlc.receiver,
            htlc.token,
            htlc.amount,
            htlc.hashlock,
            htlc.timelock,
            htlc.withdrawn,
            htlc.refunded,
            htlc.preimage
        );
    }

    /**
     * @notice Check if HTLC can be withdrawn (preimage matches)
     */
    function canWithdraw(bytes32 htlcId, bytes32 preimage) external view returns (bool) {
        HTLC storage htlc = htlcs[htlcId];
        if (htlc.sender == address(0)) return false;
        if (htlc.withdrawn || htlc.refunded) return false;
        return htlc.hashlock == sha256(abi.encodePacked(preimage));
    }

    /**
     * @notice Check if HTLC can be refunded
     */
    function canRefund(bytes32 htlcId) external view returns (bool) {
        HTLC storage htlc = htlcs[htlcId];
        if (htlc.sender == address(0)) return false;
        if (htlc.withdrawn || htlc.refunded) return false;
        return block.timestamp >= htlc.timelock;
    }
}
