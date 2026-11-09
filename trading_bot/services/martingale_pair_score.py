"""Score por regras (sem IA) das moedas da planilha CoinTech2u pro martingale hedge.

O que mata martingale: tendência forte sustentada e volatilidade que pula degraus.
Cada moeda ganha verde/amarelo/vermelho por 4 critérios (docs/MARTINGALE_HEDGE.md,
"Score dinâmico dos pares"); vermelho em qualquer um = vermelho, todos verdes = verde.
Só leitura: o dashboard mostra, nada troca de par nem pausa.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..core.config import config
from ..core.strategy import TechnicalAnalysis

logger = logging.getLogger(__name__)

ADX_PERIOD = 14
VOL_DAYS = 14
MOVE_DAYS = 30
# Klines pedidos à Binance: +1 porque o último candle está aberto e é descartado.
KLINES_1D_LIMIT = MOVE_DAYS + 1
KLINES_4H_LIMIT = 60 + 1


def _pt(v: float, nd: int = 1) -> str:
    return f"{v:.{nd}f}".replace(".", ",")


def max_move_pct(closes: List[float]) -> float:
    """Maior variação close-to-close numa só direção (maior rali ou maior queda, pico-a-vale)."""
    best = 0.0
    for i in range(len(closes)):
        for j in range(i + 1, len(closes)):
            if closes[i] > 0 and closes[j] > 0:
                best = max(best, abs(closes[j] / closes[i] - 1.0) * 100.0)
    return best


def move_green(cfg=config) -> float:
    return cfg.MARTINGALE_SCORE_MOVE_GREEN or cfg.MARTINGALE_STEP_PERCENT * cfg.MARTINGALE_MAX_LEVELS


def rank_score(sc: Dict[str, Any], cfg=config) -> Optional[float]:
    """Score contínuo pro ranking: soma das distâncias normalizadas aos limiares verdes
    (1,0 = exatamente no limiar; menor = melhor) + MIN_VOLUME/volume se abaixo do piso. None sem dado."""
    if sc.get("vol_pct") is None or sc.get("adx") is None or sc.get("max_move_pct") is None:
        return None
    vol_usd = max(float(sc.get("volume_usd") or 0.0), 1.0)
    penalty = cfg.MARTINGALE_SCORE_MIN_VOLUME_USD / vol_usd if vol_usd < cfg.MARTINGALE_SCORE_MIN_VOLUME_USD else 0.0
    return (sc["vol_pct"] / cfg.MARTINGALE_SCORE_VOL_GREEN + sc["adx"] / cfg.MARTINGALE_SCORE_ADX_GREEN
            + sc["max_move_pct"] / move_green(cfg) + penalty)


def score_pair(klines_1d: List[List[Any]], klines_4h: List[List[Any]], cfg=config) -> Dict[str, Any]:
    """Klines FECHADOS no formato bruto da Binance (lista de listas). Dados insuficientes → "unknown"."""
    out: Dict[str, Any] = {"color": "unknown", "vol_pct": None, "adx": None, "max_move_pct": None,
                           "volume_usd": None, "reasons": []}
    if len(klines_1d) < MOVE_DAYS or len(klines_4h) < ADX_PERIOD * 2 + 1:
        out["reasons"] = ["dados insuficientes"]
        return out
    d = klines_1d[-MOVE_DAYS:]
    last = d[-VOL_DAYS:]
    out["vol_pct"] = sum((float(k[2]) - float(k[3])) / float(k[4]) for k in last) / len(last) * 100.0
    out["volume_usd"] = sum(float(k[7]) for k in last) / len(last)
    out["max_move_pct"] = max_move_pct([float(k[4]) for k in d])
    out["adx"] = TechnicalAnalysis.calculate_adx([float(k[2]) for k in klines_4h], [float(k[3]) for k in klines_4h],
                                                 [float(k[4]) for k in klines_4h], period=ADX_PERIOD)
    checks = [  # (rótulo, valor, verde se <, vermelho se >, casas decimais, unidade)
        ("vol", out["vol_pct"], cfg.MARTINGALE_SCORE_VOL_GREEN, cfg.MARTINGALE_SCORE_VOL_RED, 1, "%"),
        ("adx", out["adx"], cfg.MARTINGALE_SCORE_ADX_GREEN, cfg.MARTINGALE_SCORE_ADX_RED, 0, ""),
        ("movimento 30d", out["max_move_pct"], move_green(cfg), cfg.MARTINGALE_SCORE_MOVE_RED, 1, "%"),
    ]
    red = False
    for label, value, green, red_thr, nd, unit in checks:
        if value < green:
            continue
        is_red = value > red_thr
        red |= is_red
        out["reasons"].append(f"{label} {_pt(value, nd)}{unit} (>{_pt(red_thr if is_red else green, nd)}{unit})")
    if out["volume_usd"] < cfg.MARTINGALE_SCORE_MIN_VOLUME_USD:
        red = True
        out["reasons"].append(f"volume {out['volume_usd'] / 1e6:.0f}M (<{cfg.MARTINGALE_SCORE_MIN_VOLUME_USD / 1e6:.0f}M)")
    out["color"] = "red" if red else ("green" if not out["reasons"] else "yellow")
    return out


class PairScoreCache:
    """Recalcula o score de todos os símbolos a cada `refresh_minutes` numa thread daemon
    iniciada no 1º get(). get() nunca bloqueia: devolve {} até a 1ª rodada terminar.
    Falha de rede num símbolo mantém a entrada anterior (ou "unknown" se não houver)."""

    def __init__(self, exchange, symbols: Iterable[str], refresh_minutes: float):
        self.exchange = exchange
        self.symbols = list(symbols)
        self.refresh_seconds = max(60.0, float(refresh_minutes) * 60.0)
        self.scores: Dict[str, Dict[str, Any]] = {}
        self.updated_at: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def get(self) -> Dict[str, Any]:
        if self._thread is None and self.exchange is not None and hasattr(self.exchange, "get_klines_raw"):
            self._thread = threading.Thread(target=self._loop, name="martingale-pair-score", daemon=True)
            self._thread.start()
        with self._lock:
            return {"scores": dict(self.scores), "updated_at": self.updated_at}

    def _loop(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception as exc:  # a thread nunca morre por erro de rede
                logger.warning(f"Score dos pares da planilha falhou: {exc}")
            time.sleep(self.refresh_seconds)

    def refresh(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for sym in self.symbols:
            # get_klines_raw já loga e devolve [] em erro; o último candle está aberto.
            d1 = (self.exchange.get_klines_raw(sym, "1d", limit=KLINES_1D_LIMIT) or [])[:-1]
            h4 = (self.exchange.get_klines_raw(sym, "4h", limit=KLINES_4H_LIMIT) or [])[:-1]
            score = score_pair(d1, h4)
            with self._lock:
                if score["color"] == "unknown" and sym in self.scores:
                    continue
                self.scores[sym] = {**score, "updated_at": now}
        with self._lock:
            self.updated_at = now
        logger.info("📊 Score da planilha CoinTech2u atualizado: "
                    + ", ".join(f"{s}={v['color']}" for s, v in sorted(self.scores.items())))
