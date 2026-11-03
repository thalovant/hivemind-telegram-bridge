"""While the hub thinks, the chat shows "typing…"; if no answer comes, the person is told.

Run through the real python-telegram-bot stack against a fake Bot API and
a fake hub. "typing…" starts as soon as the question is read, is renewed
while the hub thinks, and stops on the answer. With no answer within the
reply timeout, the bridge says it cannot answer right now, in the person's
language; so it does at once when the hub cannot be reached, or refuses the
only question waiting. A late answer is still delivered.
"""
import time

import pytest

import hivemind_telegram_bridge as bridge_module
from hivemind_telegram_bridge import HiveMindTelegramBridge
from hivemind_telegram_bridge.lines import LINES, UNAVAILABLE

from conftest import FakeHub, run_in_thread, text_update, wait_for


@pytest.fixture(autouse=True)
def quick_typing(monkeypatch):
    monkeypatch.setattr(bridge_module, "TYPING_EVERY", 0.2)


class Running:
    """A bridge started in a thread, stopped at the end of the test."""

    def __init__(self, telegram_api, hub, **kwargs):
        self.api, self.hub = telegram_api, hub
        self.bridge = HiveMindTelegramBridge(token="1:T", client=hub, **kwargs)
        self.outcome = run_in_thread(self.bridge)
        assert wait_for(lambda: telegram_api.calls_to("getUpdates"), timeout=10)

    def ask(self, update_id, chat_id=42, user_id=1, language_code=None):
        self.api.push(text_update(update_id, "what time is it", chat_id=chat_id,
                                  user_id=user_id, language_code=language_code))
        assert wait_for(lambda: update_id in self.api.served)
        return self.api.served[update_id]

    def typing(self, chat_id=42):
        return [t for t, _, params in self.api.calls_to("sendChatAction")
                if params.get("chat_id") == chat_id and params.get("action") == "typing"]

    def sent(self, chat_id=42):
        return [params["text"] for _, _, params in self.api.calls_to("sendMessage")
                if params.get("chat_id") == chat_id]

    def stop(self):
        self.bridge.stop()
        assert wait_for(lambda: self.outcome.get("done"), timeout=10)


@pytest.fixture
def running(telegram_api):
    made = []

    def start(hub=None, **kwargs):
        made.append(Running(telegram_api, hub or FakeHub(), **kwargs))
        return made[-1]

    yield start
    for each in made:
        each.stop()


def waiting(made, chat_id=None):
    """How many questions the bridge is waiting on (in ``chat_id``, or anywhere)."""
    return sum(len(queue) for (chat, _), queue in list(made._pending.items())
               if chat_id is None or chat == chat_id)


def _answer_last(hub, text="it is noon"):
    _, message = hub.emitted[-1]
    hub.send("speak", {"utterance": text}, dict(message.context))


def test_typing_shows_at_once_is_renewed_and_stops_on_the_answer(running):
    bridge = running()
    asked = bridge.ask(1)
    assert wait_for(lambda: bridge.typing())
    assert bridge.typing()[0] - asked < 0.1, "typing… came late"
    assert wait_for(lambda: len(bridge.typing()) >= 3, timeout=3), "typing… was not renewed"
    assert wait_for(lambda: bridge.hub.emitted)
    _answer_last(bridge.hub)
    assert wait_for(lambda: bridge.sent() == ["it is noon"])
    count = len(bridge.typing())
    time.sleep(0.6)
    assert len(bridge.typing()) == count, "typing… went on after the answer"
    assert waiting(bridge.bridge) == 0


def test_no_answer_in_time_is_said_in_the_persons_language_and_a_late_answer_still_comes(running):
    bridge = running(reply_timeout=0.5, lang="en-us")
    bridge.ask(1, language_code="fr")
    assert wait_for(lambda: bridge.sent(), timeout=3)
    assert bridge.sent() == [LINES["fr"][UNAVAILABLE]]
    count = len(bridge.typing())
    time.sleep(0.5)
    assert len(bridge.typing()) == count, "typing… went on after the timeout"
    _answer_last(bridge.hub, "late answer")
    assert wait_for(lambda: bridge.sent()[-1:] == ["late answer"])


