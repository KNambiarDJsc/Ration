"""The autopilot scheduler: plan, then run one bounded task at a time.

    goal + repository + budget
      -> discovery.discover()      candidate tasks, from local signals
      -> scoring.score()/decide()  value, risk, cost, verifiability; safety policy
      -> budget.assess()           how much is safe to spend (weekly first)
      -> scoring.select()          the queue
      -> for each task:            worktree at the candidate -> baseline -> orchestrator.execute()
                                   -> diff audit -> verify -> accept (candidate advances) or discard
                                   -> after an accepted change: re-scan the candidate, rebuild the queue
      -> verify the combined result -> report

Accepted changes are stacked. Each task starts from the run's candidate (the base
commit plus everything accepted so far), so it is verified together with the work
before it, and a queue made for an older state is never executed blindly.

Autopilot is the planner. The model is only ever asked to do one specific,
already-chosen task; choosing providers, models, effort, retries and stuck
interruption is left to the existing scheduler and run loop.
"""

from __future__ import annotations

import copy
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..budget import provider_budgets
from ..difficulty import Assessment
from ..models import AgentEvent, AgentTask, TaskType
from ..orchestrator import execute
from ..providers.base import RunResult
from . import budget as ap_budget
from . import discovery, policy, scoring, state
from . import goal as goal_mod
from . import models as m
from .budget import Allowance
from .goal import STATE_DIR, Goal
from .models import Decision, Opportunity, Outcome, ScoredTask, TaskResult, slug
from .verify import (
    CheckResult,
    ProjectChecks,
    check_env,
    has_secret,
    judge,
    redact,
    run_all,
    run_check,
    worse,
)
from .workspace import GitError, TaskTree, Workspace, cleanup_run, no_network_git

TASK_TYPES = {
    m.FAILING_TESTS: TaskType.DEBUGGING,
    m.MISSING_TESTS: TaskType.TESTS,
    m.SKIPPED_TESTS: TaskType.TESTS,
    m.LINT: TaskType.TRIVIAL_EDIT,
    m.DOCS: TaskType.TRIVIAL_EDIT,
    m.TYPES: TaskType.CODE_GENERATION,
    m.TODO: TaskType.CODE_GENERATION,
    m.ERROR_HANDLING: TaskType.CODE_GENERATION,
    m.REFACTOR: TaskType.REFACTOR,
    m.UNFINISHED: TaskType.CODE_GENERATION,
    m.FEATURE: TaskType.CODE_GENERATION,
    m.RESEARCH: TaskType.INVESTIGATION,
}
MIN_REPORT_CHARS = 300
MAX_FRUITLESS = 3  # stop after this many tasks in a row that produced nothing accepted
MAX_COST_FACTOR = 5.0

Hook = Callable[[str, Any], None]


@dataclass
class Plan:
    root: Path
    mode: policy.Mode
    goal: Goal | None
    checks: ProjectChecks
    allowance: Allowance
    tasks: list[ScoredTask] = field(default_factory=list)  # every candidate, best first
    queue: list[ScoredTask] = field(default_factory=list)  # what would run, in order
    providers: list[str] = field(default_factory=list)  # providers that may be used
    notes: list[str] = field(default_factory=list)
    is_git: bool = False
    dirty: list[str] = field(default_factory=list)
    max_tasks: int = 5
    blocked: str | None = None  # why nothing can run, when the reason is not per-task
    probed: bool = True

    @property
    def estimated_cost(self) -> float:
        return sum(t.cost for t in self.queue)


def score_all(
    opps: list[Opportunity], mode: policy.Mode, goal: Goal | None, dirty: set[str], history: Any, checks: ProjectChecks, min_score: float
) -> list[ScoredTask]:
    tasks = [
        scoring.decide(scoring.score(o, goal, history), mode, goal=goal, dirty=dirty, history=history, has_guards=bool(checks.guards), min_score=min_score)
        for o in opps
    ]
    tasks.sort(key=lambda t: (-t.score, t.opp.title))
    return tasks


