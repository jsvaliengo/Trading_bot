"""
StateManager: I/O atômico de arquivo de estado JSON do bot.

Responsabilidades (camada de persistência pura):
- Escrita atômica (tmp + rename) com backup automático do arquivo anterior
- Leitura com fallback pro backup em caso de corrupção
- Migração de formato legado (bot_state.json no root → runtime/)

A APLICAÇÃO dos dados no bot (interpretar campos, atribuir a atributos) fica
no próprio bot — esse módulo é stateless exceto pelo lock compartilhado.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class StateManager:
    """Persistência atômica de estado JSON, com backup e fallback."""

    def __init__(self, lock: Optional[threading.Lock] = None):
        # Lock compartilhado com o bot pra serializar leitura/escrita.
        # Se não passado, cria um lock novo — aceitável em testes unitários.
        self._lock = lock or threading.Lock()

    @staticmethod
    def backup_file_path(state_file_path: str) -> str:
        """Convenção: backup sempre com sufixo .bak do arquivo principal."""
        return f"{state_file_path}.bak"

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def save(self, payload: Dict[str, Any], state_file_path: str) -> bool:
        """
        Escreve payload no disco atomicamente (tmp + rename) preservando backup
        do arquivo anterior. Retorna True em sucesso, False em erro.
        """
        try:
            with self._lock:
                self._write_atomic(payload, state_file_path)
            logger.info(f"💾 Estado salvo em {state_file_path}")
            return True
        except Exception as exc:
            logger.error(f"❌ Erro ao salvar estado: {exc}")
            return False

    def load(
        self, state_file_path: str
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Tenta ler o arquivo principal; se ausente/corrompido, cai no backup.

        Returns:
            (payload_or_None, source_path)
            - payload_or_None: dict parseado, {} em reset manual, ou None se nada encontrado
            - source_path: caminho do arquivo lido (principal/backup) ou "" se nada
        """
        with self._lock:
            return self._read_with_fallback(state_file_path)

    @staticmethod
    def migrate_legacy(target_path: str, legacy_path: str) -> None:
        """
        Migra bot_state.json antigo do root pra runtime/, se aplicável.
        No-op se o destino já existe ou se a fonte legada não existe.

        Static porque é chamada no __init__ do bot antes do lock ser criado —
        e essa operação é single-threaded por definição (boot do processo).
        """
        if os.path.exists(target_path):
            return
        if not os.path.exists(legacy_path):
            return
        try:
            target_dir = os.path.dirname(target_path)
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(legacy_path, target_path)
            logger.info(f"📦 Estado legado migrado para runtime: {target_path}")
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao migrar estado legado ({legacy_path}): {exc}")

    # ------------------------------------------------------------------
    # I/O interno
    # ------------------------------------------------------------------

    def _write_atomic(self, payload: Dict[str, Any], state_file_path: str) -> None:
        state_dir = os.path.dirname(state_file_path) or "."
        os.makedirs(state_dir, exist_ok=True)
        tmp_path = f"{state_file_path}.tmp"
        backup_path = self.backup_file_path(state_file_path)

        try:
            with open(tmp_path, 'w') as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            if os.path.exists(state_file_path):
                try:
                    shutil.copy2(state_file_path, backup_path)
                except Exception as exc:
                    logger.warning(
                        f"⚠️ Não foi possível atualizar backup de estado: {exc}"
                    )

            os.replace(tmp_path, state_file_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _read_with_fallback(
        self, state_file_path: str
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        primary_path = state_file_path
        backup_path = self.backup_file_path(state_file_path)

        if os.path.exists(primary_path):
            try:
                with open(primary_path, 'r', encoding='utf-8') as f:
                    primary_raw = f.read()
                primary_stripped = primary_raw.strip()

                # Reset manual comum: arquivo vazio/{} em runtime.
                # Nesses casos, não devemos restaurar o .bak.
                if primary_stripped in {"", "{}", "null"}:
                    logger.info(
                        f"🧹 Estado principal resetado manualmente ({primary_path}). "
                        "Ignorando backup e iniciando do zero."
                    )
                    return {}, primary_path

                return json.loads(primary_raw), primary_path
            except Exception as exc:
                logger.warning(
                    f"⚠️ Estado principal inválido ({primary_path}): {exc}"
                )

        if os.path.exists(backup_path):
            try:
                with open(backup_path, 'r', encoding='utf-8') as f:
                    return json.load(f), backup_path
            except Exception as exc:
                logger.warning(
                    f"⚠️ Backup de estado inválido ({backup_path}): {exc}"
                )

        return None, ""
