"""
PositionTracker — encapsula o bookkeeping de posições rastreadas.

Motivação:
- ExecutionEngine construía manualmente um dict de 13 campos pra cada
  posição aberta (uma vez LONG, outra SHORT), com risco de divergir o
  schema entre os dois caminhos. PositionTracker.open() centraliza isso.
- bot.py e engine.py chamavam `_clear_trailing_data(k); _remove_known_position(k)`
  como par em 8+ lugares — `positions.close(k)` substitui ambos num só.
- Os 3 dicts (known_positions, peak_prices, trailing_activated) sempre
  variam juntos: posição entra → known set, peak inicial sem; posição
  fecha → todos os 3 limpos. Tratá-los como um único conceito.

Storage: continua nos atributos do bot (known_positions, peak_prices,
trailing_activated). Mantém compatibilidade com state_manager (que
serializa/desserializa known_positions) e com os 30+ leitores diretos
desses dicts no resto do bot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


class PositionTracker:
    """API uniforme pra mutações de posições + trailing state."""

    def __init__(self, bot):
        self._bot = bot

    # ------------------------------------------------------------------
    # Operações de baixo nível (substituem _set/_get/_remove_known_position)
    # ------------------------------------------------------------------

    def set(self, position_key: str, payload: Dict[str, Any]) -> None:
        """Substitui o registro completo da posição (sob lock)."""
        with self._bot._positions_lock:
            self._bot.known_positions[position_key] = dict(payload)

    def get(self, position_key: str) -> Dict[str, Any]:
        """Cópia defensiva do registro da posição (sob lock)."""
        with self._bot._positions_lock:
            return dict(self._bot.known_positions.get(position_key, {}) or {})

    def remove(self, position_key: str) -> None:
        """Remove o registro da posição (sob lock). NÃO limpa trailing."""
        with self._bot._positions_lock:
            self._bot.known_positions.pop(position_key, None)

    def clear_trailing(self, position_key: str) -> None:
        """Limpa peak_prices e trailing_activated para a posição."""
        bot = self._bot
        if position_key in bot.peak_prices:
            del bot.peak_prices[position_key]
        if position_key in bot.trailing_activated:
            del bot.trailing_activated[position_key]

    # ------------------------------------------------------------------
    # Operações compostas (cobrem padrões de uso comuns)
    # ------------------------------------------------------------------

    def close(self, position_key: str) -> None:
        """
        Fecha posição: remove do tracker + limpa trailing.

        Encapsula o par `clear_trailing + remove` que aparecia em 8+
        callsites diferentes (engine + monitor loop), sempre na mesma
        ordem. Centralizar evita esquecer um dos dois.
        """
        self.clear_trailing(position_key)
        self.remove(position_key)

    def open(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        strategy_name: str,
        strategy_type: str,
        custom_stop_loss: Optional[float] = None,
        custom_take_profit: Optional[float] = None,
        range_mid_price: Optional[float] = None,
        range_entry_side: Optional[str] = None,
        trailing_activation_pct: Optional[float] = None,
        trailing_distance_pct: Optional[float] = None,
    ) -> str:
        """
        Registra uma posição recém-aberta. Constrói o payload com o schema
        canônico (14 campos), seta entry_time e last_seen no momento da
        chamada, e retorna o position_key resultante.

        ATENÇÃO: o schema aqui DEVE ficar em sincronia com:
        - state_manager._serialize_known_positions / _deserialize_known_positions
        - bot.py reconciliação (setup_exchange e monitor_positions)
        - dashboard collect_positions
        Mudar campos aqui exige atualizar esses outros lugares também.
        """
        now = datetime.now()
        position_key = f"{symbol}_{side}"
        payload = {
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "quantity": quantity,
            "entry_time": now,
            "last_seen": now,
            "strategy_name": str(strategy_name or "primary"),
            "strategy_type": strategy_type,
            "custom_stop_loss": custom_stop_loss,
            "custom_take_profit": custom_take_profit,
            "range_mid_price": range_mid_price,
            "range_entry_side": range_entry_side,
            "trailing_activation_pct": trailing_activation_pct,
            "trailing_distance_pct": trailing_distance_pct,
        }
        self.set(position_key, payload)
        return position_key
