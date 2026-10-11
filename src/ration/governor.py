"""Context governor: choose the auto-compact window that minimizes what long
sessions cost, from your own history, and prove it.

Why this is the lever. Every call re-reads the whole context at the cache-read
rate. A session at 900k tokens pays ~9x per message what a 100k session does,
and Claude Code's default for 1M-window models is to compact at ~967k. After a
compaction the context lands near a fixed size (measured: ~85k), so compacting
earlier trades a one-off cost (summary + rebuild + re-fetching files) for a much
cheaper steady state.

Model (cache-weighted tokens: input 1, output 5, cache write 1.25, cache read 0.1):
  per call:    0.1*ctx  + 1.25*growth + 5*output
  after an idle gap longer than the cache TTL: 1.25*ctx instead of 0.1*ctx
  compaction (ctx >= window): 0.1*ctx + 5*S + 1.25*B, then ctx = B + R
Real growth per call is replayed from your transcripts; only the policy changes.
Parameters (B = post-compaction size) are measured from your own compactions,
and the recommendation must hold across a range of pessimistic assumptions,
because the cheapest window in theory thrashes if B turns out larger.

Read-only on transcripts: only timestamps, models and token counts are used.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .transcript import Call, iter_calls

DEFAULT_WINDOW = 967_000  # Claude Code default for native-1M models
MIN_WINDOW = 100_000  # documented minimum for /autocompact
TTL_S = 3600.0
DEFAULT_B = 85_000  # median post-compaction context measured on a real machine
SUMMARY_OUT = 15_000
CANDIDATES = (150_000, 200_000, 250_000, 300_000, 400_000, 500_000, 700_000)
# (label, B multiplier, tokens re-fetched after each compaction, weight of the compaction
#  request's read of the conversation: 0.1 if it hits the cache, 1.25 if it is a full rebuild)
SCENARIOS = (
    ("optimistic", 0.8, 25_000, 0.1),
    ("measured", 1.0, 25_000, 0.1),
    ("heavy re-fetch", 1.0, 75_000, 0.1),
    ("uncached compaction", 1.0, 25_000, 1.25),
    ("pessimistic", 1.5, 100_000, 1.25),
)
MAX_COMPACTIONS_PER_CALL = 1 / 12  # more often than this is thrashing
MIN_WORTHWHILE_SAVING = 0.15


@dataclass
class Cost:
    total: float = 0.0
    compactions: int = 0
    calls: int = 0


@dataclass
class Row:
    window: int
    by_scenario: dict[str, float]  # saving vs the baseline, per scenario (negative = costs more)
    compactions: int
    thrash: bool

    @property
    def worst(self) -> float:
        return min(self.by_scenario.values())

    @property
    def measured(self) -> float:
        return self.by_scenario["measured"]


@dataclass
class Report:
    sessions: int
    calls: int
    days: float
    current_window: int
    peak_ctx: int
    real_total: float
    model_error: float  # simulated(current window) / real - 1
    post_compaction_b: float
    b_samples: int
    rows: list[Row] = field(default_factory=list)
    recommended: int | None = None
    saving_low: float = 0.0
    saving_measured: float = 0.0
    reason: str = ""
    pricing_note: dict[str, float] | None = None

    @property
    def confident(self) -> bool:
        return abs(self.model_error) <= 0.35 and self.sessions >= 2 and self.calls >= 200


# ---- data -------------------------------------------------------------------------------
def load_threads(root: Path, since: float, min_calls: int = 20, until: float | None = None) -> list[list[Call]]:
    threads: list[list[Call]] = []
    for p in root.glob("*/*.jsonl"):
        try:
            if p.stat().st_mtime < since:
                continue
        except OSError:
            continue
        calls = sorted((c for c in iter_calls(p) if c.ts >= since and (until is None or c.ts < until)), key=lambda c: c.ts)
        if len(calls) >= min_calls:
            threads.append(calls)
    return threads


def real_cost(calls: list[Call]) -> float:
    return sum(0.1 * c.cr + 1.25 * c.cw + c.inp + 5 * c.out for c in calls)


def detect_drops(calls: list[Call]) -> list[tuple[int, int, int]]:
    out = []
    for i in range(1, len(calls)):
        pre, post = calls[i - 1].ctx, calls[i].ctx
        if pre > 100_000 and post < 0.6 * pre:
            out.append((i, pre, post))
    return out


def estimate_b(threads: list[list[Call]]) -> tuple[float, int]:
    posts = [post for t in threads for _, _, post in detect_drops(t)]
    if len(posts) >= 3:
        return float(statistics.median(posts)), len(posts)
    return float(DEFAULT_B), len(posts)


def growth_series(calls: list[Call]) -> list[float | None]:
    out: list[float | None] = []
    prev: Call | None = None
    for c in calls:
        if prev is None:
            out.append(float(c.ctx))
        else:
            d = c.ctx - prev.ctx
            out.append(float(d) if d > -0.4 * prev.ctx else None)  # None: a compaction in the real run
        prev = c
    return out


# ---- simulation ---------------------------------------------------------------------------
def simulate(
    calls: list[Call], window: int, b: float, r: float, s: float = SUMMARY_OUT, ttl: float = TTL_S, read_weight: float = 0.1
) -> Cost:
    ds = growth_series(calls)
    pos = [d for d in ds[1:] if d is not None and d > 0]
    typical = statistics.median(pos) if pos else 1500.0
    cost, ctx, prev_t, n = 0.0, 0.0, None, 0
    b = max(b, 40_000.0)
    for c, d in zip(calls, ds, strict=True):
        gap = (c.ts - prev_t) if prev_t is not None else 0.0
        prev_t = c.ts
        d = typical if d is None else d
        if ctx == 0:
            ctx = d
        else:
            warm = 0.1 * ctx if gap <= ttl else 1.25 * ctx
            cost += warm + 1.25 * max(d, 0.0) + 5 * c.out + c.inp
            ctx += max(d, 0.0)
        if ctx >= window:
            n += 1
            cost += read_weight * ctx + 5 * s + 1.25 * b + 1.25 * r
            ctx = b + r
    return Cost(cost, n, len(calls))


def evaluate(threads: list[list[Call]], window: int, b: float, r: float, read_weight: float = 0.1) -> Cost:
    tot = Cost()
    for t in threads:
        c = simulate(t, window, b, r, read_weight=read_weight)
        tot.total += c.total
        tot.compactions += c.compactions
        tot.calls += c.calls
    return tot


# ---- recommendation -------------------------------------------------------------------------
def recommend(
    threads: list[list[Call]],
    *,
    current_window: int = DEFAULT_WINDOW,
    days: float = 30,
    candidates: tuple[int, ...] = CANDIDATES,
) -> Report:
    calls = sum(len(t) for t in threads)
    peak = max((c.ctx for t in threads for c in t), default=0)
    b_med, b_n = estimate_b(threads)
    real = sum(real_cost(t) for t in threads)
    # If sessions never get anywhere near the window, the setting is moot.
    eff_window = current_window if peak >= 0.5 * current_window else max(current_window, peak)
    base_measured = evaluate(threads, eff_window, b_med, 25_000).total if threads else 0.0
    rep = Report(
        sessions=len(threads), calls=calls, days=days, current_window=current_window, peak_ctx=peak, real_total=real,
        model_error=(base_measured / real - 1) if real else 0.0, post_compaction_b=b_med, b_samples=b_n,
    )
    if not threads or calls < 50:
        rep.reason = "Not enough session history yet (need a few sessions with 20+ calls)."
        return rep
    if peak < 250_000:
        rep.reason = (
            f"Your sessions peak at {peak // 1000}k tokens, so a compaction window above that never triggers. "
            "Nothing to gain here; the lever matters once sessions grow past ~250k."
        )
        return rep

    base = {
        label: evaluate(threads, eff_window, b_med * bm, r, rw).total for label, bm, r, rw in SCENARIOS
    }
    best: Row | None = None
    for w in sorted(set(candidates) | set()):
        if w >= eff_window or w < MIN_WINDOW:
            continue
        by: dict[str, float] = {}
        thrash = False
        comps = 0
        for label, bm, r, rw in SCENARIOS:
            c = evaluate(threads, w, b_med * bm, r, rw)
            by[label] = 1 - c.total / base[label] if base[label] else 0.0
            if c.compactions / max(1, c.calls) > MAX_COMPACTIONS_PER_CALL:
                thrash = True
            if label == "measured":
                comps = c.compactions
        row = Row(w, by, comps, thrash)
        rep.rows.append(row)
        if not thrash and (best is None or row.worst > best.worst + 1e-9 or (abs(row.worst - best.worst) <= 0.02 and row.window > best.window)):
            best = row
    if best is None or best.worst < MIN_WORTHWHILE_SAVING:
        rep.reason = (
            "No window clears the bar across all assumptions. Compacting earlier would not reliably save at least "
            f"{MIN_WORTHWHILE_SAVING:.0%}, so the default is fine for how you work."
        )
        return rep
    rep.recommended, rep.saving_low, rep.saving_measured = best.window, best.worst, best.measured
    rep.reason = (
        f"{best.window // 1000}k is the window that saves the most under the most pessimistic assumptions "
        f"(not the theoretical optimum), and it never makes Claude compact more than once per {int(1 / MAX_COMPACTIONS_PER_CALL)} calls."
    )
    return rep


def realized(root: Path, since_applied: float, *, days_before: float = 30, now: float | None = None) -> dict[str, Any]:
    """The verifier: what actually happened after the window was changed vs before.
    Per-call numbers, because the amount of work differs between periods."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {}
    for label, lo, hi in (("before", since_applied - days_before * 86400, since_applied), ("after", since_applied, now)):
        threads = []
        for t in load_threads(root, lo, min_calls=1):
            sel = [c for c in t if lo <= c.ts < hi]
            if sel:
                threads.append(sel)
        calls = [c for t in threads for c in t]
        n = len(calls)
        drops = sum(len(detect_drops(t)) for t in threads)
        out[label] = {
            "calls": n,
            "avg_context": (sum(c.ctx for c in calls) / n) if n else 0,
            "cost_per_call": (sum(real_cost(t) for t in threads) / n) if n else 0,
            "compactions_per_100_calls": (100 * drops / n) if n else 0,
        }
    return out


