"""Scheduler properties from the spec (section 31)."""

from ration.budget import ProviderBudget, window_budget
from ration.difficulty import assess
from ration.models import (
    AgentTask,
    BudgetMode,
    Confidence,
    Effort,
    ModelClass,
    ResourceType,
    Source,
    TaskType,
    UsageSnapshot,
)
from ration.scheduler import Attempt, ProviderOption, Scheduler, SchedulerConfig

NOW = 10_000_000.0
H = 3600.0
ALL = {m: str(m) for m in ModelClass}


def budget(provider, used, frac_left, *, burn_per_h=None, has_backlog=True, reserve=20):
    window_h = 168
    reset_in = frac_left * window_h
    cur = UsageSnapshot(provider, "seven_day", ResourceType.USAGE_PERCENT, used, NOW + reset_in * H, window_h * 60, Source.PROVIDER_CLI, Confidence.HIGH, NOW)
    hist = []
    if burn_per_h is not None:
        hist = [UsageSnapshot(provider, "seven_day", ResourceType.USAGE_PERCENT, used - burn_per_h, cur.reset_at, window_h * 60, Source.PROVIDER_CLI, Confidence.HIGH, NOW - H)]
    return ProviderBudget(provider, [window_budget(cur, hist, now=NOW, reserve_percent=reserve, has_backlog=has_backlog)])


def task(value=0.8, difficulty=0.5, ttype=TaskType.DEBUGGING, risk=0.3, priority=None):
    return AgentTask("t", task_type=ttype, estimated_value=value, priority=priority if priority is not None else value, estimated_difficulty=difficulty, risk=risk)


def sched(budgets, providers=("claude",)):
    return Scheduler([ProviderOption(p, ALL) for p in providers], budgets)


def test_routine_task_starts_cheap_and_explains():
    p = sched({}).decide(task(value=0.5, difficulty=0.3, ttype=TaskType.TESTS), now=NOW)
    assert p.model_class == ModelClass.CHEAP
    assert "routine_task_cheap_first" in p.reasons and "claude_budget_unknown" in p.reasons


def test_hard_debugging_starts_strong():
    p = sched({}).decide(task(difficulty=0.8), now=NOW)
    assert p.model_class == ModelClass.STRONG and "hard_debugging" in p.reasons


def test_does_not_exceed_reserve_without_justification():
    b = {"claude": budget("claude", used=85, frac_left=0.5, reserve=20)}  # 15% left < reserve
    attempts = [Attempt("claude", ModelClass.CHEAP, "failed"), Attempt("claude", ModelClass.STRONG, "stuck")]
    p = sched(b).decide(task(value=0.9, difficulty=0.8), attempts, now=NOW)
    assert p.model_class != ModelClass.PREMIUM
    assert any(r.startswith("premium_reserved:budget_at_reserve") for r in p.reasons)


def test_premium_allowed_when_justified():
    b = {"claude": budget("claude", used=30, frac_left=0.5, burn_per_h=0.4)}
    attempts = [Attempt("claude", ModelClass.CHEAP, "failed"), Attempt("claude", ModelClass.STRONG, "stuck")]
    p = sched(b).decide(task(value=0.9, difficulty=0.8), attempts, now=NOW)
    assert p.model_class == ModelClass.PREMIUM
    assert "premium_budget_above_reserve" in p.reasons and "cheaper_attempt_failed" in p.reasons


def test_increases_spending_near_reset_when_backlog():
    idle = sched({"claude": budget("claude", used=30, frac_left=0.15, burn_per_h=0.5, has_backlog=False)})
    busy = sched({"claude": budget("claude", used=30, frac_left=0.15, burn_per_h=0.5, has_backlog=True)})
    t = task(value=0.6, difficulty=0.5, ttype=TaskType.REFACTOR)
    pi, pb = idle.decide(t, now=NOW), busy.decide(t, now=NOW)
    assert busy.budgets["claude"].mode == BudgetMode.AGGRESSIVE
    assert idle.budgets["claude"].mode == BudgetMode.NORMAL
    assert Effort(pb.effort).value >= Effort(pi.effort).value or pb.effort == Effort.HIGH
    assert "claude_unused_quota_before_reset" in pb.reasons
    assert pb.retry_budget >= pi.retry_budget


def test_reduces_spend_when_exhaustion_projected_early():
    tight = {"claude": budget("claude", used=80, frac_left=0.7, burn_per_h=3.0, reserve=10)}
    loose = {"claude": budget("claude", used=10, frac_left=0.7, burn_per_h=0.2, reserve=10)}
    t = task(value=0.6, difficulty=0.7, ttype=TaskType.REFACTOR)
    pt, pl = sched(tight).decide(t, now=NOW), sched(loose).decide(t, now=NOW)
    assert tight["claude"].mode == BudgetMode.CONSERVATIVE
    assert pt.max_turns < pl.max_turns
    assert pt.subagent_allowance < pl.subagent_allowance
    assert "claude_budget_conservative" in pt.reasons


