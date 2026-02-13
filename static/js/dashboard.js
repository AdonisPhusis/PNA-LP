/**
 * LP Dashboard - Frontend JavaScript
 */

// =============================================================================
// CONFIG
// =============================================================================

const API_URL = window.location.origin;
let refreshInterval = null;

// M1 unit preference: 'sat' or 'btc'
let m1Unit = localStorage.getItem('m1Unit') || 'sat';

// Format M1 amount based on current unit preference
function formatM1(sats) {
    if (m1Unit === 'btc') {
        return (sats / 100000000).toFixed(8) + ' M1';
    } else {
        return sats.toLocaleString() + ' M1';
    }
}

// Toggle M1 unit between SAT and BTC
function toggleM1Unit() {
    m1Unit = m1Unit === 'sat' ? 'btc' : 'sat';
    localStorage.setItem('m1Unit', m1Unit);
    updateUnitToggleUI();
    loadSwaps(); // Refresh to show new format
}

// Update toggle UI to reflect current state
function updateUnitToggleUI() {
    const toggle = document.getElementById('unit-toggle');
    const labelSat = document.getElementById('unit-label');
    const labelBtc = document.getElementById('unit-label-alt');

    if (m1Unit === 'btc') {
        toggle?.classList.add('btc');
        labelSat?.classList.remove('active');
        labelBtc?.classList.add('active');
    } else {
        toggle?.classList.remove('btc');
        labelSat?.classList.add('active');
        labelBtc?.classList.remove('active');
    }
}

// =============================================================================
// NAVIGATION
// =============================================================================

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const page = item.dataset.page;
        showPage(page);
    });
});

