import json
import os
import subprocess
import time
from datetime import UTC, datetime

from ration import advisor, cli, config, handoff
from ration.hooks import claude as hook

NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def u(text, ts, cwd):
    return {"type": "user", "timestamp": iso(ts), "cwd": cwd, "message": {"role": "user", "content": text}}


def tool_use(i, name, inp, ts, cwd, usage=None):
    return {
        "type": "assistant", "timestamp": iso(ts), "cwd": cwd, "requestId": f"r{i}",
        "message": {"id": f"m{i}", "model": "claude-opus-5", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}],
                    "usage": usage or {"input_tokens": 5, "output_tokens": 50, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 90_000}},
    }


def result(i, text, ts, cwd, is_error=False):
    return {"type": "user", "timestamp": iso(ts), "cwd": cwd,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": text, "is_error": is_error}]}}


def say(text, ts, cwd, i=99):
    return {"type": "assistant", "timestamp": iso(ts), "cwd": cwd, "requestId": f"r{i}",
            "message": {"id": f"m{i}", "model": "claude-opus-5", "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 5, "output_tokens": 80, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 160_000}}}


def session(tmp_path, cwd):
    p = tmp_path / "sess-abc12345.jsonl"
    t = NOW - 600
    rows = [
        u("fix the flaky refresh in src/auth.py, tests in tests/test_auth.py must pass", t, cwd),
        tool_use(1, "Read", {"file_path": f"{cwd}/src/auth.py"}, t + 5, cwd),
        result(1, "def refresh(): ...", t + 6, cwd),
        tool_use(2, "Edit", {"file_path": f"{cwd}/src/auth.py", "old_string": "a", "new_string": "b"}, t + 20, cwd),
        result(2, "ok", t + 21, cwd),
        tool_use(3, "Bash", {"command": "pytest -q tests/test_auth.py"}, t + 30, cwd),
        result(3, "FAILED tests/test_auth.py::test_refresh_race - AssertionError\n2 failed, 9 passed in 1.2s", t + 40, cwd, is_error=True),
        tool_use(4, "Bash", {"command": "rm -rf build"}, t + 50, cwd),
        result(4, "This command requires approval", t + 51, cwd, is_error=True),
        u("<system-reminder>ignore me</system-reminder>", t + 52, cwd),
        u("try a lock around the token cache", t + 60, cwd),
        say("I added a lock; two tests still fail because the refresh future is shared. Next: make the future per-request.", t + 90, cwd),
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_restart_kit_contains_only_what_happened(tmp_path):
    cwd = str(tmp_path / "shop")
    os.makedirs(cwd)
    subprocess.run(["git", "init", "-q", cwd], check=True)
    open(os.path.join(cwd, "new_file.py"), "w").write("x = 1\n")
    p = session(tmp_path, cwd)

    text = handoff.build(p, cwd)
    assert "Restart kit: shop" in text and "context was 160k tokens" in text
    assert "fix the flaky refresh in src/auth.py" in text and "try a lock around the token cache" in text
    assert "ignore me" not in text  # system tags are not the developer's asks
    assert "`src/auth.py`" in text  # relative, changed
    assert "9 passed, 2 failed" in text and "test_refresh_race" in text
    assert "`pytest -q tests/test_auth.py`" in text  # verify command is intact
    assert "1 tool call(s) were denied by permissions" in text
    assert "new_file.py" in text  # real git state
    assert "make the future per-request" in text
    assert len(text) <= 3000


def test_errored_test_run_is_not_reported_as_failing(tmp_path):
    cwd = str(tmp_path)
    p = tmp_path / "s.jsonl"
    rows = [u("run the tests", NOW - 30, cwd), tool_use(1, "Bash", {"command": "pytest -q"}, NOW - 20, cwd),
            result(1, "bash: pytest: command not found", NOW - 19, cwd, is_error=True)]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert "errored before producing a test summary" in handoff.build(p, cwd)


def test_max_chars_is_respected(tmp_path):
    cwd = str(tmp_path)
    p = session(tmp_path, cwd)
    assert len(handoff.build(p, cwd, max_chars=600)) <= 600


def test_find_latest_session_for_folder(tmp_path):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    for name, cwd, age in (("old-1111", a, 500), ("new-2222", a, 100), ("other-3333", b, 10)):
        f = root / f"{name}.jsonl"
        f.write_text(json.dumps(u("hello there", NOW, str(cwd))) + "\n", encoding="utf-8")
        os.utime(f, (NOW - age, NOW - age))
    found = handoff.find_session(str(a), root=tmp_path / "projects")
    assert found.stem == "new-2222"
    assert handoff.find_session(str(a), root=tmp_path / "projects", session="old").stem == "old-1111"


# ---- automatic carry-over -----------------------------------------------------------
def test_clear_carries_kit_into_new_session_only_when_opted_in(store, cfg, tmp_path):
    cwd = str(tmp_path / "shop")
    os.makedirs(cwd)
    p = session(tmp_path, cwd)
    end = {"hook_event_name": "SessionEnd", "session_id": "old", "reason": "clear", "transcript_path": str(p), "cwd": cwd}
    start = {"hook_event_name": "SessionStart", "session_id": "new", "source": "clear", "cwd": cwd}

    hook.handle(end, cfg, store)  # auto is off by default: nothing is written
    assert hook.handle(start, cfg, store) is None
    assert not (config.ration_home() / "handoffs").exists() or not list((config.ration_home() / "handoffs").glob("*.md"))

    cfg["handoff"]["auto"] = True
    hook.handle(end, cfg, store)
    out = hook.handle(start, cfg, store)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Restart kit: shop" in ctx and "9 passed, 2 failed" in ctx
    assert "carried a restart kit" in out["systemMessage"]
    assert hook.handle(start, cfg, store) is None  # used once, then gone


def test_carry_over_ignores_other_sources_and_stale_kits(store, cfg, tmp_path):
    cwd = str(tmp_path / "shop")
    os.makedirs(cwd)
    p = session(tmp_path, cwd)
    cfg["handoff"]["auto"] = True
    hook.handle({"hook_event_name": "SessionEnd", "session_id": "old", "reason": "clear", "transcript_path": str(p), "cwd": cwd}, cfg, store)
    assert hook.handle({"hook_event_name": "SessionStart", "session_id": "n", "source": "startup", "cwd": cwd}, cfg, store) is None
    # stale: older than the TTL
    kit = next((config.ration_home() / "handoffs").glob("*.md"))
    os.utime(kit, (NOW - 3 * 3600, NOW - 3 * 3600))
    assert hook.handle({"hook_event_name": "SessionStart", "session_id": "n", "source": "clear", "cwd": cwd}, cfg, store) is None


def test_other_end_reasons_do_not_write_a_kit(store, cfg, tmp_path):
    cwd = str(tmp_path)
    p = session(tmp_path, cwd)
    cfg["handoff"]["auto"] = True
    hook.handle({"hook_event_name": "SessionEnd", "session_id": "o", "reason": "logout", "transcript_path": str(p), "cwd": cwd}, cfg, store)
    assert not (config.ration_home() / "handoffs").exists() or not list((config.ration_home() / "handoffs").glob("*.md"))


# ---- the nudge ----------------------------------------------------------------------------
def test_large_context_tip_points_at_the_free_way_out(cfg, tmp_path):
    p = session(tmp_path, str(tmp_path))
    kind, msg = advisor.context_tip(str(p), cfg)
    assert kind == "context:large" and "~160k tokens" in msg and "ration handoff --copy" in msg
    cfg["handoff"]["auto"] = True
    assert "Just `/clear`" in advisor.context_tip(str(p), cfg)[1]
    cfg["advisor"]["context_tip_tokens"] = 500_000
    assert advisor.context_tip(str(p), cfg) is None


def test_cli_handoff_prints_and_writes(tmp_path, capsys):
    cwd = str(tmp_path)
    p = session(tmp_path, cwd)
    out = tmp_path / "kit.md"
    assert cli.main(["handoff", "--transcript", str(p), "--cwd", cwd, "--out", str(out)]) == 0
    assert "Restart kit" in out.read_text(encoding="utf-8")
    assert cli.main(["handoff", "--transcript", str(p), "--cwd", cwd]) == 0
    assert "Where it left off" in capsys.readouterr().out
    assert cli.main(["handoff", "--auto", "on"]) == 0
    assert config.load()["handoff"]["auto"] is True
