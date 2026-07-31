"""End-to-end check of the approval card in the real HUD. Free — no API calls.

Drives `jarvis.html` in a headless browser against a real face server, with a
real blocked agent thread on the other end. This is the wiring the unit checks
cannot see: SSE -> showApproval() -> a click -> POST /approve -> the waiting
thread wakes with the answer.

  1.  AUTHORIZE unblocks the waiter as approved, and the card goes away
      (and does NOT write an allowlist entry)
  1b. ALWAYS approves and persists the allowlist entry (to a temp path here —
      a UI test must never touch the real ~/.config/jarvis/allowlist.json)
  2.  DENY unblocks it as denied
  3.  Escape denies (the cheap action is the safe one)
  4.  push-to-talk is inert while a card is up
  5.  a server-side timeout withdraws the card on its own

The mic is denied on purpose — the HUD must stay usable for approvals in a
window with no microphone.

Run:  .venv/bin/python tests/face/hud_approval_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis import config  # noqa: E402

# ALWAYS clicks write through permissions.add_allow — point that at a
# throwaway file before the server module wires the broker's on_always.
_TMP = tempfile.TemporaryDirectory()
config.ALLOWLIST_PATH = Path(_TMP.name) / "allowlist.json"

from jarvis.face import server as face_server  # noqa: E402
from jarvis.face.approvals import ApprovalBroker  # noqa: E402

PORT = 8472
BASE = f"http://127.0.0.1:{PORT}"


class Waiter:
    """Runs one blocking approval request on its own thread, like the agent."""

    def __init__(self, broker: ApprovalBroker, command: str = "apt install cowsay"):
        self.broker = broker
        self.command = command
        self.result: list[bool] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.result.append(
            self.broker.request("run_command", {"command": self.command, "reason": "test"})
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(timeout=5)

    def outcome(self, timeout: float = 8.0) -> bool:
        self.thread.join(timeout=timeout)
        assert self.result, "the agent thread never came back"
        return self.result[0]


def main() -> int:
    server = face_server.create_server(PORT)
    broker = face_server.APPROVALS
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.grant_permissions([])  # explicitly no mic
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            page.wait_for_function("() => window.__hud && window.__hud.pendingAuth")
            # Let boot finish before asserting on status text: getUserMedia
            # rejects asynchronously here (no mic) and writes the status last.
            page.wait_for_function(
                "() => /NOMINAL|MICROPHONE OFFLINE/.test(document.getElementById('status').textContent)",
                timeout=15_000,
            )
            _wait_for_viewer()

            # 1. AUTHORIZE
            with Waiter(broker) as waiter:
                page.wait_for_selector(".auth", timeout=8_000)
                card = page.text_content(".auth")
                assert "run_command" in card and "apt install cowsay" in card, card
                assert page.text_content("#status").strip() == "AUTHORIZATION REQUIRED"
                assert page.is_visible("#authveil")
                page.click(".auth button:has-text('AUTHORIZE')")
                assert waiter.outcome() is True
                assert not config.ALLOWLIST_PATH.exists(), (
                    "plain AUTHORIZE must not write an allowlist entry"
                )
            page.wait_for_selector(".auth", state="detached", timeout=5_000)
            assert not page.is_visible("#authveil")
            print("ok  hud: AUTHORIZE unblocks the agent thread as approved, card clears")

            # 1b. ALWAYS approves AND persists an allowlist entry
            with Waiter(broker) as waiter:
                page.wait_for_selector(".auth", timeout=8_000)
                page.click(".auth button.always")
                assert waiter.outcome() is True
            page.wait_for_selector(".auth", state="detached", timeout=5_000)
            entries = json.loads(config.ALLOWLIST_PATH.read_text())
            assert {"tool": "run_command", "prefix": "apt"} in entries, entries
            print("ok  hud: ALWAYS approves and writes the allowlist entry")

            # 2. DENY
            with Waiter(broker) as waiter:
                page.wait_for_selector(".auth", timeout=8_000)
                page.click(".auth button.deny")
                assert waiter.outcome() is False
            page.wait_for_selector(".auth", state="detached", timeout=5_000)
            print("ok  hud: DENY unblocks it as denied")

            # 3. Escape denies
            with Waiter(broker) as waiter:
                page.wait_for_selector(".auth", timeout=8_000)
                page.keyboard.press("Escape")
                assert waiter.outcome() is False
            print("ok  hud: Escape denies — the safe answer is the cheap one")

            # 4. push-to-talk is inert while a card is up
            with Waiter(broker) as waiter:
                page.wait_for_selector(".auth", timeout=8_000)
                page.keyboard.press("Space")
                page.wait_for_timeout(300)
                assert page.text_content("#status").strip() == "AUTHORIZATION REQUIRED"
                assert page.evaluate("() => window.__hud.pendingAuth()") == 1
                page.click(".auth button.deny")
                assert waiter.outcome() is False
            print("ok  hud: space cannot start a recording over a pending card")

            # 5. a server-side timeout withdraws the card
            brief = ApprovalBroker(
                face_server.broadcast, face_server._viewers, timeout_s=1.0,
                announce=lambda m: None,
            )
            started = time.monotonic()
            with Waiter(brief) as waiter:
                page.wait_for_selector(".auth", timeout=8_000)
                assert waiter.outcome() is False
                page.wait_for_selector(".auth", state="detached", timeout=5_000)
            assert time.monotonic() - started < 6
            print("ok  hud: an unanswered card times out and withdraws itself")

            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        broker.deny_all("test-teardown")
        server.shutdown()
        server.server_close()

    print("all HUD approval checks passed")
    return 0


def _wait_for_viewer(timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if face_server._viewers():
            return
        time.sleep(0.02)
    raise AssertionError("the HUD never opened its SSE connection")


if __name__ == "__main__":
    sys.exit(main())
