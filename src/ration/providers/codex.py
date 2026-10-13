"""OpenAI Codex adapter.

Official surfaces used:
- `codex exec --json` for explicit runs (JSONL: thread.started, turn.completed
  with usage, item.completed with command_execution / file_change items, ...)
- rollout files under $CODEX_HOME/sessions/**/rollout-*.jsonl, which persist
  `token_count` events carrying server-reported `rate_limits`
  (primary/secondary windows: used_percent, window_minutes, resets_at)

Never used: auth.json, private endpoints, --dangerously-bypass-approvals-and-sandbox.
RATION does not pass sandbox/approval flags at all: the user's Codex config wins.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    AgentEvent,
    Confidence,
    EventType,
    ExecutionPolicy,
    ModelClass,
    ResourceType,
    Source,
    UsageSnapshot,
)
from ..normalize import tool_events
from .base import (
    EventSink,
    ProviderCapabilities,
    RunResult,
    SessionInfo,
    coerce,
    flag_features,
    parse_version,
    run_text,
    stream_lines,
    which,
)

EXEC_FLAGS = {"json": "--json", "model": "--model", "config_override": "--config", "sandbox": "--sandbox"}
SANDBOXES = ("read-only", "workspace-write")  # never danger-full-access
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def _ts(v: Any) -> float | None:
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def rate_limit_snapshots(rate_limits: dict[str, Any], captured_at: float, now: float | None = None) -> list[UsageSnapshot]:
    now = time.time() if now is None else now
    age_h = (now - captured_at) / 3600
    conf = Confidence.HIGH if age_h < 0.5 else Confidence.MEDIUM if age_h < 6 else Confidence.LOW
    out = []
    for window in ("primary", "secondary"):
        w = rate_limits.get(window)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        reset = w.get("resets_at")
        if reset is None and w.get("resets_in_seconds") is not None:  # older Codex versions
            reset = captured_at + float(w["resets_in_seconds"])
        out.append(
            UsageSnapshot(
                provider="codex",
                window=window,
                resource_type=ResourceType.USAGE_PERCENT,
                used_percent=float(w["used_percent"]),
                reset_at=float(reset) if reset is not None else None,
                window_minutes=int(w["window_minutes"]) if w.get("window_minutes") else None,
                source=Source.LOCAL_LOG,
                confidence=conf,
                captured_at=captured_at,
            )
        )
    return out


def read_rollout(path: Path) -> dict[str, Any]:
    """Session meta, last rate limits, total tokens and tool events from one rollout file."""
    meta: dict[str, Any] = {}
    last_rl: tuple[float, dict[str, Any]] | None = None
    total_tokens = None
    events: list[AgentEvent] = []
    pending: dict[str, tuple[str, dict[str, Any], float | None]] = {}
    sid = path.stem
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        ts = _ts(obj.get("timestamp"))
        typ, payload = obj.get("type"), coerce(obj.get("payload"))
        if typ == "session_meta":
            meta = payload
            sid = str(payload.get("id") or sid)
        elif typ == "event_msg" and payload.get("type") == "token_count":
            if payload.get("rate_limits") and ts:
                last_rl = (ts, payload["rate_limits"])
            tu = coerce(coerce(payload.get("info")).get("total_token_usage"))
            if tu.get("total_tokens") is not None:
                total_tokens = int(tu["total_tokens"])
        elif typ == "response_item":
            pt = payload.get("type")
            if pt in ("function_call", "custom_tool_call"):
                name = payload.get("name", "?")
                raw_args = payload.get("arguments", payload.get("input"))
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.startswith("{") else {"input": raw_args}
                except ValueError:
                    args = {"input": raw_args}
                pending[str(payload.get("call_id"))] = (name, coerce(args), ts)
            elif pt in ("function_call_output", "custom_tool_call_output"):
                name, args, cts = pending.pop(str(payload.get("call_id")), ("?", {}, ts))
                out = payload.get("output")
                ok = _exit_ok(out)
                events += _codex_tool_events(sid, name, args, ok, out, cts or ts)
    return {"session_id": sid, "meta": meta, "rate_limits": last_rl, "total_tokens": total_tokens, "events": events}


def _exit_ok(output: Any) -> bool:
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    try:
        o = json.loads(text)
        code = coerce(o.get("metadata")).get("exit_code", o.get("exit_code"))
        if code is not None:
            return int(code) == 0
    except (ValueError, AttributeError, TypeError):
        pass
    m = re.search(r"exit(?:ed with)? code:?\s*(\d+)", text or "", re.I)
    return not m or m.group(1) == "0"


def _codex_tool_events(sid: str, name: str, args: dict[str, Any], ok: bool, out: Any, ts: float | None, **kw: Any) -> list[AgentEvent]:
    if name == "apply_patch":
        patch = str(args.get("input") or "")
        evs = [AgentEvent("codex", sid, EventType.TOOL_COMPLETED if ok else EventType.TOOL_FAILED, {"tool": name}, **kw)]
        for p in PATCH_FILE_RE.findall(patch):
            evs.append(AgentEvent("codex", sid, EventType.FILE_CHANGED, {"path": p.strip()}, **kw))
        if ts:
            for e in evs:
                e.timestamp = ts
        return evs
    return tool_events(provider="codex", session_id=sid, tool=name, tool_input=args, ok=ok, response=out, timestamp=ts, **kw)


class CodexProvider:
    name = "codex"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self._caps: ProviderCapabilities | None = None

    def home(self) -> Path:
        h = os.environ.get("CODEX_HOME") or self.cfg["codex"].get("home")
        return Path(h) if h else Path.home() / ".codex"

    def rollouts(self, limit: int = 30) -> list[Path]:
        root = self.home() / "sessions"
        if not root.is_dir():
            return []
        return sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]

    def capabilities(self) -> ProviderCapabilities:
        if self._caps is None:
            exe = which("codex")
            if not exe:
                notes = ["`codex` not found on PATH"]
                if self.rollouts(1):
                    notes.append("rollout logs found: usage can still be read")
                self._caps = ProviderCapabilities("codex", False, features={"rollout_logs": bool(self.rollouts(1))}, notes=notes)
            else:
                version = parse_version(run_text([exe, "--version"]))
                feats = flag_features(run_text([exe, "exec", "--help"]), EXEC_FLAGS)
                feats["rollout_logs"] = bool(self.rollouts(1))
                self._caps = ProviderCapabilities("codex", True, version, feats)
        return self._caps

    def models(self) -> dict[ModelClass, str]:
        c = self.cfg["codex"]
        # Empty string = Codex's own configured default model; still a usable class.
        return {m: c.get(str(m), "") or "" for m in ModelClass}

    def usage(self) -> list[UsageSnapshot]:
        for path in self.rollouts(20):
            info = read_rollout(path)
            if info.get("rate_limits"):
                ts, rl = info["rate_limits"]
                return rate_limit_snapshots(rl, ts)
        return []

    def ingest_usage(self, store: Any) -> int:
        latest = {(s.provider, s.window): s.captured_at for s in store.latest_usage("codex")}
        n = 0
        for s in self.usage():
            if latest.get((s.provider, s.window), 0) < s.captured_at:
                store.add_usage(s)
                n += 1
        return n

    def sessions(self, limit: int = 20) -> list[SessionInfo]:
        out = []
        for p in self.rollouts(limit):
            info = read_rollout(p)
            cwd = coerce(info.get("meta")).get("cwd")
            out.append(SessionInfo("codex", info.get("session_id", p.stem), Path(cwd).name if cwd else None, p.stat().st_mtime, str(p), info.get("total_tokens")))
        return out

    def build_argv(self, prompt: str, policy: ExecutionPolicy) -> list[str]:
        exe = which("codex") or "codex"
        argv = [exe, "exec", "--json"]
        model = self.models().get(policy.model_class)
        if model:
            argv += ["--model", model]
        argv += ["--config", f'model_reasoning_effort="{policy.effort}"']
        # `ration run` passes no sandbox flag (your Codex config wins). Autopilot asks for one:
        # workspace-write confines writes to the working directory, which is its worktree.
        sandbox = str(self.cfg["codex"].get("sandbox") or "")
        if sandbox in SANDBOXES and self.capabilities().features.get("sandbox"):
            argv += ["--sandbox", sandbox]
        return argv + [prompt]

    def run(
        self,
        prompt: str,
        *,
        policy: ExecutionPolicy,
        cwd: str,
        run_id: str,
        task_id: str | None,
        on_event: EventSink,
        **_: Any,
    ) -> RunResult:
        state: dict[str, Any] = {"sid": "pending", "in": 0, "out": 0, "turns": 0, "error": None, "text": ""}
        kw = {"run_id": run_id, "task_id": task_id}

        def emit(evs: list[AgentEvent]) -> bool:
            keep = True
            for e in evs:
                keep = on_event(e) and keep
            return keep

        def on_line(line: str) -> bool:
            try:
                obj = json.loads(line)
            except ValueError:
                return True
            t = obj.get("type")
            sid = state["sid"]
            if t == "thread.started":
                state["sid"] = obj.get("thread_id", sid)
                return emit([AgentEvent("codex", state["sid"], EventType.SESSION_STARTED, {"source": "ration_run"}, **kw)])
            if t == "turn.completed":
                u = coerce(obj.get("usage"))
                state["in"] += int(u.get("input_tokens") or 0)
                state["out"] += int(u.get("output_tokens") or 0)
                state["turns"] += 1
                return True
            if t in ("turn.failed", "error"):
                state["error"] = str(coerce(obj.get("error")).get("message") or obj.get("message") or t)
                return emit([AgentEvent("codex", sid, EventType.ERROR, {"error": state["error"][:200]}, **kw)])
            if t == "item.completed":
                item = coerce(obj.get("item"))
                it = item.get("type")
                if it == "command_execution":
                    code = item.get("exit_code")
                    return emit(
                        tool_events(
                            provider="codex",
                            session_id=sid,
                            tool="shell",
                            tool_input={"command": item.get("command")},
                            ok=code == 0,
                            response={"output": item.get("aggregated_output")},
                            **kw,
                        )
                    )
                if it == "file_change":
                    return emit([AgentEvent("codex", sid, EventType.FILE_CHANGED, {"path": c.get("path")}, **kw) for c in item.get("changes") or [] if isinstance(c, dict)])
                if it == "mcp_tool_call":
                    et = EventType.TOOL_FAILED if item.get("status") == "failed" else EventType.TOOL_COMPLETED
                    return emit([AgentEvent("codex", sid, et, {"tool": f"mcp:{item.get('server')}:{item.get('tool')}"}, **kw)])
                if it == "agent_message":
                    state["text"] = item.get("text", "")
            return True

        rc, cancelled, err = stream_lines(self.build_argv(prompt, policy), cwd, on_line)
        return RunResult(
            ok=rc == 0 and not state["error"] and not cancelled,
            session_id=state["sid"],
            input_tokens=state["in"],
            output_tokens=state["out"],
            num_turns=state["turns"],
            error=state["error"] or (err.strip()[-500:] if rc else None),
            cancelled=cancelled,
            final_text=state["text"],
        )


def agents_md_fragment() -> str:
    """Provider-neutral snippet the user may paste into AGENTS.md. RATION never writes it."""
    return (
        "## Inference budget (RATION)\n"
        "- Prefer the cheapest approach that can be verified (run the narrowest relevant tests).\n"
        "- If the same check fails 3 times with the same error, stop and re-plan instead of retrying.\n"
        "- Do not spawn parallel agents for work one agent can finish; say why if you do.\n"
        "- Run `ration status` to see remaining budget before starting large or premium work.\n"
    )
