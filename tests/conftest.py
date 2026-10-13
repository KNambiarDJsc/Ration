import atexit
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ration import config
from ration.models import AgentEvent, Confidence, EventType, ResourceType, Source, UsageSnapshot
from ration.providers.base import RunResult
from ration.providers.mock import MockProvider
from ration.store import Store

EXE = sys.executable.replace("\\", "/")
# Stand-ins for a project's real commands: fast, deterministic, and they print what the
# real tools print. `tools/pytest.py` is recognised as a test runner by its name.
TEST_CMD = f'"{EXE}" tools/pytest.py'
LINT_CMD = f'"{EXE}" tools/lint.py'
FAKE_PYTEST = """import glob, sys
passed = sum(open(f).read().count("def test_") for f in glob.glob("tests/test_*.py"))
failed = sum(open(f).read().count("BROKEN") for f in glob.glob("src/*.py"))
print(f"{passed} passed" + (f", {failed} failed" if failed else ""))
sys.exit(1 if failed else 0)
"""
FAKE_LINT = """import glob, sys
n = sum(open(f).read().count("LINTME") for f in glob.glob("src/*.py"))
print(f"Found {n} errors." if n else "All checks passed!")
sys.exit(1 if n else 0)
"""
APP = '"""App."""\n\n\ndef add(a, b):\n    return a + b\n' + "".join(f"\n\ndef f{i}(x):\n    return x + {i}\n" for i in range(12))
UTIL = '"""Helpers nobody tests yet."""\n' + "".join(f"\n\ndef helper{i}(x):\n    y = x * {i}\n    return y\n" for i in range(14))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.config/ration or ~/.claude."""
    monkeypatch.setenv("RATION_HOME", str(tmp_path / "ration-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def cfg():
    return config.load()


@pytest.fixture
def store():
    with Store() as s:
        yield s


BASE_FILES = {
    "README.md": "# demo\n",
    "src/app.py": APP,
    "src/util.py": UTIL,
    "tests/test_app.py": "from src.app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n",
    "tools/pytest.py": FAKE_PYTEST,
    "tools/lint.py": FAKE_LINT,
}


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return r.stdout


_template: Path | None = None


def _base_repo() -> Path:
    """The committed fixture project, built once per test session and copied from then on:
    creating a repository costs several git processes, copying one costs none."""
    global _template
    if _template is None:
        root = Path(tempfile.mkdtemp(prefix="ration-fixture-"))
        atexit.register(shutil.rmtree, root, True)
        for rel, text in BASE_FILES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git(root, "config", k, v)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        _template = root
    return _template


def make_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """A small committed project: one tested module, one untested, fake test and lint commands.
    `files` are added (or replace fixture files) in the same single commit."""
    shutil.copytree(_base_repo(), root)
    if files:
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        git(root, "add", "-A", "-f")
        git(root, "commit", "-q", "--amend", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "proj")


@pytest.fixture
def ap_cfg(cfg):
    """Config for autopilot tests: the fake commands, and a threshold above the fixture's
    low-value documentation tasks so each test controls exactly which tasks run."""
    cfg["autopilot"].update(test_command=TEST_CMD, lint_command=LINT_CMD, min_score=55)
    return cfg


class FakeAgent(MockProvider):
    """A scripted coding agent: `edit(worktree, prompt)` is what it 'does'. Costs nothing."""

    def __init__(self, edit=None, *, name="mock", text="", ok=True):
        super().__init__(name=name)
        self.edit, self.text, self.ok = edit, text, ok
        self.prompts: list[str] = []
        self.cwds: list[str] = []
        self.kwargs: list[dict] = []

    def run(self, prompt, *, policy, cwd, run_id, task_id, on_event, **kw):
        self.calls.append(policy)
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        self.kwargs.append(kw)
        on_event(AgentEvent(self.name, "fake", EventType.SESSION_STARTED, {}, run_id=run_id, task_id=task_id))
        if self.edit:
            self.edit(Path(cwd), prompt)
        return RunResult(ok=self.ok, session_id="fake", cost_usd=0.01, final_text=self.text)


_tick = [0]


def add_usage(store, window, used, reset_in_h, captured_ago_h=0.0, provider="claude"):
    _tick[0] += 1
    now = time.time() + _tick[0] * 1e-3  # coarse clocks (Windows) must not make two readings look simultaneous
    minutes = {"five_hour": 300, "seven_day": 10080, "primary": 300, "secondary": 10080}[window]
    store.add_usage(
        UsageSnapshot(provider, window, ResourceType.USAGE_PERCENT, used, now + reset_in_h * 3600, minutes, Source.PROVIDER_CLI, Confidence.HIGH, now - captured_ago_h * 3600)
    )
