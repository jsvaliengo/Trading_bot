"""
Telegram Command Handler para o Trading Bot.

Permite controlar o bot via comandos do Telegram:
- Iniciar/Parar/Pausar
- Alterar configurações (alavancagem, order size, TP, SL, etc)
- Ver status, posições, saldo
- Fechar posições

Autor: Trading Bot
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict
import requests

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    """
    Handler para comandos do Telegram.
    
    Usa long polling para receber comandos em tempo real.
    """
    
    def __init__(self, token: str, chat_id: str):
        """
        Inicializa o handler de comandos.
        
        Args:
            token: Token do bot do Telegram
            chat_id: ID do chat autorizado
        """
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.usd_brl_rate = 5.0
        self.last_rate_update = None
        
        # Controle do polling
        self.running = False
        self.poll_thread = None
        self.last_update_id = 0
        
        # Callbacks para ações
        self.callbacks: Dict[str, Callable] = {}
        
        # Referência ao bot principal (será setada depois)
        self.bot = None
        self.config = None
        
        # Comandos disponíveis
        self.commands = {
            '/start': self.cmd_start,
            '/stop': self.cmd_stop,
            '/pause': self.cmd_pause,
            '/resume': self.cmd_resume,
            '/status': self.cmd_status,
            '/portfolio': self.cmd_portfolio,
            '/trades': self.cmd_trades,
            '/lockinfo': self.cmd_lockinfo,
            '/apihealth': self.cmd_apihealth,
            '/dailyreport': self.cmd_dailyreport,
            '/config': self.cmd_config,
            '/positions': self.cmd_positions,
            '/coins': self.cmd_coins,
            '/balance': self.cmd_balance,
            '/leverage': self.cmd_leverage,
            '/ordersize': self.cmd_ordersize,
            '/tp': self.cmd_take_profit,
            '/sl': self.cmd_stop_loss,
            '/trailing': self.cmd_trailing,
            '/closeall': self.cmd_close_all,
            '/help': self.cmd_help,
        }
        
        logger.info("📱 Telegram Command Handler inicializado")
    
    def set_bot_reference(self, bot, config):
        """
        Define a referência ao bot principal e config.
        
        Args:
            bot: Instância do TradingBot
            config: Instância do Config
        """
        self.bot = bot
        self.config = config
    
    def send_message(self, text: str) -> bool:
        """Envia mensagem para o Telegram."""
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem: {e}")
            return False

    def _get_usd_brl_rate(self) -> float:
        """
        Retorna cotação USD->BRL com cache de 10 minutos.
        Em caso de falha, mantém a última cotação conhecida.
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
    
    def start_polling(self):
        """Inicia o polling de comandos em uma thread separada."""
        if self.running:
            return
        
        self.running = True
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()
        logger.info("📱 Polling de comandos iniciado")
    
    def stop_polling(self):
        """Para o polling de comandos."""
        self.running = False
        if self.poll_thread:
            self.poll_thread.join(timeout=5)
        logger.info("📱 Polling de comandos parado")
    
    def _poll_loop(self):
        """Loop principal de polling."""
        while self.running:
            try:
                updates = self._get_updates()
                
                for update in updates:
                    self._process_update(update)
                
                time.sleep(1)  # Polling a cada 1 segundo
                
            except Exception as e:
                logger.error(f"Erro no polling: {e}")
                time.sleep(5)
    
    def _get_updates(self) -> list:
        """Busca atualizações do Telegram."""
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": 30,
                "allowed_updates": ["message"]
            }
            response = requests.get(url, params=params, timeout=35)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    return data.get("result", [])
            
            return []
            
        except Exception as e:
            logger.error(f"Erro ao buscar updates: {e}")
            return []
    
    def _process_update(self, update: dict):
        """Processa uma atualização do Telegram."""
        try:
            self.last_update_id = update.get("update_id", self.last_update_id)
            
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            user_id = message.get("from", {}).get("id")
            text = message.get("text", "").strip()
            
            # Verifica se é do chat autorizado
            if chat_id != self.chat_id:
                logger.warning(f"⚠️ Comando de chat não autorizado: {chat_id}")
                return

            # Se configurado, valida também o usuário autorizado
            authorized_user_id = getattr(self.config, "TELEGRAM_USER_ID", None)
            if authorized_user_id is not None and user_id != authorized_user_id:
                logger.warning(f"⚠️ Comando de usuário não autorizado: {user_id}")
                return
            
            # Verifica se é um comando
            if not text.startswith("/"):
                return
            
            # Extrai comando e argumentos
            parts = text.split()
            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []
            
            # Executa o comando
            if command in self.commands:
                logger.info(f"📱 Comando recebido: {command} {' '.join(args)}")
                self.commands[command](args)
            else:
                self.send_message(f"❓ Comando desconhecido: {command}\n\nUse /help para ver os comandos disponíveis.")
                
        except Exception as e:
            logger.error(f"Erro ao processar update: {e}")
    
    # ============================================
    # COMANDOS DE CONTROLE
    # ============================================
    
    def cmd_start(self, args: list):
        """Inicia o bot."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        if self.bot.running and not getattr(self.bot, 'paused', False):
            self.send_message("⚠️ Bot já está rodando!")
            return
        
        self.bot.running = True
        self.bot.paused = False
        
        self.send_message(
            "🚀 <b>BOT INICIADO</b>\n\n"
            "• Análise de mercado: <b>ativa</b>\n"
            "• Abertura de posições: <b>liberada</b>\n\n"
            "Bot operando! Use /status para acompanhar."
        )
    
    def cmd_stop(self, args: list):
        """Para o bot."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        if not self.bot.running:
            self.send_message("⚠️ Bot já está parado!")
            return
        
        # Pergunta se quer fechar posições
        if args and args[0].lower() == 'force':
            self.send_message("🛑 <b>PARANDO BOT...</b>\n\n⚠️ Fechando todas as posições...")
            
            # Fecha posições
            positions = self.bot.exchange.get_open_positions()
            total_positions = len(positions)
            closed_count = 0
            failures = []

            for pos in positions:
                symbol = pos.get('symbol', 'UNKNOWN')
                side = pos.get('side', 'UNKNOWN')
                qty = pos.get('quantity', 0)
                try:
                    if side == 'LONG':
                        result = self.bot.exchange.place_market_order(symbol, 'SELL', 'LONG', qty)
                    else:
                        result = self.bot.exchange.place_market_order(symbol, 'BUY', 'SHORT', qty)

                    if result:
                        closed_count += 1
                    else:
                        failures.append(f"{side} {symbol}: retorno vazio da exchange")
                except Exception as e:
                    failures.append(f"{side} {symbol}: {e}")
                    logger.error(f"Erro ao fechar posição via /stop force ({side} {symbol}): {e}")
            
            self.bot.running = False
            if total_positions == 0:
                self.send_message("✅ Bot parado. Não havia posições abertas para fechar.")
            elif failures:
                preview = "\n".join([f"• {item}" for item in failures[:5]])
                if len(failures) > 5:
                    preview += f"\n• ... e mais {len(failures) - 5} falha(s)"
                self.send_message(
                    f"⚠️ <b>BOT PARADO COM FALHAS AO FECHAR POSIÇÕES</b>\n\n"
                    f"✅ Fechadas: <code>{closed_count}/{total_positions}</code>\n"
                    f"❌ Falhas: <code>{len(failures)}</code>\n\n"
                    f"<b>Detalhes:</b>\n{preview}\n\n"
                    f"Use /positions para verificar se ainda há posições abertas."
                )
            else:
                self.send_message(
                    f"✅ Bot parado e posições fechadas com sucesso "
                    f"(<code>{closed_count}/{total_positions}</code>)."
                )
        else:
            try:
                self.bot.save_state()
            except Exception as e:
                logger.error(f"Erro ao salvar estado no /stop: {e}")
            self.bot.running = False
            self.send_message(
                "🛑 <b>BOT PARADO</b>\n\n"
                "⚠️ Posições abertas foram MANTIDAS.\n\n"
                "Use <code>/stop force</code> para fechar todas as posições."
            )
    
    def cmd_pause(self, args: list):
        """Pausa o bot (mantém posições)."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        if not self.bot.running:
            self.send_message("⚠️ Bot não está rodando!")
            return
        
        if getattr(self.bot, 'paused', False):
            self.send_message("⚠️ Bot já está pausado!")
            return
        
        self.bot.paused = True
        self.send_message(
            "⏸️ <b>BOT PAUSADO</b>\n\n"
            "• Posições abertas: <b>mantidas</b>\n"
            "• Novas posições: <b>bloqueadas</b>\n"
            "• Trailing Stop: <b>ativo</b>\n\n"
            "Use /resume para continuar."
        )
    
    def cmd_resume(self, args: list):
        """Retoma o bot após pausa."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        if not self.bot.running:
            self.send_message("⚠️ Bot não está rodando! Use /start")
            return
        
        if not getattr(self.bot, 'paused', False):
            self.send_message("⚠️ Bot não está pausado!")
            return
        
        self.bot.paused = False
        self.send_message(
            "▶️ <b>BOT RETOMADO</b>\n\n"
            "• Análise de mercado: <b>ativa</b>\n"
            "• Abertura de posições: <b>liberada</b>\n\n"
            "Bot operando normalmente! 🚀"
        )
    
    # ============================================
    # COMANDOS DE INFORMAÇÃO
    # ============================================
    
    def cmd_status(self, args: list):
        """Mostra status completo."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        try:
            # Busca dados
            account_info = self.bot.exchange.get_account_info()
            balance = account_info['wallet_balance']
            unrealized = account_info['unrealized_pnl']
            
            # Status do bot
            if not self.bot.running:
                status = "🔴 PARADO"
            elif getattr(self.bot, 'paused', False):
                status = "⏸️ PAUSADO"
            else:
                status = "🟢 RODANDO"
            
            # Win rate
            total_trades = self.bot.trades_win_count + self.bot.trades_loss_count
            win_rate = (self.bot.trades_win_count / total_trades * 100) if total_trades > 0 else 0
            
            # Posições abertas
            positions = self.bot.exchange.get_open_positions()
            
            # P&L acumulado (realizado total desde o início da sessão)
            pnl_emoji = "🟢" if self.bot.total_pnl >= 0 else "🔴"
            
            message = f"""
