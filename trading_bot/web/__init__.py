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

from .server import DashboardServer

__all__ = ["DashboardServer"]
