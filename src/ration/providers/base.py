"""Provider-neutral interface.

The spec sketched an async Protocol. V1 is synchronous on purpose: every real
adapter is "spawn a CLI and read its JSONL stream", which a blocking reader
handles with less machinery. `on_event` returning False is the cancel signal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import AgentEvent, ExecutionPolicy, ModelClass, UsageSnapshot

EventSink = Callable[[AgentEvent], bool]


@dataclass
class ProviderCapabilities:
    name: str
    installed: bool
    version: str | None = None
    features: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class SessionInfo:
    provider: str
    session_id: str
    project: str | None
    last_active: float
    path: str | None = None
    tokens: int | None = None


@dataclass
class RunResult:
    ok: bool
    session_id: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    num_turns: int | None = None
    error: str | None = None
    cancelled: bool = False
    final_text: str = ""  # kept in memory only, never stored
    usage: list[UsageSnapshot] = field(default_factory=list)  # official quota readings from the run
    permission_denials: list[str] = field(default_factory=list)  # e.g. ["safetyCheck"]
    denial_reason: str = ""  # in memory only, never stored


class AgentProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def models(self) -> dict[ModelClass, str]: ...

    def usage(self) -> list[UsageSnapshot]: ...

    def sessions(self, limit: int = 20) -> list[SessionInfo]: ...

    def run(
        self,
        prompt: str,
        *,
        policy: ExecutionPolicy,
        cwd: str,
        run_id: str,
        task_id: str | None,
        on_event: EventSink,
    ) -> RunResult: ...


def which(name: str) -> str | None:
    return shutil.which(name)


def run_text(argv: list[str], timeout: float = 20) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return None


def stop_process(proc: subprocess.Popen[str]) -> None:
    """Ask a provider process RATION started to stop. Only ever that process and its
    children. On Windows a CLI installed through npm is a `.cmd` shim: terminating it
    would leave the real agent running underneath, so the tree is stopped."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    proc.terminate()


def stream_lines(
    argv: list[str],
    cwd: str,
    on_line: Callable[[str], bool],
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, bool, str]:
    """Run argv, feed each stdout line to on_line. Returns (rc, cancelled, stderr_tail)."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if stdin_text is not None and proc.stdin:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        cancelled = False
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if not on_line(line):
                    cancelled = True
                    stop_process(proc)
                    break
        except KeyboardInterrupt:
            cancelled = True
            stop_process(proc)
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        err.seek(0)
        tail = err.read()[-2000:].decode("utf-8", "replace")
    return rc, cancelled, tail


def parse_version(text: str | None) -> str | None:
    import re

    if not text:
        return None
    m = re.search(r"\d+\.\d+\.\d+", text)
    return m.group(0) if m else None


def flag_features(help_text: str | None, flags: dict[str, str]) -> dict[str, bool]:
    t = help_text or ""
    return {k: (f in t) for k, f in flags.items()}


def coerce(d: Any) -> dict[str, Any]:
    return d if isinstance(d, dict) else {}
