"""Claude Code adapter.

Official surfaces used:
- `claude -p --output-format stream-json --verbose` for explicit runs
- command hooks (see ration.hooks.claude) for observe/guard
- statusLine JSON `rate_limits` for subscription usage (ration.hooks.statusline)

Unofficial surface, used read-only for session listing only:
- ~/.claude/projects/<project>/<session>.jsonl transcripts (format may change)

Never used: OAuth tokens, private usage endpoints, --dangerously-skip-permissions.
"""

from __future__ import annotations

import json
import os
import time
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

HELP_FLAGS = {
    "print": "--print",
    "stream_json": "stream-json",
    "model": "--model",
    "max_turns": "--max-turns",
    "effort": "--effort",
    "resume": "--resume",
    "permission_mode": "--permission-mode",
    "append_system_prompt": "--append-system-prompt",
    "max_budget_usd": "--max-budget-usd",
    "agents": "--agents",
    "mcp_config": "--mcp-config",
}


class ClaudeCapabilityDetector:
    """Detect what the installed Claude Code supports instead of assuming a version."""

    def __init__(self, exe: str | None = None):
        self.exe = exe or which("claude")

    def detect(self) -> ProviderCapabilities:
        if not self.exe:
            return ProviderCapabilities("claude", False, notes=["`claude` not found on PATH"])
        version = parse_version(run_text([self.exe, "--version"]))
        help_text = run_text([self.exe, "--help"])
        features = flag_features(help_text, HELP_FLAGS)
        features["hooks"] = True  # settings-based; validated by `ration doctor`
        features["statusline_rate_limits"] = True  # documented; Pro/Max or gateway only
        notes = []
        if not features.get("max_turns"):
            notes.append("--max-turns not advertised; RATION enforces turn caps via stuck detection only")
        return ProviderCapabilities("claude", True, version, features, notes)


WINDOW_MINUTES = {"five_hour": 300, "seven_day": 7 * 24 * 60}


def rate_limit_snapshots(info: dict[str, Any], now: float | None = None) -> list[UsageSnapshot]:
    """`claude -p --output-format stream-json` emits `rate_limit_event` lines with
    `rate_limit_info.unifiedWindows.{five_hour,seven_day}.{utilization 0..1, resetsAt}`."""
    now = time.time() if now is None else now
    out = []
    for window, w in coerce(info.get("unifiedWindows")).items():
        w = coerce(w)
        if w.get("utilization") is None:
            continue
        out.append(
            UsageSnapshot(
                provider="claude",
                window=window,
                resource_type=ResourceType.USAGE_PERCENT,
                used_percent=round(float(w["utilization"]) * 100, 2),
                reset_at=float(w["resetsAt"]) if w.get("resetsAt") else None,
                window_minutes=WINDOW_MINUTES.get(window),
                source=Source.PROVIDER_CLI,
                confidence=Confidence.HIGH,
                captured_at=now,
            )
        )
    return out


class StreamParser:
    """Map `claude -p --output-format stream-json` lines to AgentEvents."""

    def __init__(self, run_id: str, task_id: str | None, store_paths: bool = True):
        self.run_id, self.task_id, self.store_paths = run_id, task_id, store_paths
        self.session_id = "pending"
        self.pending: dict[str, tuple[str, dict[str, Any]]] = {}
        self.result: dict[str, Any] | None = None
        self.final_text = ""
        self.usage: list[UsageSnapshot] = []  # official quota readings seen in the stream
        self.permission_denials: list[str] = []  # decision_reason_type per denied tool call
        self.denied_ids: set[str] = set()
        self.denial_reason = ""  # in memory only, shown to the user, never stored

    def _ev(self, et: EventType, meta: dict[str, Any] | None = None) -> AgentEvent:
        return AgentEvent("claude", self.session_id, et, meta or {}, run_id=self.run_id, task_id=self.task_id)

    def feed(self, obj: dict[str, Any]) -> list[AgentEvent]:
        t = obj.get("type")
        if obj.get("session_id"):
            self.session_id = obj["session_id"]
        out: list[AgentEvent] = []
        if t == "system" and obj.get("subtype") == "init":
            out.append(self._ev(EventType.SESSION_STARTED, {"source": "ration_run"}))
            if obj.get("model"):
                out.append(self._ev(EventType.MODEL_SELECTED, {"model": obj["model"]}))
        elif t == "assistant":
            for block in coerce(obj.get("message")).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.pending[block.get("id", "")] = (block.get("name", "?"), coerce(block.get("input")))
                    out.append(self._ev(EventType.TOOL_STARTED, {"tool": block.get("name")}))
                    if block.get("name") in ("Agent", "Task"):
                        out.append(self._ev(EventType.SUBAGENT_STARTED, {"model": coerce(block.get("input")).get("model")}))
                elif isinstance(block, dict) and block.get("type") == "text":
                    self.final_text = block.get("text", "")
        elif t == "user":
            for block in coerce(obj.get("message")).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name, ti = self.pending.pop(block.get("tool_use_id", ""), ("?", {}))
                    if block.get("tool_use_id") in self.denied_ids:
                        continue  # never ran: already recorded as a permission denial, not a failure
                    out += tool_events(
                        provider="claude",
                        session_id=self.session_id,
                        tool=name,
                        tool_input=ti,
                        ok=not block.get("is_error"),
                        response=block.get("content"),
                        store_paths=self.store_paths,
                        run_id=self.run_id,
                        task_id=self.task_id,
                    )
        elif t == "rate_limit_event":
            self.usage += rate_limit_snapshots(coerce(obj.get("rate_limit_info")))
        elif t == "system" and obj.get("subtype") == "permission_denied":
            self.permission_denials.append(str(obj.get("decision_reason_type") or "denied"))
            self.denied_ids.add(str(obj.get("tool_use_id")))
            self.denial_reason = str(obj.get("decision_reason") or obj.get("message") or "")[:300]
            out.append(self._ev(EventType.PERMISSION_REQUESTED, {"tool": obj.get("tool_name"), "denied": True, "reason_type": obj.get("decision_reason_type")}))
        elif t == "result":
            self.result = obj
            if obj.get("result"):
                self.final_text = obj["result"]
            et = EventType.ERROR if obj.get("is_error") else EventType.TASK_COMPLETED
            out.append(self._ev(et, {"subtype": obj.get("subtype"), "turns": obj.get("num_turns"), "cost_usd": obj.get("total_cost_usd")}))
        return out


