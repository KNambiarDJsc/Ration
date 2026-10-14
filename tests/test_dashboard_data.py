import time

from ration.autopilot import state
from ration.dashboard import data
from ration.models import (
    AgentEvent,
    Confidence,
    EventType,
    ResourceType,
    Source,
    TaskType,
    UsageSnapshot,
)

NOW = time.time()


def ev(sid, et, meta=None, ts=None, provider="claude"):
    return AgentEvent(provider, sid, et, meta or {}, timestamp=NOW - 60 if ts is None else ts)


# ---- active_sessions / session_detail -------------------------------------------------------


def test_active_sessions_lists_a_healthy_session(store, cfg):
    store.add_event(ev("s1", EventType.SESSION_STARTED, ts=NOW - 60))
    store.add_event(ev("s1", EventType.PROMPT_SUBMITTED, ts=NOW - 50))
    rows = data.active_sessions(store, cfg, since_minutes=30, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r.session_id == "s1" and r.provider == "claude" and r.n_events == 2
    assert r.stuck_band == "healthy" and r.recommendation is None


def test_active_sessions_excludes_the_ration_provider_baseline(store, cfg):
    store.add_event(ev("baseline", EventType.TOOL_COMPLETED, provider="ration"))
    assert data.active_sessions(store, cfg, since_minutes=30, now=NOW) == []


def test_active_sessions_flags_a_stuck_session(store, cfg):
    for i in range(6):
        store.add_event(ev("s1", EventType.TOOL_FAILED, {"sig": "x"}, ts=NOW - 60 + i))
    rows = data.active_sessions(store, cfg, since_minutes=30, now=NOW)
    assert rows[0].stuck_band in {"watch", "intervene", "likely_stuck"}
    assert rows[0].recommendation is not None and rows[0].stuck_reasons


def test_active_sessions_respects_the_window(store, cfg):
    store.add_event(ev("old", EventType.SESSION_STARTED, ts=NOW - 3600))
    assert data.active_sessions(store, cfg, since_minutes=30, now=NOW) == []


def test_session_detail_returns_full_reasons_and_event_tail(store, cfg):
    store.add_event(ev("s1", EventType.SESSION_STARTED, ts=NOW - 60))
    store.add_event(ev("s1", EventType.FILE_CHANGED, {"path": "a.py"}, ts=NOW - 30))
    d = data.session_detail(store, "s1", now=NOW)
    assert d is not None
    assert d.row.session_id == "s1" and len(d.events) == 2
    assert d.events[0].timestamp < d.events[-1].timestamp  # oldest first


def test_session_detail_is_none_for_an_unknown_session(store, cfg):
    assert data.session_detail(store, "nope", now=NOW) is None


# ---- budget_snapshot --------------------------------------------------------------------------


def test_budget_snapshot_reports_windows_and_sparkline(store, cfg):
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 40, NOW + 3600, 300, Source.PROVIDER_CLI, Confidence.HIGH, NOW - 600))
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 55, NOW + 3600, 300, Source.PROVIDER_CLI, Confidence.HIGH, NOW))
    snaps = data.budget_snapshot(store, cfg, now=NOW)
    assert len(snaps) == 1 and snaps[0].provider == "claude"
    assert snaps[0].windows[0].window == "five_hour" and snaps[0].windows[0].used_percent == 55
    assert snaps[0].sparkline == [40, 55]


def test_budget_snapshot_is_empty_with_no_usage_data(store, cfg):
    assert data.budget_snapshot(store, cfg, now=NOW) == []


# ---- history_rows ------------------------------------------------------------------------------


def test_history_rows_reflects_a_recorded_outcome(store, cfg):
    from ration.models import AgentTask

    t = AgentTask("fix the flaky test", task_type=TaskType.TESTS)
    store.add_task(t)
    store.record_outcome(t.id, True, 4, 4, 1, 12.0, 0.03)
    rows = data.history_rows(store)
    assert len(rows) == 1
    r = rows[0]
    assert r.task_id == t.id and r.completed is True and r.checks == "4/4" and r.cost_usd == 0.03


def test_history_rows_shows_dash_when_no_checks_recorded(store, cfg):
    from ration.models import AgentTask

    t = AgentTask("investigate the slow query", task_type=TaskType.INVESTIGATION)
    store.add_task(t)
    store.record_outcome(t.id, False, None, None, 1, 5.0, None)
    assert data.history_rows(store)[0].checks == "-"


# ---- autopilot_snapshot -------------------------------------------------------------------------


def test_autopilot_snapshot_when_no_run_exists(tmp_path):
    snap = data.autopilot_snapshot(tmp_path)
    assert snap.found is False and snap.can_stop is False


def test_autopilot_snapshot_reflects_a_live_running_task(tmp_path):
    run_id = "20260101-000000-live"
    state.acquire(tmp_path, run_id)
    state.save_run(
        tmp_path,
        {
            "run_id": run_id, "status": "running", "mode": "safe", "project": "demo", "started_at": NOW,
            "queue": ["t1", "t2", "t3"],
            "results": [{"task_id": "t1", "title": "Add missing tests", "outcome": "SUCCESS", "accepted": True}],
        },
    )
    snap = data.autopilot_snapshot(tmp_path)
    assert snap.found and snap.run_id == run_id and snap.status == "running" and snap.can_stop is True
    assert snap.queue_len == 3 and snap.done_len == 1 and snap.accepted_len == 1 and snap.rejected_len == 0
    state.release(tmp_path, run_id)


def test_autopilot_snapshot_downgrades_to_interrupted_without_a_live_lock(tmp_path):
    run_id = "20260101-000000-dead"
    state.save_run(tmp_path, {"run_id": run_id, "status": "running", "queue": [], "results": []})
    snap = data.autopilot_snapshot(tmp_path)  # no matching lock.json was ever acquired
    assert snap.status == "interrupted" and snap.can_stop is False


def test_autopilot_snapshot_includes_budget_lines_when_present(tmp_path):
    run_id = "20260101-000000-b"
    state.save_run(
        tmp_path,
        {
            "run_id": run_id, "status": "done", "queue": [], "results": [],
            "budget_start": {"known": True, "short": {"used": 10.0, "remaining": 90.0, "hours_to_reset": 2.0}},
        },
    )
    snap = data.autopilot_snapshot(tmp_path)
    assert snap.budget_lines and "10%" in snap.budget_lines[0]
