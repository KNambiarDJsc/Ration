"""The four advisor limits, solved with local data only."""

import json
import time
from datetime import UTC, datetime

from ration import advisor, transcript
from ration.budget import ProviderBudget, provider_budgets, window_budget
from ration.difficulty import assess
from ration.hooks import claude as hook
from ration.hooks import statusline
from ration.models import AgentEvent, Confidence, EventType, ResourceType, Source, UsageSnapshot
from ration.normalize import sig

H = 3600.0


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def assistant(msg_id, model, usage, ts, effort="high"):
    return {
        "type": "assistant",
        "timestamp": iso(ts),
        "requestId": f"req_{msg_id}",
        "effort": effort,
        "message": {"id": msg_id, "model": model, "content": [{"type": "text", "text": "SECRET CONTENT"}], "usage": usage},
    }


def write_lines(path, rows, mode="a"):
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


U = {"input_tokens": 1000, "output_tokens": 2000, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 100000}
W = 1000 + 5 * 2000 + 1.25 * 4000 + 0.1 * 100000  # 26_000 weighted


# ---- transcript reader ---------------------------------------------------------------
def test_transcript_dedupes_repeated_lines_and_reads_incrementally(tmp_path):
    p = tmp_path / "t.jsonl"
    now = time.time()
    write_lines(p, [{"type": "user", "message": {"content": "hi"}}, assistant("m1", "claude-opus-5", U, now), assistant("m1", "claude-opus-5", U, now)], "w")
    s1 = transcript.read_slice(str(p))
    assert s1.messages == 1 and s1.by_model["claude-opus-5"].weighted == W  # duplicate line counted once
    assert s1.last_model == "claude-opus-5" and s1.last_effort == "high"
    write_lines(p, [assistant("m2", "claude-sonnet-5", U, now + 5, effort={"level": "low"})])
    s2 = transcript.read_slice(str(p), s1.end_offset)
    assert list(s2.by_model) == ["claude-sonnet-5"] and s2.last_effort == "low"


def test_transcript_ignores_partial_last_line_and_synthetic(tmp_path):
    p = tmp_path / "t.jsonl"
    write_lines(p, [assistant("m1", "<synthetic>", U, time.time())], "w")
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"type": "assistant", "message": {"id": "half')  # still being written
    s = transcript.read_slice(str(p))
    assert s.messages == 0 and s.end_offset < p.stat().st_size


# ---- limit 1: misread prompts ---------------------------------------------------------
def test_no_advice_when_classifier_is_guessing(store, cfg):
    store.add_event(AgentEvent("claude", "s1", EventType.MODEL_SELECTED, {"model": "claude-opus-5"}))
    a = assess("add a dark mode toggle to the settings page")
    assert a.confidence == "low"
    out = advisor.on_prompt({"session_id": "s1", "prompt": "add a dark mode toggle to the settings page"}, cfg, store)
    assert out[2] is None


def test_session_failing_tests_turn_generic_prompt_into_debugging(store, cfg):
    store.add_event(AgentEvent("claude", "s1", EventType.TEST_RESULT, {"ok": False, "failed": 2, "sig": "x"}))
    a = advisor.contextualize(assess("update the checkout handler to use the new client"), store, "s1", "r", time.time())
    assert a.task_type == "debugging" and any("failing test" in r for r in a.reasons)


def test_repo_history_corrects_the_keyword_guess(store, cfg):
    repo = sig("/work/monorepo")
    for i in range(5):  # "small edits" here have historically been heavy
        store.add_event(AgentEvent("claude", f"old{i}", EventType.TURN_SUMMARY, {"repo": repo, "task_type": "trivial_edit", "tools": 55, "wtok": 9e6}))
    store.add_event(AgentEvent("claude", "s1", EventType.MODEL_SELECTED, {"model": "claude-opus-5"}))
    p = {"session_id": "s1", "cwd": "/work/monorepo", "prompt": "fix the typo in the README install section please"}
    _, meta, msg = advisor.on_prompt(p, cfg, store)
    assert msg is None  # no "use haiku" advice for work that is never small in this repo
    a = advisor.contextualize(assess(p["prompt"]), store, "s1", repo, time.time())
    assert any("usually take ~55 tool calls" in r for r in a.reasons)


# ---- limit 2: works without the status line, and for API-key users --------------------
def test_model_and_effort_from_transcript_without_statusline(store, cfg, tmp_path):
    p = tmp_path / "t.jsonl"
    write_lines(p, [assistant("m1", "claude-opus-5", U, time.time(), effort="high")], "w")
    model, effort = advisor.current_model(store, "s1", str(p))
    assert (model, effort) == ("claude-opus-5", "high")
    out = hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(p), "prompt": "fix the typo in the README install section please"}, cfg, store)
    assert "`/model haiku` and `/effort low` save premium quota" in out["systemMessage"]


