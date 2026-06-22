"""
BotStatePersistence — (de)serialização do estado do bot <-> payload do state JSON.

Extraído de bot.py (Fase 2, slice A). Agrupa o mapeamento PURO entre os
atributos do TradingBot e o dict persistido no state file. Deliberadamente
NÃO faz:
- I/O de arquivo (responsabilidade do StateManager);
- chamadas a exchange/TradeStore/Binance (orquestração fica em bot.load_state,
  que mistura baseline diário, rollover de dia UTC e migração do store).

O objetivo é encolher o bot.py e isolar um seam testável: dado o bot, produz o
payload; dado o dict bruto do state, normaliza known_positions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from .config import config


class BotStatePersistence:
    """Mapeia estado do bot para/de o payload JSON do state file."""

    def __init__(self, bot):
        self._bot = bot

    # --------------------------------------------------------------- known_positions

    @staticmethod
    def serialize_known_positions(known_positions: Any) -> Dict[str, Any]:
        """
        Converte known_positions para forma serializável em JSON.
        O único campo não-JSON-nativo é last_seen (datetime) → ISO string.
        """
        out: Dict[str, Any] = {}
        for key, payload in (known_positions or {}).items():
            if not isinstance(payload, dict):
                continue
            entry = dict(payload)
            # datetimes → ISO. entry_time DEVE persistir: sem ele, o fechamento
            # server-side não tem janela p/ o income e somava o histórico do par
            # (#196). last_seen é volátil mas também serializado.
            for field in ('last_seen', 'entry_time'):
                value = entry.get(field)
                if isinstance(value, datetime):
                    entry[field] = value.isoformat()
            out[key] = entry
        return out

    @staticmethod
    def deserialize_known_positions(raw: Any) -> Dict[str, Any]:
        """Desserializa known_positions do state (last_seen ISO → datetime)."""
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, Any] = {}
        for key, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            entry = dict(payload)
            for field in ('last_seen', 'entry_time'):
                value = entry.get(field)
                if isinstance(value, str):
                    try:
                        entry[field] = datetime.fromisoformat(value)
                    except ValueError:
                        entry[field] = datetime.now() if field == 'last_seen' else None
            out[key] = entry
        return out

    # ----------------------------------------------------------------- build payload

    def build_payload(self) -> Dict[str, Any]:
        """Monta payload serializável para persistência de estado.

        `trade_history` e `portfolio_history` NÃO entram aqui — moram no
        TradeStore (SQLite), que guarda o histórico COMPLETO. O state JSON só
        carrega estado quente (contadores, posições, peaks). Ver trade_store.py.
        """
        bot = self._bot
        return {
            'version': '1.9',  # trade_history/portfolio_history movidos p/ SQLite (trade_store)
            'saved_at': datetime.now().isoformat(),
            'start_time': bot.start_time.isoformat() if isinstance(bot.start_time, datetime) else bot.start_time,
            'initial_capital': bot.initial_capital,
            'closed_trades_count': bot.closed_trades_count,
            'total_pnl': bot.total_pnl,
            'daily_realized_pnl': bot.daily_realized_pnl,
            'daily_date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'pnl_by_symbol': bot.pnl_by_symbol,
            'trades_win_count': bot.trades_win_count,
            'trades_loss_count': bot.trades_loss_count,
            'trades_win_total': bot.trades_win_total,
            'trades_loss_total': bot.trades_loss_total,
            'total_fees_paid': bot.total_fees_paid,
            'daily_pnl_binance_baseline': float(getattr(bot, 'daily_pnl_binance_baseline', 0.0)),
            'daily_baseline_date': getattr(bot, '_daily_baseline_date', None),
            'peak_prices': bot.peak_prices,
            'trailing_activated': bot.trailing_activated,
            # Cooldowns de reentrada (symbol -> epoch do último loss). Persiste
            # para um deploy não zerar o anti-churn no meio de uma maré de losses.
            'symbol_reentry_cooldowns': dict(getattr(bot, 'symbol_reentry_cooldowns', {}) or {}),
            # known_positions persistido pra não perder custom_tp/sl, strategy e
            # range_mid_price no restart — antes, restart recriava entries só com
            # campos básicos vindos da API, zerando a proteção customizada.
            'known_positions': self.serialize_known_positions(bot.known_positions),
            'double_first_used': bot.double_first_used,
            'kill_switch': bot.kill_switch.to_state() if getattr(bot, 'kill_switch', None) else {},
            'max_drawdown_from_peak_percent': float(
                getattr(config, 'MAX_DRAWDOWN_FROM_PEAK_PERCENT', 0.0) or 0.0
            ),
            'sentiment_mode_enabled': bool(bot.sentiment_mode_enabled),
            'invert_signals': bool(bot.invert_signals),
            'last_daily_performance_report_date': bot.last_daily_performance_report_date,
            'last_transfer_check_ts_ms': int(bot.last_transfer_check_ts_ms or 0),
            'processed_transfer_ids': bot.processed_transfer_ids[
                -max(100, int(config.CAPITAL_TRANSFER_TRACKED_IDS_LIMIT)):
            ],
            'disabled_pairs': list(getattr(config, 'DISABLED_PAIRS', []) or []),
            'binance_coin_list': list(getattr(config, 'BINANCE_COIN_LIST', []) or []),
            'strategy_profiles': list(getattr(config, 'STRATEGY_PROFILES', []) or []),
        }
