"""Git isolation: every autopilot task works in its own worktree, on its own branch.

    your checkout (any branch, detached, clean or dirty)   never written to
    <ration home>/worktrees/<repo>/<run>/<task>            where the agent works
    branch ration/auto/<run>/<task>                        kept only if the change was accepted
    branch ration/auto/<run>/accepted                      everything accepted in the run, together

Accepted changes are stacked: the first task starts from the commit your HEAD
pointed to when the run began, and each later task starts from the last accepted
commit (the run's *candidate*). So every task is verified on top of everything
accepted before it, a rejected task leaves no trace in the candidate, and
`accepted` is one linear, reviewable branch. Nothing is merged into your
branches and nothing is ever pushed.

This module only removes things it created: worktrees under RATION's own
directory and branches under `ration/auto/`. Both are checked before every
removal.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .policy import DiffStats

BRANCH_PREFIX = "ration/auto/"
MAX_SCAN_BYTES = 2_000_000
# Used for autopilot's own commits only when the repository has no git identity at all.
FALLBACK_IDENTITY = ("RATION Autopilot", "ration-autopilot@localhost")


class GitError(RuntimeError):
    pass


def git(cwd: str | os.PathLike[str], *args: str, check: bool = True, timeout: float = 120) -> str:
    """Run git with an argument list (never a shell string)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git {args[0]}: {e}") from e
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


