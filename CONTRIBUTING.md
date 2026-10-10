# Contributing to RATION

Thanks for helping. RATION is small on purpose; the bar for a change is "does it help developers spend inference better, verifiably?"

## Development setup

```bash
git clone https://github.com/KNambiarDJsc/Ration
cd Ration
uv venv && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
pytest
ruff check src tests
```

To try your working copy inside Claude Code, install it as a tool and re-run init:

```bash
uv tool install --reinstall .
ration init
```

Tests never touch your real `~/.claude` or `~/.config/ration`: `tests/conftest.py` points `RATION_HOME`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` at temp dirs.

## Design rules (please keep these)

1. **Local-first.** No network calls, no telemetry, no upload paths. Data lives in the local SQLite store.
2. **Never store prompt text, code, diffs, full commands or tool output.** Hash tool inputs; store counts and types.
3. **Hooks fail open and stay fast.** A RATION bug must never block a Claude Code session. `ration/hooks/*` and everything they import stays stdlib-only (no `rich`), and `ration doctor` should keep reporting latency well under 500 ms.
4. **Deterministic decisions.** No LLM calls inside hooks or the scheduler. Every decision carries human-readable reasons.
5. **Official surfaces first.** Prefer documented hooks, status line JSON, CLI stream output and provider logs over reverse engineering. Never use credentials, OAuth tokens or private endpoints. Never weaken sandbox/permission settings.
6. **Never present an estimate as exact.** Usage carries a `source` and `confidence`; estimated numbers are labelled.
7. **Advisory by default.** Observe mode must not interfere. Enforcement is opt-in (`ration guard on`).
8. **No claims without measurement.** Benchmarks state their assumptions.

## Where things live

| Area | Files |
|---|---|
| Data model, store, config | `models.py`, `store.py` (add schema changes as a new entry in `MIGRATIONS`), `config.py` |
| Budget / progress / stuck / scheduler | `budget.py`, `progress.py`, `stuck.py`, `scheduler.py`, `difficulty.py` |
| In-session advisor | `advisor.py`, `transcript.py` |
| Claude Code hooks + status line | `hooks/claude.py`, `hooks/statusline.py`, `integrations/claude_install.py` |
| Provider adapters | `providers/` (`claude.py`, `codex.py`, `generic.py`, `mock.py`) |
| `ration run` loop | `orchestrator.py` |
| Benchmark | `sim/bench.py` |

### Adding a provider adapter

Implement the `AgentProvider` protocol in `providers/base.py` (`capabilities`, `models`, `usage`, `sessions`, `run`), map its stream to `AgentEvent`s with `normalize.tool_events`, and register it in `providers/__init__.py`. Provider-specific code must not leak into the scheduler. Add tests that pin the provider's real output format (see `tests/test_providers.py`).

## Pull requests

- One focused change per PR, with tests. `pytest` and `ruff check src tests` must pass (CI runs Linux, macOS and Windows).
- If behaviour changes, update the README and `CHANGELOG.md`.
- If you change a decision rule (scheduler, stuck thresholds, advisor), say what real or simulated evidence motivated it.
- Bug reports from real sessions are gold: include `ration doctor` output and, if relevant, the event sequence from `ration watch` (never paste prompts or code you don't want public).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
