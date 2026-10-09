"""Transparent task heuristics. No ML: every number comes with a reason."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .models import AgentTask, TaskType

# (type, base difficulty, patterns). Order = precedence.
TYPE_RULES: list[tuple[TaskType, float, list[str]]] = [
    (TaskType.ARCHITECTURE, 0.75, [r"\barchitect", r"\bredesign", r"\brewrite\b", r"\bdesign (a|the)\b"]),
    (TaskType.MIGRATION, 0.65, [r"\bmigrat", r"\bupgrade\b", r"\bport (to|from)\b"]),
    (TaskType.PERFORMANCE, 0.65, [r"\bslow\b", r"\bperformance\b", r"\boptimi[sz]", r"\blatency\b", r"\bmemory leak"]),
    (TaskType.TRIVIAL_EDIT, 0.1, [r"\btypo", r"\breadme\b", r"\bdocstring", r"\blint\b", r"\bformatting\b", r"\bcomments?\b"]),
    (TaskType.DEBUGGING, 0.55, [r"\bfix\b", r"\bbug\b", r"\bcrash", r"\bfail", r"\bbroken\b", r"\bexception\b", r"\berror\b", r"\bdeadlock", r"\brace\b"]),
    (TaskType.INVESTIGATION, 0.55, [r"\bwhy\b", r"\binvestigat", r"\bfigure out\b", r"\bunderstand\b", r"\bfind out\b"]),
    (TaskType.REFACTOR, 0.5, [r"\brefactor", r"\bclean ?up\b", r"\bextract\b", r"\brestructur"]),
    (TaskType.TESTS, 0.35, [r"\badd (unit |integration )?tests?\b", r"\btest coverage\b", r"\bwrite tests?\b"]),
    (TaskType.CODE_REVIEW, 0.35, [r"\breview\b", r"\baudit\b"]),
    (TaskType.TRIVIAL_EDIT, 0.1, [r"\brename\b", r"\bbump\b"]),
]

HARDNESS = [
    (r"\brace( condition)?\b", "race condition"),
    (r"\bdeadlock", "deadlock"),
    (r"\b(intermittent|flaky|occasionally|sometimes|randomly)\b", "intermittent failure"),
    (r"\bconcurren", "concurrency"),
    (r"\bmemory leak", "memory leak"),
    (r"\bdistributed\b", "distributed system"),
]

RISKY = [
    (r"\bauth", "auth"),
    (r"\b(payment|billing)", "payments"),
    (r"\bsecurity\b|\bvulnerab", "security"),
    (r"\b(database|schema|migration)\b", "data/schema"),
    (r"\bproduction\b|\bprod\b", "production"),
    (r"\bdelete\b|\bdrop\b", "destructive"),
]

PATH_RE = re.compile(r"[\w./\\-]+\.(py|ts|tsx|js|go|rs|java|rb|cpp|c|h|cs|kt|swift|sql|toml|yaml|yml|json)\b")
TRACE_RE = re.compile(r"Traceback \(most recent call last\)|^\s+at \S+\(.*\)$|\w+(Error|Exception):", re.M)


@dataclass
class Assessment:
    task_type: TaskType
    difficulty: float
    risk: float
    uncertainty: float
    reasons: list[str] = field(default_factory=list)


def repo_file_count(repo: str | None) -> int | None:
    if not repo or not (Path(repo) / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", repo, "ls-files"], capture_output=True, text=True, timeout=5, check=False
        )
        return len(out.stdout.splitlines()) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def assess(description: str, *, repo_files: int | None = None, previous_attempts: int = 0) -> Assessment:
    text = description.lower()
    reasons: list[str] = []
    ttype, base = TaskType.CODE_GENERATION, 0.45
    for t, b, pats in TYPE_RULES:
        if any(re.search(p, text) for p in pats):
            ttype, base = t, b
            break
    reasons.append(f"type={ttype} (base {base:.2f})")
    d = base

    for pat, label in HARDNESS:
        if re.search(pat, text):
            d += 0.12
            reasons.append(f"+ {label}")
    paths = set(m.group(0) for m in PATH_RE.finditer(description))
    if len(paths) > 3:
        d += 0.1
        reasons.append(f"+ touches {len(paths)} named files")
    if previous_attempts:
        d += min(0.3, 0.1 * previous_attempts)
        reasons.append(f"+ {previous_attempts} previous attempt(s)")
    if repo_files is not None:
        if repo_files > 10000:
            d += 0.15
            reasons.append(f"+ large repo ({repo_files} files)")
        elif repo_files > 2000:
            d += 0.08
            reasons.append(f"+ mid-size repo ({repo_files} files)")

    uncertainty = 0.5
    if TRACE_RE.search(description):
        uncertainty -= 0.2
        reasons.append("- stack trace provided (easier to localize)")
    words = len(description.split())
    if words < 6:
        uncertainty += 0.2
        reasons.append("+ short/ambiguous description")

    risk = 0.2
    for pat, label in RISKY:
        if re.search(pat, text):
            risk += 0.2
            reasons.append(f"risk: {label}")
    if ttype in (TaskType.ARCHITECTURE, TaskType.MIGRATION):
        risk += 0.15

    return Assessment(
        task_type=ttype,
        difficulty=round(min(1.0, d), 3),
        risk=round(min(1.0, risk), 3),
        uncertainty=round(max(0.0, min(1.0, uncertainty)), 3),
        reasons=reasons,
    )


PRIORITY = {"low": 0.25, "normal": 0.5, "high": 0.8, "critical": 0.95}


def make_task(
    description: str,
    *,
    repo: str | None = None,
    priority: str = "normal",
    value: float | None = None,
    checks: list[str] | None = None,
    previous_attempts: int = 0,
) -> tuple[AgentTask, Assessment]:
    a = assess(description, repo_files=repo_file_count(repo), previous_attempts=previous_attempts)
    pr = PRIORITY.get(priority, 0.5)
    task = AgentTask(
        description=description,
        repo=repo,
        priority=pr,
        task_type=a.task_type,
        estimated_value=value if value is not None else pr,
        estimated_difficulty=a.difficulty,
        uncertainty=a.uncertainty,
        risk=a.risk,
        reversibility=0.5 if a.risk >= 0.6 else 0.8,
        acceptance_checks=list(checks or []),
    )
    return task, a
