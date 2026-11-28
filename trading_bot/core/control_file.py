"""
Canal de controle por arquivo entre o dashboard (processo da conta 1) e o worker de uma conta
(docs/PLATAFORMA.md §4). O dashboard escreve `runtime/accounts/N/control.json`; o worker lê no
ciclo do monitor (bot._apply_control_file), aplica e responde em `control_ack.json`.

Formato do control.json:
    {"paused": bool, "new_cycles": bool,
     "commands": [{"id": str, "cmd": "close_all"|"close_position", "symbol": str|None,
                   "side": str|None, "reason": str}]}

`paused` e os comandos são consumidos pelo worker (somem do arquivo depois de aplicados);
`new_cycles` é estado (fica): False = sem ciclo novo no martingale, ciclos abertos terminam.
Escritas são atômicas (tmp + rename), como state_manager.py. read_control nunca levanta.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONTROL_FILE = "control.json"
ACK_FILE = "control_ack.json"
COMMANDS = ("close_all", "close_position")
ACK_KEEP = 50          # acks guardados no arquivo (o dashboard procura o seu por id)

_warned: Dict[str, str] = {}   # path → conteúdo inválido já avisado (1 warning por conteúdo, não por ciclo)


def _load(path: str) -> Tuple[Any, str]:
    """(objeto, texto bruto); (None, "") se ausente ou JSON inválido (avisa 1× por conteúdo)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None, ""
    try:
        return json.loads(text), text
    except ValueError as exc:
        if _warned.get(path) != text:
            _warned[path] = text
            logger.warning(f"⚠️ {path}: JSON inválido ignorado ({exc})")
        return None, text


def _write_atomic(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _raw_control(runtime_dir: str) -> Dict[str, Any]:
    raw, _ = _load(os.path.join(runtime_dir, CONTROL_FILE))
    return raw if isinstance(raw, dict) else {}


def read_control(runtime_dir: str) -> Dict[str, Any]:
    """Lê e normaliza o control.json. Ausente/inválido → paused/new_cycles None e commands [].
    Comando sem id ou com cmd desconhecido fica de fora (não há como dar ack)."""
    raw = _raw_control(runtime_dir)
    out: Dict[str, Any] = {"paused": None, "new_cycles": None, "commands": []}
    for key in ("paused", "new_cycles"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for cmd in raw.get("commands") or []:
        if isinstance(cmd, dict) and cmd.get("id") and cmd.get("cmd") in COMMANDS:
            out["commands"].append({
                "id": str(cmd["id"]), "cmd": cmd["cmd"],
                "symbol": cmd.get("symbol"), "side": cmd.get("side"),
                "reason": str(cmd.get("reason") or "control.json"),
            })
    return out


def write_control(runtime_dir: str, **fields: Any) -> Dict[str, Any]:
    """Faz merge de campos no control.json (atômico) e devolve o conteúdo gravado. Lado do dashboard."""
    raw = {**_raw_control(runtime_dir), **fields}
    _write_atomic(os.path.join(runtime_dir, CONTROL_FILE), raw)
    return raw


def append_command(runtime_dir: str, cmd: str, symbol: Optional[str] = None, side: Optional[str] = None,
                   reason: str = "dashboard") -> str:
    """Acrescenta um comando (id novo) ao control.json preservando o resto; devolve o id. Lado do dashboard."""
    raw = _raw_control(runtime_dir)
    cmd_id = uuid.uuid4().hex
    raw["commands"] = list(raw.get("commands") or []) + [
        {"id": cmd_id, "cmd": cmd, "symbol": symbol, "side": side, "reason": reason}]
    _write_atomic(os.path.join(runtime_dir, CONTROL_FILE), raw)
    return cmd_id


def find_ack(runtime_dir: str, cmd_id: str) -> Optional[Dict[str, Any]]:
    """Ack de `cmd_id` em control_ack.json, ou None se o worker ainda não respondeu."""
    acks, _ = _load(os.path.join(runtime_dir, ACK_FILE))
    return next((a for a in acks if isinstance(a, dict) and str(a.get("id")) == cmd_id), None) \
        if isinstance(acks, list) else None


def consume_commands(runtime_dir: str, ids: Iterable[str], drop: Iterable[str] = ()) -> None:
    """Reescreve o control.json sem os comandos de `ids` e sem as chaves de `drop` (ex. "paused",
    já aplicada); o resto (new_cycles...) é preservado. Lado do worker."""
    ids, raw = set(ids), _raw_control(runtime_dir)
    raw["commands"] = [c for c in raw.get("commands") or []
                       if not (isinstance(c, dict) and str(c.get("id")) in ids)]
    for key in drop:
        raw.pop(key, None)
    _write_atomic(os.path.join(runtime_dir, CONTROL_FILE), raw)


def write_ack(runtime_dir: str, acks: List[Dict[str, Any]]) -> None:
    """Acrescenta acks ({"id", "ok", "error", "at"}) ao control_ack.json (lista, últimos ACK_KEEP)."""
    path = os.path.join(runtime_dir, ACK_FILE)
    prev, _ = _load(path)
    _write_atomic(path, ((prev if isinstance(prev, list) else []) + list(acks))[-ACK_KEEP:])

