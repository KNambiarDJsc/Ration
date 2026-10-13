"""Verification: which checks a project has, running them, and comparing before with after.

A check is a shell command the project already uses (tests, lint, type check).
Autopilot runs each one on the untouched base commit and again after the agent
finished, and accepts a change only when the comparison says it helped and
nothing got worse.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..normalize import parse_test_output, test_runner
from ..providers.base import stop_process
from ..shell import shell_argv
from .models import CheckResult, Opportunity, Outcome, Target

ISSUE_RES = [
    re.compile(r"Found (\d+) errors?"),  # ruff, mypy
    re.compile(r"(\d+) problems? \("),  # eslint
    re.compile(r"(\d+) errors?, \d+ warnings?"),  # pyright, eslint summary
    re.compile(r"Found (\d+) errors? in \d+ files?"),  # tsc
]
SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+(?:[ \t]+\S+)?"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b([a-z0-9_]*(?:secret|token|password|passwd|api[_-]?key|access[_-]?key)[a-z0-9_]*)(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"),
]
SUMMARY_LINES = 8
SECRET_ENV_RE = re.compile(r"(?i)(secret|token|password|passwd|api_?key|access_?key|credential)")


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """Remove things that look like credentials, and the values of secret-looking
    environment variables, before anything is stored or shown."""
    if not text:
        return text
    for name, value in (os.environ if env is None else env).items():
        if len(value) >= 8 and SECRET_ENV_RE.search(name) and value in text:
            text = text.replace(value, "[redacted]")
    for i, rx in enumerate(SECRET_RES):
        text = rx.sub(r"\1\2[redacted]" if i == len(SECRET_RES) - 1 else "[redacted]", text)
    return text


def short_cmd(cmd: str) -> str:
    """A check command without the absolute interpreter path, for display."""
    return re.sub(r'"[^"]*[/\\]python(?:\.exe)?"', "python", cmd)


def has_secret(text: str) -> bool:
    # Only the concrete credential formats: header and `key = value` shapes are ordinary code.
    return any(rx.search(text) for rx in SECRET_RES[:7])


def count_issues(output: str) -> int | None:
    for rx in ISSUE_RES:
        m = rx.findall(output or "")
        if m:
            return int(m[-1])
    n = len(re.findall(r"error TS\d+", output or ""))
    return n or None


# ---- which checks does this project have? ---------------------------------------------------
@dataclass
class ProjectChecks:
    test: str = ""
    lint: str = ""
    typecheck: str = ""
    kind: str = ""  # python | node | go | rust | make | ""
    notes: list[str] = field(default_factory=list)

    @property
    def guards(self) -> list[str]:
        """Checks that must not get worse, whatever the task was."""
        return [c for c in (self.test, self.lint) if c]


def project_python(root: Path) -> str:
    """The project's own interpreter when it has a virtualenv, else `python` on PATH.
    RATION itself may live in an isolated tool environment without the project's packages."""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"):
        if (root / rel).is_file():
            return f'"{(root / rel).resolve().as_posix()}"'
    return "python"


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


# One detector per ecosystem: (root, checks) -> True when the project is of that kind.
# The first that recognises the project wins; add one to support another ecosystem.
Detector = Callable[[Path, "ProjectChecks"], bool]


def _python(root: Path, c: ProjectChecks) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "noxfile.py")
    if not any((root / f).exists() for f in markers):
        return False
    c.kind = "python"
    tool = _toml(root / "pyproject.toml").get("tool")
    tool = tool if isinstance(tool, dict) else {}
    py = project_python(root)
    has_tests = any((root / d).is_dir() for d in ("tests", "test")) or "pytest" in tool or (root / "pytest.ini").exists()
    if has_tests:
        c.test = f"{py} -m pytest -p no:cacheprovider"
    elif (root / "noxfile.py").exists():
        c.test = "nox"
    elif (root / "tox.ini").exists():
        c.test = "tox -q"
    if "ruff" in tool or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        c.lint = f"{py} -m ruff check --no-cache ."
    elif (root / ".flake8").exists():
        c.lint = f"{py} -m flake8"
    if "mypy" in tool or (root / "mypy.ini").exists():
        c.typecheck = f"{py} -m mypy --no-incremental ."
    elif (root / "pyrightconfig.json").exists():
        c.typecheck = "pyright"
    return True


def _node(root: Path, c: ProjectChecks) -> bool:
    if not (root / "package.json").exists():
        return False
    c.kind = "node"
    try:
        scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        scripts = {}
    pm = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "bun" if (root / "bun.lockb").exists() else "npm"
    if isinstance(scripts, dict):
        if scripts.get("test") and "no test specified" not in str(scripts["test"]):
            c.test = f"{pm} test"
        if scripts.get("lint"):
            c.lint = f"{pm} run lint"
        for name in ("typecheck", "type-check", "tsc"):
            if scripts.get(name):
                c.typecheck = f"{pm} run {name}"
                break
    if not (root / "node_modules").is_dir():
        c.notes.append('node_modules is missing: set `autopilot.setup_command` (e.g. "npm ci") so checks can run in a fresh worktree')
    return True


