"""Who shares a conversation on the hub.

In a group with one session per chat, Bob's "and tomorrow?" continues
Alice's question about the weather. ``session_scope="person"`` gives every
person their own conversation in each chat; ``"chat"`` keeps the room as one.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def bridge(**kwargs):
    from hivemind_telegram_bridge import HiveMindTelegramBridge

    client = MagicMock(name="HiveMessageBusClient")
    app = MagicMock(name="Application")
    app.bot.id = 999
    app.bot.username = "HiveBot"
    app.bot.send_message = AsyncMock()
    made = HiveMindTelegramBridge(client=client, app=app, **kwargs)
    made.connect_hivemind()
    return made, client


def session_of(made, client, user_id, chat_id=-100):
    upd = MagicMock()
    upd.effective_message.text = "@HiveBot what is the weather"
    upd.effective_message.reply_to_message = None
    upd.effective_user.id = user_id
    upd.effective_user.username = f"user{user_id}"
    upd.effective_user.is_bot = False
    upd.effective_chat.id = chat_id
    upd.effective_chat.type = "group"
    client.emit.reset_mock()
    asyncio.run(made.handle_telegram_message(upd, MagicMock()))
    return client.emit.call_args[0][0].payload.context["session"]["session_id"]


def test_by_default_a_group_is_one_conversation():
    made, client = bridge()
    assert session_of(made, client, 1) == session_of(made, client, 2) == "telegram--100"


def test_per_person_sessions_keep_each_person_apart():
    made, client = bridge(session_scope="person")
    alice, bob = session_of(made, client, 1), session_of(made, client, 2)
    assert alice != bob
    assert alice == "telegram--100-1"
    # the same person in another chat is another conversation
    assert session_of(made, client, 1, chat_id=-200) != alice


def test_an_unknown_scope_is_refused():
    with pytest.raises(ValueError):
        bridge(session_scope="room")
