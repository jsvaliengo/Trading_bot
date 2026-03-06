"""
ESTRATÉGIAS DE TRADING
======================
Este módulo contém a lógica das estratégias de trading.
Implementa Hedge, DCA e análise técnica simples.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import numpy as np

from .config import config

logger = logging.getLogger(__name__)


class Signal(Enum):
    """
    Sinais de trading possíveis.
    """
    STRONG_BUY = "STRONG_BUY"      # Sinal forte de compra
    BUY = "BUY"                     # Sinal de compra
    NEUTRAL = "NEUTRAL"             # Sem sinal claro
    SELL = "SELL"                   # Sinal de venda
    STRONG_SELL = "STRONG_SELL"    # Sinal forte de venda


@dataclass
class TradeSetup:
    """
    Configuração de um trade a ser executado.
    """
    symbol: str
    signal: Signal
    long_size: float      # Tamanho da posição LONG (em USDT)
    short_size: float     # Tamanho da posição SHORT (em USDT)
    entry_price: float
    stop_loss: float
    take_profit: float
    dca_levels: List[float]  # Preços para ordens DCA
    metadata: Dict[str, Any] = field(default_factory=dict)


class TechnicalAnalysis:
    """
    Análise técnica simples usando médias móveis e RSI.
    Não é a estratégia do CoinTech2U exatamente, mas funciona
    como base para decisões de entrada.
    """
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> float:
        """
        Calcula a Média Móvel Exponencial (EMA).
        
        A EMA dá mais peso aos preços recentes, reagindo
        mais rápido às mudanças do que a SMA.
        """
        if len(prices) < period:
            return prices[-1] if prices else 0
        
        multiplier = 2 / (period + 1)
        ema = prices[0]
        
        for price in prices[1:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        """
        Calcula o RSI (Relative Strength Index).
        
        O RSI mede a força do movimento de preço:
        - RSI > 70 = Sobrecomprado (possível queda)
        - RSI < 30 = Sobrevendido (possível alta)
        - RSI entre 30-70 = Neutro
        
        Fórmula: RSI = 100 - (100 / (1 + RS))
        Onde RS = Média dos ganhos / Média das perdas
        """
        if len(prices) < period + 1:
            return 50  # Retorna neutro se não há dados suficientes
        
        # Calcula as mudanças de preço
        deltas = np.diff(prices[-period-1:])
        
        # Separa ganhos e perdas
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        # Calcula as médias
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        
        # Evita divisão por zero
        if avg_loss == 0:
            return 100
        
        # Calcula o RS e o RSI
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_bollinger_bands(
        prices: List[float], 
        period: int = 20, 
        std_dev: int = 2
    ) -> Tuple[float, float, float]:
        """
        Calcula as Bandas de Bollinger.
        
        As bandas mostram a volatilidade do mercado:
        - Preço tocando banda superior = possível reversão para baixo
        - Preço tocando banda inferior = possível reversão para cima
        - Bandas se estreitando = explosão de volatilidade vindo
        
        Retorna: (banda_inferior, média, banda_superior)
        """
        if len(prices) < period:
            return (prices[-1] * 0.98, prices[-1], prices[-1] * 1.02)
        
        sma = np.mean(prices[-period:])
        std = np.std(prices[-period:])
        
        upper = sma + (std * std_dev)
        lower = sma - (std * std_dev)
        
        return (lower, sma, upper)
    
    @staticmethod
    def calculate_atr(
        highs: List[float], 
        lows: List[float], 
        closes: List[float], 
        period: int = 14
    ) -> float:
        """
        Calcula o ATR (Average True Range).
        
        O ATR mede a volatilidade média do ativo.
        Útil para definir Stop Loss e Take Profit dinâmicos.
        
        True Range = max(high-low, |high-close_ant|, |low-close_ant|)
        ATR = Média dos True Ranges
        """
        if len(highs) < period + 1:
            return (highs[-1] - lows[-1]) if highs else 0
        
        true_ranges = []
        
        for i in range(1, len(highs)):
            high_low = highs[i] - lows[i]
            high_close = abs(highs[i] - closes[i-1])
            low_close = abs(lows[i] - closes[i-1])
            
            tr = max(high_low, high_close, low_close)
            true_ranges.append(tr)
        
        return np.mean(true_ranges[-period:])


class HedgeStrategy:
    """
    Estratégia de Hedge similar ao CoinTech2U.
    
    A ideia central é sempre ter posições opostas para
    minimizar o risco de movimentos bruscos do mercado.
    
    COMO FUNCIONA:
    1. Analisa o mercado e identifica a tendência
    2. Abre posição principal na direção da tendência
    3. Abre posição de hedge (menor) na direção oposta
    4. Se o mercado vai contra, a hedge compensa parte da perda
    5. Se o mercado vai a favor, lucra na posição principal
    """
    
    def __init__(self):
        self.config = config
        self.ta = TechnicalAnalysis()
    
    def analyze_market(self, klines: List[Dict]) -> Signal:
        """
        Analisa os candles e retorna um sinal de trading.
        
        Combina vários indicadores para decidir:
        - RSI para momentum
        - EMAs para tendência
        - Bollinger Bands para volatilidade
        """
        if not klines or len(klines) < 30:
            return Signal.NEUTRAL
        
        # Extrai os preços de fechamento
        closes = [k['close'] for k in klines]
        current_price = closes[-1]
        
        # Calcula indicadores
        ema_fast = self.ta.calculate_ema(closes, 9)
        ema_slow = self.ta.calculate_ema(closes, 21)
        rsi = self.ta.calculate_rsi(closes, 14)
        bb_lower, bb_mid, bb_upper = self.ta.calculate_bollinger_bands(closes, 20)
        
        # Sistema de pontuação
        score = 0
        
        # EMA Crossover: EMA rápida acima da lenta = alta
        if ema_fast > ema_slow:
            score += 2
        else:
            score -= 2
        
        # RSI
        if rsi < 30:  # Sobrevendido = possível alta
            score += 2
        elif rsi > 70:  # Sobrecomprado = possível queda
            score -= 2
        elif rsi > 50:
            score += 1
        else:
            score -= 1
        
        # Bollinger Bands
        if current_price < bb_lower:  # Preço abaixo da banda = possível alta
            score += 1
        elif current_price > bb_upper:  # Preço acima da banda = possível queda
            score -= 1
        
        # Preço em relação à EMA de 21
        if current_price > ema_slow:
            score += 1
        else:
            score -= 1
        
        # Converte score em sinal
        if score >= 4:
            return Signal.STRONG_BUY
        elif score >= 2:
            return Signal.BUY
        elif score <= -4:
            return Signal.STRONG_SELL
        elif score <= -2:
            return Signal.SELL
        else:
            return Signal.NEUTRAL
    
    def calculate_position_sizes(
        self, 
        signal: Signal, 
        available_capital: float,
        min_notional: float = 5.0
    ) -> Tuple[float, float]:
        """
        Calcula o tamanho das posições LONG e SHORT baseado no sinal.
        
        DUAS OPÇÕES (configurável em config.USE_MIN_NOTIONAL_ONLY):
        
        1. USE_MIN_NOTIONAL_ONLY = True (padrão):
           - SEMPRE usa o valor MÍNIMO do par + margem de segurança
           - Ideal para diversificar em mais pares com capital limitado
           - Ex: Mínimo $5 + 25% = $6.25 por posição
        
        2. USE_MIN_NOTIONAL_ONLY = False:
           - Usa o MAIOR entre X% do capital OU o mínimo do par
           - Ideal para capitalizar mais em cada trade
        
        A Binance exige um valor MÍNIMO POR POSIÇÃO (não pelo total).
        Então para hedge completo: 2 × mínimo da moeda (LONG + SHORT)
        """
        # Margem de segurança de 25% acima do mínimo da Binance
        SAFETY_MARGIN = 1.25
        min_per_position = min_notional * SAFETY_MARGIN
        
        # Primeiro calcula a proporção baseada no sinal
        if signal == Signal.STRONG_BUY:
            long_ratio, short_ratio = 0.7, 0.3
        elif signal == Signal.BUY:
            long_ratio, short_ratio = 0.6, 0.4
        elif signal == Signal.STRONG_SELL:
            long_ratio, short_ratio = 0.3, 0.7
        elif signal == Signal.SELL:
            long_ratio, short_ratio = 0.4, 0.6
        else:  # NEUTRAL
            long_ratio = self.config.HEDGE_RATIO
            short_ratio = 1 - self.config.HEDGE_RATIO
        
        # ============================================
        # DECIDE O TAMANHO DAS POSIÇÕES
        # ============================================
        if self.config.USE_MIN_NOTIONAL_ONLY:
            # SEMPRE usa o mínimo do par (para diversificar mais)
            long_size = min_per_position
            short_size = min_per_position
            logger.info(f"📊 Usando valor MÍNIMO: ${min_per_position:.2f} por posição")
        else:
            # Usa o MAIOR entre percentual e mínimo
            percent_value = available_capital * self.config.MAX_POSITION_PERCENT
            
            # Calcula os valores iniciais baseados no percentual
            long_size = percent_value * long_ratio
            short_size = percent_value * short_ratio
            
            # Garante que cada posição atenda ao mínimo
            long_size = max(long_size, min_per_position)
            short_size = max(short_size, min_per_position)
        
        # Calcula o valor total necessário
        total_needed = long_size + short_size
        
        # Verifica se tem capital suficiente (com 10% extra para fees)
        min_required = total_needed * 1.1
        
        if available_capital < min_required:
            logger.warning("⚠️ Capital insuficiente para hedge completo")
            logger.warning(f"   Disponível: ${available_capital:.2f} | Necessário: ${min_required:.2f}")
            logger.warning(f"   (LONG ${long_size:.2f} + SHORT ${short_size:.2f} + 10% fees)")
            return (0.0, 0.0)  # Retorna zero para pular este trade
        
        # Log da decisão
        logger.info(f"📊 Tamanho das posições: LONG ${long_size:.2f} + SHORT ${short_size:.2f} = ${total_needed:.2f}")
        
        return (long_size, short_size)
    
    def calculate_dca_levels(
        self, 
        entry_price: float, 
        signal: Signal
    ) -> List[Dict]:
        """
        Calcula os níveis de DCA (Dollar Cost Averaging).
        
        DCA funciona assim:
        1. Abre posição inicial
        2. Se o preço cai X%, adiciona mais à posição
        3. Isso reduz o preço médio de entrada
        4. Quando o preço volta, lucra mais rápido
        
        CUIDADO: DCA em posição perdedora pode aumentar muito o risco!
        """
        if not self.config.DCA_ENABLED:
            return []
        
        dca_levels = []
        current_size = 1.0  # Tamanho base
        
        for i in range(1, self.config.DCA_MAX_ORDERS + 1):
            # Calcula o preço do nível DCA
            step = self.config.DCA_STEP_PERCENT * i / 100
            
            # Para LONG: DCA em preços mais baixos
            # Para SHORT: DCA em preços mais altos
            if signal in [Signal.STRONG_BUY, Signal.BUY, Signal.NEUTRAL]:
                dca_price = entry_price * (1 - step)
                position_side = 'LONG'
            else:
                dca_price = entry_price * (1 + step)
                position_side = 'SHORT'
            
            # Aumenta o tamanho progressivamente (Martingale suave)
            current_size *= self.config.DCA_MULTIPLIER
            
            dca_levels.append({
                'level': i,
                'price': round(dca_price, 2),
                'size_multiplier': round(current_size, 2),
                'position_side': position_side
            })
        
        return dca_levels
    
    def calculate_stop_loss_take_profit(
        self, 
        entry_price: float, 
        signal: Signal,
        atr: float = None
    ) -> Tuple[float, float]:
        """
        Calcula Stop Loss e Take Profit.
        
        Pode usar:
        1. Percentual fixo (config)
        2. ATR dinâmico (mais inteligente)
        
        O ATR ajusta o SL/TP à volatilidade atual do mercado.
        """
        if signal in [Signal.STRONG_BUY, Signal.BUY, Signal.NEUTRAL]:
            # Posição principal é LONG
            if atr and atr > 0:
                # SL dinâmico: 2x ATR abaixo do preço
                stop_loss = entry_price - (atr * 2)
                take_profit = entry_price + (atr * 3)  # Risk/Reward 1:1.5
            else:
                # SL fixo baseado na config
                stop_loss = entry_price * (1 - self.config.STOP_LOSS_PERCENT / 100)
                take_profit = entry_price * (1 + self.config.TAKE_PROFIT_PERCENT / 100)
        else:
            # Posição principal é SHORT
            if atr and atr > 0:
                stop_loss = entry_price + (atr * 2)
                take_profit = entry_price - (atr * 3)
            else:
                stop_loss = entry_price * (1 + self.config.STOP_LOSS_PERCENT / 100)
                take_profit = entry_price * (1 - self.config.TAKE_PROFIT_PERCENT / 100)
        
        return (round(stop_loss, 2), round(take_profit, 2))
    
    def generate_trade_setup(
        self, 
        symbol: str, 
        klines: List[Dict],
        available_capital: float,
        min_notional: float = 5.0
    ) -> Optional[TradeSetup]:
        """
        Gera uma configuração completa de trade.
        
        Este é o método principal que combina toda a análise
        e retorna uma estrutura pronta para execução.
        
        Args:
            symbol: Par de trading (ex: 'ETHUSDT')
            klines: Dados de candles
            available_capital: Saldo disponível atual (usa X% deste valor)
            min_notional: Valor mínimo exigido pela Binance para este par
        """
        if not klines:
            return None
        
        # Analisa o mercado
        signal = self.analyze_market(klines)
        
        # Preço atual
        entry_price = klines[-1]['close']
        
        # Calcula tamanhos das posições (sistema inteligente com mínimo garantido)
        long_size, short_size = self.calculate_position_sizes(
            signal, 
            available_capital,
            min_notional=min_notional
        )
        
        # Se retornou zero, significa que não tem capital suficiente
        if long_size == 0 and short_size == 0:
            logger.warning(f"⏸️ Pulando {symbol} - capital insuficiente para mínimo")
            return None
        
        # Calcula ATR para SL/TP dinâmico
        closes = [k['close'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]
        atr = self.ta.calculate_atr(highs, lows, closes)
        
        # Calcula SL e TP
        stop_loss, take_profit = self.calculate_stop_loss_take_profit(
            entry_price, signal, atr
        )
        
        # Calcula níveis de DCA
        dca_levels = self.calculate_dca_levels(entry_price, signal)
        
        logger.info(f"""
        📊 Trade Setup para {symbol}:
        ├── Sinal: {signal.value}
        ├── Preço: ${entry_price:.2f}
        ├── LONG: ${long_size:.2f} USDT
        ├── SHORT: ${short_size:.2f} USDT
        ├── Stop Loss: ${stop_loss:.2f}
        ├── Take Profit: ${take_profit:.2f}
        └── DCA Levels: {len(dca_levels)}
        """)
        
        return TradeSetup(
            symbol=symbol,
            signal=signal,
            long_size=long_size,
            short_size=short_size,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            dca_levels=[d['price'] for d in dca_levels]
        )


class RangeScalpingStrategy:
    """
    Estratégia de range scalping com validação de estrutura antes da entrada.

    Regras principais:
    - só opera quando detectar range de qualidade
    - compra na zona inferior e vende na zona superior
    - não opera na zona morta (meio do range)
    - usa stop além do limite do range + buffer
    """

    def __init__(self):
        self.config = config

    @staticmethod
    def _timeframe_to_minutes(timeframe: str) -> int:
        token = str(timeframe or "5m").strip().lower()
        if token.endswith("m"):
            return max(1, int(token[:-1] or "1"))
        if token.endswith("h"):
            return max(1, int(token[:-1] or "1")) * 60
        if token.endswith("d"):
            return max(1, int(token[:-1] or "1")) * 1440
        return 5

    @staticmethod
    def _clip(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _analyze_range_context(self, klines: List[Dict]) -> Optional[Dict[str, float]]:
        if not klines:
            return None

        timeframe_minutes = self._timeframe_to_minutes(self.config.TIMEFRAME)
        min_window = max(
            10,
            int(np.ceil(float(self.config.RANGE_SCALP_MIN_RANGE_MINUTES) / max(1, timeframe_minutes))),
        )
        if len(klines) < min_window:
            return None

        window_size = min(len(klines), max(min_window, 12))
        window = klines[-window_size:]

        highs = [float(item["high"]) for item in window]
        lows = [float(item["low"]) for item in window]
        closes = [float(item["close"]) for item in window]
        opens = [float(item["open"]) for item in window]
        volumes = [float(item.get("volume", 0.0) or 0.0) for item in window]

        support = min(lows)
        resistance = max(highs)
        range_size = resistance - support
        if range_size <= 0:
            return None

        mid_price = (support + resistance) / 2.0
        amplitude_pct = (range_size / max(mid_price, 1e-9)) * 100.0
        if amplitude_pct < float(self.config.RANGE_SCALP_MIN_RANGE_PERCENT):
            return None

        touch_tolerance = max(
            range_size * float(self.config.RANGE_SCALP_TOUCH_TOLERANCE_RATIO),
            mid_price * 0.0005,
        )
        rejection_min_move = range_size * float(self.config.RANGE_SCALP_REJECTION_MIN_RATIO)

        support_touches = 0
        resistance_touches = 0
        for index, close_price in enumerate(closes):
            candle_low = lows[index]
            candle_high = highs[index]
            if candle_low <= (support + touch_tolerance) and close_price >= (candle_low + rejection_min_move):
                support_touches += 1
            if candle_high >= (resistance - touch_tolerance) and close_price <= (candle_high - rejection_min_move):
                resistance_touches += 1

        min_touches = max(1, int(self.config.RANGE_SCALP_MIN_TOUCHES_PER_SIDE))
        if support_touches < min_touches or resistance_touches < min_touches:
            return None

        edge_ratio = self._clip(float(self.config.RANGE_SCALP_EDGE_ZONE_RATIO), 0.05, 0.45)
        buy_zone_upper = support + (range_size * edge_ratio)
        sell_zone_lower = resistance - (range_size * edge_ratio)

        edge_closes = sum(1 for close_price in closes if close_price <= buy_zone_upper or close_price >= sell_zone_lower)
        edge_participation = edge_closes / max(1, len(closes))
        if edge_participation < float(self.config.RANGE_SCALP_MIN_EDGE_PARTICIPATION):
            return None

        # Volume no range deve estar controlado versus janela anterior.
        previous_window = klines[-(window_size * 2):-window_size] if len(klines) >= (window_size * 2) else klines[:-window_size]
        if previous_window:
            previous_volumes = [float(item.get("volume", 0.0) or 0.0) for item in previous_window]
            previous_avg_volume = float(np.mean(previous_volumes)) if previous_volumes else 0.0
            current_avg_volume = float(np.mean(volumes)) if volumes else 0.0
            if previous_avg_volume > 0 and current_avg_volume > (previous_avg_volume * float(self.config.RANGE_SCALP_MAX_VOLUME_RATIO)):
                return None

        # Invalidação: volume crescente consecutivo dentro do range.
        volume_streak = max(2, int(self.config.RANGE_SCALP_INVALIDATE_VOLUME_STREAK))
        if len(window) >= volume_streak:
            recent = window[-volume_streak:]
            recent_vol = [float(item.get("volume", 0.0) or 0.0) for item in recent]
            recent_close = [float(item["close"]) for item in recent]
            recent_inside = all(support <= value <= resistance for value in recent_close)
            increasing_vol = all(recent_vol[idx] < recent_vol[idx + 1] for idx in range(len(recent_vol) - 1))
            if recent_inside and increasing_vol:
                return None

        # Invalidação: sequência direcional forte (sinal de breakout iminente).
        momentum_candles = max(2, int(self.config.RANGE_SCALP_INVALIDATE_MOMENTUM_CANDLES))
        if len(window) >= momentum_candles:
            closes_tail = closes[-momentum_candles:]
            opens_tail = opens[-momentum_candles:]
            bullish_seq = all(closes_tail[idx] < closes_tail[idx + 1] for idx in range(len(closes_tail) - 1))
            bullish_bodies = all(closes_tail[idx] > opens_tail[idx] for idx in range(len(closes_tail)))
            bearish_seq = all(closes_tail[idx] > closes_tail[idx + 1] for idx in range(len(closes_tail) - 1))
            bearish_bodies = all(closes_tail[idx] < opens_tail[idx] for idx in range(len(closes_tail)))
            if bullish_seq and bullish_bodies and closes_tail[-1] > mid_price:
                return None
            if bearish_seq and bearish_bodies and closes_tail[-1] < mid_price:
                return None

        return {
            "support": support,
            "resistance": resistance,
            "mid_price": mid_price,
            "range_size": range_size,
            "amplitude_pct": amplitude_pct,
            "buy_zone_upper": buy_zone_upper,
            "sell_zone_lower": sell_zone_lower,
            "window_size": float(window_size),
        }

    def generate_trade_setup(
        self,
        symbol: str,
        klines: List[Dict],
        available_capital: float,
        min_notional: float = 5.0,
    ) -> Optional[TradeSetup]:
        if not klines or len(klines) < 12:
            return None

        context = self._analyze_range_context(klines)
        if not context:
            return None

        current_price = float(klines[-1]["close"])
        support = float(context["support"])
        resistance = float(context["resistance"])
        range_size = float(context["range_size"])
        buy_zone_upper = float(context["buy_zone_upper"])
        sell_zone_lower = float(context["sell_zone_lower"])

        is_buy_zone = support <= current_price <= buy_zone_upper
        is_sell_zone = sell_zone_lower <= current_price <= resistance
        if not is_buy_zone and not is_sell_zone:
            return None

        min_mult = float(self.config.RANGE_SCALP_MIN_POSITION_MULTIPLIER)
        max_mult = float(self.config.RANGE_SCALP_MAX_POSITION_MULTIPLIER)
        zone_span = max(range_size * float(self.config.RANGE_SCALP_EDGE_ZONE_RATIO), 1e-9)
        if is_buy_zone:
            depth = (buy_zone_upper - current_price) / zone_span
            signal = Signal.STRONG_BUY
            zone_side = "BUY_ZONE"
        else:
            depth = (current_price - sell_zone_lower) / zone_span
            signal = Signal.STRONG_SELL
            zone_side = "SELL_ZONE"
        depth = self._clip(depth, 0.0, 1.0)
        size_multiplier = min_mult + (max_mult - min_mult) * depth

        min_per_position = float(min_notional) * 1.25
        base_size = max(min_per_position, float(available_capital) * float(self.config.MAX_POSITION_PERCENT))
        side_size = base_size * size_multiplier

        # Proteção simples de capital antes de gerar setup.
        if available_capital < (side_size * 1.05):
            return None

        stop_buffer = max(
            range_size * float(self.config.RANGE_SCALP_STOP_BUFFER_RATIO),
            current_price * (float(self.config.RANGE_SCALP_STOP_BUFFER_MIN_PERCENT) / 100.0),
        )
        tp_ratio = float(self.config.RANGE_SCALP_TAKE_PROFIT_RATIO)

        if is_buy_zone:
            stop_loss = support - stop_buffer
            take_profit = support + (range_size * tp_ratio)
            risk = current_price - stop_loss
            reward = take_profit - current_price
            long_size, short_size = side_size, 0.0
            dca_levels = [
                round(buy_zone_upper - (range_size * 0.10), 8),
                round(support + (range_size * 0.02), 8),
            ]
        else:
            stop_loss = resistance + stop_buffer
            take_profit = resistance - (range_size * tp_ratio)
            risk = stop_loss - current_price
            reward = current_price - take_profit
            long_size, short_size = 0.0, side_size
            dca_levels = [
                round(sell_zone_lower + (range_size * 0.10), 8),
                round(resistance - (range_size * 0.02), 8),
            ]

        if risk <= 0 or reward <= 0:
            return None

        rr_ratio = reward / max(risk, 1e-9)
        if rr_ratio < float(self.config.RANGE_SCALP_MIN_RISK_REWARD):
            return None

        logger.info(
            "🧭 Range setup %s em %s | amp=%.2f%% | R:R=%.2f | depth=%.2f",
            zone_side,
            symbol,
            context["amplitude_pct"],
            rr_ratio,
            depth,
        )

        return TradeSetup(
            symbol=symbol,
            signal=signal,
            long_size=round(long_size, 8),
            short_size=round(short_size, 8),
            entry_price=current_price,
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            dca_levels=dca_levels,
            metadata={
                "strategy_type": "range_scalping",
                "range_support": round(support, 8),
                "range_resistance": round(resistance, 8),
                "range_mid_price": round(context["mid_price"], 8),
                "range_amplitude_pct": round(context["amplitude_pct"], 6),
                "custom_stop_loss": round(stop_loss, 8),
                "custom_take_profit": round(take_profit, 8),
                "position_multiplier": round(size_multiplier, 6),
                "range_window_candles": int(context["window_size"]),
                "entry_zone": zone_side,
            },
        )


class RiskManager:
    """
    Gerenciador de Risco.
    
    Monitora e controla o risco das operações para
    proteger o capital de perdas excessivas.
    """
    
    def __init__(self):
        self.config = config
        self.daily_pnl = 0.0
        self.initial_capital = config.TOTAL_CAPITAL
    
    def update_pnl(self, pnl: float):
        """Atualiza o P&L diário."""
        self.daily_pnl += pnl
    
    def can_open_position(self, current_positions: int) -> bool:
        """
        Verifica se pode abrir nova posição.
        
        Checa:
        1. Número máximo de posições
        2. Perda diária máxima
        """
        # Verifica número de posições
        if current_positions >= self.config.MAX_OPEN_POSITIONS:
            logger.warning(f"⚠️  Máximo de posições atingido ({current_positions})")
            return False
        
        # Verifica perda diária
        max_loss = self.initial_capital * (self.config.MAX_DAILY_LOSS_PERCENT / 100)
        if self.daily_pnl < -max_loss:
            logger.warning(f"⚠️  Perda diária máxima atingida (${abs(self.daily_pnl):.2f})")
            return False
        
        return True
