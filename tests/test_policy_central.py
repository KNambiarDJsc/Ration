"""Centrally-managed policy: a URL an org admin controls, merged with the
project's own .ration/policy.toml. Whichever side is stricter wins on every
field, so a local file can tighten a central policy but never loosen it.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import FakeAgent, make_repo

from ration.autopilot import engine, policy


class _Handler(BaseHTTPRequestHandler):
    body = b"[autopilot]\nmax_tasks = 2\n"
    status = 200
    hits = 0

    def do_GET(self):
        _Handler.hits += 1
        self.send_response(_Handler.status)
        self.end_headers()
        if _Handler.status == 200:
            self.wfile.write(_Handler.body)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture
def remote():
    _Handler.body = b"[autopilot]\nmax_tasks = 2\n"
    _Handler.status = 200
    _Handler.hits = 0
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/policy.toml", _Handler
    finally:
        server.shutdown()
        t.join(timeout=2)


# ---- merge_policies: pure, no I/O --------------------------------------------------


def test_merge_takes_the_stricter_side_on_every_field():
    central = policy.UserPolicy(
        weekly_reserve=50, max_tasks=5, protected_paths=("infra/",), denied_categories=frozenset({"docs"}),
        allow_lockfile_changes=True, preferred_provider="claude",
    )
    local = policy.UserPolicy(
        weekly_reserve=30, max_tasks=2, protected_paths=("secrets/",), denied_categories=frozenset({"refactor"}),
        allow_lockfile_changes=False, preferred_provider="codex",
    )
    m = policy.merge_policies(central, local)
    assert m.weekly_reserve == 50  # higher reserve is stricter
    assert m.max_tasks == 2  # lower cap is stricter
    assert set(m.protected_paths) == {"infra/", "secrets/"}  # union
    assert m.denied_categories == frozenset({"docs", "refactor"})  # union
    assert m.allow_lockfile_changes is False  # local's False wins (AND)
    assert m.preferred_provider == "claude"  # central wins when it's set


def test_merge_allowed_categories_intersects_when_both_set():
    central = policy.UserPolicy(allowed_categories=frozenset({"docs", "tests", "lint"}))
    local = policy.UserPolicy(allowed_categories=frozenset({"tests", "feature"}))
    assert policy.merge_policies(central, local).allowed_categories == frozenset({"tests"})


def test_merge_allowed_categories_falls_back_when_only_one_side_restricts():
    central = policy.UserPolicy(allowed_categories=frozenset({"docs"}))
    local = policy.UserPolicy()  # no restriction
    assert policy.merge_policies(central, local).allowed_categories == frozenset({"docs"})
    assert policy.merge_policies(local, central).allowed_categories == frozenset({"docs"})


def test_merge_local_cannot_loosen_a_central_denial():
    # local tries to re-allow "docs" by not denying it; central's denial still applies.
    central = policy.UserPolicy(denied_categories=frozenset({"docs"}))
    local = policy.UserPolicy()
    assert "docs" in policy.merge_policies(central, local).denied_categories


def test_merge_central_preferred_provider_defers_to_local_when_unset():
    central = policy.UserPolicy(preferred_provider="auto")
    local = policy.UserPolicy(preferred_provider="codex")
    assert policy.merge_policies(central, local).preferred_provider == "codex"


# ---- fetch_remote_policy: caching, TTL, fallback on failure ------------------------


def test_fetch_remote_policy_fetches_and_caches(tmp_path, remote):
    url, handler = remote
    cache = tmp_path / "policy.remote.toml"
    text = policy.fetch_remote_policy(url, cache, ttl_minutes=60)
    assert "max_tasks = 2" in text
    assert handler.hits == 1
    assert cache.read_text(encoding="utf-8") == text


def test_fetch_remote_policy_reuses_fresh_cache_without_refetching(tmp_path, remote):
    url, handler = remote
    cache = tmp_path / "policy.remote.toml"
    policy.fetch_remote_policy(url, cache, ttl_minutes=60)
    handler.body = b"[autopilot]\nmax_tasks = 9\n"  # server changed; cache is still fresh
    text = policy.fetch_remote_policy(url, cache, ttl_minutes=60)
    assert "max_tasks = 2" in text and handler.hits == 1


def test_fetch_remote_policy_falls_back_to_stale_cache_on_failure(tmp_path, remote):
    url, handler = remote
    cache = tmp_path / "policy.remote.toml"
    policy.fetch_remote_policy(url, cache, ttl_minutes=0)  # ttl=0: always stale, but caches first
    handler.status = 500
    text = policy.fetch_remote_policy(url, cache, ttl_minutes=0)  # would refetch, but server now fails
    assert "max_tasks = 2" in text  # the old cached copy, not dropped just because the network blipped


def test_fetch_remote_policy_raises_when_nothing_cached_and_unreachable(tmp_path):
    cache = tmp_path / "policy.remote.toml"
    with pytest.raises(policy.PolicyError):
        policy.fetch_remote_policy("http://127.0.0.1:1/policy.toml", cache, timeout=0.5)


# ---- load_user_policy: local + central merged --------------------------------------


def test_load_user_policy_merges_local_and_central(tmp_path, remote):
    url, _ = remote  # central: max_tasks = 2
    state_dir = tmp_path / ".ration"
    state_dir.mkdir()
    (state_dir / "policy.toml").write_text('[autopilot]\nweekly_reserve = 55\n\n[safety]\ndenied_categories = ["docs"]\n', encoding="utf-8")
    merged = policy.load_user_policy(state_dir, policy_url=url)
    assert merged.max_tasks == 2 and merged.weekly_reserve == 55 and "docs" in merged.denied_categories


def test_load_user_policy_without_url_is_local_only(tmp_path):
    state_dir = tmp_path / ".ration"
    state_dir.mkdir()
    (state_dir / "policy.toml").write_text("[autopilot]\nmax_tasks = 4\n", encoding="utf-8")
    assert policy.load_user_policy(state_dir).max_tasks == 4


def test_load_user_policy_raises_on_invalid_central_policy(tmp_path, remote):
    url, handler = remote
    handler.body = b"[notasection]\nx = 1\n"
    with pytest.raises(policy.PolicyError):
        policy.load_user_policy(tmp_path / ".ration", policy_url=url)


# ---- end-to-end through engine.make_plan -------------------------------------------


def test_engine_respects_a_stricter_central_cap_over_a_looser_local_one(tmp_path, ap_cfg, store, remote):
    url, _ = remote  # central: max_tasks = 2
    repo = make_repo(tmp_path / "r", {"src/extra.py": "def f():\n    pass\n"})
    (repo / ".ration").mkdir(exist_ok=True)
    (repo / ".ration" / "policy.toml").write_text("[autopilot]\nmax_tasks = 10\n", encoding="utf-8")
    ap_cfg["autopilot"]["policy_url"] = url
    plan = engine.make_plan(repo, ap_cfg, store, {"mock": FakeAgent()})
    assert plan.max_tasks == 2  # central's stricter cap wins over the local project's looser one
