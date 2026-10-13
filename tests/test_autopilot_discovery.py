"""Opportunity discovery: local, deterministic, and tolerant of broken input."""

import json

from conftest import LINT_CMD, TEST_CMD, git, make_repo

from ration.autopilot import discovery, policy
from ration.autopilot import models as m
from ration.autopilot.goal import Goal
from ration.autopilot.models import Opportunity, Target
from ration.autopilot.verify import detect_checks

SECRET_LINE = "launch_code = 'ACME-PROJECT-X'  # TODO: wire the unreleased ACME thing"
MESSY = {
    "src/app.py": "import os\n\n" + SECRET_LINE + "\n# FIXME: off by one\n\n\ndef add(a, b):\n    return a + b\n",
    "src/stub.py": "def later():\n    raise NotImplementedError\n",
    "src/quiet.py": "def a():\n    try:\n        return 1\n    except:\n        pass\n\n\ndef b():\n    try:\n        return 2\n    except Exception:\n        pass\n",
    "tests/test_app.py": "import pytest\nfrom src.app import add\n\n\n@pytest.mark.skip\ndef test_add():\n    assert add(1, 2) == 3\n",
    "TODO.md": "# Tasks\n- [x] done already\n- [ ] Add a CSV export command\n- [ ] Redesign the whole architecture from scratch\n",
    "README.md": "# demo\n\n## Roadmap\n\n- A dashboard someday\n\n## License\n\n- MIT\n",
}


def found(repo, cfg, packs=("developer", "user"), probe=False, **kw):
    ctx = discovery.scan(repo, cfg, probe=probe, **kw)
    return {o.id: o for o in discovery.discover(ctx, packs)}, ctx


def by_source(opps, source):
    return [o for o in opps.values() if o.source == source]


def test_static_signals_are_found(tmp_path, ap_cfg):
    repo = make_repo(tmp_path / "p", MESSY)
    opps, ctx = found(repo, ap_cfg)
    marker = by_source(opps, "markers")[0]
    assert marker.files == ["src/app.py"] and "2 FIXME" in marker.title and "line(s) 3, 4" in marker.description
    assert by_source(opps, "unfinished")[0].category == m.UNFINISHED
    assert by_source(opps, "skipped_tests")[0].files == ["tests/test_app.py"]
    assert "2 places" in by_source(opps, "error_handling")[0].title
    missing = {o.files[0] for o in by_source(opps, "missing_tests")}
    assert "src/util.py" in missing and "src/app.py" not in missing  # app is referenced by a test
    assert all(o.target == Target.MORE_TESTS for o in by_source(opps, "missing_tests"))
    titles = {o.title for o in by_source(opps, "backlog")}
    assert titles == {"Add a CSV export command", "Redesign the whole architecture from scratch", "A dashboard someday"}  # checked items and other sections are not tasks
    assert ctx.is_git and not ctx.dirty


def test_descriptions_point_at_code_but_never_quote_it(tmp_path, ap_cfg):
    repo = make_repo(tmp_path / "p", MESSY)
    opps, _ = found(repo, ap_cfg)
    dump = json.dumps([o.to_dict() for o in opps.values()])
    assert "ACME" not in dump and "launch_code" not in dump and "off by one" not in dump


def test_discovery_is_deterministic(tmp_path, ap_cfg):
    repo = make_repo(tmp_path / "p", MESSY)
    a, _ = found(repo, ap_cfg)
    b, _ = found(repo, ap_cfg)
    assert list(a) == list(b) and [o.to_dict() for o in a.values()] == [o.to_dict() for o in b.values()]


def test_probes_find_failing_tests_and_lint_issues(tmp_path, ap_cfg):
    repo = make_repo(tmp_path / "p", {"src/bad.py": "x = 1  # BROKEN\ny = 2  # LINTME\nz = 3  # LINTME\n"})
    opps, _ = found(repo, ap_cfg, probe=True)
    tests = by_source(opps, "failing_tests")[0]
    assert tests.title == "Fix 1 failing test" and tests.target == Target.PASS and tests.checks == [TEST_CMD] and tests.verifiability == 1.0
    lint = by_source(opps, m.LINT)[0]
    assert lint.title == "Resolve 2 lint issues" and lint.checks == [LINT_CMD]
    static, _ = found(repo, ap_cfg, probe=False)
    assert not by_source(static, "failing_tests") and not by_source(static, m.LINT)  # a static scan runs nothing


def test_a_clean_repo_offers_no_invented_work(repo, ap_cfg):
    opps, ctx = found(repo, ap_cfg, probe=True)
    # only what is really there: an untested module and undocumented functions. No failures are made up.
    assert {o.source for o in opps.values()} == {"missing_tests", "docs"}
    assert ctx.probes[TEST_CMD].ok and ctx.probes[LINT_CMD].ok


def test_a_check_that_cannot_run_is_a_note_not_a_task(repo, ap_cfg):
    ap_cfg["autopilot"]["test_command"] = "definitely-not-a-real-command-xyz --run"
    opps, ctx = found(repo, ap_cfg, probe=True)
    assert not by_source(opps, "failing_tests") and any("could not run" in n for n in ctx.notes)


