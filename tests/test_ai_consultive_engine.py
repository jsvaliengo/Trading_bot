import json
from types import SimpleNamespace

from trading_bot.ai.consultive_engine import (
    ConsultiveEngine,
    ConsultiveReview,
    OPENAI_CONSULTIVE_SCHEMA,
    ProviderReview,
)


def _make_config(**overrides):
    base = {
        "AI_CONSULTIVE_MODE": "consultive",
        "AI_CONSULTIVE_MODEL": "gpt-5-mini",
        "AI_CONSULTIVE_TIMEOUT_SECONDS": 8,
        "AI_CONSULTIVE_MAX_OUTPUT_TOKENS": 700,
        "AI_CONSULTIVE_REASONING_EFFORT": "low",
        "AI_CONSULTIVE_CACHE_SECONDS": 180,
        "AI_CONSULTIVE_MIN_CONFIDENCE": 80,
        "AI_CONSULTIVE_NOTIFY_REJECTED": False,
        "AI_CONSULTIVE_TELEGRAM_ENABLED": True,
        "OPENAI_API_KEY": "openai-key",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_snapshot():
    return {
        "symbol": "ETHUSDT",
        "strategy_name": "trend_strong",
        "signal": "STRONG_BUY",
        "side": "LONG",
        "allowed_entry_sides": ["LONG"],
        "current_price": 2500.0,
    }


def _provider_review(provider: str, decision: str, confidence: int) -> ProviderReview:
    return ProviderReview(
        provider=provider,
        model="gpt-5-mini",
        status="ok",
        decision=decision,
        entry_side="LONG",
        confidence=confidence,
        timing_score=8,
        risk_grade="B",
        entry_window_min=2498.0,
        entry_window_max=2501.0,
        wait_seconds=90 if decision == "WAIT_PULLBACK" else 0,
        reasons=["setup alinhado"],
        invalidators=["perda da EMA 21"],
        telegram_summary="Parecer consultivo.",
    )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
                "timeout": timeout,
            }
        )
        return _FakeResponse(self.payload)


def test_consultive_engine_skips_when_mode_is_off():
    engine = ConsultiveEngine(config_obj=_make_config(AI_CONSULTIVE_MODE="off"))

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "skipped"
    assert review.decision == "SKIPPED"
    assert review.should_notify is False


def test_consultive_engine_is_enabled_for_gated_mode():
    engine = ConsultiveEngine(config_obj=_make_config(AI_CONSULTIVE_MODE="gated"))

    assert engine.is_enabled() is True


def test_consultive_engine_uses_single_openai_provider(monkeypatch):
    engine = ConsultiveEngine(config_obj=_make_config())
    calls = []

    def _fake_call(snapshot):
        calls.append(snapshot["symbol"])
        return _provider_review("openai", "WAIT_PULLBACK", 64)

    monkeypatch.setattr(engine, "_call_provider", _fake_call)

    review = engine.evaluate_setup(_make_snapshot())

    assert calls == ["ETHUSDT"]
    assert review.status == "ok"
    assert review.decision == "WAIT_PULLBACK"
    assert review.should_notify is True


def test_consultive_engine_reuses_cache_for_same_setup(monkeypatch):
    monotonic_values = iter([10.0, 20.0])
    engine = ConsultiveEngine(
        config_obj=_make_config(AI_CONSULTIVE_CACHE_SECONDS=180),
        time_fn=lambda: next(monotonic_values),
    )
    calls = []

    def _fake_call(snapshot):
        calls.append(snapshot["symbol"])
        return _provider_review("openai", "ENTER_NOW", 88)

    monkeypatch.setattr(engine, "_call_provider", _fake_call)

    first = engine.evaluate_setup(_make_snapshot())
    second = engine.evaluate_setup(_make_snapshot())

    assert first.decision == "ENTER_NOW"
    assert second.from_cache is True
    assert second.should_notify is False
    assert calls == ["ETHUSDT"]