function showPage(pageId) {
    // Update nav
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelector(`.nav-item[data-page="${pageId}"]`)?.classList.add('active');

    // Update pages
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${pageId}`)?.classList.add('active');

    // Load page data
    if (pageId === 'overview') loadOverview();
    if (pageId === 'config') loadConfigPage();
    if (pageId === 'pricing') { refreshSettlementDesk(); }
    if (pageId === 'swaps') loadSwaps();
}

// =============================================================================
// API CALLS
// =============================================================================

async function apiCall(endpoint, options = {}) {
    try {
        const response = await fetch(`${API_URL}${endpoint}`, {
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers,
            },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (e) {
        console.error(`API Error: ${endpoint}`, e);
        return null;
    }
}

async function checkApiStatus() {
    const data = await apiCall('/api/status');
    const dot = document.getElementById('api-status');
    const text = document.getElementById('api-status-text');

    if (data && data.status === 'ok') {
        dot.classList.add('connected');
        dot.classList.remove('disconnected');
        text.textContent = 'Connected';
        return true;
    } else {
        dot.classList.remove('connected');
        dot.classList.add('disconnected');
        text.textContent = 'Disconnected';
        return false;
    }
}

// =============================================================================
// LP IDENTITY
// =============================================================================

async function loadLPIdentity() {
    const info = await apiCall('/api/lp/info');
    if (!info) return;

    const nameInput = document.getElementById('lp-display-name');
    const idDisplay = document.getElementById('lp-id-display');
    const sidebarName = document.getElementById('sidebar-lp-name');

    if (nameInput) nameInput.value = info.name || '';
    if (idDisplay) idDisplay.value = info.lp_id || '';
    if (sidebarName) sidebarName.textContent = info.name || info.lp_id || '';
}

// =============================================================================
// OVERVIEW PAGE
// =============================================================================

async function loadOverview() {
    const status = await apiCall('/api/status');
    if (!status) return;

    document.getElementById('stat-active-swaps').textContent = status.swaps_active || 0;
    document.getElementById('stat-total-swaps').textContent = status.swaps_total || 0;
    document.getElementById('stat-volume').textContent = '$0'; // TODO: Calculate from swaps
    document.getElementById('stat-earnings').textContent = '$0'; // TODO: Calculate from fees

    document.getElementById('last-update-time').textContent = new Date().toLocaleTimeString();

    // Show test mode badge if all spreads are 0
    const testBadge = document.getElementById('test-mode-badge');
    if (testBadge) testBadge.style.display = status.test_mode ? 'inline' : 'none';

    // Chain status
    await renderChainStatus();

    // Recent swaps
    await loadRecentSwaps();
}

// Chain metadata for display (icon, color, name)
const CHAIN_META = {
    btc:   { name: 'Bitcoin',       icon: '\u20bf', color: '#f7931a' },
    m1:    { name: 'M1 (BATHRON)',  icon: 'M',      color: '#3b82f6' },
    usdc:  { name: 'USDC (Base)',   icon: '$',      color: '#2775ca' },
    pivx:  { name: 'PIVX',         icon: 'P',      color: '#6b3fa0' },
    dash:  { name: 'Dash',         icon: 'D',      color: '#008ce7' },
    zcash: { name: 'Zcash',        icon: 'Z',      color: '#f4b728' },
};

async function renderChainStatus() {
    const grid = document.getElementById('chain-status-grid');

    // Fetch real chain status from server (includes all chains)
    const data = await apiCall('/api/chains/status');
    const chainInfo = data?.chains || {};

    // Overview: only show RUNNING chains + usdc (installed = always shown)
    const chainsToShow = [];
    for (const [id, status] of Object.entries(chainInfo)) {
        if (id === 'usdc' || status.running) {
            chainsToShow.push(id);
        }
    }

    // Fetch sync status for all installed non-USDC chains in parallel
    const syncPromises = {};
    for (const id of chainsToShow) {
        if (id !== 'usdc' && chainInfo[id]?.installed) {
            syncPromises[id] = apiCall(`/api/chain/${id}/sync`);
        }
    }
    const syncResults = {};
    for (const [id, promise] of Object.entries(syncPromises)) {
        syncResults[id] = await promise;
    }

    grid.innerHTML = chainsToShow.map(id => {
        const meta = CHAIN_META[id] || { name: id, icon: '?', color: '#888' };
        const status = chainInfo[id] || {};
        const sync = syncResults[id];
        let heightText = '-';
        let statusClass = 'disconnected';

        if (id === 'usdc') {
            const isOk = status.installed;
            heightText = isOk ? 'RPC OK' : 'Not configured';
            statusClass = isOk ? 'connected' : 'disconnected';
        } else if (sync && !sync.error) {
            heightText = sync.blocks?.toLocaleString() || '-';
            if (sync.syncing) {
                heightText += ` (${sync.progress?.toFixed(0)}%)`;
                statusClass = 'syncing';
            } else {
                statusClass = 'connected';
            }
        } else {
            heightText = status.installed ? 'Stopped' : 'Not installed';
            statusClass = 'disconnected';
        }

        return `
        <div class="chain-status-item">
            <span class="chain-icon" style="color: ${meta.color}">${meta.icon}</span>
            <div class="chain-info">
                <div class="chain-name">${meta.name}</div>
                <div class="chain-height">${id === 'usdc' ? '' : 'Block: '}${heightText}</div>
            </div>
            <span class="status-dot ${statusClass}"></span>
        </div>
    `}).join('');
}

async function loadRecentSwaps() {
    const data = await apiCall('/api/swaps?limit=5');
    const tbody = document.getElementById('recent-swaps-table');

    if (!data || !data.swaps || data.swaps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">No swaps yet</td></tr>';
        return;
    }

    tbody.innerHTML = data.swaps.map(swap => {
        const statusClass = getStatusClass(swap.status);
        const direction = swap.direction || `${swap.from_asset} \u2192 ${swap.to_asset}`;
        const fromDisp = swap.from_display || `${swap.from_amount} ${swap.from_asset}`;
        const toDisp = swap.to_display || `${swap.to_amount} ${swap.to_asset}`;
        return `
        <tr onclick="viewSwap('${swap.swap_id}')" style="cursor:pointer">
            <td><code>${swap.swap_id.slice(0, 16)}...</code></td>
            <td>${direction}</td>
            <td><strong>${fromDisp}</strong> &rarr; <strong>${toDisp}</strong></td>
            <td><span class="status-badge ${statusClass}">${swap.status}</span></td>
            <td>${formatTime(swap.created_at)}</td>
        </tr>
    `}).join('');
}

// =============================================================================
// SWAPS PAGE
// =============================================================================

async function loadSwaps() {
    const filter = document.getElementById('swap-filter').value;
    const endpoint = filter ? `/api/swaps?status=${filter}` : '/api/swaps';
    const data = await apiCall(endpoint);
    const tbody = document.getElementById('swaps-table');

    if (!data || !data.swaps || data.swaps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">No swaps</td></tr>';
        return;
    }

    // Update volume/earnings stats
    let totalVolumeBtc = 0;
    let totalPnlUsdc = 0;
    let totalPnlM1 = 0;
    data.swaps.forEach(s => {
        if (s.status === 'claimed') {
            totalVolumeBtc += s.from_amount || 0;
            totalPnlUsdc += s.lp_pnl_usdc || 0;
            totalPnlM1 += s.lp_pnl_m1 || 0;
        }
    });
    const volEl = document.getElementById('stat-volume');
    const earnEl = document.getElementById('stat-earnings');
    if (volEl) volEl.textContent = totalVolumeBtc.toFixed(8) + ' BTC';
    if (earnEl) earnEl.textContent = formatM1(totalPnlM1) + ' ($' + totalPnlUsdc.toFixed(2) + ')';

    tbody.innerHTML = data.swaps.map(swap => {
        const typeIcon = swap.type === 'flowswap_3s' ? '3S' :
                         swap.type === 'atomic' ? 'AT' : 'SW';
        const fromDisp = swap.from_display || `${(swap.from_amount || 0).toFixed(8)} ${swap.from_asset}`;
        const toDisp = swap.to_display || `${swap.to_amount} ${swap.to_asset}`;
        const rateDisp = swap.rate_display || '-';
        const pnlUsdc = swap.lp_pnl_usdc || 0;
        const pnlM1 = swap.lp_pnl_m1 || 0;
        const pnlClass = pnlUsdc >= 0 ? 'gain' : 'loss';
        const pnlSign = pnlUsdc >= 0 ? '+' : '';
        const duration = swap.duration_seconds
            ? formatDuration(swap.duration_seconds)
            : '-';

        // 2-leg display
        const leg1 = swap.legs?.leg1_btc_to_m1;
        const leg2 = swap.legs?.leg2_m1_to_usdc;
        const legsHtml = leg1 && leg2
            ? `<div class="legs-mini"><span class="leg-tag">L1</span> ${leg1.from} &rarr; ${leg1.to}<br><span class="leg-tag">L2</span> ${leg2.from} &rarr; ${leg2.to}</div>`
            : `<div><strong>${fromDisp}</strong> &rarr; <strong>${toDisp}</strong></div>`;

        const statusClass = getStatusClass(swap.status);

        return `
        <tr onclick="viewSwap('${swap.swap_id}')" style="cursor:pointer" title="Click for details">
            <td>
                <span class="type-badge">${typeIcon}</span>
                <code>${swap.swap_id.slice(0, 16)}...</code>
            </td>
            <td class="amount-cell">
                ${legsHtml}
            </td>
            <td class="rate-cell" title="${rateDisp}">
                <small>${rateDisp}</small>
            </td>
            <td class="amount-cell ${pnlClass}">
                <strong>${pnlSign}${formatM1(Math.abs(pnlM1))}</strong>
                <div><small>${pnlSign}$${Math.abs(pnlUsdc).toFixed(2)}</small></div>
            </td>
            <td><span class="status-badge ${statusClass}">${swap.status}</span></td>
            <td>
                <div>${formatTime(swap.created_at)}</div>
                <small class="duration">${duration}</small>
            </td>
        </tr>
    `}).join('');
}

function getStatusClass(status) {
    if (status === 'claimed' || status === 'completed') return 'success';
    if (status === 'htlc_created' || status === 'pending') return 'pending';
    if (status === 'claiming') return 'info';
    if (status === 'expired' || status === 'refunded') return 'error';
    return 'info';
}

function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '-';
    if (seconds < 60) return seconds + 's';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return m + 'm ' + s + 's';
    const h = Math.floor(m / 60);
    return h + 'h ' + (m % 60) + 'm';
}

function refreshSwaps() {
    loadSwaps();
}

// Store last loaded swaps for detail view
let _lastSwapsData = [];

async function viewSwap(swapId) {
    // Try flowswap detail endpoint first
    let detail = await apiCall(`/api/flowswap/${swapId}`);
    if (!detail) {
        detail = await apiCall(`/api/swap/${swapId}`);
    }
    if (!detail) {
        alert('Swap not found: ' + swapId);
        return;
    }

    // Build detail view
    const isFlowSwap = !!detail.state;
    const signetBase = 'https://mempool.space/signet/tx/';
    const baseBase = 'https://sepolia.basescan.org/tx/';

    let html = `<div class="swap-detail-modal">`;
    html += `<h3>Swap ${swapId}</h3>`;
    html += `<table class="detail-table">`;

    // Basic info
    html += detailRow('Type', isFlowSwap ? 'FlowSwap 3S' : 'Standard');
    html += detailRow('State', detail.state || detail.status);
    html += detailRow('Direction', `${detail.from_asset || 'BTC'} &rarr; ${detail.to_asset || 'USDC'}`);

    // 2-leg breakdown (LP internal view)
    if (detail.legs) {
        const l1 = detail.legs.leg1_btc_to_m1;
        const l2 = detail.legs.leg2_m1_to_usdc;
        html += `<tr><td colspan="2"><strong>--- Leg 1: BTC &rarr; M1 ---</strong></td></tr>`;
        html += detailRow('From', l1.from);
        html += detailRow('To', l1.to);
        html += detailRow('Rate', l1.rate);
        html += `<tr><td colspan="2"><strong>--- Leg 2: M1 &rarr; USDC ---</strong></td></tr>`;
        html += detailRow('From', l2.from);
        html += detailRow('To', l2.to);
        html += detailRow('Rate', l2.rate);
    } else {
        // Fallback: simple amounts
        if (detail.btc_amount_sats) {
            html += detailRow('BTC Amount', (detail.btc_amount_sats / 1e8).toFixed(8) + ' BTC (' + detail.btc_amount_sats + ' sats)');
        }
        if (detail.usdc_amount) {
            html += detailRow('USDC Amount', detail.usdc_amount.toFixed(2) + ' USDC');
        }
    }

    // Rate & PnL
    html += `<tr><td colspan="2"><strong>--- Rate &amp; PnL ---</strong></td></tr>`;
    html += detailRow('Effective Rate', detail.rate_display || '-');
    if (detail.spread_applied !== undefined) {
        html += detailRow('Spread Applied', detail.spread_applied + '%');
    }
    if (detail.lp_pnl) {
        html += detailRow('LP PnL (M1)', formatM1(detail.lp_pnl.m1_sats || 0));
        html += detailRow('LP PnL (USDC)', '$' + (detail.lp_pnl.usdc || 0).toFixed(4));
    }

    // Hashlocks
    if (detail.hashlocks) {
        html += `<tr><td colspan="2"><strong>--- Hashlocks ---</strong></td></tr>`;
        html += detailRow('H_user', `<code>${detail.hashlocks.H_user?.slice(0,16) || '-'}...</code>`);
        html += detailRow('H_lp1', `<code>${detail.hashlocks.H_lp1?.slice(0,16) || '-'}...</code>`);
        html += detailRow('H_lp2', `<code>${detail.hashlocks.H_lp2?.slice(0,16) || '-'}...</code>`);
    }

    // BTC leg TX
    if (detail.btc) {
        html += `<tr><td colspan="2"><strong>--- BTC Transactions ---</strong></td></tr>`;
        if (detail.btc.htlc_address) html += detailRow('HTLC Address', `<code>${detail.btc.htlc_address}</code>`);
        if (detail.btc.fund_txid) html += detailRow('Fund TX', txLink(detail.btc.fund_txid, signetBase));
        if (detail.btc.claim_txid) html += detailRow('Claim TX', txLink(detail.btc.claim_txid, signetBase));
    }

    // M1 leg TX
    if (detail.m1) {
        html += `<tr><td colspan="2"><strong>--- M1 Transactions ---</strong></td></tr>`;
        if (detail.m1.txid) html += detailRow('HTLC TX', `<code>${detail.m1.txid.slice(0,24)}...</code>`);
        if (detail.m1.claim_txid) html += detailRow('Claim TX', `<code>${detail.m1.claim_txid.slice(0,24)}...</code>`);
    }

    // EVM leg TX
    if (detail.evm) {
        html += `<tr><td colspan="2"><strong>--- USDC (EVM) Transactions ---</strong></td></tr>`;
        if (detail.evm.lock_txhash) html += detailRow('Lock TX', txLink(detail.evm.lock_txhash, baseBase));
        if (detail.evm.claim_txhash) html += detailRow('Claim TX', txLink(detail.evm.claim_txhash, baseBase));
    }

    // Secrets (if revealed)
    if (detail.secrets) {
        html += `<tr><td colspan="2"><strong>--- Secrets (revealed) ---</strong></td></tr>`;
        html += detailRow('S_lp1', `<code>${detail.secrets.S_lp1?.slice(0,24) || '-'}...</code>`);
        html += detailRow('S_lp2', `<code>${detail.secrets.S_lp2?.slice(0,24) || '-'}...</code>`);
    }

    // User
    if (detail.user_usdc_address) {
        html += detailRow('User USDC Addr', `<code>${detail.user_usdc_address}</code>`);
    }

    // Timing
    html += `<tr><td colspan="2"><strong>--- Timing ---</strong></td></tr>`;
    html += detailRow('Created', detail.created_at ? formatTime(detail.created_at) : '-');
    html += detailRow('Completed', detail.completed_at ? formatTime(detail.completed_at) : '-');
    if (detail.completed_at && detail.created_at) {
        html += detailRow('Duration', formatDuration(detail.completed_at - detail.created_at));
    }

    html += `</table>`;
    html += `<button class="btn" onclick="this.parentElement.remove()" style="margin-top:12px">Close</button>`;
    html += `</div>`;

    // Remove existing modal if any
    document.querySelector('.swap-detail-modal')?.remove();

    // Add modal overlay
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = html;
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
}

function detailRow(label, value) {
    return `<tr><td class="detail-label">${label}</td><td>${value}</td></tr>`;
}

function txLink(hash, baseUrl) {
    if (!hash) return '-';
    return `<a href="${baseUrl}${hash}" target="_blank" rel="noopener"><code>${hash.slice(0,20)}...</code></a>`;
}

document.getElementById('swap-filter').addEventListener('change', loadSwaps);

// =============================================================================
// LP CONFIG PAGE (Identity + Dynamic Wallets)
// =============================================================================

// All known chains (order for display)
const ALL_CHAINS = ['m1', 'btc', 'usdc', 'pivx', 'dash', 'zcash'];

// Wallet unit labels per chain
const WALLET_UNITS = { btc: 'BTC', m1: 'M1', usdc: 'USDC', pivx: 'PIVX', dash: 'DASH', zcash: 'ZEC' };

async function loadConfigPage() {
    await loadLPIdentity();
    await renderConfigWallets();
}

async function renderConfigWallets() {
    const grid = document.getElementById('config-wallets-grid');
    if (!grid) return;

    // Fetch chain status + wallet data in parallel
    const [chainData, walletData] = await Promise.all([
        apiCall('/api/chains/status'),
        apiCall('/api/wallets'),
    ]);

    const chains = chainData?.chains || {};
    const wallets = walletData || {};

    // Fetch sync status for installed non-USDC chains in parallel
    const syncResults = {};
    const syncPromises = ALL_CHAINS
        .filter(id => id !== 'usdc' && chains[id]?.installed)
        .map(async id => { syncResults[id] = await apiCall(`/api/chain/${id}/sync`); });
    await Promise.all(syncPromises);

    // Split into installed vs not-installed
    const installed = [];
    const notInstalled = [];
    for (const id of ALL_CHAINS) {
        const st = chains[id] || {};
        if (id === 'usdc' || st.installed || st.running) {
            installed.push(id);
        } else {
            notInstalled.push(id);
        }
    }

    // Render installed wallets (full cards)
    let html = installed.map(id => {
        const meta = CHAIN_META[id] || { name: id, icon: '?', color: '#888' };
        const st = chains[id] || {};
        const w = wallets[id] || {};
        const sync = syncResults[id];
        const unit = WALLET_UNITS[id] || id.toUpperCase();

        // Balance
        let balText = '-';
        if (id === 'usdc') {
            balText = (w.balance != null ? w.balance.toFixed(2) : '0.00');
        } else if (id === 'm1') {
            balText = (w.balance != null ? w.balance.toLocaleString() : '0');
        } else if (w.balance != null) {
            balText = w.balance.toFixed(8);
        }
        if (w.pending && w.pending > 0) {
            balText += id === 'm1'
                ? ` (+${w.pending.toLocaleString()} pending)`
                : ` (+${w.pending.toFixed(8)} pending)`;
        }

        // Address
        const addr = w.address || (st.running ? 'Loading...' : '-');

        // Status
        let statusDot = 'disconnected';
        let statusText = 'Stopped';
        if (id === 'usdc') {
            statusDot = st.installed ? 'connected' : 'disconnected';
            statusText = st.installed ? 'RPC OK' : 'Not configured';
        } else if (sync && !sync.error) {
            if (sync.syncing) {
                statusDot = 'syncing';
                statusText = `Syncing ${sync.progress?.toFixed(0)}% (Block ${sync.blocks?.toLocaleString()})`;
            } else {
                statusDot = 'connected';
                statusText = `Synced (Block ${sync.blocks?.toLocaleString()})`;
            }
        }

        // ETH gas
        const ethGasHtml = (id === 'usdc' && w.eth_balance != null)
            ? `<div class="wallet-balance-secondary">(${w.eth_balance.toFixed(6)} ETH for gas)</div>`
            : '';

        // Actions
        let actionsHtml = '';
        if (id !== 'usdc') {
            actionsHtml = st.running
                ? `<button class="btn btn-sm btn-secondary" onclick="stopChain('${id}')">Stop</button>`
                : `<button class="btn btn-sm btn-secondary" onclick="startChain('${id}')">Start</button>`;
            if (id !== 'm1') {
                actionsHtml += `<button class="btn btn-sm btn-danger" onclick="uninstallChain('${id}')">Uninstall</button>`;
            }
        }

        return `
        <div class="wallet-card" data-chain="${id}">
            <div class="wallet-header">
                <span class="wallet-icon" style="color: ${meta.color}">${meta.icon}</span>
                <h3>${meta.name}</h3>
                <span class="wallet-status-badge ${statusDot}">${statusText}</span>
            </div>
            <div class="wallet-balance">
                <span class="balance-value">${balText}</span>
                <span class="balance-unit">${unit}</span>
            </div>
            ${ethGasHtml}
            <div class="wallet-address-row">
                <code class="address-fixed">${addr}</code>
                <button class="btn btn-sm btn-icon" onclick="copyWalletAddress('${id}', this)" title="Copy address">&#128203;</button>
            </div>
            <div class="wallet-actions-row">${actionsHtml}</div>
        </div>`;
    }).join('');

    // Render not-installed chains (compact row)
    if (notInstalled.length > 0) {
        html += `<div class="wallet-not-installed-section">
            <h4>Not Installed</h4>
            <div class="wallet-not-installed-list">
                ${notInstalled.map(id => {
                    const meta = CHAIN_META[id] || { name: id, icon: '?', color: '#888' };
                    return `<div class="wallet-not-installed-item">
                        <span class="wallet-icon-sm" style="color: ${meta.color}">${meta.icon}</span>
                        <span>${meta.name}</span>
                        <button class="btn btn-sm btn-primary" onclick="installChain('${id}')">Install</button>
                    </div>`;
                }).join('')}
            </div>
        </div>`;
    }

    grid.innerHTML = html;
}

function copyWalletAddress(chainId, btn) {
    const card = btn.closest('.wallet-card');
    const addr = card?.querySelector('.address-fixed')?.textContent;
    if (addr && addr !== 'Not available' && addr !== 'Loading...') {
        navigator.clipboard.writeText(addr);
        const orig = btn.innerHTML;
        btn.innerHTML = '&#10003;';
        setTimeout(() => { btn.innerHTML = orig; }, 1200);
    }
}

async function refreshConfigWallet(chainId) {
    await renderConfigWallets();
}

async function debugWallets() {
    const debugInfo = await apiCall('/api/wallets/debug');
    if (!debugInfo) {
        alert('Failed to get debug info');
        return;
    }

    // Format debug info nicely
    let msg = '=== WALLET DEBUG INFO ===\n\n';

    // BTC
    msg += '--- BTC ---\n';
    msg += `Cached address: ${debugInfo.cached_addresses?.btc || 'None'}\n`;
    const btc = debugInfo.btc_details;
    if (btc) {
        msg += `Labeled addresses: ${btc.address_count || 0}\n`;
        msg += `Total confirmed: ${btc.total_confirmed || 0} BTC\n`;
        msg += `Total pending: ${btc.total_pending || 0} BTC\n`;
        if (btc.error) msg += `Error: ${btc.error}\n`;
    }
    msg += '\n';

    // M1
    msg += '--- M1 ---\n';
    msg += `Cached address: ${debugInfo.cached_addresses?.m1 || 'None'}\n`;
    const m1 = debugInfo.m1_details;
    if (m1) {
        msg += `Labeled addresses: ${m1.address_count || 0}\n`;
        msg += `Total confirmed: ${m1.total_confirmed || 0} M1\n`;
        msg += `Total pending: ${m1.total_pending || 0} M1\n`;
        if (m1.error) msg += `Error: ${m1.error}\n`;
    }
    msg += '\n';

    // USDC
    msg += '--- USDC (Base Sepolia) ---\n';
    msg += `Address: ${debugInfo.cached_addresses?.usdc || 'None'}\n`;
    const usdc = debugInfo.usdc_details;
    if (usdc) {
        msg += `Contract: ${usdc.contract}\n`;
        msg += `USDC balance: ${usdc.usdc_balance || 0} USDC\n`;
        msg += `ETH balance: ${usdc.eth_balance || 0} ETH (gas)\n`;
        if (usdc.error) msg += `Error: ${usdc.error}\n`;
    }

    alert(msg);
    console.log('Wallet debug info:', debugInfo);
}

// Legacy wallet helpers (used by debug)
function copyAddress(chain) {
    const card = document.querySelector(`.wallet-card[data-chain="${chain}"]`);
    const addr = card?.querySelector('.address-fixed')?.textContent;
    if (addr && addr !== 'Not available' && addr !== 'Loading...') {
        navigator.clipboard.writeText(addr);
    }
}

// =============================================================================
// CHAIN CONFIG
// =============================================================================

async function testChainConnection(chain) {
    const result = await apiCall(`/api/chain/${chain}/test`, { method: 'POST' });
    if (result && result.connected) {
        alert(`${chain.toUpperCase()} connected! Block: ${result.height || '-'}`);
    } else {
        alert(`${chain.toUpperCase()} connection failed`);
    }
    renderConfigWallets();
}

async function installChain(chain) {
    const meta = CHAIN_META[chain] || { name: chain };
    if (!confirm(`Install ${meta.name}? This will download and configure the daemon.`)) return;
    const result = await apiCall(`/api/chain/${chain}/install`, { method: 'POST' });
    if (result && result.job_id) {
        alert(`${meta.name} installation started. This may take a few minutes.`);
    } else {
        alert(`Install failed: ${result?.error || 'Unknown error'}`);
    }
    setTimeout(() => renderConfigWallets(), 5000);
}

async function uninstallChain(chain) {
    const meta = CHAIN_META[chain] || { name: chain };
    if (!confirm(`Uninstall ${meta.name}? This will stop the daemon. Data is kept.`)) return;
    await apiCall(`/api/chain/${chain}/uninstall`, { method: 'POST' });
    setTimeout(() => renderConfigWallets(), 2000);
}

async function startChain(chain) {
    const result = await apiCall(`/api/chain/${chain}/start`, { method: 'POST' });
    // Refresh wallet cards after a short delay for daemon startup
    setTimeout(() => renderConfigWallets(), 3000);
}

// pollSyncStatus removed — sync status shown inline in wallet cards

async function stopChain(chain) {
    await apiCall(`/api/chain/${chain}/stop`, { method: 'POST' });
    // Refresh wallet cards after stop
    setTimeout(() => renderConfigWallets(), 2000);
}

// deployHTLCContract removed — deploy via CLI

// saveChainConfig / loadChainConfig removed — chains are auto-detected

// =============================================================================
// SETTLEMENT DESK - Dynamic Per-Pair Pricing
// =============================================================================

const BTC_M1_FIXED_RATE = 100000000; // 1 BTC = 100M M1 (sats)

// Pair metadata — defines all tradable pairs through M1
const PAIR_META = {
    "BTC/M1":  { from: "btc",   to: "m1", fixed: true, fixedRate: 100000000, fixedLabel: "1 SAT = 1 M1", unit: "BTC" },
    "USDC/M1": { from: "usdc",  to: "m1", fixed: false, unit: "USDC", defaultPresets: ["binance_btcusdc"] },
    "PIVX/M1": { from: "pivx",  to: "m1", fixed: false, unit: "PIVX", defaultPresets: ["coingecko_pivx"] },
    "DASH/M1": { from: "dash",  to: "m1", fixed: false, unit: "DASH", defaultPresets: ["coingecko_dash"] },
    "ZEC/M1":  { from: "zcash", to: "m1", fixed: false, unit: "ZEC",  defaultPresets: ["coingecko_zec"] },
};

// Ordered pair display list
const PAIR_ORDER = ["BTC/M1", "USDC/M1", "PIVX/M1", "DASH/M1", "ZEC/M1"];

// Source presets for all chains
const SOURCE_PRESETS = {
    binance_btcusdc: {
        name: 'Binance BTCUSDC', icon: 'B',
        url: 'https://api.binance.com/api/v3/ticker/price', symbol: 'BTCUSDC',
        jsonPath: 'price', weight: 100, invert: true, scaleFactor: 100000000,
    },
    binance_usdcusdt: {
        name: 'Binance USDCUSDT', icon: 'B',
        url: 'https://api.binance.com/api/v3/ticker/price', symbol: 'USDCUSDT',
        jsonPath: 'price', weight: 50, invert: false, scaleFactor: 1,
    },
    coingecko: {
        name: 'CoinGecko USDC', icon: 'CG',
        url: 'https://api.coingecko.com/api/v3/simple/price?ids=usd-coin&vs_currencies=usd',
        symbol: '', jsonPath: 'usd-coin.usd', weight: 50, invert: false, scaleFactor: 1,
    },
    kraken: {
        name: 'Kraken USDC', icon: 'K',
        url: 'https://api.kraken.com/0/public/Ticker?pair=USDCUSD',
        symbol: '', jsonPath: 'result.USDCUSD.c.0', weight: 0, invert: false, scaleFactor: 1,
    },
    coingecko_pivx: {
        name: 'CoinGecko PIVX', icon: 'CG',
        url: 'https://api.coingecko.com/api/v3/simple/price?ids=pivx,dash,zcash&vs_currencies=usd',
        symbol: '', jsonPath: 'pivx.usd', weight: 100, invert: true, scaleFactor: 100000000,
    },
    coingecko_dash: {
        name: 'CoinGecko DASH', icon: 'CG',
        url: 'https://api.coingecko.com/api/v3/simple/price?ids=pivx,dash,zcash&vs_currencies=usd',
        symbol: '', jsonPath: 'dash.usd', weight: 100, invert: true, scaleFactor: 100000000,
    },
    coingecko_zec: {
        name: 'CoinGecko ZEC', icon: 'CG',
        url: 'https://api.coingecko.com/api/v3/simple/price?ids=pivx,dash,zcash&vs_currencies=usd',
        symbol: '', jsonPath: 'zcash.usd', weight: 100, invert: true, scaleFactor: 100000000,
    },
    custom: {
        name: 'Custom', icon: '?',
        url: '', symbol: '', jsonPath: 'price', weight: 0, invert: false, scaleFactor: 1,
    },
};

// Per-pair source arrays + state
let pairSources = {};       // { "USDC/M1": [...], "PIVX/M1": [...] }
let sourceIdCounter = 0;
let priceRefreshInterval = null;
let refreshIntervalSeconds = 10;
let expandedPair = null;    // Currently expanded pair ID
let _cachedLpConfig = null; // Cached from /api/lp/config

function generateSourceId() { return `src_${++sourceIdCounter}`; }

// =============================================================================
// SETTLEMENT BOOK RENDERER
// =============================================================================

function formatRate(pairId, rate) {
    const meta = PAIR_META[pairId];
    if (!meta) return String(rate);
    if (meta.fixed) return '1:1 (Fixed)';
    if (rate >= 1e9) return (rate / 1e9).toFixed(1) + 'B M1/' + meta.unit;
    if (rate >= 1e6) return (rate / 1e6).toFixed(1) + 'M M1/' + meta.unit;
    if (rate >= 1000) return Math.round(rate).toLocaleString() + ' M1/' + meta.unit;
    return rate.toFixed(2) + ' M1/' + meta.unit;
}

let _cachedChainStatus = {}; // { btc: {installed:true,...}, ... }

async function refreshSettlementDesk() {
    try {
        const [config, chainData] = await Promise.all([
            apiCall('/api/lp/config'),
            apiCall('/api/chains/status'),
        ]);
        if (!config) return;
        _cachedLpConfig = config;
        _cachedChainStatus = chainData?.chains || {};
        renderSettlementBook(config);

        // Fetch live prices for non-fixed pairs (only if chain installed)
        for (const pairId of PAIR_ORDER) {
            const meta = PAIR_META[pairId];
            if (!meta || meta.fixed) continue;
            const chainId = meta.from === 'zcash' ? 'zcash' : meta.from;
            if (!_cachedChainStatus[chainId]?.installed) continue;
            if (pairSources[pairId]?.length > 0) {
                const rate = await fetchPairPrices(pairId);
                if (rate && config.pairs?.[pairId]) {
                    config.pairs[pairId]._liveRate = rate;
                }
            }
        }
        renderSettlementBook(config);
    } catch (e) {
        console.error('[Settlement] Refresh failed:', e);
    }
}

function renderSettlementBook(config) {
    const body = document.getElementById('settlement-body');
    if (!body || !config?.pairs) return;

    let html = '';
    for (const pairId of PAIR_ORDER) {
        const pair = config.pairs[pairId];
        if (!pair) continue;
        const meta = PAIR_META[pairId];
        if (!meta) continue;
        const chainMeta = CHAIN_META[meta.from] || { icon: '?', color: '#888', name: meta.from };
        const slug = pairId.replace('/', '-').toLowerCase();

        // Check if the "from" chain wallet is installed
        const chainId = meta.from === 'zcash' ? 'zcash' : meta.from;
        const chainInstalled = _cachedChainStatus[chainId]?.installed ?? false;

        if (!chainInstalled) {
            // Greyed-out row — not clickable, not configurable
            html += `
            <tr class="pair-row disabled">
                <td class="col-arrow"></td>
                <td>
                    <span class="pair-chain-icon" style="color:${chainMeta.color};opacity:0.4">${chainMeta.icon}</span>
                    <span style="opacity:0.4">${pairId}</span>
                    <span class="pair-tag not-installed">Not installed</span>
                </td>
                <td class="col-rate" style="opacity:0.3">—</td>
                <td class="col-spread" style="opacity:0.3">—</td>
                <td></td>
            </tr>`;
            continue;
        }

        const isExpanded = expandedPair === pairId;
        const rate = meta.fixed ? meta.fixedRate : (pair._liveRate || pair.rate || 0);
        const spreadBid = pair.spread_bid ?? 0.5;
        const spreadAsk = pair.spread_ask ?? 0.5;

        html += `
        <tr class="pair-row ${isExpanded ? 'expanded' : ''}" onclick="togglePairDetail('${pairId}')">
            <td class="col-arrow"><span class="expand-arrow">${isExpanded ? '&#9662;' : '&#9656;'}</span></td>
            <td>
                <span class="pair-chain-icon" style="color:${chainMeta.color}">${chainMeta.icon}</span>
                ${pairId}
                ${meta.fixed ? '<span class="pair-tag fixed">Fixed</span>' : '<span class="pair-tag market">Market</span>'}
            </td>
            <td class="col-rate ${meta.fixed ? 'rate-fixed' : 'rate-market'}">${formatRate(pairId, rate)}</td>
            <td class="col-spread">${spreadBid.toFixed(1)}% / ${spreadAsk.toFixed(1)}%</td>
            <td>
                <label class="toggle" onclick="event.stopPropagation()">
                    <input type="checkbox" id="pair-${slug}-enabled" ${pair.enabled !== false ? 'checked' : ''}
                        onchange="event.stopPropagation()">
                    <span class="toggle-slider"></span>
                </label>
            </td>
        </tr>`;

        // Expanded detail panel
        if (isExpanded) {
            html += `<tr class="pair-detail-row"><td colspan="5">
                <div class="pair-detail-panel">${renderPairDetail(pairId, pair, meta)}</div>
            </td></tr>`;
        }
    }

    body.innerHTML = html || '<tr><td colspan="5" class="empty">No pairs configured</td></tr>';
}

function togglePairDetail(pairId) {
    expandedPair = (expandedPair === pairId) ? null : pairId;
    if (_cachedLpConfig) renderSettlementBook(_cachedLpConfig);
}

function renderPairDetail(pairId, pair, meta) {
    const slug = pairId.replace('/', '-').toLowerCase();
    const spreadBid = pair.spread_bid ?? 0.5;
    const spreadAsk = pair.spread_ask ?? 0.5;
    const minVal = pair.min ?? 0;
    const maxVal = pair.max ?? 0;

    let html = '';

    // Fixed pair: show protocol info
    if (meta.fixed) {
        html += `
        <div class="pair-detail-section">
            <div class="fixed-rate-box">Protocol Fixed: ${meta.fixedLabel}</div>
        </div>`;
    } else {
        // Market pair: show price sources
        html += `
        <div class="pair-detail-section">
            <h4>Price Sources</h4>
            <div class="sources-list" id="sources-${slug}">
                ${renderPairSources(pairId)}
            </div>
            <div class="add-source-row">
                <select onchange="if(this.value){addPairSource('${pairId}',this.value);this.value=''}">
                    <option value="">+ Add source...</option>
                    ${getPresetsForPair(pairId).map(k =>
                        `<option value="${k}">${SOURCE_PRESETS[k].name}</option>`
                    ).join('')}
                    <option value="custom">Custom URL</option>
                </select>
            </div>
        </div>`;
    }

    // Spread + Limits
    html += `
    <div class="pair-detail-section">
        <h4>Spread & Limits</h4>
        <div class="detail-fields">
            <div class="detail-field">
                <label>Bid Spread</label>
                <div class="inline-input">
                    <input type="number" id="pair-${slug}-spread-bid" value="${spreadBid}" min="0" max="10" step="0.1"
                        onchange="onPairFieldChange('${pairId}')">
                    <span>%</span>
                </div>
            </div>
            <div class="detail-field">
                <label>Ask Spread</label>
                <div class="inline-input">
                    <input type="number" id="pair-${slug}-spread-ask" value="${spreadAsk}" min="0" max="10" step="0.1"
                        onchange="onPairFieldChange('${pairId}')">
                    <span>%</span>
                </div>
            </div>
            <div class="detail-field">
                <label>Min Swap</label>
                <div class="inline-input">
                    <input type="number" id="pair-${slug}-min" value="${minVal}" step="0.0001">
                    <span>${meta.unit}</span>
                </div>
            </div>
            <div class="detail-field">
                <label>Max Inventory</label>
                <div class="inline-input">
                    <input type="number" id="pair-${slug}-max" value="${maxVal}" step="0.1">
                    <span>${meta.unit}</span>
                </div>
            </div>
        </div>
    </div>`;

    return html;
}

function onPairFieldChange(pairId) {
    // Update the cached config so the book row shows new spread
    if (!_cachedLpConfig?.pairs?.[pairId]) return;
    const slug = pairId.replace('/', '-').toLowerCase();
    const bid = parseFloat(document.getElementById(`pair-${slug}-spread-bid`)?.value) || 0;
    const ask = parseFloat(document.getElementById(`pair-${slug}-spread-ask`)?.value) || 0;
    _cachedLpConfig.pairs[pairId].spread_bid = bid;
    _cachedLpConfig.pairs[pairId].spread_ask = ask;
    // Re-render just the book row spread display
    renderSettlementBook(_cachedLpConfig);
}

function getPresetsForPair(pairId) {
    // Return relevant presets based on pair
    if (pairId === 'USDC/M1') return ['binance_btcusdc', 'binance_usdcusdt', 'coingecko', 'kraken'];
    if (pairId === 'PIVX/M1') return ['coingecko_pivx'];
    if (pairId === 'DASH/M1') return ['coingecko_dash'];
    if (pairId === 'ZEC/M1') return ['coingecko_zec'];
    return [];
}

// =============================================================================
// PER-PAIR SOURCE MANAGEMENT
// =============================================================================

function renderPairSources(pairId) {
    const sources = pairSources[pairId] || [];
    if (sources.length === 0) return '<div class="no-sources">No sources configured</div>';

    return sources.map(s => `
        <div class="source-item ${s.enabled ? 'enabled' : ''} ${s.expanded ? 'expanded' : ''}">
            <div class="source-item-header" onclick="togglePairSourceExpanded('${pairId}','${s.id}')">
                <input type="checkbox" class="source-toggle" ${s.enabled ? 'checked' : ''}
                    onclick="event.stopPropagation(); togglePairSource('${pairId}','${s.id}')">
                <span class="source-icon-badge">${s.icon}</span>
                <span class="source-name">${s.name}</span>
                <span class="source-weight-badge">${s.weight}%</span>
                <span class="source-status-badge ${s.status}">${s.status === 'live' && s.lastPrice ? Math.round(s.lastPrice).toLocaleString() + ' M1' : s.status}</span>
                <button class="source-delete-btn" onclick="event.stopPropagation(); removePairSource('${pairId}','${s.id}')" title="Remove">×</button>
            </div>
            <div class="source-item-body">
                <div class="source-field">
                    <label>URL</label>
                    <input type="text" value="${s.url}" onchange="updatePairSourceField('${pairId}','${s.id}','url',this.value)">
                </div>
                <div class="source-field-row">
                    <div class="source-field">
                        <label>Weight</label>
                        <input type="number" value="${s.weight}" min="0" max="100"
                            onchange="updatePairSourceField('${pairId}','${s.id}','weight',this.value)">
                    </div>
                    <div class="source-field">
                        <label>JSON Path</label>
                        <input type="text" value="${s.jsonPath}" onchange="updatePairSourceField('${pairId}','${s.id}','jsonPath',this.value)">
                    </div>
                </div>
                <div class="source-field-row">
                    <div class="source-field">
                        <label>Invert (1/x)</label>
                        <label class="checkbox-label" style="margin-top:4px">
                            <input type="checkbox" ${s.invert ? 'checked' : ''}
                                onchange="updatePairSourceField('${pairId}','${s.id}','invert',this.checked)"> Yes
                        </label>
                    </div>
                    <div class="source-field">
                        <label>Scale Factor</label>
                        <input type="number" value="${s.scaleFactor || 1}"
                            onchange="updatePairSourceField('${pairId}','${s.id}','scaleFactor',this.value)">
                    </div>
                </div>
            </div>
        </div>
    `).join('');
}

function addPairSource(pairId, presetKey) {
    const preset = SOURCE_PRESETS[presetKey];
    if (!preset) return;
    if (!pairSources[pairId]) pairSources[pairId] = [];
    pairSources[pairId].push({
        id: generateSourceId(), type: presetKey,
        name: preset.name, icon: preset.icon, url: preset.url,
        symbol: preset.symbol, jsonPath: preset.jsonPath,
        weight: preset.weight, invert: preset.invert || false,
        scaleFactor: preset.scaleFactor || 1,
        enabled: true, lastPrice: null, lastUpdate: null, status: 'off', expanded: presetKey === 'custom',
    });
    savePairSourcesAll();
    if (_cachedLpConfig) renderSettlementBook(_cachedLpConfig);
}

function removePairSource(pairId, srcId) {
    if (!pairSources[pairId]) return;
    pairSources[pairId] = pairSources[pairId].filter(s => s.id !== srcId);
    savePairSourcesAll();
    if (_cachedLpConfig) renderSettlementBook(_cachedLpConfig);
}

function togglePairSource(pairId, srcId) {
    const s = (pairSources[pairId] || []).find(s => s.id === srcId);
    if (s) { s.enabled = !s.enabled; savePairSourcesAll(); }
    if (_cachedLpConfig) renderSettlementBook(_cachedLpConfig);
}

function togglePairSourceExpanded(pairId, srcId) {
    const s = (pairSources[pairId] || []).find(s => s.id === srcId);
    if (s) s.expanded = !s.expanded;
    if (_cachedLpConfig) renderSettlementBook(_cachedLpConfig);
}

function updatePairSourceField(pairId, srcId, field, value) {
    const s = (pairSources[pairId] || []).find(s => s.id === srcId);
    if (s) {
        s[field] = (field === 'weight' || field === 'scaleFactor') ? parseFloat(value) || 0 : value;
        savePairSourcesAll();
    }
    if (_cachedLpConfig) renderSettlementBook(_cachedLpConfig);
}

// =============================================================================
// PRICE FETCHING (per-pair)
// =============================================================================

async function fetchSourcePrice(source) {
    if (!source.enabled || !source.url) { source.status = 'off'; return null; }
    try {
        let url = source.url;
        if (source.symbol && source.type.startsWith('binance')) url += `?symbol=${source.symbol}`;
        const response = await fetch(`/api/proxy/price?url=${encodeURIComponent(url)}&path=${encodeURIComponent(source.jsonPath)}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (data.price !== undefined) {
            let price = parseFloat(data.price);
            if (source.invert && price > 0) price = 1 / price;
            const sf = parseFloat(source.scaleFactor) || 1;
            if (sf !== 1) price *= sf;
            source.lastPrice = price;
            source.lastUpdate = Date.now();
            source.status = 'live';
            return price;
        }
        throw new Error('No price');
    } catch (e) {
        console.error(`[Source] ${source.name}:`, e.message);
        source.status = 'error';
        return null;
    }
}