📊 <b>STATUS DO BOT</b>
━━━━━━━━━━━━━━━━━━━━━

<b>Estado:</b> {status}

💰 <b>CAPITAL:</b>
   • Saldo: <code>{self._format_usd_brl(balance, 2, False)}</code>
   • Não Realizado: <code>{self._format_usd_brl(unrealized, 2, True)}</code>

📈 <b>PERFORMANCE:</b>
   • P&L Total Realizado: {pnl_emoji} <code>{self._format_usd_brl(self.bot.total_pnl, 2, True)}</code>
   • Trades: <code>{total_trades}</code>
   • Win Rate: <code>{win_rate:.1f}%</code>

📍 <b>POSIÇÕES:</b> {len(positions)} abertas

⚙️ <b>CONFIG:</b>
   • Alavancagem: <code>{self.config.LEVERAGE}x</code>
   • Moedas: <code>{len(self.config.TRADING_PAIRS)}</code>
━━━━━━━━━━━━━━━━━━━━━
"""
            self.send_message(message)
            
        except Exception as e:
            self.send_message(f"❌ Erro ao buscar status: {e}")
    
    def cmd_config(self, args: list):
        """Mostra configurações atuais."""
        if self.config is None:
            self.send_message("❌ Config não disponível")
            return
        
        # Estratégia Binance
        if self.config.USE_BINANCE_STRATEGY and hasattr(self.bot, 'binance_strategy') and self.bot.binance_strategy:
            strategy = self.bot.binance_strategy
            strategy_info = f"""
