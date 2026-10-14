"""Pure data layer for `ration dashboard`. No Textual import here on purpose:
every function returns a plain dataclass, so this module is fully unit
testable without a terminal, and the TUI widgets are just a thin render of it.

Each function re-derives from the same sources the equivalent plain-CLI
command already uses (`cli.session_rows`/`watch_frame`, `cmd_status`,
`cmd_history`, `autopilot.commands.cmd_status`) rather than inventing a new
notion of "session" or "budget" or "run" state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..autopilot import report as autopilot_report
from ..autopilot import state as autopilot_state
from ..budget import ProviderBudget, provider_budgets
from ..models import AgentEvent, BudgetMode
from ..progress import compute as progress_compute
from ..store import Store
from ..stuck import detect, recommend

INACTIVE = "ration"  # pre-run baseline checks are not an agent session; see cli.session_rows


@dataclass
class SessionRow:
    provider: str
    session_id: str
    n_events: int
    last_at: float
    progress_score: float
    progress_arrow: str
    stuck_score: float
    stuck_band: str  # "healthy" | "watch" | "intervene" | "likely_stuck"
    stuck_reasons: list[str]
    subagents: int
    recommendation: str | None  # None when healthy


def _session_row(store: Store, sid_row: Any, cfg: dict[str, Any], budgets: dict[str, ProviderBudget], now: float) -> SessionRow:
    events = store.events(session_id=sid_row["session_id"], limit=400)
    st = detect(events, now=now)
    pr = progress_compute(events, now=now)
    b = budgets.get(sid_row["provider"])
    subs = sum(1 for e in events if e.event_type == "SUBAGENT_STARTED")
    rec = recommend(st, budget_mode=b.mode if b else BudgetMode.NORMAL, parallel_subagents=subs)
    return SessionRow(
        provider=sid_row["provider"],
        session_id=sid_row["session_id"],
        n_events=sid_row["n"],
        last_at=sid_row["last_at"],
        progress_score=pr.score,
        progress_arrow=pr.arrow,
        stuck_score=st.score,
        stuck_band=st.band,
        stuck_reasons=st.reasons,
        subagents=subs,
        recommendation=None if st.band == "healthy" else f"{rec.intervention}: {rec.explanation}",
    )


def active_sessions(store: Store, cfg: dict[str, Any], since_minutes: float = 30.0, now: float | None = None) -> list[SessionRow]:
    now = time.time() if now is None else now
    budgets = provider_budgets(store, cfg, now=now)
    rows = store.active_sessions(now - since_minutes * 60)
    return [_session_row(store, r, cfg, budgets, now) for r in rows if r["provider"] != INACTIVE]


@dataclass
class SessionDetail:
    row: SessionRow
    progress_reasons: list[str]
    events: list[AgentEvent]  # oldest first, capped at `limit`


def session_detail(store: Store, session_id: str, *, limit: int = 200, now: float | None = None) -> SessionDetail | None:
    now = time.time() if now is None else now
    events = store.events(session_id=session_id, limit=limit)
    if not events:
        return None
    st = detect(events, now=now)
    pr = progress_compute(events, now=now)
    sessions = store.active_sessions(0)
    meta = next((s for s in sessions if s["session_id"] == session_id), None)
    row = SessionRow(
        provider=meta["provider"] if meta else (events[0].provider or "?"),
        session_id=session_id,
        n_events=meta["n"] if meta else len(events),
        last_at=meta["last_at"] if meta else events[-1].timestamp,
        progress_score=pr.score,
        progress_arrow=pr.arrow,
        stuck_score=st.score,
        stuck_band=st.band,
        stuck_reasons=st.reasons,
        subagents=sum(1 for e in events if e.event_type == "SUBAGENT_STARTED"),
        recommendation=None,
    )
    return SessionDetail(row=row, progress_reasons=pr.reasons, events=events)


@dataclass
class WindowRow:
    window: str
    used_percent: float
    remaining_percent: float
    hours_to_reset: float | None
    mode: str
    burn_pressure: float | None


@dataclass
class BudgetSnapshot:
    provider: str
    windows: list[WindowRow]
    binding_window: str | None
    sparkline: list[float]  # used_percent samples for the binding window, oldest -> newest


def budget_snapshot(store: Store, cfg: dict[str, Any], *, history_hours: float = 24.0, now: float | None = None) -> list[BudgetSnapshot]:
    now = time.time() if now is None else now
    budgets = provider_budgets(store, cfg, now=now)
    out = []
    for name, b in budgets.items():
        windows = [
            WindowRow(w.window, w.used_percent, w.remaining_percent, w.hours_to_reset, str(w.mode), w.burn_pressure)
            for w in b.windows
        ]
        binding = b.binding
        spark: list[float] = []
        if binding:
            spark = [s.used_percent for s in store.usage_history(name, binding.window, now - history_hours * 3600)]
        out.append(BudgetSnapshot(provider=name, windows=windows, binding_window=binding.window if binding else None, sparkline=spark))
    return out


@dataclass
class HistoryRow:
    task_id: str
    description: str
    task_type: str
    status: str
    completed: bool | None
    attempts: int | None
    wall_time_s: float | None
    cost_usd: float | None
    checks: str  # "3/4" or "-"


def history_rows(store: Store, limit: int = 20) -> list[HistoryRow]:
    out = []
    for r in store.history(limit):
        checks = f"{r['checks_passed']}/{r['checks_total']}" if r["checks_total"] else "-"
        out.append(
            HistoryRow(
                task_id=r["id"],
                description=r["description"],
                task_type=r["task_type"] or "",
                status=r["status"],
                completed=bool(r["completed"]) if r["completed"] is not None else None,
                attempts=r["attempts"],
                wall_time_s=r["wall_time_s"],
                cost_usd=r["cost_usd"],
                checks=checks,
            )
        )
    return out


@dataclass
class AutopilotSnapshot:
    found: bool
    run_id: str | None = None
    status: str | None = None
    mode: str | None = None
    project: str | None = None
    started_at: float | None = None
    heartbeat_at: float | None = None
    stop_reason: str | None = None
    queue_len: int = 0
    done_len: int = 0
    accepted_len: int = 0
    rejected_len: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    budget_lines: list[str] = field(default_factory=list)
    can_stop: bool = False


def autopilot_snapshot(root: Path, run_id: str | None = None) -> AutopilotSnapshot:
    s = autopilot_state.find_run(root, run_id)
    if s is None:
        return AutopilotSnapshot(found=False)
    status = autopilot_state.effective_status(root, s)
    results = [r for r in s.get("results", []) if isinstance(r, dict)]
    accepted = sum(1 for r in results if r.get("accepted"))
    budget = s.get("budget_end") or s.get("budget_start")
    return AutopilotSnapshot(
        found=True,
        run_id=s.get("run_id"),
        status=status,
        mode=s.get("mode"),
        project=s.get("project"),
        started_at=s.get("started_at"),
        heartbeat_at=s.get("heartbeat_at"),
        stop_reason=s.get("stop_reason"),
        queue_len=len(s.get("queue", [])),
        done_len=len(results),
        accepted_len=accepted,
        rejected_len=len(results) - accepted,
        results=results,
        budget_lines=autopilot_report.budget_lines(budget) if budget else [],
        can_stop=(status == "running"),
    )