async function fetchPairPrices(pairId) {
    const sources = (pairSources[pairId] || []).filter(s => s.enabled && s.url);
    if (sources.length === 0) return null;
    await Promise.all(sources.map(fetchSourcePrice));
    let totalWeight = 0, weightedSum = 0;
    sources.forEach(s => {
        if (s.lastPrice && s.weight > 0) { totalWeight += s.weight; weightedSum += s.lastPrice * s.weight; }
    });
    return totalWeight > 0 ? weightedSum / totalWeight : null;
}

// =============================================================================
// SOURCES PERSISTENCE (localStorage)
// =============================================================================

function savePairSourcesAll() {
    const config = {};
    for (const [pairId, sources] of Object.entries(pairSources)) {
        config[pairId] = sources.map(s => ({
            type: s.type, name: s.name, icon: s.icon, url: s.url,
            symbol: s.symbol, jsonPath: s.jsonPath, weight: s.weight,
            invert: s.invert || false, scaleFactor: s.scaleFactor || 1, enabled: s.enabled,
        }));
    }
    localStorage.setItem('pairSourcesConfig', JSON.stringify(config));
}

function loadPairSourcesAll() {
    // Migrate old single-pair format
    const oldSaved = localStorage.getItem('usdcM1Sources');
    const newSaved = localStorage.getItem('pairSourcesConfig');

    if (newSaved) {
        try {
            const config = JSON.parse(newSaved);
            for (const [pairId, sources] of Object.entries(config)) {
                pairSources[pairId] = sources.map(s => ({
                    ...s, id: generateSourceId(),
                    invert: s.invert || false, scaleFactor: s.scaleFactor || 1,
                    lastPrice: null, lastUpdate: null, status: 'off', expanded: false,
                }));
            }
        } catch (e) { console.error('[Sources] Load failed:', e); }
    } else if (oldSaved) {
        // Migrate old format
        try {
            const config = JSON.parse(oldSaved);
            pairSources['USDC/M1'] = config.map(s => ({
                ...s, id: generateSourceId(),
                invert: s.invert || false, scaleFactor: s.scaleFactor || 1,
                lastPrice: null, lastUpdate: null, status: 'off', expanded: false,
            }));
            localStorage.removeItem('usdcM1Sources');
            savePairSourcesAll();
        } catch (e) { console.error('[Sources] Migration failed:', e); }
    }

    // Ensure defaults for pairs with no sources
    for (const [pairId, meta] of Object.entries(PAIR_META)) {
        if (meta.fixed) continue;
        if (!pairSources[pairId] || pairSources[pairId].length === 0) {
            const presets = meta.defaultPresets || [];
            pairSources[pairId] = presets.map(k => {
                const p = SOURCE_PRESETS[k];
                return p ? {
                    id: generateSourceId(), type: k, ...p,
                    enabled: true, lastPrice: null, lastUpdate: null, status: 'off', expanded: false,
                } : null;
            }).filter(Boolean);
        }
    }
}