📊 <b>ESTRATÉGIA BINANCE:</b>
   • Faixa: {strategy['capital_range']}
   • Order Size: <code>{self._format_usd_brl(strategy['order_size'], 2, False)}</code>
   • Moedas: <code>{strategy['num_coins']}</code>"""
        else:
            strategy_info = ""
        
        message = f"""
⚙️ <b>CONFIGURAÇÕES ATUAIS</b>
━━━━━━━━━━━━━━━━━━━━━

📈 <b>TRADING:</b>
   • Alavancagem: <code>{self.config.LEVERAGE}x</code>
   • Take Profit: <code>{self.config.TAKE_PROFIT_PERCENT}%</code>
   • Stop Loss: <code>{self.config.STOP_LOSS_PERCENT}%</code> {'(ativo)' if self.config.USE_INDIVIDUAL_STOP_LOSS else '(desativado)'}

🔄 <b>TRAILING STOP:</b>
   • Ativação: <code>{self.config.TRAILING_ACTIVATION_PERCENT}%</code>
   • Distância: <code>{self.config.TRAILING_DISTANCE_PERCENT}%</code>

🎯 <b>METAS DIÁRIAS:</b> {'Ativas' if self.config.USE_DAILY_TARGETS else 'Desativadas'}
{strategy_info}
━━━━━━━━━━━━━━━━━━━━━