def test_consultive_engine_gated_review_below_confidence_does_not_notify(monkeypatch):
    engine = ConsultiveEngine(config_obj=_make_config(AI_CONSULTIVE_MODE="gated", AI_CONSULTIVE_MIN_CONFIDENCE=80))

    monkeypatch.setattr(
        engine,
        "_call_provider",
        lambda _snapshot: _provider_review("openai", "ENTER_NOW", 65),
    )

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "ok"
    assert review.decision == "ENTER_NOW"
    assert review.approval is False
    assert review.should_notify is False


def test_consultive_engine_gated_review_above_confidence_notifies(monkeypatch):
    engine = ConsultiveEngine(config_obj=_make_config(AI_CONSULTIVE_MODE="gated", AI_CONSULTIVE_MIN_CONFIDENCE=80))

    monkeypatch.setattr(
        engine,
        "_call_provider",
        lambda _snapshot: _provider_review("openai", "ENTER_NOW", 82),
    )

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "ok"
    assert review.decision == "ENTER_NOW"
    assert review.approval is True
    assert review.should_notify is True


def test_consultive_engine_requests_openai_with_strict_json_schema():
    session = _FakeSession(
        {
            "output_text": json.dumps(
                {
                    "decision": "WAIT_PULLBACK",
                    "entry_side": "LONG",
                    "confidence": 74,
                    "timing_score": 6,
                    "risk_grade": "B",
                    "entry_window_min": 2498.0,
                    "entry_window_max": 2501.0,
                    "wait_seconds": 120,
                    "reasons": ["pullback ainda incompleto"],
                    "invalidators": ["perda da EMA 21"],
                    "telegram_summary": "Melhor aguardar o pullback.",
                }
            )
        }
    )
    engine = ConsultiveEngine(config_obj=_make_config(), session=session)

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "ok"
    assert review.decision == "WAIT_PULLBACK"
    assert review.entry_side == "LONG"
    assert len(session.calls) == 1
    request_payload = session.calls[0]["json"]
    assert request_payload["model"] == "gpt-5-mini"
    assert request_payload["max_output_tokens"] == 700
    assert request_payload["reasoning"]["effort"] == "low"
    assert request_payload["text"]["verbosity"] == "low"
    assert request_payload["text"]["format"]["type"] == "json_schema"
    assert request_payload["text"]["format"]["name"] == "consultive_review"
    assert request_payload["text"]["format"]["strict"] is True
    assert request_payload["text"]["format"]["schema"] == OPENAI_CONSULTIVE_SCHEMA
    system_text = request_payload["input"][0]["content"][0]["text"]
    assert "opposite_side_entry_allowed=true" in system_text


def test_consultive_engine_returns_provider_error_when_structured_response_is_invalid(caplog):
    session = _FakeSession({"output_text": "nao eh json"})
    engine = ConsultiveEngine(config_obj=_make_config(), session=session)

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "error"
    assert review.decision == "SKIPPED"
    assert "openai: resposta estruturada inválida" in review.error
    assert review.providers[0].status == "error"
    assert "resposta estruturada inválida" in review.providers[0].error
    assert review.providers[0].raw_text == "nao eh json"
    assert 'IA consultiva (openai) request:' in caplog.text
    assert 'IA consultiva (openai) response:' in caplog.text


def test_consultive_engine_treats_refusal_as_consultive_reject(caplog):
    session = _FakeSession(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "refusal",
                            "refusal": "Nao posso ajudar com isso do jeito solicitado.",
                        }
                    ],
                }
            ]
        }
    )
    engine = ConsultiveEngine(config_obj=_make_config(), session=session)

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "ok"
    assert review.decision == "REJECT"
    assert review.risk_grade == "D"
    assert review.reasons == ["modelo recusou avaliar o setup"]
    assert review.invalidators == ["Nao posso ajudar com isso do jeito solicitado."]
    assert review.telegram_summary == "A IA recusou avaliar este setup e marcou como rejeitado."
    assert "refusal: Nao posso ajudar com isso do jeito solicitado." in review.providers[0].raw_text
    assert 'IA consultiva (openai) request:' in caplog.text
    assert 'IA consultiva (openai) response:' in caplog.text


