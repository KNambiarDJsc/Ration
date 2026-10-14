"""Budgets tab: per-provider usage windows plus a sparkline of recent history
for the window that's actually constraining spending (`.binding`)."""

from __future__ import annotations

from rich.text import Text
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Sparkline, Static

from .. import data

MODE_STYLE = {"CONSERVATIVE": "red", "NORMAL": "", "AGGRESSIVE": "cyan"}


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "?"
    return f"{h:.1f}h"


class BudgetsView(Widget):
    def compose(self):
        yield Static(id="budgets-empty")
        with Vertical(id="budgets-body"):
            yield DataTable(cursor_type="row", zebra_stripes=True)
            yield Sparkline([], summary_function=max)

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Provider", "Window", "Used %", "Remaining %", "Resets in", "Mode", "Burn pressure")

    def update_snapshot(self, snapshots: list[data.BudgetSnapshot]) -> None:
        empty = self.query_one("#budgets-empty", Static)
        body = self.query_one("#budgets-body", Vertical)
        if not snapshots:
            empty.update("No usage data yet. Run `ration statusline install` (Claude Code) or use Codex once.")
            empty.display, body.display = True, False
            return
        empty.display, body.display = False, True
        table = self.query_one(DataTable)
        table.clear()
        for snap in snapshots:
            for w in snap.windows:
                table.add_row(
                    snap.provider,
                    w.window,
                    f"{w.used_percent:.0f}",
                    f"{w.remaining_percent:.0f}",
                    fmt_hours(w.hours_to_reset),
                    Text(w.mode, style=MODE_STYLE.get(w.mode, "")),
                    f"{w.burn_pressure:.2f}" if w.burn_pressure is not None else "?",
                )
        spark = self.query_one(Sparkline)
        binding = next((s for s in snapshots if s.sparkline), None)
        spark.data = binding.sparkline if binding else []
