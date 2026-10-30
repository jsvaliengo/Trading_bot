# Relatório: martingale/DCA com hedge estilo CoinTech2u, 5m, pré-registrado

Data: 2026-10-30. Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h não foi tocado.

Pré-registro: `scripts/backtest_v2/MARTINGALE_PREREG.md`, commit `d7833a5`, escrito antes de rodar qualquer simulação. Código: `scripts/backtest_v2/martingale.py` (simulação acoplada de carteira em 5m: todas as pernas de todos os pares avançam barra a barra junto com a equity, porque liquidação, stop de portfólio e rejeição por margem dependem da conta inteira). Testes: `tests/test_backtest_v2_martingale.py` (aritmética da escada e do preço médio, TP não dispara na barra da adição, liquidação recapitaliza, stop de portfólio fecha tudo, sizing, métricas). Para reproduzir (numba é opcional: com ele cada simulação leva 0,1–1 s; sem ele, o laço roda em Python puro, mesmo resultado, ~100× mais lento):

    PYTHONPATH=. python -m scripts.backtest_v2.martingale run
    PYTHONPATH=. python -m scripts.backtest_v2.martingale run --resume --capital 3000 --size-as 300   # sensibilidade pós-hoc
    PYTHONPATH=. python -m scripts.backtest_v2.martingale report

Saídas em `runtime/backtest_v2/out/martingale/` (`results.json` com as 720 simulações, `tables.md` com os 30 grids completos).

## Veredito

**FAIL em todas as variantes, nas duas carteiras e nos dois universos. 0 de 360 células pré-registradas passa; 0 de 360 tem drawdown de equity abaixo de 20%.** A taxa de acerto por trade fechado é de 97,7% a 100,0%, como o app mostra. A conta de $300 é liquidada entre 2 e 148 vezes em 2022→2026 na versão original e nas travas de níveis; com stop de portfólio em 20% ela não liquida, mas sangra até zero em ~44 a 73 stops. O resultado é idêntico (×10) com $3.000, porque a escada é dimensionada ao capital.

| variante | critério 1: 0 liquidações ($300, completo) | critério 2: DD máx < 20% | critério 3: holdout 2026 > 0 | célula padrão (1,5% / 2× / TP 1%) | células que passam | resultado |
|---|---|---|---|---|---|---|
| V1 original (sem stop, níveis ilimitados) | FALHA: 48 liq (univ. 31), 17 liq (BTC/ETH/ADA) | FALHA: 100% | FALHA: −$251 / −$888 | líq −$12.551 / −$5.088 | 0/12 e 0/12 | **FAIL** |
| V2a travas, N = 5 | FALHA: 6 / 3 liq | FALHA: 100% / 99% | FALHA: −$469 / −$208 | líq −$1.963 / −$748 | 0/12 e 0/12 | **FAIL** |
| V2b travas, N = 7 | FALHA: 2 / 3 liq | FALHA: 99% / 100% | FALHA: −$454 / −$176 | líq −$634 / −$590 | 0/12 e 0/12 | **FAIL** |
| V3 N = 7 + stop de portfólio 20% | OK: 0 / 0 liq | FALHA: 100% / 100% (44 / 73 stops, conta vai a zero) | FALHA: −$300 / −$97 | líq −$300 / −$300 | 0/12 e 0/12 | **FAIL** |

(Em cada célula: universo 31 / universo BTC-ETH-ADA. Holdout = conta nova de $300 em 2026-01-01.)

H1 (alguma célula com 0 liquidações, DD < 20% e holdout positivo) é rejeitada. H0 confirmada: a taxa de acerto de ~99% convive com a perda do capital inteiro, uma ou muitas vezes.

## Dados e cobertura

- `.npz` de 5m já em cache da pesquisa de scalping (futuros USDT-M, `data.binance.vision`), funding real. Período completo 2022-01-01 → 2026-09-30, 499.356 barras na grade comum; holdout 2026-01-01 → 2026-09-30, 78.624 barras.
- Universo 31: point-in-time anual (12 pares por ano, 31 símbolos distintos, inclusive LUNA 2022 e MATIC 2024, que morrem no meio do caminho; par que sai do universo fecha as duas pernas no último close). Universo 3: BTC, ETH, ADA com todo o dado disponível; ADA só existe no cache a partir de 2023-12 (membro de 2024/2025). Para o holdout, baixei os 9 zips mensais de 5m de ADA de 2026 que faltavam (único download desta rodada; declarado).
- Funding real: 15.606 eventos no universo 3; LONG paga, SHORT recebe.

