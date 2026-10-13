"""Scoring, the safety policy, and the before/after verdict."""

import time

import pytest

from ration.autopilot import models as m
from ration.autopilot import policy, scoring, state
from ration.autopilot.goal import Goal
from ration.autopilot.models import CheckResult, Decision, Opportunity, Outcome, Target
from ration.autopilot.policy import DiffStats
from ration.autopilot.verify import count_issues, has_secret, judge, redact, run_check


def opp(title="Add tests for src/auth.py", category=m.MISSING_TESTS, **kw):
    base = dict(id=kw.pop("id", title), description=kw.pop("description", title), source="t", files=["src/auth.py"], value=0.6, risk=0.15, confidence=0.7, verifiability=0.9, complexity="s")
    return Opportunity(title=title, category=category, **{**base, **kw})


def decide(o, mode=policy.SAFE, goal=None, dirty=(), history=None, has_guards=True, min_score=50):
    return scoring.decide(scoring.score(o, goal, history), mode, goal=goal, dirty=set(dirty), history=history, has_guards=has_guards, min_score=min_score)


# ---- scoring ---------------------------------------------------------------------------------
def test_score_is_deterministic_and_bounded():
    a, b = scoring.score(opp()), scoring.score(opp())
    assert a.score == b.score and 0 < a.score <= 100 and set(a.factors) == set(scoring.WEIGHTS)
    assert abs(sum(scoring.WEIGHTS.values()) - 1) < 1e-9
    assert scoring.score(opp(value=1, risk=0, confidence=1, verifiability=1, complexity="xs"), Goal("auth", ["auth"])).score > 90


def test_each_factor_moves_the_score_the_right_way():
    base = scoring.score(opp()).score
    assert scoring.score(opp(value=0.9)).score > base > scoring.score(opp(value=0.3)).score
    assert scoring.score(opp(risk=0.6)).score < base
    assert scoring.score(opp(verifiability=0.3)).score < base
    assert scoring.score(opp(confidence=0.3)).score < base
    assert scoring.score(opp(complexity="l")).score < base < scoring.score(opp(complexity="xs")).score


def test_ordering_matches_engineering_sense():
    failing = opp("Fix 2 failing tests", m.FAILING_TESTS, value=0.9, risk=0.3, verifiability=1.0, files=[])
    lint = opp("Resolve 4 lint issues", m.LINT, value=0.45, confidence=0.85, verifiability=1.0, complexity="xs", files=[])
    docs = opp("Document functions", m.DOCS, value=0.2, risk=0.08, confidence=0.8, verifiability=0.5)
    rewrite = opp("Rewrite database architecture", m.FEATURE, value=0.9, risk=0.9, confidence=0.3, verifiability=0.2, complexity="xl")
    scores = [scoring.score(o).score for o in (failing, lint, docs, rewrite)]
    assert scores == sorted(scores, reverse=True)


def test_goal_alignment_raises_matching_work_and_is_explained():
    g = Goal("Launch MVP", ["finish authentication"])
    aligned, other = scoring.score(opp("Add tests for src/authentication.py", files=["src/authentication.py"]), g), scoring.score(opp("Add tests for src/colors.py", files=["src/colors.py"]), g)
    assert aligned.score > other.score and "matches priority 'finish authentication'" in " ".join(aligned.reasons)
    assert "neutral (no goal set)" in " ".join(scoring.score(opp()).reasons)


def test_reasons_explain_the_decision_in_words():
    st = decide(opp())
    text = "; ".join(st.reasons)
    assert st.decision == Decision.RUN and "usefulness" in text and "risk low" in text and "estimated cost ~2% of the 5-hour window (assumed)" in text


