"""`ration dashboard`'s CLI wiring, independent of whether the optional
`textual` dependency is actually installed on the machine running the tests.
"""

import sys

from ration import cli


def test_dashboard_shows_a_friendly_message_without_textual(monkeypatch, capsys):
    for mod in list(sys.modules):
        if mod == "textual" or mod.startswith("textual.") or mod.startswith("ration.dashboard"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setitem(sys.modules, "textual", None)  # forces the next `import textual` to raise ImportError

    rc = cli.main(["dashboard"])

    assert rc == 2
    out = capsys.readouterr().out
    assert 'ration-cli[dashboard]' in out
    assert "needs the optional textual dependency" in out