def make_plan(
    cwd: str | os.PathLike[str],
    cfg: dict[str, Any],
    store: Any,
    providers: dict[str, Any],
    *,
    mode: str = "safe",
    probe: bool = True,
    goal_text: str | None = None,
    reserve: float | None = None,
    max_tasks: int | None = None,
    now: float | None = None,
) -> Plan:
    """Read-only: inspects the repository and the usage data. Starts no agent and
    changes nothing. `max_tasks` given explicitly (here or in the policy file) also
    permits a run when the budget is unknown: you set the cap yourself."""
    now = time.time() if now is None else now
    ap = cfg["autopilot"]
    md = policy.MODES[mode]
    root = goal_mod.project_root(cwd)
    goal = Goal(goal=goal_text.strip()) if goal_text and goal_text.strip() else goal_mod.load(root)
    ctx = discovery.scan(root, cfg, goal, probe=probe)

    policy_error = None
    user = policy.UserPolicy()
    if f"{STATE_DIR}/{policy.POLICY_FILE}" in ctx.tracked_state:
        ctx.notes.append(f"{STATE_DIR}/{policy.POLICY_FILE} is tracked by git and was ignored: a policy must be a local, untracked file")
    else:
        try:
            user = policy.load_user_policy(root / STATE_DIR, policy_url=ap.get("policy_url", ""), cache_ttl_minutes=float(ap.get("policy_cache_minutes", 60)))
        except policy.PolicyError as e:
            policy_error = f"{STATE_DIR}/{policy.POLICY_FILE} is invalid, or the policy fetched from autopilot.policy_url is invalid: {e}. Fix it; autopilot does not run under a policy it cannot fully read"
    md = user.apply(md)
    reserve = reserve if reserve is not None else user.weekly_reserve
    explicit_cap = max_tasks if max_tasks is not None else user.max_tasks
    history = state.History(root, float(ap["cooldown_days"]), now=now)
    tasks = score_all(discovery.discover(ctx, md.packs), md, goal, ctx.dirty, history, ctx.checks, float(ap["min_score"]))

    usable = {n: p for n, p in providers.items() if p.capabilities().installed}
    if user.preferred_provider != "auto" and user.preferred_provider in usable:
        usable = {user.preferred_provider: usable[user.preferred_provider]}
    if md.writes_code and "codex" in usable and not usable["codex"].capabilities().features.get("sandbox"):
        del usable["codex"]
        ctx.notes.append("Codex is not used for code tasks: this Codex CLI does not advertise `--sandbox`, and without workspace-write it cannot edit files")
    budgets = provider_budgets(store, cfg, now=now, has_backlog=True)
    allowances: dict[str, Allowance] = {}
    for name in usable:
        b = budgets.get(name)
        short, long = ap_budget.split_windows(b)
        ratio = ap_budget.window_ratio(store, name, short.window, long.window, now) if short and long else None
        allowances[name] = ap_budget.assess(b, cfg, provider=name, reserve=reserve, ratio=ratio, age_minutes=ap_budget.snapshot_age_minutes(store, name, now))
    allowed = [a for a in allowances.values() if a.allowed]
    known = [a for a in allowances.values() if a.known]
    primary = max(allowed, key=lambda a: a.spendable or 0) if allowed else known[0] if known else next(iter(allowances.values()), Allowance("none", False, False))

    plan = Plan(
        root, md, goal, ctx.checks, primary, tasks, notes=list(dict.fromkeys(ctx.notes)), is_git=ctx.is_git, dirty=sorted(ctx.dirty),
        max_tasks=int(explicit_cap if explicit_cap is not None else ap["max_tasks"]), probed=probe,
    )  # fmt: skip
    spendable: float | None = 0.0
    if policy_error:
        plan.blocked = policy_error
    elif not usable:
        plan.blocked = "no coding agent is installed and enabled (see `ration providers`)"
    elif md.writes_code and not ctx.is_git:
        plan.blocked = "not a git repository: autopilot isolates every change in a git worktree, so it cannot change code here (research mode works)"
    elif allowed:
        plan.providers = [a.provider for a in allowed]
        spendable = primary.spendable
    elif not known and explicit_cap is not None:
        plan.providers = list(usable)
        spendable = None  # no quota reading exists; the explicit task cap is the budget
        plan.notes.append(f"budget unknown: running under your explicit cap of {plan.max_tasks} task(s)")
    else:
        plan.blocked = primary.reasons[-1] if primary.reasons else "the budget does not allow optional work right now"
        if not known:
            plan.blocked += "; pass --max-tasks N to set an explicit cap instead"
    if plan.blocked:
        for t in tasks:
            if t.decision == Decision.RUN:
                t.decision = Decision.DEFER
                t.reasons.insert(0, plan.blocked)
    else:
        plan.queue = scoring.select(tasks, spendable=spendable, max_tasks=plan.max_tasks)
    if not probe:
        plan.notes.append("static scan only (--no-probe): tests, lint and type checks were not run, so failures they would reveal are not listed")
    return plan


