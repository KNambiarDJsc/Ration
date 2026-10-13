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

`pytest` runs everything (about 3 minutes on Windows, where starting processes is slow). `pytest -m "not integration"` skips the tests that drive real git repositories and processes and takes about 20 seconds; run the full suite before a pull request.

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
| Autopilot | `autopilot/` (stdlib-only; see `docs/autopilot-architecture.md`) |
| Claude Code plugin | `claude-plugin/`, `.claude-plugin/marketplace.json` (skills that call the CLI; no logic) |

### Adding an opportunity source

Write a function `(RepoContext) -> list[Opportunity]` in `autopilot/discovery.py` and decorate it with `@source("name")`, or ship it in another package under the `ration.opportunity_sources` entry-point group. Point at files and lines; never quote code in a description. A source must be deterministic and must not call a model. Give it a test in `tests/test_autopilot_discovery.py`.

### Adding a provider adapter

Implement the `AgentProvider` protocol in `providers/base.py` (`capabilities`, `models`, `usage`, `sessions`, `run`), map its stream to `AgentEvent`s with `normalize.tool_events`, and register it in `providers/__init__.py`. Provider-specific code must not leak into the scheduler. Add tests that pin the provider's real output format (see `tests/test_providers.py`).

## Pull requests

- One focused change per PR, with tests. `pytest` and `ruff check src tests` must pass (CI runs Linux, macOS and Windows).
- If behaviour changes, update the README and `CHANGELOG.md`.
- If you change a decision rule (scheduler, stuck thresholds, advisor), say what real or simulated evidence motivated it.
- Bug reports from real sessions are gold: include `ration doctor` output and, if relevant, the event sequence from `ration watch` (never paste prompts or code you don't want public).

## Releasing

Maintainers only. Nothing here is automated, on purpose.

1. Set the version in `pyproject.toml`, `src/ration/__init__.py` and `claude-plugin/.claude-plugin/plugin.json`; date the `CHANGELOG.md` section.
2. `pytest`, `ruff check src tests`, `claude plugin validate claude-plugin --strict`, `claude plugin validate . --strict`.
3. `python -m build`, then install the wheel into a clean virtualenv and run `ration --version` and `ration autopilot --dry-run --no-probe`.
   The version is bumped in the release commit, not while a feature is being developed.
4. Merge to `main` and wait for CI to pass on that commit. A PyPI version can never be uploaded twice, so nothing is published from a red build.
5. PyPI: `uv build`, `uvx twine check dist/*`, then `uv publish` with a token in `UV_PUBLISH_TOKEN`. The distribution is `ration-cli` (the name `ration` belongs to an unrelated package); the import name and the `ration` command are unchanged. Check it with `uvx --from ration-cli ration --version`.
6. Tag `vX.Y.Z`, push the tag, and create the GitHub release from the changelog section. Users install with `uv tool install ration-cli`, or from GitHub with `uv tool install git+https://github.com/KNambiarDJsc/Ration@vX.Y.Z`.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