### Mudanças depois do pré-registro (declaradas)

Nenhuma regra, grid, custo, período ou critério mudou. Três mudanças, todas depois de ver resultado:

1. **Bug de métrica corrigido:** "pior mês" subtraía o capital inicial duas vezes no primeiro mês (mostrava −$3.049 em 2022-01 numa conta de $3.000 que terminou positiva). Só afetava essa coluna; tudo foi rodado de novo depois da correção e os demais números não mudaram.
2. **Métrica trocada:** "margem máxima usada" como razão `notional/10/equity` explodia quando a equity ia a zero; passou a ser `max(Σ notional)/10` em $ e em % do capital inicial. Não entra em critério.
3. **Duas sensibilidades pós-hoc, fora do veredito:** (a) variante `v3b`: igual à V3, mas a ordem base é redimensionada pela equity depois de cada stop (com `B` fixo em $, cada stop de 20% deixa a escada relativamente maior e o próximo stop vem mais rápido; queria saber se o colapso da V3 era artefato do sizing fixo; não era: ela também termina em ~$0, só mais devagar, com 55 / 40 stops); (b) capital `$3000_B300`: conta de $3.000 operando a escada dimensionada para $300 (10× mais colchão), porque com `B ∝ capital` a sensibilidade pré-registrada ao capital é nula por construção. Resultados abaixo, em seção própria.

## Resultados: célula padrão (X = 1,5%, m = 2, TP = 1%), período completo, $300

"Realizado (o que o app mostra)" = soma do lucro líquido dos trades fechados com lucro, a base da taxa de 20%. "Líquido" = equity final − tudo que foi depositado (inicial + recapitalizações após liquidação).

| universo | variante | B ($) | trades | win% | realizado (app) | líquido | taxa 20% | líquido após taxa | pior flutuante | DD máx | liq | 1ª liq | stops | pior mês | nível máx | margem máx |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 31 | V1 original | 0,49 | 217.428 | 99,7% | +$37.763 | **−$12.551** | $7.553 | −$20.103 | −$11.212 (−3.737%) | 100% | 48 | 2022-02-05 (dia 35) | 0 | 2022-05 (−$8.921) | 17 | $8.458 |
| 31 | V2a N=5 | 2,02 | 54.022 | 99,7% | +$4.714 | **−$1.963** | $943 | −$2.906 | −$1.045 (−348%) | 100% | 6 | 2022-05-11 (dia 131) | 0 | 2022-05 (−$607) | 5 | $207 |
| 31 | V2b N=7 | 0,49 | 86.544 | 99,9% | +$3.024 | **−$634** | $605 | −$1.239 | −$1.088 (−363%) | 99% | 2 | 2024-02-29 (dia 789) | 0 | 2024-11 (−$425) | 7 | $177 |
| 31 | V3 N=7 + stop 20% | 0,49 | 21.318 | 98,1% | +$719 | **−$300** | $144 | −$444 | −$166 (−55%) | 100% | 0 | — | 44 | 2022-05 (−$373) | 7 | $97 |
| 3 | V1 original | 1,97 | 25.877 | 99,8% | +$7.389 | **−$5.088** | $1.478 | −$6.565 | −$2.188 (−729%) | 100% | 17 | 2022-01-21 (dia 21) | 0 | 2023-01 (−$1.976) | 13 | $1.855 |
| 3 | V2a N=5 | 8,06 | 6.134 | 99,8% | +$2.087 | **−$748** | $417 | −$1.165 | −$893 (−298%) | 99% | 3 | 2023-12-02 (dia 701) | 0 | 2024-11 (−$529) | 5 | $220 |
| 3 | V2b N=7 | 1,97 | 11.587 | 99,9% | +$1.519 | **−$590** | $304 | −$893 | −$650 (−217%) | 100% | 3 | 2023-12-28 (dia 726) | 0 | 2024-11 (−$422) | 7 | $165 |
| 3 | V3 N=7 + stop 20% | 1,97 | 6.875 | 97,7% | +$638 | **−$300** | $128 | −$427 | −$75 (−25%) | 100% | 0 | — | 73 | 2022-02 (−$125) | 7 | $67 |

