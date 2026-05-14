"""
WebSocket user data stream da Binance Futures.

Recebe eventos em tempo real de mudanças de conta/posições/ordens e dispara
callbacks registrados. Usado pra invalidar caches (posições, balance) no
momento exato que Binance confirma alteração — sem polling e sem latência
de TTL.

Eventos relevantes:
- ACCOUNT_UPDATE: balance / posição mudou (margin call, execução, funding,
  transferência). Invalida cache de balance e de positions.
- ORDER_TRADE_UPDATE: ciclo de vida de uma ordem. Útil pra detectar fills
  e atualizar posições mesmo antes de ACCOUNT_UPDATE chegar.
- listenKeyExpired: chave expirou; lib reabre sozinha mas logamos.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from binance import ThreadedWebsocketManager

logger = logging.getLogger(__name__)

# Tipos de erro do python-binance que indicam que o read loop morreu e o socket
# precisa ser recriado. Sem isso, o TWM fica num laço apertado emitindo o mesmo
# erro pelo callback e o stream nunca volta — observado em produção testnet com
# >130k ocorrências em ~50MB de log.
_TERMINAL_ERROR_TYPES = frozenset({
    "ReadLoopClosed",
    "BinanceWebsocketUnableToConnect",
})

# Backoff exponencial entre tentativas de restart (segundos). Após o último,
# repete o último valor (mantém tentando, mas espaçado).
_RESTART_BACKOFF_SECONDS = (5, 15, 60, 300, 600)


class UserStreamMonitor:
    """
    Monitor de user data stream. Thread-safe.

    Lifecycle:
      1. `start()` — inicia TWM dedicado + abre futures_user_socket
      2. Callbacks do TWM chamam os handlers registrados (on_account_update,
         on_order_update) em thread própria do SDK
      3. `stop()` — derruba socket e TWM

    Os handlers recebem o dict da mensagem original da Binance. Nenhum
    processamento é feito aqui: a responsabilidade é só rotear.
    """

    def __init__(
        self,
        twm: ThreadedWebsocketManager,
        on_account_update: Optional[Callable[[dict], None]] = None,
        on_order_update: Optional[Callable[[dict], None]] = None,
    ):
        """
        Args:
            twm: ThreadedWebsocketManager JÁ INICIADO e COMPARTILHADO com outros
                streams do processo. Criar um segundo TWM no mesmo processo
                causa "This event loop is already running" em Python 3.13.
        """
        self._twm = twm
        self._on_account_update = on_account_update
        self._on_order_update = on_order_update

        self._socket_id: Optional[str] = None
        self._lock = threading.Lock()
        self._started = False
        self._shutdown = False

        self._message_count = 0
        self._account_update_count = 0
        self._order_update_count = 0
        self._last_message_ts: float = 0.0
        self._error_count = 0

        self._terminal_error_count = 0
        self._restart_in_progress = False
        self._restart_attempts = 0
        self._restart_success_count = 0
        self._restart_failure_count = 0
        self._last_restart_ts: float = 0.0

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return True
            if self._shutdown:
                return False
            if self._twm is None:
                return False
            try:
                self._socket_id = self._twm.start_futures_user_socket(
                    callback=self._on_message
                )
                self._started = True
                logger.info("🔌 UserStreamMonitor iniciado (TWM compartilhado)")
                return True
            except Exception as exc:
                logger.error(f"❌ Falha ao iniciar UserStreamMonitor: {exc}")
                return False

    def stop(self) -> None:
        """Para apenas este socket; NÃO para o TWM (é compartilhado)."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            twm = self._twm
            sock_id = self._socket_id
            self._socket_id = None
            self._started = False

        if twm is not None and sock_id:
            try:
                twm.stop_socket(sock_id)
            except Exception:
                pass
        logger.info("🔌 UserStreamMonitor parado (TWM permanece)")

    # ------------------------------------------------------------------
    # Callback
    # ------------------------------------------------------------------

    def _on_message(self, msg: Any) -> None:
        try:
            if not isinstance(msg, dict):
                return
            # Erros são devolvidos pelo TWM no próprio callback
            if msg.get("e") == "error":
                self._error_count += 1
                err_type = msg.get("type", "")
                if err_type in _TERMINAL_ERROR_TYPES:
                    self._handle_terminal_error(err_type, msg)
                else:
                    logger.warning(f"⚠️ UserStream error: {msg}")
                return

            self._message_count += 1
            self._last_message_ts = time.monotonic()

            event_type = msg.get("e")
            if event_type == "ACCOUNT_UPDATE":
                self._account_update_count += 1
                if self._on_account_update is not None:
                    self._on_account_update(msg)
            elif event_type == "ORDER_TRADE_UPDATE":
                self._order_update_count += 1
                if self._on_order_update is not None:
                    self._on_order_update(msg)
            elif event_type == "listenKeyExpired":
                logger.warning("⚠️ UserStream listenKey expirou — TWM reabrirá")
        except Exception as exc:
            # Nunca propaga: thread do TWM não pode morrer
            logger.error(f"UserStreamMonitor callback falhou: {exc}")

    # ------------------------------------------------------------------
    # Restart automático em erro terminal
    # ------------------------------------------------------------------

    def _handle_terminal_error(self, err_type: str, msg: dict) -> None:
        """
        Trata erros que indicam read loop morto (ReadLoopClosed etc).

        O TWM emite esses erros em loop apertado uma vez que o read loop
        interno do socket morre. Sem este handler o stream nunca se recupera
        e o log infla com milhares de linhas idênticas por segundo.
        """
        self._terminal_error_count += 1
        # Throttle de log: 1ª, depois a cada 1000.
        if self._terminal_error_count == 1 or self._terminal_error_count % 1000 == 0:
            logger.warning(
                f"⚠️ UserStream {err_type} #{self._terminal_error_count}: {msg}"
            )

        # Já existe restart em andamento ou monitor foi parado: nada a fazer.
        if self._shutdown:
            return
        with self._lock:
            if self._restart_in_progress or self._shutdown:
                return
            self._restart_in_progress = True

        thread = threading.Thread(
            target=self._restart_socket_loop,
            name="UserStreamMonitor-restart",
            daemon=True,
        )
        thread.start()

    def _restart_socket_loop(self) -> None:
        """
        Tenta restart com backoff exponencial. Roda em thread própria pra não
        bloquear o callback do TWM. Sai quando consegue um socket novo ou
        quando `stop()` é chamado.
        """
        try:
            while not self._shutdown:
                idx = min(self._restart_attempts, len(_RESTART_BACKOFF_SECONDS) - 1)
                wait_s = _RESTART_BACKOFF_SECONDS[idx]
                self._restart_attempts += 1
                logger.warning(
                    f"🔄 UserStream restart tentativa #{self._restart_attempts} "
                    f"em {wait_s}s"
                )
                # Espera em pequenos slots pra reagir a stop() rapidamente.
                slept = 0.0
                while slept < wait_s and not self._shutdown:
                    time.sleep(min(1.0, wait_s - slept))
                    slept += 1.0
                if self._shutdown:
                    return

                if self._try_restart_once():
                    self._restart_success_count += 1
                    self._restart_attempts = 0
                    self._terminal_error_count = 0
                    self._last_restart_ts = time.monotonic()
                    logger.info(
                        f"✅ UserStream restart OK (sucessos={self._restart_success_count})"
                    )
                    return
                self._restart_failure_count += 1
        finally:
            with self._lock:
                self._restart_in_progress = False

    def _try_restart_once(self) -> bool:
        """Para o socket atual (se houver) e abre um novo no mesmo TWM."""
        twm = self._twm
        if twm is None:
            return False

        old_sock = self._socket_id
        if old_sock:
            try:
                twm.stop_socket(old_sock)
            except Exception as exc:
                logger.debug(f"stop_socket falhou (ignorado): {exc}")

        try:
            new_sock = twm.start_futures_user_socket(callback=self._on_message)
        except Exception as exc:
            logger.warning(f"⚠️ start_futures_user_socket falhou no restart: {exc}")
            return False

        if not new_sock:
            return False
        with self._lock:
            self._socket_id = new_sock
            self._started = True
        return True

    # ------------------------------------------------------------------
    # Telemetria
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        last_age = (time.monotonic() - self._last_message_ts) if self._last_message_ts else None
        return {
            "started": self._started,
            "message_count": self._message_count,
            "account_update_count": self._account_update_count,
            "order_update_count": self._order_update_count,
            "error_count": self._error_count,
            "terminal_error_count": self._terminal_error_count,
            "restart_in_progress": self._restart_in_progress,
            "restart_attempts": self._restart_attempts,
            "restart_success_count": self._restart_success_count,
            "restart_failure_count": self._restart_failure_count,
            "last_message_age_seconds": last_age,
        }
