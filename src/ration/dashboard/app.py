"""`ration dashboard`: a live Textual TUI over the same local SQLite store
`ration watch`/`ration status`/`ration history` already read. Adds real
navigation (tabs + a session drill-down screen) that a single auto-refreshing
`rich.Live` panel can't do.

Optional dependency: this module is only ever imported lazily, from
`cli.cmd_dashboard`, inside a `try/except ImportError` — `textual` is not a
base-install requirement (see `[project.optional-dependencies].dashboard`)."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .. import config
from ..autopilot import goal as autopilot_goal
from ..store import Store
from . import data
from .screens.session_detail import SessionDetailScreen
from .widgets.autopilot import AutopilotView
from .widgets.budgets import BudgetsView
from .widgets.history import HistoryView
from .widgets.sessions import SessionSelected, SessionsView


class RationDashboardApp(App):
    CSS_PATH = "dashboard.tcss"
    TITLE = "RATION"
    SUB_TITLE = "Spend intelligence where it matters."
    BINDINGS = [("r", "refresh_now", "Refresh")]

    def __init__(self, *, interval: float = 2.0, cwd: str = ".", since_minutes: float = 30.0) -> None:
        super().__init__()
        self.interval = interval
        self.since_minutes = since_minutes
        self.cfg = config.load()
        self.autopilot_root: Path = autopilot_goal.project_root(cwd)
        self.store: Store | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="sessions"):
            with TabPane("Sessions", id="sessions"):
                yield SessionsView()
            with TabPane("Budgets", id="budgets"):
                yield BudgetsView()
            with TabPane("History", id="history"):
                yield HistoryView()
            with TabPane("Autopilot", id="autopilot"):
                yield AutopilotView(root=self.autopilot_root)
        yield Footer()

    def on_mount(self) -> None:
        self.store = Store()
        self.refresh_all()
        self.set_interval(self.interval, self.refresh_all)

    def on_unmount(self) -> None:
        if self.store is not None:
            self.store.close()

    def action_refresh_now(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        if self.store is None:
            return
        self.query_one(SessionsView).update_rows(data.active_sessions(self.store, self.cfg, self.since_minutes))
        self.query_one(BudgetsView).update_snapshot(data.budget_snapshot(self.store, self.cfg))
        self.query_one(HistoryView).update_rows(data.history_rows(self.store))
        self.query_one(AutopilotView).update_snapshot(data.autopilot_snapshot(self.autopilot_root))

    def on_session_selected(self, message: SessionSelected) -> None:
        if self.store is not None:
            self.push_screen(SessionDetailScreen(session_id=message.session_id, store=self.store))
