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

SYSTEM_PROMPT = """
Você é um revisor consultivo de setups de trading futures.

Sua função:
- avaliar apenas o setup recebido
- ser conservador quando houver dúvida
- jamais inventar indicadores ou contexto não presentes no payload
- preferir WAIT_PULLBACK ou REJECT quando o timing estiver atrasado

Responda SOMENTE um JSON válido com estas chaves:
- decision: ENTER_NOW | WAIT_PULLBACK | REJECT
- confidence: inteiro de 0 a 100
- timing_score: inteiro de 0 a 10
- risk_grade: A | B | C | D
- entry_window_min: número ou null
- entry_window_max: número ou null
- wait_seconds: inteiro >= 0
- reasons: lista de 1 a 4 strings curtas
- invalidators: lista de 1 a 3 strings curtas
- telegram_summary: string curta em português

Regras:
- se o setup estiver inconsistente, atrasado ou fraco, não aprove
- se faltarem dados suficientes, use REJECT
- não use markdown
- não use texto fora do JSON
""".strip()

OPENAI_CONSULTIVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": sorted(VALID_DECISIONS),
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


@dataclass
class ProviderReview:
    provider: str
    model: str
    status: str
    decision: str
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
        return str(getattr(self.config, "AI_CONSULTIVE_MODE", "off")).strip().lower() == "consultive"

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
    ) -> Dict[str, Any]:
        side = "LONG" if should_open_long and not should_open_short else "SHORT"
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

        stop_loss = float(getattr(setup, "stop_loss", 0.0) or 0.0)
        take_profit = float(getattr(setup, "take_profit", 0.0) or 0.0)
        if side == "LONG":
            stop_distance_percent = ((current_price - stop_loss) / current_price * 100.0) if current_price > 0 and stop_loss > 0 else 0.0
            take_distance_percent = ((take_profit - current_price) / current_price * 100.0) if current_price > 0 and take_profit > 0 else 0.0
        else:
            stop_distance_percent = ((stop_loss - current_price) / current_price * 100.0) if current_price > 0 and stop_loss > 0 else 0.0
            take_distance_percent = ((current_price - take_profit) / current_price * 100.0) if current_price > 0 and take_profit > 0 else 0.0

        risk_reward = (
            take_distance_percent / max(stop_distance_percent, 1e-9)
            if stop_distance_percent > 0 and take_distance_percent > 0 else 0.0
        )

        confirmation_bias = self._build_trend_bias(confirmation_klines or [])
        execution_bias = self._build_trend_bias(klines)
        same_symbol_positions = [
            {
                "side": str(pos.get("side", "")).upper(),
                "quantity": float(pos.get("quantity", 0) or 0),
                "entry_price": float(pos.get("entry_price", 0) or 0),
            }
            for pos in open_positions
            if str(pos.get("symbol", "")).upper() == str(symbol or "").upper()
        ]

        metadata = dict(getattr(setup, "metadata", {}) or {})
        snapshot: Dict[str, Any] = {
            "symbol": str(symbol or "").upper(),
            "strategy_name": str(strategy_name or "primary"),
            "strategy_type": str(strategy_type or "trend_signal"),
            "entry_mode": str(entry_mode or "strong_only"),
            "signal": str(signal_name or ""),
            "side": side,
            "execution_timeframe": str(execution_timeframe or ""),
            "confirmation_timeframe": str(confirmation_timeframe or ""),
            "current_price": round(current_price, 8),
            "entry_price": round(float(getattr(setup, "entry_price", current_price) or current_price), 8),
            "stop_loss": round(stop_loss, 8),
            "take_profit": round(take_profit, 8),
            "stop_distance_percent": round(max(0.0, stop_distance_percent), 6),
            "take_distance_percent": round(max(0.0, take_distance_percent), 6),
            "risk_reward": round(max(0.0, risk_reward), 6),
            "available_balance": round(float(available_balance or 0.0), 4),
            "min_notional": round(float(min_notional or 0.0), 4),
            "ema_9": round(ema9, 8),
            "ema_21": round(ema21, 8),
            "ema_200": round(ema200, 8),
            "vwap": round(vwap, 8),
            "rsi_14": round(rsi14, 4),
            "atr_percent": round(max(0.0, atr_percent), 6),
            "volume_ratio": round(max(0.0, volume_ratio), 6),
            "volume_current": round(current_volume, 4),
            "volume_avg_20": round(avg_volume, 4),
            "execution_bias": execution_bias,
            "confirmation_bias": confirmation_bias,
            "same_symbol_positions": same_symbol_positions,
            "open_positions_count": len(open_positions),
            "position_size_usdt": round(float(getattr(setup, "long_size", 0.0) if side == "LONG" else getattr(setup, "short_size", 0.0) or 0.0), 8),
            "sentiment": sentiment_snapshot or {},
        }

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
        )

        notify_rejected = bool(getattr(self.config, "AI_CONSULTIVE_NOTIFY_REJECTED", False))
        telegram_enabled = bool(getattr(self.config, "AI_CONSULTIVE_TELEGRAM_ENABLED", True))
        final_review.should_notify = bool(
            telegram_enabled and
            final_review.status == "ok" and
            not final_review.from_cache and
            (final_review.decision != "REJECT" or notify_rejected)
        )

        self._review_cache[cache_key] = {"ts": now_monotonic, "review": final_review}
        return final_review

    def build_telegram_message(self, review: ConsultiveReview) -> str:
        side_label = "LONG" if review.side == "LONG" else "SHORT"
        decision_labels = {
            "ENTER_NOW": "ENTRAR AGORA",
            "WAIT_PULLBACK": "AGUARDAR PULLBACK",
            "REJECT": "REJEITAR",
        }
        decision_text = decision_labels.get(review.decision, review.decision)
        provider_names = ", ".join(
            f"{provider.provider}:{provider.model}"
            for provider in review.providers
            if provider.status == "ok"
        ) or "sem provider válido"

        entry_window = ""
        if review.entry_window_min is not None and review.entry_window_max is not None:
            entry_window = (
                f"\n🎯 <b>Janela:</b> "
                f"{review.entry_window_min:.6f} - {review.entry_window_max:.6f}"
            )

        wait_line = ""
        if review.wait_seconds > 0:
            wait_line = f"\n⏳ <b>Esperar:</b> {int(review.wait_seconds)}s"

        reasons = "\n".join(
            f"   • {html.escape(reason)}" for reason in review.reasons[:4]
        ) or "   • sem observações"
        invalidators = "\n".join(
            f"   • {html.escape(item)}" for item in review.invalidators[:3]
        ) or "   • sem invalidação explícita"

        timestamp = datetime.now(BRT).strftime("%H:%M:%S")
        return f"""
🤖 <b>IA CONSULTIVA</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

📍 <b>Par:</b> {html.escape(review.symbol.replace("USDT", ""))}/USDT
🤖 <b>Estratégia:</b> {html.escape(review.strategy_name)}
📊 <b>Base:</b> {html.escape(review.signal)} → {side_label}
🧠 <b>Decisão:</b> {decision_text}
📈 <b>Confiança:</b> {review.confidence}/100
⚠️ <b>Risco:</b> {review.risk_grade}
🕒 <b>Timing:</b> {review.timing_score}/10{entry_window}{wait_line}

📝 <b>Resumo:</b> {html.escape(review.telegram_summary)}

<b>✅ Motivos:</b>
{reasons}

<b>🧱 Invalidação:</b>
{invalidators}

🔌 <b>Providers:</b> {html.escape(provider_names)}
<i>Modo consultivo: não bloqueia a execução automática.</i>
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
        if not api_key:
            return ProviderReview(
                provider=provider,
                model=model,
                status="error",
                decision="REJECT",
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                error="api key ausente",
            )

        try:
            parsed_payload, raw_text = self._call_openai(
                model=model,
                api_key=api_key,
                snapshot=snapshot,
            )
        except Exception as exc:
            logger.warning("⚠️ Falha na IA consultiva (%s): %s", provider, exc)
            return ProviderReview(
                provider=provider,
                model=model,
                status="error",
                decision="REJECT",
                confidence=0,
                timing_score=0,
                risk_grade="C",
                entry_window_min=None,
                entry_window_max=None,
                wait_seconds=0,
                error=str(exc),
            )

        if not isinstance(parsed_payload, dict):
            snippet = " ".join(str(raw_text or "").split())[:180]
            error = "resposta estruturada inválida"
            if snippet:
                error = f"{error}: {snippet}"
            return ProviderReview(
                provider=provider,
                model=model,
                status="error",
                decision="REJECT",
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

    def _call_openai(self, *, model: str, api_key: str, snapshot: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        user_prompt = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        payload = {
            "model": model,
            "max_output_tokens": 260,
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
        response = self.session.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=max(1, int(getattr(self.config, "AI_CONSULTIVE_TIMEOUT_SECONDS", 8))),
        )
        response.raise_for_status()
        data = response.json()
        output_text = self._extract_openai_output_text(data)
        if not output_text:
            return None, ""

        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            return None, output_text

        if not isinstance(parsed, dict):
            return None, output_text

        return parsed, output_text

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
        return "\n".join(content_parts).strip()

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

    def _merge_provider_reviews(
        self,
        *,
        provider_reviews: List[ProviderReview],
        symbol: str,
        strategy_name: str,
        signal: str,
        side: str,
        cache_key: str,
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
        approval = final_decision == "ENTER_NOW" and final_confidence >= min_confidence

        return ConsultiveReview(
            status="ok",
            decision=final_decision,
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
