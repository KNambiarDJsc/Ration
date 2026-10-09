"""Reproducible routing benchmark (synthetic world).

Compares, under identical normalized budgets:
  A  premium model always
  B  cheapest model always
  C  static hand-written routing (by task type)
  D  RATION adaptive scheduler (the real `Scheduler`, real budget engine)
  D- RATION without stuck interruption (isolates that one assumption)

Everything about the world is an explicit, seeded parameter below. Results are
only as good as these assumptions; they are NOT evidence about real agents.
Use them to check policy behaviour, then validate on real task suites.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

from ..budget import ProviderBudget, window_budget
from ..models import (
    AgentTask,
    Confidence,
    ModelClass,
    ResourceType,
    Source,
    TaskType,
    UsageSnapshot,
)
from ..scheduler import Attempt, ProviderOption, Scheduler, SchedulerConfig

WEEK_H = 7 * 24.0


@dataclass
class SimProvider:
    name: str
    budget: float  # normalized units per weekly window
    skill: dict[ModelClass, float]  # higher = solves harder tasks
    cost: dict[ModelClass, float]  # units per attempt
    minutes: dict[ModelClass, float]


def default_world() -> list[SimProvider]:
    C, S, P = ModelClass.CHEAP, ModelClass.STRONG, ModelClass.PREMIUM
    return [
        SimProvider("claude", 500, {C: 0.40, S: 0.62, P: 0.80}, {C: 1, S: 3, P: 8}, {C: 4, S: 8, P: 15}),
        SimProvider("codex", 400, {C: 0.42, S: 0.60, P: 0.76}, {C: 1, S: 2.5, P: 6}, {C: 4, S: 8, P: 14}),
        SimProvider("local", 250, {C: 0.30}, {C: 0.3}, {C: 10}),
    ]


TYPES_BY_DIFF = [
    (0.2, TaskType.TRIVIAL_EDIT),
    (0.35, TaskType.TESTS),
    (0.5, TaskType.CODE_GENERATION),
    (0.6, TaskType.REFACTOR),
    (0.75, TaskType.DEBUGGING),
    (1.01, TaskType.ARCHITECTURE),
]


@dataclass
class SimTask:
    task: AgentTask
    true_difficulty: float
    arrival_h: float


def make_tasks(n: int, rng: random.Random) -> list[SimTask]:
    out = []
    for i in range(n):
        d = rng.betavariate(2, 2)
        value = rng.choices([0.2, 0.5, 0.9], weights=[0.35, 0.45, 0.2])[0]
        ttype = next(t for lim, t in TYPES_BY_DIFF if d < lim)
        est = min(1.0, max(0.0, d + rng.gauss(0, 0.12)))
        task = AgentTask(
            description=f"task {i}",
            task_type=ttype,
            priority=value,
            estimated_value=value,
            estimated_difficulty=est,
            risk=0.7 if ttype == TaskType.ARCHITECTURE else 0.3,
            id=f"t{i}",
        )
        out.append(SimTask(task, d, rng.uniform(0, WEEK_H * 0.95)))
    return sorted(out, key=lambda s: s.arrival_h)


def p_success(skill: float, difficulty: float) -> float:
    return 1 / (1 + math.exp(-(skill - difficulty) * 9))


@dataclass
class Metrics:
    policy: str
    resolved: int = 0
    value: float = 0.0
    spent: float = 0.0
    stuck_spent: float = 0.0
    wall_min: float = 0.0
    attempts: int = 0
    premium_spent: float = 0.0
    unused_at_reset: float = 0.0  # % of limited budget left unspent
    exhausted_early_h: float = 0.0  # hours before reset the first provider ran out
    blocked: int = 0  # tasks that could not run for lack of budget

    @property
    def per_resolved(self) -> float:
        return self.spent / self.resolved if self.resolved else math.inf


@dataclass
class World:
    providers: list[SimProvider]
    used: dict[str, float] = field(default_factory=dict)
    history: dict[str, list[UsageSnapshot]] = field(default_factory=lambda: defaultdict(list))
    exhausted_at: float | None = None

    def remaining(self, p: SimProvider) -> float:
        return p.budget - self.used.get(p.name, 0.0)

    def snapshot(self, p: SimProvider, now_h: float) -> UsageSnapshot:
        return UsageSnapshot(
            provider=p.name,
            window="seven_day",
            resource_type=ResourceType.USAGE_PERCENT,
            used_percent=100 * self.used.get(p.name, 0.0) / p.budget,
            reset_at=WEEK_H * 3600,
            window_minutes=int(WEEK_H * 60),
            source=Source.ESTIMATED,
            confidence=Confidence.HIGH,
            captured_at=now_h * 3600,
        )

    def charge(self, p: SimProvider, units: float, now_h: float) -> None:
        self.used[p.name] = self.used.get(p.name, 0.0) + units
        self.history[p.name].append(self.snapshot(p, now_h))
        if self.remaining(p) <= 0 and self.exhausted_at is None:
            self.exhausted_at = now_h


def _attempt(world: World, p: SimProvider, mc: ModelClass, st: SimTask, rng: random.Random, now_h: float, m: Metrics, interrupt: bool) -> tuple[bool, bool, bool]:
    """Returns (ran, success, stuck). A failed attempt is 'stuck' half the time: it loops
    and costs 2x, unless stuck interruption cuts it at 0.6x."""
    cost = p.cost[mc]
    if world.remaining(p) < cost:
        return False, False, False
    ok = rng.random() < p_success(p.skill[mc], st.true_difficulty)
    stuck = (not ok) and rng.random() < 0.5
    if stuck:
        cost *= 0.6 if interrupt else 2.0
        cost = min(cost, world.remaining(p))
        m.stuck_spent += cost
    world.charge(p, cost, now_h)
    m.spent += cost
    m.attempts += 1
    m.wall_min += p.minutes[mc] * (2 if stuck and not interrupt else 1)
    if mc == ModelClass.PREMIUM:
        m.premium_spent += cost
    return True, ok, stuck


def _pick(world: World, mc: ModelClass, prefer: str | None = None) -> SimProvider | None:
    cands = [p for p in world.providers if mc in p.cost and world.remaining(p) >= p.cost[mc]]
    if prefer:
        for p in cands:
            if p.name == prefer:
                return p
    return max(cands, key=lambda p: world.remaining(p) / p.budget, default=None)


def run_static(policy: str, tasks: list[SimTask], seed: int) -> Metrics:
    rng = random.Random(seed)
    world = World(default_world())
    m = Metrics(policy)
    for st in tasks:
        if policy == "A":
            plan = [(ModelClass.PREMIUM, None)] * 2
        elif policy == "B":
            plan = [(ModelClass.CHEAP, "local"), (ModelClass.CHEAP, None)]
        else:  # C: static routing by task type
            t = st.task.task_type
            if t in (TaskType.TRIVIAL_EDIT, TaskType.TESTS):
                plan = [(ModelClass.CHEAP, "codex")] * 2
            elif t in (TaskType.DEBUGGING, TaskType.ARCHITECTURE):
                plan = [(ModelClass.PREMIUM, "claude")] * 2
            else:
                plan = [(ModelClass.STRONG, "codex")] * 2
        ran_any = False
        for mc, prefer in plan:
            p = _pick(world, mc, prefer)
            if p is None:
                continue
            ran, ok, _stuck = _attempt(world, p, mc, st, rng, st.arrival_h, m, interrupt=False)
            ran_any = ran_any or ran
            if ok:
                m.resolved += 1
                m.value += st.task.estimated_value
                break
        if not ran_any:
            m.blocked += 1
    return _finish(world, m)


def run_ration(tasks: list[SimTask], seed: int, interrupt: bool = True) -> Metrics:
    rng = random.Random(seed)
    world = World(default_world())
    m = Metrics("D" if interrupt else "D-")
    stats: dict[tuple[str, ModelClass, TaskType], list[int]] = defaultdict(lambda: [0, 0])
    opts = [ProviderOption(p.name, {mc: f"{p.name}-{mc}" for mc in p.cost}) for p in world.providers]

    def success(name: str, mc: ModelClass, tt: TaskType) -> tuple[int, int]:
        s, n = stats[(name, mc, tt)]
        return s, n

    for i, st in enumerate(tasks):
        now_h = st.arrival_h
        backlog = i < len(tasks) - 1
        budgets: dict[str, ProviderBudget] = {}
        for p in world.providers:
            snap = world.snapshot(p, now_h)
            wb = window_budget(snap, world.history[p.name][-20:], now=now_h * 3600, reserve_percent=20, has_backlog=backlog)
            budgets[p.name] = ProviderBudget(p.name, [wb])
        for o in opts:
            prov = next(p for p in world.providers if p.name == o.name)
            o.available = world.remaining(prov) > min(prov.cost.values())
        sched = Scheduler(opts, budgets, SchedulerConfig(), success)
        attempts: list[Attempt] = []
        ran_any = False
        while True:
            pol = sched.decide(st.task, attempts, now=now_h * 3600)
            if pol.stop:
                break
            p = next(p for p in world.providers if p.name == pol.provider)
            ran, ok, stuck = _attempt(world, p, pol.model_class, st, rng, now_h, m, interrupt)
            if not ran:
                next(o for o in opts if o.name == p.name).available = False
                if not any(o.available for o in opts):
                    break
                continue
            ran_any = True
            key = (p.name, pol.model_class, st.task.task_type)
            stats[key][1] += 1
            if ok:
                stats[key][0] += 1
                m.resolved += 1
                m.value += st.task.estimated_value
                break
            attempts.append(Attempt(p.name, pol.model_class, "stuck" if stuck else "failed"))
        if not ran_any:
            m.blocked += 1
    return _finish(world, m)


def _finish(world: World, m: Metrics) -> Metrics:
    total = sum(p.budget for p in world.providers)
    m.unused_at_reset = 100 * sum(max(0.0, world.remaining(p)) for p in world.providers) / total
    m.exhausted_early_h = max(0.0, WEEK_H - world.exhausted_at) if world.exhausted_at is not None else 0.0
    return m


def run(n_tasks: int = 100, seeds: int = 5) -> list[Metrics]:
    """Average each policy over `seeds` worlds (same tasks + same RNG per seed)."""
    agg: dict[str, list[Metrics]] = defaultdict(list)
    for seed in range(seeds):
        tasks = make_tasks(n_tasks, random.Random(1000 + seed))
        for pol in ("A", "B", "C"):
            agg[pol].append(run_static(pol, tasks, seed))
        agg["D"].append(run_ration(tasks, seed, interrupt=True))
        agg["D-"].append(run_ration(tasks, seed, interrupt=False))
    out = []
    for pol, ms in agg.items():
        k = len(ms)
        avg = Metrics(pol)
        for f in ("resolved", "value", "spent", "stuck_spent", "wall_min", "attempts", "premium_spent", "unused_at_reset", "exhausted_early_h", "blocked"):
            setattr(avg, f, sum(getattr(x, f) for x in ms) / k)
        out.append(avg)
    return out


POLICY_NAMES = {
    "A": "premium always",
    "B": "cheapest always",
    "C": "static routing",
    "D": "RATION adaptive",
    "D-": "RATION, no stuck interrupt",
}
