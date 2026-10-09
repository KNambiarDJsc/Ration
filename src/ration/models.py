"""Provider-neutral data model.

Everything here is stdlib-only so the hook path can import it without pulling
in third-party packages.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ResourceType(StrEnum):
    TOKENS = "tokens"
    DOLLARS = "dollars"
    USAGE_PERCENT = "usage_percent"  # provider-reported % of a rolling window
    PREMIUM_ALLOWANCE = "premium_allowance"
    REQUESTS = "requests"
    GPU_SECONDS = "gpu_seconds"
    WALL_CLOCK = "wall_clock"


class Source(StrEnum):
    """Where a number came from. Never present `estimated` as exact."""

    OFFICIAL_API = "official_api"
    PROVIDER_CLI = "provider_cli"  # e.g. Claude Code statusline JSON
    LOCAL_LOG = "local_log"  # e.g. Codex rollout files, Claude transcripts
    GATEWAY = "gateway"
    MANUAL = "manual"
    ESTIMATED = "estimated"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class UsageSnapshot:
    provider: str
    window: str  # e.g. "five_hour", "seven_day", "primary", "secondary"
    resource_type: ResourceType
    used_percent: float  # 0..100 (can exceed 100 for spend limits)
    reset_at: float | None  # unix seconds
    window_minutes: int | None
    source: Source
    confidence: Confidence
    captured_at: float = field(default_factory=time.time)

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    @property
    def window_start(self) -> float | None:
        if self.reset_at is None or self.window_minutes is None:
            return None
        return self.reset_at - self.window_minutes * 60


class EventType(StrEnum):
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_ENDED = "SESSION_ENDED"
    PROMPT_SUBMITTED = "PROMPT_SUBMITTED"
    MODEL_SELECTED = "MODEL_SELECTED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"
    FILE_READ = "FILE_READ"
    FILE_CHANGED = "FILE_CHANGED"
    TEST_RUN = "TEST_RUN"
    TEST_RESULT = "TEST_RESULT"
    SUBAGENT_STARTED = "SUBAGENT_STARTED"
    SUBAGENT_ENDED = "SUBAGENT_ENDED"
    CONTEXT_COMPACTED = "CONTEXT_COMPACTED"
    PERMISSION_REQUESTED = "PERMISSION_REQUESTED"
    ERROR = "ERROR"
    TURN_ENDED = "TURN_ENDED"
    TASK_COMPLETED = "TASK_COMPLETED"
    # RATION-specific
    BUDGET_WARNING = "BUDGET_WARNING"
    STUCK_DETECTED = "STUCK_DETECTED"
    MODEL_ESCALATED = "MODEL_ESCALATED"
    RUN_STOPPED = "RUN_STOPPED"
    POLICY_CHANGED = "POLICY_CHANGED"
    GUARD_BLOCKED = "GUARD_BLOCKED"


@dataclass
class AgentEvent:
    provider: str
    session_id: str
    event_type: EventType
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    task_id: str | None = None
    timestamp: float = field(default_factory=time.time)


class TaskType(StrEnum):
    TRIVIAL_EDIT = "trivial_edit"
    CODE_GENERATION = "code_generation"
    TESTS = "tests"
    DEBUGGING = "debugging"
    ARCHITECTURE = "architecture"
    REFACTOR = "refactor"
    INVESTIGATION = "investigation"
    MIGRATION = "migration"
    CODE_REVIEW = "code_review"
    PERFORMANCE = "performance"


@dataclass
class AgentTask:
    description: str
    repo: str | None = None
    priority: float = 0.5  # 0..1
    deadline: float | None = None  # unix seconds
    task_type: TaskType = TaskType.CODE_GENERATION
    estimated_value: float = 0.5  # 0..1
    estimated_difficulty: float = 0.5  # 0..1
    uncertainty: float = 0.5  # 0..1
    risk: float = 0.3  # 0..1
    reversibility: float = 0.8  # 0..1, 1 = trivially revertible
    acceptance_checks: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("task"))
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelClass(StrEnum):
    CHEAP = "cheap"
    STRONG = "strong"
    PREMIUM = "premium"

    @property
    def rank(self) -> int:
        return _MODEL_ORDER.index(self)

    def up(self) -> ModelClass:
        return _MODEL_ORDER[min(self.rank + 1, len(_MODEL_ORDER) - 1)]


_MODEL_ORDER = [ModelClass.CHEAP, ModelClass.STRONG, ModelClass.PREMIUM]


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    def down(self) -> Effort:
        return {"low": Effort.LOW, "medium": Effort.LOW, "high": Effort.MEDIUM}[self.value]

    def up(self) -> Effort:
        return {"low": Effort.MEDIUM, "medium": Effort.HIGH, "high": Effort.HIGH}[self.value]


class EscalationPolicy(StrEnum):
    CHEAP_FIRST = "CHEAP_FIRST"
    STRONG_FIRST = "STRONG_FIRST"
    PREMIUM_RESERVED = "PREMIUM_RESERVED"


class BudgetMode(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    NORMAL = "NORMAL"
    AGGRESSIVE = "AGGRESSIVE"


class Intervention(StrEnum):
    CONTINUE = "CONTINUE"
    REPLAN = "REPLAN"
    COMPACT_CONTEXT = "COMPACT_CONTEXT"
    RESTART_SESSION = "RESTART_SESSION"
    ESCALATE_MODEL = "ESCALATE_MODEL"
    REDUCE_PARALLELISM = "REDUCE_PARALLELISM"
    ASK_USER = "ASK_USER"
    STOP = "STOP"


@dataclass
class ExecutionPolicy:
    provider: str
    model_class: ModelClass
    effort: Effort
    max_turns: int
    retry_budget: int
    subagent_allowance: int
    escalation: EscalationPolicy
    stop: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
