# P&A LP API Reference

Base URL: `http://<LP_IP>:8080`

Interactive docs: `http://<LP_IP>:8080/docs` (Swagger UI)

## Health & Status

### GET /api/health
Deep health check — verifies node connectivity and liquidity.

```bash
curl http://LP:8080/api/health
```

Response:
```json
{
  "ok": true,
  "lp_id": "lp_pna_01",
  "timestamp": 1707600000,
  "checks": {
    "bathron": {"connected": true, "height": 11432, "headers": 11432, "chain": "test"},
    "btc_signet": {"connected": true, "height": 286100},
    "liquidity": {"btc": 0.01, "m1": 50000.0, "usdc": 100.0, "active_swaps": 0}
  }
}
```

### GET /api/status
LP status summary with swap counts and reputation.

```bash
curl http://LP:8080/api/status
```

### GET /api/reputation
LP reputation score (0-100) based on swap completion rate.

---

## Quotes

### GET /api/quote
Get a swap quote. The LP calculates rate from live BTC/USDC price + spread.

**Query params:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `from` | string | yes | Source asset: `BTC`, `USDC`, `M1` |
| `to` | string | yes | Target asset: `BTC`, `USDC`, `M1` |
| `amount` | float | yes | Amount in source asset units |

```bash
curl "http://LP:8080/api/quote?from=BTC&to=USDC&amount=0.001"
```

Response:
```json
{
  "lp_id": "lp_pna_01",
  "lp_name": "pna LP",
  "from_asset": "BTC",
  "to_asset": "USDC",
  "from_amount": 0.001,
  "to_amount": 67.29,
  "rate": 67290.06,
  "spread_pct": 0.1,
  "route": "BTC -> M1 -> USDC",
  "expires_at": 1707601800,
  "confirmations_required": 0
}
```

### GET /api/quote/leg
Get a per-leg quote (single leg of a multi-LP swap).

**Query params:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `from` | string | yes | `BTC` or `M1` |
| `to` | string | yes | `M1` or `USDC` |
| `amount` | float | yes | Amount |
| `role` | string | yes | `LP_IN` or `LP_OUT` |

---

## FlowSwap (3-Secret Protocol)

### POST /api/flowswap/init
Initialize a new FlowSwap. Returns a **plan** (no on-chain commitment yet).

**Body:**
```json
{
  "from_asset": "BTC",
  "to_asset": "USDC",
  "from_amount_sats": 100000,
  "user_H": "abc123...64hex",
  "user_btc_refund_address": "tb1q...",
  "user_evm_address": "0x..."
}
```

Response includes `swap_id`, `btc_htlc_address`, `btc_amount_sats`, `btc_redeem_script`, `timelock`.

### POST /api/flowswap/init-leg
Initialize a per-leg FlowSwap (for multi-LP routing).

**Body:**
```json
{
  "from_asset": "BTC",
  "to_asset": "M1",
  "from_amount_sats": 100000,
  "user_H": "abc123...64hex",
  "lp1_H": "def456...64hex",
  "lp2_H": "789abc...64hex",
  "user_btc_refund_address": "tb1q...",
  "routing_mode": "per_leg",
  "lp_out_url": "http://LP2:8080",
  "lp_out_m1_address": "y7XR..."
}
```

### GET /api/flowswap/{swap_id}
Get current swap status.

```bash
curl http://LP:8080/api/flowswap/fs_abc123
```

Response:
```json
{
  "swap_id": "fs_abc123",
  "state": "awaiting_btc",
  "direction": "forward",
  "from_asset": "BTC",
  "to_asset": "USDC",
  "from_amount": 100000,
  "to_amount": 6729,
  "btc_htlc_address": "tb1q...",
  "created_at": 1707600000,
  "routing_mode": "per_leg"
}
```

### POST /api/flowswap/{swap_id}/btc-funded
Notify LP that user has funded the BTC HTLC. LP verifies on-chain and locks M1.

**Body:**
```json
{
  "funding_txid": "abc123..."
}
```