def test_newer_model_switch_beats_transcript(store, cfg, tmp_path):
    p = tmp_path / "t.jsonl"
    write_lines(p, [assistant("m1", "claude-opus-5", U, time.time() - 60)], "w")
    hook.handle({"hook_event_name": "PostModelSwitch", "session_id": "s1", "from_model": "claude-opus-5", "to_model": "claude-haiku-5"}, cfg, store)
    assert advisor.current_model(store, "s1", str(p))[0] == "claude-haiku-5"


def cost(store, sid, usd, ts):
    store.add_event(AgentEvent("claude", sid, EventType.COST, {"usd": usd}, timestamp=ts))


# The daily budget resets at local midnight, so these tests pin the clock instead of using time.time().
NOON = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))


def spend_16_of_20(store, cfg, now):
    cfg["budget"]["daily_usd"] = 20
    cost(store, "a", 0.0, now - 2 * H)
    cost(store, "a", 6.0, now - 0.5 * H)
    cost(store, "b", 4.0, now)
    cost(store, "a", 12.0, now)  # $16 of $20 today, $10 in the last 30 min
    w = next(w for w in provider_budgets(store, cfg, now=now)["claude"].windows if w.window == "daily_usd")
    assert round(w.used_percent) == 80


def test_api_key_user_gets_dollar_budget_and_pace_alert(store, cfg):
    spend_16_of_20(store, cfg, NOON)
    kind, msg = advisor.budget_alert(provider_budgets(store, cfg, now=NOON))
    assert kind == "pace:daily_usd" and "daily spend budget" in msg


def test_no_pace_alert_when_the_daily_budget_resets_first(store, cfg):
    """The same spend at 23:36: the budget resets in 24 minutes, before this pace would use it up."""
    late = NOON + 11.6 * H
    spend_16_of_20(store, cfg, late)
    assert advisor.budget_alert(provider_budgets(store, cfg, now=late)) is None


def test_api_key_user_dollar_receipt(store, cfg):
    cost(store, "s1", 1.10, time.time() - 5)
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "go"}, cfg, store)
    cost(store, "s1", 1.95, time.time() + 1)
    out = hook.handle({"hook_event_name": "Stop", "session_id": "s1"}, cfg, store)
    assert "that turn cost $0.85" in out["systemMessage"] and "Session total $1.95" in out["systemMessage"]


def test_statusline_records_cost_and_effort(store):
    payload = {"session_id": "s5", "model": {"id": "claude-opus-5"}, "effort": {"level": "xhigh"}, "cost": {"total_cost_usd": 0.42}}
    statusline.record_model(payload, store)
    statusline.record_cost(payload, store)
    statusline.record_cost(payload, store)
    assert store.last_event("s5", EventType.MODEL_SELECTED).metadata["effort"] == "xhigh"
    assert len([e for e in store.events(session_id="s5") if e.event_type == EventType.COST]) == 1


# ---- limit 3: receipts that are exact, or honestly estimated --------------------------
def five_hour(store, used, reset_at, captured_at):
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, used, reset_at, 300, Source.PROVIDER_CLI, Confidence.HIGH, captured_at))


