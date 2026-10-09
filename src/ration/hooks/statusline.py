"""Claude Code statusLine command.

Claude Code pipes session JSON to the status line on every refresh. For Pro/Max
subscribers (and gateway spend limits) that JSON carries the official
`rate_limits.five_hour` / `seven_day` / `spend_limit` usage percentages and
reset times. This is the only documented, official source of subscription
usage, so RATION records it here.

If the user already had a status line, RATION runs it with the same input and
prints its output unchanged ("chaining"), so nothing visible is lost.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from .. import advisor, config
from ..budget import provider_budgets
from ..models import AgentEvent, Confidence, EventType, ResourceType, Source, UsageSnapshot
from ..shell import shell_argv
from ..store import Store

WINDOW_MINUTES = {"five_hour": 300, "seven_day": 7 * 24 * 60}
MIN_WRITE_INTERVAL_S = 60


def snapshots(payload: dict[str, Any], now: float | None = None) -> list[UsageSnapshot]:
    now = time.time() if now is None else now
    rl = payload.get("rate_limits") or {}
    out: list[UsageSnapshot] = []
    for window, data in rl.items():
        if not isinstance(data, dict) or data.get("used_percentage") is None:
            continue
        out.append(
            UsageSnapshot(
                provider="claude",
                window=window,
                resource_type=ResourceType.USAGE_PERCENT,
                used_percent=float(data["used_percentage"]),
                reset_at=float(data["resets_at"]) if data.get("resets_at") else None,
                window_minutes=WINDOW_MINUTES.get(window),
                source=Source.GATEWAY if window == "spend_limit" else Source.PROVIDER_CLI,
                confidence=Confidence.HIGH,
                captured_at=now,
            )
        )
    return out


def record(payload: dict[str, Any], store: Store) -> list[UsageSnapshot]:
    snaps = snapshots(payload)
    latest = {(s.provider, s.window): s for s in store.latest_usage("claude")}
    written = []
    for s in snaps:
        prev = latest.get((s.provider, s.window))
        if (
            prev is not None
            and abs(prev.used_percent - s.used_percent) < 0.01
            and prev.reset_at == s.reset_at
            and s.captured_at - prev.captured_at < MIN_WRITE_INTERVAL_S
        ):
            continue
        store.add_usage(s)
        written.append(s)
    return written


def _fmt_reset(reset_at: float | None) -> str:
    if not reset_at:
        return "?"
    s = max(0, int(reset_at - time.time()))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d{h:02d}h" if d else f"{h}h{m:02d}m"


def own_line(payload: dict[str, Any]) -> str:
    parts = ["RATION"]
    rl = payload.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d"), ("spend_limit", "spend")):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            parts.append(f"{label} {float(w['used_percentage']):.0f}% (reset {_fmt_reset(w.get('resets_at'))})")
    ctx = (payload.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {float(ctx):.0f}%")
    if len(parts) == 1:
        parts.append("no usage data yet")
    return " · ".join(parts)


def run_chained(command: str, raw: bytes) -> str | None:
    if not command.strip():
        return None
    try:
        argv: Any = shell_argv(command) if os.name == "nt" else command
        r = subprocess.run(argv, input=raw, capture_output=True, timeout=5, shell=isinstance(argv, str))
        return r.stdout.decode("utf-8", "replace").rstrip("\r\n")
    except (OSError, subprocess.SubprocessError):
        return None


def record_model(payload: dict[str, Any], store: Store) -> None:
    """Remember the session's current model so the advisor can compare against it."""
    sid = payload.get("session_id")
    model = (payload.get("model") or {}).get("id") if isinstance(payload.get("model"), dict) else payload.get("model")
    if not sid or not model:
        return
    last = store.last_event(str(sid), EventType.MODEL_SELECTED)
    if last is None or last.metadata.get("model") != model:
        store.add_event(AgentEvent("claude", str(sid), EventType.MODEL_SELECTED, {"model": model, "source": "statusline"}))


def alert_segment(store: Store, cfg: dict[str, Any]) -> str | None:
    adv = cfg["advisor"]
    if not (adv["enabled"] and adv["budget_alerts"]):
        return None
    tip = advisor.budget_alert(provider_budgets(store, cfg), short=True)
    return tip[1] if tip else None


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    cfg = config.load()
    segment = None
    try:
        with Store() as store:
            record(payload, store)
            record_model(payload, store)
            segment = alert_segment(store, cfg)
    except Exception:
        pass
    chained = run_chained(cfg["claude"].get("chained_statusline", ""), raw)
    text = chained if chained is not None else own_line(payload)
    if segment:
        text = f"{text}  {segment}"
    sys.stdout.buffer.write(text.encode("utf-8"))  # Claude Code reads UTF-8; Windows stdout may be cp1252
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
