"""Check the backend-generated fixtures used by the HUD approval tests."""

import json
from pathlib import Path
import unittest

from jarvis.v3.approval_card import build_card
from jarvis.v3.chat import GATED_TOOLS


def hud_cards():
    entry = {"ref": "facts/sample-entry", "revision": "absent", "flags": [], "replacement": ""}
    existing = dict(entry, revision="a" * 64)
    event = {
        "summary": "Sample planning",
        "start": {"dateTime": "2030-01-09T10:00:00-06:00"},
        "end": {"dateTime": "2030-01-09T11:00:00-06:00"},
        "description": "Sample agenda",
    }
    before = dict(event, id="sample-event", etag='"sample-revision"')
    cases = {
        "vault_add": ({"description": "Sample note", "body": "Sample text", "source": "Sample chat"}, entry),
        "vault_patch": ({"old": "Sample text", "new": "Updated text"}, dict(existing, flags=["sample flag"])),
        "vault_archive": ({}, dict(existing, replacement="facts/sample-replacement")),
        "calendar_create": ({}, {"body": dict(event, reminders={"overrides": [{"minutes": 10}]})}),
        "calendar_update": ({}, {"before": before, "body": dict(event, summary="Updated planning")}),
        "calendar_delete": ({}, {"before": before, "body": None}),
    }
    return {tool: build_card(tool, args, preview) for tool, (args, preview) in cases.items()}


class ApprovalCardShapes(unittest.TestCase):
    def test_hud_fixtures_match_the_backend_cards(self):
        repo = Path(__file__).resolve().parents[2]
        fixture = repo / "hud3" / "tests" / "fixtures" / "approval-cards.json"
        cards = hud_cards()
        self.assertEqual(set(cards), GATED_TOOLS)
        self.assertEqual(json.loads(fixture.read_text(encoding="utf-8")), cards)


if __name__ == "__main__":
    unittest.main()