Como ler o prejuízo flutuante acima de 100% do capital: a conta cresce com os TPs (na célula padrão da V1 no universo 31 a margem em uso chegou a $8.458, o que exige equity dessa ordem, tudo vindo de $300; em outras células passou de $100.000), a margem permite escadas mais fundas, e a próxima tendência leva tudo. As 48 liquidações da V1 custaram 48 × $300 = $14.400 de recapitalização para terminar com $2.149 na conta.

### Holdout 2026 (conta nova de $300 em 2026-01-01, célula padrão)

| universo | variante | líquido | liq | 1ª liq | DD máx | pior flutuante | realizado (app) |
|---|---|---|---|---|---|---|---|
| 31 | V1 | −$251 | 7 | 2026-01-01 (dia 1) | 100% | −$4.673 | +$6.859 |
| 31 | V2a | −$469 | 1 | 2026-02-13 | 100% | −$998 | +$923 |
| 31 | V2b | −$454 | 1 | 2026-02-13 | 100% | −$700 | +$564 |
| 31 | V3 | −$300 | 0 | — | 100% (72 stops) | −$93 | +$322 |
| 3 | V1 | −$888 | 3 | 2026-01-06 | 99% | −$914 | +$978 |
| 3 | V2a | −$208 | 0 | — | 80% | −$559 | +$323 |
| 3 | V2b | −$176 | 0 | — | 79% | −$391 | +$188 |
| 3 | V3 | −$97 | 0 | — | 70% (9 stops) | −$88 | +$420 |

2026 é um ano de queda/lateral. Em 9 meses, sem nenhuma liquidação em BTC/ETH/ADA com N = 7, a conta ainda termina −59% com drawdown de 79%: o LONG preso na escada cheia e o SHORT colhendo 1% de cada vez não se compensam.

## Robustez: as 12 células de cada variante

Resumo dos 30 grids (`tables.md` tem todas as linhas). "Células PASS" aplica os três critérios pré-registrados.

| universo | capital | variante | células com 0 liq | liquidações (mín–máx) | DD máx (mín–máx) | líquido completo (mín–máx) | holdout > 0 | holdout líq (mín–máx) | células PASS |
|---|---|---|---|---|---|---|---|---|---|
| 31 | $300 | V1 original | 0/12 | 29–148 | 100%–100% | −$44.235 a −$7.600 | 2/12 | −$10.335 a +$1.129 | 0/12 |
| 31 | $300 | V2a N=5 | 0/12 | 6–10 | 100%–100% | −$2.998 a −$1.778 | 0/12 | −$611 a −$469 | 0/12 |
| 31 | $300 | V2b N=7 | 0/12 | 2–8 | 99%–100% | −$2.391 a −$529 | 0/12 | −$591 a −$220 | 0/12 |
| 31 | $300 | V3 N=7 + stop 20% | 7/12 | 0–3 | 100%–100% | −$1.200 a −$300 | 0/12 | −$300 a −$131 | 0/12 |
| 3 | $300 | V1 original | 0/12 | 10–38 | 100%–100% | −$11.349 a −$2.827 | 0/12 | −$2.055 a −$9 | 0/12 |
| 3 | $300 | V2a N=5 | 0/12 | 2–5 | 99%–100% | −$1.446 a −$530 | 0/12 | −$274 a −$194 | 0/12 |
| 3 | $300 | V2b N=7 | 0/12 | 2–4 | 99%–100% | −$1.349 a −$249 | 1/12 | −$333 a +$22 | 0/12 |
| 3 | $300 | V3 N=7 + stop 20% | 10/12 | 0–2 | 100%–100% | −$897 a −$299 | 0/12 | −$299 a −$8 | 0/12 |

