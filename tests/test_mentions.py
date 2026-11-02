"""In a group the bot answers only what is addressed to it, and never sees its own name.

A question in a group reads "@botname what time is it". The mention is how
the person addresses the bot, not part of the question, and an intent
matcher that receives it may match nothing. And with Telegram's privacy
mode off, a bot receives every message in the group; forwarding them all
would ask the hub about conversations that were never meant for it.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock


def bridge_with_hub(**kwargs):
    from hivemind_telegram_bridge import HiveMindTelegramBridge

    client = MagicMock(name="HiveMessageBusClient")
    app = MagicMock(name="Application")
    app.bot = MagicMock(name="Bot")
    app.bot.id = 999
    app.bot.username = "HiveBot"
    app.bot.send_message = AsyncMock()
    bridge = HiveMindTelegramBridge(client=client, app=app, **kwargs)
    bridge.connect_hivemind()
    return bridge, client


def update(text, chat_type="private", reply_to_bot=False):
    message = MagicMock()
    message.text = text
    if reply_to_bot:
        message.reply_to_message.from_user.id = 999
    else:
        message.reply_to_message = None
    upd = MagicMock()
    upd.effective_message = message
    upd.effective_user.id = 1
    upd.effective_user.username = "alice"
    upd.effective_user.is_bot = False
    upd.effective_chat.id = -100
    upd.effective_chat.type = chat_type
    return upd


def forwarded(bridge, client, upd):
    client.emit.reset_mock()
    asyncio.run(bridge.handle_telegram_message(upd, MagicMock()))
    if not client.emit.called:
        return None
    return client.emit.call_args[0][0].payload.data["utterances"][0]


def test_the_mention_is_removed_from_the_question():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("@HiveBot what time is it", "group")) == "what time is it"


def test_the_mention_is_matched_whatever_its_case_and_position():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("what time is it @hivebot?", "supergroup")) == "what time is it ?"


def test_a_group_message_not_addressed_to_the_bot_is_not_forwarded():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("lunch at noon?", "group")) is None


def test_a_reply_to_the_bot_is_addressed_to_it():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("and tomorrow?", "group", reply_to_bot=True)) == "and tomorrow?"


def test_another_bots_name_is_not_a_mention():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("@HiveBotFan hello", "group")) is None
    assert forwarded(bridge, client, update("mail me@HiveBot.example", "group")) is None


def test_a_private_chat_needs_no_mention():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("what time is it")) == "what time is it"


def test_a_message_that_is_only_the_mention_is_not_forwarded():
    bridge, client = bridge_with_hub()
    assert forwarded(bridge, client, update("@HiveBot", "group")) is None


def test_gating_can_be_turned_off_for_a_group():
    bridge, client = bridge_with_hub(require_mention=False)
    assert forwarded(bridge, client, update("lunch at noon?", "group")) == "lunch at noon?"
    assert forwarded(bridge, client, update("@HiveBot hi", "group")) == "hi"