def test_the_hub_done_without_an_answer_ends_the_wait_quietly(running):
    bridge = running(reply_timeout=0.5)
    bridge.ask(1)
    assert wait_for(lambda: bridge.hub.emitted)
    _, message = bridge.hub.emitted[-1]
    bridge.hub.send("ovos.utterance.handled", {}, dict(message.context))
    time.sleep(0.9)
    assert bridge.sent() == []
    assert waiting(bridge.bridge) == 0


def test_a_hub_that_cannot_be_reached_is_said_at_once(running):
    hub = FakeHub()

    def down(_):
        raise RuntimeError("Can not send messages before opening the websocket connection")

    hub.emit = down
    bridge = running(hub, reply_timeout=30, lang="fr-fr")
    asked = bridge.ask(1)
    assert wait_for(lambda: bridge.sent(), timeout=2)
    assert bridge.sent() == [LINES["fr"][UNAVAILABLE]]
    first = min(t for t, _, _ in bridge.api.calls_to("sendMessage"))
    assert first - asked < 0.5


def test_a_refusal_of_the_only_waiting_question_is_said_at_once(running):
    bridge = running(reply_timeout=30)
    bridge.ask(1)
    assert wait_for(lambda: bridge.hub.emitted)
    bridge.hub.send("hive.policy.denied", {"denied_type": "recognizer_loop:utterance",
                                           "code": "intent_quota_exceeded"}, {})
    assert wait_for(lambda: bridge.sent() == [LINES["en"][UNAVAILABLE]], timeout=2)
    assert waiting(bridge.bridge) == 0


def test_a_refusal_that_could_be_anyones_is_left_to_the_timeout(running):
    bridge = running(reply_timeout=30)
    bridge.ask(1, chat_id=1, user_id=1)
    bridge.ask(2, chat_id=2, user_id=2)
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    bridge.hub.send("hive.policy.denied", {"denied_type": "recognizer_loop:utterance"}, {})
    time.sleep(0.3)
    assert bridge.api.calls_to("sendMessage") == []
    assert waiting(bridge.bridge) == 2


def test_each_unanswered_question_gets_its_own_line(running):
    """One person asks twice; neither is answered: two lines, each at its own timeout."""
    bridge = running(reply_timeout=0.4)
    first = bridge.ask(1)
    time.sleep(0.2)
    second = bridge.ask(2)
    assert wait_for(lambda: len(bridge.sent()) == 2, timeout=3)
    assert bridge.sent() == [LINES["en"][UNAVAILABLE]] * 2
    sends = [t for t, _, _ in bridge.api.calls_to("sendMessage")]
    assert sends[0] - first >= 0.35 and sends[1] - second >= 0.35


def test_waiting_can_be_turned_off(running):
    bridge = running(reply_timeout=0)
    bridge.ask(1)
    assert wait_for(lambda: bridge.hub.emitted)
    time.sleep(0.4)
    assert bridge.typing() == [] and bridge.sent() == []


def test_a_late_answer_to_an_expired_question_leaves_other_waits_alone(running):
    """Person scope: Alice's question timed out; its late answer must not end Bob's wait."""
    bridge = running(reply_timeout=0.4, session_scope="person")
    bridge.ask(1, chat_id=-100, user_id=1)
    assert wait_for(lambda: len(bridge.hub.emitted) == 1)
    _, alice = bridge.hub.emitted[-1]
    assert wait_for(lambda: bridge.sent(-100), timeout=2)  # Alice's wait ended
    bridge.api.push(text_update(2, "@HiveBot and tomorrow", chat_id=-100, user_id=2,
                                chat_type="group"))
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    bridge.hub.send("speak", {"utterance": "late for alice"}, dict(alice.context))
    assert wait_for(lambda: "late for alice" in bridge.sent(-100))
    time.sleep(0.05)
    assert waiting(bridge.bridge, -100) == 1, "Bob's wait was ended"


