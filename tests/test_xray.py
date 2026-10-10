import json
import time
from datetime import UTC, datetime

from ration import advisor, cli, xray
from ration.hooks import claude as hook

NOW = time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def line(msg_id, model, ts, *, inp=10, out=200, cw=0, cr=0, cwd="/work/shop"):
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": iso(ts),
            "requestId": f"r{msg_id}",
            "cwd": cwd,
            "message": {
                "id": msg_id,
                "model": model,
                "content": [{"type": "text", "text": "PRIVATE ANSWER TEXT"}],
                "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr},
            },
        }
    )


def build(root, sessions):
    for sid, (rows, subs) in sessions.items():
        d = root / "proj"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
        for i, srows in enumerate(subs):
            sd = d / sid / "subagents"
            sd.mkdir(parents=True, exist_ok=True)
            (sd / f"agent-{i}.jsonl").write_text("\n".join(srows) + "\n", encoding="utf-8")


def test_xray_finds_long_context_idle_rebuilds_and_subagents(tmp_path):
    t = NOW - 3600
    big = [line(f"a{i}", "claude-opus-5", t + i * 20, cw=2_000 if i else 50_000, cr=300_000 + i * 2_000) for i in range(30)]
    # idle for 2h, then the 200k context is re-created
    big.append(line("rebuild", "claude-opus-5", t + 30 * 20 + 7200, cw=230_000, cr=0))
    small = [line(f"s{i}", "claude-haiku-5", t + i * 5, cr=5_000, cwd="/work/blog") for i in range(10)]
    subs = [[line(f"x{i}", "claude-sonnet-5", t + i, cr=80_000, cw=1_000) for i in range(40)]]
    build(tmp_path, {"big1": (big, subs), "small1": (small, [])})

    r = xray.analyze(tmp_path, days=1, now=NOW + 8000)
    assert r["sessions"] == 2 and r["calls"] == 31 + 10 + 40
    assert r["components"]["cache_read"] > 0.5  # warm context dominates
    assert r["long_context_share"] > 0.5 and r["long_context_saving"] > 0.03
    assert r["recache_events"] == 1 and r["recache_gap_over_60"] == 1 and r["recache_share"] > 0.02
    assert r["subagent_share"] > 0
    kinds = [f["kind"] for f in r["findings"]]
    assert "context" in kinds and "idle" in kinds
    top = r["top_sessions"][0]
    assert top["session"] == "big1" and top["project"] == "shop" and top["peak_ctx"] >= 300_000
    assert "PRIVATE" not in json.dumps(r, default=str)  # content is never read or surfaced


def test_xray_dedupes_and_respects_window(tmp_path):
    old = [line("o1", "claude-sonnet-5", NOW - 40 * 86400, cr=100_000)]
    new = [line("n1", "claude-sonnet-5", NOW - 60, cr=1_000)] * 3  # same message written 3 times
    build(tmp_path, {"s1": (old + new, [])})
    r = xray.analyze(tmp_path, days=7, now=NOW)
    assert r["calls"] == 1


def test_xray_empty_dir_is_not_an_error(tmp_path, capsys):
    assert cli.main(["xray", "--projects-dir", str(tmp_path)]) == 0
    assert "No Claude Code sessions found" in capsys.readouterr().out


def test_xray_cli_renders_findings(tmp_path, capsys):
    t = NOW - 600
    build(tmp_path, {"s": ([line(f"a{i}", "claude-opus-5", t + i, cr=400_000 + i) for i in range(60)], [])})
    assert cli.main(["xray", "--days", "1", "--projects-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "What to change" in out and "Long contexts" in out and "/compact" in out


# ---- resume-cost tip ---------------------------------------------------------------------
RESUME = {
    "hook_event_name": "SessionStart",
    "session_id": "r1",
    "source": "resume",
    "prompt_cache_likely_expired": True,
    "context_tokens": 142_000,
    "seconds_since_last_response": 5 * 3600,
    "estimated_cache_write_usd": 0.89,
}


def test_resume_tip_says_what_the_rebuild_costs(cfg):
    msg = advisor.on_session_start(RESUME, cfg)
    assert "~142k tokens" in msg and "5h00m idle" in msg and "$0.89" in msg and "/clear" in msg


def test_resume_tip_stays_quiet_when_not_useful(cfg):
    assert advisor.on_session_start({**RESUME, "prompt_cache_likely_expired": False}, cfg) is None
    assert advisor.on_session_start({**RESUME, "context_tokens": 12_000}, cfg) is None
    assert advisor.on_session_start({**RESUME, "source": "startup"}, cfg) is None
    cfg["advisor"]["resume_tip"] = False
    assert advisor.on_session_start(RESUME, cfg) is None


def test_resume_tip_is_delivered_by_the_hook(store, cfg):
    out = hook.handle(RESUME, cfg, store)
    assert set(out) == {"systemMessage"} and "rebuilds" in out["systemMessage"]
