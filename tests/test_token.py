"""A token Telegram refuses stops the bridge with ``token_rejected``.

The platform reads the termination log to tell "replace the token" apart
from a crash worth restarting. 401 Unauthorized (wrong or revoked token) is
a rejected token whenever it comes, at start or while polling; a malformed
token (404 on getMe) is one too. 409 Conflict, another process polling with
the same token, is not.
"""
import logging

import pytest

import hivemind_telegram_bridge as bridge_module
from hivemind_telegram_bridge import HiveMindTelegramBridge, TokenRejected

from conftest import run_in_thread, wait_for

TOKEN = "123456:SECRET-token-value"


def make(hub):
    return HiveMindTelegramBridge(token=TOKEN, client=hub)


def test_a_token_refused_at_start_stops_the_bridge(telegram_api, hub, caplog):
    telegram_api.status["getMe"] = 401
    bridge = make(hub)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TokenRejected) as raised:
            bridge.start()
    # python-telegram-bot's own exception text holds the token; ours does not
    assert TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert TOKEN not in caplog.text
    assert bridge._started is False


def test_a_malformed_token_is_a_rejected_token(telegram_api, hub):
    telegram_api.status["getMe"] = 404
    with pytest.raises(TokenRejected):
        make(hub).start()


def test_a_token_revoked_while_polling_stops_the_bridge(telegram_api, hub):
    bridge = make(hub)
    outcome = run_in_thread(bridge)
    assert wait_for(lambda: len(telegram_api.calls_to("getUpdates")) >= 2)
    assert not outcome.get("done")
    telegram_api.status["getUpdates"] = 401
    assert wait_for(lambda: outcome.get("done"), timeout=10), "the bridge kept running"
    assert isinstance(outcome.get("error"), TokenRejected)
    bridge.stop()  # after the loop ended: a no-op, not an error


def test_a_conflict_is_not_a_rejected_token(telegram_api, hub):
    telegram_api.status["getUpdates"] = 409
    bridge = make(hub)
    outcome = run_in_thread(bridge)
    assert wait_for(lambda: len(telegram_api.calls_to("getUpdates")) >= 2, timeout=10)
    assert not outcome.get("done"), "a 409 stopped the bridge"
    bridge.stop()
    assert wait_for(lambda: outcome.get("done"), timeout=10)
    assert "error" not in outcome


def test_main_writes_token_rejected_and_exits_non_zero(monkeypatch, tmp_path):
    from hivemind_telegram_bridge import __main__ as cli

    log = tmp_path / "termination-log"
    monkeypatch.setattr(bridge_module, "TERMINATION_LOG", str(log))

    def refused(**_):
        raise TokenRejected("Telegram rejected the bot token")

    monkeypatch.setattr(cli, "connect_telegram_to_hivemind", refused)
    with pytest.raises(SystemExit) as exited:
        cli.main(["--token", "x"])
    assert exited.value.code == bridge_module.EXIT_TOKEN_REJECTED != 0
    assert log.read_text() == "token_rejected"


def test_an_unwritable_termination_log_is_ignored(tmp_path):
    bridge_module.report_token_rejected(str(tmp_path / "missing-dir" / "log"))


def test_the_http_client_never_logs_the_token_url():
    # httpx logs each request URL at INFO; a Bot API URL holds the token
    for name in ("httpx", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
