"""Regressions from running `ration run` against real Claude Code (2026-10-10)."""

import json
import os
import subprocess
import sys

from ration.difficulty import make_task
from ration.models import AgentEvent, EventType, ModelClass
from ration.orchestrator import execute
from ration.providers.base import RunResult
from ration.providers.claude import StreamParser
from ration.providers.mock import MockProvider

EXE = sys.executable.replace("\\", "/")
PASS = f'"{EXE}" -c "raise SystemExit(0)"'
FAIL_COUNTS = f'"{EXE}" -c "print(\'2 failed, 4 passed\'); raise SystemExit(1)"'

REAL_DENIAL = {
    "type": "system",
    "subtype": "permission_denied",
    "tool_name": "Edit",
    "tool_use_id": "toolu_01",
    "decision_reason_type": "safetyCheck",
    "decision_reason": "Claude requested permissions to edit C:\\Users\\x\\.claude\\jobs\\demo\\stats.py which is a sensitive file.",
    "session_id": "f6e5",
}


def test_checks_already_passing_means_nothing_is_spent(store, cfg, tmp_path):
    task, a = make_task("fix the flaky login test", checks=[PASS])
    prov = MockProvider()
    report = execute(task, a, providers={"mock": prov}, store=store, cfg=cfg, cwd=str(tmp_path))
    assert report.completed and report.stopped_reason == ["checks_already_pass"]
    assert prov.calls == []  # no model was run


class Blocked(MockProvider):
    def run(self, prompt, *, policy, run_id, task_id, on_event, **kw):
        self.calls.append(policy)
        on_event(AgentEvent(self.name, "s", EventType.PERMISSION_REQUESTED, {"tool": "Edit", "denied": True}, run_id=run_id, task_id=task_id))
        return RunResult(ok=True, session_id="s", cost_usd=0.008, permission_denials=["safetyCheck"], denial_reason=REAL_DENIAL["decision_reason"])


def test_permission_wall_stops_after_one_attempt(store, cfg, tmp_path):
    task, a = make_task("fix the failing median and percentile tests in stats.py", priority="high", checks=[FAIL_COUNTS])
    seen = []
    prov = Blocked()
    report = execute(task, a, providers={"mock": prov}, store=store, cfg=cfg, cwd=str(tmp_path), on=lambda k, d: seen.append((k, d)))
    assert len(prov.calls) == 1 and report.attempts[0].outcome == "blocked"
    assert report.stopped_reason == ["permission_denied:safetyCheck"]
    assert any(k == "blocked" and "sensitive file" in d for k, d in seen)


class BusyButUseless(MockProvider):
    """Edits files every time (activity) but never improves the checks."""

    def run(self, prompt, *, policy, run_id, task_id, on_event, **kw):
        self.calls.append(policy)
        for i in range(3):
            on_event(AgentEvent(self.name, "s", EventType.FILE_CHANGED, {"path": f"f{i}.py"}, run_id=run_id, task_id=task_id))
        return RunResult(ok=True, session_id="s", cost_usd=0.01)


def test_escalates_when_checks_do_not_improve_despite_activity(store, cfg, tmp_path):
    task, a = make_task("fix the failing median and percentile tests in stats.py", priority="high", checks=[FAIL_COUNTS])
    prov = BusyButUseless()
    execute(task, a, providers={"mock": prov}, store=store, cfg=cfg, cwd=str(tmp_path))
    classes = [p.model_class for p in prov.calls]
    assert classes[0] == ModelClass.CHEAP and classes[1] == ModelClass.STRONG  # no fake "progress" keeps it cheap


def test_stream_permission_denied_event_is_parsed():
    p = StreamParser("r", "t")
    evs = p.feed(REAL_DENIAL)
    assert p.permission_denials == ["safetyCheck"] and "sensitive file" in p.denial_reason
    assert evs[0].event_type == EventType.PERMISSION_REQUESTED and evs[0].metadata["denied"] is True
    assert "sensitive" not in json.dumps(evs[0].metadata)  # reason text is shown, never stored


def test_denied_test_command_is_not_counted_as_a_failing_test():
    p = StreamParser("r", "t")
    p.feed({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "u9", "name": "Bash", "input": {"command": "python -m pytest -q"}}]}})
    p.feed({**REAL_DENIAL, "tool_name": "Bash", "tool_use_id": "u9", "decision_reason_type": "subcommandResults"})
    evs = p.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u9", "is_error": True, "content": "denied"}]}})
    assert evs == []  # no TOOL_FAILED / TEST_RESULT for a call that never ran


def test_hooks_stay_out_of_ration_run_child_sessions(tmp_path):
    env = dict(os.environ, RATION_RUN_ID="run_x", RATION_HOME=str(tmp_path / "h"))
    payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "child", "tool_name": "Bash", "tool_input": {"command": "ls"}}).encode()
    r = subprocess.run([sys.executable, "-I", "-m", "ration.hooks.claude"], input=payload, capture_output=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout == b""
    assert not (tmp_path / "h" / "ration.db").exists()  # nothing recorded twice