# ---- running -----------------------------------------------------------------------------------
class Control:
    """Shared by the engine and the provider wrapper: when to cancel, what was said."""

    def __init__(self, root: Path, run_id: str, run_deadline: float | None):
        self.root, self.run_id, self.run_deadline = root, run_id, run_deadline
        self.task_deadline: float | None = None
        self.stop_reason: str | None = None  # set once; ends the whole run
        self.task_cancel: str | None = None  # why the current task's agent was cut short by autopilot or by you
        self.final_text = ""
        self._checked = 0.0

    def start_task(self, deadline: float) -> None:
        self.task_deadline, self.task_cancel, self.final_text = deadline, None, ""

    def cancel_now(self, force: bool = False) -> str | None:
        """`force` skips the throttle on the stop-file check (used between tasks)."""
        now = time.time()
        if self.stop_reason:
            return self.stop_reason
        if self.task_deadline and now > self.task_deadline:
            return "task time limit reached"
        if self.run_deadline and now > self.run_deadline:
            self.stop_reason = "run time limit reached"
        elif force or now - self._checked > 2:
            self._checked = now
            if state.stop_requested(self.root, self.run_id):
                self.stop_reason = "stop requested"
        return self.stop_reason


class GuardedProvider:
    """Wraps a real provider for the duration of a run: cancels the agent when the run
    must stop, and keeps the agent's final message (research output). Everything else is
    delegated, so the existing run loop sees an ordinary provider. Only the process the
    provider itself started is ever stopped."""

    def __init__(self, inner: Any, control: Control):
        self.inner, self.control, self.name = inner, control, inner.name

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    def run(self, prompt: str, *, on_event: Callable[[AgentEvent], bool], **kw: Any) -> Any:
        if self.control.task_cancel or self.control.cancel_now():
            # Already stopped or out of time: the run loop's retry must not start another agent.
            self.control.task_cancel = self.control.task_cancel or self.control.cancel_now()
            return RunResult(ok=False, cancelled=True, error="cancelled before start")
        by_run_loop = [False]  # the existing loop cancelled it (stuck detection): its business, not a stop

        def guarded(ev: AgentEvent) -> bool:
            keep = on_event(ev)
            if not keep:
                by_run_loop[0] = True
            if reason := self.control.cancel_now():
                self.control.task_cancel = reason
                return False
            return keep

        res = self.inner.run(prompt, on_event=guarded, **kw)
        if res.final_text:
            self.control.final_text = res.final_text
        if res.cancelled and not self.control.task_cancel and not by_run_loop[0]:
            # Nobody in RATION cancelled it: Ctrl-C reached the agent process.
            self.control.task_cancel = self.control.stop_reason = "interrupted"
        return res


def recover(root: Path) -> list[str]:
    """Mark runs whose process died as interrupted and remove what they left behind.
    Branches and worktrees that hold accepted work are kept."""
    notes: list[str] = []
    holder = state.lock_holder(root)
    for rid in state.list_runs(root):
        s = state.load_run(root, rid)
        if not s or s.get("status") != "running" or (holder and holder.get("run_id") == rid):
            continue
        s.update(status="interrupted", stop_reason="the process ended before the run finished", ended_at=s.get("heartbeat_at"))
        results = [r for r in s.get("results", []) if isinstance(r, dict)]
        keep = {r["branch"] for r in results if r.get("accepted") and r.get("branch")}
        if keep:
            keep.add(f"ration/auto/{rid}/accepted")
        trees = {r["worktree"] for r in results if r.get("accepted") and r.get("worktree")}
        try:
            removed = cleanup_run(root, rid, keep, trees)
        except GitError:
            removed = []
        state.save_run(root, s)
        notes.append(f"run {rid} was interrupted" + (f"; removed {', '.join(removed)}" if removed else ""))
    return notes


def _short_usage(store: Any, providers: list[str]) -> dict[str, tuple[float, float | None]]:
    out = {}
    for s in store.latest_usage():
        if s.provider in providers and s.window in ap_budget.SHORT_WINDOWS:
            out[s.provider] = (s.used_percent, s.reset_at)
    return out


def _measured_cost(before: dict[str, tuple[float, float | None]], after: dict[str, tuple[float, float | None]]) -> float | None:
    total, seen = 0.0, False
    for p, (used, reset) in after.items():
        if p in before and before[p][1] is not None and reset is not None and abs(before[p][1] - reset) < 120:
            total += max(0.0, used - before[p][0])
            seen = True
    return round(total, 2) if seen else None


