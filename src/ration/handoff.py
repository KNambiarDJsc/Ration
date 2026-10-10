"""Restart kit: `/clear` without amnesia.

Builds a compact, factual hand-off from a Claude Code session's transcript and
the repository's current git state. No model is used, so it's free, instant and
cannot hallucinate: every line comes from what actually happened.

Why: the biggest quota sink is a long session re-reading a huge context on
every call (see `ration xray`), and people keep such sessions alive because
/clear loses the thread and /compact costs a model call and is lossy. A restart
kit makes starting fresh cheap.

Privacy: this module reads message text at run time (the goal and where the
session left off), so the output contains short excerpts of your prompts. It is
only printed/copied on request, or, with `handoff.auto = true`, written to a
short-lived local file that is deleted when used or after its TTL. It is never
put in RATION's database.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, transcript
from .normalize import COUNT_RE, FAIL_ID_RES, test_runner

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SHELL_TOOLS = {"Bash", "PowerShell"}
DENIED_MARKERS = ("requires approval", "requested permissions", "requires appr", "was denied", "not allowed")
MAX_PROMPT_CHARS = 320
MAX_LAST_CHARS = 520


@dataclass
class TestRun:
    cmd: str
    ok: bool
    passed: int | None = None
    failed: int | None = None
    failing: list[str] = field(default_factory=list)
    status: str = "failing"  # passing | failing | errored
    full_cmd: bool = True  # False when the command was too long to show whole


@dataclass
class Facts:
    prompts: list[str] = field(default_factory=list)
    changed: Counter[str] = field(default_factory=Counter)
    read: Counter[str] = field(default_factory=Counter)
    last_test: TestRun | None = None
    last_failed_cmd: str | None = None
    denied: int = 0
    last_text: str = ""
    ctx_tokens: int = 0
    session: str = ""
    last_ts: float | None = None
    cwd: str | None = None


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _user_text(content: Any) -> str | None:
    """The human-typed text of a user message, or None for tool results / system tags."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    text = "\n".join(p for p in parts if p).strip()
    if not text or text.startswith(("<", "[Request interrupted", "Caveat:")):
        return None
    return text


