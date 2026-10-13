"""Plain-text rendering of plans, status and reports.

Only measured things are reported: check results before and after, files and
lines changed, what each attempt used. No "hours saved". Numbers that are
estimates say so.
"""

from __future__ import annotations

import time
from typing import Any

from .budget import Allowance
from .models import Decision, Outcome, ScoredTask, level
from .verify import short_cmd

MARK = {"SUCCESS": "✓", "IMPROVED": "✓", "NO_CHANGE": "·", "FAILED": "✗", "REGRESSION": "✗", "BLOCKED": "✗", "UNVERIFIABLE": "?"}


def dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    h, mnt = s // 3600, s % 3600 // 60
    return f"{h}h {mnt:02d}m" if h else f"{mnt}m {s % 60:02d}s" if mnt else f"{s}s"


def hours(h: float | None) -> str:
    return "?" if h is None else dur(h * 3600)


def budget_lines(a: Allowance | dict[str, Any], *, label_short: str = "5-hour window", label_long: str = "Weekly") -> list[str]:
    d = a.to_dict() if isinstance(a, Allowance) else a
    out = []
    if not d.get("known") and not d.get("short"):
        return ["  unknown: no usage readings are available"]
    if s := d.get("short"):
        out.append(f"  {label_short}: {s['used']:.0f}% used, {s['remaining']:.0f}% remaining, resets in {hours(s['hours_to_reset'])}")
    if lg := d.get("long"):
        out.append(f"  {label_long}: {lg['used']:.0f}% used, {lg['remaining']:.0f}% remaining, resets in {hours(lg['hours_to_reset'])}")
        reserve = f"  Weekly reserve: {d['reserve']:.0f}%"
        if abs(d["reserve"] - d["configured_reserve"]) > 0.5:
            reserve += f" (configured {d['configured_reserve']:.0f}%, relaxed near the weekly reset)"
        out.append(reserve)
    if d.get("allowed"):
        out.append(
            f"  Safe to spend now: ~{d['spendable']:.0f}% of the 5-hour window  "
            f"[estimate: 1% of it ≈ {d['ratio']:.2f}% of the week, {d['ratio_source']}; confidence {d['confidence']}]"
        )
    else:
        out.append("  Safe to spend now: nothing")
    return out


def _task_block(i: int | None, t: ScoredTask, verbose: bool) -> list[str]:
    o = t.opp
    head = f"  {f'{i}.' if i else ' -'} {o.title}"
    lines = [f"{head[:74]:<74}  score {t.score:>3.0f}   {level(o.risk).upper()} risk"]
    lines.append(f"       {'; '.join(o.evidence)}  ·  est. ~{t.cost:g}% of 5h ({t.cost_source})  ·  verification {level(o.verifiability)}")
    if verbose:
        lines.append("       why: " + "; ".join(t.reasons))
    return lines


def render_plan(plan: Any, *, dry_run: bool = False, verbose: bool = False, brand: str = "AUTOPILOT") -> str:
    L = [f"{brand} PLAN" if dry_run else f"RATION {brand}", ""]
    L.append(f"Project: {plan.root.name}   Mode: {plan.mode.name} ({plan.mode.summary})")
    if plan.goal and not plan.goal.empty:
        L.append(f"Goal: {plan.goal.goal or '(priorities only)'}")
        if plan.goal.priorities:
            L.append("  priorities: " + "; ".join(plan.goal.priorities))
        if plan.goal.constraints:
            L.append("  constraints: " + "; ".join(plan.goal.constraints))
    else:
        L.append("Goal: none set (ranking by repository signals; set one with `ration goal set \"...\"`)")
    L += ["", "Budget:"] + budget_lines(plan.allowance)
    for r in plan.allowance.reasons:
        L.append(f"  · {r}")
    checks = [f"{k} `{short_cmd(v)}`" for k, v in (("tests", plan.checks.test), ("lint", plan.checks.lint), ("types", plan.checks.typecheck)) if v]
    L += ["", "Verification: " + (", ".join(checks) if checks else "no test or lint command detected (set autopilot.test_command)")]
    if plan.dirty:
        L.append(f"Working tree: {len(plan.dirty)} uncommitted change(s). They stay untouched: tasks start from the last commit, in separate worktrees.")
    by = {d: [t for t in plan.tasks if t.decision == d] for d in Decision}
    L += ["", f"Repository scan: {len(plan.tasks)} candidate task{'s' if len(plan.tasks) != 1 else ''} discovered"]
    if plan.queue:
        L += ["", "Would run, in this order:" if dry_run else "Recommended:"]
        for i, t in enumerate(plan.queue, 1):
            L += _task_block(i, t, verbose)
    for decision, title in ((Decision.DEFER, "Deferred (worth doing, not now):"), (Decision.SKIP, "Skipped:"), (Decision.REJECT, "Rejected automatically:")):
        rows = by[decision]
        if rows:
            L += ["", title]
            shown = rows if verbose else rows[:6]
            for t in shown:
                L.append(f"   - {t.opp.title[:60]}: {t.reasons[0]}")
            if len(rows) > len(shown):
                L.append(f"     … and {len(rows) - len(shown)} more (use --verbose)")
    L.append("")
    if plan.blocked:
        L.append(f"Autopilot will not run: {plan.blocked}.")
    elif not plan.queue:
        L.append("Nothing worthwhile, safe and verifiable to do. Autopilot would stop here, which is better than spending quota for its own sake.")
    else:
        L.append(f"Estimated autonomous tasks: {len(plan.queue)}   Estimated cost: ~{plan.estimated_cost:g}% of the 5-hour window (estimate)")
        if plan.mode.writes_code:
            L.append("Each task builds on the accepted ones before it, on branches under ration/auto/<run>/. Nothing is merged into your branches, pushed or deployed.")
    for n in plan.notes:
        L.append(f"note: {n}")
    if dry_run:
        L += ["", "No changes made. Dry run only: no agent was started."]
    return "\n".join(L)


