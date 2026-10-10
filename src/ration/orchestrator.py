"""Schedule mode: `ration run "<task>"`.

decide -> run -> verify (acceptance checks) -> on failure: escalate / reroute /
stop, up to the retry budget. Every decision is logged as (state, action,
outcome) for later research.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import progress as progress_mod
from .budget import ProviderBudget, provider_budgets
from .difficulty import Assessment
from .models import AgentEvent, AgentTask, EventType, ExecutionPolicy, ModelClass, TaskType, new_id
from .normalize import parse_test_output
from .providers.base import AgentProvider
from .scheduler import Attempt, ProviderOption, Scheduler, SchedulerConfig
from .shell import shell_argv
from .store import Store
from .stuck import detect


@dataclass
class AttemptReport:
    policy: ExecutionPolicy
    outcome: str
    stuck: float
    progress: float
    checks_passed: int
    checks_total: int
    cost_usd: float | None
    error: str | None = None


@dataclass
class RunReport:
    task: AgentTask
    assessment: Assessment
    attempts: list[AttemptReport] = field(default_factory=list)
    completed: bool = False
    stopped_reason: list[str] = field(default_factory=list)
    wall_time_s: float = 0.0


Hook = Callable[[str, Any], None]


def run_checks(checks: list[str], cwd: str, session_id: str, run_id: str, task_id: str, provider: str = "ration") -> tuple[int, list[AgentEvent], str]:
    passed = 0
    events: list[AgentEvent] = []
    tail = ""
    for cmd in checks:
        argv = shell_argv(cmd)
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=900, shell=isinstance(argv, str), text=True, encoding="utf-8", errors="replace")
            out, ok = (r.stdout or "") + (r.stderr or ""), r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            out, ok = str(e), False
        meta = parse_test_output(out)
        meta.update({"ok": ok, "check": True})
        events.append(AgentEvent(provider, session_id, EventType.TEST_RESULT, meta, run_id=run_id, task_id=task_id))
        passed += int(ok)
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-25:])
    return passed, events, tail


def check_score(events: list[AgentEvent], passed: int, total: int) -> float:
    """Verified progress in 0..1: test pass ratio when counts are known, else checks passed."""
    p = sum(int(e.metadata.get("passed") or 0) for e in events)
    f = sum(int(e.metadata.get("failed") or 0) for e in events)
    if p + f:
        return p / (p + f)
    return passed / total if total else 0.0


def build_prompt(task: AgentTask, history: list[tuple[AttemptReport, str, list[str]]]) -> str:
    parts = [task.description]
    if task.acceptance_checks:
        parts.append("Done means these commands pass:\n" + "\n".join(f"  {c}" for c in task.acceptance_checks))
    if history:
        rep, tail, reasons = history[-1]
        parts.append(
            f"A previous attempt ({rep.policy.model_class} model) did not finish: outcome={rep.outcome}."
            + (f" Loop signals: {'; '.join(reasons)}." if reasons else "")
            + " Do not repeat the same approach; form a new hypothesis first."
            + (f"\nLast check output:\n{tail}" if tail else "")
        )
    return "\n\n".join(parts)


def execute(
    task: AgentTask,
    assessment: Assessment,
    *,
    providers: dict[str, AgentProvider],
    store: Store,
    cfg: dict[str, Any],
    cwd: str,
    dry_run: bool = False,
    interrupt_when_stuck: bool = True,
    permission_mode: str | None = None,
    on: Hook | None = None,
    budgets_fn: Callable[[], dict[str, ProviderBudget]] | None = None,
) -> RunReport:
    on = on or (lambda kind, data: None)
    t0 = time.time()
    report = RunReport(task, assessment)
    store.add_task(task, {"assessment": assessment.reasons, "uncertainty": task.uncertainty})

    def success(p: str, mc: ModelClass, tt: TaskType) -> tuple[int, int]:
        return store.provider_success(p, str(mc), str(tt))

    options = [
        ProviderOption(name, prov.models(), available=prov.capabilities().installed)
        for name, prov in providers.items()
    ]
    attempts: list[Attempt] = []
    history: list[tuple[AttemptReport, str, list[str]]] = []

    # Baseline: run the acceptance checks once before spending anything. If they
    # already pass there is nothing to do; otherwise progress is measured against it.
    last_score: float | None = None
    if task.acceptance_checks and not dry_run:
        b_passed, b_events, _ = run_checks(task.acceptance_checks, cwd, "baseline", "baseline", task.id)
        store.add_events(b_events)
        last_score = check_score(b_events, b_passed, len(task.acceptance_checks))
        on("baseline", b_events)
        if b_passed == len(task.acceptance_checks):
            report.completed = True
            report.stopped_reason = ["checks_already_pass"]
            store.record_outcome(task.id, True, b_passed, b_passed, 0, time.time() - t0, 0.0)
            store.set_task_status(task.id, "done")
            report.wall_time_s = time.time() - t0
            return report

    while True:
        if "codex" in providers and hasattr(providers["codex"], "ingest_usage"):
            providers["codex"].ingest_usage(store)  # type: ignore[attr-defined]
        budgets = budgets_fn() if budgets_fn else provider_budgets(store, cfg, has_backlog=True)
        sched = Scheduler(options, budgets, SchedulerConfig.from_config(cfg), success)
        policy = sched.decide(task, attempts)
        state = {
            "task": {"type": str(task.task_type), "difficulty": task.estimated_difficulty, "value": task.estimated_value, "risk": task.risk},
            "budgets": {n: {"remaining": b.remaining_percent, "pressure": b.burn_pressure, "mode": str(b.mode)} for n, b in budgets.items()},
            "attempts": [{"provider": a.provider, "class": str(a.model_class), "outcome": a.outcome, "stuck": a.stuck_score} for a in attempts],
        }
        decision_id = store.add_decision(task.id, None, state, policy.to_dict(), policy.reasons)
        on("decision", policy)
        if policy.stop:
            report.stopped_reason = policy.reasons
            break
        if dry_run:
            store.set_task_status(task.id, "planned")
            break

        provider = providers[policy.provider]
        run_id = new_id("run")
        parent = None
        store.start_run(
            run_id,
            policy.provider,
            task.id,
            model=provider.models().get(policy.model_class),
            model_class=str(policy.model_class),
            effort=str(policy.effort),
            parent_run_id=parent,
        )
        run_events: list[AgentEvent] = []
        stuck_flagged = {"v": False}

        def on_event(
            ev: AgentEvent,
            run_events: list[AgentEvent] = run_events,
            stuck_flagged: dict[str, bool] = stuck_flagged,
            run_id: str = run_id,
        ) -> bool:
            store.add_event(ev)
            run_events.append(ev)
            on("event", ev)
            if ev.event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED, EventType.TEST_RESULT):
                res = detect(run_events, now=ev.timestamp)
                if res.band == "likely_stuck" and not stuck_flagged["v"]:
                    stuck_flagged["v"] = True
                    store.add_event(AgentEvent(ev.provider, ev.session_id, EventType.STUCK_DETECTED, {"score": res.score, "reasons": res.reasons}, run_id=run_id, task_id=task.id))
                    on("stuck", res)
                    if interrupt_when_stuck:
                        store.add_event(AgentEvent(ev.provider, ev.session_id, EventType.RUN_STOPPED, {"why": "likely_stuck"}, run_id=run_id, task_id=task.id))
                        return False
            return True

        prompt = build_prompt(task, history)
        result = provider.run(prompt, policy=policy, cwd=cwd, run_id=run_id, task_id=task.id, on_event=on_event, permission_mode=permission_mode)  # type: ignore[call-arg]

        for snap in result.usage:  # official quota readings seen during the run
            store.add_usage(snap)
        sid = result.session_id or run_id
        verified_velocity = 0.0
        if task.acceptance_checks:
            passed, check_events, tail = run_checks(task.acceptance_checks, cwd, sid, run_id, task.id, provider=policy.provider)
            for e in check_events:
                on_event(e)
            total = len(task.acceptance_checks)
            verified = passed == total
            score = check_score(check_events, passed, total)
            # "Progress" means the checks got better, not that the agent was busy.
            verified_velocity = score - last_score if last_score is not None else 0.0
            last_score = score
        else:
            passed, total, tail, verified = 0, 0, "", result.ok

        res = detect(run_events)
        prog = progress_mod.compute(run_events)
        store.add_progress(sid, run_id, prog.score, res.score, prog.reasons + res.reasons)
        changed_files = any(e.event_type == EventType.FILE_CHANGED for e in run_events)
        if verified:
            outcome = "succeeded"
        elif result.permission_denials and not changed_files:
            outcome = "blocked"  # the agent wasn't allowed to act; retrying won't help
        elif stuck_flagged["v"] or res.band in ("intervene", "likely_stuck"):
            outcome = "stuck"
        elif result.error and not result.cancelled and not run_events:
            outcome = "error"
        else:
            outcome = "failed"
        store.end_run(run_id, outcome, result.cost_usd, result.input_tokens, result.output_tokens, session_id=result.session_id)
        store.set_decision_outcome(decision_id, {"outcome": outcome, "checks_passed": passed, "checks_total": total, "stuck": res.score, "progress": prog.score, "cost_usd": result.cost_usd})

        rep = AttemptReport(policy, outcome, res.score, prog.score, passed, total, result.cost_usd, result.error)
        report.attempts.append(rep)
        on("attempt", rep)
        attempts.append(Attempt(policy.provider, policy.model_class, outcome, res.score, verified_velocity))
        history.append((rep, tail, res.reasons))
        if outcome == "succeeded":
            report.completed = True
            break
        if outcome == "blocked":
            kinds = sorted(set(result.permission_denials))
            report.stopped_reason = [f"permission_denied:{','.join(kinds)}"]
            on("blocked", result.denial_reason)
            break
        if outcome == "error" and result.error:
            # Provider failed to even start (auth, missing binary...): don't retry blindly.
            options = [o for o in options if o.name != policy.provider]
            if not options:
                report.stopped_reason = [f"provider_error:{result.error[:120]}"]
                break

    report.wall_time_s = time.time() - t0
    if not dry_run:
        costs = [a.cost_usd for a in report.attempts if a.cost_usd is not None]
        last = report.attempts[-1] if report.attempts else None
        store.record_outcome(
            task.id,
            report.completed,
            last.checks_passed if last else None,
            last.checks_total if last else None,
            len(report.attempts),
            report.wall_time_s,
            sum(costs) if costs else None,
        )
        store.set_task_status(task.id, "done" if report.completed else "stopped")
    return report