def test_consultive_review_compact_for_trade_is_serializable():
    review = ConsultiveReview(
        status="ok",
        decision="ENTER_NOW",
        entry_side="LONG",
        approval=True,
        confidence=88,
        timing_score=8,
        risk_grade="A",
        entry_window_min=100.0,
        entry_window_max=101.0,
        wait_seconds=0,
        reasons=["confluencia forte"],
        invalidators=["rompimento da VWAP"],
        telegram_summary="Entrada aprovada.",
        providers=[_provider_review("openai", "ENTER_NOW", 88)],
        symbol="ETHUSDT",
        strategy_name="trend_strong",
        signal="STRONG_BUY",
        side="LONG",
        mode="consultive",
    )

    payload = review.compact_for_trade()

    assert payload["decision"] == "ENTER_NOW"
    assert payload["entry_side"] == "LONG"
    assert payload["confidence"] == 88
    assert payload["providers"] == ["openai"]


def test_build_telegram_message_is_clear_and_translated():
    engine = ConsultiveEngine(config_obj=_make_config())
    review = ConsultiveReview(
        status="ok",
        decision="WAIT_PULLBACK",
        entry_side="LONG",
        approval=False,
        confidence=60,
        timing_score=5,
        risk_grade="B",
        entry_window_min=1.2345,
        entry_window_max=1.236,
        wait_seconds=180,
        reasons=[
            "Volume abaixo da média reduz a força da entrada.",
            "low_volume",
        ],
        invalidators=[
            "volume_ratio_below_1",
            "insufficient_momentum",
        ],
        telegram_summary="WAIT: Trend bullish but volume weak and pullback is preferable before entering.",
        providers=[_provider_review("openai", "WAIT_PULLBACK", 60)],
        symbol="XRPUSDT",
        strategy_name="trend_strong",
        signal="STRONG_BUY",
        side="LONG",
        mode="consultive",
    )

    message = engine.build_telegram_message(review)

    assert "Estratégia:</b> Tendência forte" in message
    assert "Sinal:</b> Compra forte" in message
    assert "Direção sugerida:</b> Compra" in message
    assert "Ação sugerida:</b> Aguardar correção" in message
    assert "Risco:</b> Moderado (B)" in message
    assert "Momento:</b> 5/10" in message
    assert "Esperar:</b> 3 min" in message
    assert "Por que:</b>" in message
    assert "Pontos de atenção:</b>" in message
    assert "Volume abaixo da média" in message
    assert "Momentum fraco" in message
    assert "Aguardar: Tendência altista mas volume fraco e correção é preferível antes de entrar." in message
    assert "Providers:" not in message


def test_build_telegram_message_mentions_gated_mode():
    engine = ConsultiveEngine(config_obj=_make_config(AI_CONSULTIVE_MODE="gated"))
    review = ConsultiveReview(
        status="ok",
        decision="ENTER_NOW",
        entry_side="LONG",
        approval=True,
        confidence=82,
        timing_score=8,
        risk_grade="B",
        entry_window_min=1.0,
        entry_window_max=1.1,
        wait_seconds=0,
        reasons=["setup alinhado"],
        invalidators=["perda da EMA 21"],
        telegram_summary="Entrada aprovada.",
        providers=[_provider_review("openai", "ENTER_NOW", 82)],
        symbol="ETHUSDT",
        strategy_name="trend_strong",
        signal="STRONG_BUY",
        side="LONG",
        mode="gated",
    )

    message = engine.build_telegram_message(review)

    assert "Modo com gate" in message


