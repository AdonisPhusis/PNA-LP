# P&A LP Architecture

## Current Structure

```
pna-lp/
├── server.py              # Main FastAPI app (~8900 lines, monolithic)
├── routes/
│   ├── __init__.py
│   └── prices.py          # Price feeds, proxy, rate sources (extracted)
├── sdk/
│   ├── core.py            # Shared types, constants, timelock validation
│   ├── chains/
│   │   ├── btc.py         # Bitcoin Signet RPC client
│   │   ├── m1.py          # BATHRON (M1) RPC client
│   │   └── evm.py         # EVM (Base) web3 client
│   ├── htlc/
│   │   ├── btc.py         # BTC single-secret HTLC
│   │   ├── btc_3s.py      # BTC 3-secret HTLC (FlowSwap)
│   │   ├── m1.py          # M1 single-secret HTLC
│   │   ├── m1_3s.py       # M1 3-secret HTLC (FlowSwap)
│   │   └── evm_3s.py      # EVM 3-secret HTLC (Solidity)
│   └── swap/
│       └── watcher_3s.py  # Swap watcher (monitors chain events)
├── contracts/
│   └── HTLC3S.sol         # Solidity HTLC3S contract
├── static/                # LP Dashboard (HTML/CSS/JS)
├── tests/
│   └── test_perleg_e2e.py # Per-leg E2E tests (20 tests)
├── docs/
│   ├── LP-OPERATOR-GUIDE.md
│   └── API-REFERENCE.md
├── Dockerfile
└── requirements.txt
```

## Planned Extraction (Future)

The monolithic `server.py` will be split into focused modules:

```
server.py (app creation, middleware, startup/shutdown, ~200 lines)
routes/
├── prices.py          ✅ DONE — price feeds, proxy
├── wallets.py         NEXT — wallet management, address labels
├── flowswap.py        — FlowSwap 3S endpoints (init, fund, claim)
├── admin.py           — Admin endpoints (localhost-only)
├── sdk_endpoints.py   — Low-level SDK access endpoints
└── legacy.py          — Legacy swap endpoints (deprecated)
services/
├── flowswap_store.py  — FlowSwap database, persistence, recovery
├── swap_engine.py     — Swap state machine transitions
├── watcher.py         — Chain event watchers (BTC, EVM, per-leg)
└── inventory.py       — Inventory reservation management
```

## Key Design Patterns

### Dependency Injection via Callbacks
Extracted modules receive callbacks to update shared state (like LP_CONFIG) rather than importing server.py globals directly. See `routes/prices.py::configure()`.

### FastAPI APIRouter
Each extracted module exports an `APIRouter` that server.py includes via `app.include_router()`. Routes keep the same paths — no URL changes for clients.

### Thread Safety
`flowswap_db` is protected by `_flowswap_lock` (threading.Lock). All access must be wrapped in `with _flowswap_lock:`. The lock lives in the store module.

### Admin Guard
Admin endpoints use `_require_local(request)` to restrict access to 127.0.0.1/::1. No auth tokens needed.

## 92 API Endpoints

See `docs/API-REFERENCE.md` for the full list organized by category.
