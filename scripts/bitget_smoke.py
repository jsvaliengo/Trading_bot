"""
SMOKE TEST DA BITGET
====================
Com BITGET_API_KEY/BITGET_API_SECRET/BITGET_PASSPHRASE (demo por padrão, BITGET_DEMO=true) no .env:
lê saldo, contrato, preço e posições; ativa hedge mode. NÃO envia ordem sem --order.
Demo só tem BTC/ETH/XRP: com ADA/TRX nos pares, use TRADING_BOT_MARTINGALE_PAIRS=BTCUSDT.

  .venv/bin/python scripts/bitget_smoke.py            # só leitura
  .venv/bin/python scripts/bitget_smoke.py --order    # abre LONG no mínimo do 1º par e fecha em seguida
"""

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.core.config import config  # noqa: E402
from trading_bot.infra.bitget_client import BitgetConnection  # noqa: E402


def main(send_order: bool) -> int:
    pairs = list(config.MARTINGALE_PAIRS or ["ADAUSDT"])
    print(f"Bitget {'DEMO' if config.BITGET_DEMO else 'PRODUÇÃO'} | pares: {pairs}")
    ex = BitgetConnection()
    print(f"Saldo: wallet ${ex.get_account_balance():.2f} | disponível ${ex.get_available_balance():.2f}")
    print(f"hedge mode: {'OK' if ex.set_hedge_mode() else 'FALHOU (posição aberta?)'}")
    for sym in pairs:
        info = ex.get_symbol_info(sym)
        if not info:
            print(f"{sym}: contrato não existe em {ex.product_type}")
            continue
        print(f"{sym}: preço {ex.get_symbol_price(sym)} | step {info['stepSize']} "
              f"minQty {info['minQty']} minNotional ${info['minNotional']:.2f} maxLev {info['maxLeverage']}x")
    for p in ex.get_open_positions():
        print(f"  posição {p['side']} {p['symbol']}: {p['quantity']} @ {p['entry_price']} (upl {p['unrealized_pnl']})")
    if not send_order:
        print("Sem --order: nenhuma ordem enviada.")
        return 0
    sym = pairs[0]
    info = ex.get_symbol_info(sym)
    if not info or not ex.set_leverage(sym, config.MARTINGALE_LEVERAGE):
        print("Contrato/alavancagem falhou — abortando")
        return 1
    # ceil no step: floor de 5/0.1773=28.2 ADA dá 28 → $4.96 < $5 (45110); o engine faz o mesmo
    step = info["stepSize"]
    qty = max(info["minQty"], math.ceil(info["minNotional"] / ex.get_symbol_price(sym) / step) * step)
    order = ex.place_market_order(symbol=sym, side="BUY", position_side="LONG", quantity=qty)
    print(f"abertura: {order}")
    if not order:
        return 1
    print(f"fechamento: {ex.close_position(sym, 'LONG')}")
    print(f"realizado: {ex.pop_realized_close(sym, 'LONG')}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--order" in sys.argv[1:]))
