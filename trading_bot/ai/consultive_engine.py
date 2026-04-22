"""
IA consultiva para revisão de setups de entrada.

V1:
- avalia setups já aprovados pelas regras fixas do bot
- produz parecer estruturado via GPT
- não decide execução automática
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List, Optional

import requests

from ..core.strategy import TechnicalAnalysis


logger = logging.getLogger(__name__)

BRT = timezone(timedelta(hours=-3))
VALID_DECISIONS = {"ENTER_NOW", "WAIT_PULLBACK", "REJECT"}
VALID_RISK_GRADES = {"A", "B", "C", "D"}
VALID_ENTRY_SIDES = {"LONG", "SHORT", "NONE"}
DECISION_LABELS = {
    "ENTER_NOW": "Entrar agora",
    "WAIT_PULLBACK": "Aguardar correção",
    "REJECT": "Rejeitar",
}
SIGNAL_LABELS = {
    "STRONG_BUY": "Compra forte",
    "BUY": "Compra",
    "STRONG_SELL": "Venda forte",
    "SELL": "Venda",
    "NEUTRAL": "Neutro",
}
STRATEGY_LABELS = {
    "trend_strong": "Tendência forte",
    "range_scalping": "Scalping em faixa",
    "primary": "Principal",
}
RISK_LABELS = {
    "A": "Baixo",
    "B": "Moderado",
    "C": "Alto",
    "D": "Muito alto",
}
SIDE_LABELS = {
    "LONG": "Compra",
    "SHORT": "Venda",
    "NONE": "Nenhuma",
}
TEXT_ITEM_LABELS = {
    "low_volume": "Volume fraco",
    "volume_ratio_below_1": "Volume abaixo da média",
    "insufficient_momentum": "Momentum fraco",
    "overextended_price": "Preço esticado",
    "trend_conflict": "Tendência sem confirmação",
    "late_entry": "Entrada atrasada",
    "weak_trend": "Tendência fraca",
    "weak_confirmation": "Confirmação fraca",
}

SYSTEM_PROMPT = """
Você revisa setups de trading futures para triagem operacional interna.
Avalie somente o payload recebido.
Se houver dúvida, atraso ou inconsistência, prefira WAIT_PULLBACK ou REJECT.
Não invente indicadores, contexto, capital ou gestão fora do payload.
Isso não é aconselhamento financeiro personalizado.
Todos os campos textuais devem estar em português do Brasil.
Seja claro, objetivo e curto.
Use frases simples.
Em reasons e invalidators, use linguagem humana. Não use snake_case, siglas soltas nem inglês desnecessário.
Respeite as regras operacionais do payload.
Se hedge_mode_enabled=true e opposite_side_entry_allowed=true, a existência de posição no lado oposto no mesmo símbolo NÃO é motivo suficiente para rejeitar a entrada.
Só trate posição aberta no mesmo símbolo como impeditiva quando same_side_entry_blocked=true e same_side_position_open=true.
Se allowed_entry_sides contiver apenas um lado, use somente esse lado ou NONE.
Se decision=ENTER_NOW, entry_side precisa ser LONG ou SHORT e respeitar allowed_entry_sides.

Regras de timing (chase / pullback) — avalie ANTES de decidir ENTER_NOW:
- dist_from_ema9_percent mede o quanto current_price está afastado da EMA9. Em LONG, valor positivo = acima; em SHORT, considere o módulo.
- Se |dist_from_ema9_percent| > 1.5 × atr_percent, o preço está esticado e a entrada é tardia: prefira WAIT_PULLBACK.
- Se recent_range_percent > 2 × atr_percent, as últimas velas já se moveram muito; prefira WAIT_PULLBACK.
- |dist_from_vwap_percent| > 1.2 × atr_percent reforça a hipótese de entrada tardia.
- Use timing_score para refletir esse risco: entradas frescas pontuam 7–10; esticadas pontuam 3–6; muito esticadas pontuam 0–3.

Regras para entry_window_min e entry_window_max (janela ideal de entrada a limite):
- Sempre retorne números próximos do current_price (±2 × atr_percent), ou null se não houver alvo claro.
- NUNCA retorne 0, valores negativos, ou janelas amplas (max − min maior que 1.5 × atr_percent × current_price / 100).
- Em WAIT_PULLBACK, posicione a janela em torno do EMA9 ou VWAP (o mais próximo do current_price no sentido do pullback), com amplitude ≈ 0.2 × atr_percent × current_price / 100.
- Em ENTER_NOW, pode retornar null/null (entrada a mercado) ou uma janela estreita ao redor do current_price.
- Em REJECT, sempre null/null.

