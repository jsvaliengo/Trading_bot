"""
TradeStore — persistência durável de histórico de trades e de equity em SQLite.

Motivação: até aqui `trade_history` e `portfolio_history` viviam dentro do
arquivo de estado JSON, reescrito inteiro a cada save. Dois problemas:

  1. `trade_history` era capado em 500 e `portfolio_history` em 144 — ou seja,
     o histórico LONGO era PERDIDO. Diagnósticos (ex.: bruto vs fees vs net
     em 72 dias / milhares de closes) eram impossíveis a partir do estado.
  2. Os dois arrays incham o JSON que é reescrito a cada ~5-10min na VM Micro.

O TradeStore move esses dois conjuntos para um SQLite local (1 arquivo,
biblioteca stdlib, sem servidor, sem custo). O estado JSON deixa de carregar
os arrays — passa a guardar só estado quente. Os arrays em memória continuam
existindo (janela recente) para os leitores atuais (dashboard), reidratados
do SQLite no boot.

Regras:
- Escritor único (loop principal do bot). Mesmo assim usamos um Lock interno
  + check_same_thread=False para robustez caso algum caller venha de outra
  thread no futuro.
- Falha de escrita NUNCA pode derrubar o trading: os métodos de escrita logam
  e seguem (retornam None/False) em vez de propagar exceção.
- WAL ligado para leitura concorrente futura (dashboard read-only) sem travar
  a escrita do loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at     TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    signal        TEXT,
    side          TEXT    NOT NULL,
    qty           REAL,
    value         REAL,
    entry_price   REAL,
    stop_loss     REAL,
    take_profit   REAL,
    strategy_name TEXT    NOT NULL DEFAULT 'primary',
    strategy_type TEXT,
    double_first  INTEGER NOT NULL DEFAULT 0,
    ai_consultive TEXT,
    exit_at       TEXT,
    exit_price    REAL,
    pnl_gross     REAL,
    pnl_net       REAL,
    fees          REAL,
    close_reason  TEXT,
    status        TEXT    NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_trades_open_lookup ON trades (symbol, side, status);
CREATE INDEX IF NOT EXISTS idx_trades_exit_at      ON trades (exit_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol       ON trades (symbol);

CREATE TABLE IF NOT EXISTS portfolio_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_iso         TEXT NOT NULL,
    balance        REAL,
    pnl_realized   REAL,
    pnl_unrealized REAL,
    pnl_total      REAL,
    closed_trades  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_portfolio_ts ON portfolio_history (ts_iso);
"""


def _to_iso(value: Any) -> Optional[str]:
    """Normaliza timestamp (datetime|str|None) para ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_dt(value: Any) -> Any:
    """ISO string -> datetime (mantém o que não der parse)."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


