#!/usr/bin/env python3
"""Recalcula gross/fees/funding/net dos trades FECHADOS a partir da Binance.

Usa a MESMA atribuição do reconciliador ao vivo (trading_bot/services/
pnl_reconciler.py): fills reais (/fapi/v1/userTrades) cortados em round trips,
fees = comissão da entrada + da saída, funding = FUNDING_FEE com a posição aberta,
net = gross − fees + funding. Conserta o histórico gravado pelo reconciliador
antigo (só a taxa da saída → net inflado ~$0,024/trade).

Ambiente (testnet/mainnet), chaves e DB vêm do config do bot (.env carregado):
  ./venv/bin/python3 scripts/backfill_real_pnl.py                  # dry-run (padrão)
  ./venv/bin/python3 scripts/backfill_real_pnl.py --apply          # backup + 1 transação

Offline (fixtures exportadas da Binance, sem rede):
  python scripts/backfill_real_pnl.py --db copia.db \
      --fills-json usertrades.json --income-json income.json

Seguro: backup via API de backup do SQLite antes de escrever; trades sem round
trip casável ficam intactos e são listados; perna com comissão desconhecida
mantém o fee do DB.
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.services.pnl_reconciler import (  # noqa: E402
    MATCH_SLACK_S, _to_ms, attribute, fetch_fills, fetch_funding, plan_update,
)


def _load_trades(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    funding = "funding" if "funding" in cols else "NULL AS funding"
    return [dict(r) for r in con.execute(
        "SELECT id, symbol, side, opened_at, exit_at, qty, entry_price, pnl_net, pnl_gross, fees, "
        f"{funding} FROM trades WHERE status='closed' AND exit_at IS NOT NULL ORDER BY exit_at ASC"
    )]


def _load_exchange_data(args, trades):
    start_ms = min(_to_ms(t["opened_at"]) for t in trades) - MATCH_SLACK_S * 1000
    end_ms = max(_to_ms(t["exit_at"]) for t in trades) + MATCH_SLACK_S * 1000
    if args.fills_json:
        raw = json.loads(Path(args.fills_json).read_text())
        fills = [f for v in raw.values() for f in v] if isinstance(raw, dict) else raw
        funding = json.loads(Path(args.income_json).read_text()) if args.income_json else None
        return fills, funding
    from trading_bot.infra.binance_client import BinanceConnection
    exchange = BinanceConnection()
    return (
        fetch_fills(exchange, (t["symbol"] for t in trades), start_ms, end_ms),
        fetch_funding(exchange, start_ms),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="SQLite de trades (padrão: config.TRADE_DB_PATH)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="só relatório (padrão)")
    mode.add_argument("--apply", action="store_true", help="backup + grava numa transação")
    ap.add_argument("--fills-json", help="userTrades offline (lista ou {symbol: [...]})")
    ap.add_argument("--income-json", help="income offline (usa só FUNDING_FEE)")
    args = ap.parse_args(argv)

    if args.db:
        db = args.db
    else:
        from trading_bot.core.config import config
        db = config.TRADE_DB_PATH
    if not Path(db).exists():
        sys.exit(f"DB não encontrado: {db}")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if not args.apply else sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    trades = _load_trades(con)
    if not trades:
        print("Nenhum trade fechado.")
        return 0

    fills, funding = _load_exchange_data(args, trades)
    if funding is None:
        print("⚠️ sem income: funding do DB é mantido")
    real = attribute(trades, fills, funding)

    plans, unmatched = [], []
    tot = {k: [0.0, 0.0] for k in ("gross", "fees", "funding", "net")}
    print(f"DB: {db} | fechados: {len(trades)} | fills: {len(fills)} | casados: {len(real)}")
    print(f"\n{'id':>5} {'symbol':10}{'side':6}{'gross':>9}{'fees antes→depois':>22}"
          f"{'funding':>10}{'net antes→depois':>22}")
    for tr in trades:
        r = real.get(tr["id"])
        if r is None:
            unmatched.append(tr)
        plan = plan_update(tr, r) if r else None
        new = plan or {"pnl_gross": tr["pnl_gross"], "fees": tr["fees"],
                       "funding": tr["funding"], "pnl_net": tr["pnl_net"]}
        for k, old_v, new_v in (("gross", tr["pnl_gross"], new["pnl_gross"]),
                                ("fees", tr["fees"], new["fees"]),
                                ("funding", tr["funding"], new["funding"]),
                                ("net", tr["pnl_net"], new["pnl_net"])):
            tot[k][0] += float(old_v or 0)
            tot[k][1] += float(new_v or 0)
        if plan:
            plans.append((tr, plan))
            flag = "  <<< gross" if plan["gross_fixed"] else ""
            print(f"#{tr['id']:>4} {tr['symbol']:10}{tr['side']:6}{plan['pnl_gross']:+9.4f}"
                  f"{float(tr['fees'] or 0):>11.4f}→{plan['fees']:<10.4f}"
                  f"{float(plan['funding'] or 0):+10.4f}"
                  f"{float(tr['pnl_net'] or 0):>+11.4f}→{plan['pnl_net']:<+10.4f}{flag}")

    print(f"\nTOTAIS ({len(trades)} trades)   antes      depois")
    for k, (a, b) in tot.items():
        print(f"  {k:8} {a:+10.4f} {b:+10.4f}")
    print(f"\nmudanças: {len(plans)} | gross divergente: {sum(p['gross_fixed'] for _, p in plans)}"
          f" | sem round trip (intactos): {len(unmatched)}")
    for tr in unmatched:
        print(f"  #{tr['id']} {tr['symbol']} {tr['side']} opened_at={tr['opened_at']}")

    if not args.apply:
        print("\n[DRY-RUN] nada foi escrito. Rode com --apply para gravar.")
        return 0

    backup = f"{db}.bak.backfill.{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    dst = sqlite3.connect(backup)
    con.backup(dst)
    dst.close()
    print(f"\nBackup: {backup}")
    with con:  # uma transação: tudo ou nada
        if "funding" not in {r[1] for r in con.execute("PRAGMA table_info(trades)")}:
            con.execute("ALTER TABLE trades ADD COLUMN funding REAL")
        for tr, p in plans:
            con.execute(
                "UPDATE trades SET pnl_gross=?, fees=?, pnl_net=?, funding=COALESCE(?, funding), "
                "exit_price=COALESCE(?, exit_price) WHERE id=?",
                (p["pnl_gross"], p["fees"], p["pnl_net"], p["funding"], p["exit_price"], tr["id"]),
            )
    print(f"✅ {len(plans)} trades atualizados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
