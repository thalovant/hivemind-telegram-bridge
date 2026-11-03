"""A reply too long for one Telegram message is sent as several, unchanged.

Telegram refuses a message over 4096 characters (UTF-16 code units), and a
long answer used to be lost whole. And Telegram's flood control (429,
"retry after N s") is waited out instead of dropping the reply.
"""
import pytest

from hivemind_telegram_bridge import HiveMindTelegramBridge
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
    telegram_api.flood["sendMessage"] = 1
    telegram_api.flood_wait = 500  # more than RETRY_AFTER_BUDGET
    bridge, outcome = _run(telegram_api, "it is noon")
    try:
        assert wait_for(lambda: telegram_api.calls_to("sendMessage"), timeout=5)
        import time

        time.sleep(0.5)
        assert len(telegram_api.calls_to("sendMessage")) == 1
    finally:
        bridge.stop()
        wait_for(lambda: outcome.get("done"), timeout=10)
