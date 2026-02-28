"""
TESTE DE CONFIGURAÇÕES DO BOT
=============================
Simula o comportamento do bot com as novas configurações
sem conectar à Binance (usa dados mockados).
"""

import sys
from pathlib import Path

# Garante import local do projeto, independente da máquina/caminho absoluto
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dataclasses import dataclass
from typing import List, Dict
import random

# Importa configurações reais
from trading_bot.core.config import config

# Simula capital disponível
CAPITAL_SIMULADO = 112.0

print("=" * 60)
print("🧪 TESTE DE CONFIGURAÇÕES DO BOT")
print("=" * 60)

# ============================================
# 1. MOSTRA CONFIGURAÇÕES ATUAIS
# ============================================
print("\n📋 CONFIGURAÇÕES ATUAIS:")
print("-" * 40)
print(f"• Pares fixos: {config.FIXED_PAIRS}")
print(f"• Total de pares: {config.MAX_TRADING_PAIRS}")
print(f"• Seleção automática: {config.AUTO_SELECT_PAIRS}")
print(f"• Intervalo atualização: {config.PAIR_UPDATE_INTERVAL_MINUTES} min")
print(f"• Usar mínimo apenas: {config.USE_MIN_NOTIONAL_ONLY}")
print(f"• MAX_POSITION_PERCENT: {config.MAX_POSITION_PERCENT * 100}%")
print(f"• Alavancagem: {config.LEVERAGE}x")
print(f"• Max posições abertas: {config.MAX_OPEN_POSITIONS}")

print("\n📊 PESOS DE SELEÇÃO:")
for criterio, peso in config.PAIR_SELECTION_WEIGHTS.items():
    print(f"   • {criterio}: {peso}%")

print("\n📊 FILTROS MÍNIMOS:")
print(f"   • Volume 24h mínimo: ${config.MIN_VOLUME_24H_USD/1e6:.0f}M")
print(f"   • Spread máximo: {config.MAX_SPREAD_PERCENT}%")
print(f"   • Volatilidade mínima: {config.MIN_VOLATILITY_PERCENT}%")
print(f"   • Mínimo notional máximo: ${config.MAX_MIN_NOTIONAL}")

print("\n📊 TRAILING STOP:")
print(f"   • Ativação: {config.TRAILING_ACTIVATION_PERCENT}%")
print(f"   • Distância: {config.TRAILING_DISTANCE_PERCENT}%")
print(f"   • Mínimo normal: ${config.TRAILING_MIN_PROFIT_USD}")
print(f"   • Mínimo (funding alto): ${config.TRAILING_MIN_PROFIT_HIGH_FUNDING}")

print("\n📊 FUNDING RATE:")
print(f"   • Verificar funding: {config.CHECK_FUNDING_RATE}")
print(f"   • Threshold: {config.FUNDING_RATE_THRESHOLD}%")

# ============================================
# 2. SIMULA DADOS DE PARES
# ============================================
print("\n" + "=" * 60)
print("🔍 SIMULAÇÃO DE SELEÇÃO DE PARES")
print("=" * 60)

