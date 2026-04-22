"""
Kill switches de risco operacional — pausa o bot em regressão, alerta em WR baixo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol


logger = logging.getLogger(__name__)

DAILY_HISTORY_MAX_ENTRIES = 30


class _TelegramLike(Protocol):
    def send_message(self, text: str) -> bool: ...


@dataclass
class DailyPnlEntry:
    date: str  # YYYY-MM-DD (UTC)
    net_pnl: float
    trades_win: int
    trades_loss: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "net_pnl": float(self.net_pnl),
            "trades_win": int(self.trades_win),
            "trades_loss": int(self.trades_loss),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["DailyPnlEntry"]:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                date=str(raw.get("date") or ""),
                net_pnl=float(raw.get("net_pnl") or 0.0),
                trades_win=int(raw.get("trades_win") or 0),
                trades_loss=int(raw.get("trades_loss") or 0),
            )
        except (TypeError, ValueError):
            return None


class KillSwitchMonitor:
    """
    Checa 3 condições e dispara alertas no Telegram (com ou sem pausar o bot):

    1. N dias consecutivos no vermelho → pausa bot + alerta forte.
    2. Drawdown do pico >= X% → pausa bot + alerta.
    3. Win rate < Y% em amostra ≥ Z trades → apenas alerta.

    `alerted_events` evita spam — cada evento só dispara 1x até mudar o contexto
    (ex: drawdown só re-dispara se sair e voltar a bater o threshold).
    """

    EVENT_LOSS_STREAK = "loss_streak"
    EVENT_DRAWDOWN = "drawdown"
    EVENT_WIN_RATE = "win_rate"

    def __init__(self, config_obj: Any, telegram: _TelegramLike) -> None:
        self.config = config_obj
        self.telegram = telegram
        self.daily_pnl_history: List[DailyPnlEntry] = []
        self.alerted_events: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------

    def to_state(self) -> Dict[str, Any]:
        return {
            "daily_pnl_history": [entry.to_dict() for entry in self.daily_pnl_history],
            "alerted_events": dict(self.alerted_events),
        }

    def load_from_state(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        history = raw.get("daily_pnl_history", [])
        if isinstance(history, list):
            parsed = [DailyPnlEntry.from_dict(item) for item in history]
            self.daily_pnl_history = [e for e in parsed if e is not None]
        alerted = raw.get("alerted_events", {})
        if isinstance(alerted, dict):
            self.alerted_events = {
                str(k): str(v) for k, v in alerted.items() if k and v
            }

    # ------------------------------------------------------------------
    # Hook chamado no rollover diário
    # ------------------------------------------------------------------

    def record_daily_rollover(
        self,
        *,
        date: str,
        net_pnl: float,
        trades_win: int,
        trades_loss: int,
    ) -> None:
        """
        Empurra o P&L do dia que acabou para o histórico. Chamado de dentro
        de check_daily_targets, *antes* do daily_realized_pnl ser zerado.
        """
        if not date or not str(date).strip():
            return
        # Se já tem entry pra esse date (caso raro de double-call), substitui.
        self.daily_pnl_history = [
            e for e in self.daily_pnl_history if e.date != date
        ]
        self.daily_pnl_history.append(
            DailyPnlEntry(
                date=str(date),
                net_pnl=float(net_pnl),
                trades_win=int(trades_win),
                trades_loss=int(trades_loss),
            )
        )
        self.daily_pnl_history.sort(key=lambda e: e.date)
        # Mantém só os últimos N
        if len(self.daily_pnl_history) > DAILY_HISTORY_MAX_ENTRIES:
            self.daily_pnl_history = self.daily_pnl_history[-DAILY_HISTORY_MAX_ENTRIES:]
        # Reset do alerta de loss streak — novo dia pode mudar o contexto.
        self.alerted_events.pop(self.EVENT_LOSS_STREAK, None)

    # ------------------------------------------------------------------
    # Checks principais — chamado periodicamente pelo bot
    # ------------------------------------------------------------------

    def check_all(self, *, bot: Any) -> None:
        if not bool(getattr(self.config, "KILL_SWITCH_ENABLED", True)):
            return

        self._check_loss_streak(bot=bot)
        self._check_drawdown_alert(bot=bot)
        self._check_win_rate_floor(bot=bot)

    def _check_loss_streak(self, *, bot: Any) -> None:
        threshold = int(getattr(self.config, "KILL_SWITCH_LOSS_STREAK_DAYS", 3) or 3)
        if threshold < 1 or len(self.daily_pnl_history) < threshold:
            return

        recent = self.daily_pnl_history[-threshold:]
        if not all(entry.net_pnl < 0 for entry in recent):
            return

        signature = ",".join(f"{e.date}:{e.net_pnl:.2f}" for e in recent)
        if self.alerted_events.get(self.EVENT_LOSS_STREAK) == signature:
            return

        total_loss = sum(e.net_pnl for e in recent)
        streak_desc = " | ".join(f"{e.date}: ${e.net_pnl:+.2f}" for e in recent)

        self._pause_bot(bot)
        self._send_alert(
            f"🛑 <b>KILL SWITCH — SEQUÊNCIA NEGATIVA</b>\n\n"
            f"{threshold} dias consecutivos no vermelho:\n"
            f"<code>{streak_desc}</code>\n\n"
            f"Total no período: <code>${total_loss:+.2f}</code>\n\n"
            "Bot pausado. Pare, refaça os testes e use <code>/resume</code> "
            "quando quiser retomar."
        )
        self.alerted_events[self.EVENT_LOSS_STREAK] = signature

    def _check_drawdown_alert(self, *, bot: Any) -> None:
        threshold = float(
            getattr(self.config, "KILL_SWITCH_DRAWDOWN_ALERT_PERCENT", 5.0) or 0.0
        )
        if threshold <= 0:
            return

        peak = float(getattr(bot, "peak_equity", 0.0) or 0.0)
        if peak <= 0:
            return
        current = self._current_balance(bot)
        if current is None:
            return

        drawdown_pct = (peak - current) / peak * 100.0
        if drawdown_pct < threshold:
            # Saiu da zona de alerta — libera próximo disparo.
            self.alerted_events.pop(self.EVENT_DRAWDOWN, None)
            return

        # Assinatura no formato "peak=X|threshold=Y" — muda se o pico for
        # atualizado (novo episódio de drawdown merece novo alerta).
        signature = f"peak={peak:.2f}|thr={threshold:.2f}"
        if self.alerted_events.get(self.EVENT_DRAWDOWN) == signature:
            return

        drawdown_abs = peak - current
        self._pause_bot(bot)
        self._send_alert(
            f"⚠️ <b>KILL SWITCH — DRAWDOWN DO PICO</b>\n\n"
            f"Drawdown atual: <code>{drawdown_pct:.2f}%</code> "
            f"(gatilho: {threshold:.2f}%)\n"
            f"Pico: <code>${peak:.2f}</code>\n"
            f"Atual: <code>${current:.2f}</code>\n"
            f"Perda desde o pico: <code>-${drawdown_abs:.2f}</code>\n\n"
            "Bot pausado. Revise a posição atual e use <code>/resume</code> "
            "quando quiser retomar."
        )
        self.alerted_events[self.EVENT_DRAWDOWN] = signature

    def _check_win_rate_floor(self, *, bot: Any) -> None:
        wr_floor = float(
            getattr(self.config, "KILL_SWITCH_WR_FLOOR_PERCENT", 40.0) or 0.0
        )
        min_trades = int(
            getattr(self.config, "KILL_SWITCH_WR_MIN_TRADES", 20) or 0
        )
        if wr_floor <= 0 or min_trades <= 0:
            return

        wins = int(getattr(bot, "trades_win_count", 0) or 0)
        losses = int(getattr(bot, "trades_loss_count", 0) or 0)
        total = wins + losses
        if total < min_trades:
            return

        wr = (wins / total * 100.0) if total > 0 else 0.0
        if wr >= wr_floor:
            self.alerted_events.pop(self.EVENT_WIN_RATE, None)
            return

        # Só re-alerta quando o total cruza um novo marco (evita spam a cada trade).
        bucket = total // 10  # alerta a cada 10 trades adicionais se continuar ruim
        signature = f"bucket={bucket}|wr={wr:.1f}"
        if self.alerted_events.get(self.EVENT_WIN_RATE) == signature:
            return

        self._send_alert(
            f"📉 <b>KILL SWITCH — WIN RATE BAIXO</b>\n\n"
            f"Win rate: <code>{wr:.1f}%</code> (gatilho: {wr_floor:.1f}%)\n"
            f"Amostra: <code>{total}</code> trades "
            f"(<code>{wins}W / {losses}L</code>)\n\n"
            "Não é alerta de pausa — é sinal pra revisar o prompt da IA "
            "ou os thresholds da estratégia."
        )
        self.alerted_events[self.EVENT_WIN_RATE] = signature

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pause_bot(self, bot: Any) -> None:
        try:
            bot.paused = True
        except Exception as exc:
            logger.warning(f"⚠️ KillSwitch: falha ao pausar bot: {exc}")

    def _send_alert(self, text: str) -> None:
        try:
            self.telegram.send_message(text)
        except Exception as exc:
            logger.warning(f"⚠️ KillSwitch: falha ao enviar alerta Telegram: {exc}")

    def _current_balance(self, bot: Any) -> Optional[float]:
        """
        Busca o saldo atual. Tenta o cache do bot primeiro; se não tiver, usa a
        API. Se ambos falharem, retorna None e o check é ignorado (sem pânico).
        """
        try:
            cached = getattr(bot, "last_known_balance", None)
            if cached is not None:
                return float(cached)
        except (TypeError, ValueError):
            pass
        try:
            info = bot.exchange.get_account_info()
            return float(info.get("wallet_balance") or 0.0)
        except Exception:
            return None


def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