class TradeStore:
    """Persistência SQLite de trades e snapshots de equity."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False: o Lock serializa; permite acesso de outra
        # thread sem o erro "SQLite objects created in a thread...".
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("🗃️ TradeStore inicializado em %s", db_path)

    # ------------------------------------------------------------------ write

    def record_open(self, record: Dict[str, Any]) -> Optional[int]:
        """Insere um trade aberto a partir do dict canônico da ledger.

        Retorna o id da linha (ou None em falha — nunca propaga exceção).
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    INSERT INTO trades (
                        opened_at, symbol, signal, side, qty, value, entry_price,
                        stop_loss, take_profit, strategy_name, strategy_type,
                        double_first, ai_consultive, status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')
                    """,
                    (
                        _to_iso(record.get("timestamp")),
                        record.get("symbol"),
                        record.get("signal"),
                        record.get("side"),
                        record.get("qty"),
                        record.get("value"),
                        record.get("entry_price"),
                        record.get("stop_loss"),
                        record.get("take_profit"),
                        str(record.get("strategy_name") or "primary"),
                        record.get("strategy_type"),
                        1 if record.get("double_first") else 0,
                        json.dumps(record.get("ai_consultive") or {}),
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("🗃️ Falha ao gravar trade aberto no TradeStore")
            return None

    def record_close(
        self,
        *,
        symbol: str,
        side: Optional[str],
        entry_price: Optional[float],
        exit_price: Optional[float],
        exit_at: Optional[str],
        pnl_gross: Optional[float],
        pnl_net: float,
        fees: float,
        close_reason: str,
        strategy_name: str,
    ) -> bool:
        """Fecha o trade aberto mais recente (symbol+side) — lookup indexado.

        Espelha a semântica do enrich em memória: pega o último `status='open'`
        com mesmo symbol (e side, quando informado). Se não achar, insere um
        registro close-only (caso: posição vinda de reconciliação sem open).
        """
        exit_at = exit_at or _to_iso(datetime.now())
        try:
            with self._lock:
                if side is not None:
                    row = self._conn.execute(
                        "SELECT id FROM trades WHERE symbol=? AND side=? AND status='open' "
                        "ORDER BY id DESC LIMIT 1",
                        (symbol, side),
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT id FROM trades WHERE symbol=? AND status='open' "
                        "ORDER BY id DESC LIMIT 1",
                        (symbol,),
                    ).fetchone()

                if row is not None:
                    self._conn.execute(
                        """
                        UPDATE trades
                           SET exit_at=?, exit_price=?, pnl_gross=?, pnl_net=?,
                               fees=?, close_reason=?, status='closed'
                         WHERE id=?
                        """,
                        (exit_at, exit_price, pnl_gross, pnl_net, fees,
                         close_reason, row["id"]),
                    )
                else:
                    # Close-only: não havia open correspondente.
                    self._conn.execute(
                        """
                        INSERT INTO trades (
                            opened_at, symbol, side, entry_price, strategy_name,
                            exit_at, exit_price, pnl_gross, pnl_net, fees,
                            close_reason, status
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'closed')
                        """,
                        (exit_at, symbol, side or "", entry_price,
                         str(strategy_name or "primary"), exit_at, exit_price,
                         pnl_gross, pnl_net, fees, close_reason),
                    )
                self._conn.commit()
                return True
        except Exception:
            logger.exception("🗃️ Falha ao gravar fechamento no TradeStore")
            return False

    def is_duplicate_close(
        self,
        *,
        symbol: str,
        side: Optional[str],
        entry_price: Optional[float],
    ) -> bool:
        """True quando este fechamento já foi registrado (re-processamento).

        Assinatura de duplicata: NÃO há linha `open` para (symbol[, side]) — ou
        seja, nada a fechar — MAS já existe uma linha `closed` com o mesmo
        entry_price. Distingue de reconciliação legítima (close-only sem open),
        que não casa o entry_price de nenhum closed anterior.

        Usado como guarda de idempotência: um restart na janela entre registrar
        o fechamento e remover de known_positions faz o monitor re-disparar o
        mesmo close. O store persistido é a fonte de verdade que sobrevive ao
        restart. Fail-open: em erro, retorna False (não perde close legítimo).
        """
        if entry_price is None:
            return False
        try:
            with self._lock:
                if side is not None:
                    has_open = self._conn.execute(
                        "SELECT 1 FROM trades WHERE symbol=? AND side=? AND status='open' LIMIT 1",
                        (symbol, side),
                    ).fetchone()
                else:
                    has_open = self._conn.execute(
                        "SELECT 1 FROM trades WHERE symbol=? AND status='open' LIMIT 1",
                        (symbol,),
                    ).fetchone()
                if has_open is not None:
                    return False  # há um open para fechar → fechamento legítimo
                if side is not None:
                    dup = self._conn.execute(
                        "SELECT 1 FROM trades WHERE symbol=? AND side=? AND status='closed' "
                        "AND entry_price=? LIMIT 1",
                        (symbol, side, entry_price),
                    ).fetchone()
                else:
                    dup = self._conn.execute(
                        "SELECT 1 FROM trades WHERE symbol=? AND status='closed' "
                        "AND entry_price=? LIMIT 1",
                        (symbol, entry_price),
                    ).fetchone()
                return dup is not None
        except Exception:
            logger.exception("🗃️ Falha ao checar duplicata de fechamento")
            return False

    def record_equity(self, snapshot: Dict[str, Any]) -> bool:
        """Persiste um snapshot de equity."""
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO portfolio_history (
                        ts_iso, balance, pnl_realized, pnl_unrealized,
                        pnl_total, closed_trades
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        _to_iso(snapshot.get("timestamp")),
                        snapshot.get("balance"),
                        snapshot.get("pnl_realized"),
                        snapshot.get("pnl_unrealized"),
                        snapshot.get("pnl_total"),
                        int(snapshot.get("closed_trades", 0) or 0),
                    ),
                )
                self._conn.commit()
                return True
        except Exception:
            logger.exception("🗃️ Falha ao gravar snapshot de equity no TradeStore")
            return False

    # ------------------------------------------------------------------- read

    def recent_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Últimos N trades em ordem cronológica (mais antigo -> mais novo).

        Retorna dicts no MESMO formato do `trade_history` em memória, para os
        leitores atuais (dashboard) não mudarem.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)
                ).fetchall()
        except Exception:
            logger.exception("🗃️ Falha ao ler trades recentes do TradeStore")
            return []

        out: List[Dict[str, Any]] = [self._row_to_trade(r) for r in rows]
        out.reverse()  # cronológico
        return out

    def recent_equity(self, limit: int = 144) -> List[Dict[str, Any]]:
        """Últimos N snapshots em ordem cronológica, formato do snapshot in-memory."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM portfolio_history ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        except Exception:
            logger.exception("🗃️ Falha ao ler equity recente do TradeStore")
            return []

        out = [
            {
                "timestamp": _parse_dt(r["ts_iso"]),
                "balance": r["balance"],
                "pnl_realized": r["pnl_realized"],
                "pnl_unrealized": r["pnl_unrealized"],
                "pnl_total": r["pnl_total"],
                "closed_trades": r["closed_trades"],
            }
            for r in rows
        ]
        out.reverse()
        return out

    def cumulative_realized_pnl(self) -> float:
        """Soma de pnl_net de TODOS os trades fechados (realizado acumulado).

        Fonte durável e cumulativa do progresso do bot — não zera na virada do
        dia (diferente do realizado diário da Binance). Usado no saldo/P&L total
        do dashboard.
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(pnl_net), 0) FROM trades WHERE status='closed'"
                ).fetchone()
            return float(row[0] or 0.0) if row else 0.0
        except Exception:
            logger.exception("🗃️ Falha ao somar pnl realizado acumulado")
            return 0.0

    def realized_pnl_today(self, day_utc: Optional[str] = None) -> float:
        """Soma de pnl_net dos trades fechados HOJE (UTC), do TradeStore durável.

        MESMA fonte da coluna "P&L DO DIA" do dashboard (daily_pnl_history) e do
        "P&L TOTAL" (cumulative_realized_pnl). O card "P&L HOJE" usa isto em vez
        do income diário da Binance — que dependia de um baseline re-ancorado a
        cada restart (zerava o contador no meio do dia UTC). Agrega por
        date(exit_at), igual ao histórico diário.

        day_utc: data 'YYYY-MM-DD' a consultar; default = hoje em UTC.
        """
        if day_utc is None:
            day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(pnl_net), 0) FROM trades "
                    "WHERE status='closed' AND exit_at IS NOT NULL "
                    "AND substr(exit_at, 1, 10) = ?",
                    (day_utc,),
                ).fetchone()
            return float(row[0] or 0.0) if row else 0.0
        except Exception:
            logger.exception("🗃️ Falha ao somar pnl realizado de hoje")
            return 0.0

    def first_trade_time_ms(self) -> Optional[int]:
        """Epoch ms do trade mais antigo (MIN opened_at), ou None se vazio.

        Âncora do "período atual" de tracking: usada pra alinhar funding/comissão
        acumulados ao MESMO intervalo do P&L realizado acumulado (que recomeça
        quando o DB é resetado). Sem isso, o income da Binance traria a vida
        toda da conta, destoando do saldo pós-reset.
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT MIN(opened_at) FROM trades"
                ).fetchone()
            if not row or not row[0]:
                return None
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(row[0]))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            logger.exception("🗃️ Falha ao obter timestamp do primeiro trade")
            return None

    def daily_pnl_history(self, limit: int = 90) -> List[Dict[str, Any]]:
        """P&L agregado por dia (UTC) dos trades fechados, cronológico.

        Agrega por date(exit_at): nº de trades, wins, net, fees, win_rate e o
        acumulado corrido (`cumulative`). Base da tabela de histórico diário do
        dashboard. O acumulado é computado sobre TODOS os dias e só então o
        resultado é cortado nos últimos `limit`, então fica correto mesmo com o
        corte.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT substr(exit_at, 1, 10)                       AS day,
                           COUNT(*)                                     AS trades,
                           SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) AS wins,
                           COALESCE(SUM(pnl_net), 0)                    AS net,
                           COALESCE(SUM(fees), 0)                       AS fees
                      FROM trades
                     WHERE status='closed' AND exit_at IS NOT NULL
                     GROUP BY day
                     ORDER BY day
                    """
                ).fetchall()
        except Exception:
            logger.exception("🗃️ Falha ao agregar P&L diário")
            return []

        out: List[Dict[str, Any]] = []
        cumulative = 0.0
        for r in rows:
            net = float(r["net"] or 0.0)
            cumulative += net
            trades = int(r["trades"] or 0)
            wins = int(r["wins"] or 0)
            out.append({
                "day": r["day"],
                "trades": trades,
                "wins": wins,
                "losses": trades - wins,
                "win_rate": round(wins / trades * 100.0, 1) if trades else 0.0,
                "net": round(net, 4),
                "fees": round(float(r["fees"] or 0.0), 4),
                "cumulative": round(cumulative, 4),
            })
        return out[-int(limit):] if limit else out

    def count_trades(self) -> int:
        try:
            with self._lock:
                return int(
                    self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                )
        except Exception:
            logger.exception("🗃️ Falha ao contar trades no TradeStore")
            return 0

    def count_equity(self) -> int:
        try:
            with self._lock:
                return int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM portfolio_history"
                    ).fetchone()[0]
                )
        except Exception:
            logger.exception("🗃️ Falha ao contar equity no TradeStore")
            return 0

    def reset(self) -> Dict[str, int]:
        """Apaga TODO o histórico durável: trades + portfolio_history.

        Usado pelo reset do bot (scripts/reset_bot_state.py). Sem isso o reset
        zerava só o JSON e o dashboard continuava mostrando o histórico antigo
        (o acumulado/histórico por dia vêm do TradeStore). Retorna as contagens
        removidas. Faz VACUUM para recuperar espaço.
        """
        removed = {"trades": 0, "equity": 0}
        try:
            with self._lock:
                removed["trades"] = int(
                    self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                )
                removed["equity"] = int(
                    self._conn.execute("SELECT COUNT(*) FROM portfolio_history").fetchone()[0]
                )
                self._conn.execute("DELETE FROM trades")
                self._conn.execute("DELETE FROM portfolio_history")
                try:
                    self._conn.execute(
                        "DELETE FROM sqlite_sequence WHERE name IN ('trades','portfolio_history')"
                    )
                except Exception:
                    pass  # sqlite_sequence só existe se houve AUTOINCREMENT
                self._conn.commit()
                self._conn.execute("VACUUM")  # fora da transação (após commit)
        except Exception:
            logger.exception("🗃️ Falha ao resetar TradeStore")
        return removed

    # -------------------------------------------------------------- migration

    def migrate_from_state(
        self,
        trade_history: Optional[List[Dict[str, Any]]],
        portfolio_history: Optional[List[Dict[str, Any]]],
    ) -> bool:
        """Importa os arrays do estado JSON legado para o SQLite (one-shot).

        Idempotente: só importa cada tabela se estiver vazia. Retorna True se
        importou algo.
        """
        imported = False
        try:
            if trade_history and self.count_trades() == 0:
                with self._lock:
                    for rec in trade_history:
                        if not isinstance(rec, dict):
                            continue
                        closed = rec.get("exit_price") is not None
                        self._conn.execute(
                            """
                            INSERT INTO trades (
                                opened_at, symbol, signal, side, qty, value,
                                entry_price, stop_loss, take_profit, strategy_name,
                                strategy_type, double_first, ai_consultive,
                                exit_at, exit_price, pnl_gross, pnl_net, fees,
                                close_reason, status
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                _to_iso(rec.get("timestamp")),
                                rec.get("symbol"),
                                rec.get("signal"),
                                rec.get("side"),
                                rec.get("qty"),
                                rec.get("value"),
                                rec.get("entry_price"),
                                rec.get("stop_loss"),
                                rec.get("take_profit"),
                                str(rec.get("strategy_name") or "primary"),
                                rec.get("strategy_type"),
                                1 if rec.get("double_first") else 0,
                                json.dumps(rec.get("ai_consultive") or {}),
                                _to_iso(rec.get("exit_time")),
                                rec.get("exit_price"),
                                rec.get("pnl_gross"),
                                rec.get("pnl_net"),
                                rec.get("fees"),
                                rec.get("close_reason"),
                                "closed" if closed else "open",
                            ),
                        )
                    self._conn.commit()
                imported = True
                logger.info(
                    "🗃️ Migrados %d trades do estado JSON para o TradeStore",
                    len(trade_history),
                )

            if portfolio_history and self.count_equity() == 0:
                with self._lock:
                    for snap in portfolio_history:
                        if not isinstance(snap, dict):
                            continue
                        self._conn.execute(
                            """
                            INSERT INTO portfolio_history (
                                ts_iso, balance, pnl_realized, pnl_unrealized,
                                pnl_total, closed_trades
                            ) VALUES (?,?,?,?,?,?)
                            """,
                            (
                                _to_iso(snap.get("timestamp")),
                                snap.get("balance"),
                                snap.get("pnl_realized"),
                                snap.get("pnl_unrealized"),
                                snap.get("pnl_total"),
                                int(snap.get("closed_trades", 0) or 0),
                            ),
                        )
                    self._conn.commit()
                imported = True
                logger.info(
                    "🗃️ Migrados %d snapshots de equity para o TradeStore",
                    len(portfolio_history),
                )
        except Exception:
            logger.exception("🗃️ Falha na migração do estado JSON para o TradeStore")

        return imported

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> Dict[str, Any]:
        """Mapeia uma linha de `trades` para o dict do trade_history in-memory."""
        try:
            ai = json.loads(r["ai_consultive"]) if r["ai_consultive"] else {}
        except (ValueError, TypeError):
            ai = {}
        rec: Dict[str, Any] = {
            "timestamp": r["opened_at"],
            "symbol": r["symbol"],
            "signal": r["signal"],
            "side": r["side"],
            "qty": r["qty"],
            "value": r["value"],
            "entry_price": r["entry_price"],
            "stop_loss": r["stop_loss"],
            "take_profit": r["take_profit"],
            "strategy_name": r["strategy_name"],
            "strategy_type": r["strategy_type"],
            "double_first": bool(r["double_first"]),
            "ai_consultive": ai,
        }
        if r["status"] == "closed" or r["exit_price"] is not None:
            rec.update(
                {
                    "exit_price": r["exit_price"],
                    "exit_time": r["exit_at"],
                    "pnl_gross": r["pnl_gross"],
                    "pnl_net": r["pnl_net"],
                    "fees": r["fees"],
                    "close_reason": r["close_reason"],
                }
            )
        return rec

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            logger.exception("🗃️ Falha ao fechar o TradeStore")
