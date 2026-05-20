"""
DoubleFirstPolicy — regra de "Double First": dobra o tamanho da primeira
entrada de cada side (ou de cada par+side, dependendo do escopo) quando
habilitado, marca a chave como usada após confirmação de abertura, e
não reaplica até reset/restart.

Motivação: o bot tinha 5 métodos correlacionados (_double_first_scope,
_is_double_first_enabled, _double_first_state_key,
_apply_double_first_order_size, _mark_double_first_used) mais um
_normalize_double_first_state, todos manipulando o mesmo dict
self.double_first_used. ExecutionEngine chamava _apply e _mark
diretamente (3 callsites). Encapsular tudo numa policy reduz o
acoplamento e torna a regra testável isolada.

Storage: `double_first_used` continua sendo um atributo do bot (é
serializado no state_manager). A policy só LÊ e ESCREVE através do bot,
preservando compatibilidade com save/restore.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from .config import config

logger = logging.getLogger(__name__)


class DoubleFirstPolicy:
    """Aplica e rastreia o multiplicador de Double First."""

    def __init__(self, bot):
        self._bot = bot

    # ------------------------------------------------------------------
    # Helpers de configuração — lê do `config` global (mesmo padrão do bot)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_side(side: str) -> str:
        return "SHORT" if str(side).upper() == "SHORT" else "LONG"

    def scope(self) -> str:
        """'global' (um Double First por side) ou 'symbol' (por par+side)."""
        raw = str(getattr(config, "DOUBLE_FIRST_SCOPE", "global") or "global").strip().lower()
        return raw if raw in {"global", "symbol"} else "global"

    def is_enabled(self, side: str) -> bool:
        """Lê DOUBLE_FIRST_LONG_ENABLED / DOUBLE_FIRST_SHORT_ENABLED."""
        normalized = self._normalize_side(side)
        if normalized == "LONG":
            return bool(getattr(config, "DOUBLE_FIRST_LONG_ENABLED", False))
        return bool(getattr(config, "DOUBLE_FIRST_SHORT_ENABLED", False))

    def state_key(self, symbol: str, side: str) -> str:
        """Chave no dict double_first_used. Depende do scope."""
        normalized_side = self._normalize_side(side)
        if self.scope() == "symbol":
            return f"{str(symbol).upper()}_{normalized_side}"
        return normalized_side

    # ------------------------------------------------------------------
    # State storage (mantido no bot.double_first_used)
    # ------------------------------------------------------------------

    def _ensure_state_dict(self) -> Dict[str, bool]:
        if not hasattr(self._bot, "double_first_used") or not isinstance(
            self._bot.double_first_used, dict
        ):
            self._bot.double_first_used = {}
        return self._bot.double_first_used

    # ------------------------------------------------------------------
    # API principal — chamada pelo ExecutionEngine
    # ------------------------------------------------------------------

    def try_double(
        self, symbol: str, side: str, order_size: float
    ) -> Tuple[float, bool, str]:
        """
        Avalia se a regra deve dobrar `order_size`. Retorna:
          (effective_size, applied, state_key)

        - effective_size: o tamanho a usar (dobrado ou não)
        - applied: True se a regra foi aplicada de fato
        - state_key: chave a ser marcada via mark_used() depois que a
          ordem realmente abrir. String vazia se nada se aplica.

        IMPORTANTE: marcar como "usado" só deve acontecer APÓS confirmação
        de open com sucesso — chamar mark_used(state_key, ...) então.
        Aplicar e nunca marcar deixa o slot disponível pro próximo tick,
        preservando idempotência.
        """
        try:
            base = float(order_size)
        except Exception:
            return order_size, False, ""

        if base <= 0:
            return base, False, ""

        normalized_side = self._normalize_side(side)
        if not self.is_enabled(normalized_side):
            return base, False, ""

        multiplier = float(getattr(config, "DOUBLE_FIRST_MULTIPLIER", 1.0) or 1.0)
        if multiplier <= 1.0:
            return base, False, ""

        state = self._ensure_state_dict()
        key = self.state_key(symbol, normalized_side)
        if bool(state.get(key)):
            return base, False, ""

        doubled = base * multiplier
        max_margin = float(getattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 0.0) or 0.0)
        if max_margin > 0:
            doubled = min(doubled, max_margin)

        if doubled <= base:
            return base, False, ""

        return doubled, True, key

    def mark_used(
        self,
        state_key: str,
        symbol: str,
        side: str,
        base_order_size: float,
        applied_order_size: float,
    ) -> None:
        """Marca o slot como consumido após a abertura ser confirmada."""
        if not state_key:
            return
        state = self._ensure_state_dict()
        state[state_key] = True
        logger.info(
            "🚀 Double First confirmado em %s %s: $%.2f → $%.2f (escopo=%s)",
            str(symbol).upper(),
            self._normalize_side(side),
            float(base_order_size),
            float(applied_order_size),
            self.scope(),
        )

    # ------------------------------------------------------------------
    # State restore (legado: aceita dict OU list)
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_state(raw_state: Any) -> Dict[str, bool]:
        """
        Sanitiza o estado vindo do JSON antigo. Aceita:
          - dict {chave: bool}
          - list legada [chave1, chave2, ...] (sempre interpretada como True)
        Filtra chaves vazias e mantém apenas {LONG, SHORT, *_LONG, *_SHORT}.
        """
        normalized: Dict[str, bool] = {}
        if isinstance(raw_state, dict):
            items = raw_state.items()
        elif isinstance(raw_state, list):
            items = [(item, True) for item in raw_state]
        else:
            return normalized

        for key, enabled in items:
            if not enabled:
                continue
            normalized_key = str(key).strip().upper()
            if not normalized_key:
                continue
            if normalized_key in {"LONG", "SHORT"}:
                normalized[normalized_key] = True
                continue
            if normalized_key.endswith("_LONG") or normalized_key.endswith("_SHORT"):
                normalized[normalized_key] = True

        return normalized
