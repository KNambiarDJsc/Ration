"""The autopilot engine end to end, against real git repositories and a scripted agent.

No real model is ever called: `FakeAgent` performs file edits the way an agent
would, inside whatever directory autopilot gives it.
"""

import json
import os
import time
from pathlib import Path

import pytest
from conftest import UTIL, FakeAgent, add_usage, git, make_repo

from ration.autopilot import engine, policy, report, state
from ration.autopilot import goal as goal_mod
from ration.autopilot.goal import Goal
from ration.autopilot.models import Decision
from ration.autopilot.workspace import BRANCH_PREFIX, Workspace, worktrees_root
from ration.models import AgentEvent, EventType, ModelClass
from ration.orchestrator import execute, run_checks
from ration.providers.base import ProviderCapabilities, RunResult
from ration.providers.claude import ClaudeProvider
from ration.providers.mock import MockProvider

pytestmark = pytest.mark.integration

NEW_TESTS = "from src.util import helper1\n\n\ndef test_one():\n    assert helper1(2) == 2\n\n\ndef test_two():\n    assert helper1(0) == 0\n\n\ndef test_three():\n    assert helper1(1) == 1\n"


# References every fixture module, so "missing tests" finds nothing and other tasks can be tested alone.
COVER = "import src.util\nimport src.dirty\n\n\ndef test_cover():\n    assert True\n"


def write_tests(tree: Path, prompt: str = "") -> None:
    name = "extra" if "src/extra.py" in prompt else "more" if "src/more.py" in prompt else "util"
    (tree / "tests" / f"test_{name}.py").write_text(NEW_TESTS.replace("util", name), encoding="utf-8")


def plan_and_run(repo, cfg, store, agent, **kw):
    providers = {agent.name: agent}
    plan = engine.make_plan(repo, cfg, store, providers, max_tasks=kw.pop("max_tasks", 3), **kw)
    return engine.run(plan, cfg, store, providers), plan


def all_branches(repo):
    return git(repo, "branch", "--list", f"{BRANCH_PREFIX}*", "--format=%(refname:short)").split()


def branches(repo):
    """Task branches (without the run's `accepted` branch)."""
    return [b for b in all_branches(repo) if not b.endswith("/accepted")]


def accepted_branch(repo):
    found = [b for b in all_branches(repo) if b.endswith("/accepted")]
    return found[0] if found else None


def leftovers():
    root = worktrees_root()
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# ---- planning ------------------------------------------------------------------------------------
def test_dry_run_plans_without_touching_anything(repo, ap_cfg, store):
    (repo / "README.md").write_text("# demo\nmy uncommitted note\n", encoding="utf-8")
    before = git(repo, "status", "--porcelain")
    agent = FakeAgent(write_tests)
    plan = engine.make_plan(repo, ap_cfg, store, {"mock": agent}, max_tasks=3)
    assert [t.opp.title for t in plan.queue] == ["Add tests for src/util.py"] and plan.estimated_cost == 2.0
    assert agent.calls == [] and git(repo, "status", "--porcelain") == before
    assert not (repo / ".ration").exists() and not leftovers() and branches(repo) == []
    text = report.render_plan(plan, dry_run=True)
    assert "AUTOPILOT PLAN" in text and "No changes made. Dry run only" in text and "1 uncommitted change" in text


def test_plan_is_blocked_when_the_week_is_constrained_even_if_the_window_is_idle(repo, ap_cfg, store):
    add_usage(store, "five_hour", 5, reset_in_h=0.5)
    add_usage(store, "seven_day", 70, reset_in_h=80)
    agent = FakeAgent(write_tests, name="claude")
    plan = engine.make_plan(repo, ap_cfg, store, {"claude": agent})
    assert plan.blocked and "optional work is off" in plan.blocked and plan.queue == []
    assert all(t.decision != Decision.RUN for t in plan.tasks)
    assert "Autopilot will not run" in report.render_plan(plan)


