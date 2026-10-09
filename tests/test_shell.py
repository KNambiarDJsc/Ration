import os

import pytest

from ration import shell


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_never_picks_wsl_launcher(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    monkeypatch.setattr(shell.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None)
    assert shell.find_bash() is None
    assert shell.shell_argv("echo hi") == "echo hi"  # falls back to cmd via shell=True


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific")
def test_respects_claude_code_git_bash_path(monkeypatch, tmp_path):
    fake = tmp_path / "bash.exe"
    fake.write_text("")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(fake))
    assert shell.find_bash() == str(fake)