def turn(store, cfg, tpath, sid="s1", model="claude-opus-5", n_msgs=40, prompt="go"):
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": sid, "transcript_path": str(tpath), "prompt": prompt}, cfg, store)
    t = time.time()
    write_lines(tpath, [assistant(f"{sid}-{t}-{i}", model, U, t + i * 0.001) for i in range(n_msgs)])
    return hook.handle({"hook_event_name": "Stop", "session_id": sid, "transcript_path": str(tpath)}, cfg, store)


def test_learns_usage_per_token_then_estimates_when_status_is_stale(store, cfg, tmp_path):
    tpath = tmp_path / "t.jsonl"
    tpath.write_text("", encoding="utf-8")
    reset = time.time() + 3 * H
    used = 10.0
    five_hour(store, used, reset, time.time() - 10)
    # 4 turns, each 40 msgs * 26k weighted = 1.04M wtok; each costs 3% (seen at the next prompt)
    for _ in range(4):
        turn(store, cfg, tpath)
        used += 3.0
        five_hour(store, used, reset, time.time() + 0.5)
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(tpath), "prompt": "go"}, cfg, store)
    cals = store.events_of_type(EventType.CALIBRATION)
    assert len(cals) == 4 and all(abs(c.metadata["rate"] - 3.0 / 1.04) < 0.01 for c in cals)
    # Now a turn whose final usage hasn't reached the status line yet:
    t = time.time()
    write_lines(tpath, [assistant(f"x{i}", "claude-opus-5", U, t + 5 + i * 0.001) for i in range(80)])
    out = hook.handle({"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(tpath)}, cfg, store)
    msg = out["systemMessage"]
    assert "that turn used ~6% of your 5-hour window" in msg and "calibrated on this machine" in msg
    summary = store.last_event("s1", EventType.TURN_SUMMARY)
    assert summary.metadata["wtok"] == round(80 * W) and "SECRET" not in json.dumps(summary.metadata)


def test_calibration_skipped_when_another_session_shares_the_window(store, cfg, tmp_path):
    tpath = tmp_path / "t.jsonl"
    tpath.write_text("", encoding="utf-8")
    reset = time.time() + 3 * H
    five_hour(store, 10, reset, time.time() - 10)
    turn(store, cfg, tpath)
    store.add_event(AgentEvent("claude", "other-terminal", EventType.TOOL_COMPLETED, {"tool": "Bash"}))
    five_hour(store, 14, reset, time.time() + 0.5)
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(tpath), "prompt": "go"}, cfg, store)
    assert store.events_of_type(EventType.CALIBRATION) == []


def test_exact_tokens_receipt_when_no_meter_exists(store, cfg, tmp_path):
    tpath = tmp_path / "t.jsonl"
    tpath.write_text("", encoding="utf-8")
    out = turn(store, cfg, tpath, n_msgs=60)  # 60 * 26k = 1.56M weighted
    assert "that turn used" in out["systemMessage"] and "tokens on claude-opus-5" in out["systemMessage"]


def test_subagent_tokens_come_from_its_own_transcript(store, cfg, tmp_path):
    sub = tmp_path / "agent.jsonl"
    write_lines(sub, [assistant("s1", "claude-haiku-5", U, time.time())], "w")
    hook.handle({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_type": "Explore", "agent_transcript_path": str(sub)}, cfg, store)
    e = store.last_event("s1", EventType.SUBAGENT_ENDED)
    assert e.metadata["by_model"] == {"claude-haiku-5": W} and e.metadata["tokens"] == 107000


# ---- limit 4: effort -------------------------------------------------------------------
def test_effort_only_tip_when_model_is_right(store, cfg, tmp_path):
    p = tmp_path / "t.jsonl"
    write_lines(p, [assistant("m1", "claude-opus-5", U, time.time(), effort="low")], "w")
    _, _, msg = advisor.on_prompt(
        {"session_id": "s1", "transcript_path": str(p), "prompt": "find out why the websocket reconnect occasionally deadlocks"}, cfg, store
    )
    assert msg and "`/effort high` (you're on low)" in msg


# ---- regressions found by running RATION for real (2026-10-10) -----------------------
REAL_RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed", "resetsAt": 1791630600, "rateLimitType": "five_hour", "overageStatus": "rejected",
        "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1791630600}, "seven_day": {"utilization": 0.12, "resetsAt": 1792137600}},
    },
    "session_id": "74abfcee",
}


