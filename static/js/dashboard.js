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
    document.querySelector(`.nav-item[data-page="${pageId}"]`).classList.add('active');

    // Update pages
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${pageId}`).classList.add('active');

    // Load page data
    if (pageId === 'overview') loadOverview();
    if (pageId === 'swaps') loadSwaps();
    if (pageId === 'wallets') loadWallets();
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

async function renderChainStatus() {
    const grid = document.getElementById('chain-status-grid');

    // Fetch real chain status
    const data = await apiCall('/api/chains/status');
    const chainInfo = data?.chains || {};

    // Get sync status for BTC and M1
    const btcSync = await apiCall('/api/chain/btc/sync');
    const m1Sync = await apiCall('/api/chain/m1/sync');

    const chains = [
        { id: 'btc', name: 'Bitcoin', icon: '\u20bf', color: '#f7931a', sync: btcSync },
        { id: 'm1', name: 'M1 (BATHRON)', icon: 'M', color: '#3b82f6', sync: m1Sync },
        { id: 'usdc', name: 'USDC (Base)', icon: '$', color: '#2775ca', sync: null },
    ];

    grid.innerHTML = chains.map(chain => {
        const status = chainInfo[chain.id] || {};
        let isConnected = false;
        let heightText = '-';

        let statusClass = 'disconnected';

        if (chain.id === 'usdc') {
            // USDC uses external RPC, always "connected" if enabled
            isConnected = status.installed;
            heightText = isConnected ? 'RPC OK' : 'Not configured';
            statusClass = isConnected ? 'connected' : 'disconnected';
        } else if (chain.sync && !chain.sync.error) {
            // BTC or M1 with sync data
            isConnected = true;
            heightText = chain.sync.blocks?.toLocaleString() || '-';
            if (chain.sync.syncing) {
                heightText += ` (${chain.sync.progress?.toFixed(0)}%)`;
                statusClass = 'syncing';  // Orange while syncing
            } else {
                statusClass = 'connected';  // Green when synced
            }
        } else {
            isConnected = false;
            heightText = status.installed ? 'Stopped' : 'Not installed';
            statusClass = 'disconnected';
        }

        return `
        <div class="chain-status-item">
            <span class="chain-icon" style="color: ${chain.color}">${chain.icon}</span>
            <div class="chain-info">
                <div class="chain-name">${chain.name}</div>
                <div class="chain-height">${chain.id === 'usdc' ? '' : 'Block: '}${heightText}</div>
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
// WALLETS PAGE
// =============================================================================

async function loadWallets() {
    const data = await apiCall('/api/wallets');
    if (!data) return;

    // BTC
    if (data.btc) {
        let btcText = data.btc.balance?.toFixed(8) || '0.00000000';
        if (data.btc.pending && data.btc.pending > 0) {
            btcText += ` (+${data.btc.pending.toFixed(8)} pending)`;
        }
        document.getElementById('wallet-btc-balance').textContent = btcText;
        document.getElementById('wallet-btc-address').textContent =
            data.btc.address || 'Node not running';
    }

    // M1 (1 M1 = 1 sat, display as integer)
    if (data.m1) {
        let m1Balance = data.m1.balance || 0;
        let m1Text = m1Balance.toLocaleString();
        if (data.m1.pending && data.m1.pending > 0) {
            m1Text += ` (+${data.m1.pending.toLocaleString()} pending)`;
        }
        document.getElementById('wallet-m1-balance').textContent = m1Text;
        document.getElementById('wallet-m1-address').textContent =
            data.m1.address || 'Node not running';
    }

    // USDC (token balance + ETH for gas on Base Sepolia)
    if (data.usdc) {
        // Main balance is USDC token balance
        let usdcText = data.usdc.balance?.toFixed(2) || '0.00';
        document.getElementById('wallet-usdc-balance').textContent = usdcText;

        // Show ETH gas balance separately
        const ethGas = data.usdc.eth_balance || 0;
        const ethGasEl = document.getElementById('wallet-usdc-eth-gas');
        if (ethGasEl) {
            ethGasEl.textContent = `(${ethGas.toFixed(6)} ETH for gas)`;
        }

        document.getElementById('wallet-usdc-address').textContent =
            data.usdc.address || 'Not configured';
    }
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

async function resetWalletAddress(chain) {
    if (!confirm(`Reset ${chain.toUpperCase()} LP address? This will create a new address if the cached one is invalid.`)) {
        return;
    }

    const result = await apiCall(`/api/wallets/reset-address?chain=${chain}`, { method: 'POST' });
    if (result) {
        alert(`Address reset!\n\nOld: ${result.old_address || 'None'}\nNew: ${result.new_address}\nBalance: ${result.balance}`);
        await loadWallets();
    } else {
        alert('Failed to reset address');
    }
}

function copyAddress(chain) {
    const address = document.getElementById(`wallet-${chain}-address`).textContent;
    if (address && address !== 'Not configured' && address !== 'Node not running') {
        navigator.clipboard.writeText(address);
        alert('Address copied!');
    }
}

async function refreshBalance(chain) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '...';

    await loadWallets();

    btn.disabled = false;
    btn.textContent = '↻';
}

// =============================================================================
// CHAIN CONFIG
// =============================================================================

async function testChainConnection(chain) {
    const statusEl = document.getElementById(`${chain}-chain-status`);
    statusEl.innerHTML = '<span class="status-dot"></span><span>Testing...</span>';

    const result = await apiCall(`/api/chain/${chain}/test`, { method: 'POST' });

    if (result && result.connected) {
        statusEl.innerHTML = `<span class="status-dot connected"></span><span>Connected (Block: ${result.height || '-'})</span>`;
    } else {
        statusEl.innerHTML = `<span class="status-dot disconnected"></span><span>Connection failed</span>`;
    }
}

