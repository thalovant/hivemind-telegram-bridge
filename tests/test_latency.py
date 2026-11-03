"""Neither side waits for the other: measured with a fake hub and a fake Bot API.

The bridge sits between two networks that each have their own thread: the
asyncio loop polling Telegram, and the HiveMind client's bus thread that
delivers the hub's messages one after the other. A reply that waits for
Telegram on the bus thread holds up every later hub message (and the
client's keepalive); a hub send that runs on the loop holds up every reply.

Two scenarios, timed end to end through the real python-telegram-bot stack:

- burst: five people write at once; the hub answers each 50 ms later;
  Telegram takes 300 ms per sendMessage.
- slow hub socket: two messages; each hub send takes 250 ms; the first
  answer arrives while the second message is being sent.

"typing…" is timed too, from reading the message to the sendChatAction
call. Numbers are printed (``pytest -s``) and appended as JSON lines to
``$LATENCY_REPORT`` when that is set.
"""
import json
import os
import statistics

from hivemind_telegram_bridge import HiveMindTelegramBridge

from conftest import FakeHub, run_in_thread, text_update, wait_for

#: generous enough for a loaded CI runner, far below the blocking it guards
#: against (300 ms per queued reply, 250 ms per hub send)
LIMIT = 0.1


def _report(name, numbers):
    line = {"scenario": name, **{k: round(v * 1000, 1) for k, v in numbers.items()}}
    print(f"\nlatency[{name}] " + " ".join(f"{k}={v}ms" for k, v in line.items() if k != "scenario"))
    path = os.environ.get("LATENCY_REPORT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")


def _run(telegram_api, hub, updates, replies_expected):
    bridge = HiveMindTelegramBridge(token="1:T", client=hub)
    outcome = run_in_thread(bridge)
    try:
        assert wait_for(lambda: telegram_api.calls_to("getUpdates"), timeout=10)
        telegram_api.push(*updates)
        # answered, not just started: a send cancelled half way must fail this
        assert wait_for(lambda: len(telegram_api.done_to("sendMessage")) >= replies_expected,
                        timeout=15), "not every reply was sent"
    finally:
        bridge.stop()
        wait_for(lambda: outcome.get("done"), timeout=10)


def _first_send(telegram_api, chat_id):
    return min(t for t, _, params in telegram_api.calls_to("sendMessage")
               if params.get("chat_id") == chat_id)


def test_burst_replies_do_not_queue_behind_each_other(telegram_api):
    hub = FakeHub(answer=lambda message: "the answer", think=0.05)
    telegram_api.latency["sendMessage"] = 0.3
    chats = [101, 102, 103, 104, 105]
    _run(telegram_api, hub,
         [text_update(n, "what time is it", chat_id=chat, user_id=chat)
          for n, chat in enumerate(chats, start=1)], len(chats))

    inbound = [emitted_at - telegram_api.served[n]
               for n, (emitted_at, _) in enumerate(hub.emitted, start=1)]
    outbound = [_first_send(telegram_api, chat) - sent_at for sent_at, chat in hub.replies]
    numbers = {"inbound_median": statistics.median(inbound), "inbound_max": max(inbound),
               "outbound_median": statistics.median(outbound), "outbound_max": max(outbound),
               "bus_held_max": max(hub.held)}
    typing = [min(t for t, _, params in telegram_api.calls_to("sendChatAction")
                  if params.get("chat_id") == chat) - telegram_api.served[n]
              for n, chat in enumerate(chats, start=1)
              if any(params.get("chat_id") == chat for _, _, params in telegram_api.calls_to("sendChatAction"))]
    assert len(typing) == len(chats), "not every chat got typing…"
    numbers["typing_median"], numbers["typing_max"] = statistics.median(typing), max(typing)
    _report("burst", numbers)
    hub.close()
    assert numbers["inbound_max"] < LIMIT
    assert numbers["outbound_max"] < LIMIT, "replies waited for each other's sendMessage"
    assert numbers["bus_held_max"] < LIMIT, "a reply held the hub's bus thread"


def test_a_slow_hub_send_does_not_hold_up_replies(telegram_api):
    hub = FakeHub(answer=lambda message: "the answer", think=0.01, emit_delay=0.25)
    _run(telegram_api, hub,
         [text_update(1, "first", chat_id=201, user_id=1),
          text_update(2, "second", chat_id=202, user_id=2)], 2)

    (first_at, first_chat), (second_at, second_chat) = hub.replies[:2]
    numbers = {"outbound_first": _first_send(telegram_api, first_chat) - first_at,
               "outbound_second": _first_send(telegram_api, second_chat) - second_at,
               "bus_held_max": max(hub.held)}
    _report("slow-hub-socket", numbers)
    hub.close()
    assert numbers["outbound_first"] < LIMIT, "a reply waited for a hub send on the loop"
    assert numbers["outbound_second"] < LIMIT, "the second reply waited"
