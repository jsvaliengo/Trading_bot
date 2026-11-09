# Dashboard público em https://trader.adityve.com.br

Estado em 2026-11-09: infra pronta e **fechada** (Caddy responde `503 em breve`).
O acesso público só deve ser ligado depois que a tela de login (PR #256) estiver deployada na VM.

## O que foi feito (VM `oci-bot`, 147.15.92.44)

| Camada | Estado |
|---|---|
| DNS | `trader.adityve.com.br` A → 147.15.92.44 (já existia) |
| OCI Security List | `adityve-crm-sl` (subnet `adityve-crm-public`) já tinha ingress 0.0.0.0/0 TCP 80 e 443; nada foi criado |
| iptables (VM) | `ACCEPT tcp dport 80` e `443` (`-m state --state NEW`) inseridas antes do `REJECT`; persistidas em `/etc/iptables/rules.v4` via `netfilter-persistent save` |
| Caddy | v2.11.9 pelo repo oficial (apt, cloudsmith); `systemctl enable --now caddy`; `/etc/caddy/Caddyfile` em modo fechado (`respond "em breve" 503`, `reverse_proxy` comentado) |
| Certificado | Let's Encrypt (http-01), emitido e renovado automaticamente pelo Caddy; `/var/lib/caddy/.local/share/caddy/` |
| Dashboard | continua em `127.0.0.1:8080` (Basic Auth); porta 8080 não está exposta |

Regras iptables resultantes:

```
-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT
-A INPUT -j REJECT --reject-with icmp-host-prohibited
```

## Ligar o acesso público (depois do login deployado)

1. No `.env` da VM, setar e reiniciar o bot:
   ```
   TRADING_BOT_DASHBOARD_HTTPS=true          # cookie de sessão Secure
   TRADING_BOT_DASHBOARD_TRUST_PROXY=true    # rate limit do login usa X-Forwarded-For (Caddy)
   TRADING_BOT_DASHBOARD_SECRET_KEY=<64 hex aleatórios, ex.: python3 -c "import secrets;print(secrets.token_hex(32))">
   ```
2. Trocar a senha do dashboard (`TRADING_BOT_DASHBOARD_PASSWORD`): ela só circulou por túnel SSH até aqui, mas passa a ser alvo público. Use senha longa gerada; rotacione se vazar e a cada troca de chave.
3. Na VM: `cd ~/trading_bot && scripts/caddy/enable_public.sh` (copia `scripts/caddy/Caddyfile` para `/etc/caddy/Caddyfile`, `caddy validate`, `systemctl reload caddy`).
4. Testar: `curl -I https://trader.adityve.com.br` deve devolver a tela de login (200/302), não 503.

## Fechar de novo (manter TLS, sem expor o dashboard)

Editar `/etc/caddy/Caddyfile`: comentar `reverse_proxy 127.0.0.1:8080`, adicionar `respond "em breve" 503`, depois
`sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
Os backups `/etc/caddy/Caddyfile.bak.*` gerados pelo `enable_public.sh` têm a versão fechada.

## Voltar atrás por completo

```bash
sudo systemctl disable --now caddy
sudo iptables -D INPUT -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT
sudo iptables -D INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
# opcional: sudo apt-get remove caddy
```
As regras 80/443 da security list OCI são compartilhadas com o CRM (mesma subnet) e não devem ser removidas.
O acesso por túnel SSH (`ssh -L 8080:127.0.0.1:8080 oci-bot`) continua funcionando em qualquer cenário.