<i>Use /help para ver comandos de alteração</i>
"""
        self.send_message(message)

    def cmd_lockinfo(self, args: list):
        """Mostra informações do lock de instância única."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        try:
            if not hasattr(self.bot, 'get_lock_info'):
                self.send_message("⚠️ Esta versão do bot não suporta /lockinfo.")
                return

            info = self.bot.get_lock_info()
            lock_emoji = "🟢" if info.get('lock_acquired') else "🔴"
            status = "ATIVO" if info.get('lock_acquired') else "INATIVO"
            running = "SIM" if info.get('bot_running') else "NÃO"
            paused = "SIM" if info.get('bot_paused') else "NÃO"

            message = f"""
🔐 <b>LOCK INFO</b>
━━━━━━━━━━━━━━━━━━━━━

{lock_emoji} <b>Lock:</b> {status}
🧾 <b>Arquivo:</b> <code>{info.get('lock_file', 'N/A')}</code>
👤 <b>Holder:</b> <code>{info.get('holder_info', 'N/A')}</code>
🆔 <b>PID atual:</b> <code>{info.get('current_pid', 'N/A')}</code>
▶️ <b>Bot rodando:</b> <code>{running}</code>
⏸️ <b>Bot pausado:</b> <code>{paused}</code>
━━━━━━━━━━━━━━━━━━━━━
"""
            self.send_message(message)

        except Exception as e:
            self.send_message(f"❌ Erro ao buscar lock info: {e}")

    def cmd_apihealth(self, args: list):
        """Envia report de saúde operacional (API + ordens + loops)."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        try:
            if not hasattr(self.bot, 'send_api_health_report'):
                self.send_message("⚠️ Esta versão do bot não suporta /apihealth.")
                return

            sent = self.bot.send_api_health_report(force=True)
            if not sent:
                self.send_message("ℹ️ Sem dados recentes de saúde operacional nesta janela.")

        except Exception as e:
            self.send_message(f"❌ Erro ao gerar API health: {e}")

    def cmd_dailyreport(self, args: list):
        """Envia relatório diário consolidado e controla envio automático."""
        if self.bot is None or self.config is None:
            self.send_message("❌ Bot/config não disponível")
            return

        if not hasattr(self.bot, 'send_daily_performance_report'):
            self.send_message("⚠️ Esta versão do bot não suporta /dailyreport.")
            return

        if not args:
            enabled = "ON" if getattr(self.config, "DAILY_PERFORMANCE_REPORT_ENABLED", True) else "OFF"
            hour = int(getattr(self.config, "DAILY_PERFORMANCE_REPORT_HOUR_BRT", 23))
            minute = int(getattr(self.config, "DAILY_PERFORMANCE_REPORT_MINUTE_BRT", 55))
            lookback = int(getattr(self.config, "DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS", 24))
            self.send_message(
                f"📅 <b>RELATÓRIO DIÁRIO</b>\n\n"
                f"• Automático: <code>{enabled}</code>\n"
                f"• Horário (BRT): <code>{hour:02d}:{minute:02d}</code>\n"
                f"• Janela: <code>{lookback}h</code>\n\n"
                f"Uso:\n"
                f"• <code>/dailyreport now</code> (enviar agora)\n"
                f"• <code>/dailyreport on</code> / <code>/dailyreport off</code>"
            )
            return

        action = args[0].strip().lower()
        if action in {"on", "off"}:
            self.config.DAILY_PERFORMANCE_REPORT_ENABLED = (action == "on")
            state = "ATIVADO" if self.config.DAILY_PERFORMANCE_REPORT_ENABLED else "DESATIVADO"
            self.send_message(f"✅ Relatório diário automático <b>{state}</b>.")
            return

        if action in {"now", "force"}:
            sent = self.bot.send_daily_performance_report(force=True)
            if not sent:
                self.send_message("ℹ️ Não foi possível enviar o relatório agora.")
            return

        self.send_message(
            "❌ Opção inválida.\n\n"
            "Use:\n"
            "• <code>/dailyreport now</code>\n"
            "• <code>/dailyreport on</code>\n"
            "• <code>/dailyreport off</code>"
        )

    def cmd_portfolio(self, args: list):
        """Envia evolução da carteira sob demanda."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        try:
            if not hasattr(self.bot, 'send_portfolio_evolution'):
                self.send_message("⚠️ Esta versão do bot não suporta /portfolio.")
                return
            self.bot.send_portfolio_evolution()
        except Exception as e:
            self.send_message(f"❌ Erro ao gerar evolução da carteira: {e}")

    def cmd_trades(self, args: list):
        """Envia relatório de trades sob demanda."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        try:
            if not hasattr(self.bot, 'send_trades_report'):
                self.send_message("⚠️ Esta versão do bot não suporta /trades.")
                return

            total_trades = int(getattr(self.bot, 'trades_win_count', 0)) + int(
                getattr(self.bot, 'trades_loss_count', 0)
            )
            if total_trades == 0:
                self.send_message("ℹ️ Sem trades fechados para relatório no momento.")
                return

            self.bot.send_trades_report()
        except Exception as e:
            self.send_message(f"❌ Erro ao gerar relatório de trades: {e}")
    
    def cmd_positions(self, args: list):
        """Mostra posições abertas."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        try:
            positions = self.bot.exchange.get_open_positions()
            
            if not positions:
                self.send_message("📍 <b>POSIÇÕES:</b> Nenhuma posição aberta")
                return
            
            message = f"📍 <b>POSIÇÕES ABERTAS ({len(positions)})</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
            
            total_unrealized = 0
            
            for pos in positions:
                symbol = pos['symbol'].replace('USDT', '')
                side = pos['side']
                entry = pos['entry_price']
                current = self.bot.exchange.get_symbol_price(pos['symbol'])
                qty = pos['quantity']
                
                # Calcula P&L
                if side == 'LONG':
                    pnl = (current - entry) * qty
                    emoji = "📈"
                else:
                    pnl = (entry - current) * qty
                    emoji = "📉"
                
                total_unrealized += pnl
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                
                message += f"\n{emoji} <b>{symbol}</b> ({side})\n"
                message += f"   Entry: <code>{self._format_usd_brl(entry, 4, False)}</code>\n"
                message += f"   Atual: <code>{self._format_usd_brl(current, 4, False)}</code>\n"
                message += f"   P&L: {pnl_emoji} <code>{self._format_usd_brl(pnl, 2, True)}</code>\n"
            
            total_emoji = "🟢" if total_unrealized >= 0 else "🔴"
            message += "\n━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"<b>Total:</b> {total_emoji} <code>{self._format_usd_brl(total_unrealized, 2, True)}</code>"
            
            self.send_message(message)
            
        except Exception as e:
            self.send_message(f"❌ Erro ao buscar posições: {e}")
    
    def cmd_coins(self, args: list):
        """Mostra moedas ativas."""
        if self.config is None:
            self.send_message("❌ Config não disponível")
            return
        
        coins = [c.replace('USDT', '') for c in self.config.TRADING_PAIRS]
        
        message = f"""
🪙 <b>MOEDAS ATIVAS ({len(coins)})</b>
━━━━━━━━━━━━━━━━━━━━━

{', '.join(coins)}

━━━━━━━━━━━━━━━━━━━━━
<i>Ordenadas por score (spread, volume, volatilidade)</i>
"""
        self.send_message(message)
    
    def cmd_balance(self, args: list):
        """Mostra saldo detalhado."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        try:
            account_info = self.bot.exchange.get_account_info()
            balance = account_info['wallet_balance']
            available = account_info['available_balance']
            unrealized = account_info['unrealized_pnl']
            margin = account_info['margin_balance']
            
            # Busca P&L real da Binance
            daily_pnl = self.bot.exchange.get_daily_pnl_from_binance()
            
            message = f"""
