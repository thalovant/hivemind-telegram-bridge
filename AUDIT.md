Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: sections 3 and 4 fixed; findings 8-19 added (fixed in this pass, or open and low), review findings in 19.

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

- **Fixed 2026-11-03.** A reply over 4096 characters failed whole ("Message is too long"); it is now split (`hivemind_telegram_bridge/split.py`). A 429 dropped the reply; it is sent again after exactly Telegram's wait while the waits for that reply stay within 120 s (`_send_one`, `RETRY_AFTER_BUDGET`). Tests: `tests/test_split.py`.

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

## 19. Review findings on the polish pull request

- **Fixed 2026-11-03**, from the automated review of thalovant/hivemind-telegram-bridge#3:
  - In a shared chat the no-answer line used the language of whoever wrote last; languages are now kept per (chat, person) and read from the user id the hub echoes (`_remember_language`, `_language_of`). Tests: `tests/test_lines.py::test_in_a_shared_chat_each_no_answer_is_in_the_askers_language`, `test_a_sender_whose_app_reports_no_language_gets_hivemind_lang`.
  - Waits were per session: in a chat-scope group the second person was not told, a second question restarted the first one's wait, and the first answer ended both. Every question now has its own wait (`_Question` in `_pending`, oldest first per session), its own timeout and its own "cannot answer right now" line; "typing…" shows while any question in the chat waits. A reply (`speak`, or the no-answer line) ends only the oldest waiting question of its session; the `ovos.utterance.handled` that follows it ends nothing more (`_replied` pairs them), and alone it ends one question quietly. A reply is matched to its question by the session and the person the hub echoes back (`_question_for`): a further sentence or a late answer for a person or session with nothing waiting ends no one's wait, however late. Tests: `tests/test_waiting.py::test_two_people_in_one_chat_each_keep_their_wait`, `test_one_person_asking_twice_gets_one_wait_per_question`, `test_a_second_sentence_of_an_answer_ends_no_other_wait`, `test_each_unanswered_question_gets_its_own_line`, `test_each_question_in_a_shared_chat_is_told_when_the_hub_is_down`, `test_a_late_answer_to_an_expired_question_leaves_other_waits_alone`.
  - An answer with no session ended every wait in the chat; it now ends only the oldest waiting question in the chat, that person's when the hub names them (`_question_for`). Tests: `test_an_answer_with_no_session_ends_only_the_oldest_wait`, `test_in_a_shared_chat_a_second_sentence_ends_no_other_wait`, `test_an_answer_for_a_session_with_nothing_waiting_ends_no_wait`.
  - Handlers awaited the hub send with python-telegram-bot's sequential processing, so a slow hub delayed reading the next message; they run with `block=False`. Test: `test_a_slow_hub_send_does_not_hold_up_reading_the_next_message`.
  - Stopping cancelled replies mid-send; `_drain_sends` gives them `SEND_DRAIN` (5 s). Test: `test_stopping_lets_a_reply_being_sent_finish`. Only `stop()` drains: a SIGTERM still ends the process at once (SUGGESTIONS.md).
  - A rejected token left the health port bound and the HTTP clients open; both are released (`start`, `_run_telegram`). Test: `tests/test_token.py::test_a_rejected_token_frees_the_health_port_and_the_http_clients`.
  - Negative or NaN `max_message_age` and negative or non-finite `reply_timeout` are refused (`__init__`, `resolve_options`). Tests: `test_nonsense_timings_are_refused`, `tests/test_cli.py::test_the_reply_timeout_defaults_to_a_minute_and_must_be_a_number`.
  - A slow hub socket queued questions for the hub-send thread without limit, and a queued one could be sent after the person had been told the bridge cannot answer. At most `HUB_BACKLOG_MAX` (64) wait; beyond that the person is told at once; a question that waited past the reply timeout, or is by then older than `max_message_age`, is not sent (`_forward_unless_late`). Test: `test_a_question_that_got_too_old_in_the_hub_queue_is_not_sent`; Tests: `test_a_hub_that_does_not_keep_up_gets_no_endless_queue`, `test_a_question_that_waited_past_the_timeout_for_the_hub_is_not_sent`.
  - A refusal was pinned on the only waiting question, even one asked long before (whose refusal had come and gone). The hub refuses at admission, so a refusal now goes to a question only when that is the one question asked in the last `REFUSAL_WINDOW` (5 s) (`_denied`, `_recent`); otherwise the reply timeout tells the person. Tests: `test_a_refusal_is_not_pinned_on_one_of_several_waiting_questions`, `test_a_refusal_long_after_the_only_question_is_not_pinned_on_it`.
  - A stale message was dropped at DEBUG, so a wrong host clock silently dropped everything; drops are logged at INFO with the age (`_is_stale`). Test: `tests/test_stale.py::test_a_dropped_message_is_logged_with_its_age_not_its_text`.
  - Replies queued for one chat were unbounded while Telegram was slow or holding the bot back; at most `SENDS_PER_CHAT_MAX` (32) wait, more are dropped and logged (`_send_in_order`). Test: `test_replies_queued_for_one_chat_are_capped`.
  - A 429 asking for 0 s never used up the 120 s budget; retries are capped at `RETRY_AFTER_MAX_RETRIES` (10), and the waits and retries are counted for the whole reply, all its pieces together (`_send_one`'s `budget`). Tests: `tests/test_split.py::test_flood_retries_are_capped_however_short_the_wait`, `test_one_flood_budget_covers_every_piece_of_a_reply`, `test_a_flood_wait_beyond_the_budget_gives_the_reply_up_at_once`.
  - A reply Telegram refused left its question without a word (its wait had ended when the reply came); the person now gets the "cannot answer right now" line (`_send_reply`). Test: `test_a_reply_telegram_refuses_is_followed_by_the_cannot_answer_line`.
  - Splitting copied the rest of the reply after every piece (quadratic: 15 MB took 2.5 s on the loop); it walks the text by offset (0.02 s), with the same pieces (`split_message`). Test: `test_splitting_stays_linear_in_the_length`. A whitespace run longer than a whole message is shortened, since Telegram refuses an empty message; the docstrings now say so (`test_an_interior_whitespace_run_longer_than_a_piece_is_shortened_not_sent_empty`).
  - Tests: the burst test requires "typing…" in every chat, the slow-hub test times the second reply too, the language test requires all seven languages, the real `HiveMessageBusClient` constructor is exercised once, the health socket is checked closed, the HTTP-client spy records only completed shutdowns.
  - CI: the image job smoke-tests the pushed digest before signing it and before `:latest` moves (`.github/workflows/ci.yml`); the base image is still not pinned by digest (section 7: no automated digest updates on this fork yet, so a pin would freeze security fixes).
- **Not changed:** a 401 while polling is seen only on the transport the bridge builds itself (`_TokenWatch`); an injected `Application` (tests) does not report it. Failed sends keep Telegram's own error description in the log ("Forbidden: bot was blocked by the user"): it is Telegram's text about the failure, never the reply. Residual, low: a reply carries no question id, so when one person has two questions waiting in a session, a two-sentence answer to the first can end the second's wait early (no false "cannot answer" line comes of it).