function startPriceRefresh() {
    if (priceRefreshInterval) clearInterval(priceRefreshInterval);
    priceRefreshInterval = setInterval(refreshSettlementDesk, refreshIntervalSeconds * 1000);
    const el = document.getElementById('refresh-interval-display');
    if (el) el.textContent = `${refreshIntervalSeconds}s`;
}

// =============================================================================
// BTC CONFIRMATION CONFIG
// =============================================================================

// Update confirmation tier time display
function updateConfirmationTimes() {
    const tiers = [1, 2, 3, 4];
    tiers.forEach(tier => {
        const input = document.getElementById(`btc-conf-tier-${tier}`);
        const timeEl = document.getElementById(`btc-conf-tier-${tier}-time`);
        if (input && timeEl) {
            const conf = parseInt(input.value) || 1;
            const minutes = conf * 10;
            timeEl.textContent = `~${minutes} min`;
        }
    });
}

// Setup confirmation tier listeners
function setupConfirmationListeners() {
    const tiers = [1, 2, 3, 4];
    tiers.forEach(tier => {
        const input = document.getElementById(`btc-conf-tier-${tier}`);
        if (input) {
            input.addEventListener('input', updateConfirmationTimes);
        }
    });
    // Initial update
    updateConfirmationTimes();
}

