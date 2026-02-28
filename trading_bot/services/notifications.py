"""
SISTEMA DE NOTIFICAÇÕES - TELEGRAM
==================================
Envia atualizações do bot para o seu Telegram.

COMO CONFIGURAR:
1. Abra o Telegram e procure por @BotFather
2. Envie /newbot e siga as instruções para criar seu bot
3. Copie o TOKEN que o BotFather te dar
4. Procure seu bot pelo nome e envie qualquer mensagem para ele
5. Acesse: https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
6. Procure pelo "chat":{"id": XXXXXXXX} - esse é seu CHAT_ID
7. Coloque o TOKEN e CHAT_ID no config.py
"""

import logging
import requests
from typing import Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Timezone do Brasil (UTC-3)
BRT = timezone(timedelta(hours=-3))

def get_brt_timestamp() -> str:
    """Retorna o timestamp atual no horário do Brasil (BRT)."""
    return datetime.now(BRT).strftime("%H:%M:%S")


class TelegramNotifier:
    """
    Classe para enviar notificações via Telegram.
    """
    
    def __init__(self, token: str, chat_id: str, enabled: bool = True):
        """
        Inicializa o notificador do Telegram.
        
        Args:
            token: Token do bot (obtido do @BotFather)
            chat_id: ID do chat para enviar mensagens
            enabled: Se as notificações estão ativas
        """
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.usd_brl_rate = 5.0
        self.last_rate_update = None
        
        if self.enabled and self.token and self.chat_id:
            self._test_connection()
        elif self.enabled:
            logger.warning("⚠️  Telegram habilitado mas TOKEN ou CHAT_ID não configurados")
            self.enabled = False

    def _get_usd_brl_rate(self) -> float:
        """
        Retorna a cotação USD->BRL com cache de 10 minutos.
        Usa a última cotação conhecida em caso de falha.
        """
        now_utc = datetime.now(timezone.utc)

        if self.last_rate_update:
            elapsed = (now_utc - self.last_rate_update).total_seconds()
            if elapsed < 600:
                return self.usd_brl_rate

        try:
            response = requests.get(
                "https://economia.awesomeapi.com.br/json/last/USD-BRL",
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                bid = float(data.get("USDBRL", {}).get("bid", 0))
                if bid > 0:
                    self.usd_brl_rate = bid
                    self.last_rate_update = now_utc
        except Exception as e:
            logger.warning(f"⚠️ Não foi possível atualizar cotação USD/BRL: {e}")

        return self.usd_brl_rate

    def _format_usd_brl(self, amount: float, decimals: int = 2, signed: bool = False) -> str:
        """
        Formata valor em USD e BRL no formato:
        $123.45 (R$ 617.25)
        """
        rate = self._get_usd_brl_rate()
        brl_value = amount * rate

        if signed:
            return f"${amount:+.{decimals}f} (R$ {brl_value:+.{decimals}f})"
        return f"${amount:.{decimals}f} (R$ {brl_value:.{decimals}f})"
    
    def _test_connection(self) -> bool:
        """
        Testa se a conexão com o Telegram está funcionando.
        """
        try:
            response = requests.get(f"{self.base_url}/getMe", timeout=10)
            if response.status_code == 200:
                bot_info = response.json()
                bot_name = bot_info.get('result', {}).get('username', 'Unknown')
                logger.info(f"✅ Telegram conectado! Bot: @{bot_name}")
                return True
            else:
                logger.error(f"❌ Erro ao conectar ao Telegram: {response.text}")
                self.enabled = False
                return False
        except Exception as e:
            logger.error(f"❌ Erro ao conectar ao Telegram: {e}")
            self.enabled = False
            return False
    
    def send_message(self, message: str, parse_mode: str = "HTML", max_retries: int = 3) -> bool:
        """
        Envia uma mensagem para o Telegram com retry automático.
        
        Args:
            message: Texto da mensagem (suporta HTML básico)
            parse_mode: Formato do texto (HTML ou Markdown)
            max_retries: Número máximo de tentativas
        
        Returns:
            True se enviou com sucesso, False caso contrário
        """
        if not self.enabled:
            return False
        
        import time
        
        for attempt in range(max_retries):
            try:
                url = f"{self.base_url}/sendMessage"
                data = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode
                }
                
                response = requests.post(url, data=data, timeout=10)
                
                if response.status_code == 200:
                    return True
                elif response.status_code == 429:
                    # Rate limit - espera e tenta novamente
                    retry_after = response.json().get('parameters', {}).get('retry_after', 5)
                    logger.warning(f"⚠️ Telegram rate limit. Aguardando {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Erro Telegram (tentativa {attempt + 1}/{max_retries}): {response.status_code} - {response.text}")
                    if attempt < max_retries - 1:
                        time.sleep(1)  # Espera 1s antes de tentar novamente
                    continue
                    
            except requests.exceptions.Timeout:
                logger.error(f"Timeout Telegram (tentativa {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(2)
                continue
                
            except Exception as e:
                logger.error(f"Erro ao enviar mensagem Telegram (tentativa {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                continue
        
        logger.error(f"❌ Falha ao enviar mensagem após {max_retries} tentativas")
        return False
    
    def send_status(
        self, 
        balance: float,
        open_positions: int,
        total_trades: int,
        daily_pnl: float,
        funding_fee: float = 0.0,
        total_pnl_realized: float = 0.0,
        total_pnl_unrealized: float = 0.0,
        pnl_by_symbol: dict = None,
        unrealized_by_symbol: dict = None,
        daily_profit_target: float = None,
        daily_loss_limit: float = None,
        daily_target_reached: bool = False
    ) -> bool:
        """
        Envia o status completo do bot formatado.
        """
        if pnl_by_symbol is None:
            pnl_by_symbol = {}
        if unrealized_by_symbol is None:
            unrealized_by_symbol = {}
            
        timestamp = get_brt_timestamp()
        
        # Total = Diário + Não Realizado (resultado do dia)
        total_pnl = daily_pnl + total_pnl_unrealized
        
        # Emoji baseado no P&L total
        if total_pnl > 0:
            pnl_emoji = "🟢"
        elif total_pnl < 0:
            pnl_emoji = "🔴"
        else:
            pnl_emoji = "⚪"
        
        # Emoji para funding
        if funding_fee > 0:
            funding_emoji = "🟢"  # Recebeu funding
        elif funding_fee < 0:
            funding_emoji = "🔴"  # Pagou funding
        else:
            funding_emoji = "⚪"
        
        message = f"""
<b>📊 STATUS DO BOT</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

💰 <b>Saldo:</b> ${balance:.2f} USDT
📈 <b>Posições abertas:</b> {open_positions}
📝 <b>Trades fechados:</b> {total_trades}

<b>💵 P&L GERAL:</b>
   • Diário: <code>${daily_pnl:.2f}</code>
   • Realizado: <code>${total_pnl_realized:.2f}</code>
   • Não Realizado: <code>${total_pnl_unrealized:.2f}</code>
   • {pnl_emoji} <b>Total: <code>${total_pnl:.2f}</code></b>

<b>💸 FUNDING FEE:</b>
   • {funding_emoji} Hoje: <code>${funding_fee:+.2f}</code>
"""
        message = message.replace(
            f"${balance:.2f}",
            self._format_usd_brl(balance, 2, False)
        ).replace(
            f"${daily_pnl:.2f}",
            self._format_usd_brl(daily_pnl, 2, False)
        ).replace(
            f"${total_pnl_realized:.2f}",
            self._format_usd_brl(total_pnl_realized, 2, False)
        ).replace(
            f"${total_pnl_unrealized:.2f}",
            self._format_usd_brl(total_pnl_unrealized, 2, False)
        ).replace(
            f"${total_pnl:.2f}",
            self._format_usd_brl(total_pnl, 2, False)
        ).replace(
            f"${funding_fee:+.2f}",
            self._format_usd_brl(funding_fee, 2, True)
        )
        
        # Adiciona informação de metas diárias se configuradas
        if daily_profit_target is not None and daily_loss_limit is not None:
            if daily_target_reached:
                target_status = "⏸️ <b>META ATINGIDA - Pausado</b>"
            else:
                # Calcula progresso
                if daily_pnl >= 0:
                    progress_pct = (daily_pnl / daily_profit_target) * 100
                    target_status = f"🎯 Progresso: {progress_pct:.1f}% da meta de lucro"
                else:
                    progress_pct = (abs(daily_pnl) / daily_loss_limit) * 100
                    target_status = f"⚠️ Risco: {progress_pct:.1f}% do limite de perda"
            
            message += f"""
<b>🎯 METAS DIÁRIAS:</b>
   • Meta Lucro: <code>+${daily_profit_target:.2f}</code>
   • Limite Perda: <code>-${daily_loss_limit:.2f}</code>
   • {target_status}
"""
            message = message.replace(
                f"+${daily_profit_target:.2f}",
                self._format_usd_brl(daily_profit_target, 2, True)
            ).replace(
                f"-${daily_loss_limit:.2f}",
                self._format_usd_brl(-daily_loss_limit, 2, True)
            )
        
        message += """
<b>📈 P&L POR MOEDA:</b>"""
        
        # Calcula o total de cada símbolo e ordena do maior para o menor
        symbol_totals = []
        for symbol in pnl_by_symbol.keys():
            realized = pnl_by_symbol.get(symbol, 0)
            unrealized = unrealized_by_symbol.get(symbol, 0)
            total = realized + unrealized
            symbol_totals.append((symbol, realized, unrealized, total))
        
        # Ordena pelo total (maior lucro primeiro)
        symbol_totals.sort(key=lambda x: x[3], reverse=True)
        
        for symbol, realized, unrealized, total in symbol_totals:
            if total > 0:
                emoji = "🟢"
            elif total < 0:
                emoji = "🔴"
            else:
                emoji = "⚪"
            
            # Formata o nome (remove USDT)
            name = symbol.replace("USDT", "")
            
            # Mostra detalhamento: Total (Real: X | Aberto: Y)
            message += f"\n   {emoji} <b>{name}:</b> <code>{self._format_usd_brl(total, 2, False)}</code>"
            message += (
                f"\n      <i>(Real: {self._format_usd_brl(realized, 2, False)}"
                f" | Aberto: {self._format_usd_brl(unrealized, 2, False)})</i>"
            )
        
        message += "\n━━━━━━━━━━━━━━━━━━━━━"
        
        return self.send_message(message)
    
    def send_trade_alert(
        self,
        symbol: str,
        action: str,  # "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"
        price: float,
        quantity: float,
        pnl: float = None
    ) -> bool:
        """
        Envia alerta quando um trade é executado.
        """
        timestamp = get_brt_timestamp()
        name = symbol.replace("USDT", "")
        
        if "OPEN" in action:
            emoji = "🚀"
            action_text = "ABERTO"
            side = "LONG 📈" if "LONG" in action else "SHORT 📉"
        else:
            emoji = "✅"
            action_text = "FECHADO"
            side = "LONG 📈" if "LONG" in action else "SHORT 📉"
        
        message = f"""
{emoji} <b>TRADE {action_text}</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

📍 <b>Par:</b> {name}/USDT
📊 <b>Lado:</b> {side}
💵 <b>Preço:</b> ${price:.4f}
📦 <b>Quantidade:</b> {quantity:.4f}"""
        
        if pnl is not None:
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            message += f"\n{pnl_emoji} <b>P&L:</b> <code>${pnl:.2f}</code>"
        
        message += "\n━━━━━━━━━━━━━━━━━━━━━"
        
        return self.send_message(message)
    
    def send_startup_message(self, pairs: list, capital: float, leverage: int) -> bool:
        """
        Envia mensagem quando o bot inicia.
        """
        pairs_text = ", ".join([p.replace("USDT", "") for p in pairs])
        
        message = f"""
🤖 <b>BOT INICIADO!</b>
━━━━━━━━━━━━━━━━━━━━━

💰 <b>Capital:</b> {self._format_usd_brl(capital, 2, False)} USDT
⚡ <b>Alavancagem:</b> {leverage}x
📊 <b>Pares:</b> {pairs_text}

<i>Você receberá atualizações de status e alertas de trades.</i>
━━━━━━━━━━━━━━━━━━━━━"""
        
        return self.send_message(message)
    
    def send_shutdown_message(self, total_pnl: float, total_trades: int) -> bool:
        """
        Envia mensagem quando o bot é parado.
        """
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        
        message = f"""
🛑 <b>BOT FINALIZADO!</b>
━━━━━━━━━━━━━━━━━━━━━

📝 <b>Total de trades:</b> {total_trades}
{pnl_emoji} <b>P&L Total:</b> <code>{self._format_usd_brl(total_pnl, 2, False)}</code>

<i>Até a próxima!</i>
━━━━━━━━━━━━━━━━━━━━━"""
        
        return self.send_message(message)
    
    def send_error_alert(self, error_message: str) -> bool:
        """
        Envia alerta de erro.
        """
        message = f"""
⚠️ <b>ALERTA DE ERRO</b>
━━━━━━━━━━━━━━━━━━━━━

<code>{error_message}</code>

━━━━━━━━━━━━━━━━━━━━━"""
        
        return self.send_message(message)
    
    def send_position_closed(
        self,
        symbol: str,
        side: str,  # "LONG" ou "SHORT"
        entry_price: float,
        exit_price: float,
        quantity: float,
        pnl_gross: float,
        fees: float,
        pnl_net: float,
        reason: str = ""
    ) -> bool:
        """
        Envia notificação quando uma posição é fechada.
        Mostra o P&L bruto, taxas e P&L líquido.
        """
        timestamp = get_brt_timestamp()
        name = symbol.replace("USDT", "")
        
        # Determina se foi lucro ou prejuízo
        if pnl_net > 0:
            result_emoji = "🟢"
            result_text = "LUCRO"
        elif pnl_net < 0:
            result_emoji = "🔴"
            result_text = "PREJUÍZO"
        else:
            result_emoji = "⚪"
            result_text = "NEUTRO"
        
        # Emoji do lado
        side_emoji = "📈" if side == "LONG" else "📉"
        
        # Calcula variação percentual
        if side == "LONG":
            pct_change = ((exit_price - entry_price) / entry_price) * 100
        else:
            pct_change = ((entry_price - exit_price) / entry_price) * 100
        
        message = f"""
{result_emoji} <b>POSIÇÃO FECHADA - {result_text}</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

📍 <b>Par:</b> {name}/USDT
{side_emoji} <b>Lado:</b> {side}
📦 <b>Quantidade:</b> {quantity:.4f}

💰 <b>Preço entrada:</b> ${entry_price:.4f}
💰 <b>Preço saída:</b> ${exit_price:.4f}
📊 <b>Variação:</b> {pct_change:+.2f}%

<b>💵 RESULTADO:</b>
   • P&L Bruto: <code>${pnl_gross:+.4f}</code>
   • Taxas: <code>-${fees:.4f}</code>
   • {result_emoji} <b>P&L Líquido: <code>${pnl_net:+.4f}</code></b>"""
        message = message.replace(
            f"${pnl_gross:+.4f}",
            self._format_usd_brl(pnl_gross, 4, True)
        ).replace(
            f"-${fees:.4f}",
            self._format_usd_brl(-fees, 4, True)
        ).replace(
            f"${pnl_net:+.4f}",
            self._format_usd_brl(pnl_net, 4, True)
        )
        
        if reason:
            message += f"\n\n📝 <b>Motivo:</b> {reason}"
        
        message += "\n━━━━━━━━━━━━━━━━━━━━━"
        
        return self.send_message(message)
    
    def send_global_stop_loss_alert(
        self,
        initial_capital: float,
        current_balance: float,
        total_pnl: float,
        loss_percent: float
    ) -> bool:
        """
        Envia alerta quando o stop loss global é atingido.
        """
        timestamp = get_brt_timestamp()
        
        message = f"""
🚨🚨🚨 <b>STOP LOSS GLOBAL ATINGIDO</b> 🚨🚨🚨
━━━━━━━━━━━━━━━━━━━━━

⏰ <b>Horário:</b> {timestamp}

💰 <b>Capital Inicial:</b> {self._format_usd_brl(initial_capital, 2, False)}
💵 <b>Saldo Atual:</b> {self._format_usd_brl(current_balance, 2, False)}
📉 <b>P&L Total:</b> <code>{self._format_usd_brl(total_pnl, 2, False)}</code>
📊 <b>Perda:</b> {loss_percent:.1f}%

<b>⚠️ TODAS AS POSIÇÕES FORAM FECHADAS</b>
<b>🛑 BOT ENCERRADO</b>

━━━━━━━━━━━━━━━━━━━━━
<i>O bot parou automaticamente para proteger seu capital restante.</i>"""
        
        return self.send_message(message)
    
    def send_trailing_stop_activated(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        current_price: float,
        trailing_stop_price: float,
        current_profit_pct: float
    ) -> bool:
        """
        Envia notificação quando o Trailing Stop é ativado.
        """
        timestamp = get_brt_timestamp()
        name = symbol.replace("USDT", "")
        side_emoji = "📈" if side == "LONG" else "📉"
        
        message = f"""
🔔 <b>TRAILING STOP ATIVADO</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

📍 <b>Par:</b> {name}/USDT
{side_emoji} <b>Lado:</b> {side}

💰 <b>Preço entrada:</b> ${entry_price:.4f}
📊 <b>Preço atual:</b> ${current_price:.4f}
🎯 <b>Trailing Stop em:</b> ${trailing_stop_price:.4f}
✨ <b>Lucro atual:</b> {current_profit_pct:+.2f}%

<i>O stop vai subir automaticamente se o preço continuar favorável!</i>
━━━━━━━━━━━━━━━━━━━━━"""
        
        return self.send_message(message)
    
    def send_portfolio_evolution(
        self,
        initial_capital: float,
        current_balance: float,
        total_pnl: float,
        pnl_realized: float,
        pnl_unrealized: float,
        pct_change: float,
        closed_trades: int,
        trades_win_count: int = 0,
        trades_loss_count: int = 0,
        trades_win_total: float = 0.0,
        trades_loss_total: float = 0.0,
        history: list = None,
        bot_start_time = None
    ) -> bool:
        """
        Envia relatório de evolução da carteira com gráfico em texto.
        Inclui estatísticas detalhadas de trades com lucro e prejuízo.
        Usa timezone do Brasil (UTC-3).
        """
        if history is None:
            history = []
        
        now_brt = datetime.now(BRT)
        timestamp = now_brt.strftime("%H:%M:%S")
        
        # Calcula tempo de operação
        if bot_start_time:
            if bot_start_time.tzinfo is None:
                bot_start_time = bot_start_time.replace(tzinfo=BRT)
            running_time = now_brt - bot_start_time
            hours = int(running_time.total_seconds() // 3600)
            minutes = int((running_time.total_seconds() % 3600) // 60)
        else:
            hours = 0
            minutes = 0
        
        # Emoji baseado na variação TOTAL (realizado + aberto)
        if pct_change > 0:
            trend_emoji = "📈"
            pnl_emoji = "🟢"
        elif pct_change < 0:
            trend_emoji = "📉"
            pnl_emoji = "🔴"
        else:
            trend_emoji = "➡️"
            pnl_emoji = "⚪"

        # Progresso e histórico passam a usar apenas o realizado
        realized_pct_change = (pnl_realized / initial_capital * 100) if initial_capital > 0 else 0.0
        
        # Cria barra de progresso visual (apenas realizado)
        bar_length = 10
        if realized_pct_change >= 0:
            filled = min(int(realized_pct_change / 2), bar_length)  # Cada bloco = 2%
            bar = "🟩" * filled + "⬜" * (bar_length - filled)
        else:
            filled = min(int(abs(realized_pct_change) / 2), bar_length)
            bar = "🟥" * filled + "⬜" * (bar_length - filled)
        
        # Calcula win rate
        win_rate = (trades_win_count / closed_trades * 100) if closed_trades > 0 else 0
        
        # Monta a mensagem usando dados REAIS da Binance
        message = f"""
{trend_emoji} <b>EVOLUÇÃO DA CARTEIRA</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━

⏱️ <b>Tempo operando:</b> {hours}h {minutes}min
📊 <b>Trades fechados:</b> {closed_trades}

<b>💰 CAPITAL:</b>
   • Inicial: <code>{self._format_usd_brl(initial_capital, 2, False)}</code>
   • Atual: <code>{self._format_usd_brl(current_balance, 2, False)}</code>

<b>📈 PERFORMANCE:</b>
   • Realizado: <code>{self._format_usd_brl(pnl_realized, 2, True)}</code>
   • Aberto: <code>{self._format_usd_brl(pnl_unrealized, 2, True)}</code>
   • {pnl_emoji} <b>Total: <code>{self._format_usd_brl(total_pnl, 2, True)}</code></b>
   • Variação (Total): <code>{pct_change:+.2f}%</code>

<b>📊 ESTATÍSTICAS:</b>
   🟢 Lucros: <code>{trades_win_count}</code> trades = <code>{self._format_usd_brl(trades_win_total, 2, True)}</code>
   🔴 Perdas: <code>{trades_loss_count}</code> trades = <code>{self._format_usd_brl(trades_loss_total, 2, True)}</code>
   🎯 Win Rate: <code>{win_rate:.1f}%</code>

<b>📊 PROGRESSO (REALIZADO):</b>
{bar} {realized_pct_change:+.2f}%"""
        
        # Adiciona histórico se tiver dados
        if history and len(history) > 1:
            message += "\n\n<b>📜 HISTÓRICO (REALIZADO):</b>"
            
            # Encontra o máximo e mínimo para escala
            pnls = [h['pnl'] for h in history]
            max_pnl = max(pnls) if pnls else 0
            min_pnl = min(pnls) if pnls else 0
            range_pnl = max_pnl - min_pnl if max_pnl != min_pnl else 1
            
            # Mostra últimos 6 pontos do histórico com mini gráfico
            for h in history[-6:]:
                # Normaliza para criar barra (0-5 blocos)
                if range_pnl > 0:
                    normalized = (h['pnl'] - min_pnl) / range_pnl
                    blocks = int(normalized * 5)
                else:
                    blocks = 2
                
                if h['pnl'] >= 0:
                    mini_bar = "▓" * blocks + "░" * (5 - blocks)
                    emoji = "🟢"
                else:
                    mini_bar = "▓" * blocks + "░" * (5 - blocks)
                    emoji = "🔴"
                
                message += f"\n   {h['time']} {mini_bar} {emoji} {self._format_usd_brl(h['pnl'], 2, True)}"
        
        message += "\n━━━━━━━━━━━━━━━━━━━━━"
        
        return self.send_message(message)
    
    def send_trades_report(
        self,
        trades_by_symbol: dict,
        total_wins: int,
        total_losses: int,
        total_win_value: float,
        total_loss_value: float,
        total_fees: float = 0.0
    ) -> bool:
        """
        Envia relatório detalhado de trades por moeda.
        
        Args:
            trades_by_symbol: Dict com {symbol: {'wins': int, 'losses': int, 'win_value': float, 'loss_value': float, 'fees': float}}
            total_wins: Total de trades positivos
            total_losses: Total de trades negativos
            total_win_value: Valor total dos lucros
            total_loss_value: Valor total das perdas (negativo)
            total_fees: Total de taxas pagas
        """
        timestamp = get_brt_timestamp()
        
        total_trades = total_wins + total_losses
        if total_trades == 0:
            return False  # Não envia se não tem trades
        
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        net_pnl = total_win_value + total_loss_value  # loss_value já é negativo
        
        # Emoji baseado no resultado
        if net_pnl > 0:
            result_emoji = "🟢"
        elif net_pnl < 0:
            result_emoji = "🔴"
        else:
            result_emoji = "⚪"
        
        message = f"""
📈 <b>RELATÓRIO DE TRADES</b> <i>({timestamp})</i>
━━━━━━━━━━━━━━━━━━━━━
"""
        
        # Separa trades positivos e negativos por símbolo
        positive_trades = []
        negative_trades = []
        
        for symbol, data in trades_by_symbol.items():
            name = symbol.replace("USDT", "")
            
            # Adiciona trades positivos
            if data['wins'] > 0:
                positive_trades.append({
                    'name': name,
                    'count': data['wins'],
                    'value': data['win_value']
                })
            
            # Adiciona trades negativos
            if data['losses'] > 0:
                negative_trades.append({
                    'name': name,
                    'count': data['losses'],
                    'value': data['loss_value']
                })
        
        # Ordena por valor (maior primeiro para positivos, menor primeiro para negativos)
        positive_trades.sort(key=lambda x: x['value'], reverse=True)
        negative_trades.sort(key=lambda x: x['value'])  # Mais negativo primeiro
        
        # TRADES POSITIVOS
        if positive_trades:
            message += f"\n✅ <b>TRADES POSITIVOS ({total_wins}):</b>"
            for trade in positive_trades:
                message += (
                    f"\n   • {trade['name']}: "
                    f"<code>{self._format_usd_brl(trade['value'], 2, True)}</code> ({trade['count']}x)"
                )
            message += f"\n   <b>Total: <code>{self._format_usd_brl(total_win_value, 2, True)}</code></b>"
        else:
            message += f"\n✅ <b>TRADES POSITIVOS:</b> Nenhum"
        
        message += "\n"
        
        # TRADES NEGATIVOS
        if negative_trades:
            message += f"\n❌ <b>TRADES NEGATIVOS ({total_losses}):</b>"
            for trade in negative_trades:
                message += (
                    f"\n   • {trade['name']}: "
                    f"<code>{self._format_usd_brl(trade['value'], 2, True)}</code> ({trade['count']}x)"
                )
            message += f"\n   <b>Total: <code>{self._format_usd_brl(total_loss_value, 2, True)}</code></b>"
        else:
            message += f"\n❌ <b>TRADES NEGATIVOS:</b> Nenhum"
        
        # RESUMO
        message += f"""

━━━━━━━━━━━━━━━━━━━━━
📊 <b>RESUMO:</b>
   • Total de Trades: <code>{total_trades}</code>
   • Win Rate: <code>{win_rate:.1f}%</code>
   • {result_emoji} <b>Lucro Líquido: <code>{self._format_usd_brl(net_pnl, 2, True)}</code></b>"""
        
        # Melhor e pior trade
        if positive_trades:
            best = positive_trades[0]
            message += f"\n   • 🏆 Melhor: {best['name']} (<code>{self._format_usd_brl(best['value'], 2, True)}</code>)"
        
        if negative_trades:
            worst = negative_trades[0]
            message += f"\n   • 💀 Pior: {worst['name']} (<code>{self._format_usd_brl(worst['value'], 2, True)}</code>)"
        
        # TAXAS PAGAS
        message += f"""

━━━━━━━━━━━━━━━━━━━━━
💸 <b>TAXAS BINANCE:</b>
   • Total pago: <code>{self._format_usd_brl(-total_fees, 4, True)}</code>
   • Média por trade: <code>{self._format_usd_brl(-(total_fees/total_trades), 4, True)}</code>"""
        
        # Calcula % das taxas sobre o lucro bruto
        gross_profit = total_win_value - total_loss_value  # Diferença bruta (sem taxas)
        if gross_profit != 0:
            fees_pct = (total_fees / abs(gross_profit)) * 100
            message += f"\n   • % do lucro bruto: <code>{fees_pct:.1f}%</code>"
        
        message += "\n━━━━━━━━━━━━━━━━━━━━━"
        
        return self.send_message(message)
