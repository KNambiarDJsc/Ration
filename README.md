# RATION

**Spend intelligence where it matters.**

The inference control plane for coding agents.

> Your coding agents have a spending problem.

Developers run Claude Code, Codex, subagents, skills, hooks and tools all day.
RATION makes sure your best inference is spent on the work that actually deserves it.

```text
$ ration status
RATION  Spend intelligence where it matters.

Claude Code
  five_hour  ███████████░░░░░░░  62% used   reset 1h 29m
              burn pressure 0.70 LOW   confidence medium   source provider_cli
  seven_day  █████████████░░░░░  71% used   reset 2d 07h
              burn pressure 1.22 OK   confidence medium   source provider_cli

Reserve      ████░░░░░░░░░░░░░░ 20%
```

## Why RATION?

Not another token dashboard. RATION understands

**TASK VALUE × TASK DIFFICULTY × PROGRESS × BUDGET × TIME TO RESET**

and decides when to spend, save, escalate or stop.

Its objective is not "minimize tokens". It is **maximize verified useful engineering work per unit of scarce inference.**

- **Reset-aware budgets.** 70% of your weekly quota left with a day to go is waste waiting to happen; 20% left with five days to go is a crunch. RATION computes `burn_pressure = observed_burn / (remaining / time_to_reset)` from the official numbers your agents already report.
- **Stuck detection.** Same test failing six times, the same three commands in a loop, files re-read without changes: RATION scores it, explains it, and recommends (or, in guard mode, injects) an intervention.
- **Subagent governance.** Twelve premium subagents for a task that needs three? Observe mode tells you; guard mode denies the spawn with a reason the agent can act on.
- **Progressive escalation.** `ration run` starts cheap when that is likely enough, escalates only when there's no progress, and reserves premium models for high-value work with budget headroom.

## In your Claude Code session (zero extra terminals)

