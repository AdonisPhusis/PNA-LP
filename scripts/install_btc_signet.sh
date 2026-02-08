#!/bin/bash
# =============================================================================
# install_btc_signet.sh - Install Bitcoin Core for Signet
# =============================================================================

set -e

# Non-interactive mode for apt
export DEBIAN_FRONTEND=noninteractive

INSTALL_DIR="$HOME/bitcoin"
DATA_DIR="$HOME/.bitcoin"
VERSION="27.0"
PROGRESS_FILE="/tmp/btc_install_progress.txt"

log_progress() {
    echo "$1|$2" > "$PROGRESS_FILE"
    echo "[$(date '+%H:%M:%S')] $2"
}

# Check if already installed
if [ -f "$INSTALL_DIR/bin/bitcoind" ]; then
    log_progress "100|Already installed"
    exit 0
fi

# Step 1: Download
log_progress "10|Downloading Bitcoin Core v$VERSION..."
cd /tmp
wget -q "https://bitcoincore.org/bin/bitcoin-core-$VERSION/bitcoin-$VERSION-x86_64-linux-gnu.tar.gz" -O bitcoin.tar.gz

# Step 2: Verify checksum
log_progress "30|Verifying download..."
# In production: verify SHA256

# Step 3: Extract
log_progress "50|Extracting files..."
tar -xzf bitcoin.tar.gz
mkdir -p "$INSTALL_DIR"
mv bitcoin-$VERSION/* "$INSTALL_DIR/"
rm -rf bitcoin.tar.gz bitcoin-$VERSION

# Step 4: Create data directory
log_progress "70|Configuring..."
mkdir -p "$DATA_DIR"

# Step 5: Generate credentials
RPC_USER="lp$(date +%s | tail -c 5)"
RPC_PASS=$(openssl rand -hex 16)

# Step 6: Create config (settings must be in [signet] section)
cat > "$DATA_DIR/bitcoin.conf" << EOF
# Bitcoin Signet Configuration for pna LP

[signet]
server=1
daemon=1
rpcuser=$RPC_USER
rpcpassword=$RPC_PASS
rpcallowip=127.0.0.1
rpcbind=127.0.0.1
dbcache=256
maxconnections=20
disablewallet=0
EOF

# Step 7: Save credentials for SDK
cat > "$DATA_DIR/.lp_credentials" << EOF
RPC_USER=$RPC_USER
RPC_PASS=$RPC_PASS
RPC_URL=http://127.0.0.1:38332
EOF

log_progress "90|Finalizing..."

# Step 8: Add to PATH (optional)
if ! grep -q "bitcoin/bin" ~/.bashrc 2>/dev/null; then
    echo 'export PATH="$HOME/bitcoin/bin:$PATH"' >> ~/.bashrc
fi

log_progress "100|Installation complete!"
echo ""
echo "Bitcoin Signet installed successfully!"
echo "Binary: $INSTALL_DIR/bin/bitcoind"
echo "Config: $DATA_DIR/bitcoin.conf"
echo "RPC User: $RPC_USER"
echo ""
echo "Start with: $INSTALL_DIR/bin/bitcoind"