def test_routes_away_from_pressured_provider():
    b = {
        "claude": budget("claude", used=80, frac_left=0.7, burn_per_h=3.0, reserve=10),
        "codex": budget("codex", used=20, frac_left=0.5, burn_per_h=0.3, reserve=10),
    }
    p = sched(b, ("claude", "codex")).decide(task(value=0.5, difficulty=0.4, ttype=TaskType.CODE_GENERATION), now=NOW)
    assert p.provider == "codex"


def test_escalates_stuck_high_value_task():
    s = sched({})
    t = task(value=0.9, difficulty=0.5, ttype=TaskType.CODE_GENERATION)
    p1 = s.decide(t, [Attempt("claude", ModelClass.CHEAP, "stuck", 0.85)], now=NOW)
    assert p1.model_class == ModelClass.STRONG and "cheap_model_stuck_once" in p1.reasons
    p2 = s.decide(t, [Attempt("claude", ModelClass.CHEAP, "stuck"), Attempt("claude", ModelClass.STRONG, "stuck")], now=NOW)
    assert p2.model_class == ModelClass.PREMIUM


def test_stops_wasting_budget_on_low_value_loops():
    p = sched({}).decide(task(value=0.2, difficulty=0.4, ttype=TaskType.REFACTOR), [Attempt("claude", ModelClass.CHEAP, "stuck", 0.9)], now=NOW)
    assert p.stop and "low_value_task_looping" in p.reasons


def test_retry_budget_exhaustion_stops():
    t = task(value=0.5, difficulty=0.3, ttype=TaskType.TESTS)
    attempts = [Attempt("claude", ModelClass.CHEAP, "failed")] * 3
    p = sched({}).decide(t, attempts, now=NOW)
    assert p.stop and "retry_budget_exhausted" in p.reasons


def test_keeps_model_when_last_attempt_progressed():
    p = sched({}).decide(task(value=0.6), [Attempt("claude", ModelClass.CHEAP, "failed", 0.1, 0.3)], now=NOW)
    assert p.model_class == ModelClass.CHEAP and "progress_on_last_attempt_keep_model" in p.reasons


# ---- adaptive policy: this machine's own recorded outcomes, not a trained model --------


def adaptive_sched(tier_success, providers=("claude",)):
    return Scheduler(
        [ProviderOption(p, ALL) for p in providers],
        {},
        SchedulerConfig(policy="adaptive"),
        tier_success=tier_success,
    )


def test_adaptive_policy_is_off_by_default():
    # same scenario as test_hard_debugging_starts_strong, but with a tier_success callback
    # wired up: without policy="adaptive" it must still be ignored.
    s = Scheduler([ProviderOption("claude", ALL)], {}, tier_success=lambda mc, tt: (10, 10))
    p = s.decide(task(difficulty=0.8), now=NOW)
    assert p.model_class == ModelClass.STRONG and "hard_debugging" in p.reasons


def test_adaptive_policy_downgrades_hard_task_cheap_keeps_succeeding():
    s = adaptive_sched(lambda mc, tt: (8, 10))  # 80% success, well above ADAPTIVE_HIGH_SR
    p = s.decide(task(difficulty=0.8, ttype=TaskType.DEBUGGING, risk=0.3), now=NOW)
    assert p.model_class == ModelClass.CHEAP
    assert "adaptive_cheap_succeeds_here(8/10)" in p.reasons
    assert not any(r.startswith("hard_") for r in p.reasons)


def test_adaptive_policy_escalates_routine_task_cheap_keeps_failing():
    s = adaptive_sched(lambda mc, tt: (1, 10))  # 10% success, well below ADAPTIVE_LOW_SR
    p = s.decide(task(difficulty=0.3, ttype=TaskType.TESTS, risk=0.2), now=NOW)
    assert p.model_class == ModelClass.STRONG
    assert "adaptive_cheap_struggles_here(1/10)" in p.reasons
    assert "routine_task_cheap_first" not in p.reasons


def test_adaptive_policy_does_nothing_without_enough_samples():
    s = adaptive_sched(lambda mc, tt: (1, 2))  # only 2 samples, below ADAPTIVE_MIN_SAMPLES
    p = s.decide(task(difficulty=0.8, ttype=TaskType.DEBUGGING, risk=0.3), now=NOW)
    assert p.model_class == ModelClass.STRONG and "hard_debugging" in p.reasons


def test_adaptive_policy_never_overrides_high_risk():
    # cheap "succeeds" 100% of the time here, but risk alone forces STRONG_FIRST.
    s = adaptive_sched(lambda mc, tt: (10, 10))
    p = s.decide(task(difficulty=0.3, ttype=TaskType.TESTS, risk=0.7), now=NOW)
    assert p.model_class == ModelClass.STRONG and "high_risk_task" in p.reasons


def test_difficulty_heuristics_are_explained():
    a = assess("find and fix why auth refresh occasionally deadlocks")
    assert a.task_type == TaskType.DEBUGGING and a.difficulty >= 0.7
    assert "+ deadlock" in a.reasons and "risk: auth" in a.reasons
    t = assess("fix typo in README")
    assert t.task_type == TaskType.TRIVIAL_EDIT and t.difficulty < 0.2
    assert assess("rename variable foo to bar in utils").task_type == TaskType.TRIVIAL_EDIT
    assert assess("redesign the plugin architecture").task_type == TaskType.ARCHITECTURE