def test_each_question_in_a_shared_chat_is_told_when_the_hub_is_down(running):
    """Chat scope: one session for the chat, one line per question that cannot go."""
    hub = FakeHub()

    def down(_):
        raise RuntimeError("Can not send messages before opening the websocket connection")

    hub.emit = down
    bridge = running(hub, reply_timeout=30)
    bridge.api.push(text_update(1, "@HiveBot hi", chat_id=-100, user_id=1, chat_type="group"))
    assert wait_for(lambda: len(bridge.sent(-100)) == 1, timeout=2)
    bridge.api.push(text_update(2, "@HiveBot hi", chat_id=-100, user_id=2, chat_type="group"))
    assert wait_for(lambda: len(bridge.sent(-100)) == 2, timeout=2)
    bridge.api.push(text_update(3, "@HiveBot hi again", chat_id=-100, user_id=1, chat_type="group"))
    assert wait_for(lambda: len(bridge.sent(-100)) == 3, timeout=2)


def test_a_slow_hub_send_does_not_hold_up_reading_the_next_message(telegram_api):
    hub = FakeHub(emit_delay=0.5)
    made = Running(telegram_api, hub)
    try:
        telegram_api.push(text_update(1, "first", chat_id=1, user_id=1),
                          text_update(2, "second", chat_id=2, user_id=2))
        assert wait_for(lambda: made.typing(2), timeout=3)
        assert made.typing(2)[0] - telegram_api.served[2] < 0.2, "the second message waited"
    finally:
        made.stop()
        hub.close()


@pytest.mark.parametrize("setting", [{"reply_timeout": float("inf")}, {"reply_timeout": float("nan")},
                                     {"reply_timeout": -1}, {"max_message_age": -1},
                                     {"max_message_age": float("nan")}])
def test_nonsense_timings_are_refused(setting):
    from unittest.mock import MagicMock

    with pytest.raises(ValueError):
        HiveMindTelegramBridge(client=MagicMock(), app=MagicMock(), **setting)


def test_stopping_lets_a_reply_being_sent_finish(telegram_api):
    """A long reply is several sendMessage calls; stopping must not cut it in half."""
    hub = FakeHub(answer=lambda message: "\n".join("x" * 3000 for _ in range(3)))
    telegram_api.latency["sendMessage"] = 0.3
    made = Running(telegram_api, hub)
    made.ask(1)
    assert wait_for(lambda: telegram_api.calls_to("sendMessage"), timeout=5)
    made.stop()
    assert len(telegram_api.done_to("sendMessage")) == 3


def test_a_question_that_waited_past_the_timeout_for_the_hub_is_not_sent(running):
    """Told "cannot answer" already, the person must not get an answer after that."""
    hub = FakeHub(emit_delay=0.6)
    bridge = running(hub, reply_timeout=0.3)
    bridge.api.push(text_update(1, "first", chat_id=1, user_id=1),
                    text_update(2, "second", chat_id=2, user_id=2))
    assert wait_for(lambda: bridge.sent(2), timeout=3)  # told at the timeout
    time.sleep(1.0)
    assert [message.context["chat_id"] for _, message in hub.emitted] == [1]


def test_a_hub_that_does_not_keep_up_gets_no_endless_queue(running, monkeypatch):
    monkeypatch.setattr(bridge_module, "HUB_BACKLOG_MAX", 1)
    hub = FakeHub(emit_delay=0.5)
    bridge = running(hub, reply_timeout=30)
    bridge.api.push(text_update(1, "first", chat_id=1, user_id=1),
                    text_update(2, "second", chat_id=2, user_id=2))
    assert wait_for(lambda: bridge.sent(2), timeout=2)
    assert bridge.api.calls_to("sendMessage")[0][0] - bridge.api.served[2] < 0.3
    assert bridge.sent(2) == [LINES["en"][UNAVAILABLE]]
    time.sleep(0.8)
    assert [message.context["chat_id"] for _, message in hub.emitted] == [1]


