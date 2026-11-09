#!/usr/bin/env bash
# Liga o acesso público ao dashboard: troca o Caddyfile "em breve" (503) pelo final
# (reverse_proxy 127.0.0.1:8080). Rodar NA VM, dentro de ~/trading_bot, após deploy do login.
# Para fechar de novo: ver docs/DASHBOARD_PUBLIC_ACCESS.md.
set -euo pipefail
cd "$(dirname "$0")"
sudo cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d_%H%M%S)"
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
echo "OK: acesso publico ligado. Teste: curl -I https://trader.adityve.com.br"
