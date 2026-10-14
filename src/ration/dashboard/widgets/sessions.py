"""Sessions tab: a scrollable table of active sessions. Selecting a row posts
a `SessionSelected` message rather than navigating directly, so the widget
stays testable in isolation and the App owns all navigation decisions."""

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Static

from .. import data

EMPTY_HINT = "No agent activity yet. Is the Claude Code hook installed? (`ration hooks status`)"

BAND_STYLE = {"healthy": "green", "watch": "yellow", "intervene": "dark_orange", "likely_stuck": "red"}


def ago(ts: float) -> str:
    import time

    s = max(0, time.time() - ts)
    if s < 60:
        return f"{s:.0f}s ago"
    if s < 3600:
        return f"{s / 60:.0f}m ago"
    return f"{s / 3600:.1f}h ago"


class SessionSelected(Message):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__()


class SessionsView(Widget):
    def compose(self):
        yield Static(EMPTY_HINT, id="sessions-empty")
        table = DataTable(cursor_type="row", zebra_stripes=True)
        table.add_columns("Provider", "Session", "Events", "Last active", "Progress", "Subagents", "Stuck")
        yield table

    def on_mount(self) -> None:
        self.query_one(DataTable).display = False

    def update_rows(self, rows: list[data.SessionRow]) -> None:
        table = self.query_one(DataTable)
        self.query_one("#sessions-empty", Static).display = not rows
        table.display = bool(rows)
        existing = set(table.rows.keys())
        wanted = {r.session_id for r in rows}
        for key in existing - wanted:
            table.remove_row(key)
        for r in rows:
            cells = (
                r.provider,
                r.session_id[:8],
                str(r.n_events),
                ago(r.last_at),
                f"{r.progress_score:.2f} {r.progress_arrow}",
                str(r.subagents) if r.subagents else "-",
                Text(f"{r.stuck_score:.2f} {r.stuck_band}", style=BAND_STYLE.get(r.stuck_band, "")),
            )
            if r.session_id in existing:
                for col, value in enumerate(cells):
                    table.update_cell_at((table.get_row_index(r.session_id), col), value)
            else:
                table.add_row(*cells, key=r.session_id)
        if not rows:
            table.clear()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.post_message(SessionSelected(event.row_key.value))