def _go(root: Path, c: ProjectChecks) -> bool:
    if not (root / "go.mod").exists():
        return False
    c.kind, c.test, c.lint = "go", "go test ./...", "go vet ./..."
    return True


def _rust(root: Path, c: ProjectChecks) -> bool:
    if not (root / "Cargo.toml").exists():
        return False
    c.kind, c.test = "rust", "cargo test"
    return True


def _java(root: Path, c: ProjectChecks) -> bool:
    if (root / "pom.xml").exists():
        c.kind, c.test = "java", "mvn -q test"
        return True
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        wrapper = "gradlew.bat" if os.name == "nt" else "./gradlew"
        c.kind, c.test = "java", f"{wrapper} test" if (root / "gradlew").exists() else "gradle test"
        return True
    return False


def _make(root: Path, c: ProjectChecks) -> bool:
    try:
        if re.search(r"^test:", (root / "Makefile").read_text(encoding="utf-8", errors="replace"), re.M):
            c.kind, c.test = "make", "make test"
            return True
    except OSError:
        pass
    return False


DETECTORS: list[Detector] = [_python, _node, _go, _rust, _java, _make]
# A CI step is reused only when it is a plain invocation: these characters cannot chain,
# redirect or substitute, so text from a workflow file never becomes shell syntax.
CI_ARGS = r"((?: [\w./=:@,-]+)*)"
CI_RUFF_RE = re.compile(rf"^\s*(?:-\s*)?run:\s*(?:python -m )?ruff check{CI_ARGS}\s*$", re.M)
CI_PYTEST_RE = re.compile(rf"^\s*(?:-\s*)?run:\s*(?:python -m )?pytest{CI_ARGS}\s*$", re.M)


def _from_ci(root: Path, c: ProjectChecks) -> None:
    """Prefer the exact checks CI runs (GitHub Actions, Python projects): the paths and
    options a project gates merges on are the right definition of "passing"."""
    if c.kind != "python":
        return
    wf = root / ".github" / "workflows"
    try:
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(wf.glob("*.y*ml"))[:20])
    except OSError:
        return
    py = project_python(root)
    if c.lint and "ruff" in c.lint and (hit := CI_RUFF_RE.search(text)) and hit.group(1).strip():
        c.lint = f"{py} -m ruff check --no-cache{hit.group(1)}"
        c.notes.append("lint command taken from the CI workflow")
    if c.test and "pytest" in c.test and (hit := CI_PYTEST_RE.search(text)) and hit.group(1).strip():
        c.test = f"{py} -m pytest -p no:cacheprovider{hit.group(1)}"
        c.notes.append("test command taken from the CI workflow")


def detect_checks(root: Path, cfg: dict[str, Any] | None = None) -> ProjectChecks:
    """From project configuration only; nothing is executed here. Commands configured
    under `[autopilot]` win over detection."""
    c = ProjectChecks()
    for detector in DETECTORS:
        if detector(root, c):
            break
    _from_ci(root, c)
    ap = (cfg or {}).get("autopilot") or {}
    for key, attr in (("test_command", "test"), ("lint_command", "lint"), ("typecheck_command", "typecheck")):
        if str(ap.get(key) or "").strip():
            setattr(c, attr, str(ap[key]).strip())
    return c


def check_env(root: Path) -> dict[str, str]:
    """Environment for checks. The tree being checked comes first on PYTHONPATH, so an
    editable install that points at the main checkout cannot shadow a worktree's code."""
    env = dict(os.environ)
    paths = [str(root / "src")] if (root / "src").is_dir() else []
    paths.append(str(root))
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("RATION_RUN_ID", None)
    return env


def run_check(cmd: str, cwd: str | os.PathLike[str], *, timeout: float = 900, env: dict[str, str] | None = None) -> CheckResult:
    """Run one check command and read its verdict.

    Trust boundary: `cmd` is a shell command line that comes from your configuration or
    from project detection, exactly like the checks you pass to `ration run --check`. It
    runs the project's own code. It is never built from task text, model output or file
    contents. On timeout the whole process tree is stopped, so a hung test suite cannot
    outlive the check."""
    argv = shell_argv(cmd)
    t0 = time.time()
    try:
        with tempfile.TemporaryFile() as out_file:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=out_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, shell=isinstance(argv, str))
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_process(proc)  # type: ignore[arg-type]
                proc.wait(timeout=30)
                return CheckResult(cmd, False, ran=False, tail=f"timed out after {timeout:g}s", seconds=time.time() - t0)
            except BaseException:
                stop_process(proc)  # type: ignore[arg-type]
                raise
            out_file.seek(0)
            out = out_file.read().decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as e:
        return CheckResult(cmd, False, ran=False, tail=redact(str(e))[:300], seconds=time.time() - t0)
    ok = rc == 0
    tail = "\n".join(out.strip().splitlines()[-12:])
    passed = failed = issues = None
    if test_runner(cmd):
        # Counts are read from the runner's closing summary only. Anything earlier is the
        # tests' own output, which can itself contain text like "3 passed, 1 failed".
        meta = parse_test_output("\n".join(out.strip().splitlines()[-SUMMARY_LINES:]))
        passed, failed = meta.get("passed"), meta.get("failed")
    elif not ok:
        issues = count_issues(out)
    # 126 / 127 / 9009: the shell could not start the program. That is "cannot verify", not "failing".
    missing = rc in (126, 127, 9009) or ("No module named" in tail and passed is None and failed is None and issues is None)
    return CheckResult(cmd, ok, not missing, passed, failed, issues, redact(tail)[-1500:], time.time() - t0)


