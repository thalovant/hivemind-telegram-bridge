Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: rejected token reported, Telegram and hub never wait for each other, stale messages, localized lines, typing..., long replies, flood control.

# FAQ: hivemind-telegram-bridge

## Why does the bot ignore some messages in a chat?

In a chat with several people, `HiveMindTelegramBridge.addressed_text` forwards only messages that mention the bot or reply to one of its messages, and removes the mention first. Direct messages need no mention. `--no-require-mention` (`TELEGRAM_REQUIRE_MENTION=false`) forwards every message.

## How do I keep secrets off the command line?

Use the environment: `TELEGRAM_BOT_TOKEN`, `HIVEMIND_ACCESS_KEY`, `HIVEMIND_PASSWORD`, or the `_FILE` variants pointing at mounted secret files (`hivemind_telegram_bridge/__main__.py`). The image's entrypoint passes no arguments.

## Why must `/home/bridge/.config/hivemind` persist?

It holds the HiveMind identity, whose key the hub pins. A new key on every restart is refused by the hub.

## Do people in one chat share a conversation?

By default yes: one hub session per chat (`HiveMindTelegramBridge.session_id`). `--session-scope person` gives each person their own session in each chat.

## What does `/readyz` check?

That the HiveMind session is live (the client's `handshake_event`) and that the Telegram side is connected (`HiveMindTelegramBridge.ready`). `/healthz` only says the process answers.

## Why is the default branch `main` and not `dev`?

This is the Thalovant fork. Upstream's release workflows were removed, so nothing here publishes to PyPI or to upstream's registry; images go to `ghcr.io/thalovant/hivemind-telegram-bridge`.

## What happens when the bot token is wrong or revoked?

When Telegram answers 401 Unauthorized, at start (getMe) or later while polling, the bridge closes its hub connection, writes exactly `token_rejected` to `/dev/termination-log` and exits with status 3 (`hivemind_telegram_bridge.TokenRejected`, `report_token_rejected`, `_TokenWatch`). A malformed token (404 at start) counts too; 409 Conflict (another process polling with the same token) does not. The token itself is never logged: python-telegram-bot's `InvalidToken` message holds it, so it is not chained, and httpx's request-URL logging is kept at WARNING.

## Why did the bot not answer what I sent while it was down?

A message sent more than 120 s before the bridge reads it is not answered (`HiveMindTelegramBridge._is_stale`, `MAX_MESSAGE_AGE`): after a restart Telegram hands over up to 24 hours of backlog. It is still confirmed, so Telegram never sends it again. A message sent shortly before the start is answered.

## What does the bot say when the hub cannot answer, and in which language?

"I don't know how to answer that." when no skill matched (`complete_intent_failure`), and "Sorry, I can't answer right now." when no answer came within `--reply-timeout` (60 s), the hub could not be reached, or it refused the only question waiting (`hive.policy.denied`). The second is said at most once every 30 s to the same person in a chat. The language is the sender's Telegram `language_code` when `hivemind_telegram_bridge/lines.py` has it (en, fr, es, de, it, pt, nl), else `HIVEMIND_LANG`, else English.

## Why does the chat show "typing…"?

From the moment a question is read until the hub answers, the bridge sends Telegram's typing action every 4 s (`_expect_answer`, `_keep_typing`). It stops on the answer, on `ovos.utterance.handled`, or at the reply timeout. `--reply-timeout 0` turns it off together with the timeout line.

## How fast does a reply reach Telegram?

As soon as the hub's `speak` arrives: `speak()` schedules the send on the Telegram loop and returns, and hub sends run on their own thread (`_hub_sender`), so neither waits for the other. With a fake hub and a fake Bot API (`tests/test_latency.py`), five replies at once reach sendMessage within 0.4 ms of arriving; before this they queued behind each other's sends (up to 1.2 s with a 300 ms Telegram).

## What about a reply longer than a Telegram message?

It is sent as several messages, in order, with every character kept (`hivemind_telegram_bridge/split.py`, `split_message`); cuts fall on paragraph, line, sentence or word boundaries. A reply held back by flood control (429) is sent again after the wait Telegram asks for.

## The log says the hub does not allow this bridge to send `recognizer_loop:utterance`

The hub refused a question with `hive.policy.denied`, code `acl_disallowed_type`: its message-type ACL does not list `recognizer_loop:utterance` for this client. On a self-managed hub an admin runs `hivemind-core allow-msg recognizer_loop:utterance <client_id>` (and `speak`); on a managed hub the platform grants it, so the line means the grant is missing. The bridge logs it once, and only after such a refusal (`HiveMindTelegramBridge.handle_denied`); it no longer warns about it at every start.

