Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: rejected token reported, Telegram and hub never wait for each other, stale messages, localized lines, typing..., long replies, flood control.

# hivemind-telegram-bridge

A HiveMind bridge for Telegram: a satellite whose input and output are a Telegram bot instead of a microphone. Messages addressed to the bot become `recognizer_loop:utterance` on the hub; `speak` replies go back to the same chat.

- How to run it, every flag and its environment variable: [README](../readme.md)
- Bot setup walkthrough: [setup.md](setup.md)
- Facts for lookup: [QUICK_FACTS.md](../QUICK_FACTS.md)
- Questions: [FAQ.md](../FAQ.md)
- Known issues: [AUDIT.md](../AUDIT.md)
- Proposed work: [SUGGESTIONS.md](../SUGGESTIONS.md)
- What changed and how it was verified: [MAINTENANCE_REPORT.md](../MAINTENANCE_REPORT.md)

## Code map

| File | What it holds |
| --- | --- |
| `hivemind_telegram_bridge/__init__.py` | `HiveMindTelegramBridge`: connection lifecycle, `addressed_text` (mention gating), `_is_stale`, `session_id`, `ready`, reply path (`speak`, `_send_in_order`), waiting for the hub (`_expect_answer`, `_wait_for_answer`, typing); `TokenRejected`, `report_token_rejected`, `_TokenWatch` (401 seen at any time) |
| `hivemind_telegram_bridge/lines.py` | `bridge_line`: the bridge's own lines (no answer, cannot answer now) per language |
| `hivemind_telegram_bridge/split.py` | `split_message`: a long reply as several messages, every character kept |
| `hivemind_telegram_bridge/__main__.py` | `resolve_options`: flags, environment variables, `*_FILE` secrets; `main` turns a rejected token into `token_rejected` and exit status 3 |
| `tests/conftest.py` | `FakeTelegramAPI` (stands in for the Bot API under python-telegram-bot), `FakeHub` (stands in for `HiveMessageBusClient`) |
| `hivemind_telegram_bridge/health.py` | `HealthServer`: `/healthz` and `/readyz` |
| `Dockerfile` | the image: unprivileged user, entrypoint without arguments |
| `.github/workflows/ci.yml` | tests, lint, image smoke test, signed publish to `ghcr.io/thalovant/hivemind-telegram-bridge` |

Related: the HiveMind hub ([HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core)) and client library ([hivemind-bus-client](https://github.com/JarbasHiveMind/hivemind-bus-client)).
