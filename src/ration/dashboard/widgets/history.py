"""History tab: past `ration run`/autopilot task outcomes, mirroring
`cmd_history`'s table but scrollable and auto-refreshing."""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import DataTable, Static

from .. import data

EMPTY_HINT = "No tasks run through RATION yet. Try `ration run \"<task>\" --check \"<cmd>\"`."


def fmt_secs(s: float | None) -> str:
    return f"{s:.0f}s" if s is not None else "-"


def fmt_usd(v: float | None) -> str:
    return f"${v:.3f}" if v is not None else "-"


class HistoryView(Widget):
    def compose(self):
        yield Static(EMPTY_HINT, id="history-empty")
        table = DataTable(zebra_stripes=True)
        table.add_columns("Task", "Type", "Status", "Attempts", "Checks", "Time", "Cost")
        yield table

    def on_mount(self) -> None:
        self.query_one(DataTable).display = False

    def update_rows(self, rows: list[data.HistoryRow]) -> None:
        table = self.query_one(DataTable)
        self.query_one("#history-empty", Static).display = not rows
        table.display = bool(rows)
        table.clear()
        for r in rows:
            table.add_row(
                r.description[:60],
                r.task_type,
                r.status,
                str(r.attempts) if r.attempts is not None else "-",
                r.checks,
                fmt_secs(r.wall_time_s),
                fmt_usd(r.cost_usd),
                key=r.task_id,
            )
