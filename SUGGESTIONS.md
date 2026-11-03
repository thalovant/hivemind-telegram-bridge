Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Polish pass: reply-off-the-bus-thread and localized lines done; hub refusal context, /start, edits and termination reasons proposed.

# Suggestions: hivemind-telegram-bridge

## Per-person rate limit in the bridge

- **Problem:** AUDIT.md section 2: where the hub sets no limit, one person can flood it.
- **Proposal:** a token bucket per (chat, person) in `HiveMindTelegramBridge.forward_to_hivemind`, answering with the "cannot answer right now" line (already rate limited per person, `UNAVAILABLE_EVERY`), configurable by flag and environment.
- **Impact:** high for a shared chat on a hub without its own limit; small change.

## The hub echoes the refused message's context

- **Problem:** AUDIT.md section 13: `hive.policy.denied` names no chat, so with several questions waiting the person learns of a refusal only at the reply timeout.
- **Proposal:** HiveMind-core adds the refused message's `context` (or just its session id) to `hive.policy.denied`; the bridge then ends exactly that wait (`_give_up`).
- **Impact:** medium; immediate feedback on quota or rate refusals in busy chats.

## Answer `/start`

- **Problem:** AUDIT.md section 16.
- **Proposal:** a `CommandHandler("start")` that says one short, localized line ("Ask me anything.") from `lines.py`, subject to the same allow-list and mention rules.
- **Impact:** low; first contact feels alive.

## Decide on edited messages

- **Problem:** AUDIT.md section 15.
- **Proposal:** either `~filters.UpdateType.EDITED` (an edit is never a new question) or keep the current behaviour and say so in the README.
- **Impact:** low.

## More termination reasons

- **Problem:** only a rejected token is told apart from a crash.
- **Proposal:** the same termination-log mechanism for a hub that refuses the bridge's credentials (`hub_rejected`) once hivemind-bus-client raises a distinct error for it.
- **Impact:** medium; the platform can say "replace the hub key" instead of restarting.

## Drain on SIGTERM

- **Problem:** AUDIT.md section 19: replies being sent are drained on `stop()`, but a container stop sends SIGTERM, which ends the process at once.
- **Proposal:** in `__main__.main`, turn SIGTERM into `bridge.stop()` (a signal handler that calls it from the main thread), so the 5 s drain and the final getUpdates confirmation run on every stop.
- **Impact:** low to medium; replies in flight during a rollout are not cut.

## Reproducible images

- **Problem:** AUDIT.md sections 6 and 7.
- **Proposal:** pin the base image by digest and build from a constraints file generated in CI; Renovate updates both.
- **Impact:** medium; makes a rollback mean something.

## Platform integration

- **Proposal:** run the image as the managed bridge workload described in the Thalovant bridges plan: a platform-set connection kind, credentials and identity from a Secret, `--session-scope person`, `--health-port` for probes, allow-list always set, `terminationMessagePolicy` reading `/dev/termination-log`.
- **Impact:** high; this is what the fork is for.

## Done

- Reply off the bus thread (2026-11-03): AUDIT.md section 4.
- Localised fallback line (2026-11-03): AUDIT.md section 3.
