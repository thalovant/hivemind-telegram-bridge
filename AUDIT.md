Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: sections 3 and 4 fixed; findings 8-18 added (fixed in this pass, or open and low).

# Audit: hivemind-telegram-bridge

Evidence: `python -m pytest tests -q` (93 passed, 2026-11-03, Python 3.12 and 3.14) and reading the code at commit `8e0ba9456de8` (the line numbers below refer to it). Sections 1-7 were first written against `cb7ca5f5e458`.

## 1. Every chat is allowed unless an allow-list is set

- **Where:** `hivemind_telegram_bridge/__init__.py:249`.
- **Finding:** with no `--allowed-chat`, any chat the bot is in, and any DM, reaches the hub. With the list set, a DM is refused unless the person's own id is listed (a private chat's id is the user's id).
- **Impact:** anyone who can message the bot spends the hub's capacity. A managed deployment should always set the allow-list.

## 2. No per-chat or per-person rate limit in the bridge

- **Where:** `HiveMindTelegramBridge.forward_to_hivemind` (`hivemind_telegram_bridge/__init__.py:541`).
- **Finding:** every addressed message is forwarded immediately. The hub's admission chain can limit per person (a refusal now gets the "cannot answer right now" line, section 13); the bridge itself does not.
- **Impact:** one person can flood the hub where the hub sets no limit.

## 3. The fallback reply is English only

- **Fixed 2026-11-03.** The bridge's lines live in `hivemind_telegram_bridge/lines.py` (`bridge_line`; en, fr, es, de, it, pt, nl), chosen from the sender's Telegram `language_code`, then `HIVEMIND_LANG`, then English (`HiveMindTelegramBridge.line`). Tests: `tests/test_lines.py`.

## 4. A reply holds the hub's bus thread for up to 10 s

- **Fixed 2026-11-03.** `speak()` schedules the send on the Telegram loop and returns (`_send_in_order`, per-chat order kept); hub sends run on the `_hub_sender` thread, not on the loop. Measured in `tests/test_latency.py` (fake hub, fake Bot API, sendMessage 300 ms): five replies at once reached sendMessage after a median 602.8 ms and at most 1205.4 ms before, 0.1 ms and 0.4 ms after; a reply during a 250 ms hub send waited 240.4 ms before, 0.4 ms after; the bus thread was held 301.6 ms per reply before, 0.1 ms after.

## 5. Tests do not exercise a live Telegram or hub

- **Where:** `tests/conftest.py` (`FakeTelegramAPI`, `FakeHub`).
- **Finding:** the fakes now run the real python-telegram-bot stack (polling, offsets, errors), but the Bot API answers and the hub are canned; a real Telegram API change or a real handshake is not covered.

## 6. The HiveMind client is prerelease-only

- **Where:** `requirements.txt:1` (`hivemind-bus-client>=1.0.13a1`).
- **Finding:** resolves to whatever alpha is newest at build time; images are not reproducible across builds.

## 7. Base image not pinned by digest

- **Where:** `Dockerfile:1` (`FROM python:3.14-slim`).
- **Finding:** each build can pull a different base.

## 8. A token revoked while running left the bridge deaf

- **Fixed 2026-11-03.** A 401 on getUpdates ended python-telegram-bot's polling task and nothing else: the process stayed up and `/readyz` stayed green. `_TokenWatch` (`hivemind_telegram_bridge/__init__.py:163`) sees every 401; the bridge then stops, writes `token_rejected` to `/dev/termination-log` and exits with status 3 (`__main__.main`). Tests: `tests/test_token.py`.

## 9. A token refused at start was printed in the traceback

- **Fixed 2026-11-03.** python-telegram-bot's `InvalidToken` from `Bot.initialize` reads "The token `<token>` was rejected by the server." and ended the process as an uncaught exception. It is caught and not chained (`_run_telegram`); `httpx`/`httpcore` are held at WARNING since their INFO lines print request URLs, which carry the token. Tests: `tests/test_token.py::test_a_token_refused_at_start_stops_the_bridge`, `test_the_http_client_never_logs_the_token_url`.

## 10. The no-answer line never fired against a current hub

- **Fixed 2026-11-03.** The bridge listened only for `hive.complete_intent_failure`; HiveMind-core relays ovos-core's `complete_intent_failure`. Both are handled (`INTENT_FAILURE_TYPES`). Test: `tests/test_lines.py::test_current_and_older_hubs_no_answer_is_handled`.

## 11. A failed hub send logged the person's words

- **Fixed 2026-11-03.** `HiveMessageBusClient.emit` raises `RuntimeError("... Failed to emit : <serialized message>")` when the hub is down; it surfaced in python-telegram-bot's error log. The handler now logs the kind of error only and tells the person at once (`handle_telegram_message`, `_give_up`). Test: `tests/test_bridge.py::test_a_failed_hub_send_never_logs_the_text`.

## 12. Long replies and flood control lost answers

- **Fixed 2026-11-03.** A reply over 4096 characters failed whole ("Message is too long"); it is now split (`hivemind_telegram_bridge/split.py`). A 429 dropped the reply; it is retried after Telegram's wait (`_send_one`, at most 30 s, 3 attempts). Tests: `tests/test_split.py`.

## 13. A refusal by the hub names no chat

- **Where:** `HiveMindTelegramBridge.handle_denied`, `_denied` (`hivemind_telegram_bridge/__init__.py:625`, `:784`).
- **Finding:** `hive.policy.denied` carries no context from the refused message, so the bridge tells the person at once only when exactly one question is waiting; otherwise they hear "cannot answer right now" at the reply timeout (60 s). Status: open, low; needs the hub to echo the refused message's context (SUGGESTIONS.md).

## 14. A typing action can land after the reply

- **Where:** `_keep_typing`, `_send_typing` (`hivemind_telegram_bridge/__init__.py:807`, `:816`).
- **Finding:** typing is its own request and replies never wait for it; if the hub answers while a sendChatAction is in flight and Telegram applies it after the sendMessage, "typing…" shows for up to 5 s after the answer. Status: open, low, by choice (waiting for it would delay every fast reply by a round trip).

## 15. Edited messages are asked again

- **Where:** `hivemind_telegram_bridge/__init__.py:264` (`filters.TEXT & ~filters.COMMAND` also matches edited messages).
- **Finding:** editing a message within 120 s of sending it asks the hub again (an edit keeps the message's original date, so an edit made later than that is not answered). Status: open, low; may be wanted (fix a typo, get the right answer).

## 16. Commands, `/start` included, get no answer

- **Where:** `hivemind_telegram_bridge/__init__.py:264`.
- **Finding:** a person opening the bot for the first time sends `/start` and hears nothing until they ask a question. Status: open, low.

## 17. The default session scope is the chat

- **Where:** `hivemind_telegram_bridge/__init__.py:200` (`session_scope: str = "chat"`).
- **Finding:** the bridge contract names `person` as the default; this bridge defaults to `chat` and the platform sets `TELEGRAM_SESSION_SCOPE`. Status: open, by design until the platform changes its default.

## 18. Every start warned about hub permissions

- **Fixed 2026-11-03.** `start()` logged a WARNING that a fresh client is denied every message type until an admin runs `hivemind-core allow-msg ...`. On a managed hub the platform grants `recognizer_loop:utterance`, so the line was false and sent readers of the log the wrong way. The hint is now logged once, as a WARNING, only when the hub refuses an utterance with `hive.policy.denied` code `acl_disallowed_type` (`HiveMindTelegramBridge.handle_denied`, `ACL_DISALLOWED_TYPE`). Tests: `tests/test_acl_hint.py`.
