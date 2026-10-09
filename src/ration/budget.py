"""Reset-aware budgeting.

    target_burn_rate = remaining_budget / remaining_window_time
    burn_pressure    = observed_burn_rate / target_burn_rate

pressure > 1  -> on track to run out before the reset
pressure < 1  -> on track to leave quota unused at the reset

Unused quota is only ever offered to *queued useful work* (AGGRESSIVE needs a
backlog). RATION never burns quota for its own sake.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from .models import BudgetMode, Confidence, EventType, ResourceType, Source, UsageSnapshot

LOOKBACK_HOURS = 3.0  # used when the window length is unknown
MAX_LOOKBACK_HOURS = 24.0
MIN_SPAN_MINUTES = 10.0


def lookback_hours(window_minutes: int | None) -> float:
    """Measure pace over ~10% of the window: 30 min for a 5-hour window, ~17 h
    for a weekly one, so a short burst of work doesn't read as weekly pace."""
    if not window_minutes:
        return LOOKBACK_HOURS
    return min(MAX_LOOKBACK_HOURS, max(0.5, window_minutes / 60 * 0.1))


@dataclass
class WindowBudget:
    provider: str
    window: str
    used_percent: float
    remaining_percent: float
    hours_to_reset: float | None
    time_remaining_frac: float | None
    observed_burn: float | None  # percent per hour
    target_burn: float | None  # percent per hour
    burn_pressure: float | None
    mode: BudgetMode
    depletes_early_hours: float | None  # >0: runs out this many hours before reset
    confidence: Confidence
    source: str
    reasons: list[str] = field(default_factory=list)
    burn_method: str = "insufficient_history"
    elapsed_hours: float | None = None

    @property
    def reliable(self) -> bool:
        """Is the pace estimate solid enough to warn a human about?"""
        if self.burn_method == "recent_samples":
            return True
        return self.burn_method == "window_average" and (self.elapsed_hours or 0) >= 0.5

    @property
    def hours_to_exhaust(self) -> float | None:
        if not self.observed_burn or self.observed_burn <= 0:
            return None
        return self.remaining_percent / self.observed_burn


@dataclass
class ProviderBudget:
    provider: str
    windows: list[WindowBudget]

    @property
    def known(self) -> bool:
        return bool(self.windows)

    @property
    def binding(self) -> WindowBudget | None:
        """The window that constrains spending the most."""
        if not self.windows:
            return None

        def key(w: WindowBudget) -> tuple[int, float, float]:
            order = {BudgetMode.CONSERVATIVE: 0, BudgetMode.NORMAL: 1, BudgetMode.AGGRESSIVE: 2}
            return (order[w.mode], -(w.burn_pressure or 0.0), w.remaining_percent)

        return sorted(self.windows, key=key)[0]

    @property
    def mode(self) -> BudgetMode:
        b = self.binding
        return b.mode if b else BudgetMode.NORMAL

    @property
    def remaining_percent(self) -> float | None:
        return min((w.remaining_percent for w in self.windows), default=None)

    @property
    def burn_pressure(self) -> float | None:
        vals = [w.burn_pressure for w in self.windows if w.burn_pressure is not None]
        return max(vals) if vals else None


def _observed_burn(
    snap: UsageSnapshot, history: list[UsageSnapshot], now: float
) -> tuple[float | None, str]:
    """Percent-per-hour burn inside the current window."""
    start = snap.window_start
    lb = lookback_hours(snap.window_minutes)
    min_span_h = max(MIN_SPAN_MINUTES / 60, lb * 0.25)
    same_window = [
        h
        for h in history
        if (start is None or h.captured_at >= start)
        and (snap.reset_at is None or h.reset_at is None or abs(h.reset_at - snap.reset_at) < 120)
        and h.captured_at >= now - lb * 3600
    ]
    pts = sorted(same_window + [snap], key=lambda s: s.captured_at)
    if len(pts) >= 2:
        first, last = pts[0], pts[-1]
        span_h = (last.captured_at - first.captured_at) / 3600
        if span_h >= min_span_h:
            return max(0.0, (last.used_percent - first.used_percent) / span_h), "recent_samples"
    if start is not None:
        elapsed_h = (now - start) / 3600
        if elapsed_h > 0.05:
            return snap.used_percent / elapsed_h, "window_average"
    return None, "insufficient_history"