# Dados mockados de pares (simulando API da Binance)
MOCK_PAIRS_DATA = {
    "BTCUSDT": {"volume": 800_000_000, "volatility": 2.5, "trend": 1.2, "funding": 0.01, "spread": 0.01, "min_notional": 100},
    "ETHUSDT": {"volume": 600_000_000, "volatility": 3.0, "trend": 1.5, "funding": 0.02, "spread": 0.02, "min_notional": 20},
    "BNBUSDT": {"volume": 150_000_000, "volatility": 2.8, "trend": 0.8, "funding": 0.015, "spread": 0.02, "min_notional": 5},
    "SOLUSDT": {"volume": 200_000_000, "volatility": 4.5, "trend": 2.1, "funding": 0.025, "spread": 0.03, "min_notional": 5},
    "XRPUSDT": {"volume": 180_000_000, "volatility": 3.2, "trend": 1.0, "funding": 0.01, "spread": 0.02, "min_notional": 5},
    "DOGEUSDT": {"volume": 120_000_000, "volatility": 5.0, "trend": 1.8, "funding": 0.03, "spread": 0.04, "min_notional": 5},
    "AVAXUSDT": {"volume": 100_000_000, "volatility": 4.0, "trend": 2.5, "funding": 0.02, "spread": 0.03, "min_notional": 5},
    "LINKUSDT": {"volume": 90_000_000, "volatility": 3.5, "trend": 1.5, "funding": 0.015, "spread": 0.03, "min_notional": 5},
    "ADAUSDT": {"volume": 80_000_000, "volatility": 2.5, "trend": 0.5, "funding": 0.01, "spread": 0.03, "min_notional": 5},
    "MATICUSDT": {"volume": 70_000_000, "volatility": 3.8, "trend": 1.2, "funding": 0.02, "spread": 0.04, "min_notional": 5},
    "DOTUSDT": {"volume": 60_000_000, "volatility": 3.0, "trend": 0.8, "funding": 0.015, "spread": 0.04, "min_notional": 5},
    "LTCUSDT": {"volume": 55_000_000, "volatility": 2.2, "trend": 0.6, "funding": 0.01, "spread": 0.03, "min_notional": 5},
    "UNIUSDT": {"volume": 50_000_000, "volatility": 4.2, "trend": 1.8, "funding": 0.025, "spread": 0.05, "min_notional": 5},
    # Pares que devem ser filtrados (baixo volume ou alta spread)
    "SHIBUSDT": {"volume": 30_000_000, "volatility": 6.0, "trend": 2.0, "funding": 0.05, "spread": 0.08, "min_notional": 5},
    "PEPEUSDT": {"volume": 25_000_000, "volatility": 8.0, "trend": 3.0, "funding": 0.08, "spread": 0.12, "min_notional": 5},
}

def calculate_score(data: dict) -> float:
    """Calcula score usando os pesos configurados"""
    weights = config.PAIR_SELECTION_WEIGHTS
    
    # Volatilidade (2-5% ideal)
    vol = data['volatility']
    if vol < 1:
        vol_score = vol * 50
    elif vol <= 5:
        vol_score = 100
    else:
        vol_score = max(0, 100 - (vol - 5) * 10)
    
    # Volume (normalizado)
    volume = data['volume']
    if volume >= 500_000_000:
        volume_score = 100
    elif volume >= config.MIN_VOLUME_24H_USD:
        volume_score = (volume - config.MIN_VOLUME_24H_USD) / (500_000_000 - config.MIN_VOLUME_24H_USD) * 100
    else:
        volume_score = 0
    
    # Tendência (1-3% ideal)
    trend = data['trend']
    if trend < 0.5:
        trend_score = trend * 100
    elif trend <= 3:
        trend_score = 100
    else:
        trend_score = max(0, 100 - (trend - 3) * 15)
    
    # Funding (mais baixo = melhor)
    funding = abs(data['funding'])
    if funding <= 0.01:
        funding_score = 100
    elif funding <= 0.03:
        funding_score = 70
    elif funding <= 0.05:
        funding_score = 40
    else:
        funding_score = 10
    
    # Spread (menor = melhor)
    spread = data['spread']
    if spread <= 0.02:
        spread_score = 100
    elif spread <= 0.05:
        spread_score = 70
    elif spread <= config.MAX_SPREAD_PERCENT:
        spread_score = 40
    else:
        spread_score = 0
    
    # Score final
    total_weight = sum(weights.values())
    final_score = (
        vol_score * weights['volatility'] +
        volume_score * weights['volume'] +
        trend_score * weights['trend'] +
        funding_score * weights['funding'] +
        spread_score * weights['spread']
    ) / total_weight
    
    return final_score

# Filtra e pontua pares
print("\n📊 ANÁLISE DE TODOS OS PARES:")
print("-" * 40)

# Filtra e pontua pares
print("\n📊 ANÁLISE DE TODOS OS PARES:")
print("-" * 40)

SAFETY_MARGIN = 1.25
FEE_MARGIN = 1.10

def calculate_capital_needed(min_notional):
    """Quanto capital precisa para operar um par em hedge"""
    return min_notional * SAFETY_MARGIN * 2 * FEE_MARGIN