async function installChain(chain) {
    const btn = document.getElementById(`${chain}-install-btn`);
    const progress = document.getElementById(`${chain}-install-progress`);
    const progressFill = document.getElementById(`${chain}-progress-fill`);
    const progressText = document.getElementById(`${chain}-progress-text`);
    const status = document.getElementById(`${chain}-install-status`);

    // Disable button
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">&#8987;</span> Installing...';

    // Show progress
    progress.classList.remove('hidden');
    status.className = 'install-status installing';
    status.innerHTML = '<span class="status-icon">&#8987;</span><span>Installing...</span>';

    // Start installation
    const result = await apiCall(`/api/chain/${chain}/install`, { method: 'POST' });

    if (result && result.job_id) {
        // Poll for progress
        pollInstallProgress(chain, result.job_id);
    } else {
        // Installation failed to start
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">&#8595;</span> Install Failed - Retry';
        progress.classList.add('hidden');
        status.className = 'install-status';
        status.innerHTML = '<span class="status-icon">&#10007;</span><span>Install failed</span>';
    }
}

async function pollInstallProgress(chain, jobId) {
    const progressFill = document.getElementById(`${chain}-progress-fill`);
    const progressText = document.getElementById(`${chain}-progress-text`);
    const status = document.getElementById(`${chain}-install-status`);
    const btn = document.getElementById(`${chain}-install-btn`);
    const progress = document.getElementById(`${chain}-install-progress`);

    const poll = async () => {
        const result = await apiCall(`/api/chain/${chain}/install/status?job_id=${jobId}`);

        if (!result) {
            progressText.textContent = 'Error checking status...';
            return;
        }

        progressFill.style.width = `${result.progress || 0}%`;
        progressText.textContent = result.message || 'Installing...';

        if (result.status === 'complete') {
            status.className = 'install-status installed';
            status.innerHTML = '<span class="status-icon">&#10003;</span><span>Installed</span>';
            btn.innerHTML = '<span class="btn-icon">&#10003;</span> Installed';
            btn.disabled = true;
            progress.classList.add('hidden');
        } else if (result.status === 'failed') {
            status.className = 'install-status';
            status.innerHTML = '<span class="status-icon">&#10007;</span><span>Install failed</span>';
            btn.innerHTML = '<span class="btn-icon">&#8595;</span> Retry Install';
            btn.disabled = false;
        } else {
            // Still installing, poll again
            setTimeout(poll, 2000);
        }
    };

    poll();
}

async function startChain(chain) {
    const statusEl = document.getElementById(`${chain}-chain-status`);
    statusEl.innerHTML = '<span class="status-dot"></span><span>Starting...</span>';

    const result = await apiCall(`/api/chain/${chain}/start`, { method: 'POST' });

    if (result && result.started) {
        statusEl.innerHTML = '<span class="status-dot connected"></span><span>Running - checking sync...</span>';
        // Start polling sync status
        pollSyncStatus(chain);
    } else {
        statusEl.innerHTML = `<span class="status-dot disconnected"></span><span>Start failed: ${result?.error || 'Unknown'}</span>`;
    }
}

async function pollSyncStatus(chain) {
    const statusEl = document.getElementById(`${chain}-chain-status`);
    const progressEl = document.getElementById(`${chain}-sync-progress`);
    const progressFill = document.getElementById(`${chain}-sync-fill`);
    const progressText = document.getElementById(`${chain}-sync-text`);

    const poll = async () => {
        const result = await apiCall(`/api/chain/${chain}/sync`);

        if (!result || result.error) {
            if (result?.error === 'Node not running') {
                statusEl.innerHTML = '<span class="status-dot disconnected"></span><span>Stopped</span>';
                if (progressEl) progressEl.classList.add('hidden');
            }
            return;
        }

        // Update status
        if (result.syncing) {
            statusEl.innerHTML = `<span class="status-dot syncing"></span><span>Syncing ${result.progress.toFixed(1)}%</span>`;
            // Show progress bar
            if (progressEl) {
                progressEl.classList.remove('hidden');
                progressFill.style.width = `${result.progress}%`;
                progressText.textContent = `Block ${result.blocks.toLocaleString()} / ${result.headers.toLocaleString()}`;
            }
            // Continue polling
            setTimeout(poll, 5000);
        } else {
            // Synced
            statusEl.innerHTML = `<span class="status-dot connected"></span><span>Synced (Block ${result.blocks.toLocaleString()})</span>`;
            if (progressEl) progressEl.classList.add('hidden');
        }
    };

    // Start polling after a short delay
    setTimeout(poll, 2000);
}

async function stopChain(chain) {
    const statusEl = document.getElementById(`${chain}-chain-status`);
    statusEl.innerHTML = '<span class="status-dot"></span><span>Stopping...</span>';

    const result = await apiCall(`/api/chain/${chain}/stop`, { method: 'POST' });

    if (result && result.stopped) {
        statusEl.innerHTML = '<span class="status-dot disconnected"></span><span>Stopped</span>';
    } else {
        statusEl.innerHTML = `<span class="status-dot"></span><span>Stop failed</span>`;
    }
}

async function deployHTLCContract() {
    const result = await apiCall('/api/chain/usdc/deploy-htlc', { method: 'POST' });

    if (result && result.address) {
        document.getElementById('usdc-htlc-contract').value = result.address;
        alert(`HTLC Contract deployed!\n\nAddress: ${result.address}`);
    } else {
        alert('Failed to deploy HTLC contract. Check your private key and RPC.');
    }
}

