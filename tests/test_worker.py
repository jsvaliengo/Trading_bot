"""Worker por conta: env montado do platform.db, abortos e modo DRY (nunca importa o bot de verdade)."""

import os
import sys
import types

import pytest

from trading_bot.core import worker
from trading_bot.web.platform import PlatformStore

KEY, SECRET, PASS = "key-abcd-1234", "secret-efgh-5678", "pass-ijkl-9012"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """platform.db temporário apontado por env; env do processo limpo das variáveis que o worker força."""
    for name in ("TRADING_BOT_ENV_FILE", "TRADING_BOT_PLATFORM_SECRET", "TRADING_BOT_DASHBOARD_SECRET_KEY",
                 "TRADING_BOT_ACCOUNT_DRY", "TRADING_BOT_ACCOUNT_ID", *worker.SECRET_KEYS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRADING_BOT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRADING_BOT_PLATFORM_DB_PATH", str(tmp_path / "platform.db"))
    monkeypatch.setenv("TRADING_BOT_PLATFORM_SECRET", "x")
    monkeypatch.setattr(worker, "_load_env_file", lambda: None)   # nunca lê o .env real nos testes
    s = PlatformStore(str(tmp_path / "platform.db"), secret="x")
    s.create_user("admin", "senha")   # id 1
    s.create_user("maria", "senha")   # id 2
    s.create_account(1, "bitget", label="conta 1 (.env)")   # id 1: a do dono, fica fora do worker
    return s


@pytest.fixture
def fake_bot(monkeypatch):
    """Substitui trading_bot.core.bot por um stub que registra a chamada de main()."""
    calls = []
    stub = types.ModuleType("trading_bot.core.bot")
    stub.main = lambda: calls.append("main")
    monkeypatch.setitem(sys.modules, "trading_bot.core.bot", stub)
    return calls


def _inactive_account(store):
    acc = _prod_account(store)
    store.set_account_active(acc, False)
    return acc


def _prod_account(store, **kw):
    params = dict(exchange="bitget", label="maria", api_key=KEY, api_secret=SECRET, passphrase=PASS,
                  environment="prod")
    params.update(kw)
    return store.create_account(2, **params)


def test_main_populates_env_and_calls_bot(store, fake_bot, tmp_path, monkeypatch):
    acc = _prod_account(store)
    assert worker.main([str(acc)]) == 0
    assert fake_bot == ["main"]
    runtime_dir = tmp_path / "runtime" / "accounts" / str(acc)
    assert runtime_dir.is_dir()
    assert os.environ["TRADING_BOT_RUNTIME_DIR"] == str(runtime_dir)
    assert (os.environ["BITGET_API_KEY"], os.environ["BITGET_API_SECRET"], os.environ["BITGET_PASSPHRASE"]) == (KEY, SECRET, PASS)
    assert os.environ["BITGET_DEMO"] == "false"
    assert os.environ["TRADING_BOT_EXCHANGE"] == "bitget"
    assert os.environ["TRADING_BOT_TELEGRAM_ENABLED"] == "false"
    assert os.environ["TELEGRAM_TOKEN"] == "" and os.environ["TELEGRAM_CHAT_ID"] == ""
    assert os.environ["TRADING_BOT_DAILY_REPORT_ENABLED"] == "false"
    assert os.environ["TRADING_BOT_DASHBOARD_ENABLED"] == "false"
    assert os.environ["TRADING_BOT_METRICS_ENABLED"] == "false"
    assert os.environ["TRADING_BOT_START_PAUSED"] == "true"
    assert os.environ["TRADING_BOT_CONTROL_FILE_ENABLED"] == "true"


def test_account_id_from_env_and_demo_account(store, fake_bot, monkeypatch):
    acc = _prod_account(store, environment="demo")
    monkeypatch.setenv("TRADING_BOT_ACCOUNT_ID", str(acc))
    assert worker.main([]) == 0
    assert os.environ["BITGET_DEMO"] == "true" and fake_bot == ["main"]


def test_dry_run_prints_masked_env_without_importing_bot(store, monkeypatch, capsys):
    acc = _prod_account(store)
    monkeypatch.setenv("TRADING_BOT_ACCOUNT_DRY", "1")
    monkeypatch.delitem(sys.modules, "trading_bot.core.bot", raising=False)
    assert worker.main([str(acc)]) == 0
    out = capsys.readouterr().out
    assert "trading_bot.core.bot" not in sys.modules
    for full in (KEY, SECRET, PASS):
        assert full not in out
    assert "BITGET_API_KEY=***1234" in out and "BITGET_API_SECRET=***5678" in out and "BITGET_PASSPHRASE=***9012" in out
    assert "BITGET_DEMO=false" in out and "TRADING_BOT_START_PAUSED=true" in out


@pytest.mark.parametrize("setup, expected", [
    (lambda s: 1, "conta 1"),
    (lambda s: 99, "não existe"),
    (_inactive_account, "inativa"),
    (lambda s: _prod_account(s, api_secret=""), "sem chaves"),
    (lambda s: _prod_account(s, exchange="binance"), "só suporta bitget"),
])
def test_aborts_without_touching_bot(store, fake_bot, setup, expected):
    account_id = setup(store)
    with pytest.raises(SystemExit) as exc:
        worker.main([str(account_id)])
    assert expected in str(exc.value)
    assert fake_bot == []


def test_abort_message_never_contains_secret(store, fake_bot):
    acc = _prod_account(store, passphrase="")
    with pytest.raises(SystemExit) as exc:
        worker.main([str(acc)])
    assert KEY not in str(exc.value) and SECRET not in str(exc.value)


def test_bad_argument(store, fake_bot):
    with pytest.raises(SystemExit) as exc:
        worker.main(["abc"])
    assert "uso:" in str(exc.value) and fake_bot == []


def test_web_package_still_exposes_dashboard_server_lazily():
    # bot.py:5643 faz `from ..web import DashboardServer`; o __init__ lazy não pode quebrar isso
    import trading_bot.web as web
    from trading_bot.web import DashboardServer
    assert DashboardServer.__name__ == "DashboardServer"
    with pytest.raises(AttributeError):
        web.nao_existe
