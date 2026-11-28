"""
Dashboard web do bot — Flask + Flask-SocketIO rodando como thread no
processo do bot. Permite visualização em tempo real e ações interativas
(pause/resume/close_all) sem precisar do Telegram.

Por segurança, é opt-in: só sobe quando DASHBOARD_ENABLED=True e ambas
DASHBOARD_USERNAME + DASHBOARD_PASSWORD estão configuradas.

Uso típico (na inicialização do bot):

    from trading_bot.web import DashboardServer
    server = DashboardServer(bot)
    server.start()  # spawn thread; sem-op se DASHBOARD_ENABLED=False
"""

__all__ = ["DashboardServer"]


def __getattr__(name):
    # Lazy: `.server` importa o singleton `config` (montado do env no import). O worker
    # por conta (core/worker.py) precisa de `trading_bot.web.platform` ANTES de popular
    # o env, então o pacote não pode puxar o config só por ser importado.
    if name == "DashboardServer":
        from .server import DashboardServer
        return DashboardServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
