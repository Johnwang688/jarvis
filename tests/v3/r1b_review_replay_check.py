"""Check what the chat reports after automatic writes succeed or fail."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from a6b1_acceptance_check import reply
from a6c1_acceptance_check import ADD, Base
from jarvis.v3.approvals import ApprovalBroker
from jarvis.v3.policy import set_auto


class AutomaticReplay(Base):
    def setUp(self):
        super().setUp()
        set_auto(self.policy_file, True)

    def test_failed_write_keeps_failure_visible(self):
        service = self.talk(reply(calls=[("vault_add", ADD)]), reply("Fixture complete."))
        with patch("jarvis.v3.vault.Vault.add", side_effect=OSError("fixture disk error")) as write:
            service.send("Save the fixture note.")
            self.assertEqual(self.wait_idle(service), "idle")
        write.assert_called_once()
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.states(None), ["auto"])
        events = service.events(0)["events"]
        (result,) = [row for row in events if row["kind"] == "tool_end"]
        self.assertFalse(result["ok"])
        self.assertNotIn(
            "auto", [row["kind"] for row in events],
            "The HUD replaces the failed tool row with an unqualified 'Ran without asking'.",
        )


if __name__ == "__main__":
    unittest.main()
