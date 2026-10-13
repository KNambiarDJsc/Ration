"""Weekly quota is protected before the idle short window is harvested."""

import time

from conftest import add_usage

from ration import advisor
from ration.autopilot import budget as ab
from ration.budget import provider_budgets
from ration.hooks import claude as hook
from ration.models import EventType


def allowance(store, cfg, **kw):
    return ab.assess(provider_budgets(store, cfg).get("claude"), cfg, **kw)


def test_healthy_windows_allow_a_bounded_amount(store, cfg):
    add_usage(store, "five_hour", 32, reset_in_h=1.8)
    add_usage(store, "seven_day", 43, reset_in_h=30)
    a = allowance(store, cfg)
    assert a.allowed and a.known and a.reserve == 40
    # weekly: 57 left - 40 reserve - 5 margin = 12, capped at 10% per run; short: 68 - 10 = 58
    assert a.weekly_spendable == 10 and a.spendable == 58
    assert a.ratio_source == "assumed" and a.confidence == "medium"  # an assumption is never called exact


def test_short_reset_soon_but_weekly_constrained_means_no_optional_work(store, cfg):
    add_usage(store, "five_hour", 10, reset_in_h=0.5)  # 90% of the window about to expire...
    add_usage(store, "seven_day", 65, reset_in_h=80)  # ...but only 35% of the week is left
    a = allowance(store, cfg)
    assert not a.allowed and a.known and "optional work is off" in a.reasons[-1]


def test_weekly_reserve_is_configurable(store, cfg):
    add_usage(store, "five_hour", 10, reset_in_h=1)
    add_usage(store, "seven_day", 65, reset_in_h=30)
    assert allowance(store, cfg, reserve=20).allowed
    cfg["autopilot"]["weekly_reserve_percent"] = 20
    assert allowance(store, cfg).allowed
    assert not allowance(store, cfg, reserve=60).allowed


def test_your_own_weekly_pace_is_protected(store, cfg):
    add_usage(store, "five_hour", 20, reset_in_h=3)
    # 30% used in the last 12h of a week with 100h to go: you will need the rest yourself
    add_usage(store, "seven_day", 20, reset_in_h=100, captured_ago_h=12)
    add_usage(store, "seven_day", 50, reset_in_h=100)
    a = allowance(store, cfg)
    assert not a.allowed and "pace" in " ".join(a.reasons)


def test_projected_own_use_shrinks_the_allowance(store, cfg):
    add_usage(store, "five_hour", 20, reset_in_h=3)
    add_usage(store, "seven_day", 32.5, reset_in_h=100, captured_ago_h=12)
    add_usage(store, "seven_day", 38.5, reset_in_h=100)  # 0.5%/h -> ~50% more by the reset
    a = allowance(store, cfg)
    assert a.allowed and a.weekly_spendable < 10 and any("projected use" in r for r in a.reasons)


def test_reserve_relaxes_only_when_the_weekly_reset_is_near(store, cfg):
    assert ab.effective_reserve(40, 96) == 40 and ab.effective_reserve(40, 12) == 20 and ab.effective_reserve(40, 1) == 10
    add_usage(store, "five_hour", 30, reset_in_h=2)
    add_usage(store, "seven_day", 70, reset_in_h=6)  # 30% left would expire in 6h
    a = allowance(store, cfg)
    assert a.allowed and round(a.reserve, 3) == 10 and any("relaxed" in r for r in a.reasons)


def test_unknown_or_partial_usage_is_never_treated_as_healthy(store, cfg):
    a = allowance(store, cfg)
    assert not a.known and not a.allowed and "no usage data" in a.reasons[0]
    add_usage(store, "five_hour", 5, reset_in_h=1)
    a = allowance(store, cfg)
    assert not a.known and not a.allowed and "weekly" in a.reasons[0]


def test_exhausted_short_window_and_fresh_window(store, cfg):
    add_usage(store, "seven_day", 30, reset_in_h=90)
    a = allowance(store, cfg)  # no five_hour reading: it has reset
    assert a.allowed and any("fresh window" in r for r in a.reasons)
    add_usage(store, "five_hour", 95, reset_in_h=2)
    assert not allowance(store, cfg).allowed


