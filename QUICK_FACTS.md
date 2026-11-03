Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: rejected token reported, Telegram and hub never wait for each other, stale messages, localized lines, typing..., long replies, flood control.

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
| Tests | `tests/`, 143 tests (`python -m pytest tests -q`); `tests/conftest.py` has a fake Bot API and a fake hub |
| HiveMind message types | sends `recognizer_loop:utterance`; handles `speak`, `complete_intent_failure` (and the older `hive.complete_intent_failure`), `ovos.utterance.handled`, `hive.policy.denied` |
| Rejected token | writes `token_rejected` to `/dev/termination-log`, exit status 3 (`TokenRejected`, `report_token_rejected`) |
| Bridge's own lines | `hivemind_telegram_bridge/lines.py`: en, fr, es, de, it, pt, nl; the asker's `language_code` (kept per chat and person), then `HIVEMIND_LANG`, then English |
| Limits | messages older than 120 s not answered (`MAX_MESSAGE_AGE`); reply timeout 60 s per question (`REPLY_TIMEOUT`, one wait per question); typing renewed every 4 s; replies split at 4096 UTF-16 units (`split.py`); 429 waited out up to 120 s and 10 retries per reply, all pieces together (`RETRY_AFTER_BUDGET`, `RETRY_AFTER_MAX_RETRIES`); at most 32 replies queued per chat (`SENDS_PER_CHAT_MAX`, + 8 for the bridge's own lines, `LINES_ROOM`); each utterance carries `bridge_question` in its context (`QUESTION_KEY`); a refusal is pinned only on the one question asked in the last 5 s (`REFUSAL_WINDOW`); replies being sent get 5 s at stop (`SEND_DRAIN`); at most 64 questions queued for the hub (`HUB_BACKLOG_MAX`) |
| License | Apache-2.0 |

## Settings

| Flag | Environment variable |
| --- | --- |
| `--token` | `TELEGRAM_BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN_FILE`) |
| `--allowed-chat` | `TELEGRAM_ALLOWED_CHATS` |
| `--no-require-mention` | `TELEGRAM_REQUIRE_MENTION` |
| `--session-scope` | `TELEGRAM_SESSION_SCOPE` (`chat` or `person`) |
| `--health-port` | `BRIDGE_HEALTH_PORT` |
| `--reply-timeout` | `BRIDGE_REPLY_TIMEOUT` (seconds, default 60, `0` turns waiting off) |
| `--access-key` | `HIVEMIND_ACCESS_KEY` (or `_FILE`) |
| `--password` | `HIVEMIND_PASSWORD` (or `_FILE`) |
| `--host`, `--port` | `HIVEMIND_HOST`, `HIVEMIND_PORT` |
| `--site-id`, `--lang`, `--self-signed` | `HIVEMIND_SITE_ID`, `HIVEMIND_LANG`, `HIVEMIND_SELF_SIGNED` |
