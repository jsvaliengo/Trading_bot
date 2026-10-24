"""scripts/backfill_real_pnl.py: dry-run não escreve; --apply faz backup, cria a
coluna funding num DB antigo e grava; rodar de novo não muda nada."""
import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_real_pnl.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_real_pnl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _old_db(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, opened_at TEXT, symbol TEXT, side TEXT, "
        "qty REAL, entry_price REAL, exit_at TEXT, exit_price REAL, pnl_gross REAL, "
        "pnl_net REAL, fees REAL, status TEXT)"
    )
    con.execute(
        "INSERT INTO trades VALUES (1, '2026-10-20T10:00:01+00:00', 'SUIUSDT', 'SHORT', 25, 2.4, "
        "'2026-10-20T12:15:01+00:00', 2.39, 0.0474, 0.0235, 0.0239, 'closed')"
    )
    con.commit()
    con.close()


def _fixtures(tmp_path):
    fills = {"SUIUSDT": [
        {"symbol": "SUIUSDT", "id": 1, "side": "SELL", "positionSide": "SHORT", "qty": "25",
         "price": "2.40", "realizedPnl": "0", "commission": "0.024", "commissionAsset": "USDT",
         "time": 1792490399500},
        {"symbol": "SUIUSDT", "id": 2, "side": "BUY", "positionSide": "SHORT", "qty": "25",
         "price": "2.3981", "realizedPnl": "0.0474", "commission": "0.0239",
         "commissionAsset": "USDT", "time": 1792498500900},
    ]}
    income = [{"symbol": "SUIUSDT", "incomeType": "FUNDING_FEE", "income": "0.006",
               "time": 1792497600000, "tradeId": ""}]
    (tmp_path / "f.json").write_text(json.dumps(fills))
    (tmp_path / "i.json").write_text(json.dumps(income))
    return ["--fills-json", str(tmp_path / "f.json"), "--income-json", str(tmp_path / "i.json")]


def _row(db):
    return sqlite3.connect(db).execute("SELECT fees, funding, pnl_net FROM trades").fetchone()


def test_dry_run_apply_e_idempotencia(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _old_db(db)
    fx = _fixtures(tmp_path)
    mod = _load()

    assert mod.main(["--db", db, *fx]) == 0
    assert "mudanças: 1" in capsys.readouterr().out
    assert sqlite3.connect(db).execute("SELECT fees, pnl_net FROM trades").fetchone() == (0.0239, 0.0235)

    assert mod.main(["--db", db, "--apply", *fx]) == 0
    assert _row(db) == (0.0479, 0.006, 0.0055)
    assert len(list(tmp_path.glob("t.db.bak.backfill.*"))) == 1

    mod.main(["--db", db, "--apply", *fx])
    assert "mudanças: 0" in capsys.readouterr().out
    assert _row(db) == (0.0479, 0.006, 0.0055)
