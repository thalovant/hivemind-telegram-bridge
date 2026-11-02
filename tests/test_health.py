"""Liveness and readiness, as a container platform asks for them."""
import threading
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, MagicMock

import pytest

from hivemind_telegram_bridge.health import HealthServer


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def served():
    state = {"ready": False}
    server = HealthServer(0, lambda: state["ready"], host="127.0.0.1")
    server.start()
    yield server, state
    server.stop()


def test_liveness_answers_while_not_ready(served):
    server, _ = served
    assert get(server.port, "/healthz") == 200
    assert get(server.port, "/readyz") == 503


def test_readiness_follows_the_bridge(served):
    server, state = served
    state["ready"] = True
    assert get(server.port, "/readyz") == 200
    state["ready"] = False
    assert get(server.port, "/readyz") == 503


def test_a_failing_check_is_not_ready_not_a_crash():
    def broken():
        raise RuntimeError("boom")

    server = HealthServer(0, broken, host="127.0.0.1")
    server.start()
    try:
        assert get(server.port, "/readyz") == 503
        assert get(server.port, "/healthz") == 200
    finally:
        server.stop()


def test_anything_else_is_not_found(served):
    server, _ = served
    assert get(server.port, "/") == 404


def make_bridge():
    from hivemind_telegram_bridge import HiveMindTelegramBridge

    client = MagicMock()
    client.handshake_event = threading.Event()
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    app.updater.running = True
    return HiveMindTelegramBridge(client=client, app=app), client, app


def test_the_bridge_is_ready_only_with_a_live_hub_session_and_polling():
    bridge, client, app = make_bridge()
    assert not bridge.ready()
    bridge.connect_hivemind()
    client.handshake_event.set()
    assert not bridge.ready(), "not polling Telegram yet"
    bridge._polling.set()
    assert bridge.ready()
    client.handshake_event.clear()  # the hub connection dropped
    assert not bridge.ready()
    client.handshake_event.set()
    app.updater.running = False  # the updater died
    assert not bridge.ready()
