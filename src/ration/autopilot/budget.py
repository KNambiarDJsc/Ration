"""How much autopilot may spend: weekly quota first, the idle short window second.

Built on the existing budget engine (`ration.budget.WindowBudget`); nothing here
re-measures usage. The answer is an `Allowance`: a number of percent of the
short (5-hour) window that is safe to use for optional work right now, plus the
reasons, or a refusal.

    weekly spendable = weekly remaining
                       - max(weekly reserve, your own projected use until the weekly reset)
                       - an uncertainty margin
    short spendable  = short-window remaining - a small reserve for when you come back
    allowance        = min(short spendable, weekly spendable / weekly-cost-of-1%-short)

Every number that is not an official reading is labelled as an estimate.
Stdlib-only: the in-session advisor imports this on the hook path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..budget import ProviderBudget, WindowBudget
from ..models import Confidence

DAY_MINUTES = 24 * 60
# How much of the weekly window 1% of the short window costs, when this machine has not
# shown us yet. An assumption, and labelled as one wherever it is used.
DEFAULT_RATIO = 0.12
MIN_RATIO_SIGNAL = 20.0  # percent of the short window observed before a measured ratio is trusted
STALE_MINUTES = 30.0
MIN_USEFUL = 1.0  # below this much allowance nothing can run


@dataclass
class Allowance:
    provider: str
    known: bool  # is there usage data at all?
    allowed: bool
    spendable: float | None = None  # estimated % of the short window autopilot may use
    weekly_spendable: float | None = None  # % of the long window
    short: WindowBudget | None = None
    long: WindowBudget | None = None
    reserve: float = 0.0  # effective weekly reserve
    configured_reserve: float = 0.0
    ratio: float = DEFAULT_RATIO
    ratio_source: str = "assumed"
    confidence: str = "low"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def w(b: WindowBudget | None) -> dict[str, Any] | None:
            if b is None:
                return None
            return {"window": b.window, "used": b.used_percent, "remaining": b.remaining_percent, "hours_to_reset": b.hours_to_reset, "source": b.source}

        return {
            "provider": self.provider,
            "known": self.known,
            "allowed": self.allowed,
            "spendable": self.spendable,
            "weekly_spendable": self.weekly_spendable,
            "short": w(self.short),
            "long": w(self.long),
            "reserve": self.reserve,
            "configured_reserve": self.configured_reserve,
            "ratio": self.ratio,
            "ratio_source": self.ratio_source,
            "confidence": self.confidence,
            "reasons": self.reasons,
        }


SHORT_WINDOWS = {"five_hour", "primary", "daily_usd"}
LONG_WINDOWS = {"seven_day", "secondary", "weekly_usd"}


def split_windows(budget: ProviderBudget | None) -> tuple[WindowBudget | None, WindowBudget | None]:
    """(short, long): the rolling session window and the multi-day one. A window of
    unknown length (e.g. a gateway spend limit) is treated as long, the cautious reading."""
    short = long = None
    for w in budget.windows if budget else []:
        if w.window in SHORT_WINDOWS:
            is_long = False
        elif w.window in LONG_WINDOWS or not w.time_remaining_frac or not w.hours_to_reset:
            is_long = True
        else:
            is_long = w.hours_to_reset * 60 / w.time_remaining_frac > DAY_MINUTES
        if is_long and (long is None or w.remaining_percent < long.remaining_percent):
            long = w
        elif not is_long and (short is None or w.remaining_percent < short.remaining_percent):
            short = w
    return short, long


def effective_reserve(configured: float, hours_to_reset: float | None) -> float:
    """Near the weekly reset, unused weekly quota is about to expire, so the reserve
    relaxes: linearly over the last 24 hours, never below a quarter of what you set."""
    if hours_to_reset is None:
        return configured
    return configured * max(0.25, min(1.0, hours_to_reset / 24))


def window_ratio(store: Any, provider: str, short: str, long: str, now: float, days: float = 14) -> tuple[float, str]:
    """Weekly % consumed per 1% of the short window, from this machine's official readings."""

    def climbed(window: str) -> float:
        total, prev = 0.0, None
        for s in store.usage_history(provider, window, now - days * 86400):
            if prev is not None and prev.reset_at is not None and s.reset_at is not None and abs(prev.reset_at - s.reset_at) < 120:
                total += max(0.0, s.used_percent - prev.used_percent)
            prev = s
        return total

    try:
        ds, dl = climbed(short), climbed(long)
    except Exception:
        return DEFAULT_RATIO, "assumed"
    if ds < MIN_RATIO_SIGNAL or dl <= 0:
        return DEFAULT_RATIO, "assumed"
    return max(0.02, min(0.5, dl / ds)), "measured on this machine"


