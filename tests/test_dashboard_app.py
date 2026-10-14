"""Smoke tests for the Textual dashboard app. Skipped entirely (not failed) in
an environment without the optional `textual` dependency installed. These
exist to catch wiring bugs data.py's own unit tests can't see: a DataTable
column/row mismatch, a custom message not reaching its handler, a button's
disabled-state not following the snapshot, a CSS_PATH typo that crashes on
mount. Deep interaction testing (scrolling, exact key bindings) stays manual.
"""

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, DataTable, TabbedContent  # noqa: E402

from ration.autopilot import state as autopilot_state  # noqa: E402
from ration.dashboard.app import RationDashboardApp  # noqa: E402
from ration.dashboard.screens.session_detail import SessionDetailScreen  # noqa: E402
from ration.dashboard.widgets.autopilot import AutopilotView  # noqa: E402
from ration.models import AgentEvent, EventType  # noqa: E402


def _run(coro):
    asyncio.run(coro)


def test_app_mounts_and_sessions_tab_shows_a_live_session(store, cfg, tmp_path):
    store.add_event(AgentEvent("claude", "sess-1", EventType.SESSION_STARTED, {}, timestamp=time.time() - 30))

    async def body():
        app = RationDashboardApp(interval=9999, cwd=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("TabPane#sessions DataTable", DataTable)
            assert table.row_count == 1

    _run(body())


def test_switching_to_budgets_tab_renders_its_view(store, cfg, tmp_path):
    from conftest import add_usage

    add_usage(store, "five_hour", 42, reset_in_h=2)

    async def body():
        app = RationDashboardApp(interval=9999, cwd=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "budgets"
            await pilot.pause()
            table = app.query_one("TabPane#budgets DataTable", DataTable)
            assert table.row_count == 1
            row = table.get_row_at(0)
            assert "42" in str(row)

    _run(body())


def test_selecting_a_session_row_pushes_the_detail_screen(store, cfg, tmp_path):
    store.add_event(AgentEvent("claude", "sess-1", EventType.SESSION_STARTED, {}, timestamp=time.time() - 30))

    async def body():
        app = RationDashboardApp(interval=9999, cwd=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("TabPane#sessions DataTable", DataTable)
            table.cursor_coordinate = (0, 0)
            table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], SessionDetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen_stack[-1], SessionDetailScreen)

    _run(body())


def test_stop_button_disabled_state_follows_whether_a_run_is_live(store, cfg, tmp_path):
    async def body():
        app = RationDashboardApp(interval=9999, cwd=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "autopilot"
            await pilot.pause()
            btn = app.query_one(AutopilotView).query_one("#stop-btn", Button)
            assert btn.disabled is True  # no run at all yet

    _run(body())


def test_stop_button_is_enabled_for_a_live_run_and_confirming_requests_a_stop(store, cfg, tmp_path):
    root = Path(tmp_path)
    run_id = "20260101-000000-test"
    autopilot_state.acquire(root, run_id)
    autopilot_state.save_run(
        root, {"run_id": run_id, "status": "running", "mode": "safe", "project": "demo", "started_at": time.time(), "queue": ["t1"], "results": []}
    )

    async def body():
        app = RationDashboardApp(interval=9999, cwd=str(root))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "autopilot"
            await pilot.pause()
            btn = app.query_one(AutopilotView).query_one("#stop-btn", Button)
            assert btn.disabled is False
            btn.press()
            await pilot.pause()
            confirm_btn = app.screen_stack[-1].query_one("#confirm", Button)
            confirm_btn.press()
            await pilot.pause()
            await pilot.pause()

    try:
        _run(body())
        assert autopilot_state.stop_requested(root, run_id)
    finally:
        autopilot_state.release(root, run_id)
