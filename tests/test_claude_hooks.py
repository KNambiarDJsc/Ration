"""Event-replay tests for the Claude Code hook handler."""

import json
import subprocess
import sys

from ration import config
from ration.hooks import claude as hook
from ration.models import EventType


def payload(event, **kw):
    return {"hook_event_name": event, "session_id": "sess-1", "cwd": "/repo", "transcript_path": "/t.jsonl", **kw}


def bash_fail(cmd="pytest -q", out="FAILED tests/test_auth.py::test_refresh - AssertionError\n3 failed, 4 passed in 0.52s"):
    return payload("PostToolUseFailure", tool_name="Bash", tool_input={"command": cmd}, tool_use_id="t", error=f"Exit code 1\n{out}")


def test_replay_session_records_normalized_events(store, cfg):
    stream = [
        payload("SessionStart", source="startup", model="claude-sonnet-5"),
        payload("UserPromptSubmit", prompt="secret prompt text"),
        payload("PostToolUse", tool_name="Read", tool_input={"file_path": "/repo/a.py"}, tool_response={"content": "x"}),
        payload("PostToolUse", tool_name="Edit", tool_input={"file_path": "/repo/a.py", "old_string": "a", "new_string": "b"}, tool_response={}),
        bash_fail(),
        payload("PreCompact", trigger="auto"),
        payload("Stop", last_assistant_message="done"),
        payload("StopFailure", error="rate_limit"),
        payload("SessionEnd", reason="other"),
    ]
    for p in stream:
        hook.handle(p, cfg, store)
    types = [e.event_type for e in store.events(session_id="sess-1")]
    for t in (
        EventType.SESSION_STARTED, EventType.MODEL_SELECTED, EventType.PROMPT_SUBMITTED, EventType.FILE_READ,
        EventType.FILE_CHANGED, EventType.TOOL_FAILED, EventType.TEST_RUN, EventType.TEST_RESULT,
        EventType.CONTEXT_COMPACTED, EventType.TURN_ENDED, EventType.BUDGET_WARNING, EventType.SESSION_ENDED,
    ):
        assert t in types, t
    result = next(e for e in store.events(session_id="sess-1") if e.event_type == EventType.TEST_RESULT)
    assert result.metadata["failed"] == 3 and result.metadata["passed"] == 4 and result.metadata["ok"] is False


def test_privacy_no_prompt_or_command_text_stored(store, cfg):
    hook.handle(payload("UserPromptSubmit", prompt="my secret api plan"), cfg, store)
    hook.handle(bash_fail(cmd="pytest -q --token=SECRET123"), cfg, store)
    dump = json.dumps([e.metadata for e in store.events()])
    assert "secret api plan" not in dump and "SECRET123" not in dump


def agent_pre(model=None, desc="search"):
    ti = {"subagent_type": "general-purpose", "description": desc, "prompt": "..."}
    if model:
        ti["model"] = model
    return payload("PreToolUse", tool_name="Agent", tool_input=ti, tool_use_id="x")


def test_observe_mode_never_blocks_but_records_would_block(store, cfg):
    cfg["guard"]["max_premium_subagents"] = 1
    outs = [hook.handle(agent_pre("opus", f"d{i}"), cfg, store) for i in range(3)]
    assert outs == [None, None, None]
    started = [e for e in store.events() if e.event_type == EventType.SUBAGENT_STARTED]
    assert len(started) == 3 and "would_block" in started[-1].metadata


def test_guard_mode_blocks_premium_subagent_explosion(store, cfg):
    cfg["guard"].update(mode="guard", max_premium_subagents=2, max_subagents_per_task=6)
    for i in range(2):
        assert hook.handle(agent_pre("opus", f"d{i}"), cfg, store) is None
    out = hook.handle(agent_pre("opus", "d3"), cfg, store)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "premium subagents" in hso["permissionDecisionReason"]
    assert "sonnet" in hso["permissionDecisionReason"]  # suggests a cheaper model
    # a cheap subagent is still allowed
    assert hook.handle(agent_pre("haiku", "d4"), cfg, store) is None
    assert any(e.event_type == EventType.GUARD_BLOCKED for e in store.events())


def test_guard_mode_blocks_total_explosion(store, cfg):
    cfg["guard"].update(mode="guard", max_subagents_per_task=3)
    outs = [hook.handle(agent_pre("haiku", f"d{i}"), cfg, store) for i in range(5)]
    assert outs[:3] == [None, None, None]
    assert outs[3]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "subagent explosion" in outs[3]["hookSpecificOutput"]["permissionDecisionReason"]


def test_non_agent_pretooluse_is_ignored(store, cfg):
    cfg["guard"]["mode"] = "guard"
    assert hook.handle(payload("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}), cfg, store) is None
    assert store.events() == []


def test_stuck_loop_injects_context_only_in_guard_mode(store, cfg):
    outs = []
    for _ in range(6):
        hook.handle(payload("PostToolUse", tool_name="Edit", tool_input={"file_path": "/repo/a.py"}, tool_response={}), cfg, store)
        outs.append(hook.handle(bash_fail(), cfg, store))
    assert all(o is None for o in outs)  # observe mode: silent
    assert any(e.event_type == EventType.STUCK_DETECTED for e in store.events())

    cfg["guard"]["mode"] = "guard"
    store.purge()
    outs = []
    for _ in range(6):
        outs.append(hook.handle(payload("PostToolUse", tool_name="Edit", tool_input={"file_path": "/repo/a.py"}, tool_response={}), cfg, store))
        outs.append(hook.handle(bash_fail(), cfg, store))
    injected = [o for o in outs if o]
    assert len(injected) == 1  # rate-limited: one warning, not one per tool call
    ctx = injected[0]["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("RATION [") and "same test failure" in ctx


def test_hook_process_fails_open_on_garbage():
    r = subprocess.run([sys.executable, "-I", "-m", "ration.hooks.claude"], input=b"not json{", capture_output=True, timeout=60)
    assert r.returncode == 0 and r.stdout == b""
    assert "JSONDecodeError" in (config.ration_home() / "hook-errors.log").read_text()


def test_hook_process_end_to_end_deny(tmp_path):
    config.save({**config.DEFAULTS, "guard": {**config.DEFAULTS["guard"], "mode": "guard", "max_subagents_per_task": 0}})
    data = json.dumps(agent_pre("haiku")).encode()
    r = subprocess.run([sys.executable, "-I", "-m", "ration.hooks.claude"], input=data, capture_output=True, timeout=60)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
