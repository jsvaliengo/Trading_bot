"""
SMOKE TEST DA OKX
=================
Com OKX_API_KEY/OKX_API_SECRET/OKX_PASSPHRASE (demo por padrão, OKX_DEMO=true) no .env:
lê saldo, instrumento, preço, posições e o modo de posição. NÃO envia ordem sem --order.

  .venv/bin/python scripts/okx_smoke.py            # só leitura
  .venv/bin/python scripts/okx_smoke.py --order    # abre LONG no mínimo do 1º par e fecha em seguida
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.core.config import config  # noqa: E402
from trading_bot.infra.okx_client import OkxConnection  # noqa: E402


def main(send_order: bool) -> int:
    pairs = list(config.MARTINGALE_PAIRS or ["ADAUSDT"])
    print(f"OKX {'DEMO' if config.OKX_DEMO else 'PRODUÇÃO'} | pares: {pairs}")
    ex = OkxConnection()
    print(f"Saldo: wallet ${ex.get_account_balance():.2f} | disponível ${ex.get_available_balance():.2f}")
    cfg = ex._request("GET", "/api/v5/account/config")
    print(f"posMode: {cfg[0].get('posMode') if cfg else '?'} (martingale precisa de long_short_mode)")
    for sym in pairs:
        info = ex.get_symbol_info(sym)
        print(f"{sym}: preço {ex.get_symbol_price(sym)} | ctVal {info.get('ctVal')} lotSz {info.get('lotSz')} "
              f"minSz {info.get('minSz')} → minQty {info.get('minQty')} moedas, minNotional ${info.get('minNotional', 0):.2f}")
    for p in ex.get_open_positions():
        print(f"  posição {p['side']} {p['symbol']}: {p['quantity']} @ {p['entry_price']} (upl {p['unrealized_pnl']})")
    if not send_order:
        print("Sem --order: nenhuma ordem enviada.")
        return 0
    sym = pairs[0]
    info = ex.get_symbol_info(sym)
    if not ex.set_hedge_mode() or not ex.set_leverage(sym, config.MARTINGALE_LEVERAGE):
        print("Hedge mode/alavancagem falhou — abortando")
        return 1
    order = ex.place_market_order(symbol=sym, side="BUY", position_side="LONG", quantity=info["minQty"])
    print(f"abertura: {order}")
    if not order:
        return 1
    print(f"fechamento: {ex.close_position(sym, 'LONG')}")
    print(f"realizado: {ex.pop_realized_close(sym, 'LONG')}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--order" in sys.argv[1:]))