def test_healthy_budget_allows_work_and_a_larger_reserve_blocks_it(repo, ap_cfg, store):
    add_usage(store, "five_hour", 32, reset_in_h=1.8)
    add_usage(store, "seven_day", 43, reset_in_h=30)
    agent = FakeAgent(write_tests, name="claude")
    plan = engine.make_plan(repo, ap_cfg, store, {"claude": agent})
    assert plan.blocked is None and len(plan.queue) == 1 and plan.providers == ["claude"]
    text = report.render_plan(plan)
    assert "32% used, 68% remaining" in text and "Weekly reserve: 40%" in text and "estimate" in text
    assert engine.make_plan(repo, ap_cfg, store, {"claude": agent}, reserve=60).blocked


def test_unknown_budget_needs_an_explicit_cap(repo, ap_cfg, store):
    agent = FakeAgent(write_tests)
    plan = engine.make_plan(repo, ap_cfg, store, {"mock": agent})
    assert plan.blocked and "--max-tasks" in plan.blocked
    assert engine.make_plan(repo, ap_cfg, store, {"mock": agent}, max_tasks=1).blocked is None


def test_no_installed_agent_and_no_git(tmp_path, ap_cfg, store):
    class Missing(FakeAgent):
        def capabilities(self):
            return ProviderCapabilities("mock", False)

    repo = make_repo(tmp_path / "r")
    assert "no coding agent is installed" in engine.make_plan(repo, ap_cfg, store, {"mock": Missing()}, max_tasks=1).blocked
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "a.py").write_text(UTIL)
    assert "not a git repository" in engine.make_plan(plain, ap_cfg, store, {"mock": FakeAgent()}, max_tasks=1).blocked
    assert engine.make_plan(plain, ap_cfg, store, {"mock": FakeAgent()}, max_tasks=1, mode="research").blocked is None


# ---- a successful task -----------------------------------------------------------------------------
def test_accepted_change_lands_on_its_own_branch_and_the_checkout_is_untouched(repo, ap_cfg, store):
    (repo / "README.md").write_text("# demo\nmy uncommitted note\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked", encoding="utf-8")
    goal_mod.save(repo, Goal("Ship the MVP", ["reliability"], ["don't redesign UI"]))
    status_before, head_before = git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD")
    agent = FakeAgent(write_tests)
    final, _ = plan_and_run(repo, ap_cfg, store, agent)

    (res,) = final["results"]
    assert res["outcome"] == "SUCCESS" and res["accepted"] and res["files_changed"] == ["tests/test_util.py"] and res["added"] > 0
    assert "3 more passing tests (2 passed, 0 failed → 5 passed, 0 failed)" in res["detail"]
    # the change is committed on a task branch, and only there
    assert branches(repo) == [res["branch"]] and res["branch"].startswith(f"{BRANCH_PREFIX}{final['run_id']}/01-")
    assert "tests/test_util.py" in git(repo, "show", "--stat", "--format=%s%n%b", res["branch"])
    assert "ration autopilot: Add tests for src/util.py" in git(repo, "log", "-1", "--format=%s", res["branch"])
    # your checkout: same branch, same commit, same uncommitted work
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main" and git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "status", "--porcelain") == status_before and not (repo / "tests" / "test_util.py").exists()
    assert (repo / "README.md").read_text() == "# demo\nmy uncommitted note\n" and (repo / "notes.txt").read_text() == "untracked"
    assert final["checkout_untouched"] and git(repo, "remote") == "" and not leftovers()
    # the agent worked in the worktree, under the rules, with the goal, without bypassing permissions
    assert os.path.normcase(str(worktrees_root())) in os.path.normcase(agent.cwds[0]) and agent.kwargs[0]["permission_mode"] == "acceptEdits"
    assert "Never push" in agent.prompts[0] and "Project goal: Ship the MVP" in agent.prompts[0] and "don't redesign UI" in agent.prompts[0]
    # state, report and history are on disk
    saved = state.load_run(repo, final["run_id"])
    assert saved["status"] == "completed" and saved["results"][0]["commit"] == res["commit"] and state.lock_holder(repo) is None
    text = (state.run_dir(repo, final["run_id"]) / "report.md").read_text(encoding="utf-8")
    assert "RATION AUTOPILOT REPORT" in text and "✓ Add tests for src/util.py" in text and res["branch"] in text
    assert "base commit  `python tools/pytest.py`: 2 passing, 0 failing" in text and "combined     `python tools/pytest.py`: 5 passing, 0 failing" in text
    assert "OK: no check is worse than at the base commit" in text and "No pushes. No merges. No deployments." in text
    # the run's `accepted` branch is the same commit: everything accepted, together
    assert git(repo, "rev-parse", accepted_branch(repo)).strip() == res["commit"] == final["combined"]["commit"] and final["combined"]["ok"]
    assert state.History(repo).entries[-1]["accepted"] is True


