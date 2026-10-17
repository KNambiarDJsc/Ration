"""In-flow advisor for interactive sessions.

Shown to the developer through the hook `systemMessage` field (never added to
the model's context), and advisory only:

- on prompt submit: model + effort suggestion, limit warnings, stuck-session tip
- on stop: a per-turn receipt ("that turn used 7% of your 5-hour window")
- in the status line: a short limit warning segment

Data sources, all local (no APIs, no LLM calls):
- Claude Code status line JSON: rate_limits %, session cost estimate, model, effort
- session transcripts (`transcript_path`): exact per-turn tokens, model, effort
- RATION's own history: how turns of each kind actually went in this repo

Prompt text is classified in memory and never stored. Each kind of tip is
rate-limited, and model tips are withheld when the classifier isn't confident.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import replace
from typing import Any

from . import transcript
from .budget import ProviderBudget, provider_budgets
from .difficulty import Assessment, assess
from .models import AgentEvent, BudgetMode, Effort, EventType, ModelClass, TaskType
from .normalize import sig
from .stuck import detect

PROVIDER = "claude"
WINDOW_LABEL = {
    "five_hour": "5-hour",
    "seven_day": "weekly",
    "spend_limit": "spend-limit",
    "daily_usd": "daily spend",
    "weekly_usd": "weekly spend",
}
SHORT_LABEL = {"five_hour": "5h", "seven_day": "7d", "spend_limit": "spend", "daily_usd": "$/day", "weekly_usd": "$/wk"}
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
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max"]
USE_IT_QUIET_S = 6 * 3600
AUTOPILOT_QUIET_S = 5 * 3600  # at most once per 5-hour window, across sessions
HISTORY_DAYS = 60
MIN_HISTORY = 4
MIN_CALIBRATION = 3
Tip = tuple[str, str]  # (kind, message)


# ---- formatting ----------------------------------------------------------
def fmt_h(hours: float | None) -> str:
    if hours is None:
        return "?"
    m = max(0, int(round(hours * 60)))
    if m < 60:
        return f"{m}m"
    if m < 48 * 60:
        return f"{m // 60}h{m % 60:02d}m"
    return f"{m // 1440}d{m % 1440 // 60:02d}h"


def fmt_tok(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


# ---- current model / effort ------------------------------------------------
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


def current_model(store: Any, session_id: str, transcript_path: str | None) -> tuple[str | None, str | None]:
    """Newest of: status line / model-switch events, and the transcript's last reply."""
    ev = store.last_event(session_id, EventType.MODEL_SELECTED)
    tl = transcript.tail(transcript_path) if transcript_path else transcript.Slice()
    ev_model = ev.metadata.get("model") if ev else None
    ev_effort = ev.metadata.get("effort") if ev else None
    if ev_model and (tl.last_ts is None or ev.timestamp >= tl.last_ts):
        return ev_model, ev_effort or tl.last_effort
    return tl.last_model or ev_model, tl.last_effort or ev_effort


# ---- classification ---------------------------------------------------------
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


def history_stats(store: Any, repo: str, task_type: TaskType, now: float) -> tuple[int, float, float] | None:
    rows = [
        e.metadata
        for e in store.events_of_type(EventType.TURN_SUMMARY, since=now - HISTORY_DAYS * 86400, limit=3000)
        if e.metadata.get("repo") == repo and e.metadata.get("task_type") == str(task_type)
    ]
    if len(rows) < MIN_HISTORY:
        return None
    return (
        len(rows),
        statistics.median(float(r.get("tools") or 0) for r in rows),
        statistics.median(float(r.get("wtok") or 0) for r in rows),
    )


