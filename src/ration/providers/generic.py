"""Generic CLI adapter for agents without a dedicated integration.

Configure in config.toml:

    [generic.aider]
    command = "aider --yes --message {prompt} --model {model}"
    cheap = "gpt-4.1-mini"
    strong = "gpt-4.1"

Only start/end/exit-code are observable; RATION does not guess more.
"""

from __future__ import annotations

import shlex
from typing import Any

from ..models import AgentEvent, EventType, ExecutionPolicy, ModelClass, UsageSnapshot
from .base import EventSink, ProviderCapabilities, RunResult, SessionInfo, stream_lines, which


class GenericCLIProvider:
    def __init__(self, name: str, spec: dict[str, Any]):
        self.name = name
        self.spec = spec

    def capabilities(self) -> ProviderCapabilities:
        exe = shlex.split(self.spec["command"])[0]
        return ProviderCapabilities(self.name, bool(which(exe)), features={"events": False})

    def models(self) -> dict[ModelClass, str]:
        return {m: self.spec[str(m)] for m in ModelClass if self.spec.get(str(m))} or {ModelClass.CHEAP: ""}

    def usage(self) -> list[UsageSnapshot]:
        return []

    def sessions(self, limit: int = 20) -> list[SessionInfo]:
        return []

    def run(self, prompt: str, *, policy: ExecutionPolicy, cwd: str, run_id: str, task_id: str | None, on_event: EventSink, **_: Any) -> RunResult:
        model = self.models().get(policy.model_class, "")
        argv = [part.format(prompt=prompt, model=model) for part in shlex.split(self.spec["command"])]
        on_event(AgentEvent(self.name, run_id, EventType.SESSION_STARTED, {"source": "ration_run"}, run_id=run_id, task_id=task_id))
        rc, cancelled, err = stream_lines(argv, cwd, lambda _line: True)
        on_event(AgentEvent(self.name, run_id, EventType.SESSION_ENDED, {"rc": rc}, run_id=run_id, task_id=task_id))
        return RunResult(ok=rc == 0 and not cancelled, session_id=run_id, error=err[-500:] if rc else None, cancelled=cancelled)
