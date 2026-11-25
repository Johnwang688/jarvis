"""Regressions for chat read tools and the owner-facing tool events."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from a6b1_acceptance_check import Base, Model, reply
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.tools.skills import SkillTools


class ToolRegression(Base):
    def test_skill_names_leaves_consumer_links_unchanged(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, HOME=str(home)))
        root = self.v3 / "skills"
        self.enterContext(patch.dict(os.environ, JARVIS_LEARNED_SKILLS=str(root)))
        store = SkillStore(root, ledger=self.v3 / "skill-ledger")
        skills = SkillTools(store, session="fixture")
        name = "fixture-checklist"
        result = skills.skill_manage("create", name, description="A checklist.",
                                     body="Read the checklist and record the result.")
        self.assertFalse(result.startswith("Error"), result)
        consumer_link = home / ".claude" / "skills" / name
        self.assertTrue(consumer_link.is_symlink())
        consumer_link.unlink()

        model = Model(reply(calls=[("skill_names", {})]), reply("Listed."))
        chat = self.service(model)
        chat.send("List the learned skills.")
        self.assertEqual(self.wait_idle(chat), "idle")
        results = [row["content"] for row in model.seen[-1]["messages"] if row["role"] == "tool"]
        self.assertEqual(results, [name])
        self.assertFalse(consumer_link.is_symlink(), "A chat read recreated a consumer link")

    def test_index_search_does_not_create_a_missing_index(self):
        index = self.v3 / "index.sqlite"
        self.assertFalse(index.exists())
        chat = self.service(Model(reply(calls=[("index_search", {"query": "fixture"})]),
                                  reply("Finished looking.")))
        chat.send("Look for the fixture in indexed chats.")
        self.assertEqual(self.wait_idle(chat), "idle")
        self.assertFalse(index.exists(), "A chat search initialized a database")

    def test_tool_exception_details_stay_out_of_events(self):
        marker = "private fixture details from an internal exception"
        model = Model(reply(calls=[("vault_list", {})]), reply("The lookup failed."))
        chat = self.service(model)
        with patch.object(MemoryTools, "memory_list", side_effect=RuntimeError(marker)):
            chat.send("List the vault.")
            self.assertEqual(self.wait_idle(chat), "idle")

        (result,) = [row for row in chat.events(0)["events"] if row["kind"] == "tool_end"]
        self.assertFalse(result["ok"])
        self.assertNotIn(marker, json.dumps(result))
        self.assertNotIn(marker, json.dumps(chat.snapshot()))


if __name__ == "__main__":
    unittest.main()
