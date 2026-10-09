# Architecture

## Core loop

```
agent activity ──► hooks / statusline / logs ──► normalize ──► SQLite (events, usage)
                                                                  │
                         budget engine ◄── usage snapshots ◄──────┤
                         progress + stuck ◄── events ◄────────────┘
                                   │
                              scheduler (rules + reasons)
                                   │
            observe: recommend · guard: deny/inject · schedule: run/escalate/stop
```

- **Events** (`models.AgentEvent`): `timestamp, provider, session_id, run_id, task_id, event_type, metadata`. Event types follow the spec (SESSION_STARTED … TASK_COMPLETED, plus BUDGET_WARNING, STUCK_DETECTED, MODEL_ESCALATED, RUN_STOPPED, POLICY_CHANGED, GUARD_BLOCKED).
- **Usage** (`models.UsageSnapshot`): `provider, window, resource_type, used_percent, reset_at, window_minutes, source, confidence, captured_at`. `source` ∈ official_api / provider_cli / local_log / gateway / manual / estimated, and the CLI always shows it.
- **Storage**: one SQLite file, WAL + busy_timeout (hooks run in parallel), `PRAGMA user_version` migrations. Tables: providers, usage_snapshots, budget_windows, tasks, runs, agent_events, scheduler_decisions (state/action/outcome JSON), progress_events, interventions, task_outcomes, workflow_edges.

## Where the implementation deliberately differs from the original spec

| Spec | V1 | Why |
|---|---|---|
| `rationd` daemon | none | Hooks, the statusline and CLI write to the same WAL SQLite file. A daemon adds install/upgrade/lifecycle cost for no V1 benefit. Revisit only if hook latency measurements demand a warm process. |
| Async `AgentProvider` Protocol | synchronous Protocol, `on_event` callback returning `False` = cancel | Every adapter is "spawn a CLI, read JSONL". A blocking reader is simpler and fully testable. |
| Usage "estimates" | official numbers first | Both Claude Code (status line) and Codex (rollout `token_count`) already expose server-reported quota percentages and reset times. Estimation is the fallback, never the default. |
| Hook-enforced subagent caps | budget-aware caps only | Claude Code ≥2.1.217 has native spawn caps (`CLAUDE_CODE_MAX_*`). RATION adds what those can't know: premium count, reserve, burn pressure. |
| MCP server | not built | Optional by design; nothing in the core depends on it. |
| TUI (Textual) | `ration watch` with Rich Live | The spec says no fancy UI until the core works. |
| Codex guard mode | observe only | Codex hooks exist but the event schema is newer than Claude's; wire it once stable. |

## Not built yet (on purpose)

SaaS/cloud/team features, RL or learned routing, LLM-based judgments inside hooks, auto-editing of CLAUDE.md/AGENTS.md, anything that touches credentials or bypasses quotas, cross-agent DAG optimization beyond recording `workflow_edges`.
