"""Session detail: a read-only magnifier on one session's progress/stuck
reasons and its recent event tail. No actions here on purpose — this screen
only ever shows what SessionDetail already computed, never a control surface."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from .. import data


def render_detail(d: data.SessionDetail | None, session_id: str) -> str:
    if d is None:
        return f"Session {session_id} has no recorded events (it may have aged out of the active window)."
    r = d.row
    lines = [
        f"[bold]{r.provider} session {r.session_id}[/]",
        f"{r.n_events} events, last active a moment ago   subagents: {r.subagents}",
        "",
        f"[bold]Progress[/]  {r.progress_score:.2f} {r.progress_arrow}",
    ]
    lines += [f"  {x}" for x in d.progress_reasons] or ["  (no signal yet)"]
    lines += ["", f"[bold]Stuck[/]  {r.stuck_score:.2f} ({r.stuck_band})"]
    lines += [f"  ! {x}" for x in r.stuck_reasons] or ["  (nothing unusual)"]
    lines += ["", "[bold]Recent events[/]"]
    for e in d.events[-50:]:
        lines.append(f"  {e.event_type}  {e.metadata}")
    return "\n".join(lines)


class SessionDetailScreen(Screen):
    BINDINGS = [Binding("escape", "dismiss_screen", "Back")]

    def __init__(self, session_id: str, store) -> None:
        super().__init__()
        self.session_id = session_id
        self.store = store

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="detail-body"):
            yield Static(id="detail-text")
        yield Footer()

    def on_mount(self) -> None:
        d = data.session_detail(self.store, self.session_id)
        self.query_one("#detail-text", Static).update(render_detail(d, self.session_id))

    def action_dismiss_screen(self) -> None:
        self.dismiss()