As linhas de $3.000 são as mesmas ×10 (mesmas liquidações, mesmos DDs, mesmas datas), porque `B ∝ capital`. As duas células de V1 com holdout positivo (+$200 e +$1.129, universo 31, X = 1,5%, m = 1,5) liquidaram 4 e 5 vezes dentro do próprio holdout: o "lucro" é o que a última conta recapitalizada fez antes de o período acabar.

### Grid completo: BTC/ETH/ADA, $300, V1 original

| X / m / TP | B ($) | líq ($) | líq % | win% | trades | pior flutuante | DD máx | liq | 1ª liq | pior mês ($) | nível máx | margem máx $ (% cap) | líq após 20% ($) | holdout líq ($) | holdout liq | holdout DD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1% / 1,5 / 0,6% | 7,77 | −7.755 | −2.585% | 99,8% | 47.175 | −1.055% | 100% | 26 | 2022-01-21 | 2023-01 (−3.141) | 18 | 2.618 (873%) | −10.076 | −2.055 | 7 | 100% |
| 1% / 1,5 / 1% | 7,77 | −11.321 | −3.774% | 99,5% | 23.581 | −1.286% | 100% | 38 | 2022-01-08 | 2023-01 (−3.582) | 18 | 2.697 (899%) | −14.950 | −1.421 | 5 | 100% |
| 1% / 2 / 0,6% | 1,97 | −6.778 | −2.259% | 99,9% | 56.469 | −1.463% | 100% | 23 | 2022-01-08 | 2024-11 (−3.985) | 14 | 3.665 (1.222%) | −8.706 | −9 | 3 | 100% |
| 1% / 2 / 1% | 1,97 | −11.349 | −3.783% | 99,7% | 28.073 | −2.604% | 100% | 38 | 2022-01-08 | 2024-05 (−7.308) | 15 | 7.244 (2.415%) | −15.708 | −1.749 | 6 | 100% |
| 1,5% / 1,5 / 0,6% | 7,77 | −3.138 | −1.046% | 99,9% | 38.059 | −448% | 100% | 15 | 2022-01-22 | 2023-01 (−1.165) | 16 | 1.166 (389%) | −4.271 | −771 | 3 | 100% |
| 1,5% / 1,5 / 1% | 7,77 | −6.273 | −2.091% | 99,7% | 20.773 | −830% | 100% | 21 | 2022-01-10 | 2024-11 (−2.128) | 17 | 2.232 (744%) | −8.186 | −875 | 3 | 99% |
| 1,5% / 2 / 0,6% | 1,97 | −2.827 | −942% | 99,9% | 47.493 | −399% | 100% | 12 | 2023-01-18 | 2023-01 (−1.085) | 12 | 916 (305%) | −3.554 | −420 | 2 | 100% |
| **1,5% / 2 / 1%** | 1,97 | −5.088 | −1.696% | 99,8% | 25.877 | −729% | 100% | 17 | 2022-01-21 | 2023-01 (−1.976) | 13 | 1.855 (618%) | −6.565 | −888 | 3 | 99% |
| 2% / 1,5 / 0,6% | 7,77 | −4.186 | −1.395% | 99,9% | 31.243 | −367% | 100% | 14 | 2022-01-24 | 2024-11 (−1.089) | 15 | 805 (268%) | −4.885 | −585 | 2 | 99% |
| 2% / 1,5 / 1% | 7,77 | −4.481 | −1.494% | 99,7% | 17.875 | −564% | 100% | 15 | 2022-01-22 | 2025-05 (−1.637) | 16 | 1.301 (434%) | −5.615 | −811 | 3 | 99% |
| 2% / 2 / 0,6% | 1,97 | −2.956 | −985% | 99,9% | 38.259 | −253% | 100% | 10 | 2022-05-12 | 2024-11 (−713) | 11 | 534 (178%) | −3.328 | −256 | 1 | 96% |
| 2% / 2 / 1% | 1,97 | −3.590 | −1.197% | 99,8% | 22.032 | −290% | 100% | 15 | 2022-05-11 | 2025-10 (−949) | 12 | 920 (307%) | −4.335 | −260 | 1 | 100% |

### Grid completo: BTC/ETH/ADA, $300, V2b (N = 7)

