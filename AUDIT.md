Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Thalovant fork set up: fixes on main, Thalovant CI and signed images, docs set added.

# Audit: hivemind-telegram-bridge

Evidence: `python -m pytest tests -q` (44 passed, 2026-11-02, Python 3.12) and reading the code at `main`.

## 1. Every chat is allowed unless an allow-list is set

- **Where:** `hivemind_telegram_bridge/__init__.py:125`.
- **Finding:** with no `--allowed-chat`, any chat the bot is in, and any DM, reaches the hub.
- **Impact:** anyone who can message the bot spends the hub's capacity. A managed deployment should always set the allow-list.

## 2. No per-chat or per-person rate limit

- **Where:** `HiveMindTelegramBridge.forward_to_hivemind` (`hivemind_telegram_bridge/__init__.py:312`).
- **Finding:** every addressed message is forwarded immediately.
- **Impact:** one person can flood the hub.

## 3. The fallback reply is English only

- **Where:** `hivemind_telegram_bridge/__init__.py:353`.
- **Finding:** `hive.complete_intent_failure` always answers "I don't know how to answer that", whatever `--lang` is.

## 4. A reply holds the hub's bus thread for up to 10 s

- **Where:** `hivemind_telegram_bridge/__init__.py:374` (`future.result(timeout=10)`).
- **Finding:** `speak()` waits for the Telegram send on the HiveMind bus thread, so a slow Telegram API delays every other message from the hub.

## 5. Tests do not exercise a live Telegram or hub

- **Where:** `tests/test_bridge.py` (module docstring).
- **Finding:** both sides are mocks; the suite checks wiring and behaviour, not a real handshake or a real Telegram API change.

## 6. The HiveMind client is prerelease-only

- **Where:** `requirements.txt:1` (`hivemind-bus-client>=1.0.13a1`).
- **Finding:** resolves to whatever alpha is newest at build time; images are not reproducible across builds.

## 7. Base image not pinned by digest

- **Where:** `Dockerfile:1` (`FROM python:3.14-slim`).
- **Finding:** each build can pull a different base.
