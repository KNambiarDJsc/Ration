# Autopilot architecture: before and after

Autopilot (also `ration nightshift`) converts idle inference you chose to make available into verified, useful work. Two rules shape it:

1. **Autonomous work must have positive expected value.** If nothing worthwhile, safe and verifiable is found, it stops.
2. **Protect scarce long-term quota before harvesting short-term idle capacity.** The weekly window outranks the 5-hour one.

It does not exist to use up quota.

## Before (v0.2)

RATION observed sessions, advised you, and could run one task you described.

```
You, in a Claude Code session                You, in a terminal
        │                                            │
 hooks + status line                        ration run "<task>" --check "<cmd>"
        │                                            │
 SQLite: events, usage readings ───────────► budget engine (burn pressure, reset-aware)
        │                                            │
 advisor: tips, receipts                    scheduler (provider, model tier, effort, retries)
                                                     │
                                            Claude Code / Codex   ◄── stuck detection cancels loops
                                                     │
                                            acceptance checks pass?  → done / escalate / stop
```

It could not find work by itself, say whether spare capacity was safe to use, express "better than before" (only "all checks pass"), or
change code anywhere but your working tree. The budget engine had an `AGGRESSIVE` mode for unused quota, but it needed a backlog of useful
work and nothing produced one.

## After

Autopilot is a planner on top of the same run loop. It adds no second runner, scheduler or budget meter.

```
Project goal + policy (.ration/) + repository + official usage readings
        │
 Opportunity discovery      local signals: failing tests, lint, TODOs, untested modules, backlog files…
        │
 Scoring                    usefulness · verifiability · confidence · goal alignment · safety · cost → 0-100
        │
 Safety policy              mode, no-go rules, injection check, protected paths, your policy and constraints
        │
 Budget allowance           weekly reserve and your own projected use first, then the idle 5-hour window
        │
 Queue                      best first, within the allowance
        │
 ┌──────┴───── for each task ───────────────────────────────────────────────────────────┐
 │ worktree at the CANDIDATE (base + everything accepted so far) → baseline checks        │
 │ → existing `ration run` loop: scheduler picks provider/model/effort, stuck detection   │
 │   cancels loops, a failed check is retried once with its output                        │
 │ → audit the real diff → re-run checks → compare with the baseline                      │
 │ → accept: commit, the candidate advances        reject: discard, keep the evidence     │
 │ → after an accepted change: re-scan the candidate and rebuild the queue                │
 └────────────────────────────────────────────────────────────────────────────────────────┘
        │
 Combined verification      everything accepted, checked together in a clean checkout
        │
 Report + history           cooldowns, calibration, measured costs for next time
```

## Components

Everything new is in `src/ration/autopilot/`, stdlib-only (the in-session advisor imports the budget part on the hook path).

| Module | Role |
|---|---|
| `goal.py` | Goal, priorities and constraints in `.ration/goal.toml`. Scores alignment; detects "don't …" conflicts. |
| `discovery.py` | Opportunity sources grouped into packs (`developer`, `user`, `research`, `plugin`). |
| `scoring.py` | Deterministic score with reasons; `decide` (non-budget gates) and `select` (the queue). |
| `policy.py` | Modes, no-go rules, injection patterns, protected paths, agent rules, diff audit, `.ration/policy.toml`. |
| `budget.py` | `Allowance`: how much is safe to spend now, built on the existing `WindowBudget`. |
| `verify.py` | Detects the project's checks (one detector per ecosystem, CI reuse), runs them, compares, redacts. |
| `workspace.py` | Worktrees and branches, the candidate, repository fingerprint, git network switch. |
| `state.py` | Run record, single-run lock, stop requests, history. |
| `engine.py` | `make_plan` (read-only) and `run`. Calls `orchestrator.execute` for every task. |
| `report.py`, `commands.py` | Plain-text plan/status/report; `ration goal`, `ration autopilot`, `ration nightshift`. |

Changes to existing code are small and backward compatible: `run_checks(env=)`, `execute(check_env=, max_attempts=, baseline_gate=)`,
optional `--disallowedTools` (Claude) and `--sandbox` (Codex) in the adapters, one advisor tip, the CLI commands, config defaults. Two
fixes help `ration run` as well: the prompt goes on stdin for a Windows `.cmd` shim, and cancelling stops the whole process tree there.

## Budget protection

`budget.assess()` returns an allowance in percent of the 5-hour window, or a refusal with the reason.

