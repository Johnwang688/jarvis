"""Synthetic checks for permission modes and the allowlist. Free — no API.

What must hold:
  1. default mode is "ask" and gate() defers to the surface approver
  2. allowlist entries: whole-tool, and first-word prefix for commands —
     "git" allows `git push`, never `gitfoo` or `rm`
  3. "always" answers persist entries; duplicates don't pile up
  4. mode "all" approves without consulting the approver; it is a process
     variable (nothing written), so restart semantics are automatic
  5. the broker's resolve(..., always=True) records the entry via on_always

Run:  .venv/bin/python tests/permissions_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, permissions
from jarvis.face.approvals import ApprovalBroker


def gate_checks() -> None:
    assert permissions.mode() == "ask", "default mode must be ask"

    asked = []

    def inner(tool, args):
        asked.append(tool.name)
        return False

    class FakeTool:
        name = "run_command"

    gated = permissions.gate(inner)
    assert gated(FakeTool(), {"command": "rm -rf /tmp/x"}) is False
    assert asked == ["run_command"], "ask mode must consult the surface approver"
    print("ok  gate: ask mode defers to the surface approver")


def allowlist_checks() -> None:
    entry = permissions.add_allow("run_command", {"command": "git status"})
    assert entry == {"tool": "run_command", "prefix": "git"}, entry
    assert permissions.allows("run_command", {"command": "git push origin main"})
    assert not permissions.allows("run_command", {"command": "gitfoo --evil"})
    assert not permissions.allows("run_command", {"command": "rm -rf /"})
    assert not permissions.allows("gmail_send", {"to": "a@b.c"})

    permissions.add_allow("gmail_send", {"to": "a@b.c", "subject": "s", "body": "b"})
    assert permissions.allows("gmail_send", {"to": "other@x.y"}), "whole-tool entry"

    permissions.add_allow("run_command", {"command": "git log"})  # same prefix
    entries = permissions.load_allowlist()
    assert len(entries) == 2, f"duplicate entry appended: {entries}"

    on_disk = json.loads(config.ALLOWLIST_PATH.read_text())
    assert {"tool": "run_command", "prefix": "git"} in on_disk, "must persist"

    def never(tool, args):
        raise AssertionError("allowlisted call must not reach the approver")

    class GitTool:
        name = "run_command"

    assert permissions.gate(never)(GitTool(), {"command": "git diff"}) is True
    print("ok  allowlist: prefix + whole-tool entries persist, no duplicates")


def mode_all_checks() -> None:
    def never(tool, args):
        raise AssertionError("mode all must not reach the approver")

    class Nuke:
        name = "run_command"

    permissions.set_mode("all")
    try:
        assert permissions.gate(never)(Nuke(), {"command": "rm -rf /"}) is True
    finally:
        permissions.set_mode("ask")
    try:
        permissions.set_mode("everything")
        raise AssertionError("bad mode accepted")
    except ValueError:
        pass
    print("ok  mode all: approves everything, only while set; bad modes refused")


def broker_always_checks() -> None:
    events = []
    broker = ApprovalBroker(
        broadcast=lambda kind, data: events.append((kind, data)),
        viewers=lambda: 1,
        timeout_s=5,
        announce=lambda line: None,
        on_always=permissions.add_allow,
    )

    result = {}

    def agent_side():
        result["allowed"] = broker.request("run_command", {"command": "uv pip list"})

    thread = threading.Thread(target=agent_side)
    thread.start()
    for _ in range(100):
        pending = [d for k, d in events if k == "approval"]
        if pending:
            break
        threading.Event().wait(0.05)
    assert pending, "no approval broadcast"

    assert broker.resolve(pending[0]["id"], True, always=True)
    thread.join(timeout=5)
    assert result["allowed"] is True
    assert permissions.allows("run_command", {"command": "uv sync"}), "on_always entry"
    assert broker.decisions[-1]["resolution"] == "approved-always"
    print("ok  broker: ALWAYS resolves approved and records the allowlist entry")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config.ALLOWLIST_PATH = Path(tmp) / "allowlist.json"
        gate_checks()
        allowlist_checks()
        mode_all_checks()
        broker_always_checks()
    print("\nall permission checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
