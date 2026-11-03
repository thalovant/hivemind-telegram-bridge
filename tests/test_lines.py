"""The bridge's own lines are short and in the person's language.

The bridge speaks only when the hub cannot: no skill matched, or no answer
came. The line is in the sender's Telegram language when there is one for
it, else in HIVEMIND_LANG, else in English.
"""
import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from ovos_bus_client.message import Message

from hivemind_telegram_bridge import HiveMindTelegramBridge
from hivemind_telegram_bridge.lines import LINES, NO_ANSWER, UNAVAILABLE, bridge_line, language_of


@pytest.mark.parametrize("tag, expected", [
    ("fr-FR", "fr"), ("fr_ca", "fr"), ("FR", "fr"), ("pt-br", "pt"), ("en-us", "en"),
    ("", None), (None, None), (42, None),
])
def test_language_of(tag, expected):
    assert language_of(tag) == expected


def test_every_language_has_every_line_and_each_is_short():
    assert LINES.keys() == {"en", "fr", "es", "de", "it", "pt", "nl"}  # as documented
    for lines in LINES.values():
        assert lines.keys() == {NO_ANSWER, UNAVAILABLE}
        assert all(0 < len(line) <= 60 for line in lines.values())


EXPECTED = {
    "en": ("I don't know how to answer that.", "Sorry, I can't answer right now."),
    "fr": ("Je ne sais pas répondre à ça.", "Désolé, je ne peux pas répondre pour le moment."),
    "es": ("No sé cómo responder a eso.", "Lo siento, ahora mismo no puedo responder."),
    "de": ("Darauf weiß ich keine Antwort.", "Entschuldigung, ich kann gerade nicht antworten."),
    "it": ("Non so come rispondere.", "Mi dispiace, al momento non posso rispondere."),
    "pt": ("Não sei responder a isso.", "Desculpe, não consigo responder agora."),
    "nl": ("Daar weet ik geen antwoord op.", "Sorry, ik kan nu niet antwoorden."),
}


@pytest.mark.parametrize("language", sorted(EXPECTED))
def test_each_line_is_really_in_its_language(language):
    """Literal text: replacing a translation with English must fail here."""
    assert (LINES[language][NO_ANSWER], LINES[language][UNAVAILABLE]) == EXPECTED[language]


def test_the_first_language_with_a_line_wins():
    assert bridge_line(NO_ANSWER, "fr-FR") == LINES["fr"][NO_ANSWER]
    assert bridge_line(UNAVAILABLE, "xx", None, "fr-fr") == LINES["fr"][UNAVAILABLE]
    assert bridge_line(NO_ANSWER, "es", "fr-fr") == LINES["es"][NO_ANSWER]
    assert bridge_line(NO_ANSWER, "xx-yy") == LINES["en"][NO_ANSWER]
    assert bridge_line(NO_ANSWER) == LINES["en"][NO_ANSWER]


def _bridge(**kwargs):
    client = MagicMock()
    app = MagicMock()
    app.bot.id = 999
    app.bot.username = "HiveBot"
    app.bot.send_message = AsyncMock()
    made = HiveMindTelegramBridge(client=client, app=app, **kwargs)
    made.connect_hivemind()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    made._loop = loop
    return made, client, app


def _ask(made, language_code=None, chat_id=42, user_id=1):
    upd = MagicMock()
    upd.effective_message.text = "hello"
    upd.effective_message.date = None
    upd.effective_user.id = user_id
    upd.effective_user.username = "alice"
    upd.effective_user.is_bot = False
    upd.effective_user.language_code = language_code
    upd.effective_chat.id = chat_id
    upd.effective_chat.type = "private"
    asyncio.run(made.handle_telegram_message(upd, MagicMock()))


def _no_answer(made, client, msg_type="complete_intent_failure", chat_id=42, user_id=1):
    """The hub's no-answer, carrying the question's context back, as the hub does."""
    handlers = {call.args[0]: call.args[1] for call in client.on_mycroft.call_args_list}
    session = {"session_id": made.session_id(chat_id, None)}
    handlers[msg_type](Message(msg_type, {}, {"chat_id": chat_id, "session": session,
                                              "user": {"telegram_user_id": user_id}}))


def _sent(app):
    deadline = time.monotonic() + 2
    while not app.bot.send_message.await_count and time.monotonic() < deadline:
        time.sleep(0.01)
    return app.bot.send_message.call_args.kwargs["text"]


def test_no_answer_follows_hivemind_lang():
    made, client, app = _bridge(lang="fr-fr")
    _no_answer(made, client)
    assert _sent(app) == LINES["fr"][NO_ANSWER]


def test_the_senders_telegram_language_comes_first():
    made, client, app = _bridge(lang="fr-fr")
    _ask(made, language_code="es")
    _no_answer(made, client)
    assert _sent(app) == LINES["es"][NO_ANSWER]


def test_a_sender_language_without_lines_falls_back_to_hivemind_lang():
    made, client, app = _bridge(lang="fr-fr")
    _ask(made, language_code="ja")
    _no_answer(made, client)
    assert _sent(app) == LINES["fr"][NO_ANSWER]


@pytest.mark.parametrize("msg_type", ["complete_intent_failure", "hive.complete_intent_failure"])
def test_current_and_older_hubs_no_answer_is_handled(msg_type):
    made, client, app = _bridge()
    _no_answer(made, client, msg_type)
    assert _sent(app) == LINES["en"][NO_ANSWER]


def _sent_all(app, count):
    deadline = time.monotonic() + 2
    while app.bot.send_message.await_count < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return [call.kwargs["text"] for call in app.bot.send_message.call_args_list]


def test_in_a_shared_chat_each_no_answer_is_in_the_askers_language():
    made, client, app = _bridge(lang="en-us")  # one hub session for the whole chat
    _ask(made, language_code="fr", user_id=1)
    _ask(made, language_code="de", user_id=2)
    _no_answer(made, client, user_id=1)
    assert _sent_all(app, 1) == [LINES["fr"][NO_ANSWER]]
    _no_answer(made, client, user_id=2)
    assert _sent_all(app, 2)[-1] == LINES["de"][NO_ANSWER]


def test_a_sender_whose_app_reports_no_language_gets_hivemind_lang():
    made, client, app = _bridge(lang="fr-fr")
    _ask(made, language_code="es", user_id=1)
    _ask(made, language_code=None, user_id=1)  # the same person, now without one
    _no_answer(made, client, user_id=1)
    assert _sent(app) == LINES["fr"][NO_ANSWER]
