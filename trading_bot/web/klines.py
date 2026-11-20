"""
/api/klines — velas 1m do par pro gráfico do dashboard, via a conexão da exchange
do bot (sem CORS nem chave no navegador). Módulo separado pra não mexer no app.py.
"""

from __future__ import annotations

import re

from flask import Flask, jsonify, request

from .auth import login_required

INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}
_SYMBOL = re.compile(r"^[A-Z0-9]{3,20}$")


def register(app: Flask) -> None:
    @app.route("/api/klines")
    @login_required
    def klines():
        symbol = str(request.args.get("symbol") or "").upper()
        interval = request.args.get("interval") or "1m"
        if not _SYMBOL.match(symbol) or interval not in INTERVALS:
            return jsonify({"ok": False, "error": "symbol/interval inválidos"}), 400
        limit = max(1, min(int(request.args.get("limit") or 300), 1000))
        exchange = getattr(app.config["BOT"], "exchange", None)
        raw = exchange.get_klines_raw(symbol, interval, limit) if exchange else []
        # get_klines_raw é Binance-like nas 3 exchanges: [openTime, o, h, l, c, v, closeTime, ...], ascendente, última aberta
        return jsonify({"symbol": symbol, "interval": interval,
                        "candles": [{"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                                     "c": float(k[4]), "v": float(k[5])} for k in raw]})
