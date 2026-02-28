#!/usr/bin/env python3
"""
Script de teste para verificar o funding fee da Binance
Execute: python3 test_funding.py
"""

import os
from datetime import datetime, timezone, timedelta
from binance.client import Client

# Carrega as credenciais
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

if not API_KEY or not API_SECRET:
    print("❌ Variáveis de ambiente BINANCE_API_KEY e BINANCE_API_SECRET não encontradas!")
    print("   Execute: source ~/.bashrc")
    exit(1)

# Conecta na Binance (Mainnet)
print("🔗 Conectando na Binance Mainnet...")
client = Client(api_key=API_KEY, api_secret=API_SECRET)

# Testa conexão
try:
    account = client.futures_account()
    balance = float(account.get('totalWalletBalance', 0))
    print(f"✅ Conectado! Saldo: ${balance:.2f} USDT")
except Exception as e:
    print(f"❌ Erro ao conectar: {e}")
    exit(1)

print("\n" + "="*60)
print("📊 BUSCANDO INCOME HISTORY (últimas 24h)")
print("="*60)

# Busca income das últimas 24h
now = datetime.now(timezone.utc)
start_24h = now - timedelta(hours=24)
start_timestamp = int(start_24h.timestamp() * 1000)

try:
    income_list = client.futures_income_history(
        startTime=start_timestamp,
        limit=1000
    )
    
    print(f"\n📋 Total de registros encontrados: {len(income_list)}")
    
    if not income_list:
        print("\n⚠️  Nenhum registro de income encontrado nas últimas 24h!")
        print("   Isso pode significar:")
        print("   - Nenhum trade foi executado")
        print("   - Nenhuma posição estava aberta nos horários de funding")
    else:
        # Agrupa por tipo
        by_type = {}
        for item in income_list:
            income_type = item.get('incomeType', 'UNKNOWN')
            amount = float(item.get('income', 0))
            symbol = item.get('symbol', 'N/A')
            time_ms = item.get('time', 0)
            time_str = datetime.fromtimestamp(time_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            
            if income_type not in by_type:
                by_type[income_type] = {'total': 0, 'count': 0, 'items': []}
            by_type[income_type]['total'] += amount
            by_type[income_type]['count'] += 1
            by_type[income_type]['items'].append({
                'amount': amount,
                'symbol': symbol,
                'time': time_str
            })
        
        # Mostra resumo por tipo
        print("\n📈 RESUMO POR TIPO:")
        print("-"*60)
        for income_type, data in sorted(by_type.items()):
            print(f"\n   {income_type}:")
            print(f"      Quantidade: {data['count']} registros")
            print(f"      Total: ${data['total']:.4f}")
            
            # Mostra últimos 3 de cada tipo
            print("      Últimos registros:")
            for item in data['items'][-3:]:
                print(f"         • {item['time']} | {item['symbol']}: ${item['amount']:.4f}")
        
        # Mostra especificamente FUNDING_FEE
        print("\n" + "="*60)
        print("💸 DETALHES DO FUNDING FEE:")
        print("="*60)
        
        if 'FUNDING_FEE' in by_type:
            funding_data = by_type['FUNDING_FEE']
            print(f"\n✅ {funding_data['count']} registros de funding encontrados")
            print(f"   Total: ${funding_data['total']:.4f}")
            print("\n   Todos os registros de funding:")
            for item in funding_data['items']:
                emoji = "🟢" if item['amount'] > 0 else "🔴"
                print(f"      {emoji} {item['time']} | {item['symbol']}: ${item['amount']:.4f}")
        else:
            print("\n⚠️  NENHUM FUNDING_FEE encontrado nas últimas 24h!")
            print("   Possíveis razões:")
            print("   1. Você não tinha posições abertas nos horários de funding (00:00, 08:00, 16:00 UTC)")
            print("   2. As posições foram abertas DEPOIS do último funding")

except Exception as e:
    print(f"❌ Erro ao buscar income history: {e}")
    import traceback
    traceback.print_exc()

# Verifica posições atuais
print("\n" + "="*60)
print("📊 POSIÇÕES ATUAIS:")
print("="*60)

try:
    positions = client.futures_position_information()
    open_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
    
    if not open_positions:
        print("\n⚠️  Nenhuma posição aberta no momento")
    else:
        print(f"\n✅ {len(open_positions)} posições abertas:")
        for pos in open_positions:
            symbol = pos.get('symbol', 'N/A')
            amt = float(pos.get('positionAmt', 0))
            side = 'LONG' if amt > 0 else 'SHORT'
            entry = float(pos.get('entryPrice', 0))
            pnl = float(pos.get('unRealizedProfit', 0))
            print(f"   • {symbol} {side}: {abs(amt):.4f} @ ${entry:.4f} | P&L: ${pnl:.4f}")

except Exception as e:
    print(f"❌ Erro ao buscar posições: {e}")

# Próximo horário de funding
print("\n" + "="*60)
print("⏰ PRÓXIMO FUNDING:")
print("="*60)

try:
    # Pega info de um par para ver próximo funding
    mark_price = client.futures_mark_price(symbol='BTCUSDT')
    next_funding_time = mark_price.get('nextFundingTime', 0)
    current_funding_rate = float(mark_price.get('lastFundingRate', 0))
    
    if next_funding_time:
        next_funding_dt = datetime.fromtimestamp(next_funding_time/1000, tz=timezone.utc)
        # Converte para BRT (UTC-3)
        brt = timezone(timedelta(hours=-3))
        next_funding_brt = next_funding_dt.astimezone(brt)
        
        print(f"\n   Próximo funding: {next_funding_brt.strftime('%H:%M:%S')} BRT")
        print(f"   Taxa atual BTC: {current_funding_rate*100:.4f}%")
        
        # Quanto tempo falta
        time_until = next_funding_dt - now
        hours = int(time_until.total_seconds() // 3600)
        minutes = int((time_until.total_seconds() % 3600) // 60)
        print(f"   Tempo restante: {hours}h {minutes}min")

except Exception as e:
    print(f"❌ Erro: {e}")

print("\n" + "="*60)
print("✅ Teste finalizado!")
print("="*60)