def test_stream_rate_limit_event_becomes_official_usage():
    from ration.providers.claude import StreamParser

    p = StreamParser("run1", None)
    p.feed(REAL_RATE_LIMIT_EVENT)
    snaps = {s.window: s for s in p.usage}
    assert snaps["five_hour"].used_percent == 20.0 and snaps["seven_day"].used_percent == 12.0
    assert snaps["five_hour"].reset_at == 1791630600 and str(snaps["five_hour"].source) == "provider_cli"


def test_first_prompt_of_new_session_counts_tokens_from_byte_zero(store, cfg, tmp_path):
    tpath = tmp_path / "new-session.jsonl"  # does not exist yet at the first prompt
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "n1", "transcript_path": str(tpath), "prompt": "go"}, cfg, store)
    assert store.last_event("n1", EventType.PROMPT_SUBMITTED).metadata["transcript_offset"] == 0
    write_lines(tpath, [assistant("a1", "claude-haiku-5-5", U, time.time())], "w")
    hook.handle({"hook_event_name": "Stop", "session_id": "n1", "transcript_path": str(tpath)}, cfg, store)
    assert store.last_event("n1", EventType.TURN_SUMMARY).metadata["wtok"] == round(W)


def test_model_tip_deferred_until_first_reply_reveals_model(store, cfg, tmp_path):
    tpath = tmp_path / "s.jsonl"
    prompt = "Investigate why test_median_even and test_percentile_p90 fail in this repo and explain the root cause"
    out = hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "d1", "transcript_path": str(tpath), "prompt": prompt}, cfg, store)
    assert out is None  # model unknown: nothing to compare against yet
    write_lines(tpath, [assistant("a1", "claude-haiku-5-5", U, time.time(), effort="low")], "w")
    tool = {"hook_event_name": "PostToolUse", "session_id": "d1", "transcript_path": str(tpath), "tool_name": "Read", "tool_input": {"file_path": "stats.py"}, "tool_response": {}}
    out = hook.handle(tool, cfg, store)
    assert out and "`/model sonnet`" in out["systemMessage"]
    assert hook.handle(tool, cfg, store) is None  # delivered once


def test_stale_lower_reading_for_same_window_is_ignored(store):
    reset = time.time() + 3 * H
    statusline.record({"rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": reset}}}, store)
    statusline.record({"rate_limits": {"seven_day": {"used_percentage": 1, "resets_at": reset}}}, store)
    assert store.latest_usage("claude")[0].used_percent == 10


# ---- limit 5: will this window absorb the next heavy turn? ----------------------------
HARD = "find and fix why the auth token refresh occasionally deadlocks under load"


def _budget(remaining_percent, *, reserve_percent=20, window="five_hour", hours_to_reset=2.0):
    now = time.time()
    snap = UsageSnapshot("claude", window, ResourceType.USAGE_PERCENT, 100 - remaining_percent, now + hours_to_reset * H, 300, Source.PROVIDER_CLI, Confidence.HIGH, now)
    return ProviderBudget("claude", [window_budget(snap, [], now=now, reserve_percent=reserve_percent)])


def test_capacity_tip_warns_when_remaining_is_below_the_predicted_cost():
    stats = (5, 30.0, 2_000_000.0)  # 5 turns seen, median 2M weighted tokens
    t = advisor.capacity_tip(stats, _budget(remaining_percent=3), rate=5.0, reserve_percent=20, label="debugging")
    assert t is not None and t[0] == "capacity:insufficient" and "may not get a full reply" in t[1]


def test_capacity_tip_warns_when_it_would_eat_into_the_reserve():
    stats = (5, 30.0, 2_000_000.0)  # predicted cost = 5.0 * 2.0 = 10%
    t = advisor.capacity_tip(stats, _budget(remaining_percent=25, reserve_percent=20), rate=5.0, reserve_percent=20, label="debugging")
    assert t is not None and t[0] == "capacity:risky" and "reserve" in t[1]


