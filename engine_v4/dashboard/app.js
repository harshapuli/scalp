document.addEventListener("DOMContentLoaded", () => {
    const confirmedContainer = document.getElementById("confirmed-container");
    const watchingContainer = document.getElementById("watching-container");
    const expiredContainer = document.getElementById("expired-container");
    const breakoutsContainer = document.getElementById("breakouts-container");
    const dynamicContainer = document.getElementById("dynamic-container");
    const positionsContainer = document.getElementById("positions-container");
    const gapsContainer = document.getElementById("gaps-container");
    const baselineContainer = document.getElementById("baseline-container");
    const regimeBadge = document.getElementById("macro-regime");
    const regimeText = document.getElementById("regime-text");
    const navConfirmed = document.getElementById("nav-count-confirmed");
    const navWatching = document.getElementById("nav-count-watching");
    const navExpired = document.getElementById("nav-count-expired");
    const navBreakouts = document.getElementById("nav-count-breakouts");
    const navDynamic = document.getElementById("nav-count-dynamic");
    const navPositions = document.getElementById("nav-count-positions");
    const navGaps = document.getElementById("nav-count-gaps");
    const navBaseline = document.getElementById("nav-count-baseline");
    const metaConfirmed = document.getElementById("meta-confirmed");
    const metaWatching = document.getElementById("meta-watching");
    const metaExpired = document.getElementById("meta-expired");
    const metaBreakouts = document.getElementById("meta-breakouts");
    const metaDynamic = document.getElementById("meta-dynamic");
    const metaPositions = document.getElementById("meta-positions");
    const metaGaps = document.getElementById("meta-gaps");
    const metaBaseline = document.getElementById("meta-baseline");

    let lastUpdateStr = "";
    let targetTime = 0;
    let timerInterval = null;

    // ---------- ROUTING ----------
    const VALID_PAGES = ["confirmed", "watching", "expired", "breakouts", "dynamic", "positions", "gaps", "baseline"];

    function pageFromHash() {
        const h = (location.hash || "").replace(/^#\/?/, "").toLowerCase();
        return VALID_PAGES.includes(h) ? h : "confirmed";
    }

    function activatePage(name) {
        document.querySelectorAll(".page-view").forEach(el => {
            el.classList.toggle("active-page", el.dataset.page === name);
        });
        document.querySelectorAll(".nav-links a").forEach(a => {
            a.classList.toggle("active", a.dataset.page === name);
        });
    }

    document.querySelectorAll(".nav-links a").forEach(a => {
        a.addEventListener("click", (e) => {
            const target = a.dataset.page;
            if (!target) return;
            location.hash = `#/${target}`;
            activatePage(target);
            e.preventDefault();
        });
    });
    window.addEventListener("hashchange", () => activatePage(pageFromHash()));
    activatePage(pageFromHash());

    // ---------- TIMERS ----------
    function fmtRelative(targetMs) {
        const diffMs = targetMs - Date.now();
        if (diffMs <= 0) return "expired";
        const m = Math.floor(diffMs / 60000);
        if (m < 60) return `${m}m`;
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    }

    function updateTimerDisplay() {
        if (!targetTime) return;
        const diff = Math.max(Math.floor((targetTime - Date.now()) / 1000), 0);
        const m = Math.floor(diff / 60);
        const s = diff % 60;
        const el = document.getElementById("countdown-text");
        el.innerText = "Next: " + m + ":" + s.toString().padStart(2, "0");
        if (diff === 0) el.innerText = "Scanning...";
    }

    function tickAllExpiries() {
        document.querySelectorAll("[data-expires-utc]").forEach(el => {
            const ts = parseInt(el.getAttribute("data-expires-utc"));
            if (ts) el.innerText = fmtRelative(ts);
        });
    }

    // ---------- DATA FETCH ----------
    const SCAN_INTERVAL_MS = 5 * 60 * 1000;  // mirror engine SCAN_INTERVAL_SECONDS

    // Scan window — must mirror v4_armada.py WINDOW_START / WINDOW_END.
    // Mon-Fri only, 6:00 AM PT to 1:30 PM PT.
    const WINDOW_START_MIN = 6 * 60;       // 6:00 AM PT
    const WINDOW_END_MIN = 13 * 60 + 30;   // 1:30 PM PT

    function nowInPacific() {
        // Returns {weekday: 0-6 (Mon=0...Sun=6), minutes: minutes-since-midnight, dateLabel: 'Mon 06:30 PT'}
        const fmt = new Intl.DateTimeFormat('en-US', {
            timeZone: 'America/Los_Angeles',
            weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
        });
        const parts = fmt.formatToParts(new Date());
        const get = t => parts.find(p => p.type === t)?.value || '';
        const weekdayStr = get('weekday');
        const hour = parseInt(get('hour'), 10);
        const minute = parseInt(get('minute'), 10);
        const map = {Sun:6, Mon:0, Tue:1, Wed:2, Thu:3, Fri:4, Sat:5};
        return {
            weekday: map[weekdayStr] ?? 0,
            minutes: hour * 60 + minute,
            label: `${weekdayStr} ${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')} PT`,
        };
    }

    function inScanWindow() {
        const t = nowInPacific();
        if (t.weekday >= 5) return false;  // weekend
        return t.minutes >= WINDOW_START_MIN && t.minutes <= WINDOW_END_MIN;
    }

    function minutesUntilNextOpen() {
        const t = nowInPacific();
        let dayOffset = 0;
        if (t.weekday >= 5) {
            // Sat → Mon (2 days), Sun → Mon (1 day)
            dayOffset = t.weekday === 5 ? 2 : 1;
        } else if (t.minutes >= WINDOW_END_MIN) {
            // Today's window closed → tomorrow (or Mon if Friday)
            dayOffset = (t.weekday === 4) ? 3 : 1;
        } else if (t.minutes < WINDOW_START_MIN) {
            // Before today's open
            dayOffset = 0;
        } else {
            return 0;  // currently inside window
        }
        const targetMinutes = dayOffset * 24 * 60 + WINDOW_START_MIN;
        return targetMinutes - t.minutes;
    }

    function fmtMinutes(mins) {
        if (mins < 60) return `${mins}m`;
        const h = Math.floor(mins / 60);
        const m = mins % 60;
        if (h < 24) return `${h}h ${m}m`;
        const d = Math.floor(h / 24);
        return `${d}d ${h % 24}h`;
    }

    // ---------- POSITIONS ----------
    function renderPositionCard(p) {
        const direction = p.direction || 'CALL';
        const status = p.status || 'UNKNOWN';
        const contractSymbol = p.contract_symbol || '?';
        const strike = p.strike;
        const expiration = p.expiration;

        // Parse expiration to a friendlier format
        let expDisplay = '—';
        if (expiration) {
            try {
                const d = new Date(expiration + 'T00:00:00Z');
                expDisplay = d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'});
            } catch (e) { expDisplay = expiration; }
        }

        const entryPrice = p.entry_price_filled ?? p.entry_price_estimate;
        const currentPremium = p.current_premium;
        const unrealPct = p.unrealized_pnl_pct;
        const realPct = p.realized_pnl_pct;
        const realUsd = p.realized_pnl_usd;
        const tpTarget = p.tp_premium_target;
        const slUnderlying = p.sl_underlying;
        const currentUnderlying = p.current_underlying;

        // Status styling
        let statusCls = 'status-position-open';
        let statusIcon = '🟢';
        if (status === 'PENDING_ENTRY') { statusCls = 'status-position-pending'; statusIcon = '⏳'; }
        else if (status === 'PENDING_EXIT') { statusCls = 'status-position-exiting'; statusIcon = '🔄'; }
        else if (status === 'CLOSED') { statusCls = 'status-position-closed'; statusIcon = '✓'; }
        else if (status === 'ENTRY_FAILED' || status === 'EXIT_FAILED') { statusCls = 'status-position-failed'; statusIcon = '❌'; }

        // P&L line (live or realized)
        let pnlLine = '';
        if (status === 'CLOSED' && realUsd !== null && realUsd !== undefined) {
            const cls = realUsd > 0 ? 'v-pos' : realUsd < 0 ? 'v-neg' : 'v-zero';
            pnlLine = `<div class="pos-pnl ${cls}">Closed P&L: $${realUsd.toFixed(0)} (${realPct > 0 ? '+' : ''}${realPct}%)</div>`;
        } else if (unrealPct !== null && unrealPct !== undefined) {
            const cls = unrealPct > 0 ? 'v-pos' : unrealPct < 0 ? 'v-neg' : 'v-zero';
            pnlLine = `<div class="pos-pnl ${cls}">Unrealized: ${unrealPct > 0 ? '+' : ''}${unrealPct}% · option $${currentPremium}</div>`;
        }

        return `
            <div class="signal-card position-card ${statusCls}">
                <div class="card-header">
                    <div class="ticker-group">
                        <div class="ticker-badge">${p.ticker}</div>
                        ${pillType(direction)}
                    </div>
                    <div class="pos-status">
                        <span class="pos-status-label">${statusIcon} ${status}</span>
                        <span class="pos-qty">${p.qty}x</span>
                    </div>
                </div>

                <div class="pos-contract-hero">
                    <div class="pos-strike-exp">
                        $${strike} ${direction} · ${expDisplay}
                    </div>
                    <div class="pos-contract-symbol">${contractSymbol}</div>
                </div>

                ${pnlLine}

                <div class="trade-grid">
                    <div class="trade-cell">
                        <span class="cell-label">Entry Fill</span>
                        <span class="cell-value">$${entryPrice?.toFixed ? entryPrice.toFixed(2) : entryPrice || '?'}</span>
                    </div>
                    <div class="trade-cell">
                        <span class="cell-label">Current Premium</span>
                        <span class="cell-value">${currentPremium ? '$' + currentPremium : '—'}</span>
                    </div>
                    <div class="trade-cell">
                        <span class="cell-label">TP Target</span>
                        <span class="cell-value">${tpTarget ? '$' + tpTarget : '—'}</span>
                    </div>
                    <div class="trade-cell">
                        <span class="cell-label">SL (underlying)</span>
                        <span class="cell-value">${slUnderlying ? '$' + slUnderlying : '—'}</span>
                    </div>
                    <div class="trade-cell">
                        <span class="cell-label">Underlying Now</span>
                        <span class="cell-value">${currentUnderlying ? '$' + currentUnderlying : '—'}</span>
                    </div>
                    <div class="trade-cell">
                        <span class="cell-label">Source</span>
                        <span class="cell-value">${p.signal_source || '?'}</span>
                    </div>
                </div>

                ${p.exit_reason ? `<div class="screener-note">Exit reason: ${p.exit_reason}</div>` : ''}
            </div>`;
    }

    async function fetchPositions() {
        try {
            const res = await fetch("/api/v4/positions");
            if (!res.ok) return;
            const data = await res.json();
            const positions = data.positions || [];
            // Sort: OPEN first, then PENDING, then CLOSED
            const order = { 'OPEN': 0, 'PENDING_ENTRY': 1, 'PENDING_EXIT': 2, 'CLOSED': 3, 'ENTRY_FAILED': 4, 'EXIT_FAILED': 5 };
            positions.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));

            const openCount = positions.filter(p => p.status === 'OPEN' || p.status === 'PENDING_ENTRY').length;
            navPositions.innerText = openCount;
            metaPositions.innerText = positions.length;
            navPositions.classList.toggle("has-items", openCount > 0);

            if (positions.length === 0) {
                positionsContainer.innerHTML = `<div class="empty-state"><p>No positions yet. The paper trader auto-enters on confirmed triggers during market hours.</p></div>`;
            } else {
                positionsContainer.innerHTML = positions.map(renderPositionCard).join("");
            }
        } catch (err) {
            // silent
        }
    }

    let lastBreakoutsScanDate = "";
    async function fetchBreakouts() {
        try {
            // Try the API endpoint first; fall back to direct file fetch
            let res = await fetch("/api/v4/breakouts");
            if (!res.ok) {
                // Fall back: direct fetch of the JSON file (server serves the dashboard dir, not parent)
                res = await fetch("/v4_preposition_watchlist.json");
            }
            if (!res.ok) {
                renderBreakouts({candidates: [], metadata: {}});
                return;
            }
            const data = await res.json();
            const md = data.metadata || {};
            if (md.scan_date !== lastBreakoutsScanDate) {
                lastBreakoutsScanDate = md.scan_date;
            }
            renderBreakouts(data);
        } catch (err) {
            // Silent — file probably doesn't exist yet (scanner not built/run)
            renderBreakouts({candidates: [], metadata: {}});
        }
    }

    // Per-page filter state for status pills (All / Buy / Approaching / Waiting)
    const statusFilters = { breakouts: 'all', dynamic: 'all' };
    // Cache the latest split so filter clicks can re-render without re-fetching
    let latestSplit = { megaCap: [], dynamic: [], md: {} };

    function matchesStatusFilter(candidate, filter) {
        const status = candidate.trigger_status || 'WAITING';
        switch (filter) {
            case 'buy': return status === 'CONFIRMED' || status === 'TRIGGERED';
            case 'approaching': return status === 'APPROACHING';
            case 'waiting': return status === 'WAITING';
            case 'all':
            default: return true;
        }
    }

    function updateFilterCounts(pageKey, candidates) {
        const counts = { all: candidates.length, buy: 0, approaching: 0, waiting: 0 };
        for (const c of candidates) {
            const s = c.trigger_status || 'WAITING';
            if (s === 'CONFIRMED' || s === 'TRIGGERED') counts.buy++;
            else if (s === 'APPROACHING') counts.approaching++;
            else if (s === 'WAITING') counts.waiting++;
        }
        for (const f of ['all', 'buy', 'approaching', 'waiting']) {
            const el = document.getElementById(`filter-count-${pageKey}-${f}`);
            if (el) el.innerText = counts[f];
        }
    }

    function renderTierPage(pageKey, candidates, container, navEl, metaEl, emptyMsg) {
        navEl.innerText = candidates.length;
        metaEl.innerText = candidates.length;
        navEl.classList.toggle("has-items", candidates.length > 0);
        updateFilterCounts(pageKey, candidates);

        const filter = statusFilters[pageKey] || 'all';
        const filtered = candidates.filter(c => matchesStatusFilter(c, filter));

        if (filtered.length === 0) {
            const noteAll = candidates.length === 0 ? emptyMsg : `No candidates match "${filter}" filter (${candidates.length} total — try "All").`;
            container.innerHTML = `<div class="empty-state"><p>${noteAll}</p></div>`;
        } else {
            container.innerHTML = filtered.map(renderBreakoutCard).join("");
        }
    }

    function renderBreakouts(data) {
        const all = (data.candidates || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
        const megaCap = all.filter(c => (c.tier || 1) === 1);
        const dynamic = all.filter(c => c.tier === 2);
        const md = data.metadata || {};
        latestSplit = { megaCap, dynamic, md };

        const megaCapEmpty = md.scan_date
            ? `No mega-cap candidates from last scan (${md.scan_date}). Scanner evaluated the universe but nothing met threshold today.`
            : `No mega-cap candidates yet. The scanner runs once daily after close on 15 mega-caps.`;
        renderTierPage('breakouts', megaCap, breakoutsContainer, navBreakouts, metaBreakouts, megaCapEmpty);

        const dynamicEmpty = md.scan_date
            ? `No dynamic candidates from last scan (${md.scan_date}). Flow-ranked universe was pulled but nothing passed ≥40.`
            : `No dynamic candidates yet. Next scan (after close) will pull top ~35 tickers by net options premium and score them.`;
        renderTierPage('dynamic', dynamic, dynamicContainer, navDynamic, metaDynamic, dynamicEmpty);
    }

    // Filter button click handlers — re-render from cached data
    document.querySelectorAll('.status-filters').forEach(group => {
        const pageKey = group.getAttribute('data-page-target');
        group.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const filter = btn.getAttribute('data-filter');
                statusFilters[pageKey] = filter;
                // Toggle active class within this group only
                group.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                // Re-render the affected page from cached data
                if (pageKey === 'breakouts') {
                    const empty = latestSplit.md.scan_date
                        ? `No mega-cap candidates from last scan.`
                        : `No mega-cap candidates yet.`;
                    renderTierPage('breakouts', latestSplit.megaCap, breakoutsContainer, navBreakouts, metaBreakouts, empty);
                } else if (pageKey === 'dynamic') {
                    const empty = latestSplit.md.scan_date
                        ? `No dynamic candidates from last scan.`
                        : `No dynamic candidates yet.`;
                    renderTierPage('dynamic', latestSplit.dynamic, dynamicContainer, navDynamic, metaDynamic, empty);
                }
            });
        });
    });

    async function fetchSignals() {
        try {
            const res = await fetch("/api/v4/signals");
            if (!res.ok) return;
            const data = await res.json();
            const md = data.metadata || {};
            const el = document.getElementById("countdown-text");

            // Step 1: Are we even inside the daily scan window?
            if (!inScanWindow()) {
                targetTime = 0;
                if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
                const mins = minutesUntilNextOpen();
                el.innerText = `Closed · opens in ${fmtMinutes(mins)}`;
            } else {
                // Inside window — use engine-emitted next_run or fallback
                let nextRunMs = null;
                if (md.next_run) nextRunMs = new Date(md.next_run).getTime();
                else if (md.last_updated) nextRunMs = new Date(md.last_updated).getTime() + SCAN_INTERVAL_MS;

                if (nextRunMs && nextRunMs > Date.now() - SCAN_INTERVAL_MS) {
                    targetTime = nextRunMs;
                    if (!timerInterval) timerInterval = setInterval(updateTimerDisplay, 1000);
                } else if (md.last_updated) {
                    targetTime = 0;
                    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
                    el.innerText = "Daemon idle";
                } else {
                    targetTime = 0;
                    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
                    el.innerText = "Awaiting first scan";
                }
            }

            if (md.last_updated !== lastUpdateStr) {
                lastUpdateStr = md.last_updated;
                renderDashboard(data);
            }
        } catch (err) {
            console.error("Dashboard sync error:", err);
        }
    }

    function setRegime(regime) {
        regimeText.innerText = regime || "—";
        const dot = regimeBadge.querySelector(".dot");
        dot.className = "dot pulse";
        if (regime === "TREND_UP") dot.classList.add("bull");
        else if (regime === "TREND_DOWN") dot.classList.add("bear");
        else dot.classList.add("warning");
    }

    // ---------- CARD HELPERS ----------
    const pillType = t => `<span class="type-badge ${t === "CALL" ? "type-call" : "type-put"}">${t}</span>`;
    const pillPath = p => p ? `<span class="path-badge ${p === "BREAKOUT" ? "path-breakout" : "path-pullback"}">${p}</span>` : "";

    function fmtScore(s) {
        const n = parseFloat(s) || 0;
        let cls = "score-mid";
        if (n >= 70) cls = "score-high";
        else if (n < 50) cls = "score-low";
        return `<div class="score-badge ${cls}">${n.toFixed(1)}<span>/100</span></div>`;
    }

    function fmtMatrix(m) {
        if (!m) return "";
        const cell = (label, val) => {
            if (val === undefined || val === null) return "";
            const n = parseFloat(val);
            const cls = n > 0 ? "v-pos" : n < 0 ? "v-neg" : "v-zero";
            const sign = n > 0 ? "+" : "";
            return `<div class="matrix-cell"><span class="matrix-label">${label}</span><span class="matrix-value ${cls}">${sign}${n.toFixed(0)}</span></div>`;
        };
        return `
            <div class="score-matrix">
                ${cell("Premium", m.persistence)}
                ${cell("SMC", m.smc)}
                ${cell("Darkpool", m.dp)}
                ${cell("Gamma", m.greek)}
                ${cell("IV pen.", m.iv)}
                ${cell("Open+TOD", (m.open_conf || 0) + (m.tod || 0))}
                ${m.ask_dom !== undefined ? `<div class="matrix-cell"><span class="matrix-label">Ask Dom</span><span class="matrix-value">${(m.ask_dom * 100).toFixed(0)}%</span></div>` : ""}
            </div>`;
    }

    // ---------- CONFIRMED CARD ----------
    function renderConfirmedCard(sig) {
        const ep = sig.Exit_Protocol || {};
        const sizeMult = sig.Size_Multiplier ?? ep.Size_Mult ?? 1.0;
        const path = ep.Path || (sig.Status.includes("BREAKOUT") ? "BREAKOUT" : "PULLBACK");
        const sizePct = (sizeMult * 100).toFixed(0);

        // Contract info — may be on the signal directly or nested in exit_protocol
        const strike = sig.Strike ?? ep.Strike;
        const expiration = sig.Expiration ?? ep.Expiration;
        const contractSym = sig.Contract_Symbol ?? ep.Contract_Symbol;
        let expDisplay = '—';
        if (expiration) {
            try {
                const d = new Date(expiration + 'T00:00:00Z');
                expDisplay = d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'});
            } catch (e) { expDisplay = expiration; }
        }
        const hasContract = strike && expiration;

        // Suggested strike (always shown — uses spot if available, falls back to SL ± 2%)
        const slNum = parseFloat(ep.SL);
        const proxySpot = ep.Spot_At_Watch || (slNum && !isNaN(slNum) ? slNum * 1.02 : null);
        const suggestedStrike = suggestStrike(proxySpot, sig.Type);
        const suggestedDte = sig.DTE || 14;

        return `
            <div class="signal-card confirmed-card">
                <div class="card-header">
                    <div class="ticker-group">
                        <div class="ticker-badge">${sig.Ticker}</div>
                        ${pillType(sig.Type)}
                        ${pillPath(path)}
                        <span class="dte-chip">${sig.DTE}DTE</span>
                    </div>
                    ${fmtScore(sig.Confidence)}
                </div>

                <div class="entry-banner">
                    <span class="entry-icon">⚡</span>
                    <span class="entry-action">${sig._age_minutes !== undefined ? 'FIRED ' + sig._age_minutes + ' MIN AGO' : 'EXECUTE NOW'}</span>
                    ${sig.Entry_Reason ? `<span class="entry-detail">${sig.Entry_Reason}</span>` : ""}
                </div>

                ${hasContract ? `
                <div class="contract-suggest">
                    <div class="contract-main">Buy: <strong>$${strike} ${sig.Type} · ${expDisplay}</strong></div>
                    ${contractSym ? `<div class="contract-occ">${contractSym}</div>` : ''}
                </div>
                ` : strikeBlock(suggestedStrike, suggestedDte, sig.Type)}

                <div class="trade-grid">
                    <div class="trade-cell"><span class="cell-label">Stop Loss</span><span class="cell-value">${ep.SL || "—"}</span></div>
                    <div class="trade-cell"><span class="cell-label">Take Profit</span><span class="cell-value">${ep.TP || "—"}</span></div>
                    <div class="trade-cell"><span class="cell-label">Time Stop</span><span class="cell-value">${ep.TIME_STOP || "—"}</span></div>
                    <div class="trade-cell"><span class="cell-label">Size</span><span class="cell-value size-value">${sizePct}%</span></div>
                </div>

                ${fmtMatrix(sig.Score_Matrix)}
                ${sig.Screener_Logic ? `<div class="screener-note">${sig.Screener_Logic}</div>` : ""}
            </div>`;
    }

    // ---------- WATCHING CARD ----------
    function renderWatchingCard(sig) {
        const ep = sig.Exit_Protocol || {};
        const path = ep.Path || (sig.Status.includes("BREAKOUT") ? "BREAKOUT" : "PULLBACK");
        const zoneStr = (ep.Watch_Zone_Low !== undefined && ep.Watch_Zone_High !== undefined)
            ? `${ep.Watch_Zone_Low.toFixed(2)} – ${ep.Watch_Zone_High.toFixed(2)}` : "—";
        const expiresUtc = ep.Expires_UTC ? new Date(ep.Expires_UTC).getTime() : null;
        const createdUtc = ep.Created_UTC ? new Date(ep.Created_UTC).getTime() : null;
        const sizePct = ((ep.Size_Mult ?? 1.0) * 100).toFixed(0);
        const triggerLabel = path === "BREAKOUT" ? "Body close + retest of level" : "Rejection wick into FVG zone";

        // Contract suggestion (same as confirmed)
        const strike = sig.Strike ?? ep.Strike;
        const expiration = sig.Expiration ?? ep.Expiration;
        const contractSym = sig.Contract_Symbol ?? ep.Contract_Symbol;
        let expDisplay = '—';
        if (expiration) {
            try {
                const d = new Date(expiration + 'T00:00:00Z');
                expDisplay = d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'});
            } catch (e) { expDisplay = expiration; }
        }
        const hasContract = strike && expiration;

        return `
            <div class="signal-card watching-card">
                <div class="card-header">
                    <div class="ticker-group">
                        <div class="ticker-badge">${sig.Ticker}</div>
                        ${pillType(sig.Type)}
                        ${pillPath(path)}
                        <span class="dte-chip">${sig.DTE}DTE</span>
                    </div>
                    ${fmtScore(sig.Confidence)}
                </div>

                <div class="watch-banner">
                    <span class="watch-icon">👁️</span>
                    <span class="watch-action">QUEUED FOR ENTRY</span>
                    <span class="watch-detail">${triggerLabel}</span>
                </div>

                ${hasContract ? `
                <div class="contract-suggest">
                    <div class="contract-main">If triggers: <strong>$${strike} ${sig.Type} · ${expDisplay}</strong></div>
                    ${contractSym ? `<div class="contract-occ">${contractSym}</div>` : ''}
                </div>
                ` : ''}

                <div class="trade-grid">
                    <div class="trade-cell"><span class="cell-label">Watch Zone</span><span class="cell-value">${zoneStr}</span></div>
                    <div class="trade-cell"><span class="cell-label">Spot @ Watch</span><span class="cell-value">${ep.Spot_At_Watch !== undefined ? ep.Spot_At_Watch.toFixed(2) : "—"}</span></div>
                    <div class="trade-cell"><span class="cell-label">Planned SL</span><span class="cell-value">${ep.SL || "—"}</span></div>
                    <div class="trade-cell"><span class="cell-label">Planned Size</span><span class="cell-value size-value">${sizePct}%</span></div>
                    <div class="trade-cell"><span class="cell-label">Expires In</span><span class="cell-value expiry-countdown" data-expires-utc="${expiresUtc || ''}">${expiresUtc ? fmtRelative(expiresUtc) : "—"}</span></div>
                    <div class="trade-cell"><span class="cell-label">Watch Created</span><span class="cell-value">${createdUtc ? new Date(createdUtc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : "—"}</span></div>
                </div>

                ${fmtMatrix(sig.Score_Matrix)}
                ${sig.Screener_Logic ? `<div class="screener-note">${sig.Screener_Logic}</div>` : ""}
            </div>`;
    }

    // ---------- BREAKOUT CANDIDATE CARD ----------
    function signalPill(label, lit, score) {
        const cls = lit ? "signal-pill lit" : "signal-pill dim";
        const scoreStr = (lit && score !== undefined) ? ` +${score}` : "";
        return `<span class="${cls}">${label}${scoreStr}</span>`;
    }

    // Shared helper — suggest a slightly-OTM standard option strike from a price + direction
    function suggestStrike(price, direction) {
        if (!price || isNaN(price)) return null;
        const target = direction === 'CALL' ? price * 1.02 : price * 0.98;
        let inc;
        if (price < 25) inc = 0.5;
        else if (price < 50) inc = 1;
        else if (price < 200) inc = 2.5;
        else if (price < 500) inc = 5;
        else inc = 10;
        return Math.round(target / inc) * inc;
    }

    function strikeBlock(strike, dte, direction) {
        if (!strike) return '';
        const dteMid = dte || 21;
        return `
            <div class="bk-strike-suggest">
                <span class="bk-strike-label">Suggest:</span>
                <strong>$${strike} ${direction}</strong>
                <span class="bk-strike-dte">~${dteMid} DTE</span>
            </div>`;
    }

    const STATUS_DISPLAY = {
        WAITING:     { icon: "⏳", label: "Waiting", cls: "status-waiting" },
        APPROACHING: { icon: "🟡", label: "Approaching", cls: "status-approaching" },
        TRIGGERED:   { icon: "🚨", label: "TRIGGERED", cls: "status-triggered" },
        CONFIRMED:   { icon: "🟢", label: "BUY NOW", cls: "status-confirmed" },
        FAILED:      { icon: "❌", label: "Failed", cls: "status-failed" },
    };

    function renderBreakoutCard(c) {
        const sig = c.signals || {};
        const direction = c.direction || "CALL";
        const dteRange = (c.suggested_dte_min && c.suggested_dte_max)
            ? `${c.suggested_dte_min}–${c.suggested_dte_max}DTE`
            : "—";
        const lvls = c.key_levels || {};
        const triggerPrice = lvls.trigger_above || lvls.compression_high;
        const stopPrice = lvls.stop_below || lvls.compression_low;
        const arrow = direction === "CALL" ? "↑" : "↓";
        const verb = direction === "CALL" ? "BREAKS ABOVE" : "BREAKS BELOW";

        // Status from patrol's trigger monitor (defaults to WAITING if patrol hasn't run yet)
        const status = c.trigger_status || "WAITING";
        const sd = STATUS_DISPLAY[status] || STATUS_DISPLAY.WAITING;
        const spot = c.current_spot;
        const distPct = c.distance_to_trigger_pct;

        // Trigger fired info — shown prominently when status is BUY-ish
        const isBuyState = status === 'CONFIRMED' || status === 'TRIGGERED';
        const trigUtc = c.triggered_at_utc;
        const trigPrice = c.triggered_at_price;
        let firedTimeStr = '';
        let minutesAgo = null;
        if (trigUtc) {
            try {
                const trigDate = new Date(trigUtc);
                firedTimeStr = trigDate.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles'}) + ' PT';
                minutesAgo = Math.floor((Date.now() - trigDate.getTime()) / 60000);
            } catch (e) {}
        }

        const suggestedStrike = suggestStrike(triggerPrice, direction);
        const dteMid = c.suggested_dte_min && c.suggested_dte_max
            ? Math.round((c.suggested_dte_min + c.suggested_dte_max) / 2)
            : 21;

        // Target price — projected underlying move after breakout (5% extension)
        // Premium-based TP (option will gain ~30-50% if underlying moves 5% on a CALL)
        const targetPrice = triggerPrice
            ? (direction === 'CALL' ? triggerPrice * 1.05 : triggerPrice * 0.95)
            : null;
        const targetPctMove = 5.0;

        // Limit order entry — price to place buy-stop-limit on underlying
        // (slightly above breakout trigger to catch the cross with minimal slippage)
        const limitEntry = triggerPrice
            ? (direction === 'CALL' ? triggerPrice * 1.002 : triggerPrice * 0.998)
            : null;

        // Rough estimate of OPTION premium for slightly-OTM call near 14-30 DTE.
        // Heuristic: premium ≈ stock × 0.03 × sqrt(DTE/21). Liquid mega-caps usually
        // come in 0.025 (low IV) – 0.05 (high IV) per dollar of stock. Tells the user
        // a ballpark — they verify the actual ask in their broker.
        const estPremium = (triggerPrice && dteMid)
            ? Math.round(triggerPrice * 0.03 * Math.sqrt(dteMid / 21) * 100) / 100
            : null;

        // Build a clean reasons list from lit signals only
        const reasons = [];
        if (sig.persistent_flow && sig.persistent_flow.score > 0) {
            const m = (sig.persistent_flow.net_call_premium_avg || 0) / 1e6;
            const days = sig.persistent_flow.days_with_data || 5;
            reasons.push(`Persistent ${direction} flow — $${Math.abs(m).toFixed(0)}M/day for ${days} days`);
        }
        if (sig.darkpool_accumulation && sig.darkpool_accumulation.score > 0) {
            const dp = sig.darkpool_accumulation;
            reasons.push(`Dark pool buying at $${dp.cluster_price} (${dp.validation_score}/4 confirmed)`);
        }
        if (sig.sector_strength && sig.sector_strength.score > 0) {
            const op = sig.sector_strength.outperformance_pct;
            reasons.push(`Outperforming SPY by ${op > 0 ? '+' : ''}${op}% over 5 days`);
        }
        if (sig.compression && sig.compression.score > 0) {
            reasons.push(`Price coiling — ${(sig.compression.range_pct * 100).toFixed(1)}% range over ${sig.compression.days} days`);
        }
        const litCount = reasons.length;
        const sizeNote = c.score >= 60 ? "Normal size" : c.score >= 50 ? "Smaller size" : "Smallest size";

        // Live spot vs trigger line
        const liveLine = (spot !== undefined && distPct !== undefined)
            ? `<div class="bk-live">Now $${spot} · ${distPct > 0 ? distPct.toFixed(2) + '% to trigger' : Math.abs(distPct).toFixed(2) + '% past trigger'}</div>`
            : "";

        // Triggered_at info if we have it
        const trigInfo = (c.triggered_at_utc && (status === 'TRIGGERED' || status === 'CONFIRMED'))
            ? `<div class="bk-trig-info">Triggered at $${c.triggered_at_price} · ${new Date(c.triggered_at_utc).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</div>`
            : "";

        // Build the trigger-fired banner shown only on BUY/TRIGGERED state
        const triggerBanner = isBuyState && trigUtc ? `
            <div class="bk-fired-banner">
                <span class="bk-fired-icon">🚨</span>
                <span class="bk-fired-text">Triggered ${firedTimeStr}${minutesAgo !== null ? ' · ' + minutesAgo + ' min ago' : ''}</span>
                ${trigPrice ? `<span class="bk-fired-price">@ $${trigPrice}</span>` : ''}
            </div>` : '';

        // Suggested option contract — slightly OTM strike at suggested DTE midpoint
        const breakoutStrikeBlock = strikeBlock(suggestedStrike, dteMid, direction);

        return `
            <div class="signal-card breakout-card simple ${sd.cls}">
                <div class="bk-header">
                    <div class="bk-ticker-group">
                        <div class="ticker-badge">${c.ticker}</div>
                        ${pillType(direction)}
                    </div>
                    <div class="bk-status">
                        <span class="bk-status-label">${sd.icon} ${sd.label}</span>
                        <span class="bk-score">${c.score}/100</span>
                    </div>
                </div>

                <div class="bk-instructions">
                    <div class="bk-instr-step">
                        <span class="bk-instr-num">1</span>
                        <div class="bk-instr-body">
                            <div class="bk-instr-label">Place buy-stop on ${c.ticker} stock @</div>
                            <div class="bk-instr-value">$${limitEntry ? limitEntry.toFixed(2) : triggerPrice}</div>
                            <div class="bk-instr-sub">Fires when ${c.ticker} stock crosses this level</div>
                        </div>
                    </div>
                    <div class="bk-instr-step">
                        <span class="bk-instr-num">2</span>
                        <div class="bk-instr-body">
                            <div class="bk-instr-label">Once filled, buy this option contract</div>
                            <div class="bk-instr-value bk-instr-option">${suggestedStrike ? `$${suggestedStrike} ${direction}` : '—'} <span class="bk-instr-dte">~${dteMid} DTE</span></div>
                            <div class="bk-instr-sub">~2% OTM strike, monthly expiration</div>
                        </div>
                    </div>
                    ${estPremium ? `
                    <div class="bk-instr-step">
                        <span class="bk-instr-num">3</span>
                        <div class="bk-instr-body">
                            <div class="bk-instr-label">Limit price for the option</div>
                            <div class="bk-instr-value bk-instr-premium">~$${estPremium.toFixed(2)} <span class="bk-instr-dte">per share</span></div>
                            <div class="bk-instr-sub">Estimate — check broker's ask price. Place limit at ask or +$0.10. One contract = 100 shares = ~$${(estPremium * 100).toFixed(0)} total cost.</div>
                        </div>
                    </div>` : ''}
                    ${liveLine}
                    ${trigInfo}
                </div>

                <div class="bk-rules-grid bk-rules-3">
                    <div class="bk-rule-cell bk-rule-stop">
                        <span class="bk-rule-label">Stop (stock)</span>
                        <span class="bk-rule-value">$${stopPrice}</span>
                    </div>
                    <div class="bk-rule-cell bk-rule-target">
                        <span class="bk-rule-label">Target (stock)</span>
                        <span class="bk-rule-value">${targetPrice ? '$' + targetPrice.toFixed(2) : '—'}</span>
                        ${targetPrice ? `<span class="bk-rule-sub">+${targetPctMove}% · ~+30-50% prem</span>` : ''}
                    </div>
                    <div class="bk-rule-cell">
                        <span class="bk-rule-label">Size</span>
                        <span class="bk-rule-value bk-size">${sizeNote}</span>
                    </div>
                </div>

                <details class="bk-evidence">
                    <summary>Why (${litCount} of 6 signals)</summary>
                    <ul class="bk-reasons">
                        ${reasons.map(r => `<li>${r}</li>`).join("")}
                    </ul>
                </details>
            </div>`;
    }

    function renderExpiredCard(sig) {
        const ep = sig.Exit_Protocol || {};
        const path = ep.Path || (sig.Status.includes("BREAKOUT") ? "BREAKOUT" : "PULLBACK");
        return `
            <div class="signal-card expired-card">
                <div class="card-header compact">
                    <div class="ticker-group">
                        <div class="ticker-badge">${sig.Ticker}</div>
                        ${pillType(sig.Type)}
                        ${pillPath(path)}
                    </div>
                    <div class="expired-meta">
                        <span class="score-mini">${parseFloat(sig.Confidence).toFixed(1)}</span>
                        <span class="expired-tag">expired · no retest</span>
                    </div>
                </div>
            </div>`;
    }

    // ---------- MAIN RENDER ----------
    function renderDashboard(data) {
        setRegime(data.metadata && data.metadata.regime);
        const signals = data.signals || [];
        const confirmed = signals.filter(s => s.Status && s.Status.startsWith("TRIGGER_"));
        const watching = signals.filter(s => s.Status === "WATCH_PULLBACK" || s.Status === "WATCH_BREAKOUT");
        const expired = signals.filter(s => s.Status === "WATCH_EXPIRED");

        // Counts in nav + page meta
        navConfirmed.innerText = confirmed.length;
        navWatching.innerText = watching.length;
        navExpired.innerText = expired.length;
        metaConfirmed.innerText = confirmed.length;
        metaWatching.innerText = watching.length;
        metaExpired.innerText = expired.length;

        // Visual emphasis when items exist
        navConfirmed.classList.toggle("has-items", confirmed.length > 0);
        navWatching.classList.toggle("has-items", watching.length > 0);
        navExpired.classList.toggle("has-items", expired.length > 0);

        confirmedContainer.innerHTML = confirmed.length === 0
            ? `<div class="empty-state empty-confirmed"><p>No confirmed entries. Engine fires when watched tickers retest their FVG/OB zone with a rejection wick.</p></div>`
            : confirmed.map(renderConfirmedCard).join("");

        watchingContainer.innerHTML = watching.length === 0
            ? `<div class="empty-state empty-watching"><p>No active watches. Engine queues tickers when flow + structure align above threshold.</p></div>`
            : watching.map(renderWatchingCard).join("");

        expiredContainer.innerHTML = expired.length === 0
            ? `<div class="empty-state"><p>No expired watches yet.</p></div>`
            : expired.map(renderExpiredCard).join("");
    }

    // ---------- GAPS (standalone gap-up / gap-down strategy) ----------
    function renderGapCard(g) {
        const dirCls = g.direction === 'CALL' ? 'gap-call' : 'gap-put';
        const arrow = g.direction === 'CALL' ? '📈' : '📉';
        const sourceTag = g.source === 'mega_cap' ? 'MEGA' : 'DYN';
        const sign = g.gap_pct >= 0 ? '+' : '';
        return `
        <div class="signal-card ${dirCls}">
            <div class="card-head">
                <div>
                    <span class="signal-ticker">${arrow} ${g.ticker}</span>
                    <span class="signal-direction ${g.direction === 'CALL' ? 'dir-call' : 'dir-put'}">${g.direction}</span>
                    <span class="signal-tier">${sourceTag}</span>
                </div>
                <div class="signal-score">${g.score} pts</div>
            </div>
            <div class="card-body">
                <div class="kvrow"><span>Gap</span><strong>${sign}${g.gap_pct.toFixed(2)}%</strong></div>
                <div class="kvrow"><span>Spot vs open</span><strong>${g.vs_gap_pct >= 0 ? '+' : ''}${g.vs_gap_pct.toFixed(2)}%</strong></div>
                <div class="kvrow"><span>Volume</span><strong>${g.vol_ratio.toFixed(1)}× avg</strong></div>
                <div class="kvrow"><span>Suggested DTE</span><strong>${g.suggested_dte_min}-${g.suggested_dte_max}</strong></div>
                <div class="signal-narrative">${g.narrative}</div>
            </div>
        </div>`;
    }

    async function fetchGaps() {
        try {
            const r = await fetch('/api/v4/gaps');
            if (!r.ok) return;
            const data = await r.json();
            const calls = data.gap_calls || [];
            const puts = data.gap_puts || [];
            const total = calls.length + puts.length;
            if (navGaps) navGaps.innerText = total;
            if (metaGaps) metaGaps.innerText = total;
            if (!gapsContainer) return;
            if (total === 0) {
                gapsContainer.innerHTML = '<div class="empty-state"><p>No gap setups right now. The gap scanner runs daily and flags names that gapped overnight AND are still holding the gap during the session.</p></div>';
                return;
            }
            let html = '';
            if (calls.length) {
                html += `<h2 class="section-heading">📈 Gap Up — ${calls.length} CALL setups</h2>`;
                html += calls.map(renderGapCard).join('');
            }
            if (puts.length) {
                html += `<h2 class="section-heading">📉 Gap Down — ${puts.length} PUT setups</h2>`;
                html += puts.map(renderGapCard).join('');
            }
            gapsContainer.innerHTML = html;
        } catch (e) {
            // Gap scanner output may not exist yet — fail silently
        }
    }

    // ---------- BASELINE A/B (original strategy without Phase 1-5 enhancements) ----------
    // Tracks counts of latest enhanced vs baseline signals so user can see what each engine
    // would have triggered today. Both run in shadow mode each pipeline cycle.
    let lastEnhancedCount = 0;
    let lastBaselineCount = 0;

    function renderBaselineCard(s) {
        const dirCls = s.Type === 'CALL' ? 'dir-call' : (s.Type === 'PUT' ? 'dir-put' : '');
        return `
        <div class="signal-card baseline-card">
            <div class="card-head">
                <div>
                    <span class="signal-ticker">${s.Ticker}</span>
                    <span class="signal-direction ${dirCls}">${s.Type || '?'}</span>
                    <span class="signal-status">${s.Status}</span>
                </div>
                <div class="signal-score">${s.Confidence || 0} pts</div>
            </div>
            <div class="card-body">
                <div class="signal-narrative">${s.Screener_Logic || ''}</div>
            </div>
        </div>`;
    }

    async function fetchBaseline() {
        try {
            const r = await fetch('/api/v4/signals_baseline');
            if (!r.ok) {
                if (baselineContainer) baselineContainer.innerHTML = '<div class="empty-state"><p>Baseline engine has not run yet. First cycle Monday at 6 AM PT.</p></div>';
                return;
            }
            const data = await r.json();
            const sigs = data.signals || [];
            // Show only WATCH/TRIGGER (actionable) — rejections live in the ledger
            const actionable = sigs.filter(s => s.Status && (s.Status.startsWith('WATCH_') || s.Status.startsWith('TRIGGER_')));
            lastBaselineCount = actionable.length;
            if (navBaseline) navBaseline.innerText = lastBaselineCount;
            if (metaBaseline) metaBaseline.innerText = lastBaselineCount;

            // Update A/B comparison summary
            const summary = document.getElementById('ab-counts');
            if (summary) {
                const delta = lastBaselineCount - lastEnhancedCount;
                const arrow = delta > 0 ? `+${delta} more` : (delta < 0 ? `${delta} fewer` : 'same count');
                summary.innerHTML = `<strong>Today's count:</strong> ENHANCED engine: <strong>${lastEnhancedCount}</strong> actionable signals · BASELINE engine: <strong>${lastBaselineCount}</strong> · Baseline shows ${arrow} signals than Enhanced`;
            }

            if (!baselineContainer) return;
            if (actionable.length === 0) {
                baselineContainer.innerHTML = '<div class="empty-state"><p>Baseline engine ran but emitted no actionable signals this cycle.</p></div>';
                return;
            }
            baselineContainer.innerHTML = actionable.map(renderBaselineCard).join('');
        } catch (e) {
            // Silent fail — baseline output may not exist yet
        }
    }

    // Hook enhanced count tracker into existing fetchSignals
    const origFetchSignals = fetchSignals;
    fetchSignals = async function() {
        await origFetchSignals();
        try {
            const r = await fetch('/api/v4/signals');
            if (r.ok) {
                const data = await r.json();
                const sigs = data.signals || [];
                lastEnhancedCount = sigs.filter(s => s.Status && (s.Status.startsWith('WATCH_') || s.Status.startsWith('TRIGGER_'))).length;
            }
        } catch (e) {}
    };

    setInterval(fetchSignals, 2000);
    setInterval(tickAllExpiries, 1000);
    setInterval(fetchBreakouts, 5000);
    setInterval(fetchPositions, 5000);  // live P&L on positions page
    setInterval(fetchGaps, 10000);      // gap scanner runs once per session, slow poll
    setInterval(fetchBaseline, 8000);   // baseline runs once per cycle (5min); poll modest
    fetchSignals();
    fetchBreakouts();
    fetchPositions();
    fetchGaps();
    fetchBaseline();
});
