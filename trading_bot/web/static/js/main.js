/*
 * Dashboard v3: painel do forward test Donchian 4h.
 * Dois payloads: `live` (/api/live ou evento socket `snapshot`, a cada poll: KPIs,
 * posições, motor, saúde, forward test) e `history` (/api/history, só quando um
 * trade fecha ou a cada 2 min: trades, curva de equity). Os gráficos são
 * atualizados no lugar, nunca recriados a cada poll.
 */
(function () {
    'use strict';

    // Sessão expirou: qualquer /api/* com 401 manda pro login e volta pra cá depois.
    const rawFetch = window.fetch.bind(window);
    window.fetch = async (url, opts) => {
        const r = await rawFetch(url, opts);
        if (r.status === 401 && String(url).startsWith('/api/')) location.href = '/login?next=' + encodeURIComponent(location.pathname);
        return r;
    };

    const $ = (id) => document.getElementById(id);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));
    const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const isNum = (v) => v !== null && v !== undefined && !isNaN(v);
    const exchangeName = () => (state.live && state.live.summary && state.live.summary.exchange) || 'exchange';
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // sinal após arredondar a d casas: o que vira "0,00" na tela é zero (sem sinal, sem cor)
    const sgn = (v, d = 2) => Math.abs(v) < 0.5 * 10 ** -d ? 0 : Math.sign(v);
    // Moeda principal dos valores monetários (USD ou BRL pelo botão do topo); sem cotação cai em USD.
    const CUR_KEY = 'tradingbot-currency';
    let currency = 'USD';
    try { if (localStorage.getItem(CUR_KEY) === 'BRL') currency = 'BRL'; } catch (e) { /* storage bloqueado */ }
    const fxRate = () => { const r = state.fx && state.fx.usd_brl; return isNum(r) ? r : null; };
    const inBrl = () => currency === 'BRL' && fxRate() !== null;
    const fmt = {
        _usd(v, signed) { const s = sgn(v), sign = s < 0 ? '−' : (signed && s > 0 ? '+' : ''); return sign + '$' + Math.abs(Number(v)).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); },
        _brl(usd, signed) { const b = usd * fxRate(), s = sgn(b), sign = s < 0 ? '−' : (signed && s > 0 ? '+' : ''); return sign + Math.abs(b).toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' }); },
        // valor em USD formatado na moeda principal escolhida
        money(v, o = {}) { if (!isNum(v)) return '—'; return inBrl() ? fmt._brl(v, o.signed) : fmt._usd(v, o.signed); },
        // o mesmo valor na outra moeda ("≈ ..."); '' sem cotação, aí o texto some
        moneyAlt(v) { if (!isNum(v) || fxRate() === null) return ''; return '≈ ' + (inBrl() ? fmt._usd(v) : fmt._brl(v)); },
        pct(v, d = 2, signed = true) { if (!isNum(v)) return '—'; const z = sgn(v, d), s = z < 0 ? '−' : (signed && z > 0 ? '+' : ''); return s + Math.abs(Number(v)).toFixed(d).replace('.', ',') + '%'; },
        num(v, d = 2) { if (!isNum(v)) return '—'; return Number(v).toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d }); },
        price(v) { if (!isNum(v)) return '—'; const a = Math.abs(v); const d = a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 7; return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); },
        r(v) { if (!isNum(v)) return '—'; const z = sgn(v); return (z < 0 ? '−' : z > 0 ? '+' : '') + Math.abs(v).toFixed(2).replace('.', ',') + 'R'; },
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
    const cls = (v) => { const z = sgn(v); return z > 0 ? 'pos' : z < 0 ? 'neg' : ''; };
    const sum = (a) => a.reduce((s, v) => s + v, 0);

    const state = { live: null, fx: null, hist: null, pnl: null, chartRange: 'test', page: 1, sortKey: 'exit_time', sortDir: -1, agg: 'week',
        filter: { strategy: document.body.dataset.strategy === 'donchian_4h' ? 'donchian_4h' : 'all', side: 'all', reason: 'all' } };
    const PAGE = 15;
    const charts = {};

    // ───────── Relógio (America/Sao_Paulo) ─────────
    const clockFmt = new Intl.DateTimeFormat('pt-BR', { timeZone: 'America/Sao_Paulo', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const tickClock = () => { $('clock').textContent = clockFmt.format(new Date()); };
    tickClock(); setInterval(tickClock, 1000);

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
    // aberto/fechado de cada card recolhível sobrevive ao reload (padrão: todos fechados)
    $$('details.card[id]').forEach(d => {
        try { const v = localStorage.getItem('tradingbot-open:' + d.id); if (v !== null) d.open = v === '1'; } catch (e) { /* storage bloqueado */ }
        d.addEventListener('toggle', () => { try { localStorage.setItem('tradingbot-open:' + d.id, d.open ? '1' : '0'); } catch (e) { /* idem */ } });
    });

    // --top-h é a altura do topo fixo (scroll-margin-top dos cards).
    if (window.ResizeObserver) new ResizeObserver(([e]) => document.documentElement.style.setProperty('--top-h', e.target.offsetHeight + 'px')).observe(document.querySelector('.top'));

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
        $('fx-toggle').textContent = currency === 'BRL' ? 'ver em US$' : 'ver em R$';
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
    const brlTag = (usd) => { const t = fmt.moneyAlt(usd); return t ? `<small class="brl">${t}</small>` : ''; };
    function setTile(id, value, foot, k, usd) {
        const v = $(id); v.innerHTML = esc(value) + brlTag(usd); v.className = 'tv ' + (k || '');
        $(id + '-f').textContent = foot;
        const hd = $('hd-' + id.slice(3)); // espelho no topo fixo do celular (hero)
        if (hd) { hd.textContent = value; hd.className = 'tv ' + (k || ''); }
    }
    function renderStatus(s, ft, e, positions) {
        const eq = s.last_balance;
        setTile('st-equity', fmt.money(eq), `pico ${fmt.money(ft.peak_equity)} · capital ${fmt.money(s.initial_capital)}`, '', eq);
        const dd = ft.drawdown_pct, lim = ft.dd_limit_pct || 20;
        setTile('st-dd', fmt.pct(dd, 2, false), isNum(ft.dd_room_usd) ? `${fmt.money(ft.dd_room_usd)} de folga até ${lim}% (${fmt.money(ft.dd_floor_equity)})` : 'sem pico registrado',
            dd >= lim * 0.8 ? 'neg' : '');
        const w = state.hist && state.hist.max_drawdown;   // pior drawdown flutuante desde o reset (histórico, 1×/min)
        $('st-dd-w').textContent = w ? `pior: ${fmt.money(-w.usd, { signed: true })} (${fmt.pct(w.pct, 1, false)}) · ${fmt.dateTime(w.at)}` : 'pior: —';
        const max = e && e.params ? e.params.max_positions : null;
        const cap = e && e.params && isNum(eq) ? eq * (e.params.max_gross_mult || 3) : null;
        setTile('st-pos', max ? `${positions.length} / ${max}` : String(positions.length),
            isNum(e && e.gross_notional) && cap ? `notional ${fmt.money(e.gross_notional)} de ${fmt.money(cap)} (${e.params.max_gross_mult}× equity)` : '—');
        setTile('st-bar', e && e.bar_close ? fmt.time(e.bar_close) : '—',
            e && e.last_bar_processed_at ? `processada ${fmt.dateTime(e.last_bar_processed_at)} ${fmt.tzLabel()}` : (e && e.bar_close ? 'ainda não processada' : 'motor Donchian inativo'));
        renderNext();
        // uma linha por perna (4 maiores em módulo, "+N" pro resto): "BNB LONG +$0,00 · BNB SHORT −$0,04"
        const legs = [...positions].sort((a, b) => Math.abs(b.unrealized_pnl_usd || 0) - Math.abs(a.unrealized_pnl_usd || 0));
        const legTxt = legs.slice(0, 4).map(p => `${String(p.symbol || '').replace(/USDT$/, '')} ${p.side} ${fmt.money(p.unrealized_pnl_usd, { signed: true })}`).concat(legs.length > 4 ? [`+${legs.length - 4}`] : []).join(' · ');
        setTile('st-unreal', fmt.money(s.unrealized_pnl, { signed: true }), legTxt || 'nenhuma posição aberta', cls(s.unrealized_pnl), s.unrealized_pnl);
        const bd = s.daily_pnl_breakdown || {};
        setTile('st-today', fmt.money(s.daily_pnl, { signed: true }), `trades ${fmt.money(bd.trades, { signed: true })} · funding ${fmt.money(bd.funding, { signed: true })} · comissão ${fmt.money(bd.commission, { signed: true })} · ${s.closed_trades_today ?? 0} fechado(s)`, cls(s.daily_pnl), s.daily_pnl);
        setTile('st-net', fmt.money(ft.net_usd, { signed: true }), `${ft.trades || 0} trades · taxas ${fmt.money(-(ft.fees_usd || 0), { signed: true })} · funding ${fmt.money(ft.funding_usd, { signed: true })}`, cls(ft.net_usd), ft.net_usd);
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
        const ex = $('tr-exp'); ex.textContent = fmt.money(ft.expectancy_usd, { signed: true }) + (isNum(ft.avg_r) ? ` · ${fmt.r(ft.avg_r)}` : ''); ex.className = cls(ft.expectancy_usd || 0);
        const ci = ft.expectancy_ci95;
        $('tr-exp-f').textContent = ci ? `IC 95% de ${fmt.money(ci[0], { signed: true })} a ${fmt.money(ci[1], { signed: true })} (bootstrap, ${tr} trades)` : `o intervalo de confiança aparece a partir de 5 trades (${tr} até agora)`;
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
            expectancy: fmt.money(ft.expectancy_usd, { signed: true }) + (isNum(ft.expectancy_pct) ? ` (${fmt.pct(ft.expectancy_pct)} notional)` : ''),
            avg_r: fmt.r(ft.avg_r), profit_factor: isNum(ft.profit_factor) ? fmt.num(ft.profit_factor) : '—',
            sharpe: isNum(ft.sharpe) ? fmt.num(ft.sharpe) : `— (${ft.sharpe_days || 0}/20 dias)`,
            win_rate: fmt.pct(ft.win_rate, 0, false),
            avg_win: isNum(ft.avg_win_usd) ? `${fmt.money(ft.avg_win_usd, { signed: true })} (${fmt.pct(ft.avg_win_pct, 1)})` : '—',
            avg_loss: isNum(ft.avg_loss_usd) ? `${fmt.money(ft.avg_loss_usd, { signed: true })} (${fmt.pct(ft.avg_loss_pct, 1)})` : '—',
            trades_per_week: fmt.num(ft.trades_per_week, 1), max_drawdown_pct: fmt.pct(ft.max_drawdown_pct, 2, false),
            costs: fmt.money(-(ft.fees_usd || 0) + (ft.funding_usd || 0), { signed: true }),
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
        out += ticks.map(t => `<line class="axis" x1="${x(t)}" x2="${x(t)}" y1="${y - 4}" y2="${y + 4}"/><text x="${x(t)}" y="${y + 20}" text-anchor="middle">${fmt.money(t, { signed: true })}</text>`).join('');
        const zRight = research == null || research >= 0;
        out += `<line class="zero" x1="${x(0)}" x2="${x(0)}" y1="${y - 30}" y2="${y + 8}"/><text class="strong" x="${x(0) + (zRight ? -6 : 6)}" y="${y - 36}" text-anchor="${zRight ? 'end' : 'start'}">zero</text>`;
        if (research != null) out += `<line class="research" x1="${x(research)}" x2="${x(research)}" y1="${y - 30}" y2="${y + 8}"/><text x="${x(research) + (zRight ? 6 : -6)}" y="${y - 36}" text-anchor="${zRight ? 'start' : 'end'}">pesquisa ${fmt.money(research, { signed: true })}</text>`;
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
        $('eq-sub').textContent = pts.length ? `${fmt.dateTime(f[0].timestamp)} → ${fmt.dateTime(f[f.length - 1].timestamp)} · ${fmt.money(pts[0].y)} → ${fmt.money(pts[pts.length - 1].y)}` : 'sem pontos nesta janela';
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
                    label: (it) => it.raw.t ? `${it.raw.t.symbol} ${it.raw.t.side} ${fmt.money(it.raw.t.pnl_net, { signed: true })} (${fmt.r(it.raw.t.r_multiple)})` : `equity ${fmt.money(it.parsed.y)}`,
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
        $('pos-sum').textContent = positions.length ? `não realizado ${fmt.money(sum(unreal), { signed: true })} · margem ${fmt.money(sum(positions.map(p => p.margin_usd).filter(isNum)))} · risco inicial ${fmt.money(sum(risk))}` : '';
        if (!positions.length) { root.innerHTML = '<p class="empty">Nenhuma posição aberta. O motor entra só no fechamento de barra 4h com rompimento de 40 barras.</p>'; return; }
        root.innerHTML = positions.map(p => {
            const age = p.entry_time ? (Date.now() - new Date(p.entry_time).getTime()) / 36e5 : null;
            const isD = p.strategy_type === 'donchian_4h';
            return `<div class="prow">
                <div class="prow-top">
                    <span class="psym"><button type="button" class="sym-btn" data-chart="${esc(p.symbol)}">${esc(p.symbol)}</button> <span class="side ${p.side === 'LONG' ? 'l' : 's'}">${esc(p.side)}</span>${isD ? '' : `<small class="dim">${esc(p.strategy_type)}</small>`}</span>
                    <span class="pr-pnl ${cls(p.unrealized_pnl_usd)}">${fmt.money(p.unrealized_pnl_usd, { signed: true })}<small>${fmt.pct(p.unrealized_pnl_percent)}</small></span>
                    <span class="pr-r ${cls(p.r_now)}">${fmt.r(p.r_now)}<small>risco ${fmt.money(p.risk_usd)}${isNum(p.trail_floor_r) ? ` · piso ${fmt.r(p.trail_floor_r)}` : ''}</small></span>
                    <span class="pr-meta">${fmt.price(p.quantity)} contratos · ${fmt.dur(age)}</span>
                    ${closeCell(p)}
                </div>
                ${isD ? ruler(p) : `<div class="ruler-l"><span>stop <b>${fmt.price(p.custom_stop_loss)}</b></span><span>TP <b>${fmt.price(p.custom_take_profit)}</b></span></div>`}
                ${futLine(p)}
            </div>`;
        }).join('');
    }

    // Fechar posição: o botão vira Confirmar/Cancelar inline; o próximo poll remove o card.
    const closing = new Set();
    const closeCell = (p) => `<span class="pr-close" data-symbol="${esc(p.symbol)}" data-side="${esc(p.side)}" data-pnl="${esc(fmt.money(p.unrealized_pnl_usd, { signed: true }))}">${
        closing.has(p.symbol) ? '<small class="dim">fechando…</small>' : '<button type="button" class="btn btn-quiet btn-xs" data-close="ask">Fechar</button>'
    }</span>`;
    const onClose = async (e) => {
        const btn = e.target.closest('[data-close]'); if (!btn) return;
        const cell = btn.closest('.pr-close'), sym = cell.dataset.symbol, side = cell.dataset.side;
        if (btn.dataset.close === 'ask') {
            cell.innerHTML = `<small>fechar ${esc(side)} ${esc(sym)} a mercado (${esc(cell.dataset.pnl)})?</small>
                <button type="button" class="btn btn-danger btn-xs" data-close="yes">Confirmar</button>
                <button type="button" class="btn btn-quiet btn-xs" data-close="no">Cancelar</button>`;
        } else if (btn.dataset.close === 'no') {
            cell.innerHTML = '<button type="button" class="btn btn-quiet btn-xs" data-close="ask">Fechar</button>';
        } else {
            closing.add(sym); cell.innerHTML = '<small class="dim">fechando…</small>';
            try {
                const r = await fetch(`/api/positions/${encodeURIComponent(sym)}/close`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: sym, side }), credentials: 'include' });
                const body = await r.json().catch(() => ({}));
                if (!r.ok || !body.ok) throw new Error(body.error || ('HTTP ' + r.status));
            } catch (err) {
                closing.delete(sym);
                cell.innerHTML = `<small class="neg">${esc(err.message)}</small> <button type="button" class="btn btn-quiet btn-xs" data-close="ask">Fechar</button>`;
                return;
            }
            reqRefresh();
        }
    };
    $('positions-list').addEventListener('click', onClose); $('mg-list').addEventListener('click', onClose);

    // ───────── Motor Donchian ─────────
    function renderEngine(e, positions) {
        const has = e && e.params;
        $('eng-params').textContent = has ? `entrada ${e.params.entry_bars} barras · saída ${e.params.exit_bars} · stop ${e.params.stop_atr_mult}×ATR(${e.params.atr_bars}) · risco ${String(e.params.risk_percent).replace('.', ',')}% por trade · máx. ${e.params.max_positions} posições, ${e.params.max_gross_mult}× equity bruto` : 'estratégia Donchian não ativa neste processo';
        $('eng-card').hidden = $('tile-last-bar').hidden = $('tile-next-bar').hidden = $('rdist-card').hidden = !has;
        $('cal-sub').textContent = has ? 'dia de trading em BRT, só trades Donchian' : 'dia de trading em BRT';
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
    function renderMartingale(m, s, positions, sheet) {
        // Equity × Performance ficam em qualquer estratégia (abaixo do anel/pernas no martingale)
        $('ft-card').hidden = $('pos-card').hidden = $('tile-equity').hidden = $('rdist-row').hidden = !!m; $('ring-card').hidden = $('legs-card').hidden = !m;
        $('trades-card').hidden = !!m;
        // a planilha é estática: renderiza sempre, com a faixa da equity atual (o runner só acrescenta score/pares ativos)
        $('sheet-card').hidden = !sheet; renderSheet(sheet, Object.keys((m && m.pairs) || {}));
        pageEmpties();
        if (!m) return;
        const rc = $('ring-card'), st = $('status'); if (st.parentElement !== rc) rc.appendChild(st);   // tiles viram o grid de métricas do anel
        renderRing(m, s);
        const away = (px, pct) => isNum(px) ? `<b>${fmt.price(px)}</b>${isNum(pct) ? ` <small>(${fmt.pct(pct)})</small>` : ''}` : '<b>—</b>';
        // Barra de saída: stop ─ degrau N+1 ─ empate ─ alvo, ponteiro no preço atual (anda a cada poll);
        // vermelho do stop ao empate, verde do empate ao alvo; rótulo = preço + P&L da perna naquele preço.
        const mgRuler = (l) => {
            const long = l.side === 'LONG', avg = l.avg_price, be = l.breakeven_price, tp = l.tp_price, st = l.stop_price, nx = l.next_add_price, mk = l.mark_price;
            if (![avg, be, tp].every(isNum)) return '';
            const pnlAt = (p) => fmt.money((p - be) * l.quantity * (long ? 1 : -1), { signed: true });   // líquido: do empate (fees já descontadas)
            const marks = [[st, 'stop', 'Stop'], [nx, 'add', `Degrau ${l.level + 1}`], [be, 'entry', 'Empate'], [tp, 'tp', 'Alvo']].filter(m => isNum(m[0]));
            // Eixo: alvo → marco mais distante entre empate e degrau seguinte, + margem (maior de alvo→empate, 1 passo da escada).
            // Stop/preço fora do eixo prendem na borda com seta em vez de esticar a escala (stop de portfólio pode estar a 6× do preço).
            const far = isNum(nx) ? nx : be, margin = Math.max(Math.abs(tp - be), isNum(nx) ? Math.abs(avg - nx) : 0);
            let lo = Math.min(tp, far), hi = Math.max(tp, far); if (long) lo -= margin; else hi += margin;
            const pad = (hi - lo) * 0.08 || avg * 0.01; lo -= pad; hi += pad;
            const clamp = (v) => Math.min(hi, Math.max(lo, v)), out = (v) => isNum(v) && (v < lo || v > hi) ? (v > hi ? 'r' : 'l') : '';
            const pct = (v) => (clamp(v) - lo) / (hi - lo) * 100, at = (v) => pct(v).toFixed(2) + '%';
            const zone = (a, b, c) => `<div class="zone ${c}" style="left:${at(Math.min(a, b))};width:${(Math.abs(clamp(a) - clamp(b)) / (hi - lo) * 100).toFixed(2)}%"></div>`;
            const worst = isNum(st) ? st : (long ? lo : hi);   // sem stop (hedge cobre): vermelho até a borda
            const arrow = (v, txt) => out(v) === 'r' ? `${txt} →` : out(v) === 'l' ? `← ${txt}` : txt;
            const edge = (v, txt) => out(v) ? `<span class="edge ${out(v)}">${arrow(v, txt)}</span>` : '';
            // Escala de ganho sobre o verde: mín (empate) → máx (alvo), preenchida até o preço quando ele está dentro.
            const z0 = Math.min(be, tp), z1 = Math.max(be, tp), zw = pct(z1) - pct(z0), inside = isNum(mk) && mk >= z0 && mk <= z1;
            const fillCut = inside ? (long ? `0 ${100 - (pct(mk) - pct(z0)) / zw * 100}% 0 0` : `0 0 0 ${(pct(mk) - pct(z0)) / zw * 100}%`) : '';
            const gl = (v, txt, side) => `<span class="gl ${side}" style="${side === 'r' ? `right:${(100 - pct(v)).toFixed(2)}%` : `left:${at(v)}`}">${txt}</span>`;
            const [mn, mx] = [`mín ${pnlAt(be)}`, `máx ${pnlAt(tp)}`];
            // faixa estreita: os 3 rótulos (mín/meio/máx) não cabem → uma linha só, ancorada no lado com espaço. A largura é a do CARD (grade de 2 no desktop).
            const cardW = $('mg-list').clientWidth / (matchMedia('(min-width: 1101px)').matches ? 2 : 1) - 44;
            const narrow = zw / 100 * cardW < 260;
            const gain = `<div class="gain" aria-hidden="true">
                ${inside && !narrow ? gl(mk, `agora ${pnlAt(mk)}`, 'now') : ''}
                <div class="gbar${long ? '' : ' rev'}" style="left:${at(z0)};width:${zw.toFixed(2)}%"></div>
                ${inside ? `<div class="gbar gfill${long ? '' : ' rev'}" style="left:${at(z0)};width:${zw.toFixed(2)}%;clip-path:inset(${fillCut})"></div>` : ''}
                ${narrow
                    ? gl(pct(z0) < 50 ? z0 : z1, `0 → ${mx}${inside ? ` · agora ${pnlAt(mk)}` : ''}`, pct(z0) < 50 ? 'l' : 'r')
                    : gl(z0, long ? mn : mx, 'l') + gl((z0 + z1) / 2, pnlAt((be + tp) / 2), 'c') + gl(z1, long ? mx : mn, 'r')}
            </div>`;
            // Rótulos ancorados no próprio marcador (duas fileiras alternadas pra não se sobrepor), preço atual acima do eixo.
            const anchored = marks.slice().sort((a, b) => a[0] - b[0]).map(([v, c, n], i) =>
                `<span class="ra ${c} r${i % 2}" style="left:${at(v)}"><em>${arrow(v, n)}</em><b>${fmt.price(v)}</b><small>${pnlAt(v)}</small></span>`).join('');
            return `<div class="ruler" aria-hidden="true">
                <div class="line"></div>${zone(worst, be, 'neg')}${zone(be, tp, '')}
                ${marks.map(([v, c, n]) => `<span class="mk ${c}" style="left:${at(v)}" title="${n} ${fmt.price(v)} (${pnlAt(v)})"></span>`).join('')}
                ${isNum(mk) ? `<span class="mk mark" style="left:${at(mk)}" title="preço ${fmt.price(mk)}"></span><span class="now" style="left:${at(mk)}">${arrow(mk, 'preço')} ${fmt.price(mk)}</span>` : ''}
                ${isNum(st) ? edge(st, `Stop ${fmt.price(st)}`) : ''}${isNum(mk) && !(isNum(st) && out(st) === out(mk)) ? edge(mk, `preço ${fmt.price(mk)}`) : ''}
            </div>
            <div class="ruler-anchors" aria-hidden="true">${anchored}</div>${gain}`;
        };
        const unreal = (positions || []).map(p => p.unrealized_pnl_usd).filter(isNum);
        $('legs-sum').textContent = unreal.length ? `não realizado ${fmt.money(sum(unreal), { signed: true })}` : '';
        const draining = new Set(m.draining || []);
        // Um card por perna, como a tela de posições de uma exchange: cabeçalho (par, lado, alavancagem),
        // P&L + ROE em destaque, escada N de 9 como barra, a régua de saída grande e a grade de métricas.
        // Perna sem posição vira um card compacto "aguardando entrada" com a próxima adição e o alvo.
        const kv = (k, v) => isNum(v) || typeof v === 'string' ? `<div><dt>${k}</dt><dd>${v}</dd></div>` : '';
        $('mg-list').innerHTML = (m.legs || []).map(l => {
            const pos = (positions || []).find(p => p.symbol === l.symbol && p.side === l.side);
            const age = pos && pos.entry_time ? (Date.now() - new Date(pos.entry_time).getTime()) / 36e5 : null;
            const long = l.side === 'LONG', lev = pos && isNum(pos.leverage) ? `<span class="leg-lev">${pos.leverage}×</span>` : '';
            const head = `<header class="leg-h">
                <div class="leg-id"><button type="button" class="sym-btn leg-sym" data-chart="${esc(l.symbol)}">${esc(l.symbol.replace(/USDT$/, ''))}<small>USDT perp</small></button><span class="side ${long ? 'l' : 's'}">${esc(l.side)}</span>${lev}${draining.has(l.symbol) ? '<span class="badge warn">drenando</span>' : ''}</div>
                ${pos ? `<div class="leg-pnl ${cls(l.unrealized_pnl_usd)}"><b>${fmt.money(l.unrealized_pnl_usd, { signed: true })}</b><small>${isNum(pos.roe_percent) ? `ROE ${fmt.pct(pos.roe_percent, 1)}` : fmt.pct(pos.unrealized_pnl_percent)}</small></div>` : ''}
                ${pos ? closeCell(pos) : ''}
            </header>`;
            if (!pos) return `<article class="leg wait">${head}
                <p class="leg-wait">Aguardando entrada<small>${l.level >= l.max_levels ? 'teto da escada' : `próx. adição @ ${away(l.next_add_price, l.next_add_pct)}`} · alvo @ ${away(l.tp_price, l.tp_pct)}</small></p>
            </article>`;
            const ladder = `<div class="leg-ladder"><span>Escada <b>${l.level} de ${l.max_levels}</b> ordens</span><div class="meter"><i style="width:${(l.level / l.max_levels * 100).toFixed(0)}%"></i></div><span>${l.level >= l.max_levels ? 'teto' : `próx. degrau @ ${away(l.next_add_price, l.next_add_pct)}`}</span></div>`;
            const liq = isNum(pos.liquidation_price) ? `<b class="neg">${fmt.price(pos.liquidation_price)}</b>${isNum(pos.mark_price) && pos.mark_price > 0 ? ` <small>(${fmt.pct(Math.abs(pos.liquidation_price - pos.mark_price) / pos.mark_price * 100, 1, false)})</small>` : ''}` : '—';
            const grid = `<dl class="leg-grid">
                ${kv('Contratos', fmt.price(l.quantity))}${kv('Notional', fmt.money(l.notional))}${kv('Margem', fmt.money(pos.margin_usd))}
                ${kv('Média', fmt.price(l.avg_price))}${kv('Marca', fmt.price(l.mark_price))}${kv('Liquidação', liq)}
                ${kv('Alvo', away(l.tp_price, l.tp_pct))}${kv('Idade', fmt.dur(age))}
            </dl>`;
            return `<article class="leg ${long ? 'long' : 'short'}">${head}${ladder}
                <div class="leg-ruler">${mgRuler(l) || `<div class="ruler-l"><span>próx. adição @ ${away(l.next_add_price, l.next_add_pct)}</span><span>TP @ ${away(l.tp_price, l.tp_pct)}</span></div>`}</div>
                ${grid}
            </article>`; }).join('') || '<p class="empty">Nenhum par na escada (estratégia não iniciou ou nenhum par cabe na ordem base).</p>';
    }

    // ───────── Anel da carteira ─────────
    // Um arco por par com perna aberta, comprimento ∝ notional (qty×mark das duas pernas); toque seleciona o par.
    let ringSel = null, ringLast = null;
    const ringPt = (a, r) => { const t = (a - 90) * Math.PI / 180; return [160 + r * Math.cos(t), 160 + r * Math.sin(t)]; };
    function ringArc(a0, a1) { const [x0, y0] = ringPt(a0, 118), [x1, y1] = ringPt(a1, 118); return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A 118 118 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`; }
    function renderRing(m, s) {
        ringLast = [m, s];
        const pairs = {};
        for (const l of m.legs || []) {
            if (!isNum(l.quantity) || l.quantity <= 0) continue;
            const p = pairs[l.symbol] || (pairs[l.symbol] = { symbol: l.symbol, notional: 0, upnl: 0, legs: [] });
            p.notional += isNum(l.mark_price) ? l.quantity * l.mark_price : (l.notional || 0); p.upnl += l.unrealized_pnl_usd || 0; p.legs.push(l);
        }
        const list = Object.values(pairs), total = sum(list.map(p => p.notional)) || 1, draining = new Set(m.draining || []);
        if (ringSel && !pairs[ringSel]) ringSel = null;
        const GAP = 6, span = (360 - list.length * GAP) / total;
        let a = 0, segs = '', badges = '';
        list.forEach((p, i) => {
            const a0 = a + GAP / 2, a1 = a0 + p.notional * span, mid = (a0 + a1) / 2; a = a1 + GAP / 2;
            const sel = p.symbol === ringSel, [mx, my] = ringPt(a1 - 2, 118), [bx, by] = ringPt(mid, 150);
            segs += `<path class="ring-seg${sel ? ' sel' : ''}" d="${ringArc(a0, a1)}" stroke="url(#rg${i % 5})" data-symbol="${esc(p.symbol)}"/>` +
                `<circle class="ring-mk ${cls(p.upnl)}" cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="4"/>`;
            badges += `<button type="button" class="ring-badge${sel ? ' on' : ''}${draining.has(p.symbol) ? ' drain' : ''}" style="left:${(bx / 3.2).toFixed(1)}%;top:${(by / 3.2).toFixed(1)}%" data-symbol="${esc(p.symbol)}" aria-pressed="${sel}">${esc(p.symbol.replace(/USDT$/, ''))}</button>`;
        });
        $('ring-segs').innerHTML = segs; $('ring-badges').innerHTML = badges;
        $('ring-state').textContent = s.paused ? 'Pausado' : (s.running ? 'Rodando' : 'Parado');
        $('ring-state').className = 'ring-state ' + (s.paused ? 'warn' : (s.running ? 'ok' : 'bad'));
        $('ring-stop').textContent = `stop em ${fmt.money(m.portfolio_stop_equity)}` + (isNum(m.portfolio_stop_room_pct) ? ` · faltam ${fmt.pct(m.portfolio_stop_room_pct, 1, false)}` : '');
        $('ring-stop').className = 'cs ' + (isNum(m.portfolio_stop_room_pct) && m.portfolio_stop_room_pct < 5 ? 'warn' : '');
        $('tile-stop').hidden = false;
        setTile('st-stop', fmt.money(m.portfolio_stop_equity), (isNum(m.portfolio_stop_room_pct) ? `faltam ${fmt.pct(m.portfolio_stop_room_pct, 1, false)} da equity` : 'sem equity') + ` · pico ${fmt.money(m.peak_equity)}` + (m.portfolio_stops ? ` · ${m.portfolio_stops} stop(s) de portfólio` : ''),
            isNum(m.portfolio_stop_room_pct) && m.portfolio_stop_room_pct < 5 ? 'neg' : '', m.portfolio_stop_equity);
        const c = $('ring-center'), sp = pairs[ringSel];
        if (sp) {
            const deep = sp.legs.reduce((x, y) => (y.level > x.level ? y : x));
            c.innerHTML = `<span class="rc-l">${esc(sp.symbol.replace(/USDT$/, ''))}</span><b class="rc-v ${cls(sp.upnl)}">${fmt.money(sp.upnl, { signed: true })}</b>` +
                `<span class="rc-t">${sp.legs.map(l => `${esc(l.side)} L${l.level}`).join(' · ')}</span>` +
                `<small class="rc-s">add ${fmt.price(deep.next_add_price)} · TP ${fmt.price(deep.tp_price)}</small>`;
        } else {
            const eq = s.last_balance, d = s.daily_pnl, pct = isNum(d) && isNum(eq) && eq - d > 0 ? d / (eq - d) * 100 : null;
            c.innerHTML = `<span class="rc-l">Saldo</span><b class="rc-v">${fmt.money(eq)}</b>` +
                `<span class="rc-t ${cls(d)}">${fmt.money(d, { signed: true })}${isNum(pct) ? ` (${fmt.pct(pct)})` : ''}</span>` + brlTag(eq) +
                `<span class="rc-s rc-sub">${esc($('st-equity-f').textContent)}</span>`;
        }
    }
    $('ring-card').addEventListener('click', (e) => {
        const t = e.target.closest('[data-symbol]');
        if (t) ringSel = ringSel === t.dataset.symbol ? null : t.dataset.symbol;
        else if (e.target.closest('.ring-center')) ringSel = null;
        else return;
        if (ringLast) renderRing(...ringLast);
    });

    // Planilha de banca: linha da equity atual destacada, moedas da planilha com as do bot marcadas.
    function renderSheet(sh, botPairs) {
        if (!sh) return;
        const eq = state.live && state.live.summary && state.live.summary.last_balance;
        const cur = sh.current, rows = sh.rows || [], nxt = rows[cur == null ? 0 : cur + 1];
        // linha: [rótulo, banca mín., estratégia, 1ª long, 1ª short, alavancagem, stop, double first, moedas]
        $('sheet-sub').textContent = `Banca ${fmt.money(eq)} → ` + (cur == null ? 'abaixo da 1ª faixa (90 USDT)' : `faixa ${rows[cur][1]} USDT · ${rows[cur][8]} moeda(s)`)
            + (nxt ? ` · próxima faixa $${nxt[1]} (${nxt[8]} moedas)` : '');
        const pj = sh.projection, pace = $('sheet-pace');
        pace.hidden = !pj;
        if (pj) {
            const dias = `${pj.days_elapsed} dia${pj.days_elapsed === 1 ? '' : 's'}`;
            pace.textContent = `Ritmo: ${fmt.money(pj.avg_daily, { signed: true })}/dia (${pj.trades} trades em ${dias})`
                + (pj.next_bank == null ? '' : pj.eta_days == null ? ' · próxima faixa não alcançável neste ritmo'
                    : ` · faltam ${fmt.money(pj.gap)} p/ $${pj.next_bank} → ~${pj.eta_days} dias (≈ ${pj.eta_date.split('-').reverse().join('/')})`);
        }
        $('sheet-table').querySelector('tbody').innerHTML = rows.map((r, i) => `<tr class="${i === cur ? 'cur' : ''}">
            <td>${esc(r[0])}</td><td class="sans">${esc(r[2])}</td><td class="r">${r[3]}</td><td class="r">${r[4]}</td><td class="r">${r[5]}</td><td class="r">${Number(r[6]).toLocaleString('pt-BR')}</td><td class="sans">${r[7] ? 'ON' : 'OFF'}</td><td class="r">${r[8]}</td></tr>`).join('');
        const bases = new Set((botPairs || []).map(p => String(p).replace(/USDT$/, '')));
        // Score por regras (verde/amarelo/vermelho/cinza=sem dado) com os números no tooltip.
        const scores = sh.scores || {}, cls = { green: 'l', yellow: 'w', red: 's' };
        const num = (v, nd = 1) => isNum(v) ? Number(v).toFixed(nd).replace('.', ',') : '—';
        const tip = (c, sc) => sc && sc.color !== 'unknown'
            ? `${c}: vol ${num(sc.vol_pct)}% · adx ${num(sc.adx, 0)} · mov. 30d ${num(sc.max_move_pct)}% · vol. ${num((sc.volume_usd || 0) / 1e6, 0)}M` + ((sc.reasons || []).length ? ` — ${sc.reasons.join('; ')}` : ' — tudo verde')
            : `${c}: sem dado ainda`;
        const pill = (c) => { const sc = scores[c + 'USDT']; return `<span class="side ${cls[(sc || {}).color] || 'u'}${bases.has(c) ? ' on' : ''}" title="${esc(tip(c, sc))}">${esc(c)}</span>`; };
        const at = sh.scores_updated_at ? Date.parse(sh.scores_updated_at) : NaN;
        $('sheet-coins').innerHTML = 'Moedas da planilha: ' + (sh.green || []).map(pill).join('') + ' <span>planilha: evitar (voláteis/memecoins):</span> ' + (sh.red || []).map(pill).join('')
            + ` <small>· score ${isNum(at) ? 'atualizado há ' + fmt.ago((Date.now() - at) / 1000) : 'ainda sem dado'}</small>`;
        // Ranking pelo score: top N (N = moedas da faixa atual) + onde ficaram os pares ativos fora do top.
        const ord = (i) => `${i}º`, slots = (rows[cur == null ? 0 : cur] || [])[8] || 0;
        const ranking = (sh.ranking || []).filter(r => !r.sheet_red), ranked = ranking.filter(r => isNum(r.score));
        const rpill = (r) => `<span class="side ${cls[r.color] || 'u'}${r.active ? ' on' : ''}" title="${esc(tip(r.symbol.replace(/USDT$/, ''), scores[r.symbol]))}">${ord(r.rank)} ${esc(r.symbol.replace(/USDT$/, ''))}</span>`;
        const top = ranked.slice(0, slots), outside = ranking.filter(r => r.active && !top.includes(r));
        $('sheet-rank').innerHTML = ranked.length
            ? `Melhores agora (${slots} vaga${slots === 1 ? '' : 's'} na faixa): ` + top.map(rpill).join('')
              + (outside.length ? ` <span>ativos fora do top: ${outside.map(r => `${esc(r.symbol.replace(/USDT$/, ''))} está em ${isNum(r.score) ? ord(r.rank) : 'sem dado'}`).join(', ')}</span>` : '')
            : 'Melhores agora: ainda sem dado';
        const bad = [...bases].filter(c => (scores[c + 'USDT'] || {}).color === 'red');
        $('sheet-warn').hidden = !bad.length;
        $('sheet-warn').textContent = bad.map(c => `⚠ ${c} está vermelho: ${(scores[c + 'USDT'].reasons || []).join('; ')}`).join(' · ');
    }

    // ───────── Saúde ─────────
    function renderHealth(h, s) {
        const items = [];
        const dot = (st) => `<i class="dotc ${st}"></i>`;
        if (!h) { $('health-list').innerHTML = '<li class="empty">sem dados</li>'; return; }
        let worst = 'ok';
        const bump = (st) => { if (st === 'bad' || (st === 'warn' && worst === 'ok')) worst = st; };
        const us = h.user_stream;
        if (us) { const st = !us.started ? 'bad' : (us.error_count > 0 ? 'warn' : 'ok'); bump(st);
            items.push([dot(st) + 'User stream (fills)', `${us.started ? 'ativo' : 'parado'}<small>${us.error_count || 0} erros · ${us.restart_attempts || 0} restarts · última msg há ${fmt.ago(us.last_message_age_seconds)}</small>`]); }
        const rt = h.runtime || {};
        { const st = rt.loop_errors > 0 ? 'warn' : 'ok'; bump(st);
            items.push([dot(st) + 'Loop principal', `${rt.loop_errors || 0} erro${rt.loop_errors === 1 ? '' : 's'} desde o último relatório<small>${rt.last_error ? esc(rt.last_error) : 'sem erro registrado'} · ${rt.monitor_cycles || 0} ciclos</small>`]); }
        { const st = s.running ? 'ok' : 'bad'; bump(st);
            items.push([dot(st) + 'Processo', `${s.running ? 'rodando' : 'parado'} há ${fmt.ago(h.uptime_seconds)}<small>deploy ${h.deploy_sha || 'n/d'}${h.deploy_at ? ' · ' + fmt.dateTime(h.deploy_at) : ''}</small>`]); }
        items.push([dot(conn === 'connected' ? 'ok' : conn === 'polling' ? 'warn' : 'bad') + 'Dashboard', `${connLabel()}<small>estado a cada ${REFRESH}s · histórico quando um trade fecha · P&L reconciliado com fills da ${esc(s.exchange || 'exchange')}</small>`]);
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
        $('tr-stats').innerHTML = n ? `<span>líquido <b class="${cls(net)}">${fmt.money(net, { signed: true })}</b></span><span>win rate <b>${fmt.pct(wins / n * 100, 0, false)}</b></span><span>R médio <b>${rs.length ? fmt.r(sum(rs) / rs.length) : '—'}</b></span><span>Σ R <b>${rs.length ? fmt.r(sum(rs)) : '—'}</b></span><span>custos <b>${fmt.money(costs, { signed: true })}</b></span>` : '';
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
                <td class="r ${cls(t.pnl_net)}">${fmt.money(t.pnl_net, { signed: true })}</td>
                <td class="r dim">${fmt.money(-(t.fees || 0) + (t.funding || 0), { signed: true })}</td>
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
                plugins: { legend: { display: false }, tooltip: { ...baseTooltip(p), callbacks: { title: (it) => it[0].raw.e.symbol, label: (it) => `MFE ${fmt.pct(it.raw.e.mfe, 2, false)} · MAE ${isNum(it.raw.e.mae) ? fmt.pct(it.raw.e.mae, 2, false) : 'n/d'} · ${fmt.r(it.raw.y)} (${fmt.money(it.raw.e.pnl, { signed: true })})` } } },
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
            const c = v ? cls(v.net) : '';
            const op = v ? (0.35 + 0.65 * Math.min(1, Math.abs(v.net) / maxAbs)).toFixed(2) : '';
            html += `<i class="${c}${future || before ? ' future' : ''}" style="${op ? `opacity:${op}` : ''}" title="${fmt.day(key)}${v ? `: ${fmt.money(v.net, { signed: true })} em ${v.trades} trade(s)` : ''}"></i>`;
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
            return `<tr><th scope="row">${label(k)}</th><td class="r">${g.trades}</td><td class="r">${fmt.pct(g.wins / g.trades * 100, 0, false)}</td><td class="r ${cls(g.net)}">${fmt.money(g.net, { signed: true })}</td></tr>`; }).join('')
            || '<tr><td colspan="4" class="empty">sem trades fechados</td></tr>';
    }

    // ───────── Análise do P&L de futuros, réplica da tela da Bitget (/api/pnl_report) ─────────
    const PNL_PAGE = 10;
    const dayStr = (d) => d.toISOString().slice(0, 10);
    const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return dayStr(d); };
    const dayRange = (from, to) => { const out = []; for (let d = from; d <= to; d = addDays(d, 1)) out.push(d); return out; };
    const pnlState = { days: 30, from: null, to: null, tab: 'account', month: null, page: {}, len: {}, symDir: -1 };
    // presets mandam só `days`: o servidor ancora no dia UTC dele (como a Bitget), não na data do navegador
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
        const t = p.totals || {}, w = p.windows || {}, a = p.account || {};
        $('pnl-sub').textContent = `${fmt.day(p.from)} → ${fmt.day(p.to)} · ${t.trades || 0} trade(s) · dia UTC (como na Bitget)`;
        // Análise da conta: saldo + hoje/7d/30d
        $('pnl-equity').textContent = a.ok ? fmt.money(a.equity) : '—';
        $('pnl-equity-alt').textContent = a.ok ? fmt.moneyAlt(a.equity) : `saldo indisponível (${exchangeName()} não respondeu)`;
        [['today', p.today_pnl], ['7d', w.d7], ['30d', w.d30]].forEach(([k, v]) => {
            const usd = v && v.usd, b = $(`pnl-${k}-usd`); b.innerHTML = fmt.money(usd, { signed: true }) + brlTag(usd); b.className = cls(usd);
            const sm = $(`pnl-${k}-pct`); sm.textContent = pctTxt(v); sm.className = cls(usd);
        });
        // Análise de trading: realizado + taxa de ganho + extremos + lista
        const real = $('pnl-real-usd'); real.textContent = fmt.money(t.net, { signed: true }); real.className = 'tv ' + cls(t.net);
        $('pnl-real-alt').textContent = fmt.moneyAlt(t.net);
        $('pnl-win-rate').textContent = fmt.pct(t.win_rate, 2, false);
        $('pnl-best').textContent = fmt.money(t.best, { signed: true }); $('pnl-worst').textContent = fmt.money(t.worst, { signed: true });
        const it = p.income_totals || {}, fees = p.income_ok ? it.COMMISSION : -(t.fees || 0), funding = p.income_ok ? it.FUNDING_FEE : t.funding;
        $('pnl-stats').innerHTML = [
            ['Número de trades encerrados', `${t.trades || 0} <small class="dim">(<span class="pos">${t.wins || 0} lucro</span> / <span class="neg">${t.losses || 0} perda</span>)</small>`],
            ['Índice long/short', `${t.long || 0} : ${t.short || 0}`],
            ['Índice de P&L', isNum(t.profit_factor) ? fmt.num(t.profit_factor) : '—'],
            ['Taxa de transação', `<span class="${cls(fees)}">${fmt.money(fees, { signed: true })}</span>`],
            ['Taxa de financiamento', `<span class="${cls(funding)}">${fmt.money(funding, { signed: true })}</span>`],
        ].map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('');
        if (pnlState.days === 'custom') { $('pnl-from').value = p.from; $('pnl-to').value = p.to; }
        if (!pnlState.month || pnlState.month < p.from.slice(0, 7) || pnlState.month > p.to.slice(0, 7)) pnlState.month = p.to.slice(0, 7);
        renderPnlCalendar(p); renderPnlTotal(p); renderPnlRoi(p); renderPnlEquity(p);
        renderPnlBars(p); renderPnlFunding(p);
        renderPnlTrades(p); renderPnlSymbols(p); renderPnlIncome(p);
    }
    function renderPnlCalendar(p) {
        // extrato da conta por dia UTC (p.calendar: realizado + taxas + funding); o nº de trades do dia vem da aba de trading
        const byDay = new Map((p.calendar || []).map(d => [d.day, d])), tradesOf = new Map((p.daily || []).map(d => [d.day, d.trades]));
        const [y, m] = pnlState.month.split('-').map(Number);
        $('pnl-cal-month').textContent = new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString('pt-BR', { month: 'long', year: 'numeric', timeZone: 'UTC' });
        $('pnl-cal-prev').disabled = pnlState.month <= p.from.slice(0, 7); $('pnl-cal-next').disabled = pnlState.month >= p.to.slice(0, 7);
        $('pnl-cal-sub').textContent = 'extrato da conta: realizado + taxas + funding · dia UTC (como na Bitget)';
        const first = new Date(Date.UTC(y, m - 1, 1)), nDays = new Date(Date.UTC(y, m, 0)).getUTCDate();
        let html = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'].map(d => `<span class="wd">${d}</span>`).join('') + '<i></i>'.repeat(first.getUTCDay());
        for (let d = 1; d <= nDays; d++) {
            const key = `${pnlState.month}-${String(d).padStart(2, '0')}`, v = byDay.get(key);
            const out = key < p.from || key > p.to, future = key > p.today, today = key === p.today;
            const c = v ? (cls(v.net) || 'zero') : (out || future ? 'off' : 'zero');
            html += `<span class="day ${c}${today ? ' today' : ''}" role="gridcell" title="${fmt.day(key)}${v ? `: ${fmt.money(v.net, { signed: true })} · ${tradesOf.get(key) || 0} trade(s)` : ''}"><small>${today ? 'Hoje' : d}</small>${v ? `<b>${fmt.money(v.net, { signed: true })}</b>` : (out || future ? '' : '<b>0,00</b>')}</span>`;
        }
        // completa a última semana com células "off" pra grade não terminar com buraco
        html += '<span class="day off" aria-hidden="true"></span>'.repeat((7 - (first.getUTCDay() + nDays) % 7) % 7);
        $('pnl-cal').innerHTML = html;
    }
    function pnlBase(p, id) { const pal = palette(); return { pal, el: $(id), ok: !!$(id) && typeof Chart !== 'undefined', days: dayRange(p.from, p.to) }; }
    // barras por dia (verde ≥ 0, vermelha < 0), reaproveitadas pelo realizado diário e pelo funding diário
    function pnlBars(p, key, id, byDay) {
        const { pal, el, ok, days } = pnlBase(p, id); if (!ok) return;
        const data = days.map(d => byDay.get(d) ?? 0), colors = data.map(v => v < 0 ? pal.bad : pal.good);
        if (charts[key]) { charts[key].data.labels = days; charts[key].data.datasets[0].data = data; charts[key].data.datasets[0].backgroundColor = colors; charts[key].update('none'); return; }
        charts[key] = new Chart(el, { type: 'bar', data: { labels: days, datasets: [{ data, backgroundColor: colors, borderRadius: 3, maxBarThickness: 28 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { ...baseTooltip(pal), callbacks: { title: (it) => fmt.day(it[0].label), label: (it) => fmt.money(it.parsed.y, { signed: true }) } } },
                scales: { x: { ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 8, callback: mmdd }, grid: { display: false } }, y: { ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 5, callback: v => fmt.money(v, { signed: true }) }, grid: { color: pal.grid } } } } });
    }
    function renderPnlBars(p) { pnlBars(p, 'pnlBars', 'pnl-bars-chart', new Map((p.daily || []).map(d => [d.day, d.net]))); }
    function renderPnlFunding(p) {
        // income da exchange por dia; sem income, o funding atribuído a cada trade fechado (pelo dia da saída)
        const src = p.income_ok ? p.funding_daily : (p.daily || []).map(d => ({ day: d.day, amount: d.funding }));
        $('pnl-funding-sub').textContent = p.income_ok ? `income da ${exchangeName()}` : 'funding dos trades fechados (income indisponível)';
        pnlBars(p, 'pnlFunding', 'pnl-funding-chart', new Map((src || []).map(d => [d.day, d.amount])));
    }
    // rótulo do eixo x lido da escala (não de um array fechado na criação: o período muda e os labels crescem)
    function mmdd(v) { return String(this.getLabelForValue(v)).slice(5); }
    function lineOpts(pal, fmtY) {
        return { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: 'index', intersect: false },
            plugins: { legend: { display: false }, tooltip: { ...baseTooltip(pal), displayColors: true, callbacks: { title: (it) => fmt.day(it[0].label), label: (it) => `${it.dataset.label || ''} ${fmtY(it.parsed.y)}`.trim() } } },
            scales: { x: { ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 6, callback: mmdd }, grid: { display: false } }, y: { position: 'right', ticks: { color: pal.mut, font: tickFont, maxTicksLimit: 5, callback: fmtY }, grid: { color: pal.grid } } } };
    }
    // série por dia com o último valor carregado pra frente (dia sem trade/snapshot repete o anterior)
    const carry = (days, map, init = null) => { let last = init; return days.map(d => { if (map.has(d)) last = map.get(d); return last; }); };
    const lastOf = (a) => { for (let i = a.length - 1; i >= 0; i--) if (isNum(a[i])) return a[i]; return null; };
    const lineDs = (label, data, color, w = 1.5) => ({ label, data, borderColor: color, borderWidth: w, pointRadius: 0, tension: 0.2, spanGaps: true });
    function pnlLines(p, key, id, dss, fmtY) {
        const { pal, el, ok, days } = pnlBase(p, id); if (!ok) return;
        if (charts[key]) { charts[key].data.labels = days; charts[key].data.datasets = dss; charts[key].update('none'); return; }
        charts[key] = new Chart(el, { type: 'line', data: { labels: days, datasets: dss }, options: lineOpts(pal, fmtY) });
    }
    function renderPnlTotal(p) {
        const pal = palette(), days = dayRange(p.from, p.to);
        // total = extrato da conta acumulado (p.cumulative); como na Bitget, realizado = o mesmo acumulado
        const total = carry(days, new Map((p.cumulative || []).map(c => [c.day, c.value])), 0), realized = total;
        const unreal = carry(days, new Map((p.series || []).map(s => [s.day, s.unrealized])));
        const liveUnreal = p.account && p.account.ok ? p.account.unrealized : lastOf(unreal);
        const series = [['P&L total', total, pal.warn, 2], ['P&L realizado', realized, pal.good], ['P&L não realizado', unreal, pal.ac]];
        [['pnl-tot-total', lastOf(total)], ['pnl-tot-real', lastOf(realized)], ['pnl-tot-unreal', liveUnreal]].forEach(([id, v]) => { const el = $(id); el.textContent = fmt.money(v, { signed: true }); el.className = cls(v); });
        $('pnl-total-legend').innerHTML = series.map(([label, , color]) => `<i class="sw" style="background:${color}"></i>${label}`).join('');
        pnlLines(p, 'pnlTotal', 'pnl-total-chart', series.map(([l, d, c, w]) => lineDs(l, d, c, w)), v => fmt.money(v, { signed: true }));
    }
    function renderPnlRoi(p) {
        const pal = palette(), days = dayRange(p.from, p.to), eq = p.period && p.period.start_equity;
        const roi = carry(days, new Map((p.cumulative || []).map(c => [c.day, c.value])), 0).map(v => eq > 0 ? v / eq * 100 : null);
        const btc = carry(days, new Map(((p.reference || {}).BTCUSDT || []).map(r => [r.day, r.pct])));
        [['pnl-roi', lastOf(roi)], ['pnl-roi-btc', lastOf(btc)]].forEach(([id, v]) => { const el = $(id); el.textContent = fmt.pct(v); el.className = cls(v); });
        pnlLines(p, 'pnlRoi', 'pnl-roi-chart', [lineDs('ROI', roi, pal.warn, 2), lineDs('BTC', btc, pal.ac)], v => fmt.pct(v));
    }
    function renderPnlEquity(p) {
        const pal = palette(), days = dayRange(p.from, p.to);
        const eq = carry(days, new Map((p.series || []).map(s => [s.day, s.equity])));
        const n = (p.series || []).length;
        $('pnl-equity-sub').textContent = n ? `${n} dia(s) com snapshot` : 'sem snapshots de equity no período';
        const ds = { ...lineDs('Equity', eq, pal.ac, 2), fill: true, backgroundColor: hexa(pal.ac, 0.12) };
        pnlLines(p, 'pnlEquity', 'pnl-equity-chart', [ds], v => fmt.money(v));
    }
    // fatia da página atual de `rows` e atualiza o pager da aba; volta à página 1 quando a lista muda de tamanho (refresh/período)
    function pnlPaginate(tab, rows) {
        if (pnlState.len[tab] !== rows.length) { pnlState.len[tab] = rows.length; pnlState.page[tab] = 1; }
        const pages = Math.max(1, Math.ceil(rows.length / PNL_PAGE)), page = pnlState.page[tab] = Math.max(1, Math.min(pnlState.page[tab] || 1, pages));
        $(`pnl-${tab}-pager`).hidden = pages <= 1; $(`pnl-${tab}-page`).textContent = `página ${page} de ${pages}`;
        $(`pnl-${tab}-prev`).disabled = page <= 1; $(`pnl-${tab}-next`).disabled = page >= pages;
        return rows.slice((page - 1) * PNL_PAGE, page * PNL_PAGE);
    }
    function renderPnlTrades(p) {
        const rows = pnlPaginate('details', p.trades || []), tb = $('pnl-trades').querySelector('tbody');
        if (!rows.length) { tb.innerHTML = '<tr><td colspan="9" class="empty sans">Nenhum trade fechado no período.</td></tr>'; return; }
        tb.innerHTML = rows.map(t => `<tr>
            <td><small>${fmt.dateTime(t.exit_at)}</small></td>
            <td class="sans"><b>${esc(t.symbol)}</b></td>
            <td class="sans"><span class="side ${t.side === 'LONG' ? 'l' : 's'}">${esc(t.side)}</span></td>
            <td class="r">${fmt.price(t.qty)}</td><td class="r">${fmt.price(t.entry_price)}</td><td class="r">${fmt.price(t.exit_price)}</td>
            <td class="r ${cls(t.pnl_net)}">${fmt.money(t.pnl_net, { signed: true })}${brlTag(t.pnl_net)}</td>
            <td class="r dim">${fmt.money(-(t.fees || 0), { signed: true })}</td>
            <td class="sans"><small title="${esc(t.close_reason)}">${esc(t.close_reason || '—')}</small></td></tr>`).join('');
    }
    function renderPnlSymbols(p) {
        const rows = [...(p.by_symbol || [])].sort((a, b) => (a.net - b.net) * pnlState.symDir), tb = $('pnl-symbols').querySelector('tbody');
        if (!rows.length) { tb.innerHTML = '<tr><td colspan="6" class="empty sans">Nenhum trade fechado no período.</td></tr>'; return; }
        const max = Math.max(0.01, ...rows.map(r => Math.abs(r.net)));
        tb.innerHTML = rows.map(r => `<tr><td class="sans"><b>${esc(r.symbol)}</b></td><td class="r">${r.trades}</td><td class="r">${fmt.pct(r.win_rate, 0, false)}</td>
            <td class="r ${cls(r.net)}"><span class="rbar"><i class="${r.net >= 0 ? 'pos' : 'neg'}" style="width:${(Math.abs(r.net) / max * 50).toFixed(1)}%"></i></span>${fmt.money(r.net, { signed: true })}${brlTag(r.net)}</td>
            <td class="r dim">${fmt.money(-(r.fees || 0), { signed: true })}</td><td class="r dim">${fmt.money(r.funding, { signed: true })}</td></tr>`).join('');
    }
    const INCOME_LABEL = { REALIZED_PNL: 'P&L realizado', COMMISSION: 'Comissão', FUNDING_FEE: 'Funding', TRANSFER: 'Transferência' };
    function renderPnlIncome(p) {
        const tb = $('pnl-income').querySelector('tbody'), tot = p.income_totals || {};
        $('pnl-income-totals').innerHTML = p.income_ok ? Object.keys(INCOME_LABEL).map(k => `<span>${INCOME_LABEL[k]} <b class="${cls(tot[k])}">${fmt.money(tot[k] || 0, { signed: true })}</b></span>`).join('') : `<span class="warn">Income da ${esc(exchangeName())} indisponível no momento; o restante da aba usa o banco local.</span>`;
        const rows = pnlPaginate('income', p.income || []);
        if (!rows.length) { tb.innerHTML = `<tr><td colspan="4" class="empty sans">${p.income_ok ? 'Nenhum lançamento no período.' : 'Sem dados.'}</td></tr>`; return; }
        tb.innerHTML = rows.map(i => `<tr><td><small>${fmt.dateTime(i.time)}</small></td><td class="sans">${esc(INCOME_LABEL[i.type] || i.type)}</td><td class="sans">${esc(i.symbol || '—')}</td><td class="r ${cls(i.amount)}">${fmt.money(i.amount, { signed: true })}</td></tr>`).join('');
    }
    function initPnl() {
        $$('#pnl-ranges .chip').forEach(b => b.addEventListener('click', () => {
            const custom = b.dataset.days === 'custom'; $('pnl-custom').hidden = !custom;
            pnlState.days = custom ? 'custom' : Number(b.dataset.days); pnlState.page = {}; pnlState.month = null;
            if (!custom || (pnlState.from && pnlState.to)) fetchPnl();
        }));
        $('pnl-custom').addEventListener('submit', (e) => { e.preventDefault(); pnlState.from = $('pnl-from').value; pnlState.to = $('pnl-to').value; pnlState.page = {}; pnlState.month = null; fetchPnl(); });
        $$('#pnl-tabs .chip').forEach(b => b.addEventListener('click', () => {
            pnlState.tab = b.dataset.tab; pnlState.page = {}; $$('.pnl-tab').forEach(t => { t.hidden = t.dataset.tab !== pnlState.tab; }); Object.values(charts).forEach(c => c.resize());
            if (state.pnl) { renderPnlTrades(state.pnl); renderPnlIncome(state.pnl); }
        }));
        const shiftMonth = (n) => { const [y, m] = pnlState.month.split('-').map(Number); pnlState.month = dayStr(new Date(Date.UTC(y, m - 1 + n, 1))).slice(0, 7); renderPnlCalendar(state.pnl); };
        $('pnl-cal-prev').addEventListener('click', () => shiftMonth(-1)); $('pnl-cal-next').addEventListener('click', () => shiftMonth(1));
        [['details', renderPnlTrades], ['income', renderPnlIncome]].forEach(([tab, render]) => {
            $(`pnl-${tab}-prev`).addEventListener('click', () => { pnlState.page[tab]--; render(state.pnl); });
            $(`pnl-${tab}-next`).addEventListener('click', () => { pnlState.page[tab]++; render(state.pnl); });
        });
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
        renderMartingale(l.martingale, s, pos, l.sheet);
        renderMarketPairs(l);
        renderEngine(e, pos);
        renderHealth(h, s);
        renderRHist(ft.r_histogram);
        renderExcursions(ft.excursions);
        renderCalendar(ft.calendar, ft);
        if (state.hist) renderEquity(state.hist.portfolio_history, state.hist.recent_trades, ft);
        renderKpis();
    }
    function renderHistory(hs) {
        renderTrades(hs.recent_trades || []);
        renderEquity(hs.portfolio_history || [], hs.recent_trades || [], state.live && state.live.forward_test);
        renderKpis();
    }
    let lastClosed = null;
    function applyLive(l) {
        if (!l || !l.summary) return;
        state.live = l;
        fmt.tzOffset = Number(l.tz_offset_hours) || 0;
        const hadFx = isNum(state.fx && state.fx.usd_brl); if (l.fx) state.fx = l.fx;
        if (!hadFx && isNum(state.fx && state.fx.usd_brl) && state.pnl) try { renderPnl(state.pnl); } catch (err) { console.error('render do P&L falhou', err); }
        try { renderLive(l); } catch (err) { console.error('render falhou', err); }
        try { chartSync(); } catch (err) { console.error('linhas do gráfico falharam', err); }
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
    $('fx-toggle').addEventListener('click', () => {
        currency = currency === 'BRL' ? 'USD' : 'BRL';
        try { localStorage.setItem(CUR_KEY, currency); } catch (e) { /* storage bloqueado */ }
        if (state.live) renderLive(state.live);
        if (state.hist) renderHistory(state.hist);
        if (state.pnl) renderPnl(state.pnl);
    });
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

    // Gráfico 1m do par (Lightweight Charts) como no app da Bitget: velas + volume + EMAs 5/10/20/50,
    // linha da média de cada perna (qtd · P&L) e etiquetas no eixo pra alvo/degrau/stop (cores da régua).
    // Velas via /api/klines (exchange do bot) a cada 5s; as linhas acompanham o snapshot (chartSync em applyLive).
    const chart = $('chart-modal');
    const EMAS = [5, 10, 20, 50], EMA_COLORS = ['#f6c343', '#f28b3c', '#c86bf2', '#3987e5'];
    let cg = null;   // { sym, api, candles, vol, emas: [series], lines: [priceLine], timer }
    const ema = (closes, n) => { const k = 2 / (n + 1); let v = null; return closes.map(c => (v = v === null ? c : c * k + v * (1 - k))); };
    // Um gráfico (modal 1m e aba Mercado usam o mesmo): velas + volume + EMAs; `scale` guarda os níveis das pernas
    // pra escala incluí-los (linhas de preço não entram no autoscale por padrão).
    function buildChart(box) {
        const api = LightweightCharts.createChart(box, {
            autoSize: true,
            layout: { background: { type: 'solid', color: cssv('--bg-card') }, textColor: cssv('--text-dim'), fontSize: 11 },
            grid: { vertLines: { color: cssv('--grid') }, horzLines: { color: cssv('--grid') } },
            rightPriceScale: { borderColor: cssv('--border-strong'), scaleMargins: { top: 0.05, bottom: 0.22 } },
            timeScale: { borderColor: cssv('--border-strong'), timeVisible: true, secondsVisible: false },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        });
        const g = { api, lines: [], scale: [], loaded: false };
        g.candles = api.addCandlestickSeries({ upColor: cssv('--good'), downColor: cssv('--bad'), borderVisible: false, wickUpColor: cssv('--good'), wickDownColor: cssv('--bad'), priceFormat: { type: 'price', precision: 5, minMove: 0.00001 },
            autoscaleInfoProvider: (orig) => { const r = orig(); if (!r || !r.priceRange || !g.scale.length) return r;
                return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...g.scale), maxValue: Math.max(r.priceRange.maxValue, ...g.scale) } }; } });
        g.vol = api.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false });
        api.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
        g.emas = EMAS.map((n, i) => api.addLineSeries({ color: EMA_COLORS[i], lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false }));
        return g;
    }
    // casas do eixo de preço pelo nível de preço (ADA 5, BNB 2, BTC 1) — como o fmt.price
    const priceDecimals = (p) => p >= 1000 ? 1 : p >= 10 ? 2 : p >= 1 ? 3 : p >= 0.01 ? 5 : 7;
    function chartFill(g, candles, firstView) {
        const last = candles.length ? candles[candles.length - 1].c : 1, dec = priceDecimals(last);
        g.candles.applyOptions({ priceFormat: { type: 'price', precision: dec, minMove: 10 ** -dec } });
        g.candles.setData(candles.map(k => ({ time: k.t, open: k.o, high: k.h, low: k.l, close: k.c })));
        g.vol.setData(candles.map(k => ({ time: k.t, value: k.v, color: k.c >= k.o ? 'rgba(47,181,90,.45)' : 'rgba(224,82,79,.45)' })));
        const closes = candles.map(k => k.c);
        g.emas.forEach((s, i) => s.setData(ema(closes, EMAS[i]).map((v, j) => ({ time: candles[j].t, value: v })).slice(EMAS[i])));
        g.loaded = true;
        if (firstView) g.api.timeScale().setVisibleLogicalRange({ from: candles.length - 90, to: candles.length + 6 });
    }
    // Linhas das pernas do par: média (qtd · P&L), alvo, degrau seguinte e stop (cores da régua); o empate fica só na régua.
    function chartLevels(g, sym) {
        if (!g || !g.loaded) return;
        g.lines.forEach(l => g.candles.removePriceLine(l)); g.lines = []; g.scale = [];
        const legs = ((state.live && state.live.martingale && state.live.martingale.legs) || []).filter(l => l.symbol === sym && isNum(l.avg_price));
        const positions = (state.live && state.live.positions) || [];
        const LS = LightweightCharts.LineStyle, add = (price, color, title, style, width, offscale) => {
            if (!isNum(price)) return;
            g.lines.push(g.candles.createPriceLine({ price, color, title, lineWidth: width || 1, lineStyle: style, axisLabelVisible: true }));
            if (!offscale) g.scale.push(price);
        };
        for (const l of legs) {
            const long = l.side === 'LONG', pos = positions.find(p => p.symbol === l.symbol && p.side === l.side) || {};
            const pnl = isNum(l.unrealized_pnl_usd) ? ` ${l.unrealized_pnl_usd >= 0 ? '+' : '−'}${Math.abs(l.unrealized_pnl_usd).toFixed(4)} USDT${isNum(pos.unrealized_pnl_percent) ? ` (${pos.unrealized_pnl_percent >= 0 ? '+' : '−'}${Math.abs(pos.unrealized_pnl_percent).toFixed(2)}%)` : ''}` : '';
            add(l.avg_price, long ? cssv('--good') : cssv('--bad'), `${long ? '' : '−'}${fmt.price(l.quantity)}${pnl}`, LS.Solid, 2);
            add(l.tp_price, cssv('--good'), `Alvo ${l.side}`, LS.Dashed);
            add(l.next_add_price, cssv('--warn'), `Degrau ${l.level + 1} ${l.side}`, LS.Dashed);
            add(l.stop_price, cssv('--bad'), `Stop ${l.side}`, LS.LargeDashed, 1, true);   // stop pode estar longe: não estica a escala
        }
        g.candles.applyOptions({ autoscaleInfoProvider: g.candles.options().autoscaleInfoProvider });   // reavalia a escala com os níveis novos
    }
    async function chartLoad() {
        if (!cg) return;
        try {
            const r = await fetch(`/api/klines?symbol=${encodeURIComponent(cg.sym)}&interval=1m&limit=300`, { credentials: 'include' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const { candles } = await r.json(); if (!cg || !candles) return;
            const first = !cg.loaded; chartFill(cg, candles, first);
            if (first) chartSync();
        } catch (err) { console.error('klines falharam', err); }
    }
    // snapshot novo: as linhas do modal e da aba Mercado acompanham
    function chartSync() { if (cg) chartLevels(cg, cg.sym); if (mk.g) chartLevels(mk.g, mk.sym); }
    function openChart(sym) {
        closeChart(true);
        $('chart-title').textContent = sym;
        lastFocus = document.activeElement; chart.hidden = false; $('chart-close').focus();
        if (!window.LightweightCharts) { $('chart-box').textContent = 'Biblioteca do gráfico não carregou.'; return; }
        cg = buildChart($('chart-box')); cg.sym = sym; cg.timer = setInterval(chartLoad, 5000);
        chartLoad();
    }
    function closeChart(keepOpen) {
        if (cg) { clearInterval(cg.timer); cg.api.remove(); cg = null; $('chart-box').textContent = ''; }
        if (keepOpen) return;
        chart.hidden = true; if (lastFocus) lastFocus.focus();
    }
    document.addEventListener('click', (e) => { const b = e.target.closest('[data-chart]'); if (b) openChart(b.dataset.chart); });
    $('chart-close').addEventListener('click', closeChart);
    chart.addEventListener('click', (e) => { if (e.target === chart) closeChart(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !chart.hidden) closeChart(); });
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

    // ───────── Mercado (API pública da Bitget via /api/market/<par>) ─────────
    // Par = chips com os pares ativos do martingale (mesma fonte do anel) + BTC como referência; padrão = 1º par ativo.
    // Velas em lightweight-charts (Chart.js não tem candlestick); funding e long/short em Chart.js. Só busca com a página visível.
    const mk = { tf: '1h', sym: null, pairs: [], g: null, timer: null, data: null, shown: null };   // shown = par+tf já desenhado (1ª vista)
    function renderMarketPairs(live) {
        const active = Object.keys((live && live.martingale && live.martingale.pairs) || {});
        const pairs = [...active, 'BTCUSDT'].filter((s, i, a) => a.indexOf(s) === i);
        const changed = pairs.join() !== mk.pairs.join(); mk.pairs = pairs;
        if (!mk.sym || !pairs.includes(mk.sym)) { mk.sym = pairs[0]; if (mk.timer) fetchMarket(); }   // par ativo mudou: segue o novo
        if (!changed && $('mk-pairs').children.length) return;
        $('mk-pairs').innerHTML = pairs.map(s => `<button class="chip${s === mk.sym ? ' on' : ''}" role="tab" data-sym="${esc(s)}" aria-selected="${s === mk.sym}">${esc(s.replace(/USDT$/, ''))}${active.includes(s) ? '' : ' <small>ref.</small>'}</button>`).join('');
    }
    $('mk-pairs').addEventListener('click', (e) => {
        const b = e.target.closest('[data-sym]'); if (!b || b.dataset.sym === mk.sym) return;
        mk.sym = b.dataset.sym; $$('#mk-pairs .chip').forEach(c => { const on = c === b; c.classList.toggle('on', on); c.setAttribute('aria-selected', String(on)); });
        fetchMarket();
    });
    const big = (v) => !isNum(v) ? '—' : v >= 1e9 ? (v / 1e9).toFixed(2).replace('.', ',') + ' bi' : v >= 1e6 ? (v / 1e6).toFixed(1).replace('.', ',') + ' mi' : fmt.num(v, 0);
    function renderMarket(d) {
        mk.data = d; const t = d.ticker || {}, f = d.funding || {}, ls = d.long_short, p = palette();
        if (d.symbol !== mk.sym) return;   // resposta atrasada de outro par
        $('mk-error').hidden = true;
        const base = String(d.symbol).replace(/USDT$/, '');
        $('mk-h').textContent = `${d.symbol} perpétuo`; $('mk-last-l').textContent = `${d.symbol} · último`;
        $('mk-last').textContent = fmt.price(t.last); $('mk-last').className = 'kpi-v ' + cls(t.change_24h_pct);
        $('mk-last-s').textContent = `${fmt.pct(t.change_24h_pct)} em 24h · vol ${big(t.volume_usdt_24h)} USDT`;
        $('mk-range').innerHTML = `<span class="neg">mín ${esc(fmt.price(t.low_24h))}</span><span class="pos">máx ${esc(fmt.price(t.high_24h))}</span>`;
        $('mk-mark').textContent = fmt.price(t.mark);
        $('mk-mark-s').textContent = `índice ${fmt.price(t.index)} · prêmio ${fmt.pct(t.basis_pct, 3)}`;
        $('mk-funding').textContent = fmt.pct(f.rate_pct, 4); $('mk-funding').className = 'kpi-v ' + cls(f.rate_pct);
        const left = f.next_at ? (new Date(f.next_at).getTime() - Date.now()) / 1000 : null;
        $('mk-funding-s').textContent = `${sgn(f.rate_pct) > 0 ? 'long paga short' : sgn(f.rate_pct) < 0 ? 'short paga long' : 'neutro'} · próximo em ${isNum(left) && left > 0 ? fmt.clock(left) : '—'} (a cada ${f.interval_hours || 8}h)`;
        const fb = $('mk-funding-bar'); fb.firstElementChild.style.width = `${Math.min(100, Math.abs(f.rate_pct || 0) / 0.1 * 100)}%`; fb.className = 'kpi-bar ' + (sgn(f.rate_pct) < 0 ? 'fund-neg' : 'fund-pos');
        $('mk-oi').textContent = isNum(t.open_interest_btc) ? `${big(t.open_interest_btc)} ${base}` : '—';
        $('mk-oi-s').textContent = isNum(t.open_interest_usdt) ? `≈ ${big(t.open_interest_usdt)} USDT em aberto` : 'sem dado';
        if (ls) {
            $('mk-ls').textContent = `${fmt.pct(ls.accounts_long_pct, 1, false)} long`;
            $('mk-ls-s').textContent = `razão ${fmt.num(ls.accounts_ratio, 2)} · posições ${fmt.pct(ls.positions_long_pct, 1, false)} long (razão ${fmt.num(ls.positions_ratio, 2)})`;
            $('mk-ls-bar').firstElementChild.style.width = `${ls.accounts_long_pct || 0}%`;
        } else { $('mk-ls').textContent = '—'; $('mk-ls-s').textContent = 'sem dado'; }
        $('mk-sub').textContent = `Bitget USDT-M · ${d.interval} · ${(d.candles || []).length} velas · atualizado ${fmt.time(d.fetched_at)}`;
        if (!mk.g && window.LightweightCharts) mk.g = buildChart($('mk-chart'));
        if (mk.g) { const key = `${d.symbol}/${d.interval}`; chartFill(mk.g, d.candles || [], mk.shown !== key); mk.shown = key; chartLevels(mk.g, mk.sym); }
        const fh = f.history || [], fl = fh.map(h => fmt.dateTime(h.t)), fd = fh.map(h => h.rate_pct);
        $('mk-funding-sub').textContent = fh.length ? `${fh.length} liquidações · média ${fmt.pct(sum(fd) / fd.length, 4)}` : 'sem dado';
        const tt = baseTooltip(p);
        if (charts.mkFund) { charts.mkFund.data.labels = fl; charts.mkFund.data.datasets[0].data = fd; charts.mkFund.data.datasets[0].backgroundColor = fd.map(v => v < 0 ? p.bad : p.good); charts.mkFund.update('none'); }
        else if ($('mk-funding-chart') && typeof Chart !== 'undefined') charts.mkFund = new Chart($('mk-funding-chart'), { type: 'bar', data: { labels: fl, datasets: [{ data: fd, backgroundColor: fd.map(v => v < 0 ? p.bad : p.good), borderRadius: 2, maxBarThickness: 14 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { ...tt, callbacks: { label: (it) => fmt.pct(it.parsed.y, 4) } } },
                scales: { x: { ticks: { color: p.mut, font: tickFont, maxTicksLimit: 6 }, grid: { display: false } }, y: { ticks: { color: p.mut, font: tickFont, maxTicksLimit: 5, callback: v => fmt.pct(v, 3) }, grid: { color: p.grid } } } } });
        const lh = (ls && ls.history) || [], ll = lh.map(h => fmt.dateTime(h.t)), ld = lh.map(h => h.accounts_long_pct);
        $('mk-ls-sub').textContent = lh.length ? `últimas ${lh.length} h · agora ${fmt.pct(ld[ld.length - 1], 1, false)} das contas long` : 'sem dado';
        if (charts.mkLs) { charts.mkLs.data.labels = ll; charts.mkLs.data.datasets[0].data = ld; charts.mkLs.update('none'); }
        else if ($('mk-ls-chart') && typeof Chart !== 'undefined') charts.mkLs = new Chart($('mk-ls-chart'), { type: 'line', data: { labels: ll, datasets: [{ data: ld, borderColor: p.ac, backgroundColor: hexa(p.ac, 0.12), fill: true, borderWidth: 2, pointRadius: 0, tension: 0.3 }] },
            options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { ...tt, callbacks: { label: (it) => `${fmt.pct(it.parsed.y, 1, false)} long` } } },
                scales: { x: { ticks: { color: p.mut, font: tickFont, maxTicksLimit: 6 }, grid: { display: false } }, y: { ticks: { color: p.mut, font: tickFont, maxTicksLimit: 5, callback: v => fmt.pct(v, 0, false) }, grid: { color: p.grid } } } } });
    }
    async function fetchMarket() {
        try {
            if (!mk.sym) renderMarketPairs(state.live);
            const r = await fetch(`/api/market/${encodeURIComponent(mk.sym)}?interval=${mk.tf}`, { credentials: 'include' });
            const d = await r.json(); if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
            renderMarket(d);
        } catch (err) { console.warn('mercado BTC falhou', err); const e = $('mk-error'); e.textContent = 'Bitget não respondeu: ' + err.message; e.hidden = false; }
    }
    function marketVisible(on) { clearInterval(mk.timer); mk.timer = null; if (on) { fetchMarket(); mk.timer = setInterval(fetchMarket, 15000); } }
    $$('#mk-tf .chip').forEach(b => b.addEventListener('click', () => { mk.tf = b.dataset.tf; fetchMarket(); }));

    // ───────── Relatórios › Histórico de fills (/api/fills: execuções da conta na exchange) ─────────
    const fl = { page: 1, symbol: '', side: '', from: '', to: '', loaded: false };
    const flQuery = (extra) => { const q = new URLSearchParams(); if (fl.symbol) q.set('symbol', fl.symbol); if (fl.side) q.set('side', fl.side); if (fl.from) q.set('from', fl.from); if (fl.to) q.set('to', fl.to); Object.entries(extra || {}).forEach(([k, v]) => q.set(k, v)); return q.toString(); };
    async function fetchFills() {
        const tb = $('fills-table').querySelector('tbody'), err = $('fills-error');
        try {
            const r = await fetch(`/api/fills?${flQuery({ page: fl.page, per_page: 50 })}`, { credentials: 'include' });
            const d = await r.json(); if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
            err.hidden = true; fl.loaded = true;
            const sel = $('fl-symbol'), cur = sel.value;
            sel.innerHTML = '<option value="">todos</option>' + (d.symbols || []).map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join(''); sel.value = cur;
            $('fl-from').value = d.from; $('fl-to').value = d.to;
            $('fills-stats').innerHTML = `<span><b>${d.total}</b> fill(s) de ${fmt.day(d.from)} a ${fmt.day(d.to)}</span><span>fees <b class="${cls(d.fee_total)}">${esc(fmt.money(d.fee_total, { signed: true }))}</b></span><span>P&L realizado <b class="${cls(d.profit_total)}">${esc(fmt.money(d.profit_total, { signed: true }))}</b></span><span><b>${d.matched}</b> batem com trades do bot</span>`;
            $('fills-csv').href = `/api/fills?${flQuery({ format: 'csv' })}`;
            tb.innerHTML = (d.fills || []).map(f => `<tr>
                <td>${esc(fmt.dateTime(new Date(f.time).toISOString()))}</td><td class="sans"><b>${esc(f.symbol)}</b></td>
                <td><span class="side ${f.position_side === 'LONG' ? 'l' : 's'}">${esc(f.position_side)}</span></td>
                <td class="sans act ${esc(f.action)}">${f.action === 'open' ? 'abre' : 'fecha'} <small>(${esc(f.side)})</small></td>
                <td class="r">${esc(fmt.price(f.price))}</td><td class="r">${esc(fmt.price(f.qty))}</td><td class="r">${esc(fmt.money(f.notional))}</td>
                <td class="r ${cls(f.fee)}">${esc(fmt.money(f.fee, { signed: true }))}${f.fee_coin && f.fee_coin !== 'USDT' ? ` <small>${esc(f.fee_coin)}</small>` : ''}</td>
                <td class="r ${cls(f.profit)}">${f.action === 'close' ? esc(fmt.money(f.profit, { signed: true })) : '—'}</td>
                <td class="sans">${esc(f.role || '—')}</td>
                <td class="sans">${f.bot_trade_id != null ? `<span class="bot">bot #${esc(f.bot_trade_id)}</span>` : '<span class="nobot">manual / fora do bot</span>'}</td>
            </tr>`).join('') || '<tr><td colspan="11" class="empty sans">Nenhum fill no período.</td></tr>';
            $('fills-pager').hidden = d.pages <= 1; $('fills-page').textContent = `${d.page} / ${d.pages}`;
            $('fills-prev').disabled = d.page <= 1; $('fills-next').disabled = d.page >= d.pages;
        } catch (e) { err.textContent = 'Fills indisponíveis: ' + e.message; err.hidden = false; tb.innerHTML = '<tr><td colspan="11" class="empty sans">—</td></tr>'; }
    }
    $('fills-filters').addEventListener('submit', (e) => { e.preventDefault(); fl.symbol = $('fl-symbol').value; fl.side = $('fl-side').value; fl.from = $('fl-from').value; fl.to = $('fl-to').value; fl.page = 1; fetchFills(); });
    $('fills-prev').addEventListener('click', () => { fl.page -= 1; fetchFills(); });
    $('fills-next').addEventListener('click', () => { fl.page += 1; fetchFills(); });
    function fillsVisible(on) { if (on && !fl.loaded) fetchFills(); }

    // ───────── Shell: KPIs com sparkline, páginas do menu, menu off-canvas, tema ─────────
    function sparkline(id, values, color) {
        const el = $(id); if (!el || typeof Chart === 'undefined') return;
        const data = values.map((y, x) => ({ x, y }));
        if (charts[id]) { const d = charts[id].data.datasets[0]; d.data = data; d.borderColor = color; d.backgroundColor = hexa(color, 0.18); charts[id].update('none'); return; }
        charts[id] = new Chart(el, {
            type: 'line', data: { datasets: [{ data, borderColor: color, backgroundColor: hexa(color, 0.18), fill: true, borderWidth: 2, pointRadius: 0, tension: 0.4 }] },
            options: { responsive: true, maintainAspectRatio: false, parsing: false, animation: false, events: [],
                plugins: { legend: { display: false }, tooltip: { enabled: false } }, scales: { x: { type: 'linear', display: false }, y: { display: false } } },
        });
    }
    // Conta de perpétuos: equity (curva do portfolio_history), margem usada × disponível, não realizado por perna,
    // realizado do dia UTC (net por dia, 30d) e funding acumulado (com comissões) sobre o acumulado por dia.
    function renderKpis() {
        const s = state.live && state.live.summary; if (!s) return;
        const p = palette(), h = state.hist || {}, days = (h.daily_history || []).slice(-30), pos = (state.live.positions || []);
        const eq = s.last_balance, today = s.daily_pnl;
        $('kpi-equity').innerHTML = esc(fmt.money(eq)) + brlTag(eq);
        $('kpi-equity-s').textContent = `wallet ${fmt.money(isNum(eq) && isNum(s.unrealized_pnl) ? eq - s.unrealized_pnl : null)} · capital ${fmt.money(s.initial_capital)} · P&L total ${fmt.money(s.total_pnl, { signed: true })} (${fmt.pct(s.roi_percent)})`;
        sparkline('kpi-equity-spark', (h.portfolio_history || []).map(q => q.equity ?? q.balance).filter(isNum), p.ac);
        const used = isNum(s.margin_used) ? s.margin_used : (pos.length ? sum(pos.map(q => q.margin_usd).filter(isNum)) : null);
        const pct = isNum(used) && isNum(eq) && eq > 0 ? used / eq * 100 : null;
        $('kpi-margin').innerHTML = esc(fmt.money(used)) + brlTag(used);
        $('kpi-margin-s').textContent = `${isNum(pct) ? fmt.pct(pct, 1, false) + ' da equity · ' : ''}disponível ${fmt.money(isNum(s.available_balance) ? s.available_balance : (isNum(used) && isNum(eq) ? eq - used : null))}`;
        const bar = $('kpi-margin-bar'); bar.firstElementChild.style.width = `${Math.min(100, Math.max(0, pct || 0))}%`; bar.setAttribute('aria-valuenow', String(Math.round(pct || 0))); bar.classList.toggle('hot', isNum(pct) && pct >= 70);
        const legs = [...pos].sort((a, b) => Math.abs(b.unrealized_pnl_usd || 0) - Math.abs(a.unrealized_pnl_usd || 0));
        $('kpi-unreal').innerHTML = esc(fmt.money(s.unrealized_pnl, { signed: true })) + brlTag(s.unrealized_pnl); $('kpi-unreal').className = 'kpi-v ' + cls(s.unrealized_pnl);
        $('kpi-unreal-s').textContent = pos.length ? `${pos.length} perna(s) · notional ${fmt.money(sum(pos.map(q => q.notional).filter(isNum)))}` : 'nenhuma posição aberta';
        $('kpi-unreal-legs').innerHTML = legs.slice(0, 6).map(q => `<span class="${cls(q.unrealized_pnl_usd)}">${esc(String(q.symbol || '').replace(/USDT$/, ''))} ${esc(q.side)} ${esc(fmt.money(q.unrealized_pnl_usd, { signed: true }))}${isNum(q.roe_percent) ? ` (${esc(fmt.pct(q.roe_percent, 1))})` : ''}</span>`).join('');
        $('kpi-pnl').innerHTML = esc(fmt.money(today, { signed: true })) + brlTag(today); $('kpi-pnl').className = 'kpi-v ' + cls(today);
        const bd = s.daily_pnl_breakdown || {};
        $('kpi-pnl-s').textContent = `trades ${fmt.money(bd.trades, { signed: true })} · funding ${fmt.money(bd.funding, { signed: true })} · taxas ${fmt.money(bd.commission, { signed: true })} · ${s.closed_trades_today ?? 0} fechado(s)`;
        sparkline('kpi-pnl-spark', days.map(d => d.net).filter(isNum), sgn(today) < 0 ? p.bad : p.good);
        const fund = s.funding_fee_total;
        $('kpi-funding').innerHTML = esc(fmt.money(fund, { signed: true })) + brlTag(fund); $('kpi-funding').className = 'kpi-v ' + cls(fund);
        $('kpi-funding-s').textContent = `${sgn(fund) < 0 ? 'pago' : sgn(fund) > 0 ? 'recebido' : 'líquido'} desde o 1º trade · taxas ${fmt.money(s.commission_total, { signed: true })} · hoje ${fmt.money(s.funding_fee_today, { signed: true })}`;
        sparkline('kpi-funding-spark', days.map(d => d.cumulative).filter(isNum), p.warn);
    }
    // Linha de perpétuo de uma posição/perna: alavancagem, notional, margem, marca, liquidação, ROE (o que a exchange deu).
    function futLine(p) {
        if (!p) return '';
        const it = [
            isNum(p.leverage) ? `<span>alav. <b>${p.leverage}×</b></span>` : '',
            isNum(p.notional) ? `<span>notional <b>${esc(fmt.money(p.notional))}</b></span>` : '',
            isNum(p.margin_usd) ? `<span>margem <b>${esc(fmt.money(p.margin_usd))}</b></span>` : '',
            isNum(p.mark_price) ? `<span>marca <b>${esc(fmt.price(p.mark_price))}</b></span>` : '',
            isNum(p.liquidation_price) ? `<span class="liq">liquidação <b>${esc(fmt.price(p.liquidation_price))}</b>${isNum(p.mark_price) && p.mark_price > 0 ? ` <small>(${esc(fmt.pct(Math.abs(p.liquidation_price - p.mark_price) / p.mark_price * 100, 1, false))})</small>` : ''}</span>` : '',
            isNum(p.roe_percent) ? `<span>ROE <b class="${cls(p.roe_percent)}">${esc(fmt.pct(p.roe_percent, 1))}</b></span>` : '',
        ].filter(Boolean);
        return it.length ? `<div class="fut">${it.join('')}</div>` : '';
    }

    const PAGES = $$('.page').map(p => p.dataset.page);
    // hash = "pagina" ou "pagina/id-do-card" (subitem do menu: mostra a página e rola até o card, abrindo-o)
    function showPage(hash) {
        const [raw, target] = String(hash || '').split('/');
        const name = PAGES.includes(raw) ? raw : PAGES[0];
        $$('.page').forEach(p => { p.hidden = p.dataset.page !== name; });
        $$('a[data-page]').forEach(a => {
            // no menu lateral o item ativo é o que casa página E card (Dashboard só sem card); na tabbar basta a página
            const on = a.dataset.page === name && (!a.closest('.nav') || (a.dataset.target || '') === (target || ''));
            a.classList.toggle('on', on); if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
        });
        $$('.nav-group').forEach(g => g.classList.toggle('has-on', !!g.querySelector('a.on')));
        const el = target && $(target);
        if (el && !el.hidden) { if (el.tagName === 'DETAILS') el.open = true; }
        else { const first = $$(`.page[data-page="${name}"] details.card`).find(d => !d.hidden); if (first) first.open = true; }   // página só com cards recolhíveis: abre o primeiro
        pageEmpties();
        Object.values(charts).forEach(c => c.resize());   // canvas em página oculta nasce com 0px
        closeSidebar();
        marketVisible(name === 'market');
        fillsVisible(name === 'fills');
        if (el && !el.hidden) el.scrollIntoView({ block: 'start' });
    }
    // página sem nenhum card visível (ex.: fora do martingale) mostra um aviso em vez de ficar em branco
    function pageEmpties() { $$('.page').forEach(p => { const e = p.querySelector('.page-empty'); if (e) e.hidden = Array.from(p.children).some(c => c !== e && !c.hidden); }); }
    // Desktop: menu recolhido (só ícones); lembrado em localStorage. No celular o menu é off-canvas e isto não se aplica.
    const NAV_KEY = 'tradingbot-nav';
    function setCompact(on) { document.body.classList.toggle('nav-compact', on); $('sidebar-collapse').setAttribute('aria-pressed', String(on)); $('sidebar-collapse').title = $('sidebar-collapse').ariaLabel = on ? 'Expandir menu' : 'Recolher menu'; Object.values(charts).forEach(c => c.resize()); }
    try { setCompact(localStorage.getItem(NAV_KEY) === 'compact'); } catch (e) { /* storage bloqueado */ }
    $('sidebar-collapse').addEventListener('click', () => { const on = !document.body.classList.contains('nav-compact'); setCompact(on); try { localStorage.setItem(NAV_KEY, on ? 'compact' : 'full'); } catch (e) { /* storage bloqueado */ } });
    function closeSidebar() { document.body.classList.remove('nav-open'); $('sidebar-backdrop').hidden = true; $('sidebar-toggle').setAttribute('aria-expanded', 'false'); }
    $('sidebar-toggle').addEventListener('click', () => { const open = !document.body.classList.contains('nav-open'); document.body.classList.toggle('nav-open', open); $('sidebar-backdrop').hidden = !open; $('sidebar-toggle').setAttribute('aria-expanded', String(open)); });
    $('sidebar-backdrop').addEventListener('click', closeSidebar);
    window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
    showPage(location.hash.slice(1));
    // Grupos do menu: accordion (um aberto por vez), lembrado em localStorage; o grupo do item ativo abre no boot.
    const GROUP_KEY = 'tradingbot-nav-group';
    function openGroup(key) { $$('.nav-group').forEach(g => { const on = g.dataset.group === key; g.classList.toggle('open', on); g.querySelector('.nav-toggle').setAttribute('aria-expanded', String(on)); }); }
    $$('.nav-toggle').forEach(b => b.addEventListener('click', () => {
        const g = b.parentElement, key = g.classList.contains('open') ? '' : g.dataset.group; openGroup(key);
        try { localStorage.setItem(GROUP_KEY, key); } catch (e) { /* storage bloqueado */ }
    }));
    { let key = null; try { key = localStorage.getItem(GROUP_KEY); } catch (e) { /* storage bloqueado */ }
      const active = document.querySelector('.nav-group.has-on'); openGroup(key !== null ? key : (active ? active.dataset.group : 'trading')); }

    // Tema: grava em localStorage (o <head> aplica antes do primeiro paint). Os gráficos leem as cores ao nascer: destrói e redesenha.
    const THEME_KEY = 'tradingbot-theme';
    $('theme-toggle').addEventListener('click', () => {
        const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* storage bloqueado */ }
        Object.values(charts).forEach(c => c.destroy()); Object.keys(charts).forEach(k => { delete charts[k]; });
        if (mk.g) { mk.g.api.remove(); mk.g = null; mk.shown = null; if (mk.data) renderMarket(mk.data); }
        if (state.live) renderLive(state.live);
        if (state.hist) renderHistory(state.hist);
        if (state.pnl) renderPnl(state.pnl);
    });

    // ───────── Boot ─────────
    initChips(); initTrades(); initPnl();
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
