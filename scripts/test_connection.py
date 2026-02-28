"""
SCRIPT DE TESTE DE CONEXÃO
==========================
Use este script para testar se sua conexão com a Binance
está funcionando antes de rodar o bot completo.

Não executa nenhum trade - apenas testa a API.
"""

import sys
from pathlib import Path

# Garante import local do projeto quando executado via scripts/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.core.config import config
from trading_bot.infra.binance_client import BinanceConnection


def test_connection():
    """
    Testa a conexão e mostra informações úteis.
    """
    print("=" * 60)
    print("🧪 TESTE DE CONEXÃO COM A BINANCE")
    print("=" * 60)
    print()
    
    # Mostra configuração
    print(f"📋 Configuração:")
    print(f"   • Testnet: {'Sim' if config.USE_TESTNET else 'Não (MAINNET)'}")
    print(f"   • Capital configurado: ${config.TOTAL_CAPITAL}")
    print(f"   • Alavancagem: {config.LEVERAGE}x")
    print()
    
    try:
        # Tenta conectar
        print("🔌 Conectando à Binance...")
        exchange = BinanceConnection()
        print("✅ Conexão estabelecida!")
        print()
        
        # Mostra saldo
        balance = exchange.get_account_balance()
        print(f"💰 Saldo disponível: ${balance:.2f} USDT")
        print()
        
        # Mostra preços
        print("📊 Preços atuais dos pares configurados:")
        for symbol in config.TRADING_PAIRS:
            price = exchange.get_symbol_price(symbol)
            info = exchange.get_symbol_info(symbol)
            min_notional = info.get('minNotional', 'N/A')
            print(f"   • {symbol}: ${price:.2f} (mín: ${min_notional})")
        print()
        
        # Verifica posições abertas
        positions = exchange.get_open_positions()
        print(f"📈 Posições abertas: {len(positions)}")
        for pos in positions:
            print(f"   • {pos['side']} {pos['symbol']}: {pos['quantity']} @ ${pos['entry_price']:.2f}")
            print(f"     P&L: ${pos['unrealized_pnl']:.2f}")
        print()
        
        # Testa hedge mode
        print("⚙️  Verificando Hedge Mode...")
        if exchange.set_hedge_mode():
            print("✅ Hedge Mode está ativo!")
        else:
            print("❌ Não foi possível ativar Hedge Mode")
        print()
        
        print("=" * 60)
        print("✅ TODOS OS TESTES PASSARAM!")
        print("   Você pode executar o bot com: python -m trading_bot.core.bot")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"❌ ERRO: {e}")
        print()
        print("Possíveis causas:")
        print("   1. API Key ou Secret incorretos")
        print("   2. API Key sem permissão para Futures")
        print("   3. Problema de conexão com a internet")
        print("   4. API Key da Mainnet usada com Testnet (ou vice-versa)")
        return False


if __name__ == "__main__":
    test_connection()
