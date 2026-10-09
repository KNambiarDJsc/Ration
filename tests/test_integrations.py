import json
import time

import pytest

from ration import config
from ration.hooks import statusline
from ration.integrations import claude_install as ci

USER_SETTINGS = {
    "model": "sonnet",
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard.sh"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
    },
    "statusLine": {"type": "command", "command": "~/.claude/my-status.sh", "padding": 1},
}


@pytest.fixture
def settings():
    p = ci.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    return p


def test_install_coexists_with_user_hooks_and_is_idempotent(settings):
    ci.install_hooks()
    ci.install_hooks()  # twice
    data = json.loads(settings.read_text())
    assert data["model"] == "sonnet" and data["permissions"] == USER_SETTINGS["permissions"]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == USER_SETTINGS["hooks"]["PreToolUse"][0]  # user hook untouched, first
    ours = [g for g in pre if "ration.hooks.claude" in g["hooks"][0]["command"]]
    assert len(ours) == 1 and ours[0]["matcher"] == "Agent|Task"
    assert any("notify-send" in g["hooks"][0]["command"] for g in data["hooks"]["Stop"])
    assert set(ci.status()["hooks"]) == set(ci.HOOK_EVENTS)
    assert list(settings.parent.glob("settings.json.ration-bak-*"))


def test_uninstall_removes_only_ours(settings):
    ci.install_hooks()
    ci.uninstall_hooks()
    data = json.loads(settings.read_text())
    assert data["hooks"] == USER_SETTINGS["hooks"]


def test_invalid_settings_are_never_modified(settings):
    settings.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ci.SettingsError):
        ci.install_hooks()
    assert settings.read_text() == "{ not json"


def test_statusline_chains_and_restores(settings):
    _, chained = ci.install_statusline()
    assert chained == "~/.claude/my-status.sh"
    data = json.loads(settings.read_text())
    assert "ration.hooks.statusline" in data["statusLine"]["command"] and data["statusLine"]["padding"] == 1
    assert config.load()["claude"]["chained_statusline"] == chained
    ci.uninstall_statusline()
    assert json.loads(settings.read_text())["statusLine"] == USER_SETTINGS["statusLine"]


SL_PAYLOAD = {
    "session_id": "s",
    "model": {"id": "claude-sonnet-5", "display_name": "Sonnet"},
    "context_window": {"used_percentage": 42},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": time.time() + 3600},
        "seven_day": {"used_percentage": 41.2, "resets_at": time.time() + 3 * 86400},
    },
}


def test_statusline_records_official_rate_limits(store):
    written = statusline.record(SL_PAYLOAD, store)
    assert {s.window for s in written} == {"five_hour", "seven_day"}
    seven = next(s for s in store.latest_usage("claude") if s.window == "seven_day")
    assert seven.used_percent == 41.2 and seven.window_minutes == 7 * 24 * 60
    assert str(seven.source) == "provider_cli"
    assert statusline.record(SL_PAYLOAD, store) == []  # throttled duplicate


def test_statusline_absent_rate_limits_is_fine(store):
    assert statusline.record({"session_id": "s"}, store) == []
    assert "no usage data yet" in statusline.own_line({})
    assert "5h 24%" in statusline.own_line(SL_PAYLOAD)