def per_message_price(ctx: int, rate_pct_per_mtok: float | None) -> float | None:
    """% of the 5-hour window one more message costs at this context size (re-read + a typical reply)."""
    if not rate_pct_per_mtok:
        return None
    return rate_pct_per_mtok * (0.1 * ctx + 5 * 1_500) / 1e6


def run(
    root: Path, *, days: float = 30, current_window: int = DEFAULT_WINDOW, now: float | None = None, until: float | None = None
) -> Report:
    """`current_window` must be the window that was in effect while the history was
    recorded. After the governor changes the setting, pass `until` = when it changed
    (and the previous window), so the replay is compared against what really happened."""
    now = time.time() if now is None else now
    end = until if until is not None else now
    return recommend(load_threads(root, end - days * 86400, until=until), current_window=current_window, days=days)


def as_dict(r: Report) -> dict[str, Any]:
    return {
        "sessions": r.sessions, "calls": r.calls, "days": r.days, "current_window": r.current_window,
        "peak_context": r.peak_ctx, "real_weighted_usage": r.real_total, "model_error": r.model_error,
        "post_compaction_context": r.post_compaction_b, "compactions_observed": r.b_samples,
        "recommended_window": r.recommended, "saving_low": r.saving_low, "saving_measured": r.saving_measured,
        "reason": r.reason,
        "rows": [{"window": x.window, "saving": x.by_scenario, "compactions": x.compactions, "thrash": x.thrash} for x in r.rows],
    }