pair_scores = []
for symbol, data in MOCK_PAIRS_DATA.items():
    # Aplica filtros
    if data['volume'] < config.MIN_VOLUME_24H_USD:
        print(f"❌ {symbol}: Volume baixo ${data['volume']/1e6:.0f}M < ${config.MIN_VOLUME_24H_USD/1e6:.0f}M")
        continue
    if data['spread'] > config.MAX_SPREAD_PERCENT:
        print(f"❌ {symbol}: Spread alto {data['spread']}% > {config.MAX_SPREAD_PERCENT}%")
        continue
    if data['volatility'] < config.MIN_VOLATILITY_PERCENT:
        print(f"❌ {symbol}: Volatilidade baixa {data['volatility']}%")
        continue
    if data['min_notional'] > config.MAX_MIN_NOTIONAL:
        print(f"⏭️ {symbol}: Mínimo ${data['min_notional']} > limite ${config.MAX_MIN_NOTIONAL} (PULADO)")
        continue
    
    score = calculate_score(data)
    capital_needed = calculate_capital_needed(data['min_notional'])
    pair_scores.append((symbol, score, data, capital_needed))
    print(f"✅ {symbol}: Score {score:.1f} | Vol {data['volatility']:.1f}% | Precisa ${capital_needed:.2f}")

# Ordena por score
pair_scores.sort(key=lambda x: x[1], reverse=True)

# Seleciona os melhores CONSIDERANDO O CAPITAL
print("\n" + "=" * 60)
print("🏆 PARES SELECIONADOS (considerando capital)")
print("=" * 60)

num_dynamic = config.MAX_TRADING_PAIRS - len(config.FIXED_PAIRS)
selected = list(config.FIXED_PAIRS)
capital_used = 0.0

print(f"\n💵 Capital disponível: ${CAPITAL_SIMULADO:.2f}")
print(f"📊 Máximo de pares: {config.MAX_TRADING_PAIRS}")

# Adiciona fixos primeiro
if config.FIXED_PAIRS:
    print("\n📌 PARES FIXOS:")
    for symbol in config.FIXED_PAIRS:
        data = MOCK_PAIRS_DATA.get(symbol, {})
        print(f"   • {symbol} (mínimo ${data.get('min_notional', '?')})")

# Adiciona dinâmicos até acabar o capital
print(f"\n🔄 PARES DINÂMICOS (selecionando até {num_dynamic}):")
count = 0
for symbol, score, data, capital_needed in pair_scores:
    if symbol in config.FIXED_PAIRS:
        continue
    if count >= num_dynamic:
        print(f"   ⏹️ Limite de {num_dynamic} pares atingido")
        break
    
    # Verifica se cabe no capital
    if capital_used + capital_needed > CAPITAL_SIMULADO:
        print(f"   ⏭️ {symbol}: Capital insuficiente (precisa ${capital_needed:.2f}, restam ${CAPITAL_SIMULADO - capital_used:.2f})")
        continue
    
    # Adiciona o par
    selected.append(symbol)
    capital_used += capital_needed
    count += 1
    print(f"   ✅ {symbol}: Score {score:.1f} | Precisa ${capital_needed:.2f} | Usado ${capital_used:.2f}")

print(f"\n✅ LISTA FINAL: {selected}")
print(f"💰 Capital usado: ${capital_used:.2f} / ${CAPITAL_SIMULADO:.2f}")

# ============================================
# 3. SIMULA TAMANHO DOS TRADES
# ============================================
print("\n" + "=" * 60)
print("💰 SIMULAÇÃO DE TAMANHO DOS TRADES")
print("=" * 60)

print(f"\n💵 Capital: ${CAPITAL_SIMULADO:.2f}")
print(f"📊 USE_MIN_NOTIONAL_ONLY: {config.USE_MIN_NOTIONAL_ONLY}")

print("\n📊 TAMANHO POR PAR:")
print("-" * 50)

total_long = 0
total_short = 0