### POST /api/flowswap/{swap_id}/m1-locked
(Per-leg) Notify LP_OUT that LP_IN has locked M1 HTLC.

**Body:**
```json
{
  "m1_htlc_outpoint": "txid:vout",
  "m1_amount_sats": 99800,
  "m1_htlc_expiry": 11552
}
```

### POST /api/flowswap/{swap_id}/btc-claimed
(Per-leg) Notify LP_IN that LP_OUT has claimed BTC (secrets revealed on-chain).

**Body:**
```json
{
  "claim_txid": "def456...",
  "S_user": "secret1...64hex",
  "S_lp1": "secret2...64hex",
  "S_lp2": "secret3...64hex"
}
```

### GET /api/flowswap/list
List all FlowSwaps.

**Query params:**
| Param | Type | Description |
|-------|------|-------------|
| `state` | string | Filter by state |
| `limit` | int | Max results (default 50) |

---

## Swap Flow (BTC -> USDC, Forward)

```
1. GET  /api/quote?from=BTC&to=USDC&amount=0.001
   → Get rate and HTLC parameters

2. POST /api/flowswap/init
   → Get swap_id + btc_htlc_address
   → State: awaiting_btc

3. User sends BTC to btc_htlc_address

4. POST /api/flowswap/{id}/btc-funded
   → LP verifies on-chain, locks M1 + USDC HTLCs
   → State: btc_funded → lp_locked

5. LP claims BTC with 3 preimages
   → State: btc_claimed

6. LP delivers USDC to user
   → State: completing → completed
```

## Swap Flow (USDC -> BTC, Reverse)

```
1. POST /api/flowswap/init (from=USDC, to=BTC)
   → Get swap_id + USDC HTLC parameters
   → State: awaiting_usdc

2. User locks USDC in EVM HTLC

3. POST /api/flowswap/{id}/usdc-funded
   → LP verifies on-chain, locks M1 + creates BTC output
   → State: usdc_funded → lp_locked

4. LP claims USDC
   → State: completing → completed
```

---

## Admin Endpoints (localhost only)

### GET /api/admin/stuck-swaps
List swaps stuck in non-terminal states for >1 hour.

### POST /api/admin/swap/{swap_id}/force-fail
Force a stuck swap to FAILED state and release inventory.

### POST /api/admin/cleanup-terminal
Archive terminal swaps older than `max_age_hours` (default 24).

```bash
curl -X POST "http://localhost:8080/api/admin/cleanup-terminal?max_age_hours=12"
```

---

## Error Codes

| HTTP Code | Meaning |
|-----------|---------|
| 400 | Bad request (invalid params, wrong state) |
| 403 | Forbidden (admin endpoint from non-localhost) |
| 404 | Swap/resource not found |
| 409 | Conflict (swap already in target state) |
| 500 | Internal error (node connection, signing failure) |
| 503 | Service unavailable (node not connected) |

---

## SDK Endpoints

Low-level SDK access for direct HTLC operations:

| Endpoint | Description |
|----------|-------------|
| `POST /api/sdk/htlc/generate` | Generate secret + hashlock |
| `GET /api/sdk/htlc/verify` | Verify preimage matches hashlock |
| `POST /api/sdk/htlc/m1/create` | Create M1 HTLC |
| `POST /api/sdk/htlc/m1/claim` | Claim M1 HTLC |
| `POST /api/sdk/btc/htlc/create` | Create BTC 3S HTLC |
| `POST /api/sdk/btc/htlc/claim` | Claim BTC HTLC |
| `POST /api/sdk/usdc/htlc/create` | Create USDC HTLC (EVM) |
| `POST /api/sdk/usdc/htlc/withdraw` | Withdraw USDC HTLC |
| `GET /api/sdk/m1/balance` | Get M1 balance |
| `GET /api/sdk/m1/receipts` | List M1 receipts |
| `POST /api/sdk/m1/lock` | Lock M0 -> M1 |
