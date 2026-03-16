from types import SimpleNamespace

from trading_bot.ai.consultive_engine import ConsultiveEngine, ConsultiveReview, ProviderReview


def _make_config(**overrides):
    base = {
        "AI_CONSULTIVE_MODE": "consultive",
        "AI_CONSULTIVE_MODEL": "gpt-5-mini",
        "AI_CONSULTIVE_TIMEOUT_SECONDS": 8,
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
        "current_price": 2500.0,
    }


def _provider_review(provider: str, decision: str, confidence: int) -> ProviderReview:
    return ProviderReview(
        provider=provider,
        model="gpt-5-mini",
        status="ok",
        decision=decision,
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


def test_consultive_engine_skips_when_mode_is_off():
    engine = ConsultiveEngine(config_obj=_make_config(AI_CONSULTIVE_MODE="off"))

    review = engine.evaluate_setup(_make_snapshot())

    assert review.status == "skipped"
    assert review.decision == "SKIPPED"
    assert review.should_notify is False


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


def test_consultive_review_compact_for_trade_is_serializable():
    review = ConsultiveReview(
        status="ok",
        decision="ENTER_NOW",
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
    assert payload["confidence"] == 88
    assert payload["providers"] == ["openai"]