for symbol in selected:
    data = MOCK_PAIRS_DATA.get(symbol, {"min_notional": 5})
    min_notional = data['min_notional']
    min_per_position = min_notional * SAFETY_MARGIN
    
    if config.USE_MIN_NOTIONAL_ONLY:
        # Sempre usa o mínimo
        long_size = min_per_position
        short_size = min_per_position
    else:
        # Usa o maior entre percentual e mínimo
        percent_value = CAPITAL_SIMULADO * config.MAX_POSITION_PERCENT
        long_size = max(percent_value * 0.5, min_per_position)
        short_size = max(percent_value * 0.5, min_per_position)
    
    total = long_size + short_size
    total_long += long_size
    total_short += short_size
    
    print(f"   {symbol}:")
    print(f"      Mínimo Binance: ${min_notional:.2f}")
    print(f"      LONG: ${long_size:.2f} | SHORT: ${short_size:.2f} | Total: ${total:.2f}")

print("-" * 50)
print(f"\n📊 TOTAIS:")
print(f"   • Total LONG: ${total_long:.2f}")
print(f"   • Total SHORT: ${total_short:.2f}")
print(f"   • Total GERAL: ${total_long + total_short:.2f}")
print(f"   • Capital necessário (+10% fees): ${(total_long + total_short) * 1.1:.2f}")

if CAPITAL_SIMULADO >= (total_long + total_short) * 1.1:
    print(f"\n✅ Capital SUFICIENTE para todos os pares!")
else:
    print(f"\n⚠️ Capital INSUFICIENTE! Alguns pares serão pulados.")

# ============================================
# 4. SIMULA CENÁRIOS DE TRAILING STOP
# ============================================
print("\n" + "=" * 60)
print("🎯 SIMULAÇÃO DE TRAILING STOP")
print("=" * 60)

def simulate_trailing(position_size: float, entry_price: float, peak_price: float, 
                      current_price: float, side: str, funding_rate: float):
    """Simula a lógica do trailing stop"""
    
    # Calcula lucro
    if side == "LONG":
        profit_pct = ((current_price - entry_price) / entry_price) * 100
        profit_usd = (current_price - entry_price) * position_size / entry_price
        trailing_stop = peak_price * (1 - config.TRAILING_DISTANCE_PERCENT / 100)
        triggered = current_price <= trailing_stop
    else:
        profit_pct = ((entry_price - current_price) / entry_price) * 100
        profit_usd = (entry_price - current_price) * position_size / entry_price
        trailing_stop = peak_price * (1 + config.TRAILING_DISTANCE_PERCENT / 100)
        triggered = current_price >= trailing_stop
    
    # Verifica ativação
    activated = profit_pct >= config.TRAILING_ACTIVATION_PERCENT
    
    # Determina mínimo baseado no funding
    min_profit = config.TRAILING_MIN_PROFIT_USD
    funding_against = False
    
    if side == "LONG" and funding_rate > config.FUNDING_RATE_THRESHOLD:
        funding_against = True
        min_profit = config.TRAILING_MIN_PROFIT_HIGH_FUNDING
    elif side == "SHORT" and funding_rate < -config.FUNDING_RATE_THRESHOLD:
        funding_against = True
        min_profit = config.TRAILING_MIN_PROFIT_HIGH_FUNDING
    
    # Decisão final
    should_close = activated and triggered and profit_usd >= min_profit
    
    return {
        'activated': activated,
        'triggered': triggered,
        'profit_pct': profit_pct,
        'profit_usd': profit_usd,
        'trailing_stop': trailing_stop,
        'min_profit': min_profit,
        'funding_against': funding_against,
        'should_close': should_close
    }

# Cenário 1: Trade normal
print("\n📊 CENÁRIO 1: Trade normal (BNB, funding baixo)")
print("-" * 40)
result = simulate_trailing(
    position_size=6.25,  # Mínimo BNB
    entry_price=600.0,
    peak_price=604.0,    # Subiu 0.67%
    current_price=602.5, # Voltou um pouco
    side="LONG",
    funding_rate=0.01    # Funding baixo
)
print(f"   Entrada: $600 | Pico: $604 | Atual: $602.50")
print(f"   Lucro: {result['profit_pct']:.2f}% = ${result['profit_usd']:.4f}")
print(f"   Trailing ativado: {'✅' if result['activated'] else '❌'} (>= {config.TRAILING_ACTIVATION_PERCENT}%)")
print(f"   Trailing stop em: ${result['trailing_stop']:.2f}")
print(f"   Stop atingido: {'✅' if result['triggered'] else '❌'}")
print(f"   Mínimo exigido: ${result['min_profit']:.2f}")
print(f"   Funding contra: {'⚠️ SIM' if result['funding_against'] else '✅ NÃO'}")
print(f"   🎯 FECHA POSIÇÃO: {'✅ SIM' if result['should_close'] else '❌ NÃO'}")

