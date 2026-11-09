/*
 * Dashboard v3: painel do forward test Donchian 4h.
 * Dois payloads: `live` (/api/live ou evento socket `snapshot`, a cada poll: KPIs,
 * posições, motor, saúde, forward test) e `history` (/api/history, só quando um
 * trade fecha ou a cada 2 min: trades, curva de equity). Os gráficos são
 * atualizados no lugar, nunca recriados a cada poll.
 */
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));
    const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const isNum = (v) => v !== null && v !== undefined && !isNaN(v);
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    const fmt = {
        usd(v, o = {}) {
            if (!isNum(v)) return '—';
            const sign = v < 0 ? '−' : (o.signed && v > 0 ? '+' : '');
            return sign + '$' + Math.abs(Number(v)).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        },
        // equivalente em BRL pela cotação do payload (state.fx); '' sem cotação, aí o texto some
        brl(usd) { const r = state.fx && state.fx.usd_brl; if (!isNum(usd) || !isNum(r)) return ''; return '≈ ' + (usd < 0 ? '−' : '') + Math.abs(usd * r).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' }); },
        pct(v, d = 2, signed = true) { if (!isNum(v)) return '—'; const s = v < 0 ? '−' : (signed && v > 0 ? '+' : ''); return s + Math.abs(Number(v)).toFixed(d).replace('.', ',') + '%'; },
        num(v, d = 2) { if (!isNum(v)) return '—'; return Number(v).toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d }); },
        price(v) { if (!isNum(v)) return '—'; const a = Math.abs(v); const d = a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 7; return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); },
        r(v) { if (!isNum(v)) return '—'; return (v < 0 ? '−' : '+') + Math.abs(v).toFixed(2).replace('.', ',') + 'R'; },
        // Horários no fuso do dia de trading (snapshot.tz_offset_hours), não no do navegador:
        // desloca o instante e formata como UTC, porque toLocale* só aceita nomes IANA.
        _tz(iso) { const d = new Date(iso); return isNaN(d) ? null : new Date(d.getTime() + (fmt.tzOffset || 0) * 3600e3); },
        tzLabel() { const o = fmt.tzOffset || 0; return o === -3 ? 'BRT' : o ? `UTC${o > 0 ? '+' : '−'}${Math.abs(o)}` : 'UTC'; },
        time(iso) { if (!iso) return '—'; const d = fmt._tz(iso); return d ? d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) : iso; },
        dateTime(iso) { if (!iso) return '—'; const d = fmt._tz(iso); return d ? d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) : iso; },
        date(iso) { if (!iso) return '—'; const d = fmt._tz(iso); return d ? d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'UTC' }) : iso; },
        day(ymd) { return /^\d{4}-\d{2}-\d{2}$/.test(ymd || '') ? `${ymd.slice(8, 10)}/${ymd.slice(5, 7)}/${ymd.slice(0, 4)}` : '—'; },   // data pura (dia de trading), sem deslocar fuso
        dur(h) {
            if (!isNum(h)) return '—';
            if (h < 1) return Math.round(h * 60) + ' min';
            if (h < 48) return h.toFixed(1).replace('.', ',') + ' h';
            return (h / 24).toFixed(1).replace('.', ',') + ' d';
        },
        ago(sec) {
            if (!isNum(sec)) return '—';
            if (sec < 90) return Math.round(sec) + ' s';
            if (sec < 5400) return Math.round(sec / 60) + ' min';
            if (sec < 172800) return (sec / 3600).toFixed(1).replace('.', ',') + ' h';
            return Math.round(sec / 86400) + ' d';
        },
        clock(sec) { sec = Math.max(0, Math.round(sec)); const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60; return h ? `${h}h ${String(m).padStart(2, '0')}min` : `${m}:${String(s).padStart(2, '0')}`; },
    };
    const cls = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : '';
    const sum = (a) => a.reduce((s, v) => s + v, 0);

    const state = { live: null, fx: null, hist: null, pnl: null, chartRange: 'test', page: 1, sortKey: 'exit_time', sortDir: -1, agg: 'week',
        filter: { strategy: document.body.dataset.strategy === 'donchian_4h' ? 'donchian_4h' : 'all', side: 'all', reason: 'all' } };
    const PAGE = 15;
    const charts = {};

    // ───────── Tema ─────────
    const THEME_KEY = 'tradingbot-theme';
    function currentTheme() {
        const forced = document.documentElement.getAttribute('data-theme');
        if (forced) return forced;
        return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    function rerenderCharts() {
        Object.keys(charts).forEach(k => { try { charts[k].destroy(); } catch (e) { /* já destruído */ } delete charts[k]; });
        if (state.live) renderLive(state.live);
        if (state.hist) renderHistory(state.hist);
        if (state.pnl) renderPnl(state.pnl);
    }
    function initTheme() {
        const saved = localStorage.getItem(THEME_KEY);
        if (saved) document.documentElement.setAttribute('data-theme', saved);
        $('theme-toggle').addEventListener('click', () => {
            const next = currentTheme() === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem(THEME_KEY, next);
            rerenderCharts();
        });
        matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => { if (!localStorage.getItem(THEME_KEY)) rerenderCharts(); });
    }

    // ───────── Chart helpers ─────────
    function palette() {
        return { ac: cssv('--accent'), good: cssv('--good'), bad: cssv('--bad'), warn: cssv('--warn'),
            dim: cssv('--text-dim'), mut: cssv('--text-muted'), grid: cssv('--grid'), el: cssv('--bg-elevated'), card: cssv('--bg-card') };
    }
    const tickFont = { family: 'IBM Plex Mono', size: 10 };
    const hexa = (hex, a) => hex.length === 7 ? hex + Math.round(a * 255).toString(16).padStart(2, '0') : hex;
    const baseTooltip = (p) => ({ backgroundColor: p.el, titleColor: p.dim, bodyColor: cssv('--text'), borderColor: cssv('--border-strong'), borderWidth: 1, padding: 8, displayColors: false, titleFont: { family: 'IBM Plex Sans', size: 11 }, bodyFont: { family: 'IBM Plex Mono', size: 11 } });
    // canvas dentro de <details> fechado nasce com 0px: ao abrir, o Chart.js precisa remedir
    $$('details').forEach(d => d.addEventListener('toggle', () => { if (d.open) Object.values(charts).forEach(c => c.resize()); }));

    // ───────── Topo + status ─────────
    const BAR_MS = 4 * 3600e3;
    function renderTop(s, h, e) {
        const pill = $('state-pill');
        const run = s.running && !s.paused;
        pill.textContent = s.paused ? 'pausado' : (run ? 'rodando' : 'parado');
        pill.className = 'state ' + (s.paused ? 'warn' : (run ? 'ok' : 'bad'));
        $('top-title').textContent = s.strategy === 'donchian_4h' ? 'Forward test · Donchian 4h' : `Trading bot · ${s.strategy || 'pullback'}`;
        $('top-meta').textContent = `sinais ${s.signal_data_source || '—'} · deploy ${(h && h.deploy_sha) || '—'} · no ar há ${h ? fmt.ago(h.uptime_seconds) : '—'} · ${connLabel()} · atualizado ${fmt.time(state.live.server_time)}`;
        const fx = state.fx || {}; $('fx-badge').hidden = !isNum(fx.usd_brl);
        if (isNum(fx.usd_brl)) { $('fx-rate').textContent = `US$ 1 = R$ ${fmt.num(fx.usd_brl)}`; $('fx-at').textContent = `câmbio ao vivo · ${fmt.dateTime(fx.fetched_at)}`; }
        $('btn-pause').disabled = !!s.paused; $('btn-resume').disabled = !s.paused;
        const banner = $('pause-banner');
        if (s.paused) {
            const why = (h && h.paused_reason) || 'motivo não registrado';
            banner.textContent = `Bot pausado: ${why}. Posições abertas seguem monitoradas; nenhuma entrada nova.`;
            banner.className = 'banner' + (/drawdown/.test(why) ? ' bad' : '');
            banner.hidden = false;
        } else if (!s.running) {
            banner.textContent = 'Loop principal parado. O dashboard responde, mas o bot não está ciclando.';
            banner.className = 'banner bad'; banner.hidden = false;
        } else if (e && e.last_bar_processed_at && Date.now() - new Date(e.last_bar_processed_at).getTime() > BAR_MS + 45 * 60e3) {
            banner.textContent = `A última barra processada foi às ${fmt.dateTime(e.last_bar_processed_at)}: o motor deveria ter processado uma barra nova. Verifique o log.`;
            banner.className = 'banner bad'; banner.hidden = false;
        } else banner.hidden = true;
    }
    const brlTag = (usd) => { const t = fmt.brl(usd); return t ? `<small class="brl">${t}</small>` : ''; };
    function setTile(id, value, foot, k, usd) {
        const v = $(id); v.innerHTML = esc(value) + brlTag(usd); v.className = 'tv ' + (k || '');
        $(id + '-f').textContent = foot;
    }
    function renderStatus(s, ft, e, positions) {
        const eq = s.last_balance;
        setTile('st-equity', fmt.usd(eq), `pico ${fmt.usd(ft.peak_equity)} · capital ${fmt.usd(s.initial_capital)}`, '', eq);
        const dd = ft.drawdown_pct, lim = ft.dd_limit_pct || 20;
        setTile('st-dd', fmt.pct(dd, 2, false), isNum(ft.dd_room_usd) ? `${fmt.usd(ft.dd_room_usd)} de folga até ${lim}% (${fmt.usd(ft.dd_floor_equity)})` : 'sem pico registrado',
            dd >= lim * 0.8 ? 'neg' : '');
        const max = e && e.params ? e.params.max_positions : null;
        const cap = e && e.params && isNum(eq) ? eq * (e.params.max_gross_mult || 3) : null;
        setTile('st-pos', max ? `${positions.length} / ${max}` : String(positions.length),
            isNum(e && e.gross_notional) && cap ? `notional ${fmt.usd(e.gross_notional)} de ${fmt.usd(cap)} (${e.params.max_gross_mult}× equity)` : '—');
        setTile('st-bar', e && e.bar_close ? fmt.time(e.bar_close) : '—',
            e && e.last_bar_processed_at ? `processada ${fmt.dateTime(e.last_bar_processed_at)} ${fmt.tzLabel()}` : (e && e.bar_close ? 'ainda não processada' : 'motor Donchian inativo'));
        renderNext();
        setTile('st-unreal', fmt.usd(s.unrealized_pnl, { signed: true }), `${positions.length} posição(ões) abertas`, cls(s.unrealized_pnl), s.unrealized_pnl);
        setTile('st-today', fmt.usd(s.daily_pnl, { signed: true }), `${s.closed_trades_today ?? 0} fechado(s) hoje · funding ${fmt.usd(s.funding_fee_today, { signed: true })}`, cls(s.daily_pnl), s.daily_pnl);
        setTile('st-net', fmt.usd(ft.net_usd, { signed: true }), `${ft.trades || 0} trades · taxas ${fmt.usd(-(ft.fees_usd || 0), { signed: true })} · funding ${fmt.usd(ft.funding_usd, { signed: true })}`, cls(ft.net_usd), ft.net_usd);
    }
    function renderNext() {
        const e = state.live && state.live.engine;
        if (!e || !e.next_bar_close) { setTile('st-next', '—', 'barras 4h UTC: 00/04/08/12/16/20'); return; }
        const left = (new Date(e.next_bar_close).getTime() - Date.now()) / 1000;
        if (left > 0) setTile('st-next', fmt.clock(left), `fecha às ${fmt.time(e.next_bar_close)} ${fmt.tzLabel()}`);
        else setTile('st-next', 'processando', `barra fechou há ${fmt.ago(-left)}` + (e.pending_count ? ` · ${e.pending_count} pares pendentes` : ''), 'warn');
    }

    // ───────── Forward test ─────────
    function renderForwardTest(ft) {
        const v = $('verdict');
        if (!ft || !ft.strategy) { v.textContent = 'sem dados do forward test'; v.className = 'verdict'; return; }
        v.textContent = ft.verdict_label; v.className = 'verdict ' + ft.verdict;
        const tr = ft.trades || 0, tt = ft.target_trades || 150;
        $('tr-trades').textContent = `${tr} / ${tt}`;
        setMeter('m-trades', tr / tt * 100, tt, tr);
        $('tr-trades-f').textContent = `${ft.trades_before_change ?? 0} com universo de 30 · ${ft.trades_after_change ?? 0} com universo completo` +
            (ft.trades_remaining > 0 && ft.trades_per_week > 0 ? ` · faltam ${ft.trades_remaining}, ~${Math.ceil(ft.trades_remaining / ft.trades_per_week)} semanas no ritmo atual` : '');
        const d = ft.elapsed_days || 0, td = ft.target_days || 182;
        $('tr-days').textContent = `${Math.floor(d)} / ${td} dias`;
        setMeter('m-days', d / td * 100, td, Math.floor(d));
        $('tr-days-f').textContent = `início ${fmt.dateTime(ft.start)} ${fmt.tzLabel()} · prazo mínimo ${fmt.date(ft.end_by_time)}` +
            (ft.restart ? ` · reinício (trailing R): ${ft.trades_before_restart ?? 0} trades anteriores fora do critério` : '');
        const ex = $('tr-exp'); ex.textContent = fmt.usd(ft.expectancy_usd, { signed: true }) + (isNum(ft.avg_r) ? ` · ${fmt.r(ft.avg_r)}` : ''); ex.className = cls(ft.expectancy_usd || 0);
        const ci = ft.expectancy_ci95;
        $('tr-exp-f').textContent = ci ? `IC 95% de ${fmt.usd(ci[0], { signed: true })} a ${fmt.usd(ci[1], { signed: true })} (bootstrap, ${tr} trades)` : `o intervalo de confiança aparece a partir de 5 trades (${tr} até agora)`;
        const mdd = ft.max_drawdown_pct || 0, lim = ft.dd_limit_pct || 20;
        $('tr-dd').textContent = `${fmt.pct(mdd, 2, false)} / ${lim}%`;
        setMeter('m-dd', (ft.drawdown_pct || 0) / lim * 100, lim, ft.drawdown_pct || 0);
        const m = $('m-dd'); m.classList.toggle('hot', mdd / lim >= 0.8);
        const mx = $('m-dd-max'); mx.style.left = Math.min(100, mdd / lim * 100) + '%'; mx.title = `máximo até agora: ${fmt.pct(mdd, 2, false)}`;
        $('tr-dd-f').textContent = `agora ${fmt.pct(ft.drawdown_pct, 2, false)} · parada em ${lim}% do pico`;
    }
    function setMeter(id, pct, max, val) {
        const m = $(id); m.querySelector('i').style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
        m.setAttribute('aria-valuemax', max); m.setAttribute('aria-valuenow', Math.round(val * 100) / 100);
    }

    // ───────── Performance e risco ─────────
    function renderPerf(ft) {
        if (!ft || !ft.strategy) return;
        const vals = {
            expectancy: fmt.usd(ft.expectancy_usd, { signed: true }) + (isNum(ft.expectancy_pct) ? ` (${fmt.pct(ft.expectancy_pct)} notional)` : ''),
            avg_r: fmt.r(ft.avg_r), profit_factor: isNum(ft.profit_factor) ? fmt.num(ft.profit_factor) : '—',
            sharpe: isNum(ft.sharpe) ? fmt.num(ft.sharpe) : `— (${ft.sharpe_days || 0}/20 dias)`,
            win_rate: fmt.pct(ft.win_rate, 0, false),
            avg_win: isNum(ft.avg_win_usd) ? `${fmt.usd(ft.avg_win_usd, { signed: true })} (${fmt.pct(ft.avg_win_pct, 1)})` : '—',
            avg_loss: isNum(ft.avg_loss_usd) ? `${fmt.usd(ft.avg_loss_usd, { signed: true })} (${fmt.pct(ft.avg_loss_pct, 1)})` : '—',
            trades_per_week: fmt.num(ft.trades_per_week, 1), max_drawdown_pct: fmt.pct(ft.max_drawdown_pct, 2, false),
            costs: fmt.usd(-(ft.fees_usd || 0) + (ft.funding_usd || 0), { signed: true }),
        };
        $$('#perf td[data-k]').forEach(td => { td.textContent = vals[td.dataset.k]; td.className = 'r ' + (td.dataset.k === 'expectancy' || td.dataset.k === 'avg_r' ? cls(td.dataset.k === 'expectancy' ? ft.expectancy_usd : ft.avg_r) : ''); });
        $('perf-sub').textContent = `${ft.trades} trades desde ${fmt.date(ft.start)}`;
        const ci = ft.expectancy_ci95;
        $('ci-cap').textContent = ci ? `Expectativa por trade com IC 95% (bootstrap): a tese passa quando a faixa inteira fica acima de zero.` : 'Intervalo de confiança da expectativa a partir de 5 trades.';
        drawCI(ft);
    }
    function drawCI(ft) {
        const svg = $('ci-plot'); const W = 600, H = 110, L = 24, R = 24, y = 60;
        const mean = ft.expectancy_usd, ci = ft.expectancy_ci95;
        if (!isNum(mean)) { svg.innerHTML = ''; return; }
        const avgNotional = isNum(ft.expectancy_pct) && ft.expectancy_pct !== 0 ? mean / (ft.expectancy_pct / 100) : null;
        const research = avgNotional ? ft.research.expectancy_pct / 100 * avgNotional : null;
        const vals = [0, mean, ...(ci || []), ...(research != null ? [research] : [])];
        let lo = Math.min(...vals), hi = Math.max(...vals);
        const pad = (hi - lo) * 0.25 || Math.abs(mean) || 1; lo -= pad; hi += pad;
        const x = (v) => L + (v - lo) / (hi - lo) * (W - L - R);
        const ticks = niceTicks(lo, hi, 5);
        let out = `<line class="axis" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>`;
        out += ticks.map(t => `<line class="axis" x1="${x(t)}" x2="${x(t)}" y1="${y - 4}" y2="${y + 4}"/><text x="${x(t)}" y="${y + 20}" text-anchor="middle">${fmt.usd(t, { signed: true })}</text>`).join('');
        const zRight = research == null || research >= 0;
        out += `<line class="zero" x1="${x(0)}" x2="${x(0)}" y1="${y - 30}" y2="${y + 8}"/><text class="strong" x="${x(0) + (zRight ? -6 : 6)}" y="${y - 36}" text-anchor="${zRight ? 'end' : 'start'}">zero</text>`;
        if (research != null) out += `<line class="research" x1="${x(research)}" x2="${x(research)}" y1="${y - 30}" y2="${y + 8}"/><text x="${x(research) + (zRight ? 6 : -6)}" y="${y - 36}" text-anchor="${zRight ? 'start' : 'end'}">pesquisa ${fmt.usd(research, { signed: true })}</text>`;
        if (ci) out += `<rect class="band" x="${x(ci[0])}" y="${y - 11}" width="${Math.max(2, x(ci[1]) - x(ci[0]))}" height="22" rx="3"/>`;
        out += `<circle class="mean" cx="${x(mean)}" cy="${y}" r="6"/>`;
        svg.innerHTML = out;
    }
    function niceTicks(lo, hi, n) {
        const span = hi - lo, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
        const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || mag * 10;
        const out = []; for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) out.push(Math.round(t * 1e6) / 1e6);
        return out;
    }

    // ───────── Equity + drawdown ─────────
    function filterRange(hist, range, testStart) {
        if (!hist.length || range === 'all') return hist;
        let cut = 0;
        if (range === 'test') cut = testStart ? new Date(testStart).getTime() : 0;
        else cut = Date.now() - ({ '7d': 7 * 864e5, '24h': 864e5 }[range] || 0);
        const f = hist.filter(s => { const t = new Date(s.timestamp).getTime(); return !isNaN(t) && t >= cut; });
        return f.length >= 2 ? f : hist.slice(-2);
    }
    function timeScale(p, show) {
        return { type: 'linear', display: show, ticks: { color: p.mut, font: tickFont, maxTicksLimit: 6, callback: v => fmt.dateTime(v) }, grid: { color: p.grid } };
    }
    function renderEquity(hist, trades, ft) {
        const el = $('equity-chart'), elD = $('drawdown-chart');
        if (!el || typeof Chart === 'undefined') return;
        const p = palette();
        const f = filterRange(hist || [], state.chartRange, ft && ft.start);
        const pts = f.map(s => ({ x: new Date(s.timestamp).getTime(), y: s.equity ?? s.balance })).filter(d => !isNaN(d.x) && d.y != null);
        $('eq-sub').textContent = pts.length ? `${fmt.dateTime(f[0].timestamp)} → ${fmt.dateTime(f[f.length - 1].timestamp)} · ${fmt.usd(pts[0].y)} → ${fmt.usd(pts[pts.length - 1].y)}` : 'sem pontos nesta janela';
        const t0 = pts.length ? pts[0].x : 0, t1 = pts.length ? pts[pts.length - 1].x : 0;
        const marks = (trades || []).filter(t => t.exit_time && t.strategy_type === 'donchian_4h').map(t => {
            const x = new Date(t.exit_time).getTime(); if (isNaN(x) || x < t0 || x > t1) return null;
            let best = pts[0]; for (const q of pts) if (Math.abs(q.x - x) < Math.abs(best.x - x)) best = q;
            return { x, y: best.y, t };
        }).filter(Boolean);
        const dsLine = { data: pts, borderColor: p.ac, backgroundColor: hexa(p.ac, 0.10), fill: true, borderWidth: 2, pointRadius: 0, tension: 0.15, order: 2 };
        const dsMarks = { type: 'scatter', data: marks, showLine: false, fill: false, pointRadius: 4.5, pointHoverRadius: 7, pointBackgroundColor: marks.map(m => m.t.pnl_net >= 0 ? p.good : p.bad), pointBorderColor: p.card, pointBorderWidth: 2, order: 1 };
        let peak = ft && ft.sim_balance && state.chartRange === 'test' ? ft.sim_balance : -Infinity;
        const dd = pts.map(q => { peak = Math.max(peak, q.y); return { x: q.x, y: peak > 0 ? -((peak - q.y) / peak) * 100 : 0 }; });
        const ddMin = Math.min(-2, Math.min(...dd.map(q => q.y), 0) * 1.3);
        const dsDD = { data: dd, borderColor: p.bad, backgroundColor: hexa(p.bad, 0.18), fill: 'origin', borderWidth: 1.5, pointRadius: 0, tension: 0.15 };
        const tt = baseTooltip(p);
        if (charts.eq) {
            charts.eq.data.datasets = [dsMarks, dsLine]; charts.eq.update('none');
            charts.dd.data.datasets = [dsDD]; charts.dd.options.scales.y.min = ddMin; charts.dd.update('none');
            return;
        }
        charts.eq = new Chart(el, {
            type: 'line', data: { datasets: [dsMarks, dsLine] },
            options: { responsive: true, maintainAspectRatio: false, parsing: false, animation: false, interaction: { mode: 'nearest', intersect: false, axis: 'x' },
                plugins: { legend: { display: false }, tooltip: { ...tt, callbacks: {
                    title: (items) => fmt.dateTime(items[0].parsed.x),
                    label: (it) => it.raw.t ? `${it.raw.t.symbol} ${it.raw.t.side} ${fmt.usd(it.raw.t.pnl_net, { signed: true })} (${fmt.r(it.raw.t.r_multiple)})` : `equity ${fmt.usd(it.parsed.y)}`,
                } } },
                scales: { x: timeScale(p, false), y: { position: 'right', ticks: { color: p.mut, font: tickFont, maxTicksLimit: 5, callback: v => '$' + Number(v).toFixed(0) }, grid: { color: p.grid } } } },
        });
        charts.dd = new Chart(elD, {
            type: 'line', data: { datasets: [dsDD] },
            options: { responsive: true, maintainAspectRatio: false, parsing: false, animation: false, interaction: { mode: 'nearest', intersect: false, axis: 'x' },
                plugins: { legend: { display: false }, tooltip: { ...tt, callbacks: { title: (items) => fmt.dateTime(items[0].parsed.x), label: (it) => `drawdown ${fmt.pct(-it.parsed.y, 2, false)}` } } },
                scales: { x: timeScale(p, true), y: { position: 'right', min: ddMin, max: 0, ticks: { color: p.mut, font: tickFont, maxTicksLimit: 3, callback: v => Number(-v).toFixed(0) + '%' }, grid: { color: p.grid } } } },
        });
    }

    // ───────── Posições ─────────
    function ruler(p) {
        const en = p.entry_price, st = p.custom_stop_loss, mk = p.mark_price, ex = p.exit_level, tf = p.trail_floor_price, long = p.side === 'LONG';
        if (!en || !st) return '';
        const vals = [en, st, mk, ex, tf].filter(isNum);
        let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.08 || en * 0.01; lo -= pad; hi += pad;
        const at = (v) => ((v - lo) / (hi - lo) * 100).toFixed(2) + '%';
        const zone = long ? `left:${at(en)};right:0` : `left:0;right:calc(100% - ${at(en)})`;
        return `<div class="ruler" aria-hidden="true">
            <div class="line"></div><div class="zone" style="${zone}"></div>
            <span class="mk stop" style="left:${at(st)}" title="stop ${fmt.price(st)}"></span>
            <span class="mk entry" style="left:${at(en)}" title="entrada ${fmt.price(en)}"></span>
            ${isNum(ex) ? `<span class="mk exit" style="left:${at(ex)}" title="canal de saída ${fmt.price(ex)}"></span>` : ''}
            ${isNum(tf) ? `<span class="mk trail" style="left:${at(tf)}" title="piso trailing ${fmt.r(p.trail_floor_r)} ${fmt.price(tf)}"></span>` : ''}
            ${isNum(mk) ? `<span class="mk mark" style="left:${at(mk)}" title="preço ${fmt.price(mk)}"></span>` : ''}
        </div>
        <div class="ruler-l">
            <span>stop <b>${fmt.price(st)}</b>${isNum(p.stop_distance_pct) ? ` <small>(${fmt.pct(-p.stop_distance_pct, 2)})</small>` : ''}</span>
            <span>entrada <b>${fmt.price(en)}</b></span>
            <span>preço <b>${fmt.price(mk)}</b></span>
            <span>canal 20 <b>${isNum(ex) ? fmt.price(ex) : 'n/d'}</b></span>
        </div>`;
    }
    function renderPositions(positions) {
        const root = $('positions-list'); $('positions-count').textContent = positions.length;
        for (const s of closing) if (!positions.some(p => p.symbol === s)) closing.delete(s);   // símbolo já saiu: libera o botão numa reentrada
        const unreal = positions.map(p => p.unrealized_pnl_usd).filter(isNum), risk = positions.map(p => p.risk_usd).filter(isNum);
        $('pos-sum').textContent = positions.length ? `não realizado ${fmt.usd(sum(unreal), { signed: true })} · risco inicial total ${fmt.usd(sum(risk))}` : '';
        if (!positions.length) { root.innerHTML = '<p class="empty">Nenhuma posição aberta. O motor entra só no fechamento de barra 4h com rompimento de 40 barras.</p>'; return; }
        root.innerHTML = positions.map(p => {
            const age = p.entry_time ? (Date.now() - new Date(p.entry_time).getTime()) / 36e5 : null;
            const isD = p.strategy_type === 'donchian_4h';
            return `<div class="prow">
                <div class="prow-top">
                    <span class="psym">${esc(p.symbol)} <span class="side ${p.side === 'LONG' ? 'l' : 's'}">${esc(p.side)}</span>${isD ? '' : `<small class="dim">${esc(p.strategy_type)}</small>`}</span>
                    <span class="pr-pnl ${cls(p.unrealized_pnl_usd)}">${fmt.usd(p.unrealized_pnl_usd, { signed: true })}<small>${fmt.pct(p.unrealized_pnl_percent)}</small></span>
                    <span class="pr-r ${cls(p.r_now)}">${fmt.r(p.r_now)}<small>risco ${fmt.usd(p.risk_usd)}${isNum(p.trail_floor_r) ? ` · piso ${fmt.r(p.trail_floor_r)}` : ''}</small></span>
                    <span class="pr-meta">${fmt.price(p.quantity)} un · ${fmt.dur(age)}</span>
                    <span class="pr-close" data-symbol="${esc(p.symbol)}" data-pnl="${esc(fmt.usd(p.unrealized_pnl_usd, { signed: true }))}">${
                        closing.has(p.symbol) ? '<small class="dim">fechando…</small>' : '<button type="button" class="btn btn-quiet btn-xs" data-close="ask">Fechar</button>'
                    }</span>
                </div>
                ${isD ? ruler(p) : `<div class="ruler-l"><span>stop <b>${fmt.price(p.custom_stop_loss)}</b></span><span>TP <b>${fmt.price(p.custom_take_profit)}</b></span></div>`}
            </div>`;
        }).join('');
    }

    // Fechar posição: o botão vira Confirmar/Cancelar inline; o próximo poll remove o card.
    const closing = new Set();
    $('positions-list').addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-close]'); if (!btn) return;
        const cell = btn.closest('.pr-close'), sym = cell.dataset.symbol;
        if (btn.dataset.close === 'ask') {
            cell.innerHTML = `<small>fechar ${esc(sym)} a mercado (${esc(cell.dataset.pnl)})?</small>
                <button type="button" class="btn btn-danger btn-xs" data-close="yes">Confirmar</button>
                <button type="button" class="btn btn-quiet btn-xs" data-close="no">Cancelar</button>`;
        } else if (btn.dataset.close === 'no') {
            cell.innerHTML = '<button type="button" class="btn btn-quiet btn-xs" data-close="ask">Fechar</button>';
        } else {
            closing.add(sym); cell.innerHTML = '<small class="dim">fechando…</small>';
            try {
                const r = await fetch(`/api/positions/${encodeURIComponent(sym)}/close`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: sym }), credentials: 'include' });
                const body = await r.json().catch(() => ({}));
                if (!r.ok || !body.ok) throw new Error(body.error || ('HTTP ' + r.status));
            } catch (err) {
                closing.delete(sym);
                cell.innerHTML = `<small class="neg">${esc(err.message)}</small> <button type="button" class="btn btn-quiet btn-xs" data-close="ask">Fechar</button>`;
                return;
            }
            reqRefresh();
        }
    });

    // ───────── Motor Donchian ─────────
    function renderEngine(e, positions) {
        const has = e && e.params;
        $('eng-params').textContent = has ? `entrada ${e.params.entry_bars} barras · saída ${e.params.exit_bars} · stop ${e.params.stop_atr_mult}×ATR(${e.params.atr_bars}) · risco ${String(e.params.risk_percent).replace('.', ',')}% por trade · máx. ${e.params.max_positions} posições, ${e.params.max_gross_mult}× equity bruto` : 'estratégia Donchian não ativa neste processo';
        if (!has) { $('eng-sub').textContent = '—'; $('eng-list').innerHTML = ''; return; }
        const c = e.counters || {};
        const skips = e.skips || {}; const nSkip = sum(Object.values(skips));
        $('eng-sub').textContent = `universo ${e.universe_size} pares` + (e.universe_week ? ` (semana de ${fmt.date(e.universe_week)})` : ' (parcial)');
        const rows = [
            ['Última barra', e.bar_close ? `${fmt.dateTime(e.bar_close)} ${fmt.tzLabel()}` : '—'],
            ['Processada em', e.last_bar_processed_at ? `${fmt.dateTime(e.last_bar_processed_at)} ${fmt.tzLabel()}` : 'ainda não'],
            ['Pares analisados', c.total != null ? `${c.ok ?? 0} de ${c.total}` + (c.stale ? ` · ${c.stale} com dados parados` : '') + (e.pending_count ? ` · ${e.pending_count} pendentes` : '') : '—'],
            ['Rompimentos', c.total != null ? `${(c.LONG || 0) + (c.SHORT || 0)} (${c.LONG || 0} LONG / ${c.SHORT || 0} SHORT)` : '—'],
            ['Entradas / saídas', c.total != null ? `${c.entries || 0} / ${c.exits || 0}` : '—'],
            ['Pulados', String(nSkip)],
            ['Posições', `${c.open ?? positions.length} de ${e.params.max_positions}`],
        ];
        $('eng-list').innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
        $('eng-skip-list').innerHTML = Object.entries(skips).map(([k, v]) => `<li>${esc(k)} <b>×${v}</b></li>`).join('') + (c.deferred ? `<li>barra adiada ${c.deferred}× (${esc(c.defer_reason)})</li>` : '');
    }

    // ───────── Escada martingale ─────────
    function renderMartingale(m) {
        $('ft-card').hidden = !!m; $('mg-card').hidden = !m;
        if (!m) return;
        const p = m.params || {}, n = (v, d = 2) => String(Number(v).toFixed(d)).replace(/\.?0+$/, '').replace('.', ',');
        $('mg-params').textContent = `X ${n(p.step_percent)}% · m ${n(p.multiplier)} · TP ${n(p.tp_percent)}% · N ${p.max_levels} · base ${fmt.usd(m.base_notional)} · capital ${fmt.usd(m.capital)} · pico ${fmt.usd(m.peak_equity)} · ${p.leverage}×` + (m.portfolio_stops ? ` · ${m.portfolio_stops} stop(s) de portfólio` : '');
        const stop = $('mg-stop'); stop.textContent = `stop de portfólio em ${fmt.usd(m.portfolio_stop_equity)}` + (isNum(m.portfolio_stop_room_pct) ? ` — faltam ${fmt.pct(m.portfolio_stop_room_pct, 2, false)}` : '');
        stop.className = 'cs ' + (isNum(m.portfolio_stop_room_pct) && m.portfolio_stop_room_pct < 5 ? 'warn' : '');
        const away = (px, pct) => isNum(px) ? `<b>${fmt.price(px)}</b>${isNum(pct) ? ` <small>(${fmt.pct(pct)})</small>` : ''}` : '<b>—</b>';
        $('mg-list').innerHTML = (m.legs || []).map(l => `<div class="prow">
            <div class="prow-top">
                <span class="psym">${esc(l.symbol)} <span class="side ${l.side === 'LONG' ? 'l' : 's'}">${esc(l.side)}</span></span>
                <span class="pr-pnl ${cls(l.unrealized_pnl_usd)}">${fmt.usd(l.unrealized_pnl_usd, { signed: true })}</span>
                <span class="pr-r">${l.level}/${l.max_levels}<small>nível</small></span>
                <span class="pr-meta">${isNum(l.quantity) ? `${fmt.price(l.quantity)} un · ${fmt.usd(l.notional)}` : 'sem posição'}</span>
                <span class="pr-meta">média <b>${fmt.price(l.avg_price)}</b></span>
            </div>
            <div class="ruler-l">
                <span>próx. adição @ ${l.level >= l.max_levels ? '<b>teto da escada</b>' : away(l.next_add_price, l.next_add_pct)}</span>
                <span>TP @ ${away(l.tp_price, l.tp_pct)}</span>
                <span>preço <b>${fmt.price(l.mark_price)}</b></span>
            </div>
        </div>`).join('') || '<p class="empty">Nenhum par na escada (estratégia não iniciou ou nenhum par cabe na ordem base).</p>';
    }

    // ───────── Saúde ─────────
    function renderHealth(h, s) {
        const items = [];
        const dot = (st) => `<i class="dotc ${st}"></i>`;
        if (!h) { $('health-list').innerHTML = '<li class="empty">sem dados</li>'; return; }
        let worst = 'ok';
        const bump = (st) => { if (st === 'bad' || (st === 'warn' && worst === 'ok')) worst = st; };
        const ws = h.ws;
        if (ws) { const st = ws.recovering > 0 ? 'warn' : (ws.max_age_seconds > 900 ? 'bad' : 'ok'); bump(st);
            items.push([dot(st) + 'WebSocket de klines', `${ws.subscriptions} streams<small>${ws.reconnects} reconexões · ${ws.recovering} recuperando · último dado há ${fmt.ago(ws.max_age_seconds)}</small>`]); }
        else items.push([dot('') + 'WebSocket de klines', 'desligado<small>klines via REST</small>']);
        const us = h.user_stream;
        if (us) { const st = !us.started ? 'bad' : (us.error_count > 0 ? 'warn' : 'ok'); bump(st);
            items.push([dot(st) + 'User stream (fills)', `${us.started ? 'ativo' : 'parado'}<small>${us.error_count || 0} erros · ${us.restart_attempts || 0} restarts · última msg há ${fmt.ago(us.last_message_age_seconds)}</small>`]); }
        const rt = h.runtime || {};
        { const st = rt.loop_errors > 0 ? 'warn' : 'ok'; bump(st);
            items.push([dot(st) + 'Loop principal', `${rt.loop_errors || 0} erro${rt.loop_errors === 1 ? '' : 's'} desde o último relatório<small>${rt.last_error ? esc(rt.last_error) : 'sem erro registrado'} · ${rt.monitor_cycles || 0} ciclos</small>`]); }
        { const st = s.running ? 'ok' : 'bad'; bump(st);
            items.push([dot(st) + 'Processo', `${s.running ? 'rodando' : 'parado'} há ${fmt.ago(h.uptime_seconds)}<small>deploy ${h.deploy_sha || 'n/d'}${h.deploy_at ? ' · ' + fmt.dateTime(h.deploy_at) : ''}</small>`]); }
        items.push([dot(conn === 'connected' ? 'ok' : conn === 'polling' ? 'warn' : 'bad') + 'Dashboard', `${connLabel()}<small>estado a cada ${REFRESH}s · histórico quando um trade fecha · P&L reconciliado com fills da Binance</small>`]);
        $('health-list').innerHTML = items.map(([l, v]) => `<li><span class="hl">${l}</span><span class="hv">${v}</span></li>`).join('');
        const b = $('hl-badge'); b.textContent = worst === 'ok' ? 'tudo ok' : worst === 'warn' ? 'atenção' : 'problema'; b.className = 'badge ' + worst;
    }

    // ───────── Trades ─────────
    const reasonKind = (r) => /stop/i.test(r || '') ? 'stop' : /canal|channel|donchian: sa/i.test(r || '') ? 'channel' : 'other';
    function filteredTrades(all) {
        const f = state.filter;
        return (all || []).filter(t => t.exit_time || t.exit_price > 0).filter(t =>
            (f.strategy === 'all' || (f.strategy === 'other' ? t.strategy_type !== 'donchian_4h' : t.strategy_type === f.strategy)) &&
            (f.side === 'all' || t.side === f.side) &&
            (f.reason === 'all' || reasonKind(t.close_reason) === f.reason));
    }
    function renderTrades(all) {
        const rows = filteredTrades(all);
        const k = state.sortKey, dir = state.sortDir;
        rows.sort((a, b) => { let x = a[k], y = b[k]; if (k === 'exit_time') { x = new Date(x).getTime(); y = new Date(y).getTime(); } if (x == null) return 1; if (y == null) return -1; return (x > y ? 1 : x < y ? -1 : 0) * dir; });
        const n = rows.length, net = sum(rows.map(t => t.pnl_net || 0)), wins = rows.filter(t => t.pnl_net > 0).length;
        const rs = rows.map(t => t.r_multiple).filter(isNum), costs = sum(rows.map(t => (t.fees || 0) + (t.funding || 0)));
        $('tr-sub').textContent = `${n} trade(s) no filtro`;
        $('tr-stats').innerHTML = n ? `<span>líquido <b class="${cls(net)}">${fmt.usd(net, { signed: true })}</b></span><span>win rate <b>${fmt.pct(wins / n * 100, 0, false)}</b></span><span>R médio <b>${rs.length ? fmt.r(sum(rs) / rs.length) : '—'}</b></span><span>Σ R <b>${rs.length ? fmt.r(sum(rs)) : '—'}</b></span><span>custos <b>${fmt.usd(costs, { signed: true })}</b></span>` : '';
        const tb = $('trades-table').querySelector('tbody'), pager = $('trades-pager');
        if (!n) { tb.innerHTML = '<tr><td colspan="12" class="empty sans">Nenhum trade fechado neste filtro.</td></tr>'; pager.hidden = true; return; }
        const pages = Math.ceil(n / PAGE); state.page = Math.max(1, Math.min(state.page, pages));
        pager.hidden = pages <= 1;
        $('trades-page').textContent = `${state.page} / ${pages}`; $('trades-prev').disabled = state.page <= 1; $('trades-next').disabled = state.page >= pages;
        const start = (state.page - 1) * PAGE;
        tb.innerHTML = rows.slice(start, start + PAGE).map(t => {
            const r = t.r_multiple, w = isNum(r) ? Math.min(50, Math.abs(r) / 4 * 50) : 0;
            const rbar = isNum(r) ? `<span class="rbar"><i class="${r >= 0 ? 'pos' : 'neg'}" style="width:${w}%"></i></span>` : '';
            return `<tr>
                <td><small>${fmt.dateTime(t.exit_time || t.timestamp)}</small></td>
                <td class="sans"><b>${esc(t.symbol)}</b>${t.strategy_type !== 'donchian_4h' ? ` <small>${esc(t.strategy_type || '')}</small>` : ''}</td>
                <td class="sans"><span class="side ${t.side === 'LONG' ? 'l' : 's'}">${esc(t.side)}</span></td>
                <td class="r">${fmt.price(t.entry_price)}</td>
                <td class="r">${fmt.price(t.exit_price)}</td>
                <td class="r ${cls(r)}">${rbar}${fmt.r(r)}</td>
                <td class="r ${cls(t.pnl_net)}">${fmt.usd(t.pnl_net, { signed: true })}</td>
                <td class="r dim">${fmt.usd(-(t.fees || 0) + (t.funding || 0), { signed: true })}</td>
                <td class="r">${fmt.dur(t.duration_hours)}</td>
                <td class="r">${isNum(t.mfe_pct) ? fmt.pct(t.mfe_pct, 2, false) : '—'}</td>
                <td class="r">${isNum(t.mae_pct) ? fmt.pct(t.mae_pct, 2, false) : '—'}</td>
                <td class="sans"><small title="${esc(t.close_reason)}">${esc((t.close_reason || '—').replace(/^Donchian:\s*/i, ''))}</small></td>
            </tr>`;
        }).join('');
    }
    function initTrades() {
        $$('#trades-table th[data-sort]').forEach(th => th.querySelector('button').addEventListener('click', () => {
            const k = th.dataset.sort;
            if (state.sortKey === k) state.sortDir *= -1; else { state.sortKey = k; state.sortDir = k === 'symbol' || k === 'side' || k === 'close_reason' ? 1 : -1; }
            $$('#trades-table th[data-sort]').forEach(x => x.removeAttribute('aria-sort'));
            th.setAttribute('aria-sort', state.sortDir > 0 ? 'ascending' : 'descending');
            state.page = 1; if (state.hist) renderTrades(state.hist.recent_trades);
        }));
        [['f-strategy', 'strategy'], ['f-side', 'side'], ['f-reason', 'reason']].forEach(([id, key]) => {
            const el = $(id); el.value = state.filter[key];
            el.addEventListener('change', () => { state.filter[key] = el.value; state.page = 1; if (state.hist) renderTrades(state.hist.recent_trades); });
        });
        $('trades-prev').addEventListener('click', () => { state.page--; renderTrades(state.hist.recent_trades); });
        $('trades-next').addEventListener('click', () => { state.page++; renderTrades(state.hist.recent_trades); });
    }

    // ───────── Distribuição de R + MFE × R ─────────
    function renderRHist(h) {
        const el = $('r-chart'); if (!el || typeof Chart === 'undefined' || !h) return;
        const p = palette();
        $('rh-n').textContent = `${h.n || 0} trades com stop registrado`;
        const colors = (h.labels || []).map((_, i) => (i < (h.edges || []).indexOf(0) + 1 ? p.bad : p.good));
        if (charts.r) { charts.r.data.labels = h.labels; charts.r.data.datasets[0].data = h.counts; charts.r.data.datasets[0].backgroundColor = colors; charts.r.update('none'); return; }
        charts.r = new Chart(el, {
            type: 'bar',
            data: { labels: h.labels, datasets: [{ data: h.counts, backgroundColor: colors, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: 'bottom', maxBarThickness: 24, categoryPercentage: 0.9, barPercentage: 0.9 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { ...baseTooltip(p), callbacks: { label: (it) => `${it.parsed.y} trade(s)` } } },
                scales: { x: { ticks: { color: p.mut, font: tickFont, maxRotation: 60, autoSkip: true }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: p.mut, font: tickFont, precision: 0 }, grid: { color: p.grid } } } },
        });
    }
    function renderExcursions(list) {
        const el = $('mfe-chart'); if (!el || typeof Chart === 'undefined') return;
        const p = palette();
        const pts = (list || []).filter(e => isNum(e.mfe) && isNum(e.r)).map(e => ({ x: e.mfe, y: e.r, e }));
        const ds = { data: pts, pointRadius: 5, pointHoverRadius: 8, pointBackgroundColor: pts.map(q => q.y >= 0 ? p.good : p.bad), pointBorderColor: p.card, pointBorderWidth: 1.5 };
        if (charts.mfe) { charts.mfe.data.datasets = [ds]; charts.mfe.update('none'); return; }
        charts.mfe = new Chart(el, {
            type: 'scatter', data: { datasets: [ds] },
            options: { responsive: true, maintainAspectRatio: false, animation: false,
                plugins: { legend: { display: false }, tooltip: { ...baseTooltip(p), callbacks: { title: (it) => it[0].raw.e.symbol, label: (it) => `MFE ${fmt.pct(it.raw.e.mfe, 2, false)} · MAE ${isNum(it.raw.e.mae) ? fmt.pct(it.raw.e.mae, 2, false) : 'n/d'} · ${fmt.r(it.raw.y)} (${fmt.usd(it.raw.e.pnl, { signed: true })})` } } },
                scales: { x: { title: { display: true, text: 'MFE (% a favor)', color: p.mut, font: tickFont }, beginAtZero: true, ticks: { color: p.mut, font: tickFont, callback: v => v + '%' }, grid: { color: p.grid } },
                    y: { title: { display: true, text: 'R realizado', color: p.mut, font: tickFont }, ticks: { color: p.mut, font: tickFont, callback: v => v + 'R' }, grid: { color: p.grid } } } },
        });
    }

    // ───────── Calendário + agregados ─────────
    function renderCalendar(days, ft) {
        const root = $('calendar'); if (!root) return;
        const byDay = new Map((days || []).map(d => [d.day, d]));
        const startIso = ft && ft.start ? ft.start.slice(0, 10) : (days && days[0] && days[0].day);
        if (!startIso) { root.innerHTML = '<span class="empty">sem trades fechados</span>'; $('cal-sum').textContent = '—'; $('cal-table').querySelector('tbody').innerHTML = ''; return; }
        const start = new Date(startIso + 'T00:00:00Z'); start.setUTCDate(start.getUTCDate() - start.getUTCDay());
        const today = new Date(); const end = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()));
        const last = new Date(end); last.setUTCDate(last.getUTCDate() + (6 - last.getUTCDay()));
        const maxAbs = Math.max(0.01, ...(days || []).map(d => Math.abs(d.net)));
        let html = '';
        for (let d = new Date(start); d <= last; d.setUTCDate(d.getUTCDate() + 1)) {
            const key = d.toISOString().slice(0, 10), v = byDay.get(key);
            const future = d > end, before = key < startIso;
            const c = v ? (v.net > 0 ? 'pos' : v.net < 0 ? 'neg' : '') : '';
            const op = v ? (0.35 + 0.65 * Math.min(1, Math.abs(v.net) / maxAbs)).toFixed(2) : '';
            html += `<i class="${c}${future || before ? ' future' : ''}" style="${op ? `opacity:${op}` : ''}" title="${fmt.day(key)}${v ? `: ${fmt.usd(v.net, { signed: true })} em ${v.trades} trade(s)` : ''}"></i>`;
        }
        root.innerHTML = html;
        const n = (days || []).length, pos = (days || []).filter(d => d.net > 0).length;
        $('cal-sum').textContent = n ? `${pos} dias positivos / ${n - pos} negativos` : 'nenhum dia com trade';
        // agregado semanal (segunda) / mensal
        const key = (day) => {
            if (state.agg === 'month') return day.slice(0, 7);
            const d = new Date(day + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7)); return d.toISOString().slice(0, 10);
        };
        const groups = new Map();
        (days || []).forEach(d => { const k = key(d.day); const g = groups.get(k) || { net: 0, trades: 0, wins: 0 }; g.net += d.net; g.trades += d.trades; g.wins += d.wins; groups.set(k, g); });
        const label = (k) => state.agg === 'month' ? new Date(k + '-01T00:00:00Z').toLocaleDateString('pt-BR', { month: 'short', year: 'numeric', timeZone: 'UTC' }) : `semana de ${fmt.date(k)}`;
        $('cal-table').querySelector('tbody').innerHTML = [...groups.keys()].sort().reverse().map(k => { const g = groups.get(k);
            return `<tr><th scope="row">${label(k)}</th><td class="r">${g.trades}</td><td class="r">${fmt.pct(g.wins / g.trades * 100, 0, false)}</td><td class="r ${cls(g.net)}">${fmt.usd(g.net, { signed: true })}</td></tr>`; }).join('')
            || '<tr><td colspan="4" class="empty">sem trades fechados</td></tr>';
    }

    // ───────── Ganhos e Perdas de Futuros (/api/pnl_report) ─────────
    const PNL_PAGE = 20;
    const dayStr = (d) => d.toISOString().slice(0, 10);
    const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return dayStr(d); };
    const dayRange = (from, to) => { const out = []; for (let d = from; d <= to; d = addDays(d, 1)) out.push(d); return out; };
    const pnlState = { days: 7, from: null, to: null, tab: 'overview', mode: 'calendar', month: null, page: 1, symDir: -1 };
    // presets mandam só `days`: o servidor ancora no dia de trading dele (BRT), não na data do navegador
    const pnlQuery = () => pnlState.days === 'custom' ? (pnlState.from && pnlState.to ? `?from=${pnlState.from}&to=${pnlState.to}` : '') : `?days=${pnlState.days}`;
    async function fetchPnl() {
        try {
            const r = await fetch('/api/pnl_report' + pnlQuery(), { credentials: 'include' }); if (!r.ok) throw new Error('HTTP ' + r.status);
            state.pnl = await r.json();
            if (state.pnl.fx) state.fx = state.pnl.fx;
            try { renderPnl(state.pnl); } catch (err) { console.error('render do P&L falhou', err); }
        } catch (err) { console.warn('P&L falhou:', err); $('pnl-sub').textContent = 'sem resposta do servidor'; }
    }
    function pctTxt(w) { return isNum(w && w.pct) ? fmt.pct(w.pct) : '—'; }
    function renderPnl(p) {
        const t = p.totals || {};
        $('pnl-sub').textContent = `${fmt.day(p.from)} → ${fmt.day(p.to)} · ${t.trades || 0} trade(s) · dia de trading ${fmt.tzLabel()}`;
        const td = p.today_pnl || {};
        const tv = $('pnl-today-usd'); tv.innerHTML = fmt.usd(td.usd, { signed: true }) + brlTag(td.usd); tv.className = 'tv ' + cls(td.usd);
        const tp = $('pnl-today-pct'); tp.textContent = pctTxt(td); tp.className = 'pnl-pct ' + cls(td.usd);
        const w = p.windows || {};
        [['7d', w.d7], ['30d', w.d30]].forEach(([k, v]) => { const b = $(`pnl-${k}-usd`); b.innerHTML = fmt.usd(v && v.usd, { signed: true }) + brlTag(v && v.usd); b.className = cls(v && v.usd); const s = $(`pnl-${k}-pct`); s.textContent = pctTxt(v); s.className = cls(v && v.usd); });
        const all = $('pnl-all-usd'); all.innerHTML = fmt.usd(w.all && w.all.usd, { signed: true }) + brlTag(w.all && w.all.usd); all.className = cls(w.all && w.all.usd);
        $('pnl-profit').innerHTML = fmt.usd(t.profit, { signed: true }) + brlTag(t.profit);
        $('pnl-loss').innerHTML = fmt.usd(t.loss, { signed: true }) + brlTag(t.loss);
        const net = $('pnl-net'); net.innerHTML = fmt.usd(t.net, { signed: true }) + (isNum(t.pct) ? ` (${fmt.pct(t.pct)})` : '') + brlTag(t.net); net.className = cls(t.net);
        $('pnl-more').innerHTML = [
            ['Trades', String(t.trades || 0)], ['Win rate', fmt.pct(t.win_rate, 0, false)],
            ['Taxas', fmt.usd(-(t.fees || 0), { signed: true })], ['Funding', fmt.usd(t.funding, { signed: true })],
            ['Maior ganho', fmt.usd(t.best, { signed: true })], ['Maior perda', fmt.usd(t.worst, { signed: true })],
            ['Equity no início do período', fmt.usd(t.start_equity)], ['Fórmula do %', 'P&L ÷ (capital + realizado antes do período)'],
        ].map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
        if (pnlState.days === 'custom') { $('pnl-from').value = p.from; $('pnl-to').value = p.to; }
        if (!pnlState.month || pnlState.month < p.from.slice(0, 7) || pnlState.month > p.to.slice(0, 7)) pnlState.month = p.to.slice(0, 7);
        renderPnlCalendar(p); renderPnlBars(p); renderPnlCum(p); renderPnlPct(p); renderPnlAssets(p);
        renderPnlTrades(p); renderPnlSymbols(p); renderPnlIncome(p);
    }
    function renderPnlCalendar(p) {
        const byDay = new Map((p.daily || []).map(d => [d.day, d]));
        const [y, m] = pnlState.month.split('-').map(Number);
        $('pnl-cal-month').textContent = pnlState.month;
        $('pnl-cal-prev').disabled = pnlState.month <= p.from.slice(0, 7); $('pnl-cal-next').disabled = pnlState.month >= p.to.slice(0, 7);
        const first = new Date(Date.UTC(y, m - 1, 1)), nDays = new Date(Date.UTC(y, m, 0)).getUTCDate();
        let html = ['D', 'S', 'T', 'Q', 'Q', 'S', 'S'].map(d => `<span class="wd">${d}</span>`).join('') + '<i></i>'.repeat(first.getUTCDay());
        for (let d = 1; d <= nDays; d++) {
            const key = `${pnlState.month}-${String(d).padStart(2, '0')}`, v = byDay.get(key);
            const out = key < p.from || key > p.to, future = key > p.today;
            const c = v ? (v.net > 0 ? 'pos' : v.net < 0 ? 'neg' : 'zero') : (out || future ? 'off' : 'zero');
            html += `<span class="day ${c}" role="gridcell" title="${fmt.day(key)}${v ? `: ${fmt.usd(v.net, { signed: true })} em ${v.trades} trade(s)` : ''}"><small>${d}</small>${v ? `<b>${fmt.usd(v.net, { signed: true })}</b>` : (out || future ? '' : '<b>0</b>')}</span>`;
        }
        $('pnl-cal').innerHTML = html;
    }
    function pnlBase(p, id) { const pal = palette(); return { pal, el: $(id), ok: !!$(id) && typeof Chart !== 'undefined', days: dayRange(p.from, p.to) }; }
    function renderPnlBars(p) {
        const { pal, el, ok, days } = pnlBase(p, 'pnl-bars-chart'); if (!ok) return;
        const byDay = new Map((p.daily || []).map(d => [d.day, d.net]));
        const data = days.map(d => byDay.get(d) ?? 0), colors = data.map(v => v < 0 ? pal.bad : pal.good);
        if (charts.pnlBars) { charts.pnlBars.data.labels = days; charts.pnlBars.data.datasets[0].data = data; charts.pnlBars.data.datasets[0].backgroundColor = colors; charts.pnlBars.update('none'); return; }
        charts.pnlBars = new Chart(el, { type: 'bar', data: { labels: days, datasets: [{ data, backgroundColor: colors, borderRadius: 3, maxBarThickness: 28 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { ...baseTooltip(pal), callbacks: { title: (it) => fmt.day(it[0].label), label: (it) => fmt.usd(it.parsed.y, { signed: true }) } } },
                scales: { x: { ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 8, callback: mmdd }, grid: { display: false } }, y: { ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 5, callback: v => fmt.usd(v, { signed: true }) }, grid: { color: pal.grid } } } } });
    }
    // rótulo do eixo x lido da escala (não de um array fechado na criação: o período muda e os labels crescem)
    function mmdd(v) { return String(this.getLabelForValue(v)).slice(5); }
    function lineOpts(pal, fmtY) {
        return { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: 'index', intersect: false },
            plugins: { legend: { display: false }, tooltip: { ...baseTooltip(pal), displayColors: true, callbacks: { title: (it) => fmt.day(it[0].label), label: (it) => `${it.dataset.label || ''} ${fmtY(it.parsed.y)}`.trim() } } },
            scales: { x: { ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 6, callback: mmdd }, grid: { display: false } }, y: { position: 'right', ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 5, callback: fmtY }, grid: { color: pal.grid } } } };
    }
    function renderPnlCum(p) {
        const { pal, el, ok, days } = pnlBase(p, 'pnl-cum-chart'); if (!ok) return;
        const byDay = new Map((p.cumulative || []).map(c => [c.day, c.value]));
        let last = 0; const data = days.map(d => { if (byDay.has(d)) last = byDay.get(d); return last; });
        const v = $('pnl-cum-val'); v.textContent = fmt.usd(last, { signed: true }) + ` em ${fmt.day(p.to)}`; v.className = 'pnl-val ' + cls(last);
        const ds = { data, borderColor: last < 0 ? pal.bad : pal.good, backgroundColor: hexa(last < 0 ? pal.bad : pal.good, 0.12), fill: true, borderWidth: 2, pointRadius: 0, tension: 0.2 };
        if (charts.pnlCum) { charts.pnlCum.data.labels = days; charts.pnlCum.data.datasets = [ds]; charts.pnlCum.update('none'); return; }
        charts.pnlCum = new Chart(el, { type: 'line', data: { labels: days, datasets: [ds] }, options: lineOpts(pal, v2 => fmt.usd(v2, { signed: true })) });
    }
    function renderPnlPct(p) {
        const { pal, el, ok, days } = pnlBase(p, 'pnl-pct-chart'); if (!ok) return;
        const eq = p.totals && p.totals.start_equity;
        const byDay = new Map((p.cumulative || []).map(c => [c.day, c.value]));
        let last = 0; const bot = days.map(d => { if (byDay.has(d)) last = byDay.get(d); return eq > 0 ? last / eq * 100 : null; });
        const ref = (sym) => { const m = new Map(((p.reference || {})[sym] || []).map(r => [r.day, r.pct])); let l = null; return days.map(d => { if (m.has(d)) l = m.get(d); return l; }); };
        const series = [['Bot', bot, pal.warn], ['BTCUSDT PERP', ref('BTCUSDT'), pal.ac], ['BNBUSDT PERP', ref('BNBUSDT'), hexa(pal.ac, 0.55)]];
        const dss = series.map(([label, data, color]) => ({ label, data, borderColor: color, borderWidth: label === 'Bot' ? 2 : 1.5, pointRadius: 0, tension: 0.2, spanGaps: true }));
        const lastOf = (a) => { for (let i = a.length - 1; i >= 0; i--) if (isNum(a[i])) return a[i]; return null; };
        $('pnl-pct-legend').innerHTML = series.map(([label, data, color]) => `<i class="sw" style="background:${color}"></i>${label === 'Bot' ? '% de G&amp;P Cum.' : `% de Preço de Referência de ${label} Cum.`} <b class="${cls(lastOf(data))}">${fmt.pct(lastOf(data))}</b>`).join('');
        if (charts.pnlPct) { charts.pnlPct.data.labels = days; charts.pnlPct.data.datasets = dss; charts.pnlPct.update('none'); return; }
        charts.pnlPct = new Chart(el, { type: 'line', data: { labels: days, datasets: dss }, options: lineOpts(pal, v => fmt.pct(v)) });
    }
    function renderPnlAssets(p) {
        const a = p.assets || {}, pal = palette(), el = $('pnl-assets-chart');
        $('pnl-assets-sub').textContent = a.ok ? 'carteira de futuros USDT-M' : 'saldo indisponível (Binance não respondeu)';
        $('pnl-assets-total').textContent = a.ok ? fmt.usd(a.total) : '—';
        $('pnl-assets-list').innerHTML = (a.items || []).map(i => `<li><i class="sw" style="background:${pal.warn}"></i>${esc(i.asset)} <b>${fmt.usd(i.balance)}</b> <small>${a.total ? fmt.pct(i.balance / a.total * 100, 1, false) : ''}</small></li>`).join('');
        if (!el || typeof Chart === 'undefined') return;
        const data = (a.items || []).map(i => i.balance), colors = [pal.warn, pal.ac, pal.good, pal.bad];
        if (charts.pnlAssets) { charts.pnlAssets.data.datasets[0].data = data.length ? data : [1]; charts.pnlAssets.update('none'); return; }
        charts.pnlAssets = new Chart(el, { type: 'doughnut', data: { labels: (a.items || []).map(i => i.asset), datasets: [{ data: data.length ? data : [1], backgroundColor: data.length ? colors : [pal.el], borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, cutout: '72%', plugins: { legend: { display: false }, tooltip: { ...baseTooltip(pal), callbacks: { label: (it) => `${it.label} ${fmt.usd(it.parsed)}` } } } } });
    }
    function renderPnlTrades(p) {
        const rows = p.trades || [], tb = $('pnl-trades').querySelector('tbody'), pager = $('pnl-pager');
        if (!rows.length) { tb.innerHTML = '<tr><td colspan="9" class="empty sans">Nenhum trade fechado no período.</td></tr>'; pager.hidden = true; return; }
        const pages = Math.ceil(rows.length / PNL_PAGE); pnlState.page = Math.max(1, Math.min(pnlState.page, pages));
        pager.hidden = pages <= 1; $('pnl-page').textContent = `${pnlState.page} / ${pages}`; $('pnl-prev').disabled = pnlState.page <= 1; $('pnl-next').disabled = pnlState.page >= pages;
        const start = (pnlState.page - 1) * PNL_PAGE;
        tb.innerHTML = rows.slice(start, start + PNL_PAGE).map(t => `<tr>
            <td><small>${fmt.dateTime(t.exit_at)}</small></td>
            <td class="sans"><b>${esc(t.symbol)}</b></td>
            <td class="sans"><span class="side ${t.side === 'LONG' ? 'l' : 's'}">${esc(t.side)}</span></td>
            <td class="r">${fmt.price(t.qty)}</td><td class="r">${fmt.price(t.entry_price)}</td><td class="r">${fmt.price(t.exit_price)}</td>
            <td class="r ${cls(t.pnl_net)}">${fmt.usd(t.pnl_net, { signed: true })}${brlTag(t.pnl_net)}</td>
            <td class="r dim">${fmt.usd(-(t.fees || 0), { signed: true })}</td>
            <td class="sans"><small title="${esc(t.close_reason)}">${esc(t.close_reason || '—')}</small></td></tr>`).join('');
    }
    function renderPnlSymbols(p) {
        const rows = [...(p.by_symbol || [])].sort((a, b) => (a.net - b.net) * pnlState.symDir), tb = $('pnl-symbols').querySelector('tbody');
        if (!rows.length) { tb.innerHTML = '<tr><td colspan="6" class="empty sans">Nenhum trade fechado no período.</td></tr>'; return; }
        const max = Math.max(0.01, ...rows.map(r => Math.abs(r.net)));
        tb.innerHTML = rows.map(r => `<tr><td class="sans"><b>${esc(r.symbol)}</b></td><td class="r">${r.trades}</td><td class="r">${fmt.pct(r.win_rate, 0, false)}</td>
            <td class="r ${cls(r.net)}"><span class="rbar"><i class="${r.net >= 0 ? 'pos' : 'neg'}" style="width:${(Math.abs(r.net) / max * 50).toFixed(1)}%"></i></span>${fmt.usd(r.net, { signed: true })}${brlTag(r.net)}</td>
            <td class="r dim">${fmt.usd(-(r.fees || 0), { signed: true })}</td><td class="r dim">${fmt.usd(r.funding, { signed: true })}</td></tr>`).join('');
    }
    const INCOME_LABEL = { REALIZED_PNL: 'P&L realizado', COMMISSION: 'Comissão', FUNDING_FEE: 'Funding', TRANSFER: 'Transferência' };
    function renderPnlIncome(p) {
        const tb = $('pnl-income').querySelector('tbody'), tot = p.income_totals || {};
        $('pnl-income-totals').innerHTML = p.income_ok ? Object.keys(INCOME_LABEL).map(k => `<span>${INCOME_LABEL[k]} <b class="${cls(tot[k])}">${fmt.usd(tot[k] || 0, { signed: true })}</b></span>`).join('') : '<span class="warn">Income da Binance indisponível no momento; o restante da aba usa o banco local.</span>';
        const rows = p.income || [];
        if (!rows.length) { tb.innerHTML = `<tr><td colspan="4" class="empty sans">${p.income_ok ? 'Nenhum lançamento no período.' : 'Sem dados.'}</td></tr>`; return; }
        tb.innerHTML = rows.slice(0, 300).map(i => `<tr><td><small>${fmt.dateTime(i.time)}</small></td><td class="sans">${esc(INCOME_LABEL[i.type] || i.type)}</td><td class="sans">${esc(i.symbol || '—')}</td><td class="r ${cls(i.amount)}">${fmt.usd(i.amount, { signed: true })}</td></tr>`).join('');
    }
    function initPnl() {
        $$('#pnl-ranges .chip').forEach(b => b.addEventListener('click', () => {
            const custom = b.dataset.days === 'custom'; $('pnl-custom').hidden = !custom;
            pnlState.days = custom ? 'custom' : Number(b.dataset.days); pnlState.page = 1; pnlState.month = null;
            if (!custom || (pnlState.from && pnlState.to)) fetchPnl();
        }));
        $('pnl-custom').addEventListener('submit', (e) => { e.preventDefault(); pnlState.from = $('pnl-from').value; pnlState.to = $('pnl-to').value; pnlState.page = 1; pnlState.month = null; fetchPnl(); });
        $$('#pnl-tabs .chip').forEach(b => b.addEventListener('click', () => { pnlState.tab = b.dataset.tab; $$('.pnl-tab').forEach(t => { t.hidden = t.dataset.tab !== pnlState.tab; }); Object.values(charts).forEach(c => c.resize()); }));
        $$('#pnl-daily-mode .chip').forEach(b => b.addEventListener('click', () => { pnlState.mode = b.dataset.mode; $('pnl-cal-wrap').hidden = pnlState.mode !== 'calendar'; $('pnl-bars-wrap').hidden = pnlState.mode !== 'bars'; if (charts.pnlBars) charts.pnlBars.resize(); }));
        const shiftMonth = (n) => { const [y, m] = pnlState.month.split('-').map(Number); pnlState.month = dayStr(new Date(Date.UTC(y, m - 1 + n, 1))).slice(0, 7); renderPnlCalendar(state.pnl); };
        $('pnl-cal-prev').addEventListener('click', () => shiftMonth(-1)); $('pnl-cal-next').addEventListener('click', () => shiftMonth(1));
        $('pnl-prev').addEventListener('click', () => { pnlState.page--; renderPnlTrades(state.pnl); }); $('pnl-next').addEventListener('click', () => { pnlState.page++; renderPnlTrades(state.pnl); });
        $('pnl-symbols').querySelector('th[data-sort] button').addEventListener('click', (e) => { pnlState.symDir *= -1; e.target.closest('th').setAttribute('aria-sort', pnlState.symDir > 0 ? 'ascending' : 'descending'); renderPnlSymbols(state.pnl); });
    }

    // ───────── Aplicar payloads ─────────
    function renderLive(l) {
        const s = l.summary || {}, ft = l.forward_test || {}, h = l.health, e = l.engine, pos = l.positions || [];
        renderTop(s, h, e);
        renderStatus(s, ft, e, pos);
        renderForwardTest(ft);
        renderPerf(ft);
        renderPositions(pos);
        renderMartingale(l.martingale);
        renderEngine(e, pos);
        renderHealth(h, s);
        renderRHist(ft.r_histogram);
        renderExcursions(ft.excursions);
        renderCalendar(ft.calendar, ft);
        if (state.hist) renderEquity(state.hist.portfolio_history, state.hist.recent_trades, ft);
    }
    function renderHistory(hs) {
        renderTrades(hs.recent_trades || []);
        renderEquity(hs.portfolio_history || [], hs.recent_trades || [], state.live && state.live.forward_test);
    }
    let lastClosed = null;
    function applyLive(l) {
        if (!l || !l.summary) return;
        state.live = l;
        fmt.tzOffset = Number(l.tz_offset_hours) || 0;
        const hadFx = isNum(state.fx && state.fx.usd_brl); if (l.fx) state.fx = l.fx;
        if (!hadFx && isNum(state.fx && state.fx.usd_brl) && state.pnl) try { renderPnl(state.pnl); } catch (err) { console.error('render do P&L falhou', err); }
        try { renderLive(l); } catch (err) { console.error('render falhou', err); }
        resetCountdown();
        // trade fechou desde o último poll: o histórico mudou
        const closed = l.summary.closed_trades;
        if (lastClosed !== null && closed !== lastClosed) fetchHistory();
        lastClosed = closed;
    }
    async function fetchHistory() {
        try {
            const r = await fetch('/api/history', { credentials: 'include' }); if (!r.ok) throw new Error('HTTP ' + r.status);
            state.hist = await r.json();
            try { renderHistory(state.hist); } catch (err) { console.error('render do histórico falhou', err); }
        } catch (err) { console.warn('histórico falhou:', err); }
        fetchPnl();   // trade fechou (ou passaram 2 min): a aba de P&L acompanha
    }
    function initChips() {
        $$('.chips .chip').forEach(b => b.addEventListener('click', () => {
            const group = b.parentElement;
            Array.from(group.children).forEach(x => { x.classList.remove('on'); x.setAttribute('aria-selected', 'false'); });
            b.classList.add('on'); b.setAttribute('aria-selected', 'true');
            if (b.dataset.range) { state.chartRange = b.dataset.range; if (state.hist) renderEquity(state.hist.portfolio_history || [], state.hist.recent_trades || [], state.live && state.live.forward_test); }
            if (b.dataset.agg) { state.agg = b.dataset.agg; if (state.live) renderCalendar(state.live.forward_test.calendar, state.live.forward_test); }
        }));
    }

    // ───────── Controles + modal ─────────
    const modal = $('confirm-modal');
    let pending = null, lastFocus = null;
    function ask(title, msg, action) { $('confirm-title').textContent = title; $('confirm-message').textContent = msg; pending = action; lastFocus = document.activeElement; modal.hidden = false; $('confirm-cancel').focus(); }
    function closeModal() { modal.hidden = true; pending = null; if (lastFocus) lastFocus.focus(); }
    $('confirm-cancel').addEventListener('click', closeModal);
    $('confirm-ok').addEventListener('click', () => { const a = pending; closeModal(); if (a) a(); });
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !modal.hidden) closeModal(); });
    async function postControl(path, body) {
        try {
            const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}), credentials: 'include' });
            if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + (await r.text()));
            reqRefresh();
        } catch (e) { const b = $('pause-banner'); b.textContent = 'Falha no comando: ' + e.message; b.className = 'banner bad'; b.hidden = false; }
    }
    $$('[data-action]').forEach(btn => btn.addEventListener('click', () => {
        const a = btn.dataset.action;
        if (a === 'pause') ask('Pausar o bot', 'O bot para de abrir posições novas. As abertas seguem com stop na exchange e saída pelo canal. A pausa é só em memória: um restart (deploy) volta ao estado do .env.', () => postControl('/api/control/pause'));
        else if (a === 'resume') ask('Retomar o bot', 'O bot volta a abrir posições no próximo fechamento de barra 4h com sinal.', () => postControl('/api/control/resume'));
        else if (a === 'close_all') ask('Fechar todas as posições', 'Envia ordens a mercado fechando todas as posições agora. Isso encerra trades fora da regra do teste e entra na amostra. Confirma?', () => postControl('/api/control/close_all', { reason: 'Dashboard: fechar tudo' }));
    }));

    // ───────── Conexão ─────────
    const pollInterval = (parseInt(document.body.dataset.pollInterval, 10) || 5) * 1000;
    const HISTORY_INTERVAL = 120 * 1000;
    let pollTimer = null, socket = null, conn = 'connecting';
    async function pollOnce() { try { const r = await fetch('/api/live', { credentials: 'include' }); if (!r.ok) throw new Error('HTTP ' + r.status); applyLive(await r.json()); } catch (e) { console.warn('Poll falhou:', e); setConn('down'); } }
    function startPolling() { if (pollTimer) return; pollOnce(); pollTimer = setInterval(pollOnce, pollInterval); }
    function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
    const connLabel = () => conn === 'connected' ? 'ao vivo' : conn === 'polling' ? 'polling' : conn === 'down' ? 'sem resposta' : 'conectando';
    function setConn(st) { conn = st; }

    const REFRESH = Math.max(1, Math.round(pollInterval / 1000));
    let left = REFRESH;
    function resetCountdown() { left = REFRESH; }
    function reqRefresh() { if (socket && socket.connected) socket.emit('request_snapshot'); else pollOnce(); }
    setInterval(() => { left -= 1; if (left <= 0) { reqRefresh(); left = REFRESH; } if (state.live) renderNext(); }, 1000);
    setInterval(fetchHistory, HISTORY_INTERVAL);

    // ───────── Boot ─────────
    initTheme(); initChips(); initTrades(); initPnl();
    fetchHistory();   // também dispara o primeiro fetchPnl()
    if (typeof io !== 'undefined') {
        socket = io({ transports: ['polling', 'websocket'], reconnectionDelayMax: 10000 });
        socket.on('connect', () => { setConn('connected'); stopPolling(); socket.emit('request_snapshot'); });
        socket.on('disconnect', () => { setConn('polling'); startPolling(); });
        socket.on('connect_error', () => { setConn('polling'); startPolling(); });
        socket.on('snapshot', applyLive);
        socket.on('control_changed', () => reqRefresh());
        ['position_opened', 'position_closed'].forEach(ev => socket.on(ev, () => { reqRefresh(); fetchHistory(); }));
        ['regime_changed', 'balance_update'].forEach(ev => socket.on(ev, () => reqRefresh()));
    } else { setConn('polling'); startPolling(); }
    setTimeout(() => { if (!state.live) { setConn('polling'); startPolling(); } }, 1500);
})();