def assess(
    budget: ProviderBudget | None,
    cfg: dict[str, Any],
    *,
    provider: str = "claude",
    reserve: float | None = None,
    ratio: tuple[float, str] | None = None,
    age_minutes: float | None = None,
    spent_weekly: float = 0.0,
) -> Allowance:
    """`spent_weekly`: weekly % this autopilot run has already used (for the per-run cap)."""
    ap = cfg["autopilot"]
    configured = float(ap["weekly_reserve_percent"] if reserve is None else reserve)
    r, r_src = ratio or (DEFAULT_RATIO, "assumed")
    a = Allowance(provider, known=False, allowed=False, configured_reserve=configured, reserve=configured, ratio=r, ratio_source=r_src)
    short, long = split_windows(budget)
    a.short, a.long = short, long
    if long is None:
        a.reasons.append(
            "no weekly usage reading is available, so the weekly budget cannot be protected"
            if short is not None
            else "no usage data yet (run `ration statusline install`, or use Claude Code once)"
        )
        return a
    a.known = True
    a.reserve = effective_reserve(configured, long.hours_to_reset)
    if a.reserve < configured - 0.5:
        a.reasons.append(f"weekly window resets in {long.hours_to_reset:.0f}h: reserve relaxed from {configured:.0f}% to {a.reserve:.0f}%")

    low_conf = long.confidence != Confidence.HIGH or (short is not None and short.confidence == Confidence.LOW)
    stale = age_minutes is not None and age_minutes > STALE_MINUTES
    margin = 2.0 + (3.0 if low_conf else 0.0) + (3.0 if stale else 0.0)
    a.confidence = "low" if stale else "medium" if low_conf or r_src == "assumed" else "high"
    if stale:
        a.reasons.append(f"latest usage reading is {age_minutes:.0f} min old: treated as uncertain")

    if long.remaining_percent <= a.reserve:
        a.reasons.append(f"weekly quota is at {long.remaining_percent:.0f}% left, at or below the {a.reserve:.0f}% reserve: optional work is off")
        return a
    conservative = float(cfg["budget"]["conservative_pressure"])
    if long.reliable and long.burn_pressure is not None and long.burn_pressure >= conservative:
        a.reasons.append(f"your own weekly pace (burn pressure {long.burn_pressure:.2f}) is already on track to run out before the reset")
        return a

    protect = a.reserve
    if long.reliable and long.observed_burn and long.hours_to_reset:
        projected = long.observed_burn * long.hours_to_reset
        if projected > protect:
            protect = projected
            a.reasons.append(f"protecting ~{projected:.0f}% of the week for your own projected use (estimate from your recent pace)")
    weekly = long.remaining_percent - protect - margin
    cap = float(ap["max_weekly_percent_per_run"]) - spent_weekly
    if weekly > cap:
        weekly = cap
        a.reasons.append(f"one run may use at most {ap['max_weekly_percent_per_run']:g}% of the weekly window")
    a.weekly_spendable = max(0.0, weekly)
    if a.weekly_spendable <= 0:
        a.reasons.append("nothing is left of the week after the reserve, your projected use and the uncertainty margin")
        a.spendable = 0.0
        return a

    if short is None:
        short_spendable = 100.0 - float(ap["window_reserve_percent"])
        a.reasons.append("no current short-window reading (it has probably reset): assuming a fresh window")
    else:
        short_spendable = short.remaining_percent - float(ap["window_reserve_percent"])
    a.spendable = max(0.0, min(short_spendable, a.weekly_spendable / r))
    if short is not None and short_spendable <= MIN_USEFUL:
        reset = f"; it resets in {short.hours_to_reset:.1f}h" if short.hours_to_reset else ""
        a.reasons.append(f"the short window has only {short.remaining_percent:.0f}% left{reset}")
    a.allowed = a.spendable >= MIN_USEFUL
    if a.allowed:
        a.reasons.append(
            f"weekly {long.remaining_percent:.0f}% left, {protect:.0f}% protected: up to ~{a.weekly_spendable:.0f}% of the week is spendable"
        )
    return a


def snapshot_age_minutes(store: Any, provider: str, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    ages = [(now - s.captured_at) / 60 for s in store.latest_usage(provider) if s.reset_at is None or s.reset_at > now]
    return max(ages) if ages else None


def suggestion(budget: ProviderBudget | None, cfg: dict[str, Any]) -> str | None:
    """One line for the in-session advisor: plenty of the short window is left, it
    resets soon, and the week is healthy. A pointer to the dry run, never an auto-start."""
    ap = cfg["autopilot"]
    a = assess(budget, cfg)
    s = a.short
    if not (a.allowed and s is not None and a.long is not None and s.hours_to_reset):
        return None
    if s.hours_to_reset > float(ap["suggest_reset_within_hours"]) or s.remaining_percent < float(ap["suggest_min_remaining_percent"]):
        return None
    if (a.spendable or 0) < 10:
        return None
    return (
        f"{s.remaining_percent:.0f}% of this window remains and resets in ~{s.hours_to_reset * 60:.0f}m. "
        f"Weekly reserve is healthy ({a.long.remaining_percent:.0f}% left, {a.reserve:.0f}% protected). "
        "Run `ration autopilot --dry-run` to see useful unattended work."
    )
