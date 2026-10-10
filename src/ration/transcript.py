"""Read token usage, model and effort from Claude Code session transcripts.

Every hook payload carries `transcript_path` (and SubagentStop carries
`agent_transcript_path`). Each assistant line holds `message.model`,
`message.usage` and a top-level `effort`. The same message can be written
several times (one line per content block), so usage is de-duplicated by
message id + request id.

Only usage/model/effort/timestamp fields are read; message content is ignored.
Reads are incremental from a byte offset, so hooks stay fast on long sessions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TAIL_BYTES = 256 * 1024
MAX_READ_BYTES = 64 * 1024 * 1024
# Relative cost weights per token type (input = 1). These ratios match
# Anthropic's published price structure and are stable across model tiers;
# per-model differences are learned separately by calibration.
WEIGHTS = {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    def add_usage(self, u: dict[str, Any]) -> None:
        self.input += int(u.get("input_tokens") or 0)
        self.output += int(u.get("output_tokens") or 0)
        self.cache_write += int(u.get("cache_creation_input_tokens") or 0)
        self.cache_read += int(u.get("cache_read_input_tokens") or 0)

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read

    @property
    def weighted(self) -> float:
        return (
            self.input * WEIGHTS["input"]
            + self.output * WEIGHTS["output"]
            + self.cache_write * WEIGHTS["cache_write"]
            + self.cache_read * WEIGHTS["cache_read"]
        )


@dataclass
class Slice:
    by_model: dict[str, Tokens] = field(default_factory=dict)
    last_model: str | None = None
    last_effort: str | None = None
    last_ts: float | None = None
    last_ctx: int = 0  # prompt size of the most recent call
    end_offset: int = 0
    messages: int = 0

    @property
    def weighted(self) -> float:
        return sum(t.weighted for t in self.by_model.values())

    @property
    def total(self) -> int:
        return sum(t.total for t in self.by_model.values())

    def weighted_by_model(self) -> dict[str, float]:
        return {m: round(t.weighted, 1) for m, t in self.by_model.items()}


def _ts(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _effort(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and isinstance(v.get("level"), str):
        return v["level"]
    return None


@dataclass
class Call:
    """One model API call (one de-duplicated assistant message)."""

    ts: float
    model: str
    inp: int
    out: int
    cw: int  # cache write
    cr: int  # cache read
    cwd: str | None = None

    @property
    def ctx(self) -> int:
        """Prompt size of this call."""
        return self.inp + self.cw + self.cr

    @property
    def weighted(self) -> float:
        return self.inp * WEIGHTS["input"] + self.out * WEIGHTS["output"] + self.cw * WEIGHTS["cache_write"] + self.cr * WEIGHTS["cache_read"]


def iter_calls(path: str | os.PathLike[str]) -> list[Call]:
    """Every call in a transcript, in order, de-duplicated. Content is never read."""
    out: list[Call] = []
    seen: set[tuple[Any, Any]] = set()
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_READ_BYTES)
    except OSError:
        return out
    for raw in data.split(b"\n"):
        if b'"assistant"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        u = m.get("usage")
        model = m.get("model")
        if r.get("type") != "assistant" or not isinstance(u, dict) or not model or model == "<synthetic>":
            continue
        key = (m.get("id"), r.get("requestId"))
        if key in seen:
            continue
        seen.add(key)
        ts = _ts(r.get("timestamp"))
        if ts is None:
            continue
        out.append(
            Call(
                ts=ts,
                model=model,
                inp=int(u.get("input_tokens") or 0),
                out=int(u.get("output_tokens") or 0),
                cw=int(u.get("cache_creation_input_tokens") or 0),
                cr=int(u.get("cache_read_input_tokens") or 0),
                cwd=r.get("cwd") if isinstance(r.get("cwd"), str) else None,
            )
        )
    return out


def size(path: str | None) -> int | None:
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


def read_slice(path: str | None, offset: int = 0, *, skip_partial_first: bool = False) -> Slice:
    out = Slice(end_offset=offset)
    if not path:
        return out
    seen: set[tuple[Any, Any]] = set()
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(MAX_READ_BYTES)
    except OSError:
        return out
    lines = data.split(b"\n")
    complete = lines[:-1]  # the last element is a partial line (or b"")
    consumed = sum(len(ln) + 1 for ln in complete)
    if skip_partial_first and complete:
        complete = complete[1:]
    for raw in complete:
        if b'"assistant"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("type") != "assistant":
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        model = m.get("model")
        if not model or model == "<synthetic>":
            continue
        out.last_model = model
        out.last_effort = _effort(r.get("effort")) or out.last_effort
        out.last_ts = _ts(r.get("timestamp")) or out.last_ts
        key = (m.get("id"), r.get("requestId"))
        if key in seen or not isinstance(m.get("usage"), dict):
            continue
        seen.add(key)
        out.by_model.setdefault(model, Tokens()).add_usage(m["usage"])
        u = m["usage"]
        out.last_ctx = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
        out.messages += 1
    out.end_offset = offset + consumed
    return out


def tail(path: str | None, nbytes: int = TAIL_BYTES) -> Slice:
    n = size(path)
    if n is None:
        return Slice()
    start = max(0, n - nbytes)
    return read_slice(path, start, skip_partial_first=start > 0)
