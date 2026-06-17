/*
 * Trading Bot Dashboard — client logic.
 *
 * Layout estilo Capsule: sidebar nav (router de views), main center,
 * right panel com hero balance + controles + atividade.
 *
 * Eventos do server:
 *   connect / disconnect / connect_error
 *   snapshot          — payload completo (renderiza tudo)
 *   position_opened   — emite request_snapshot
 *   position_closed
 *   regime_changed
 *   control_changed   — { paused: bool }
 *   balance_update
 */

(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    const fmt = {
        usd(value, opts = {}) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            const sign = opts.signed && value > 0 ? '+' : '';
            return sign + '$' + Number(value).toLocaleString('en-US', {
                minimumFractionDigits: 2, maximumFractionDigits: 2,
            });
        },
        num(value, decimals = 4) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            return Number(value).toFixed(decimals);
        },
        pct(value) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            const sign = value > 0 ? '+' : '';
            return sign + Number(value).toFixed(2) + '%';
        },
        time(iso) {
            if (!iso) return '—';
            const d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            return d.toLocaleTimeString('pt-BR', { hour12: false });
        },
        dateTime(iso) {
            if (!iso) return '—';
            const d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            return d.toLocaleString('pt-BR', { hour12: false });
        },
    };

    const state = {
        snapshot: null,
        equityChart: null,
        equitySeries: null,
        chartRange: 'all',
        dailyPnlChart: null,
        dailyPnlSeries: null,
        cumPnlChart: null,
        cumPnlSeries: null,
    };

    function classify(value) {
        if (value > 0) return 'pos';
        if (value < 0) return 'neg';
        return '';
    }
    function setPnlClass(el, value) {
        el.classList.remove('pos', 'neg');
        const cls = classify(value);
        if (cls) el.classList.add(cls);
    }

    // ───────── Theme ─────────
    const THEME_KEY = 'tradingbot-theme';
    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem(THEME_KEY, theme);
        // Re-renderiza chart se necessário (cores trocam)
        if (state.equityChart && state.snapshot) {
            destroyChart();
            renderEquity(state.snapshot.portfolio_history || []);
        }
        if ((state.dailyPnlChart || state.cumPnlChart) && state.snapshot) {
            destroyAnalysisCharts();
            const analysisView = $('analysis-view');
            if (analysisView && analysisView.classList.contains('active')) {
                renderAnalysisCharts(state.snapshot.daily_history || []);
            }
        }
    }
    function initTheme() {
        const stored = localStorage.getItem(THEME_KEY) || 'dark';
        applyTheme(stored);
        $('theme-toggle').addEventListener('click', () => {
            const current = document.documentElement.getAttribute('data-theme') || 'dark';
            applyTheme(current === 'dark' ? 'light' : 'dark');
        });
    }

    // ───────── Router (sidebar nav) ─────────
    const titles = {
        'dashboard-view': 'Dashboard',
        'positions-view': 'Posições abertas',
        'trades-view': 'Trades recentes',
        'regime-view': 'Regime Classifier',
        'analysis-view': 'Análise de P&L',
        'historico-view': 'Histórico por dia',
    };
    function showView(targetId) {
        $$('.view').forEach(v => v.classList.remove('active'));
        $$('.nav-item').forEach(n => n.classList.remove('active'));
        $(targetId).classList.add('active');
        const navBtn = document.querySelector(`.nav-item[data-target="${targetId}"]`);
        if (navBtn) navBtn.classList.add('active');
        $('page-title').textContent = titles[targetId] || 'Dashboard';
        // Os gráficos da Análise vivem numa view escondida (clientWidth=0 até
        // exibir). Renderiza ao abrir, quando o container já tem largura.
        if (targetId === 'analysis-view' && state.snapshot) {
            renderAnalysisCharts(state.snapshot.daily_history || []);
        }
    }
    function initRouter() {
        $$('.nav-item').forEach(btn => {
            btn.addEventListener('click', () => {
                showView(btn.dataset.target);
                // No mobile, fecha o menu colapsado após escolher.
                const sb = $('sidebar');
                if (sb) sb.classList.remove('open');
            });
        });
    }

    function initNavToggle() {
        const toggle = $('nav-toggle');
        const sidebar = $('sidebar');
        if (!toggle || !sidebar) return;
        toggle.addEventListener('click', () => {
            const open = sidebar.classList.toggle('open');
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
    }

    // ───────── Render: Summary (KPIs + hero + topbar status) ─────────
    function renderSummary(summary) {
        if (!summary) return;

        $('kpi-balance').textContent = fmt.usd(summary.last_balance);
        $('kpi-initial').textContent = fmt.usd(summary.initial_capital);
        // Delta atual vs inicial — mostra evolução com sinal e %
        const delta = (summary.last_balance || 0) - (summary.initial_capital || 0);
        const deltaPct = summary.initial_capital > 0
            ? (delta / summary.initial_capital) * 100
            : 0;
        const deltaEl = $('kpi-balance-delta');
        deltaEl.textContent = `${fmt.usd(delta, { signed: true })} (${fmt.pct(deltaPct)})`;
        deltaEl.classList.remove('pnl-pos', 'pnl-neg');
        if (delta > 0) deltaEl.classList.add('pnl-pos');
        else if (delta < 0) deltaEl.classList.add('pnl-neg');

        const pnlTotal = $('kpi-pnl-total');
        pnlTotal.textContent = fmt.usd(summary.total_pnl, { signed: true });
        setPnlClass(pnlTotal, summary.total_pnl);
        $('kpi-roi').textContent = 'ROI: ' + fmt.pct(summary.roi_percent);

        const pnlDaily = $('kpi-pnl-daily');
        pnlDaily.textContent = fmt.usd(summary.daily_pnl, { signed: true });
        setPnlClass(pnlDaily, summary.daily_pnl);
        $('kpi-closed-trades').textContent = 'Trades fechados: ' + summary.closed_trades;

        // Funding fee ACUMULADO (todos os dias): positivo = recebendo (verde),
        // negativo = pagando (vermelho). Sub explica direção em PT-BR e mostra a
        // comissão acumulada abaixo. Não zera na virada do dia UTC.
        const fundingVal = summary.funding_fee_total;
        const funding = $('kpi-funding');
        const fundingSub = $('kpi-funding-sub');
        if (fundingVal === undefined || fundingVal === null || isNaN(fundingVal)) {
            funding.textContent = '—';
            fundingSub.textContent = '—';
            funding.classList.remove('pos', 'neg');
        } else {
            funding.textContent = fmt.usd(fundingVal, { signed: true });
            setPnlClass(funding, fundingVal);
            let direction;
            if (fundingVal > 0) direction = 'Recebendo';
            else if (fundingVal < 0) direction = 'Pagando';
            else direction = 'Neutro';
            const comm = summary.commission_total;
            const commTxt = (comm !== undefined && comm !== null && !isNaN(comm))
                ? ` · Taxas: ${fmt.usd(comm, { signed: true })}`
                : '';
            fundingSub.textContent = direction + commTxt;
        }

        // Hero
        $('hero-balance').textContent = fmt.usd(summary.last_balance);

        // Status pills
        const running = summary.running && !summary.paused;
        const pillRunning = $('status-running');
        pillRunning.textContent = summary.paused ? 'PAUSADO' : (running ? 'RODANDO' : 'PARADO');
        pillRunning.classList.remove('ok', 'warn', 'error');
        pillRunning.classList.add(summary.paused ? 'warn' : (running ? 'ok' : 'error'));

        $('status-ai').textContent = 'AI: ' + (summary.ai_mode || 'off').toUpperCase();

        $('btn-pause').disabled = summary.paused;
        $('btn-resume').disabled = !summary.paused;
    }

    // ───────── Render: Positions (full + compact + hero count) ─────────
    function renderPositionRowCompact(p) {
        const sideClass = p.side === 'LONG' ? 'side-long' : 'side-short';
        let pnlCell;
        if (p.unrealized_pnl_usd === null || p.unrealized_pnl_usd === undefined) {
            pnlCell = '<span style="color:var(--text-muted)">—</span>';
        } else {
            const cls = p.unrealized_pnl_usd > 0 ? 'pnl-pos' : p.unrealized_pnl_usd < 0 ? 'pnl-neg' : '';
            pnlCell = `<span class="${cls}">${fmt.usd(p.unrealized_pnl_usd, { signed: true })}</span>`;
        }
        return `<tr>
            <td><strong>${p.symbol}</strong></td>
            <td><span class="${sideClass}">${p.side}</span></td>
            <td>${fmt.num(p.entry_price, 4)}</td>
            <td>${pnlCell}</td>
        </tr>`;
    }
    function renderPositionRowFull(p) {
        const sideClass = p.side === 'LONG' ? 'side-long' : 'side-short';
        const trailing = (p.trailing_activation_pct !== null && p.trailing_distance_pct !== null)
            ? `${Number(p.trailing_activation_pct).toFixed(2)}% / ${Number(p.trailing_distance_pct).toFixed(2)}%`
            : '<span style="color:var(--text-muted)">config</span>';
        let pnlCell;
        if (p.unrealized_pnl_usd === null || p.unrealized_pnl_usd === undefined) {
            pnlCell = '<span style="color:var(--text-muted)">—</span>';
        } else {
            const cls = p.unrealized_pnl_usd > 0 ? 'pnl-pos' : p.unrealized_pnl_usd < 0 ? 'pnl-neg' : '';
            const pctTxt = (p.unrealized_pnl_percent !== null && p.unrealized_pnl_percent !== undefined)
                ? ` <small>(${fmt.pct(p.unrealized_pnl_percent)})</small>` : '';
            pnlCell = `<span class="${cls}">${fmt.usd(p.unrealized_pnl_usd, { signed: true })}${pctTxt}</span>`;
        }
        const markPrice = (p.mark_price !== null && p.mark_price !== undefined && p.mark_price > 0)
            ? fmt.num(p.mark_price, 4)
            : '<span style="color:var(--text-muted)">—</span>';
        return `<tr>
            <td><strong>${p.symbol}</strong></td>
            <td><span class="${sideClass}">${p.side}</span></td>
            <td>${fmt.num(p.entry_price, 4)}</td>
            <td>${markPrice}</td>
            <td>${fmt.num(p.quantity, 4)}</td>
            <td>${pnlCell}</td>
            <td><small>${p.strategy_name}</small></td>
            <td>${p.custom_stop_loss !== null ? fmt.num(p.custom_stop_loss, 4) : '—'}</td>
            <td>${p.custom_take_profit !== null ? fmt.num(p.custom_take_profit, 4) : '—'}</td>
            <td><small>${trailing}</small></td>
        </tr>`;
    }

    function renderPositions(positions) {
        const tbodyFull = $('positions-table').querySelector('tbody');
        const tbodyCompact = $('positions-table-compact').querySelector('tbody');
        const count = positions.length;

        $('positions-count').textContent = count;
        $('positions-count-full').textContent = count + ' abertas';
        $('hero-positions').textContent = count;

        if (!count) {
            tbodyFull.innerHTML = '<tr><td colspan="10" class="empty">Sem posições.</td></tr>';
            tbodyCompact.innerHTML = '<tr><td colspan="4" class="empty">Sem posições.</td></tr>';
            return;
        }
        tbodyFull.innerHTML = positions.map(renderPositionRowFull).join('');
        tbodyCompact.innerHTML = positions.map(renderPositionRowCompact).join('');
    }

    // ───────── Render: Trades (full table + activity list compacta) ─────────
    function renderTrades(trades) {
        const tbody = $('trades-table').querySelector('tbody');
        $('trades-count').textContent = trades.length;

        if (!trades.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="empty">Sem trades ainda.</td></tr>';
            renderActivityList([]);
            return;
        }
        tbody.innerHTML = trades.map(t => {
            const sideClass = t.side === 'LONG' ? 'side-long' : 'side-short';
            const isClosed = t.exit_price !== null && t.exit_price !== undefined && t.exit_price > 0;
            const muted = '<span style="color:var(--text-muted)">—</span>';
            const exitCell = isClosed ? fmt.num(t.exit_price, 4) : muted;
            const feesCell = isClosed ? '<span class="pnl-neg">' + fmt.usd(t.fees || 0) + '</span>' : muted;
            const pnlGrossClass = (t.pnl_gross || 0) > 0 ? 'pnl-pos' : (t.pnl_gross || 0) < 0 ? 'pnl-neg' : '';
            const pnlGrossCell = isClosed
                ? `<span class="${pnlGrossClass}">${fmt.usd(t.pnl_gross, { signed: true })}</span>`
                : muted;
            const pnlNetClass = (t.pnl_net || 0) > 0 ? 'pnl-pos' : (t.pnl_net || 0) < 0 ? 'pnl-neg' : '';
            const pnlNetCell = isClosed
                ? `<span class="${pnlNetClass}"><strong>${fmt.usd(t.pnl_net, { signed: true })}</strong></span>`
                : muted;
            const reasonCell = isClosed
                ? `<small>${t.close_reason || '—'}</small>`
                : '<small style="color:var(--accent)">Aberta</small>';
            return `<tr>
                <td><small>${fmt.dateTime(t.timestamp)}</small></td>
                <td><strong>${t.symbol}</strong></td>
                <td><span class="${sideClass}">${t.side}</span></td>
                <td>${fmt.num(t.entry_price, 4)}</td>
                <td>${exitCell}</td>
                <td>${feesCell}</td>
                <td>${pnlGrossCell}</td>
                <td>${pnlNetCell}</td>
                <td>${reasonCell}</td>
            </tr>`;
        }).join('');

        renderActivityList(trades.slice(0, 8));
    }

    function renderActivityList(trades) {
        const root = $('activity-list');
        if (!trades.length) {
            root.innerHTML = '<div class="activity-empty">Sem trades ainda.</div>';
            return;
        }
        root.innerHTML = trades.map(t => {
            const sym3 = (t.symbol || '').replace('USDT', '').slice(0, 4) || '—';
            const isClosed = t.exit_price !== null && t.exit_price !== undefined && t.exit_price > 0;
            let value;
            if (isClosed && t.pnl_net !== null && t.pnl_net !== undefined) {
                const cls = t.pnl_net > 0 ? 'pnl-pos' : t.pnl_net < 0 ? 'pnl-neg' : '';
                value = `<span class="${cls}">${fmt.usd(t.pnl_net, { signed: true })}</span>`;
            } else {
                value = '<span style="color:var(--accent)">Aberta</span>';
            }
            return `<div class="activity-item">
                <div class="activity-avatar">${sym3}</div>
                <div class="activity-meta">
                    <div class="activity-pair">${t.symbol || '—'}</div>
                    <div class="activity-side">${t.side || '—'}</div>
                </div>
                <div class="activity-value">${value}</div>
            </div>`;
        }).join('');
    }

    // ───────── Render: Regime (full + compact) ─────────
    function renderDailyHistory(history) {
        const tbody = $('historico-table').querySelector('tbody');
        const sub = $('historico-summary');
        if (!history || !history.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty">Sem histórico ainda.</td></tr>';
            if (sub) sub.textContent = '—';
            return;
        }
        if (sub) {
            const last = history[history.length - 1];
            sub.textContent = `${history.length} dia(s) · acumulado ${fmt.usd(last.cumulative, { signed: true })}`;
        }
        // Mais recente primeiro
        tbody.innerHTML = history.slice().reverse().map(d => {
            const netCls = (d.net || 0) > 0 ? 'pnl-pos' : (d.net || 0) < 0 ? 'pnl-neg' : '';
            const cumCls = (d.cumulative || 0) > 0 ? 'pnl-pos' : (d.cumulative || 0) < 0 ? 'pnl-neg' : '';
            return `<tr>
                <td><strong>${d.day}</strong></td>
                <td>${d.trades} <small style="color:var(--text-muted)">(${d.wins}W/${d.losses}L)</small></td>
                <td>${Number(d.win_rate || 0).toFixed(1)}%</td>
                <td><span class="pnl-neg">${fmt.usd(d.fees)}</span></td>
                <td><span class="${netCls}"><strong>${fmt.usd(d.net, { signed: true })}</strong></span></td>
                <td><span class="${cumCls}">${fmt.usd(d.cumulative, { signed: true })}</span></td>
            </tr>`;
        }).join('');
    }

    // ───────── Render: Análise P&L (stats + 2 gráficos) ─────────
    function renderPnlAnalysis(an) {
        const set = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
        const setSigned = (id, value) => {
            const el = $(id);
            if (!el) return;
            el.textContent = fmt.usd(value, { signed: true });
            setPnlClass(el, value);
        };
        if (!an || an.trades === undefined) {
            ['an-total-profit', 'an-total-loss', 'an-net-pnl', 'an-volume', 'an-win-rate',
             'an-winning-days', 'an-losing-days', 'an-breakeven-days', 'an-avg-profit',
             'an-avg-loss', 'an-pl-ratio', 'an-trades'].forEach(id => set(id, '—'));
            const sub = $('analysis-summary'); if (sub) sub.textContent = '—';
            return;
        }
        setSigned('an-total-profit', an.total_profit);
        setSigned('an-total-loss', an.total_loss);
        setSigned('an-net-pnl', an.net_pnl);
        set('an-volume', fmt.usd(an.trading_volume));
        set('an-win-rate', Number(an.win_rate || 0).toFixed(2) + ' %');
        set('an-winning-days', an.winning_days + ' dias');
        set('an-losing-days', an.losing_days + ' dias');
        set('an-breakeven-days', an.breakeven_days + ' dias');
        setSigned('an-avg-profit', an.avg_profit);
        setSigned('an-avg-loss', an.avg_loss);
        set('an-pl-ratio', Number(an.profit_loss_ratio || 0).toFixed(2));
        set('an-trades', `${an.trades} (${an.wins}W / ${an.losses}L)`);
        const sub = $('analysis-summary');
        if (sub) sub.textContent = `${an.trades} trade(s) · ${Number(an.win_rate || 0).toFixed(1)}% win rate`;
    }

    function ensureBarChart(containerId, key, seriesKey) {
        if (state[key]) return state[key];
        const container = $(containerId);
        if (!container || typeof LightweightCharts === 'undefined') return null;
        const c = chartColors();
        const chart = LightweightCharts.createChart(container, {
            layout: { background: { color: 'transparent' }, textColor: c.text, fontFamily: 'Inter, sans-serif', fontSize: 11 },
            grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
            timeScale: { timeVisible: false, secondsVisible: false, borderVisible: false },
            rightPriceScale: { borderVisible: false },
            crosshair: { mode: 1 },
            handleScroll: false,
            handleScale: false,
        });
        state[seriesKey] = chart.addHistogramSeries({ priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
        window.addEventListener('resize', () => chart.applyOptions({ width: container.clientWidth }));
        chart.applyOptions({ width: container.clientWidth, height: 320 });
        state[key] = chart;
        return chart;
    }

    function ensureLineChart(containerId, key, seriesKey) {
        if (state[key]) return state[key];
        const container = $(containerId);
        if (!container || typeof LightweightCharts === 'undefined') return null;
        const c = chartColors();
        const chart = LightweightCharts.createChart(container, {
            layout: { background: { color: 'transparent' }, textColor: c.text, fontFamily: 'Inter, sans-serif', fontSize: 11 },
            grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
            timeScale: { timeVisible: false, secondsVisible: false, borderVisible: false },
            rightPriceScale: { borderVisible: false },
            crosshair: { mode: 1 },
            handleScroll: false,
            handleScale: false,
        });
        state[seriesKey] = chart.addAreaSeries({
            lineColor: '#f0b90b', topColor: 'rgba(240,185,11,0.25)', bottomColor: 'rgba(240,185,11,0.02)', lineWidth: 2,
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        });
        window.addEventListener('resize', () => chart.applyOptions({ width: container.clientWidth }));
        chart.applyOptions({ width: container.clientWidth, height: 320 });
        state[key] = chart;
        return chart;
    }

    function renderAnalysisCharts(daily) {
        const data = (daily || []).filter(d => d && d.day);
        // Daily PNL — barras verde/vermelho por dia
        const bar = ensureBarChart('daily-pnl-chart', 'dailyPnlChart', 'dailyPnlSeries');
        if (bar && state.dailyPnlSeries) {
            state.dailyPnlSeries.setData(data.map(d => ({
                time: d.day,
                value: Number(d.net || 0),
                color: (d.net || 0) >= 0 ? '#26a69a' : '#ef5350',
            })));
            bar.timeScale().fitContent();
        }
        // Cumulative PNL — linha do acumulado
        const line = ensureLineChart('cumulative-pnl-chart', 'cumPnlChart', 'cumPnlSeries');
        if (line && state.cumPnlSeries) {
            state.cumPnlSeries.setData(data.map(d => ({
                time: d.day,
                value: Number(d.cumulative || 0),
            })));
            line.timeScale().fitContent();
        }
    }

    function destroyAnalysisCharts() {
        [['dailyPnlChart', 'dailyPnlSeries'], ['cumPnlChart', 'cumPnlSeries']].forEach(([k, s]) => {
            if (state[k]) { try { state[k].remove(); } catch (e) { /* ignore */ } }
            state[k] = null;
            state[s] = null;
        });
    }

    function renderRegime(regime) {
        if (!regime) return;
        const tbodyFull = $('regime-table').querySelector('tbody');
        const tbodyCompact = $('regime-table-compact').querySelector('tbody');
        const statusTxt = regime.enabled ? 'ativo' : 'desabilitado';
        $('regime-status').textContent = statusTxt;
        $('regime-status-full').textContent = statusTxt;

        const committed = regime.committed || {};
        const observations = regime.observations || {};
        const symbols = Array.from(new Set([
            ...Object.keys(committed),
            ...Object.keys(observations),
        ])).sort();

        if (!symbols.length) {
            tbodyFull.innerHTML = '<tr><td colspan="3" class="empty">Sem dados ainda.</td></tr>';
            tbodyCompact.innerHTML = '<tr><td colspan="2" class="empty">Sem dados ainda.</td></tr>';
            return;
        }
        tbodyFull.innerHTML = symbols.map(sym => {
            const r = committed[sym] || 'neutral';
            const window = (observations[sym] || []).join(' · ') || '—';
            return `<tr>
                <td><strong>${sym}</strong></td>
                <td><span class="regime-${r}">${r}</span></td>
                <td><small>${window}</small></td>
            </tr>`;
        }).join('');
        tbodyCompact.innerHTML = symbols.map(sym => {
            const r = committed[sym] || 'neutral';
            return `<tr>
                <td><strong>${sym}</strong></td>
                <td><span class="regime-${r}">${r}</span></td>
            </tr>`;
        }).join('');
    }

    // ───────── Equity chart ─────────
    function chartColors() {
        const isLight = document.documentElement.getAttribute('data-theme') === 'light';
        return {
            text: isLight ? '#475569' : '#9aa4b8',
            grid: isLight ? 'rgba(15,23,42,0.06)' : 'rgba(255,255,255,0.05)',
            line: '#2c8eff',
            top: isLight ? 'rgba(44,142,255,0.20)' : 'rgba(44,142,255,0.35)',
            bottom: isLight ? 'rgba(44,142,255,0.01)' : 'rgba(44,142,255,0.02)',
        };
    }
    function destroyChart() {
        if (state.equityChart) {
            try { state.equityChart.remove(); } catch (e) { /* ignore */ }
        }
        state.equityChart = null;
        state.equitySeries = null;
    }
    function ensureChart() {
        if (state.equityChart) return state.equityChart;
        const container = $('equity-chart');
        if (!container || typeof LightweightCharts === 'undefined') return null;
        const c = chartColors();
        const chart = LightweightCharts.createChart(container, {
            layout: { background: { color: 'transparent' }, textColor: c.text, fontFamily: 'Inter, sans-serif', fontSize: 11 },
            grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
            timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false },
            rightPriceScale: { borderVisible: false },
            crosshair: { mode: 1 },
            handleScroll: false,
            handleScale: false,
        });
        const series = chart.addAreaSeries({
            lineColor: c.line, topColor: c.top, bottomColor: c.bottom, lineWidth: 2,
        });
        window.addEventListener('resize', () => chart.applyOptions({ width: container.clientWidth }));
        chart.applyOptions({ width: container.clientWidth, height: 320 });
        state.equityChart = chart;
        state.equitySeries = series;
        return chart;
    }

    function filterHistoryByRange(history, range) {
        if (range === 'all' || !history.length) return history;
        const now = Date.now();
        const windows = { '24h': 24 * 3600 * 1000, '6h': 6 * 3600 * 1000, '1h': 3600 * 1000 };
        const cutoff = now - (windows[range] || 0);
        return history.filter(s => {
            const t = new Date(s.timestamp).getTime();
            return !isNaN(t) && t >= cutoff;
        });
    }
    function renderEquity(history) {
        const chart = ensureChart();
        if (!chart || !state.equitySeries) return;
        const filtered = filterHistoryByRange(history || [], state.chartRange);
        if (!filtered.length) {
            state.equitySeries.setData([]);
            $('chart-range').textContent = 'sem dados nesta janela';
            return;
        }
        const data = filtered.map(snap => {
            const t = new Date(snap.timestamp);
            if (isNaN(t.getTime())) return null;
            const v = snap.equity !== undefined && snap.equity !== null ? snap.equity : snap.balance;
            return { time: Math.floor(t.getTime() / 1000), value: v };
        }).filter(Boolean);

        state.equitySeries.setData(data);
        const first = filtered[0].timestamp;
        const last = filtered[filtered.length - 1].timestamp;
        $('chart-range').textContent = `${fmt.dateTime(first)} → ${fmt.dateTime(last)}`;
    }

    function initChartTabs() {
        $$('.chart-tabs .chip').forEach(btn => {
            btn.addEventListener('click', () => {
                $$('.chart-tabs .chip').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.chartRange = btn.dataset.range;
                if (state.snapshot) renderEquity(state.snapshot.portfolio_history || []);
            });
        });
    }

    // ───────── Apply snapshot ─────────
    function applySnapshot(snap) {
        if (!snap) return;
        state.snapshot = snap;
        renderSummary(snap.summary);
        renderPositions(snap.positions || []);
        renderTrades(snap.recent_trades || []);
        renderRegime(snap.regime);
        renderEquity(snap.portfolio_history || []);
        renderDailyHistory(snap.daily_history || []);
        renderPnlAnalysis(snap.pnl_analysis);
        // Só re-desenha os gráficos da Análise se a view estiver visível
        // (containers escondidos têm largura 0 e o chart fica quebrado).
        const analysisView = $('analysis-view');
        if (analysisView && analysisView.classList.contains('active')) {
            renderAnalysisCharts(snap.daily_history || []);
        }
        $('kpi-last-update').textContent = fmt.time(snap.server_time);
        resetRefreshCountdown();
    }

    // ───────── Controls ─────────
    const confirmModal = $('confirm-modal');
    let pendingAction = null;
    function askConfirm(title, message, action) {
        $('confirm-title').textContent = title;
        $('confirm-message').textContent = message;
        pendingAction = action;
        confirmModal.classList.add('open');
    }
    function closeConfirm() {
        confirmModal.classList.remove('open');
        pendingAction = null;
    }
    $('confirm-cancel').addEventListener('click', closeConfirm);
    $('confirm-ok').addEventListener('click', () => {
        const action = pendingAction; closeConfirm();
        if (action) action();
    });

    async function postControl(path, body) {
        const r = await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
            credentials: 'include',
        });
        if (!r.ok) {
            const txt = await r.text();
            alert('Falha: ' + r.status + ' — ' + txt);
            return null;
        }
        return r.json();
    }

    document.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', () => {
            const action = btn.dataset.action;
            if (action === 'pause') {
                askConfirm('Pausar bot', 'O bot vai PARAR de abrir novas posições. Posições abertas continuam sendo monitoradas (trailing, SL, TP).',
                    async () => { await postControl('/api/control/pause'); });
            } else if (action === 'resume') {
                askConfirm('Retomar bot', 'O bot vai voltar a procurar entradas conforme as estratégias ativas.',
                    async () => { await postControl('/api/control/resume'); });
            } else if (action === 'close_all') {
                askConfirm('🚨 FECHAR TODAS AS POSIÇÕES',
                    'Vai enviar ordens de MARKET fechando TODAS as posições agora. Equivalente ao /closeall do Telegram. Confirma?',
                    async () => { await postControl('/api/control/close_all', { reason: 'Dashboard panic close' }); });
            }
        });
    });

    // ───────── Connection (Socket.IO + polling) ─────────
    const pillConn = $('status-connection');
    const pollInterval = (parseInt(document.body.dataset.pollInterval, 10) || 5) * 1000;
    let pollTimer = null;
    let socket = null;

    async function pollOnce() {
        try {
            const r = await fetch('/api/snapshot', { credentials: 'include' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            applySnapshot(await r.json());
        } catch (err) { console.warn('Poll falhou:', err); }
    }
    function startPolling() {
        if (pollTimer) return;
        pollOnce();
        pollTimer = setInterval(pollOnce, pollInterval);
    }
    function stopPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
    }
    function setConn(state_) {
        pillConn.classList.remove('ok', 'warn', 'error');
        if (state_ === 'connected') { pillConn.textContent = 'ao vivo'; pillConn.classList.add('ok'); }
        else if (state_ === 'polling') { pillConn.textContent = 'polling'; pillConn.classList.add('warn'); }
        else { pillConn.textContent = 'desconectado'; pillConn.classList.add('error'); }
    }

    // ───────── Refresh countdown ─────────
    // Contador regressivo "próxima em Xs" até o próximo refresh. Reseta a cada
    // snapshot recebido (push ao vivo OU polling). Ao zerar, força um refresh —
    // garante atualização no máximo a cada REFRESH_SECS mesmo sem evento algum.
    const REFRESH_SECS = Math.max(1, Math.round(pollInterval / 1000));
    // Pega o span do template; se o template estiver em cache (sem o span),
    // cria dinamicamente dentro do #page-sub — assim o contador funciona sem
    // depender de restart pra recarregar o HTML.
    let nextUpdateEl = $('kpi-next-update');
    if (!nextUpdateEl) {
        const sub = $('page-sub');
        if (sub) {
            nextUpdateEl = document.createElement('span');
            nextUpdateEl.id = 'kpi-next-update';
            nextUpdateEl.className = 'next-update';
            sub.appendChild(document.createTextNode(' · '));
            sub.appendChild(nextUpdateEl);
        }
    }
    let refreshLeft = REFRESH_SECS;

    function renderCountdown() {
        if (nextUpdateEl) nextUpdateEl.textContent = 'próxima em ' + refreshLeft + 's';
    }
    function resetRefreshCountdown() {
        refreshLeft = REFRESH_SECS;
        renderCountdown();
    }
    function requestRefresh() {
        if (socket && socket.connected) socket.emit('request_snapshot');
        else pollOnce();
    }
    function tickCountdown() {
        refreshLeft -= 1;
        if (refreshLeft <= 0) {
            requestRefresh();          // dispara; o snapshot que chegar reseta o contador
            refreshLeft = REFRESH_SECS;
        }
        renderCountdown();
    }
    renderCountdown();
    setInterval(tickCountdown, 1000);

    // ───────── Boot ─────────
    initTheme();
    initRouter();
    initNavToggle();
    initChartTabs();

    if (typeof io !== 'undefined') {
        socket = io({
            transports: ['polling', 'websocket'],
            reconnectionDelayMax: 10000,
        });
        socket.on('connect', () => {
            setConn('connected');
            stopPolling();
            socket.emit('request_snapshot');
        });
        socket.on('disconnect',    () => { setConn('polling'); startPolling(); });
        socket.on('connect_error', () => { setConn('polling'); startPolling(); });
        socket.on('snapshot', applySnapshot);
        socket.on('control_changed', (payload) => {
            if (state.snapshot && state.snapshot.summary) {
                state.snapshot.summary.paused = !!payload.paused;
                renderSummary(state.snapshot.summary);
            }
        });
        socket.on('position_opened', () => socket.emit('request_snapshot'));
        socket.on('position_closed', () => socket.emit('request_snapshot'));
        socket.on('regime_changed',  () => socket.emit('request_snapshot'));
        socket.on('balance_update',  () => socket.emit('request_snapshot'));
    } else {
        setConn('polling');
        startPolling();
    }

    setTimeout(() => {
        if (!state.snapshot) { setConn('polling'); startPolling(); }
    }, 1500);
})();