def test_a_refusal_is_not_pinned_on_one_of_several_waiting_questions(running):
    """Chat scope: two people's questions wait; the refused one is unknown."""
    bridge = running(reply_timeout=30)
    bridge.api.push(text_update(1, "@HiveBot one", chat_id=-100, user_id=1, chat_type="group"),
                    text_update(2, "@HiveBot two", chat_id=-100, user_id=2, chat_type="group"))
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    bridge.hub.send("hive.policy.denied", {"denied_type": "recognizer_loop:utterance"}, {})
    time.sleep(0.3)
    assert bridge.sent(-100) == []
    assert waiting(bridge.bridge, -100) == 2


def _group(bridge, update_id, user_id, text="@HiveBot what time is it"):
    bridge.api.push(text_update(update_id, text, chat_id=-100, user_id=user_id,
                                chat_type="group"))
    assert wait_for(lambda: update_id in bridge.api.served)


def test_two_people_in_one_chat_each_keep_their_wait(running):
    """Chat scope: one session; the hub answers Alice; Bob still waits, then hears his own line."""
    bridge = running(reply_timeout=0.8)
    _group(bridge, 1, user_id=1)
    _group(bridge, 2, user_id=2)
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    _, alice = bridge.hub.emitted[0]
    bridge.hub.send("speak", {"utterance": "noon, Alice"}, dict(alice.context))
    bridge.hub.send("ovos.utterance.handled", {}, dict(alice.context))
    assert wait_for(lambda: "noon, Alice" in bridge.sent(-100))
    time.sleep(0.1)
    assert waiting(bridge.bridge, -100) == 1, "Bob's wait ended with Alice's answer"
    count = len(bridge.typing(-100))
    assert wait_for(lambda: len(bridge.typing(-100)) > count, timeout=1), "no typing… for Bob"
    assert wait_for(lambda: bridge.sent(-100)[-1:] == [LINES["en"][UNAVAILABLE]], timeout=3)
    assert bridge.sent(-100) == ["noon, Alice", LINES["en"][UNAVAILABLE]]


def test_one_person_asking_twice_gets_one_wait_per_question(running):
    """The first reply ends the first question's wait only; the second is answered next."""
    bridge = running(reply_timeout=30, session_scope="person")
    _group(bridge, 1, user_id=1)
    _group(bridge, 2, user_id=1, text="@HiveBot and the weather")
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    _, first = bridge.hub.emitted[0]
    _, second = bridge.hub.emitted[1]
    bridge.hub.send("speak", {"utterance": "noon"}, dict(first.context))
    bridge.hub.send("ovos.utterance.handled", {}, dict(first.context))
    assert wait_for(lambda: bridge.sent(-100) == ["noon"])
    time.sleep(0.1)
    assert waiting(bridge.bridge, -100) == 1
    bridge.hub.send("speak", {"utterance": "sunny"}, dict(second.context))
    assert wait_for(lambda: bridge.sent(-100) == ["noon", "sunny"])
    time.sleep(0.05)
    assert waiting(bridge.bridge) == 0


def test_a_second_sentence_of_an_answer_ends_no_other_wait(running):
    """Person scope: Alice's answer comes in two speaks; Bob's wait is untouched."""
    bridge = running(reply_timeout=30, session_scope="person")
    _group(bridge, 1, user_id=1)
    _group(bridge, 2, user_id=2)
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    _, alice = bridge.hub.emitted[0]
    for sentence in ("It is noon.", "Have a nice lunch."):
        bridge.hub.send("speak", {"utterance": sentence}, dict(alice.context))
    bridge.hub.send("ovos.utterance.handled", {}, dict(alice.context))
    assert wait_for(lambda: len(bridge.sent(-100)) == 2)
    time.sleep(0.1)
    assert waiting(bridge.bridge, -100) == 1