def contextualize(a: Assessment, store: Any, session_id: str, repo: str, now: float) -> Assessment:
    """Correct the keyword guess with what this session and this repo actually show."""
    reasons, d, ttype, conf = list(a.reasons), a.difficulty, a.task_type, a.confidence
    recent = store.events(session_id=session_id, since=now - 1800, limit=300)
    fails = [e for e in recent if e.event_type == EventType.TEST_RESULT and not e.metadata.get("ok")]
    if fails and ttype in (TaskType.CODE_GENERATION, TaskType.REFACTOR, TaskType.TESTS) and conf != "high":
        ttype, d, conf = TaskType.DEBUGGING, max(d, 0.55) + 0.05, "medium"
        reasons.append(f"+ session has {len(fails)} failing test run(s)")
    stats = history_stats(store, repo, ttype, now)
    if stats:
        n, med_tools, med_wtok = stats
        label = TYPE_LABEL.get(ttype, str(ttype))
        if med_tools >= 40 or med_wtok >= 8e6:
            d = max(d + 0.15, 0.45)  # never call these "simple" again
            reasons.append(f"+ {label} turns in this repo usually take ~{med_tools:.0f} tool calls ({n} seen)")
        elif med_tools <= 6 and med_wtok <= 5e5:
            d -= 0.1
            reasons.append(f"- {label} turns in this repo are usually quick (~{med_tools:.0f} tool calls, {n} seen)")
    return replace(a, task_type=ttype, difficulty=round(max(0.0, min(1.0, d)), 3), reasons=reasons, confidence=conf)


def _hardness(a: Assessment) -> list[str]:
    return [r[2:] for r in a.reasons if r.startswith("+ ") and "short/ambiguous" not in r][:2]


# ---- tips -------------------------------------------------------------------
def _effort_rank(e: str | None) -> int | None:
    return EFFORT_ORDER.index(e) if e in EFFORT_ORDER else None


def effort_change(rec: Effort, cur: str | None, confident_down: bool) -> str | None:
    r, c = _effort_rank(str(rec)), _effort_rank(cur)
    if r is None or c is None:
        return None
    if r > c and rec == Effort.HIGH:
        return "high"
    if c > r and confident_down and c >= EFFORT_ORDER.index("high"):
        return str(rec)
    return None


