"""Local export: a shareable snapshot of `ration xray` + current usage, bundled
into one file (HTML or JSON) a developer can hand to a team lead.

This is deliberately not a hosted dashboard. Nothing is uploaded, nothing is
collected beyond what `xray` and `status` already compute from local session
files, and sharing the file is something the developer does on purpose, once,
the same way they would share a screenshot. Aggregating several people's
exports is left to whatever the team already uses (a shared drive, a wiki
page, their own script over several `--json` files).
"""

from __future__ import annotations

import html as _html
import time
from pathlib import Path
from typing import Any

from . import xray
from .budget import provider_budgets

WINDOW_LABEL = {"five_hour": "5-hour", "seven_day": "weekly", "daily_usd": "daily spend", "weekly_usd": "weekly spend", "spend_limit": "spend limit"}
COMPONENT_LABEL = [("cache_read", "re-reading context"), ("cache_write", "writing new context"), ("output", "model output"), ("input", "fresh input")]


def build(cfg: dict[str, Any], store: Any, *, days: float = 7, projects_dir: Path | None = None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    x = xray.analyze(projects_dir, days=days, now=now, premium_markers=cfg["claude"].get("premium_markers", "opus"))
    budgets = provider_budgets(store, cfg, now=now)
    return {
        "label": str(cfg.get("report", {}).get("label") or ""),
        "generated_at": now,
        "days": days,
        "xray": x,
        "budgets": {n: [w.__dict__ for w in b.windows] for n, b in budgets.items()},
    }


# ---- HTML rendering (stdlib only; no template engine, no external assets) ---------------------
_STYLE = """
body{font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#0b0e14;color:#e6e6e6;margin:0;padding:32px 16px}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.meta{color:#8b949e;font-size:13px;margin-bottom:28px}
h2{font-size:15px;color:#8b949e;text-transform:uppercase;letter-spacing:.04em;margin:32px 0 10px;font-weight:600}
.row{display:flex;align-items:center;gap:10px;margin:4px 0;font-size:13px}
.label{width:200px;flex:none;color:#c9d1d9}
.bar{flex:1;height:10px;background:#1c2128;border-radius:5px;overflow:hidden}
.bar>span{display:block;height:100%;background:#58a6ff;border-radius:5px}
.pct{width:46px;text-align:right;flex:none;color:#8b949e}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:600;font-size:12px;text-transform:uppercase}
.finding{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:12px 14px;margin:8px 0}
.finding b{color:#e6e6e6}
.finding .do{color:#8b949e;margin-top:4px}
.empty{color:#8b949e;font-style:italic}
"""


def _esc(s: Any) -> str:
    return _html.escape(str(s))


def _bar_row(label: str, share: float) -> str:
    pct = max(0.0, min(1.0, share)) * 100
    return f'<div class="row"><div class="label">{_esc(label)}</div><div class="bar"><span style="width:{pct:.1f}%"></span></div><div class="pct">{share:.0%}</div></div>'


def render_html(data: dict[str, Any]) -> str:
    x = data["xray"]
    label = f" &middot; {_esc(data['label'])}" if data.get("label") else ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["generated_at"]))
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>RATION usage report</title>",
        f"<style>{_STYLE}</style></head><body><div class='wrap'>",
        f"<h1>RATION usage report{label}</h1>",
        f"<div class='meta'>generated {_esc(when)} &middot; last {data['days']:g} days &middot; {x.get('sessions', 0)} sessions, "
        f"{x.get('calls', 0):,} model calls &middot; local export, nothing uploaded</div>",
    ]
    if not x.get("calls"):
        parts.append("<p class='empty'>No Claude Code sessions found in this window.</p>")
    else:
        parts.append("<h2>What the tokens were</h2>")
        for key, lbl in COMPONENT_LABEL:
            parts.append(_bar_row(lbl, x["components"].get(key, 0.0)))

        parts.append("<h2>By model</h2>")
        for m, s in list(x.get("by_model", {}).items())[:6]:
            parts.append(_bar_row(m, s))

        sessions = x.get("top_sessions") or []
        if sessions:
            parts.append("<h2>Biggest sessions</h2><table><tr><th>share</th><th>project</th><th>session</th><th>hours</th><th>calls</th><th>peak context</th></tr>")
            for s in sessions:
                parts.append(
                    f"<tr><td>{s['share']:.0%}</td><td>{_esc(s['project'])}</td><td>{_esc(s['session'][:8])}</td>"
                    f"<td>{s['hours']:.1f}h</td><td>{s['calls']}</td><td>{s['peak_ctx'] // 1000}k</td></tr>"
                )
            parts.append("</table>")

        findings = x.get("findings") or []
        parts.append("<h2>What to change</h2>")
        if not findings:
            parts.append("<p class='empty'>Nothing stood out when this was generated.</p>")
        for f in findings:
            parts.append(f"<div class='finding'><b>{_esc(f['title'])}</b><div class='do'>&rarr; {_esc(f['do'])}</div></div>")

    budgets = data.get("budgets") or {}
    if budgets:
        parts.append("<h2>Current standing</h2><table><tr><th>provider</th><th>window</th><th>used</th><th>resets in</th></tr>")
        for name, windows in budgets.items():
            for w in windows:
                reset = f"{w['hours_to_reset']:.1f}h" if w.get("hours_to_reset") is not None else "?"
                parts.append(f"<tr><td>{_esc(name)}</td><td>{_esc(WINDOW_LABEL.get(w['window'], w['window']))}</td><td>{w['used_percent']:.0f}%</td><td>{reset}</td></tr>")
        parts.append("</table>")

    parts.append("</div></body></html>")
    return "\n".join(parts)
