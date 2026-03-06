#!/usr/bin/env python3
"""Sync Trading Bot operational docs to Apple Notes."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.core.config import config  # noqa: E402


def on_off(value: bool) -> str:
    return "ON" if bool(value) else "OFF"


def mainnet_testnet() -> str:
    return "TESTNET" if bool(config.USE_TESTNET) else "MAINNET"


def update_note(title: str, body_html: str, folder_name: str) -> str:
    """Create or update a note in Apple Notes."""
    script = r"""
on run argv
  set noteTitle to item 1 of argv
  set noteBody to item 2 of argv
  set folderName to item 3 of argv

  tell application "Notes"
    set targetAccount to missing value
    repeat with acc in accounts
      if (name of acc as text) contains "iCloud" then
        set targetAccount to acc
        exit repeat
      end if
    end repeat

    if targetAccount is missing value then
      set targetAccount to first account
    end if

    try
      set targetFolder to folder folderName of targetAccount
    on error
      set targetFolder to make new folder at targetAccount with properties {name:folderName}
    end try

    set existingNotes to (every note of targetFolder whose name is noteTitle)
    if (count of existingNotes) > 0 then
      set body of item 1 of existingNotes to noteBody
      return "updated:" & noteTitle
    else
      make new note at targetFolder with properties {name:noteTitle, body:noteBody}
      return "created:" & noteTitle
    end if
  end tell
end run
"""
    result = subprocess.run(
        ["osascript", "-", title, body_html, folder_name],
        input=script,
        text=True,
        check=True,
        capture_output=True,
    )
    return result.stdout.strip()


def build_notes() -> list[tuple[str, str]]:
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")

    map_body = f"""
<h1>Mapa de Estratégias do Trading Bot (Snapshot)</h1>
<p><b>Atualizado em:</b> {now_local}</p>
<p><b>Ambiente:</b> {mainnet_testnet()}</p>
<p><b>Estrategia principal:</b> USE_BINANCE_STRATEGY={config.USE_BINANCE_STRATEGY}</p>

<h2>Ativas agora</h2>
<ul>
<li>Hedge LONG/SHORT - HEDGE_RATIO={config.HEDGE_RATIO}</li>
<li>DCA - DCA_ENABLED={config.DCA_ENABLED}</li>
<li>Take Profit - {config.TAKE_PROFIT_PERCENT:.2f}%</li>
<li>Trailing Stop - {config.TRAILING_ACTIVATION_PERCENT:.2f} / {config.TRAILING_DISTANCE_PERCENT:.2f}% (min USD {config.TRAILING_MIN_PROFIT_USD:.2f})</li>
<li>Funding-aware - CHECK_FUNDING_RATE={config.CHECK_FUNDING_RATE}</li>
<li>Stop Loss Global - {config.GLOBAL_STOP_LOSS_PERCENT:.2f}%</li>
<li>Deteccao de deposito/saque - CAPITAL_TRANSFER_DETECTION_ENABLED={config.CAPITAL_TRANSFER_DETECTION_ENABLED}</li>
<li>Relatorio diario Telegram - {on_off(config.DAILY_PERFORMANCE_REPORT_ENABLED)} ({int(config.DAILY_PERFORMANCE_REPORT_HOUR_BRT):02d}:{int(config.DAILY_PERFORMANCE_REPORT_MINUTE_BRT):02d} BRT / janela {int(config.DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS)}h)</li>
<li>Dashboard web - {on_off(True)} ({config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}, refresh {config.DASHBOARD_REFRESH_SECONDS}s)</li>
</ul>

<h2>Desativadas agora</h2>
<ul>
<li>Stop Loss individual - USE_INDIVIDUAL_STOP_LOSS={config.USE_INDIVIDUAL_STOP_LOSS}</li>
<li>Filtro de sentimento - USE_MARKET_SENTIMENT_FILTER={config.USE_MARKET_SENTIMENT_FILTER}</li>
<li>Double First LONG - {config.DOUBLE_FIRST_LONG_ENABLED}</li>
<li>Double First SHORT - {config.DOUBLE_FIRST_SHORT_ENABLED}</li>
<li>Metas diarias - USE_DAILY_TARGETS={config.USE_DAILY_TARGETS}</li>
</ul>

<h2>Resumo executivo</h2>
<p>Rodando em <b>{mainnet_testnet()}</b> com Hedge + DCA + TP + Trailing + Funding + protecao global. Filtros experimentais (Sentimento/Double First) seguem desligados.</p>
"""

    checklist_body = f"""
<h1>Checklist Operacional - Estratégias (Ativo vs Impacto)</h1>
<p><b>Atualizado em:</b> {now_local}</p>
<p><b>Ambiente:</b> {mainnet_testnet()}</p>