Retorne somente um JSON válido com:
decision, entry_side, confidence, timing_score, risk_grade, entry_window_min, entry_window_max, wait_seconds, reasons, invalidators, telegram_summary.
Sem markdown. Sem texto fora do JSON.
""".strip()

OPENAI_CONSULTIVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": sorted(VALID_DECISIONS),
        },
        "entry_side": {
            "type": "string",
            "enum": sorted(VALID_ENTRY_SIDES),
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
        "timing_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
        },
        "risk_grade": {
            "type": "string",
            "enum": sorted(VALID_RISK_GRADES),
        },
        "entry_window_min": {
            "type": ["number", "null"],
        },
        "entry_window_max": {
            "type": ["number", "null"],
        },
        "wait_seconds": {
            "type": "integer",
            "minimum": 0,
            "maximum": 86400,
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 4,
        },
        "invalidators": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
        "telegram_summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 220,
        },
    },
    "required": [
        "decision",
        "entry_side",
        "confidence",
        "timing_score",
        "risk_grade",
        "entry_window_min",
        "entry_window_max",
        "wait_seconds",
        "reasons",
        "invalidators",
        "telegram_summary",
    ],
}


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe_text_items(values: Any, limit: int) -> List[str]:
    items = values if isinstance(values, list) else []
    normalized: List[str] = []
    seen = set()
    for raw in items:
        token = str(raw or "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(token[:160])
        if len(normalized) >= limit:
            break
    return normalized


def _serialize_for_log(value: Any, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _translate_strategy_name(name: str) -> str:
    token = str(name or "").strip()
    return STRATEGY_LABELS.get(token, token.replace("_", " ").strip().title() or "Estratégia")


def _translate_signal_name(signal: str) -> str:
    token = str(signal or "").strip().upper()
    return SIGNAL_LABELS.get(token, token.replace("_", " ").strip().title() or "Sinal")


def _translate_risk_grade(grade: str) -> str:
    token = str(grade or "").strip().upper()
    label = RISK_LABELS.get(token, "Indefinido")
    return f"{label} ({token})" if token else label


def _translate_side_name(side: str) -> str:
    token = str(side or "").strip().upper()
    return SIDE_LABELS.get(token, token.title() if token else "Nenhuma")


def _humanize_wait_seconds(seconds: int) -> str:
    total = max(0, int(seconds or 0))
    if total == 0:
        return "0s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    parts: List[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


def _humanize_text_item(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    key = raw.lower()
    if key in TEXT_ITEM_LABELS:
        return TEXT_ITEM_LABELS[key]
    if "_" in raw and raw.replace("_", "").isalnum():
        words = raw.replace("_", " ").strip()
        return words[:1].upper() + words[1:]
    return raw


def _humanize_summary(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    replacements = {
        "WAIT:": "Aguardar:",
        "REJECT:": "Rejeitar:",
        "ENTER:": "Entrar:",
        "Trend ": "Tendência ",
        "bullish": "altista",
        "bearish": "baixista",
        " but ": " mas ",
        " and ": " e ",
        "pullback": "correção",
        "volume weak": "volume fraco",
        "momentum limited": "momentum limitado",
        "stronger confirmation": "confirmação mais forte",
        " is preferable ": " é preferível ",
        "before entering": "antes de entrar",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


@dataclass
class ProviderReview:
    provider: str
    model: str
    status: str
    decision: str
    entry_side: str
    confidence: int
    timing_score: int
    risk_grade: str
    entry_window_min: Optional[float]
    entry_window_max: Optional[float]
    wait_seconds: int
    reasons: List[str] = field(default_factory=list)
    invalidators: List[str] = field(default_factory=list)
    telegram_summary: str = ""
    raw_text: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "decision": self.decision,
            "entry_side": self.entry_side,
            "confidence": self.confidence,
            "timing_score": self.timing_score,
            "risk_grade": self.risk_grade,
            "entry_window_min": self.entry_window_min,
            "entry_window_max": self.entry_window_max,
            "wait_seconds": self.wait_seconds,
            "reasons": list(self.reasons),
            "invalidators": list(self.invalidators),
            "telegram_summary": self.telegram_summary,
            "error": self.error,
        }


@dataclass
class ConsultiveReview:
    status: str
    decision: str
    entry_side: str
    approval: bool
    confidence: int
    timing_score: int
    risk_grade: str
    entry_window_min: Optional[float]
    entry_window_max: Optional[float]
    wait_seconds: int
    reasons: List[str]
    invalidators: List[str]
    telegram_summary: str
    providers: List[ProviderReview] = field(default_factory=list)
    from_cache: bool = False
    should_notify: bool = False
    cache_key: str = ""
    symbol: str = ""
    strategy_name: str = ""
    signal: str = ""
    side: str = ""
    mode: str = "off"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "entry_side": self.entry_side,
            "approval": self.approval,
            "confidence": self.confidence,
            "timing_score": self.timing_score,
            "risk_grade": self.risk_grade,
            "entry_window_min": self.entry_window_min,
            "entry_window_max": self.entry_window_max,
            "wait_seconds": self.wait_seconds,
            "reasons": list(self.reasons),
            "invalidators": list(self.invalidators),
            "telegram_summary": self.telegram_summary,
            "providers": [provider.to_dict() for provider in self.providers],
            "from_cache": self.from_cache,
            "should_notify": self.should_notify,
            "cache_key": self.cache_key,
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "signal": self.signal,
            "side": self.side,
            "mode": self.mode,
            "error": self.error,
        }

    def compact_for_trade(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "entry_side": self.entry_side,
            "approval": self.approval,
            "confidence": self.confidence,
            "timing_score": self.timing_score,
            "risk_grade": self.risk_grade,
            "wait_seconds": self.wait_seconds,
            "providers": [provider.provider for provider in self.providers if provider.status == "ok"],
            "summary": self.telegram_summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class ConsultiveEngine:
    """Motor de IA consultiva com OpenAI."""

    def __init__(
        self,
        config_obj: Any,
        session: Optional[requests.Session] = None,
        time_fn: Any = None,
    ):
        self.config = config_obj
        self.session = session or requests.Session()
        self.time_fn = time_fn or time.monotonic
        self.ta = TechnicalAnalysis()
        self._review_cache: Dict[str, Dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return str(getattr(self.config, "AI_CONSULTIVE_MODE", "off")).strip().lower() in {"consultive", "gated"}

    def _reasoning_effort(self) -> str:
        raw = str(getattr(self.config, "AI_CONSULTIVE_REASONING_EFFORT", "low") or "low").strip().lower()
        if raw not in {"minimal", "low", "medium", "high"}:
            return "low"
        return raw

    def build_market_snapshot(
        self,
        *,
        symbol: str,
        strategy_name: str,
        strategy_type: str,
        entry_mode: str,
        signal_name: str,
        setup: Any,
        klines: List[Dict[str, Any]],
        confirmation_klines: Optional[List[Dict[str, Any]]],
        execution_timeframe: str,
        confirmation_timeframe: Optional[str],
        available_balance: float,
        open_positions: List[Dict[str, Any]],
        should_open_long: bool,
        should_open_short: bool,
        min_notional: float,
        sentiment_snapshot: Optional[Dict[str, Any]] = None,
        requested_side: Optional[str] = None,
        allowed_entry_sides: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        normalized_requested_side = str(requested_side or "").strip().upper()
        if normalized_requested_side not in {"LONG", "SHORT"}:
            if should_open_long and not should_open_short:
                normalized_requested_side = "LONG"
            elif should_open_short and not should_open_long:
                normalized_requested_side = "SHORT"
            else:
                normalized_requested_side = "NONE"
        normalized_allowed_sides = [
            side
            for side in [str(item or "").strip().upper() for item in (allowed_entry_sides or [])]
            if side in {"LONG", "SHORT"}
        ]
        if not normalized_allowed_sides and normalized_requested_side in {"LONG", "SHORT"}:
            normalized_allowed_sides = [normalized_requested_side]

        side = normalized_requested_side
        closes = [float(item.get("close", 0) or 0) for item in klines]
        highs = [float(item.get("high", 0) or 0) for item in klines]
        lows = [float(item.get("low", 0) or 0) for item in klines]
        volumes = [float(item.get("volume", 0) or 0) for item in klines]
        current_price = float(closes[-1] if closes else getattr(setup, "entry_price", 0.0) or 0.0)

        ema9 = float(self.ta.calculate_ema(closes, 9)) if closes else 0.0
        ema21 = float(self.ta.calculate_ema(closes, 21)) if closes else 0.0
        ema200 = float(self.ta.calculate_ema(closes, 200)) if closes else 0.0
        rsi14 = float(self.ta.calculate_rsi(closes, 14)) if closes else 50.0
        vwap = float(self.ta.calculate_vwap(highs, lows, closes, volumes)) if closes and volumes else current_price
        atr_value = float(self.ta.calculate_atr(highs, lows, closes)) if closes else 0.0
        atr_percent = (atr_value / current_price * 100.0) if current_price > 0 else 0.0

        recent_volumes = [value for value in volumes[-20:] if value > 0]
        avg_volume = float(mean(recent_volumes)) if recent_volumes else 0.0
        current_volume = float(volumes[-1]) if volumes else 0.0
        volume_ratio = (current_volume / avg_volume) if avg_volume > 0 else 0.0

        # Features de "quão esticada está a entrada" — chave para a IA decidir
        # entre ENTER_NOW e WAIT_PULLBACK em sinais de tendência.
        dist_from_ema9_pct = (
            ((current_price - ema9) / ema9 * 100.0)
            if ema9 > 0 and current_price > 0 else 0.0
        )
        dist_from_vwap_pct = (
            ((current_price - vwap) / vwap * 100.0)
            if vwap > 0 and current_price > 0 else 0.0
        )
        # Alcance acumulado das últimas 5 velas como % do preço atual —
        # se for muito grande, o movimento já "anda" (chasing).
        recent_highs = [h for h in highs[-5:] if h > 0]
        recent_lows = [l for l in lows[-5:] if l > 0]
        recent_range_pct = (
            ((max(recent_highs) - min(recent_lows)) / current_price * 100.0)
            if recent_highs and recent_lows and current_price > 0 else 0.0
        )

        stop_loss = float(getattr(setup, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(setup, "take_profit", 0.0) or 0.0)
        if side == "LONG":
            stop_distance_percent = ((current_price - stop_loss) / current_price * 100.0) if current_price > 0 and stop_loss > 0 else 0.0
            take_distance_percent = ((take_profit - current_price) / current_price * 100.0) if current_price > 0 and take_profit > 0 else 0.0
        elif side == "SHORT":
            stop_distance_percent = ((stop_loss - current_price) / current_price * 100.0) if current_price > 0 and stop_loss > 0 else 0.0
            take_distance_percent = ((current_price - take_profit) / current_price * 100.0) if current_price > 0 and take_profit > 0 else 0.0
        else:
            stop_distance_percent = 0.0
            take_distance_percent = 0.0

        risk_reward = (
            take_distance_percent / max(stop_distance_percent, 1e-9)
            if stop_distance_percent > 0 and take_distance_percent > 0 else 0.0
        )

        confirmation_bias = self._build_trend_bias(confirmation_klines or [])
        execution_bias = self._build_trend_bias(klines)
        same_symbol_sides = [
            str(pos.get("side", "")).upper()
            for pos in open_positions
            if str(pos.get("symbol", "")).upper() == str(symbol or "").upper()
        ]
        opposite_side = "SHORT" if side == "LONG" else "LONG" if side == "SHORT" else ""

        metadata = dict(getattr(setup, "metadata", {}) or {})
        snapshot: Dict[str, Any] = {
            "symbol": str(symbol or "").upper(),
            "strategy_name": str(strategy_name or "primary"),
            "strategy_type": str(strategy_type or "trend_signal"),
            "entry_mode": str(entry_mode or "strong_only"),
            "signal": str(signal_name or ""),
            "side": side,
            "allowed_entry_sides": list(normalized_allowed_sides),
            "execution_timeframe": str(execution_timeframe or ""),
            "confirmation_timeframe": str(confirmation_timeframe or ""),
            "current_price": round(current_price, 8),
            "entry_price": round(float(getattr(setup, "entry_price", current_price) or current_price), 8),
            "stop_distance_percent": round(max(0.0, stop_distance_percent), 6),
            "take_distance_percent": round(max(0.0, take_distance_percent), 6),
            "risk_reward": round(max(0.0, risk_reward), 6),
            "ema_9": round(ema9, 8),
            "ema_21": round(ema21, 8),
            "ema_200": round(ema200, 8),
            "vwap": round(vwap, 8),
            "rsi_14": round(rsi14, 4),
            "atr_percent": round(max(0.0, atr_percent), 6),
            "volume_ratio": round(max(0.0, volume_ratio), 6),
            "dist_from_ema9_percent": round(dist_from_ema9_pct, 6),
            "dist_from_vwap_percent": round(dist_from_vwap_pct, 6),
            "recent_range_percent": round(max(0.0, recent_range_pct), 6),
            "execution_direction": execution_bias.get("direction", "NEUTRAL"),
            "confirmation_direction": confirmation_bias.get("direction", "NEUTRAL"),
            "open_positions_count": len(open_positions),
            "same_symbol_position_count": len(same_symbol_sides),
            "same_symbol_has_long": "LONG" in same_symbol_sides,
            "same_symbol_has_short": "SHORT" in same_symbol_sides,
            "hedge_mode_enabled": True,
            "opposite_side_entry_allowed": True,
            "same_side_entry_blocked": True,
            "same_side_position_open": side in same_symbol_sides if side in {"LONG", "SHORT"} else False,
            "opposite_side_position_open": opposite_side in same_symbol_sides if opposite_side else False,
        }

        if available_balance:
            snapshot["available_balance"] = round(float(available_balance or 0.0), 4)
        if min_notional:
            snapshot["min_notional"] = round(float(min_notional or 0.0), 4)
        if sentiment_snapshot:
            snapshot["sentiment"] = sentiment_snapshot

        if metadata:
            range_fields = {
                "range_support": _to_float(metadata.get("range_support")),
                "range_resistance": _to_float(metadata.get("range_resistance")),
                "range_mid_price": _to_float(metadata.get("range_mid_price")),
                "range_amplitude_pct": _to_float(metadata.get("range_amplitude_pct")),
                "entry_zone": str(metadata.get("entry_zone", "") or ""),
                "position_multiplier": _to_float(metadata.get("position_multiplier")),
            }
            snapshot["setup_metadata"] = {
                key: value for key, value in range_fields.items()
                if value is not None and value != ""
            }
            source_signal = str(metadata.get("source_signal", "") or "").strip().upper()
            if source_signal:
                snapshot["source_signal"] = source_signal
            if metadata.get("ai_override_from_neutral"):
                snapshot["ai_override_from_neutral"] = True
            candidate_reason = str(metadata.get("ai_override_reason", "") or "").strip()
            if candidate_reason:
                snapshot["ai_override_reason"] = candidate_reason[:160]
        else:
            snapshot["setup_metadata"] = {}

        return snapshot

    def evaluate_setup(self, snapshot: Dict[str, Any]) -> ConsultiveReview:
        symbol = str(snapshot.get("symbol", "") or "")
        strategy_name = str(snapshot.get("strategy_name", "primary") or "primary")
        signal_name = str(snapshot.get("signal", "") or "")
        side = str(snapshot.get("side", "") or "")
        cache_key = self._build_cache_key(symbol=symbol, strategy_name=strategy_name, signal=signal_name, side=side)
        now_monotonic = float(self.time_fn())
        cache_ttl = max(0, int(getattr(self.config, "AI_CONSULTIVE_CACHE_SECONDS", 180)))

        if not self.is_enabled():
            return ConsultiveReview(
                status="skipped",
                decision="SKIPPED",
                entry_side="NONE",
                approval=False,
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                reasons=[],
                invalidators=[],
                telegram_summary="IA consultiva desativada.",
                providers=[],
                should_notify=False,
                cache_key=cache_key,
                symbol=symbol,
                strategy_name=strategy_name,
                signal=signal_name,
                side=side,
                mode=str(getattr(self.config, "AI_CONSULTIVE_MODE", "off")),
            )

        cached = self._review_cache.get(cache_key)
        if cached and cache_ttl > 0 and (now_monotonic - float(cached.get("ts", 0.0))) < cache_ttl:
            cached_review = cached.get("review")
            if isinstance(cached_review, ConsultiveReview):
                cached_copy = ConsultiveReview(**cached_review.to_dict())
                cached_copy.providers = [ProviderReview(**provider.to_dict()) for provider in cached_review.providers]
                cached_copy.from_cache = True
                cached_copy.should_notify = False
                return cached_copy

        provider_reviews: List[ProviderReview] = [
            self._call_provider(snapshot)
        ]

        final_review = self._merge_provider_reviews(
            provider_reviews=provider_reviews,
            symbol=symbol,
            strategy_name=strategy_name,
            signal=signal_name,
            side=side,
            cache_key=cache_key,
            allowed_entry_sides=snapshot.get("allowed_entry_sides"),
        )

        self._sanitize_entry_window(final_review, snapshot)

        notify_rejected = bool(getattr(self.config, "AI_CONSULTIVE_NOTIFY_REJECTED", False))
        telegram_enabled = bool(getattr(self.config, "AI_CONSULTIVE_TELEGRAM_ENABLED", True))
        gated_mode = str(getattr(self.config, "AI_CONSULTIVE_MODE", "off")).strip().lower() == "gated"
        final_review.should_notify = bool(
            telegram_enabled and
            final_review.status == "ok" and
            not final_review.from_cache and
            (final_review.decision != "REJECT" or notify_rejected) and
            (not gated_mode or final_review.approval)
        )

        self._review_cache[cache_key] = {"ts": now_monotonic, "review": final_review}
        return final_review

    def build_telegram_message(self, review: ConsultiveReview) -> str:
        decision_text = DECISION_LABELS.get(review.decision, review.decision)
        signal_text = _translate_signal_name(review.signal)
        strategy_text = _translate_strategy_name(review.strategy_name)
        risk_text = _translate_risk_grade(review.risk_grade)
        side_text = _translate_side_name(review.entry_side)

        entry_window = ""
        if review.entry_window_min is not None and review.entry_window_max is not None:
            entry_window = (
                f"\n🎯 <b>Faixa ideal:</b> "
                f"{review.entry_window_min:.6f} - {review.entry_window_max:.6f}"
            )

        wait_line = ""
        if review.wait_seconds > 0:
            wait_line = f"\n⏳ <b>Esperar:</b> {_humanize_wait_seconds(review.wait_seconds)}"

        reasons = "\n".join(
            f"   • {html.escape(_humanize_text_item(reason))}" for reason in review.reasons[:4]
        ) or "   • sem observações"
        invalidators = "\n".join(
            f"   • {html.escape(_humanize_text_item(item))}" for item in review.invalidators[:3]
        ) or "   • sem ponto de atenção"

        mode_text = "Modo consultivo: não bloqueia a execução automática."
        if str(review.mode or "").strip().lower() == "gated":
            mode_text = "Modo com gate: só entra quando a IA aprova o setup."

        timestamp = datetime.now(BRT).strftime("%H:%M:%S")
        return f"""
