"""Project goal: what this repository is for, kept in `.ration/goal.toml`.

Local only. The goal is used to rank work, and is included in the instructions
given to the coding agent you chose; it goes nowhere else.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STATE_DIR = ".ration"
STOPWORDS = set(
    "a an and are as at be but by can do does for from has have how in into is it its of on or our that the their then "
    "this to we with you your will not don dont never avoid without any all make more most new use using should must "
    "change changes modify touch edit automatically anything code files file project app application build ready".split()
)
NEGATION_RE = re.compile(r"^\s*(?:don'?t|do not|never|no|avoid|without)\b", re.I)


def project_root(cwd: str | os.PathLike[str] | None = None) -> Path:
    """The enclosing git repository, or the directory itself."""
    cur = Path(cwd or os.getcwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            return d
    return cur


def state_dir(root: Path, create: bool = False) -> Path:
    """`<project>/.ration/`: project-local state. It ignores itself, so it never
    shows up in `git status` and RATION never edits your .gitignore."""
    d = root / STATE_DIR
    if create and not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitignore").write_text("*\n", encoding="utf-8")
    return d


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()) if w not in STOPWORDS}


@dataclass
class Goal:
    goal: str = ""
    priorities: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.goal.strip() or self.priorities or self.constraints)

    def alignment(self, text: str) -> tuple[float, str]:
        """0..1: how much a piece of work serves the goal. Earlier priorities count more."""
        if self.empty:
            return 0.7, "no goal set"
        have = words(text)
        best, why = 0.0, ""
        for i, p in enumerate(self.priorities):
            pw = words(p)
            if pw and (hit := pw & have):
                s = (1.0 - 0.1 * min(i, 4)) * min(1.0, len(hit) / min(len(pw), 2))
                if s > best:
                    best, why = s, f"matches priority '{p}'"
        gw = words(self.goal)
        if gw and (hit := gw & have):
            s = 0.6 * min(1.0, len(hit) / 2)
            if s > best:
                best, why = s, "matches the goal"
        # Constraints phrased positively ("prioritize tests and reliability") are preferences.
        for c in self.constraints:
            if not NEGATION_RE.match(c) and (hit := words(c) & have):
                s = 0.8 * min(1.0, len(hit) / 2)
                if s > best:
                    best, why = s, f"matches '{c}'"
        return 0.4 + 0.6 * best, why or "not related to the goal"

    def conflict(self, text: str) -> str | None:
        """A 'don't ...' constraint this work may break, if any. Deliberately cautious."""
        have = words(text)
        for c in self.constraints:
            if not NEGATION_RE.match(c):
                continue
            named = words(NEGATION_RE.sub("", c))
            # A short constraint ("don't touch billing") matches on its one subject. A longer
            # one needs two of its words, or a single common word would block unrelated work.
            if len(named & have) >= (1 if len(named) <= 2 else 2):
                return c
        return None

    def brief(self) -> str:
        parts = []
        if self.goal.strip():
            parts.append(f"Project goal: {self.goal.strip()}")
        if self.priorities:
            parts.append("Priorities: " + "; ".join(self.priorities))
        if self.constraints:
            parts.append("Constraints (do not break these): " + "; ".join(self.constraints))
        return "\n".join(parts)


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip() + '"'


def dumps(g: Goal) -> str:
    def arr(items: list[str]) -> str:
        return "[\n" + "".join(f"  {_q(i)},\n" for i in items) + "]" if items else "[]"

    return (
        "# RATION project goal. Local to this machine; edit freely or use `ration goal set`.\n"
        f"goal = {_q(g.goal)}\n\n"
        "# Most important first.\n"
        f"priorities = {arr(g.priorities)}\n\n"
        "# Things autopilot must not do, e.g. \"don't change the database schema\".\n"
        f"constraints = {arr(g.constraints)}\n\n"
        f"updated_at = {int(g.updated_at or time.time())}\n"
    )


def loads(text: str) -> Goal:
    """Raises ValueError when the file is not a usable goal."""
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"not valid TOML: {e}") from e

    def strs(key: str) -> list[str]:
        v = d.get(key) or []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            raise ValueError(f"`{key}` must be a list of strings")
        return [str(x).strip() for x in v if str(x).strip()]

    if not isinstance(d.get("goal", ""), str):
        raise ValueError("`goal` must be a string")
    ts = d.get("updated_at")
    return Goal(d.get("goal", "").strip(), strs("priorities"), strs("constraints"), float(ts) if isinstance(ts, int | float) else 0.0)


def path(root: Path) -> Path:
    return root / STATE_DIR / "goal.toml"


def load(root: Path) -> Goal | None:
    """The stored goal, or None when there is none. A broken file reads as none
    (`ration goal show` says why); autopilot then falls back to repository signals."""
    try:
        g = loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if g.empty else g


def save(root: Path, g: Goal) -> Path:
    state_dir(root, create=True)
    g.updated_at = time.time()
    p = path(root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(dumps(g), encoding="utf-8")
    os.replace(tmp, p)
    return p


def clear(root: Path) -> bool:
    try:
        path(root).unlink()
        return True
    except OSError:
        return False


def editor_argv() -> list[str]:
    import shlex

    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if ed:
        # Non-POSIX splitting keeps Windows backslashes; it also keeps the quotes, so drop them.
        return shlex.split(ed) if os.name != "nt" else [a.strip('"') for a in shlex.split(ed, posix=False)]
    return ["notepad"] if os.name == "nt" else ["vi"]


def edit(root: Path) -> Goal:
    """Open the goal in $VISUAL / $EDITOR. An invalid result is not kept."""
    p = path(root)
    before = p.read_text(encoding="utf-8") if p.exists() else None
    if before is None:
        save(root, Goal())
    try:
        subprocess.run([*editor_argv(), str(p)], check=False)
        return loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if before is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(before, encoding="utf-8")
        raise