function saveChainConfig() {
    const config = {
        btc: {
            enabled: document.getElementById('chain-btc-enabled').checked,
            rpc: document.getElementById('btc-rpc').value,
            user: document.getElementById('btc-rpc-user').value,
            pass: document.getElementById('btc-rpc-pass').value,
        },
        m1: {
            enabled: document.getElementById('chain-m1-enabled').checked,
            rpc: document.getElementById('m1-rpc').value,
            user: document.getElementById('m1-rpc-user').value,
            pass: document.getElementById('m1-rpc-pass').value,
            masternode: document.getElementById('m1-masternode').checked,
        },
        usdc: {
            enabled: document.getElementById('chain-usdc-enabled').checked,
            rpc: document.getElementById('usdc-rpc').value,
            privkey: document.getElementById('usdc-privkey').value,
            htlcContract: document.getElementById('usdc-htlc-contract').value,
        },
    };

    console.log('Saving chain config:', config);
    localStorage.setItem('chainConfig', JSON.stringify(config));
    alert('Configuration saved!');
}

function loadChainConfig() {
    const saved = localStorage.getItem('chainConfig');
    if (!saved) return;

    try {
        const config = JSON.parse(saved);

        // BTC
        if (config.btc) {
            if (config.btc.enabled !== undefined) document.getElementById('chain-btc-enabled').checked = config.btc.enabled;
            if (config.btc.rpc) document.getElementById('btc-rpc').value = config.btc.rpc;
            if (config.btc.user) document.getElementById('btc-rpc-user').value = config.btc.user;
            if (config.btc.pass) document.getElementById('btc-rpc-pass').value = config.btc.pass;
        }

        // M1
        if (config.m1) {
            if (config.m1.enabled !== undefined) document.getElementById('chain-m1-enabled').checked = config.m1.enabled;
            if (config.m1.rpc) document.getElementById('m1-rpc').value = config.m1.rpc;
            if (config.m1.user) document.getElementById('m1-rpc-user').value = config.m1.user;
            if (config.m1.pass) document.getElementById('m1-rpc-pass').value = config.m1.pass;
            if (config.m1.masternode !== undefined) document.getElementById('m1-masternode').checked = config.m1.masternode;
        }

        // USDC
        if (config.usdc) {
            if (config.usdc.enabled !== undefined) document.getElementById('chain-usdc-enabled').checked = config.usdc.enabled;
            if (config.usdc.rpc) document.getElementById('usdc-rpc').value = config.usdc.rpc;
            if (config.usdc.privkey) document.getElementById('usdc-privkey').value = config.usdc.privkey;
            if (config.usdc.htlcContract) document.getElementById('usdc-htlc-contract').value = config.usdc.htlcContract;
        }

        console.log('Chain config loaded from localStorage');
    } catch (e) {
        console.error('Failed to load chain config:', e);
    }
}

// =============================================================================
// LP CONFIG - Per-Pair Pricing
// =============================================================================

// =============================================================================
// ATOMIC UNITS - All calculations in integers, no floats
// =============================================================================

// BTC/M1 is FIXED: 1 SAT = 1 M1, so 1 BTC = 100,000,000 M1
const BTC_M1_FIXED_RATE = 100000000n; // BigInt for precision
const SATS_PER_BTC = 100000000n;
const MICRO_USDC_PER_USDC = 1000000n;

// Spread basis points (1% = 100 bp, 0.1% = 10 bp)
const BASIS_POINTS = 10000n;

// =============================================================================
// PRICE SOURCES SYSTEM
// =============================================================================

// Source presets
const SOURCE_PRESETS = {
    binance_btcusdc: {
        name: 'Binance (via BTCUSDC)',
        icon: 'B',
        url: 'https://api.binance.com/api/v3/ticker/price',
        symbol: 'BTCUSDC',
        jsonPath: 'price',
        weight: 100,
        invert: true,           // 1/BTCUSDC = USDC/BTC
        scaleFactor: 100000000, // × 100M to get M1 (sats)
    },
    binance_usdcusdt: {
        name: 'Binance USDCUSDT',
        icon: 'B',
        url: 'https://api.binance.com/api/v3/ticker/price',
        symbol: 'USDCUSDT',
        jsonPath: 'price',
        weight: 50,
        invert: false,
        scaleFactor: 1,
    },
    coingecko: {
        name: 'CoinGecko',
        icon: 'CG',
        url: 'https://api.coingecko.com/api/v3/simple/price?ids=usd-coin&vs_currencies=usd',
        symbol: '',
        jsonPath: 'usd-coin.usd',
        weight: 50,
        invert: false,
        scaleFactor: 1,
    },
    kraken: {
        name: 'Kraken',
        icon: 'K',
        url: 'https://api.kraken.com/0/public/Ticker?pair=USDCUSD',
        symbol: '',
        jsonPath: 'result.USDCUSD.c.0',
        weight: 0,
        invert: false,
        scaleFactor: 1,
    },
    custom: {
        name: 'Custom',
        icon: '?',
        url: '',
        symbol: '',
        jsonPath: 'price',
        weight: 0,
        invert: false,
        scaleFactor: 1,
    },
};

// Active sources for USDC/M1
let usdcM1Sources = [];
let sourceIdCounter = 0;
let priceRefreshInterval = null;
let refreshIntervalSeconds = 10;

// Generate unique source ID
function generateSourceId() {
    return `source_${++sourceIdCounter}`;
}

// Add a new source
function addSource(presetKey) {
    const preset = SOURCE_PRESETS[presetKey];
    if (!preset) return;

    const source = {
        id: generateSourceId(),
        type: presetKey,
        name: preset.name,
        icon: preset.icon,
        url: preset.url,
        symbol: preset.symbol,
        jsonPath: preset.jsonPath,
        weight: preset.weight,
        invert: preset.invert || false,
        scaleFactor: preset.scaleFactor || 1,
        enabled: true,
        lastPrice: null,
        lastUpdate: null,
        status: 'off',
        expanded: presetKey === 'custom', // Expand custom by default
    };

    usdcM1Sources.push(source);
    renderSources();
    updateWeightTotal();
    saveSourcesConfig();

    // Reset dropdown
    document.getElementById('add-source-preset').value = '';
}

