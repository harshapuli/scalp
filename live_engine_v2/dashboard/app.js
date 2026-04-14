const POLL_INTERVAL_MS = 2000;

const DOM = {
    statusDot: document.getElementById('connection-status-dot'),
    statusText: document.getElementById('connection-status-text'),
    globalPnl: document.getElementById('global-pnl'),
    
    whaleBias: document.getElementById('whale-bias-intent'),
    instFlowList: document.getElementById('institutional-flow-list'),
    
    statOpenPos: document.getElementById('stat-open-pos'),
    statTradesToday: document.getElementById('stat-trades-today'),
    statWinRate: document.getElementById('stat-win-rate'),
    statBlocks: document.getElementById('stat-blocks'),
    statExecs: document.getElementById('stat-execs'),
    
    activeHoldings: document.getElementById('active-holdings-list'),
    latestSignalsList: document.getElementById('latest-signals-list')
};

function updateConnectionState(isConnected) {
    if (isConnected) {
        DOM.statusDot.classList.add('connected');
        DOM.statusText.textContent = "CONNECTED TO API";
        DOM.statusText.style.color = "var(--profit-green)";
    } else {
        DOM.statusDot.classList.remove('connected');
        DOM.statusText.textContent = "DISCONNECTED";
        DOM.statusText.style.color = "var(--loss-red)";
    }
}

function formatDollars(val) {
    const sign = val >= 0 ? '+' : '-';
    return `${sign}$${Math.abs(val).toFixed(2)}`;
}