def test_build_market_snapshot_marks_opposite_side_entry_as_allowed_in_hedge_mode():
    engine = ConsultiveEngine(config_obj=_make_config())
    setup = SimpleNamespace(
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        metadata={},
    )
    klines = [
        {"close": "100", "high": "101", "low": "99", "volume": "10"},
        {"close": "101", "high": "102", "low": "100", "volume": "12"},
        {"close": "102", "high": "103", "low": "101", "volume": "14"},
    ] * 80
    open_positions = [
        {"symbol": "SOLUSDT", "side": "SHORT", "quantity": 1, "entry_price": 100},
        {"symbol": "ETHUSDT", "side": "LONG", "quantity": 1, "entry_price": 2000},
    ]

    snapshot = engine.build_market_snapshot(
        symbol="SOLUSDT",
        strategy_name="trend_strong",
        strategy_type="trend_signal",
        entry_mode="strong_only",
        signal_name="STRONG_BUY",
        setup=setup,
        klines=klines,
        confirmation_klines=klines,
        execution_timeframe="3m",
        confirmation_timeframe="5m",
        available_balance=100.0,
        open_positions=open_positions,
        should_open_long=True,
        should_open_short=False,
        min_notional=5.0,
        sentiment_snapshot=None,
    )

    assert snapshot["hedge_mode_enabled"] is True
    assert snapshot["opposite_side_entry_allowed"] is True
    assert snapshot["same_side_entry_blocked"] is True
    assert snapshot["same_side_position_open"] is False
    assert snapshot["opposite_side_position_open"] is True
    assert snapshot["same_symbol_has_short"] is True


def test_build_market_snapshot_defaults_side_to_none_when_not_requested():
    engine = ConsultiveEngine(config_obj=_make_config())
    setup = SimpleNamespace(
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        metadata={},
    )
    klines = [
        {"close": "100", "high": "101", "low": "99", "volume": "10"},
        {"close": "101", "high": "102", "low": "100", "volume": "12"},
        {"close": "102", "high": "103", "low": "101", "volume": "14"},
    ] * 80

    snapshot = engine.build_market_snapshot(
        symbol="SOLUSDT",
        strategy_name="trend_strong",
        strategy_type="trend_signal",
        entry_mode="strong_only",
        signal_name="NEUTRAL",
        setup=setup,
        klines=klines,
        confirmation_klines=klines,
        execution_timeframe="3m",
        confirmation_timeframe="5m",
        available_balance=100.0,
        open_positions=[],
        should_open_long=False,
        should_open_short=False,
        min_notional=5.0,
        sentiment_snapshot=None,
        allowed_entry_sides=[],
    )

    assert snapshot["side"] == "NONE"
    assert snapshot["allowed_entry_sides"] == []


def test_build_market_snapshot_includes_timing_features():
    """Features de 'quão esticada está a entrada' devem vir no payload."""
    engine = ConsultiveEngine(config_obj=_make_config())
    setup = SimpleNamespace(
        entry_price=100.0, stop_loss=99.0, take_profit=102.0, metadata={}
    )
    # Tendência de alta consistente — preço anda entre 98 e 105, última vela em 105.
    klines = [
        {"close": "98", "high": "99", "low": "97", "volume": "10"},
        {"close": "100", "high": "101", "low": "99", "volume": "12"},
        {"close": "102", "high": "103", "low": "101", "volume": "14"},
    ] * 80 + [
        {"close": "105", "high": "105.5", "low": "104.5", "volume": "20"},
    ]

    snapshot = engine.build_market_snapshot(
        symbol="ETHUSDT",
        strategy_name="trend_strong",
        strategy_type="trend_signal",
        entry_mode="strong_only",
        signal_name="STRONG_BUY",
        setup=setup,
        klines=klines,
        confirmation_klines=klines,
        execution_timeframe="3m",
        confirmation_timeframe="5m",
        available_balance=100.0,
        open_positions=[],
        should_open_long=True,
        should_open_short=False,
        min_notional=5.0,
        sentiment_snapshot=None,
    )

    # current_price = 105, EMAs giram em torno de 100 -> dist positiva.
    assert "dist_from_ema9_percent" in snapshot
    assert "dist_from_vwap_percent" in snapshot
    assert "recent_range_percent" in snapshot
    assert snapshot["dist_from_ema9_percent"] > 0
    assert snapshot["recent_range_percent"] > 0


