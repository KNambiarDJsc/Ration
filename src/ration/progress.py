"""Explainable progress model.

Progress is about verified movement toward done, not activity. Without any
verification signal (tests / acceptance checks) the score is capped, because
an agent that edits lots of files has not shown it is closer to done.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import AgentEvent, EventType

UNVERIFIED_CAP = 0.6


@dataclass
class Progress:
    score: float
    velocity: float  # change in score over the last `velocity_window_s`
    reasons: list[str] = field(default_factory=list)

    @property
    def arrow(self) -> str:
        if self.velocity > 0.02:
            return "↑"
        if self.velocity < -0.02:
            return "↓"
        return "→"


def _test_results(events: list[AgentEvent]) -> list[AgentEvent]:
    return [e for e in events if e.event_type == EventType.TEST_RESULT]


def _ratio(e: AgentEvent) -> float | None:
    p = e.metadata.get("passed")
    f = e.metadata.get("failed")
    if p is None and f is None:
        ok = e.metadata.get("ok")
        return None if ok is None else (1.0 if ok else 0.0)
    p, f = int(p or 0), int(f or 0)
    if p + f == 0:
        return 1.0 if e.metadata.get("ok") else None
    return p / (p + f)


def score(events: list[AgentEvent]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    reads = {e.metadata.get("path") for e in events if e.event_type == EventType.FILE_READ}
    reads.discard(None)
    changes = [e.metadata.get("path") for e in events if e.event_type == EventType.FILE_CHANGED]
    changed = {c for c in changes if c}

    explore = min(1.0, len(reads) / 8)
    diff = min(1.0, len(changed) / 3)
    if changed:
        reasons.append(f"+ diff emerging: {len(changed)} file(s) changed")

    penalty = 0.0
    churn = Counter(c for c in changes if c)
    if churn:
        path, n = churn.most_common(1)[0]
        if n >= 6:
            penalty += 0.1
            reasons.append(f"- same file modified {n} times ({_short(path)})")

    results = [(e, _ratio(e)) for e in _test_results(events)]
    results = [(e, r) for e, r in results if r is not None]
    if results:
        first_e, first = results[0]
        last_e, last = results[-1]
        tests = last
        p, f = last_e.metadata.get("passed"), last_e.metadata.get("failed")
        if p is not None and f is not None:
            reasons.append(f"+ {p}/{int(p) + int(f)} tests pass" if last > 0 else f"- 0/{int(p) + int(f)} tests pass")
        elif last == 1.0:
            reasons.append("+ checks pass")
        else:
            reasons.append("- checks failing")
        if len(results) > 1 and last > first:
            reasons.append("+ test results improving")
        elif len(results) > 1 and last < first:
            reasons.append("- test results regressed")
        sigs = [e.metadata.get("sig") for e, r in results if r < 1.0]
        if len(sigs) >= 3 and len(set(sigs[-3:])) == 1 and sigs[-1]:
            mins = (last_e.timestamp - next(e.timestamp for e, _ in results if e.metadata.get("sig") == sigs[-1])) / 60
            if mins >= 5:
                penalty += 0.1
                reasons.append(f"- one failure unchanged for {mins:.0f} minutes")
        raw = 0.1 * explore + 0.25 * diff + 0.65 * tests
    else:
        raw = min(UNVERIFIED_CAP, 0.35 * explore + 0.65 * diff)
        if events:
            reasons.append("? no tests/checks observed yet (score capped)")

    return max(0.0, min(1.0, raw - penalty)), reasons


def compute(events: list[AgentEvent], *, now: float | None = None, velocity_window_s: float = 600) -> Progress:
    s, reasons = score(events)
    if not events:
        return Progress(0.0, 0.0, reasons)
    now = now if now is not None else events[-1].timestamp
    before = [e for e in events if e.timestamp <= now - velocity_window_s]
    prev, _ = score(before) if before else (0.0, [])
    return Progress(s, s - prev, reasons)


def _short(path: str | None) -> str:
    if not path:
        return "?"
    p = path.replace("\\", "/")
    return p if len(p) <= 40 else "…" + p[-39:]
