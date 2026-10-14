import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ration.integrations import slack
from ration.models import AgentEvent, Confidence, EventType, ResourceType, Source, UsageSnapshot

H = 3600.0


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Handler.received.append(json.loads(body))
        self.send_response(_Handler.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, fmt, *args):  # silence the default stderr logging
        pass


@pytest.fixture
def webhook():
    _Handler.received = []
    _Handler.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/webhook", _Handler
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_post_sends_the_text_and_reports_success(webhook):
    url, handler = webhook
    assert slack.post(url, "RATION: budget alert") is True
    assert handler.received == [{"text": "RATION: budget alert"}]


def test_post_reports_failure_on_non_2xx(webhook):
    url, handler = webhook
    handler.status = 500
    assert slack.post(url, "hi") is False


def test_post_is_a_noop_without_url_or_text():
    assert slack.post("", "hi") is False
    assert slack.post("http://example.invalid/hook", "") is False


def test_post_never_raises_on_unreachable_host():
    # nothing listening on this port: must fail closed, not raise
    assert slack.post("http://127.0.0.1:1/webhook", "hi", timeout=0.5) is False


# ---- hook wiring: which tip kinds get mirrored --------------------------------------


def advice(kind):
    return AgentEvent("claude", "s1", EventType.ADVICE, {"kind": kind})


def test_notify_slack_sends_when_a_wanted_kind_fired(monkeypatch, cfg):
    from ration.hooks import claude as hook

    cfg["slack"]["webhook_url"] = "http://example.invalid/hook"
    sent = []
    monkeypatch.setattr("ration.integrations.slack.post", lambda url, text, **kw: sent.append((url, text)))
    hook.notify_slack([advice("pace:five_hour")], "RATION: at this pace...", cfg)
    assert sent == [("http://example.invalid/hook", "RATION: at this pace...")]


def test_notify_slack_filters_out_unwanted_kinds(monkeypatch, cfg):
    from ration.hooks import claude as hook

    cfg["slack"]["webhook_url"] = "http://example.invalid/hook"
    sent = []
    monkeypatch.setattr("ration.integrations.slack.post", lambda url, text, **kw: sent.append((url, text)))
    hook.notify_slack([advice("model:up")], "RATION: consider opus", cfg)  # not in default events
    assert sent == []


def test_notify_slack_is_off_without_a_webhook_url(monkeypatch, cfg):
    from ration.hooks import claude as hook

    sent = []
    monkeypatch.setattr("ration.integrations.slack.post", lambda url, text, **kw: sent.append((url, text)))
    hook.notify_slack([advice("stuck")], "RATION: this session looks stuck", cfg)
    assert sent == []


def test_notify_slack_never_raises_when_post_fails(cfg):
    from ration.hooks import claude as hook

    cfg["slack"]["webhook_url"] = "http://127.0.0.1:1/webhook"  # nothing listening
    hook.notify_slack([advice("stuck")], "RATION: this session looks stuck", cfg)  # must not raise


def _usage(store, window, used, reset_in_h, captured_ago_h=0.0):
    now = time.time()
    store.add_usage(
        UsageSnapshot(
            "claude", window, ResourceType.USAGE_PERCENT, used, now + reset_in_h * H,
            {"five_hour": 300, "seven_day": 10080}[window], Source.PROVIDER_CLI, Confidence.HIGH, now - captured_ago_h * H,
        )
    )


def test_end_to_end_hook_mirrors_a_real_budget_alert_to_slack(monkeypatch, store, cfg):
    """Same scenario as test_advisor.test_five_hour_pace_alert, run through the real
    hook entry point, with slack configured: the exact tip shown to the developer
    is the one that gets mirrored."""
    from ration.hooks import claude as hook

    cfg["slack"]["webhook_url"] = "http://example.invalid/hook"
    _usage(store, "five_hour", 60, reset_in_h=2, captured_ago_h=1 / 3)
    _usage(store, "five_hour", 75, reset_in_h=2)  # 45%/h, 25% left -> ~33 min
    sent = []
    monkeypatch.setattr("ration.integrations.slack.post", lambda url, text, **kw: sent.append((url, text)))
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "short follow-up"}, cfg, store)
    assert len(sent) == 1
    assert sent[0][0] == "http://example.invalid/hook" and "5-hour limit" in sent[0][1]