🤖 <b>IA CONSULTIVA</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

📍 <b>Par:</b> {html.escape(review.symbol.replace("USDT", ""))}/USDT
🤖 <b>Estratégia:</b> {html.escape(strategy_text)}
📊 <b>Sinal:</b> {html.escape(signal_text)}
🧭 <b>Direção sugerida:</b> {html.escape(side_text)}
🧠 <b>Ação sugerida:</b> {html.escape(decision_text)}
📈 <b>Confiança:</b> {review.confidence}/100
⚠️ <b>Risco:</b> {html.escape(risk_text)}
🕒 <b>Momento:</b> {review.timing_score}/10{entry_window}{wait_line}

📝 <b>Resumo:</b> {html.escape(_humanize_summary(review.telegram_summary))}

<b>✅ Por que:</b>
{reasons}

<b>⚠️ Pontos de atenção:</b>
{invalidators}

<i>{html.escape(mode_text)}</i>
━━━━━━━━━━━━━━━━━━━━━
""".strip()

    def _build_trend_bias(self, klines: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not klines:
            return {"direction": "NEUTRAL", "ema_9": 0.0, "ema_21": 0.0, "ema_200": 0.0, "vwap": 0.0}

        closes = [float(item.get("close", 0) or 0) for item in klines]
        highs = [float(item.get("high", 0) or 0) for item in klines]
        lows = [float(item.get("low", 0) or 0) for item in klines]
        volumes = [float(item.get("volume", 0) or 0) for item in klines]
        current_price = closes[-1] if closes else 0.0
        ema9 = float(self.ta.calculate_ema(closes, 9)) if closes else 0.0
        ema21 = float(self.ta.calculate_ema(closes, 21)) if closes else 0.0
        ema200 = float(self.ta.calculate_ema(closes, 200)) if closes else 0.0
        vwap = float(self.ta.calculate_vwap(highs, lows, closes, volumes)) if closes else 0.0

        if current_price > ema200 and ema9 > ema21 and current_price > vwap:
            direction = "LONG"
        elif current_price < ema200 and ema9 < ema21 and current_price < vwap:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        return {
            "direction": direction,
            "ema_9": round(ema9, 8),
            "ema_21": round(ema21, 8),
            "ema_200": round(ema200, 8),
            "vwap": round(vwap, 8),
            "current_price": round(current_price, 8),
        }

    def _build_cache_key(self, *, symbol: str, strategy_name: str, signal: str, side: str) -> str:
        payload = f"{symbol}|{strategy_name}|{signal}|{side}"
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"{symbol}:{strategy_name}:{digest}"

    def _provider_api_key(self) -> str:
        return str(getattr(self.config, "OPENAI_API_KEY", "") or "").strip()

    def _provider_model(self) -> str:
        return str(getattr(self.config, "AI_CONSULTIVE_MODEL", "gpt-5-mini") or "gpt-5-mini").strip()

    def _call_provider(self, snapshot: Dict[str, Any]) -> ProviderReview:
        provider = "openai"
        model = self._provider_model()
        api_key = self._provider_api_key()
        request_payload = self._build_openai_request_payload(model=model, snapshot=snapshot)
        request_preview = _serialize_for_log(request_payload)
        if not api_key:
            return ProviderReview(
                provider=provider,
                model=model,
                status="error",
                decision="REJECT",
                entry_side="NONE",
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                error="api key ausente",
            )

        try:
            parsed_payload, raw_text, response_preview = self._call_openai(
                api_key=api_key,
                payload=request_payload,
            )
        except Exception as exc:
            logger.warning("⚠️ Falha na IA consultiva (%s): %s", provider, exc)
            logger.warning("⚠️ IA consultiva (%s) request: %s", provider, request_preview)
            return ProviderReview(
                provider=provider,
                model=model,
                status="error",
                decision="REJECT",
                entry_side="NONE",
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                error=str(exc),
            )

        refusal_reason = self._extract_refusal_reason(raw_text)
        if refusal_reason:
            logger.warning("⚠️ IA consultiva (%s) request: %s", provider, request_preview)
            if response_preview:
                logger.warning(
                    "⚠️ IA consultiva (%s) response: %s",
                    provider,
                    response_preview[:2000],
                )
            return ProviderReview(
                provider=provider,
                model=model,
                status="ok",
                decision="REJECT",
                entry_side="NONE",
                confidence=0,
                timing_score=0,
                risk_grade="D",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                reasons=["modelo recusou avaliar o setup"],
                invalidators=[refusal_reason[:160]],
                telegram_summary="A IA recusou avaliar este setup e marcou como rejeitado.",
                raw_text=raw_text[:1000],
            )

        if not isinstance(parsed_payload, dict):
            snippet = " ".join(str(raw_text or "").split())[:180]
            error = "resposta estruturada inválida"
            if snippet:
                error = f"{error}: {snippet}"
            logger.warning("⚠️ IA consultiva (%s) request: %s", provider, request_preview)
            if response_preview:
                logger.warning(
                    "⚠️ IA consultiva (%s) response: %s",
                    provider,
                    response_preview[:2000],
                )
            return ProviderReview(
                provider=provider,
                model=model,
                status="error",
                decision="REJECT",
                entry_side="NONE",
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                raw_text=raw_text[:1000],
                error=error,
            )

        normalized = self._normalize_provider_payload(
            provider=provider,
            model=model,
            payload=parsed_payload,
            raw_text=raw_text,
        )
        return normalized

    def _build_openai_request_payload(self, *, model: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        user_prompt = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        return {
            "model": model,
            "max_output_tokens": max(200, int(getattr(self.config, "AI_CONSULTIVE_MAX_OUTPUT_TOKENS", 700))),
            "reasoning": {
                "effort": self._reasoning_effort(),
            },
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "consultive_review",
                    "strict": True,
                    "schema": OPENAI_CONSULTIVE_SCHEMA,
                },
            },
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
        }

    def _call_openai(self, *, api_key: str, payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        response = self.session.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=max(1, int(getattr(self.config, "AI_CONSULTIVE_TIMEOUT_SECONDS", 8))),
        )
        response.raise_for_status()
        data = response.json()
        response_preview = _serialize_for_log(data)
        output_text = self._extract_openai_output_text(data)
        if not output_text:
            return None, "", response_preview

        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            return None, output_text, response_preview

        if not isinstance(parsed, dict):
            return None, output_text, response_preview

        return parsed, output_text, response_preview

    def _extract_openai_output_text(self, data: Dict[str, Any]) -> str:
        output_text = str(data.get("output_text", "") or "").strip()
        if output_text:
            return output_text

        content_parts: List[str] = []
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content_item in item.get("content", []):
                text_value = content_item.get("text", "")
                if isinstance(text_value, str) and text_value.strip():
                    content_parts.append(text_value.strip())
                    continue

                refusal_value = content_item.get("refusal", "")
                if isinstance(refusal_value, str) and refusal_value.strip():
                    content_parts.append(f"refusal: {refusal_value.strip()}")
                    continue

                if content_item:
                    try:
                        content_parts.append(json.dumps(content_item, ensure_ascii=False, separators=(",", ":"))[:1000])
                    except (TypeError, ValueError):
                        content_parts.append(str(content_item)[:1000])
        if content_parts:
            return "\n".join(content_parts).strip()

        try:
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:1000]
        except (TypeError, ValueError):
            return str(data)[:1000]

    def _extract_refusal_reason(self, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        prefix = "refusal:"
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
        return ""

    def _normalize_provider_payload(
        self,
        *,
        provider: str,
        model: str,
        payload: Dict[str, Any],
        raw_text: str,
    ) -> ProviderReview:
        decision = str(payload.get("decision", "REJECT") or "REJECT").strip().upper()
        if decision not in VALID_DECISIONS:
            decision = "REJECT"
        entry_side = str(payload.get("entry_side", "NONE") or "NONE").strip().upper()
        if entry_side not in VALID_ENTRY_SIDES:
            entry_side = "NONE"

        confidence = _clamp_int(payload.get("confidence"), 0, 100, 0)
        timing_score = _clamp_int(payload.get("timing_score"), 0, 10, 0)
        risk_grade = str(payload.get("risk_grade", "C") or "C").strip().upper()
        if risk_grade not in VALID_RISK_GRADES:
            risk_grade = "C"

        telegram_summary = str(payload.get("telegram_summary", "") or "").strip()
        if not telegram_summary:
            if decision == "ENTER_NOW":
                telegram_summary = "Entrada aprovada pela IA."
            elif decision == "WAIT_PULLBACK":
                telegram_summary = "Melhor aguardar um pullback antes da entrada."
            else:
                telegram_summary = "Setup rejeitado pela IA consultiva."

        return ProviderReview(
            provider=provider,
            model=model,
            status="ok",
            decision=decision,
            entry_side=entry_side,
            confidence=confidence,
            timing_score=timing_score,
            risk_grade=risk_grade,
            entry_window_min=_to_float(payload.get("entry_window_min")),
            entry_window_max=_to_float(payload.get("entry_window_max")),
            wait_seconds=max(0, _clamp_int(payload.get("wait_seconds"), 0, 86400, 0)),
            reasons=_dedupe_text_items(payload.get("reasons"), limit=4),
            invalidators=_dedupe_text_items(payload.get("invalidators"), limit=3),
            telegram_summary=telegram_summary[:220],
            raw_text=raw_text[:1000],
        )

    def _sanitize_entry_window(
        self,
        review: ConsultiveReview,
        snapshot: Dict[str, Any],
    ) -> None:
        """
        Anula entry_window_min/max quando a IA devolve lixo (placeholders
        tipo 0-300, janelas largas demais, ou current_price fora da faixa).
        Muta `review` in-place.

        Regras:
        - Se min ou max não forem números positivos finitos → anula.
        - Se min >= max → anula.
        - Em REJECT, a janela não faz sentido → anula.
        - Se current_price estiver fora de [min, max] com folga de 0.2 × ATR,
          é placeholder → anula.
        - Se largura (max - min) > 2 × ATR × current_price / 100 → anula.
        """
        min_val = review.entry_window_min
        max_val = review.entry_window_max
        if min_val is None and max_val is None:
            return

        current_price = float(snapshot.get("current_price") or 0.0)
        atr_pct = float(snapshot.get("atr_percent") or 0.0)

        def _drop(reason: str) -> None:
            logger.info(
                "🩹 entry_window descartada (%s): min=%s max=%s price=%s atr%%=%s",
                reason, min_val, max_val, current_price, atr_pct,
            )
            review.entry_window_min = None
            review.entry_window_max = None

        if not (isinstance(min_val, (int, float)) and isinstance(max_val, (int, float))):
            _drop("valores não numéricos")
            return
        if min_val <= 0 or max_val <= 0:
            _drop("valor não-positivo")
            return
        if min_val >= max_val:
            _drop("min >= max")
            return
        if review.decision == "REJECT":
            _drop("decisão é REJECT")
            return

        # Daqui pra frente, sanity depende do preço atual e do ATR.
        if current_price <= 0 or atr_pct <= 0:
            return  # sem como validar; deixa passar.

        atr_abs = current_price * (atr_pct / 100.0)
        tolerance = 0.2 * atr_abs
        if current_price < (min_val - tolerance) or current_price > (max_val + tolerance):
            _drop("current_price fora da janela")
            return

        width = max_val - min_val
        if width > 2.0 * atr_abs:
            _drop("janela larga demais")
            return

    def _merge_provider_reviews(
        self,
        *,
        provider_reviews: List[ProviderReview],
        symbol: str,
        strategy_name: str,
        signal: str,
        side: str,
        cache_key: str,
        allowed_entry_sides: Any = None,
    ) -> ConsultiveReview:
        valid_reviews = [review for review in provider_reviews if review.status == "ok"]
        if not valid_reviews:
            error_details = "; ".join(
                f"{review.provider}: {review.error or 'resposta inválida'}"
                for review in provider_reviews
            ) or "nenhum provider retornou resposta válida"
            return ConsultiveReview(
                status="error",
                decision="SKIPPED",
                entry_side="NONE",
                approval=False,
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                reasons=[],
                invalidators=[],
                telegram_summary="IA consultiva indisponível no momento.",
                providers=list(provider_reviews),
                should_notify=False,
                cache_key=cache_key,
                symbol=symbol,
                strategy_name=strategy_name,
                signal=signal,
                side=side,
                mode=str(getattr(self.config, "AI_CONSULTIVE_MODE", "off")),
                error=error_details,
            )

        chosen = valid_reviews[0]
        final_decision = chosen.decision
        final_entry_side = chosen.entry_side
        final_confidence = chosen.confidence
        final_timing = chosen.timing_score
        final_risk = chosen.risk_grade
        entry_window_min = chosen.entry_window_min
        entry_window_max = chosen.entry_window_max
        wait_seconds = chosen.wait_seconds
        reasons = chosen.reasons
        invalidators = chosen.invalidators
        summary = chosen.telegram_summary

        min_confidence = _clamp_int(
            getattr(self.config, "AI_CONSULTIVE_MIN_CONFIDENCE", 80),
            0,
            100,
            80,
        )
        allowed_sides = [
            item
            for item in [str(value or "").strip().upper() for value in (allowed_entry_sides or [])]
            if item in {"LONG", "SHORT"}
        ]
        approval = (
            final_decision == "ENTER_NOW"
            and final_confidence >= min_confidence
            and final_entry_side in allowed_sides
        )

        return ConsultiveReview(
            status="ok",
            decision=final_decision,
            entry_side=final_entry_side,
            approval=approval,
            confidence=final_confidence,
            timing_score=final_timing,
            risk_grade=final_risk,
            entry_window_min=entry_window_min,
            entry_window_max=entry_window_max,
            wait_seconds=wait_seconds,
            reasons=reasons,
            invalidators=invalidators,
            telegram_summary=summary,
            providers=list(provider_reviews),
            should_notify=False,
            cache_key=cache_key,
            symbol=symbol,
            strategy_name=strategy_name,
            signal=signal,
            side=side,
            mode=str(getattr(self.config, "AI_CONSULTIVE_MODE", "off")),
        )
