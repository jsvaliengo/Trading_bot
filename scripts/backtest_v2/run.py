#!/usr/bin/env python3
"""CLI do backtest_v2.

    PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run download [--tfs 1d,4h,1h]
    PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run backtest [--only NOME]
"""
from __future__ import annotations

import argparse
import logging

from . import data

# Barras de aquecimento por TF (≥ maior janela que qualquer estratégia pede:
# 260 exec/confirm, 255 HTF, EMA200 diária com folga).
WARMUP = {"3m": 300, "5m": 300, "15m": 300, "1h": 300, "4h": 300, "1d": 400}


def cmd_download(args):
    syms = args.symbols.split(",") if args.symbols else data.load_universe()
    tfs = {tf: WARMUP[tf] for tf in args.tfs.split(",")}
    start, end = data.to_ms(args.start), data.to_ms(args.end)
    data.download_all(syms, tfs, start, end, with_funding=not args.no_funding)
    print(f"cache: {data.cache_size_bytes() / 1e6:.0f} MB em {data.CACHE_DIR}")


def cmd_coverage(args):
    import json

    syms = args.symbols.split(",") if args.symbols else data.load_universe()
    start, end = data.to_ms(args.start), data.to_ms(args.end)
    out = {tf: data.coverage(syms, tf, start, end, WARMUP[tf]) for tf in args.tfs.split(",")}
    p = data.OUT_DIR / "coverage.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2, sort_keys=True))
    for tf, cov in out.items():
        late = {s: c["first"] for s, c in cov.items() if not c["bars"] or c["first"] > args.start}
        print(f"{tf}: {sum(1 for c in cov.values() if c['bars'])}/{len(cov)} com dados; "
              f"gaps={sum(c.get('gaps', 0) for c in cov.values())}; começam depois do início: {late}")
    print(f"cache: {data.cache_size_bytes() / 1e6:.0f} MB")


def main(argv=None):
    p = argparse.ArgumentParser(prog="backtest_v2")
    p.add_argument("--start", default=data.DEFAULT_START)
    p.add_argument("--end", default=data.DEFAULT_END)
    p.add_argument("--symbols", default="")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--tfs", default="1d,4h,1h,15m,5m,3m")
    d.add_argument("--no-funding", action="store_true")
    d.set_defaults(fn=cmd_download)
    c = sub.add_parser("coverage")
    c.add_argument("--tfs", default="1d,4h,1h,15m,5m,3m")
    c.set_defaults(fn=cmd_coverage)
    b = sub.add_parser("backtest")
    b.add_argument("--only", default="", help="nomes de estratégia separados por vírgula")
    b.add_argument("--start-3m", default="", help="início alternativo p/ variantes 3m/5m")
    b.add_argument("--workers", type=int, default=0)
    b.set_defaults(fn=lambda a: __import__("scripts.backtest_v2.pipeline", fromlist=["x"]).cmd_backtest(a))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()