@contextlib.contextmanager
def no_network_git() -> Iterator[None]:
    """While an agent runs, every git process it starts inherits `protocol.allow=never`:
    push, fetch, pull and clone fail inside git itself, whatever the agent was told and
    whichever tool it used to call git. Local operations (commit, diff, worktree) work."""
    try:
        n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    added = {"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "protocol.allow", f"GIT_CONFIG_VALUE_{n}": "never", "GIT_TERMINAL_PROMPT": "0"}
    saved = {k: os.environ.get(k) for k in added}
    os.environ.update(added)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def worktrees_root() -> Path:
    return config.ration_home() / "worktrees"


def repo_key(root: Path) -> str:
    return hashlib.sha1(os.path.normcase(str(root.resolve())).encode()).hexdigest()[:10]


@dataclass
class TaskTree:
    path: Path
    branch: str
    start: str  # the commit the task began from (the candidate at that moment)


@dataclass
class Fingerprint:
    """What your repository looked like, to prove autopilot left it alone: the checkout
    (HEAD, branch, status) and everything outside autopilot's own namespace (branches,
    tags, remote refs, remote URLs, stashes)."""

    head: str
    branch: str
    status: str
    refs: str
    remotes: str

    def changed(self, other: Fingerprint) -> str | None:
        if (self.head, self.branch, self.status) != (other.head, other.branch, other.status):
            return "your checkout changed"
        if self.refs != other.refs:
            return "a branch, tag, stash or remote-tracking ref outside autopilot's own changed"
        if self.remotes != other.remotes:
            return "the repository's remote configuration changed"
        return None


class Workspace:
    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.run_id = run_id
        try:
            self.base = git(root, "rev-parse", "--verify", "HEAD").strip()
        except GitError as e:
            raise GitError("this repository has no commits yet; autopilot needs a commit to branch from") from e
        name = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        self.detached = name in ("", "HEAD")
        self.base_branch = self.base[:10] if self.detached else name
        self.home = worktrees_root() / repo_key(root) / run_id
        self.prefix = f"{BRANCH_PREFIX}{run_id}/"
        self._candidate_tree: Path | None = None

    @staticmethod
    def is_repo(root: Path) -> bool:
        try:
            return git(root, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
        except GitError:
            return False

    def fingerprint(self) -> Fingerprint:
        refs = git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", check=False)
        mine = f"refs/heads/{self.prefix}"
        return Fingerprint(
            git(self.root, "rev-parse", "HEAD", check=False).strip(),
            git(self.root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip(),
            git(self.root, "status", "--porcelain", check=False),
            "\n".join(ln for ln in refs.splitlines() if not ln.startswith(mine)),
            git(self.root, "config", "--get-regexp", r"^remote\.", check=False),
        )

    # ---- one task ----------------------------------------------------------------------
    def create(self, name: str, start: str | None = None) -> TaskTree:
        """A new worktree and branch at `start` (default: the base commit). A name that is
        already taken, by a branch or a directory, gets a numeric suffix: nothing existing
        is ever reused or overwritten."""
        start = start or self.base
        last: GitError | None = None
        for n in range(1, 6):
            suffix = "" if n == 1 else f"-{n}"
            tree = TaskTree(self.home / f"{name}{suffix}", f"{self.prefix}{name}{suffix}", start)
            if tree.path.exists() or git(self.root, "rev-parse", "--verify", "--quiet", f"refs/heads/{tree.branch}", check=False).strip():
                continue
            tree.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                git(self.root, "worktree", "add", "-b", tree.branch, str(tree.path), start)
                return tree
            except GitError as e:
                last = e
        raise last or GitError(f"could not find a free branch name for {name}")

    def stage(self, tree: TaskTree) -> DiffStats:
        """Stage everything in the worktree and describe it relative to where the task
        started (so a commit the agent made itself is counted too)."""
        git(tree.path, "add", "-A")
        d = DiffStats()
        for ln in git(tree.path, "diff", "--cached", "--name-status", "--no-renames", tree.start).splitlines():
            status, _, path = ln.partition("\t")
            if path:
                d.files.append(path)
                if status.startswith("D"):
                    d.deleted_files.append(path)
        for ln in git(tree.path, "diff", "--cached", "--numstat", "--no-renames", tree.start).splitlines():
            a, _, rest = ln.partition("\t")
            b = rest.partition("\t")[0]
            d.added += int(a) if a.isdigit() else 0
            d.deleted += int(b) if b.isdigit() else 0
        if d.files:
            patch = git(tree.path, "diff", "--cached", "--no-color", "--unified=0", tree.start)[:MAX_SCAN_BYTES]
            d.added_text = "\n".join(ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        return d

    def patch(self, tree: TaskTree) -> str:
        return git(tree.path, "diff", "--cached", "--no-color", tree.start, check=False)

    def commit(self, tree: TaskTree, message: str) -> str | None:
        """Commit the staged change on the task branch. Your commit hooks run; they are
        not bypassed. Uses your git identity; if the repository has none at all, the commit
        is authored as "RATION Autopilot". Returns the commit id, or None if refused."""
        ident: list[str] = []
        if not git(tree.path, "config", "user.email", check=False).strip():
            ident = ["-c", f"user.name={FALLBACK_IDENTITY[0]}", "-c", f"user.email={FALLBACK_IDENTITY[1]}"]
        try:
            if git(tree.path, "diff", "--cached", "--name-only").strip():
                git(tree.path, *ident, "commit", "-m", message)
            return git(tree.path, "rev-parse", "HEAD").strip()
        except GitError:
            return None

    def release(self, tree: TaskTree) -> None:
        """Remove the worktree and keep its branch (an accepted change)."""
        self._remove_tree(tree.path)

    def discard(self, tree: TaskTree) -> None:
        """Remove the worktree and its branch (a rejected change)."""
        self._remove_tree(tree.path)
        if tree.branch.startswith(self.prefix):
            git(self.root, "branch", "-D", tree.branch, check=False)

    # ---- the accumulated result ----------------------------------------------------------
    @property
    def accepted_branch(self) -> str:
        return f"{self.prefix}accepted"

    def mark_accepted(self, sha: str) -> None:
        """Point the run's `accepted` branch at the newest accepted commit."""
        git(self.root, "branch", "-f", self.accepted_branch, sha)

    def candidate_tree(self, sha: str) -> Path:
        """A clean, detached checkout of `sha`, reused across the run: where the accumulated
        result is re-scanned and verified as a whole."""
        if self._candidate_tree is None or not self._candidate_tree.exists():
            path = self.home / "_candidate"
            path.parent.mkdir(parents=True, exist_ok=True)
            git(self.root, "worktree", "add", "--detach", str(path), sha)
            self._candidate_tree = path
        else:
            git(self._candidate_tree, "checkout", "--quiet", "--detach", sha)
            git(self._candidate_tree, "clean", "-fdq")  # our own scratch checkout, never yours
        return self._candidate_tree

    def close(self) -> None:
        if self._candidate_tree is not None:
            with contextlib.suppress(GitError):
                self._remove_tree(self._candidate_tree)
            self._candidate_tree = None

    def _remove_tree(self, path: Path) -> None:
        root = worktrees_root().resolve()
        try:
            inside = path.resolve().is_relative_to(root) and path.resolve() != root
        except OSError:
            inside = False
        if not inside:
            raise GitError(f"refusing to remove {path}: not an autopilot worktree")
        git(self.root, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        git(self.root, "worktree", "prune", check=False)
        for parent in (path.parent, path.parent.parent):
            try:
                parent.rmdir()  # only succeeds when empty
            except OSError:
                break


def cleanup_run(root: Path, run_id: str, keep_branches: set[str], keep_trees: set[str] | None = None) -> list[str]:
    """After a run that was interrupted: remove its leftover worktrees and the branches of
    tasks that never produced an accepted change. Worktrees that hold an accepted but
    uncommitted change are kept. Returns what was removed."""
    removed: list[str] = []
    keep = {os.path.normcase(os.path.abspath(p)) for p in keep_trees or ()}
    home = worktrees_root() / repo_key(root) / run_id
    if home.is_dir():
        for child in home.iterdir():
            if os.path.normcase(os.path.abspath(child)) in keep:
                continue
            git(root, "worktree", "remove", "--force", str(child), check=False)
            shutil.rmtree(child, ignore_errors=True)
            removed.append(f"worktree {child.name}")
        with contextlib.suppress(OSError):
            home.rmdir()
    git(root, "worktree", "prune", check=False)
    prefix = f"{BRANCH_PREFIX}{run_id}/"
    for b in git(root, "branch", "--list", f"{prefix}*", "--format=%(refname:short)", check=False).split():
        if b.startswith(prefix) and b not in keep_branches:
            git(root, "branch", "-D", b, check=False)
            removed.append(f"branch {b}")
    return removed