def test_history_calibrates_confidence_and_replaces_assumed_cost(repo):
    h = state.History(repo)
    for accepted in (False, False, False, True):
        h.record({"id": "x", "category": m.MISSING_TESTS, "outcome": "SUCCESS" if accepted else "FAILED", "accepted": accepted, "cost_percent": 6.0})
    st = scoring.score(opp(), None, state.History(repo))
    assert st.score < scoring.score(opp()).score  # this kind of task fails here, and costs more than assumed
    assert st.cost == 6.0 and "measured in this repo (4 runs)" in st.cost_source and "1/4 earlier" in " ".join(st.reasons)


# ---- policy ----------------------------------------------------------------------------------
def test_safe_mode_allows_tests_lint_docs_and_rejects_features():
    for cat in (m.FAILING_TESTS, m.MISSING_TESTS, m.LINT, m.TYPES, m.DOCS, m.TODO, m.ERROR_HANDLING):
        assert policy.gate(opp(category=cat), policy.SAFE, has_guards=True) is None
    for cat in (m.FEATURE, m.UNFINISHED):
        why = policy.gate(opp(category=cat), policy.SAFE, has_guards=True)
        assert "not allowed in safe mode (allowed in build)" in why
        assert policy.gate(opp(category=cat), policy.BUILD, has_guards=True) is None


def test_build_mode_takes_larger_and_riskier_work_but_has_limits():
    big = opp(category=m.FEATURE, risk=0.55, complexity="l")
    assert "risk too high" in policy.gate(big, policy.SAFE, has_guards=True) or "not allowed" in policy.gate(big, policy.SAFE, has_guards=True)
    assert policy.gate(big, policy.BUILD, has_guards=True) is None
    assert "risk too high" in policy.gate(opp(category=m.FEATURE, risk=0.8), policy.BUILD, has_guards=True)
    assert "scope too large" in policy.gate(opp(category=m.FEATURE, complexity="xl"), policy.BUILD, has_guards=True)
    assert "scope too large" in policy.gate(opp(complexity="l"), policy.SAFE, has_guards=True)


def test_research_mode_changes_no_code():
    assert "not allowed in research mode" in policy.gate(opp(), policy.RESEARCH, has_guards=True)
    r = opp("Technical-debt review", m.RESEARCH, writes_code=False, files=[], verifiability=0.35)
    assert policy.gate(r, policy.RESEARCH, has_guards=False) is None and policy.RESEARCH.permission_mode == "plan"
    assert "not allowed in safe mode (allowed in research)" in policy.gate(r, policy.SAFE, has_guards=True)


@pytest.mark.parametrize(
    "title",
    [
        "Deploy the new build to production",
        "Publish the package to PyPI",
        "git push the fix to origin",
        "Drop table users and recreate it",
        "Add a database migration for the orders schema",
        "Rotate the API keys in config",
        "Commit the credentials file",
        "Run terraform apply for the new cluster",
        "Rewrite the entire architecture from scratch",
        "Upgrade React to v19 (major, breaking)",
        "Bump all dependencies",
        "Send emails to every customer",
        "Apply for jobs on LinkedIn",
        "Refund customers charged twice",
    ],
)
def test_dangerous_work_is_rejected_in_every_mode(title):
    for mode in policy.MODES.values():
        o = opp(title, m.FEATURE if mode.writes_code else m.RESEARCH, files=[])
        why = policy.gate(o, mode, has_guards=True)
        assert why and "never done unattended" in why, (mode.name, title, why)
    assert decide(opp(title, m.USER, files=[]), policy.BUILD).decision == Decision.REJECT


@pytest.mark.parametrize("title", ["Add tests for src/auth.py", "Resolve 4 lint issues", "Fix 2 failing tests", "Improve error handling in the API client", "Document the release process"])
def test_ordinary_work_is_not_caught_by_the_no_go_rules(title):
    assert policy.no_go(title) is None


@pytest.mark.parametrize("path", [".env", "config/.env.production", "deploy/id_rsa", "keys/server.pem", "db/migrations/0003_add.py", "schema.sql", "infra/main.tf", ".github/workflows/ci.yml", "secrets.yaml"])
def test_protected_paths(path):
    assert policy.protected(path)
    assert "protected path" in policy.gate(opp(files=[path]), policy.BUILD, has_guards=True)


