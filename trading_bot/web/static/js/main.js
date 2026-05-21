/*
 * Trading Bot Dashboard — client logic.
 *
 * Conecta via Socket.IO. Em fallback (WebSocket bloqueado), faz polling
 * de /api/snapshot a cada DASHBOARD_POLL_INTERVAL_SECONDS.
 *
 * Eventos do server:
 *   connect, disconnect
 *   snapshot          — payload completo (renderiza tudo)
 *   position_opened   — payload da posição
 *   position_closed   — payload do trade
 *   regime_changed    — { symbol, regime }
 *   control_changed   — { paused: bool }
 */

(function() {
    'use strict';

    const $ = (id) => document.getElementById(id);
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

    // ------------------------------------------------------------------
    // Estado e renderização
    // ------------------------------------------------------------------

    const state = {
        snapshot: null,
        equityChart: null,
        equitySeries: null,
    };

    function classify(value) {
        if (value > 0) return 'pos';
        if (value < 0) return 'neg';
        return '';
    }

    // classList.add('') é SyntaxError no DOM — guarda antes de chamar.
    function setPnlClass(el, value) {
        el.classList.remove('pos', 'neg');
        const cls = classify(value);
        if (cls) el.classList.add(cls);
    }

    function renderSummary(summary) {
        if (!summary) return;
        $('kpi-balance').textContent = fmt.usd(summary.last_balance);
        $('kpi-initial').textContent = 'Inicial: ' + fmt.usd(summary.initial_capital);

        const pnlTotal = $('kpi-pnl-total');
        pnlTotal.textContent = fmt.usd(summary.total_pnl, { signed: true });
        setPnlClass(pnlTotal, summary.total_pnl);
        $('kpi-roi').textContent = 'ROI: ' + fmt.pct(summary.roi_percent);

        const pnlDaily = $('kpi-pnl-daily');
        pnlDaily.textContent = fmt.usd(summary.daily_pnl, { signed: true });
        setPnlClass(pnlDaily, summary.daily_pnl);
        $('kpi-closed-trades').textContent = 'Trades fechados: ' + summary.closed_trades;

        const running = summary.running && !summary.paused;
        const pillRunning = $('status-running');
        pillRunning.textContent = summary.paused ? 'PAUSADO' : (running ? 'RODANDO' : 'PARADO');
        pillRunning.classList.remove('ok', 'warn', 'error');
        pillRunning.classList.add(summary.paused ? 'warn' : (running ? 'ok' : 'error'));

        const pillAi = $('status-ai');
        pillAi.textContent = 'AI: ' + (summary.ai_mode || 'off').toUpperCase();

        // Botões: habilita só o que faz sentido
        $('btn-pause').disabled = summary.paused;
        $('btn-resume').disabled = !summary.paused;
    }

    function renderPositions(positions) {
        const tbody = $('positions-table').querySelector('tbody');
        $('positions-count').textContent = positions.length + ' aberta(s)';
        $('kpi-positions-open').textContent = positions.length;

        if (!positions.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty">Sem posições.</td></tr>';
            return;
        }

        tbody.innerHTML = positions.map(p => {
            const sideClass = p.side === 'LONG' ? 'side-long' : 'side-short';
            const trailing = (p.trailing_activation_pct !== null && p.trailing_distance_pct !== null)
                ? `${Number(p.trailing_activation_pct).toFixed(2)}% / ${Number(p.trailing_distance_pct).toFixed(2)}%`
                : '<span style="color:var(--text-muted)">config</span>';
            return `<tr>
                <td><strong>${p.symbol}</strong></td>
                <td><span class="${sideClass}">${p.side}</span></td>
                <td>${fmt.num(p.entry_price, 4)}</td>
                <td>${fmt.num(p.quantity, 4)}</td>
                <td><small>${p.strategy_name}</small></td>
                <td>${p.custom_stop_loss !== null ? fmt.num(p.custom_stop_loss, 4) : '—'}</td>
                <td>${p.custom_take_profit !== null ? fmt.num(p.custom_take_profit, 4) : '—'}</td>
                <td><small>${trailing}</small></td>
            </tr>`;
        }).join('');
    }

    function renderTrades(trades) {
        const tbody = $('trades-table').querySelector('tbody');
        $('trades-count').textContent = trades.length + ' recente(s)';
        if (!trades.length) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty">Sem trades fechados ainda.</td></tr>';
            return;
        }
        tbody.innerHTML = trades.map(t => {
            const sideClass = t.side === 'LONG' ? 'side-long' : 'side-short';
            const pnlClass = t.pnl_net > 0 ? 'pnl-pos' : (t.pnl_net < 0 ? 'pnl-neg' : '');
            return `<tr>
                <td><small>${fmt.dateTime(t.timestamp)}</small></td>
                <td><strong>${t.symbol}</strong></td>
                <td><span class="${sideClass}">${t.side}</span></td>
                <td>${fmt.num(t.entry_price, 4)}</td>
                <td>${fmt.num(t.exit_price, 4)}</td>
                <td class="${pnlClass}">${fmt.usd(t.pnl_net, { signed: true })}</td>
                <td><small>${t.close_reason || '—'}</small></td>
            </tr>`;
        }).join('');
    }

    function renderRegime(regime) {
        if (!regime) return;
        const tbody = $('regime-table').querySelector('tbody');
        const status = $('regime-status');
        status.textContent = regime.enabled ? 'ativo' : 'desabilitado';

        const committed = regime.committed || {};
        const observations = regime.observations || {};
        const symbols = Array.from(new Set([
            ...Object.keys(committed),
            ...Object.keys(observations),
        ])).sort();

        if (!symbols.length) {
            tbody.innerHTML = '<tr><td colspan="3" class="empty">Sem dados ainda.</td></tr>';
            return;
        }

        tbody.innerHTML = symbols.map(sym => {
            const r = committed[sym] || 'neutral';
            const window = (observations[sym] || []).join(' · ') || '—';
            return `<tr>
                <td><strong>${sym}</strong></td>
                <td><span class="regime-${r}">${r}</span></td>
                <td><small>${window}</small></td>
            </tr>`;
        }).join('');
    }

    // ------------------------------------------------------------------
    // Chart de equity (Lightweight Charts)
    // ------------------------------------------------------------------

    function ensureChart() {
        if (state.equityChart) return state.equityChart;
        const container = $('equity-chart');
        if (!container || typeof LightweightCharts === 'undefined') return null;

        const chart = LightweightCharts.createChart(container, {
            layout: {
                background: { color: 'transparent' },
                textColor: '#8b9bbb',
                fontFamily: 'Inter, sans-serif',
                fontSize: 11,
            },
            grid: {
                vertLines: { color: 'rgba(110, 140, 200, 0.08)' },
                horzLines: { color: 'rgba(110, 140, 200, 0.08)' },
            },
            timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false },
            rightPriceScale: { borderVisible: false },
            crosshair: { mode: 1 },
            handleScroll: false,
            handleScale: false,
        });

        const series = chart.addAreaSeries({
            lineColor: '#4fd6ff',
            topColor: 'rgba(79, 214, 255, 0.35)',
            bottomColor: 'rgba(79, 214, 255, 0.02)',
            lineWidth: 2,
        });

        window.addEventListener('resize', () => {
            chart.applyOptions({ width: container.clientWidth });
        });
        chart.applyOptions({ width: container.clientWidth, height: 320 });

        state.equityChart = chart;
        state.equitySeries = series;
        return chart;
    }

    function renderEquity(history) {
        const chart = ensureChart();
        if (!chart || !state.equitySeries) return;
        if (!history || !history.length) {
            state.equitySeries.setData([]);
            $('chart-range').textContent = 'sem dados ainda';
            return;
        }
        const data = history
            .map(snap => {
                const t = new Date(snap.timestamp);
                if (isNaN(t.getTime())) return null;
                return { time: Math.floor(t.getTime() / 1000), value: snap.balance };
            })
            .filter(Boolean);

        state.equitySeries.setData(data);
        const first = history[0].timestamp;
        const last = history[history.length - 1].timestamp;
        $('chart-range').textContent = `${fmt.dateTime(first)} → ${fmt.dateTime(last)}`;
    }

    // ------------------------------------------------------------------
    // Apply snapshot
    // ------------------------------------------------------------------

    function applySnapshot(snap) {
        if (!snap) return;
        state.snapshot = snap;
        renderSummary(snap.summary);
        renderPositions(snap.positions || []);
        renderTrades(snap.recent_trades || []);
        renderRegime(snap.regime);
        renderEquity(snap.portfolio_history || []);
        $('kpi-last-update').textContent = 'Atualizado: ' + fmt.time(snap.server_time);
    }

    // ------------------------------------------------------------------
    // Controles
    // ------------------------------------------------------------------

    const confirmModal = $('confirm-modal');
    let pendingAction = null;

    function askConfirm(title, message, action) {
        $('confirm-title').textContent = title;
        $('confirm-message').textContent = message;
        pendingAction = action;
        confirmModal.classList.add('open');
        confirmModal.setAttribute('aria-hidden', 'false');
    }

    function closeConfirm() {
        confirmModal.classList.remove('open');
        confirmModal.setAttribute('aria-hidden', 'true');
        pendingAction = null;
    }

    $('confirm-cancel').addEventListener('click', closeConfirm);
    $('confirm-ok').addEventListener('click', () => {
        const action = pendingAction;
        closeConfirm();
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
                askConfirm('Pausar bot', 'O bot vai PARAR de abrir novas posições. Posições abertas continuam sendo monitoradas (trailing, SL, TP).', async () => {
                    await postControl('/api/control/pause');
                });
            } else if (action === 'resume') {
                askConfirm('Retomar bot', 'O bot vai voltar a procurar entradas conforme as estratégias ativas.', async () => {
                    await postControl('/api/control/resume');
                });
            } else if (action === 'close_all') {
                askConfirm('🚨 FECHAR TODAS AS POSIÇÕES',
                    'Vai enviar ordens de MARKET fechando TODAS as posições agora. Equivalente ao /closeall do Telegram. Confirma?',
                    async () => {
                        await postControl('/api/control/close_all', { reason: 'Dashboard panic close' });
                    });
            }
        });
    });

    // ------------------------------------------------------------------
    // Conexão (Socket.IO + fallback polling)
    // ------------------------------------------------------------------

    const pillConn = $('status-connection');
    const footerTransport = $('footer-transport');
    const pollInterval = (parseInt(document.body.dataset.pollInterval, 10) || 5) * 1000;
    let pollTimer = null;

    async function pollOnce() {
        try {
            const r = await fetch('/api/snapshot', { credentials: 'include' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            applySnapshot(await r.json());
        } catch (err) {
            console.warn('Poll falhou:', err);
        }
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

    function setConn(state) {
        pillConn.classList.remove('ok', 'warn', 'error');
        if (state === 'connected') {
            pillConn.textContent = 'ao vivo';
            pillConn.classList.add('ok');
        } else if (state === 'polling') {
            pillConn.textContent = 'polling';
            pillConn.classList.add('warn');
        } else {
            pillConn.textContent = 'desconectado';
            pillConn.classList.add('error');
        }
    }

    if (typeof io !== 'undefined') {
        const socket = io({ transports: ['websocket', 'polling'] });

        socket.on('connect', () => {
            setConn('connected');
            footerTransport.textContent = socket.io.engine.transport.name;
            stopPolling();
            socket.emit('request_snapshot');
        });

        socket.on('disconnect', () => {
            setConn('polling');
            startPolling();
        });

        socket.on('connect_error', () => {
            setConn('polling');
            startPolling();
        });

        socket.on('snapshot', applySnapshot);

        socket.on('control_changed', (payload) => {
            // Atualiza paused imediatamente — re-render leve até próximo snapshot.
            if (state.snapshot && state.snapshot.summary) {
                state.snapshot.summary.paused = !!payload.paused;
                renderSummary(state.snapshot.summary);
            }
        });

        socket.on('position_opened', () => socket.emit('request_snapshot'));
        socket.on('position_closed', () => socket.emit('request_snapshot'));
        socket.on('regime_changed',   () => socket.emit('request_snapshot'));
        socket.on('balance_update',   () => socket.emit('request_snapshot'));
    } else {
        // Sem socket.io carregado — só polling.
        setConn('polling');
        startPolling();
    }

    // Primeira render fallback (caso conexão demore)
    setTimeout(() => {
        if (!state.snapshot) pollOnce();
    }, 1500);
})();
