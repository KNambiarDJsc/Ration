"""In-flow advisor for interactive sessions.

Shown to the developer through the hook `systemMessage` field (never added to
the model's context), and advisory only:

- on prompt submit: model/effort suggestion, limit warnings, stuck-session tip
- on stop: a per-turn usage receipt ("that turn used ~6% of your 5-hour window")
- in the status line: a short limit warning segment

Prompt text is classified in memory and never stored. Each kind of tip is
rate-limited so the advisor stays quiet unless something matters.
"""

from __future__ import annotations

import time
from typing import Any

from .budget import ProviderBudget, provider_budgets
from .difficulty import Assessment, assess
from .models import AgentEvent, BudgetMode, Effort, EventType, ModelClass, TaskType
from .stuck import detect

PROVIDER = "claude"
WINDOW_LABEL = {"five_hour": "5-hour", "seven_day": "weekly", "spend_limit": "spend-limit"}
SHORT_LABEL = {"five_hour": "5h", "seven_day": "7d", "spend_limit": "spend"}
TYPE_LABEL = {
    TaskType.TRIVIAL_EDIT: "small edit",
    TaskType.CODE_GENERATION: "feature change",
    TaskType.TESTS: "test-writing task",
    TaskType.DEBUGGING: "debugging",
    TaskType.ARCHITECTURE: "architecture work",
    TaskType.REFACTOR: "refactor",
    TaskType.INVESTIGATION: "investigation",
    TaskType.MIGRATION: "migration",
    TaskType.CODE_REVIEW: "review",
    TaskType.PERFORMANCE: "performance work",
}
USE_IT_QUIET_S = 6 * 3600
Tip = tuple[str, str]  # (kind, message)


def fmt_h(hours: float | None) -> str:
    if hours is None:
        return "?"
    m = max(0, int(round(hours * 60)))
    if m < 60:
        return f"{m}m"
    if m < 48 * 60:
        return f"{m // 60}h{m % 60:02d}m"
    return f"{m // 1440}d{m % 1440 // 60:02d}h"


def _markers(s: str) -> list[str]:
    return [k.strip().lower() for k in s.split(",") if k.strip()]


def model_class_of(model: str | None, cfg: dict[str, Any]) -> ModelClass | None:
    if not model:
        return None
    m = model.lower()
    c = cfg["claude"]
    if any(k in m for k in _markers(c.get("premium_markers", "opus"))):
        return ModelClass.PREMIUM
    if any(k in m for k in _markers(c.get("cheap_markers", "haiku"))):
        return ModelClass.CHEAP
    return ModelClass.STRONG


def recommend(a: Assessment) -> tuple[ModelClass, Effort]:
    """Interactive recommendation. Unlike `ration run`, a failed cheap attempt here
    costs the developer's time, so routine work maps to the strong tier."""
    if a.task_type == TaskType.TRIVIAL_EDIT or a.difficulty < 0.25:
        return ModelClass.CHEAP, Effort.LOW
    if a.difficulty >= 0.65 or a.risk >= 0.6 or a.task_type in (TaskType.ARCHITECTURE, TaskType.MIGRATION):
        return ModelClass.PREMIUM, Effort.HIGH
    return ModelClass.STRONG, Effort.HIGH if a.difficulty >= 0.5 else Effort.MEDIUM


def is_task_like(prompt: str) -> bool:
    p = prompt.strip()
    return bool(p) and p[0] not in "/!#" and len(p.split()) >= 6


def _hardness(a: Assessment) -> list[str]:
    return [r[2:] for r in a.reasons if r.startswith("+ ") and "short/ambiguous" not in r][:2]


