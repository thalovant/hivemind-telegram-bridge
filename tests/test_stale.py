"""A question asked while the bridge was down is not answered minutes later.

Telegram keeps a bot's updates for up to 24 hours and hands them all over
on the first getUpdates. Those older than ``max_message_age`` (120 s) are
read and confirmed, so Telegram never sends them again, and not answered.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from hivemind_telegram_bridge import MAX_MESSAGE_AGE, HiveMindTelegramBridge

from conftest import run_in_thread, text_update, wait_for


def test_the_default_age_is_two_minutes():
    assert MAX_MESSAGE_AGE == 120


def test_old_pending_updates_are_confirmed_and_not_answered(telegram_api, hub):
    now = time.time()
    telegram_api.push(
        text_update(10, "asked an hour ago", chat_id=1, date=now - 3600),
        text_update(11, "asked three minutes ago", chat_id=2, date=now - 180),
        text_update(12, "asked just before the start", chat_id=3, date=now - 30),
    )
    bridge = HiveMindTelegramBridge(token="1:T", client=hub)
    outcome = run_in_thread(bridge)
    try:
        assert wait_for(lambda: hub.emitted, timeout=10)
        # the next poll confirms all three
        assert wait_for(lambda: any(params.get("offset") == 13 for _, _, params
                                    in telegram_api.calls_to("getUpdates")), timeout=10)
        time.sleep(0.3)
    finally:
        bridge.stop()
        assert wait_for(lambda: outcome.get("done"), timeout=10)
    assert [message.data["utterances"] for _, message in hub.emitted] == [
        ["asked just before the start"]]
    # never handed over twice
    assert telegram_api.served.keys() == {10, 11, 12}
    assert not telegram_api._pending


def _update(age_seconds):
    message = MagicMock()
    message.text = "hello"
    message.date = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    upd = MagicMock()
    upd.effective_message = message
    upd.effective_user.is_bot = False
    upd.effective_chat.id = 42
    upd.effective_chat.type = "private"
    return upd


def _bridge(**kwargs):
    client = MagicMock()
    app = MagicMock()
    app.bot.id = 999
    app.bot.username = "HiveBot"
    app.bot.send_message = AsyncMock()
    made = HiveMindTelegramBridge(client=client, app=app, **kwargs)
    made.connect_hivemind()
    return made, client


def test_a_fresh_message_is_answered_and_an_old_one_is_not():
    made, client = _bridge()
    asyncio.run(made.handle_telegram_message(_update(5), MagicMock()))
    assert client.emit.call_count == 1
    asyncio.run(made.handle_telegram_message(_update(121), MagicMock()))
    assert client.emit.call_count == 1


def test_the_age_can_be_changed():
    made, client = _bridge(max_message_age=600)
    asyncio.run(made.handle_telegram_message(_update(300), MagicMock()))
    assert client.emit.call_count == 1