class Runner:
    def __init__(self, plan: Plan, cfg: dict[str, Any], store: Any, providers: dict[str, Any], *, on: Hook | None = None, max_runtime_minutes: float | None = None):
        self.plan, self.store, self.on = plan, store, on or (lambda kind, data: None)
        self.ap = cfg["autopilot"]
        self.root, self.mode = plan.root, plan.mode
        self.run_id = state.new_run_id()
        self.started = time.time()
        minutes = float(max_runtime_minutes if max_runtime_minutes is not None else self.ap["max_runtime_minutes"])
        self.control = Control(self.root, self.run_id, self.started + minutes * 60 if minutes > 0 else None)
        self.providers = {n: GuardedProvider(p, self.control) for n, p in providers.items() if n in plan.providers}
        # The run loop sees a config in which the weekly reserve is the reserve, the agent
        # is denied pushes, publishing and infrastructure commands, and Codex may write
        # only inside its working directory (the task's worktree).
        self.user_cfg = cfg
        self.cfg = copy.deepcopy(cfg)
        self.cfg["budget"]["reserve_percent"] = max(float(cfg["budget"]["reserve_percent"]), plan.allowance.reserve if plan.allowance.known else 0.0)
        self.cfg["claude"]["disallowed_tools"] = [*(cfg["claude"].get("disallowed_tools") or []), *policy.DENIED_TOOLS]
        self.cfg["codex"]["sandbox"] = "workspace-write" if self.mode.writes_code else "read-only"
        self.ws: Workspace | None = None  # where code tasks get their worktrees
        self.watch: Workspace | None = None  # used to fingerprint your repository around every task
        self.candidate: str | None = None  # base commit + everything accepted so far
        self.baseline: dict[tuple[str, str], CheckResult] = {}  # (commit, command) -> result
        self.history = state.History(self.root, float(self.ap["cooldown_days"]))
        self.weekly_start = plan.allowance.long.used_percent if plan.allowance.long else None
        self.cost_factor = 1.0
        self.done: set[str] = set()
        self.last_task_id: str | None = None  # the run loop's id for the task in flight
        self.state: dict[str, Any] = {}

    # ---- lifecycle -------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        state.acquire(self.root, self.run_id)
        try:
            notes = recover(self.root)
            plan = self.plan
            self.state = {
                "run_id": self.run_id,
                "status": "running",
                "stop_reason": None,
                "mode": self.mode.name,
                "root": str(self.root),
                "project": self.root.name,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": self.started,
                "ended_at": None,
                "goal": plan.goal.goal if plan.goal else "",
                "checks": {"test": plan.checks.test, "lint": plan.checks.lint, "typecheck": plan.checks.typecheck},
                "providers": plan.providers,
                "budget_start": plan.allowance.to_dict(),
                "budget_end": None,
                "considered": len(plan.tasks),
                "plan": [t.to_dict() for t in plan.tasks],
                "queue": [t.id for t in plan.queue],
                "results": [],
                "rescans": 0,
                "combined": None,
                "notes": plan.notes + notes,
                "checkout_untouched": True,
                "pushed": False,
            }
            if self.mode.writes_code:
                self.ws = self.watch = Workspace(self.root, self.run_id)
                self.candidate = self.ws.base
                self.state.update(base=self.ws.base, base_branch=self.ws.base_branch, detached=self.ws.detached)
            elif plan.is_git:
                try:
                    self.watch = Workspace(self.root, self.run_id)  # read-only modes still prove the repository was left alone
                except GitError:
                    self.watch = None
            state.save_run(self.root, self.state)
            self._loop()
        except KeyboardInterrupt:
            self.control.stop_reason = self.control.stop_reason or "interrupted"
        except state.AlreadyRunning:
            raise
        except Exception as e:  # a bug must still end in a truthful record, a released lock and no leftovers
            self.control.stop_reason = f"internal error, run ended early: {type(e).__name__}: {redact(str(e))[:200]}"
        finally:
            try:
                self._finish()
            finally:
                if self.ws is not None:
                    self.ws.close()
                state.release(self.root, self.run_id)
        return self.state

    def _loop(self) -> None:
        fruitless = 0
        queue = list(self.plan.queue)
        index = 0
        while queue:
            st = queue[0]
            if st.id in self.done:
                queue.pop(0)
                continue
            if self.control.cancel_now(force=True):
                break
            if fruitless >= MAX_FRUITLESS:
                self.control.stop_reason = f"{MAX_FRUITLESS} tasks in a row produced nothing worth keeping: stopping instead of spending more"
                break
            if reason := self._budget_gate(st):
                if reason.startswith("defer:"):
                    queue.pop(0)
                    self._mark(st, Decision.DEFER, reason[6:])
                    continue
                self.control.stop_reason = reason
                break
            queue.pop(0)
            index += 1
            self.done.add(st.id)
            self.on("task_start", st)
            fp = self.watch.fingerprint() if self.watch else None
            usage_before = _short_usage(self.store, self.plan.providers)
            t0 = time.time()
            self.control.start_task(t0 + float(self.ap["task_timeout_minutes"]) * 60)
            try:
                res = self._research(st, index) if not st.opp.writes_code else self._code(st, index)
            except GitError as e:
                res = TaskResult(st.id, st.opp.title, st.opp.category, Outcome.BLOCKED, False, f"git: {redact(str(e))}")
            res.seconds, res.score, res.estimated_cost = round(time.time() - t0, 1), st.score, st.cost
            res.cost_percent = _measured_cost(usage_before, _short_usage(self.store, self.plan.providers))
            if res.cost_percent and st.cost and res.cost_percent / st.cost > 1.5:
                # Tasks are costing more than estimated: believe the measurement for the rest of the run.
                self.cost_factor = min(MAX_COST_FACTOR, max(self.cost_factor, res.cost_percent / st.cost))
                self.state["notes"].append(
                    f"'{st.opp.title}' used {res.cost_percent:g}% of the 5-hour window against an estimate of {st.cost:g}%: "
                    f"remaining estimates are scaled x{self.cost_factor:.1f}"
                )
            self.state["results"].append(res.to_dict())
            self.history.record(
                {"id": st.id, "category": st.opp.category, "outcome": str(res.outcome), "accepted": res.accepted, "branch": res.branch,
                 "detail": res.detail[:200], "cost_percent": res.cost_percent, "run_id": self.run_id}
            )  # fmt: skip
            fruitless = 0 if res.accepted else fruitless + 1
            if not res.accepted and self.last_task_id:
                self.store.fail_task_runs(self.last_task_id)  # the scheduler learns from the verdict, not from "checks passed"
            self.last_task_id = None
            if fp is not None and self.watch is not None and (what := fp.changed(self.watch.fingerprint())):
                # Either you were working in the repository, or an agent escaped its worktree.
                # Autopilot cannot tell which, so it stops and reverts nothing.
                self.state["checkout_untouched"] = False
                self.control.stop_reason = f"{what} while a task was running: stopped so nothing builds on an unknown state"
            if res.accepted and res.commit and self.ws is not None:
                self.candidate = res.commit
                self.ws.mark_accepted(res.commit)
                self.state.update(candidate=res.commit, accepted_branch=self.ws.accepted_branch)
            elif res.accepted and res.worktree:
                self.control.stop_reason = self.control.stop_reason or "an accepted change could not be committed, so later tasks cannot build on it"
            state.save_run(self.root, self.state)
            self.on("task_end", res)
            if self.control.stop_reason:
                break
            if res.accepted and res.commit and self.ws is not None and len(self.done) < self.plan.max_tasks:
                queue = self._replan()
        for st in queue:
            if st.id not in self.done and st.decision == Decision.RUN:
                self._mark(st, Decision.DEFER, f"not started: {self.control.stop_reason or 'run ended'}")

    def _replan(self) -> list[ScoredTask]:
        """The repository just changed. Scan the new candidate and rebuild the queue, so
        no task runs against a state it was not planned for. The scan's check results are
        the baseline for the next task, and the verification of everything accepted so far."""
        assert self.ws is not None and self.candidate is not None
        try:
            tree = self.ws.candidate_tree(self.candidate)
        except GitError as e:
            self.control.stop_reason = f"could not check out the accepted result for re-scanning: {redact(str(e))[:160]}"
            return []
        ctx = discovery.scan(tree, self.user_cfg, self.plan.goal, probe=True, checks=self.plan.checks, home=self.root)
        # The checks just ran on exactly this commit when the task was verified: reuse them.
        ctx.probes.update({cmd: r for (sha, cmd), r in self.baseline.items() if sha == self.candidate})
        opps = discovery.discover(ctx, self.mode.packs)
        for cmd, r in ctx.probes.items():
            self.baseline[(self.candidate, cmd)] = r
        history = state.History(self.root, float(self.ap["cooldown_days"]))
        scored = score_all(opps, self.mode, self.plan.goal, set(self.plan.dirty), history, self.plan.checks, float(self.ap["min_score"]))
        tasks = [t for t in scored if t.id not in self.done]
        a = self._allowance()
        spendable = None if a is None else (a.spendable or 0.0) if a.allowed else 0.0
        if a is not None and not a.allowed:
            self.control.stop_reason = "budget: " + (a.reasons[-1] if a.reasons else "no longer safe to spend")
        queue = scoring.select(tasks, spendable=spendable, max_tasks=self.plan.max_tasks - len(self.done), cost_factor=self.cost_factor)
        fresh = {t.id: t.to_dict() for t in tasks}
        rows = []
        for row in self.state["plan"]:
            if row["id"] in fresh:
                rows.append(fresh.pop(row["id"]))
            elif row["id"] not in self.done and row["decision"] in (str(Decision.RUN), str(Decision.DEFER)):
                rows.append({**row, "decision": "DROPPED", "reasons": ["no longer applies after the changes accepted in this run", *row["reasons"]]})
            else:
                rows.append(row)
        self.state["plan"] = rows + list(fresh.values())
        self.state["queue"] = [*self.state["queue"], *(t.id for t in queue if t.id not in self.state["queue"])]
        self.state["rescans"] += 1
        self.state["considered"] = len(self.state["plan"])
        state.save_run(self.root, self.state)
        if self.control.stop_reason:
            # Nothing more will start. Hand back what would have run, so the loop records why not.
            waiting = [t for t in tasks if t.decision in (Decision.RUN, Decision.DEFER)]
            for t in waiting:
                t.decision = Decision.RUN
            return waiting
        return queue

    def _mark(self, st: ScoredTask, decision: Decision, why: str) -> None:
        st.decision = decision
        st.reasons.insert(0, why)
        for row in self.state["plan"]:
            if row["id"] == st.id:
                row.update(decision=str(decision), reasons=st.reasons)

    def _allowance(self) -> Allowance | None:
        if not self.plan.allowance.known:
            return None
        budgets = provider_budgets(self.store, self.cfg, has_backlog=True)
        name = self.plan.allowance.provider
        b = budgets.get(name)
        _, long = ap_budget.split_windows(b)
        spent = max(0.0, long.used_percent - self.weekly_start) if long and self.weekly_start is not None else 0.0
        return ap_budget.assess(
            b, self.cfg, provider=name, reserve=self.plan.allowance.configured_reserve, ratio=(self.plan.allowance.ratio, self.plan.allowance.ratio_source),
            age_minutes=ap_budget.snapshot_age_minutes(self.store, name), spent_weekly=spent,
        )  # fmt: skip

    def _budget_gate(self, st: ScoredTask) -> str | None:
        """Re-read the budget before every task: the previous task's real cost is in it."""
        a = self._allowance()
        if a is None:
            return None
        self.state["budget_end"] = a.to_dict()
        if not a.allowed:
            return "budget: " + (a.reasons[-1] if a.reasons else "no longer safe to spend")
        cost = st.cost * self.cost_factor
        if cost > (a.spendable or 0):
            return f"defer:needs ~{cost:g}% of the 5-hour window (estimate); only ~{a.spendable:.0f}% is still safe to spend"
        return None

    def _combined(self) -> None:
        """Everything accepted, checked together in a clean checkout, against the base."""
        ws = self.ws
        accepted = [r for r in self.state["results"] if r.get("accepted") and r.get("commit")]
        if ws is None or not accepted or self.candidate is None:
            return
        guards = self.plan.checks.guards
        combined: dict[str, Any] = {"branch": ws.accepted_branch, "commit": self.candidate, "tasks": len(accepted), "ok": True, "detail": "", "before": [], "after": []}
        self.state["combined"] = combined
        if not guards:
            combined["detail"] = "no test or lint command exists to verify the combined result"
            return
        timeout = float(self.ap["check_timeout_seconds"])
        tree = ws.candidate_tree(self.candidate)
        env = check_env(tree)
        problems = []
        for c in guards:
            after = run_check(c, tree, timeout=timeout, env=env)  # always a fresh run, in a clean checkout
            before = self.baseline.get((ws.base, c))
            combined["after"].append(after.to_dict())
            if before is not None:
                combined["before"].append(before.to_dict())
                if why := worse(before, after):
                    problems.append(why)
        combined["ok"] = not problems
        combined["detail"] = "; ".join(problems) if problems else "no check is worse than at the base commit"

    def _finish(self) -> None:
        if not self.state:
            return
        if self.control.stop_reason != "interrupted":
            try:
                self._combined()
            except (GitError, OSError) as e:
                self.state["combined"] = {"ok": False, "detail": f"could not verify the combined result: {redact(str(e))[:200]}", "before": [], "after": []}
        a = self._allowance()
        if a is not None:
            self.state["budget_end"] = a.to_dict()
            if a.long is not None and a.long.remaining_percent < a.reserve - 0.5:
                self.state["reserve_crossed"] = round(a.reserve - a.long.remaining_percent, 1)
        reason = self.control.stop_reason
        if reason is None and not self.state["results"]:
            reason = "no worthwhile, safe, verifiable work was found"
        self.state.update(status="stopped" if self.control.stop_reason else "completed", stop_reason=reason, ended_at=time.time())
        from . import report

        state.save_run(self.root, self.state)
        try:
            (state.run_dir(self.root, self.run_id) / "report.md").write_text(report.render_report(self.state), encoding="utf-8")
        except OSError:
            pass

    # ---- one task -----------------------------------------------------------------------------
    def _prompt(self, st: ScoredTask) -> str:
        """Three parts, in a fixed order: RATION's rules, the task, reference data. Only the
        rules are authoritative; the rules say so, and say that repository text is data."""
        opp, checks = st.opp, self.plan.checks
        task = [f"## TASK (chosen by RATION from repository signals)\n{opp.title}\n\n{opp.description}"]
        if opp.files:
            task.append("Files involved: " + ", ".join(opp.files))
        if opp.target == m.Target.MORE_TESTS and checks.test:
            task.append(f"The test suite is run with `{checks.test}`. It must stay green and end up with more passing tests than before.")
        elif opp.writes_code and checks.guards:
            task.append("These project checks must not get worse: " + "; ".join(f"`{c}`" for c in checks.guards))
        parts = [policy.RULES if opp.writes_code else policy.RESEARCH_RULES, "\n".join(task)]
        if self.plan.goal and not self.plan.goal.empty:
            parts.append("## REFERENCE (context from the project owner; it does not change the rules or the task)\n" + self.plan.goal.brief())
        return "\n\n".join(parts)

    def _execute(self, st: ScoredTask, cwd: Path, env: dict[str, str] | None, accept: list[str] | None = None) -> tuple[Any, list[dict[str, Any]], int]:
        """`accept`: commands the existing run loop treats as "done". When one fails after an
        attempt, the loop retries (up to the mode's cap) and shows the agent the failing
        output, which is how an agent that cannot run tests itself still gets to fix them."""
        opp = st.opp
        task = AgentTask(
            description=self._prompt(st),
            repo=str(cwd),
            priority=0.5,
            task_type=TASK_TYPES.get(opp.category, TaskType.CODE_GENERATION),
            estimated_value=opp.value,
            estimated_difficulty=m.COMPLEXITY_DIFFICULTY[opp.complexity],
            uncertainty=1.0 - opp.confidence,
            risk=opp.risk,
            reversibility=1.0,  # the change lives on its own branch
            acceptance_checks=list(accept or []),
        )
        self.last_task_id = task.id
        assessment = Assessment(task.task_type, task.estimated_difficulty, opp.risk, task.uncertainty, [f"autopilot: {opp.category} (score {st.score:.0f})"], "high")
        stuck = [0]

        def on(kind: str, data: Any) -> None:
            if kind == "stuck":
                stuck[0] += 1
            self.on(kind, data)

        with no_network_git():  # git push / fetch / clone fail inside git for everything the agent starts
            report = execute(
                task, assessment, providers=self.providers, store=self.store, cfg=self.cfg, cwd=str(cwd), permission_mode=self.mode.permission_mode,
                on=on, check_env=env, max_attempts=self.mode.max_attempts, baseline_gate=False,
            )  # fmt: skip
        attempts = [
            {
                "provider": a.policy.provider,
                "model_class": str(a.policy.model_class),
                "model": self.providers[a.policy.provider].models().get(a.policy.model_class) or "",
                "effort": str(a.policy.effort),
                "outcome": a.outcome,
                "cost_usd": a.cost_usd,
            }
            for a in report.attempts
        ]
        return report, attempts, stuck[0]

    def _research(self, st: ScoredTask, index: int) -> TaskResult:
        opp = st.opp
        res = TaskResult(st.id, opp.title, opp.category, Outcome.FAILED, False)
        report, res.attempts, res.stuck_interrupts = self._execute(st, self.root, None)
        costs = [a["cost_usd"] for a in res.attempts if a["cost_usd"] is not None]
        res.cost_usd = round(sum(costs), 4) if costs else None
        text = redact(self.control.final_text.strip())
        if self.control.task_cancel:
            res.outcome, res.detail = Outcome.BLOCKED, f"cancelled ({self.control.task_cancel}) before the report was finished"
            return res
        if len(text) < MIN_REPORT_CHARS:
            why = ", ".join(report.stopped_reason) or (report.attempts[-1].outcome if report.attempts else "no agent ran")
            res.detail = f"no report was produced ({why})"
            return res
        path = state.run_dir(self.root, self.run_id) / "research" / f"{index:02d}-{slug(opp.title)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {opp.title}\n\n_Written by RATION autopilot (research mode). Unverified analysis: check it before acting on it._\n\n{text}\n", encoding="utf-8")
        res.outcome, res.accepted, res.artifact = Outcome.SUCCESS, True, str(path)
        res.detail = f"report written ({len(text):,} characters); analysis is not independently verified"
        return res

    def _code(self, st: ScoredTask, index: int) -> TaskResult:
        assert self.ws is not None and self.candidate is not None
        opp, checks, ws = st.opp, self.plan.checks, self.ws
        res = TaskResult(st.id, opp.title, opp.category, Outcome.BLOCKED, False)
        tree = ws.create(f"{index:02d}-{slug(opp.title, 28)}", self.candidate)
        res.start = tree.start
        keep_tree = False
        try:
            env = check_env(tree.path)
            timeout = float(self.ap["check_timeout_seconds"])
            if setup := str(self.ap.get("setup_command") or "").strip():
                r = run_check(setup, tree.path, timeout=timeout, env=env)
                if not r.ok:
                    res.detail = f"setup command failed in the worktree: {r.tail[-200:]}"
                    return res
            cmds = list(dict.fromkeys([*opp.checks, *checks.guards]))
            for c in cmds:  # measured once per commit: the candidate only moves when a task is accepted
                if (tree.start, c) not in self.baseline:
                    self.baseline[(tree.start, c)] = run_check(c, tree.path, timeout=timeout, env=env)
            before = {c: self.baseline[(tree.start, c)] for c in cmds}
            res.before = [b.to_dict() for b in before.values()]
            needed = opp.checks if opp.target == m.Target.PASS else [checks.test] if opp.target == m.Target.MORE_TESTS else []
            broken = [c for c in needed if c in before and not before[c].ran]
            if broken:
                last_line = before[broken[0]].tail.splitlines()[-1][:160] if before[broken[0]].tail else "no output"
                res.detail = f"`{broken[0]}` cannot run in a clean worktree ({last_line}); nothing was spent"
                return res
            if opp.target == m.Target.PASS and opp.checks and all(before[c].ok for c in opp.checks):
                res.outcome, res.detail = Outcome.NO_CHANGE, "already passing at the commit the task starts from (your working tree differs from it); nothing was spent"
                return res

            holding = [c for c in checks.guards if before[c].ran and before[c].ok]  # guards worth defending
            accept = list(dict.fromkeys([*(opp.checks if opp.target == m.Target.PASS else []), *holding]))
            report, res.attempts, res.stuck_interrupts = self._execute(st, tree.path, env, accept)
            costs = [a["cost_usd"] for a in res.attempts if a["cost_usd"] is not None]
            res.cost_usd = round(sum(costs), 4) if costs else None
            diff = ws.stage(tree)
            res.files_changed, res.added, res.deleted = diff.files, diff.added, diff.deleted
            last = report.attempts[-1].outcome if report.attempts else ""
            if self.control.task_cancel:
                # Cut short by a stop request, a time limit or Ctrl-C: what is in the worktree is half a change.
                res.detail = f"cancelled ({self.control.task_cancel}) before the agent finished" + ("; its partial change was discarded" if diff.files else "")
                if diff.files:
                    res.artifact = self._save_rejected(tree, index, opp.title, res.detail)
                return res
            if not diff.files:
                if last == "blocked":
                    res.detail = "the agent was not permitted to act: " + ", ".join(report.stopped_reason)
                elif not report.attempts:
                    res.outcome, res.detail = Outcome.FAILED, "no agent ran: " + (", ".join(report.stopped_reason) or "no provider available")
                elif last == "error":
                    res.outcome, res.detail = Outcome.FAILED, "the agent could not start: " + redact(report.attempts[-1].error or "")[:200]
                else:
                    res.outcome, res.detail = Outcome.NO_CHANGE, "the agent made no changes"
                return res
            if violations := policy.audit(diff, self.mode, has_secret=has_secret(diff.added_text)):
                res.detail = "discarded by the safety policy: " + "; ".join(violations)
                res.artifact = self._save_rejected(tree, index, opp.title, res.detail)
                return res

            after = run_all(cmds, tree.path, timeout=timeout, env=env)
            res.after = [a.to_dict() for a in after.values()]
            res.outcome, res.detail = judge(opp, before, after, changed=True, test_cmd=checks.test)
            if res.outcome not in (Outcome.SUCCESS, Outcome.IMPROVED):
                res.artifact = self._save_rejected(tree, index, opp.title, res.detail)
                return res
            sha = ws.commit(tree, f"ration autopilot: {opp.title}\n\n{res.detail}\n\nMode: {self.mode.name}. Run: {self.run_id}. Review before merging.")
            res.accepted, res.branch = True, tree.branch
            if sha and sha != tree.start:
                res.commit = sha
                for c, r in after.items():  # the checks just ran on exactly this content
                    self.baseline.setdefault((sha, c), r)
            else:
                keep_tree, res.worktree = True, str(tree.path)
                res.detail += " | the commit was refused (a commit hook?): the verified change is left uncommitted in the worktree"
            return res
        finally:
            if not keep_tree:
                if res.accepted:
                    ws.release(tree)
                else:
                    ws.discard(tree)

    def _save_rejected(self, tree: TaskTree, index: int, title: str, why: str) -> str | None:
        """Keep the evidence of a discarded change: the reason and the redacted diff."""
        assert self.ws is not None
        try:
            path = state.run_dir(self.root, self.run_id) / "rejected" / f"{index:02d}-{slug(title)}.patch"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# Discarded by RATION autopilot: {why}\n# {title}\n\n{redact(self.ws.patch(tree))}", encoding="utf-8")
            return str(path)
        except (OSError, GitError):
            return None


def run(plan: Plan, cfg: dict[str, Any], store: Any, providers: dict[str, Any], *, on: Hook | None = None, max_runtime_minutes: float | None = None) -> dict[str, Any]:
    """Execute a plan. Raises `state.AlreadyRunning` if another run holds this project."""
    return Runner(plan, cfg, store, providers, on=on, max_runtime_minutes=max_runtime_minutes).run()