def test_finished_work_is_not_redone_and_rejected_work_cools_down(repo, ap_cfg, store):
    plan_and_run(repo, ap_cfg, store, FakeAgent(write_tests))
    again = engine.make_plan(repo, ap_cfg, store, {"mock": FakeAgent()}, max_tasks=3)
    assert again.queue == [] and "awaiting your review" in again.tasks[0].reasons[0]

    other = make_repo(repo.parent / "other")
    plan_and_run(other, ap_cfg, store, FakeAgent())  # the agent does nothing
    again = engine.make_plan(other, ap_cfg, store, {"mock": FakeAgent()}, max_tasks=3)
    assert again.queue == [] and "cooling down" in again.tasks[0].reasons[0]
    assert "Nothing worthwhile" in report.render_plan(again)


# ---- rejected tasks ----------------------------------------------------------------------------------
def test_regression_is_discarded_and_the_next_task_still_runs(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"src/extra.py": UTIL})

    def agent_edit(tree, prompt):
        write_tests(tree, prompt)
        if "src/extra.py" in prompt:
            (tree / "src" / "app.py").write_text((tree / "src" / "app.py").read_text() + "\n# BROKEN\n", encoding="utf-8")

    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(agent_edit))
    by_title = {r["title"]: r for r in final["results"]}
    bad, good = by_title["Add tests for src/extra.py"], by_title["Add tests for src/util.py"]
    assert bad["outcome"] == "REGRESSION" and not bad["accepted"] and "more failing test" in bad["detail"]
    assert good["outcome"] == "SUCCESS" and good["accepted"]
    assert branches(repo) == [good["branch"]]  # the bad branch is gone; the good one does not contain the bad change
    assert "BROKEN" not in git(repo, "show", f"{good['branch']}:src/app.py")
    patch = Path(bad["artifact"]).read_text(encoding="utf-8")
    assert "Discarded by RATION autopilot" in patch and "+# BROKEN" in patch  # the evidence is kept
    assert not leftovers() and git(repo, "status", "--porcelain") == ""
    text = report.render_report(final)
    assert "Not accepted:" in text and "Regressions caught and discarded: 1" in text


def test_no_change_and_agent_failure_are_not_accepted(repo, ap_cfg, store):
    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent())
    assert final["results"][0]["outcome"] == "NO_CHANGE" and branches(repo) == [] and not leftovers()

    other = make_repo(repo.parent / "other")

    def useless(tree, prompt):
        (tree / "src" / "util.py").write_text(UTIL + "\n# touched\n", encoding="utf-8")

    final, _ = plan_and_run(other, ap_cfg, store, FakeAgent(useless))
    res = final["results"][0]
    assert res["outcome"] == "FAILED" and "no additional passing tests" in res["detail"] and branches(other) == []


@pytest.mark.parametrize(
    ("path", "content", "why"),
    [
        (".env", "DEBUG=1\n", "protected path"),
        ("db/migrations/0002_x.py", "x = 1\n", "protected path"),
        ("poetry.lock", "# lock\n", "lockfile"),
        ("tests/test_keys.py", "KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n\n\ndef test_k():\n    assert KEY\n", "credential"),
    ],
)
def test_safety_policy_discards_changes_the_agent_should_not_make(repo, ap_cfg, store, path, content, why):
    def edit(tree, prompt):
        write_tests(tree)
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(content, encoding="utf-8")

    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(edit))
    res = final["results"][0]
    assert res["outcome"] == "BLOCKED" and not res["accepted"] and why in res["detail"] and "safety policy" in res["detail"]
    assert branches(repo) == [] and not (repo / path).exists()
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in Path(res["artifact"]).read_text(encoding="utf-8")  # stored evidence is redacted
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(final)


