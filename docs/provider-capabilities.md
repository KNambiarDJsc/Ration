# Provider capabilities (Phase 0 research spike)

Date: 2026-10-10. Machine: Windows 11, Claude Code 2.1.295 installed, Codex not installed
(Codex findings come from the `openai/codex` source tree on `main` and its docs).

Rule followed: official mechanisms first; private/unstable surfaces are read-only and never part of
core logic. `ration providers` re-detects features at runtime instead of trusting this table.

## Capability matrix

| Capability | Claude Code | Codex | Used by RATION |
|---|---|---|---|
| Non-interactive run | `claude -p` | `codex exec` | ✓ Schedule mode |
| Machine-readable stream | `--output-format stream-json --verbose` | `--json` (thread.started, turn.completed{usage}, item.completed{command_execution, file_change, mcp_tool_call}, turn.failed, error) | ✓ |
| Model selection | `--model` | `--model` | ✓ |
| Effort | `--effort` (detected) | `-c model_reasoning_effort=…` | ✓ |
| Turn cap | `--max-turns` (detected; not always advertised) | none | ✓ when present; otherwise stuck detection |
| **Subscription usage** | statusLine JSON `rate_limits.five_hour/seven_day.{used_percentage,resets_at}` (Pro/Max, after first API response), `spend_limit` behind a gateway | rollout `token_count` events: `rate_limits.primary/secondary.{used_percent,window_minutes,resets_at}` (older: `resets_in_seconds`) | ✓ official numbers, `source=provider_cli` / `local_log` |
| Token / cost per run | stream `result.total_cost_usd`, `usage` | `turn.completed.usage` | ✓ |
| Lifecycle hooks | 33 events; command hooks with JSON stdin/stdout; `PreToolUse` deny via `hookSpecificOutput.permissionDecision`; `additionalContext`; hooks merge across settings files and run in parallel | `hooks.json` / `[hooks]` in config.toml (PreToolUse, PostToolUse, Stop, …); project hooks only in trusted projects | Claude: ✓ observe + guard. Codex: roadmap |
| Subagent visibility | `PreToolUse` on `Agent` tool (`subagent_type`, `description`, `model`), `SubagentStop`, Agent `tool_response.usage` | collab/sub-agent events in protocol | Claude: ✓ governance |
| Native subagent limits | `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` (default 200), `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` (2.1.212+/2.1.217+) | n/a | RATION adds *budget-aware* limits (premium count, reserve) on top |
| OpenTelemetry | metrics `claude_code.token.usage`, `cost.usage` (estimates), events `api_request`, `tool_result`, … — no quota attributes | `codex-rs/otel` | not used in V1 (needs a collector); candidate for later |
| Session logs | `~/.claude/projects/<proj>/<session>.jsonl` (private format; path given to every hook as `transcript_path`, subagents as `agent_transcript_path`) | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` | Claude: per-turn tokens, model, effort (read-only: `message.usage`/`model`, top-level `effort`; de-duplicated by message id + request id; content never read). Codex: usage + tool events |
| Session cost estimate | statusLine `cost.total_cost_usd` (all users; list-price estimate; resets on `/clear`) | n/a | dollar budgets for API-key users |
| Live effort | statusLine `effort.level` (`low`…`max`), transcript `effort`; changed in-session with `/effort` | `model_reasoning_effort` | effort suggestions |
| Instruction files | CLAUDE.md | AGENTS.md | never written; `ration snippet` prints a fragment |

## What is official vs. estimated

- Claude subscription usage %: **official** (status line), confidence high when fresh.
- Codex usage %: **official values, read from a local log**; confidence decays with age of the last `token_count` (high < 30 min, medium < 6 h, low after).
- Burn rate / pressure: **derived** from snapshots. Window-average fallback lowers confidence.
- Claude OTel/stream `cost_usd`: **estimate** per Anthropic's docs; shown as such.

## Unstable / private surfaces (not in core logic)

- Claude transcript JSONL format (session listing only).
- Codex rollout line schema (`session_meta`, `event_msg`, `response_item`); parsing is defensive and tolerant of unknown types.
- Undocumented usage endpoints (e.g. OAuth "usage" APIs used by some menu-bar tools): **not used**. Using subscription OAuth tokens outside the official clients conflicts with Anthropic's consumer terms; RATION never touches credentials.

## Assumptions likely to fail

1. `rate_limits` is absent for API-key users and for the first seconds of a session. RATION shows "no usage data" rather than guessing.
2. Codex rollout schema changes. Parsers ignore unknown lines; tests pin the current shape.
3. `--max-turns` / `--effort` flags vary by Claude version: detected from `--help`, never assumed.
4. Hook latency (~80–150 ms/tool call on Windows, Python cold start) may be noticeable for some users. Mitigation path: a long-lived local daemon with a tiny client, only if measurements justify it.
5. Test-output parsing is heuristic (pytest/jest/vitest/mocha/go/cargo patterns). Unknown runners fall back to exit code + a failure-text signature.
6. Stuck thresholds are hand-set. They are logged with outcomes so they can be calibrated from real data.
