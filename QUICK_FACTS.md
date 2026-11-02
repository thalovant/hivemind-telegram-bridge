Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: fixes on main, Thalovant CI and signed images, docs set added.

# Quick facts: hivemind-telegram-bridge

| Key | Value |
| --- | --- |
| Package (distribution) | `HiveMind-telegram-bridge` |
| Import name | `hivemind_telegram_bridge` |
| Version | `0.1.0` (`hivemind_telegram_bridge/version.py`) |
| Console entry point | `hivemind-telegram-bridge` -> `hivemind_telegram_bridge.__main__:main` |
| Main class | `hivemind_telegram_bridge.HiveMindTelegramBridge` |
| Health server | `hivemind_telegram_bridge.health.HealthServer` (`/healthz`, `/readyz`) |
| Image | `ghcr.io/thalovant/hivemind-telegram-bridge` (`:sha-<commit>` and `:latest`, signed by digest with cosign keyless) |
| Image user | `bridge`, uid 10001; identity in `/home/bridge/.config/hivemind` |
| Default branch | `main` (upstream: `JarbasHiveMind/hivemind-telegram-bridge`, branch `dev`) |
| CI | `.github/workflows/ci.yml`: tests on Python 3.12 and 3.14, ruff `F,E9`, image smoke test, publish on `main` |
| Tests | `tests/`, 44 tests (`python -m pytest tests -q`) |
| HiveMind message types | sends `recognizer_loop:utterance`; handles `speak`, `hive.complete_intent_failure` |
| License | Apache-2.0 |

## Settings

| Flag | Environment variable |
| --- | --- |
| `--token` | `TELEGRAM_BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN_FILE`) |
| `--allowed-chat` | `TELEGRAM_ALLOWED_CHATS` |
| `--no-require-mention` | `TELEGRAM_REQUIRE_MENTION` |
| `--session-scope` | `TELEGRAM_SESSION_SCOPE` (`chat` or `person`) |
| `--health-port` | `BRIDGE_HEALTH_PORT` |
| `--access-key` | `HIVEMIND_ACCESS_KEY` (or `_FILE`) |
| `--password` | `HIVEMIND_PASSWORD` (or `_FILE`) |
| `--host`, `--port` | `HIVEMIND_HOST`, `HIVEMIND_PORT` |
| `--site-id`, `--lang`, `--self-signed` | `HIVEMIND_SITE_ID`, `HIVEMIND_LANG`, `HIVEMIND_SELF_SIGNED` |