def model_tip(a: Assessment, current_model: str | None, cfg: dict[str, Any], budget: ProviderBudget | None) -> Tip | None:
    cur = model_class_of(current_model, cfg)
    if cur is None:
        return None
    rec, effort = recommend(a)
    c = cfg["claude"]
    name = {ModelClass.CHEAP: c["cheap"], ModelClass.STRONG: c["strong"], ModelClass.PREMIUM: c["premium"]}
    known = budget is not None and budget.known
    mode = budget.mode if known else BudgetMode.NORMAL
    label = TYPE_LABEL.get(a.task_type, str(a.task_type))
    bw = budget.binding if known else None

    if cur == ModelClass.PREMIUM and rec != ModelClass.PREMIUM and a.difficulty < 0.45:
        if mode == BudgetMode.AGGRESSIVE:
            return None  # quota would go unused anyway; don't nag
        extra = f" Your {WINDOW_LABEL.get(bw.window, bw.window)} window is {bw.used_percent:.0f}% used." if mode == BudgetMode.CONSERVATIVE and bw else ""
        return (
            "model:down",
            f"this looks like a {label}: {name[rec]} should be enough (you're on {current_model}). "
            f"`/model {name[rec]}` saves premium quota.{extra}",
        )
    if rec == ModelClass.PREMIUM and cur != ModelClass.PREMIUM:
        why = ", ".join(_hardness(a)) or label
        rem = budget.remaining_percent if known else None
        if known and bw and (mode == BudgetMode.CONSERVATIVE or (rem is not None and rem <= cfg["budget"]["reserve_percent"])):
            return (
                "model:up-held",
                f"this looks hard ({why}). It would normally deserve {name[ModelClass.PREMIUM]}, but your "
                f"{WINDOW_LABEL.get(bw.window, bw.window)} window is {bw.used_percent:.0f}% used: stay on {current_model}, "
                "give it the failing test or stack trace up front, and keep the scope tight.",
            )
        return (
            "model:up",
            f"this looks like hard {label} ({why}): consider `/model {name[ModelClass.PREMIUM]}` with {effort} effort. "
            "A stronger first attempt is usually cheaper than several weak ones.",
        )
    if cur == ModelClass.CHEAP and rec == ModelClass.STRONG and a.difficulty >= 0.45:
        return "model:up", f"this looks bigger than a quick edit: `/model {name[ModelClass.STRONG]}` will likely save retries."
    return None


def budget_alert(budgets: dict[str, ProviderBudget], *, short: bool = False) -> Tip | None:
    b = budgets.get(PROVIDER)
    if not b:
        return None
    best: tuple[int, float, str, str, str] | None = None
    for w in b.windows:
        if not w.hours_to_reset:
            continue
        label, sl = WINDOW_LABEL.get(w.window, w.window), SHORT_LABEL.get(w.window, w.window)
        if w.remaining_percent <= 5:
            cand = (
                0,
                w.hours_to_reset,
                f"limit:{w.window}",
                f"your {label} limit is {w.used_percent:.0f}% used; it resets in {fmt_h(w.hours_to_reset)}.",
                f"⚠ {sl} {w.used_percent:.0f}% · resets {fmt_h(w.hours_to_reset)}",
            )
        else:
            eta = w.hours_to_exhaust
            horizon = 2.0 if w.window == "five_hour" else 48.0
            if not (w.reliable and eta is not None and eta < w.hours_to_reset and eta <= horizon):
                continue
            cand = (
                1,
                eta,
                f"pace:{w.window}",
                f"at this pace you'll hit your {label} limit in ~{fmt_h(eta)} (it resets in {fmt_h(w.hours_to_reset)}). "
                "Use a lighter model or fewer subagents for routine steps.",
                f"⚠ {sl} limit in ~{fmt_h(eta)}",
            )
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        return None
    return best[2], best[4] if short else best[3]


def use_it_tip(budgets: dict[str, ProviderBudget]) -> Tip | None:
    b = budgets.get(PROVIDER)
    for w in b.windows if b else []:
        if w.window != "seven_day" or w.hours_to_reset is None:
            continue
        eta = w.hours_to_exhaust
        if w.hours_to_reset <= 24 and w.remaining_percent >= 40 and (eta is None or eta > w.hours_to_reset):
            return (
                "use_it:seven_day",
                f"your weekly window still has {w.remaining_percent:.0f}% left and resets in {fmt_h(w.hours_to_reset)}. "
                "Unused quota is lost at reset: good time for that big refactor, test backfill or review.",
            )
    return None


def stuck_tip(events: list[AgentEvent]) -> Tip | None:
    r = detect(events, now=time.time())
    if r.band not in ("intervene", "likely_stuck"):
        return None
    why = "; ".join(r.reasons[:2])
    return (
        "stuck",
        f"this session looks stuck ({why}). A fresh session (/clear) with a 3-line summary of what failed "
        "is often cheaper than continuing, or try a different approach instead of repeating this one.",
    )