def test_ordinary_paths_and_lockfiles():
    assert not any(policy.protected(p) for p in ("src/app.py", "tests/test_env.py", "README.md", "docs/environment.md"))
    assert "dependency lockfile" in policy.gate(opp(files=["package-lock.json"]), policy.BUILD, has_guards=True)
    assert "dependency manifest" in policy.gate(opp(files=["package.json"]), policy.BUILD, has_guards=True)


def test_code_changes_need_something_to_verify_them():
    assert "could not be verified" in policy.gate(opp(category=m.TODO, verifiability=0.5), policy.SAFE, has_guards=False)
    assert policy.gate(opp(category=m.DOCS, files=["README.md"], verifiability=0.4), policy.SAFE, has_guards=False) is None  # docs only
    assert policy.gate(opp(category=m.USER, checks=["make check"], target=Target.PASS), policy.SAFE, has_guards=False) is None  # brings its own check
    assert "verified well enough" in policy.gate(opp(verifiability=0.2), policy.SAFE, has_guards=True)


def test_modes_are_extensible():
    custom = policy.Mode("docs-only", "docs", frozenset({m.DOCS}), ("developer",), 0.2, "s", 0.3, "acceptEdits")
    assert policy.gate(opp(category=m.DOCS, risk=0.1), custom, has_guards=True) is None and policy.gate(opp(), custom, has_guards=True)


def test_local_work_constraints_cooldowns_and_low_value_are_skipped(repo):
    assert "uncommitted changes" in decide(opp(), dirty={"src/auth.py"}).reasons[0]
    g = Goal("x", constraints=["don't touch the auth module"])
    st = decide(opp(), goal=g)
    assert st.decision == Decision.SKIP and "may conflict with your constraint" in st.reasons[0]
    low = decide(opp(value=0.1))
    assert low.decision == Decision.SKIP and "expected value too low" in low.reasons[0]

    h = state.History(repo, cooldown_days=7)
    h.record({"id": "rejected", "category": "x", "outcome": "REGRESSION", "accepted": False})
    h.record({"id": "done", "category": "x", "outcome": "SUCCESS", "accepted": True, "branch": "ration/auto/r/01-x"})
    h.record({"id": "old", "category": "x", "outcome": "FAILED", "accepted": False, "at": time.time() - 9 * 86400})
    h = state.History(repo, cooldown_days=7)
    assert "cooling down" in decide(opp(id="rejected"), history=h).reasons[0]
    assert "awaiting your review" in decide(opp(id="done"), history=h).reasons[0]
    assert decide(opp(id="old"), history=h).decision == Decision.RUN  # the cooldown expired
    assert decide(opp(id="never"), history=h).decision == Decision.RUN


def test_selection_respects_budget_and_task_limit():
    def task(title, files, value, size="s"):
        return decide(opp(title, files=files, value=value, complexity=size, id=title))

    tasks = [task("a", ["x.py"], 0.9), task("b", ["x.py"], 0.8), task("c", ["y.py"], 0.7, "m"), task("d", ["z.py"], 0.6)]
    chosen = scoring.select(tasks, spendable=5, max_tasks=5)
    # a and b share a file: fine, tasks are stacked. c needs 5% but only 1% is left; d does not fit either.
    assert [t.opp.title for t in chosen] == ["a", "b"]
    why = {t.opp.title: t.reasons[0] for t in tasks if t.decision == Decision.DEFER}
    assert "only ~1% is safe to spend" in why["c"] and "(estimate)" in why["c"] and "safe to spend" in why["d"]
    # when tasks have been costing 3x their estimate, the same budget buys fewer of them
    tasks = [task(n, [f"{n}.py"], 0.9 - i / 10) for i, n in enumerate("abc")]
    assert [t.opp.title for t in scoring.select(tasks, spendable=7, max_tasks=5, cost_factor=3.0)] == ["a"]

    tasks = [task(n, [f"{n}.py"], 0.9 - i / 10) for i, n in enumerate("abc")]
    assert [t.opp.title for t in scoring.select(tasks, spendable=None, max_tasks=2)] == ["a", "b"]  # an unknown budget is capped by the task limit
    assert "over the limit of 2 tasks" in tasks[2].reasons[0]
    assert scoring.select([task("a", ["a.py"], 0.9)], spendable=0, max_tasks=5) == []


