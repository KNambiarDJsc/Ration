"""Claude Code command-hook handler.

Invoked as:  <python> -I -m ration.hooks.claude   (hook JSON on stdin)

Contract:
- Fail open. Any internal error -> exit 0 with no output, so RATION can never
  break the user's Claude Code session.
- Deterministic. No LLM calls; decisions are plain rules over local data.
- Observe mode (default) records only. Guard mode may deny a subagent spawn
  (PreToolUse on the Agent tool) or inject a short stuck warning as
  additionalContext after a tool call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from .. import advisor, config, transcript
from ..models import AgentEvent, EventType
from ..normalize import AGENT_TOOLS, is_premium, sig, tool_events
from ..store import Store
from ..stuck import detect, recommend

PROVIDER = "claude"
STUCK_REPEAT_S = 300


def normalize(payload: dict[str, Any], cfg: dict[str, Any]) -> list[AgentEvent]:
    """Pure mapping from one hook payload to AgentEvents (no guard logic)."""
    ev = payload.get("hook_event_name", "")
    sid = str(payload.get("session_id") or "unknown")
    store_paths = bool(cfg["privacy"]["store_paths"])
    E = EventType

    def one(et: EventType, meta: dict[str, Any] | None = None) -> list[AgentEvent]:
        return [AgentEvent(PROVIDER, sid, et, meta or {})]

    if ev == "SessionStart":
        out = one(E.SESSION_STARTED, {"source": payload.get("source")})
        if payload.get("model"):
            out += one(E.MODEL_SELECTED, {"model": _model_id(payload["model"])})
        return out
    if ev == "UserPromptSubmit":
        return one(E.PROMPT_SUBMITTED, {"prompt_len": len(payload.get("prompt") or "")})
    if ev in ("PostToolUse", "PostToolUseFailure"):
        tool = payload.get("tool_name") or "?"
        ok = ev == "PostToolUse"
        resp = payload.get("tool_response") if ok else payload.get("error")
        if tool in AGENT_TOOLS and ok and isinstance(resp, dict):
            usage = resp.get("usage") or {}
            return one(
                E.SUBAGENT_ENDED,
                {
                    "model": resp.get("resolvedModel"),
                    "tokens": resp.get("totalTokens"),
                    "duration_ms": resp.get("totalDurationMs"),
                    "tool_uses": resp.get("totalToolUseCount"),
                    "output_tokens": usage.get("output_tokens"),
                },
            )
        return tool_events(
            provider=PROVIDER,
            session_id=sid,
            tool=tool,
            tool_input=payload.get("tool_input"),
            ok=ok,
            response=resp,
            store_paths=store_paths,
        )
    if ev == "SubagentStop":
        meta: dict[str, Any] = {"agent_type": payload.get("agent_type")}
        sl = transcript.read_slice(payload.get("agent_transcript_path"))
        if sl.messages:  # exact subagent tokens from its own transcript
            meta.update(by_model=sl.weighted_by_model(), wtok=round(sl.weighted), tokens=sl.total)
        return one(E.SUBAGENT_ENDED, meta)
    if ev in ("PreCompact", "PostCompact"):
        return one(E.CONTEXT_COMPACTED, {"trigger": payload.get("trigger"), "phase": ev})
    if ev == "Stop":
        return one(E.TURN_ENDED)
    if ev == "StopFailure":
        err = payload.get("error")
        out = one(E.ERROR, {"error": err})
        if err == "rate_limit":
            out += one(E.BUDGET_WARNING, {"reason": "provider_rate_limit_hit"})
        return out
    if ev == "SessionEnd":
        return one(E.SESSION_ENDED, {"reason": payload.get("reason")})
    if ev in ("PreModelSwitch", "PostModelSwitch"):
        return one(E.MODEL_SELECTED, {"from": payload.get("from_model"), "model": payload.get("to_model")})
    return []


def _model_id(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("id") or m.get("display_name") or "")
    return str(m)


def guard_subagent(payload: dict[str, Any], cfg: dict[str, Any], store: Store) -> tuple[list[AgentEvent], dict[str, Any] | None]:
    """PreToolUse on Agent/Task: govern subagent spawning."""
    sid = str(payload.get("session_id") or "unknown")
    ti = payload.get("tool_input") or {}
    g, c = cfg["guard"], cfg["claude"]
    model = ti.get("model")
    premium = is_premium(model, c.get("premium_markers", "opus"))
    now = time.time()
    since = now - g["subagent_window_minutes"] * 60
    recent = [e for e in store.events(session_id=sid, since=since, limit=1000) if e.event_type == EventType.SUBAGENT_STARTED]
    n_total = len(recent) + 1
    n_premium = sum(1 for e in recent if e.metadata.get("premium")) + int(premium)

    violations: list[str] = []
    suggestions: list[str] = []
    if n_total > g["max_subagents_per_task"]:
        violations.append(
            f"subagent explosion: {n_total} spawned in the last {g['subagent_window_minutes']} min (limit {g['max_subagents_per_task']})"
        )
        suggestions.append("finish or consolidate running subagents before spawning more")
    if premium and n_premium > g["max_premium_subagents"]:
        violations.append(f"{n_premium} premium subagents (limit {g['max_premium_subagents']})")
        suggestions.append(f"use model '{c['strong']}' or '{c['cheap']}' for this subagent")
    if premium:
        from ..budget import provider_budgets

        b = provider_budgets(store, cfg, now=now).get(PROVIDER)
        if b and b.known:
            rem = b.remaining_percent or 0.0
            if rem <= cfg["budget"]["reserve_percent"]:
                violations.append(f"Claude usage at reserve ({rem:.0f}% left in {b.binding.window})")
                suggestions.append(f"use model '{c['strong']}' instead of a premium model")
    if g["require_explicit_model"] and not model:
        violations.append("no explicit model set for subagent (it would inherit the session model)")
        suggestions.append(f"pass model: '{c['cheap']}' for search/read-only work or '{c['strong']}' for coding")

    meta = {
        "agent_type": ti.get("subagent_type"),
        "model": model,
        "premium": premium,
        "desc_sig": sig(ti.get("subagent_type"), ti.get("description")),
        "background": bool(ti.get("run_in_background")),
    }
    if not violations:
        return [AgentEvent(PROVIDER, sid, EventType.SUBAGENT_STARTED, meta)], None

    reason = "; ".join(violations)
    if g["mode"] != "guard":
        meta["would_block"] = reason
        return [AgentEvent(PROVIDER, sid, EventType.SUBAGENT_STARTED, meta)], None

    store.add_intervention(sid, None, "REDUCE_PARALLELISM", reason, enforced=True)
    msg = f"RATION BLOCKED this subagent. Reason: {reason}. Suggested: {'; '.join(suggestions)}."
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": msg,
        }
    }
    return [AgentEvent(PROVIDER, sid, EventType.GUARD_BLOCKED, {**meta, "reason": reason})], out


def stuck_check(payload: dict[str, Any], cfg: dict[str, Any], store: Store) -> tuple[list[AgentEvent], dict[str, Any] | None]:
    sid = str(payload.get("session_id") or "unknown")
    events = store.events(session_id=sid, limit=300)
    res = detect(events, now=time.time())
    if res.band not in ("intervene", "likely_stuck"):
        return [], None
    last = store.last_intervention(sid)
    if last is not None and time.time() - last["at"] < STUCK_REPEAT_S and last["intervention"] != "CONTINUE":
        return [], None
    rec = recommend(res, max_parallel=cfg["scheduler"]["max_parallel_agents"])
    enforce = cfg["guard"]["mode"] == "guard" and cfg["guard"]["inject_stuck_context"]
    store.add_intervention(sid, None, str(rec.intervention), rec.explanation, enforced=enforce)
    evs = [
        AgentEvent(
            PROVIDER,
            sid,
            EventType.STUCK_DETECTED,
            {"score": res.score, "band": res.band, "intervention": str(rec.intervention), "reasons": res.reasons},
        )
    ]
    if not enforce:
        return evs, None
    out = {
        "hookSpecificOutput": {
            "hookEventName": payload.get("hook_event_name"),
            "additionalContext": f"RATION [{rec.intervention}]: {rec.explanation}",
        }
    }
    return evs, out


def handle(payload: dict[str, Any], cfg: dict[str, Any], store: Store) -> dict[str, Any] | None:
    ev = payload.get("hook_event_name", "")
    output: dict[str, Any] | None = None
    message: str | None = None
    if ev == "PreToolUse":
        if payload.get("tool_name") in AGENT_TOOLS:
            events, output = guard_subagent(payload, cfg, store)
        else:
            events = []
    else:
        events = normalize(payload, cfg)
    if ev == "SessionStart":
        message = advisor.on_session_start(payload, cfg)
    elif ev == "UserPromptSubmit":
        adv_events, meta, message = advisor.on_prompt(payload, cfg, store)
        if events:
            events[0].metadata.update(meta)
        events += adv_events
    elif ev in ("PostToolUse", "PostToolUseFailure", "Stop"):
        store.add_events(events)
        events = []
        adv_events, message = advisor.deferred_model_tip(payload, cfg, store)
        events += adv_events
        if ev == "Stop":
            stop_events, receipt = advisor.on_stop(payload, cfg, store)
            events += stop_events
            message = "\n".join(m for m in (message, receipt) if m) or None
    if events:
        store.add_events(events)
    if ev in ("PostToolUse", "PostToolUseFailure"):
        sevents, sout = stuck_check(payload, cfg, store)
        if sevents:
            store.add_events(sevents)
        output = output or sout
    if message:
        # systemMessage is shown to the developer only; it never enters the model's context.
        output = {**(output or {}), "systemMessage": message}
    return output


def main() -> int:
    if os.environ.get("RATION_RUN_ID"):
        return 0  # inside a `ration run` child session: the orchestrator observes the stream
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        cfg = config.load()
        with Store() as store:
            out = handle(payload, cfg, store)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as e:  # fail open, but leave a trace for `ration doctor`
        try:
            log = config.ration_home() / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {type(e).__name__}: {e}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
