"""Task scoring: one deterministic number, and the reasons behind it.

    score = 100 x usefulness^.30 x verifiability^.20 x confidence^.15
                x goal alignment^.15 x safety^.10 x cheapness^.10

A weighted geometric mean: every factor is in 0..1, a weak factor drags the
score down in proportion to its weight, and no strong factor can buy back a
factor near zero. The same inputs always give the same score.

    usefulness     value of the work if it succeeds
    verifiability  how well the result can be checked
    confidence     that it is real work an agent can finish; calibrated by how
                   tasks of this kind actually went in this repository
    goal alignment how much it serves the project goal (neutral when none is set)
    safety         1 - risk
    cheapness      1 / (1 + estimated cost / 8% of the short window)

Selection (`decide`) is separate from scoring: the safety policy, the user's
constraints, local uncommitted work and past outcomes can each rule a task out
regardless of its score, and each says why.
"""

from __future__ import annotations

import math
from typing import Any

from . import policy
from .goal import Goal
from .models import COMPLEXITY_COST, Decision, Opportunity, ScoredTask, level

WEIGHTS = {"usefulness": 0.30, "verifiability": 0.20, "confidence": 0.15, "alignment": 0.15, "safety": 0.10, "cheapness": 0.10}
COST_REFERENCE = 8.0  # a task costing this much of the short window gets cheapness 0.5
FLOOR = 0.02


def geometric(factors: dict[str, float]) -> float:
    return 100 * math.exp(sum(WEIGHTS[k] * math.log(max(FLOOR, min(1.0, v))) for k, v in factors.items()))


def score(opp: Opportunity, goal: Goal | None = None, history: Any = None) -> ScoredTask:
    text = " ".join([opp.title, opp.category.replace("_", " "), " ".join(opp.files).replace("/", " ").replace("_", " ").replace(".", " ")])
    alignment, why_aligned = (goal or Goal()).alignment(text)
    cost, cost_source = COMPLEXITY_COST[opp.complexity], "assumed"
    confidence, calibrated = opp.confidence, ""
    if history is not None:
        if measured := history.cost(opp.category):
            cost, cost_source = measured[0], f"measured in this repo ({measured[1]} runs)"
        if rate := history.success_rate(opp.category):
            ok, n = rate
            # Laplace-smoothed success rate; 50% leaves confidence unchanged.
            confidence = max(0.1, min(1.0, confidence * (0.6 + 0.8 * (ok + 1) / (n + 2))))
            calibrated = f"{ok}/{n} earlier '{opp.category}' tasks here were accepted"
    factors = {
        "usefulness": opp.value,
        "verifiability": opp.verifiability,
        "confidence": confidence,
        "alignment": alignment,
        "safety": 1.0 - opp.risk,
        "cheapness": 1.0 / (1.0 + cost / COST_REFERENCE),
    }
    st = ScoredTask(opp, score=round(geometric(factors), 1), alignment=alignment, cost=cost, cost_source=cost_source, factors=factors)
    st.reasons = [
        f"usefulness {level(opp.value)}",
        f"goal alignment {'neutral' if goal is None or goal.empty else level(alignment)} ({why_aligned})",
        f"risk {level(opp.risk)}",
        f"verification {level(opp.verifiability)}",
        f"confidence {level(confidence)}" + (f" ({calibrated})" if calibrated else ""),
        f"estimated cost ~{cost:g}% of the 5-hour window ({cost_source})",
    ]
    return st


def decide(st: ScoredTask, mode: policy.Mode, *, goal: Goal | None, dirty: set[str], history: Any, has_guards: bool, min_score: float) -> ScoredTask:
    """Everything except the budget. Sets `decision` and puts the deciding reason first."""
    opp = st.opp

    def rule(decision: Decision, why: str) -> ScoredTask:
        st.decision = decision
        st.reasons.insert(0, why)
        return st

    if why := policy.gate(opp, mode, has_guards=has_guards):
        return rule(Decision.REJECT, why)
    if goal and (broken := goal.conflict(f"{opp.title} {' '.join(opp.files).replace('/', ' ').replace('_', ' ').replace('.', ' ')}")):
        return rule(Decision.SKIP, f"may conflict with your constraint \"{broken}\"")
    overlap = sorted(set(opp.files) & dirty)
    if overlap:
        return rule(Decision.SKIP, f"{overlap[0]} has uncommitted changes; autopilot works from the last commit and will not build on or overwrite them")
    if history is not None and (cool := history.cooldown(opp.id)):
        return rule(Decision.SKIP, cool)
    if st.score < min_score:
        return rule(Decision.SKIP, f"expected value too low (score {st.score:.1f}, minimum {min_score:g})")
    st.decision = Decision.RUN
    return st


def select(tasks: list[ScoredTask], *, spendable: float | None, max_tasks: int, cost_factor: float = 1.0) -> list[ScoredTask]:
    """Greedy by score within the budget. `cost_factor` scales the estimates when tasks
    in this run have been costing more than estimated."""
    chosen: list[ScoredTask] = []
    left = spendable
    for st in sorted(tasks, key=lambda t: (-t.score, t.opp.title)):
        if st.decision != Decision.RUN:
            continue
        cost = st.cost * cost_factor
        if len(chosen) >= max_tasks:
            st.decision = Decision.DEFER
            st.reasons.insert(0, f"over the limit of {max_tasks} tasks for one run")
        elif left is not None and cost > left:
            st.decision = Decision.DEFER
            st.reasons.insert(0, f"needs ~{cost:g}% of the 5-hour window (estimate); only ~{max(0.0, left):.0f}% is safe to spend")
        else:
            chosen.append(st)
            if left is not None:
                left -= cost
    return chosen
