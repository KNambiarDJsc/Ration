"""Stuck detection.

Each signal is normalized to 0..1, weighted, and combined with a noisy-OR:

    stuck = 1 - prod(1 - w_i * s_i)

so any single strong signal can dominate, several weak ones accumulate, and
the result stays in 0..1. Every contributing signal yields a human-readable
reason, and every recommended intervention carries an explanation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import AgentEvent, BudgetMode, EventType, Intervention, ModelClass

WEIGHTS: dict[str, float] = {
    "test_loop": 0.85,
    "tool_repeat": 0.6,
    "fail_streak": 0.6,
    "sequence_loop": 0.6,
    "edit_churn": 0.5,
    "no_progress": 0.5,
    "subagent_repeat": 0.5,
    "file_rereads": 0.4,
    "compaction": 0.4,
}

BANDS = [(0.35, "healthy"), (0.65, "watch"), (0.80, "intervene"), (1.01, "likely_stuck")]

TOOL_EVENTS = (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class StuckResult:
    score: float
    band: str
    signals: dict[str, float] = field(default_factory=dict)  # weighted contributions
    reasons: list[str] = field(default_factory=list)

    @property
    def dominant(self) -> str | None:
        live = {k: v for k, v in self.signals.items() if v > 0}
        return max(live, key=lambda k: live[k]) if live else None


def band_for(score: float) -> str:
    for upper, name in BANDS:
        if score < upper:
            return name
    return "likely_stuck"


def _tail_run(items: list[str | None]) -> tuple[str | None, int]:
    if not items:
        return None, 0
    last = items[-1]
    n = 0
    for x in reversed(items):
        if x == last:
            n += 1
        else:
            break
    return last, n


def _sequence_repeats(sigs: list[str]) -> tuple[int, int]:
    """Largest number of back-to-back repeats of a 2- or 3-step sequence at the tail."""
    best = (0, 0)
    for k in (2, 3):
        if len(sigs) < 2 * k:
            continue
        pat = sigs[-k:]
        if len(set(pat)) == 1:
            continue  # handled by tool_repeat
        reps = 1
        i = len(sigs) - 2 * k
        while i >= 0 and sigs[i : i + k] == pat:
            reps += 1
            i -= k
        if reps > best[0]:
            best = (reps, k)
    return best


def detect(events: list[AgentEvent], *, now: float | None = None, window: int = 60) -> StuckResult:
    raw: dict[str, float] = {}
    reasons: list[str] = []
    if not events:
        return StuckResult(0.0, "healthy")
    now = now if now is not None else events[-1].timestamp

    tools = [e for e in events if e.event_type in TOOL_EVENTS][-window:]
    sigs = [str(e.metadata.get("sig")) for e in tools if e.metadata.get("sig")]

    # 1. identical tool calls
    if sigs:
        sig, c = Counter(sigs).most_common(1)[0]
        raw["tool_repeat"] = _clamp((c - 2) / 4)
        if c >= 3:
            tool = next((e.metadata.get("tool") for e in tools if e.metadata.get("sig") == sig), "tool")
            reasons.append(f"identical {tool} call repeated {c}x")

    # 2. repeated command sequences (A,B,A,B,A,B)
    reps, k = _sequence_repeats(sigs)
    raw["sequence_loop"] = _clamp((reps - 2) / 3)
    if reps >= 3:
        reasons.append(f"same {k}-step command sequence repeated {reps}x")

    # 3. unchanged failing test
    results = [e for e in events if e.event_type == EventType.TEST_RESULT]
    fail_sigs = [e.metadata.get("sig") if not e.metadata.get("ok") else None for e in results]
    last_sig, k_same = _tail_run(fail_sigs)
    if last_sig:
        raw["test_loop"] = _clamp((k_same - 1) / 4)
        if k_same >= 2:
            reasons.append(f"same test failure repeated {k_same}x")

    # 4. failed tool streak
    statuses = ["fail" if e.event_type == EventType.TOOL_FAILED else "ok" for e in tools]
    st, n = _tail_run(statuses)  # type: ignore[arg-type]
    if st == "fail":
        raw["fail_streak"] = _clamp((n - 1) / 4)
        if n >= 2:
            reasons.append(f"{n} consecutive failed tool calls")

    # 5. edit churn (patch/revert behaviour)
    changes = Counter(
        e.metadata.get("path") for e in events[-window * 2 :] if e.event_type == EventType.FILE_CHANGED and e.metadata.get("path")
    )
    if changes:
        path, m = changes.most_common(1)[0]
        tests_green = bool(results) and bool(results[-1].metadata.get("ok"))
        if not tests_green:
            raw["edit_churn"] = _clamp((m - 3) / 5)
            if m >= 4:
                reasons.append(f"same file edited {m}x without passing checks")

    # 6. re-reading files that did not change
    reads_since_change: Counter[str] = Counter()
    for e in events[-window * 2 :]:
        p = e.metadata.get("path")
        if not p:
            continue
        if e.event_type == EventType.FILE_CHANGED:
            reads_since_change[p] = 0
        elif e.event_type == EventType.FILE_READ:
            reads_since_change[p] += 1
    if reads_since_change:
        path, r = reads_since_change.most_common(1)[0]
        raw["file_rereads"] = _clamp((r - 2) / 4)
        if r >= 3:
            reasons.append(f"unchanged file re-read {r}x")

    # 7. time without improvement while active
    improve_ts = [e.timestamp for e in events if _is_improvement(e, results)]
    anchor = improve_ts[-1] if improve_ts else events[0].timestamp
    since_tools = sum(1 for e in tools if e.timestamp > anchor)
    mins = (now - anchor) / 60
    if since_tools >= 10:
        raw["no_progress"] = _clamp((mins - 10) / 30)
        if mins >= 15:
            reasons.append(f"no measurable progress for {mins:.0f} min ({since_tools} tool calls)")

    # 8. context growth without progress
    compactions = sum(1 for e in events if e.event_type == EventType.CONTEXT_COMPACTED and e.timestamp > anchor)
    raw["compaction"] = _clamp(0.5 * compactions)
    if compactions:
        reasons.append(f"context compacted {compactions}x since last progress")

    # 9. repeated subagent delegation
    subs = Counter(
        e.metadata.get("desc_sig") for e in events if e.event_type == EventType.SUBAGENT_STARTED and e.metadata.get("desc_sig")
    )
    if subs:
        _, c = subs.most_common(1)[0]
        raw["subagent_repeat"] = _clamp((c - 1) / 3)
        if c >= 2:
            reasons.append(f"same subagent task delegated {c}x")

    weighted = {k: WEIGHTS[k] * v for k, v in raw.items()}
    prod = 1.0
    for v in weighted.values():
        prod *= 1 - v
    score = round(1 - prod, 4)
    return StuckResult(score, band_for(score), weighted, reasons)


def _is_improvement(e: AgentEvent, results: list[AgentEvent]) -> bool:
    if e.event_type == EventType.TEST_RESULT:
        if e.metadata.get("ok"):
            return True
        i = results.index(e)
        if i > 0:
            prev, cur = results[i - 1].metadata.get("failed"), e.metadata.get("failed")
            if prev is not None and cur is not None and int(cur) < int(prev):
                return True
    return e.event_type == EventType.TASK_COMPLETED


@dataclass
class Recommendation:
    intervention: Intervention
    explanation: str


def recommend(
    result: StuckResult,
    *,
    task_value: float = 0.5,
    budget_mode: BudgetMode = BudgetMode.NORMAL,
    model_class: ModelClass | None = None,
    parallel_subagents: int = 0,
    max_parallel: int = 4,
) -> Recommendation:
    why = "; ".join(result.reasons) or "no warning signals"
    if result.band == "healthy":
        return Recommendation(Intervention.CONTINUE, f"stuck {result.score:.2f}: {why}")
    if result.band == "watch":
        return Recommendation(Intervention.CONTINUE, f"watching (stuck {result.score:.2f}): {why}")

    dom = result.dominant
    prefix = f"stuck {result.score:.2f} ({result.band}): {why}."
    if dom == "subagent_repeat" or parallel_subagents > max_parallel:
        return Recommendation(
            Intervention.REDUCE_PARALLELISM,
            f"{prefix} Parallel delegation is not converging; run one focused agent instead.",
        )
    if dom == "compaction":
        return Recommendation(
            Intervention.RESTART_SESSION,
            f"{prefix} Context keeps growing without progress; a fresh session with a short summary is likely cheaper than continuing.",
        )
    if dom == "file_rereads":
        return Recommendation(
            Intervention.COMPACT_CONTEXT,
            f"{prefix} The agent keeps re-reading unchanged files, a sign it lost track of context.",
        )
    if result.band == "likely_stuck":
        if task_value >= 0.6 and budget_mode != BudgetMode.CONSERVATIVE and model_class != ModelClass.PREMIUM:
            return Recommendation(
                Intervention.ESCALATE_MODEL,
                f"{prefix} High-value task and budget allows it: escalate to a stronger model with a fresh plan.",
            )
        if task_value < 0.4:
            return Recommendation(
                Intervention.STOP,
                f"{prefix} Low-value task looping; stop spending on it.",
            )
        return Recommendation(Intervention.ASK_USER, f"{prefix} Needs a human decision before spending more.")
    return Recommendation(
        Intervention.REPLAN,
        f"{prefix} Step back: state the hypothesis, then try a different approach instead of repeating the same one.",
    )