def _counts(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in results if r["accepted"]], [r for r in results if not r["accepted"]]


def _summary(c: dict[str, Any]) -> str:
    if not c.get("ran", True):
        return "could not run"
    if c.get("passed") is not None or c.get("failed") is not None:
        return f"{c.get('passed') or 0} passing, {c.get('failed') or 0} failing"
    if c.get("issues") is not None and not c.get("ok"):
        return f"{c['issues']} issue(s)"
    return "passing" if c.get("ok") else "failing"


def render_status(s: dict[str, Any], status: str) -> str:
    accepted, rejected = _counts(s.get("results", []))
    queue = len(s.get("queue", []))
    L = [
        f"Autopilot run {s['run_id']}: {status.upper()}  ({s.get('mode')} mode, project {s.get('project')})",
        f"  started {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['started_at']))}, "
        + (f"ran {dur((s.get('ended_at') or s.get('heartbeat_at') or s['started_at']) - s['started_at'])}" if status != "running" else f"running for {dur(time.time() - s['started_at'])}"),
        f"  tasks: {len(s.get('results', []))} of {queue} done, {len(accepted)} accepted, {len(rejected)} not accepted",
    ]
    if s.get("stop_reason"):
        L.append(f"  stopped: {s['stop_reason']}")
    for r in s.get("results", []):
        L.append(f"  {MARK.get(r['outcome'], '?')} {r['title'][:64]}  [{r['outcome']}]")
    return "\n".join(L)


