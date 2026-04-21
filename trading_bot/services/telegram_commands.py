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
from difflib import get_close_matches
from datetime import datetime, timezone
from typing import Any, List
import requests
from requests.exceptions import RequestException

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
        self.last_rate_update: datetime | None = None
        
        # Controle do polling
        self.running = False
        self.poll_thread = None
        self.last_update_id = 0
        self._poll_timeout_seconds = 8
        self._poll_request_timeout_seconds = 10

        # Referência ao bot principal (será setada depois)
        self.bot = None
        self.config = None
        self._http_session = requests.Session()
        
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
            '/sentiment': self.cmd_sentiment,
            '/config': self.cmd_config,
            '/positions': self.cmd_positions,
            '/coins': self.cmd_coins,
            '/balance': self.cmd_balance,
            '/leverage': self.cmd_leverage,
            '/ordersize': self.cmd_ordersize,
            '/tp': self.cmd_take_profit,
            '/sl': self.cmd_stop_loss,
            '/drawdown': self.cmd_drawdown,
            '/trailing': self.cmd_trailing,
            '/closeall': self.cmd_close_all,
            '/env': self.cmd_env,
            '/help': self.cmd_help,
            '/strategy': self.cmd_strategy,
            '/rescore': self.cmd_rescore,
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
            response = self._request_with_retry(
                method="POST",
                url=url,
                max_attempts=3,
                base_backoff_seconds=0.4,
                timeout=10,
                data=data,
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem: {e}")
            return False

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_attempts: int,
        base_backoff_seconds: float,
        timeout: float,
        **kwargs: Any,
    ):
        """
        Executa request HTTP com retry exponencial para falhas transitórias.
        """
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._http_session.request(method=method, url=url, timeout=timeout, **kwargs)
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    raise RuntimeError(f"HTTP {response.status_code}")
                return response
            except (RequestException, RuntimeError) as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                sleep_seconds = min(3.0, base_backoff_seconds * (2 ** (attempt - 1)))
                time.sleep(sleep_seconds)

        raise RuntimeError(f"Falha em request HTTP após {max_attempts} tentativa(s): {last_error}")

    def _get_usd_brl_rate(self) -> float:
        """
        Retorna cotação USD->BRL com cache de 10 minutos.
        Em caso de falha, mantém a última cotação conhecida.
        """
        now_utc = datetime.now(timezone.utc)

        # Cache hit — pula fetch (inclusive após falha recente, pra não spamar DNS)
        if self.last_rate_update:
            elapsed = (now_utc - self.last_rate_update).total_seconds()
            if elapsed < 600:
                return self.usd_brl_rate

        try:
            response = self._request_with_retry(
                method="GET",
                url="https://economia.awesomeapi.com.br/json/last/USD-BRL",
                max_attempts=2,
                base_backoff_seconds=0.3,
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                bid = float(data.get("USDBRL", {}).get("bid", 0))
                if bid > 0:
                    self.usd_brl_rate = bid
            # Sempre marca última tentativa — protege cache mesmo em falha lógica
            self.last_rate_update = now_utc
        except Exception as e:
            # Falha de rede: log uma vez por ciclo (next retry só em 10min),
            # e marca timestamp pra evitar tempestade de warnings.
            logger.warning(f"⚠️ Não foi possível atualizar cotação USD/BRL: {e}")
            self.last_rate_update = now_utc

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

    def _normalize_pair_symbol(self, symbol: str) -> str:
        """Normaliza ticker para o padrão XXXUSDT."""
        if self.config is not None and hasattr(self.config, "normalize_pair_symbol"):
            return self.config.normalize_pair_symbol(symbol)

        token = str(symbol or "").strip().upper().strip(",;")
        token = token.replace("/", "").replace("-", "").replace("_", "")
        if not token:
            return ""
        if token.endswith("USDT"):
            return token
        return f"{token}USDT"

    def _normalize_pair_list(self, pairs: List[str]) -> List[str]:
        """Normaliza e deduplica lista de símbolos."""
        if self.config is not None and hasattr(self.config, "normalize_pair_list"):
            return self.config.normalize_pair_list(pairs)

        symbols = []
        seen = set()
        for raw_symbol in pairs or []:
            symbol = self._normalize_pair_symbol(raw_symbol)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _base_symbol(self, symbol: str) -> str:
        """Retorna símbolo base sem sufixo USDT."""
        normalized = self._normalize_pair_symbol(symbol)
        return normalized[:-4] if normalized.endswith("USDT") else normalized

    def _get_known_pair_symbols(self) -> List[str]:
        """Retorna universo conhecido de pares válidos para comandos Telegram."""
        sources = []
        sources.extend(self._get_exchange_pair_symbols())

        for attr_name in ("BINANCE_COIN_LIST", "TRADING_PAIRS", "FIXED_PAIRS", "DISABLED_PAIRS"):
            sources.extend(list(getattr(self.config, attr_name, []) or []))

        for profile in list(getattr(self.config, "STRATEGY_PROFILES", []) or []):
            if isinstance(profile, dict):
                sources.extend(list(profile.get("pairs", []) or []))

        return self._normalize_pair_list(sources)

    def _get_exchange_pair_symbols(self) -> List[str]:
        """Retorna pares USDT perpétuos ativos direto da exchange, sem filtrar desabilitados."""
        if self.bot is None or not hasattr(self.bot, "exchange") or self.bot.exchange is None:
            return []

        exchange = self.bot.exchange
        if not hasattr(exchange, "get_exchange_info"):
            return []

        try:
            exchange_info = exchange.get_exchange_info()
        except Exception as exc:
            logger.debug("⚠️ Falha ao buscar universo da exchange para /coins: %s", exc)
            return []

        symbols = []
        for symbol_info in list((exchange_info or {}).get("symbols", []) or []):
            symbol = self._normalize_pair_symbol(symbol_info.get("symbol"))
            if not symbol or not symbol.endswith("USDT"):
                continue
            if symbol_info.get("contractType") != "PERPETUAL":
                continue
            if symbol_info.get("status") != "TRADING":
                continue
            symbols.append(symbol)
        return self._normalize_pair_list(symbols)

    def _prune_unknown_disabled_pairs(self) -> List[str]:
        """Remove pares desabilitados inválidos deixados por versões antigas do comando."""
        if self.config is None:
            return []

        exchange_pairs = set(self._get_exchange_pair_symbols())
        if not exchange_pairs:
            return []

        current_disabled = self._normalize_pair_list(list(getattr(self.config, "DISABLED_PAIRS", []) or []))
        kept = [symbol for symbol in current_disabled if symbol in exchange_pairs]
        removed = [symbol for symbol in current_disabled if symbol not in exchange_pairs]
        if removed:
            self.config.DISABLED_PAIRS = kept
        return removed

    def _parse_coin_symbols(self, args: List[str], *, validate_known: bool = False) -> tuple[list[str], list[dict[str, Any]]]:
        """Converte argumentos do comando em lista de símbolos normalizados."""
        symbols = []
        invalid = []
        seen_symbols = set()
        seen_invalid = set()
        known_pairs: set[str] = (
            set(self._get_known_pair_symbols()) if validate_known and self.config is not None else set()
        )
        known_bases = sorted({self._base_symbol(symbol) for symbol in known_pairs})

        for arg in args:
            for raw_token in str(arg).replace(",", " ").split():
                token = self._base_symbol(raw_token)
                symbol = self._normalize_pair_symbol(raw_token)
                if not symbol:
                    continue
                if validate_known and symbol not in known_pairs:
                    if token not in seen_invalid:
                        seen_invalid.add(token)
                        invalid.append(
                            {
                                "token": token,
                                "suggestions": get_close_matches(token, known_bases, n=3, cutoff=0.6),
                            }
                        )
                    continue
                if symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                symbols.append(symbol)
        return symbols, invalid

    def _format_invalid_coin_symbols(self, invalid_symbols: List[dict[str, Any]]) -> str:
        """Formata resposta de símbolos inválidos com sugestões."""
        if not invalid_symbols:
            return ""

        lines = []
        for item in invalid_symbols:
            token = str(item.get("token", "") or "").upper()
            suggestions = [str(s).upper() for s in (item.get("suggestions") or [])]
            if suggestions:
                lines.append(f"• <code>{token}</code> → talvez <code>{', '.join(suggestions)}</code>")
            else:
                lines.append(f"• <code>{token}</code>")

        return "⚠️ <b>Símbolos não reconhecidos:</b>\n" + "\n".join(lines)

    def _refresh_pairs_after_coin_change(self, action: str) -> dict:
        """Recalcula pares ativos após alteração manual de moedas."""
        if self.bot is not None and hasattr(self.bot, "refresh_trading_pairs"):
            try:
                return self.bot.refresh_trading_pairs(trigger_reason=f"telegram:{action}")
            except Exception as e:
                logger.warning(f"⚠️ Falha ao recarregar pares ({action}): {e}")

        if self.config is None:
            return {
                "old_pairs": [],
                "new_pairs": [],
                "added_pairs": [],
                "removed_pairs": [],
            }

        old_pairs = list(getattr(self.config, "TRADING_PAIRS", []) or [])
        if hasattr(self.config, "filter_disabled_pairs"):
            self.config.TRADING_PAIRS = self.config.filter_disabled_pairs(old_pairs)
        else:
            disabled = {str(item).upper() for item in (getattr(self.config, "DISABLED_PAIRS", []) or [])}
            self.config.TRADING_PAIRS = [
                str(item).upper() for item in old_pairs if str(item).upper() not in disabled
            ]

        old_set = set(old_pairs)
        new_set = set(self.config.TRADING_PAIRS)
        return {
            "old_pairs": old_pairs,
            "new_pairs": list(self.config.TRADING_PAIRS),
            "added_pairs": sorted(new_set - old_set),
            "removed_pairs": sorted(old_set - new_set),
        }

    def _persist_runtime_state(self):
        """Salva estado quando disponível para persistir ajustes via Telegram."""
        if self.bot is not None and hasattr(self.bot, "save_state"):
            try:
                self.bot.save_state()
            except Exception as e:
                logger.warning(f"⚠️ Falha ao salvar estado após comando Telegram: {e}")

    def _get_drawdown_snapshot(self) -> dict[str, Any]:
        """Monta snapshot do drawdown desde o pico para uso nos comandos Telegram."""
        limit_pct = float(getattr(self.config, "MAX_DRAWDOWN_FROM_PEAK_PERCENT", 0) or 0)
        peak_equity = float(getattr(self.bot, "peak_equity", 0) or 0)
        peak_ts = getattr(self.bot, "peak_equity_ts", None)
        current_balance = None
        drawdown_pct = None
        blocked = False
        balance_error = None

        exchange = getattr(self.bot, "exchange", None) if self.bot is not None else None
        if exchange is not None and hasattr(exchange, "get_account_balance"):
            try:
                current_balance = float(exchange.get_account_balance())
            except Exception as exc:
                balance_error = str(exc)

        if peak_equity > 0 and current_balance is not None and current_balance > 0:
            drawdown_pct = max(0.0, (peak_equity - current_balance) / peak_equity * 100)
            blocked = bool(limit_pct > 0 and drawdown_pct >= limit_pct)

        return {
            "limit_pct": limit_pct,
            "peak_equity": peak_equity,
            "peak_ts": peak_ts,
            "current_balance": current_balance,
            "drawdown_pct": drawdown_pct,
            "blocked": blocked,
            "balance_error": balance_error,
        }
    
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
            self.poll_thread.join(timeout=max(5, self._poll_request_timeout_seconds + 2))
        logger.info("📱 Polling de comandos parado")
    
    def _poll_loop(self):
        """Loop principal de polling."""
        while self.running:
            try:
                updates = self._get_updates()
                
                for update in updates:
                    self._process_update(update)
                
                time.sleep(0.2)
                
            except Exception as e:
                logger.error(f"Erro no polling: {e}")
                time.sleep(1)
    
    def _get_updates(self) -> list:
        """Busca atualizações do Telegram."""
        try:
            url = f"{self.base_url}/getUpdates"
            params = {
                "offset": self.last_update_id + 1,
                "timeout": self._poll_timeout_seconds,
                "allowed_updates": ["message"]
            }
            response = self._request_with_retry(
                method="GET",
                url=url,
                max_attempts=2,
                base_backoff_seconds=0.4,
                timeout=self._poll_request_timeout_seconds,
                params=params,
            )
            
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
            command_token = parts[0].lower()
            command = command_token.split("@", 1)[0]
            args = parts[1:] if len(parts) > 1 else []
            
            # Executa o comando
            if command in self.commands:
                logger.info(f"📱 Comando recebido: {command} {' '.join(args)}")
                self.commands[command](args)
            else:
                self.send_message(f"❓ Comando desconhecido: {command}\n\nUse /help para ver os comandos disponíveis.")
                
        except Exception as e:
            logger.error(f"Erro ao processar update: {e}")
            try:
                self.send_message(
                    "❌ Erro ao executar comando.\n"
                    "Verifique os parâmetros e tente novamente."
                )
            except Exception:
                logger.error("Falha ao enviar mensagem de erro para o Telegram.")
    
    # ============================================
    # COMANDOS DE CONTROLE
    # ============================================
    
    def cmd_start(self, args: list):
        """Retoma o bot quando estiver pausado (processo já em execução)."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        if self.bot.running and not getattr(self.bot, 'paused', False):
            self.send_message("⚠️ Bot já está rodando!")
            return

        # Evita falso positivo: mudar running=True aqui NÃO reinicia o loop principal.
        # O processo precisa ser iniciado por supervisor/screen/systemd/GitHub Actions.
        if not self.bot.running:
            self.send_message(
                "⚠️ <b>BOT PARADO</b>\n\n"
                "O comando <code>/start</code> não reinicia o processo do bot.\n"
                "Inicie no servidor (screen/systemd/deploy) e depois use /status."
            )
            return

        # Se chegou aqui, está em execução mas pausado -> retoma.
        self.bot.paused = False
        self.send_message(
            "▶️ <b>BOT RETOMADO</b>\n\n"
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

            # Fecha posições (force_refresh: fechamento em massa, snapshot tem que ser fresco)
            try:
                positions = self.bot.exchange.get_open_positions(force_refresh=True)
            except Exception as exc:
                self.send_message(
                    f"❌ API indisponível ao listar posições: {exc}\n"
                    "Abortando /stop force — tente novamente."
                )
                return
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
                "Use <code>/stop force</code> para fechar todas as posições.\n"
                "Para voltar a operar, reinicie o processo do bot no servidor."
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
            self.send_message("⚠️ Bot não está rodando! Reinicie o processo no servidor e depois use /status")
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

            # Posições abertas — se API falhar, mostra lista vazia em vez de quebrar /status
            try:
                positions = self.bot.exchange.get_open_positions()
            except Exception as exc:
                logger.warning(f"⚠️ /status: API indisponível ao listar posições: {exc}")
                positions = []
            
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
   • Sentimento: <code>{"ON" if getattr(self.bot, "sentiment_mode_enabled", False) else "OFF"}</code>
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

        drawdown = self._get_drawdown_snapshot() if self.bot is not None else {}
        drawdown_limit = float(drawdown.get("limit_pct", 0) or 0)
        drawdown_limit_display = "OFF" if drawdown_limit <= 0 else f"{drawdown_limit:.2f}%"
        peak_equity = float(drawdown.get("peak_equity", 0) or 0)
        current_balance = drawdown.get("current_balance")
        drawdown_pct = drawdown.get("drawdown_pct")
        peak_display = self._format_usd_brl(peak_equity, 2, False) if peak_equity > 0 else "n/d"
        current_display = (
            self._format_usd_brl(float(current_balance), 2, False)
            if current_balance is not None and float(current_balance) >= 0
            else "n/d"
        )
        drawdown_display = f"{float(drawdown_pct):.2f}%" if drawdown_pct is not None else "n/d"
        
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

🧭 <b>SENTIMENTO:</b>
   • Modo: <code>{"ON" if getattr(self.bot, "sentiment_mode_enabled", False) else "OFF"}</code>
   • Timeframe: <code>{self.config.SENTIMENT_TIMEFRAME}</code>
   • Score mínimo: <code>{self.config.SENTIMENT_MIN_SCORE}</code>

🚀 <b>DOUBLE FIRST:</b>
   • LONG: <code>{'ON' if self.config.DOUBLE_FIRST_LONG_ENABLED else 'OFF'}</code>
   • SHORT: <code>{'ON' if self.config.DOUBLE_FIRST_SHORT_ENABLED else 'OFF'}</code>
   • Multiplicador: <code>{self.config.DOUBLE_FIRST_MULTIPLIER:.2f}x</code>
   • Cap de margem: <code>{self._format_usd_brl(self.config.DOUBLE_FIRST_MAX_MARGIN_USDT, 2, False) if self.config.DOUBLE_FIRST_MAX_MARGIN_USDT > 0 else 'sem limite'}</code>
   • Escopo: <code>{self.config.DOUBLE_FIRST_SCOPE}</code>

📉 <b>DRAWDOWN:</b>
   • Limite: <code>{drawdown_limit_display}</code>
   • Pico da sessão: <code>{peak_display}</code>
   • Equity atual: <code>{current_display}</code>
   • Drawdown atual: <code>{drawdown_display}</code>

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

    def cmd_sentiment(self, args: list):
        """Controla filtro de sentimento e consulta viés por par."""
        if self.bot is None or self.config is None:
            self.send_message("❌ Bot/config não disponível")
            return

        if not hasattr(self.bot, "set_sentiment_mode") or not hasattr(self.bot, "get_sentiment_snapshot"):
            self.send_message("⚠️ Esta versão do bot não suporta /sentiment.")
            return

        if not args or args[0].strip().lower() in {"status", "info"}:
            enabled = bool(getattr(self.bot, "sentiment_mode_enabled", False))
            status = "ON" if enabled else "OFF"
            self.send_message(
                f"🧭 <b>FILTRO DE SENTIMENTO</b>\n\n"
                f"• Modo atual: <code>{status}</code>\n"
                f"• Timeframe: <code>{self.config.SENTIMENT_TIMEFRAME}</code>\n"
                f"• Lookback: <code>{self.config.SENTIMENT_CANDLES_LOOKBACK}</code> candles\n"
                f"• Score mínimo: <code>{self.config.SENTIMENT_MIN_SCORE}</code>\n"
                f"• Momentum mínimo: <code>{self.config.SENTIMENT_MIN_MOMENTUM_PERCENT:.2f}%</code>\n\n"
                f"Uso:\n"
                f"• <code>/sentiment on</code> (ativar filtro)\n"
                f"• <code>/sentiment off</code> (modo normal)\n"
                f"• <code>/sentiment SOL</code> (consultar viés do par)"
            )
            return

        action = args[0].strip().lower()
        if action in {"on", "enable", "ativar"}:
            self.bot.set_sentiment_mode(True, persist=True)
            self.send_message(
                "✅ <b>Filtro de sentimento ATIVADO</b>\n\n"
                "Entradas novas só serão abertas na direção do viés detectado."
            )
            return

        if action in {"off", "disable", "normal"}:
            self.bot.set_sentiment_mode(False, persist=True)
            self.send_message(
                "✅ <b>Modo normal restaurado</b>\n\n"
                "Filtro de sentimento DESATIVADO. O bot voltou ao fluxo padrão de sinais."
            )
            return

        symbol_arg = args[1] if action in {"check", "pair"} and len(args) > 1 else args[0]
        symbol = self._normalize_pair_symbol(symbol_arg)
        if not symbol:
            self.send_message("❌ Símbolo inválido. Exemplo: <code>/sentiment SOL</code>")
            return

        snapshot = self.bot.get_sentiment_snapshot(symbol, force_refresh=True)
        direction = str(snapshot.get("direction", "BOTH")).upper()
        if direction == "LONG_ONLY":
            allowed = "Somente LONG (compra)"
        elif direction == "SHORT_ONLY":
            allowed = "Somente SHORT (venda)"
        else:
            allowed = "Neutro (LONG/SHORT)"

        updated_at = str(snapshot.get("updated_at", "") or "")
        if "T" in updated_at:
            updated_at = updated_at.replace("T", " ").split("+")[0]

        self.send_message(
            f"🧭 <b>VIÉS DE MERCADO - {symbol}</b>\n\n"
            f"• Bias: <code>{snapshot.get('bias', 'NEUTRAL')}</code>\n"
            f"• Direção permitida: <code>{allowed}</code>\n"
            f"• Score: <code>{snapshot.get('score', 0)}</code>\n"
            f"• RSI: <code>{float(snapshot.get('rsi', 50.0)):.2f}</code>\n"
            f"• Momentum: <code>{float(snapshot.get('momentum_pct', 0.0)):+.2f}%</code>\n"
            f"• Timeframe: <code>{snapshot.get('timeframe', self.config.SENTIMENT_TIMEFRAME)}</code>\n"
            f"• Motivo: <code>{snapshot.get('reason', '-')}</code>\n"
            f"• Atualizado: <code>{updated_at or 'agora'}</code>"
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
        """Mostra/gerencia moedas ativas."""
        if self.config is None:
            self.send_message("❌ Config não disponível")
            return

        pruned_disabled = self._prune_unknown_disabled_pairs()
        if pruned_disabled:
            self._persist_runtime_state()

        def _display(symbols: list) -> str:
            return ", ".join([s.replace("USDT", "") for s in symbols]) if symbols else "-"

        if not args:
            active_pairs = list(getattr(self.config, "TRADING_PAIRS", []) or [])
            disabled_pairs = list(getattr(self.config, "DISABLED_PAIRS", []) or [])
            candidate_pairs = list(getattr(self.config, "BINANCE_COIN_LIST", []) or [])
            pruned_notice = ""
            if pruned_disabled:
                pruned_notice = (
                    f"🧹 <b>Removidos inválidos:</b> <code>{_display(pruned_disabled)}</code>\n\n"
                )
            self.send_message(
                f"🪙 <b>MOEDAS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ <b>Ativas ({len(active_pairs)}):</b>\n"
                f"{_display(active_pairs)}\n\n"
                f"⛔ <b>Desabilitadas ({len(disabled_pairs)}):</b>\n"
                f"{_display(disabled_pairs)}\n\n"
                f"📚 <b>Lista permitida ({len(candidate_pairs)}):</b>\n"
                f"{_display(candidate_pairs)}\n\n"
                f"{pruned_notice}"
                f"Uso:\n"
                f"• <code>/coins disable ETH SOL ADA</code>\n"
                f"• <code>/coins enable ETH</code>\n"
                f"• <code>/coins add MATIC</code>"
            )
            return

        action = str(args[0]).strip().lower()
        validate_known = action in {"disable", "off", "enable", "on"}
        symbols, invalid_symbols = self._parse_coin_symbols(args[1:], validate_known=validate_known)
        invalid_block = self._format_invalid_coin_symbols(invalid_symbols)

        if action in {"disable", "off"}:
            if not symbols:
                detail = f"\n\n{invalid_block}" if invalid_block else ""
                self.send_message(
                    "❌ Informe ao menos 1 moeda.\n\n"
                    "Exemplo:\n"
                    "<code>/coins disable ETH SOL ADA</code>"
                    f"{detail}"
                )
                return

            current_disabled = list(getattr(self.config, "DISABLED_PAIRS", []) or [])
            disabled_set = {str(item).upper() for item in current_disabled}
            added = []
            already = []
            for symbol in symbols:
                if symbol in disabled_set:
                    already.append(symbol)
                    continue
                current_disabled.append(symbol)
                disabled_set.add(symbol)
                added.append(symbol)

            if hasattr(self.config, "normalize_pair_list"):
                self.config.DISABLED_PAIRS = self.config.normalize_pair_list(current_disabled)
            else:
                self.config.DISABLED_PAIRS = sorted(disabled_set)

            refresh_info = self._refresh_pairs_after_coin_change("coins-disable")
            self._persist_runtime_state()

            self.send_message(
                f"⛔ <b>PARES DESABILITADOS</b>\n\n"
                f"✅ Novos: <code>{_display(added)}</code>\n"
                f"ℹ️ Já estavam: <code>{_display(already)}</code>\n"
                f"{invalid_block + chr(10) + chr(10) if invalid_block else ''}"
                f"🧾 Ativos agora ({len(refresh_info.get('new_pairs', []))}):\n"
                f"<code>{_display(refresh_info.get('new_pairs', []))}</code>"
            )
            return

        if action in {"enable", "on"}:
            if not symbols:
                detail = f"\n\n{invalid_block}" if invalid_block else ""
                self.send_message(
                    "❌ Informe ao menos 1 moeda.\n\n"
                    "Exemplo:\n"
                    "<code>/coins enable ETH</code>"
                    f"{detail}"
                )
                return

            current_disabled = list(getattr(self.config, "DISABLED_PAIRS", []) or [])
            disabled_set = {str(item).upper() for item in current_disabled}

            enabled_now = []
            not_disabled = []
            for symbol in symbols:
                if symbol in disabled_set:
                    disabled_set.remove(symbol)
                    enabled_now.append(symbol)
                else:
                    not_disabled.append(symbol)

            self.config.DISABLED_PAIRS = sorted(disabled_set)
            refresh_info = self._refresh_pairs_after_coin_change("coins-enable")
            self._persist_runtime_state()

            self.send_message(
                f"✅ <b>PARES HABILITADOS</b>\n\n"
                f"✅ Reabilitados: <code>{_display(enabled_now)}</code>\n"
                f"ℹ️ Já estavam habilitados: <code>{_display(not_disabled)}</code>\n"
                f"{invalid_block + chr(10) + chr(10) if invalid_block else ''}"
                f"🧾 Ativos agora ({len(refresh_info.get('new_pairs', []))}):\n"
                f"<code>{_display(refresh_info.get('new_pairs', []))}</code>"
            )
            return

        if action == "add":
            if not symbols:
                self.send_message(
                    "❌ Informe ao menos 1 moeda.\n\n"
                    "Exemplo:\n"
                    "<code>/coins add MATIC</code>"
                )
                return

            current_candidates = list(getattr(self.config, "BINANCE_COIN_LIST", []) or [])
            candidate_set = {str(item).upper() for item in current_candidates}
            current_disabled = {str(item).upper() for item in (getattr(self.config, "DISABLED_PAIRS", []) or [])}

            added = []
            already = []
            reenabled = []

            for symbol in symbols:
                if symbol in candidate_set:
                    already.append(symbol)
                else:
                    current_candidates.append(symbol)
                    candidate_set.add(symbol)
                    added.append(symbol)

                if symbol in current_disabled:
                    current_disabled.remove(symbol)
                    reenabled.append(symbol)

            if hasattr(self.config, "normalize_pair_list"):
                self.config.BINANCE_COIN_LIST = self.config.normalize_pair_list(current_candidates)
                self.config.DISABLED_PAIRS = self.config.normalize_pair_list(list(current_disabled))
            else:
                self.config.BINANCE_COIN_LIST = current_candidates
                self.config.DISABLED_PAIRS = sorted(current_disabled)

            # Sem estratégia automática, adiciona também na lista ativa.
            if not getattr(self.config, "USE_BINANCE_STRATEGY", False) and not getattr(self.config, "AUTO_SELECT_PAIRS", False):
                active = list(getattr(self.config, "TRADING_PAIRS", []) or [])
                for symbol in symbols:
                    if symbol not in active:
                        active.append(symbol)
                if hasattr(self.config, "filter_disabled_pairs"):
                    self.config.TRADING_PAIRS = self.config.filter_disabled_pairs(active)
                else:
                    self.config.TRADING_PAIRS = active

            refresh_info = self._refresh_pairs_after_coin_change("coins-add")
            self._persist_runtime_state()

            self.send_message(
                f"➕ <b>PARES ADICIONADOS</b>\n\n"
                f"✅ Novos na lista permitida: <code>{_display(added)}</code>\n"
                f"ℹ️ Já existiam: <code>{_display(already)}</code>\n"
                f"🔓 Reabilitados automaticamente: <code>{_display(reenabled)}</code>\n"
                f"🧾 Ativos agora ({len(refresh_info.get('new_pairs', []))}):\n"
                f"<code>{_display(refresh_info.get('new_pairs', []))}</code>"
            )
            return

        self.send_message(
            "❌ Ação inválida para /coins.\n\n"
            "Use:\n"
            "• <code>/coins</code>\n"
            "• <code>/coins disable ETH SOL ADA</code>\n"
            "• <code>/coins enable ETH</code>\n"
            "• <code>/coins add MATIC</code>"
        )
    
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
            self._persist_runtime_state()

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
            self._persist_runtime_state()

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
            self._persist_runtime_state()
            self.send_message(f"✅ Stop Loss <b>ATIVADO</b> ({self.config.STOP_LOSS_PERCENT}%)")
            return

        if arg == 'off':
            self.config.USE_INDIVIDUAL_STOP_LOSS = False
            self._persist_runtime_state()
            self.send_message("✅ Stop Loss <b>DESATIVADO</b>")
            return
        
        try:
            new_sl = float(arg)
            
            if new_sl < 0.1 or new_sl > 100:
                self.send_message("❌ Stop Loss deve ser entre 0.1% e 100%")
                return
            
            old_sl = self.config.STOP_LOSS_PERCENT
            self.config.STOP_LOSS_PERCENT = new_sl
            self._persist_runtime_state()

            self.send_message(
                f"✅ <b>STOP LOSS ALTERADO</b>\n\n"
                f"   Anterior: <code>{old_sl}%</code>\n"
                f"   Novo: <code>{new_sl}%</code>\n\n"
                f"⚠️ Aplicado às NOVAS posições."
            )
            
        except ValueError:
            self.send_message("❌ Valor inválido.\n\nExemplos:\n• <code>/sl 5</code>\n• <code>/sl on</code>\n• <code>/sl off</code>")

    def cmd_drawdown(self, args: list):
        """Consulta, ajusta ou reseta a proteção de drawdown desde o pico."""
        if self.config is None or self.bot is None:
            self.send_message("❌ Bot/config não disponível")
            return

        snapshot = self._get_drawdown_snapshot()
        limit_pct = float(snapshot["limit_pct"])
        peak_equity = float(snapshot["peak_equity"])
        current_balance = snapshot["current_balance"]
        drawdown_pct = snapshot["drawdown_pct"]
        peak_ts = snapshot["peak_ts"]
        limit_display = "OFF" if limit_pct <= 0 else f"{limit_pct:.2f}%"
        peak_display = self._format_usd_brl(peak_equity, 2, False) if peak_equity > 0 else "n/d"
        current_display = (
            self._format_usd_brl(float(current_balance), 2, False)
            if current_balance is not None and float(current_balance) >= 0
            else "n/d"
        )
        drawdown_display = f"{float(drawdown_pct):.2f}%" if drawdown_pct is not None else "n/d"
        peak_ts_display = peak_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if isinstance(peak_ts, datetime) else "n/d"
        blocked_display = "SIM" if snapshot["blocked"] else "NÃO"

        if not args or args[0].strip().lower() in {"status", "info"}:
            extra = ""
            if snapshot["balance_error"]:
                extra = f"\n⚠️ Saldo atual indisponível: <code>{snapshot['balance_error']}</code>"
            self.send_message(
                f"📉 <b>DRAWDOWN DESDE O PICO</b>\n\n"
                f"• Limite: <code>{limit_display}</code>\n"
                f"• Pico da sessão: <code>{peak_display}</code>\n"
                f"• Pico registrado em: <code>{peak_ts_display}</code>\n"
                f"• Equity atual: <code>{current_display}</code>\n"
                f"• Drawdown atual: <code>{drawdown_display}</code>\n"
                f"• Entradas bloqueadas: <code>{blocked_display}</code>"
                f"{extra}\n\n"
                f"Uso:\n"
                f"• <code>/drawdown reset</code>\n"
                f"• <code>/drawdown off</code>\n"
                f"• <code>/drawdown 40</code>"
            )
            return

        action = args[0].strip().lower()

        if action in {"reset", "clear"}:
            if current_balance is not None and float(current_balance) > 0:
                self.bot.peak_equity = float(current_balance)
                self.bot.peak_equity_ts = datetime.now(timezone.utc)
                self._persist_runtime_state()
                self.send_message(
                    f"✅ <b>PICO DE EQUITY RESETADO</b>\n\n"
                    f"• Novo pico: <code>{self._format_usd_brl(float(current_balance), 2, False)}</code>\n"
                    f"• Limite atual: <code>{limit_display}</code>\n\n"
                    f"Novas entradas deixam de ser bloqueadas por esse drawdown acumulado."
                )
                return

            self.bot.peak_equity = 0.0
            self.bot.peak_equity_ts = None
            self._persist_runtime_state()
            self.send_message(
                "✅ <b>PICO DE EQUITY LIMPO</b>\n\n"
                "Não foi possível ler o saldo atual, então o pico foi zerado.\n"
                "No próximo ciclo o bot recalcula a referência automaticamente."
            )
            return

        if action in {"off", "disable"}:
            self.config.MAX_DRAWDOWN_FROM_PEAK_PERCENT = 0.0
            self._persist_runtime_state()
            self.send_message(
                "✅ <b>PROTEÇÃO DE DRAWDOWN DESATIVADA</b>\n\n"
                "O bot não vai mais bloquear novas entradas por drawdown desde o pico."
            )
            return

        try:
            new_limit = float(action)
        except ValueError:
            self.send_message(
                "❌ Opção inválida.\n\n"
                "Use:\n"
                "• <code>/drawdown</code>\n"
                "• <code>/drawdown reset</code>\n"
                "• <code>/drawdown off</code>\n"
                "• <code>/drawdown 40</code>"
            )
            return

        if new_limit < 0 or new_limit > 100:
            self.send_message("❌ O limite de drawdown deve ficar entre 0% e 100%.")
            return

        self.config.MAX_DRAWDOWN_FROM_PEAK_PERCENT = new_limit
        self._persist_runtime_state()

        suffix = ""
        if drawdown_pct is not None and new_limit > 0:
            if float(drawdown_pct) >= new_limit:
                suffix = (
                    f"\n\n⚠️ O drawdown atual ainda está em <code>{float(drawdown_pct):.2f}%</code>."
                    f"\nNovas entradas continuarão bloqueadas até você usar <code>/drawdown reset</code>"
                    f"\nou aumentar mais o limite."
                )
            else:
                suffix = (
                    f"\n\n✅ Com drawdown atual em <code>{float(drawdown_pct):.2f}%</code>,"
                    f"\nas novas entradas já ficam liberadas."
                )

        state_label = "desativado" if new_limit == 0 else f"{new_limit:.2f}%"
        self.send_message(
            f"✅ <b>LIMITE DE DRAWDOWN ATUALIZADO</b>\n\n"
            f"• Novo limite: <code>{state_label}</code>"
            f"{suffix}"
        )

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
    
    def cmd_env(self, args: list):
        """
        Mostra/troca a rede Binance ativa (mainnet/testnet).

        Uso:
          /env                         — mostra status atual
          /env testnet                 — troca para testnet
          /env mainnet                 — exige confirmação adicional
          /env mainnet confirmar       — efetiva a troca para mainnet
        """
        if self.bot is None or self.config is None:
            self.send_message("❌ Bot não configurado")
            return

        current = str(getattr(self.config, "ENVIRONMENT", "") or "").lower()
        current_label = current.upper() if current else "?"

        # Sem args → status
        if not args:
            has_mainnet = self.config.has_credentials_for("mainnet")
            has_testnet = self.config.has_credentials_for("testnet")
            with self.bot._positions_lock:
                open_count = sum(1 for p in self.bot.positions.values() if p)

            state_file = getattr(self.config, "STATE_FILE_NAME", "?")
            self.send_message(
                f"🌐 <b>REDE BINANCE</b>\n\n"
                f"• Ativa: <b>{current_label}</b>{' 💰' if current == 'mainnet' else ' 🧪'}\n"
                f"• State: <code>{state_file}</code>\n"
                f"• Posições abertas: <code>{open_count}</code>\n\n"
                f"<b>Credenciais configuradas:</b>\n"
                f"• Mainnet: {'✅' if has_mainnet else '❌'}\n"
                f"• Testnet: {'✅' if has_testnet else '❌'}\n\n"
                f"<b>Uso:</b>\n"
                f"<code>/env testnet</code>\n"
                f"<code>/env mainnet confirmar</code>\n\n"
                f"<i>Troca é bloqueada se houver posições abertas.</i>"
            )
            return

        target = str(args[0] or "").strip().lower()
        if target not in {"mainnet", "testnet"}:
            self.send_message(
                f"❌ Rede inválida: <code>{target}</code>\n"
                f"Use <code>/env mainnet</code> ou <code>/env testnet</code>."
            )
            return

        # Mainnet exige confirmação explícita
        if target == "mainnet":
            confirm = args[1].strip().lower() if len(args) > 1 and args[1] else ""
            if confirm != "confirmar":
                self.send_message(
                    f"⚠️ <b>ATENÇÃO — TROCA PARA MAINNET</b>\n\n"
                    f"Mainnet opera com <b>DINHEIRO REAL</b>.\n"
                    f"Rede atual: <b>{current_label}</b>\n\n"
                    f"Para confirmar, envie:\n"
                    f"<code>/env mainnet confirmar</code>"
                )
                return

        success, message = self.bot.switch_environment(target)
        prefix = "" if success else ""
        self.send_message(f"{prefix}{message}")

    def cmd_close_all(self, args: list):
        """Fecha todas as posições."""
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        try:
            # force_refresh: /closeall é ação destrutiva; snapshot tem que ser fresco
            positions = self.bot.exchange.get_open_positions(force_refresh=True)

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
            failures = []

            for pos in positions:
                symbol = pos.get('symbol', 'UNKNOWN')
                side = pos.get('side', 'UNKNOWN')
                qty = pos.get('quantity', 0)
                try:
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
                    else:
                        failures.append(f"{side} {symbol}: retorno vazio da exchange")

                except Exception as e:
                    failures.append(f"{side} {symbol}: {e}")
                    logger.error(f"Erro ao fechar {symbol}: {e}")

            total_positions = len(positions)
            if failures:
                preview = "\n".join([f"• {item}" for item in failures[:5]])
                if len(failures) > 5:
                    preview += f"\n• ... e mais {len(failures) - 5} falha(s)"

                self.send_message(
                    f"⚠️ <b>FECHAMENTO PARCIAL</b>\n\n"
                    f"✅ Fechadas: <code>{closed_count}/{total_positions}</code>\n"
                    f"❌ Falhas: <code>{len(failures)}</code>\n\n"
                    f"<b>Detalhes:</b>\n{preview}\n\n"
                    f"Use /positions para confirmar se ainda restou posição aberta."
                )
            else:
                self.send_message(
                    f"✅ <b>POSIÇÕES FECHADAS</b>\n\n"
                    f"   Fechadas: <code>{closed_count}/{total_positions}</code>\n\n"
                    f"Use /balance para ver o resultado."
                )

        except Exception as e:
            self.send_message(f"❌ Erro ao fechar posições: {e}")
    
    def cmd_strategy(self, args: list):
        """Ativa, desativa ou lista estratégias em runtime.

        Uso:
          /strategy              → lista estratégias
          /strategy enable  <nome>  → ativa
          /strategy disable <nome>  → desativa
        """
        if self.bot is None:
            self.send_message("❌ Bot não configurado")
            return

        try:
            if not args:
                self.send_message(self.bot.list_strategies())
                return

            action = args[0].lower()
            if action not in ("enable", "disable"):
                self.send_message(
                    "❌ Ação inválida. Use:\n"
                    "• <code>/strategy enable &lt;nome&gt;</code>\n"
                    "• <code>/strategy disable &lt;nome&gt;</code>"
                )
                return

            if len(args) < 2:
                self.send_message(f"❌ Informe o nome da estratégia. Ex: <code>/strategy {action} range_scalp_v1</code>")
                return

            name = args[1]
            result = self.bot.set_strategy_enabled(name, enabled=(action == "enable"))
            self.send_message(result)

        except Exception as e:
            self.send_message(f"❌ Erro ao alterar estratégia: {e}")

    def cmd_rescore(self, args: list):
        """Executa o rescore de pares imediatamente e reprograma o próximo para 6h."""
        if self.bot is None:
            self.send_message("❌ Bot não disponível.")
            return

        self.send_message("🔄 <b>Iniciando rescore de pares...</b>\n<i>Calculando scores de volatilidade, volume, tendência e funding...</i>")

        try:
            result = self.bot.trigger_pair_rescore()
            pairs = result.get("pairs", [])
            next_in = result.get("next_rescore_in", "6h")
            coins_display = "\n".join(
                f"  {i+1}. {p.replace('USDT', '')}" for i, p in enumerate(pairs)
            )
            self.send_message(
                f"✅ <b>RESCORE CONCLUÍDO</b>\n\n"
                f"🪙 <b>Top {len(pairs)} pares selecionados:</b>\n{coins_display}\n\n"
                f"⏰ Próximo rescore automático em <b>{next_in}</b>"
            )
        except Exception as e:
            logger.error("Erro no cmd_rescore: %s", e)
            self.send_message(f"❌ Erro ao executar rescore: {e}")

    def cmd_help(self, args: list):
        """Mostra lista de comandos."""
        message = """
📚 <b>COMANDOS DISPONÍVEIS</b>
━━━━━━━━━━━━━━━━━━━━━

<b>🎮 CONTROLE:</b>
/start - Retomar se pausado
/stop - Parar o bot
/stop force - Parar e fechar posições
/pause - Pausar (mantém posições)
/resume - Retomar após pausa

<b>📊 INFORMAÇÕES:</b>
/status - Status completo
/portfolio - Evolução da carteira
/trades - Relatório de trades
/dailyreport - Relatório diário (on/off)
/sentiment - Filtro de viés (on/off/par)
/lockinfo - Status do lock
/apihealth - Saúde operacional
/config - Ver configurações
/positions - Posições abertas
/coins - Moedas ativas e gestão
/balance - Saldo detalhado

<b>⚙️ CONFIGURAÇÕES:</b>
/leverage [valor] - Alavancagem
/ordersize [valor] - Tamanho ordem
/tp [valor] - Take Profit %
/sl [valor/on/off] - Stop Loss %
/drawdown [valor/off/reset] - Limite e reset
/trailing [ativ] [dist] - Trailing

<b>🧠 ESTRATÉGIAS:</b>
/strategy - Listar estratégias
/strategy enable &lt;nome&gt; - Ativar
/strategy disable &lt;nome&gt; - Desativar

<b>⚡ AÇÕES:</b>
/closeall - Fechar todas posições
/closeall confirm - Confirmar
/rescore - Forçar rescore de pares agora

<b>🌐 REDE BINANCE:</b>
/env - Ver rede ativa e credenciais
/env testnet - Trocar para testnet
/env mainnet confirmar - Trocar para mainnet

━━━━━━━━━━━━━━━━━━━━━
<i>Exemplos:</i>
• <code>/leverage 20</code>
• <code>/tp 10</code>
• <code>/dailyreport now</code>
• <code>/sentiment on</code>
• <code>/sentiment SOL</code>
• <code>/sl off</code>
• <code>/drawdown reset</code>
• <code>/trailing 0.5 0.25</code>
• <code>/coins disable ETH SOL ADA</code>
"""
        self.send_message(message)
