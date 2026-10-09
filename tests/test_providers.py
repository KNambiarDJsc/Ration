import json
import os
import time

from ration import config
from ration.models import EventType, ModelClass
from ration.providers.claude import StreamParser
from ration.providers.codex import CodexProvider, rate_limit_snapshots, read_rollout


def write_rollout(home, lines):
    d = home / "sessions" / "2026" / "10" / "10"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "rollout-2026-10-10T00-00-00-abc.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return p


def iso(ts):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def test_codex_rollout_rate_limits_and_tools(tmp_path):
    now = time.time()
    home = tmp_path / "codex"
    lines = [
        {"timestamp": iso(now - 60), "type": "session_meta", "payload": {"id": "thread-1", "cwd": "/w/myrepo"}},
        {"timestamp": iso(now - 50), "type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]})}},
        {"timestamp": iso(now - 40), "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"output": "2 failed, 5 passed", "metadata": {"exit_code": 1}})}},
        {"timestamp": iso(now - 35), "type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "input": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch"}},
        {"timestamp": iso(now - 34), "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": "Success"}},
        {"timestamp": iso(now - 30), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 12345}}, "rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": int(now + 3600)}, "secondary": {"used_percent": 55.5, "window_minutes": 10080, "resets_at": int(now + 86400)}}}},
    ]
    write_rollout(home, lines)
    prov = CodexProvider(config.load())
    snaps = {s.window: s for s in prov.usage()}
    assert snaps["secondary"].used_percent == 55.5 and snaps["secondary"].window_minutes == 10080
    assert str(snaps["primary"].source) == "local_log"
    [sess] = prov.sessions()
    assert sess.session_id == "thread-1" and sess.project == "myrepo" and sess.tokens == 12345
    info = read_rollout(next((home / "sessions").rglob("*.jsonl")))
    types = [e.event_type for e in info["events"]]
    assert EventType.TOOL_FAILED in types and EventType.TEST_RESULT in types and EventType.FILE_CHANGED in types
    tr = next(e for e in info["events"] if e.event_type == EventType.TEST_RESULT)
    assert tr.metadata["failed"] == 2 and tr.metadata["runner"] == "pytest"


def test_codex_legacy_resets_in_seconds():
    [s] = rate_limit_snapshots({"primary": {"used_percent": 5, "window_minutes": 300, "resets_in_seconds": 600}}, captured_at=1000.0, now=1000.0)
    assert s.reset_at == 1600.0


def test_codex_ingest_is_deduplicated(tmp_path, store):
    now = time.time()
    write_rollout(tmp_path / "codex", [{"timestamp": iso(now), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": int(now + 100)}}}}])
    prov = CodexProvider(config.load())
    assert prov.ingest_usage(store) == 1
    assert prov.ingest_usage(store) == 0


def test_codex_argv_never_weakens_sandbox(cfg):
    from ration.models import Effort, EscalationPolicy, ExecutionPolicy

    pol = ExecutionPolicy("codex", ModelClass.STRONG, Effort.HIGH, 20, 1, 0, EscalationPolicy.CHEAP_FIRST)
    argv = CodexProvider(cfg).build_argv("do it", pol)
    joined = " ".join(argv)
    assert "--json" in argv and 'model_reasoning_effort="high"' in joined
    assert "bypass" not in joined and "--sandbox" not in joined and "danger" not in joined


def test_claude_stream_parser_maps_tools_and_result():
    p = StreamParser("run1", "task1")
    lines = [
        {"type": "system", "subtype": "init", "session_id": "S", "model": "claude-sonnet-5"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "npm test"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "is_error": True, "content": "Tests: 2 failed, 9 passed"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "u2", "name": "Write", "input": {"file_path": "x.ts", "content": "..."}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u2", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.12, "num_turns": 4, "session_id": "S", "usage": {"input_tokens": 10, "output_tokens": 20}, "result": "done"},
    ]
    evs = [e for line in lines for e in p.feed(line)]
    types = [e.event_type for e in evs]
    assert p.session_id == "S" and p.result["total_cost_usd"] == 0.12
    assert EventType.TOOL_FAILED in types and EventType.FILE_CHANGED in types and EventType.TASK_COMPLETED in types
    tr = next(e for e in evs if e.event_type == EventType.TEST_RESULT)
    assert tr.metadata["failed"] == 2 and tr.metadata["passed"] == 9
    assert all(e.run_id == "run1" for e in evs)


def test_claude_capability_detector_reads_help(tmp_path, monkeypatch):
    """Detect features from --help instead of assuming a version."""
    from ration.providers.claude import ClaudeCapabilityDetector

    script = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    if os.name == "nt":
        script.write_text('@echo off\r\nif "%1"=="--version" goto v\r\necho   -p, --print  --model M  --output-format stream-json\r\ngoto :eof\r\n:v\r\necho 9.9.9 Claude Code\r\n')
    else:
        script.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; else echo "  -p, --print  --model <m>  --output-format stream-json"; fi\n')
        script.chmod(0o755)
    caps = ClaudeCapabilityDetector(str(script)).detect()
    assert caps.installed and caps.version == "9.9.9"
    assert caps.features["print"] and caps.features["stream_json"]
    assert not caps.features["max_turns"] and caps.notes