def test_capacity_tip_is_silent_with_plenty_of_headroom():
    stats = (5, 30.0, 2_000_000.0)  # predicted cost = 10%, 80% remaining, reserve 20%
    assert advisor.capacity_tip(stats, _budget(remaining_percent=80), rate=5.0, reserve_percent=20, label="debugging") is None


def test_capacity_tip_is_silent_below_the_min_percent_threshold():
    stats = (5, 30.0, 10_000.0)  # predicted cost = 5.0 * 0.01 = 0.05%, trivially cheap
    assert advisor.capacity_tip(stats, _budget(remaining_percent=3), rate=5.0, reserve_percent=20, label="debugging", min_percent=3.0) is None


def test_capacity_tip_is_silent_without_history_or_a_calibrated_rate():
    budget = _budget(remaining_percent=3)
    assert advisor.capacity_tip(None, budget, rate=5.0, reserve_percent=20, label="debugging") is None
    assert advisor.capacity_tip((5, 30.0, 2_000_000.0), budget, rate=None, reserve_percent=20, label="debugging") is None
    assert advisor.capacity_tip((5, 30.0, 2_000_000.0), None, rate=5.0, reserve_percent=20, label="debugging") is None


def _seed_capacity_history(store, repo, now):
    for i in range(5):
        store.add_event(AgentEvent("claude", f"h{i}", EventType.TURN_SUMMARY, {"repo": repo, "task_type": "debugging", "tools": 30, "wtok": 2_000_000}, timestamp=now - 3600))
    for i in range(3):
        store.add_event(AgentEvent("claude", f"c{i}", EventType.CALIBRATION, {"meter": "five_hour", "model": "claude-sonnet-5", "rate": 5.0}, timestamp=now - 1800))


def test_on_prompt_warns_live_when_the_window_cannot_absorb_this_turn(store, cfg):
    repo = sig("/work/bigrepo")
    now = time.time()
    _seed_capacity_history(store, repo, now)
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 97, now + 2 * H, 300, Source.PROVIDER_CLI, Confidence.HIGH, now))
    store.add_event(AgentEvent("claude", "s1", EventType.MODEL_SELECTED, {"model": "claude-sonnet-5"}))
    _, _, msg = advisor.on_prompt({"session_id": "s1", "cwd": "/work/bigrepo", "prompt": HARD}, cfg, store, now=now)
    assert msg and "may not get a full reply" in msg


def test_on_prompt_capacity_tip_is_silent_with_low_confidence_prompts(store, cfg):
    repo = sig("/work/bigrepo")
    now = time.time()
    _seed_capacity_history(store, repo, now)
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 97, now + 2 * H, 300, Source.PROVIDER_CLI, Confidence.HIGH, now))
    cfg["advisor"]["budget_alerts"] = cfg["advisor"]["model_suggestions"] = False  # isolate the capacity tip
    _, _, msg = advisor.on_prompt({"session_id": "s1", "cwd": "/work/bigrepo", "prompt": "yes do it"}, cfg, store, now=now)
    assert msg is None  # too short/ambiguous to classify at all: no assessment, no tip


def test_on_prompt_capacity_tip_respects_its_config_toggle(store, cfg):
    repo = sig("/work/bigrepo")
    now = time.time()
    _seed_capacity_history(store, repo, now)
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 97, now + 2 * H, 300, Source.PROVIDER_CLI, Confidence.HIGH, now))
    store.add_event(AgentEvent("claude", "s1", EventType.MODEL_SELECTED, {"model": "claude-sonnet-5"}))
    cfg["advisor"]["capacity_tip"] = False
    cfg["advisor"]["budget_alerts"] = cfg["advisor"]["model_suggestions"] = False  # isolate the capacity tip
    _, _, msg = advisor.on_prompt({"session_id": "s1", "cwd": "/work/bigrepo", "prompt": HARD}, cfg, store, now=now)
    assert msg is None