def test_deleting_files_and_oversized_changes_are_discarded(repo, ap_cfg, store):
    def edit(tree, prompt):
        write_tests(tree)
        (tree / "src" / "app.py").unlink()

    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(edit))
    assert final["results"][0]["outcome"] == "BLOCKED" and "deleted 1 file" in final["results"][0]["detail"]


def test_fix_tasks_succeed_improve_or_fail_by_measurement(tmp_path, ap_cfg, store):
    files = {"src/dirty.py": "a = 1  # LINTME\nb = 2  # LINTME\n", "tests/test_cover.py": COVER}

    def fixer(n):
        calls = []

        def edit(tree, prompt):
            calls.append(1)
            p = tree / "src" / "dirty.py"
            fixed = n if len(calls) == 1 else 0  # a retry in the same worktree gets no further
            p.write_text(p.read_text().replace("  # LINTME", "", fixed) + "# edited\n", encoding="utf-8")

        return edit

    for n, outcome, accepted, calls in ((2, "SUCCESS", True, 1), (1, "IMPROVED", True, 2), (0, "FAILED", False, 2)):
        repo = make_repo(tmp_path / f"r{n}", files)
        agent = FakeAgent(fixer(n))
        final, plan = plan_and_run(repo, ap_cfg, store, agent, max_tasks=1)
        res = final["results"][0]
        assert plan.queue[0].opp.title == "Resolve 2 lint issues"
        assert (res["outcome"], res["accepted"], len(agent.calls)) == (outcome, accepted, calls), res["detail"]
        assert "Done means these commands pass" in agent.prompts[0]  # the existing run loop owns the retry
        assert len(branches(repo)) == int(accepted)


def test_problem_only_in_uncommitted_work_spends_nothing(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"tests/test_cover.py": COVER})
    (repo / "src" / "wip.py").write_text("x = 1  # LINTME\n", encoding="utf-8")  # untracked: not in the last commit
    agent = FakeAgent()
    final, _ = plan_and_run(repo, ap_cfg, store, agent, max_tasks=1)
    res = final["results"][0]
    assert res["outcome"] == "NO_CHANGE" and "already passing at the commit the task starts from" in res["detail"] and agent.calls == []
    assert (repo / "src" / "wip.py").exists()


def test_check_that_cannot_run_blocks_the_task_before_any_spend(repo, ap_cfg, store):
    plan = engine.make_plan(repo, ap_cfg, store, {"mock": FakeAgent()}, max_tasks=1)
    ap_cfg["autopilot"]["test_command"] = "definitely-not-a-real-command-xyz"
    plan.checks.test = "definitely-not-a-real-command-xyz"
    agent = FakeAgent(write_tests)
    final = engine.run(plan, ap_cfg, store, {"mock": agent})
    assert final["results"][0]["outcome"] == "BLOCKED" and "cannot run in a clean worktree" in final["results"][0]["detail"] and agent.calls == []


def test_verified_change_is_kept_when_the_commit_is_refused(repo, ap_cfg, store, monkeypatch):
    monkeypatch.setattr(Workspace, "commit", lambda self, tree, message: None)  # e.g. a failing pre-commit hook
    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(write_tests))
    res = final["results"][0]
    assert res["accepted"] and res["commit"] is None and "left uncommitted in the worktree" in res["detail"]
    assert (Path(res["worktree"]) / "tests" / "test_util.py").exists() and branches(repo) == [res["branch"]]