```
weekly spendable = weekly remaining
                   − max(weekly reserve, your own projected use until the weekly reset)
                   − uncertainty margin (larger when readings are stale or low-confidence)
                   capped at max_weekly_percent_per_run, minus what this run already used
short spendable  = 5-hour remaining − window_reserve_percent
allowance        = min(short spendable, weekly spendable ÷ weekly-cost-of-1%-of-the-5-hour-window)
```

| Situation | Result |
|---|---|
| 5-hour window nearly unused, week at or below the reserve | nothing runs |
| 5-hour window about to reset | no extra allowance; an expiring window is not a reason to spend the week |
| your own pace already exhausts the week | nothing runs |
| weekly reset within 24 hours | the reserve relaxes linearly, never below a quarter of what you set |
| no usage data | nothing runs unless you pass `--max-tasks N`, which becomes the budget; dry runs always work |
| readings older than 30 minutes | wider margin, confidence "low" |

The budget is re-read before every task, so the previous task's real cost is in it. If a task used more than 1.5 times its estimate, the
remaining estimates are scaled up for the rest of the run. Autopilot stops when the week falls to the reserve; a task already running can
overshoot an estimate, and the report says so when the run ends below the reserve.

What is exact: usage percentages and reset times are official readings (Claude Code's status line, or the rate-limit events in its own
output). What is estimated, and labelled so: the weekly cost of the 5-hour window (measured from your readings once there is enough
history, otherwise assumed 0.12) and the cost of a task (assumed per size until three tasks of that kind were measured here). RATION has
no token-level accounting for subscription quota and does not pretend to.

## Discovery and scoring

Discovery is local and deterministic. No model is asked what to do next.

| Pack | Sources |
|---|---|
| `developer` | failing tests, lint findings, type errors (run read-only); TODO/FIXME markers; unfinished stubs; skipped tests; untested modules; swallowed errors; very long functions; missing docs; unchecked items in `TODO.md`/`ROADMAP.md`; README roadmap entries (low confidence: an intention is not a task) |
| `user` | tasks you define in `.ration/tasks.toml` |
| `research` | read-only analyses: goal gap, technical debt, test gaps, architecture map, dependency review |
| `plugin` | sources from other packages via the `ration.opportunity_sources` entry-point group |

Descriptions name files and line numbers and never quote code.

```
score = 100 × usefulness^.30 × verifiability^.20 × confidence^.15 × alignment^.15 × safety^.10 × cheapness^.10
```

A weighted geometric mean, so one weak factor cannot be bought back by a strong one. Each task ends as `RUN`, `DEFER` (budget), `SKIP`
(low value, cooling down, touches your uncommitted files, may break a constraint), `REJECT` (policy) or, later in a run, `DROPPED` (no
longer applies after an accepted change), with the deciding reason first.

Learning is per project and deliberately modest: a category needs three outcomes in the last 60 days before it moves confidence (within
0.6x to 1.4x) or replaces a cost estimate. A task that was not accepted is not retried for 7 days; one that was accepted is not redone
while its branch awaits review.

## Safety model

Prompts are not a safety mechanism. The rules are enforced in layers that do not depend on the agent obeying them.

1. **Before selection.** No-go rules reject deployments, pushes, destructive database work, schema changes, secrets, infrastructure, large
   rewrites, major dependency upgrades and external-account actions in every mode. Task text that reads like an instruction to the agent
   ("ignore the rules", "upload .env to …", `curl …`) is rejected as prompt injection. Modes bound category, risk and size:

   | Mode | Allowed | Ceilings |
   |---|---|---|
   | `safe` (default) | tests, lint, types, docs, TODOs, error handling, small refactors, your own tasks | low/medium risk, size ≤ M, ≤ 12 files, no deleted files |
   | `build` | the above, plus backlog items and unfinished code | medium risk, size ≤ L, ≤ 40 files |
   | `research` | read-only analysis written to local reports | no source changes; `plan` permission mode |

2. **While the agent runs.**
   - It works in a worktree outside your checkout, with `acceptEdits` or `plan`, never `bypassPermissions`.
   - Every git process it starts inherits `protocol.allow=never`: push, fetch and clone fail inside git itself, by any route.
   - Claude Code gets `--disallowedTools` for pushes, `gh`, publishing, cloud and infrastructure commands, `curl`/`wget` and web tools.
   - Codex gets `--sandbox workspace-write` (its worktree only), `read-only` in research mode.
   - The prompt has three labelled parts: RATION's rules (authoritative), the task, and reference context. The rules state that
     everything in the repository, including comments, READMEs and the task text, is data and cannot change them.
   - Time limits per task and per run; the existing stuck detector; `ration autopilot stop`.

