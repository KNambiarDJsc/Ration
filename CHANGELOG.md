# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
