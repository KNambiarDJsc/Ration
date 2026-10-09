from ration import progress
from ration.models import AgentEvent, BudgetMode, EventType, Intervention, ModelClass
from ration.stuck import detect, recommend

E = EventType


def ev(et, t, **meta):
    return AgentEvent("claude", "s1", et, meta, timestamp=1000.0 + t)


def healthy_session():
    out, t = [], 0
    for i in range(8):
        out.append(ev(E.FILE_READ, t, path=f"src/f{i}.py"))
        out.append(ev(E.TOOL_COMPLETED, t + 1, tool="Read", sig=f"r{i}"))
        t += 20
    out.append(ev(E.FILE_CHANGED, t, path="src/f1.py"))
    out.append(ev(E.TEST_RESULT, t + 5, passed=3, failed=4, ok=False, sig="a"))
    out.append(ev(E.FILE_CHANGED, t + 30, path="src/f2.py"))
    out.append(ev(E.TEST_RESULT, t + 60, passed=7, failed=0, ok=True, sig="green"))
    return out


def failing_test_loop(n=6):
    out, t = [], 0
    for _ in range(n):
        out.append(ev(E.FILE_CHANGED, t, path="src/auth.py"))
        out.append(ev(E.TOOL_FAILED, t + 10, tool="Bash", sig="pytest-q"))
        out.append(ev(E.TEST_RESULT, t + 11, passed=4, failed=3, ok=False, sig="same"))
        t += 120
    return out


def test_healthy_session_scores_low():
    r = detect(healthy_session())
    assert r.band == "healthy", r


def test_failing_test_loop_is_likely_stuck_with_reasons():
    r = detect(failing_test_loop(6))
    assert r.band == "likely_stuck"
    assert any("same test failure repeated 6x" in x for x in r.reasons)
    assert r.dominant == "test_loop"


def test_sequence_loop_detected():
    evs = []
    for i in range(4):
        evs.append(ev(E.TOOL_COMPLETED, i * 10, tool="Read", sig="A"))
        evs.append(ev(E.TOOL_COMPLETED, i * 10 + 5, tool="Bash", sig="B"))
    r = detect(evs)
    assert r.signals["sequence_loop"] > 0
    assert any("2-step command sequence repeated 4x" in x for x in r.reasons)


def test_every_intervention_has_explanation():
    r = detect(failing_test_loop(6))
    for value, mode, mc, expected in [
        (0.9, BudgetMode.NORMAL, ModelClass.CHEAP, Intervention.ESCALATE_MODEL),
        (0.2, BudgetMode.NORMAL, ModelClass.CHEAP, Intervention.STOP),
        (0.5, BudgetMode.CONSERVATIVE, ModelClass.CHEAP, Intervention.ASK_USER),
    ]:
        rec = recommend(r, task_value=value, budget_mode=mode, model_class=mc)
        assert rec.intervention == expected
        assert rec.explanation and "same test failure" in rec.explanation


def test_parallelism_recommendation():
    r = detect(failing_test_loop(4))
    rec = recommend(r, parallel_subagents=9, max_parallel=4)
    assert rec.intervention in (Intervention.REDUCE_PARALLELISM, Intervention.CONTINUE)


def test_progress_unverified_is_capped():
    evs = [ev(E.FILE_CHANGED, i, path=f"f{i}.py") for i in range(10)]
    p = progress.compute(evs)
    assert p.score <= progress.UNVERIFIED_CAP
    assert any("no tests" in r for r in p.reasons)


def test_progress_rises_when_tests_go_green():
    evs = healthy_session()
    p = progress.compute(evs)
    assert p.score > 0.8
    assert any("7/7 tests pass" in r for r in p.reasons)
    assert any("improving" in r for r in p.reasons)


def test_progress_penalizes_churn_and_stuck_failure():
    p = progress.compute(failing_test_loop(7))
    assert any("same file modified 7 times" in r for r in p.reasons)
    assert any("unchanged for" in r for r in p.reasons)
