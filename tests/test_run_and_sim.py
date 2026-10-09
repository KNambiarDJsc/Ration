import sys

from ration import cli
from ration.difficulty import make_task
from ration.models import ModelClass
from ration.orchestrator import execute
from ration.providers.mock import MockProvider
from ration.sim import bench


def test_run_escalates_after_stuck_and_verifies_with_checks(store, cfg, tmp_path):
    marker = tmp_path / "fixed"
    # acceptance check passes only once the marker exists (relative path: checks run in cwd)
    exe = sys.executable.replace("\\", "/")
    check = f'"{exe}" -c "import os,sys; sys.exit(0 if os.path.exists(\'fixed\') else 1)"'
    task, a = make_task("fix the refresh bug in src/auth.py", priority="high", checks=[check])

    class FixOnStrong(MockProvider):
        def run(self, prompt, *, policy, **kw):
            if policy.model_class != ModelClass.CHEAP:
                marker.write_text("ok")
            return super().run(prompt, policy=policy, **kw)

    prov = FixOnStrong(success={ModelClass.CHEAP: 0.0, ModelClass.STRONG: 1.0, ModelClass.PREMIUM: 1.0})
    report = execute(task, a, providers={"mock": prov}, store=store, cfg=cfg, cwd=str(tmp_path))
    assert report.completed
    assert [x.policy.model_class for x in report.attempts] == [ModelClass.CHEAP, ModelClass.STRONG]
    assert report.attempts[0].outcome == "stuck"  # interrupted by stuck detection
    assert report.attempts[1].checks_passed == 1
    rows = store.conn.execute("SELECT state, action, outcome FROM scheduler_decisions ORDER BY id").fetchall()
    assert len(rows) == 2 and all(r["outcome"] for r in rows)  # state/action/outcome logged


def test_low_value_loop_is_stopped(store, cfg, tmp_path):
    task, a = make_task("refactor helper names", priority="low", checks=[f'"{sys.executable}" -c "raise SystemExit(1)"'])
    prov = MockProvider(success={m: 0.0 for m in ModelClass})
    report = execute(task, a, providers={"mock": prov}, store=store, cfg=cfg, cwd=str(tmp_path))
    assert not report.completed
    assert len(report.attempts) <= 2
    assert report.stopped_reason


def test_simulation_is_reproducible_and_respects_budgets():
    tasks = bench.make_tasks(60, __import__("random").Random(7))
    m1 = bench.run_ration(tasks, seed=1)
    m2 = bench.run_ration(tasks, seed=1)
    assert (m1.resolved, m1.spent) == (m2.resolved, m2.spent)
    total = sum(p.budget for p in bench.default_world())
    assert m1.spent <= total + 1e-9
    a = bench.run_static("A", tasks, seed=1)
    assert m1.premium_spent < a.premium_spent  # D only goes premium when justified


def test_cli_smoke(capsys):
    assert cli.main(["plan", "fix", "the", "flaky", "login", "test"]) == 0
    assert cli.main(["status"]) == 0
    assert cli.main(["privacy"]) == 0
    assert cli.main(["watch", "--once"]) == 0
    assert cli.main(["benchmark", "--tasks", "20", "--seeds", "1"]) == 0
    assert cli.main(["guard", "on"]) == 0
    out = capsys.readouterr().out
    assert "Decision" in out and "No usage data yet" in out and "Never stored" in out
