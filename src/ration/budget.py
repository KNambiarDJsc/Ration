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

from .models import BudgetMode, Confidence, UsageSnapshot

LOOKBACK_HOURS = 3.0
MIN_SPAN_MINUTES = 10.0


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
    same_window = [
        h
        for h in history
        if (start is None or h.captured_at >= start)
        and (snap.reset_at is None or h.reset_at is None or abs(h.reset_at - snap.reset_at) < 120)
        and h.captured_at >= now - LOOKBACK_HOURS * 3600
    ]
    pts = sorted(same_window + [snap], key=lambda s: s.captured_at)
    if len(pts) >= 2:
        first, last = pts[0], pts[-1]
        span_h = (last.captured_at - first.captured_at) / 3600
        if span_h * 60 >= MIN_SPAN_MINUTES:
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
        hist = store.usage_history(snap.provider, snap.window, now - LOOKBACK_HOURS * 3600)
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
    return out


def describe_pressure(p: float | None) -> str:
    if p is None:
        return "unknown"
    if p >= 1.25:
        return "HIGH"
    if p <= 0.75:
        return "LOW"
    return "OK"