def test_diff_audit_discards_what_the_agent_should_not_have_done():
    ok = DiffStats(files=["src/a.py", "tests/test_a.py"], added=40, deleted=3)
    assert policy.audit(ok, policy.SAFE) == []
    assert "protected path" in policy.audit(DiffStats(files=["src/a.py", ".env"]), policy.SAFE)[0]
    assert "lockfile" in policy.audit(DiffStats(files=["poetry.lock"]), policy.BUILD)[0]
    assert "changed 13 files" in policy.audit(DiffStats(files=[f"f{i}.py" for i in range(13)]), policy.SAFE)[0]
    assert policy.audit(DiffStats(files=[f"f{i}.py" for i in range(13)]), policy.BUILD) == []
    assert "deleted 500 lines" in policy.audit(DiffStats(files=["a.py"], deleted=500), policy.SAFE)[0]
    assert "deleted 1 file" in policy.audit(DiffStats(files=["a.py"], deleted_files=["a.py"]), policy.SAFE)[0]
    assert "credential" in policy.audit(ok, policy.SAFE, has_secret=True)[0]


def test_agent_tool_denials_cover_pushes_and_deploys():
    denied = " ".join(policy.DENIED_TOOLS)
    for needle in ("git push", "gh:", "npm publish", "terraform", "kubectl", "docker push"):
        assert needle in denied
    for mode in policy.MODES.values():
        assert mode.permission_mode != "bypassPermissions"
    assert "Never push" in policy.RULES and "secrets" in policy.RULES


# ---- redaction ----------------------------------------------------------------------------------
def test_secrets_are_redacted_from_anything_stored_or_shown():
    # Obviously fake, and assembled from pieces so secret scanners do not flag this file.
    gh, aws, pem = "gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789", "AK" + "IAABCDEFGHIJKLMNOP", "PRIVATE " + "KEY"
    text = (
        f"token sk-abcdefghijklmnopqrstuvwxyz123456 and {gh} and {aws}\n"
        "API_KEY=supersecretvalue123 password: 'hunter2hunter2'\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        f"-----BEGIN RSA {pem}-----\nMIIEabc\n-----END RSA {pem}-----\nplain text stays"
    )
    out = redact(text, env={})
    for leaked in ("sk-abcdef", gh, aws, "supersecretvalue123", "hunter2hunter2", "MIIEabc", "abcdefghijklmnopqrstuvwxyz\n"):
        assert leaked not in out
    assert "plain text stays" in out and "API_KEY=[redacted]" in out
    assert redact("connecting with s3cr3t-value-xyz now", env={"DB_PASSWORD": "s3cr3t-value-xyz", "HOME": "connecting"}) == "connecting with [redacted] now"
    assert has_secret("key = 'sk-abcdefghijklmnopqrstuvwxyz123456'") and not has_secret("token = load_token()")


# ---- verdicts ----------------------------------------------------------------------------------
T, L = "pytest -q", "ruff check ."


def cr(cmd, ok, passed=None, failed=None, issues=None, ran=True):
    return CheckResult(cmd, ok, ran, passed, failed, issues)


def verdict(o, before, after, changed=True):
    return judge(o, {c.cmd: c for c in before}, {c.cmd: c for c in after}, changed=changed, test_cmd=T)[0]


