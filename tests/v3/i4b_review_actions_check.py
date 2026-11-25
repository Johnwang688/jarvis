"""I4b regressions using private stores and an in-process Calendar transport."""

from contextlib import contextmanager
from datetime import timedelta
import json
import threading
import unittest
from unittest.mock import patch

import httpx

import executor_actions_check as fixture
from jarvis.v3.policy import set_rule
from jarvis.v3.proposals import action_sha256
from jarvis.v3.tools.calendar import CalendarTools


DEFAULT_REQUEST = CalendarTools._default_request


class Review(fixture.ExecutorActions):
    def test_vault_refuses_a_target_other_than_the_one_on_the_card(self):
        draft = self.draft(fixture.VAULT)
        draft.action["args"]["name"] = "fake-other-note"
        proposal = self.store.add(draft)
        brief = self.store.brief(proposal["id"], self.feed)
        self.feed.answer(brief, "approve")

        self.executor.sweep()

        self.assertEqual(len(self.vault.entries()), 0)
        self.assert_failed(proposal)

    def test_calendar_grant_repr_hides_private_text(self):
        proposal = self.approve(fixture.CALENDAR)
        self.executor.sweep()

        grant = self.calendar_factory.call_args.kwargs["approve"]
        args = proposal["action"]["args"]
        self.assertFalse(any(args[field] in repr(grant) for field in ("title", "description")),
                         "The grant repr contains approved event text.")


if __name__ == "__main__":
    unittest.main()
