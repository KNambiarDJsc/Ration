import json
import time
from datetime import UTC, datetime

from ration import advisor, cli, config, governor
from ration.hooks import claude as hook
from ration.hooks import statusline
from ration.integrations import claude_install as ci
from ration.models import AgentEvent, EventType

NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def line(i, ts, ctx, out=300):
    cw, cr = 2_000, max(0, ctx - 2_010)
    return json.dumps(
        {
            "type": "assistant", "timestamp": iso(ts), "requestId": f"r{i}", "cwd": "/w/shop",
            "message": {"id": f"m{i}", "model": "claude-sonnet-5", "content": [{"type": "text", "text": "SECRET"}],
                        "usage": {"input_tokens": 10, "output_tokens": out, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}},
        }
    )


def sawtooth(path, calls=1_400, growth=1_800, window=900_000, post=85_000, start=NOW - 6 * 3600, step=20):
    """A session that grows ~1.8k tokens per call and compacts at `window` down to `post`."""
    ctx, rows = 40_000, []
    for i in range(calls):
        rows.append(line(i, start + i * step, int(ctx)))
        ctx += growth
        if ctx >= window:
            ctx = post
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def projects(tmp_path, n=2, **kw):
    d = tmp_path / "projects" / "p"
    d.mkdir(parents=True)
    for k in range(n):
        sawtooth(d / f"s{k}.jsonl", **kw)
    return tmp_path / "projects"


def test_compaction_target_is_measured_from_history(tmp_path):
    root = projects(tmp_path)
    threads = governor.load_threads(root, 0)
    b, n = governor.estimate_b(threads)
    assert n >= 3 and 80_000 <= b <= 90_000  # the 85k the sessions actually compacted down to


def test_recommends_a_robust_window_not_the_theoretical_optimum(tmp_path):
    root = projects(tmp_path)
    rep = governor.run(root, days=1, current_window=967_000, now=NOW)
    assert rep.recommended and 250_000 <= rep.recommended <= 500_000
    assert rep.saving_low >= governor.MIN_WORTHWHILE_SAVING and rep.saving_measured >= rep.saving_low
    assert abs(rep.model_error) < 0.35
    # a window so small the compactor can't get below it must be flagged, never recommended
    small = next(r for r in rep.rows if r.window == 150_000)
    assert small.thrash and rep.recommended != 150_000


def test_pessimistic_assumptions_cap_how_low_it_goes(tmp_path):
    root = projects(tmp_path)
    rep = governor.run(root, days=1, now=NOW)
    best = next(r for r in rep.rows if r.window == rep.recommended)
    assert best.worst >= governor.MIN_WORTHWHILE_SAVING  # holds even when B is 1.5x and re-fetch is 100k


def test_no_recommendation_when_sessions_stay_small(tmp_path):
    root = projects(tmp_path, window=180_000, post=60_000)
    rep = governor.run(root, days=1, now=NOW)
    assert rep.recommended is None and "peak" in rep.reason


def test_no_recommendation_without_history(tmp_path):
    rep = governor.run(tmp_path / "none", days=1, now=NOW)
    assert rep.recommended is None and "Not enough" in rep.reason


def test_privacy_only_counts_are_used(tmp_path):
    root = projects(tmp_path)
    out = json.dumps(governor.as_dict(governor.run(root, days=1, now=NOW)), default=str)
    assert "SECRET" not in out and "shop" not in out


# ---- applying and reverting the one official control --------------------------------------
def test_apply_and_revert_preserve_everything_else(tmp_path, capsys):
    p = ci.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    original = {"model": "sonnet", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}, "autoCompactWindow": 800_000}
    p.write_text(json.dumps(original), encoding="utf-8")
    assert cli.main(["governor", "apply", "--window", "300000"]) == 0
    data = json.loads(p.read_text())
    assert data["autoCompactWindow"] == 300_000 and data["model"] == "sonnet" and data["hooks"] == original["hooks"]
    assert list(p.parent.glob("settings.json.ration-bak-*"))
    assert cli.main(["governor", "revert"]) == 0
    assert json.loads(p.read_text())["autoCompactWindow"] == 800_000
    assert cli.main(["governor", "revert"]) == 0
    assert "Nothing to revert" in capsys.readouterr().out


def test_apply_warns_about_precedence_and_validates_range(tmp_path, capsys):
    p = ci.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"modelSettings": {"claude-sonnet-5": {"autoCompactWindow": 500_000}}}), encoding="utf-8")
    assert cli.main(["governor", "apply", "--window", "50000"]) == 2
    assert cli.main(["governor", "apply", "--window", "300000"]) == 0
    assert "take precedence" in capsys.readouterr().out
    assert ci.get_auto_compact_window()["per_model"] == {"claude-sonnet-5": 500_000}


def test_status_verifies_before_and_after(tmp_path, capsys):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    t_apply = NOW - 3600
    sawtooth(root / "old.jsonl", calls=300, start=t_apply - 8 * 3600, growth=3_000, window=900_000)
    sawtooth(root / "new.jsonl", calls=200, start=t_apply, growth=1_000, window=300_000)
    r = governor.realized(tmp_path / "projects", t_apply, now=NOW)
    assert r["before"]["calls"] > 0 and r["after"]["calls"] > 0
    assert r["after"]["avg_context"] < r["before"]["avg_context"]