| X / m / TP | B ($) | líq ($) | líq % | win% | trades | pior flutuante | DD máx | liq | 1ª liq | pior mês ($) | margem máx $ (% cap) | líq após 20% ($) | holdout líq ($) | holdout liq | holdout DD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1% / 1,5 / 0,6% | 7,77 | −487 | −162% | 99,9% | 12.804 | −315% | 99% | 2 | 2023-12-09 | 2024-11 (−470) | 224 (75%) | −851 | −264 | 1 | 98% |
| 1% / 1,5 / 1% | 7,77 | −773 | −258% | 99,8% | 5.683 | −296% | 100% | 3 | 2023-11-09 | 2024-11 (−492) | 218 (73%) | −1.185 | −239 | 0 | 89% |
| 1% / 2 / 0,6% | 1,97 | −332 | −111% | 100,0% | 20.450 | −261% | 100% | 2 | 2023-12-08 | 2024-11 (−455) | 217 (72%) | −621 | −333 | 1 | 100% |
| 1% / 2 / 1% | 1,97 | −249 | −83% | 99,9% | 8.965 | −349% | 99% | 2 | 2023-11-09 | 2024-11 (−572) | 218 (73%) | −645 | −125 | 0 | 78% |
| 1,5% / 1,5 / 0,6% | 7,77 | −566 | −189% | 99,9% | 13.783 | −271% | 100% | 2 | 2023-11-09 | 2024-11 (−417) | 210 (70%) | −851 | −198 | 0 | 98% |
| 1,5% / 1,5 / 1% | 7,77 | −791 | −264% | 99,8% | 7.408 | −277% | 99% | 3 | 2023-04-14 | 2024-11 (−503) | 217 (72%) | −1.176 | −264 | 0 | 97% |
| 1,5% / 2 / 0,6% | 1,97 | −713 | −238% | 100,0% | 24.465 | −198% | 100% | 3 | 2024-01-10 | 2024-11 (−342) | 163 (54%) | −928 | −144 | 0 | 69% |
| **1,5% / 2 / 1%** | 1,97 | −590 | −197% | 99,9% | 11.587 | −217% | 100% | 3 | 2023-12-28 | 2024-11 (−422) | 165 (55%) | −893 | −176 | 0 | 79% |
| 2% / 1,5 / 0,6% | 7,77 | −725 | −242% | 99,9% | 14.422 | −239% | 99% | 3 | 2024-01-10 | 2024-11 (−360) | 201 (67%) | −970 | −165 | 0 | 72% |
| 2% / 1,5 / 1% | 7,77 | −1.349 | −450% | 99,8% | 7.836 | −253% | 99% | 4 | 2024-01-10 | 2024-11 (−484) | 172 (57%) | −1.668 | −78 | 0 | 56% |
| 2% / 2 / 0,6% | 1,97 | −1.151 | −384% | 100,0% | 22.974 | −181% | 100% | 3 | 2024-01-10 | 2024-11 (−215) | 141 (47%) | −1.301 | −42 | 0 | 40% |
| 2% / 2 / 1% | 1,97 | −720 | −240% | 99,9% | 12.168 | −207% | 100% | 3 | 2023-12-13 | 2024-11 (−340) | 148 (49%) | −949 | +22 | 0 | 37% |

Em BTC/ETH/ADA a escada de 7 níveis aguentou 2022 e 2023 (primeira liquidação entre 2023-04 e 2024-01, ~470 a 740 dias) e morreu no rali de novembro de 2024 (pior mês nas 12 células): o SHORT de ADA/BTC/ETH ficou preso na escada cheia enquanto o preço subia 50–100%. É o mesmo mecanismo da LONG na queda; a direção só muda de nome.

### Grid completo: universo 31, $300, V3 (N = 7 + stop de portfólio 20%)

