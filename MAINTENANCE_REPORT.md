Last Edit: Claude Opus 5.5 - 2026-11-02 - Motive: Address the CI and documentation review: publish from main only, sign before :latest, pinned audit evidence, qualified claims.

# Maintenance report: hivemind-telegram-bridge

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