def test_dirty_and_untracked_files_are_known(repo, ap_cfg):
    (repo / "src" / "app.py").write_text("# TODO: mine\n", encoding="utf-8")
    (repo / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    _, ctx = found(repo, ap_cfg)
    assert ctx.dirty == {"src/app.py", "scratch.py"} and "scratch.py" not in ctx.files  # untracked files are not scanned


def test_unreadable_and_broken_sources_do_not_stop_discovery(tmp_path, ap_cfg):
    repo = make_repo(tmp_path / "p", {"src/broken.py": "def (:\n    # TODO: fix\n", "src/blob.py": "\x00\x01\x02 # TODO\n"})
    opps, _ = found(repo, ap_cfg)
    assert any(o.files == ["src/broken.py"] for o in by_source(opps, "markers"))


def test_works_without_git(tmp_path, ap_cfg):
    (tmp_path / "plain" / "src").mkdir(parents=True)
    (tmp_path / "plain" / "src" / "a.py").write_text("# TODO: something\n", encoding="utf-8")
    opps, ctx = found(tmp_path / "plain", ap_cfg)
    assert not ctx.is_git and by_source(opps, "markers")


def test_user_defined_tasks_and_malformed_task_file(repo, ap_cfg):
    d = repo / ".ration"
    d.mkdir()
    (d / "tasks.toml").write_text(
        '[[task]]\ntitle = "Keep the changelog current"\ndescription = "Add missing entries."\ncheck = "python tools/lint.py"\nrisk = "low"\n\n'
        '[[task]]\ndescription = "no title: ignored"\n',
        encoding="utf-8",
    )
    opps, _ = found(repo, ap_cfg)
    (task,) = by_source(opps, "user_tasks")
    assert task.category == m.USER and task.checks == ["python tools/lint.py"] and task.target == Target.PASS and task.risk == 0.15
    (d / "tasks.toml").write_text("[[task\n", encoding="utf-8")
    opps, ctx = found(repo, ap_cfg)
    assert not by_source(opps, "user_tasks") and any("tasks.toml is not valid TOML" in n for n in ctx.notes)


def test_research_pack_is_read_only_and_goal_aware(repo, ap_cfg):
    opps, _ = found(repo, ap_cfg, packs=policy.RESEARCH.packs, goal=Goal("Ship the MVP"))
    assert opps and all(o.category == m.RESEARCH and not o.writes_code for o in opps.values())
    assert any("project goal" in o.title for o in opps.values())
    without, _ = found(repo, ap_cfg, packs=policy.RESEARCH.packs)
    assert not any("project goal" in o.title for o in without.values())


def test_packs_are_pluggable_and_isolated_from_each_other(repo, ap_cfg, monkeypatch):
    monkeypatch.setattr(discovery, "SOURCES", dict(discovery.SOURCES))

    def accessibility(ctx):
        return [Opportunity(id="a11y-1", title="Add alt text", description="...", category="accessibility", source="a11y")]

    def broken(ctx):
        raise RuntimeError("boom")

    discovery.register("a11y", accessibility, pack="frontend")
    discovery.register("broken", broken, pack="frontend")
    opps, ctx = found(repo, ap_cfg, packs=("frontend",))
    assert list(opps) == ["a11y-1"] and any("source 'broken' failed" in n for n in ctx.notes)
    assert "a11y-1" not in found(repo, ap_cfg)[0]  # other packs are untouched
    # an unknown category is rejected by every built-in mode until a mode allows it
    assert "not allowed" in policy.gate(opps["a11y-1"], policy.SAFE, has_guards=True)


def test_check_detection_from_project_files(tmp_path, cfg):
    py = tmp_path / "py"
    (py / "tests").mkdir(parents=True)
    (py / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n")
    c = detect_checks(py, cfg)
    assert c.kind == "python" and "pytest" in c.test and "ruff check" in c.lint and "mypy" in c.typecheck and c.guards == [c.test, c.lint]

    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "lint": "eslint .", "typecheck": "tsc --noEmit"}}))
    (node / "pnpm-lock.yaml").write_text("")
    c = detect_checks(node, cfg)
    assert (c.test, c.lint, c.typecheck) == ("pnpm test", "pnpm run lint", "pnpm run typecheck") and any("node_modules" in n for n in c.notes)
    (node / "package.json").write_text(json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert detect_checks(node, cfg).test == ""

    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n")
    assert detect_checks(go, cfg).test == "go test ./..."

    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_checks(empty, cfg).guards == []
    cfg["autopilot"]["test_command"] = "make check"
    assert detect_checks(empty, cfg).test == "make check"  # configuration wins over detection


def test_recently_changed_files_are_worth_more(tmp_path, ap_cfg):
    repo = make_repo(tmp_path / "p", {"src/old.py": "# TODO: a\n", "src/new.py": "# TODO: b\n"})
    for i in range(31):  # push the first commit out of the recent-history window
        (repo / "noise.txt").write_text(str(i))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"n{i}")
    (repo / "src" / "new.py").write_text("# TODO: b\nx = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "touch new")
    opps, _ = found(repo, ap_cfg)
    value = {o.files[0]: o.value for o in by_source(opps, "markers")}
    assert value["src/new.py"] > value["src/old.py"]
