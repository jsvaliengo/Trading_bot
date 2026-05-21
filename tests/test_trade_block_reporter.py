"""
Testes do TradeBlockReporter (Phase 4 — segundo pedaço).

Cobre os early-returns que o reporter herdou do método antigo, mais o
cooldown anti-flood e a formatação da mensagem Telegram.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


from trading_bot.core.trade_block_reporter import TradeBlockReporter


def _make_telegram(send_returns: bool = True) -> SimpleNamespace:
    return SimpleNamespace(send_message=MagicMock(return_value=send_returns))


def _make_config(ai_mode: str = "gated") -> SimpleNamespace:
    return SimpleNamespace(AI_CONSULTIVE_MODE=ai_mode)


def _gated_metadata(**overrides) -> dict:
    base = {
        "ai_consultive": {
            "approval": True,
            "confidence": 85,
            "decision": "ENTER_NOW",
        }
    }
    base["ai_consultive"].update(overrides)
    return base


# ---------- Early returns ----------


def test_skips_when_telegram_unavailable():
    reporter = TradeBlockReporter(lambda: None, _make_config())
    result = reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="x", setup_metadata=_gated_metadata(),
    )
    assert result is False


def test_skips_when_ai_mode_is_off():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config(ai_mode="off"))
    result = reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="x", setup_metadata=_gated_metadata(),
    )
    assert result is False
    telegram.send_message.assert_not_called()


def test_skips_when_ai_mode_is_advisory():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config(ai_mode="advisory"))
    reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="x", setup_metadata=_gated_metadata(),
    )
    telegram.send_message.assert_not_called()


def test_skips_when_ai_did_not_approve():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    metadata = {"ai_consultive": {"approval": False, "confidence": 85, "decision": "VETO"}}
    result = reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="x", setup_metadata=metadata,
    )
    assert result is False
    telegram.send_message.assert_not_called()


def test_skips_when_setup_metadata_missing():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    result = reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="x", setup_metadata=None,
    )
    assert result is False


# ---------- Happy path + formatação ----------


def test_sends_telegram_message_on_valid_gated_block():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    result = reporter.notify_blocked(
        symbol="XRPUSDT", side="SHORT", strategy_name="trend_strong",
        reason="Exposição total excedida",
        detail="450.0% acima do limite de 300%",
        setup_metadata=_gated_metadata(confidence=92),
    )
    assert result is True
    telegram.send_message.assert_called_once()
    message = telegram.send_message.call_args.args[0]
    assert "ENTRADA CANCELADA" in message
    assert "Exposição total excedida" in message
    assert "ENTER_NOW (92/100)" in message
    assert "Detalhe" in message
    assert "Venda" in message  # SHORT → label "Venda"


def test_side_label_long_renders_as_compra():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="r", setup_metadata=_gated_metadata(),
    )
    message = telegram.send_message.call_args.args[0]
    assert "Compra" in message


def test_strategy_name_underscores_normalized_in_label():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="range_scalp_v1",
        reason="r", setup_metadata=_gated_metadata(),
    )
    message = telegram.send_message.call_args.args[0]
    assert "range scalp v1" in message


def test_html_special_chars_escaped_in_reason_and_detail():
    """HTML injection via reason/detail não vaza: <script> vira &lt;script&gt;."""
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    reporter.notify_blocked(
        symbol="BTCUSDT", side="LONG", strategy_name="trend",
        reason="<script>x</script>",
        detail="Limit: <b>foo</b> & bar",
        setup_metadata=_gated_metadata(),
    )
    message = telegram.send_message.call_args.args[0]
    assert "<script>x</script>" not in message
    assert "&lt;script&gt;" in message
    assert "&amp;" in message


# ---------- Cooldown anti-flood ----------


def test_cooldown_suppresses_same_block_within_180s():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    metadata = _gated_metadata()
    # 1ª chamada: envia
    r1 = reporter.notify_blocked(
        symbol="X", side="LONG", strategy_name="s", reason="exposure", setup_metadata=metadata
    )
    # 2ª chamada imediata: suprimida pelo cooldown
    r2 = reporter.notify_blocked(
        symbol="X", side="LONG", strategy_name="s", reason="exposure", setup_metadata=metadata
    )
    assert r1 is True
    assert r2 is False
    telegram.send_message.assert_called_once()


def test_cooldown_does_not_suppress_different_symbol():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    metadata = _gated_metadata()
    reporter.notify_blocked(symbol="BTCUSDT", side="LONG", strategy_name="s", reason="r", setup_metadata=metadata)
    reporter.notify_blocked(symbol="ETHUSDT", side="LONG", strategy_name="s", reason="r", setup_metadata=metadata)
    assert telegram.send_message.call_count == 2


def test_cooldown_does_not_suppress_different_reason():
    telegram = _make_telegram()
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    metadata = _gated_metadata()
    reporter.notify_blocked(symbol="BTCUSDT", side="LONG", strategy_name="s", reason="A", setup_metadata=metadata)
    reporter.notify_blocked(symbol="BTCUSDT", side="LONG", strategy_name="s", reason="B", setup_metadata=metadata)
    assert telegram.send_message.call_count == 2


# ---------- Telegram lookup é tardio (provider callable) ----------


def test_telegram_provider_resolves_at_call_time():
    """telegram_provider é callable — captura o telegram CORRENTE no notify."""
    telegram_box = {"client": None}  # começa None
    reporter = TradeBlockReporter(lambda: telegram_box["client"], _make_config())

    # Sem telegram, skipa
    r0 = reporter.notify_blocked(
        symbol="X", side="LONG", strategy_name="s", reason="r", setup_metadata=_gated_metadata()
    )
    assert r0 is False

    # Bot seta o telegram depois — próxima chamada usa o cliente novo
    telegram = _make_telegram()
    telegram_box["client"] = telegram
    r1 = reporter.notify_blocked(
        symbol="X", side="LONG", strategy_name="s", reason="r", setup_metadata=_gated_metadata()
    )
    assert r1 is True


def test_telegram_send_exception_is_caught_and_logged():
    telegram = SimpleNamespace(send_message=MagicMock(side_effect=RuntimeError("network down")))
    reporter = TradeBlockReporter(lambda: telegram, _make_config())
    # Não deve propagar — bug em telegram não pode derrubar o trading
    result = reporter.notify_blocked(
        symbol="X", side="LONG", strategy_name="s", reason="r", setup_metadata=_gated_metadata()
    )
    assert result is False
