Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: fixes on main, Thalovant CI and signed images, docs set added.

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
| `hivemind_telegram_bridge/__init__.py` | `HiveMindTelegramBridge`: connection lifecycle, `addressed_text` (mention gating), `session_id`, `ready`, reply path |
| `hivemind_telegram_bridge/__main__.py` | `resolve_options`: flags, environment variables, `*_FILE` secrets |
| `hivemind_telegram_bridge/health.py` | `HealthServer`: `/healthz` and `/readyz` |
| `Dockerfile` | the image: unprivileged user, entrypoint without arguments |
| `.github/workflows/ci.yml` | tests, lint, image smoke test, signed publish to `ghcr.io/thalovant/hivemind-telegram-bridge` |

Related: the HiveMind hub ([HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core)) and client library ([hivemind-bus-client](https://github.com/JarbasHiveMind/hivemind-bus-client)).
