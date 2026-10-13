"""`ration goal` and `ration autopilot` (alias `ration nightshift`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from rich.console import Console
from rich.prompt import Confirm

from .. import config
from ..store import Store
from . import engine, policy, report, state
from . import goal as goal_mod
from .models import Outcome

console = Console(highlight=False)


def out(text: str = "", style: str | None = None) -> None:
    console.print(text, markup=False, style=style, soft_wrap=True)


# ---- ration goal ---------------------------------------------------------------------------
def cmd_goal(args: argparse.Namespace) -> int:
    root = goal_mod.project_root(args.cwd)
    path = goal_mod.path(root)
    if args.action == "clear":
        out("Goal cleared." if goal_mod.clear(root) else "No goal was set.")
        return 0
    if args.action == "set":
        text = " ".join(args.text).strip()
        if not (text or args.priority or args.constraint):
            out('Usage: ration goal set "<goal>" [--priority "..."]... [--constraint "..."]...', "red")
            return 2
        g = goal_mod.load(root) or goal_mod.Goal()
        if text:
            g.goal = text
        if args.priority:
            g.priorities = [p.strip() for p in args.priority if p.strip()]
        if args.constraint:
            g.constraints = [c.strip() for c in args.constraint if c.strip()]
        goal_mod.save(root, g)
        out(f"Saved to {path} (local; this folder ignores itself in git).", "green")
    elif args.action == "edit":
        try:
            goal_mod.edit(root)
        except ValueError as e:
            out(f"Not saved: {e}. The previous goal is unchanged.", "red")
            return 2
        except OSError as e:
            out(f"Could not open an editor ({e}). Set $EDITOR, or use `ration goal set`.", "red")
            return 2
    g = goal_mod.load(root)
    if args.json:
        print(json.dumps({"path": str(path), "goal": g.goal if g else "", "priorities": g.priorities if g else [], "constraints": g.constraints if g else []}, indent=2))
        return 0
    if g is None:
        if path.exists():
            try:
                goal_mod.loads(path.read_text(encoding="utf-8"))
                out(f"{path} is empty.")
            except (OSError, ValueError) as e:
                out(f"{path} cannot be read ({e}). Fix it with `ration goal edit` or replace it with `ration goal set`.", "yellow")
                return 1
        else:
            out(f"No goal set for {root.name}. Autopilot ranks work by repository signals alone.")
        out('Set one: ration goal set "Ship the MVP" --priority "finish authentication" --constraint "don\'t change the database schema"')
        return 0
    out(f"Goal for {root.name}  ({path})")
    out(f"  {g.goal or '(no goal sentence; priorities only)'}")
    for title, items in (("Priorities (most important first)", g.priorities), ("Constraints", g.constraints)):
        if items:
            out(f"\n{title}:")
            for i in items:
                out(f"  - {i}")
    return 0


# ---- ration autopilot ----------------------------------------------------------------------
def _brand(args: argparse.Namespace) -> str:
    return "NIGHTSHIFT" if getattr(args, "cmd", "") == "nightshift" else "AUTOPILOT"


def _providers(cfg: dict[str, Any], which: str, seed: int = 0) -> dict[str, Any]:
    if which == "mock":
        from ..providers.mock import MockProvider

        return {"mock": MockProvider(seed=seed)}
    from ..providers import get_providers

    provs = get_providers(cfg)
    return provs if which == "auto" else {k: v for k, v in provs.items() if k == which}


def _plan_json(plan: engine.Plan) -> dict[str, Any]:
    return {
        "project": plan.root.name,
        "mode": plan.mode.name,
        "goal": plan.goal.goal if plan.goal else "",
        "budget": plan.allowance.to_dict(),
        "checks": {"test": plan.checks.test, "lint": plan.checks.lint, "typecheck": plan.checks.typecheck},
        "tasks": [t.to_dict() for t in plan.tasks],
        "queue": [t.id for t in plan.queue],
        "estimated_cost_percent": plan.estimated_cost,
        "blocked": plan.blocked,
        "notes": plan.notes,
    }


def _status(args: argparse.Namespace, root: Any) -> int:
    s = state.find_run(root, args.run)
    holder = state.lock_holder(root)
    if s is None:
        out(f"No autopilot runs yet in {root.name}. Try `ration autopilot --dry-run`.")
        return 0
    status = state.effective_status(root, s)
    if args.json:
        print(json.dumps({**s, "status": status}, indent=2, default=str))
        return 0
    out(report.render_status(s, status))
    if holder and status == "running":
        out(f"\nStop it after the current step: ration autopilot stop   (pid {holder.get('pid')})")
    elif status == "interrupted":
        out("\nThe process ended before the run finished. Finished tasks kept their branches; leftovers are removed when the next run starts.")
    out(f"Full report: ration autopilot report --run {s['run_id']}")
    return 0


def _stop(root: Any) -> int:
    holder = state.lock_holder(root)
    if holder is None:
        out("No autopilot run is active in this project.")
        return 0
    state.request_stop(root, str(holder["run_id"]))
    out(f"Stop requested for run {holder['run_id']}. The current agent is cancelled at its next step, its unfinished change is discarded, and the report is written.")
    return 0


def _report(args: argparse.Namespace, root: Any) -> int:
    s = state.find_run(root, args.run)
    if s is None:
        out("No autopilot report found for this project." + (f" (no run id starts with '{args.run}')" if args.run else ""))
        return 1
    s = {**s, "status": state.effective_status(root, s)}
    if args.json:
        print(json.dumps(s, indent=2, default=str))
    else:
        out(report.render_report(s, brand=_brand(args)))
        runs = state.list_runs(root)
        if len(runs) > 1:
            out(f"{len(runs)} runs on record; older ones: ration autopilot report --run <id>   ({', '.join(runs[1:4])}{' …' if len(runs) > 4 else ''})", "dim")
    return 0


def cmd_autopilot(args: argparse.Namespace) -> int:
    root = goal_mod.project_root(args.cwd)
    if args.action == "status":
        return _status(args, root)
    if args.action == "stop":
        return _stop(root)
    if args.action == "report":
        return _report(args, root)

    cfg = config.load(root)
    if args.mode not in policy.MODES:
        out(f"Unknown mode '{args.mode}'. Available: {', '.join(policy.MODES)}", "red")
        return 2
    holder = state.lock_holder(root)
    if holder is not None and not args.dry_run:
        out(f"Autopilot run {holder.get('run_id')} is already active in this project (pid {holder.get('pid')}). See `ration autopilot status`, or `ration autopilot stop`.", "red")
        return 2
    providers = _providers(cfg, args.provider, args.seed)
    if not providers:
        out(f"Provider '{args.provider}' is not enabled. See `ration providers`.", "red")
        return 2
    quiet = args.json
    if not quiet:
        out("Scanning the repository" + (" and running its checks (read-only)…" if not args.no_probe else " (static scan)…"), "dim")
    with Store() as store:
        if "codex" in providers and hasattr(providers["codex"], "ingest_usage"):
            try:
                providers["codex"].ingest_usage(store)
            except Exception:
                pass
        plan = engine.make_plan(
            root, cfg, store, providers, mode=args.mode, probe=not args.no_probe, goal_text=args.goal, reserve=args.reserve_weekly, max_tasks=args.max_tasks
        )
        if args.json and (args.dry_run or plan.blocked or not plan.queue):
            print(json.dumps(_plan_json(plan), indent=2, default=str))
            return 0
        if not quiet:
            out(report.render_plan(plan, dry_run=args.dry_run, verbose=args.verbose, brand=_brand(args)))
        if args.dry_run or plan.blocked or not plan.queue:
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                out("\nNot started: autopilot needs an explicit yes. Re-run with --yes to start it without a prompt.", "yellow")
                return 2
            if not Confirm.ask(f"\nStart {_brand(args).title()}?", default=False, console=console):
                out("Not started.")
                return 0

        def on(kind: str, data: Any) -> None:
            if quiet:
                return
            if kind == "task_start":
                out(f"\n▶ {data.opp.title}  (score {data.score:.0f})")
            elif kind == "decision" and not data.stop:
                out(f"    {data.provider} · {data.model_class} model · effort {data.effort}", "dim")
            elif kind == "stuck":
                out(f"    stuck ({data.score:.2f}): interrupted", "yellow")
            elif kind == "task_end":
                style = "green" if data.accepted else "yellow" if data.outcome == Outcome.NO_CHANGE else "red"
                out(f"  {report.MARK.get(str(data.outcome), '?')} {data.outcome}  {data.detail[:200]}", style)

        t0 = time.time()
        try:
            final = engine.run(plan, cfg, store, providers, on=on, max_runtime_minutes=args.max_runtime)
        except state.AlreadyRunning as e:
            out(str(e), "red")
            return 2
    if quiet:
        print(json.dumps(final, indent=2, default=str))
        return 0
    out("\n" + report.render_report(final, brand=_brand(args)))
    out(f"Saved: {os.path.join(str(state.run_dir(root, final['run_id'])), 'report.md')}   ({time.time() - t0:.0f}s)", "dim")
    out("Show it again: ration autopilot report", "dim")
    return 0


def add_parsers(sub: Any) -> None:
    gl = sub.add_parser("goal", help="this project's goal: what autopilot ranks work against (stored locally in .ration/)")
    gl.add_argument("action", nargs="?", choices=["show", "set", "edit", "clear"], default="show")
    gl.add_argument("text", nargs="*", help="with `set`: the goal, in a sentence")
    gl.add_argument("--priority", action="append", default=[], help="with `set`: a priority, most important first (repeatable; replaces the list)")
    gl.add_argument("--constraint", action="append", default=[], help="with `set`: something autopilot must not do (repeatable; replaces the list)")
    gl.add_argument("--cwd", default=".")
    gl.add_argument("--json", action="store_true")

    for name, helptext in (
        ("autopilot", "put idle capacity to useful, verified work: finds, ranks and runs safe tasks on separate branches"),
        ("nightshift", "the same command under its other name (`ration nightshift` = `ration autopilot`)"),
    ):
        ap = sub.add_parser(name, help=helptext)
        ap.add_argument("action", nargs="?", choices=["run", "status", "stop", "report"], default="run")
        ap.add_argument("--mode", default="safe", help=f"what may be done unattended: {', '.join(policy.MODES)} (default: safe)")
        ap.add_argument("--dry-run", action="store_true", help="show the plan and the reasons; start no agent, change nothing")
        ap.add_argument("--max-tasks", type=int, default=None, help="at most this many tasks (also the cap when the budget is unknown)")
        ap.add_argument("--max-runtime", type=float, default=None, metavar="MINUTES", help="stop after this long")
        ap.add_argument("--provider", default="auto", help="auto, claude or codex")
        ap.add_argument("--reserve-weekly", type=float, default=None, metavar="PERCENT", help="weekly quota to protect (default: autopilot.weekly_reserve_percent)")
        ap.add_argument("--goal", default=None, help="use this goal for one run instead of the stored one")
        ap.add_argument("--no-probe", action="store_true", help="static scan only: do not run tests, lint or type checks while planning")
        ap.add_argument("--run", default=None, help="with status/report: a run id (prefix) instead of the latest")
        ap.add_argument("--yes", "-y", action="store_true", help="start without asking")
        ap.add_argument("--verbose", "-v", action="store_true", help="show every candidate and the full reasoning")
        ap.add_argument("--json", action="store_true")
        ap.add_argument("--cwd", default=".")
        ap.add_argument("--seed", type=int, default=0, help=argparse.SUPPRESS)