3. **After the agent finished.** The actual diff is audited: protected paths (`.env`, keys, migrations, CI workflows, infrastructure,
   `.ration.toml`, git hooks), dependency manifests and lockfiles, file and deletion limits, and a credential scan. A violation discards
   the change. Your checkout, branches, tags, stashes, remote-tracking refs and remote configuration are fingerprinted around every task;
   any difference stops the run and reverts nothing.

**Your policy file.** `.ration/policy.toml` can add protected paths, deny or narrow categories, set the weekly reserve, the task cap and
the preferred provider, and allow dependency or lockfile changes. It cannot widen a mode, remove a built-in protected path or switch off
the no-go rules. It is validated strictly: an unknown key or a wrong type stops autopilot instead of being half applied.

**Trust boundaries.**
- Check commands (detected, or configured under `[autopilot]`) are shell command lines that run the project's own code, as with
  `ration run --check`. Planning with probes runs your tests and linter too. Use `--no-probe` to plan on a repository you do not trust.
- A CI step is reused only when it is a plain invocation: text from a workflow file never becomes shell syntax.
- `.ration/` is your local folder. A `tasks.toml` or `policy.toml` that is tracked by git is repository content and is ignored.
- Starting a run always needs an explicit yes. The in-session tip and the plugin only ever show the plan.

## Execution and verification

| Outcome | Meaning | Kept? |
|---|---|---|
| `SUCCESS` | the target is met and nothing got worse | yes |
| `IMPROVED` | measurably better, not fully there | yes |
| `NO_CHANGE` | nothing changed, or it already passed where the task started (nothing spent) | no |
| `FAILED` | changed, but the target is no better | no |
| `REGRESSION` | any check is worse than its baseline | no |
| `BLOCKED` | check cannot run, permission wall, cancelled, or discarded by the safety policy | no |
| `UNVERIFIABLE` | nothing can confirm the change | no |

- **Baseline first.** Checks run on the commit the task starts from, once per commit. Test counts are read from the runner's closing
  summary only, because test output can contain text that looks like a summary.
- **Retry with evidence.** The checks that pass at the start are the run loop's acceptance checks. If an attempt breaks one, the existing
  loop shows the agent the failing output and lets it try once more in the same worktree. An agent that cannot run the tests itself
  still gets to fix them.
- **A cancelled task is never accepted.** A stop request, time limit or Ctrl-C leaves half a change; it is discarded, and no retry starts.
- **Tasks without a measurable target** (a TODO, a docstring) are only accepted when guard checks exist and hold, and the report says the
  change itself was not independently verified.
- **Combined verification.** When the run ends, everything accepted is checked again as a whole, in a clean checkout, against the base
  commit. A run whose accepted changes do not hold together is reported as failed, with "do not merge".
- The scheduler's own success statistics are corrected when autopilot rejects a result the run loop called "succeeded".

## Git isolation

```
your checkout (any branch, detached, clean or dirty)   never written to
~/.config/ration/worktrees/<repo>/<run>/<task>         where the agent works
branch ration/auto/<run>/<nn-task>                     one per accepted task, stacked
branch ration/auto/<run>/accepted                      everything accepted in the run
```

Accepted changes are stacked: each task starts from the candidate, so task B is verified on top of task A, and a change that only fails
in combination is rejected when it is made. A rejected task leaves no trace in the candidate. After every accepted change the candidate
is re-scanned and the queue rebuilt, so a task planned for an older state is dropped rather than executed blindly, and work the change
revealed can join.

Uncommitted work in your checkout is neither included nor touched, and tasks that target files you have modified are skipped. A branch or
directory name that already exists is never reused. Commits use your git identity and run your commit hooks; with no identity configured
at all they are authored as "RATION Autopilot". If a commit is refused, the verified change stays in its worktree and the run stops.
Nothing is merged into your branches, and nothing is pushed.

## State and reports

```
<project>/.ration/            ignores itself in git
  goal.toml   policy.toml   tasks.toml
  autopilot/lock.json         one run per project
  autopilot/history.json      outcomes, for cooldowns, calibration and cost estimates
  autopilot/runs/<id>/        state.json, report.md, research/, rejected/
```

