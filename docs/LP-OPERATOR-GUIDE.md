# LP Operator Guide

Guide for running a P&A Liquidity Provider node on BATHRON testnet.

## Overview

An LP provides liquidity for cross-chain swaps (BTC <-> USDC) using M1 as an internal settlement rail. LPs earn the bid/ask spread on each swap.

**Architecture:**
```
User (BTC) ──HTLC──► LP ──M1──► LP ──HTLC──► User (USDC)
                     ↑                  ↑
                   LP_IN (BTC/M1)    LP_OUT (M1/USDC)
```

In single-LP mode, one LP handles both legs. In per-leg mode, two independent LPs each handle one leg.

## Requirements

- Ubuntu 22.04+ (or Docker)
- BATHRON node (`bathrond`) synced to testnet
- Bitcoin Core (Signet) synced
- Python 3.10+ with venv
- Port 8080 open (LP API)

## Installation

### Option A: Bare Metal

```bash
# 1. Clone the LP code
cd ~
git clone https://github.com/AdonisPhusis/PNA-LP.git pna-lp
cd pna-lp

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure keys (see Key Setup below)

# 4. Start
LP_ID=lp_pna_01 LP_NAME="My LP" python3 server.py
```

### Option B: Docker

```bash
docker pull ghcr.io/adonisphusis/pna-lp:latest

docker run -d \
  -p 8080:8080 \
  -v ~/.BathronKey:/root/.BathronKey:ro \
  -v ~/bathron/bin/bathron-cli:/usr/local/bin/bathron-cli:ro \
  -v ~/bitcoin/bin/bitcoin-cli:/usr/local/bin/bitcoin-cli:ro \
  -v ~/.bathron:/root/.bathron \
  -v ~/.bitcoin-signet:/root/.bitcoin-signet:ro \
  -e LP_ID=lp_pna_01 \
  -e LP_NAME="pna LP" \
  ghcr.io/adonisphusis/pna-lp:latest
```

## Key Setup

All keys are stored in `~/.BathronKey/` with permissions `700` (directory) and `600` (files).

### Required Files

**`~/.BathronKey/wallet.json`** — BATHRON (M1) wallet
```json
{
  "name": "alice",
  "role": "liquidity_provider",
  "address": "yJYD2bfYYBe6qAojSzMKX949H7QoQifNAo",
  "wif": "cTuaDJPC..."
}
```

**`~/.BathronKey/btc.json`** — BTC Signet wallet
```json
{
  "address": "tb1q...",
  "pubkey": "02...",
  "claim_wif": "cVAU..."
}
```
The `claim_wif` field is required for automatic BTC refunds. Without it, stuck swaps cannot be auto-recovered.

**`~/.BathronKey/evm.json`** — EVM wallet (Base Sepolia)
```json
{
  "address": "0x78F5...",
  "private_key": "0x..."
}
```

**`~/.BathronKey/htlc3s.json`** — HTLC3S contract config
```json
{
  "contract_address": "0x2493Eaaa...",
  "abi_path": "/path/to/HTLC3S.json"
}
```

### Permissions Check
```bash
ls -la ~/.BathronKey/
# drwx------ 2 ubuntu ubuntu 4096 ... .BathronKey
# -rw------- 1 ubuntu ubuntu  256 ... wallet.json
# -rw------- 1 ubuntu ubuntu  128 ... btc.json
# -rw------- 1 ubuntu ubuntu  128 ... evm.json
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LP_ID` | `lp_pna_01` | Unique LP identifier |
| `LP_NAME` | `pna LP` | Display name |
| `PORT` | `8080` | API port |
| `LP_FLOWSWAP_DB` | `~/.bathron/flowswap_db_{LP_ID}.json` | Swap database path |

## Managing Liquidity

### Lock M0 -> M1 (provide settlement liquidity)

```bash
# Check current state
bathron-cli -testnet getwalletstate true

# Lock 20000 sats M0 -> M1
bathron-cli -testnet lock 20000

# Verify M1 receipt created
bathron-cli -testnet getwalletstate true
```

### Check LP Inventory

```bash
curl http://localhost:8080/api/health | python3 -m json.tool
```

The `liquidity` section shows available BTC, M1, and USDC balances minus active reservations.

### Refresh Inventory

```bash
curl -X POST http://localhost:8080/api/lp/inventory/refresh
```

Inventory auto-refreshes every 60 seconds.

## Monitoring

### Health Check

```bash
curl http://localhost:8080/api/health
```

Returns:
```json
{
  "ok": true,
  "lp_id": "lp_pna_01",
  "checks": {
    "bathron": {"connected": true, "height": 11432},
    "btc_signet": {"connected": true, "height": 286100},
    "liquidity": {"btc": 0.01, "m1": 50000.0, "usdc": 100.0, "active_swaps": 0}
  }
}
```

### Dashboard

Open `http://<LP_IP>:8080/` in a browser for the visual dashboard.

### Logs

```bash
# If running directly
tail -f /tmp/pna-lp.log

# If using systemd
journalctl -u pna-lp -f

# Via deploy script
./contrib/testnet/deploy_pna_lp.sh logs
```

### Reputation

```bash
curl http://localhost:8080/api/reputation
```

Score = `100 * (completed_swaps / total_swaps)`. Below threshold triggers blacklisting by the frontend aggregator.

## Configuration

### Spreads

```bash
curl -X POST http://localhost:8080/api/lp/config \
  -H 'Content-Type: application/json' \
  -d '{"pairs": {"BTC/M1": {"spread_bid": 0.1, "spread_ask": 0.1}}}'
```

### Price Sources

```bash
# View current sources
curl http://localhost:8080/api/rates/sources

# Update
curl -X POST http://localhost:8080/api/rates/sources \
  -H 'Content-Type: application/json' \
  -d '{"sources": ["binance", "coingecko"]}'
```

## Troubleshooting

### Stuck Swaps

List swaps stuck for more than 1 hour:
```bash
curl http://localhost:8080/api/admin/stuck-swaps
```

Force-fail a stuck swap (localhost only):
```bash
curl -X POST http://localhost:8080/api/admin/swap/<swap_id>/force-fail
```

### BTC Refund Failed

If auto-refund fails with "signrawtransactionwithwallet failed":
1. Ensure `claim_wif` is set in `~/.BathronKey/btc.json`
2. The WIF must correspond to the LP's BTC refund pubkey
3. Check if the swap is marked `btc_refund_unrecoverable` — these require manual intervention

### Node Not Connected

```bash
# Check BATHRON node
bathron-cli -testnet getblockchaininfo

# Check BTC Signet
bitcoin-cli -signet getblockchaininfo

# Restart nodes if needed
sudo systemctl restart bathrond
sudo systemctl restart bitcoind-signet
```

### Low Liquidity

```bash
# Lock more M0 -> M1
bathron-cli -testnet lock 50000

# Fund BTC from Signet faucet
# https://signetfaucet.com

# Fund USDC from Circle faucet
# https://faucet.circle.com/ (select Base Sepolia)
```

## Systemd Service

Install the LP as a persistent service:

```bash
cd /path/to/BATHRON
sudo cp contrib/testnet/systemd/pna-lp.service /etc/systemd/system/
# Edit LP_ID and LP_NAME in the service file if needed
sudo systemctl daemon-reload
sudo systemctl enable pna-lp
sudo systemctl start pna-lp
```

Check status:
```bash
sudo systemctl status pna-lp
journalctl -u pna-lp -f
```