# ---- budget while running ----------------------------------------------------------------------------
def test_run_stops_when_the_weekly_budget_stops_being_safe(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"src/extra.py": UTIL})
    add_usage(store, "five_hour", 20, reset_in_h=2)
    add_usage(store, "seven_day", 40, reset_in_h=30)

    def edit(tree, prompt):
        write_tests(tree, prompt)
        add_usage(store, "five_hour", 23, reset_in_h=2)  # official readings arriving during the task
        add_usage(store, "seven_day", 62, reset_in_h=30)  # someone else used the week meanwhile

    agent = FakeAgent(edit, name="claude")
    final, plan = plan_and_run(repo, ap_cfg, store, agent)
    assert len(plan.queue) == 2 and len(agent.calls) == 1 and len(final["results"]) == 1
    assert final["status"] == "stopped" and final["stop_reason"].startswith("budget:") and "optional work is off" in final["stop_reason"]
    assert final["results"][0]["cost_percent"] == 3.0  # measured from the official window readings
    deferred = [t for t in final["plan"] if t["decision"] == "DEFER"]
    assert deferred and "not started: budget" in deferred[0]["reasons"][0]
    text = report.render_report(final)
    assert "Weekly: 40% → 62% used" in text and "Stopped because: budget" in text
    assert state.History(repo).entries[-1]["cost_percent"] == 3.0


def test_scheduler_and_provider_get_autopilot_guardrails(repo, ap_cfg, store):
    add_usage(store, "five_hour", 20, reset_in_h=2)
    add_usage(store, "seven_day", 40, reset_in_h=30)
    agent = FakeAgent(write_tests, name="claude")
    plan = engine.make_plan(repo, ap_cfg, store, {"claude": agent})
    runner = engine.Runner(plan, ap_cfg, store, {"claude": agent})
    assert runner.cfg["budget"]["reserve_percent"] == 40 and ap_cfg["budget"]["reserve_percent"] == 20  # a copy; your config is not changed
    assert "Bash(git push:*)" in runner.cfg["claude"]["disallowed_tools"] and "disallowed_tools" not in ap_cfg["claude"]
    final = runner.run()
    attempt = final["results"][0]["attempts"][0]
    assert attempt["provider"] == "claude" and attempt["model_class"] == "cheap" and agent.calls[0].model_class == ModelClass.CHEAP  # the existing scheduler chose

    prov = ClaudeProvider(runner.cfg)
    prov._caps = ProviderCapabilities("claude", True, "2.1.296", {"model": True, "permission_mode": True, "disallowed_tools": True})
    argv = prov.build_argv("do it", agent.calls[0], permission_mode="acceptEdits")
    denied = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(git push:*)" in denied and "Bash(terraform:*)" in denied and "bypassPermissions" not in argv
    plain = ClaudeProvider(ap_cfg)
    plain._caps = prov._caps
    assert "--disallowedTools" not in plain.build_argv("x", agent.calls[0])  # plain `ration run` is unchanged


def test_multiline_prompt_goes_to_stdin_for_a_windows_batch_shim(monkeypatch):
    """cmd.exe ends a command line at the first newline. Found by a real run: the prompt was
    cut short and `--output-format stream-json` after it was lost."""
    import ration.providers.claude as claude_mod

    prompt = "Rules:\n- stay in the worktree\n\nTask: add tests"
    shim = ["C:\\npm\\claude.CMD", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", "haiku"]
    native = ["C:\\bin\\claude.exe", *shim[1:]]
    monkeypatch.setattr(claude_mod.os, "name", "nt")
    argv, stdin = ClaudeProvider.prompt_on_stdin(shim, prompt)
    assert stdin == prompt and prompt not in argv and argv[1:4] == ["-p", "--output-format", "stream-json"] and argv[-1] == "haiku"
    assert ClaudeProvider.prompt_on_stdin(native, prompt) == (native, None)
    monkeypatch.setattr(claude_mod.os, "name", "posix")
    assert ClaudeProvider.prompt_on_stdin(shim, prompt) == (shim, None)


# ---- control: locks, stop, limits, interruption ----------------------------------------------------------
def test_second_run_in_the_same_project_is_refused(repo, ap_cfg, store):
    state.acquire(repo, "someone-else")  # held by this (live) process
    agent = FakeAgent(write_tests)
    plan = engine.make_plan(repo, ap_cfg, store, {"mock": agent}, max_tasks=1)
    with pytest.raises(state.AlreadyRunning) as e:
        engine.run(plan, ap_cfg, store, {"mock": agent})
    assert "someone-else" in str(e.value) and agent.calls == [] and branches(repo) == []
    assert state.lock_holder(repo)["run_id"] == "someone-else"  # the other run's lock is left alone
    state.release(repo, "someone-else")
    assert state.lock_holder(repo) is None


def test_lock_left_by_a_dead_process_is_taken_over(repo, ap_cfg, store):
    import socket

    state.autopilot_dir(repo, create=True)
    state.write_json(state.lock_path(repo), {"run_id": "dead", "pid": 2_000_000_000, "host": socket.gethostname()})
    assert state.lock_holder(repo) is None and not state.pid_alive(2_000_000_000) and state.pid_alive(os.getpid())
    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(write_tests))
    assert final["results"][0]["accepted"]


