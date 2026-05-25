"""
LoopScheduler: gerenciamento de "quando rodar cada tarefa" no loop principal.

Motivação:
- O `run()` do bot acumulava ~15 variáveis `next_*_time` intercaladas, cada uma
  com seu intervalo próprio (monitor, state_save, commission, pair_update, etc).
- Extrair isso pra um objeto focado torna o loop legível e o timing testável.

O scheduler **não executa** callbacks — só diz se uma tarefa está devida (`due()`)
e re-agenda após execução (`mark_ran()`). Toda lógica de trabalho fica no bot,
mantendo o fluxo de controle do loop principal intacto.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class _Task:
    interval: float
    next_time: float


class LoopScheduler:
    """Trilha múltiplas tarefas periódicas usando time.monotonic()."""

    def __init__(self) -> None:
        self._tasks: Dict[str, _Task] = {}

    def add(self, name: str, interval: float, initial_delay: float | None = None) -> None:
        """
        Registra uma nova tarefa periódica.

        Args:
            name: identificador único
            interval: segundos entre execuções
            initial_delay: tempo até a PRIMEIRA execução. Se None, tarefa começa
                           devida imediatamente (padrão pra monitor). Use `interval`
                           pra começar apenas após uma janela (padrão pra
                           manutenção periódica tipo state_save).
        """
        if interval <= 0:
            raise ValueError(f"interval deve ser > 0 (recebido {interval} para {name})")
        first_delay = interval if initial_delay is None else max(0.0, float(initial_delay))
        self._tasks[name] = _Task(
            interval=float(interval),
            next_time=time.monotonic() + first_delay,
        )

    def remove(self, name: str) -> None:
        """Desregistra uma tarefa. No-op se não existir."""
        self._tasks.pop(name, None)

    def set_interval(self, name: str, interval: float) -> None:
        """Atualiza o intervalo de uma tarefa (aplica a partir da próxima execução)."""
        if interval <= 0:
            raise ValueError(f"interval deve ser > 0 (recebido {interval})")
        task = self._tasks.get(name)
        if task is None:
            return
        task.interval = float(interval)

    def due(self, name: str, now: float) -> bool:
        """True se a tarefa está pronta pra executar agora."""
        task = self._tasks.get(name)
        if task is None:
            return False
        return now >= task.next_time

    def mark_ran(self, name: str, now: float) -> None:
        """Marca que a tarefa acabou de executar — re-agenda para now + interval."""
        task = self._tasks.get(name)
        if task is None:
            return
        task.next_time = now + task.interval

    def next_time(self, name: str) -> float:
        """Timestamp monotônico da próxima execução. inf se tarefa não existe."""
        task = self._tasks.get(name)
        if task is None:
            return float("inf")
        return task.next_time

    def advance_next_time(self, name: str, when: float) -> None:
        """Força uma nova próxima execução (usado em reagendamento de timing profile)."""
        task = self._tasks.get(name)
        if task is None:
            return
        task.next_time = float(when)


# ---------------------------------------------------------------------------
# Timing profile — derivado do config + número de pares
# ---------------------------------------------------------------------------

def get_loop_timing_profile(config_obj: Any, num_pairs: int) -> Dict[str, Any]:
    """
    Calcula o perfil de timing do loop baseado em config + pares ativos.

    Modo MANUAL (se USE_BINANCE_STRATEGY=False): usa CHECK_INTERVAL /
    POSITION_MONITOR_INTERVAL / ANALYSIS_SYMBOL_DELAY do config.

    Modo AUTO (USE_BINANCE_STRATEGY=True): ajusta dinamicamente conforme a
    quantidade de pares operados (mais pares → ciclo de análise mais longo
    pra cobrir todos com delay entre cada).
    """
    profile = {
        'monitor_interval': max(1, int(getattr(config_obj, "POSITION_MONITOR_INTERVAL", 2))),
        'analysis_cycle_interval': max(1, int(getattr(config_obj, "CHECK_INTERVAL", 5))),
        'analysis_symbol_delay': max(0.1, float(getattr(config_obj, "ANALYSIS_SYMBOL_DELAY", 1.0))),
        'mode': 'manual',
        'pairs': num_pairs,
    }

    if not getattr(config_obj, "USE_BINANCE_STRATEGY", False):
        return profile

    # Preset por faixa de pares (paridade 1:1 com bot original)
    if num_pairs <= 3:
        profile.update(monitor_interval=2, analysis_cycle_interval=3, analysis_symbol_delay=0.5)
    elif num_pairs <= 6:
        profile.update(monitor_interval=2, analysis_cycle_interval=4, analysis_symbol_delay=0.7)
    elif num_pairs <= 9:
        profile.update(monitor_interval=2, analysis_cycle_interval=5, analysis_symbol_delay=0.9)
    elif num_pairs <= 10:
        profile.update(monitor_interval=2, analysis_cycle_interval=6, analysis_symbol_delay=1.0)
    elif num_pairs <= 11:
        profile.update(monitor_interval=3, analysis_cycle_interval=6, analysis_symbol_delay=1.1)
    else:
        profile.update(monitor_interval=3, analysis_cycle_interval=7, analysis_symbol_delay=1.2)

    profile['mode'] = 'dynamic_binance'
    return profile


def timing_profile_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    """True se os campos materiais de dois profiles divergem."""
    keys = ('monitor_interval', 'analysis_cycle_interval', 'analysis_symbol_delay', 'pairs', 'mode')
    return any(old.get(k) != new.get(k) for k in keys)