💰 <b>SALDO DA CONTA</b>
━━━━━━━━━━━━━━━━━━━━━

💵 <b>Carteira:</b> <code>{self._format_usd_brl(balance, 2, False)}</code>
💳 <b>Disponível:</b> <code>{self._format_usd_brl(available, 2, False)}</code>
📊 <b>Margem:</b> <code>{self._format_usd_brl(margin, 2, False)}</code>

📈 <b>P&L NÃO REALIZADO:</b>
   <code>{self._format_usd_brl(unrealized, 2, True)}</code>

📊 <b>P&L DO DIA (Binance):</b>
   • Trades: <code>{self._format_usd_brl(daily_pnl['realized_pnl'], 2, True)}</code>
   • Funding: <code>{self._format_usd_brl(daily_pnl['funding_fee'], 2, True)}</code>
   • Comissões: <code>{self._format_usd_brl(daily_pnl['commission'], 2, True)}</code>
   • <b>Total:</b> <code>{self._format_usd_brl(daily_pnl['total'], 2, True)}</code>

━━━━━━━━━━━━━━━━━━━━━
"""
            self.send_message(message)
            
        except Exception as e:
            self.send_message(f"❌ Erro ao buscar saldo: {e}")
    
    # ============================================
    # COMANDOS DE CONFIGURAÇÃO
    # ============================================
    
    def cmd_leverage(self, args: list):
        """Altera a alavancagem."""
        if self.config is None or self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        if not args:
            self.send_message(
                f"⚙️ <b>Alavancagem atual:</b> <code>{self.config.LEVERAGE}x</code>\n\n"
                f"Para alterar, use:\n<code>/leverage [valor]</code>\n\n"
                f"Exemplo: <code>/leverage 20</code>"
            )
            return
        
        try:
            new_leverage = int(args[0])
            
            if new_leverage < 1 or new_leverage > 125:
                self.send_message("❌ Alavancagem deve ser entre 1 e 125")
                return
            
            old_leverage = self.config.LEVERAGE
            self.config.LEVERAGE = new_leverage
            
            # Atualiza na Binance para cada par
            for symbol in self.config.TRADING_PAIRS:
                self.bot.exchange.set_leverage(symbol, new_leverage)
            
            self.send_message(
                f"✅ <b>ALAVANCAGEM ALTERADA</b>\n\n"
                f"   Anterior: <code>{old_leverage}x</code>\n"
                f"   Nova: <code>{new_leverage}x</code>\n\n"
                f"⚠️ Aplicada a todos os {len(self.config.TRADING_PAIRS)} pares."
            )
            
        except ValueError:
            self.send_message("❌ Valor inválido. Use um número inteiro.\n\nExemplo: <code>/leverage 20</code>")
    
    def cmd_ordersize(self, args: list):
        """Altera o tamanho da ordem."""
        if self.config is None or self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        # Verifica se está usando estratégia Binance
        if self.config.USE_BINANCE_STRATEGY and hasattr(self.bot, 'binance_strategy') and self.bot.binance_strategy:
            current_size = self.bot.binance_strategy['order_size']
        else:
            current_size = "Automático"

        if isinstance(current_size, (int, float)):
            current_size_display = self._format_usd_brl(float(current_size), 2, False)
        else:
            current_size_display = current_size
        
        if not args:
            self.send_message(
                f"💵 <b>Order Size atual:</b> <code>{current_size_display}</code>\n\n"
                f"Para alterar, use:\n<code>/ordersize [valor]</code>\n\n"
                f"Exemplo: <code>/ordersize 5</code>"
            )
            return
        
        try:
            new_size = float(args[0])
            
            if new_size < 1:
                self.send_message(f"❌ Order size mínimo é {self._format_usd_brl(1, 2, False)}")
                return
            
            if self.config.USE_BINANCE_STRATEGY and hasattr(self.bot, 'binance_strategy') and self.bot.binance_strategy:
                old_size = self.bot.binance_strategy['order_size']
                self.bot.binance_strategy['order_size'] = new_size
                
                self.send_message(
                    f"✅ <b>ORDER SIZE ALTERADO</b>\n\n"
                    f"   Anterior: <code>{self._format_usd_brl(old_size, 2, False)}</code>\n"
                    f"   Novo: <code>{self._format_usd_brl(new_size, 2, False)}</code>\n\n"
                    f"⚠️ Esta alteração é temporária.\n"
                    f"Será resetada ao mudar de faixa de capital."
                )
            else:
                self.send_message(
                    "⚠️ Order size é calculado automaticamente\n"
                    "baseado no % do capital.\n\n"
                    f"Atual: {self.config.MAX_POSITION_PERCENT * 100:.0f}% do capital"
                )
            
        except ValueError:
            self.send_message("❌ Valor inválido. Use um número.\n\nExemplo: <code>/ordersize 5</code>")
    
    def cmd_take_profit(self, args: list):
        """Altera o Take Profit %."""
        if self.config is None:
            self.send_message("❌ Config não disponível")
            return
        
        if not args:
            self.send_message(
                f"🎯 <b>Take Profit atual:</b> <code>{self.config.TAKE_PROFIT_PERCENT}%</code>\n\n"
                f"Para alterar, use:\n<code>/tp [valor]</code>\n\n"
                f"Exemplo: <code>/tp 10</code> (para 10%)"
            )
            return
        
        try:
            new_tp = float(args[0])
            
            if new_tp < 0.1 or new_tp > 100:
                self.send_message("❌ Take Profit deve ser entre 0.1% e 100%")
                return
            
            old_tp = self.config.TAKE_PROFIT_PERCENT
            self.config.TAKE_PROFIT_PERCENT = new_tp
            
            self.send_message(
                f"✅ <b>TAKE PROFIT ALTERADO</b>\n\n"
                f"   Anterior: <code>{old_tp}%</code>\n"
                f"   Novo: <code>{new_tp}%</code>\n\n"
                f"⚠️ Aplicado às NOVAS posições."
            )
            
        except ValueError:
            self.send_message("❌ Valor inválido. Use um número.\n\nExemplo: <code>/tp 10</code>")
    
    def cmd_stop_loss(self, args: list):
        """Altera o Stop Loss %."""
        if self.config is None:
            self.send_message("❌ Config não disponível")
            return
        
        status = "ativo" if self.config.USE_INDIVIDUAL_STOP_LOSS else "desativado"
        
        if not args:
            self.send_message(
                f"🛑 <b>Stop Loss atual:</b> <code>{self.config.STOP_LOSS_PERCENT}%</code> ({status})\n\n"
                f"Para alterar, use:\n<code>/sl [valor]</code> ou <code>/sl on</code> / <code>/sl off</code>\n\n"
                f"Exemplos:\n"
                f"• <code>/sl 5</code> (para 5%)\n"
                f"• <code>/sl on</code> (ativar)\n"
                f"• <code>/sl off</code> (desativar)"
            )
            return
        
        arg = args[0].lower()
        
        if arg == 'on':
            self.config.USE_INDIVIDUAL_STOP_LOSS = True
            self.send_message(f"✅ Stop Loss <b>ATIVADO</b> ({self.config.STOP_LOSS_PERCENT}%)")
            return
        
        if arg == 'off':
            self.config.USE_INDIVIDUAL_STOP_LOSS = False
            self.send_message("✅ Stop Loss <b>DESATIVADO</b>")
            return
        
        try:
            new_sl = float(arg)
            
            if new_sl < 0.1 or new_sl > 100:
                self.send_message("❌ Stop Loss deve ser entre 0.1% e 100%")
                return
            
            old_sl = self.config.STOP_LOSS_PERCENT
            self.config.STOP_LOSS_PERCENT = new_sl
            
            self.send_message(
                f"✅ <b>STOP LOSS ALTERADO</b>\n\n"
                f"   Anterior: <code>{old_sl}%</code>\n"
                f"   Novo: <code>{new_sl}%</code>\n\n"
                f"⚠️ Aplicado às NOVAS posições."
            )
            
        except ValueError:
            self.send_message("❌ Valor inválido.\n\nExemplos:\n• <code>/sl 5</code>\n• <code>/sl on</code>\n• <code>/sl off</code>")
    
    def cmd_trailing(self, args: list):
        """Altera configurações do Trailing Stop."""
        if self.config is None:
            self.send_message("❌ Config não disponível")
            return
        
        if not args:
            self.send_message(
                f"🔄 <b>TRAILING STOP</b>\n\n"
                f"   Ativação: <code>{self.config.TRAILING_ACTIVATION_PERCENT}%</code>\n"
                f"   Distância: <code>{self.config.TRAILING_DISTANCE_PERCENT}%</code>\n\n"
                f"Para alterar, use:\n<code>/trailing [ativação] [distância]</code>\n\n"
                f"Exemplo: <code>/trailing 0.5 0.25</code>"
            )
            return
        
        try:
            if len(args) >= 2:
                new_activation = float(args[0])
                new_distance = float(args[1])
            else:
                self.send_message("❌ Informe ativação e distância.\n\nExemplo: <code>/trailing 0.5 0.25</code>")
                return
            
            if new_activation < 0.01 or new_activation > 50:
                self.send_message("❌ Ativação deve ser entre 0.01% e 50%")
                return
            
            if new_distance < 0.01 or new_distance > 50:
                self.send_message("❌ Distância deve ser entre 0.01% e 50%")
                return
            
            old_activation = self.config.TRAILING_ACTIVATION_PERCENT
            old_distance = self.config.TRAILING_DISTANCE_PERCENT
            
            self.config.TRAILING_ACTIVATION_PERCENT = new_activation
            self.config.TRAILING_DISTANCE_PERCENT = new_distance
            
            self.send_message(
                f"✅ <b>TRAILING STOP ALTERADO</b>\n\n"
                f"<b>Ativação:</b>\n"
                f"   Anterior: <code>{old_activation}%</code>\n"
                f"   Novo: <code>{new_activation}%</code>\n\n"
                f"<b>Distância:</b>\n"
                f"   Anterior: <code>{old_distance}%</code>\n"
                f"   Novo: <code>{new_distance}%</code>"
            )
            
        except ValueError:
            self.send_message("❌ Valores inválidos.\n\nExemplo: <code>/trailing 0.5 0.25</code>")
    
    # ============================================
    # COMANDOS DE AÇÃO
    # ============================================
    
    def cmd_close_all(self, args: list):
        """Fecha todas as posições."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return
        
        try:
            positions = self.bot.exchange.get_open_positions()
            
            if not positions:
                self.send_message("📍 Nenhuma posição aberta para fechar.")
                return
            
            # Pede confirmação
            if not args or args[0].lower() != 'confirm':
                self.send_message(
                    f"⚠️ <b>FECHAR {len(positions)} POSIÇÕES?</b>\n\n"
                    f"Esta ação é irreversível!\n\n"
                    f"Para confirmar, use:\n<code>/closeall confirm</code>"
                )
                return
            
            self.send_message(f"🔄 Fechando {len(positions)} posições...")
            
            closed_count = 0
            
            for pos in positions:
                try:
                    symbol = pos['symbol']
                    side = pos['side']
                    qty = pos['quantity']
                    
                    # Fecha a posição
                    if side == 'LONG':
                        result = self.bot.exchange.place_market_order(
                            symbol=symbol,
                            side='SELL',
                            position_side='LONG',
                            quantity=qty
                        )
                    else:
                        result = self.bot.exchange.place_market_order(
                            symbol=symbol,
                            side='BUY',
                            position_side='SHORT',
                            quantity=qty
                        )
                    
                    if result:
                        closed_count += 1
                        
                except Exception as e:
                    logger.error(f"Erro ao fechar {pos['symbol']}: {e}")
            
            self.send_message(
                f"✅ <b>POSIÇÕES FECHADAS</b>\n\n"
                f"   Fechadas: <code>{closed_count}/{len(positions)}</code>\n\n"
                f"Use /balance para ver o resultado."
            )
            
        except Exception as e:
            self.send_message(f"❌ Erro ao fechar posições: {e}")
    
    def cmd_help(self, args: list):
        """Mostra lista de comandos."""
        message = """
📚 <b>COMANDOS DISPONÍVEIS</b>
━━━━━━━━━━━━━━━━━━━━━

<b>🎮 CONTROLE:</b>
/start - Iniciar o bot
/stop - Parar o bot
/stop force - Parar e fechar posições
/pause - Pausar (mantém posições)
/resume - Retomar após pausa

<b>📊 INFORMAÇÕES:</b>
/status - Status completo
/portfolio - Evolução da carteira
/trades - Relatório de trades
/dailyreport - Relatório diário (on/off)
/lockinfo - Status do lock
/apihealth - Saúde operacional
/config - Ver configurações
/positions - Posições abertas
/coins - Moedas ativas
/balance - Saldo detalhado

<b>⚙️ CONFIGURAÇÕES:</b>
/leverage [valor] - Alavancagem
/ordersize [valor] - Tamanho ordem
/tp [valor] - Take Profit %
/sl [valor/on/off] - Stop Loss %
/trailing [ativ] [dist] - Trailing

<b>⚡ AÇÕES:</b>
/closeall - Fechar todas posições
/closeall confirm - Confirmar

━━━━━━━━━━━━━━━━━━━━━
<i>Exemplos:</i>
• <code>/leverage 20</code>
• <code>/tp 10</code>
• <code>/dailyreport now</code>
• <code>/sl off</code>
• <code>/trailing 0.5 0.25</code>
"""
        self.send_message(message)