function renderDashboard(data) {
    if (data.error) {
        console.warn(data.error);
        return;
    }

    // Main Summary
    const summary = data.summary || {};
    DOM.statOpenPos.textContent = summary.open_positions || 0;
    DOM.statTradesToday.textContent = summary.trades_today || 0;
    DOM.statWinRate.textContent = `${summary.win_rate || 0}%`;
    
    const pnl = summary.daily_pnl || 0;
    DOM.globalPnl.textContent = formatDollars(pnl);
    if (pnl > 0) {
        DOM.globalPnl.className = 'value profit';
    } else if (pnl < 0) {
        DOM.globalPnl.className = 'value loss';
    } else {
        DOM.globalPnl.className = 'value';
    }

    // Stats
    const stats = data.stats || {};
    let blocks = 0;
    Object.keys(stats).forEach(k => {
        if (k.startsWith('blocked_')) blocks += stats[k];
    });
    DOM.statBlocks.textContent = blocks;
    DOM.statExecs.textContent = stats.trades_placed || 0;

    // Whale Intelligence
    const inst = data.institutional_state || {};
    if (inst.SPY && inst.SPY.whale_intent) {
        DOM.whaleBias.textContent = inst.SPY.whale_intent.state || 'NEUTRAL';
        if (inst.SPY.whale_intent.state === 'DISTRIBUTION') {
            DOM.whaleBias.style.color = "var(--loss-red)";
            DOM.whaleBias.style.background = "none";
            DOM.whaleBias.style.webkitTextFillColor = "var(--loss-red)";
        } else if (inst.SPY.whale_intent.state === 'ACCUMULATION') {
            DOM.whaleBias.style.color = "var(--profit-green)";
            DOM.whaleBias.style.background = "none";
            DOM.whaleBias.style.webkitTextFillColor = "var(--profit-green)";
        }
    }

    // Active Holdings
    const trades = data.active_trades || [];
    if (trades.length === 0) {
        DOM.activeHoldings.innerHTML = `
            <div class="placeholder-card">
                <p class="text-dim">Engine scanning for A+ Grade Setups...</p>
            </div>
        `;
    } else {
        DOM.activeHoldings.innerHTML = trades.map(t => {
            const isProfit = t.pnl >= 0;
            const pClass = isProfit ? 'win' : 'loss';
            const sign = isProfit ? '+' : '';
            return `
                <div class="holding-card ${pClass}">
                    <div class="hc-header">
                        <span class="hc-ticker">${t.ticker}</span>
                        <span class="hc-dir ${t.direction}">${t.direction}</span>
                    </div>
                    <div class="hc-metrics">
                        <span>Entry: $${t.entry_price.toFixed(2)}</span>
                        <span>TP: $${t.tp_price ? t.tp_price.toFixed(2) : 'N/A'}</span>
                    </div>
                    <div class="hc-header" style="margin-top: 10px; margin-bottom: 0;">
                        <span class="text-dim" style="font-size: 0.8rem">${t.strategy}</span>
                        <span class="hc-pnl ${isProfit ? 'green-txt' : 'red-txt'}">${sign}$${t.pnl.toFixed(2)}</span>
                    </div>
                </div>
            `;
        }).join('');
    }

    // Latest Scanner Signals
    const signals = data.latest_signals || [];
    if (signals.length > 0 && DOM.latestSignalsList) {
        DOM.latestSignalsList.innerHTML = signals.map(s => {
            const isCall = s.direction === 'CALL';
            const bgClass = isCall ? 'win' : 'loss';
            const isBlocked = s.status && s.status.includes('BLOCKED');
            const isGap = s.signal_type && s.signal_type.includes('gap');
            let outlineStyle = isGap ? 'border: 2px solid var(--profit-green); box-shadow: 0 0 10px rgba(0,255,100,0.2);' : '';
            if (isGap && !isCall) {
                outlineStyle = 'border: 2px solid var(--loss-red); box-shadow: 0 0 10px rgba(255,50,50,0.2);';
            }

            return `
                <div class="holding-card ${bgClass}" style="position: relative; ${outlineStyle}">
                    ${isBlocked 
                        ? '<div style="position:absolute; top:4px; right:4px; background:var(--loss-red); color:var(--bg-dark); font-size:0.6rem; font-weight:800; padding:2px 4px; border-radius:3px; z-index:1;">BLOCKED</div>' 
                        : '<div style="position:absolute; top:4px; right:4px; background:var(--profit-green); color:var(--bg-dark); font-size:0.6rem; font-weight:800; padding:2px 4px; border-radius:3px; z-index:1;">ACTIVE</div>'}
                    ${isGap 
                        ? '<div style="position:absolute; top:28px; right:4px; background:var(--panel-border); color:var(--text-light); font-size:0.65rem; font-weight:700; padding:2px 4px; border-radius:3px; z-index:1;">⚡ GAP SETUP</div>' 
                        : ''}
                    <div class="hc-header">
                        <span class="hc-ticker">${s.ticker}</span>
                        <span class="hc-dir ${s.direction}">${s.direction}</span>
                    </div>
                    <div style="font-size: 0.85rem; margin-top: 5px;">
                        <span>Strike: $${s.strike.toFixed(2)}</span> &bull; 
                        <span>Exp: ${s.expiry}</span>
                    </div>
                    <div style="margin-top:5px; font-size:0.75rem; color:var(--text-dim); display:flex; justify-content:space-between; overflow:hidden;">
                        <span>${s.occ}</span>
                        <span>${new Date(s.time * 1000).toLocaleTimeString()}</span>
                    </div>
                    <div style="font-size:0.65rem; color:${isBlocked ? 'var(--loss-red)' : 'var(--profit-green)'}; margin-top:4px;">${s.status} | ${s.signal_type || ''}</div>
                </div>
            `;
        }).join('');
    }
}

async function fetchState() {
    try {
        const response = await fetch('/api/live');
        if (!response.ok) throw new Error("Network response was not ok");
        const data = await response.json();
        updateConnectionState(true);
        renderDashboard(data);
    } catch (err) {
        console.error("Failed to fetch API state:", err);
        updateConnectionState(false);
    }
}

// Boot loop
fetchState();
setInterval(fetchState, POLL_INTERVAL_MS);