def run_all(cmds: list[str], cwd: str | os.PathLike[str], *, timeout: float = 900, env: dict[str, str] | None = None) -> dict[str, CheckResult]:
    return {c: run_check(c, cwd, timeout=timeout, env=env) for c in dict.fromkeys(cmds)}


# ---- comparing before with after ------------------------------------------------------------
def worse(before: CheckResult, after: CheckResult) -> str | None:
    """Why `after` is a regression of `before`, or None."""
    if not before.ran:
        return None  # nothing to compare against
    if not after.ran:
        return f"`{short_cmd(before.cmd)}` could run before and cannot run now"
    if before.failed is not None or after.failed is not None:
        bf, af = before.failed or 0, after.failed or 0
        if af > bf:
            return f"{af - bf} more failing test{'s' if af - bf != 1 else ''} ({before.summary()} → {after.summary()})"
        if before.passed is not None and after.passed is not None and after.passed < before.passed and af >= bf:
            return f"fewer tests pass ({before.summary()} → {after.summary()})"
    if before.ok and not after.ok:
        return f"`{short_cmd(before.cmd)}` passed before and fails now ({after.summary()})"
    if before.issues is not None and after.issues is not None and after.issues > before.issues:
        return f"{after.issues - before.issues} new issue(s) from `{short_cmd(before.cmd)}`"
    return None


def better(before: CheckResult, after: CheckResult) -> bool:
    if not (before.ran and after.ran):
        return False
    if not before.ok and after.ok:
        return True
    if before.failed is not None and after.failed is not None and after.failed < before.failed:
        return True
    return before.issues is not None and after.issues is not None and after.issues < before.issues


def judge(
    opp: Opportunity,
    before: dict[str, CheckResult],
    after: dict[str, CheckResult],
    *,
    changed: bool,
    test_cmd: str = "",
) -> tuple[Outcome, str]:
    """The verdict for one task, with the sentence that explains it."""
    for cmd, b in before.items():
        a = after.get(cmd)
        if a is not None and (why := worse(b, a)):
            return Outcome.REGRESSION, why
    if not changed:
        return Outcome.NO_CHANGE, "the agent made no changes"
    if opp.target == Target.PASS:
        pairs = [(before[c], after[c]) for c in opp.checks if c in before and c in after]
        if not pairs or any(not a.ran for _, a in pairs):
            return Outcome.UNVERIFIABLE, "the task's check could not be run"
        if all(a.ok for _, a in pairs):
            return Outcome.SUCCESS, "; ".join(f"`{short_cmd(b.cmd)}`: {b.summary()} → {a.summary()}" for b, a in pairs)
        if any(better(b, a) for b, a in pairs):
            return Outcome.IMPROVED, "; ".join(f"`{short_cmd(b.cmd)}`: {b.summary()} → {a.summary()}" for b, a in pairs)
        return Outcome.FAILED, "the check is no better than before: " + "; ".join(a.summary() for _, a in pairs)
    if opp.target == Target.MORE_TESTS:
        b, a = before.get(test_cmd), after.get(test_cmd)
        if b is None or a is None or not (b.ran and a.ran) or a.passed is None:
            return Outcome.UNVERIFIABLE, "test counts are not available to confirm new tests"
        if a.passed > (b.passed or 0) and a.ok:
            return Outcome.SUCCESS, f"{a.passed - (b.passed or 0)} more passing tests ({b.summary()} → {a.summary()})"
        if a.passed > (b.passed or 0):
            return Outcome.IMPROVED, f"more tests pass, but the suite is not green ({b.summary()} → {a.summary()})"
        return Outcome.FAILED, f"no additional passing tests ({b.summary()} → {a.summary()})"
    ran = [a for a in after.values() if a.ran]
    if not ran:
        return Outcome.UNVERIFIABLE, "there is no check that can confirm this change"
    return Outcome.SUCCESS, "no regression in " + ", ".join(f"`{short_cmd(a.cmd)}` ({a.summary()})" for a in ran) + "; the change itself is not independently verified"

