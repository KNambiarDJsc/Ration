"""Deterministic mock provider for tests, demos and simulations."""

from __future__ import annotations

import random
from typing import Any

from ..models import AgentEvent, EventType, ExecutionPolicy, ModelClass, UsageSnapshot
from ..normalize import sig
from .base import EventSink, ProviderCapabilities, RunResult, SessionInfo


class MockProvider:
    def __init__(
        self,
        name: str = "mock",
        success: dict[ModelClass, float] | None = None,
        seed: int = 0,
        cost: dict[ModelClass, float] | None = None,
    ):
        self.name = name
        self.success = success or {ModelClass.CHEAP: 0.3, ModelClass.STRONG: 0.6, ModelClass.PREMIUM: 0.85}
        self.cost = cost or {ModelClass.CHEAP: 0.01, ModelClass.STRONG: 0.05, ModelClass.PREMIUM: 0.25}
        self.rng = random.Random(seed)
        self.calls: list[ExecutionPolicy] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(self.name, True, "0.0.0", {"events": True})

    def models(self) -> dict[ModelClass, str]:
        return {m: f"{self.name}-{m}" for m in ModelClass}

    def usage(self) -> list[UsageSnapshot]:
        return []

    def sessions(self, limit: int = 20) -> list[SessionInfo]:
        return []

    def run(self, prompt: str, *, policy: ExecutionPolicy, cwd: str, run_id: str, task_id: str | None, on_event: EventSink, **_: Any) -> RunResult:
        self.calls.append(policy)
        sid = f"{self.name}-{run_id}"
        kw = {"run_id": run_id, "task_id": task_id}
        t = 1_000_000.0 + len(self.calls) * 10_000

        def emit(et: EventType, meta: dict[str, Any]) -> bool:
            nonlocal t
            t += 30
            return on_event(AgentEvent(self.name, sid, et, meta, timestamp=t, **kw))

        emit(EventType.SESSION_STARTED, {"source": "mock"})
        emit(EventType.MODEL_SELECTED, {"model": self.models()[policy.model_class]})
        ok = self.rng.random() < self.success.get(policy.model_class, 0.5)
        emit(EventType.FILE_READ, {"path": "src/app.py"})
        emit(EventType.FILE_CHANGED, {"path": "src/app.py"})
        if ok:
            emit(EventType.TOOL_COMPLETED, {"tool": "Bash", "sig": sig("pytest", run_id)})
            emit(EventType.TEST_RESULT, {"passed": 7, "failed": 0, "ok": True, "sig": "green"})
        else:
            for _i in range(6):  # loop on the same failure
                if not emit(EventType.TOOL_FAILED, {"tool": "Bash", "sig": sig("pytest -q")}):
                    break
                if not emit(EventType.TEST_RESULT, {"passed": 4, "failed": 3, "ok": False, "sig": "fail-a"}):
                    break
        emit(EventType.TASK_COMPLETED if ok else EventType.ERROR, {})
        return RunResult(ok=ok, session_id=sid, cost_usd=self.cost[policy.model_class], num_turns=3 if ok else 12)
