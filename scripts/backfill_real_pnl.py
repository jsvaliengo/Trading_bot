#!/usr/bin/env python3
"""Backfill do P&L real da Binance nos trades históricos do DB (#181).

Os fechamentos server-side anteriores ao fix #181/#183 gravaram P&L fabricado
(fallback por preço atual). Este script casa cada trade FECHADO do DB com os
registros REALIZED_PNL + COMMISSION reais da Binance (por símbolo + janela de
horário em torno do exit_at) e corrige pnl_gross/fees/pnl_net/exit_price.

Uso (na VM, com .env carregado):
  ./venv/bin/python3 scripts/backfill_real_pnl.py            # DRY-RUN (só relatório)
  BACKFILL_APPLY=1 ./venv/bin/python3 scripts/backfill_real_pnl.py   # aplica (faz backup antes)

Seguro: faz backup do .db antes de qualquer escrita; trades sem match são
deixados intactos e listados.
"""
import os
import sqlite3
import shutil
import datetime as dt

DB = os.getenv("BACKFILL_DB", "runtime/trades.prod.mainnet.db")
APPLY = os.getenv("BACKFILL_APPLY", "") in ("1", "true", "yes")
WINDOW_BEFORE_S = 15 * 60   # income pode ser até 15min antes do exit_at detectado
WINDOW_AFTER_S = 5 * 60     # ou um pouco depois


def _to_ms(iso):
    d = dt.datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def main():
    from binance.client import Client
    cl = Client(os.getenv("BINANCE_MAINNET_API_KEY"), os.getenv("BINANCE_MAINNET_API_SECRET"))

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    trades = cur.execute(
        "SELECT id, symbol, side, opened_at, exit_at, qty, entry_price, pnl_net, pnl_gross, fees "
        "FROM trades WHERE status='closed' AND exit_at IS NOT NULL ORDER BY exit_at ASC"
    ).fetchall()
    if not trades:
        print("Nenhum trade fechado.")
        return

    start = _to_ms(cur.execute("SELECT MIN(opened_at) FROM trades").fetchone()[0])

    # Puxa TODO o income (REALIZED_PNL + COMMISSION) do período, paginando.
    income = []
    st = start
    while True:
        rows = cl.futures_income_history(startTime=st, limit=1000)
        if not rows:
            break
        income.extend(rows)
        st = int(rows[-1]["time"]) + 1
        if len(rows) < 1000:
            break
    realized = [r for r in income if r["incomeType"] == "REALIZED_PNL"]
    commission = [r for r in income if r["incomeType"] == "COMMISSION"]
    used_r, used_c = set(), set()

    def claim(pool, used, symbol, center_ms):
        """Soma e consome os registros do símbolo na janela em torno de center_ms."""
        total = 0.0
        hits = 0
        for i, r in enumerate(pool):
            if i in used or r.get("symbol") != symbol:
                continue
            t = int(r["time"])
            if center_ms - WINDOW_BEFORE_S * 1000 <= t <= center_ms + WINDOW_AFTER_S * 1000:
                total += float(r["income"])
                used.add(i)
                hits += 1
        return total, hits

    updates = []
    unmatched = []
    for tr in trades:
        center = _to_ms(tr["exit_at"])
        gross, gh = claim(realized, used_r, tr["symbol"], center)
        comm, ch = claim(commission, used_c, tr["symbol"], center)
        if gh == 0:
            unmatched.append(tr)
            continue
        fees = abs(comm)
        net = gross + comm  # comm é negativo
        qty = tr["qty"] or 0
        if qty and tr["side"]:
            delta = gross / qty
            exit_price = tr["entry_price"] + (delta if tr["side"] == "LONG" else -delta)
        else:
            exit_price = None
        updates.append((tr, round(gross, 6), round(fees, 6), round(net, 6), exit_price))

    print(f"DB: {DB}  | trades fechados: {len(trades)}")
    print(f"income real: {len(realized)} REALIZED_PNL, {len(commission)} COMMISSION")
    print(f"casados: {len(updates)}  | sem match: {len(unmatched)}")
    old_total = sum(float(t["pnl_net"] or 0) for t in trades)
    new_total = sum(u[3] for u in updates) + sum(float(t["pnl_net"] or 0) for t in unmatched)
    print(f"\nP&L net SOMA — antes(DB): {old_total:+.2f}  depois(real): {new_total:+.2f}")
    print("\n-- mudanças por trade (id symbol side: net DB -> real) --")
    for tr, gross, fees, net, xp in updates:
        flag = "  <<< invertido" if (float(tr["pnl_net"] or 0) > 0) != (net > 0) else ""
        print(f"  #{tr['id']:3} {tr['symbol']:9}{tr['side']:5} {float(tr['pnl_net'] or 0):+7.3f} -> {net:+7.3f}{flag}")
    if unmatched:
        print("\n-- SEM match (deixados intactos) --")
        for tr in unmatched:
            print(f"  #{tr['id']} {tr['symbol']} {tr['side']} exit_at={tr['exit_at']}")

    if not APPLY:
        print("\n[DRY-RUN] nada foi escrito. Rode com BACKFILL_APPLY=1 para aplicar.")
        return

    backup = f"{DB}.bak.backfill.{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(DB, backup)
    print(f"\nBackup: {backup}")
    for tr, gross, fees, net, xp in updates:
        cur.execute(
            "UPDATE trades SET pnl_gross=?, fees=?, pnl_net=?, exit_price=COALESCE(?, exit_price) WHERE id=?",
            (gross, fees, net, xp, tr["id"]),
        )
    con.commit()
    print(f"✅ {len(updates)} trades atualizados com o P&L real da Binance.")


if __name__ == "__main__":
    main()
