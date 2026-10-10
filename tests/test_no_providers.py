from ration.difficulty import make_task
from ration.models import ModelClass
from ration.scheduler import ProviderOption, Scheduler


def test_plan_without_any_installed_agent_says_why(capsys):
    """On a machine without Claude Code / Codex (e.g. CI), plan must explain, not crash."""
    task, _ = make_task("fix the flaky login test")
    opts = [ProviderOption("claude", {m: str(m) for m in ModelClass}, available=False)]
    p = Scheduler(opts).decide(task)
    assert p.stop and "no_provider_available" in p.reasons
