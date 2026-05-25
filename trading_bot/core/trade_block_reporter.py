"""
TradeBlockReporter — notifica via Telegram quando uma entrada APROVADA
pela IA é barrada na execução (cooldown estrutural, exposição, falha de
abertura, preço inválido, etc.).

Motivação: o ExecutionEngine tinha 10 callsites chamando
`bot._notify_ai_approved_trade_block(...)` espalhados pelos vários
"return False" do open_signal_trade. Encapsular essa lógica numa classe
única:
- Remove o método "_notify_*" privado do bot (que de privado só tinha o
  underscore — era na verdade um colaborador externo)
- Permite testar isolado: AI mode check, approval check, cooldown,
  formatação HTML, sem precisar instanciar o bot inteiro
- Reduz acoplamento de engine.py com 10 acessos a um único colaborador

Estado preservado:
- Cooldown cache (dict in-memory): vive AGORA dentro do reporter.
  Antes ficava em `bot._ai_execution_block_notifications`. Não era
  serializado entre restarts (usa time.monotonic), então a migração não
  requer compat layer.
- Telegram client e config: passados como dependências no construtor.
"""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Janela em que a mesma rejeição (symbol+side+reason) não é re-notificada,
# pra evitar flood de Telegram quando o bot tenta abrir várias vezes a
# mesma posição barrada (ex.: cooldown estrutural que dura 30min).
_NOTIFICATION_COOLDOWN_SECONDS = 180.0


class TradeBlockReporter:
    """Notifica bloqueios de execução em entradas aprovadas pela IA."""

    def __init__(self, telegram_provider, config):
        """
        telegram_provider: callable que retorna o cliente telegram OU None.
        Usar callable (em vez de receber o cliente direto) preserva o
        comportamento do método original: `bot.telegram` pode ser setado
        DEPOIS de __init__ (no setup_exchange), e o reporter sempre vê o
        valor corrente em vez de uma referência travada como None.
        """
        self._get_telegram = telegram_provider
        self._config = config
        self._cooldown_cache: Dict[str, float] = {}

    def notify_blocked(
        self,
        *,
        symbol: str,
        side: str,
        strategy_name: str,
        reason: str,
        detail: str = "",
        setup_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Retorna True se a mensagem foi enviada, False em qualquer skip."""
        telegram = self._get_telegram()
        if telegram is None:
            return False

        # Só notifica se o bot está no modo "gated" — fora disso, IA é
        # advisory e bloqueios são silenciosos (já tem log).
        ai_mode = str(getattr(self._config, "AI_CONSULTIVE_MODE", "off") or "off")
        if ai_mode.strip().lower() != "gated":
            return False

        # Só notifica se a IA REALMENTE aprovou (não inunda Telegram com
        # bloqueios em setups que a IA já tinha vetado por outras razões).
        ai_metadata = dict((setup_metadata or {}).get("ai_consultive", {}) or {})
        if not bool(ai_metadata.get("approval")):
            return False

        # Cooldown por (symbol, side, reason): evita flood quando a mesma
        # condição persiste por minutos (cooldown estrutural, exposição
        # acima do limite, etc.).
        cache_key = f"{symbol}:{side}:{reason}"
        now_mono = time.monotonic()
        last_sent = self._cooldown_cache.get(cache_key)
        if last_sent is not None and (now_mono - float(last_sent)) < _NOTIFICATION_COOLDOWN_SECONDS:
            return False
        self._cooldown_cache[cache_key] = now_mono

        confidence = int(ai_metadata.get("confidence", 0) or 0)
        decision = str(ai_metadata.get("decision", "ENTER_NOW") or "ENTER_NOW")
        side_label = "Compra" if side == "LONG" else "Venda" if side == "SHORT" else side
        strategy_label = str(strategy_name or "primary").replace("_", " ")
        detail_line = f"\n📝 <b>Detalhe:</b> {html.escape(detail)}" if detail else ""
        now_brt = datetime.now(timezone(timedelta(hours=-3))).strftime("%H:%M:%S")

        message = (
            f"⏸️ <b>ENTRADA CANCELADA</b> <i>({now_brt})</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 <b>Par:</b> {html.escape(symbol.replace('USDT', ''))}/USDT\n"
            f"🤖 <b>Estratégia:</b> {html.escape(strategy_label)}\n"
            f"🧭 <b>Direção:</b> {html.escape(side_label)}\n"
            f"🧠 <b>IA:</b> {html.escape(decision)} ({confidence}/100)\n"
            f"⚠️ <b>Motivo do bloqueio:</b> {html.escape(reason)}{detail_line}\n\n"
            f"<i>A IA aprovou o setup, mas a execução foi barrada pelos freios "
            f"operacionais do bot.</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )
        try:
            return bool(telegram.send_message(message))
        except Exception:
            logger.exception("Falha ao enviar notificação de bloqueio via Telegram")
            return False
