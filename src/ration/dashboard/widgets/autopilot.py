"""Autopilot tab: live progress of an in-progress run, plus the one
interactive action this dashboard offers: requesting a stop. This calls the
pre-existing, already-safe `state.request_stop()` sentinel-file primitive
that the real autopilot run loop already polls for — it never starts a run,
and the dashboard introduces no new risk surface by exposing it."""

from __future__ import annotations

import time
from pathlib import Path

from textual import work
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Button, Static

from ...autopilot import report as autopilot_report
from ...autopilot import state as autopilot_state
from .. import data
from ..screens.confirm import ConfirmScreen


def _dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def render_summary(snap: data.AutopilotSnapshot) -> str:
    if not snap.found:
        return "No autopilot run found in this project. `ration autopilot --dry-run` to preview one."
    lines = [
        f"Run {snap.run_id}: [bold]{(snap.status or '?').upper()}[/]  ({snap.mode} mode, project {snap.project})",
        f"started {time.strftime('%Y-%m-%d %H:%M', time.localtime(snap.started_at))}"
        + (f", running for {_dur(time.time() - snap.started_at)}" if snap.status == "running" and snap.started_at else ""),
        f"tasks: {snap.done_len} of {snap.queue_len} done, {snap.accepted_len} accepted, {snap.rejected_len} not accepted",
    ]
    if snap.stop_reason:
        lines.append(f"stopped: {snap.stop_reason}")
    lines += snap.budget_lines
    for r in snap.results[-10:]:
        mark = autopilot_report.MARK.get(r.get("outcome"), "?")
        lines.append(f"  {mark} {str(r.get('title', ''))[:64]}  [{r.get('outcome')}]")
    return "\n".join(lines)


class AutopilotView(Widget):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self._run_id: str | None = None

    def compose(self):
        with Vertical():
            yield Static(id="summary")
            yield Button("Stop run", id="stop-btn", disabled=True, variant="error")

    def update_snapshot(self, snap: data.AutopilotSnapshot) -> None:
        self._run_id = snap.run_id
        self.query_one("#summary", Static).update(render_summary(snap))
        self.query_one("#stop-btn", Button).disabled = not snap.can_stop

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "stop-btn" and self._run_id:
            self._confirm_and_stop(self._run_id)

    @work
    async def _confirm_and_stop(self, run_id: str) -> None:
        # push_screen_wait requires a worker context (NoActiveWorker otherwise) — that's
        # what @work provides here; the plain Button.Pressed handler stays synchronous.
        if await self.app.push_screen_wait(ConfirmScreen(f"Stop autopilot run {run_id}?")):
            autopilot_state.request_stop(self.root, run_id)
            self.app.notify(f"Stop requested for {run_id}", severity="warning")
