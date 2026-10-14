"""Optional Slack (or any incoming-webhook-compatible service, e.g. Teams) alerts.

Opt-in only: nothing is sent unless `[slack].webhook_url` is set in config. This
mirrors a subset of the advisor's own in-session tips (budget limits, pace
warnings, stuck sessions) to a team channel, using the exact same local
computation RATION already does for the developer's terminal: no new data is
collected, and no service but the one the user configured is contacted.

Stdlib-only, so this costs nothing to import on the hot path. Never raises:
a team's webhook being slow or down must not affect the developer's session,
so failures are swallowed and bounded by a short timeout.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.error import URLError

TIMEOUT_S = 3.0


def post(webhook_url: str, text: str, *, timeout: float = TIMEOUT_S) -> bool:
    """Best-effort POST of `{"text": text}` to an incoming webhook URL. Returns
    whether it looked like it worked; never raises."""
    if not webhook_url or not text:
        return False
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (user-configured destination)
            return 200 <= resp.status < 300
    except (URLError, OSError, ValueError):
        return False