def test_stop_request_ends_the_run_after_the_current_task(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"src/extra.py": UTIL})

    def edit(tree, prompt):
        write_tests(tree, prompt)
        state.request_stop(repo, state.lock_holder(repo)["run_id"])  # `ration autopilot stop` from another terminal

    agent = FakeAgent(edit)
    final, _ = plan_and_run(repo, ap_cfg, store, agent)
    assert final["status"] == "stopped" and final["stop_reason"] == "stop requested" and len(agent.calls) == 1
    assert final["results"][0]["accepted"] and state.lock_holder(repo) is None  # the finished task keeps its result


class LoopingAgent(MockProvider):
    """Keeps emitting events until it is told to stop, like a real stream."""

    def __init__(self):
        super().__init__()
        self.cancelled = False

    def run(self, prompt, *, policy, cwd, run_id, task_id, on_event, **kw):
        self.calls.append(policy)
        for i in range(200):
            time.sleep(0.002)
            if not on_event(AgentEvent(self.name, "loop", EventType.FILE_READ, {"path": f"f{i}.py"}, run_id=run_id, task_id=task_id)):
                self.cancelled = True
                return RunResult(ok=False, session_id="loop", cancelled=True)
        return RunResult(ok=True, session_id="loop")


def test_task_time_limit_cancels_a_runaway_agent(repo, ap_cfg, store, monkeypatch):
    agent = LoopingAgent()
    real_start = engine.Control.start_task
    # the limit expires while the agent is working (after the baseline, before it finishes)
    monkeypatch.setattr(engine.Control, "start_task", lambda self, deadline: real_start(self, 0))
    monkeypatch.setattr(engine.GuardedProvider, "run", _run_then_expire(engine.GuardedProvider.run))
    final, _ = plan_and_run(repo, ap_cfg, store, agent, max_tasks=1)
    res = final["results"][0]
    assert agent.cancelled and len(agent.calls) == 1  # cancelled mid-flight, and the retry did not start another agent
    assert res["outcome"] == "BLOCKED" and "task time limit reached" in res["detail"] and all_branches(repo) == []


def _run_then_expire(real_run):
    def run(self, prompt, **kw):
        if not self.inner.calls:  # first attempt: let it start, then let the clock run out
            self.control.task_deadline = time.time() + 0.05
        return real_run(self, prompt, **kw)

    return run


def test_run_time_limit_stops_the_run(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"src/extra.py": UTIL})
    agent = LoopingAgent()
    providers = {"mock": agent}
    plan = engine.make_plan(repo, ap_cfg, store, providers, max_tasks=3)
    final = engine.run(plan, ap_cfg, store, providers, max_runtime_minutes=0.00001)
    assert final["status"] == "stopped" and final["stop_reason"] == "run time limit reached" and len(agent.calls) <= 1


def test_stuck_agent_is_interrupted_by_the_existing_detector(repo, ap_cfg, store):
    agent = MockProvider(success={mc: 0.0 for mc in ModelClass})  # loops on the same failing test
    final, _ = plan_and_run(repo, ap_cfg, store, agent, max_tasks=1)
    res = final["results"][0]
    assert res["stuck_interrupts"] >= 1 and res["outcome"] == "NO_CHANGE" and not res["accepted"]
    assert 1 <= len(res["attempts"]) <= policy.SAFE.max_attempts
    # a run that produced nothing must not be remembered as a success for this model
    assert store.provider_success("mock", "cheap") == (0, len(res["attempts"]))
    assert "Stuck runs interrupted: " in report.render_report(final) and "Stuck runs interrupted: 0" not in report.render_report(final)


