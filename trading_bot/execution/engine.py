"""
ExecutionEngine — orquestra fechamento de posições (normal, bulk, emergência).

Mantém referência ao bot por pragmatismo: as rotinas de close tocam em vários
atributos internos (stats, estratégia, known_positions, telegram) e decouplar
completamente exigiria PR muito maior. Aqui separamos o CÓDIGO da god class
mas o ACOPLAMENTO de dados permanece — próxima iteração pode limpar as
dependências via callbacks/interfaces se necessário.

Responsabilidades:
- Abertura de posição direcional com checagens de risco (exposição, concentração,
  idade do sinal, minNotional, double_first)
- Fechar uma posição com cálculo de P&L real + notificação Telegram + stats
- Fechamento em massa ao atingir meta diária
- Checagem e execução de Stop Loss Global (emergência)

Mantém fora de escopo (ainda no bot):
- Análise/geração de sinais (strategy.generate_trade_setup + analyze_and_trade)
- Lógica de risco dinâmico por tick (trailing stop, SL individual, monitor loop)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.config import config
from ..observability import metrics

if TYPE_CHECKING:
    from ..core.bot import TradingBot

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Orquestrador de fechamento e emergências, opera sobre o TradingBot."""

    def __init__(self, bot: "TradingBot"):
        self._bot = bot

    # ------------------------------------------------------------------
    # Abertura de posição direcional
    # ------------------------------------------------------------------

    def open_signal_trade(
        self,
        setup,
        open_long: bool = False,
        open_short: bool = False,
        strategy_name: str = "primary",
    ) -> bool:
        """
        Executa um trade baseado no sinal (direcional).

        ESTRATÉGIA DIRECIONAL:
        - open_long=True → Abre apenas LONG
        - open_short=True → Abre apenas SHORT
        - Nunca abre ambos ao mesmo tempo (diferente do hedge)

        1. Abre posição na direção do sinal
        2. Configura SL/TP
        3. Registra no histórico
        """
        bot = self._bot
        symbol = setup.symbol
        signal_name = setup.signal.name if hasattr(setup.signal, 'name') else str(setup.signal)
        setup_metadata = dict(getattr(setup, "metadata", {}) or {})
        requested_side = "LONG" if open_long else "SHORT" if open_short else "NONE"

        # Curto-circuito: símbolo em cooldown estrutural não tenta abrir
        # nem chama funding/preço/info. A IA já avaliou e aprovou; o motivo
        # do bloqueio é puramente operacional (limite de leverage/tier).
        cooldown_info = bot.exchange.get_symbol_cooldown_info(symbol)
        if cooldown_info is not None and requested_side in ("LONG", "SHORT"):
            remaining_min = max(1, int(cooldown_info["remaining_seconds"] / 60))
            logger.info(
                f"⏳ {symbol} em cooldown estrutural — entrada {requested_side} cancelada "
                f"(code={cooldown_info['code']}, {remaining_min}min restantes)"
            )
            bot.block_reporter.notify_blocked(
                symbol=symbol,
                side=requested_side,
                strategy_name=strategy_name,
                reason="Cooldown estrutural ativo",
                detail=(
                    f"Exchange rejeitou ordens anteriores (code {cooldown_info['code']}). "
                    f"Reentrada bloqueada por ~{remaining_min}min."
                ),
                setup_metadata=setup_metadata,
            )
            return False

        def _safe_float(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        strategy_type = bot._normalize_strategy_type(
            setup_metadata.get("strategy_type", "trend_signal")
        )
        custom_stop_loss = _safe_float(setup_metadata.get("custom_stop_loss", setup.stop_loss))
        custom_take_profit = _safe_float(setup_metadata.get("custom_take_profit", setup.take_profit))
        range_mid_price = _safe_float(setup_metadata.get("range_mid_price"))
        # Trailing dinâmico computado pela strategy a partir do ATR no momento
        # do setup. None aqui significa "usar config global no _check_trailing_stop".
        trailing_activation_pct = _safe_float(setup_metadata.get("trailing_activation_pct"))
        trailing_distance_pct = _safe_float(setup_metadata.get("trailing_distance_pct"))
        exchange_stop_loss_enabled = bool(getattr(config, "USE_INDIVIDUAL_STOP_LOSS", False))
        if not exchange_stop_loss_enabled:
            custom_stop_loss = None

        # Log do funding rate (apenas informativo)
        if config.CHECK_FUNDING_RATE:
            funding_info = bot.exchange.get_funding_rate(symbol)
            funding_rate = funding_info['rate_percent']
            if funding_rate > 0:
                logger.info(f"📊 Funding {symbol}: {funding_rate:+.4f}% (LONGs pagam)")
            elif funding_rate < 0:
                logger.info(f"📊 Funding {symbol}: {funding_rate:+.4f}% (SHORTs pagam)")
            else:
                logger.info(f"📊 Funding {symbol}: neutro")

        # Log da ação
        if open_long:
            logger.info(f"🚀 Sinal {signal_name} → Abrindo LONG em {symbol}")
        elif open_short:
            logger.info(f"🚀 Sinal {signal_name} → Abrindo SHORT em {symbol}")
        else:
            logger.info(f"⏸️  Nada a fazer em {symbol}")
            return False

        try:
            # Calcula quantidades
            price = bot.exchange.get_symbol_price(symbol)
            if price <= 0:
                logger.error(f"❌ Preço inválido para {symbol}: {price} — abortando abertura")
                bot.block_reporter.notify_blocked(
                    symbol=symbol,
                    side=requested_side,
                    strategy_name=strategy_name,
                    reason="Preço inválido",
                    detail=f"Preço retornado pela exchange: {price}",
                    setup_metadata=setup_metadata,
                )
                return False
            info = bot.exchange.get_symbol_info(symbol)

            # Tamanho mínimo (minNotional) vindo da Binance
            min_notional = float(info.get('minNotional', 5.0))

            # Alavancagem usada no cálculo de qty (order_size aqui é MARGEM em USDT)
            try:
                leverage = float(config.LEVERAGE)
            except Exception:
                leverage = 1.0

            # Garante que o notional efetivo (margem * alavancagem) respeite o mínimo
            # Buffer de 5% para evitar cair abaixo do mínimo por arredondamento/variação
            min_margin_needed = (min_notional / max(leverage, 1e-9)) * 1.05

            # ============================================
            # DETERMINA O TAMANHO DA ORDEM
            # ============================================
            # Se usando estratégia Binance, usa o order_size da faixa
            if config.USE_BINANCE_STRATEGY and hasattr(bot, 'binance_strategy') and bot.binance_strategy:
                order_size = bot.binance_strategy['order_size']
                logger.info(f"💵 Usando Order Size da Estratégia Binance: ${order_size}")
            else:
                # Usa o tamanho do setup (cálculo antigo)
                order_size = setup.long_size if open_long else setup.short_size

            base_order_size = float(order_size)
            trade_side = "LONG" if open_long else "SHORT"

            # Ajuste automático do order_size (margem) para cumprir minNotional
            if order_size < min_margin_needed:
                logger.info(
                    f"🔧 Ajustando order_size para respeitar minNotional em {symbol}: "
                    f"${order_size:.2f} → ${min_margin_needed:.2f} "
                    f"(minNotional ${min_notional:.2f}, {leverage:g}x)"
                )
                order_size = min_margin_needed

            order_size, double_first_applied, double_first_state_key = bot.double_first_policy.try_double(
                symbol=symbol,
                side=trade_side,
                order_size=order_size,
            )

            # Improvement 7: verifica idade do sinal antes de executar
            _signal_ts = setup_metadata.get("signal_timestamp")
            if _signal_ts is not None:
                try:
                    _signal_age = (datetime.now() - _signal_ts).total_seconds()
                    _max_age = float(getattr(config, "MAX_SIGNAL_AGE_SECONDS", 120.0))
                    if _signal_age > _max_age:
                        logger.info(
                            f"⏱️ Sinal expirado para {symbol}: {_signal_age:.0f}s > {_max_age:.0f}s — pulando"
                        )
                        bot.block_reporter.notify_blocked(
                            symbol=symbol,
                            side=requested_side,
                            strategy_name=strategy_name,
                            reason="Sinal expirado",
                            detail=f"Idade {int(_signal_age)}s acima do máximo de {int(_max_age)}s",
                            setup_metadata=setup_metadata,
                        )
                        return False
                except Exception:
                    pass

            # Improvement 4: verifica exposição total
            _notional_pct = bot._get_total_open_notional_percent()
            _max_notional = float(getattr(config, "MAX_TOTAL_NOTIONAL_PERCENT", 80.0))
            if _notional_pct >= _max_notional:
                logger.warning(
                    f"⚠️ Exposição total {_notional_pct:.1f}% excede limite {_max_notional:.0f}%"
                )
                bot.block_reporter.notify_blocked(
                    symbol=symbol,
                    side=requested_side,
                    strategy_name=strategy_name,
                    reason="Exposição total excedida",
                    detail=f"{_notional_pct:.1f}% acima do limite de {_max_notional:.0f}%",
                    setup_metadata=setup_metadata,
                )
                return False

            # Improvement 10: verifica concentração individual da posição (baseado em margem)
            try:
                _balance_for_conc = bot.exchange.get_account_balance()
                if _balance_for_conc > 0:
                    # Compara margem (order_size) contra saldo, não notional.
                    # Notional = order_size * leverage seria sempre alto e bloquearia trades normais.
                    _conc_pct = (order_size / _balance_for_conc) * 100
                    _max_conc = float(getattr(config, "MAX_POSITION_CONCENTRATION_PERCENT", 15.0))
                    if _conc_pct > _max_conc:
                        logger.warning(
                            f"⚠️ {symbol}: Margem da posição {_conc_pct:.1f}% do saldo "
                            f"excede limite {_max_conc:.0f}%"
                        )
                        bot.block_reporter.notify_blocked(
                            symbol=symbol,
                            side=requested_side,
                            strategy_name=strategy_name,
                            reason="Concentração da posição excedida",
                            detail=f"Margem { _conc_pct:.1f}% do saldo acima do limite de {_max_conc:.0f}%",
                            setup_metadata=setup_metadata,
                        )
                        return False
            except Exception:
                pass

            # ============================================
            # ABRE LONG (quando sinal de entrada direciona para compra)
            # ============================================
            if open_long:
                # Verifica se atende ao mínimo (minNotional é NOTIONAL; order_size é MARGEM)
                effective_notional = order_size * leverage
                if effective_notional < min_notional:
                    logger.warning(f"⚠️  Posição LONG muito pequena para {symbol}")
                    logger.warning(
                        f"   Mínimo: ${min_notional:.2f}, Notional: ${effective_notional:.2f} "
                        f"(Order Size: ${order_size:.2f} x {leverage:g}x)"
                    )
                    bot.block_reporter.notify_blocked(
                        symbol=symbol,
                        side="LONG",
                        strategy_name=strategy_name,
                        reason="Posição abaixo do mínimo da Binance",
                        detail=f"Notional ${effective_notional:.2f} abaixo do mínimo de ${min_notional:.2f}",
                        setup_metadata=setup_metadata,
                    )
                    return False

                long_qty = (order_size * config.LEVERAGE) / price

                logger.info(f"📈 Abrindo LONG: {long_qty:.4f} {symbol} @ ${price:.4f}")
                long_order = bot.exchange.place_market_order(
                    symbol=symbol,
                    side='BUY',
                    position_side='LONG',
                    quantity=long_qty
                )
                metrics.record_order(side="LONG", success=bool(long_order))

                if not long_order:
                    logger.error("❌ Falha ao abrir posição LONG")
                    post_cooldown = bot.exchange.get_symbol_cooldown_info(symbol)
                    if post_cooldown is not None:
                        remaining_min = max(1, int(post_cooldown["remaining_seconds"] / 60))
                        block_reason = "Cooldown estrutural ativado"
                        block_detail = (
                            f"Exchange rejeitou a ordem (code {post_cooldown['code']}). "
                            f"Reentrada em {symbol} bloqueada por ~{remaining_min}min."
                        )
                    else:
                        block_reason = "Falha ao abrir posição LONG"
                        block_detail = "A exchange rejeitou ou não retornou a ordem de mercado."
                    bot.block_reporter.notify_blocked(
                        symbol=symbol,
                        side="LONG",
                        strategy_name=strategy_name,
                        reason=block_reason,
                        detail=block_detail,
                        setup_metadata=setup_metadata,
                    )
                    return False

                if double_first_applied:
                    bot.double_first_policy.mark_used(
                        state_key=double_first_state_key,
                        symbol=symbol,
                        side="LONG",
                        base_order_size=base_order_size,
                        applied_order_size=order_size,
                    )

                # Define SL fixo na Binance somente quando o SL individual está ativo.
                sl_price_long = None
                if exchange_stop_loss_enabled:
                    if custom_stop_loss and custom_stop_loss > 0:
                        sl_price_long = custom_stop_loss
                    else:
                        _sl_pct = float(getattr(config, "STOP_LOSS_PERCENT", 3.0))
                        sl_price_long = round(price * (1 - _sl_pct / 100), info.get('pricePrecision', 4))
                bot.exchange.set_stop_loss_take_profit(
                    symbol=symbol,
                    position_side='LONG',
                    stop_loss_price=sl_price_long,
                    take_profit_price=custom_take_profit if custom_take_profit else setup.take_profit
                )

                # Notifica no Telegram
                bot.telegram.send_trade_alert(
                    symbol=symbol,
                    action="OPEN_LONG",
                    price=price,
                    quantity=long_qty,
                    strategy_name=strategy_name
                )

                bot.ledger.record_trade_opened(
                    symbol=symbol,
                    signal=signal_name,
                    side="LONG",
                    quantity=long_qty,
                    order_size=order_size,
                    entry_price=price,
                    stop_loss=custom_stop_loss,
                    take_profit=custom_take_profit if custom_take_profit else setup.take_profit,
                    strategy_name=strategy_name,
                    strategy_type=strategy_type,
                    double_first=double_first_applied,
                    ai_consultive=setup_metadata.get("ai_consultive"),
                )

                bot.positions.open(
                    symbol=symbol,
                    side="LONG",
                    entry_price=price,
                    quantity=long_qty,
                    strategy_name=strategy_name,
                    strategy_type=strategy_type,
                    custom_stop_loss=custom_stop_loss,
                    custom_take_profit=custom_take_profit,
                    range_mid_price=range_mid_price,
                    range_entry_side="LONG",
                    trailing_activation_pct=trailing_activation_pct,
                    trailing_distance_pct=trailing_distance_pct,
                )

                logger.info("✅ LONG aberto com sucesso!")
                logger.info(f"   {long_qty:.4f} {symbol} @ ${price:.4f}")
                if double_first_applied:
                    logger.info(
                        f"   Order Size: ${order_size} (double first aplicado sobre ${base_order_size:.2f}) | "
                        f"TP: ${(custom_take_profit if custom_take_profit else setup.take_profit):.4f}"
                    )
                else:
                    logger.info(
                        f"   Order Size: ${order_size} | "
                        f"TP: ${(custom_take_profit if custom_take_profit else setup.take_profit):.4f}"
                    )

                if getattr(bot, "dashboard_server", None):
                    bot.dashboard_server.emit_position_opened({
                        "symbol": symbol,
                        "side": "LONG",
                        "entry_price": price,
                        "quantity": long_qty,
                        "strategy_name": str(strategy_name or "primary"),
                        "strategy_type": strategy_type,
                    })

                return True

            # ============================================
            # ABRE SHORT (quando sinal de entrada direciona para venda)
            # ============================================
            if open_short:
                # Verifica se atende ao mínimo (minNotional é NOTIONAL; order_size é MARGEM)
                effective_notional = order_size * leverage
                if effective_notional < min_notional:
                    logger.warning(f"⚠️  Posição SHORT muito pequena para {symbol}")
                    logger.warning(
                        f"   Mínimo: ${min_notional:.2f}, Notional: ${effective_notional:.2f} "
                        f"(Order Size: ${order_size:.2f} x {leverage:g}x)"
                    )
                    bot.block_reporter.notify_blocked(
                        symbol=symbol,
                        side="SHORT",
                        strategy_name=strategy_name,
                        reason="Posição abaixo do mínimo da Binance",
                        detail=f"Notional ${effective_notional:.2f} abaixo do mínimo de ${min_notional:.2f}",
                        setup_metadata=setup_metadata,
                    )
                    return False

                short_qty = (order_size * config.LEVERAGE) / price

                logger.info(f"📉 Abrindo SHORT: {short_qty:.4f} {symbol} @ ${price:.4f}")
                short_order = bot.exchange.place_market_order(
                    symbol=symbol,
                    side='SELL',
                    position_side='SHORT',
                    quantity=short_qty
                )
                metrics.record_order(side="SHORT", success=bool(short_order))

                if not short_order:
                    logger.error("❌ Falha ao abrir posição SHORT")
                    post_cooldown = bot.exchange.get_symbol_cooldown_info(symbol)
                    if post_cooldown is not None:
                        remaining_min = max(1, int(post_cooldown["remaining_seconds"] / 60))
                        block_reason = "Cooldown estrutural ativado"
                        block_detail = (
                            f"Exchange rejeitou a ordem (code {post_cooldown['code']}). "
                            f"Reentrada em {symbol} bloqueada por ~{remaining_min}min."
                        )
                    else:
                        block_reason = "Falha ao abrir posição SHORT"
                        block_detail = "A exchange rejeitou ou não retornou a ordem de mercado."
                    bot.block_reporter.notify_blocked(
                        symbol=symbol,
                        side="SHORT",
                        strategy_name=strategy_name,
                        reason=block_reason,
                        detail=block_detail,
                        setup_metadata=setup_metadata,
                    )
                    return False

                if double_first_applied:
                    bot.double_first_policy.mark_used(
                        state_key=double_first_state_key,
                        symbol=symbol,
                        side="SHORT",
                        base_order_size=base_order_size,
                        applied_order_size=order_size,
                    )

                # Define TP para o SHORT e SL fixo apenas quando habilitado.
                short_tp = custom_take_profit if custom_take_profit else setup.take_profit
                if short_tp is None or short_tp <= 0:
                    short_tp = price * (1 - config.TAKE_PROFIT_PERCENT / 100)
                sl_price_short = None
                if exchange_stop_loss_enabled:
                    if custom_stop_loss and custom_stop_loss > 0:
                        sl_price_short = custom_stop_loss
                    else:
                        _sl_pct = float(getattr(config, "STOP_LOSS_PERCENT", 3.0))
                        sl_price_short = round(price * (1 + _sl_pct / 100), info.get('pricePrecision', 4))
                bot.exchange.set_stop_loss_take_profit(
                    symbol=symbol,
                    position_side='SHORT',
                    stop_loss_price=sl_price_short,
                    take_profit_price=short_tp
                )

                # Notifica no Telegram
                bot.telegram.send_trade_alert(
                    symbol=symbol,
                    action="OPEN_SHORT",
                    price=price,
                    quantity=short_qty,
                    strategy_name=strategy_name
                )

                bot.ledger.record_trade_opened(
                    symbol=symbol,
                    signal=signal_name,
                    side="SHORT",
                    quantity=short_qty,
                    order_size=order_size,
                    entry_price=price,
                    stop_loss=custom_stop_loss,
                    take_profit=short_tp,
                    strategy_name=strategy_name,
                    strategy_type=strategy_type,
                    double_first=double_first_applied,
                    ai_consultive=setup_metadata.get("ai_consultive"),
                )

                bot.positions.open(
                    symbol=symbol,
                    side="SHORT",
                    entry_price=price,
                    quantity=short_qty,
                    strategy_name=strategy_name,
                    strategy_type=strategy_type,
                    custom_stop_loss=custom_stop_loss,
                    custom_take_profit=short_tp,
                    range_mid_price=range_mid_price,
                    range_entry_side="SHORT",
                    trailing_activation_pct=trailing_activation_pct,
                    trailing_distance_pct=trailing_distance_pct,
                )

                logger.info("✅ SHORT aberto com sucesso!")
                logger.info(f"   {short_qty:.4f} {symbol} @ ${price:.4f}")
                if double_first_applied:
                    logger.info(
                        f"   Order Size: ${order_size} (double first aplicado sobre ${base_order_size:.2f}) | "
                        f"TP: ${short_tp:.4f}"
                    )
                else:
                    logger.info(f"   Order Size: ${order_size} | TP: ${short_tp:.4f}")

                if getattr(bot, "dashboard_server", None):
                    bot.dashboard_server.emit_position_opened({
                        "symbol": symbol,
                        "side": "SHORT",
                        "entry_price": price,
                        "quantity": short_qty,
                        "strategy_name": str(strategy_name or "primary"),
                        "strategy_type": strategy_type,
                    })

                return True

            return False

        except Exception as e:
            logger.error(f"❌ Erro ao executar trade: {e}")
            bot.block_reporter.notify_blocked(
                symbol=symbol,
                side=requested_side,
                strategy_name=strategy_name,
                reason="Erro ao executar trade",
                detail=str(e),
                setup_metadata=setup_metadata,
            )
            return False

    # ------------------------------------------------------------------
    # Fechamento individual (com cálculo de P&L e notificação)
    # ------------------------------------------------------------------

    def close_position_with_notification(self, pos: dict, reason: str) -> bool:
        """
        Fecha uma posição e envia notificação com P&L líquido.
        Também atualiza o contador de trades fechados e o P&L diário.

        IMPORTANTE: P&L calculado com base nos preços REAIS de entrada/saída,
        não no unrealized_pnl que pode estar desatualizado.

        Returns:
            True se o fechamento foi confirmado; False caso contrário.
        """
        bot = self._bot
        symbol = pos['symbol']
        side = pos['side']
        entry_price = pos['entry_price']
        quantity = pos['quantity']
        position_key = f"{symbol}_{side}"
        pos_meta = bot.positions.get(position_key)
        strategy_name = pos_meta.get('strategy_name')
        if not strategy_name:
            profile = bot._resolve_strategy_context(symbol)
            strategy_name = profile.get('name', 'primary')
        logger.info(f"🚨 Fechando posição: {reason}")

        # Pega o preço atual ANTES de fechar (será o preço de saída aproximado)
        current_price = bot.exchange.get_current_price(symbol)

        # Fecha a posição e só contabiliza se o fechamento for confirmado
        try:
            close_success = bot.exchange.close_position(symbol, side)
        except Exception as e:
            logger.error(f"❌ Exceção ao fechar posição {side} {symbol}: {e}")
            return False

        if not close_success:
            logger.error(
                f"❌ Falha ao fechar posição {side} {symbol}. "
                "Nenhuma estatística/P&L será contabilizada."
            )
            return False

        # ============================================
        # CALCULA P&L REAL BASEADO NOS PREÇOS
        # ============================================
        notional_value = entry_price * quantity

        if side == 'LONG':
            price_change_pct = (current_price - entry_price) / entry_price
        else:
            price_change_pct = (entry_price - current_price) / entry_price

        # P&L bruto = variação × valor nocional (quantidade já alavancada)
        pnl_gross = price_change_pct * notional_value

        taker_fee_rate = bot.get_taker_fee_rate()
        fee_open = entry_price * quantity * taker_fee_rate
        fee_close = current_price * quantity * taker_fee_rate
        total_fees = fee_open + fee_close
        pnl_net = pnl_gross - total_fees

        logger.info("📊 Cálculo P&L:")
        logger.info(f"   Entrada: ${entry_price:.4f} | Saída: ${current_price:.4f}")
        logger.info(f"   Quantidade: {quantity:.6f} | Nocional: ${notional_value:.2f}")
        logger.info(f"   Variação: {price_change_pct*100:.2f}% | P&L Bruto: ${pnl_gross:.4f}")

        # Risk manager continua direto — é lógica de risco, não bookkeeping.
        bot.risk_manager.update_pnl(pnl_net)

        # Bookkeeping de stats encapsulado em TradeLedger (counters, dicts
        # por símbolo/estratégia, métrica Prometheus). Retorna resumo pra log.
        ledger_summary = bot.ledger.record_trade_closed(
            symbol=symbol,
            strategy_name=strategy_name,
            pnl_net=pnl_net,
            total_fees=total_fees,
            close_reason=reason,
            side=side,
            entry_price=entry_price,
            exit_price=current_price,
            pnl_gross=pnl_gross,
        )

        logger.info(
            f"💰 P&L Bruto: ${pnl_gross:.4f} | Taxas: ${total_fees:.4f} | "
            f"P&L Líquido: ${pnl_net:.4f}"
        )
        logger.info(
            f"📊 Trade #{ledger_summary['closed_trades_count']} | "
            f"Win Rate: {ledger_summary['win_rate']:.1f}% | "
            f"P&L Diário: ${ledger_summary['daily_pnl']:.2f}"
        )

        telegram_sent = bot.telegram.send_position_closed(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=current_price,
            quantity=quantity,
            pnl_gross=pnl_gross,
            fees=total_fees,
            pnl_net=pnl_net,
            reason=reason,
            strategy_name=strategy_name,
        )

        if telegram_sent:
            logger.info(
                f"✅ Notificação Telegram enviada para trade #{bot.closed_trades_count}"
            )
        else:
            logger.error(
                f"❌ FALHA ao enviar notificação Telegram para trade #{bot.closed_trades_count}"
            )

        if getattr(bot, "dashboard_server", None):
            bot.dashboard_server.emit_position_closed({
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "exit_price": current_price,
                "quantity": quantity,
                "pnl_gross": pnl_gross,
                "pnl_net": pnl_net,
                "fees": total_fees,
                "reason": reason,
                "strategy_name": strategy_name,
            })

        return True

    # ------------------------------------------------------------------
    # Fechamento em massa — meta diária
    # ------------------------------------------------------------------

    def close_all_for_daily_target(self, reason: str) -> None:
        """
        Fecha TODAS as posições abertas quando a meta diária é atingida.
        Não atualiza stats aqui — é fechamento "bruto" pra realizar P&L.
        """
        bot = self._bot
        try:
            # force_refresh: fechamento em massa não pode agir em snapshot stale
            positions = bot.exchange.get_open_positions(force_refresh=True)
        except Exception as exc:
            logger.error(
                f"❌ API indisponível ao tentar fechar posições para meta diária: {exc}. "
                f"Tarefa abortada — será retentada no próximo tick."
            )
            return

        if not positions:
            logger.info("📭 Nenhuma posição aberta para fechar")
            return

        logger.info(f"🔒 Fechando {len(positions)} posições - Motivo: {reason}")

        for pos in positions:
            symbol = pos['symbol']
            side = pos['side']

            try:
                logger.info(f"   Fechando {side} {symbol}...")
                bot.exchange.close_position(symbol, side)

                bot.positions.close(f"{symbol}_{side}")
            except Exception as e:
                logger.error(f"   ❌ Erro ao fechar {side} {symbol}: {e}")

        logger.info(f"✅ Posições fechadas - {reason}")

    # ------------------------------------------------------------------
    # Stop Loss Global
    # ------------------------------------------------------------------

    def check_global_stop_loss(self) -> bool:
        """
        Verifica se o Stop Loss Global foi atingido.
        Usa dados REAIS da Binance para calcular o P&L total.
        """
        bot = self._bot
        account_info = bot.exchange.get_account_info()
        total_unrealized = account_info['unrealized_pnl']

        daily_pnl = bot.exchange.get_daily_pnl_from_binance()
        total_pnl = daily_pnl['total'] + total_unrealized

        try:
            initial_capital = float(bot.initial_capital or 0.0)
        except (TypeError, ValueError):
            initial_capital = 0.0

        if initial_capital <= 0:
            logger.warning(
                "⚠️ Stop Loss Global desativado neste ciclo: "
                f"initial_capital inválido ({bot.initial_capital})."
            )
            return False

        loss_percent = abs(total_pnl / initial_capital * 100) if total_pnl < 0 else 0

        if loss_percent >= config.GLOBAL_STOP_LOSS_PERCENT:
            logger.warning(f"🚨 STOP LOSS GLOBAL ATINGIDO! Perda: {loss_percent:.1f}%")
            return True

        return False

    def execute_global_stop_loss(self) -> None:
        """
        Executa o Stop Loss Global: fecha todas as posições, notifica e para o bot.
        """
        bot = self._bot
        logger.warning("=" * 60)
        logger.warning("🚨🚨🚨 EXECUTANDO STOP LOSS GLOBAL 🚨🚨🚨")
        logger.warning("=" * 60)

        try:
            # force_refresh: SL global é evento de emergência; snapshot precisa ser fresco
            positions = bot.exchange.get_open_positions(force_refresh=True)
            account_info = bot.exchange.get_account_info()
        except Exception as exc:
            # API falhou na emergência — NÃO paramos o bot por conta de falha
            # transitória. Loga, retorna; próximo tick tenta de novo.
            logger.error(
                f"❌ API indisponível durante Global Stop Loss: {exc}. "
                "Mantendo posições abertas — será retentado no próximo ciclo."
            )
            return

        current_balance = account_info['wallet_balance']
        total_unrealized = account_info['unrealized_pnl']

        daily_pnl = bot.exchange.get_daily_pnl_from_binance()
        total_pnl = daily_pnl['total'] + total_unrealized

        try:
            initial_capital = float(bot.initial_capital or 0.0)
        except (TypeError, ValueError):
            initial_capital = 0.0

        if initial_capital <= 0:
            logger.warning(
                "⚠️ initial_capital inválido durante Stop Loss Global. "
                "Usando saldo atual como base de referência para cálculo de perda."
            )
            initial_capital = max(1.0, float(current_balance))

        loss_percent = abs(total_pnl / initial_capital * 100) if total_pnl < 0 else 0

        for pos in positions:
            logger.warning(f"Fechando {pos['side']} {pos['symbol']}...")
            closed = self.close_position_with_notification(pos, "Stop Loss Global")
            if not closed:
                logger.error(
                    f"❌ Não foi possível confirmar fechamento de {pos['side']} {pos['symbol']} "
                    "durante Stop Loss Global."
                )

        bot.telegram.send_global_stop_loss_alert(
            initial_capital=initial_capital,
            current_balance=current_balance,
            total_pnl=total_pnl,
            loss_percent=loss_percent,
        )

        logger.info("💾 Salvando estado...")
        bot.save_state()

        logger.warning("🛑 Bot encerrado pelo Stop Loss Global")
        bot.running = False
