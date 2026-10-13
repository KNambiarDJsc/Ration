"""Opportunity discovery: find concrete, checkable work in a repository.

Deterministic and local. No model is asked "what should I do next?": every
opportunity comes from a signal that can be pointed at (a failing test, a lint
finding, a TODO at a line, a module without tests, an unchecked backlog item).

Sources are small functions grouped into packs. Add one with `@source(...)`, or
ship it in another package through the `ration.opportunity_sources` entry-point
group; the scorer, policy and scheduler never need to know where a task came from.

Descriptions name files and line numbers and never quote source code, because
task descriptions are stored in RATION's local database.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..difficulty import assess as assess_text
from ..models import TaskType
from . import models as m
from .goal import STATE_DIR, Goal
from .models import Opportunity, Target, fingerprint
from .verify import CheckResult, ProjectChecks, check_env, detect_checks, run_check, short_cmd

SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", ".git", ".venv", "venv", "__pycache__", "migrations", "third_party",
    "target", ".next", "coverage", STATE_DIR,
}  # fmt: skip
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".kt", ".swift", ".c", ".h", ".cpp", ".cs", ".php", ".sh"}
MAX_FILE_BYTES = 400_000
MAX_FILES = 20_000
MARKER_RE = re.compile(r"(?:#|//|/\*|<!--|--\s|^\s*\*)[^\n]*?\b(TODO|FIXME|XXX|HACK)\b")
SKIP_RES = [
    re.compile(r"@pytest\.mark\.skip\b|@unittest\.skip\b|pytest\.skip\("),
    re.compile(r"\b(?:it|test|describe)\.skip\(|\bxit\(|\bxdescribe\("),
    re.compile(r"\bt\.Skip\(|#\[ignore\]"),
]
STUB_RES = [
    re.compile(r"raise NotImplementedError"),
    re.compile(r"throw new Error\(\s*['\"](?:not implemented|todo)", re.I),
    re.compile(r"\b(?:todo|unimplemented)!\("),
    re.compile(r"panic\(\s*\"(?:not implemented|todo)", re.I),
]
ABSTRACT_RE = re.compile(r"\babstractmethod\b|\bABC\b|\bProtocol\b")
EMPTY_CATCH_RE = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}")
BACKLOG_FILES = ("TODO.md", "TASKS.md", "ROADMAP.md", "BACKLOG.md", "docs/roadmap.md", "docs/TODO.md")
UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.+)$", re.M)
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$")
ROADMAP_HEADING_RE = re.compile(r"^#{1,3}\s+(roadmap|todo|to do|planned|next steps)\s*$", re.I)


@dataclass
class RepoContext:
    root: Path
    cfg: dict[str, Any]
    checks: ProjectChecks
    goal: Goal | None = None
    files: list[str] = field(default_factory=list)
    is_git: bool = False
    dirty: set[str] = field(default_factory=set)
    recent: Counter[str] = field(default_factory=Counter)  # files touched by recent commits
    probe: bool = True  # may read-only project commands (tests, lint) be run?
    probes: dict[str, CheckResult] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    home: Path | None = None  # the project folder holding `.ration/` when `root` is a worktree of it
    tracked_state: set[str] = field(default_factory=set)  # files under .ration/ that git tracks
    _text: dict[str, str | None] = field(default_factory=dict)
    _ast: dict[str, ast.Module | None] = field(default_factory=dict)

    def read(self, rel: str) -> str | None:
        if rel not in self._text:
            p = self.root / rel
            try:
                self._text[rel] = p.read_text(encoding="utf-8", errors="replace") if p.stat().st_size <= MAX_FILE_BYTES else None
            except OSError:
                self._text[rel] = None
        return self._text[rel]

    def tree(self, rel: str) -> ast.Module | None:
        if rel not in self._ast:
            src = self.read(rel)
            try:
                self._ast[rel] = ast.parse(src) if src is not None else None
            except (SyntaxError, ValueError, RecursionError):
                self._ast[rel] = None
        return self._ast[rel]

    def code_files(self) -> list[str]:
        return [f for f in self.files if os.path.splitext(f)[1] in CODE_EXT]

    def run(self, cmd: str) -> CheckResult | None:
        """Run a read-only project command once (cached). None when probing is off."""
        if not self.probe or not cmd:
            return None
        if cmd not in self.probes:
            timeout = float(self.cfg["autopilot"]["probe_timeout_seconds"])
            self.probes[cmd] = run_check(cmd, self.root, timeout=timeout, env=check_env(self.root))
            if not self.probes[cmd].ran:
                self.notes.append(f"could not run `{cmd}`: {self.probes[cmd].tail.splitlines()[-1] if self.probes[cmd].tail else 'unknown error'}")
        return self.probes[cmd]


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def skipped(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in rel.split("/")[:-1])


def is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    parts = rel.split("/")[:-1]
    return (
        name.startswith("test_")
        or re.search(r"(_test\.\w+|\.(test|spec)\.\w+)$", name) is not None
        or name == "conftest.py"
        or any(p in ("tests", "test", "__tests__", "spec") for p in parts)
    )


def scan(
    root: Path,
    cfg: dict[str, Any],
    goal: Goal | None = None,
    *,
    probe: bool = True,
    checks: ProjectChecks | None = None,
    home: Path | None = None,
) -> RepoContext:
    """`checks` and `home` are given when `root` is an autopilot worktree: the commands
    were detected in the real project (its virtualenv lives there), and so does `.ration/`."""
    ctx = RepoContext(root=root, cfg=cfg, checks=checks or detect_checks(root, cfg), goal=goal, probe=probe, home=home or root)
    if checks is None:
        ctx.notes += ctx.checks.notes
    listed = _git(root, "ls-files", "-z") if (root / ".git").exists() else None
    if listed is not None:
        ctx.is_git = True
        files = [f for f in listed.split("\0") if f]
        ctx.tracked_state = {f for f in (_git(ctx.home or root, "ls-files", "-z", "--", STATE_DIR) or "").split("\0") if f}
        status = _git(root, "status", "--porcelain", "-z", "--no-renames") or ""
        ctx.dirty = {entry[3:] for entry in status.split("\0") if len(entry) > 3}
        log = _git(root, "log", "-30", "--name-only", "--pretty=format:") or ""
        ctx.recent = Counter(ln for ln in log.splitlines() if ln.strip())
    else:
        files = []
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for n in names:
                files.append(os.path.relpath(os.path.join(base, n), root).replace(os.sep, "/"))
            if len(files) > MAX_FILES:
                break
    ctx.files = [f for f in files if not skipped(f)][:MAX_FILES]
    return ctx


# ---- registry ----------------------------------------------------------------------------
SourceFn = Callable[[RepoContext], list[Opportunity]]


@dataclass
class Source:
    name: str
    pack: str
    fn: SourceFn


SOURCES: dict[str, Source] = {}
_plugins_loaded = False


def register(name: str, fn: SourceFn, pack: str = "developer") -> None:
    SOURCES[name] = Source(name, pack, fn)


def source(name: str, pack: str = "developer") -> Callable[[SourceFn], SourceFn]:
    def deco(fn: SourceFn) -> SourceFn:
        register(name, fn, pack)
        return fn

    return deco


def load_plugins() -> list[str]:
    """Sources from other installed packages (entry-point group `ration.opportunity_sources`).
    A plugin that fails to load is reported, never fatal."""
    global _plugins_loaded
    errors: list[str] = []
    if _plugins_loaded:
        return errors
    _plugins_loaded = True
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="ration.opportunity_sources"):
            try:
                register(ep.name, ep.load(), pack="plugin")
            except Exception as e:
                errors.append(f"opportunity pack '{ep.name}' failed to load: {type(e).__name__}")
    except Exception:
        pass
    return errors


def discover(ctx: RepoContext, packs: tuple[str, ...] | list[str]) -> list[Opportunity]:
    ctx.notes += load_plugins()
    found: dict[str, Opportunity] = {}
    for src in list(SOURCES.values()):
        if src.pack not in packs:
            continue
        try:
            for opp in src.fn(ctx):
                found.setdefault(opp.id, opp)
        except Exception as e:  # one broken source must not hide the others
            ctx.notes.append(f"source '{src.name}' failed: {type(e).__name__}: {e}"[:200])
    return list(found.values())


# ---- helpers -------------------------------------------------------------------------------
def _recent_bonus(ctx: RepoContext, files: list[str]) -> float:
    return 0.1 if any(ctx.recent.get(f) for f in files) else 0.0


def _lines(nums: list[int], cap: int = 8) -> str:
    return ", ".join(map(str, nums[:cap])) + (f" and {len(nums) - cap} more" if len(nums) > cap else "")


def _size(n: int, small: int, medium: int) -> str:
    return "xs" if n <= small else "s" if n <= medium else "m"


def _guarded(ctx: RepoContext) -> float:
    """Verifiability of a change that has no measurable target of its own."""
    return 0.6 if ctx.checks.test else 0.45 if ctx.checks.guards else 0.25


# ---- developer pack: probes ------------------------------------------------------------------
@source("failing_tests")
def failing_tests(ctx: RepoContext) -> list[Opportunity]:
    r = ctx.run(ctx.checks.test)
    if r is None or not r.ran or r.ok:
        return []
    if not r.failed:
        ctx.notes.append(f"`{ctx.checks.test}` exits with an error but reports no failing tests; not treated as a task")
        return []
    n = r.failed
    return [
        Opportunity(
            id=fingerprint("failing_tests", ctx.checks.test),
            title=f"Fix {n} failing test{'s' if n != 1 else ''}",
            description=(
                f"The test suite has {n} failing test(s). Find the cause of each failure and fix it in the code under test. "
                "Do not delete, skip or weaken tests to make them pass; if a test itself is wrong, fix the test and say why."
            ),
            category=m.FAILING_TESTS,
            source="failing_tests",
            value=0.9,
            risk=0.3,
            confidence=0.7 if n <= 5 else 0.5,
            verifiability=1.0,
            complexity="s" if n <= 2 else "m" if n <= 10 else "l",
            target=Target.PASS,
            checks=[ctx.checks.test],
            evidence=[f"`{short_cmd(ctx.checks.test)}`: {r.summary()}"],
        )
    ]


def _issue_task(ctx: RepoContext, cmd: str, category: str, noun: str, value: float, risk: float, conf: float) -> list[Opportunity]:
    r = ctx.run(cmd)
    if r is None or not r.ran or r.ok:
        return []
    n = r.issues
    count = f"{n} " if n else ""
    return [
        Opportunity(
            id=fingerprint(category, cmd),
            title=f"Resolve {count}{noun} issue{'s' if n != 1 else ''}",
            description=(
                f"`{short_cmd(cmd)}` reports {count}{noun} issue(s). Fix them properly in the code. "
                "Do not silence findings with ignore comments or by loosening the tool's configuration."
            ),
            category=category,
            source=category,
            value=min(value + 0.1, value + 0.002 * (n or 0)),
            risk=risk,
            confidence=conf if (n or 0) <= 40 else conf - 0.2,
            verifiability=1.0,
            complexity=_size(n or 10, 5, 30),
            target=Target.PASS,
            checks=[cmd],
            evidence=[f"`{short_cmd(cmd)}`: {r.summary()}"],
        )
    ]


@source("lint")
def lint(ctx: RepoContext) -> list[Opportunity]:
    return _issue_task(ctx, ctx.checks.lint, m.LINT, "lint", 0.45, 0.15, 0.85)


@source("types")
def types(ctx: RepoContext) -> list[Opportunity]:
    return _issue_task(ctx, ctx.checks.typecheck, m.TYPES, "type-check", 0.55, 0.25, 0.65)


# ---- developer pack: static signals ----------------------------------------------------------
@source("markers")
def markers(ctx: RepoContext) -> list[Opportunity]:
    out = []
    for f in ctx.code_files():
        src = ctx.read(f)
        if not src or "TODO" not in src and "FIXME" not in src and "XXX" not in src and "HACK" not in src:
            continue
        hits = [(i, mm.group(1)) for i, ln in enumerate(src.splitlines(), 1) if (mm := MARKER_RE.search(ln))]
        if not hits:
            continue
        kinds = Counter(k for _, k in hits)
        fixme = kinds["FIXME"] + kinds["XXX"]
        out.append(
            Opportunity(
                id=fingerprint("markers", f, sorted(kinds.items())),
                title=f"Resolve {len(hits)} {'FIXME' if fixme else 'TODO'}{'s' if len(hits) != 1 else ''} in {f}",
                description=(
                    f"`{f}` has {len(hits)} TODO/FIXME marker(s) at line(s) {_lines([i for i, _ in hits])}. "
                    "For each one that is small and clearly defined, do what it asks and remove the marker. "
                    "Leave any marker that needs a product decision or a large change, and say which ones you left and why."
                ),
                category=m.TODO,
                source="markers",
                files=[f],
                value=(0.6 if fixme else 0.45) + _recent_bonus(ctx, [f]),
                risk=0.35,
                confidence=0.5,
                verifiability=_guarded(ctx),
                complexity="s" if len(hits) <= 2 else "m",
                evidence=[", ".join(f"{n} {k}" for k, n in kinds.most_common())],
            )
        )
    return sorted(out, key=lambda o: -o.value)[:15]


@source("unfinished")
def unfinished(ctx: RepoContext) -> list[Opportunity]:
    out = []
    for f in ctx.code_files():
        src = ctx.read(f)
        if not src or is_test_file(f) or (f.endswith(".py") and ABSTRACT_RE.search(src)):
            continue
        hits = [i for i, ln in enumerate(src.splitlines(), 1) if any(rx.search(ln) for rx in STUB_RES)]
        if hits:
            out.append(
                Opportunity(
                    id=fingerprint("unfinished", f),
                    title=f"Implement {len(hits)} unfinished function{'s' if len(hits) != 1 else ''} in {f}",
                    description=(
                        f"`{f}` has {len(hits)} stub(s) that only raise 'not implemented' at line(s) {_lines(hits)}. "
                        "Implement the ones whose intended behaviour is clear from the callers, names and existing tests, and add tests for them. "
                        "Leave a stub alone if its behaviour would be a guess."
                    ),
                    category=m.UNFINISHED,
                    source="unfinished",
                    files=[f],
                    value=0.6 + _recent_bonus(ctx, [f]),
                    risk=0.5,
                    confidence=0.4,
                    verifiability=_guarded(ctx),
                    complexity="m",
                    evidence=[f"{len(hits)} stub(s)"],
                )
            )
    return out[:8]


@source("skipped_tests")
def skipped_tests(ctx: RepoContext) -> list[Opportunity]:
    out = []
    for f in ctx.code_files():
        if not is_test_file(f):
            continue
        src = ctx.read(f) or ""
        hits = [i for i, ln in enumerate(src.splitlines(), 1) if any(rx.search(ln) for rx in SKIP_RES)]
        if hits:
            out.append(
                Opportunity(
                    id=fingerprint("skipped_tests", f),
                    title=f"Re-enable {len(hits)} skipped test{'s' if len(hits) != 1 else ''} in {f}",
                    description=(
                        f"`{f}` skips {len(hits)} test(s) at line(s) {_lines(hits)}. For each, find out whether the reason still holds. "
                        "Re-enable and fix the ones that can pass; leave the ones skipped for a real reason (platform, missing service) untouched."
                    ),
                    category=m.SKIPPED_TESTS,
                    source="skipped_tests",
                    files=[f],
                    value=0.5,
                    risk=0.3,
                    confidence=0.35,
                    verifiability=0.7 if ctx.checks.test else 0.3,
                    complexity="s",
                    target=Target.MORE_TESTS if ctx.checks.test else Target.NONE,
                    evidence=[f"{len(hits)} skip marker(s)"],
                )
            )
    return out[:6]


@source("missing_tests")
def missing_tests(ctx: RepoContext) -> list[Opportunity]:
    if not ctx.checks.test:
        return []  # without a test command a new test cannot be confirmed
    tests = [f for f in ctx.code_files() if is_test_file(f)]
    corpus = "\n".join(filter(None, (ctx.read(t) for t in tests[:400])))
    names = {t.rsplit("/", 1)[-1] for t in tests}
    out = []
    for f in ctx.code_files():
        ext = os.path.splitext(f)[1]
        stem = os.path.splitext(f.rsplit("/", 1)[-1])[0]
        if is_test_file(f) or ext not in (".py", ".js", ".jsx", ".ts", ".tsx", ".go") or stem in ("__init__", "__main__", "index", "setup", "conftest", "main"):
            continue
        src = ctx.read(f)
        loc = sum(1 for ln in (src or "").splitlines() if ln.strip())
        if loc < 40:
            continue
        covered = (
            any(n in names for n in (f"test_{stem}.py", f"{stem}_test.py", f"{stem}_test.go", f"{stem}.test{ext}", f"{stem}.spec{ext}"))
            or re.search(rf"\b{re.escape(stem)}\b", corpus) is not None
        )
        if covered:
            continue
        out.append(
            Opportunity(
                id=fingerprint("missing_tests", f),
                title=f"Add tests for {f}",
                description=(
                    f"`{f}` ({loc} lines) has no tests. Add focused tests for its public behaviour, following the conventions of the existing test suite. "
                    "Test what the code does today; if you find a real bug, leave the code as it is, write the test as an expected failure and report the bug."
                ),
                category=m.MISSING_TESTS,
                source="missing_tests",
                files=[f],
                value=min(0.75, 0.5 + loc / 2000) + _recent_bonus(ctx, [f]),
                risk=0.15,
                confidence=0.7,
                verifiability=0.9,
                complexity="s" if loc < 150 else "m",
                target=Target.MORE_TESTS,
                evidence=[f"{loc} lines, not referenced by any test" + (", changed recently" if ctx.recent.get(f) else "")],
            )
        )
    return sorted(out, key=lambda o: -o.value)[:8]


def _swallowed(tree: ast.Module) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
            silent = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in node.body)
            if node.type is None or (broad and silent):
                lines.append(node.lineno)
    return lines


@source("error_handling")
def error_handling(ctx: RepoContext) -> list[Opportunity]:
    out = []
    for f in ctx.code_files():
        if is_test_file(f):
            continue
        hits: list[int] = []
        if f.endswith(".py"):
            tree = ctx.tree(f)
            hits = _swallowed(tree) if tree else []
        elif os.path.splitext(f)[1] in (".js", ".jsx", ".ts", ".tsx"):
            hits = [i for i, ln in enumerate((ctx.read(f) or "").splitlines(), 1) if EMPTY_CATCH_RE.search(ln)]
        if len(hits) >= 2:
            out.append(
                Opportunity(
                    id=fingerprint("error_handling", f),
                    title=f"Stop swallowing errors in {f} ({len(hits)} places)",
                    description=(
                        f"`{f}` catches errors too broadly or silently at line(s) {_lines(hits)}. Where a failure is hidden by accident, "
                        "narrow the exception type and handle or report it. Keep handlers that are deliberate (fail-open paths, cleanup) and say which ones you kept."
                    ),
                    category=m.ERROR_HANDLING,
                    source="error_handling",
                    files=[f],
                    value=0.4 + _recent_bonus(ctx, [f]),
                    risk=0.4,
                    confidence=0.45,
                    verifiability=_guarded(ctx),
                    complexity="s",
                    evidence=[f"{len(hits)} bare or silent handlers"],
                )
            )
    return sorted(out, key=lambda o: -o.value)[:5]


@source("refactor")
def long_functions(ctx: RepoContext) -> list[Opportunity]:
    if not ctx.checks.test:
        return []  # a refactor without tests around it is a gamble
    out = []
    for f in ctx.code_files():
        if not f.endswith(".py") or is_test_file(f):
            continue
        tree = ctx.tree(f)
        if tree is None:
            continue
        long = [
            (n.name, n.end_lineno - n.lineno + 1)
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.end_lineno and n.end_lineno - n.lineno + 1 >= 120
        ]
        if long:
            name, size = max(long, key=lambda x: x[1])
            out.append(
                Opportunity(
                    id=fingerprint("refactor", f, name),
                    title=f"Split the {size}-line function `{name}` in {f}",
                    description=(
                        f"`{name}` in `{f}` is {size} lines long. Extract well-named helper functions without changing behaviour or the public interface. "
                        "Keep the change local to this file; all existing tests must still pass."
                    ),
                    category=m.REFACTOR,
                    source="refactor",
                    files=[f],
                    value=0.3,
                    risk=0.45,
                    confidence=0.5,
                    verifiability=0.6,
                    complexity="m",
                    evidence=[f"{size} lines"],
                )
            )
    return out[:3]


@source("docs")
def docs(ctx: RepoContext) -> list[Opportunity]:
    out = []
    if ctx.files and not any(f.lower() in ("readme.md", "readme.rst", "readme.txt", "readme") for f in ctx.files):
        out.append(
            Opportunity(
                id=fingerprint("docs", "readme"),
                title="Write a README",
                description=(
                    "This project has no README. Write a short README.md from what the code and configuration actually show: what it is, "
                    "how to install it, how to run it and how to run its tests. Do not invent features."
                ),
                category=m.DOCS,
                source="docs",
                files=["README.md"],
                value=0.5,
                risk=0.05,
                confidence=0.8,
                verifiability=0.4,
                complexity="s",
                evidence=["no README"],
            )
        )
    for f in ctx.code_files():
        if not f.endswith(".py") or is_test_file(f):
            continue
        tree = ctx.tree(f)
        if tree is None:
            continue
        public = [n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and not n.name.startswith("_")]
        bare = [n for n in public if ast.get_docstring(n) is None]
        if len(public) >= 6 and len(bare) >= 6 and len(bare) / len(public) >= 0.6:
            out.append(
                Opportunity(
                    id=fingerprint("docs", f),
                    title=f"Document {len(bare)} public functions in {f}",
                    description=(
                        f"`{f}` has {len(bare)} public functions or classes without a docstring. Add short, accurate docstrings that say what each one "
                        "does and returns, in the style the project already uses. Change no behaviour."
                    ),
                    category=m.DOCS,
                    source="docs",
                    files=[f],
                    value=0.2,
                    risk=0.08,
                    confidence=0.8,
                    verifiability=_guarded(ctx),
                    complexity="s",
                    evidence=[f"{len(bare)} of {len(public)} public definitions undocumented"],
                )
            )
    return out[:5]


def _backlog_items(ctx: RepoContext) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    lower = {f.lower(): f for f in ctx.files}
    for name in BACKLOG_FILES:
        f = lower.get(name.lower())
        if f:
            items += [(f, t.strip()) for t in UNCHECKED_RE.findall(ctx.read(f) or "")]
    readme = lower.get("readme.md")
    if readme:
        inside = False
        for ln in (ctx.read(readme) or "").splitlines():
            if ln.startswith("#"):
                inside = bool(ROADMAP_HEADING_RE.match(ln))
            elif inside and (mm := BULLET_RE.match(ln)) and not mm.group(1).startswith("[x]"):
                items.append((readme, re.sub(r"^\[ \]\s*", "", mm.group(1)).strip()))
    return items


@source("backlog")
def backlog(ctx: RepoContext) -> list[Opportunity]:
    out = []
    for f, text in _backlog_items(ctx)[:12]:
        plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text).replace("`", "").replace("*", "")[:240]
        a = assess_text(plain)
        hard = a.task_type in (TaskType.ARCHITECTURE, TaskType.MIGRATION)
        # A checkbox in a task file is a task someone wrote down; a README roadmap bullet is an intention.
        specified = not f.lower().startswith("readme")
        out.append(
            Opportunity(
                id=fingerprint("backlog", f, plain[:80].lower()),
                title=plain[:90],
                description=(
                    f"From the project's backlog in `{f}`: {plain}\n"
                    "Implement the smallest complete version of this, with tests. If it turns out to need a design decision or a much larger change, "
                    "stop and write down what you found instead of guessing."
                ),
                category=m.FEATURE,
                source="backlog",
                value=0.6,
                risk=max(a.risk, 0.7 if hard else 0.4),
                confidence=(0.35 if a.confidence == "low" else 0.45) if specified else 0.2,
                verifiability=_guarded(ctx) - 0.1,
                complexity="xl" if hard else "s" if a.difficulty < 0.35 else "m" if a.difficulty < 0.6 else "l",
                evidence=[f"open item in {f}" if specified else f"roadmap entry in {f} (an intention, not a specified task)"]
                + [r for r in a.reasons if r.startswith("risk:")],
            )
        )
    return out


# ---- user pack: tasks you define yourself ---------------------------------------------------
RISK_WORDS = {"low": 0.15, "medium": 0.4, "high": 0.7}


@source("user_tasks", pack="user")
def user_tasks(ctx: RepoContext) -> list[Opportunity]:
    """`.ration/tasks.toml`:

    [[task]]
    title = "Keep the changelog current"
    description = "Add an entry for anything merged since the last release."
    check = "python scripts/check_changelog.py"   # optional; a string or a list
    risk = "low"                                   # low | medium | high
    """
    p = (ctx.home or ctx.root) / STATE_DIR / "tasks.toml"
    if not p.is_file():
        return []
    if f"{STATE_DIR}/tasks.toml" in ctx.tracked_state:
        # Committed to the repository, so it is repository content, not your instruction:
        # its `check` lines would be commands chosen by whoever wrote the repository.
        ctx.notes.append(f"{STATE_DIR}/tasks.toml is tracked by git and was ignored: task packs must be local, untracked files")
        return []
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        ctx.notes.append(f"{STATE_DIR}/tasks.toml is not valid TOML and was ignored ({e})"[:200])
        return []
    out = []
    for t in data.get("task") or []:
        if not isinstance(t, dict) or not str(t.get("title") or "").strip():
            continue
        checks = t.get("check") or []
        checks = [checks] if isinstance(checks, str) else [str(c) for c in checks if str(c).strip()] if isinstance(checks, list) else []
        title = str(t["title"]).strip()[:120]
        value = t.get("value")
        out.append(
            Opportunity(
                id=fingerprint("user", title.lower()),
                title=title,
                description=str(t.get("description") or title).strip()[:2000],
                category=str(t.get("category") or m.USER),
                source="user_tasks",
                files=[str(f) for f in t.get("files") or [] if isinstance(f, str)],
                value=float(value) if isinstance(value, int | float) and 0 <= value <= 1 else 0.7,
                risk=RISK_WORDS.get(str(t.get("risk") or "medium").lower(), 0.4),
                confidence=0.75,
                verifiability=1.0 if checks else _guarded(ctx),
                complexity=str(t.get("size")) if t.get("size") in m.COMPLEXITY_COST else "s",
                target=Target.PASS if checks else Target.NONE,
                checks=checks,
                evidence=[f"defined in {STATE_DIR}/tasks.toml"],
            )
        )
    return out


# ---- research pack: analysis only, no source changes -------------------------------------------
def _research(key: str, title: str, ask: str, value: float, evidence: list[str]) -> Opportunity:
    return Opportunity(
        id=fingerprint("research", key),
        title=title,
        description=(
            f"{ask}\nThis is read-only analysis: do not modify, create or delete any file. Base every statement on files you actually read "
            "and cite them as path:line. Finish with a prioritised list of concrete next steps, each small enough to do and verify on its own. "
            "Your final message is the report."
        ),
        category=m.RESEARCH,
        source="research",
        value=value,
        risk=0.05,
        confidence=0.8,
        verifiability=0.35,
        complexity="m",
        evidence=evidence,
        writes_code=False,
    )


@source("research", pack="research")
def research(ctx: RepoContext) -> list[Opportunity]:
    code = ctx.code_files()
    if not code:
        return []
    out = []
    if ctx.goal and not ctx.goal.empty:
        out.append(
            _research(
                "goal-gap",
                "Gap analysis against the project goal",
                "Compare the repository with the project goal and priorities given below. What already exists, what is missing, and what is the "
                "shortest verifiable path to each priority?",
                0.75,
                ["a project goal is set"],
            )
        )
    n_markers = sum(len(MARKER_RE.findall(ctx.read(f) or "")) for f in code[:2000])
    out.append(
        _research(
            "tech-debt",
            "Technical-debt review",
            "Review this codebase for technical debt: duplicated logic, modules that do too much, fragile error handling, dead code and unfinished work. "
            "Rank the findings by how much they slow down or endanger future changes.",
            0.55,
            [f"{len(code)} source files, {n_markers} TODO/FIXME markers"],
        )
    )
    tests = [f for f in code if is_test_file(f)]
    out.append(
        _research(
            "test-gaps",
            "Test-gap analysis",
            "Map which behaviour is covered by tests and which is not. Name the riskiest untested paths and, for each, the test that would cover it.",
            0.6 if len(tests) * 4 < len(code) else 0.45,
            [f"{len(tests)} test files for {len(code) - len(tests)} source files"],
        )
    )
    out.append(
        _research(
            "architecture",
            "Architecture map",
            "Describe the architecture as it is: the main modules, what each owns, how data flows between them, and where the boundaries leak.",
            0.45,
            [f"{len(code)} source files"],
        )
    )
    manifests = [f for f in ctx.files if f in ("pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml")]
    if manifests:
        out.append(
            _research(
                "dependencies",
                "Dependency review",
                f"Review the dependencies declared in {', '.join(manifests)}: which are unused, duplicated in purpose, unpinned or carrying more weight than "
                "the project needs? Use only what is in the repository; do not install or upgrade anything.",
                0.4,
                [f"manifest: {', '.join(manifests)}"],
            )
        )
    return out
