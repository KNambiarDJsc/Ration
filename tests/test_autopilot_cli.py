"""CLI behaviour for `ration autopilot` / `ration nightshift`, and backwards compatibility."""

import json

import pytest
from conftest import LINT_CMD, TEST_CMD, git

from ration import cli, config
from ration.autopilot import state
from ration.autopilot.workspace import BRANCH_PREFIX

pytestmark = pytest.mark.integration


@pytest.fixture
def project(repo):
    """The fixture repo with its fake commands configured the way a user would: `.ration.toml`."""
    (repo / ".ration.toml").write_text(f"[autopilot]\ntest_command = '{TEST_CMD}'\nlint_command = '{LINT_CMD}'\nmin_score = 55\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    return repo


def run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_dry_run_shows_the_plan_and_changes_nothing(project, capsys):
    before = git(project, "status", "--porcelain")
    rc, out = run(capsys, "autopilot", "--dry-run", "--provider", "mock", "--max-tasks", "3", "--cwd", str(project))
    assert rc == 0 and "AUTOPILOT PLAN" in out and "Would run, in this order:" in out and "Add tests for src/util.py" in out
    assert "score" in out and "LOW risk" in out and "No changes made. Dry run only: no agent was started." in out
    assert "Goal: none set" in out and "budget unknown" in out.lower()
    assert git(project, "status", "--porcelain") == before and not (project / ".ration").exists()
    assert git(project, "branch", "--list", f"{BRANCH_PREFIX}*") == "" and state.find_run(project) is None


def test_dry_run_explains_why_nothing_will_run_without_a_budget(project, capsys):
    rc, out = run(capsys, "autopilot", "--dry-run", "--provider", "mock", "--cwd", str(project))
    assert rc == 0 and "Autopilot will not run" in out and "--max-tasks" in out and "Deferred" in out


def test_json_dry_run_is_machine_readable(project, capsys):
    rc, out = run(capsys, "autopilot", "--dry-run", "--json", "--provider", "mock", "--max-tasks", "2", "--no-probe", "--cwd", str(project))
    data = json.loads(out)
    assert rc == 0 and data["mode"] == "safe" and data["queue"] and data["tasks"][0]["decision"] == "RUN"
    assert {"score", "risk", "cost", "reasons", "verifiability", "confidence", "category", "source", "files", "evidence"} <= set(data["tasks"][0])
    assert data["budget"]["known"] is False


def test_modes_and_goal_override(project, capsys):
    rc, out = run(capsys, "autopilot", "--dry-run", "--mode", "research", "--provider", "mock", "--max-tasks", "2", "--goal", "Ship the billing MVP", "--cwd", str(project))
    assert rc == 0 and "Mode: research" in out and "Goal: Ship the billing MVP" in out and "Gap analysis against the project goal" in out
    rc, out = run(capsys, "autopilot", "--dry-run", "--mode", "build", "--provider", "mock", "--max-tasks", "2", "--cwd", str(project))
    assert rc == 0 and "Mode: build" in out
    rc, out = run(capsys, "autopilot", "--mode", "yolo", "--cwd", str(project))
    assert rc == 2 and "Unknown mode" in out


def test_nightshift_is_an_alias(project, capsys):
    rc, out = run(capsys, "nightshift", "--dry-run", "--provider", "mock", "--max-tasks", "1", "--cwd", str(project))
    assert rc == 0 and "NIGHTSHIFT PLAN" in out and "AUTOPILOT PLAN" not in out
    rc, out = run(capsys, "nightshift", "status", "--cwd", str(project))
    assert rc == 0 and "No autopilot runs yet" in out


def test_starting_needs_an_explicit_yes(project, capsys):
    rc, out = run(capsys, "autopilot", "--provider", "mock", "--max-tasks", "1", "--cwd", str(project))  # stdin is not a terminal here
    assert rc == 2 and "needs an explicit yes" in out and state.find_run(project) is None


def test_full_run_report_and_status_from_the_cli(project, capsys):
    # the mock provider "works" but changes no files, so the task is honestly reported as no change
    rc, out = run(capsys, "autopilot", "--provider", "mock", "--max-tasks", "1", "--yes", "--seed", "1", "--cwd", str(project))
    assert rc == 0 and "RATION AUTOPILOT REPORT" in out and "NO_CHANGE" in out and "No pushes. No merges. No deployments." in out
    run_id = state.find_run(project)["run_id"]
    assert (state.run_dir(project, run_id) / "report.md").exists() and git(project, "status", "--porcelain") == ""

    rc, out = run(capsys, "autopilot", "report", "--cwd", str(project))
    assert rc == 0 and f"Run {run_id}" in out and "Not accepted:" in out
    rc, out = run(capsys, "autopilot", "report", "--run", run_id[:8], "--json", "--cwd", str(project))
    assert rc == 0 and json.loads(out)["run_id"] == run_id
    rc, out = run(capsys, "autopilot", "status", "--cwd", str(project))
    assert rc == 0 and "COMPLETED" in out and "1 of 1 done" in out
    rc, out = run(capsys, "autopilot", "report", "--run", "nope", "--cwd", str(project))
    assert rc == 1


def test_status_stop_and_report_without_any_run(project, capsys):
    assert run(capsys, "autopilot", "status", "--cwd", str(project)) == (0, f"No autopilot runs yet in {project.name}. Try `ration autopilot --dry-run`.\n")
    rc, out = run(capsys, "autopilot", "stop", "--cwd", str(project))
    assert rc == 0 and "No autopilot run is active" in out
    assert run(capsys, "autopilot", "report", "--cwd", str(project))[0] == 1


def test_active_run_blocks_a_second_start_and_can_be_asked_to_stop(project, capsys):
    state.acquire(project, "20260101-000000-live")
    rc, out = run(capsys, "autopilot", "--provider", "mock", "--max-tasks", "1", "--yes", "--cwd", str(project))
    assert rc == 2 and "already active" in out
    rc, out = run(capsys, "autopilot", "--dry-run", "--provider", "mock", "--max-tasks", "1", "--cwd", str(project))
    assert rc == 0  # looking is always allowed
    rc, out = run(capsys, "autopilot", "stop", "--cwd", str(project))
    assert rc == 0 and "Stop requested for run 20260101-000000-live" in out and state.stop_requested(project, "20260101-000000-live")
    state.release(project, "20260101-000000-live")


def test_reserve_flag_and_verbose_reasons(project, capsys, store):
    from conftest import add_usage

    add_usage(store, "five_hour", 30, reset_in_h=1.5, provider="mock")
    add_usage(store, "seven_day", 50, reset_in_h=30, provider="mock")
    rc, out = run(capsys, "autopilot", "--dry-run", "--provider", "mock", "--verbose", "--cwd", str(project))
    assert rc == 0 and "Safe to spend now: ~" in out and "why: usefulness" in out and "Weekly reserve: 40%" in out
    rc, out = run(capsys, "autopilot", "--dry-run", "--provider", "mock", "--reserve-weekly", "60", "--cwd", str(project))
    assert rc == 0 and "Autopilot will not run" in out and "60% reserve" in out


# ---- backwards compatibility ---------------------------------------------------------------------------
def test_existing_commands_still_parse_and_run(capsys, tmp_path):
    parser = cli.build_parser()
    for argv in (
        ["governor"], ["governor", "status"], ["governor", "apply", "--window", "400000"], ["xray", "--days", "3"], ["handoff", "--copy"],
        ["status", "--json"], ["watch", "--once"], ["plan", "fix", "x"], ["run", "fix", "x", "--check", "pytest"], ["advisor", "on"], ["init", "-y"],
        ["hooks", "status"], ["statusline", "install"], ["guard", "off"], ["sessions"], ["history"], ["providers"], ["doctor"], ["privacy"], ["snippet"],
        ["benchmark"], ["goal"], ["autopilot"], ["nightshift", "status"],
    ):  # fmt: skip
        assert parser.parse_args(argv).cmd == argv[0]
    assert set(cli.COMMANDS) >= {"governor", "xray", "handoff", "status", "watch", "plan", "run", "advisor", "init", "goal", "autopilot", "nightshift"}
    empty = str(tmp_path / "no-sessions")
    assert cli.main(["governor", "--projects-dir", empty]) == 0
    assert cli.main(["governor", "status"]) == 0
    assert cli.main(["xray", "--projects-dir", empty]) == 0
    assert cli.main(["handoff", "--cwd", str(tmp_path)]) == 1  # no session for this folder: says so, as before
    assert cli.main(["status"]) == 0 and cli.main(["watch", "--once"]) == 0 and cli.main(["history"]) == 0
    assert cli.main(["plan", "fix", "the", "flaky", "login", "test", "--provider", "mock"]) == 0
    assert cli.main(["advisor", "off"]) == 0 and cli.main(["advisor", "on"]) == 0
    out = capsys.readouterr().out
    assert "Decision" in out and "advisor: on" in out


def test_old_config_files_get_the_new_defaults(tmp_path):
    home = config.ration_home()
    home.mkdir(parents=True, exist_ok=True)
    # a config written by v0.2: no [autopilot] section, no advisor.autopilot_tip
    config.config_path().write_text('[budget]\nreserve_percent = 25\n\n[advisor]\nenabled = true\nquiet_minutes = 5\n\n[claude]\ncheap = "haiku"\n', encoding="utf-8")
    cfg = config.load()
    assert cfg["budget"]["reserve_percent"] == 25 and cfg["advisor"]["quiet_minutes"] == 5
    assert cfg["autopilot"]["weekly_reserve_percent"] == 40 and cfg["advisor"]["autopilot_tip"] is True
    # saving the defaults (what `ration init` does) still produces a file that loads back identically
    config.save(config.DEFAULTS)
    assert config.load() == config.DEFAULTS


def test_privacy_mentions_where_autopilot_keeps_its_files(capsys):
    assert cli.main(["privacy"]) == 0
    out = capsys.readouterr().out
    assert ".ration/" in out and "Never stored" in out