class ClaudeProvider:
    name = "claude"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self._caps: ProviderCapabilities | None = None

    def capabilities(self) -> ProviderCapabilities:
        if self._caps is None:
            self._caps = ClaudeCapabilityDetector().detect()
        return self._caps

    def models(self) -> dict[ModelClass, str]:
        c = self.cfg["claude"]
        return {m: c[str(m)] for m in ModelClass if c.get(str(m))}

    def usage(self) -> list[UsageSnapshot]:
        return []  # arrives via the statusline hook into the store

    def sessions(self, limit: int = 20) -> list[SessionInfo]:
        root = Path.home() / ".claude" / "projects"
        if not root.is_dir():
            return []
        files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [SessionInfo("claude", f.stem, f.parent.name, f.stat().st_mtime, str(f)) for f in files]

    def build_argv(self, prompt: str, policy: ExecutionPolicy, permission_mode: str | None = None) -> list[str]:
        caps = self.capabilities()
        f = caps.features
        exe = which("claude") or "claude"
        argv = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        model = self.models().get(policy.model_class)
        if model and f.get("model", True):
            argv += ["--model", model]
        if f.get("max_turns") and policy.max_turns:
            argv += ["--max-turns", str(policy.max_turns)]
        if f.get("effort"):
            argv += ["--effort", str(policy.effort)]
        if permission_mode and permission_mode != "bypassPermissions" and f.get("permission_mode"):
            argv += ["--permission-mode", permission_mode]
        return argv

    def run(
        self,
        prompt: str,
        *,
        policy: ExecutionPolicy,
        cwd: str,
        run_id: str,
        task_id: str | None,
        on_event: EventSink,
        permission_mode: str | None = None,
    ) -> RunResult:
        parser = StreamParser(run_id, task_id, bool(self.cfg["privacy"]["store_paths"]))

        def on_line(line: str) -> bool:
            line = line.strip()
            if not line.startswith("{"):
                return True
            try:
                obj = json.loads(line)
            except ValueError:
                return True
            keep = True
            for ev in parser.feed(obj):
                keep = on_event(ev) and keep
            return keep

        t0 = time.time()
        # RATION_RUN_ID tells RATION's own hooks inside this child session to stay out:
        # the orchestrator already sees everything through the stream.
        env = dict(os.environ, RATION_RUN_ID=run_id)
        rc, cancelled, err = stream_lines(self.build_argv(prompt, policy, permission_mode), cwd, on_line, env=env)
        r = parser.result or {}
        if not parser.permission_denials and r.get("permission_denials"):
            parser.permission_denials = ["denied"] * len(r["permission_denials"])
        usage = coerce(r.get("usage"))
        return RunResult(
            ok=(rc == 0 and not r.get("is_error") and not cancelled),
            session_id=parser.session_id,
            cost_usd=r.get("total_cost_usd"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            num_turns=r.get("num_turns"),
            error=None if rc == 0 else (err.strip()[-500:] or f"exit {rc} after {time.time() - t0:.0f}s"),
            cancelled=cancelled,
            final_text=parser.final_text,
            usage=parser.usage,
            permission_denials=parser.permission_denials,
            denial_reason=parser.denial_reason,
        )