// Get confirmation config from UI
function getConfirmationConfig() {
    return {
        BTC: {
            default: parseInt(document.getElementById('btc-conf-tier-3')?.value) || 3,
            min: 1,
            max: 6,
            tiers: [
                { max_btc: 0.01, confirmations: parseInt(document.getElementById('btc-conf-tier-1')?.value) || 1 },
                { max_btc: 0.1, confirmations: parseInt(document.getElementById('btc-conf-tier-2')?.value) || 2 },
                { max_btc: 0.5, confirmations: parseInt(document.getElementById('btc-conf-tier-3')?.value) || 3 },
                { max_btc: 1.0, confirmations: parseInt(document.getElementById('btc-conf-tier-4')?.value) || 6 },
            ],
        },
    };
}

// Load confirmation config from server
async function loadConfirmationConfig() {
    try {
        const response = await fetch('/api/lp/confirmations');
        if (response.ok) {
            const data = await response.json();
            const btcConfig = data.confirmations?.BTC;
            if (btcConfig?.tiers) {
                btcConfig.tiers.forEach((tier, i) => {
                    const input = document.getElementById(`btc-conf-tier-${i + 1}`);
                    if (input) {
                        input.value = tier.confirmations;
                    }
                });
                updateConfirmationTimes();
            }
        }
    } catch (e) {
        console.error('[Confirmations] Failed to load:', e);
    }
}

