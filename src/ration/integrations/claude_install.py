"""Install / remove RATION's Claude Code integration in user settings.

Rules (from the spec, enforced here):
- Never touch CLAUDE.md, permissions, auth or sandbox settings.
- Coexist with user hooks: only append RATION entries; uninstall removes only
  entries whose command runs `ration.hooks.*`.
- Back up settings.json before every write; refuse to write if it is not valid
  JSON rather than risk clobbering it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .. import config

HOOK_MODULE = "ration.hooks.claude"
STATUS_MODULE = "ration.hooks.statusline"

# event -> matcher (None = no matcher field)
HOOK_EVENTS: dict[str, str | None] = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "Agent|Task",  # guard only needs subagent spawns
    "PostToolUse": "*",
    "PostToolUseFailure": "*",
    "SubagentStop": None,
    "PreCompact": None,
    "Stop": None,
    "StopFailure": None,
    "SessionEnd": None,
    "PostModelSwitch": None,
}


class SettingsError(RuntimeError):
    pass


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def python_cmd() -> str:
    exe = Path(sys.executable).as_posix()
    return f'"{exe}" -I -m'


def hook_command() -> str:
    return f"{python_cmd()} {HOOK_MODULE}"


def statusline_command() -> str:
    return f"{python_cmd()} {STATUS_MODULE}"


def _is_ours(cmd: Any) -> bool:
    return isinstance(cmd, str) and ("ration.hooks." in cmd)


def load_settings(path: Path | None = None) -> dict[str, Any]:
    p = path or settings_path()
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"{p} is not valid JSON ({e}); not modifying it") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{p} does not contain a JSON object; not modifying it")
    return data


def _write(data: dict[str, Any], path: Path) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.ration-bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(".ration-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def _strip_ours(hooks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        kept = []
        for g in groups:
            if not isinstance(g, dict):
                kept.append(g)
                continue
            inner = [h for h in g.get("hooks", []) if not (isinstance(h, dict) and _is_ours(h.get("command")))]
            if inner:
                kept.append({**g, "hooks": inner})
            elif not g.get("hooks"):
                kept.append(g)
        if kept:
            out[event] = kept
    return out


def install_hooks(path: Path | None = None, timeout: int = 10) -> Path | None:
    p = path or settings_path()
    data = load_settings(p)
    hooks = _strip_ours(data.get("hooks") or {})
    cmd = hook_command()
    for event, matcher in HOOK_EVENTS.items():
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    data["hooks"] = hooks
    return _write(data, p)


def uninstall_hooks(path: Path | None = None) -> Path | None:
    p = path or settings_path()
    data = load_settings(p)
    if "hooks" not in data:
        return None
    data["hooks"] = _strip_ours(data["hooks"])
    if not data["hooks"]:
        del data["hooks"]
    return _write(data, p)


def install_statusline(path: Path | None = None) -> tuple[Path | None, str]:
    """Wrap the status line. Returns (backup, chained_command)."""
    p = path or settings_path()
    data = load_settings(p)
    existing = data.get("statusLine") or {}
    chained = ""
    if isinstance(existing, dict) and existing.get("command") and not _is_ours(existing.get("command")):
        chained = str(existing["command"])
        config.set_value("claude", "chained_statusline", chained)
    new = {"type": "command", "command": statusline_command()}
    if isinstance(existing, dict) and "padding" in existing:
        new["padding"] = existing["padding"]
    data["statusLine"] = new
    return _write(data, p), chained


def uninstall_statusline(path: Path | None = None) -> Path | None:
    p = path or settings_path()
    data = load_settings(p)
    sl = data.get("statusLine")
    if not (isinstance(sl, dict) and _is_ours(sl.get("command"))):
        return None
    chained = config.load()["claude"].get("chained_statusline", "")
    if chained:
        data["statusLine"] = {**sl, "command": chained}
        config.set_value("claude", "chained_statusline", "")
    else:
        del data["statusLine"]
    return _write(data, p)


def status(path: Path | None = None) -> dict[str, Any]:
    p = path or settings_path()
    try:
        data = load_settings(p)
    except SettingsError as e:
        return {"settings": str(p), "error": str(e), "hooks": [], "statusline": False}
    events = []
    for event, groups in (data.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            if any(_is_ours(h.get("command")) for h in g.get("hooks", []) if isinstance(h, dict)):
                events.append(event)
    sl = data.get("statusLine")
    return {
        "settings": str(p),
        "hooks": sorted(set(events)),
        "statusline": isinstance(sl, dict) and _is_ours(sl.get("command")),
        "disable_all_hooks": bool(data.get("disableAllHooks")),
    }
