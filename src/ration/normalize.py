"""Turn provider-specific tool calls into provider-neutral AgentEvents.

Shared by the Claude hook handler, the Claude stream-json parser and the Codex
parsers. Stdlib-only.

Privacy: we store hashes (signatures) of tool inputs, the test runner name and
pass/fail counts, never full commands, prompts, diffs or file contents.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .models import AgentEvent, EventType

READ_TOOLS = {"Read", "NotebookRead", "View"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
SHELL_TOOLS = {"Bash", "PowerShell", "shell", "local_shell", "exec_command"}
AGENT_TOOLS = {"Agent", "Task"}

TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|unittest|jest|vitest|mocha|go test|cargo test|cargo nextest|"
    r"(npm|yarn|pnpm|bun)( run)? test|rspec|phpunit|dotnet test|mvn( -\S+)* test|gradle\S* test|"
    r"tox|nox|ctest|make test|deno test)\b"
)
# Same-line, lowercase summaries ("3 failed, 4 passed", "2 failing", "5 passed; 1 failed").
# Case-sensitive on purpose: "Exit code 1\nFAILED ..." must not count as "1 failed".
COUNT_RE = re.compile(r"(\d+)[ \t]+(passed|passing|failed|failing|errors?)\b")
FAIL_ID_RES = [
    re.compile(r"^FAILED\s+(\S+)", re.M),  # pytest
    re.compile(r"^\s*(?:✕|×)\s+(.+)$", re.M),  # jest / vitest
    re.compile(r"^--- FAIL:\s+(\S+)", re.M),  # go
    re.compile(r"^test (\S+) \.\.\. FAILED", re.M),  # cargo
]


def sig(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _text(resp: Any) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [resp.get(k) for k in ("stdout", "stderr", "output", "content", "error", "aggregated_output")]
        return "\n".join(p if isinstance(p, str) else json.dumps(p, default=str) for p in parts if p)
    if isinstance(resp, list):
        return "\n".join(_text(x.get("text") if isinstance(x, dict) else x) for x in resp)
    return str(resp)


def test_runner(command: str) -> str | None:
    m = TEST_CMD_RE.search(command or "")
    return m.group(1).split()[0] if m else None


def parse_test_output(output: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for n, kind in COUNT_RE.findall(output or ""):
        k = kind.lower()
        key = "passed" if k in ("passed", "passing") else "failed"
        counts[key] = counts.get(key, 0) + int(n)
    failing: list[str] = []
    for rx in FAIL_ID_RES:
        failing += [m.strip() for m in rx.findall(output or "")]
    meta: dict[str, Any] = {}
    if counts:
        meta["passed"] = counts.get("passed", 0)
        meta["failed"] = counts.get("failed", 0)
    if failing:
        meta["sig"] = sig(sorted(set(failing)))
    else:
        tail = [ln for ln in (output or "").strip().splitlines() if ln.strip()][-3:]
        # strip timings/addresses so the same failure hashes the same
        meta["sig"] = sig([re.sub(r"[\d.]+(ms|s)?\b|0x[0-9a-f]+", "#", ln) for ln in tail])
    return meta


def tool_events(
    *,
    provider: str,
    session_id: str,
    tool: str,
    tool_input: dict[str, Any] | None,
    ok: bool,
    response: Any = None,
    timestamp: float | None = None,
    store_paths: bool = True,
    run_id: str | None = None,
    task_id: str | None = None,
) -> list[AgentEvent]:
    """Events for one completed (ok) or failed tool call."""
    ti = tool_input or {}
    kw: dict[str, Any] = {"run_id": run_id, "task_id": task_id}
    if timestamp is not None:
        kw["timestamp"] = timestamp
    meta: dict[str, Any] = {"tool": tool, "sig": sig(tool, ti)}
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if path and store_paths:
        meta["path"] = str(path)
    command = ti.get("command") if isinstance(ti.get("command"), str) else None
    if isinstance(ti.get("command"), list):
        command = " ".join(map(str, ti["command"]))
    runner = test_runner(command) if command else None
    if command:
        meta["cmd"] = command.split()[0] if command.split() else ""
    out: list[AgentEvent] = []
    et = EventType.TOOL_COMPLETED if ok else EventType.TOOL_FAILED
    out.append(AgentEvent(provider, session_id, et, meta, **kw))

    ev_path = {"path": str(path)} if path and store_paths else {"path_sig": sig(path)} if path else {}
    if path and tool in READ_TOOLS:
        out.append(AgentEvent(provider, session_id, EventType.FILE_READ, ev_path, **kw))
    if path and tool in EDIT_TOOLS and ok:
        out.append(AgentEvent(provider, session_id, EventType.FILE_CHANGED, ev_path, **kw))
    if runner:
        out.append(AgentEvent(provider, session_id, EventType.TEST_RUN, {"runner": runner}, **kw))
        tmeta = parse_test_output(_text(response))
        tmeta["runner"] = runner
        tmeta["ok"] = bool(ok) and not tmeta.get("failed")
        out.append(AgentEvent(provider, session_id, EventType.TEST_RESULT, tmeta, **kw))
    return out


def is_premium(model: str | None, markers: str = "opus") -> bool:
    if not model:
        return False
    m = model.lower()
    return any(k.strip() and k.strip() in m for k in markers.split(","))
