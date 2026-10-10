"""Render README screenshots (SVG) from RATION's real CLI code.

- status.svg / watch.svg: the real `ration status` / `ration watch --once` renderers,
  fed with demo data in a throwaway RATION_HOME (no personal usage is published).
- run.svg: the verbatim output of a real `ration run` (2026-10-10, see README).

Usage: python scripts/gen_readme_assets.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"
os.environ["RATION_HOME"] = tempfile.mkdtemp(prefix="ration-assets-")

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from ration import cli  # noqa: E402
from ration.models import (  # noqa: E402
    AgentEvent,
    Confidence,
    EventType,
    ResourceType,
    Source,
    UsageSnapshot,
)
from ration.store import Store  # noqa: E402

H = 3600.0
WIDTH = 112


def recorder() -> Console:
    return Console(record=True, width=WIDTH, force_terminal=True, color_system="truecolor", highlight=False)


def demo_data() -> None:
    now = time.time()
    with Store() as s:
        five_reset, week_reset = now + 3.4 * H, now + 6 * 24 * H

        def snap(window, used, reset, minutes, ago_h):
            s.add_usage(UsageSnapshot("claude", window, ResourceType.USAGE_PERCENT, used, reset, minutes, Source.PROVIDER_CLI, Confidence.HIGH, now - ago_h * H))

        snap("five_hour", 41, five_reset, 300, 0.4)
        snap("five_hour", 58, five_reset, 300, 0.0)
        snap("seven_day", 12, week_reset, 10080, 6.0)
        snap("seven_day", 14, week_reset, 10080, 0.0)

        def ev(sid, et, ago_s, **meta):
            s.add_event(AgentEvent("claude", sid, et, meta, timestamp=now - ago_s))

        # a healthy session
        for i, f in enumerate(["src/api/routes.py", "src/api/auth.py", "tests/test_auth.py"]):
            ev("7f3a91c2", EventType.FILE_READ, 600 - i * 20, path=f)
            ev("7f3a91c2", EventType.TOOL_COMPLETED, 599 - i * 20, tool="Read", sig=f"r{i}")
        ev("7f3a91c2", EventType.FILE_CHANGED, 400, path="src/api/auth.py")
        ev("7f3a91c2", EventType.TEST_RESULT, 380, passed=9, failed=3, ok=False, sig="a")
        ev("7f3a91c2", EventType.FILE_CHANGED, 200, path="src/api/auth.py")
        ev("7f3a91c2", EventType.TEST_RESULT, 120, passed=12, failed=0, ok=True, sig="g")
        # a session looping on the same failing test
        for i in range(6):
            t = 1500 - i * 200
            ev("c08e44d1", EventType.FILE_CHANGED, t, path="src/checkout/lock.py")
            ev("c08e44d1", EventType.TOOL_FAILED, t - 30, tool="Bash", sig="pytest-checkout")
            ev("c08e44d1", EventType.TEST_RESULT, t - 31, passed=14, failed=1, ok=False, sig="deadlock")


def save(console: Console, name: str, title: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUT / name), title=title)
    print("wrote", OUT / name)


def main() -> int:
    demo_data()
    for name, argv, title in (
        ("status.svg", ["status"], "ration status  (demo data)"),
        ("watch.svg", ["watch", "--once", "--minutes", "60"], "ration watch  (demo data)"),
    ):
        cli.console = recorder()
        cli.main(argv)
        save(cli.console, name, title)

    c = recorder()
    real = [
        ("$ ration run \"fix the failing median and percentile tests in stats.py\" \\\n    --check \"python -m pytest -q\" --permission-mode acceptEdits", "bold"),
        ("", ""),
        ("[bold]Task[/]  fix the failing median and percentile tests in stats.py", None),
        ("type debugging   difficulty 0.55   value 0.50   risk 0.20", ""),
        ("", ""),
        ("[bold]Baseline[/]  checks before any spend: 4 passed, 2 failed", None),
        ("", ""),
        ("[bold]Decision[/]  Claude Code · cheap model · effort low · max turns 21 · retries 1 · subagents 1 · CHEAP_FIRST", None),
        ('  reasons: ["routine_task_cheap_first", "all_budgets_tight_fewer_retries", "route_claude",', "dim"),
        ('            "claude_burn_pressure_high", "claude_budget_conservative", "reset_within_3_hours"]', "dim"),
        ("  attempt → [green]succeeded[/]   checks 1/1   progress 0.76   stuck 0.00   $0.010", None),
        ("", ""),
        ("[green]completed[/] after 1 attempt(s) in 40s, $0.010 API-equivalent", None),
    ]
    for line, style in real:
        c.print(line if style is None else Text(line, style=style or ""))
    save(c, "run.svg", "ration run  (real run, 2026-10-10)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