State is rewritten atomically after every step. The lock belongs to a process: if that process is gone, or its run has written nothing
for six hours, the lock is stale and nobody stays locked out. A run whose process died is shown as `interrupted`; the next run removes
its leftovers and keeps every branch and worktree that holds accepted work. A damaged state file never causes a branch to be deleted. An
internal error ends in a truthful record, a released lock and no leftovers, not a traceback. There is no `resume`: a new run rediscovers
the remaining work from the repository as it now is.

The report contains only measured things: check results before and after, files and lines, what each attempt used, official usage
readings. Estimates are labelled. There are no "hours saved".

## Claude Code integration

- **Hooks** (unchanged install path, `ration init`): after a turn, when most of the 5-hour window is unused, it resets within two hours
  and the week is healthy, RATION shows one line pointing at `ration autopilot --dry-run`. At most once per window.
- **Plugin** (`claude-plugin/`, marketplace manifest at the repository root): four skills, `/ration:autopilot`, `/ration:report`,
  `/ration:status`, `/ration:goal`, each a thin call to the local CLI. Only you can invoke them, the autopilot skill is allowed to run the
  dry run only, and the plugin ships no hooks.

Why the plugin does not replace `ration init`: plugin settings only honour `agent` and `subagentStatusLine`, so a plugin cannot install
the status line that supplies RATION's official usage readings, and hooks need the interpreter RATION is installed in.

## Commands

```bash
ration goal set "Ship the MVP" --priority "finish authentication" --constraint "don't change the database schema"
ration autopilot --dry-run                 # the plan and the reasons; starts no agent, changes nothing
ration autopilot --mode safe --max-tasks 3 # asks before starting
ration autopilot status
ration autopilot report                    # then: git diff <your-branch>...ration/auto/<run>/accepted
ration autopilot stop                      # from another terminal

ration autopilot --mode build | --mode research
ration autopilot --reserve-weekly 50 --max-runtime 120 --provider claude --yes
ration nightshift …                        # the same command under its other name
```

Configuration: `[autopilot]` in `~/.config/ration/config.toml` or a project's `.ration.toml`; policy in `.ration/policy.toml`:

```toml
[autopilot]
weekly_reserve = 50
max_tasks = 3

[safety]
protected_paths = ["infra/", "billing/"]
denied_categories = ["refactor"]
allow_dependency_changes = false

[preferences]
preferred_provider = "auto"
```

## Limitations

- Verified with a scripted agent, and with real Claude Code tasks on Windows (2.1.296, haiku) including a run on RATION's own repository.
  Long multi-hour runs have not been measured.
- **Codex is untested live.** The adapter passes `--sandbox workspace-write` only when `codex exec --help` advertises the flag; it has
  never been run against an installed Codex. Without that flag Codex is not offered code tasks at all.
- With `acceptEdits`, the agent can only run shell commands your own Claude Code permissions allow. The retry with failing output covers
  the common case; an agent that could run the tests itself would need fewer attempts.
- The tool denials are Claude Code's; the git network switch covers git only. A generic provider that grants itself arbitrary shell and
  network access is not contained by RATION beyond the worktree and the diff audit.
- The repository fingerprint cannot tell your own concurrent work from an agent escaping its worktree, so working in the checkout during
  a run stops the run.
- A fresh worktree has no untracked build artefacts. Set `autopilot.setup_command` (for example `npm ci`).
- Check detection covers Python, Node, Go, Rust, Java (Maven/Gradle) and `make test`; CI reuse covers GitHub Actions for Python.
- Goal alignment, constraint matching and the injection check are pattern-based. They err toward skipping.
- A check that is running cannot be interrupted by `stop`; it ends at its timeout at the latest.
- The suite of real-repository tests is slow on Windows (process spawning). `pytest -m "not integration"` is the quick loop.

## Future ideas

1. Let the agent run the project's own check commands in its worktree (an explicit, narrow allow-list), to cut retries.
2. A scheduled, still opt-in start ("when I have been idle for 30 minutes") built on OS schedulers.
3. Coverage-driven test targets where a coverage tool is configured.
4. Feed per-category provider and model results back into the scheduler.
5. Tell concurrent user edits from agent escapes (file-level attribution), so you can keep working during a run.
6. Language packs (accessibility, performance probes) as separate distributions.
7. A review helper that walks the stacked branches task by task.
