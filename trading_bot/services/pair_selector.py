"""
SELEÇÃO INTELIGENTE DE PARES
============================
Analisa todos os pares disponíveis na Binance Futures e seleciona
os melhores baseado em múltiplos critérios.

Critérios (em ordem de prioridade):
1. Volatilidade - Pares que se movem mais (mais oportunidades)
2. Volume 24h - Pares com mais liquidez (menos slippage)
3. Tendência - Pares com direção clara (mais previsíveis)
4. Funding Rate - Pares com funding favorável (menos custos)
5. Spread - Pares com menor diferença bid/ask

Autor: Trading Bot
"""

import logging
from typing import List, Dict, Tuple
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class PairSelector:
    """
    Classe para selecionar os melhores pares de trading automaticamente.
    Considera o capital disponível para não selecionar pares que não cabem no orçamento.
    """
    
    def __init__(self, exchange, config):
        """
        Inicializa o seletor de pares.
        
        Args:
            exchange: Instância do BinanceClient
            config: Configurações do bot
        """
        self.exchange = exchange
        self.config = config
        self.last_update = None
        self.pair_scores = {}  # Cache dos scores
        
        # Lista de pares a ignorar (stablecoins, etc)
        self.IGNORE_PAIRS = [
            'USDCUSDT', 'BUSDUSDT', 'TUSDUSDT', 'USDPUSDT',
            'FDUSDUSDT', 'EURUSDT', 'GBPUSDT'
        ]
        
        # Margem de segurança para trades (25% acima do mínimo)
        self.SAFETY_MARGIN = 1.25
        # Margem para fees (10%)
        self.FEE_MARGIN = 1.10
        
        logger.info("✅ Seletor de pares inicializado")

    def _is_disabled(self, symbol: str) -> bool:
        """Retorna True se o par estiver desabilitado na configuração."""
        if hasattr(self.config, "is_pair_disabled"):
            return bool(self.config.is_pair_disabled(symbol))

        disabled_pairs = {
            str(item).upper() for item in (getattr(self.config, "DISABLED_PAIRS", []) or [])
        }
        return str(symbol).upper() in disabled_pairs
    
    def get_all_futures_pairs(self) -> List[str]:
        """
        Busca todos os pares de futuros disponíveis na Binance.
        Filtra apenas pares USDT perpétuos.
        """
        try:
            exchange_info = self.exchange.get_exchange_info()
            if not exchange_info:
                return []
            pairs = []
            
            for symbol_info in exchange_info.get('symbols', []):
                symbol = symbol_info.get('symbol')
                if not symbol:
                    continue
                
                # Filtra apenas pares USDT perpétuos ativos
                if (symbol.endswith('USDT') and 
                    symbol_info.get('contractType') == 'PERPETUAL' and
                    symbol_info.get('status') == 'TRADING' and
                    symbol not in self.IGNORE_PAIRS and
                    not self._is_disabled(symbol)):
                    pairs.append(symbol)
            
            logger.info(f"📊 {len(pairs)} pares de futuros disponíveis")
            return pairs
            
        except Exception as e:
            logger.error(f"Erro ao buscar pares de futuros: {e}")
            return []
    
    def get_pair_metrics(
        self,
        symbol: str,
        prefetched_ticker: Dict = None,
        prefetched_funding_rate: float = None,
    ) -> Dict:
        """
        Calcula as métricas de um par específico.

        Args:
            symbol: Par de trading
            prefetched_ticker: Dados de ticker 24h já buscados em bulk (evita chamada extra)
            prefetched_funding_rate: Funding rate já buscado em bulk (evita chamada extra)

        Returns:
            Dict com volatilidade, volume, tendência, funding, spread
        """
        try:
            # Ticker 24h — usa pré-buscado se disponível
            if prefetched_ticker:
                ticker_24h = prefetched_ticker
            else:
                ticker_24h = self.exchange.get_ticker_24h(symbol)

            # Orderbook para spread — sempre por símbolo (não há bulk)
            orderbook = self.exchange.get_order_book(symbol, limit=5)

            # Klines para volatilidade e tendência — sempre por símbolo
            klines = self.exchange.get_klines_raw(
                symbol=symbol,
                interval='1h',
                limit=24
            )

            # Funding rate — usa pré-buscado se disponível
            if prefetched_funding_rate is not None:
                funding_rate = prefetched_funding_rate
            else:
                funding_info = self.exchange.get_funding_rate(symbol)
                funding_rate = funding_info['rate_percent']

            # Min notional (usa cache interno da exchange)
            symbol_info = self.exchange.get_symbol_info(symbol)
            min_notional = symbol_info.get('minNotional', 100)

            # ============================================
            # CALCULA MÉTRICAS
            # ============================================

            # 1. VOLUME 24H (em USD)
            volume_24h = float(ticker_24h['quoteVolume'])

            # 2. VOLATILIDADE (desvio padrão dos retornos horários)
            closes = [float(k[4]) for k in klines]
            if len(closes) > 1:
                returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                           for i in range(1, len(closes))]
                volatility = self._calculate_std(returns)
            else:
                volatility = 0

            # 3. TENDÊNCIA (força e direção)
            if len(closes) >= 2:
                price_change_24h = (closes[-1] - closes[0]) / closes[0] * 100
                trend_strength = abs(price_change_24h)
            else:
                price_change_24h = 0
                trend_strength = 0

            # 4. SPREAD
            best_bid = float(orderbook['bids'][0][0]) if orderbook['bids'] else 0
            best_ask = float(orderbook['asks'][0][0]) if orderbook['asks'] else 0
            if best_bid > 0 and best_ask > 0:
                spread_percent = ((best_ask - best_bid) / best_bid) * 100
            else:
                spread_percent = 999

            # 5. PREÇO ATUAL
            current_price = float(ticker_24h['lastPrice'])

            return {
                'symbol': symbol,
                'volume_24h': volume_24h,
                'volatility': volatility,
                'trend_strength': trend_strength,
                'price_change_24h': price_change_24h,
                'funding_rate': funding_rate,
                'spread_percent': spread_percent,
                'current_price': current_price,
                'min_notional': min_notional
            }

        except Exception as e:
            logger.error(f"Erro ao calcular métricas de {symbol}: {e}")
            return None
    
    def _calculate_std(self, values: List[float]) -> float:
        """Calcula o desvio padrão de uma lista de valores."""
        if not values:
            return 0
        
        n = len(values)
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / n
        return variance ** 0.5
    
    def score_pair(self, metrics: Dict) -> float:
        """
        Calcula o score de um par baseado nas métricas e pesos configurados.
        
        Maior score = melhor par para trading
        """
        if not metrics:
            return 0
        
        weights = self.config.PAIR_SELECTION_WEIGHTS
        
        # ============================================
        # NORMALIZAÇÃO E SCORING
        # ============================================
        
        # 1. VOLATILIDADE (maior = melhor, até certo ponto)
        # Ideal: 2-5% de volatilidade
        vol = metrics['volatility']
        if vol < 1:
            vol_score = vol * 50  # Penaliza baixa volatilidade
        elif vol <= 5:
            vol_score = 100  # Ideal
        else:
            vol_score = max(0, 100 - (vol - 5) * 10)  # Penaliza muito alta
        
        # 2. VOLUME (maior = melhor)
        # Normaliza para 0-100 baseado em $50M-$500M
        volume = metrics['volume_24h']
        if volume >= 500_000_000:
            volume_score = 100
        elif volume >= self.config.MIN_VOLUME_24H_USD:
            volume_score = (volume - self.config.MIN_VOLUME_24H_USD) / (500_000_000 - self.config.MIN_VOLUME_24H_USD) * 100
        else:
            volume_score = 0  # Abaixo do mínimo
        
        # 3. TENDÊNCIA (maior força = melhor)
        # Ideal: 1-3% de movimento
        trend = metrics['trend_strength']
        if trend < 0.5:
            trend_score = trend * 100  # Penaliza muito parado
        elif trend <= 3:
            trend_score = 100
        else:
            trend_score = max(0, 100 - (trend - 3) * 15)
        
        # 4. FUNDING RATE (mais próximo de 0 = melhor, negativo = bom para longs)
        # Score maior se funding está baixo ou negativo
        funding = abs(metrics['funding_rate'])
        if funding <= 0.01:
            funding_score = 100
        elif funding <= 0.03:
            funding_score = 70
        elif funding <= 0.05:
            funding_score = 40
        else:
            funding_score = 10
        
        # 5. SPREAD (menor = melhor)
        spread = metrics['spread_percent']
        if spread <= 0.02:
            spread_score = 100
        elif spread <= 0.05:
            spread_score = 70
        elif spread <= self.config.MAX_SPREAD_PERCENT:
            spread_score = 40
        else:
            spread_score = 0  # Acima do máximo
        
        # ============================================
        # CALCULA SCORE FINAL PONDERADO
        # ============================================
        total_weight = sum(weights.values())
        
        final_score = (
            vol_score * weights['volatility'] +
            volume_score * weights['volume'] +
            trend_score * weights['trend'] +
            funding_score * weights['funding'] +
            spread_score * weights['spread']
        ) / total_weight
        
        return final_score
    
    def calculate_pair_capital_needed(self, min_notional: float) -> float:
        """
        Calcula quanto capital é necessário para operar um par em hedge.
        
        Fórmula: min_notional × margem_segurança × 2 (LONG + SHORT) × margem_fees
        Ex: $5 × 1.25 × 2 × 1.10 = $13.75
        """
        return min_notional * self.SAFETY_MARGIN * 2 * self.FEE_MARGIN

    def _estimate_fixed_pair_capital_needed(self, symbol: str) -> float:
        """
        Estima capital necessário para um par fixo.

        Tenta usar métricas completas; se falhar, usa get_symbol_info;
        no pior caso usa mínimo conservador de $5.
        """
        min_notional = None

        metrics = self.get_pair_metrics(symbol)
        if metrics:
            try:
                min_notional = float(metrics.get('min_notional', 0) or 0)
            except (TypeError, ValueError):
                min_notional = None

        if min_notional is None or min_notional <= 0:
            try:
                info = self.exchange.get_symbol_info(symbol) or {}
                min_notional = float(info.get('minNotional', 0) or 0)
            except Exception as e:
                logger.warning(f"⚠️ Falha ao obter minNotional de {symbol}: {e}")
                min_notional = None

        if min_notional is None or min_notional <= 0:
            min_notional = 5.0
            logger.warning(
                f"⚠️ Usando minNotional fallback para par fixo {symbol}: ${min_notional:.2f}"
            )

        return self.calculate_pair_capital_needed(min_notional)
    
    def select_best_pairs(self, available_capital: float = None) -> Tuple[List[str], Dict]:
        """
        Seleciona os melhores pares para trading considerando o capital disponível.
        
        Args:
            available_capital: Capital disponível para trading. Se None, busca da exchange.
        
        Returns:
            Tuple com:
            - Lista de símbolos selecionados
            - Dict com scores de cada par
        """
        logger.info("🔍 Iniciando seleção de pares...")
        
        # Busca capital disponível se não foi passado
        if available_capital is None:
            try:
                available_capital = self.exchange.get_available_balance()
            except Exception as e:
                logger.warning(f"⚠️ Falha ao obter saldo disponível da exchange: {e}")
                available_capital = 100.0  # Fallback
        
        logger.info(f"💰 Capital disponível: ${available_capital:.2f}")
        
        # 1. Busca todos os pares disponíveis
        all_pairs = self.get_all_futures_pairs()
        
        if not all_pairs:
            logger.error("❌ Nenhum par encontrado!")
            return self.config.TRADING_PAIRS, {}
        
        # 2. Analisa e pontua cada par
        logger.info("📊 Analisando pares (isso pode levar alguns segundos)...")
        
        pair_scores = {}
        total_pairs = len(all_pairs)
        analyzed = 0
        
        for symbol in all_pairs:
            analyzed += 1
            
            # Log de progresso a cada 50 pares
            if analyzed % 50 == 0 or analyzed == total_pairs:
                logger.info(f"   ⏳ Progresso: {analyzed}/{total_pairs} pares analisados ({analyzed*100//total_pairs}%)")
            
            # Pula pares fixos (serão adicionados depois)
            if symbol in self.config.FIXED_PAIRS:
                continue

            if self._is_disabled(symbol):
                continue
            
            metrics = self.get_pair_metrics(symbol)
            
            if not metrics:
                continue
            
            # ============================================
            # FILTROS
            # ============================================
            
            # Volume mínimo
            if metrics['volume_24h'] < self.config.MIN_VOLUME_24H_USD:
                continue
            
            # Spread máximo
            if metrics['spread_percent'] > self.config.MAX_SPREAD_PERCENT:
                continue
            
            # Volatilidade mínima
            if metrics['volatility'] < self.config.MIN_VOLATILITY_PERCENT:
                continue
            
            # Mínimo notional máximo (exclui BTC, ETH, etc)
            if metrics['min_notional'] > self.config.MAX_MIN_NOTIONAL:
                logger.info(f"   ⏭️ {symbol}: Mínimo ${metrics['min_notional']} > limite ${self.config.MAX_MIN_NOTIONAL}")
                continue
            
            # Calcula capital necessário para este par
            capital_needed = self.calculate_pair_capital_needed(metrics['min_notional'])
            metrics['capital_needed'] = capital_needed
            
            # Calcula score
            score = self.score_pair(metrics)
            pair_scores[symbol] = {
                'score': score,
                'metrics': metrics,
                'capital_needed': capital_needed
            }
        
        # 3. Ordena por score (maior primeiro)
        sorted_pairs = sorted(
            pair_scores.items(),
            key=lambda x: x[1]['score'],
            reverse=True
        )
        
        # ============================================
        # 4. SELECIONA PARES CONSIDERANDO CAPITAL
        # ============================================
        selected_pairs = []  # Começa com os fixos (deduplicados)
        capital_used = 0.0

        # Calcula capital usado pelos pares fixos de forma explícita
        fixed_capital_needed = {}
        for symbol in self.config.FIXED_PAIRS:
            if symbol in selected_pairs:
                continue
            if self._is_disabled(symbol):
                logger.info(f"   ⏭️ {symbol}: desabilitado manualmente")
                continue
            pair_capital_needed = self._estimate_fixed_pair_capital_needed(symbol)
            fixed_capital_needed[symbol] = pair_capital_needed
            selected_pairs.append(symbol)
            capital_used += pair_capital_needed

        if capital_used > available_capital:
            logger.warning(
                f"⚠️ Pares fixos consomem mais capital que o disponível: "
                f"${capital_used:.2f} > ${available_capital:.2f}"
            )

        # Número máximo de pares dinâmicos
        max_dynamic = max(0, self.config.MAX_TRADING_PAIRS - len(selected_pairs))
        selected_count = 0
        
        logger.info(f"📊 Selecionando até {max_dynamic} pares dinâmicos...")
        logger.info(f"💵 Capital restante: ${available_capital - capital_used:.2f}")
        
        for symbol, data in sorted_pairs:
            if selected_count >= max_dynamic:
                break
            
            capital_needed = data['capital_needed']
            
            # Verifica se cabe no capital disponível
            if capital_used + capital_needed > available_capital:
                logger.info(f"   ⏭️ {symbol}: Capital insuficiente (precisa ${capital_needed:.2f}, restam ${available_capital - capital_used:.2f})")
                continue
            
            # Adiciona o par
            selected_pairs.append(symbol)
            capital_used += capital_needed
            selected_count += 1
            
            logger.info(f"   ✅ {symbol}: Score {data['score']:.1f} | Precisa ${capital_needed:.2f} | Usado ${capital_used:.2f}")
        
        # 5. Log dos resultados
        logger.info("=" * 50)
        logger.info("🏆 PARES SELECIONADOS:")
        logger.info("=" * 50)
        
        if self.config.FIXED_PAIRS:
            logger.info("📌 FIXOS:")
            for symbol in self.config.FIXED_PAIRS:
                logger.info(
                    f"   • {symbol}: precisa ${fixed_capital_needed.get(symbol, 0.0):.2f}"
                )
        
        logger.info(f"🔄 DINÂMICOS ({selected_count}/{max_dynamic}):")
        for symbol in selected_pairs:
            if symbol not in self.config.FIXED_PAIRS:
                data = pair_scores.get(symbol, {})
                metrics = data.get('metrics', {})
                logger.info(f"   • {symbol}: Score {data.get('score', 0):.1f} | ${data.get('capital_needed', 0):.2f}")
        
        logger.info("-" * 50)
        logger.info(f"💰 CAPITAL: Usado ${capital_used:.2f} / Disponível ${available_capital:.2f}")
        logger.info(f"📊 PARES: {len(selected_pairs)} selecionados")
        logger.info("=" * 50)
        
        # 6. Atualiza timestamp
        self.last_update = datetime.now(timezone.utc)
        self.pair_scores = pair_scores
        
        return selected_pairs, pair_scores
    
    def should_update(self) -> bool:
        """
        Verifica se está na hora de atualizar a lista de pares.
        """
        if not self.config.AUTO_SELECT_PAIRS:
            return False
        
        if self.last_update is None:
            return True
        
        elapsed = datetime.now(timezone.utc) - self.last_update
        interval = timedelta(minutes=self.config.PAIR_UPDATE_INTERVAL_MINUTES)
        
        return elapsed >= interval
    
