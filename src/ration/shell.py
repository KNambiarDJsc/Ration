"""Run user-supplied shell snippets (acceptance checks, chained status lines).

On Windows, `bash` on PATH is often C:\\Windows\\System32\\bash.exe, the WSL
launcher, which fails without a distro. Prefer the Git Bash that Claude Code
itself uses (CLAUDE_CODE_GIT_BASH_PATH, then Git for Windows), and fall back to
cmd.exe via shell=True.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and Path(env).is_file():
        return env
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # ...\Git\cmd\git.exe -> ...\Git
        for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def shell_argv(cmd: str) -> list[str] | str:
    """argv for subprocess; a plain string means `shell=True`."""
    bash = find_bash()
    return [bash, "-c", cmd] if bash else cmd
