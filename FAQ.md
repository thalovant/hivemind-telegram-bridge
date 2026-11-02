Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: fixes on main, Thalovant CI and signed images, docs set added.

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
