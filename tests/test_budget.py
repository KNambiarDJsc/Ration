from ration.budget import provider_budgets, window_budget
from ration.models import BudgetMode, Confidence, ResourceType, Source, UsageSnapshot

H = 3600.0
WEEK_MIN = 7 * 24 * 60


def snap(used, reset_in_h, captured_at, window_minutes=WEEK_MIN, provider="claude"):
    return UsageSnapshot(
        provider=provider,
        window="seven_day",
        resource_type=ResourceType.USAGE_PERCENT,
        used_percent=used,
        reset_at=captured_at + reset_in_h * H,
        window_minutes=window_minutes,
        source=Source.PROVIDER_CLI,
        confidence=Confidence.HIGH,
        captured_at=captured_at,
    )


def test_target_burn_and_pressure_from_recent_samples():
    now = 1_000_000.0
    prev = snap(40, 50.5, now - 0.5 * H)
    cur = snap(45, 50, now)  # 5% in 30 min = 10%/h
    w = window_budget(cur, [prev], now=now)
    assert abs(w.observed_burn - 10.0) < 1e-6
    assert abs(w.target_burn - 55 / 50) < 1e-6  # remaining / hours to reset
    assert w.burn_pressure > 9
    assert w.mode == BudgetMode.CONSERVATIVE
    assert w.depletes_early_hours and w.depletes_early_hours > 40


def test_overspending_is_conservative():
    # remaining 20%, 70% of window left -> overspending (spec section 8)
    now = 2_000_000.0
    reset_in = 0.7 * 168
    w = window_budget(snap(80, reset_in, now), [], now=now, reserve_percent=10)
    assert w.mode == BudgetMode.CONSERVATIVE
    assert w.burn_pressure > 1.25


def test_underutilization_needs_backlog_to_be_aggressive():
    # remaining 70%, 20% of window left -> under-utilization (spec section 8)
    now = 3_000_000.0
    reset_in = 0.2 * 168
    idle = window_budget(snap(30, reset_in, now), [], now=now, has_backlog=False)
    busy = window_budget(snap(30, reset_in, now), [], now=now, has_backlog=True)
    assert idle.mode == BudgetMode.NORMAL and "underutilized_but_no_backlog" in idle.reasons
    assert busy.mode == BudgetMode.AGGRESSIVE


def test_below_reserve_is_conservative():
    now = 4_000_000.0
    w = window_budget(snap(85, 100, now), [], now=now, reserve_percent=20)
    assert w.mode == BudgetMode.CONSERVATIVE
    assert any(r.startswith("below_reserve") for r in w.reasons)


def test_no_history_without_window_is_low_confidence():
    now = 5_000_000.0
    s = snap(50, 10, now, window_minutes=None)
    w = window_budget(s, [], now=now)
    assert w.burn_pressure is None and w.confidence == Confidence.LOW


def test_provider_budgets_skips_expired_windows(store, cfg):
    import time

    now = time.time()
    store.add_usage(snap(10, -1, now - 2 * H))  # already reset
    store.add_usage(snap(20, 5, now, provider="codex"))
    b = provider_budgets(store, cfg, now=now)
    assert "claude" not in b and "codex" in b