def test_per_run_cap_counts_what_the_run_already_spent(store, cfg):
    add_usage(store, "five_hour", 30, reset_in_h=2)
    add_usage(store, "seven_day", 20, reset_in_h=30)
    assert allowance(store, cfg).weekly_spendable == 10
    assert allowance(store, cfg, spent_weekly=7).weekly_spendable == 3
    assert not allowance(store, cfg, spent_weekly=10).allowed


def test_stale_readings_lower_confidence_and_widen_the_margin(store, cfg):
    add_usage(store, "five_hour", 30, reset_in_h=2)
    add_usage(store, "seven_day", 52, reset_in_h=30)
    fresh, stale = allowance(store, cfg, age_minutes=1), allowance(store, cfg, age_minutes=120)
    assert stale.confidence == "low" and stale.weekly_spendable < fresh.weekly_spendable


def test_weekly_cost_of_the_short_window_is_measured_when_history_allows(store, cfg):
    now = time.time()
    assert ab.window_ratio(store, "claude", "five_hour", "seven_day", now) == (ab.DEFAULT_RATIO, "assumed")
    for i, (short, long) in enumerate([(10, 40.0), (30, 44.0), (60, 50.0)]):
        ago = (2 - i) * 0.5
        add_usage(store, "five_hour", short, reset_in_h=2, captured_ago_h=ago)
        add_usage(store, "seven_day", long, reset_in_h=90, captured_ago_h=ago)
    ratio, src = ab.window_ratio(store, "claude", "five_hour", "seven_day", now)
    assert src == "measured on this machine" and abs(ratio - 0.2) < 1e-6  # 10% of the week per 50% of the window


def test_codex_window_names_map_to_short_and_long(store, cfg):
    add_usage(store, "primary", 20, reset_in_h=2, provider="codex")
    add_usage(store, "secondary", 30, reset_in_h=90, provider="codex")
    a = ab.assess(provider_budgets(store, cfg).get("codex"), cfg, provider="codex")
    assert a.allowed and a.short.window == "primary" and a.long.window == "secondary"


# ---- the in-session suggestion ------------------------------------------------------------------
def turn(store, cfg, sid="s1"):
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "ok"}, cfg, store)
    return hook.handle({"hook_event_name": "Stop", "session_id": sid}, cfg, store)


def test_suggestion_appears_once_when_idle_capacity_is_about_to_expire(store, cfg):
    add_usage(store, "five_hour", 38, reset_in_h=1.5)
    add_usage(store, "seven_day", 43, reset_in_h=90)
    out = turn(store, cfg)
    msg = out["systemMessage"]
    assert "62% of this window remains and resets in ~90m" in msg and "ration autopilot --dry-run" in msg
    assert turn(store, cfg) is None and turn(store, cfg, sid="other") is None  # once per window, across sessions
    kinds = [e.metadata["kind"] for e in store.events(limit=100) if e.event_type == EventType.ADVICE]
    assert kinds.count("autopilot") == 1


def test_no_suggestion_when_the_week_is_tight_far_from_reset_or_switched_off(store, cfg):
    add_usage(store, "five_hour", 38, reset_in_h=1.5)
    add_usage(store, "seven_day", 70, reset_in_h=90)
    assert turn(store, cfg) is None  # weekly below the reserve
    add_usage(store, "seven_day", 30, reset_in_h=90)
    add_usage(store, "five_hour", 38, reset_in_h=4)
    assert advisor.autopilot_tip(store, cfg, time.time()) is None  # the window is not about to reset
    add_usage(store, "five_hour", 70, reset_in_h=1)
    assert advisor.autopilot_tip(store, cfg, time.time()) is None  # not much idle capacity
    add_usage(store, "five_hour", 20, reset_in_h=1)
    assert advisor.autopilot_tip(store, cfg, time.time())
    cfg["advisor"]["autopilot_tip"] = False
    assert advisor.autopilot_tip(store, cfg, time.time()) is None


def test_suggestion_never_starts_anything(store, cfg):
    add_usage(store, "five_hour", 20, reset_in_h=1)
    add_usage(store, "seven_day", 30, reset_in_h=90)
    turn(store, cfg)
    types = {e.event_type for e in store.events(limit=200)}
    assert types <= {EventType.PROMPT_SUBMITTED, EventType.TURN_ENDED, EventType.TURN_SUMMARY, EventType.ADVICE}  # text only