| X / m / TP | B ($) | líq ($) | win% | trades | pior flutuante | DD máx | liq | stops | pior mês ($) | margem máx $ (% cap) | líq após 20% ($) | holdout líq ($) | holdout DD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1% / 1,5 / 0,6% | 1,94 | −300 | 97,4% | 17.329 | −34% | 100% | 0 | 47 | 2022-01 (−194) | 121 (40%) | −406 | −300 | 100% |
| 1% / 1,5 / 1% | 1,94 | −300 | 95,4% | 12.315 | −44% | 100% | 0 | 67 | 2022-01 (−199) | 110 (37%) | −473 | −300 | 100% |
| 1% / 2 / 0,6% | 0,49 | −300 | 99,1% | 44.225 | −31% | 100% | 0 | 38 | 2022-05 (−194) | 92 (31%) | −455 | −300 | 100% |
| 1% / 2 / 1% | 0,49 | −1.200 | 97,4% | 55.891 | −53% | 100% | 3 | 129 | 2022-05 (−743) | 115 (38%) | −1.748 | −300 | 100% |
| 1,5% / 1,5 / 0,6% | 1,94 | −300 | 98,5% | 30.678 | −33% | 100% | 0 | 42 | 2022-01 (−111) | 111 (37%) | −447 | −300 | 100% |
| 1,5% / 1,5 / 1% | 1,94 | −900 | 97,9% | 74.492 | −73% | 100% | 2 | 127 | 2022-05 (−419) | 116 (39%) | −1.799 | −300 | 100% |
| 1,5% / 2 / 0,6% | 0,49 | −300 | 99,4% | 36.777 | −50% | 100% | 0 | 31 | 2022-05 (−400) | 82 (27%) | −388 | −300 | 100% |
| **1,5% / 2 / 1%** | 0,49 | −300 | 98,1% | 21.318 | −55% | 100% | 0 | 44 | 2022-05 (−373) | 97 (32%) | −444 | −300 | 100% |
| 2% / 1,5 / 0,6% | 1,94 | −600 | 98,2% | 28.077 | −37% | 100% | 1 | 46 | 2022-05 (−519) | 81 (27%) | −721 | −300 | 100% |
| 2% / 1,5 / 1% | 1,94 | −900 | 95,9% | 20.429 | −43% | 100% | 2 | 74 | 2022-05 (−815) | 100 (33%) | −1.090 | −131 | 80% |
| 2% / 2 / 0,6% | 0,49 | −300 | 99,5% | 32.676 | −44% | 100% | 0 | 24 | 2022-01 (−291) | 64 (21%) | −357 | −300 | 100% |
| 2% / 2 / 1% | 0,49 | −1.132 | 99,1% | 154.911 | −119% | 100% | 3 | 87 | 2022-05 (−656) | 107 (36%) | −1.872 | −232 | 89% |

O stop de portfólio troca "uma liquidação" por "dezenas de perdas de 20%", e em 5 das 12 células nem isso: uma barra de 5m com gap grande pula de −19% para a liquidação. Como a escada é fixa em $, cada stop deixa a próxima escada relativamente maior e o próximo stop vem mais rápido; a `v3b` (redimensiona a escada depois do stop) corrige esse artefato e termina no mesmo lugar: −$296 a −$300 em todas as 12 células do universo 31 (0 liquidações, 33–55 stops, DD 99–100%), −$281 a −$299 em BTC/ETH/ADA.

## Sensibilidade pós-hoc: $3.000 operando a escada de $300

Isto não é o produto que o app vende (lá a escada escala com o depósito); é a pergunta "e se eu deixasse 90% do capital parado como colchão?". Fora do veredito.

| universo | variante | células com 0 liq | DD máx (mín–máx) | líquido completo (mín–máx) | célula padrão: líquido / após taxa 20% / DD | holdout > 0 |
|---|---|---|---|---|---|---|
| 31 | V1 original | 0/12 | 100% | −$208.293 a −$38.496 | −$79.831 / −$98.843 / 100% (33 liq) | 5/12 (todas com liquidação dentro do holdout) |
| 31 | V2a N=5 | 11/12 | 72%–99% | −$3.582 a −$504 | −$753 / −$1.347 / 82% | 0/12 |
| 31 | V2b N=7 | 12/12 | 47%–85% | −$950 a +$7 | +$7 / −$537 / 55% | 0/12 |
| 31 | V3 N=7 + stop 20% | 12/12 | 25%–63% | −$1.800 a −$309 | −$735 / −$1.354 / 37% | 0/12 |
| 3 | V1 original | 0/12 | 100% | −$17.933 a −$4.011 | −$4.011 / −$7.338 / 100% (3 liq) | 11/12 (DD de 10% a 84%) |
| 3 | V2a N=5 | 12/12 | 37%–48% | −$268 a +$199 | +$154 / −$126 / 39% | 0/12 |
| 3 | V2b N=7 | 12/12 | 32%–42% | −$136 a +$390 | +$390 / +$166 / 36% | 1/12 (+$22) |
| 3 | V3 N=7 + stop 20% | 12/12 | 31%–56% | −$1.638 a −$467 | −$575 / −$882 / 35% | 1/12 |

