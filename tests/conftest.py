import pytest

from ration import config
from ration.store import Store


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