def usage_mark(store: Any, now: float) -> dict[str, list[float | None]]:
    return {
        s.window: [s.used_percent, s.reset_at]
        for s in store.latest_usage(PROVIDER)
        if s.reset_at is None or s.reset_at > now
    }


def on_prompt(payload: dict[str, Any], cfg: dict[str, Any], store: Any, now: float | None = None) -> tuple[list[AgentEvent], dict[str, Any], str | None]:
    """Returns (advice events, extra PROMPT_SUBMITTED metadata, message for the developer)."""
    now = time.time() if now is None else now
    adv = cfg["advisor"]
    sid = str(payload.get("session_id") or "unknown")
    prompt = payload.get("prompt") or ""
    meta: dict[str, Any] = {"usage_mark": usage_mark(store, now)}
    a = assess(prompt) if is_task_like(prompt) else None
    if a:
        meta.update(task_type=str(a.task_type), difficulty=a.difficulty)
    if not adv["enabled"]:
        return [], meta, None

    budgets = provider_budgets(store, cfg, now=now)
    tips: list[Tip] = []
    if adv["budget_alerts"]:
        t = budget_alert(budgets) or use_it_tip(budgets)
        if t:
            tips.append(t)
    t = stuck_tip(store.events(session_id=sid, limit=300))
    if t:
        tips.append(t)
    if adv["model_suggestions"] and a:
        cur = store.last_event(sid, EventType.MODEL_SELECTED)
        t = model_tip(a, cur.metadata.get("model") if cur else None, cfg, budgets.get(PROVIDER))
        if t:
            tips.append(t)

    shown: list[str] = []
    events: list[AgentEvent] = []
    for kind, msg in tips:
        account_wide = kind.startswith(("use_it", "pace:seven_day", "limit:seven_day"))
        quiet = USE_IT_QUIET_S if kind.startswith("use_it") else adv["quiet_minutes"] * 60
        if store.recent_advice(kind, now - quiet, None if account_wide else sid):
            continue
        shown.append(msg)
        events.append(AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": kind}, timestamp=now))
    return events, meta, ("RATION: " + "\nRATION: ".join(shown)) if shown else None


def on_stop(payload: dict[str, Any], cfg: dict[str, Any], store: Any) -> tuple[list[AgentEvent], str | None]:
    adv = cfg["advisor"]
    if not (adv["enabled"] and adv["turn_receipts"]) or payload.get("stop_hook_active"):
        return [], None
    sid = str(payload.get("session_id") or "unknown")
    p = store.last_event(sid, EventType.PROMPT_SUBMITTED)
    if p is None or not p.metadata.get("usage_mark"):
        return [], None
    latest = {s.window: s for s in store.latest_usage(PROVIDER)}
    deltas: dict[str, float] = {}
    for window, (used0, reset0) in p.metadata["usage_mark"].items():
        s = latest.get(window)
        same_window = s is not None and (reset0 is None or s.reset_at is None or abs(s.reset_at - reset0) < 120)
        if s is not None and same_window and s.captured_at > p.timestamp:
            deltas[window] = s.used_percent - float(used0)
    main = next((w for w in ("five_hour", "spend_limit", "seven_day") if w in deltas), None)
    if main is None or deltas[main] < adv["receipt_min_percent"]:
        return [], None
    turn = store.events(session_id=sid, since=p.timestamp, limit=5000)
    subs = sum(1 for e in turn if e.event_type == EventType.SUBAGENT_STARTED)
    tools = sum(1 for e in turn if e.event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED))
    s = latest[main]
    detail = [f"{subs} subagent{'s' if subs != 1 else ''}"] if subs else []
    if tools:
        detail.append(f"{tools} tool calls")
    hours = None if s.reset_at is None else max(0.0, (s.reset_at - time.time()) / 3600)
    msg = (
        f"RATION: that turn used ~{deltas[main]:.0f}% of your {WINDOW_LABEL.get(main, main)} window"
        + (f" ({', '.join(detail)})" if detail else "")
        + f". Now {s.used_percent:.0f}% used, resets in {fmt_h(hours)}."
    )
    if main != "seven_day" and deltas.get("seven_day", 0) >= 1:
        msg += f" Weekly +{deltas['seven_day']:.0f}%."
    ev = AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": "receipt", "delta": round(deltas[main], 2), "subagents": subs})
    return [ev], msg
