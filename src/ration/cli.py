"""`ration` command line."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from . import __version__, config
from . import progress as progress_mod
from .budget import ProviderBudget, describe_pressure, provider_budgets
from .difficulty import make_task
from .models import BudgetMode
from .store import SCHEMA_VERSION, Store
from .stuck import detect, recommend

console = Console(highlight=False)
TAGLINE = "Spend intelligence where it matters."
NAMES = {"claude": "Claude Code", "codex": "Codex"}


# ---------------------------------------------------------------- helpers
def bar(pct: float, width: int = 18) -> Text:
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100)
    color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    t = Text("█" * filled, style=color)
    t.append("░" * (width - filled), style="dim")
    return t


def fmt_dur(hours: float | None) -> str:
    if hours is None:
        return "?"
    s = int(hours * 3600)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h:02d}h" if d else f"{h}h {m:02d}m"


def ago(ts: float) -> str:
    s = max(0, time.time() - ts)
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s / 60:.0f}m ago"
    if s < 172800:
        return f"{s / 3600:.0f}h ago"
    return f"{s / 86400:.0f}d ago"


def header() -> Text:
    t = Text("RATION", style="bold")
    t.append(f"  {TAGLINE}", style="dim")
    return t


def refresh_usage(store: Store, cfg: dict[str, Any]) -> None:
    if cfg["codex"].get("enabled", True):
        try:
            from .providers.codex import CodexProvider

            CodexProvider(cfg).ingest_usage(store)
        except Exception:
            pass


def recommendations(budgets: dict[str, ProviderBudget]) -> list[str]:
    recs: list[str] = []
    tight = [n for n, b in budgets.items() if b.mode == BudgetMode.CONSERVATIVE]
    loose = [
        n
        for n, b in budgets.items()
        if b.mode != BudgetMode.CONSERVATIVE and (b.burn_pressure is not None and b.burn_pressure < 0.75)
    ]
    for n in tight:
        b = budgets[n].binding
        if b and b.depletes_early_hours:
            recs.append(f"{NAMES.get(n, n)} is projected to run out {fmt_dur(b.depletes_early_hours)} before its reset.")
        else:
            recs.append(f"{NAMES.get(n, n)} is tight ({budgets[n].remaining_percent:.0f}% left).")
    if tight and loose:
        recs.append(f"Route routine tasks toward {', '.join(NAMES.get(n, n) for n in loose)}.")
    if tight:
        recs.append(f"Reserve {NAMES.get(tight[0], tight[0])} premium capacity for high-value debugging.")
    for n in loose:
        b = budgets[n].binding
        if b and b.time_remaining_frac is not None and b.time_remaining_frac < 0.35 and b.remaining_percent > 40:
            recs.append(
                f"{NAMES.get(n, n)} has {b.remaining_percent:.0f}% left with {fmt_dur(b.hours_to_reset)} to reset: "
                "spend it on queued valuable work (reviews, tests, refactors), not on nothing."
            )
    return recs


# ---------------------------------------------------------------- commands
def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    with Store() as store:
        refresh_usage(store, cfg)
        budgets = provider_budgets(store, cfg)
    if args.json:
        print(json.dumps({n: [w.__dict__ for w in b.windows] for n, b in budgets.items()}, default=str, indent=2))
        return 0
    console.print(header())
    console.print()
    if not budgets:
        console.print("No usage data yet.", style="yellow")
        console.print("  Claude Code: run [bold]ration statusline install[/] (reads the official rate_limits from Claude Code's status line; Pro/Max).")
        console.print("  Codex: usage is read from local rollout logs after you use Codex once.")
        return 0
    for name, b in budgets.items():
        console.print(Text(NAMES.get(name, name), style="bold"))
        for w in sorted(b.windows, key=lambda w: w.window):
            line = Text(f"  {w.window:<11}")
            line.append_text(bar(w.used_percent))
            line.append(f" {w.used_percent:>3.0f}% used   reset {fmt_dur(w.hours_to_reset)}")
            console.print(line)
            p = w.burn_pressure
            style = {"HIGH": "red", "LOW": "cyan", "OK": "green"}.get(describe_pressure(p), "dim")
            detail = Text("              burn pressure ")
            detail.append(f"{p:.2f} {describe_pressure(p)}" if p is not None and p != float("inf") else "unknown", style=style)
            detail.append(f"   confidence {w.confidence}   source {w.source}", style="dim")
            console.print(detail)
        console.print()
    reserve = cfg["budget"]["reserve_percent"]
    r = Text("Reserve      ")
    r.append_text(bar(reserve))
    r.append(f" {reserve}%")
    console.print(r)
    recs = recommendations(budgets)
    if recs:
        console.print()
        console.print("Recommendation:", style="bold")
        for rec in recs:
            console.print(f"  {rec}")
    return 0


def session_rows(store: Store, cfg: dict[str, Any], since_min: float) -> list[dict[str, Any]]:
    rows = []
    budgets = provider_budgets(store, cfg)
    for s in store.active_sessions(time.time() - since_min * 60):
        evs = store.events(session_id=s["session_id"], limit=400)
        st = detect(evs, now=time.time())
        pr = progress_mod.compute(evs, now=time.time())
        b = budgets.get(s["provider"])
        subs = sum(1 for e in evs if e.event_type == "SUBAGENT_STARTED")
        rec = recommend(st, budget_mode=b.mode if b else BudgetMode.NORMAL, parallel_subagents=subs)
        rows.append({"s": s, "stuck": st, "progress": pr, "rec": rec, "budget": b, "subagents": subs})
    return rows


def watch_frame(store: Store, cfg: dict[str, Any], since_min: float) -> Group:
    parts: list[Any] = [header()]
    budgets = provider_budgets(store, cfg)
    if budgets:
        bl = Text()
        for n, b in budgets.items():
            bw = b.binding
            if bw:
                bl.append(f"{NAMES.get(n, n)}: {bw.used_percent:.0f}% used ({bw.window}), burn {describe_pressure(bw.burn_pressure)}   ")
        parts.append(bl)
    rows = session_rows(store, cfg, since_min)
    if not rows:
        parts.append(Text(f"\nNo agent activity in the last {since_min:.0f} min. Is the Claude hook installed? (`ration hooks status`)", style="dim"))
        return Group(*parts)
    for r in rows:
        s, st, pr, rec = r["s"], r["stuck"], r["progress"], r["rec"]
        band_style = {"healthy": "green", "watch": "yellow", "intervene": "dark_orange", "likely_stuck": "red"}[st.band]
        t = Table.grid(padding=(0, 2))
        t.add_row("progress", f"{pr.score:.2f} {pr.arrow}")
        t.add_row("stuck", Text(f"{st.score:.2f} {st.band}", style=band_style))
        t.add_row("events", f"{s['n']} (last {ago(s['last_at'])})")
        if r["subagents"]:
            t.add_row("subagents", str(r["subagents"]))
        reasons = pr.reasons[:3] + [f"! {x}" for x in st.reasons[:4]]
        body: list[Any] = [t]
        if reasons:
            body.append(Text("\n".join(reasons), style="dim"))
        if st.band != "healthy":
            body.append(Text(f"Suggestion: {rec.intervention} — {rec.explanation}", style=band_style))
        title = f"{NAMES.get(s['provider'], s['provider'])} session {s['session_id'][:8]}"
        parts.append(Panel(Group(*body), title=title, title_align="left", border_style=band_style))
    return Group(*parts)


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = config.load()
    with Store() as store:
        refresh_usage(store, cfg)
        if args.once:
            console.print(watch_frame(store, cfg, args.minutes))
            return 0
        try:
            with Live(watch_frame(store, cfg, args.minutes), console=console, refresh_per_second=1, screen=False) as live:
                while True:
                    time.sleep(args.interval)
                    live.update(watch_frame(store, cfg, args.minutes))
        except KeyboardInterrupt:
            pass
    return 0


def _print_assessment(task: Any, a: Any) -> None:
    console.print(header())
    console.print(f"\n[bold]Task[/]  {task.description}")
    console.print(
        f"type {task.task_type}   difficulty {task.estimated_difficulty:.2f}   value {task.estimated_value:.2f}   risk {task.risk:.2f}"
    )
    console.print("  " + "; ".join(a.reasons), style="dim")


def _print_policy(p: Any) -> None:
    if p.stop:
        console.print(f"\n[bold red]STOP[/]  reasons: {', '.join(p.reasons)}")
        return
    console.print(
        f"\n[bold]Decision[/]  {NAMES.get(p.provider, p.provider)} · {p.model_class} model · effort {p.effort} · "
        f"max turns {p.max_turns} · retries {p.retry_budget} · subagents {p.subagent_allowance} · {p.escalation}"
    )
    console.print("  reasons: " + json.dumps(p.reasons), style="dim")


def cmd_run(args: argparse.Namespace, dry_run: bool = False) -> int:
    from .orchestrator import execute
    from .providers import get_providers

    cfg = config.load()
    cwd = os.path.abspath(args.cwd)
    task, a = make_task(" ".join(args.task), repo=cwd, priority=args.priority, value=args.value, checks=args.check)
    providers = get_providers(cfg)
    if args.provider:
        if args.provider == "mock":
            from .providers.mock import MockProvider

            providers = {"mock": MockProvider(seed=args.seed)}  # type: ignore[dict-item]
        else:
            providers = {k: v for k, v in providers.items() if k == args.provider}
    if not providers:
        console.print("No providers enabled/available. See `ration providers`.", style="red")
        return 2
    _print_assessment(task, a)
    counts: dict[str, int] = {}

    def on(kind: str, data: Any) -> None:
        if kind == "decision":
            _print_policy(data)
        elif kind == "event":
            counts[str(data.event_type)] = counts.get(str(data.event_type), 0) + 1
        elif kind == "stuck":
            console.print(f"  [red]likely stuck[/] ({data.score:.2f}): {'; '.join(data.reasons)}")
        elif kind == "attempt":
            style = "green" if data.outcome == "succeeded" else "yellow"
            checks = f"checks {data.checks_passed}/{data.checks_total}" if data.checks_total else "no checks (unverified)"
            cost = f"   ${data.cost_usd:.3f}" if data.cost_usd is not None else ""
            console.print(f"  attempt → [{style}]{data.outcome}[/]   {checks}   progress {data.progress:.2f}   stuck {data.stuck:.2f}{cost}")
            if data.error:
                console.print(f"  error: {data.error[:300]}", style="red")

    with Store() as store:
        report = execute(
            task,
            a,
            providers=providers,  # type: ignore[arg-type]
            store=store,
            cfg=cfg,
            cwd=cwd,
            dry_run=dry_run or args.dry_run,
            interrupt_when_stuck=not args.no_interrupt,
            permission_mode=args.permission_mode,
            on=on,
        )
    if dry_run or args.dry_run:
        console.print("\n(plan only, nothing was run)", style="dim")
        return 0
    status = "[green]completed[/]" if report.completed else "[yellow]not completed[/]"
    console.print(f"\n{status} after {len(report.attempts)} attempt(s) in {report.wall_time_s:.0f}s")
    if report.stopped_reason:
        console.print(f"stopped: {', '.join(report.stopped_reason)}", style="dim")
    return 0 if report.completed else 1


def cmd_plan(args: argparse.Namespace) -> int:
    return cmd_run(args, dry_run=True)


def cmd_sessions(args: argparse.Namespace) -> int:
    from .providers import get_providers

    cfg = config.load()
    t = Table("provider", "session", "project", "last active", "observed events", "tokens")
    seen = set()
    with Store() as store:
        observed = {r["session_id"]: r for r in store.active_sessions(time.time() - 7 * 86400)}
    for name, prov in get_providers(cfg).items():
        for s in prov.sessions(args.limit):
            seen.add(s.session_id)
            o = observed.get(s.session_id)
            t.add_row(name, s.session_id[:12], s.project or "", ago(s.last_active), str(o["n"]) if o else "-", f"{s.tokens:,}" if s.tokens else "")
    for sid, o in observed.items():
        if sid not in seen:
            t.add_row(o["provider"], sid[:12], "", ago(o["last_at"]), str(o["n"]), "")
    console.print(t)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    t = Table("when", "task", "type", "status", "attempts", "checks", "time", "cost")
    with Store() as store:
        for r in store.history(args.limit):
            checks = f"{r['checks_passed']}/{r['checks_total']}" if r["checks_total"] else "-"
            t.add_row(
                ago(r["created_at"]),
                r["description"][:50],
                r["task_type"] or "",
                r["status"],
                str(r["attempts"] or 0),
                checks,
                f"{r['wall_time_s']:.0f}s" if r["wall_time_s"] else "-",
                f"${r['cost_usd']:.3f}" if r["cost_usd"] else "-",
            )
    console.print(t)
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    from .providers import get_providers

    cfg = config.load()
    t = Table("provider", "installed", "version", "features", "notes")
    for name, prov in get_providers(cfg).items():
        c = prov.capabilities()
        feats = ", ".join(k for k, v in c.features.items() if v)
        t.add_row(NAMES.get(name, name), "✓" if c.installed else "✗", c.version or "", feats, "; ".join(c.notes))
    console.print(t)
    return 0


def hook_latency(n: int = 5) -> float | None:
    payload = json.dumps({"hook_event_name": "RationDoctorPing", "session_id": "doctor"}).encode()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, "-I", "-m", "ration.hooks.claude"], input=payload, capture_output=True, timeout=30)
        if r.returncode != 0:
            return None
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .integrations import claude_install
    from .providers import get_providers

    cfg = config.load()
    ok = True

    def check(good: bool, msg: str, hint: str = "", warn: bool = False) -> None:
        nonlocal ok
        mark = "[green]✓[/]" if good else ("[yellow]![/]" if warn else "[red]✗[/]")
        console.print(f"{mark} {msg}")
        if not good and hint:
            console.print(f"    {hint}", style="dim")
        if not good and not warn:
            ok = False

    check(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    home = config.ration_home()
    try:
        with Store() as store:
            v = store.conn.execute("PRAGMA user_version").fetchone()[0]
            mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        check(v == SCHEMA_VERSION, f"store {config.db_path()} (schema v{v}, {mode})")
    except Exception as e:
        check(False, f"store at {home}: {e}")
    for name, prov in get_providers(cfg).items():
        c = prov.capabilities()
        check(c.installed, f"{NAMES.get(name, name)} {c.version or ''}".strip(), "; ".join(c.notes), warn=True)
    st = claude_install.status()
    if st.get("error"):
        check(False, st["error"])
    else:
        check(bool(st["hooks"]), f"Claude hooks: {', '.join(st['hooks']) or 'not installed'}", "run `ration hooks install`", warn=True)
        check(st["statusline"], "Claude statusline usage capture", "run `ration statusline install`", warn=True)
        if st["disable_all_hooks"]:
            check(False, "disableAllHooks is set in Claude settings: RATION hooks will not run", warn=True)
    check(True, f"guard mode: {cfg['guard']['mode']}  (change with `ration guard on|off`)")
    check(True, f"advisor: {'on' if cfg['advisor']['enabled'] else 'off'}  (change with `ration advisor on|off`)")
    lat = hook_latency()
    check(lat is not None and lat < 500, f"hook latency {lat:.0f} ms (median of 5)" if lat else "hook failed to run", "hooks must stay fast; check your Python install")
    log = home / "hook-errors.log"
    if log.exists() and log.stat().st_size:
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        check(False, "recent hook errors (hooks fail open, but please report):", "\n    ".join(tail), warn=True)
    return 0 if ok else 1


def cmd_privacy(args: argparse.Namespace) -> int:
    cfg = config.load()
    with Store() as store:
        if args.purge:
            if args.yes or Confirm.ask("Delete ALL locally stored RATION data?", default=False):
                store.purge()
                console.print("Purged.")
            return 0
        counts = store.table_counts()
    db = config.db_path()
    console.print(header())
    console.print(f"\nAll data is local: [bold]{db}[/] ({db.stat().st_size / 1024:.0f} KB)" if db.exists() else f"\nNo data yet ({db})")
    console.print(f"Telemetry: [bold]{'ON' if cfg['privacy']['telemetry'] else 'off'}[/] (RATION has no upload code path)")
    t = Table("table", "rows")
    for k, v in counts.items():
        t.add_row(k, str(v))
    console.print(t)
    console.print("[bold]Stored[/]: event types, tool names, hashes of tool inputs, test runner names and pass/fail counts,")
    console.print(f"  token/cost numbers, usage percentages, task descriptions you pass to `ration run`, file paths ({'on' if cfg['privacy']['store_paths'] else 'off'}).")
    console.print("  For prompts: only length, an estimated task type/difficulty, and the usage % at submit time.")
    console.print("[bold]Never stored[/]: source code, diffs, prompt text, full commands, tool output, credentials.")
    console.print("Delete everything: [bold]ration privacy --purge[/]", style="dim")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from .sim import bench

    results = bench.run(args.tasks, args.seeds)
    t = Table("policy", "resolved", "value", "units/resolved", "stuck units", "premium units", "wall h", "unused @reset", "ran out early", "blocked")
    for m in results:
        t.add_row(
            f"{m.policy} {bench.POLICY_NAMES[m.policy]}",
            f"{m.resolved:.1f}",
            f"{m.value:.1f}",
            f"{m.per_resolved:.2f}",
            f"{m.stuck_spent:.0f}",
            f"{m.premium_spent:.0f}",
            f"{m.wall_min / 60:.1f}",
            f"{m.unused_at_reset:.0f}%",
            f"{m.exhausted_early_h:.0f}h",
            f"{m.blocked:.1f}",
        )
    console.print(f"Synthetic benchmark: {args.tasks} tasks × {args.seeds} seeds, 3 providers, identical weekly budgets")
    console.print(t)
    console.print(
        "These numbers come from a simulated world whose parameters live in ration/sim/bench.py. "
        "They check policy behaviour; they are not evidence about real agents.",
        style="dim",
    )
    return 0


def cmd_hooks(args: argparse.Namespace) -> int:
    from .integrations import claude_install as ci

    try:
        if args.action == "install":
            b = ci.install_hooks()
            console.print(f"Installed RATION observer hooks into {ci.settings_path()}" + (f" (backup: {b.name})" if b else ""))
            console.print(f"Mode: {config.load()['guard']['mode']}. Your existing hooks are untouched. Restart Claude Code sessions to pick them up.")
        elif args.action == "uninstall":
            b = ci.uninstall_hooks()
            console.print("Removed RATION hooks." + (f" (backup: {b.name})" if b else ""))
        else:
            console.print_json(json.dumps(ci.status()))
    except ci.SettingsError as e:
        console.print(str(e), style="red")
        return 2
    return 0


def cmd_statusline(args: argparse.Namespace) -> int:
    from .integrations import claude_install as ci

    try:
        if args.action == "install":
            b, chained = ci.install_statusline()
            console.print("RATION now records Claude's official rate_limits from the status line.")
            if chained:
                console.print(f"Your existing status line is preserved and still shown: {chained}", style="dim")
            if b:
                console.print(f"Backup: {b}", style="dim")
        else:
            ci.uninstall_statusline()
            console.print("Restored your previous status line (if any).")
    except ci.SettingsError as e:
        console.print(str(e), style="red")
        return 2
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    mode = "guard" if args.state == "on" else "observe"
    config.set_value("guard", "mode", mode)
    console.print(f"guard mode: {mode}")
    if mode == "guard":
        console.print("Hooks may now deny wasteful subagent spawns and inject stuck warnings. `ration guard off` to return to observe-only.", style="dim")
    return 0


def cmd_advisor(args: argparse.Namespace) -> int:
    on = args.state == "on"
    config.set_value("advisor", "enabled", on)
    if on:
        console.print("advisor: on. In Claude Code you'll see RATION tips when they matter:")
        console.print("  · on prompt submit: model/effort suggestion, limit warnings, stuck-session tip", style="dim")
        console.print("  · after a turn: how much of your 5-hour window it used (only if ≥ 2%)", style="dim")
        console.print("  · in the status line: '⚠ 5h limit in ~35m' when you're on pace to hit it", style="dim")
        console.print("Tips are shown to you only, never sent to the model. Tune them under [advisor] in config.toml.", style="dim")
    else:
        console.print("advisor: off (observation and guard mode are unaffected)")
    return 0


def cmd_snippet(args: argparse.Namespace) -> int:
    from .providers.codex import agents_md_fragment

    print(agents_md_fragment())
    console.print("RATION never edits AGENTS.md / CLAUDE.md. Paste this yourself if you want it.", style="dim", stderr=True)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    import shutil

    from .integrations import claude_install as ci

    console.print(header())
    console.print("\nDetecting…")
    found = {
        "Claude Code": shutil.which("claude"),
        "Codex": shutil.which("codex"),
        "Gemini CLI": shutil.which("gemini"),
        "Git": shutil.which("git"),
        "Python": sys.executable,
    }
    for k, v in found.items():
        console.print(f"  {'[green]✓[/]' if v else '[red]✗[/]'} {k}")
    if not config.config_path().exists():
        config.save(config.DEFAULTS)
        console.print(f"\nWrote {config.config_path()}", style="dim")

    def ask(q: str) -> bool:
        return True if args.yes else Confirm.ask(q, default=True)

    console.print()
    if found["Claude Code"]:
        try:
            if ask("Enable Claude Code observer integration (hooks; observe-only by default)?"):
                ci.install_hooks()
                console.print("  hooks installed", style="green")
            if ask("Capture Claude subscription usage from the status line (your current status line keeps working)?"):
                ci.install_statusline()
                console.print("  statusline usage capture installed", style="green")
        except ci.SettingsError as e:
            console.print(f"  {e}", style="red")
    codex_on = ask("Enable Codex integration (reads usage from local Codex logs only)?")
    config.set_value("codex", "enabled", codex_on)
    console.print(
        "\nIn Claude Code you'll now see short RATION tips when they matter: which model fits the task you just typed,"
        " a warning before you hit your 5-hour limit, and what each heavy turn cost. Off: [bold]ration advisor off[/]."
    )
    console.print("Everything stays on this machine. See [bold]ration privacy[/].")
    console.print("Next: [bold]ration status[/] · [bold]ration watch[/] · [bold]ration plan \"<task>\"[/]")
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ration", description=f"RATION — {TAGLINE} The inference control plane for coding agents.")
    p.add_argument("--version", action="version", version=f"ration {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="detect agents and enable integrations").add_argument("--yes", "-y", action="store_true")
    s = sub.add_parser("status", help="budgets, burn pressure, reset forecast")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("watch", help="live view of agent sessions: progress and stuck detection")
    w.add_argument("--once", action="store_true")
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument("--minutes", type=float, default=30.0, help="show sessions active in the last N minutes")

    for name, helptext in (("run", "schedule and run a task"), ("plan", "show what `run` would do, without running")):
        r = sub.add_parser(name, help=helptext)
        r.add_argument("task", nargs="+")
        r.add_argument("--check", action="append", default=[], help="acceptance check command (repeatable)")
        r.add_argument("--priority", choices=["low", "normal", "high", "critical"], default="normal")
        r.add_argument("--value", type=float, default=None, help="task value 0..1 (default: from priority)")
        r.add_argument("--provider", default=None, help="restrict to one provider (claude, codex, mock)")
        r.add_argument("--cwd", default=".")
        r.add_argument("--dry-run", action="store_true")
        r.add_argument("--no-interrupt", action="store_true", help="don't stop a run that looks stuck")
        r.add_argument("--permission-mode", default=None, help="pass through to Claude Code (never bypassPermissions)")
        r.add_argument("--seed", type=int, default=0, help=argparse.SUPPRESS)

    sub.add_parser("sessions", help="discovered and observed agent sessions").add_argument("--limit", type=int, default=15)
    sub.add_parser("history", help="tasks run through RATION").add_argument("--limit", type=int, default=20)
    sub.add_parser("providers", help="detected providers and capabilities")
    sub.add_parser("doctor", help="check installation, hooks, latency")
    pv = sub.add_parser("privacy", help="show exactly what is stored")
    pv.add_argument("--purge", action="store_true")
    pv.add_argument("--yes", "-y", action="store_true")
    b = sub.add_parser("benchmark", help="compare routing policies in a reproducible simulation")
    b.add_argument("--tasks", type=int, default=100)
    b.add_argument("--seeds", type=int, default=5)
    h = sub.add_parser("hooks", help="Claude Code hook integration")
    h.add_argument("action", choices=["install", "uninstall", "status"])
    sl = sub.add_parser("statusline", help="Claude Code status line usage capture")
    sl.add_argument("action", choices=["install", "uninstall"])
    g = sub.add_parser("guard", help="turn enforcement on/off (default: observe only)")
    g.add_argument("state", choices=["on", "off"])
    ad = sub.add_parser("advisor", help="in-session tips: model suggestions, limit warnings, turn receipts")
    ad.add_argument("state", choices=["on", "off"])
    sub.add_parser("snippet", help="print an optional AGENTS.md / CLAUDE.md fragment (never written for you)")
    return p


COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "watch": cmd_watch,
    "run": cmd_run,
    "plan": cmd_plan,
    "sessions": cmd_sessions,
    "history": cmd_history,
    "providers": cmd_providers,
    "doctor": cmd_doctor,
    "privacy": cmd_privacy,
    "benchmark": cmd_benchmark,
    "hooks": cmd_hooks,
    "statusline": cmd_statusline,
    "guard": cmd_guard,
    "advisor": cmd_advisor,
    "snippet": cmd_snippet,
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    return COMMANDS[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
