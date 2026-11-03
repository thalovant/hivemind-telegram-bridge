"""A reply too long for one Telegram message is sent as several, unchanged.

Telegram refuses a message over 4096 characters (UTF-16 code units), and a
long answer used to be lost whole. And Telegram's flood control (429,
"retry after N s") is waited out instead of dropping the reply.
"""
import pytest

from hivemind_telegram_bridge import HiveMindTelegramBridge
from hivemind_telegram_bridge.lines import LINES, UNAVAILABLE
from hivemind_telegram_bridge.split import TELEGRAM_LIMIT, split_message

from conftest import FakeHub, run_in_thread, text_update, wait_for


def units(text):
    return len(text.encode("utf-16-le")) // 2


def test_a_short_reply_is_one_message():
    assert split_message("it is noon") == ["it is noon"]


def test_pieces_fit_and_join_back_to_the_reply():
    text = "\n\n".join(f"Paragraph {n}. " + "word " * 300 for n in range(10))
    pieces = split_message(text)
    assert len(pieces) > 1
    assert "".join(pieces) == text
    assert all(units(piece) <= TELEGRAM_LIMIT for piece in pieces)


def test_a_cut_falls_on_a_paragraph_before_a_word():
    first = "a " * 1500
    text = first + "\n\n" + "b " * 1500
    assert split_message(text)[0] == first + "\n\n"


def test_an_emoji_outside_the_bmp_counts_twice():
    text = "\U0001F600" * 3000  # 6000 UTF-16 code units, no space anywhere
    pieces = split_message(text)
    assert "".join(pieces) == text
    assert [units(piece) for piece in pieces] == [4096, 6000 - 4096]


def test_a_word_longer_than_a_message_is_cut():
    text = "x" * 9000
    assert [len(piece) for piece in split_message(text)] == [4096, 4096, 808]


def test_whitespace_only_pieces_are_not_sent():
    pieces = split_message("a" + "\n" * 9000 + "b")
    assert pieces == ["a" + "\n" * 4095, "\n" * 809 + "b"]  # 4096 newlines dropped


def test_a_tiny_limit_is_refused():
    with pytest.raises(ValueError):
        split_message("abc", limit=1)


def _run(telegram_api, answer):
    hub = FakeHub(answer=lambda message: answer)
    bridge = HiveMindTelegramBridge(token="1:T", client=hub)
    outcome = run_in_thread(bridge)
    assert wait_for(lambda: telegram_api.calls_to("getUpdates"), timeout=10)
    telegram_api.push(text_update(1, "tell me a long story"))
    return bridge, outcome


def test_a_long_answer_reaches_telegram_in_order(telegram_api):
    answer = "\n".join(f"line {n:04d} " + "z" * 60 for n in range(200))
    bridge, outcome = _run(telegram_api, answer)
    try:
        assert wait_for(lambda: "".join(p["text"] for _, _, p in telegram_api.calls_to("sendMessage"))
                        == answer, timeout=10)
        # the fake Bot API, like Telegram, refuses a longer message
        assert all(units(p["text"]) <= TELEGRAM_LIMIT for _, _, p in telegram_api.calls_to("sendMessage"))
    finally:
        bridge.stop()
        wait_for(lambda: outcome.get("done"), timeout=10)


def test_flood_control_is_waited_out(telegram_api):
    telegram_api.flood["sendMessage"] = 1
    bridge, outcome = _run(telegram_api, "it is noon")
    try:
        assert wait_for(lambda: len(telegram_api.calls_to("sendMessage")) == 2, timeout=10)
        first, second = telegram_api.calls_to("sendMessage")
        assert second[2]["text"] == "it is noon"
        assert second[0] - first[0] >= 0.9
    finally:
        bridge.stop()
        wait_for(lambda: outcome.get("done"), timeout=10)