// Remove a source
function removeSource(sourceId) {
    usdcM1Sources = usdcM1Sources.filter(s => s.id !== sourceId);
    renderSources();
    updateWeightTotal();
    saveSourcesConfig();
}

// Toggle source enabled
function toggleSource(sourceId) {
    const source = usdcM1Sources.find(s => s.id === sourceId);
    if (source) {
        source.enabled = !source.enabled;
        renderSources();
        updateWeightTotal();
        saveSourcesConfig();
    }
}

// Toggle source expanded
function toggleSourceExpanded(sourceId) {
    const source = usdcM1Sources.find(s => s.id === sourceId);
    if (source) {
        source.expanded = !source.expanded;
        renderSources();
    }
}

// Update source field
function updateSourceField(sourceId, field, value) {
    const source = usdcM1Sources.find(s => s.id === sourceId);
    if (source) {
        source[field] = field === 'weight' ? parseFloat(value) || 0 : value;
        if (field === 'weight') {
            updateWeightTotal();
        }
        saveSourcesConfig();
        renderSources();
    }
}

// Render sources list
function renderSources() {
    const container = document.getElementById('usdc-m1-sources-list');
    if (!container) return;

    if (usdcM1Sources.length === 0) {
        container.innerHTML = '<div class="no-sources">Aucune source configurée</div>';
        return;
    }

    container.innerHTML = usdcM1Sources.map(source => `
        <div class="source-item ${source.enabled ? 'enabled' : ''} ${source.expanded ? 'expanded' : ''}" data-id="${source.id}">
            <div class="source-item-header" onclick="toggleSourceExpanded('${source.id}')">
                <input type="checkbox" class="source-toggle"
                    ${source.enabled ? 'checked' : ''}
                    onclick="event.stopPropagation(); toggleSource('${source.id}')">
                <span class="source-icon-badge">${source.icon}</span>
                <span class="source-name">${source.name}</span>
                <span class="source-weight-badge">${source.weight}%</span>
                <span class="source-status-badge ${source.status}">${source.status === 'live' ? '1 USDC = ' + Math.round(source.lastPrice).toLocaleString() + ' M1' : source.status}</span>
                <button class="source-delete-btn" onclick="event.stopPropagation(); removeSource('${source.id}')" title="Supprimer">×</button>
            </div>
            <div class="source-item-body">
                <div class="source-field">
                    <label>Nom</label>
                    <input type="text" value="${source.name}"
                        onchange="updateSourceField('${source.id}', 'name', this.value)">
                </div>
                <div class="source-field">
                    <label>URL Endpoint</label>
                    <input type="text" value="${source.url}"
                        onchange="updateSourceField('${source.id}', 'url', this.value)"
                        placeholder="https://api.exchange.com/price">
                    <div class="field-hint">URL de l'API REST</div>
                </div>
                <div class="source-field-row">
                    <div class="source-field">
                        <label>Symbole</label>
                        <input type="text" value="${source.symbol}"
                            onchange="updateSourceField('${source.id}', 'symbol', this.value)"
                            placeholder="USDCUSDT">
                        <div class="field-hint">Paramètre symbol (si requis)</div>
                    </div>
                    <div class="source-field">
                        <label>Poids</label>
                        <input type="number" value="${source.weight}" min="0" max="100"
                            onchange="updateSourceField('${source.id}', 'weight', this.value)">
                    </div>
                </div>
                <div class="source-field">
                    <label>JSON Path</label>
                    <input type="text" value="${source.jsonPath}"
                        onchange="updateSourceField('${source.id}', 'jsonPath', this.value)"
                        placeholder="price ou data.rate">
                </div>
                <div class="source-field-row">
                    <div class="source-field">
                        <label>Inverser (1/x)</label>
                        <label class="checkbox-label" style="margin-top:4px">
                            <input type="checkbox" ${source.invert ? 'checked' : ''}
                                onchange="updateSourceField('${source.id}', 'invert', this.checked)">
                            Oui
                        </label>
                    </div>
                    <div class="source-field">
                        <label>Facteur (×)</label>
                        <input type="number" value="${source.scaleFactor || 1}"
                            onchange="updateSourceField('${source.id}', 'scaleFactor', this.value)"
                            placeholder="1">
                        <div class="field-hint">Ex: 100000000 pour SAT→M1</div>
                    </div>
                </div>
                <div class="source-formula">
                    Formule: prix ${source.invert ? '→ 1/x' : ''} ${source.scaleFactor > 1 ? '→ ×' + source.scaleFactor.toLocaleString() : ''}
                </div>
                ${source.lastPrice ? `
                <div class="source-price-preview">
                    <span>Résultat:</span>
                    <span class="source-price-value">1 USDC = ${Math.round(source.lastPrice).toLocaleString()} M1</span>
                </div>
                ` : ''}
            </div>
        </div>
    `).join('');
}

