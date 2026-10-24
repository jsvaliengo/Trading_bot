"""Mascara segredos em registros de log (mensagem, args e traceback).

As exceções do requests trazem a URL inteira: `api.telegram.org/bot<token>/getUpdates`
e, na Binance, a query assinada (`signature=`) ou o listenKey do user stream.
"""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),
    (re.compile(r"(?i)\b(signature|listenKey|api_?key|apiSecret|secret|token)=[^&\s'\"]+"), r"\1=<redacted>"),
    (re.compile(r"(?i)(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]+"), r"\1<redacted>"),
    (re.compile(r"(/ws/)[A-Za-z0-9]{40,}"), r"\1<listenKey>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Filtro de handler: reescreve o registro já formatado com os segredos mascarados."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:
            pass  # nunca derrubar um log por causa do filtro
        return True


def install_on_root_handlers() -> None:
    """Filtro de logger não alcança registros propagados de loggers filhos; filtro de
    handler alcança tudo que o handler escreve, então instala em cada handler do root."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
            handler.addFilter(RedactSecretsFilter())