def window_budget(
    snap: UsageSnapshot,
    history: list[UsageSnapshot] | None = None,
    *,
    now: float | None = None,
    reserve_percent: float = 20,
    conservative_pressure: float = 1.25,
    aggressive_pressure: float = 0.75,
    has_backlog: bool = False,
) -> WindowBudget:
    now = time.time() if now is None else now
    reasons: list[str] = []
    remaining = snap.remaining_percent
    hours_to_reset = None if snap.reset_at is None else max(0.0, (snap.reset_at - now) / 3600)
    frac = None
    if hours_to_reset is not None and snap.window_minutes:
        frac = min(1.0, hours_to_reset * 60 / snap.window_minutes)

    observed, method = _observed_burn(snap, history or [], now)
    confidence = snap.confidence
    if method == "window_average" and confidence == Confidence.HIGH:
        confidence = Confidence.MEDIUM
    if method == "insufficient_history":
        confidence = Confidence.LOW

    target = None
    pressure = None
    if hours_to_reset is not None and hours_to_reset > 0:
        target = remaining / hours_to_reset
        if observed is not None:
            if target > 0:
                pressure = observed / target
            else:
                pressure = math.inf if observed > 0 else 0.0

    depletes_early = None
    if observed and observed > 0 and hours_to_reset is not None:
        hours_to_exhaust = remaining / observed
        if hours_to_exhaust < hours_to_reset:
            depletes_early = hours_to_reset - hours_to_exhaust

    mode = BudgetMode.NORMAL
    if remaining <= reserve_percent and (hours_to_reset is None or hours_to_reset > 0.25):
        mode = BudgetMode.CONSERVATIVE
        reasons.append(f"below_reserve({remaining:.0f}%<={reserve_percent:.0f}%)")
    elif pressure is not None and pressure >= conservative_pressure:
        mode = BudgetMode.CONSERVATIVE
        reasons.append(f"burn_pressure_high({pressure:.2f})")
    elif (
        pressure is not None
        and pressure <= aggressive_pressure
        and frac is not None
        and frac <= 0.35
        and remaining > reserve_percent + 20
    ):
        if has_backlog:
            mode = BudgetMode.AGGRESSIVE
            reasons.append(f"underutilized_near_reset({remaining:.0f}%_left,{hours_to_reset:.1f}h)")
        else:
            reasons.append("underutilized_but_no_backlog")
    if depletes_early:
        reasons.append(f"projected_depletion_{depletes_early:.1f}h_before_reset")

    return WindowBudget(
        provider=snap.provider,
        window=snap.window,
        used_percent=snap.used_percent,
        remaining_percent=remaining,
        hours_to_reset=hours_to_reset,
        time_remaining_frac=frac,
        observed_burn=observed,
        target_burn=target,
        burn_pressure=pressure,
        mode=mode,
        depletes_early_hours=depletes_early,
        confidence=confidence,
        source=str(snap.source),
        reasons=reasons,
        burn_method=method,
        elapsed_hours=None if snap.window_start is None else max(0.0, (now - snap.window_start) / 3600),
    )


def provider_budgets(
    store: Any, cfg: dict[str, Any], *, now: float | None = None, has_backlog: bool = False
) -> dict[str, ProviderBudget]:
    now = time.time() if now is None else now
    b = cfg["budget"]
    out: dict[str, ProviderBudget] = {}
    for snap in store.latest_usage():
        if snap.reset_at is not None and snap.reset_at < now:
            continue  # window already reset; the old number is meaningless
        hist = store.usage_history(snap.provider, snap.window, now - lookback_hours(snap.window_minutes) * 3600)
        wb = window_budget(
            snap,
            hist,
            now=now,
            reserve_percent=b["reserve_percent"],
            conservative_pressure=b["conservative_pressure"],
            aggressive_pressure=b["aggressive_pressure"],
            has_backlog=has_backlog,
        )
        out.setdefault(snap.provider, ProviderBudget(snap.provider, [])).windows.append(wb)
    for wb in usd_windows(store, cfg, now=now, has_backlog=has_backlog):
        out.setdefault("claude", ProviderBudget("claude", [])).windows.append(wb)
    return out


# ---- dollar budgets for API-key users ---------------------------------------
def usd_spend(store: Any, start: float, at: float) -> float:
    """Dollars spent across all sessions in [start, at], from Claude Code's own
    cumulative per-session cost estimate (statusline `cost.total_cost_usd`)."""
    samples = store.events_of_type(EventType.COST, since=start - 2 * 86400, limit=20000)
    per: dict[str, tuple[float, float]] = {}  # session -> (baseline before start, peak in range)
    for e in samples:
        if e.timestamp > at:
            continue
        usd = float(e.metadata.get("usd") or 0.0)
        base, peak = per.get(e.session_id, (0.0, 0.0))
        if e.timestamp < start:
            per[e.session_id] = (usd, usd)
        else:
            per[e.session_id] = (base, max(peak, usd))
    return sum(max(0.0, peak - base) for base, peak in per.values())


def _period_start(now: float, days: int) -> float:
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight - (lt.tm_wday * 86400 if days == 7 else 0)


def usd_windows(store: Any, cfg: dict[str, Any], *, now: float, has_backlog: bool = False) -> list[WindowBudget]:
    b = cfg["budget"]
    out: list[WindowBudget] = []
    for name, limit, days in (("daily_usd", b.get("daily_usd", 0), 1), ("weekly_usd", b.get("weekly_usd", 0), 7)):
        if not limit:
            continue
        start = _period_start(now, days)
        end = start + days * 86400
        minutes = days * 24 * 60
        then = max(start, now - lookback_hours(minutes) * 3600)

        def snap(at: float, start: float = start, end: float = end, limit: float = limit, minutes: int = minutes, name: str = name) -> UsageSnapshot:
            used = 100 * usd_spend(store, start, at) / float(limit)
            return UsageSnapshot("claude", name, ResourceType.DOLLARS, used, end, minutes, Source.PROVIDER_CLI, Confidence.MEDIUM, at)

        out.append(
            window_budget(
                snap(now),
                [snap(then)] if then < now else [],
                now=now,
                reserve_percent=b["reserve_percent"],
                conservative_pressure=b["conservative_pressure"],
                aggressive_pressure=b["aggressive_pressure"],
                has_backlog=has_backlog,
            )
        )
    return out


def describe_pressure(p: float | None) -> str:
    if p is None:
        return "unknown"
    if p >= 1.25:
        return "HIGH"
    if p <= 0.75:
        return "LOW"
    return "OK"