// Fetch price from a single source
async function fetchSourcePrice(source) {
    if (!source.enabled || !source.url) {
        source.status = 'off';
        return null;
    }

    try {
        // Build URL with symbol if needed
        let url = source.url;
        if (source.symbol && source.type.startsWith('binance')) {
            url += `?symbol=${source.symbol}`;
        }

        // Use backend proxy to avoid CORS
        const response = await fetch(`/api/proxy/price?url=${encodeURIComponent(url)}&path=${encodeURIComponent(source.jsonPath)}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        if (data.price !== undefined) {
            let price = parseFloat(data.price);

            // Invert if needed (e.g., BTCUSDC -> USDC/BTC)
            if (source.invert && price > 0) {
                price = 1 / price;
            }

            // Apply scale factor (e.g., ×100M to convert to M1/sats)
            const scaleFactor = parseFloat(source.scaleFactor) || 1;
            if (scaleFactor !== 1) {
                price = price * scaleFactor;
            }

            source.lastPrice = price;
            source.lastUpdate = Date.now();
            source.status = 'live';
            return source.lastPrice;
        } else {
            throw new Error('No price in response');
        }
    } catch (e) {
        console.error(`Error fetching ${source.name}:`, e);
        source.status = 'error';
        return null;
    }
}

// Fetch all prices and calculate weighted average
async function fetchAllPrices() {
    const enabledSources = usdcM1Sources.filter(s => s.enabled && s.url);

    if (enabledSources.length === 0) {
        return 1.0; // Default
    }

    // Fetch all prices in parallel
    await Promise.all(enabledSources.map(fetchSourcePrice));

    // Calculate weighted average
    let totalWeight = 0;
    let weightedSum = 0;

    enabledSources.forEach(source => {
        if (source.lastPrice && source.weight > 0) {
            totalWeight += source.weight;
            weightedSum += source.lastPrice * source.weight;
        }
    });

    renderSources();

    if (totalWeight === 0) {
        return 1.0;
    }

    return weightedSum / totalWeight;
}

// Update weight total display
function updateWeightTotal() {
    let total = 0;
    usdcM1Sources.forEach(source => {
        if (source.enabled) {
            total += source.weight || 0;
        }
    });

    const totalEl = document.getElementById('usdc-m1-weight-total');
    const warnEl = document.getElementById('usdc-m1-weight-warn');

    if (totalEl) totalEl.textContent = total;
    if (warnEl) {
        warnEl.style.display = (total === 100) ? 'none' : 'inline';
    }
}

// Save sources config to localStorage
function saveSourcesConfig() {
    const config = usdcM1Sources.map(s => ({
        type: s.type,
        name: s.name,
        icon: s.icon,
        url: s.url,
        symbol: s.symbol,
        jsonPath: s.jsonPath,
        weight: s.weight,
        invert: s.invert || false,
        scaleFactor: s.scaleFactor || 1,
        enabled: s.enabled,
    }));
    localStorage.setItem('usdcM1Sources', JSON.stringify(config));
}

// Load sources config from localStorage
function loadSourcesConfig() {
    const saved = localStorage.getItem('usdcM1Sources');
    if (saved) {
        try {
            const config = JSON.parse(saved);
            usdcM1Sources = config.map(s => ({
                ...s,
                id: generateSourceId(),
                invert: s.invert || false,
                scaleFactor: s.scaleFactor || 1,
                lastPrice: null,
                lastUpdate: null,
                status: 'off',
                expanded: false, // Always collapsed on load
            }));
        } catch (e) {
            console.error('Failed to load sources config:', e);
            loadDefaultSources();
        }
    } else {
        loadDefaultSources();
    }
    renderSources();
    updateWeightTotal();
}

// Load default sources
function loadDefaultSources() {
    usdcM1Sources = [
        {
            id: generateSourceId(),
            type: 'binance_btcusdc',
            ...SOURCE_PRESETS.binance_btcusdc,
            enabled: true,
            lastPrice: null,
            lastUpdate: null,
            status: 'off',
            expanded: false,
        },
    ];
}

// Refresh all rates
async function refreshAllRates() {
    const usdcM1Rate = await fetchAllPrices();
    updateUsdcM1PreviewWithRate(usdcM1Rate);
    updateBtcM1Preview();
    updateDerivedPair();

    // Update last update time
    const lastUpdateEl = document.getElementById('usdc-m1-last-update');
    if (lastUpdateEl) {
        lastUpdateEl.textContent = new Date().toLocaleTimeString();
    }
}

// Start price refresh interval
function startPriceRefresh() {
    if (priceRefreshInterval) {
        clearInterval(priceRefreshInterval);
    }
    priceRefreshInterval = setInterval(refreshAllRates, refreshIntervalSeconds * 1000);

    const displayEl = document.getElementById('refresh-interval-display');
    if (displayEl) displayEl.textContent = `${refreshIntervalSeconds}s`;
}

// Setup add source dropdown
function setupAddSourceDropdown() {
    const dropdown = document.getElementById('add-source-preset');
    if (dropdown) {
        dropdown.addEventListener('change', (e) => {
            if (e.target.value) {
                addSource(e.target.value);
            }
        });
    }
}

// Collapse all sources
function collapseAllSources() {
    usdcM1Sources.forEach(s => s.expanded = false);
    renderSources();
}

// Save and refresh sources
async function saveAndRefreshSources() {
    saveSourcesConfig();

    // Show feedback
    const btn = document.querySelector('.btn-save-sources');
    const originalText = btn.textContent;
    btn.textContent = 'Sauvegardé !';
    btn.disabled = true;

    // Refresh prices
    await refreshAllRates();

    // Collapse all after save
    collapseAllSources();

    setTimeout(() => {
        btn.textContent = originalText;
        btn.disabled = false;
    }, 1500);
}

// Update BTC/M1 preview (fixed rate with bid/ask spread)
// All calculations in atomic units (integers)
function updateBtcM1Preview() {
    const rate = BTC_M1_FIXED_RATE; // BigInt: 100,000,000

    // Spreads in basis points (1% = 100bp)
    const bidSpreadPercent = parseFloat(document.getElementById('btc-m1-spread-bid')?.value) || 0.5;
    const askSpreadPercent = parseFloat(document.getElementById('btc-m1-spread-ask')?.value) || 0.5;
    const bidSpreadBp = BigInt(Math.round(bidSpreadPercent * 100));
    const askSpreadBp = BigInt(Math.round(askSpreadPercent * 100));

    // Bid: LP buys BTC from user, pays less M1
    // bidRate = rate * (10000 - bidSpreadBp) / 10000
    const bidRate = (rate * (BASIS_POINTS - bidSpreadBp)) / BASIS_POINTS;

    // Ask: LP sells BTC to user, charges more M1
    // askRate = rate * (10000 + askSpreadBp) / 10000
    const askRate = (rate * (BASIS_POINTS + askSpreadBp)) / BASIS_POINTS;

    const rateEl = document.getElementById('btc-m1-rate');
    const bidEl = document.getElementById('btc-m1-bid');
    const askEl = document.getElementById('btc-m1-ask');

    if (rateEl) rateEl.textContent = `1 BTC = ${Number(rate).toLocaleString()} M1`;
    if (bidEl) bidEl.textContent = `1 BTC = ${Number(bidRate).toLocaleString()} M1`;
    if (askEl) askEl.textContent = `1 BTC = ${Number(askRate).toLocaleString()} M1`;

    // Store for derived calculation (as BigInt)
    window.btcm1Rate = rate;
    window.btcm1BidRate = bidRate;
    window.btcm1AskRate = askRate;
}

// Update preview for USDC/M1 pair with given rate (USDC per M1)
// rate = how many M1 per 1 USDC (e.g., 1315)
function updateUsdcM1PreviewWithRate(usdcToM1Rate) {
    const bidSpread = parseFloat(document.getElementById('usdc-m1-spread-bid')?.value) || 0.5;
    const askSpread = parseFloat(document.getElementById('usdc-m1-spread-ask')?.value) || 0.5;

    // Convert to M1/USDC for display (how many USDC per M1)
    const m1ToUsdcRate = 1 / usdcToM1Rate;

    // Bid: LP buys USDC from user (user sells USDC for M1)
    // User gets less M1, so M1/USDC rate is lower for user
    const bidRate = m1ToUsdcRate * (1 + bidSpread / 100);

    // Ask: LP sells USDC to user (user buys USDC with M1)
    // User pays more M1, so M1/USDC rate is higher for user
    const askRate = m1ToUsdcRate * (1 - askSpread / 100);

    const rateEl = document.getElementById('usdc-m1-rate');
    const bidEl = document.getElementById('usdc-m1-bid');
    const askEl = document.getElementById('usdc-m1-ask');

    // Display as M1/USDC (e.g., 1 M1 = 0.00076 USDC)
    if (rateEl) rateEl.textContent = `1 M1 = ${m1ToUsdcRate.toFixed(8)} USDC`;
    if (bidEl) bidEl.textContent = `1 M1 = ${bidRate.toFixed(8)} USDC`;
    if (askEl) askEl.textContent = `1 M1 = ${askRate.toFixed(8)} USDC`;

    // Store for derived calculation (keep USDC/M1 internally)
    window.usdcm1Rate = usdcToM1Rate;
    window.usdcm1BidSpread = bidSpread;
    window.usdcm1AskSpread = askSpread;
}

// Legacy function for compatibility
function updateUsdcM1Preview() {
    const rate = window.usdcm1Rate || 1.0;
    updateUsdcM1PreviewWithRate(rate);
}

// Update derived BTC/USDC pair
function updateDerivedPair() {
    // Get rates (BigInt or Number)
    const btcM1Rate = Number(window.btcm1Rate || BTC_M1_FIXED_RATE);
    const usdcM1Rate = window.usdcm1Rate || 1;

    // Get spreads from inputs
    const btcM1BidSpread = parseFloat(document.getElementById('btc-m1-spread-bid')?.value) || 0;
    const btcM1AskSpread = parseFloat(document.getElementById('btc-m1-spread-ask')?.value) || 0;
    const usdcM1BidSpread = parseFloat(document.getElementById('usdc-m1-spread-bid')?.value) || 0;
    const usdcM1AskSpread = parseFloat(document.getElementById('usdc-m1-spread-ask')?.value) || 0;

    // BTC/USDC = BTC/M1 / USDC/M1
    const btcUsdcRate = btcM1Rate / usdcM1Rate;

    // Combined spreads per direction:
    // BTC → USDC: user sells BTC (BTC bid) + buys USDC (USDC ask)
    // USDC → BTC: user sells USDC (USDC bid) + buys BTC (BTC ask)
    const spreadBtcToUsdc = btcM1BidSpread + usdcM1AskSpread;
    const spreadUsdcToBtc = usdcM1BidSpread + btcM1AskSpread;

    const bidRate = btcUsdcRate * (1 - spreadBtcToUsdc / 100);
    const askRate = btcUsdcRate * (1 + spreadUsdcToBtc / 100);

    const rateEl = document.getElementById('btc-usdc-rate');
    const buyEl = document.getElementById('btc-usdc-buy');
    const sellEl = document.getElementById('btc-usdc-sell');
    const spreadEl = document.getElementById('btc-usdc-spread');

    if (rateEl) rateEl.textContent = `1 BTC = ${Math.round(btcUsdcRate).toLocaleString()} USDC`;
    if (buyEl) buyEl.textContent = `1 BTC = ${Math.round(bidRate).toLocaleString()} USDC`;
    if (sellEl) sellEl.textContent = `1 BTC = ${Math.round(askRate).toLocaleString()} USDC`;
    if (spreadEl) spreadEl.textContent = `BTC→USDC: ${spreadBtcToUsdc.toFixed(1)}% | USDC→BTC: ${spreadUsdcToBtc.toFixed(1)}%`;
}

// Update all pair previews
function updateAllPairPreviews() {
    updateBtcM1Preview();
    updateUsdcM1Preview();
    updateDerivedPair();
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

// Setup event listeners for pair config inputs
function setupPairConfigListeners() {
    // BTC confirmations
    setupConfirmationListeners();

    // BTC/M1: Fixed rate, only bid/ask spread
    const btcM1BidSpread = document.getElementById('btc-m1-spread-bid');
    const btcM1AskSpread = document.getElementById('btc-m1-spread-ask');

    if (btcM1BidSpread) {
        btcM1BidSpread.addEventListener('input', () => {
            updateBtcM1Preview();
            updateDerivedPair();
        });
    }
    if (btcM1AskSpread) {
        btcM1AskSpread.addEventListener('input', () => {
            updateBtcM1Preview();
            updateDerivedPair();
        });
    }

    // USDC/M1: Bid/Ask spread listeners
    const usdcM1BidSpread = document.getElementById('usdc-m1-spread-bid');
    const usdcM1AskSpread = document.getElementById('usdc-m1-spread-ask');

    if (usdcM1BidSpread) {
        usdcM1BidSpread.addEventListener('input', () => {
            updateUsdcM1Preview();
            updateDerivedPair();
        });
    }
    if (usdcM1AskSpread) {
        usdcM1AskSpread.addEventListener('input', () => {
            updateUsdcM1Preview();
            updateDerivedPair();
        });
    }

    // Setup add source dropdown
    setupAddSourceDropdown();

    // Refresh interval selector
    const intervalSelect = document.getElementById('rate-refresh-interval');
    if (intervalSelect) {
        intervalSelect.addEventListener('change', (e) => {
            refreshIntervalSeconds = parseInt(e.target.value) || 10;
            startPriceRefresh();
        });
    }
}

function saveLPConfig() {
    const config = {
        pairs: {
            'btc-m1': {
                enabled: document.getElementById('pair-btc-m1-enabled')?.checked ?? true,
                // Fixed rate: 1 SAT = 1 M1, only spreads configurable
                spreadBid: parseFloat(document.getElementById('btc-m1-spread-bid')?.value) || 0.5,
                spreadAsk: parseFloat(document.getElementById('btc-m1-spread-ask')?.value) || 0.5,
                limits: {
                    min: parseFloat(document.getElementById('btc-m1-min')?.value) || 0.0001,
                    max: parseFloat(document.getElementById('btc-m1-max')?.value) || 1.0,
                },
            },
            'usdc-m1': {
                enabled: document.getElementById('pair-usdc-m1-enabled')?.checked ?? true,
                spreadBid: parseFloat(document.getElementById('usdc-m1-spread-bid')?.value) || 0.5,
                spreadAsk: parseFloat(document.getElementById('usdc-m1-spread-ask')?.value) || 0.5,
                limits: {
                    min: parseFloat(document.getElementById('usdc-m1-min')?.value) || 10,
                    max: parseFloat(document.getElementById('usdc-m1-max')?.value) || 100000,
                },
            },
            'btc-usdc': {
                enabled: document.getElementById('pair-btc-usdc-enabled')?.checked ?? true,
            },
        },
        apiKeys: {
            binance: document.getElementById('api-key-binance')?.value || '',
            coingecko: document.getElementById('api-key-coingecko')?.value || '',
            kraken: document.getElementById('api-key-kraken')?.value || '',
        },
        auto: {
            claim: document.getElementById('auto-claim')?.checked ?? true,
            refund: document.getElementById('auto-refund')?.checked ?? true,
            rebalance: document.getElementById('auto-rebalance')?.checked ?? false,
        },
        rateRefreshInterval: parseInt(document.getElementById('rate-refresh-interval')?.value) || 10,
    };

    console.log('Saving LP config:', config);
    localStorage.setItem('lpConfig', JSON.stringify(config));
    alert('LP Configuration saved!');
}

function loadLPConfig() {
    const saved = localStorage.getItem('lpConfig');
    if (!saved) return;

    try {
        const config = JSON.parse(saved);

        // Load BTC/M1 config (fixed rate, only spreads)
        if (config.pairs?.['btc-m1']) {
            const p = config.pairs['btc-m1'];
            if (p.enabled !== undefined) document.getElementById('pair-btc-m1-enabled').checked = p.enabled;
            if (p.spreadBid !== undefined) document.getElementById('btc-m1-spread-bid').value = p.spreadBid;
            if (p.spreadAsk !== undefined) document.getElementById('btc-m1-spread-ask').value = p.spreadAsk;
            if (p.limits?.min !== undefined) document.getElementById('btc-m1-min').value = p.limits.min;
            if (p.limits?.max !== undefined) document.getElementById('btc-m1-max').value = p.limits.max;
        }

        // Load USDC/M1 config (bid/ask spreads)
        if (config.pairs?.['usdc-m1']) {
            const p = config.pairs['usdc-m1'];
            if (p.enabled !== undefined) document.getElementById('pair-usdc-m1-enabled').checked = p.enabled;
            if (p.spreadBid !== undefined) document.getElementById('usdc-m1-spread-bid').value = p.spreadBid;
            if (p.spreadAsk !== undefined) document.getElementById('usdc-m1-spread-ask').value = p.spreadAsk;
            if (p.limits?.min !== undefined) document.getElementById('usdc-m1-min').value = p.limits.min;
            if (p.limits?.max !== undefined) document.getElementById('usdc-m1-max').value = p.limits.max;
        }

        // Load BTC/USDC enabled
        if (config.pairs?.['btc-usdc']?.enabled !== undefined) {
            document.getElementById('pair-btc-usdc-enabled').checked = config.pairs['btc-usdc'].enabled;
        }

        // Load API keys
        if (config.apiKeys) {
            ['binance', 'coingecko', 'kraken'].forEach(src => {
                const input = document.getElementById(`api-key-${src}`);
                if (input && config.apiKeys[src]) input.value = config.apiKeys[src];
            });
        }

        // Load auto settings
        if (config.auto) {
            if (config.auto.claim !== undefined) document.getElementById('auto-claim').checked = config.auto.claim;
            if (config.auto.refund !== undefined) document.getElementById('auto-refund').checked = config.auto.refund;
            if (config.auto.rebalance !== undefined) document.getElementById('auto-rebalance').checked = config.auto.rebalance;
        }

        if (config.rateRefreshInterval) {
            document.getElementById('rate-refresh-interval').value = config.rateRefreshInterval;
        }

        console.log('LP config loaded from localStorage');
    } catch (e) {
        console.error('Failed to load LP config:', e);
    }
}

function resetLPConfig() {
    if (confirm('Reset all LP configuration to defaults?')) {
        localStorage.removeItem('lpConfig');
        location.reload();
    }
}

// Save ALL config (sources + spreads + limits) - local + server
async function saveAllConfig() {
    const btn = document.querySelector('.btn-save-all');
    const originalText = btn.textContent;
    btn.textContent = 'Sauvegarde...';
    btn.disabled = true;

    // Save sources locally
    saveSourcesConfig();

    // Save LP config locally
    saveLPConfig();

    // Push config to server for quotes/API
    await pushConfigToServer();

    // Refresh prices
    await refreshAllRates();

    // Collapse sources
    collapseAllSources();

    btn.textContent = 'Sauvegardé !';
    setTimeout(() => {
        btn.textContent = originalText;
        btn.disabled = false;
    }, 1500);
}

// Push LP config to server
async function pushConfigToServer() {
    const config = {
        pairs: {
            'BTC/M1': {
                enabled: document.getElementById('pair-btc-m1-enabled')?.checked ?? true,
                spread_bid: parseFloat(document.getElementById('btc-m1-spread-bid')?.value) || 0.5,
                spread_ask: parseFloat(document.getElementById('btc-m1-spread-ask')?.value) || 0.5,
                min: parseFloat(document.getElementById('btc-m1-min')?.value) || 0.0001,
                max: parseFloat(document.getElementById('btc-m1-max')?.value) || 1.0,
            },
            'USDC/M1': {
                enabled: document.getElementById('pair-usdc-m1-enabled')?.checked ?? true,
                spread_bid: parseFloat(document.getElementById('usdc-m1-spread-bid')?.value) || 0.5,
                spread_ask: parseFloat(document.getElementById('usdc-m1-spread-ask')?.value) || 0.5,
                rate: window.usdcm1Rate || 1309.0,  // Current rate from price feed
                min: parseFloat(document.getElementById('usdc-m1-min')?.value) || 10,
                max: parseFloat(document.getElementById('usdc-m1-max')?.value) || 100000,
            },
            'BTC/USDC': {
                enabled: document.getElementById('pair-btc-usdc-enabled')?.checked ?? true,
                min: parseFloat(document.getElementById('btc-m1-min')?.value) || 0.0001,
                max: parseFloat(document.getElementById('btc-m1-max')?.value) || 1.0,
            },
        },
        // Include confirmation config
        confirmations: getConfirmationConfig(),
    };

    try {
        const response = await fetch('/api/lp/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });

        if (response.ok) {
            console.log('[Config] Pushed to server successfully (incl. confirmations)');
        } else {
            console.error('[Config] Failed to push to server:', response.status);
        }
    } catch (e) {
        console.error('[Config] Error pushing to server:', e);
    }
}

// Reset ALL config
function resetAllConfig() {
    if (confirm('Réinitialiser toute la configuration LP ?')) {
        localStorage.removeItem('lpConfig');
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

async function refreshChainStatuses() {
    const data = await apiCall('/api/chains/status');
    if (!data || !data.chains) return;

    for (const [chain, status] of Object.entries(data.chains)) {
        if (chain === 'usdc') continue; // Skip USDC (no install)

        const installStatus = document.getElementById(`${chain}-install-status`);
        const installBtn = document.getElementById(`${chain}-install-btn`);
        const chainStatus = document.getElementById(`${chain}-chain-status`);

        if (!installStatus) continue;

        if (status.installed) {
            installStatus.className = 'install-status installed';
            installStatus.innerHTML = '<span class="status-icon">&#10003;</span><span>Installed</span>';
            if (installBtn) {
                installBtn.innerHTML = '<span class="btn-icon">&#10003;</span> Installed';
                installBtn.disabled = true;
            }

            // Update running status
            if (status.running && chainStatus) {
                // Start polling sync status
                pollSyncStatus(chain);
            } else if (chainStatus) {
                chainStatus.innerHTML = '<span class="status-dot disconnected"></span><span>Stopped</span>';
            }
        }
    }
}

// =============================================================================
// INIT
// =============================================================================

async function init() {
    console.log('[Dashboard] Initializing...');

    // Initialize M1 unit toggle UI
    updateUnitToggleUI();

    // Load saved config from localStorage
    loadChainConfig();
    loadLPConfig();
    loadSourcesConfig();

    // Setup LP config event listeners
    setupPairConfigListeners();

    // Load confirmation config from server
    await loadConfirmationConfig();

    // Check API status
    const connected = await checkApiStatus();

    // Refresh chain statuses (detect installed/running)
    await refreshChainStatuses();

    // Fetch initial rates and update previews
    await refreshAllRates();

    // Push loaded config to server (sync localStorage → server)
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
        if (activePage.id === 'page-overview') loadOverview();
        if (activePage.id === 'page-swaps') loadSwaps();
        if (activePage.id === 'page-chains') refreshChainStatuses();
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