def test_run_stops_after_repeated_fruitless_tasks(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {f"src/{n}.py": UTIL for n in ("extra", "more", "another")})
    agent = FakeAgent()  # never changes anything
    final, plan = plan_and_run(repo, ap_cfg, store, agent, max_tasks=5)
    assert len(plan.queue) == 4 and len(final["results"]) == 3 and "in a row produced nothing" in final["stop_reason"]


def test_checkout_changing_during_a_task_stops_the_run_and_reverts_nothing(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"src/extra.py": UTIL})

    def escape(tree, prompt):
        write_tests(tree, prompt)
        (repo / "src" / "app.py").write_text("# written outside the worktree\n", encoding="utf-8")

    agent = FakeAgent(escape)
    final, _ = plan_and_run(repo, ap_cfg, store, agent)
    assert not final["checkout_untouched"] and "your checkout changed" in final["stop_reason"] and len(agent.calls) == 1
    assert (repo / "src" / "app.py").read_text() == "# written outside the worktree\n"  # reported, never "fixed" behind your back
    assert "WARNING: your checkout or refs changed" in report.render_report(final)


def test_interrupted_run_is_recovered_without_losing_finished_work(repo, ap_cfg, store):
    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(write_tests))
    kept = final["results"][0]["branch"]
    # simulate a run that died mid-task: state says running, its process is gone, a worktree is left behind
    ws = Workspace(repo, "20260101-000000-dead")
    tree = ws.create("02-half-done")
    (tree.path / "junk.py").write_text("x = 1\n")
    dead = {**final, "run_id": ws.run_id, "status": "running", "pid": 2_000_000_000, "results": [{**final["results"][0], "branch": kept}]}
    state.save_run(repo, dead)
    assert state.effective_status(repo, state.load_run(repo, ws.run_id)) == "interrupted"

    notes = engine.recover(repo)
    assert any("was interrupted" in n and "02-half-done" in n for n in notes)
    assert state.load_run(repo, ws.run_id)["status"] == "interrupted" and not tree.path.exists()
    assert branches(repo) == [kept] and "02-half-done" not in git(repo, "worktree", "list")
    assert engine.recover(repo) == []  # idempotent


def test_damaged_state_files_are_tolerated(repo, ap_cfg, store):
    d = state.autopilot_dir(repo, create=True)
    (d / "history.json").write_text("{not json", encoding="utf-8")
    (d / "lock.json").write_text("garbage", encoding="utf-8")
    (d / "runs" / "20250101-000000-bad").mkdir(parents=True)
    (d / "runs" / "20250101-000000-bad" / "state.json").write_text("[1, 2", encoding="utf-8")
    assert state.History(repo).entries == [] and state.lock_holder(repo) is None and state.find_run(repo) is None
    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(write_tests))
    assert final["results"][0]["accepted"] and state.find_run(repo)["run_id"] == final["run_id"]
    assert len(state.History(repo).entries) == 1  # rewritten cleanly


def test_project_config_with_bad_toml_falls_back_to_defaults(repo):
    from ration import config

    (repo / ".ration.toml").write_text("[autopilot\nweekly_reserve_percent = ", encoding="utf-8")
    cfg = config.load(repo)
    assert cfg["autopilot"]["weekly_reserve_percent"] == 40
    (repo / ".ration.toml").write_text("[autopilot]\nweekly_reserve_percent = 55\n", encoding="utf-8")
    assert config.load(repo)["autopilot"]["weekly_reserve_percent"] == 55 and config.load(repo)["autopilot"]["max_tasks"] == 5


