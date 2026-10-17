# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Capacity tip (`[advisor].capacity_tip`, on by default): before a heavy turn, warns if this repo's own median cost for a turn of this type likely won't fit in what's left of the window (or would eat into the reserve), using the same learned per-token rate the turn-receipt already calibrates. About the *next* turn, not a whole multi-turn task — RATION doesn't track task boundaries in interactive sessions, and the tip says so by only ever talking about "a turn like this." Silent without enough history (`[advisor].capacity_min_percent` floor, default 3%) or a calibrated rate yet.

## [0.3.0] - 2026-10-13

First release on PyPI, as **`ration-cli`** (`uv tool install ration-cli`). The name `ration` there belongs to an unrelated package; the command and the import name are still `ration`. If you installed from GitHub earlier, run `uv tool uninstall ration` before installing `ration-cli`.

### Added
- **`ration autopilot`** (also `ration nightshift`): converts idle inference you chose to make available into verified, useful work. It finds tasks from local repository signals, scores them, and runs them one bounded task at a time through the existing `ration run` loop. Weekly quota is protected before the idle 5-hour window is used; if nothing worthwhile, safe and verifiable exists, it stops. Modes `safe`, `build` and `research`; `--dry-run`; `status`, `stop`, `report`. Design: `docs/autopilot-architecture.md`.
  - **Stacked, verified changes.** Each task works in its own git worktree, starting from everything accepted before it, and is compared with a baseline (`SUCCESS`, `IMPROVED`, `NO_CHANGE`, `FAILED`, `REGRESSION`, `BLOCKED`, `UNVERIFIABLE`). Accepted changes are committed under `ration/auto/<run>/`; the run's `accepted` branch holds them all and is verified again as a whole. After every accepted change the repository is re-scanned and the queue rebuilt. Nothing is merged into your branches or pushed, and the checkout is never written to.
  - **Safety that does not rely on the prompt.** No-go rules, a prompt-injection check on task text, mode limits; an isolated worktree, git network transports switched off for the agent, tool denials (Claude Code) and a workspace-write sandbox (Codex); an audit of the real diff (protected paths, dependency manifests and lockfiles, deletions, credentials); a fingerprint of your checkout, branches and remotes around every task.
  - **`.ration/policy.toml`**: add protected paths, deny categories, set the weekly reserve, task cap and preferred provider. Strictly validated; it cannot switch built-in protections off.
  - Retry with evidence: when an attempt breaks a check, the run loop shows the agent the failing output and lets it try once more.
  - Opportunity packs (`developer`, `user` via `.ration/tasks.toml`, `research`) and an entry-point group, `ration.opportunity_sources`, for external ones. Check detection per ecosystem (Python, Node, Go, Rust, Java, make), reusing plain CI commands.
  - Local history per project: cooldowns for rejected work, confidence calibrated by recent outcomes, cost estimates replaced by measured costs.
- **`ration goal`**: a project goal with priorities and constraints in `.ration/goal.toml`, used to rank work.
- In-session tip pointing at `ration autopilot --dry-run` when the 5-hour window is mostly unused, resets soon and the week is healthy (at most once per window; `advisor.autopilot_tip`).
- Claude Code plugin (`claude-plugin/`, marketplace manifest at the repository root) with skills that call the local CLI.
- `[autopilot]` configuration section. Existing config files keep working; missing keys take defaults.
- Project logo (`docs/assets/logo.png`) at the top of the README.

### Docs
- README: `ration governor` was missing from the command table; the configuration example now shows the governor-related advisor keys and `[handoff]`; hook latency is the measured figure; the modes table says which commands need no setup.

### Changed
- `orchestrator.run_checks` accepts an environment, and `orchestrator.execute` accepts `check_env`, `max_attempts` and `baseline_gate`. Defaults are unchanged.
- The Codex adapter passes `--sandbox read-only|workspace-write` when `codex.sandbox` is set and the installed CLI advertises the flag (autopilot sets it for its own runs only; `ration run` still passes no sandbox flag).
- The Claude adapter passes `--disallowedTools` when `claude.disallowed_tools` is set (autopilot sets it for its own runs only).
- Hooks import the advisor, handoff and transcript modules only on the events that use them. Measured on Windows with Python 3.11, both variants interleaved in one environment over 40 pairs: a tool-call hook takes a median 140 ms instead of 166 ms (25 ms less in the median pair, faster in 38 of 40). About 54 ms of either is interpreter start.

### Fixed
- Cancelling a run on Windows (stuck detection, Ctrl-C, autopilot stop) terminated only the `.cmd` shim of an npm-installed agent and left the agent itself running. The whole process tree RATION started is now stopped.
- `ration run` and autopilot on Windows with an npm install of Claude Code: `claude` is then a `.cmd` shim, and cmd.exe cut a multi-line prompt at its first newline, losing the rest of the prompt and every flag after it (including `--output-format stream-json`), so the run reported nothing. The prompt now goes on stdin for batch shims. Found by a real autopilot run.
- The test fixture that copies a prepared git repository failed at random on CI: `git commit` starts background maintenance, and its short-lived `maintenance.lock` could vanish mid-copy. Maintenance is switched off in the fixture and lock files are not copied.
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
