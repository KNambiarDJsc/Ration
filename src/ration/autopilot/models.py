"""Data model for autopilot: opportunities, scored tasks, results."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

# Categories are plain strings so opportunity packs can add their own.
FAILING_TESTS = "failing_tests"
MISSING_TESTS = "missing_tests"
SKIPPED_TESTS = "skipped_tests"
LINT = "lint"
TYPES = "types"
DOCS = "docs"
TODO = "todo"
ERROR_HANDLING = "error_handling"
REFACTOR = "refactor"
UNFINISHED = "unfinished"
FEATURE = "feature"
USER = "user"
RESEARCH = "research"

# Estimated cost of one task, in percent of the short (5-hour) usage window.
# These are assumptions until this repository has history of its own (see state.History).
COMPLEXITY_COST = {"xs": 1.0, "s": 2.0, "m": 5.0, "l": 10.0, "xl": 25.0}
COMPLEXITY_ORDER = list(COMPLEXITY_COST)
COMPLEXITY_DIFFICULTY = {"xs": 0.15, "s": 0.3, "m": 0.5, "l": 0.65, "xl": 0.8}


class Target(StrEnum):
    """What must measurably improve for a task to count."""

    PASS = "pass"  # the task's checks go from failing to passing (fewer failures = improved)
    MORE_TESTS = "more_tests"  # more passing tests, no new failures
    NONE = "none"  # nothing measurable beyond "the guard checks did not regress"


class Decision(StrEnum):
    RUN = "RUN"
    DEFER = "DEFER"  # worthwhile, but not within the budget that is safe to spend now
    SKIP = "SKIP"  # not now: low expected value, cooling down, conflicts with local work
    REJECT = "REJECT"  # not allowed for unattended execution in this mode


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    IMPROVED = "IMPROVED"
    NO_CHANGE = "NO_CHANGE"
    FAILED = "FAILED"
    REGRESSION = "REGRESSION"
    BLOCKED = "BLOCKED"
    UNVERIFIABLE = "UNVERIFIABLE"


def level(x: float) -> str:
    """Words for a 0..1 number, so reports read like a person wrote them."""
    return "very low" if x < 0.15 else "low" if x < 0.35 else "medium" if x < 0.6 else "high" if x < 0.8 else "very high"


def slug(text: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "task"


def fingerprint(*parts: Any) -> str:
    """Stable id for 'the same opportunity' across runs (line numbers excluded on purpose)."""
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:10]


@dataclass
class Opportunity:
    id: str
    title: str
    description: str  # the instruction given to the agent; names files and lines, never quotes code
    category: str
    source: str
    files: list[str] = field(default_factory=list)
    value: float = 0.5  # usefulness if it succeeds, 0..1
    risk: float = 0.3  # chance and cost of harm, 0..1
    confidence: float = 0.5  # that this is real work an agent can finish, 0..1
    verifiability: float = 0.5  # how well a result can be checked, 0..1
    complexity: str = "s"  # xs | s | m | l | xl
    target: Target = Target.NONE
    checks: list[str] = field(default_factory=list)  # for Target.PASS: commands that must end up passing
    evidence: list[str] = field(default_factory=list)  # why this was discovered
    writes_code: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoredTask:
    opp: Opportunity
    score: float = 0.0  # 0..100
    alignment: float = 0.7
    cost: float = 0.0  # estimated % of the short window
    cost_source: str = "assumed"  # "assumed" | "measured in this repo (n)"
    factors: dict[str, float] = field(default_factory=dict)
    decision: Decision = Decision.SKIP
    reasons: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.opp.id

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.opp.to_dict(),
            "score": round(self.score, 1),
            "alignment": round(self.alignment, 2),
            "cost": self.cost,
            "cost_source": self.cost_source,
            "factors": {k: round(v, 3) for k, v in self.factors.items()},
            "decision": str(self.decision),
            "reasons": self.reasons,
        }


@dataclass
class CheckResult:
    cmd: str
    ok: bool
    ran: bool = True  # False: the command could not be started or timed out
    passed: int | None = None
    failed: int | None = None
    issues: int | None = None  # lint / type-check findings
    tail: str = ""  # last lines of output, redacted
    seconds: float = 0.0

    def summary(self) -> str:
        if not self.ran:
            return "could not run"
        if self.passed is not None or self.failed is not None:
            return f"{self.passed or 0} passed, {self.failed or 0} failed"
        if self.issues is not None and not self.ok:
            return f"{self.issues} issue{'s' if self.issues != 1 else ''}"
        return "passing" if self.ok else "failing"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskResult:
    task_id: str
    title: str
    category: str
    outcome: Outcome
    accepted: bool
    detail: str = ""
    branch: str | None = None
    commit: str | None = None
    worktree: str | None = None  # only when a change had to be left uncommitted
    artifact: str | None = None  # research output or rejected patch
    files_changed: list[str] = field(default_factory=list)
    added: int = 0
    deleted: int = 0
    before: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    stuck_interrupts: int = 0
    start: str | None = None  # the commit the task started from: base + everything accepted before it
    estimated_cost: float | None = None  # what the plan assumed, in % of the short window
    cost_percent: float | None = None  # measured change of the short window, when attributable
    cost_usd: float | None = None  # the provider's own list-price estimate
    seconds: float = 0.0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["outcome"] = str(self.outcome)
        return d
