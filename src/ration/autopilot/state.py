"""Run state on disk: the run record, the single-run lock, stop requests, history.

Everything lives in `<project>/.ration/autopilot/`:

    lock.json                 one autopilot run per project at a time
    history.json              outcomes of past tasks (cooldowns, calibration, cost estimates)
    runs/<run-id>/state.json  the run record, rewritten atomically after every step
    runs/<run-id>/report.md   the report
    runs/<run-id>/research/   research output
    runs/<run-id>/rejected/   the diff and reason for every discarded change
    runs/<run-id>/stop        present when `ration autopilot stop` was asked

A run that dies (power loss, closed terminal) leaves a state file whose
process is gone. It is reported as `interrupted`, its finished tasks keep their
results and branches, and its leftovers are cleaned up before the next run.
There is no `resume`: discovery is deterministic, so a new run finds the
remaining work again from the repository as it now is, which is safer than
replaying a plan made for an older state.
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import time
from pathlib import Path
from typing import Any

from .goal import state_dir

HISTORY_LIMIT = 400
LEARN_DAYS = 60  # outcomes older than this no longer calibrate confidence or cost
STALE_LOCK_HOURS = 6  # a lock whose run has not written state for this long is abandoned
REVIEW_DAYS = 14  # how long an accepted, unmerged change keeps its task from being redone


def autopilot_dir(root: Path, create: bool = False) -> Path:
    d = state_dir(root, create=create) / "autopilot"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(root: Path, run_id: str) -> Path:
    return autopilot_dir(root) / "runs" / run_id


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    """None when the file is missing or damaged; callers treat both as 'nothing there'."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def new_run_id(now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + os.urandom(2).hex()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Not os.kill: on Windows os.kill(pid, 0) terminates the process.
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---- lock ------------------------------------------------------------------------------------
class AlreadyRunning(RuntimeError):
    def __init__(self, info: dict[str, Any]):
        super().__init__(f"autopilot run {info.get('run_id')} is already active in this project (pid {info.get('pid')})")
        self.info = info


def lock_path(root: Path) -> Path:
    return autopilot_dir(root) / "lock.json"


def lock_holder(root: Path) -> dict[str, Any] | None:
    """The live run holding the lock, if any. A lock left by a dead process is not a holder."""
    info = read_json(lock_path(root))
    if not isinstance(info, dict):
        return None
    same_host = info.get("host") == socket.gethostname()
    if same_host and not pid_alive(int(info.get("pid") or 0)):
        return None
    # A live pid is not proof: the pid may have been reused, or the lock may come from
    # another machine sharing this folder. A run rewrites its state after every step, so
    # one that has been silent for hours is gone, and nobody stays locked out.
    run = read_json(run_dir(root, str(info.get("run_id"))) / "state.json")
    beat = float((run.get("heartbeat_at") if isinstance(run, dict) else None) or info.get("started_at") or 0)
    if beat and time.time() - beat > STALE_LOCK_HOURS * 3600:
        return None
    return info


def acquire(root: Path, run_id: str) -> None:
    autopilot_dir(root, create=True)
    p = lock_path(root)
    info = {"run_id": run_id, "pid": os.getpid(), "host": socket.gethostname(), "started_at": time.time()}
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = lock_holder(root)
            if holder is not None:
                raise AlreadyRunning(holder) from None
            p.unlink(missing_ok=True)  # stale: its process is gone
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f)
        return
    raise AlreadyRunning(lock_holder(root) or {})


def release(root: Path, run_id: str) -> None:
    info = read_json(lock_path(root))
    if isinstance(info, dict) and info.get("run_id") == run_id:
        lock_path(root).unlink(missing_ok=True)


# ---- runs ------------------------------------------------------------------------------------
def save_run(root: Path, state: dict[str, Any]) -> None:
    state["heartbeat_at"] = time.time()
    write_json(run_dir(root, state["run_id"]) / "state.json", state)


def load_run(root: Path, run_id: str) -> dict[str, Any] | None:
    s = read_json(run_dir(root, run_id) / "state.json")
    return s if isinstance(s, dict) and s.get("run_id") else None


def list_runs(root: Path) -> list[str]:
    d = autopilot_dir(root) / "runs"
    return sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True) if d.is_dir() else []


def find_run(root: Path, run_id: str | None = None) -> dict[str, Any] | None:
    """The newest readable run, or the one whose id starts with `run_id`."""
    for rid in list_runs(root):
        if run_id and not rid.startswith(run_id):
            continue
        if s := load_run(root, rid):
            return s
    return None


def effective_status(root: Path, state: dict[str, Any]) -> str:
    """`running` only if its process still holds the lock."""
    if state.get("status") != "running":
        return str(state.get("status") or "unknown")
    holder = lock_holder(root)
    return "running" if holder and holder.get("run_id") == state.get("run_id") else "interrupted"


def request_stop(root: Path, run_id: str) -> Path:
    p = run_dir(root, run_id) / "stop"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()), encoding="utf-8")
    return p


def stop_requested(root: Path, run_id: str) -> bool:
    return (run_dir(root, run_id) / "stop").exists()


# ---- history: what happened before, so the next plan is better ------------------------------
class History:
    """Past task outcomes in this project.

    - cooldown: a task that was rejected is not retried for `cooldown_days`; one that
      was accepted is not redone while its branch awaits review.
    - success_rate: how tasks of a category went here, used to calibrate confidence.
    - cost: what tasks of a category actually cost here, used instead of the assumption.
    """

    def __init__(self, root: Path, cooldown_days: float = 7, now: float | None = None):
        self.path = autopilot_dir(root) / "history.json"
        self.cooldown_days = cooldown_days
        self.now = time.time() if now is None else now
        data = read_json(self.path)
        self.entries: list[dict[str, Any]] = [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append({**entry, "at": entry.get("at") or time.time()})
        self.entries = self.entries[-HISTORY_LIMIT:]
        write_json(self.path, self.entries)

    def cooldown(self, task_id: str) -> str | None:
        last = next((e for e in reversed(self.entries) if e.get("id") == task_id), None)
        if last is None:
            return None
        days = (self.now - float(last.get("at") or 0)) / 86400
        when = "today" if days < 1 else f"{days:.0f}d ago"
        if last.get("accepted"):
            if days < REVIEW_DAYS:
                return f"already done {when} on branch {last.get('branch') or '?'}: awaiting your review"
            return None
        if last.get("outcome") == "NO_CHANGE" and "already" in str(last.get("detail") or ""):
            return None
        if days < self.cooldown_days:
            return f"tried {when} and not accepted ({str(last.get('outcome') or '').lower()}): cooling down for {self.cooldown_days:g} days"
        return None

    def _recent(self) -> list[dict[str, Any]]:
        """What still counts for learning: old outcomes fade out instead of branding a
        category for ever."""
        cutoff = self.now - LEARN_DAYS * 86400
        return [e for e in self.entries if isinstance(e.get("at"), int | float) and e["at"] >= cutoff]

    def success_rate(self, category: str, minimum: int = 3) -> tuple[int, int] | None:
        rows = [e for e in self._recent() if e.get("category") == category and e.get("outcome") not in ("BLOCKED", None)]
        if len(rows) < minimum:
            return None
        return sum(1 for e in rows if e.get("accepted")), len(rows)

    def cost(self, category: str, minimum: int = 3) -> tuple[float, int] | None:
        costs = [float(e["cost_percent"]) for e in self._recent() if e.get("category") == category and isinstance(e.get("cost_percent"), int | float)]
        if len(costs) < minimum:
            return None
        return round(max(0.5, statistics.median(costs)), 1), len(costs)
