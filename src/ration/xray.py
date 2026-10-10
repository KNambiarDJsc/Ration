"""Quota X-ray: where did your Claude Code usage actually go?

Reads past session transcripts (main sessions and their subagents), read-only,
from ~/.claude/projects. No model calls, nothing is stored, message content is
never read; only timestamps, models and token counts.

Everything is expressed as *shares of your cache-weighted token usage*
(input 1.0, output 5.0, cache write 1.25, cache read 0.1), which is what makes
long, warm contexts expensive. The "what if" numbers are labelled estimates
with their assumption stated; they are upper bounds, not promises.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .transcript import Call, iter_calls

DEFAULT_CTX_CAP = 120_000
RECACHE_MIN_WRITE = 20_000  # tokens written after an idle gap to count as a rebuild
RECACHE_MIN_GAP_S = 300
READ_WEIGHT = 0.1


@dataclass
class Thread:
    """One transcript file: a main session, or one subagent of a session."""

    session: str
    project: str
    is_sub: bool
    calls: list[Call] = field(default_factory=list)


def default_root() -> Path:
    import os

    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def find_threads(root: Path, since: float) -> list[Thread]:
    threads: list[Thread] = []
    if not root.is_dir():
        return threads
    files: list[tuple[Path, str, bool]] = []
    for p in root.glob("*/*.jsonl"):
        files.append((p, p.stem, False))
    for p in root.glob("*/*/subagents/*.jsonl"):
        files.append((p, p.parent.parent.name, True))
    for path, session, is_sub in files:
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        calls = [c for c in iter_calls(path) if c.ts >= since]
        if not calls:
            continue
        cwd = next((c.cwd for c in calls if c.cwd), None)
        project = Path(cwd).name if cwd else path.parent.name[-24:]
        threads.append(Thread(session, project or "?", is_sub, calls))
    return threads


def _is_premium(model: str, markers: str) -> bool:
    m = model.lower()
    return any(k.strip() and k.strip() in m for k in markers.split(","))


def analyze(
    root: Path | None = None,
    *,
    days: float = 7,
    ctx_cap: int = DEFAULT_CTX_CAP,
    premium_markers: str = "opus",
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    since = now - days * 86400
    threads = find_threads(root or default_root(), since)

    total = 0.0
    by_model: dict[str, float] = defaultdict(float)
    comp = {"cache_read": 0.0, "cache_write": 0.0, "output": 0.0, "input": 0.0}
    by_day: dict[str, float] = defaultdict(float)
    by_project: dict[str, float] = defaultdict(float)
    sessions: dict[str, dict[str, Any]] = {}
    sub_total = 0.0
    long_total = 0.0
    long_saving = 0.0
    recache_total = 0.0
    recache_events: list[dict[str, Any]] = []
    premium_total = 0.0
    n_calls = 0

    for t in threads:
        calls = sorted(t.calls, key=lambda c: c.ts)
        s = sessions.setdefault(
            t.session,
            {"session": t.session, "project": t.project, "weighted": 0.0, "calls": 0, "peak_ctx": 0, "subagent": 0.0, "first": calls[0].ts, "last": calls[0].ts},
        )
        prev: Call | None = None
        for c in calls:
            w = c.weighted
            total += w
            n_calls += 1
            by_model[c.model] += w
            comp["cache_read"] += c.cr * 0.1
            comp["cache_write"] += c.cw * 1.25
            comp["output"] += c.out * 5.0
            comp["input"] += c.inp
            by_day[time.strftime("%Y-%m-%d", time.localtime(c.ts))] += w
            by_project[t.project] += w
            s["weighted"] += w
            s["calls"] += 1
            s["first"], s["last"] = min(s["first"], c.ts), max(s["last"], c.ts)
            if not t.is_sub:
                s["peak_ctx"] = max(s["peak_ctx"], c.ctx)
            if t.is_sub:
                sub_total += w
                s["subagent"] += w
            if _is_premium(c.model, premium_markers):
                premium_total += w
            if c.ctx > ctx_cap:
                long_total += w
                # If the context had been kept at the cap, these cache reads would be smaller.
                long_saving += min(c.cr, c.ctx - ctx_cap) * READ_WEIGHT
            if prev is not None and c.ts - prev.ts >= RECACHE_MIN_GAP_S and c.cw >= RECACHE_MIN_WRITE:
                cost = c.cw * 1.25
                recache_total += cost
                recache_events.append({"session": t.session, "project": t.project, "gap_min": round((c.ts - prev.ts) / 60), "tokens": c.cw, "weighted": cost})
            prev = c

    def share(x: float) -> float:
        return x / total if total else 0.0

    top_sessions = sorted(sessions.values(), key=lambda s: s["weighted"], reverse=True)[:5]
    for s in top_sessions:
        s["share"] = share(s["weighted"])
        s["hours"] = (s["last"] - s["first"]) / 3600
    top3_share = share(sum(s["weighted"] for s in sorted(sessions.values(), key=lambda s: s["weighted"], reverse=True)[:3]))

    result: dict[str, Any] = {
        "days": days,
        "ctx_cap": ctx_cap,
        "calls": n_calls,
        "sessions": len(sessions),
        "total_weighted": total,
        "components": {k: share(v) for k, v in comp.items()},
        "by_model": {m: share(w) for m, w in sorted(by_model.items(), key=lambda kv: -kv[1])},
        "by_day": {d: share(w) for d, w in sorted(by_day.items())},
        "by_project": {p: share(w) for p, w in sorted(by_project.items(), key=lambda kv: -kv[1])[:6]},
        "top_sessions": top_sessions,
        "top3_session_share": top3_share,
        "subagent_share": share(sub_total),
        "premium_share": share(premium_total),
        "long_context_share": share(long_total),
        "long_context_saving": share(long_saving),
        "recache_share": share(recache_total),
        "recache_events": len(recache_events),
        "recache_gap_over_60": sum(1 for e in recache_events if e["gap_min"] >= 60),
    }
    result["findings"] = findings(result)
    return result


def findings(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Concrete changes, each with the measured share it applies to."""
    out: list[dict[str, Any]] = []
    cap_k = r["ctx_cap"] // 1000
    if r["long_context_saving"] >= 0.03:
        out.append(
            {
                "kind": "context",
                "share": r["long_context_saving"],
                "title": f"Long contexts: up to {r['long_context_saving']:.0%} of your usage was re-reading context above {cap_k}k",
                "do": f"/compact (or /clear with a short summary) once a session passes ~{cap_k}k. {r['long_context_share']:.0%} of usage came from calls above that size.",
                "assumption": f"upper bound: assumes the work could have continued from a {cap_k}k context",
            }
        )
    if r["recache_share"] >= 0.02:
        out.append(
            {
                "kind": "idle",
                "share": r["recache_share"],
                "title": f"Idle rebuilds: {r['recache_share']:.0%} of usage re-created context after you stepped away ({r['recache_events']} times, {r['recache_gap_over_60']} after an hour or more)",
                "do": "Before leaving a big session for more than a few minutes, /compact it, or start the next session fresh with a 3-line summary instead of resuming.",
                "assumption": "measured: tokens written to cache after a gap of 5+ minutes",
            }
        )
    if r["subagent_share"] >= 0.25:
        out.append(
            {
                "kind": "subagents",
                "share": r["subagent_share"],
                "title": f"Subagents: {r['subagent_share']:.0%} of usage ran in subagents",
                "do": "Ask for fewer or narrower subagents, give them model: sonnet/haiku for read-only search, or turn on `ration guard on` to cap premium fan-out.",
                "assumption": "measured",
            }
        )
    if r["premium_share"] >= 0.5:
        out.append(
            {
                "kind": "model",
                "share": r["premium_share"],
                "title": f"Model mix: {r['premium_share']:.0%} of usage ran on the premium model",
                "do": "Default to a cheaper model and switch up for hard debugging/design; the advisor will suggest switches per prompt.",
                "assumption": "measured share; the saving depends on your plan's relative model rates",
            }
        )
    out.sort(key=lambda f: -f["share"])
    top = r["top_sessions"][0] if r["top_sessions"] else None
    if top and top["share"] >= 0.4 and r["calls"] >= 50:
        out.append(
            {
                "kind": "concentration",
                "share": top["share"],
                "title": f"One session ({top['project']}, {top['session'][:8]}) used {top['share']:.0%} of everything: {top['hours']:.0f}h open, {top['calls']} calls, peak context {top['peak_ctx'] // 1000}k",
                "do": "Long-lived sessions are where quota quietly goes. Close the thread when a task is done and start the next one fresh.",
                "assumption": "measured",
            }
        )
    return out