// Setup event listeners for settings (confirmations + rate refresh)
function setupSettingsListeners() {
    setupConfirmationListeners();

    const intervalSelect = document.getElementById('rate-refresh-interval');
    if (intervalSelect) {
        intervalSelect.addEventListener('change', (e) => {
            refreshIntervalSeconds = parseInt(e.target.value) || 10;
            startPriceRefresh();
        });
    }
}

function saveLPConfig() {
    // Dynamically read pair config from settlement book DOM
    const pairs = {};
    for (const pairId of PAIR_ORDER) {
        const slug = pairId.replace('/', '-').toLowerCase();
        pairs[slug] = {
            enabled: document.getElementById(`pair-${slug}-enabled`)?.checked ?? true,
            spreadBid: parseFloat(document.getElementById(`pair-${slug}-spread-bid`)?.value) || 0.5,
            spreadAsk: parseFloat(document.getElementById(`pair-${slug}-spread-ask`)?.value) || 0.5,
            limits: {
                min: parseFloat(document.getElementById(`pair-${slug}-min`)?.value) || 0,
                max: parseFloat(document.getElementById(`pair-${slug}-max`)?.value) || 0,
            },
        };
    }

    const config = {
        pairs,
        apiKeys: {
            binance: !!document.getElementById('api-key-binance')?.value,
            coingecko: !!document.getElementById('api-key-coingecko')?.value,
            kraken: !!document.getElementById('api-key-kraken')?.value,
        },
        auto: {
            claim: document.getElementById('auto-claim')?.checked ?? true,
            refund: document.getElementById('auto-refund')?.checked ?? true,
            rebalance: document.getElementById('auto-rebalance')?.checked ?? false,
        },
        rateRefreshInterval: parseInt(document.getElementById('rate-refresh-interval')?.value) || 10,
    };

    console.log('[Config] Saving LP config:', config);
    localStorage.setItem('lpConfig', JSON.stringify(config));
}

