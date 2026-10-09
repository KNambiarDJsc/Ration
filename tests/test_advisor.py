import json
import time

from ration import advisor
from ration.budget import provider_budgets
from ration.hooks import claude as hook
from ration.hooks import statusline
from ration.models import AgentEvent, Confidence, EventType, ResourceType, Source, UsageSnapshot

H = 3600.0
TRIVIAL = "please fix the typo in the README install section"
HARD = "find and fix why the auth token refresh occasionally deadlocks under load"


def usage(store, window, used, reset_in_h, captured_ago_h=0.0, minutes=None):
    now = time.time()
    store.add_usage(
        UsageSnapshot(
            "claude", window, ResourceType.USAGE_PERCENT, used, now + reset_in_h * H,
            minutes or {"five_hour": 300, "seven_day": 10080}[window],
            Source.PROVIDER_CLI, Confidence.HIGH, now - captured_ago_h * H,
        )
    )


def on_model(store, model, sid="s1"):
    store.add_event(AgentEvent("claude", sid, EventType.MODEL_SELECTED, {"model": model}))


def prompt(text, sid="s1"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}


def test_suggests_cheaper_model_for_trivial_task_on_opus(store, cfg):
    on_model(store, "claude-opus-5-5")
    _, meta, msg = advisor.on_prompt(prompt(TRIVIAL), cfg, store)
    assert msg and "`/model haiku`" in msg and "small edit" in msg
    assert meta["task_type"] == "trivial_edit"


def test_suggests_premium_for_hard_task_on_sonnet(store, cfg):
    on_model(store, "claude-sonnet-5")
    _, _, msg = advisor.on_prompt(prompt(HARD), cfg, store)
    assert msg and "`/model opus`" in msg and "high effort" in msg and "deadlock" in msg


def test_holds_premium_when_budget_is_tight(store, cfg):
    on_model(store, "claude-sonnet-5")
    usage(store, "seven_day", 90, reset_in_h=72)
    _, _, msg = advisor.on_prompt(prompt(HARD), cfg, store)
    assert msg and "/model opus" not in msg and "90% used" in msg


def test_no_model_tip_for_short_followups_or_unknown_model(store, cfg):
    on_model(store, "claude-opus-5-5")
    assert advisor.on_prompt(prompt("yes do it"), cfg, store)[2] is None
    assert advisor.on_prompt(prompt(TRIVIAL, sid="other-session"), cfg, store)[2] is None  # model unknown


def test_tips_are_throttled_per_session(store, cfg):
    on_model(store, "claude-opus-5-5")
    events, _, first = advisor.on_prompt(prompt(TRIVIAL), cfg, store)
    store.add_events(events)
    _, _, second = advisor.on_prompt(prompt(TRIVIAL), cfg, store)
    assert first and second is None


def test_advisor_off_is_silent(store, cfg):
    on_model(store, "claude-opus-5-5")
    cfg["advisor"]["enabled"] = False
    assert advisor.on_prompt(prompt(TRIVIAL), cfg, store)[2] is None


def test_five_hour_pace_alert(store, cfg):
    usage(store, "five_hour", 60, reset_in_h=2, captured_ago_h=1 / 3)
    usage(store, "five_hour", 75, reset_in_h=2)  # 45%/h, 25% left -> ~33 min
    kind, msg = advisor.budget_alert(provider_budgets(store, cfg))
    assert kind == "pace:five_hour" and "hit your 5-hour limit in ~3" in msg and "resets in 2h00m" in msg
    assert advisor.budget_alert(provider_budgets(store, cfg), short=True)[1].startswith("⚠ 5h limit in ~3")


def test_weekly_burst_does_not_cry_wolf(store, cfg):
    usage(store, "seven_day", 50, reset_in_h=72, captured_ago_h=0.5)
    usage(store, "seven_day", 55, reset_in_h=72)  # 10%/h for 30 min, but weekly pace is ~0.6%/h
    assert advisor.budget_alert(provider_budgets(store, cfg)) is None


def test_use_it_or_lose_it_tip(store, cfg):
    usage(store, "seven_day", 40, reset_in_h=10)
    _, _, msg = advisor.on_prompt(prompt("short"), cfg, store)
    assert msg and "60% left" in msg and "lost at reset" in msg


def test_turn_receipt_and_threshold(store, cfg):
    usage(store, "five_hour", 40, reset_in_h=3)
    reset = store.latest_usage("claude")[0].reset_at
    hook.handle(prompt("refactor the payment retry module into smaller functions"), cfg, store)
    for i in range(2):
        store.add_event(AgentEvent("claude", "s1", EventType.SUBAGENT_STARTED, {"desc_sig": f"x{i}"}))
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 47, reset, 300, Source.PROVIDER_CLI, Confidence.HIGH, time.time() + 1))
    out = hook.handle({"hook_event_name": "Stop", "session_id": "s1"}, cfg, store)
    assert out and "that turn used ~7% of your 5-hour window (2 subagents" in out["systemMessage"]

    cfg["advisor"]["receipt_min_percent"] = 10
    assert hook.handle({"hook_event_name": "Stop", "session_id": "s1"}, cfg, store) is None


def test_prompt_hook_outputs_system_message_and_never_stores_prompt(store, cfg):
    on_model(store, "claude-opus-5-5")
    secret = "fix the typo in README about our unreleased ACME-PROJECT-X launch"
    out = hook.handle(prompt(secret), cfg, store)
    assert set(out) == {"systemMessage"} and out["systemMessage"].startswith("RATION: ")
    dump = json.dumps([e.metadata for e in store.events()])
    assert "ACME" not in dump and "launch" not in dump
    p = store.last_event("s1", EventType.PROMPT_SUBMITTED)
    assert p.metadata["prompt_len"] == len(secret) and "usage_mark" in p.metadata


def test_statusline_records_model_once_and_shows_alert(store, cfg):
    payload = {"session_id": "s9", "model": {"id": "claude-opus-5-5"}}
    statusline.record_model(payload, store)
    statusline.record_model(payload, store)
    models = [e for e in store.events(session_id="s9") if e.event_type == EventType.MODEL_SELECTED]
    assert len(models) == 1
    assert statusline.alert_segment(store, cfg) is None
    usage(store, "five_hour", 60, reset_in_h=2, captured_ago_h=1 / 3)
    usage(store, "five_hour", 75, reset_in_h=2)
    assert statusline.alert_segment(store, cfg).startswith("⚠ 5h limit in ~")
