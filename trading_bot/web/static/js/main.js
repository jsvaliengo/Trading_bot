/*
 * Trading Bot Dashboard — overview single-screen (redesign 2026-06).
 * Command bar + KPIs com sparkline + posições ricas (barra SL─preço─TP) +
 * trades com régua de MFE + win/loss + P&L por moeda + distribuição de MFE +
 * drawdown + histórico. Charts via Chart.js. Mesmo contrato de snapshot.
 */
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));
    const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

    const fmt = {
        usd(v, o = {}) {
            if (v === null || v === undefined || isNaN(v)) return '—';
            const s = o.signed && v > 0 ? '+' : '';
            return s + '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        },
        num(v, d = 4) { if (v === null || v === undefined || isNaN(v)) return '—'; return Number(v).toFixed(d); },
        pct(v, d = 2) { if (v === null || v === undefined || isNaN(v)) return '—'; return (v > 0 ? '+' : '') + Number(v).toFixed(d) + '%'; },
        time(iso) { if (!iso) return '—'; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleTimeString('pt-BR', { hour12: false }); },
        dateTime(iso) { if (!iso) return '—'; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString('pt-BR', { hour12: false }); },
    };

    const state = { snapshot: null, chartRange: 'all' };
    const charts = {};

    function classify(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
    function setPnl(el, v, prefix) {
        if (!el) return;
        el.classList.remove('pos', 'neg', 'pnl-pos', 'pnl-neg');
        const c = classify(v);
        if (c) el.classList.add(c);
    }

    // ───────── Theme ─────────
    const THEME_KEY = 'tradingbot-theme';
    function applyTheme(t) {
        document.documentElement.setAttribute('data-theme', t);
        localStorage.setItem(THEME_KEY, t);
        // cores dos charts mudam — destrói e re-renderiza do último snapshot
        Object.keys(charts).forEach(k => { try { charts[k].destroy(); } catch (e) {} delete charts[k]; });
        if (state.snapshot) renderAll(state.snapshot);
    }
    function initTheme() {
        applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
        $('theme-toggle').addEventListener('click', () => {
            const cur = document.documentElement.getAttribute('data-theme') || 'dark';
            applyTheme(cur === 'dark' ? 'light' : 'dark');
        });
    }

    // ───────── Chart helpers ─────────
    function palette() {
        return {
            ac: cssv('--accent'), pos: cssv('--success'), neg: cssv('--danger'),
            mut: cssv('--text-muted'), dim: cssv('--text-dim'), grid: cssv('--border'), card2: cssv('--bg-elevated'),
        };
    }
    function areaGradient(canvas, color, h) {
        const g = canvas.getContext('2d').createLinearGradient(0, 0, 0, h || 200);
        g.addColorStop(0, color + '40'); g.addColorStop(1, color + '00');
        return g;
    }
    const tickFont = { family: 'JetBrains Mono', size: 9 };

    function sparkline(id, data, color) {
        const s = $(id);
        if (!s || !data || data.length < 2) { if (s) s.innerHTML = ''; return; }
        const w = 64, h = 24, mn = Math.min(...data), mx = Math.max(...data), rng = (mx - mn) || 1;
        const pts = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - 2 - ((v - mn) / rng) * (h - 4)}`).join(' ');
        s.setAttribute('viewBox', `0 0 ${w} ${h}`);
        s.innerHTML = `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`;
    }

    // ───────── Summary (KPIs + status) ─────────
    function renderSummary(s) {
        if (!s) return;
        $('kpi-balance').textContent = fmt.usd(s.last_balance);
        $('kpi-initial').textContent = fmt.usd(s.initial_capital);
        const delta = (s.last_balance || 0) - (s.initial_capital || 0);
        const dpct = s.initial_capital > 0 ? (delta / s.initial_capital) * 100 : 0;
        const de = $('kpi-balance-delta');
        de.textContent = `${fmt.usd(delta, { signed: true })} · ${fmt.pct(dpct)}`;
        de.className = 'kpi-tag ' + (delta > 0 ? 'pos' : delta < 0 ? 'neg' : '');

        const pt = $('kpi-pnl-total');
        pt.textContent = fmt.usd(s.total_pnl, { signed: true }); setPnl(pt, s.total_pnl);
        $('kpi-roi').textContent = 'ROI ' + fmt.pct(s.roi_percent);

        const pd = $('kpi-pnl-daily');
        pd.textContent = fmt.usd(s.daily_pnl, { signed: true }); setPnl(pd, s.daily_pnl);
        $('kpi-closed-trades').textContent = (s.closed_trades ?? '—') + ' trades fechados';

        const fv = s.funding_fee_total, fEl = $('kpi-funding'), fSub = $('kpi-funding-sub');
        if (fv === undefined || fv === null || isNaN(fv)) { fEl.textContent = '—'; fSub.textContent = '—'; }
        else {
            fEl.textContent = fmt.usd(fv, { signed: true }); setPnl(fEl, fv);
            const dir = fv > 0 ? 'Recebendo' : fv < 0 ? 'Pagando' : 'Neutro';
            const c = s.commission_total;
            fSub.textContent = dir + ((c !== undefined && c !== null && !isNaN(c)) ? ` · taxas ${fmt.usd(c, { signed: true })}` : '');
        }

        const run = s.running && !s.paused, pr = $('status-running');
        pr.textContent = s.paused ? 'Pausado' : (run ? 'Rodando' : 'Parado');
        pr.className = 'status-pill ' + (s.paused ? 'warn' : (run ? 'ok' : 'error'));
        $('status-ai').textContent = 'IA: ' + (s.ai_mode || 'off').toUpperCase();
        $('btn-pause').disabled = s.paused; $('btn-resume').disabled = !s.paused;
    }

    function renderSparklines(snap) {
        const hist = snap.portfolio_history || [];
        const eq = hist.map(h => h.equity ?? h.balance).filter(v => v !== undefined && v !== null).slice(-14);
        const pnl = hist.map(h => h.pnl_total).filter(v => v !== undefined && v !== null).slice(-14);
        const daily = (snap.daily_history || []).map(d => d.net).slice(-8);
        const p = palette();
        sparkline('sp-equity', eq, eq.length && eq[eq.length - 1] >= eq[0] ? p.pos : p.neg);
        sparkline('sp-pnl', pnl, pnl.length && pnl[pnl.length - 1] >= 0 ? p.pos : p.neg);
        sparkline('sp-daily', daily, daily.length && daily[daily.length - 1] >= 0 ? p.pos : p.neg);
    }

    // ───────── Análise / Win-Loss ─────────
    function renderAnalysis(an, mfe) {
        const p = palette();
        const set = (id, t) => { const e = $(id); if (e) e.textContent = t; };
        const setS = (id, v) => { const e = $(id); if (e) { e.textContent = fmt.usd(v, { signed: true }); setPnl(e, v); } };
        an = an || {};
        const wins = an.wins || 0, losses = an.losses || 0;
        set('kpi-winrate', (an.win_rate !== undefined ? Number(an.win_rate).toFixed(0) : '—') + '%');
        $('kpi-winloss').innerHTML = `<b class="pos">${wins}</b>W / <b class="neg">${losses}</b>L`;
        set('an-win-rate', (an.win_rate !== undefined ? Number(an.win_rate).toFixed(1) : '—') + '%');
        set('an-trades', `${an.trades || 0} (${wins}W / ${losses}L)`);
        set('an-pl-ratio', an.profit_loss_ratio !== undefined ? Number(an.profit_loss_ratio).toFixed(2) : '—');
        setS('an-total-profit', an.total_profit); setS('an-total-loss', an.total_loss);
        setS('an-avg-profit', an.avg_profit); setS('an-avg-loss', an.avg_loss);
        set('an-volume', fmt.usd(an.trading_volume));
        set('an-days', `${an.winning_days || 0}+ / ${an.losing_days || 0}−`);
        set('winloss-sub', `${an.trades || 0} trades`);

        // MFE médio KPI
        mfe = mfe || {};
        const act = mfe.activation_pct || 0;
        if (mfe.n) {
            set('kpi-mfe', Number(mfe.avg).toFixed(2) + '%');
            const sub = $('kpi-mfe-sub');
            if (sub) {
                const below = mfe.avg < act;
                sub.textContent = below ? `abaixo do gatilho ${act}%` : `acima do gatilho ${act}%`;
                sub.className = 'kpi-mfe-sub ' + (below ? 'neg' : 'pos');
            }
        } else { set('kpi-mfe', '—'); set('kpi-mfe-sub', 'sem dados'); }

        // donuts
        upsertDoughnut('dm-winrate', [an.win_rate || 0, 100 - (an.win_rate || 0)], [p.ac, p.card2]);
        upsertDoughnut('winloss-chart', [wins, losses], [p.pos, p.neg]);
    }

    function upsertDoughnut(id, data, colors) {
        const el = $(id); if (!el || typeof Chart === 'undefined') return;
        if (charts[id]) { charts[id].data.datasets[0].data = data; charts[id].data.datasets[0].backgroundColor = colors; charts[id].update('none'); return; }
        charts[id] = new Chart(el, {
            type: 'doughnut',
            data: { datasets: [{ data, backgroundColor: colors, borderWidth: 0 }] },
            // responsive:false → respeita o width/height fixo do <canvas> (36/92px).
            // Sem isso o Chart.js infla o canvas pra preencher o flex e estoura o card.
            options: { responsive: false, maintainAspectRatio: false, cutout: '70%',
                plugins: { legend: { display: false }, tooltip: { enabled: id !== 'dm-winrate' } } },
        });
    }

    // ───────── Positions (barra SL─preço─TP) ─────────
    function posBar(p) {
        const sl = p.custom_stop_loss, tp = p.custom_take_profit, en = p.entry_price, mk = p.mark_price;
        if (!sl || !tp || !en) return '';
        // Ancora SL=0% (esquerda) e TP=100% (direita) nos DOIS lados. Para SHORT,
        // SL é o preço MAIOR e TP o MENOR — usar min/max de preço invertia a barra
        // (marcador/rótulo trocados, preço aparecia "perto do TP" perdendo). Com
        // (v−SL)/(TP−SL) o eixo segue a direção do trade: vermelho=stop à esquerda,
        // verde=alvo à direita, preço/entrada proporcionais entre eles.
        const span = (tp - sl) || 1;
        const at = (v) => Math.max(0, Math.min(100, ((v - sl) / span) * 100));
        const mkPart = (mk && mk > 0) ? `<span class="mk pr" style="left:${at(mk)}%"></span>` : '';
        const prLbl = (mk && mk > 0) ? `<span>preço <b>${fmt.num(mk, 4)}</b></span>` : '';
        return `<div class="bar">
            <span class="mk sl" style="left:${at(sl)}%"></span>
            <span class="mk en" style="left:${at(en)}%"></span>
            ${mkPart}
            <span class="mk tp" style="left:${at(tp)}%"></span>
        </div>
        <div class="barlbl">
            <span>SL <b>${fmt.num(sl, 4)}</b></span>
            <span>entrada <b>${fmt.num(en, 4)}</b></span>
            ${prLbl}
            <span>TP <b>${fmt.num(tp, 4)}</b></span>
        </div>`;
    }
    function renderPositions(positions) {
        const root = $('positions-list');
        $('positions-count').textContent = positions.length;
        if (!positions.length) { root.innerHTML = '<div class="empty">Sem posições.</div>'; return; }
        root.innerHTML = positions.map(p => {
            const sc = p.side === 'LONG' ? 'l' : 's';
            const pnlTxt = (p.unrealized_pnl_usd === null || p.unrealized_pnl_usd === undefined)
                ? '<span class="dim">—</span>'
                : `<span class="${classify(p.unrealized_pnl_usd)} n">${fmt.usd(p.unrealized_pnl_usd, { signed: true })}` +
                  (p.unrealized_pnl_percent != null ? ` <small>(${fmt.pct(p.unrealized_pnl_percent)})</small>` : '') + '</span>';
            const trl = (p.trailing_activation_pct != null)
                ? `<span class="trl">trail ${Number(p.trailing_activation_pct).toFixed(1)}/${Number(p.trailing_distance_pct).toFixed(1)}%</span>` : '';
            return `<div class="prow">
                <div class="prow-top">
                    <span class="psym">${p.symbol} <span class="side ${sc}">${p.side}</span> ${trl}</span>
                    ${pnlTxt}
                </div>
                ${posBar(p)}
            </div>`;
        }).join('');
    }

    // ───────── Trades (régua de MFE) ─────────
    function renderTrades(trades) {
        const tb = $('trades-table').querySelector('tbody');
        $('trades-count').textContent = trades.length;
        if (!trades.length) { tb.innerHTML = '<tr><td colspan="8" class="empty">Sem trades ainda.</td></tr>'; return; }
        tb.innerHTML = trades.map(t => {
            const sc = t.side === 'LONG' ? 'l' : 's';
            const closed = t.exit_price != null && t.exit_price > 0;
            const dim = '<span class="dim">—</span>';
            const net = closed ? `<span class="${classify(t.pnl_net)} n">${fmt.usd(t.pnl_net, { signed: true })}</span>` : dim;
            let mfe = dim;
            if (t.mfe_pct != null) {
                const w = Math.max(4, Math.min(100, (t.mfe_pct / 2) * 100));
                mfe = `<span class="mferul"><i style="width:${w}%"></i></span> <span class="n mf-t">${Number(t.mfe_pct).toFixed(2)}%</span>`;
            }
            const reason = closed ? `<small>${t.close_reason || '—'}</small>` : '<small class="ac">Aberta</small>';
            return `<tr>
                <td><small>${fmt.dateTime(t.timestamp)}</small></td>
                <td class="psym">${t.symbol}</td>
                <td><span class="side ${sc}">${t.side}</span></td>
                <td class="r n">${fmt.num(t.entry_price, 4)}</td>
                <td class="r n">${closed ? fmt.num(t.exit_price, 4) : dim}</td>
                <td class="r">${net}</td>
                <td>${mfe}</td>
                <td>${reason}</td>
            </tr>`;
        }).join('');
    }

    // ───────── P&L por moeda ─────────
    function renderPnlBySymbol(rows) {
        const root = $('pnl-symbol-list');
        if (!rows || !rows.length) { root.innerHTML = '<div class="empty">Sem dados.</div>'; return; }
        const maxAbs = Math.max(...rows.map(r => Math.abs(r.net))) || 1;
        root.innerHTML = rows.slice(0, 8).map(r => {
            const sym = (r.symbol || '').replace('USDT', '');
            const w = Math.max(3, (Math.abs(r.net) / maxAbs) * 100);
            const cls = r.net >= 0 ? 'pos' : 'neg';
            return `<div class="srow">
                <span class="sl psym">${sym}</span>
                <span class="sbar"><i class="bg-${cls}" style="width:${w}%"></i></span>
                <span class="${cls} n">${fmt.usd(r.net, { signed: true })}</span>
            </div>`;
        }).join('');
    }

    // ───────── MFE distribution ─────────
    function renderMfe(mfe) {
        const el = $('mfe-chart'); if (!el || typeof Chart === 'undefined' || !mfe) return;
        const p = palette();
        const edges = mfe.edges || [], act = mfe.activation_pct || 0;
        // bucket i cobre [prev, edges[i]); accent quando o piso do bucket ≥ gatilho
        const colors = (mfe.counts || []).map((_, i) => {
            const lowEdge = i === 0 ? 0 : edges[i - 1];
            return lowEdge >= act ? p.ac : p.mut;
        });
        const note = $('mfe-note');
        if (note) note.innerHTML = mfe.n
            ? `<span class="dot" style="background:${p.ac}"></span> gatilho do trailing em ${act}% — ${mfe.n} trades medidos, média ${Number(mfe.avg).toFixed(2)}%`
            : 'sem MFE medido ainda (acumula a cada trade fechado)';
        if (charts['mfe-chart']) {
            const c = charts['mfe-chart'];
            c.data.labels = mfe.labels; c.data.datasets[0].data = mfe.counts; c.data.datasets[0].backgroundColor = colors;
            c.update('none'); return;
        }
        charts['mfe-chart'] = new Chart(el, {
            type: 'bar',
            data: { labels: mfe.labels, datasets: [{ data: mfe.counts, backgroundColor: colors, borderRadius: 4, maxBarThickness: 34 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
                scales: { x: { ticks: { color: p.mut, font: tickFont }, grid: { display: false } },
                          y: { ticks: { color: p.mut, font: tickFont, precision: 0 }, grid: { color: p.grid }, beginAtZero: true } } },
        });
    }

    // ───────── Regime ─────────
    function renderRegime(rg) {
        const root = $('regime-list'); if (!root) return;
        $('regime-status').textContent = rg && rg.enabled ? 'ativo' : 'desabilitado';
        const committed = (rg && rg.committed) || {};
        const syms = Object.keys(committed).sort();
        if (!syms.length) { root.innerHTML = '<div class="empty">Sem dados ainda.</div>'; return; }
        root.innerHTML = syms.map(s => {
            const r = committed[s] || 'neutral';
            return `<div class="srow"><span class="psym">${s}</span><span class="rg rg-${r}">${r}</span></div>`;
        }).join('');
    }

    // ───────── Equity + Drawdown ─────────
    function filterRange(hist, range) {
        if (range === 'all' || !hist.length) return hist;
        const win = { '24h': 864e5, '6h': 216e5, '1h': 36e5 }[range] || 0;
        const cut = Date.now() - win;
        return hist.filter(s => { const t = new Date(s.timestamp).getTime(); return !isNaN(t) && t >= cut; });
    }
    function renderEquity(hist) {
        const el = $('equity-chart'); if (!el || typeof Chart === 'undefined') return;
        const p = palette();
        const f = filterRange(hist || [], state.chartRange);
        const data = f.map(s => ({ x: new Date(s.timestamp).getTime(), y: s.equity ?? s.balance })).filter(d => !isNaN(d.x) && d.y != null);
        $('chart-range').textContent = data.length ? `${fmt.dateTime(f[0].timestamp)} → ${fmt.dateTime(f[f.length - 1].timestamp)}` : 'sem dados nesta janela';
        const ds = { data, borderColor: p.ac, backgroundColor: areaGradient(el, p.ac, 210), fill: true, tension: 0.35, borderWidth: 2, pointRadius: 0 };
        if (charts['equity-chart']) { charts['equity-chart'].data.datasets[0] = ds; charts['equity-chart'].update('none'); return; }
        charts['equity-chart'] = new Chart(el, {
            type: 'line', data: { datasets: [ds] },
            options: { responsive: true, maintainAspectRatio: false, parsing: false, plugins: { legend: { display: false } },
                scales: { x: { type: 'linear', display: false }, y: { ticks: { color: p.mut, font: tickFont }, grid: { color: p.grid } } } },
        });
    }
    function renderDrawdown(hist) {
        const el = $('drawdown-chart'); if (!el || typeof Chart === 'undefined') return;
        const p = palette();
        let peak = -Infinity;
        const data = (hist || []).map(s => {
            const eq = s.equity ?? s.balance; if (eq == null) return null;
            peak = Math.max(peak, eq);
            return { x: new Date(s.timestamp).getTime(), y: peak > 0 ? ((eq - peak) / peak) * 100 : 0 };
        }).filter(d => d && !isNaN(d.x));
        const ds = { data, borderColor: p.neg, backgroundColor: areaGradient(el, p.neg, 150), fill: true, tension: 0.3, borderWidth: 1.6, pointRadius: 0 };
        if (charts['drawdown-chart']) { charts['drawdown-chart'].data.datasets[0] = ds; charts['drawdown-chart'].update('none'); return; }
        charts['drawdown-chart'] = new Chart(el, {
            type: 'line', data: { datasets: [ds] },
            options: { responsive: true, maintainAspectRatio: false, parsing: false, plugins: { legend: { display: false } },
                scales: { x: { type: 'linear', display: false }, y: { ticks: { color: p.mut, font: tickFont, callback: v => v + '%' }, grid: { color: p.grid } } } },
        });
    }

    // ───────── Histórico ─────────
    function renderDaily(history) {
        const tb = $('historico-table').querySelector('tbody'), sub = $('historico-summary');
        if (!history || !history.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">Sem histórico ainda.</td></tr>'; if (sub) sub.textContent = '—'; return; }
        if (sub) { const last = history[history.length - 1]; sub.textContent = `${history.length} dia(s) · acum. ${fmt.usd(last.cumulative, { signed: true })}`; }
        tb.innerHTML = history.slice().reverse().map(d => `<tr>
            <td>${d.day}</td>
            <td class="r n">${d.trades} <small class="dim">(${d.wins}W/${d.losses}L)</small></td>
            <td class="r n">${Number(d.win_rate || 0).toFixed(1)}%</td>
            <td class="r n neg">${fmt.usd(d.fees)}</td>
            <td class="r n ${classify(d.net)}">${fmt.usd(d.net, { signed: true })}</td>
            <td class="r n ${classify(d.cumulative)}">${fmt.usd(d.cumulative, { signed: true })}</td>
        </tr>`).join('');
    }

    // ───────── Apply snapshot ─────────
    function renderAll(snap) {
        renderSummary(snap.summary);
        renderSparklines(snap);
        renderAnalysis(snap.pnl_analysis, snap.mfe_distribution);
        renderPositions(snap.positions || []);
        renderTrades(snap.recent_trades || []);
        renderPnlBySymbol(snap.pnl_by_symbol || []);
        renderMfe(snap.mfe_distribution);
        renderRegime(snap.regime);
        renderEquity(snap.portfolio_history || []);
        renderDrawdown(snap.portfolio_history || []);
        renderDaily(snap.daily_history || []);
    }
    function applySnapshot(snap) {
        if (!snap) return;
        state.snapshot = snap;
        renderAll(snap);
        $('kpi-last-update').textContent = fmt.time(snap.server_time);
        resetCountdown();
    }

    function initChips() {
        $$('.chips .chip').forEach(b => b.addEventListener('click', () => {
            $$('.chips .chip').forEach(x => x.classList.remove('on'));
            b.classList.add('on');
            state.chartRange = b.dataset.range;
            if (state.snapshot) renderEquity(state.snapshot.portfolio_history || []);
        }));
    }

    // ───────── Controls + modal ─────────
    const modal = $('confirm-modal');
    let pending = null;
    function ask(title, msg, action) { $('confirm-title').textContent = title; $('confirm-message').textContent = msg; pending = action; modal.classList.add('open'); }
    function closeModal() { modal.classList.remove('open'); pending = null; }
    $('confirm-cancel').addEventListener('click', closeModal);
    $('confirm-ok').addEventListener('click', () => { const a = pending; closeModal(); if (a) a(); });
    async function postControl(path, body) {
        const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}), credentials: 'include' });
        if (!r.ok) { alert('Falha: ' + r.status + ' — ' + (await r.text())); return null; }
        return r.json();
    }
    $$('[data-action]').forEach(btn => btn.addEventListener('click', () => {
        const a = btn.dataset.action;
        if (a === 'pause') ask('Pausar bot', 'O bot PARA de abrir novas posições. As abertas seguem monitoradas (trailing, SL, TP).', () => postControl('/api/control/pause'));
        else if (a === 'resume') ask('Retomar bot', 'O bot volta a procurar entradas conforme as estratégias ativas.', () => postControl('/api/control/resume'));
        else if (a === 'close_all') ask('⚠ FECHAR TODAS AS POSIÇÕES', 'Envia ordens MARKET fechando TODAS as posições agora. Confirma?', () => postControl('/api/control/close_all', { reason: 'Dashboard panic close' }));
    }));

    // ───────── Connection ─────────
    const pillConn = $('status-connection');
    const pollInterval = (parseInt(document.body.dataset.pollInterval, 10) || 5) * 1000;
    let pollTimer = null, socket = null;
    async function pollOnce() { try { const r = await fetch('/api/snapshot', { credentials: 'include' }); if (!r.ok) throw new Error('HTTP ' + r.status); applySnapshot(await r.json()); } catch (e) { console.warn('Poll falhou:', e); } }
    function startPolling() { if (pollTimer) return; pollOnce(); pollTimer = setInterval(pollOnce, pollInterval); }
    function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
    function setConn(st) {
        pillConn.classList.remove('ok', 'warn', 'error');
        if (st === 'connected') { pillConn.textContent = 'ao vivo'; pillConn.classList.add('ok'); }
        else if (st === 'polling') { pillConn.textContent = 'polling'; pillConn.classList.add('warn'); }
        else { pillConn.textContent = 'desconectado'; pillConn.classList.add('error'); }
    }

    // ───────── Countdown ─────────
    const REFRESH = Math.max(1, Math.round(pollInterval / 1000));
    const nextEl = $('kpi-next-update');
    let left = REFRESH;
    function renderCd() { if (nextEl) nextEl.textContent = 'próxima em ' + left + 's'; }
    function resetCountdown() { left = REFRESH; renderCd(); }
    function reqRefresh() { if (socket && socket.connected) socket.emit('request_snapshot'); else pollOnce(); }
    function tick() { left -= 1; if (left <= 0) { reqRefresh(); left = REFRESH; } renderCd(); }
    renderCd(); setInterval(tick, 1000);

    // ───────── Boot ─────────
    initTheme();
    initChips();
    if (typeof io !== 'undefined') {
        socket = io({ transports: ['polling', 'websocket'], reconnectionDelayMax: 10000 });
        socket.on('connect', () => { setConn('connected'); stopPolling(); socket.emit('request_snapshot'); });
        socket.on('disconnect', () => { setConn('polling'); startPolling(); });
        socket.on('connect_error', () => { setConn('polling'); startPolling(); });
        socket.on('snapshot', applySnapshot);
        socket.on('control_changed', (pl) => { if (state.snapshot && state.snapshot.summary) { state.snapshot.summary.paused = !!pl.paused; renderSummary(state.snapshot.summary); } });
        socket.on('position_opened', () => socket.emit('request_snapshot'));
        socket.on('position_closed', () => socket.emit('request_snapshot'));
        socket.on('regime_changed', () => socket.emit('request_snapshot'));
        socket.on('balance_update', () => socket.emit('request_snapshot'));
    } else { setConn('polling'); startPolling(); }
    setTimeout(() => { if (!state.snapshot) { setConn('polling'); startPolling(); } }, 1500);
})();