async function loadLPConfig() {
    // Load API key status from server (always, regardless of localStorage)
    try {
        const keyResp = await fetch('/api/lp/api-keys/status');
        if (keyResp.ok) {
            const keyData = await keyResp.json();
            if (keyData?.keys) {
                for (const [src, configured] of Object.entries(keyData.keys)) {
                    const input = document.getElementById(`api-key-${src}`);
                    if (input && configured) {
                        input.placeholder = '\u2713 configured on server';
                        input.value = '';
                    }
                }
            }
        }
    } catch (e) {
        console.log('[Config] Could not load API key status');
    }

    const saved = localStorage.getItem('lpConfig');
    if (!saved) return;

    try {
        const config = JSON.parse(saved);

        // Load auto settings
        if (config.auto) {
            if (config.auto.claim !== undefined) document.getElementById('auto-claim').checked = config.auto.claim;
            if (config.auto.refund !== undefined) document.getElementById('auto-refund').checked = config.auto.refund;
            if (config.auto.rebalance !== undefined) document.getElementById('auto-rebalance').checked = config.auto.rebalance;
        }

        if (config.rateRefreshInterval) {
            document.getElementById('rate-refresh-interval').value = config.rateRefreshInterval;
            refreshIntervalSeconds = config.rateRefreshInterval;
        }

        console.log('[Config] LP settings loaded from localStorage');
    } catch (e) {
        console.error('[Config] Failed to load:', e);
    }
}