def test_verdicts_cover_every_state():
    fix = opp("Fix failing tests", m.FAILING_TESTS, target=Target.PASS, checks=[T])
    assert verdict(fix, [cr(T, False, 184, 2)], [cr(T, True, 186, 0)]) == Outcome.SUCCESS
    assert verdict(fix, [cr(T, False, 184, 2)], [cr(T, False, 185, 1)]) == Outcome.IMPROVED
    assert verdict(fix, [cr(T, False, 184, 2)], [cr(T, False, 184, 2)]) == Outcome.FAILED
    assert verdict(fix, [cr(T, False, 184, 2)], [cr(T, False, 181, 5)]) == Outcome.REGRESSION
    assert verdict(fix, [cr(T, False, 184, 2)], [cr(T, False, 184, 2)], changed=False) == Outcome.NO_CHANGE
    assert verdict(fix, [cr(T, False, ran=False)], [cr(T, False, ran=False)]) == Outcome.UNVERIFIABLE

    lint = opp("Resolve lint", m.LINT, target=Target.PASS, checks=[L])
    assert verdict(lint, [cr(L, False, issues=9), cr(T, True, 10, 0)], [cr(L, True), cr(T, True, 10, 0)]) == Outcome.SUCCESS
    assert verdict(lint, [cr(L, False, issues=9), cr(T, True, 10, 0)], [cr(L, False, issues=4), cr(T, True, 10, 0)]) == Outcome.IMPROVED
    # the lint got fixed but a guard check broke: the whole change is a regression
    assert verdict(lint, [cr(L, False, issues=9), cr(T, True, 10, 0)], [cr(L, True), cr(T, False, 9, 1)]) == Outcome.REGRESSION

    more = opp(target=Target.MORE_TESTS)
    assert verdict(more, [cr(T, True, 214, 0)], [cr(T, True, 232, 0)]) == Outcome.SUCCESS
    assert verdict(more, [cr(T, True, 214, 0)], [cr(T, True, 214, 0)]) == Outcome.FAILED
    assert verdict(more, [cr(T, True, 214, 0)], [cr(T, False, 214, 3)]) == Outcome.REGRESSION
    assert verdict(more, [cr(T, True)], [cr(T, True)]) == Outcome.UNVERIFIABLE  # no counts to prove new tests

    todo = opp("Resolve TODO", m.TODO)
    assert verdict(todo, [cr(T, True, 10, 0)], [cr(T, True, 10, 0)]) == Outcome.SUCCESS  # guarded, though not independently verified
    assert verdict(todo, [cr(T, True, 10, 0)], [cr(T, True, 8, 0)]) == Outcome.REGRESSION  # tests disappeared
    assert verdict(todo, [], []) == Outcome.UNVERIFIABLE
    assert verdict(todo, [cr(T, True, 10, 0)], [cr(T, False, ran=False)]) == Outcome.REGRESSION  # a check that ran before must still run


def test_check_runner_reports_counts_issues_and_missing_tools(tmp_path):
    from conftest import EXE

    r = run_check(f'"{EXE}" -c "print(\'3 passed, 1 failed\'); raise SystemExit(1)" # pytest', tmp_path)
    assert (r.ok, r.ran, r.passed, r.failed) == (False, True, 3, 1) and r.summary() == "3 passed, 1 failed"
    r = run_check(f'"{EXE}" -c "print(\'Found 7 errors.\'); raise SystemExit(1)"', tmp_path)
    assert (r.ok, r.issues, r.passed) == (False, 7, None) and r.summary() == "7 issues"
    missing = run_check("definitely-not-a-real-command-xyz", tmp_path)
    assert not missing.ran and not missing.ok and missing.summary() == "could not run"
    slow = run_check(f'"{EXE}" -c "import time; time.sleep(5)"', tmp_path, timeout=0.5)
    assert not slow.ran and "timed out" in slow.tail
    leaky = run_check(f'"{EXE}" -c "print(\'API_KEY=supersecretvalue123\'); raise SystemExit(1)"', tmp_path)
    assert "supersecretvalue123" not in leaky.tail
    assert count_issues("✖ 12 problems (10 errors, 2 warnings)") == 12 and count_issues("a.ts(1,1): error TS2322: x\nb.ts(2,2): error TS1005: y") == 2