def test_after_apply_the_replay_still_uses_the_window_history_ran_at(tmp_path, capsys):
    """Regression: once the setting is 400k, the report must not treat 400k as the
    baseline for history that was recorded at the old window."""
    root = projects(tmp_path)  # history recorded at the ~900k default
    p = ci.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    assert cli.main(["governor", "apply", "--window", "400000"]) == 0
    capsys.readouterr()
    assert cli.main(["governor", "--projects-dir", str(root), "--days", "1", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["current_window"] == governor.DEFAULT_WINDOW  # replayed against what really happened
    assert abs(rep["model_error"]) < 0.35 and rep["recommended_window"] is not None
    assert cli.main(["governor", "--projects-dir", str(root), "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "RATION changed it" in out and "your setting" in out


def test_recommendation_survives_an_uncached_compaction_request(tmp_path):
    labels = [s[0] for s in governor.SCENARIOS]
    assert "uncached compaction" in labels and "pessimistic" in labels
    rep = governor.run(projects(tmp_path), days=1, now=NOW)
    best = next(r for r in rep.rows if r.window == rep.recommended)
    assert best.by_scenario["uncached compaction"] > 0 and best.by_scenario["pessimistic"] >= governor.MIN_WORTHWHILE_SAVING


def test_report_cli_runs_on_empty_history(tmp_path, capsys):
    assert cli.main(["governor", "--projects-dir", str(tmp_path / "none")]) == 0
    assert "Not enough" in capsys.readouterr().out


# ---- the price tag and the boundary nudge -------------------------------------------------
def calibrate(store, rate=6.0):
    for i in range(3):
        store.add_event(AgentEvent("claude", f"c{i}", EventType.CALIBRATION, {"meter": "five_hour", "model": "claude-sonnet-5", "rate": rate}))


def test_price_tag_appears_only_when_context_is_large(store, cfg):
    calibrate(store, 6.0)
    small = {"context_window": {"current_usage": {"input_tokens": 5, "cache_creation_input_tokens": 1_000, "cache_read_input_tokens": 40_000}}}
    big = {"context_window": {"current_usage": {"input_tokens": 5, "cache_creation_input_tokens": 2_000, "cache_read_input_tokens": 800_000}}}
    assert statusline.advisor.price_segment(small, store, cfg) is None
    tag = advisor.price_segment(big, store, cfg)
    assert tag.startswith("ctx 802k") and "msg ≈" in tag
    assert advisor.message_price(800_000, 6.0) > 10 * advisor.message_price(80_000, 6.0) * 0.5


def test_price_tag_without_calibration_still_shows_context(store, cfg):
    big = {"context_window": {"current_usage": {"input_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 400_000}}}
    assert advisor.price_segment(big, store, cfg) == "ctx 400k"


def stop_payload(tpath):
    return {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(tpath)}


def big_session(tmp_path, ctx=420_000):
    p = tmp_path / "t.jsonl"
    sawtooth(p, calls=3, growth=0, window=10**9, start=NOW - 120)
    p.write_text(line(1, NOW - 60, ctx) + "\n", encoding="utf-8")
    return p


def test_compact_nudge_fires_at_a_green_boundary_once(store, cfg, tmp_path):
    calibrate(store)
    tpath = big_session(tmp_path)
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(tpath), "prompt": "go"}, cfg, store)
    store.add_event(AgentEvent("claude", "s1", EventType.FILE_CHANGED, {"path": "a.py"}))
    store.add_event(AgentEvent("claude", "s1", EventType.TEST_RESULT, {"ok": True, "passed": 9, "failed": 0}))
    out = hook.handle(stop_payload(tpath), cfg, store)
    assert "good moment to compact" in out["systemMessage"] and "420k" in out["systemMessage"]
    assert "good moment" not in (hook.handle(stop_payload(tpath), cfg, store) or {}).get("systemMessage", "")


def test_compact_nudge_stays_quiet_mid_task_or_when_small(store, cfg, tmp_path):
    tpath = big_session(tmp_path)
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "transcript_path": str(tpath), "prompt": "go"}, cfg, store)
    store.add_event(AgentEvent("claude", "s1", EventType.FILE_CHANGED, {"path": "a.py"}))
    store.add_event(AgentEvent("claude", "s1", EventType.TEST_RESULT, {"ok": False, "passed": 5, "failed": 4}))  # tests red
    assert "good moment" not in (hook.handle(stop_payload(tpath), cfg, store) or {}).get("systemMessage", "")
    cfg["advisor"]["compact_nudge_tokens"] = 0
    store.add_event(AgentEvent("claude", "s1", EventType.TEST_RESULT, {"ok": True, "passed": 9, "failed": 0}))
    assert "good moment" not in (hook.handle(stop_payload(tpath), cfg, store) or {}).get("systemMessage", "")
    assert config.load()["advisor"]["compact_nudge_tokens"] == 250_000