# Cenário 2: Funding alto contra LONG
print("\n📊 CENÁRIO 2: Funding alto contra LONG (SOL)")
print("-" * 40)
result = simulate_trailing(
    position_size=6.25,
    entry_price=150.0,
    peak_price=151.5,    # Subiu 1%
    current_price=151.0, # Voltou um pouco
    side="LONG",
    funding_rate=0.05    # Funding ALTO - LONGs pagam
)
print(f"   Entrada: $150 | Pico: $151.50 | Atual: $151")
print(f"   Lucro: {result['profit_pct']:.2f}% = ${result['profit_usd']:.4f}")
print(f"   Trailing ativado: {'✅' if result['activated'] else '❌'} (>= {config.TRAILING_ACTIVATION_PERCENT}%)")
print(f"   Trailing stop em: ${result['trailing_stop']:.2f}")
print(f"   Stop atingido: {'✅' if result['triggered'] else '❌'}")
print(f"   Mínimo exigido: ${result['min_profit']:.2f} (aumentado por funding)")
print(f"   Funding contra: {'⚠️ SIM' if result['funding_against'] else '✅ NÃO'}")
print(f"   🎯 FECHA POSIÇÃO: {'✅ SIM' if result['should_close'] else '❌ NÃO (lucro < mínimo)'}")

# Cenário 3: BTC com lucro grande
print("\n📊 CENÁRIO 3: BTC com lucro maior")
print("-" * 40)
result = simulate_trailing(
    position_size=125.0,  # Mínimo BTC
    entry_price=50000.0,
    peak_price=50500.0,   # Subiu 1%
    current_price=50400.0,# Voltou um pouco
    side="LONG",
    funding_rate=0.01
)
print(f"   Entrada: $50000 | Pico: $50500 | Atual: $50400")
print(f"   Lucro: {result['profit_pct']:.2f}% = ${result['profit_usd']:.4f}")
print(f"   Trailing ativado: {'✅' if result['activated'] else '❌'} (>= {config.TRAILING_ACTIVATION_PERCENT}%)")
print(f"   Trailing stop em: ${result['trailing_stop']:.2f}")
print(f"   Stop atingido: {'✅' if result['triggered'] else '❌'}")
print(f"   Mínimo exigido: ${result['min_profit']:.2f}")
print(f"   🎯 FECHA POSIÇÃO: {'✅ SIM' if result['should_close'] else '❌ NÃO'}")

# ============================================
# 5. RESUMO FINAL
# ============================================
print("\n" + "=" * 60)
print("📋 RESUMO DA CONFIGURAÇÃO")
print("=" * 60)

print(f"""
🔧 CONFIGURAÇÃO ATUAL:
   • 6 pares dinâmicos (sem fixos)
   • Mínimo notional máximo: ${config.MAX_MIN_NOTIONAL} (exclui BTC/ETH)
   • Atualização a cada 1 hora
   • Trades sempre no valor MÍNIMO de cada par
   • Trailing: Ativa em {config.TRAILING_ACTIVATION_PERCENT}%, stop {config.TRAILING_DISTANCE_PERCENT}% do pico
   • Mínimo para fechar: ${config.TRAILING_MIN_PROFIT_USD} (ou ${config.TRAILING_MIN_PROFIT_HIGH_FUNDING} se funding alto)

💰 CAPITAL ESTIMADO NECESSÁRIO:
   • Para operar 6 pares em hedge: ~${(total_long + total_short) * 1.1:.2f}
   • Seu capital simulado: ${CAPITAL_SIMULADO:.2f}
   
✅ OBSERVAÇÕES:
   • Todos os pares têm mínimo <= ${config.MAX_MIN_NOTIONAL}
   • Com ${CAPITAL_SIMULADO:.2f}, consegue operar todos os 6 pares
   • BTC e ETH serão excluídos automaticamente (mínimo muito alto)
""")

print("=" * 60)
print("✅ TESTE CONCLUÍDO!")
print("=" * 60)
