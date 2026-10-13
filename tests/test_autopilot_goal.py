import json
import sys

from ration import cli
from ration.autopilot import goal as goal_mod
from ration.autopilot.goal import Goal


def test_goal_round_trips_and_stays_out_of_git(repo):
    g = Goal('Ship a "production-ready" SaaS\\app', ["finish authentication", "implement billing"], ["don't change the database schema"])
    path = goal_mod.save(repo, g)
    loaded = goal_mod.load(repo)
    assert (loaded.goal, loaded.priorities, loaded.constraints) == (g.goal, g.priorities, g.constraints)
    assert path == repo / ".ration" / "goal.toml" and (repo / ".ration" / ".gitignore").read_text() == "*\n"
    from conftest import git

    assert git(repo, "status", "--porcelain") == ""  # the state folder ignores itself


def test_no_goal_and_cleared_goal_read_as_none(repo):
    assert goal_mod.load(repo) is None
    goal_mod.save(repo, Goal("x"))
    assert goal_mod.clear(repo) and goal_mod.load(repo) is None and not goal_mod.clear(repo)


def test_malformed_goal_file_is_ignored_not_fatal(repo, capsys):
    goal_mod.state_dir(repo, create=True)
    goal_mod.path(repo).write_text("goal = [unclosed", encoding="utf-8")
    assert goal_mod.load(repo) is None
    assert cli.main(["goal", "show", "--cwd", str(repo)]) == 1
    assert "cannot be read" in capsys.readouterr().out
    goal_mod.path(repo).write_text("goal = 3\npriorities = 'one'\n", encoding="utf-8")
    assert goal_mod.load(repo) is None  # wrong types are rejected too


def test_alignment_prefers_work_that_serves_the_priorities():
    g = Goal("Launch the restaurant inventory MVP", ["finish authentication", "improve reliability"], ["prioritize tests"])
    auth, _ = g.alignment("Add tests for src/authentication/refresh.py")
    other, why = g.alignment("Document public functions in scripts/gen_assets.py")
    assert auth > other and "not related" in why
    assert Goal().alignment("anything")[0] == 0.7  # neutral without a goal
    assert g.alignment("resolve lint in tests folder")[0] > other  # positive constraints act as preferences


def test_dont_constraints_are_detected_cautiously():
    g = Goal("x", constraints=["don't change the database schema", "Don't redesign UI", "prioritize tests"])
    assert g.conflict("Resolve 2 TODOs in db schema.py") == "don't change the database schema"
    assert g.conflict("Add tests for src billing.py") is None
    assert g.conflict("prioritize tests everywhere") is None  # not a prohibition


def test_goal_cli_set_show_edit_clear(repo, capsys, monkeypatch, tmp_path):
    cwd = ["--cwd", str(repo)]
    assert cli.main(["goal", "show", *cwd]) == 0 and "No goal set" in capsys.readouterr().out
    assert cli.main(["goal", "set", "Ship", "the", "MVP", "--priority", "finish auth", "--priority", "billing", "--constraint", "don't redesign UI", *cwd]) == 0
    out = capsys.readouterr().out
    assert "Ship the MVP" in out and "finish auth" in out and "don't redesign UI" in out
    assert cli.main(["goal", "set", "--priority", "only this", *cwd]) == 0  # lists replace; the sentence is kept
    capsys.readouterr()
    assert cli.main(["goal", "show", "--json", *cwd]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["goal"] == "Ship the MVP" and data["priorities"] == ["only this"] and data["constraints"] == ["don't redesign UI"]
    assert cli.main(["goal", "set", *cwd]) == 2  # nothing to set

    # `edit` keeps a valid result and restores the old goal after an invalid one
    script = tmp_path / "ed.py"
    script.write_text("import sys\nopen(sys.argv[1], 'w').write(open(sys.argv[0] + '.txt').read())\n")
    monkeypatch.setenv("EDITOR", f'"{sys.executable}" "{script}"')
    (tmp_path / "ed.py.txt").write_text('goal = "Edited goal"\npriorities = []\nconstraints = []\n')
    assert cli.main(["goal", "edit", *cwd]) == 0 and goal_mod.load(repo).goal == "Edited goal"
    (tmp_path / "ed.py.txt").write_text("goal = = broken")
    capsys.readouterr()
    assert cli.main(["goal", "edit", *cwd]) == 2 and "Not saved" in capsys.readouterr().out
    assert goal_mod.load(repo).goal == "Edited goal"

    assert cli.main(["goal", "clear", *cwd]) == 0 and goal_mod.load(repo) is None