After `ration init`, keep working as usual. RATION speaks up only when it matters, and only to you (tips use the hook `systemMessage` channel and never enter the model's context):

```text
> fix the typo in the README quickstart section please
  RATION: this looks like a small edit: haiku should be enough (you're on claude-opus-5-5).
          `/model haiku` saves premium quota.

> find out why the websocket reconnect logic occasionally deadlocks
  RATION: this looks like hard debugging (race condition, deadlock): consider `/model opus`
          with high effort. A stronger first attempt is usually cheaper than several weak ones.

  RATION: at this pace you'll hit your 5-hour limit in ~40m (it resets in 2h00m).
  RATION: that turn used ~7% of your 5-hour window (3 subagents, 41 tool calls).
  RATION: your weekly window still has 55% left and resets in 9h. Unused quota is lost at reset.

status line:  RATION · 5h 72% (reset 1h59m)  ⚠ 5h limit in ~40m
```

- **Model + effort suggestion** when you submit a task-like prompt, compared with the model you're actually on. It never switches models for you, and it holds back premium suggestions when your window is under pressure.
- **Limit warnings before you hit the wall**, from your real pace (5-hour pace over the last ~30 min; weekly pace over ~17 h, so a burst of work doesn't cry wolf).
- **Turn receipts**: what a heavy turn cost (only when ≥ 2% of the 5-hour window).
- **Stuck-session tip** when you keep prompting an agent that's looping.
- Each tip kind is rate-limited. `ration advisor off` silences all of it; thresholds live under `[advisor]` in `config.toml`.

## Quickstart

```bash
pipx install ration      # or: uv tool install ration
ration init              # detects Claude Code / Codex, asks before changing anything
ration status            # budgets, burn pressure, reset forecast
ration watch             # live progress + stuck detection for running sessions
```

Keep using `claude` and `codex` exactly as before. RATION observes.

```bash
ration plan "find and fix why auth refresh occasionally deadlocks" --priority high
ration run  "fix the flaky checkout test" --check "pytest -q tests/test_checkout.py"
```

## How it works

```
                    RATION (local, SQLite)
                      │
       ┌──────────────┼──────────────┐
   Budget Engine   Scheduler     Progress / Stuck Engine
       └──────────────┼──────────────┘
               Provider Adapters
      ┌───────────────┼────────────────┐
 Claude Code        Codex          Generic CLI / Mock
 hooks + statusline rollout logs   exit codes
 claude -p stream   codex exec --json
```

Three modes:

| Mode | What it does | How |
|---|---|---|
| **Observe** (default) | records sessions, tool calls, tests, subagents, usage; recommends | Claude Code command hooks + statusline; Codex rollout logs |
| **Guard** (`ration guard on`) | denies wasteful subagent spawns, injects stuck warnings | `PreToolUse` deny on the Agent tool; `additionalContext` after tool calls |
| **Schedule** (`ration run`) | picks provider, model class, effort, turns, retries, subagent allowance; escalates; verifies with your checks | `claude -p --output-format stream-json`, `codex exec --json` |

## Claude Code integration

- **Usage comes from the official status line JSON** (`rate_limits.five_hour` / `seven_day`, Pro/Max and gateway spend limits). `ration statusline install` wraps your existing status line and still prints it, so nothing you see changes.
- **Hooks** (`ration hooks install`) are appended next to yours in `~/.claude/settings.json` (backed up first). Uninstall removes only RATION's entries.
- Hooks are deterministic Python (no LLM calls), fail open (an internal error never blocks your session), and take ~80–150 ms per tool call on a Windows laptop (`ration doctor` measures yours).
- RATION never edits `CLAUDE.md`, permissions, auth or sandbox settings, and never uses OAuth tokens or private endpoints.

## Codex integration

- Usage comes from Codex's own rollout logs (`$CODEX_HOME/sessions/**/rollout-*.jsonl`), which record server-reported `rate_limits` (primary/secondary windows). Read-only.
- `ration run --provider codex` uses `codex exec --json`. RATION passes no sandbox or approval flags; your Codex config always wins.
- `ration snippet` prints an optional AGENTS.md fragment. RATION never writes AGENTS.md for you.

## Privacy

All data stays local in SQLite (`~/.config/ration/ration.db`). There is no upload code path; telemetry is off.
`ration privacy` shows exactly what is stored. Stored: event types, tool names, *hashes* of tool inputs, test pass/fail counts, token/cost numbers, usage percentages, file paths (optional). Never stored: source code, diffs, prompts (only their length), full commands, tool output, credentials. `ration privacy --purge` deletes everything.

## Scheduler

Rule-based and explainable (V1 has no RL and no learned routing). Every decision carries its reasons:

```text
Decision  Claude Code · strong model · effort high · max turns 36 · retries 3 · subagents 4 · STRONG_FIRST
  reasons: ["high_priority_task", "hard_debugging", "route_claude", "claude_budget_unknown"]
```

Policies: `CHEAP_FIRST` (routine work), `STRONG_FIRST` (high-risk or hard debugging/architecture/migration), `PREMIUM_RESERVED` (premium only when value is high, a cheaper attempt failed or it's urgent, and the budget is above reserve or would otherwise go unused).

## Stuck detection

Nine signals (repeated identical tool calls, repeated command sequences, the same failing test, failed-tool streaks, edit churn, unchanged re-reads, time without progress, compaction without progress, repeated delegation) are combined with a noisy-OR into `stuck ∈ [0,1]`:

| stuck | band | default action |
|---|---|---|
| < 0.35 | healthy | continue |
| 0.35–0.65 | watch | continue, show reasons |
| 0.65–0.80 | intervene | REPLAN / COMPACT_CONTEXT / RESTART_SESSION / REDUCE_PARALLELISM |
| > 0.80 | likely stuck | ESCALATE_MODEL (valuable + budget) · STOP (low value) · ASK_USER |

## Benchmarks

`ration benchmark` runs a reproducible **synthetic** world (100 tasks, 3 providers, identical weekly budgets, 5 seeds). Its parameters live in [`src/ration/sim/bench.py`](src/ration/sim/bench.py).

| policy | resolved /100 | value | units / resolved | stuck units | premium units | wall h | unused at reset | ran out early |
|---|---|---|---|---|---|---|---|---|
| A premium always | 88.0 | 42.9 | 10.08 | 178 | 887 | 30.8 | 23% | 2h |
| B cheapest always | 45.4 | 22.3 | 3.14 | 75 | 0 | 30.1 | 88% | 0h |
| C static routing | 88.0 | 43.3 | 7.43 | 232 | 506 | 24.5 | 43% | 4h |
| **D RATION adaptive** | 81.8 | 43.8 | **5.32** | 74 | 217 | 26.0 | 62% | 0h |
| D− RATION, no stuck interrupt | 82.0 | 44.7 | 6.71 | 217 | 226 | 30.3 | 52% | 0h |

Read this honestly:
- In this world RATION delivers about the same task value as premium-always and static routing (differences between A, C, D and D− are within seed noise) at ~28% lower cost per resolved task than static routing and ~47% lower than premium-always, and it never runs a provider dry. It resolves *fewer* tasks than A/C because it deliberately stops low-value loops.
- Part of D's efficiency comes from one assumption: stuck attempts cost 2× without interruption and 0.6× with it. D− isolates that assumption.
- **This is not evidence about real agents.** Real-task benchmarks (SWE-bench-style) are on the roadmap; until then, treat these numbers as a behaviour check of the policy.

## Architecture

See [docs/architecture.md](docs/architecture.md) and [docs/provider-capabilities.md](docs/provider-capabilities.md).

```
src/ration/
  models.py        provider-neutral events, tasks, policies, usage snapshots
  store.py         SQLite (WAL, versioned migrations)
  budget.py        reset-aware budgeting
  progress.py      explainable progress score + velocity
  stuck.py         stuck score + interventions
  difficulty.py    transparent task heuristics
  scheduler.py     V1 rule-based scheduler
  orchestrator.py  `ration run` loop: decide → run → verify → escalate/stop
  normalize.py     tool calls → events (file reads/changes, test results)
  hooks/           Claude Code hook + statusline entry points (stdlib-only, fast)
  integrations/    settings.json installer (coexists with user hooks)
  providers/       claude, codex, generic CLI, mock
  sim/             benchmark harness
```

## Roadmap

- Real-task benchmark suite (controlled repos with acceptance checks; SWE-bench-style adapter)
- Codex lifecycle hooks (Codex now loads `hooks.json`; wire guard mode once the event schema is stable)
- Gemini CLI and OpenAI-compatible/local adapters
- Optional MCP server (`ration_get_budget`, `ration_request_escalation`, …); the core never depends on it
- Textual dashboard once the core loop is proven
- Learned allocation policies, trained on the state/action/outcome log RATION already records

## Research

Every scheduler decision is logged as **state** (budgets, reset times, task features, attempts, stuck/progress), **action** (provider, model class, effort, parallelism, intervention) and **outcome** (completed, checks, cost, wall time). The open question: *can learned compute-allocation policies beat static routing under real quota constraints?* RATION is built to answer it with data, later.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest
ruff check src tests
```

License: MIT.
