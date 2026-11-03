"""The "grant recognizer_loop:utterance" hint is logged only when the hub refuses one.

It used to be a WARNING at every start, which is false on a managed hub
(the platform grants the type) and misleads whoever reads the log. The hub
says when it refuses a message type: ``hive.policy.denied`` with code
``acl_disallowed_type``. The hint is logged then, once.
"""
from unittest.mock import MagicMock

import pytest

import hivemind_telegram_bridge as bridge_module
from hivemind_telegram_bridge import HiveMindTelegramBridge

from conftest import FakeHub, run_in_thread, wait_for


@pytest.fixture
def logged(monkeypatch):
    lines = []
    log = MagicMock()
    for level in ("debug", "info", "warning", "error", "exception"):
        getattr(log, level).side_effect = (
            lambda *a, _level=level, **k: lines.append((_level, " ".join(map(str, a)))))
    monkeypatch.setattr(bridge_module, "LOG", log)
    return lines


def _hints(lines):
    return [(level, text) for level, text in lines if "allow-msg" in text]


def test_starting_says_nothing_about_hub_permissions(telegram_api, logged):
    hub = FakeHub()
    bridge = HiveMindTelegramBridge(token="1:T", client=hub)
    outcome = run_in_thread(bridge)
    try:
        assert wait_for(lambda: telegram_api.calls_to("getUpdates"), timeout=10)
    finally:
        bridge.stop()
        assert wait_for(lambda: outcome.get("done"), timeout=10)
        hub.close()
    assert logged, "the bridge logged nothing; the test would prove nothing"
    assert _hints(logged) == []


def _refuse(code):
    return {"denied_type": "recognizer_loop:utterance", "code": code}


def test_a_refused_message_type_names_the_grant_once(logged):
    hub = FakeHub()
    bridge = HiveMindTelegramBridge(client=hub, app=MagicMock())
    bridge.connect_hivemind()
    try:
        for _ in range(3):
            hub.send("hive.policy.denied", _refuse("acl_disallowed_type"), {})
        assert wait_for(lambda: len(hub.held) == 3)
    finally:
        hub.close()
    hints = _hints(logged)
    assert len(hints) == 1
    level, text = hints[0]
    assert level == "warning"
    assert "hivemind-core allow-msg recognizer_loop:utterance" in text


def test_other_refusals_do_not_mention_the_grant(logged):
    hub = FakeHub()
    bridge = HiveMindTelegramBridge(client=hub, app=MagicMock())
    bridge.connect_hivemind()
    try:
        hub.send("hive.policy.denied", _refuse("intent_quota_exceeded"), {})
        assert wait_for(lambda: len(hub.held) == 1)
    finally:
        hub.close()
    assert _hints(logged) == []
    assert any("intent_quota_exceeded" in text for _, text in logged)