def _result_text(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return str(c or "")


def _rel(path: str, cwd: str | None) -> str:
    p = path.replace("\\", "/")
    if cwd:
        base = cwd.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(base.lower()):
            return p[len(base):]
    return p


def extract(path: str | os.PathLike[str]) -> Facts:
    f = Facts(session=Path(path).stem)
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        lines = Path(path).read_bytes().split(b"\n")
    except OSError:
        return f
    for raw in lines:
        if b'"user"' not in raw and b'"assistant"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("isSidechain"):
            continue
        t = r.get("type")
        if r.get("cwd") and not f.cwd:
            f.cwd = r["cwd"]
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content")
        ts = transcript._ts(r.get("timestamp"))
        if ts:
            f.last_ts = ts
        if t == "user":
            if isinstance(content, list):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    name, ti = pending.pop(str(b.get("tool_use_id")), ("?", {}))
                    text = _result_text(b.get("content"))
                    err = bool(b.get("is_error"))
                    if err and any(m in text.lower() for m in DENIED_MARKERS):
                        f.denied += 1
                        continue
                    if name in EDIT_TOOLS and not err:
                        p = ti.get("file_path") or ti.get("notebook_path")
                        if p:
                            f.changed[str(p)] += 1
                    elif name in READ_TOOLS and not err:
                        p = ti.get("file_path") or ti.get("notebook_path")
                        if p:
                            f.read[str(p)] += 1
                    elif name in SHELL_TOOLS:
                        cmd = str(ti.get("command") or "")
                        if test_runner(cmd):
                            counts: dict[str, int] = {}
                            for n, kind in COUNT_RE.findall(text):
                                key = "passed" if kind in ("passed", "passing") else "failed"
                                counts[key] = counts.get(key, 0) + int(n)
                            failing: list[str] = []
                            for rx in FAIL_ID_RES:
                                failing += [m.strip() for m in rx.findall(text)]
                            failing = [x.split("::")[-1] for x in dict.fromkeys(failing)][:5]
                            if counts or failing:
                                status = "failing" if (counts.get("failed") or failing) else "passing"
                            else:
                                status = "errored" if err else "passing"  # errored: no test summary to trust
                            f.last_test = TestRun(
                                _clip(cmd, 90), status == "passing", counts.get("passed"), counts.get("failed"), failing, status, len(cmd) <= 90
                            )
                        elif err:
                            f.last_failed_cmd = _clip(cmd, 100)
            txt = _user_text(content)
            if txt:
                f.prompts.append(txt)
        elif t == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    pending[str(b.get("id"))] = (str(b.get("name")), b.get("input") if isinstance(b.get("input"), dict) else {})
                elif b.get("type") == "text" and b.get("text", "").strip():
                    f.last_text = b["text"].strip()
    calls = transcript.iter_calls(path)
    if calls:
        f.ctx_tokens = calls[-1].ctx
    return f


def git_state(cwd: str | None) -> dict[str, Any] | None:
    if not cwd or not Path(cwd).is_dir():
        return None

    def run(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace")
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    status = run("status", "--short", "--branch")
    if status is None:
        return None
    lines = [ln for ln in status.splitlines() if ln.strip()]
    stat = (run("diff", "--stat") or "").strip().splitlines()
    return {"branch": lines[0].lstrip("# ") if lines else "", "changes": lines[1:], "summary": stat[-1].strip() if stat else ""}


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "earlier"
    s = max(0, now - ts)
    return f"{s / 60:.0f} min ago" if s < 5400 else f"{s / 3600:.0f} h ago" if s < 172800 else f"{s / 86400:.0f} d ago"


def render(f: Facts, git: dict[str, Any] | None, *, max_chars: int = 3000, now: float | None = None) -> str:
    now = time.time() if now is None else now
    project = Path(f.cwd).name if f.cwd else "session"
    ctx = f", context was {f.ctx_tokens // 1000}k tokens" if f.ctx_tokens else ""
    head = [
        f"# Restart kit: {project} (session {f.session[:8]}, {_ago(f.last_ts, now)}{ctx})",
        "_Built by RATION from the session record and the repo; no model was used. It is a map, not the full history: re-read the files you need._",
        "",
    ]
    goal: list[str] = []
    if f.prompts:
        goal = ["## Goal", _clip(f.prompts[0], MAX_PROMPT_CHARS)]
        recent = f.prompts[1:][-3:]
        if recent:
            goal += ["", "Recent asks:"] + [f"- {_clip(p, 200)}" for p in recent]
        goal.append("")
    repo: list[str] = []
    if git:
        repo = ["## State of the repo", f"Branch: `{git['branch']}`"]
        repo += [f"    {ln}" for ln in git["changes"][:15]]
        if len(git["changes"]) > 15:
            repo.append(f"    … and {len(git['changes']) - 15} more")
        if git["summary"]:
            repo.append(f"Diff: {git['summary']}")
        if not git["changes"]:
            repo.append("Working tree is clean.")
        repo.append("")
    did: list[str] = ["## What happened"]
    if f.changed:
        did.append("- Changed: " + ", ".join(f"`{_rel(p, f.cwd)}`" + (f" (x{n})" if n > 1 else "") for p, n in f.changed.most_common(8)))
    else:
        did.append("- No files were changed.")
    if f.last_test:
        t = f.last_test
        if t.passed is not None or t.failed is not None:
            counts = f"{t.passed or 0} passed, {t.failed or 0} failed"
        else:
            counts = "errored before producing a test summary" if t.status == "errored" else t.status
        did.append(f"- Last test run: `{t.cmd}` → {counts}" + (f"; failing: {', '.join(t.failing)}" if t.failing else ""))
    if f.last_failed_cmd:
        did.append(f"- Last failing command: `{f.last_failed_cmd}`")
    if f.denied:
        did.append(f"- {f.denied} tool call(s) were denied by permissions (check your allow rules before retrying).")
    reads = [f"`{_rel(p, f.cwd)}`" for p, _ in f.read.most_common(6) if p not in f.changed]
    if reads:
        did.append("- Mostly read: " + ", ".join(reads))
    did.append("")
    left: list[str] = []
    if f.last_text:
        left = ["## Where it left off", "> " + _clip(f.last_text, MAX_LAST_CHARS), ""]
    nxt = ["## Next", "Continue from \"Where it left off\"."]
    if f.last_test and f.last_test.full_cmd:
        nxt[-1] += f" Verify with `{f.last_test.cmd}`."
    # Trim lowest-value pieces first until it fits.
    blocks = [head, goal, repo, did, left, nxt]
    text = "\n".join(line for b in blocks for line in b)
    if len(text) > max_chars:
        goal = goal[:2] + [""] if goal else goal
        text = "\n".join(line for b in [head, goal, repo, did, left, nxt] for line in b)
    if len(text) > max_chars:
        repo = repo[:8] + [""] if repo else repo
        text = "\n".join(line for b in [head, goal, repo, did, left, nxt] for line in b)
    return text[: max(0, max_chars - 1)].rstrip() + "\n"


def build(path: str | os.PathLike[str], cwd: str | None = None, *, max_chars: int = 3000) -> str:
    f = extract(path)
    return render(f, git_state(cwd or f.cwd), max_chars=max_chars)


# ---- finding sessions ---------------------------------------------------------------------
def first_cwd(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            for _ in range(80):
                line = fh.readline()
                if not line:
                    break
                if b'"cwd"' in line:
                    try:
                        c = json.loads(line).get("cwd")
                    except ValueError:
                        continue
                    if isinstance(c, str):
                        return c
    except OSError:
        pass
    return None


def _same(a: str | None, b: str) -> bool:
    return bool(a) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def find_session(cwd: str, root: Path | None = None, session: str | None = None) -> Path | None:
    root = root or (Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects")
    if not root.is_dir():
        return None
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        if session:
            if p.stem.startswith(session):
                return p
        elif _same(first_cwd(p), cwd):
            return p
    return None


# ---- auto carry-over (opt-in) -------------------------------------------------------------
def _dir() -> Path:
    return config.ration_home() / "handoffs"


def _key(cwd: str) -> str:
    import hashlib

    return hashlib.sha1(os.path.normcase(os.path.abspath(cwd)).encode()).hexdigest()[:16]


def save_pending(cwd: str, text: str) -> Path:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_key(cwd)}.md"
    p.write_text(text, encoding="utf-8")
    return p


def take_pending(cwd: str, ttl_minutes: float, now: float | None = None) -> str | None:
    """Return and delete the pending hand-off for this directory if it is fresh."""
    p = _dir() / f"{_key(cwd)}.md"
    now = time.time() if now is None else now
    try:
        age = now - p.stat().st_mtime
        text = p.read_text(encoding="utf-8")
        p.unlink()
    except OSError:
        return None
    return text if age <= ttl_minutes * 60 else None


def purge_expired(ttl_minutes: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    n = 0
    for p in _dir().glob("*.md") if _dir().is_dir() else []:
        try:
            if now - p.stat().st_mtime > ttl_minutes * 60:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def clipboard(text: str) -> bool:
    import shutil

    try:
        if os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True, timeout=5)
            return True
        for exe, args in (("pbcopy", []), ("wl-copy", []), ("xclip", ["-selection", "clipboard"])):
            if shutil.which(exe):
                subprocess.run([exe, *args], input=text.encode("utf-8"), check=True, timeout=5)
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


# Re-exported for tests
__all__ = ["Facts", "build", "extract", "find_session", "git_state", "render", "save_pending", "take_pending", "purge_expired", "clipboard", "re"]
