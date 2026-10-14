import json
import time
from datetime import UTC, datetime

from ration import cli, report
from ration.models import Confidence, ResourceType, Source, UsageSnapshot

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
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr},
            },
        }
    )


def build_transcripts(root, rows, session="s1", project="proj"):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_build_bundles_xray_and_budgets(tmp_path, store, cfg):
    t = NOW - 600
    rows = [line(f"a{i}", "claude-opus-5", t + i, cr=400_000 + i) for i in range(40)]
    build_transcripts(tmp_path, rows)
    store.add_usage(UsageSnapshot("claude", "five_hour", ResourceType.USAGE_PERCENT, 42, NOW + 3600, 300, Source.PROVIDER_CLI, Confidence.HIGH, NOW))

    data = report.build(cfg, store, days=1, projects_dir=tmp_path, now=NOW)
    assert data["xray"]["calls"] == 40 and data["xray"]["sessions"] == 1
    assert data["days"] == 1 and data["label"] == ""
    assert data["budgets"]["claude"][0]["used_percent"] == 42


def test_build_uses_the_configured_label(tmp_path, store, cfg):
    cfg["report"]["label"] = "alice"
    data = report.build(cfg, store, days=1, projects_dir=tmp_path, now=NOW)
    assert data["label"] == "alice"


def test_render_html_contains_expected_sections(tmp_path, store, cfg):
    t = NOW - 600
    rows = [line(f"a{i}", "claude-opus-5", t + i, cr=400_000 + i) for i in range(40)]
    build_transcripts(tmp_path, rows)
    data = report.build(cfg, store, days=1, projects_dir=tmp_path, now=NOW)
    html = report.render_html(data)
    assert "<!doctype html>" in html.lower()
    assert "What the tokens were" in html and "By model" in html and "Biggest sessions" in html and "What to change" in html
    assert "re-reading context" in html and "claude-opus-5" in html
    assert "nothing uploaded" in html


def test_render_html_handles_an_empty_window(store, cfg, tmp_path):
    data = report.build(cfg, store, days=1, projects_dir=tmp_path, now=NOW)
    html = report.render_html(data)
    assert "No Claude Code sessions found" in html
    assert "Biggest sessions" not in html  # nothing to show, so the section is omitted


def test_render_html_escapes_the_label_and_project_name(tmp_path, store, cfg):
    cfg["report"]["label"] = "<script>alert(1)</script>"
    build_transcripts(tmp_path, [line("a1", "claude-opus-5", NOW - 60, cr=1000, cwd="/work/<img src=x>")])
    data = report.build(cfg, store, days=1, projects_dir=tmp_path, now=NOW)
    html = report.render_html(data)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img src=x>" not in html


# ---- CLI -----------------------------------------------------------------------------


def test_cli_report_writes_an_html_file(tmp_path, capsys):
    out = tmp_path / "out.html"
    rc = cli.main(["report", "--days", "1", "--projects-dir", str(tmp_path / "projects"), "--out", str(out)])
    assert rc == 0 and out.is_file()
    assert "Wrote" in capsys.readouterr().out
    assert "<!doctype html>" in out.read_text(encoding="utf-8").lower()


def test_cli_report_json(tmp_path, capsys):
    rc = cli.main(["report", "--days", "1", "--projects-dir", str(tmp_path / "projects"), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "xray" in data and "budgets" in data and "days" in data