def _make_review(**kwargs) -> ConsultiveReview:
    base = dict(
        status="ok",
        decision="ENTER_NOW",
        entry_side="LONG",
        approval=True,
        confidence=85,
        timing_score=8,
        risk_grade="B",
        entry_window_min=None,
        entry_window_max=None,
        wait_seconds=0,
        reasons=["ok"],
        invalidators=["ok"],
        telegram_summary="ok",
        providers=[],
        should_notify=False,
        cache_key="k",
        symbol="ETHUSDT",
        strategy_name="trend_strong",
        signal="STRONG_BUY",
        side="LONG",
        mode="consultive",
    )
    base.update(kwargs)
    return ConsultiveReview(**base)


def test_sanitize_entry_window_drops_nonpositive_placeholder():
    """O caso da tela: IA devolve 0-300 pra SOL a $85 → deve virar null/null."""
    engine = ConsultiveEngine(config_obj=_make_config())
    review = _make_review(entry_window_min=0.0, entry_window_max=300.0)
    snapshot = {"current_price": 85.57, "atr_percent": 0.5}

    engine._sanitize_entry_window(review, snapshot)

    assert review.entry_window_min is None
    assert review.entry_window_max is None


def test_sanitize_entry_window_drops_when_current_price_outside():
    engine = ConsultiveEngine(config_obj=_make_config())
    review = _make_review(entry_window_min=200.0, entry_window_max=210.0)
    snapshot = {"current_price": 100.0, "atr_percent": 0.5}

    engine._sanitize_entry_window(review, snapshot)

    assert review.entry_window_min is None
    assert review.entry_window_max is None


def test_sanitize_entry_window_drops_when_width_too_large():
    engine = ConsultiveEngine(config_obj=_make_config())
    # ATR 0.5% de 100 = $0.50. 2 × ATR = $1. Largura $5 é grande demais.
    review = _make_review(entry_window_min=98.0, entry_window_max=103.0)
    snapshot = {"current_price": 100.0, "atr_percent": 0.5}

    engine._sanitize_entry_window(review, snapshot)

    assert review.entry_window_min is None
    assert review.entry_window_max is None


def test_sanitize_entry_window_keeps_valid_tight_window():
    engine = ConsultiveEngine(config_obj=_make_config())
    # Janela de $0.20 em cima de $100 com ATR 0.5% (~$0.50) — válida.
    review = _make_review(entry_window_min=99.90, entry_window_max=100.10)
    snapshot = {"current_price": 100.0, "atr_percent": 0.5}

    engine._sanitize_entry_window(review, snapshot)

    assert review.entry_window_min == 99.90
    assert review.entry_window_max == 100.10


def test_sanitize_entry_window_drops_on_reject_decision():
    engine = ConsultiveEngine(config_obj=_make_config())
    review = _make_review(
        decision="REJECT",
        entry_window_min=99.90,
        entry_window_max=100.10,
    )
    snapshot = {"current_price": 100.0, "atr_percent": 0.5}

    engine._sanitize_entry_window(review, snapshot)

    assert review.entry_window_min is None
    assert review.entry_window_max is None


def test_sanitize_entry_window_noop_when_already_null():
    engine = ConsultiveEngine(config_obj=_make_config())
    review = _make_review(entry_window_min=None, entry_window_max=None)
    snapshot = {"current_price": 100.0, "atr_percent": 0.5}

    engine._sanitize_entry_window(review, snapshot)

    assert review.entry_window_min is None
    assert review.entry_window_max is None