def render_report(s: dict[str, Any], brand: str = "AUTOPILOT") -> str:
    results = s.get("results", [])
    accepted, rejected = _counts(results)
    plan = s.get("plan", [])
    started, ended = s["started_at"], s.get("ended_at") or s.get("heartbeat_at") or s["started_at"]
    L = [f"RATION {brand} REPORT", ""]
    L.append(f"Run {s['run_id']}  ·  project {s.get('project')}  ·  mode {s.get('mode')}  ·  {str(s.get('status')).upper()}")
    L.append(f"Started {time.strftime('%Y-%m-%d %H:%M', time.localtime(started))}  ·  duration {dur(ended - started)}")
    if s.get("goal"):
        L.append(f"Goal: {s['goal']}")
    if s.get("stop_reason"):
        L.append(f"Stopped because: {s['stop_reason']}")

    L += ["", "Budget:"]
    b0, b1 = s.get("budget_start") or {}, s.get("budget_end") or {}
    if b0.get("short") and b1.get("short"):
        if b1["short"]["used"] >= b0["short"]["used"]:
            L.append(f"  5-hour window: {b0['short']['used']:.0f}% → {b1['short']['used']:.0f}% used during the run (official readings; includes anything else you ran)")
        else:
            L.append(f"  5-hour window reset during the run; it is now {b1['short']['used']:.0f}% used")
    if b0.get("long") and b1.get("long"):
        L.append(f"  Weekly: {b0['long']['used']:.0f}% → {b1['long']['used']:.0f}% used; {b1['long']['remaining']:.0f}% remains (reserve {b1['reserve']:.0f}% protected)")
    elif b0.get("long"):
        L.append(f"  Weekly at start: {b0['long']['remaining']:.0f}% remaining (reserve {b0['reserve']:.0f}%)")
    if s.get("reserve_crossed"):
        L.append(f"  WARNING: the weekly window ended {s['reserve_crossed']:g}% below the reserve. Autopilot stops when it sees this, but a task already running can overshoot an estimate.")
    if not b0.get("known"):
        L.append("  No usage readings were available: the run was limited by your task cap, not by a measured budget.")
    usd = [r["cost_usd"] for r in results if r.get("cost_usd") is not None]
    if usd:
        L.append(f"  Provider's own list-price estimate for these tasks: ${sum(usd):.2f} (not what a subscription pays)")

    L += ["", f"Tasks considered: {s.get('considered', len(plan))}   attempted: {len(results)}   accepted: {len(accepted)}   queue re-planned: {s.get('rescans', 0)} time(s)"]
    if accepted:
        L += ["", "Completed:"]
        for r in accepted:
            L.append(f"  ✓ {r['title']}  [{r['outcome']}]")
            L.append(f"      {r['detail']}")
            if r.get("commit"):
                L.append(f"      branch {r['branch']} @ {r['commit'][:10]}  ·  {len(r['files_changed'])} file(s), +{r['added']} -{r['deleted']}")
                L.append(f"      this task alone: git diff {(r.get('start') or '')[:10]}..{r['branch']}")
            elif r.get("worktree"):
                L.append(f"      uncommitted in worktree {r['worktree']} (branch {r['branch']})")
            elif r.get("artifact"):
                L.append(f"      {r['artifact']}")
    if rejected:
        L += ["", "Not accepted:"]
        for r in rejected:
            L.append(f"  {MARK.get(r['outcome'], '✗')} {r['title']}  [{r['outcome']}]")
            L.append(f"      Reason: {r['detail']}")
            if r.get("artifact"):
                L.append(f"      discarded change kept for inspection: {r['artifact']}")
    attempted = {r["task_id"] for r in results}
    not_run = [t for t in plan if t["id"] not in attempted and t["decision"] in (str(Decision.SKIP), str(Decision.REJECT), str(Decision.DEFER), "DROPPED")]
    if not_run:
        L += ["", "Not attempted:"]
        for t in not_run[:12]:
            L.append(f"  - {t['title'][:60]}  [{t['decision']}]  {t['reasons'][0] if t['reasons'] else ''}")
        if len(not_run) > 12:
            L.append(f"    … and {len(not_run) - 12} more")

    comb = s.get("combined")
    if comb:
        L += ["", "Combined result (everything accepted, verified together in a clean checkout):"]
        for label, key in (("base commit", "before"), ("combined", "after")):
            for c in comb.get(key, []):
                L.append(f"  {label:<12} `{short_cmd(c['cmd'])}`: {_summary(c)}")
        L.append(f"  {'OK' if comb.get('ok') else 'FAILED'}: {comb.get('detail')}")
        if not comb.get("ok"):
            L.append("  Do not merge the accepted branch as it is: the tasks passed one by one but not together.")

    files = sorted({f for r in accepted for f in r.get("files_changed", [])})
    if files:
        L += ["", f"Files changed on accepted branches ({len(files)}):"] + [f"  {f}" for f in files[:25]]
        if len(files) > 25:
            L.append(f"  … and {len(files) - 25} more")
    if comb and comb.get("branch"):
        base = (s.get("base") or "")[:10] if s.get("detached") else s.get("base_branch") or "HEAD"
        L += ["", "To review (nothing was merged into your branches):", f"  git diff {base}...{comb['branch']}    # all accepted changes, {comb.get('tasks')} task(s)"]

    models: dict[str, int] = {}
    for r in results:
        for a in r.get("attempts", []):
            key = f"{a['provider']} {a['model'] or a['model_class']} ({a['effort']} effort)"
            models[key] = models.get(key, 0) + 1
    if models:
        L += ["", "Models:"] + [f"  {k}: {n} attempt{'s' if n != 1 else ''}" for k, n in sorted(models.items())]
    stuck = sum(r.get("stuck_interrupts", 0) for r in results)
    L += ["", f"Stuck runs interrupted: {stuck}"]
    regressions = sum(1 for r in results if r["outcome"] == str(Outcome.REGRESSION))
    if regressions:
        L.append(f"Regressions caught and discarded: {regressions}")
    L.append("No pushes. No merges. No deployments.")
    if s.get("checkout_untouched", True):
        L.append("Checked around every task: your checkout, branches, tags, stashes and remote-tracking refs are unchanged. Git network access was off for the agent.")
    else:
        L.append("WARNING: your checkout or refs changed during the run. Autopilot stopped and reverted nothing; check `git status` and `git reflog`.")
    for n in s.get("notes", []):
        L.append(f"note: {n}")
    return "\n".join(L) + "\n"