Grid completo de BTC/ETH/ADA com N = 7 e 10× de colchão, o melhor caso de toda a rodada:

| X / m / TP | B ($) | líq ($) | líq % | win% | trades | pior flutuante | DD máx | pior mês ($) | margem máx $ (% cap) | líq após 20% ($) | holdout líq ($) | holdout DD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1% / 1,5 / 0,6% | 7,77 | +197 | 7% | 100,0% | 9.204 | −61% | 35% | 2024-11 (−728) | 319 (11%) | −66 | −270 | 11% |
| 1% / 1,5 / 1% | 7,77 | +95 | 3% | 99,9% | 3.757 | −72% | 39% | 2024-11 (−773) | 350 (12%) | −179 | −229 | 9% |
| 1% / 2 / 0,6% | 1,97 | +270 | 9% | 100,0% | 15.655 | −57% | 37% | 2024-11 (−779) | 298 (10%) | +45 | −157 | 10% |
| 1% / 2 / 1% | 1,97 | +334 | 11% | 100,0% | 6.542 | −68% | 37% | 2024-11 (−791) | 333 (11%) | +45 | −117 | 9% |
| 1,5% / 1,5 / 0,6% | 7,77 | −17 | −1% | 100,0% | 10.207 | −60% | 39% | 2024-11 (−768) | 285 (10%) | −228 | −310 | 12% |
| 1,5% / 1,5 / 1% | 7,77 | −7 | −0% | 100,0% | 4.573 | −67% | 38% | 2024-11 (−781) | 310 (10%) | −243 | −251 | 10% |
| 1,5% / 2 / 0,6% | 1,97 | +114 | 4% | 100,0% | 18.586 | −55% | 42% | 2024-11 (−816) | 266 (9%) | −51 | −143 | 7% |
| **1,5% / 2 / 1%** | 1,97 | +390 | 13% | 100,0% | 8.464 | −56% | 36% | 2024-11 (−807) | 271 (9%) | +166 | −158 | 8% |
| 2% / 1,5 / 0,6% | 7,77 | +78 | 3% | 100,0% | 10.556 | −52% | 36% | 2024-11 (−674) | 256 (9%) | −103 | −164 | 8% |
| 2% / 1,5 / 1% | 7,77 | +66 | 2% | 100,0% | 4.745 | −54% | 32% | 2024-11 (−675) | 272 (9%) | −131 | −78 | 6% |
| 2% / 2 / 0,6% | 1,97 | −136 | −5% | 100,0% | 17.814 | −48% | 36% | 2024-11 (−613) | 243 (8%) | −256 | −42 | 5% |
| 2% / 2 / 1% | 1,97 | +190 | 6% | 100,0% | 8.921 | −49% | 33% | 2024-11 (−624) | 243 (8%) | +19 | +22 | 5% |

Lendo: com 90% do capital parado, a escada de 7 níveis em três pares líquidos sobrevive 4,75 anos sem liquidar, com win rate de 100,0%, e entrega **+13% no total (2,7% ao ano) com drawdown de 36%**, antes da taxa de 20% do app. Depois da taxa, +$166 em $3.000 (5,5% em 4,75 anos). BTC comprado e segurado no mesmo período fez +81% (ETH, −27%): o colchão de 90% parado em stablecoin rende mais que a escada. E no holdout de 2026 todas as 12 células perdem (−$42 a −$310), com uma exceção de +$22.

## O que o app do CoinTech2u mostra vs o que o backtest mostra