def model_tip(
    a: Assessment,
    current: str | None,
    cfg: dict[str, Any],
    budget: ProviderBudget | None,
    current_effort: str | None = None,
) -> Tip | None:
    if a.confidence == "low":
        return None  # don't advise on a guess
    cur = model_class_of(current, cfg)
    rec, effort = recommend(a)
    c = cfg["claude"]
    name = {ModelClass.CHEAP: c["cheap"], ModelClass.STRONG: c["strong"], ModelClass.PREMIUM: c["premium"]}
    known = budget is not None and budget.known
    mode = budget.mode if known else BudgetMode.NORMAL
    label = TYPE_LABEL.get(a.task_type, str(a.task_type))
    bw = budget.binding if known else None
    confident_simple = a.confidence == "high" and a.difficulty < 0.45
    eff = effort_change(effort, current_effort, confident_simple)
    eff_cmd = f" and `/effort {eff}`" if eff else ""

    if cur == ModelClass.PREMIUM and rec != ModelClass.PREMIUM and confident_simple:
        if mode == BudgetMode.AGGRESSIVE:
            return None  # quota would go unused anyway; don't nag
        extra = f" Your {WINDOW_LABEL.get(bw.window, bw.window)} window is {bw.used_percent:.0f}% used." if mode == BudgetMode.CONSERVATIVE and bw else ""
        return (
            "model:down",
            f"this looks like a {label}: {name[rec]} should be enough (you're on {current}). "
            f"`/model {name[rec]}`{eff_cmd} {'save' if eff_cmd else 'saves'} premium quota.{extra}",
        )
    if rec == ModelClass.PREMIUM and cur is not None and cur != ModelClass.PREMIUM:
        why = ", ".join(_hardness(a)) or label
        rem = budget.remaining_percent if known else None
        if known and bw and (mode == BudgetMode.CONSERVATIVE or (rem is not None and rem <= cfg["budget"]["reserve_percent"])):
            return (
                "model:up-held",
                f"this looks hard ({why}). It would normally deserve {name[ModelClass.PREMIUM]}, but your "
                f"{WINDOW_LABEL.get(bw.window, bw.window)} window is {bw.used_percent:.0f}% used: stay on {current}, "
                "give it the failing test or stack trace up front, and keep the scope tight.",
            )
        return (
            "model:up",
            f"this looks like hard {label} ({why}): consider `/model {name[ModelClass.PREMIUM]}`"
            f"{eff_cmd or ' with high effort'}. A stronger first attempt is usually cheaper than several weak ones.",
        )
    if cur == ModelClass.CHEAP and rec == ModelClass.STRONG and a.difficulty >= 0.45:
        return "model:up", f"this looks bigger than a quick edit: `/model {name[ModelClass.STRONG]}`{eff_cmd} will likely save retries."
    if eff:  # right model, wrong effort
        direction = "up" if eff == "high" else "down"
        why = f"hard {label}" if direction == "up" else f"a {label}"
        return f"effort:{direction}", f"this looks like {why}: `/effort {eff}` (you're on {current_effort})."
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
        noun = "budget" if w.window.endswith("_usd") else "limit"
        if w.remaining_percent <= 5:
            cand = (
                0,
                w.hours_to_reset,
                f"limit:{w.window}",
                f"your {label} {noun} is {w.used_percent:.0f}% used; it resets in {fmt_h(w.hours_to_reset)}.",
                f"⚠ {sl} {w.used_percent:.0f}% · resets {fmt_h(w.hours_to_reset)}",
            )
        else:
            eta = w.hours_to_exhaust
            horizon = 2.0 if w.window == "five_hour" else 12.0 if w.window == "daily_usd" else 48.0
            if not (w.reliable and eta is not None and eta < w.hours_to_reset and eta <= horizon):
                continue
            cand = (
                1,
                eta,
                f"pace:{w.window}",
                f"at this pace you'll hit your {label} {noun} in ~{fmt_h(eta)} (it resets in {fmt_h(w.hours_to_reset)}). "
                "Use a lighter model or fewer subagents for routine steps.",
                f"⚠ {sl} {noun} in ~{fmt_h(eta)}",
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


def capacity_tip(
    stats: tuple[int, float, float] | None,
    budget: ProviderBudget | None,
    rate: float | None,
    reserve_percent: float,
    label: str,
    min_percent: float = 3.0,
) -> Tip | None:
    """Before a heavy turn, say whether the window can likely absorb it: this is about
    the *next* turn, not a whole multi-turn task (RATION doesn't track task boundaries
    in interactive sessions), using this repo's own median turn cost for this task type.
    Silent without enough history or a calibrated rate — a guess here is worse than none."""
    if stats is None or not rate or not budget or not budget.known:
        return None
    n, _, median_wtok = stats
    predicted_pct = rate * median_wtok / 1e6
    bw = budget.binding
    if predicted_pct < min_percent or bw is None:
        return None
    window_label = WINDOW_LABEL.get(bw.window, bw.window)
    remaining = budget.remaining_percent or 0.0
    reset = f" (resets in {fmt_h(bw.hours_to_reset)})" if bw.hours_to_reset else ""
    if remaining < predicted_pct:
        return (
            "capacity:insufficient",
            f"a {label} turn here usually costs ~{predicted_pct:.0f}% of your {window_label} window ({n} seen); "
            f"you have {remaining:.0f}% left{reset} and may not get a full reply. "
            "Consider waiting for reset, a cheaper model, or narrowing the ask.",
        )
    if remaining - predicted_pct < reserve_percent:
        return (
            "capacity:risky",
            f"a {label} turn here usually costs ~{predicted_pct:.0f}% of your {window_label} window ({n} seen); "
            f"sending it would take you into your reserve ({reserve_percent:.0f}%){reset}.",
        )
    return None


def context_tip(tpath: str | None, cfg: dict[str, Any]) -> Tip | None:
    """When the context is large, every call re-reads all of it. Offer the free way out."""
    limit = int(cfg["advisor"].get("context_tip_tokens", 0))
    if not limit or not tpath:
        return None
    ctx = transcript.tail(tpath).last_ctx
    if ctx < limit:
        return None
    if cfg["handoff"]["auto"]:
        how = "Just `/clear`: RATION carries a restart kit (goal, changed files, last test, git state) into the new session."
    else:
        how = "Run `ration handoff --copy`, then `/clear` and paste: a free restart kit (goal, changed files, last test, git state), no model call."
    return (
        "context:large",
        f"this session's context is ~{fmt_tok(ctx)} tokens and every call re-reads all of it. {how}",
    )


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


def autopilot_tip(store: Any, cfg: dict[str, Any], now: float) -> str | None:
    """At the end of a turn: most of the short window is unused, it resets soon and the
    week is healthy. Points at the dry run; autopilot itself only ever starts when asked."""
    adv = cfg["advisor"]
    if not (adv["enabled"] and adv.get("autopilot_tip", True)):
        return None
    from .autopilot import budget as autopilot_budget

    msg = autopilot_budget.suggestion(provider_budgets(store, cfg, now=now).get(PROVIDER), cfg)
    if not msg or store.recent_advice("autopilot", now - AUTOPILOT_QUIET_S):
        return None
    return f"RATION: {msg}"


# ---- metering + calibration --------------------------------------------------
def usage_mark(store: Any, session_id: str, now: float) -> dict[str, list[float | None]]:
    """Readings at prompt time: {meter: [value, reset_at, captured_at]}."""
    mark: dict[str, list[float | None]] = {
        s.window: [s.used_percent, s.reset_at, s.captured_at]
        for s in store.latest_usage(PROVIDER)
        if s.reset_at is None or s.reset_at > now
    }
    cost = store.last_event(session_id, EventType.COST)
    if cost is not None:
        mark["usd"] = [float(cost.metadata.get("usd") or 0.0), None, cost.timestamp]
    return mark


def _same_window(a: float | None, b: float | None) -> bool:
    return a is None or b is None or abs(a - b) < 120


def reconcile(store: Any, session_id: str, mark: dict[str, Any], now: float) -> list[AgentEvent]:
    """At the next prompt, the previous turn's exact usage is known (mark-to-mark).
    Pair it with that turn's exact token count to learn usage-per-token locally."""
    prev = store.last_event(session_id, EventType.PROMPT_SUBMITTED)
    summ = store.last_event(session_id, EventType.TURN_SUMMARY)
    if prev is None or summ is None or summ.timestamp < prev.timestamp:
        return []
    by_model = summ.metadata.get("by_model") or {}
    total = float(sum(by_model.values()))
    if total <= 0:
        return []
    dom, dom_w = max(by_model.items(), key=lambda kv: kv[1])
    if dom_w / total < 0.8:
        return []  # mixed models: can't attribute usage to one model's rate
    old = prev.metadata.get("usage_mark") or {}
    out = []
    for meter, minimum in (("five_hour", 1.0), ("usd", 0.01)):
        a, b = old.get(meter), mark.get(meter)
        if not a or not b:
            continue
        if meter != "usd" and (not _same_window(a[1], b[1]) or store.other_session_activity(session_id, prev.timestamp, now)):
            continue  # shared window touched by another session: not attributable
        delta = float(b[0]) - float(a[0])
        if delta < minimum:
            continue
        out.append(
            AgentEvent(
                PROVIDER,
                session_id,
                EventType.CALIBRATION,
                {"meter": meter, "model": dom, "rate": delta / (total / 1e6), "delta": round(delta, 4), "wtok": round(total)},
                timestamp=now,
            )
        )
    return out


def calibrated_rate(store: Any, meter: str, model: str, now: float) -> tuple[float, int] | None:
    rates = [
        float(e.metadata["rate"])
        for e in store.events_of_type(EventType.CALIBRATION, since=now - 30 * 86400, limit=2000)
        if e.metadata.get("meter") == meter and e.metadata.get("model") == model and e.metadata.get("rate")
    ][-25:]
    if len(rates) < MIN_CALIBRATION:
        return None
    return statistics.median(rates), len(rates)


# ---- hook entry points ---------------------------------------------------------
def on_prompt(payload: dict[str, Any], cfg: dict[str, Any], store: Any, now: float | None = None) -> tuple[list[AgentEvent], dict[str, Any], str | None]:
    """Returns (events, extra PROMPT_SUBMITTED metadata, message for the developer)."""
    now = time.time() if now is None else now
    adv = cfg["advisor"]
    sid = str(payload.get("session_id") or "unknown")
    tpath = payload.get("transcript_path")
    repo = sig(payload.get("cwd") or "")
    prompt = payload.get("prompt") or ""
    mark = usage_mark(store, sid, now)
    events = reconcile(store, sid, mark, now)
    # A brand-new session's transcript doesn't exist yet at its first prompt: start at 0.
    offset = (transcript.size(tpath) or 0) if tpath else None
    meta: dict[str, Any] = {"usage_mark": mark, "repo": repo, "transcript_offset": offset}
    a = assess(prompt) if is_task_like(prompt) else None
    if a:
        a = contextualize(a, store, sid, repo, now)
        meta.update(task_type=str(a.task_type), difficulty=a.difficulty, confidence=a.confidence, risk=a.risk, hints=_hardness(a))
    if not adv["enabled"]:
        return events, meta, None

    budgets = provider_budgets(store, cfg, now=now)
    tips: list[Tip] = []
    if adv["budget_alerts"]:
        t = budget_alert(budgets) or use_it_tip(budgets)
        if t:
            tips.append(t)
    t = stuck_tip(store.events(session_id=sid, limit=300))
    if t:
        tips.append(t)
    t = context_tip(tpath, cfg)
    if t:
        tips.append(t)
    if adv.get("capacity_tip", True) and a and a.confidence != "low":
        stats = history_stats(store, repo, a.task_type, now)
        rate = any_rate(store, now)
        t = capacity_tip(
            stats, budgets.get(PROVIDER), rate, cfg["budget"]["reserve_percent"],
            TYPE_LABEL.get(a.task_type, str(a.task_type)), float(adv.get("capacity_min_percent", 3.0)),
        )
        if t:
            tips.append(t)
    if adv["model_suggestions"] and a:
        model, effort = current_model(store, sid, tpath)
        if model is None:
            meta["model_tip_pending"] = True  # first prompt, model not known yet: decide on first reply
        t = model_tip(a, model, cfg, budgets.get(PROVIDER), effort)
        if t:
            tips.append(t)

    shown: list[str] = []
    for kind, msg in tips:
        account_wide = kind.startswith(("use_it", "pace:seven_day", "limit:seven_day", "pace:weekly_usd"))
        quiet = USE_IT_QUIET_S if kind.startswith("use_it") else adv["quiet_minutes"] * 60
        if store.recent_advice(kind, now - quiet, None if account_wide else sid):
            continue
        shown.append(msg)
        events.append(AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": kind}, timestamp=now))
    return events, meta, ("RATION: " + "\nRATION: ".join(shown)) if shown else None


def on_session_start(payload: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Resuming a session whose prompt cache has expired re-creates the whole context.
    Say what that costs while the developer can still choose the cheaper path.

    Uses fields Claude Code puts on SessionStart for resume/fork:
    `prompt_cache_likely_expired`, `context_tokens`, `seconds_since_last_response`,
    `estimated_cache_write_usd` (a list-price estimate)."""
    adv = cfg["advisor"]
    if not (adv["enabled"] and adv.get("resume_tip", True)):
        return None
    if payload.get("source") not in ("resume", "fork") or not payload.get("prompt_cache_likely_expired"):
        return None
    ctx = int(payload.get("context_tokens") or 0)
    if ctx < 40_000:
        return None  # small context: a rebuild is cheap, don't nag
    idle = payload.get("seconds_since_last_response")
    usd = payload.get("estimated_cache_write_usd")
    when = f" after {fmt_h(float(idle) / 3600)} idle" if idle else ""
    cost = f" (about ${float(usd):.2f} at list price)" if usd else ""
    return (
        f"RATION: resuming rebuilds ~{fmt_tok(ctx)} tokens of context{when}{cost}, before your first reply. "
        "If you only need the gist, start fresh (/clear) with a 3-line summary of where you were, or run /compact first. "
        "If you need the full thread, carry on."
    )


def deferred_model_tip(payload: dict[str, Any], cfg: dict[str, Any], store: Any, now: float | None = None) -> tuple[list[AgentEvent], str | None]:
    """If the model was unknown at prompt time (new session, no status line), give the
    model tip as soon as the transcript reveals which model is answering."""
    now = time.time() if now is None else now
    adv = cfg["advisor"]
    if not (adv["enabled"] and adv["model_suggestions"]):
        return [], None
    sid = str(payload.get("session_id") or "unknown")
    p = store.last_event(sid, EventType.PROMPT_SUBMITTED)
    if p is None or not p.metadata.get("model_tip_pending") or not p.metadata.get("task_type"):
        return [], None
    done = any(
        e.event_type == EventType.ADVICE and str(e.metadata.get("kind", "")).startswith(("model:", "effort:", "deferred"))
        for e in store.events(session_id=sid, since=p.timestamp, limit=2000)
    )
    if done:
        return [], None
    model, effort = current_model(store, sid, payload.get("transcript_path"))
    if model is None:
        return [], None
    a = Assessment(
        task_type=TaskType(p.metadata["task_type"]),
        difficulty=float(p.metadata.get("difficulty") or 0.5),
        risk=float(p.metadata.get("risk") or 0.3),
        uncertainty=0.5,
        reasons=[f"+ {h}" for h in p.metadata.get("hints") or []],
        confidence=str(p.metadata.get("confidence") or "low"),
    )
    tip = model_tip(a, model, cfg, provider_budgets(store, cfg, now=now).get(PROVIDER), effort)
    marker = AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": tip[0] if tip else "deferred:none"}, timestamp=now)
    if tip is None or store.recent_advice(tip[0], now - adv["quiet_minutes"] * 60, sid):
        return [marker], None
    return [marker], f"RATION: {tip[1]}"


def _turn_tokens(store: Any, sid: str, prompt: AgentEvent, tpath: str | None) -> tuple[dict[str, float], int, float | None]:
    """Exact weighted tokens by model for the turn: main transcript slice + subagents."""
    by_model: dict[str, float] = {}
    total = 0
    last_ts = None
    offset = prompt.metadata.get("transcript_offset")
    if tpath and offset is not None:
        sl = transcript.read_slice(tpath, int(offset))
        for m, t in sl.by_model.items():
            by_model[m] = by_model.get(m, 0.0) + t.weighted
        total += sl.total
        last_ts = sl.last_ts
    for e in store.events(session_id=sid, since=prompt.timestamp, limit=5000):
        if e.event_type == EventType.SUBAGENT_ENDED and e.metadata.get("by_model"):
            for m, w in e.metadata["by_model"].items():
                by_model[m] = by_model.get(m, 0.0) + float(w)
            total += int(e.metadata.get("tokens") or 0)
    return by_model, total, last_ts


def any_rate(store: Any, now: float, meter: str = "five_hour") -> float | None:
    """% of the 5-hour window per million cache-weighted tokens, learned on this machine
    (median across models; the per-model rates are close, and this is only a price tag)."""
    rates = [
        float(e.metadata["rate"])
        for e in store.events_of_type(EventType.CALIBRATION, since=now - 30 * 86400, limit=2000)
        if e.metadata.get("meter") == meter and e.metadata.get("rate")
    ]
    return statistics.median(rates) if rates else None


def message_price(ctx: int, rate: float | None) -> float | None:
    """% of the 5-hour window one more message costs at this context size."""
    if not rate:
        return None
    return rate * (0.1 * ctx + 5 * 1_500) / 1e6


def price_segment(payload: dict[str, Any], store: Any, cfg: dict[str, Any], now: float | None = None) -> str | None:
    """Status-line price tag, e.g. 'ctx 312k · msg ≈0.2%'. Shown only once context is large."""
    limit = int(cfg["advisor"].get("price_tag_min_ctx", 0))
    cw = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    cu = cw.get("current_usage") if isinstance(cw.get("current_usage"), dict) else {}
    ctx = sum(int(cu.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if not limit or ctx < limit:
        return None
    price = message_price(ctx, any_rate(store, time.time() if now is None else now))
    return f"{fmt_tok(ctx)} in context" + (f" · next msg ≈{price:.1f}% of 5h" if price and price >= 0.05 else "")


def compact_nudge(store: Any, sid: str, turn: list[AgentEvent], tpath: str | None, cfg: dict[str, Any], now: float) -> str | None:
    """At a natural boundary (the turn changed files and ended with tests green), a
    compaction loses the least. Say so once, with what the large context is costing."""
    adv = cfg["advisor"]
    limit = int(adv.get("compact_nudge_tokens", 0))
    if not (adv["enabled"] and limit and tpath):
        return None
    changed = any(e.event_type == EventType.FILE_CHANGED for e in turn)
    results = [e for e in turn if e.event_type == EventType.TEST_RESULT]
    if not (changed and results and results[-1].metadata.get("ok")):
        return None
    ctx = transcript.tail(tpath).last_ctx
    if ctx < limit or store.recent_advice("compact-nudge", now - 30 * 60, sid):
        return None
    price = message_price(ctx, any_rate(store, now))
    cost = f" (≈{price:.1f}% of your 5-hour window per message)" if price and price >= 0.05 else ""
    return (
        f"RATION: good moment to compact: tests are green and the context is {fmt_tok(ctx)}, re-read on every message{cost}. "
        "`/compact` now loses the least, or `ration handoff --copy` then `/clear` for a fresh start."
    )


def on_stop(payload: dict[str, Any], cfg: dict[str, Any], store: Any, now: float | None = None) -> tuple[list[AgentEvent], str | None]:
    now = time.time() if now is None else now
    adv = cfg["advisor"]
    if payload.get("stop_hook_active"):
        return [], None
    sid = str(payload.get("session_id") or "unknown")
    p = store.last_event(sid, EventType.PROMPT_SUBMITTED)
    if p is None:
        return [], None
    by_model, tokens, last_ts = _turn_tokens(store, sid, p, payload.get("transcript_path"))
    wtok = sum(by_model.values())
    turn = store.events(session_id=sid, since=p.timestamp, limit=5000)
    subs = sum(1 for e in turn if e.event_type == EventType.SUBAGENT_STARTED)
    tools = sum(1 for e in turn if e.event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED))
    summary = {
        "task_type": p.metadata.get("task_type"),
        "difficulty": p.metadata.get("difficulty"),
        "repo": p.metadata.get("repo"),
        "tools": tools,
        "subagents": subs,
        "tokens": tokens,
        "wtok": round(wtok),
        "by_model": {m: round(w, 1) for m, w in by_model.items()},
    }
    events = [AgentEvent(PROVIDER, sid, EventType.TURN_SUMMARY, summary, timestamp=now)]
    nudge = compact_nudge(store, sid, turn, payload.get("transcript_path"), cfg, now)
    if nudge:
        events.append(AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": "compact-nudge"}, timestamp=now))
    auto = autopilot_tip(store, cfg, now)
    if auto:
        events.append(AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": "autopilot"}, timestamp=now))
        nudge = "\n".join(m for m in (nudge, auto) if m)
    if not (adv["enabled"] and adv["turn_receipts"]):
        return events, nudge

    detail = [f"{subs} subagent{'s' if subs != 1 else ''}"] if subs else []
    if tools:
        detail.append(f"{tools} tool calls")
    dstr = f" ({', '.join(detail)})" if detail else ""
    dominant = max(by_model.items(), key=lambda kv: kv[1])[0] if by_model else None
    mark = p.metadata.get("usage_mark") or {}
    latest = {s.window: s for s in store.latest_usage(PROVIDER)}
    cost = store.last_event(sid, EventType.COST)
    fresh_after = (last_ts or p.timestamp) - 1

    # Subscription users get % of their window (their cost figure is only a list-price
    # estimate, not what they pay); API-key users get dollars.
    meters = [m for m in ("five_hour", "spend_limit") if m in mark] or (["usd"] if "usd" in mark else [])
    msg: str | None = None
    for meter in meters:
        if meter == "usd":
            cur_val, cur_ts, reset_ok = (float(cost.metadata.get("usd") or 0), cost.timestamp, True) if cost else (None, None, False)
        else:
            s = latest.get(meter)
            cur_val, cur_ts = (s.used_percent, s.captured_at) if s else (None, None)
            reset_ok = s is not None and _same_window(mark[meter][1], s.reset_at)
        exact = cur_val is not None and reset_ok and cur_ts is not None and cur_ts >= fresh_after
        if exact:
            delta, approx = float(cur_val) - float(mark[meter][0]), ""
        else:
            rate = calibrated_rate(store, "usd" if meter == "usd" else "five_hour", dominant, now) if dominant and meter != "spend_limit" else None
            if rate is None or wtok <= 0:
                continue
            delta, approx = rate[0] * wtok / 1e6, "~"
        threshold = adv["receipt_min_usd"] if meter == "usd" else adv["receipt_min_percent"]
        if delta < threshold:
            break  # measured, and too small to mention
        how = f"; estimated from {fmt_tok(tokens)} tokens, calibrated on this machine" if approx else ""
        if meter == "usd":
            total_usd = float(cost.metadata.get("usd") or 0) if cost else 0.0
            msg = f"RATION: that turn cost {approx}${delta:.2f}{dstr}{how}. Session total ${total_usd:.2f}."
        else:
            s = latest.get(meter)
            now_pct = f" Now {s.used_percent:.0f}% used, resets in {fmt_h(None if s.reset_at is None else max(0.0, (s.reset_at - now) / 3600))}." if s and not approx else ""
            msg = f"RATION: that turn used {approx}{delta:.0f}% of your {WINDOW_LABEL.get(meter, meter)} window{dstr}{how}.{now_pct}"
        break
    else:
        # No % or $ meter at all (e.g. no status line installed): exact tokens still work.
        if wtok >= adv["receipt_min_mtokens"] * 1e6 and dominant:
            msg = f"RATION: that turn used {fmt_tok(tokens)} tokens on {dominant}{dstr}."
    if msg:
        events.append(AgentEvent(PROVIDER, sid, EventType.ADVICE, {"kind": "receipt", "wtok": round(wtok), "subagents": subs}, timestamp=now))
    return events, "\n".join(m for m in (msg, nudge) if m) or None
