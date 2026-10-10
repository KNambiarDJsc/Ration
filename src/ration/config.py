"""Configuration: ~/.config/ration/config.toml plus per-project .ration.toml.

Stdlib-only (tomllib) so hooks can load it cheaply.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "budget": {
        # For API-key users (no subscription windows): dollar budgets built from
        # Claude Code's own session cost estimate. 0 = off.
        "daily_usd": 0,
        "weekly_usd": 0,
        "reserve_percent": 20,
        # burn_pressure above this -> CONSERVATIVE
        "conservative_pressure": 1.25,
        # burn_pressure below this, late in the window, with backlog -> AGGRESSIVE
        "aggressive_pressure": 0.75,
    },
    "scheduler": {
        "policy": "adaptive",
        "max_parallel_agents": 4,
        "default_max_turns": 30,
    },
    "guard": {
        # "observe": record + recommend only. "guard": hooks may block/inject.
        "mode": "observe",
        "max_subagents_per_task": 6,
        "max_premium_subagents": 2,
        "subagent_window_minutes": 30,
        "require_explicit_model": False,
        "inject_stuck_context": True,
    },
    "advisor": {
        # In-flow tips shown to the developer (never to the model). Advisory only.
        "enabled": True,
        "model_suggestions": True,
        "budget_alerts": True,
        "turn_receipts": True,
        "resume_tip": True,  # say what resuming an expired-cache session will rebuild
        # Suggest a restart kit when a session's context passes this many tokens (0 = off).
        "context_tip_tokens": 150000,
        # Only show a per-turn receipt when a turn used at least this much.
        "receipt_min_percent": 2.0,  # of the 5-hour window (subscriptions)
        "receipt_min_usd": 0.25,  # API-key users
        "receipt_min_mtokens": 1.0,  # weighted million tokens, when no % or $ is known
        # Don't repeat the same kind of tip in a session within this many minutes.
        "quiet_minutes": 20,
    },
    "handoff": {
        # When true, `/clear` carries a restart kit into the new session automatically.
        # Off by default: it adds text to the model's context, so it is opt-in.
        "auto": False,
        "max_chars": 3000,
        "ttl_minutes": 30,
    },
    "claude": {
        "enabled": True,
        "cheap": "haiku",
        "strong": "sonnet",
        "premium": "opus",
        "cheap_markers": "haiku",
        "premium_markers": "opus",
        "chained_statusline": "",
    },
    "codex": {
        "enabled": True,
        # Empty string = let Codex use its configured default model.
        "cheap": "",
        "strong": "",
        "premium": "",
        "home": "",
    },
    "privacy": {
        "telemetry": False,
        "store_paths": True,
    },
}


def ration_home() -> Path:
    env = os.environ.get("RATION_HOME")
    if env:
        return Path(env)
    return Path.home() / ".config" / "ration"


def config_path() -> Path:
    return ration_home() / "config.toml"


def db_path() -> Path:
    return ration_home() / "ration.db"


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_project_config(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        cand = d / ".ration.toml"
        if cand.is_file():
            return cand
        if (d / ".git").exists():
            return None
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load(cwd: Path | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    p = config_path()
    if p.is_file():
        cfg = _merge(cfg, _read_toml(p))
    proj = find_project_config(cwd)
    if proj:
        cfg = _merge(cfg, _read_toml(proj))
    return cfg


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dumps(cfg: dict[str, Any]) -> str:
    lines: list[str] = []
    for section, values in cfg.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for k, v in values.items():
            lines.append(f"{k} = {_fmt(v)}")
        lines.append("")
    return "\n".join(lines)


def save(cfg: dict[str, Any]) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(cfg), encoding="utf-8")
    return p


def set_value(section: str, key: str, value: Any) -> None:
    """Persist one value into the user config, keeping everything else."""
    p = config_path()
    current = _read_toml(p) if p.is_file() else {}
    current.setdefault(section, {})[key] = value
    save(current)
