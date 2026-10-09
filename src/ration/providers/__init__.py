"""Provider adapters. Provider-specific code stays here, never in the scheduler."""

from __future__ import annotations

from typing import Any

from .base import AgentProvider, ProviderCapabilities, RunResult, SessionInfo


def get_providers(cfg: dict[str, Any]) -> dict[str, AgentProvider]:
    from .claude import ClaudeProvider
    from .codex import CodexProvider

    out: dict[str, AgentProvider] = {}
    if cfg["claude"].get("enabled", True):
        out["claude"] = ClaudeProvider(cfg)
    if cfg["codex"].get("enabled", True):
        out["codex"] = CodexProvider(cfg)
    for name, spec in (cfg.get("generic") or {}).items():
        if isinstance(spec, dict) and spec.get("command"):
            from .generic import GenericCLIProvider

            out[name] = GenericCLIProvider(name, spec)
    return out


__all__ = ["AgentProvider", "ProviderCapabilities", "RunResult", "SessionInfo", "get_providers"]
