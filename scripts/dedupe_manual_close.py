#!/usr/bin/env python3
"""Remove fechamentos duplicados pelo fechamento manual (corrida com o monitor, 30/10/26).

Antes do fix (PR "Fix(close): fechamento manual não duplica registro"), o botão
"fechar" do dashboard registrava o trade como "Manual (dashboard)" e o monitor,
vendo a posição sumir da exchange, inseria uma 2ª linha close-only "... (Binance)"
para o mesmo symbol/side/entry_price no mesmo minuto (BATUSDT ids 121 e 126).
Mantém a linha manual e apaga a "(Binance)".

  ./venv/bin/python3 scripts/dedupe_manual_close.py            # lista (padrão)
  ./venv/bin/python3 scripts/dedupe_manual_close.py --apply    # backup + DELETE
  ./venv/bin/python3 scripts/dedupe_manual_close.py --db copia.db
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WINDOW_SECONDS = 60

FIND_DUPES = """
SELECT b.id AS dup_id, m.id AS keep_id, b.symbol, b.side, b.entry_price, b.pnl_net,
       b.close_reason, m.exit_at AS manual_exit_at, b.exit_at AS dup_exit_at
  FROM trades b
  JOIN trades m
    ON m.symbol = b.symbol AND m.side = b.side AND m.entry_price = b.entry_price
   AND m.status = 'closed' AND m.close_reason = 'Manual (dashboard)'
   AND abs(strftime('%s', m.exit_at) - strftime('%s', b.exit_at)) <= ?
 WHERE b.status = 'closed' AND b.close_reason LIKE '%(Binance)%'
 ORDER BY b.id
"""


def find_duplicates(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(FIND_DUPES, (WINDOW_SECONDS,)).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="SQLite de trades (padrão: config.TRADE_DB_PATH)")
    ap.add_argument("--apply", action="store_true", help="backup + apaga as duplicatas numa transação")
    args = ap.parse_args(argv)

    if args.db:
        db = args.db
    else:
        from trading_bot.core.config import config
        db = config.TRADE_DB_PATH
    if not Path(db).exists():
        sys.exit(f"DB não encontrado: {db}")

    con = sqlite3.connect(db) if args.apply else sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    dupes = find_duplicates(con)
    print(f"DB: {db} | duplicatas: {len(dupes)}")
    for r in dupes:
        print(f"  apagar #{r['dup_id']:>4} {r['symbol']:10}{r['side']:6}entry={r['entry_price']} "
              f"pnl={float(r['pnl_net'] or 0):+.4f} '{r['close_reason']}' exit={r['dup_exit_at']}"
              f"  (mantém #{r['keep_id']} manual exit={r['manual_exit_at']})")
    if not dupes:
        return 0
    if not args.apply:
        print("\n[DRY-RUN] nada foi escrito. Rode com --apply para apagar.")
        return 0

    backup = f"{db}.bak.dedupe.{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    with sqlite3.connect(backup) as bk:
        con.backup(bk)
    print(f"backup: {backup}")
    with con:
        con.executemany("DELETE FROM trades WHERE id=?", [(r["dup_id"],) for r in dupes])
    print(f"apagadas: {len(dupes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