| o que aparece na tela do app | o que o backtest mede | célula padrão, BTC/ETH/ADA, $300, V1 |
|---|---|---|
| Taxa de acerto ~99% | Trades fechados com lucro ÷ trades fechados. Correto, e **por construção**: a perna só fecha quando volta ao preço médio + TP. A perna que não volta não fecha e não entra na estatística. | 99,8% (25.877 trades) |
| Lucro acumulado, sempre subindo | Soma dos trades fechados. Correto, e é a base da taxa de 20%. | +$7.389 realizados em 4,75 anos |
| Prejuízo flutuante nas pernas abertas (às vezes escondido numa aba) | Mínimo da soma do não realizado. É aqui que mora o custo da estratégia. | −$2.188 (7× o capital) |
| Saldo da conta | Equity = realizado + não realizado − depósitos. | **−$5.088**: 17 liquidações, $5.100 depositados, $12 na conta ao final |
| Taxa de 20% sobre o lucro | Cobrada sobre os trades fechados com lucro, não sobre a equity. A perda flutuante e a liquidação não abatem a taxa. | $1.478 pagos ao app enquanto o cliente perdia $5.088 |
| "Sem perdas há N dias" | Tempo até a primeira liquidação. | 21 dias (BTC/ETH/ADA), 35 dias (universo 31), 1 dia no holdout 2026 do universo 31 |
| Resultado mensal positivo | Variação da equity em mês-calendário, incluindo não realizado. Pode ser positivo em todos os meses e ainda assim a conta ter 77% de drawdown dentro do mês (V1 com colchão 10×, holdout 2026, BTC/ETH/ADA). | pior mês 2023-01: −$1.976 |

A estratégia não tem edge direcional nenhum: ela vende volatilidade com alavancagem crescente. Em mercado lateral colhe 0,6–1% por ciclo (os ~99% de acertos); em tendência, uma das pernas acumula posição exponencial contra o movimento até a margem acabar. O hedge não protege: a perna a favor fecha a cada 1% e reabre pequena, a perna contra cresce 2× por nível. 2022 (queda), novembro de 2024 (alta) e 2026 (queda) foram cada um suficiente para zerar a conta, e isso com liquidação modelada de forma otimista (preenchimento no preço do nível, sem gap; sem clearance fee).

## Ressalvas

- Simulação otimista em três pontos declarados no pré-registro: ordens limite preenchem no preço do nível mesmo em barras de gap; liquidação só pela taxa de manutenção de 0,5% sem clearance fee; sem notional mínimo. Este último é o mais relevante para $300: `B` sai em $0,49 (universo 31) ou $1,97 (3 pares), abaixo do mínimo de $5 (ou $100 em BTC) da Binance. Uma conta real de $300 não consegue rodar a escada nesse tamanho; teria de concentrar em menos pares ou usar ordens maiores, o que só piora a razão escada/capital. O cenário "$3.000 operando a escada de $300" é a forma honesta de ver o que uma conta pequena com ordens reais enfrentaria, e mesmo ele fica em +13% em 4,75 anos com 36% de drawdown.
- Fee taker (0,05%) em todas as pernas é conservador para adições e TP (seriam maker, 0,02%). Não muda nada: o resultado é dominado pelas liquidações, não pelas fees.
- 5m não resolve a ordem dos extremos dentro da barra; a convenção (adições antes, TP só na barra seguinte) atrasa alguns TPs em uma barra, nunca inventa um. A cobertura de 2022→2026 inclui o colapso de LUNA e a renomeação de MATIC como pares mortos (universo 31), o que é realista para quem opera "os 12 maiores".
- 360 células pré-registradas e 360 pós-hoc, sem seleção: todas estão em `tables.md`. Com 0 de 360 abaixo de 20% de drawdown, não há interpretação de múltiplos testes que salve a hipótese.

## O que isto diz para o bot

- Não há nada aqui para copiar: a estratégia não tem expectativa positiva, ela tem distribuição de resultados assimétrica (muitos ganhos minúsculos, perdas raras que levam a conta) e o app só mostra o lado bom.
- Se alguma ideia de DCA for revisitada no futuro, a métrica que importa é drawdown de equity com não realizado e número de liquidações, nunca a taxa de acerto de trades fechados. O pré-registro deste relatório serve de molde.
- O forward test Donchian 4h continua sendo a única linha com expectativa bruta positiva no repositório; nada deste relatório muda o que está rodando.
