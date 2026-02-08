#!/bin/bash
# =============================================================================
# install_bathron.sh - Install BATHRON Node for M1
# =============================================================================

set -e

# Non-interactive mode for apt
export DEBIAN_FRONTEND=noninteractive

INSTALL_DIR="$HOME/bathron"
DATA_DIR="$HOME/.bathron"
PROGRESS_FILE="/tmp/m1_install_progress.txt"

# GitHub release URL (placeholder - update with real release)
RELEASE_URL="https://github.com/AdonisPhusis/BATHRON/releases/download/testnet-v0.1"

log_progress() {
    echo "$1|$2" > "$PROGRESS_FILE"
    echo "[$(date '+%H:%M:%S')] $2"
}

# Check if already installed
if [ -f "$INSTALL_DIR/bin/bathrond" ]; then
    log_progress "100|Already installed"
    exit 0
fi

# Step 1: Install dependencies
log_progress "10|Installing dependencies..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libboost-all-dev libevent-dev libssl-dev libsodium-dev libzmq3-dev

# Step 2: Get binaries
log_progress "30|Getting BATHRON binaries..."
mkdir -p "$INSTALL_DIR/bin"

# Option 1: Copy from Core+SDK server (fastest for testnet)
CORE_SDK_IP="162.19.251.75"
log_progress "40|Copying binaries from Core+SDK..."

# Try to copy pre-compiled binaries (requires SSH key)
if scp -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    ubuntu@$CORE_SDK_IP:~/BATHRON/src/bathrond \
    ubuntu@$CORE_SDK_IP:~/BATHRON/src/bathron-cli \
    "$INSTALL_DIR/bin/" 2>/dev/null; then
    log_progress "60|Binaries copied successfully"
    chmod +x "$INSTALL_DIR/bin/"*
else
    # Option 2: Download from release
    log_progress "40|Downloading from release..."
    cd /tmp
    wget -q "$RELEASE_URL/bathrond" -O bathrond 2>/dev/null && \
    wget -q "$RELEASE_URL/bathron-cli" -O bathron-cli 2>/dev/null && {
        chmod +x bathrond bathron-cli
        mv bathrond bathron-cli "$INSTALL_DIR/bin/"
        log_progress "60|Downloaded from release"
    } || {
        log_progress "100|Error: Cannot get BATHRON binaries"
        exit 1
    }
fi

# Step 3: Create data directory
log_progress "70|Configuring..."
mkdir -p "$DATA_DIR"

# Step 4: Generate credentials
RPC_USER="lp$(date +%s | tail -c 5)"
RPC_PASS=$(openssl rand -hex 16)

# Step 5: Create config
cat > "$DATA_DIR/bathron.conf" << EOF
# BATHRON Testnet Configuration for pna LP
testnet=1
server=1
daemon=1

# RPC Configuration
rpcuser=$RPC_USER
rpcpassword=$RPC_PASS
rpcallowip=127.0.0.1
rpcbind=127.0.0.1

# Network
addnode=57.131.33.151:27171
addnode=162.19.251.75:27171

# Performance
dbcache=256
maxconnections=20
EOF

# Step 6: Save credentials for SDK
cat > "$DATA_DIR/.lp_credentials" << EOF
RPC_USER=$RPC_USER
RPC_PASS=$RPC_PASS
RPC_URL=http://127.0.0.1:27172
EOF

log_progress "90|Finalizing..."

# Step 7: Add to PATH
if ! grep -q "bathron/bin" ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/bathron/bin:$PATH"' >> ~/.bashrc
fi

# Cleanup
rm -rf /tmp/bathron-download

log_progress "100|Installation complete!"
echo ""
echo "BATHRON installed successfully!"
echo "Binary: $INSTALL_DIR/bin/bathrond"
echo "Config: $DATA_DIR/bathron.conf"
echo "RPC User: $RPC_USER"
echo ""
echo "Start with: $INSTALL_DIR/bin/bathrond -testnet"
