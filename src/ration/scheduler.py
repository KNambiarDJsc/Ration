"""Scheduler V1: explainable, rule-based. No RL, no learned routing.

Inputs: task (priority, value, difficulty, risk), provider budgets (remaining,
burn pressure, time to reset), historical provider success, and the previous
attempts on this task (stuck score, progress velocity).

Output: an ExecutionPolicy with decision reasons, e.g.
    ["high_priority_task", "cheap_model_failed_once",
     "premium_budget_above_reserve", "reset_within_18_hours"]
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .budget import ProviderBudget
from .models import (
    AgentTask,
    BudgetMode,
    Effort,
    EscalationPolicy,
    ExecutionPolicy,
    ModelClass,
    TaskType,
)

HARD_TYPES = {TaskType.DEBUGGING, TaskType.ARCHITECTURE, TaskType.MIGRATION, TaskType.PERFORMANCE}


@dataclass
class ProviderOption:
    name: str
    models: dict[ModelClass, str]  # class -> model id ("" = provider default)
    available: bool = True


@dataclass
class Attempt:
    provider: str
    model_class: ModelClass
    outcome: str  # "succeeded" | "failed" | "stuck" | "error"
    stuck_score: float = 0.0
    progress_velocity: float = 0.0


@dataclass
class SchedulerConfig:
    reserve_percent: float = 20
    max_parallel_agents: int = 4
    default_max_turns: int = 30

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> SchedulerConfig:
        return cls(
            reserve_percent=cfg["budget"]["reserve_percent"],
            max_parallel_agents=cfg["scheduler"]["max_parallel_agents"],
            default_max_turns=cfg["scheduler"]["default_max_turns"],
        )


SuccessFn = Callable[[str, ModelClass, TaskType], tuple[int, int]]


@dataclass
class Scheduler:
    providers: list[ProviderOption]
    budgets: dict[str, ProviderBudget] = field(default_factory=dict)
    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    success: SuccessFn | None = None

    def decide(self, task: AgentTask, attempts: list[Attempt] | None = None, *, now: float | None = None) -> ExecutionPolicy:
        now = time.time() if now is None else now
        attempts = attempts or []
        reasons: list[str] = []
        cfg = self.config
        diff, value = task.estimated_difficulty, task.estimated_value

        if task.priority >= 0.8:
            reasons.append("high_priority_task")
        elif task.priority <= 0.3:
            reasons.append("low_priority_task")
        urgent = task.priority >= 0.8 or (task.deadline is not None and task.deadline - now < 24 * 3600)
        if task.deadline is not None and task.deadline - now < 24 * 3600:
            reasons.append("deadline_within_24h")

        # 1. escalation policy
        if task.risk >= 0.6:
            escalation = EscalationPolicy.STRONG_FIRST
            reasons.append("high_risk_task")
        elif diff >= 0.7 and task.task_type in HARD_TYPES:
            escalation = EscalationPolicy.STRONG_FIRST
            reasons.append(f"hard_{task.task_type}")
        else:
            escalation = EscalationPolicy.CHEAP_FIRST
            reasons.append("routine_task_cheap_first")

        # 2. stop conditions (retry cap flexes with budget headroom)
        failed = [a for a in attempts if a.outcome != "succeeded"]
        retry_cap = self._retry_budget(task)
        known = [b for b in self.budgets.values() if b.known]
        if known and any(self._headroom(b) for b in known):
            retry_cap += 1
            reasons.append("budget_headroom_extra_retry")
        elif known and all(b.mode == BudgetMode.CONSERVATIVE for b in known):
            retry_cap = max(1, retry_cap - 1)
            reasons.append("all_budgets_tight_fewer_retries")
        if len(failed) > retry_cap:
            return self._stop(task, escalation, reasons + ["retry_budget_exhausted"])
        if value < 0.3 and failed and failed[-1].outcome == "stuck":
            return self._stop(task, escalation, reasons + ["low_value_task_looping"])

        # 3. model tier
        tier = ModelClass.CHEAP if escalation == EscalationPolicy.CHEAP_FIRST else ModelClass.STRONG
        if task.task_type == TaskType.TRIVIAL_EDIT and diff < 0.2:
            tier = ModelClass.CHEAP
        if attempts:
            last = attempts[-1]
            if last.outcome == "succeeded":
                tier = last.model_class
            elif last.progress_velocity > 0.05 and last.outcome != "stuck":
                tier = last.model_class
                reasons.append("progress_on_last_attempt_keep_model")
            else:
                tier = last.model_class.up()
                n_same = sum(1 for a in failed if a.model_class == last.model_class)
                reasons.append(
                    f"{last.model_class}_model_{'stuck' if last.outcome == 'stuck' else 'failed'}"
                    f"_{'once' if n_same == 1 else f'{n_same}x'}"
                )

        cheaper_failed = any(a.model_class.rank < ModelClass.PREMIUM.rank for a in failed)

        # 4. provider choice (scored, explainable)
        provider, pscore, preasons = self._pick_provider(task, tier, attempts)
        if provider is None and tier != ModelClass.CHEAP:
            tier = ModelClass.STRONG if tier == ModelClass.PREMIUM else ModelClass.CHEAP
            provider, pscore, preasons = self._pick_provider(task, tier, attempts)
        if provider is None:
            return self._stop(task, escalation, reasons + ["no_provider_available"])
        reasons += preasons
        budget = self.budgets.get(provider)
        mode = budget.mode if budget and budget.known else BudgetMode.NORMAL

        # 5. premium gate (PREMIUM_RESERVED)
        if tier == ModelClass.PREMIUM:
            ok, why = self._premium_allowed(task, budget, cheaper_failed, urgent)
            if ok:
                reasons += why
                escalation = EscalationPolicy.PREMIUM_RESERVED
            else:
                tier = ModelClass.STRONG
                reasons += [f"premium_reserved:{w}" for w in why]

        # 6. effort, turns, retries, subagents
        effort = Effort.LOW if diff < 0.35 else Effort.MEDIUM if diff < 0.65 else Effort.HIGH
        max_turns = 10 if task.task_type == TaskType.TRIVIAL_EDIT else int(cfg.default_max_turns * (0.6 + 0.8 * diff))
        subagents = 0 if diff < 0.35 else min(cfg.max_parallel_agents, 1 + int(diff * 4))
        if mode == BudgetMode.CONSERVATIVE:
            effort = effort.down()
            max_turns = max(5, int(max_turns * 0.7))
            subagents //= 2
            reasons.append(f"{provider}_budget_conservative")
        elif mode == BudgetMode.AGGRESSIVE and value >= 0.5:
            effort = effort.up()
            reasons.append(f"{provider}_unused_quota_before_reset")
        if tier == ModelClass.PREMIUM:
            subagents = min(subagents, 2)
        if budget and budget.binding and budget.binding.hours_to_reset is not None:
            h = budget.binding.hours_to_reset
            if h <= 24:
                reasons.append(f"reset_within_{max(1, round(h))}_hours")

        return ExecutionPolicy(
            provider=provider,
            model_class=tier,
            effort=effort,
            max_turns=max_turns,
            retry_budget=retry_cap,
            subagent_allowance=subagents,
            escalation=escalation,
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    def _retry_budget(self, task: AgentTask) -> int:
        n = 1 + (task.estimated_value >= 0.7) + (task.estimated_difficulty >= 0.6)
        return int(n)

    def _stop(self, task: AgentTask, escalation: EscalationPolicy, reasons: list[str]) -> ExecutionPolicy:
        return ExecutionPolicy(
            provider="",
            model_class=ModelClass.CHEAP,
            effort=Effort.LOW,
            max_turns=0,
            retry_budget=0,
            subagent_allowance=0,
            escalation=escalation,
            stop=True,
            reasons=reasons,
        )

    def _headroom(self, b: ProviderBudget) -> bool:
        """Quota that will likely go unused at reset unless queued work uses it."""
        if b.mode == BudgetMode.AGGRESSIVE:
            return True
        p, rem = b.burn_pressure, b.remaining_percent or 0.0
        return b.mode == BudgetMode.NORMAL and p is not None and p < 0.75 and rem > self.config.reserve_percent + 30

    def _premium_allowed(
        self, task: AgentTask, budget: ProviderBudget | None, cheaper_failed: bool, urgent: bool
    ) -> tuple[bool, list[str]]:
        why: list[str] = []
        headroom = budget is not None and budget.known and self._headroom(budget)
        if task.estimated_value >= 0.7:
            why.append("high_value_task")
        elif headroom and task.estimated_value >= 0.45 and cheaper_failed:
            why.append("unused_quota_allows_premium")
        else:
            return False, ["task_value_too_low"]
        if not (cheaper_failed or urgent):
            return False, ["no_cheaper_attempt_failed"]
        why.append("cheaper_attempt_failed" if cheaper_failed else "urgent")
        if budget is None or not budget.known:
            return True, why + ["budget_unknown"]
        rem = budget.remaining_percent or 0.0
        if rem > self.config.reserve_percent:
            return True, why + ["premium_budget_above_reserve"]
        if budget.mode == BudgetMode.AGGRESSIVE:
            return True, why + ["unused_quota_before_reset"]
        return False, [f"budget_at_reserve({rem:.0f}%)"]

    def _pick_provider(
        self, task: AgentTask, tier: ModelClass, attempts: list[Attempt]
    ) -> tuple[str | None, float, list[str]]:
        best: tuple[str | None, float, list[str]] = (None, -1.0, [])
        for p in self.providers:
            if not p.available or tier not in p.models:
                continue
            why: list[str] = []
            s, n = self.success(p.name, tier, task.task_type) if self.success else (0, 0)
            score = (s + 1) / (n + 2)
            if n >= 3:
                why.append(f"{p.name}_{tier}_success_{s}/{n}")
            b = self.budgets.get(p.name)
            if b is None or not b.known:
                score *= 0.8
                why.append(f"{p.name}_budget_unknown")
            else:
                rem = b.remaining_percent or 0.0
                if b.mode == BudgetMode.CONSERVATIVE:
                    score *= 0.4
                    why.append(f"{p.name}_burn_pressure_high")
                elif b.mode == BudgetMode.AGGRESSIVE:
                    score *= 1.3
                    why.append(f"{p.name}_use_it_or_lose_it")
                if rem <= self.config.reserve_percent and tier != ModelClass.CHEAP:
                    score *= 0.2
                    why.append(f"{p.name}_at_reserve")
                score *= 0.75 + 0.25 * (rem / 100)
            if attempts and attempts[-1].outcome == "stuck" and attempts[-1].provider == p.name:
                score *= 0.85
            if score > best[1]:
                best = (p.name, score, [f"route_{p.name}"] + why)
        return best