def test_an_answer_with_no_session_ends_only_the_oldest_wait(running):
    """Person scope: an answer that names no session ends Alice's wait, not Bob's."""
    bridge = running(reply_timeout=30, session_scope="person")
    _group(bridge, 1, user_id=1)
    _group(bridge, 2, user_id=2)
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    _, alice = bridge.hub.emitted[0]
    context = {k: v for k, v in alice.context.items() if k != "session"}
    bridge.hub.send("speak", {"utterance": "noon"}, context)
    assert wait_for(lambda: bridge.sent(-100) == ["noon"])
    time.sleep(0.1)
    left = [q.user_id for queue in bridge.bridge._pending.values() for q in queue]
    assert left == [2]



def test_in_a_shared_chat_a_second_sentence_ends_no_other_wait(running):
    """Chat scope (one session): Alice's answer in two speaks leaves Bob's wait alone."""
    bridge = running(reply_timeout=30)
    _group(bridge, 1, user_id=1)
    _group(bridge, 2, user_id=2)
    assert wait_for(lambda: len(bridge.hub.emitted) == 2)
    _, alice = bridge.hub.emitted[0]
    for sentence in ("It is noon.", "Have a nice lunch."):
        bridge.hub.send("speak", {"utterance": sentence}, dict(alice.context))
    bridge.hub.send("ovos.utterance.handled", {}, dict(alice.context))
    assert wait_for(lambda: len(bridge.sent(-100)) == 2)
    time.sleep(0.1)
    left = [q.user_id for queue in bridge.bridge._pending.values() for q in queue]
    assert left == [2], "Bob's wait was ended by Alice's second sentence"


def test_an_answer_for_a_session_with_nothing_waiting_ends_no_wait(running):
    """However late (the session long forgotten), it never ends someone else's wait."""
    bridge = running(reply_timeout=30, session_scope="person")
    _group(bridge, 1, user_id=2)
    assert wait_for(lambda: bridge.hub.emitted)
    bridge.hub.send("speak", {"utterance": "very late, for someone"},
                    {"chat_id": -100, "session": {"session_id": "telegram--100-7"}})
    assert wait_for(lambda: bridge.sent(-100) == ["very late, for someone"])
    time.sleep(0.05)
    assert waiting(bridge.bridge, -100) == 1


def test_a_refusal_long_after_the_only_question_is_not_pinned_on_it(running, monkeypatch):
    """Refusals come at admission: one arriving later is not about this question."""
    monkeypatch.setattr(bridge_module, "REFUSAL_WINDOW", 0.2)
    bridge = running(reply_timeout=30)
    bridge.ask(1)
    assert wait_for(lambda: bridge.hub.emitted)
    time.sleep(0.4)
    bridge.hub.send("hive.policy.denied", {"denied_type": "recognizer_loop:utterance"}, {})
    time.sleep(0.3)
    assert bridge.sent() == []
    assert waiting(bridge.bridge) == 1


def test_a_question_that_got_too_old_in_the_hub_queue_is_not_sent(running):
    """Read in time, but older than max_message_age by the time the hub socket is free."""
    # Bot API dates are whole seconds: date them up to 1 s ahead, so both are
    # fresh when read, and the second is over 0.5 s old once the 1.6 s send
    # of the first frees the hub socket.
    hub = FakeHub(emit_delay=1.6)
    bridge = running(hub, reply_timeout=30, max_message_age=0.5)
    ahead = time.time() + 1
    bridge.api.push(text_update(1, "first", chat_id=1, user_id=1, date=ahead),
                    text_update(2, "second", chat_id=2, user_id=2, date=ahead))
    assert wait_for(lambda: len(hub.emitted) == 1, timeout=5)
    time.sleep(2.0)  # long enough for the second to have been sent, had it gone
    assert [message.context["chat_id"] for _, message in hub.emitted] == [1]


def test_replies_queued_for_one_chat_are_capped(running, monkeypatch):
    monkeypatch.setattr(bridge_module, "SENDS_PER_CHAT_MAX", 2)
    bridge = running(reply_timeout=0)
    bridge.api.latency["sendMessage"] = 0.3
    for n in range(5):
        bridge.bridge.speak(f"reply {n}", 42)
    time.sleep(1.0)
    assert [params["text"] for _, _, params in bridge.api.calls_to("sendMessage")] == [
        "reply 0", "reply 1"]