def test_a_flood_wait_beyond_the_budget_gives_the_reply_up_at_once(telegram_api):
    """Given up, not slept on: the chat's next reply goes out at once."""
    telegram_api.flood["sendMessage"] = 1
    telegram_api.flood_wait = 500  # more than RETRY_AFTER_BUDGET
    bridge, outcome = _run(telegram_api, "it is noon")
    try:
        assert wait_for(lambda: telegram_api.calls_to("sendMessage"), timeout=5)
        bridge.speak("next reply", 42)
        assert wait_for(lambda: any(p["text"] == "next reply" for _, _, p
                                    in telegram_api.done_to("sendMessage")), timeout=1), \
            "the chat's next reply waited behind a 500 s flood wait"
        sent = [p["text"] for _, _, p in telegram_api.calls_to("sendMessage")]
        # tried once, given up; the person is told (the reply is lost)
        assert sent[0] == "it is noon" and sent.count("it is noon") == 1
        assert sorted(sent[1:]) == sorted([LINES["en"][UNAVAILABLE], "next reply"])
    finally:
        bridge.stop()
        wait_for(lambda: outcome.get("done"), timeout=10)


def test_flood_retries_are_capped_however_short_the_wait():
    """A 429 asking for 0 s adds nothing to the budget; the count stops the loop."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import RetryAfter

    import hivemind_telegram_bridge as bridge_module

    bridge = HiveMindTelegramBridge(client=MagicMock(), app=MagicMock())
    bridge.app.bot.send_message = AsyncMock(side_effect=RetryAfter(0))
    with pytest.raises(RetryAfter):  # not a timeout: an endless loop fails here
        asyncio.run(asyncio.wait_for(bridge._send_one(42, "hi"), 2))
    assert bridge.app.bot.send_message.await_count == bridge_module.RETRY_AFTER_MAX_RETRIES + 1


def test_splitting_stays_linear_in_the_length():
    """Walked by offset: copying the rest after every piece made 15 MB take seconds."""
    import time

    text = ("word " * 199 + "\n") * 16000
    started = time.monotonic()
    pieces = split_message(text)
    assert time.monotonic() - started < 1.0
    assert "".join(pieces) == text


def test_an_interior_whitespace_run_longer_than_a_piece_is_shortened_not_sent_empty():
    """Telegram refuses an empty message: the run loses only what no message could hold."""
    text = "before" + " " * 9000 + "after"
    pieces = split_message(text)
    assert all(piece.strip() for piece in pieces)
    assert pieces[0].startswith("before") and pieces[-1].endswith("after")
    assert "".join(pieces).replace(" ", "") == "beforeafter"


def test_one_flood_budget_covers_every_piece_of_a_reply(monkeypatch):
    """Two pieces, each held back 1 s: within a 1.5 s budget only the first goes."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from telegram.error import RetryAfter

    import hivemind_telegram_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "RETRY_AFTER_BUDGET", 1.5)
    bridge = HiveMindTelegramBridge(client=MagicMock(), app=MagicMock())
    bridge.app.bot.send_message = AsyncMock(side_effect=[RetryAfter(1), None, RetryAfter(1), None])
    text = "a" * 4000 + "\n\n" + "b" * 4000
    with pytest.raises(RetryAfter):
        asyncio.run(bridge._send_in_order(42, text))
    assert bridge.app.bot.send_message.await_count == 3


def test_a_reply_telegram_refuses_is_followed_by_the_cannot_answer_line(telegram_api):
    """The question stopped waiting when its reply came; if the reply cannot go, say so."""
    telegram_api.refuse_text = {"the answer"}
    hub = FakeHub(answer=lambda message: "the answer")
    bridge = HiveMindTelegramBridge(token="1:T", client=hub)
    outcome = run_in_thread(bridge)
    try:
        assert wait_for(lambda: telegram_api.calls_to("getUpdates"), timeout=10)
        telegram_api.push(text_update(1, "what time is it"))
        assert wait_for(lambda: [p["text"] for _, _, p in telegram_api.done_to("sendMessage")]
                        == [LINES["en"][UNAVAILABLE]], timeout=5)
        tried = [p["text"] for _, _, p in telegram_api.calls_to("sendMessage")]
        assert tried == ["the answer", LINES["en"][UNAVAILABLE]], "the answer was not tried first"
    finally:
        bridge.stop()
        wait_for(lambda: outcome.get("done"), timeout=10)
        hub.close()


def test_an_interior_whitespace_run_shorter_than_a_message_is_kept():
    text = "a" * 4096 + " " * 2050 + "b" * 4096
    pieces = split_message(text)
    assert "".join(pieces) == text
    assert all(piece.strip() for piece in pieces)
