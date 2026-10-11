# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Hooks import the advisor, handoff and transcript modules only on the events that use them. Measured on Windows with Python 3.11: a tool-call hook does about 76 ms of work instead of about 109 ms, on top of a 50 ms interpreter start.

### Fixed
- A hook payload that starts with a UTF-8 byte order mark (a PowerShell pipe adds one) was logged as a hook error and dropped.
- The daily dollar-budget test depended on the time of day and failed late in the evening. The clock is pinned, and the late-evening case (the budget resets before the pace would use it up, so no alert) is its own test.

## [0.2.0] - 2026-10-11

### Verified in real Claude Code sessions (2.1.296)
- Resume-cost tip: predicted a 43k-token rebuild; Claude Code billed 42,674.
- Compact nudge at a green-test boundary; restart kit delivered to the new session after `/clear`.
- Auto-compaction fires at a configured window; a 100k window came back at 120k (thrash is real); the compaction request is billed as fresh input and is absent from the transcript. The governor's cost model now uses these measurements.

### Fixed
- Governor report used the new setting as the baseline for history recorded under the old one.

### Added
- **`ration governor`**: replays your real sessions under candidate auto-compact windows with a cost model fitted to your own compactions, recommends the window that holds up under pessimistic assumptions (never one that thrashes), and can `apply`/`revert` the official `autoCompactWindow` setting. `ration governor status` verifies the realized before/after. Status-line price tag (`ctx 312k · msg ≈0.2%`) and a "good moment to compact" nudge at green-test boundaries. Design: `docs/design/governor.md`.
- **`ration xray`**: read-only analysis of past Claude Code sessions (main + subagent transcripts, content never read) showing where usage went by token type, model, day, project and session, with ranked, quantified recommendations (long contexts, idle cache rebuilds, subagent fan-out, premium share, one runaway session). Works from day one, no setup.
- **`ration handoff`**: deterministic restart kit (goal, changed files, last test result and failing tests, denied actions, git state, where the session left off) built from a session transcript and the repo, with no model call; `--copy`, `--out`, and opt-in `--auto on` that carries the kit into the new session after `/clear` (temp file, deleted on use or after the TTL). The advisor suggests it when a session's context passes `advisor.context_tip_tokens`.
- **Resume-cost tip**: on resuming a session whose prompt cache has expired, say what the rebuild will cost and suggest `/clear` + summary or `/compact` first.

## [0.1.0] - 2026-10-10

First public release (alpha).

### Added
- **Observe mode** for Claude Code: command hooks record sessions, tool calls, file reads/changes, test results, subagents and compactions into a local SQLite store (WAL, versioned migrations). Hooks are stdlib-only and fail open.
- **Official usage capture**: Claude Code status line `rate_limits` (5-hour / weekly / spend limit), `rate_limit_event` from `claude -p` stream output, Codex rollout `token_count` rate limits. Stale readings are ignored.
- **Reset-aware budget engine**: burn pressure, projected exhaustion, conservative / normal / aggressive modes, per-window pace measurement; dollar budgets for API-key users from Claude Code's own cost estimate.
- **In-session advisor** (shown to the developer only): model and effort suggestions with confidence gating and per-repo history correction, limit warnings, "use it or lose it" tips, stuck-session tips, per-turn receipts with exact transcript token counts and locally calibrated usage estimates.
- **Stuck detection** (9 signals, noisy-OR) and an explainable **progress** score.
- **Guard mode** (opt-in): budget-aware subagent governance and stuck warnings injected into the agent's context.
- **Scheduler v1** (rule-based, explainable) and **`ration run`**: baseline checks before any spend, budget-aware model/effort/retry choice, stuck interruption, verified-progress escalation, permission-wall detection, state/action/outcome research logging.
- Provider adapters: Claude Code (`claude -p` stream-json, capability detection from `--help`), Codex (`codex exec --json`, rollout logs), generic CLI, mock.
- CLI: `init`, `status`, `watch`, `plan`, `run`, `sessions`, `history`, `providers`, `doctor`, `privacy`, `benchmark`, `advisor`, `guard`, `hooks`, `statusline`, `snippet`.
- Reproducible synthetic routing benchmark (premium-always, cheapest-always, static routing, RATION).