// Save ALL config (sources + spreads + limits) - local + server
async function saveAllConfig() {
    // Save sources per-pair
    savePairSourcesAll();

    // Save LP settings locally
    saveLPConfig();

    // Push config to server for quotes/API
    await pushConfigToServer();

    // Refresh settlement desk
    await refreshSettlementDesk();

    alert('Configuration saved!');
}

// Push LP config to server — dynamic pairs
async function pushConfigToServer() {
    const lpName = document.getElementById('lp-display-name')?.value?.trim() || undefined;

    // Build pairs config dynamically — only installed chains
    const pairs = {};
    for (const pairId of PAIR_ORDER) {
        const meta = PAIR_META[pairId];
        if (!meta) continue;
        const chainId = meta.from === 'zcash' ? 'zcash' : meta.from;
        if (!(_cachedChainStatus[chainId]?.installed)) continue; // skip non-installed

        const slug = pairId.replace('/', '-').toLowerCase();
        const enabled = document.getElementById(`pair-${slug}-enabled`)?.checked ?? true;
        const spreadBid = parseFloat(document.getElementById(`pair-${slug}-spread-bid`)?.value) || 0.5;
        const spreadAsk = parseFloat(document.getElementById(`pair-${slug}-spread-ask`)?.value) || 0.5;
        const min = parseFloat(document.getElementById(`pair-${slug}-min`)?.value) || 0;
        const max = parseFloat(document.getElementById(`pair-${slug}-max`)?.value) || 0;

        pairs[pairId] = { enabled, spread_bid: spreadBid, spread_ask: spreadAsk, min, max };

        // For non-fixed pairs, include live rate if available
        if (!meta.fixed && _cachedLpConfig?.pairs?.[pairId]?._liveRate) {
            pairs[pairId].rate = _cachedLpConfig.pairs[pairId]._liveRate;
        }
    }

    const config = {
        name: lpName,
        pairs,
        confirmations: getConfirmationConfig(),
    };

    try {
        const response = await fetch('/api/lp/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });

        if (response.ok) {
            console.log('[Config] Pushed to server successfully');
            const sidebar = document.getElementById('sidebar-lp-name');
            if (sidebar && lpName) sidebar.textContent = lpName;
        } else {
            console.error('[Config] Failed to push to server:', response.status);
        }
    } catch (e) {
        console.error('[Config] Error pushing to server:', e);
    }

    // Push API keys to backend (separate endpoint for security)
    const apiKeys = {};
    const binKey = document.getElementById('api-key-binance')?.value?.trim();
    const binSecret = document.getElementById('api-secret-binance')?.value?.trim();
    const cgKey = document.getElementById('api-key-coingecko')?.value?.trim();
    const krKey = document.getElementById('api-key-kraken')?.value?.trim();
    if (binKey) apiKeys.binance_api_key = binKey;
    if (binSecret) apiKeys.binance_api_secret = binSecret;
    if (cgKey) apiKeys.coingecko_api_key = cgKey;
    if (krKey) apiKeys.kraken_api_key = krKey;

    if (Object.keys(apiKeys).length > 0) {
        try {
            const keyResp = await fetch('/api/lp/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(apiKeys),
            });
            if (keyResp.ok) {
                console.log('[Config] API keys pushed to server');
                // Clear input values and show configured status
                ['binance', 'coingecko', 'kraken'].forEach(src => {
                    const input = document.getElementById(`api-key-${src}`);
                    if (input && input.value) {
                        input.value = '';
                        input.placeholder = '\u2713 configured on server';
                    }
                });
                const secretInput = document.getElementById('api-secret-binance');
                if (secretInput && secretInput.value) {
                    secretInput.value = '';
                    secretInput.placeholder = '\u2713 configured on server';
                }
            } else {
                console.error('[Config] Failed to push API keys:', keyResp.status);
            }
        } catch (e) {
            console.error('[Config] Error pushing API keys:', e);
        }
    }
}

// Reset ALL config
function resetAllConfig() {
    if (confirm('Reset all LP configuration to defaults?')) {
        localStorage.removeItem('lpConfig');
        localStorage.removeItem('pairSourcesConfig');
        localStorage.removeItem('usdcM1Sources');
        location.reload();
    }
}

// =============================================================================
// UTILITIES
// =============================================================================

function formatTime(timestamp) {
    const date = new Date(timestamp * 1000);
    return date.toLocaleString();
}

// =============================================================================
// CHAIN STATUS REFRESH
// =============================================================================

// refreshChainStatuses removed — dynamic wallets in LP Config handle this

// =============================================================================
// INIT
// =============================================================================

async function init() {
    console.log('[Dashboard] Initializing...');

    // Initialize M1 unit toggle UI
    updateUnitToggleUI();

    // Load saved settings from localStorage + API key status from server
    await loadLPConfig();

    // Load per-pair price sources from localStorage
    loadPairSourcesAll();

    // Setup settings event listeners (confirmations, rate refresh)
    setupSettingsListeners();

    // Load confirmation config from server
    await loadConfirmationConfig();

    // Check API status
    await checkApiStatus();

    // Load LP identity (name, id) from server
    await loadLPIdentity();

    // Fetch initial settlement book (rates + pair config)
    await refreshSettlementDesk();

    // Push loaded config to server (sync localStorage -> server)
    await pushConfigToServer();

    // Refresh inventory from wallets
    await refreshInventory();

    // Start price refresh interval
    startPriceRefresh();

    // Load overview
    loadOverview();

    // Auto-refresh every 30s (for non-price stuff)
    refreshInterval = setInterval(() => {
        checkApiStatus();
        const activePage = document.querySelector('.page.active');
        if (activePage?.id === 'page-overview') loadOverview();
        if (activePage?.id === 'page-swaps') loadSwaps();
        if (activePage?.id === 'page-config') renderConfigWallets();
    }, 30000);

    console.log('[Dashboard] Ready');
}

// Refresh LP inventory from wallet balances
async function refreshInventory() {
    try {
        const response = await fetch('/api/lp/inventory/refresh', { method: 'POST' });
        if (response.ok) {
            const data = await response.json();
            console.log('[Inventory] Refreshed:', data.inventory);
        }
    } catch (e) {
        console.error('[Inventory] Refresh failed:', e);
    }
}

document.addEventListener('DOMContentLoaded', init);
