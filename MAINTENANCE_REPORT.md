Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: rejected token reported, Telegram and hub never wait for each other, stale messages, localized lines, typing..., long replies, flood control.

# Maintenance report: hivemind-telegram-bridge

## 2026-11-03: Polish pass (solid, fast)

**Changes** (each its own commit on `polish/solid-fast-replies`, oldest first)

1. A rejected bot token (401 at start or while polling; 404 at start) closes the hub connection, writes `token_rejected` to `/dev/termination-log` and exits with status 3; the token is no longer printed in the start-up traceback, and httpx's URL logging is held at WARNING (`TokenRejected`, `_TokenWatch`, `__main__.main`).
2. Telegram and the hub never wait for each other: `speak()` schedules the send and returns (per-chat order kept), hub sends run on one dedicated thread; a failed hub send no longer logs the person's words.
3. Messages sent more than 120 s before they are read are confirmed and not answered (`_is_stale`).
4. The bridge's own lines in seven languages, from the sender's `language_code`, then `HIVEMIND_LANG` (`lines.py`); `complete_intent_failure`, which current hubs send, is handled.
5. "typing…" while the hub thinks; "cannot answer right now" at the reply timeout (60 s, `BRIDGE_REPLY_TIMEOUT`), or at once when the hub is unreachable or refuses the only waiting question; `ovos.utterance.handled` ends a wait quietly.
6. Long replies split at 4096 UTF-16 units, in order (`split.py`, walked by offset); flood control (429) waited out, up to 120 s and 10 retries per reply.
7. `stop()` after the bridge ended is a no-op; module docstring says which thread does what.
8. The start-up WARNING about `hivemind-core allow-msg` is gone: it was false on a managed hub. The hint is logged once, only when the hub refuses an utterance with `acl_disallowed_type`.
9. Review fixes (automated reviews of both pull requests, threads and review bodies; each listed in AUDIT section 19): lines per person; one wait per question (own timeout, own line; a reply ends the oldest waiting question of its session, an answer with no session the oldest in the chat); late answers end no other waits; `block=False` handlers, 5 s drain on stop, full flood waits within a budget, health port and HTTP clients released on a rejected token, nonsense timings refused; the latency test waits for answered sends. From the Discord pull request's review, applied here too: the hub-send backlog is bounded (64), a question that waited past the reply timeout is not sent, a refusal is not pinned on a wait shared by several questions, stale drops are logged at INFO.
10. Docs: README, QUICK_FACTS, FAQ, AUDIT (sections 3 and 4 fixed, 8-19 added), SUGGESTIONS, docs/index.md, AGENTS.md.

**Rationale:** the owner asked for the live chat bridges to be perfect, solid, fast and polished. The rejected-token state lets the platform ask for a new token instead of restarting forever; the threading change removes head-of-line blocking that grew with every simultaneous person; the rest closes gaps where a person got silence (no-answer never fired, long replies and 429s lost, hub down) or a stale answer.

**Verification**

- `python -m pytest tests -q`: 128 passed on Python 3.14.8 and 3.12 (local venvs). New tests were checked to fail without their change (the old code at `140ca52`, or the change reverted in place): token watch, non-blocking speak, per-chat order, emit text in logs, stale messages, split, the permission hint, and each review fix (`block=False`, ended waits, per-person throttle, drain, timing validation, backlog bound, late-question drop, shared-wait refusal, stale log level, one wait per question, no-session answers, per-person reply matching, refusal window, queue age, per-chat send cap, retry cap, shared flood budget, refused-reply fallback, linear split).
- `ruff check --isolated --select F,E9 .`: clean.
- Latency, `tests/test_latency.py` (fake hub + fake Bot API through the real python-telegram-bot stack; sendMessage 300 ms; second scenario: hub send 250 ms):

  | Measure | Before (`140ca52`) | After |
  | --- | --- | --- |
  | speak -> sendMessage, five replies at once, median | 602.8 ms | 0.1-0.2 ms |
  | speak -> sendMessage, five replies at once, max | 1205.4 ms | 0.4 ms |
  | hub bus thread held per reply | 301.6 ms | 0.1 ms |
  | reply during a slow hub send | 240.4 ms | 0.3-0.5 ms |
  | message read -> `recognizer_loop:utterance` emitted, median | 0.7 ms | 2-3 ms (hop to the hub-send thread) |
  | message read -> "typing…" requested, median | none | 2.9 ms |

- Live check against Telegram's Bot API, not committed as a test: a made-up well-formed token through the real stack gives `TokenRejected` in 1.9 s (401), a malformed one in 0.3 s (404).
- Image built locally with podman: `--help` lists `--reply-timeout`, runs as uid 10001, refuses to start without a token and names `TELEGRAM_BOT_TOKEN`, the package imports on a read-only root filesystem.
- Not verified: a live Telegram bot with a real token, a live hub, CI on GitHub for this branch (runs on the pull request).

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** wrote the code changes, the fakes in `tests/conftest.py` and every new test; measured latency on the old and new code; updated these documents.
- **Oversight:** directed by the repository owner through an orchestrating agent; no human code review yet; the pull request is not merged.

## 2026-11-02: Thalovant fork

**Changes** (each its own commit on `main`, oldest first)

1. Settings from the environment and `*_FILE` secret files; the image starts the bridge directly as uid 10001 with no secret in its arguments.
2. Mention gating in chats with the mention stripped before forwarding; DMs unchanged.
3. Optional per-person hub sessions (`--session-scope person`).
4. Reply text no longer logged at debug level.
5. Optional `/healthz` and `/readyz` (`--health-port`).
6. Inherited upstream workflows (release, publish, image push to upstream's registry) removed; `.github/workflows/ci.yml` added (tests, lint, image smoke test; on `main` only, the image is pushed under `:sha-<commit>`, signed and verified by digest, and only then tagged `:latest`); docs set added.

**Rationale:** run the bridge as a managed workload: no secrets in process arguments, no message text in logs, no forwarding of conversations not addressed to the bot while mention gating is on (the default; `--no-require-mention` forwards every message the bot receives), and probes a platform can use.

**Verification**

- `python -m pytest tests -q`: 44 passed (Python 3.12, local). Every change has tests that fail with the change reverted and pass with it.
- `ruff check --isolated --select F,E9 .`: clean.
- Image built locally with podman: `--help` answers, runs as uid 10001, refuses to start without a token and names `TELEGRAM_BOT_TOKEN`.
- Not verified: CI on GitHub (the fork's Actions need enabling once in the Actions tab), a live Telegram bot, a live hub.

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** wrote the five code changes and their tests, the CI workflow, the Dockerfile changes and these documents; ran the tests and the local image build.
- **Oversight:** directed by the repository owner (fork, fix, own CI and registry); no human code review yet.