# ---- research mode -------------------------------------------------------------------------------------
def test_research_mode_writes_reports_and_never_changes_source(repo, ap_cfg, store):
    findings = "# Findings\n\n" + "The scheduler in src/app.py:4 does one thing. " * 12 + "\nAPI_KEY=supersecretvalue123\n"
    agent = FakeAgent(text=findings)
    final, plan = plan_and_run(repo, ap_cfg, store, agent, mode="research", max_tasks=2)
    assert len(final["results"]) == 2 and all(r["outcome"] == "SUCCESS" and r["category"] == "research" for r in final["results"])
    artifact = Path(final["results"][0]["artifact"])
    text = artifact.read_text(encoding="utf-8")
    assert artifact.parent == state.run_dir(repo, final["run_id"]) / "research" and "Unverified analysis" in text and "The scheduler in src/app.py:4" in text
    assert "supersecretvalue123" not in text
    assert agent.kwargs[0]["permission_mode"] == "plan" and Path(agent.cwds[0]) == repo and "read-only" in agent.prompts[0]
    assert branches(repo) == [] and git(repo, "status", "--porcelain") == "" and final["checkout_untouched"] and not leftovers()
    assert all(t.opp.category == "research" for t in plan.tasks)


def test_research_without_a_real_report_is_not_counted(repo, ap_cfg, store):
    final, _ = plan_and_run(repo, ap_cfg, store, FakeAgent(text="ok"), mode="research", max_tasks=1)
    assert final["results"][0]["outcome"] == "FAILED" and "no report was produced" in final["results"][0]["detail"]


def test_build_mode_runs_backlog_items_that_safe_mode_rejects(tmp_path, ap_cfg, store):
    repo = make_repo(tmp_path / "r", {"TODO.md": "- [ ] Add a CSV export command for reports\n- [ ] Deploy the new build to production\n"})
    ap_cfg["autopilot"]["min_score"] = 45
    safe = engine.make_plan(repo, ap_cfg, store, {"mock": FakeAgent()}, max_tasks=5)
    by_title = {t.opp.title: t for t in safe.tasks}
    assert by_title["Add a CSV export command for reports"].decision == Decision.REJECT

    def edit(tree, prompt):
        if "CSV export" in prompt:
            (tree / "src" / "export.py").write_text("def export():\n    return 'a,b'\n", encoding="utf-8")
        else:
            write_tests(tree, prompt)

    final, plan = plan_and_run(repo, ap_cfg, store, FakeAgent(edit), mode="build", max_tasks=5)
    by_title = {t.opp.title: t for t in plan.tasks}
    assert by_title["Add a CSV export command for reports"].decision == Decision.RUN
    assert by_title["Deploy the new build to production"].decision == Decision.REJECT  # still never
    feature = next(r for r in final["results"] if "CSV export" in r["title"])
    assert feature["outcome"] == "SUCCESS" and "not independently verified" in feature["detail"] and feature["files_changed"] == ["src/export.py"]


# ---- the two small changes to the existing run loop -----------------------------------------------------
def test_execute_honours_a_caller_attempt_cap(store, cfg, tmp_path):
    from conftest import EXE

    from ration.difficulty import make_task

    task, a = make_task("fix the failing parser tests", priority="high", checks=[f'"{EXE}" -c "raise SystemExit(1)"'])
    agent = FakeAgent()
    rep = execute(task, a, providers={"mock": agent}, store=store, cfg=cfg, cwd=str(tmp_path), max_attempts=1)
    assert len(agent.calls) == 1 and rep.stopped_reason == ["max_attempts_reached"] and not rep.completed
    task, a = make_task("fix the failing parser tests", priority="high", checks=[f'"{EXE}" -c "raise SystemExit(1)"'])
    agent = FakeAgent()
    execute(task, a, providers={"mock": agent}, store=store, cfg=cfg, cwd=str(tmp_path))
    assert len(agent.calls) > 1  # default behaviour is unchanged


def test_run_checks_passes_an_environment_to_the_check(tmp_path):
    from conftest import EXE

    check = f'"{EXE}" -c "import os, sys; sys.exit(0 if os.environ.get(\'RATION_T\') == \'1\' else 1)"'
    assert run_checks([check], str(tmp_path), "s", "r", "t")[0] == 0
    assert run_checks([check], str(tmp_path), "s", "r", "t", env={**os.environ, "RATION_T": "1"})[0] == 1
