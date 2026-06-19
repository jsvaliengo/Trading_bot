"""
Coleta de dados do bot para o dashboard. Lê estruturas in-memory do
TradingBot respeitando os locks existentes (_positions_lock, _state_io_lock).

Princípio: não fazer chamadas pesadas à Binance aqui — apenas LER o que o
bot já cacheou. O loop principal do bot é quem alimenta esses caches. Se
um dado não estiver disponível (ex: posições nunca foram listadas ainda),
retornamos campos vazios em vez de bloquear.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.config import config


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(dt: Any) -> Optional[str]:
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        return dt
    return None


def collect_snapshot(bot) -> Dict[str, Any]:
    """Snapshot completo para a primeira carga e fallback de polling."""
    return {
        "summary": collect_summary(bot),
        "positions": collect_positions(bot),
        "recent_trades": collect_recent_trades(bot, limit=20),
        "regime": collect_regime(bot),
        "portfolio_history": collect_portfolio_history(bot, limit=200),
        "daily_history": collect_daily_history(bot),
        "pnl_analysis": collect_pnl_analysis(bot),
        "pnl_by_symbol": collect_pnl_by_symbol(bot),
        "mfe_distribution": collect_mfe_distribution(bot),
        "server_time": datetime.utcnow().isoformat() + "Z",
    }


def collect_pnl_by_symbol(bot) -> List[Dict[str, Any]]:
    """P&L líquido por moeda (trades fechados) — viz "P&L por moeda"."""
    store = getattr(bot, "trade_store", None)
    if store is None:
        return []
    try:
        return store.pnl_by_symbol()
    except Exception:
        return []


def collect_mfe_distribution(bot) -> Dict[str, Any]:
    """Distribuição do MFE + o gatilho de ativação do trailing (linha de
    referência na viz)."""
    store = getattr(bot, "trade_store", None)
    base = {"labels": [], "counts": [], "edges": [], "avg": 0.0, "n": 0}
    if store is not None:
        try:
            base = store.mfe_distribution()
        except Exception:
            pass
    base["activation_pct"] = _safe_float(
        getattr(config, "TRAILING_ACTIVATION_MIN_PERCENT", 0.0)
    )
    return base


def collect_pnl_analysis(bot) -> Dict[str, Any]:
    """Agregados de P&L (Total Profit/Loss, win rate, dias, médias, volume) do
    TradeStore durável — base do painel "Análise P&L". Vazio sem store."""
    store = getattr(bot, "trade_store", None)
    if store is None:
        return {}
    try:
        return store.pnl_analysis()
    except Exception:
        return {}


def collect_daily_history(bot, limit: int = 90) -> List[Dict[str, Any]]:
    """Histórico de P&L por dia (UTC) — fonte durável do TradeStore.

    Cada item: day, trades, wins, losses, win_rate, net (P&L do dia), fees e
    cumulative (acumulado corrido). Base da tabela "Histórico por dia" do
    dashboard. Vazio quando não há TradeStore.
    """
    store = getattr(bot, "trade_store", None)
    if store is None:
        return []
    try:
        return store.daily_pnl_history(limit=limit)
    except Exception:
        return []


def collect_summary(bot) -> Dict[str, Any]:
    """KPIs do topo da página.

    Fonte de verdade para os valores monetários é a Binance (mesma origem
    que o /portfolio do Telegram), não os contadores internos do bot —
    os contadores estimam taxas com base no taker rate e não capturam
    funding fee + slippage real, então divergem do extrato. Bot counters
    ficam disponíveis em `bot_*` fields pra debugging.

    Saldo exibido = wallet REAL + unrealized PnL (equity total). Em testnet
    com SIMULATED_BALANCE_USD ativo, o wallet vem cappado em $130; nesse
    caso o equity é $130 + realized_dia + unrealized.
    """
    initial_capital = _safe_float(getattr(bot, "initial_capital", 0.0))
    # Contagem de fechados vem do SQLite (fonte de verdade): o contador em
    # memória pode dessincronizar dos fechamentos server-side (caso 14/06:
    # SQLite=7, contador=6). Fallback no contador em memória se não houver store.
    closed_trades = int(getattr(bot, "closed_trades_count", 0) or 0)
    _store = getattr(bot, "trade_store", None)
    if _store is not None:
        try:
            closed_trades = int(_store.count_closed_trades())
        except Exception:
            pass
    paused = bool(getattr(bot, "paused", False))
    running = bool(getattr(bot, "running", False))

    # Contadores internos do bot (fonte secundária, mostrada como bot_*)
    bot_total_pnl = _safe_float(getattr(bot, "total_pnl", 0.0))
    bot_daily_pnl = _safe_float(getattr(bot, "daily_realized_pnl", 0.0))

    # Fonte de verdade: Binance
    binance_daily_realized = 0.0
    binance_unrealized = 0.0
    binance_funding_fee = 0.0
    binance_commission = 0.0
    binance_funding_total = 0.0
    binance_commission_total = 0.0
    wallet_balance = _safe_float(getattr(bot, "last_known_balance", 0.0) or 0.0)
    exchange = getattr(bot, "exchange", None)
    if exchange is not None:
        try:
            daily = exchange.get_daily_pnl_from_binance()
            # Subtrai o baseline ancorado no reset/rollover de dia UTC para
            # que o display comece em $0 após /reset. Funding e commission
            # ficam no valor raw (são breakdowns informativos).
            baseline = _safe_float(getattr(bot, "daily_pnl_binance_baseline", 0.0))
            binance_daily_realized = _safe_float(daily.get("total")) - baseline
            # funding_fee é negativo quando paga, positivo quando recebe.
            # commission é sempre negativo (custo).
            binance_funding_fee = _safe_float(daily.get("funding_fee"))
            binance_commission = _safe_float(daily.get("commission"))
        except Exception:
            pass
        try:
            # Funding/comissão ACUMULADOS do PERÍODO ATUAL — alinhados ao P&L
            # realizado (que recomeça no reset do DB). Âncora = 1º trade do
            # trade_store; sem trades, fica em 0 (período recém-começado).
            _store = getattr(bot, "trade_store", None)
            _start_ms = _store.first_trade_time_ms() if _store is not None else None
            if _start_ms is not None:
                cum = exchange.get_cumulative_income_from_binance(start_ms=_start_ms)
                binance_funding_total = _safe_float(cum.get("funding_fee"))
                binance_commission_total = _safe_float(cum.get("commission"))
        except Exception:
            pass
        try:
            info = exchange.get_account_info()
            wallet_balance = _safe_float(info.get("wallet_balance", wallet_balance))
            binance_unrealized = _safe_float(info.get("unrealized_pnl"))
        except Exception:
            pass

    # Realizado ACUMULADO (todos os dias), do TradeStore durável — não zera na
    # virada do dia UTC. Antes o saldo/P&L usava só o realizado do DIA, então
    # voltava pro capital inicial todo dia e escondia o progresso do bot.
    # Fallback no contador interno do bot quando não há store.
    cumulative_realized = bot_total_pnl
    # P&L HOJE (card): realizado de HOJE do MESMO TradeStore durável — igual à
    # coluna "P&L DO DIA" do histórico. Antes usava o income diário da Binance
    # menos um baseline que era re-ancorado a cada restart, então o card zerava
    # no meio do dia UTC e divergia do histórico/total. Fallback no realizado
    # da Binance quando não há store.
    daily_realized = binance_daily_realized
    store = getattr(bot, "trade_store", None)
    if store is not None:
        try:
            cumulative_realized = _safe_float(store.cumulative_realized_pnl())
        except Exception:
            pass
        try:
            daily_realized = _safe_float(store.realized_pnl_today())
        except Exception:
            pass

    # P&L total = realizado ACUMULADO + não realizado das posições abertas.
    total_pnl = cumulative_realized + binance_unrealized
    # Equity = capital/wallet + acumulado + não realizado. No mainnet o wallet
    # já reflete o realizado; no testnet simulated o cap é fixo, então somamos
    # o realizado acumulado (não só o do dia) pra refletir o progresso real.
    if getattr(config, "USE_TESTNET", False) and float(
        getattr(config, "SIMULATED_BALANCE_USD", 0.0) or 0.0
    ) > 0:
        effective_balance = float(getattr(config, "SIMULATED_BALANCE_USD")) + cumulative_realized + binance_unrealized
    else:
        effective_balance = wallet_balance + binance_unrealized

    if initial_capital > 0:
        roi_percent = (total_pnl / initial_capital) * 100.0
    else:
        roi_percent = 0.0

    return {
        "initial_capital": initial_capital,
        "last_balance": effective_balance,
        "total_pnl": total_pnl,
        "daily_pnl": daily_realized,
        "unrealized_pnl": binance_unrealized,
        "funding_fee_today": binance_funding_fee,
        "commission_today": binance_commission,
        # Acumulado (todos os dias) — usado nos cards que não devem zerar diário.
        "funding_fee_total": binance_funding_total,
        "commission_total": binance_commission_total,
        # Debug: contadores internos do bot (estimados, podem divergir)
        "bot_total_pnl": bot_total_pnl,
        "bot_daily_pnl": bot_daily_pnl,
        "roi_percent": round(roi_percent, 4),
        "closed_trades": closed_trades,
        "paused": paused,
        "running": running,
        "environment": "testnet" if getattr(config, "USE_TESTNET", False) else "mainnet",
        "ai_mode": str(getattr(config, "AI_CONSULTIVE_MODE", "off") or "off"),
    }


def collect_positions(bot) -> List[Dict[str, Any]]:
    """Posições conhecidas + mark_price e P&L unrealized do cache da exchange.

    Lê known_positions (metadata estratégica) e cruza com o snapshot live
    do bot.exchange (mark_price, unrealized_pnl). O get_open_positions tem
    cache de 5s — se for hit, é gratuito; se for miss, faz uma chamada e
    aquece o cache pro monitor_positions usar logo a seguir. Em caso de
    falha de API, mark_price/unrealized_pnl voltam None.
    """
    positions: List[Dict[str, Any]] = []
    lock = getattr(bot, "_positions_lock", None)
    known = getattr(bot, "known_positions", {}) or {}

    if lock is not None:
        try:
            with lock:
                snapshot = dict(known)
        except Exception:
            snapshot = dict(known)
    else:
        snapshot = dict(known)

    # Cruza com o cache da exchange pra ter mark_price/unrealized_pnl.
    live_by_key: Dict[str, Dict[str, Any]] = {}
    exchange = getattr(bot, "exchange", None)
    if exchange is not None:
        try:
            for live in exchange.get_open_positions() or []:
                key = f"{live.get('symbol', '')}_{live.get('side', '')}"
                live_by_key[key] = live
        except Exception:
            # API down: dashboard segue sem mark_price (mostra "—" nos campos).
            live_by_key = {}

    for position_key, payload in snapshot.items():
        if not isinstance(payload, dict):
            continue
        live = live_by_key.get(position_key, {})
        mark_price = _safe_float(live.get("mark_price")) if live else None
        unrealized_pnl = _safe_float(live.get("unrealized_pnl")) if live else None
        entry_price = _safe_float(payload.get("entry_price"))
        side = payload.get("side", "")

        pnl_percent: Optional[float] = None
        if mark_price and entry_price > 0:
            raw_pct = (mark_price - entry_price) / entry_price * 100.0
            pnl_percent = raw_pct if side == "LONG" else -raw_pct

        positions.append(
            {
                "key": position_key,
                "symbol": payload.get("symbol", ""),
                "side": side,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "quantity": _safe_float(payload.get("quantity")),
                "unrealized_pnl_usd": unrealized_pnl,
                "unrealized_pnl_percent": pnl_percent,
                "strategy_name": payload.get("strategy_name", "primary"),
                "strategy_type": payload.get("strategy_type", "trend_signal"),
                "custom_stop_loss": payload.get("custom_stop_loss"),
                "custom_take_profit": payload.get("custom_take_profit"),
                "trailing_activation_pct": payload.get("trailing_activation_pct"),
                "trailing_distance_pct": payload.get("trailing_distance_pct"),
                "last_seen": _iso(payload.get("last_seen")),
                "entry_time": _iso(payload.get("entry_time")),
            }
        )

    positions.sort(key=lambda p: (p["symbol"], p["side"]))
    return positions


def collect_recent_trades(bot, limit: int = 20) -> List[Dict[str, Any]]:
    """Últimos trades — fonte: TradeStore durável (traz o mfe_pct, fonte de
    verdade). Fallback no trade_history em memória se não houver store."""
    store = getattr(bot, "trade_store", None)
    if store is not None:
        try:
            rows = store.recent_trades(limit)  # cronológico
            out = [
                {
                    "timestamp": _iso(e.get("timestamp")) or e.get("timestamp"),
                    "symbol": e.get("symbol", ""),
                    "side": e.get("side", ""),
                    "signal": e.get("signal", ""),
                    "entry_price": _safe_float(e.get("entry_price")),
                    "exit_price": _safe_float(e.get("exit_price")),
                    "pnl_net": _safe_float(e.get("pnl_net")),
                    "pnl_gross": _safe_float(e.get("pnl_gross")),
                    "fees": _safe_float(e.get("fees")),
                    "strategy_name": e.get("strategy_name", "primary"),
                    "close_reason": e.get("close_reason", ""),
                    "mfe_pct": e.get("mfe_pct"),
                }
                for e in rows
                if isinstance(e, dict)
            ]
            out.reverse()  # mais recentes primeiro
            return out
        except Exception:
            pass

    history = getattr(bot, "trade_history", []) or []
    out: List[Dict[str, Any]] = []
    for entry in history[-limit:]:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "timestamp": _iso(entry.get("timestamp")) or entry.get("timestamp"),
                "symbol": entry.get("symbol", ""),
                "side": entry.get("side", ""),
                "signal": entry.get("signal", ""),
                "entry_price": _safe_float(entry.get("entry_price")),
                "exit_price": _safe_float(entry.get("exit_price")),
                "pnl_net": _safe_float(entry.get("pnl_net")),
                "pnl_gross": _safe_float(entry.get("pnl_gross")),
                "fees": _safe_float(entry.get("fees")),
                "strategy_name": entry.get("strategy_name", "primary"),
                "close_reason": entry.get("close_reason", ""),
                "mfe_pct": entry.get("mfe_pct"),
            }
        )
    out.reverse()
    return out


def collect_regime(bot) -> Dict[str, Any]:
    """Estado do regime classifier — só pares ATIVOS no momento.

    O classifier acumula observações pra todos os símbolos que passam
    pela análise (top N por score), inclusive os que o bot nem chega a
    operar. Pra o painel, filtramos só os pares que estão sendo
    efetivamente operados — união de:
      - pares no trading set atual (config.TRADING_PAIRS)
      - pares com posição aberta agora (known_positions)
    """
    committed_all = dict(getattr(bot, "_regime_committed", {}) or {})
    observations_all = dict(getattr(bot, "_regime_observations", {}) or {})

    active_pairs = set(str(p).upper() for p in (getattr(config, "TRADING_PAIRS", []) or []))
    for pk in (getattr(bot, "known_positions", {}) or {}).keys():
        # known_positions keys são "{SYMBOL}_{SIDE}" — extrai o símbolo
        if isinstance(pk, str) and "_" in pk:
            sym = pk.rsplit("_", 1)[0]
            if sym:
                active_pairs.add(sym.upper())

    committed = {k: v for k, v in committed_all.items() if str(k).upper() in active_pairs}
    observations = {
        k: list(v or [])
        for k, v in observations_all.items()
        if str(k).upper() in active_pairs
    }
    return {
        "committed": committed,
        "observations": observations,
        "enabled": bool(getattr(config, "REGIME_CLASSIFIER_ENABLED", False)),
    }


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse robusto de timestamp p/ datetime UTC-aware (None se ilegível)."""
    from datetime import timezone
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def collect_portfolio_history(bot, limit: int = 200) -> List[Dict[str, Any]]:
    """Série de equity para o gráfico.

    Equity de cada ponto = capital + realizado ACUMULADO até aquele instante +
    não-realizado do snapshot — derivado do MESMO realizado do SQLite que o card
    SALDO usa (store.cumulative_realized_pnl). Antes a curva usava o `pnl_total`
    do snapshot, que carrega o realizado do DIA (reseta à meia-noite UTC), então
    o gráfico divergia do card (302 no card, 299 na curva). Agora batem.
    """
    history = getattr(bot, "portfolio_history", []) or []
    store = getattr(bot, "trade_store", None)
    sim_cap = float(getattr(config, "SIMULATED_BALANCE_USD", 0.0) or 0.0)
    use_sim = bool(getattr(config, "USE_TESTNET", False)) and sim_cap > 0

    # Trades fechados (exit_at, pnl_net) ordenados — base do realizado acumulado.
    closed: List = []
    if store is not None:
        try:
            for t in store.recent_trades(1000):
                if str(t.get("status")) == "closed":
                    ts = _parse_ts(t.get("exit_at"))
                    if ts is not None:
                        closed.append((ts, _safe_float(t.get("pnl_net"))))
            closed.sort(key=lambda x: x[0])
        except Exception:
            closed = []

    def _cumulative_realized_until(ts: Optional[datetime]) -> float:
        if ts is None:
            return sum(p for _t, p in closed)
        return sum(p for _t, p in closed if _t <= ts)

    out: List[Dict[str, Any]] = []
    for snap in history[-limit:]:
        if not isinstance(snap, dict):
            continue
        balance = _safe_float(snap.get("balance"))
        unrealized = _safe_float(snap.get("pnl_unrealized"))
        snap_ts = _parse_ts(snap.get("timestamp"))
        cum_realized = _cumulative_realized_until(snap_ts)
        # Mesma fórmula do card: testnet simulado soma o realizado acumulado ao
        # cap fixo; em mainnet o wallet já reflete o realizado.
        if use_sim:
            equity = sim_cap + cum_realized + unrealized
        else:
            equity = balance + unrealized
        out.append(
            {
                "timestamp": _iso(snap.get("timestamp")) or snap.get("timestamp"),
                "balance": balance,
                "equity": equity,
                "pnl_realized": cum_realized,
                "pnl_unrealized": unrealized,
                "pnl_total": cum_realized + unrealized,
                "closed_trades": int(snap.get("closed_trades", 0) or 0),
            }
        )
    return out