<h2>Tabela rapida</h2>
<ul>
<li><b>USE_BINANCE_STRATEGY:</b> {on_off(config.USE_BINANCE_STRATEGY)} - estrategia por faixa de capital</li>
<li><b>Hedge:</b> ON - reduz vies direcional</li>
<li><b>DCA:</b> {on_off(config.DCA_ENABLED)} - media em movimento contra</li>
<li><b>TP:</b> ON - {config.TAKE_PROFIT_PERCENT:.2f}%</li>
<li><b>Trailing:</b> {on_off(config.USE_TRAILING_STOP)} - ativ {config.TRAILING_ACTIVATION_PERCENT:.2f} / dist {config.TRAILING_DISTANCE_PERCENT:.2f}%</li>
<li><b>Funding-aware:</b> {on_off(config.CHECK_FUNDING_RATE)}</li>
<li><b>SL global:</b> ON - {config.GLOBAL_STOP_LOSS_PERCENT:.2f}%</li>
<li><b>SL individual:</b> {on_off(config.USE_INDIVIDUAL_STOP_LOSS)}</li>
<li><b>Sentimento:</b> {on_off(config.USE_MARKET_SENTIMENT_FILTER)}</li>
<li><b>Double First:</b> LONG {on_off(config.DOUBLE_FIRST_LONG_ENABLED)} | SHORT {on_off(config.DOUBLE_FIRST_SHORT_ENABLED)}</li>
<li><b>Relatorio diario:</b> {on_off(config.DAILY_PERFORMANCE_REPORT_ENABLED)}</li>
<li><b>Dashboard:</b> ON</li>
</ul>

<h2>Checklist diario</h2>
<ul>
<li>[ ] Confirmar bot e dashboard ativos</li>
<li>[ ] Validar modo (Mainnet/Testnet)</li>
<li>[ ] Conferir /status e posicoes abertas</li>
<li>[ ] Conferir /apihealth e retries/falhas</li>
<li>[ ] Revisar /config (risco)</li>
<li>[ ] Validar /dailyreport</li>
<li>[ ] Antes de deploy: pytest -q</li>
<li>[ ] Pos deploy: validar runtime/deploy_info.json</li>
</ul>

<h2>Comandos Telegram uteis</h2>
<p>/status, /config, /portfolio, /trades, /apihealth, /dailyreport now, /sentiment status, /sentiment on|off, /stop, /stop force</p>
"""

    # Semaforo rapido baseado em regras simples.
    yellow_items = 0
    if not config.USE_TESTNET:
        yellow_items += 1
    if not config.USE_INDIVIDUAL_STOP_LOSS:
        yellow_items += 1
    if float(config.GLOBAL_STOP_LOSS_PERCENT) >= 80.0:
        yellow_items += 1

    if yellow_items == 0:
        decision = "GO"
    elif yellow_items <= 2:
        decision = "GO com cuidado"
    else:
        decision = "GO com atencao reforcada"

    semaforo_body = f"""
<h1>Painel Semáforo - Trading Bot (Go/No-Go)</h1>
<p><b>Atualizado em:</b> {now_local}</p>
<p><b>Ambiente:</b> {mainnet_testnet()}</p>

<h2>Regra de decisao</h2>
<ul>
<li><b>GO:</b> nenhum item vermelho e no maximo 2 amarelos</li>
<li><b>GO com cuidado:</b> 3-4 amarelos, sem vermelho</li>
<li><b>NO-GO:</b> qualquer vermelho</li>
</ul>

<h2>Semaforo atual</h2>
<ul>
<li>CI/Testes: 🟢 (criterio: validar pytest antes de deploy)</li>
<li>Ambiente: {"🟢" if config.USE_TESTNET else "🟡"} ({mainnet_testnet()})</li>
<li>Risco por posicao (Trailing/TP): {"🟢" if config.USE_TRAILING_STOP else "🟡"}</li>
<li>SL individual: {"🟢" if config.USE_INDIVIDUAL_STOP_LOSS else "🟡"}</li>
<li>SL global ({config.GLOBAL_STOP_LOSS_PERCENT:.2f}%): {"🟢" if float(config.GLOBAL_STOP_LOSS_PERCENT) < 80.0 else "🟡"}</li>
<li>Sentimento: {"🟢" if not config.USE_MARKET_SENTIMENT_FILTER else "🟡"} ({on_off(config.USE_MARKET_SENTIMENT_FILTER)})</li>
<li>Double First: {"🟢" if (not config.DOUBLE_FIRST_LONG_ENABLED and not config.DOUBLE_FIRST_SHORT_ENABLED) else "🟡"}</li>
<li>Deploy com backup/rollback: 🟢 (workflow habilitado)</li>
<li>Dashboard ativo em modo seguro: 🟢</li>
</ul>

<h2>Decisao atual</h2>
<p><b>{decision}</b> (itens amarelos: {yellow_items})</p>

<h2>Checklist 60s pre-deploy</h2>
<ul>
<li>[ ] pytest -q passou</li>
<li>[ ] /status e /apihealth sem critico</li>
<li>[ ] Confirmar Mainnet/Testnet</li>
<li>[ ] Confirmar risco em /config</li>
<li>[ ] Dashboard ativo apos deploy</li>
<li>[ ] Rollback pronto (workflow/manual)</li>
</ul>
"""

    return [
        ("Mapa de Estratégias do Trading Bot (Snapshot)", map_body),
        ("Checklist Operacional - Estratégias (Ativo vs Impacto)", checklist_body),
        ("Painel Semáforo - Trading Bot (Go/No-Go)", semaforo_body),
    ]


def run() -> int:
    folder_name = os.getenv("TRADING_BOT_NOTES_FOLDER", "Trading Bot")
    results: list[str] = []
    for title, body in build_notes():
        result = update_note(title=title, body_html=body, folder_name=folder_name)
        results.append(result)
    print("\n".join(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
