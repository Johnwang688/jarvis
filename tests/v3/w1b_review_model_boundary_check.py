import json
import unittest
from dataclasses import replace
from threading import Event
from unittest.mock import Mock, patch

import w1b_acceptance_check as acceptance
from jarvis.v3 import fastpath, t3_client
from jarvis.v3.work import WorkError, WorkService


class ModelBoundary(acceptance.Base):
    def test_stop_during_grant_validation_prevents_the_start(self):
        validating, release = Event(), Event()
        digest = fastpath.digest

        def held_digest(*args):
            validating.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release grant validation.")
            return digest(*args)

        svc = self.talk(acceptance.reply(calls=[("work_start", acceptance.START)]),
                        acceptance.reply("Done."))
        with patch.object(fastpath, "digest", held_digest):
            svc.send("Start work in the example project.")
            item = self.card_after()
            self.broker.answer(item["id"], item["digest"], "approve")
            try:
                self.assertTrue(validating.wait(5))
                self.assertEqual(svc.stop()["state"], "stopping")
                self.assertEqual(self.work.t3, [])
            finally:
                release.set()
            self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.work.t3, [])

    def test_invisible_folder_is_refused_on_registration_and_start(self):
        t3 = Mock(spec=t3_client.T3)
        t3.start.return_value = t3_client.Run("t-new", "r-new", "running")
        work = WorkService(t3=t3)
        visible = self.tmp / "example"
        actual = self.tmp / "exam\u200bple"
        visible.mkdir()
        actual.mkdir()
        fields = {"provider": "codex", "model": "example-model"}
        with self.assertRaises(WorkError):
            work._registry.add("sample-app", actual, **fields)
        registry = work._registry.path
        registry.parent.mkdir(mode=0o700, parents=True)
        registry.write_text(json.dumps({"v": 1, "projects": {
            "sample-app": {**fields, "path": str(actual), "base": None},
        }}))
        registry.chmod(0o600)
        svc = self.talk(acceptance.reply(calls=[("work_start", acceptance.START)]),
                        acceptance.reply("Done."), work=work)
        with patch.object(self.broker, "ask", return_value=False) as ask:
            refused, = self.run_turn(svc, "Start work in the example project.")
        self.assertTrue(refused.startswith(("Error:", "Refused:")))
        ask.assert_not_called()
        self.assertEqual(t3.mock_calls, [])

    def test_work_show_rejects_a_response_for_a_different_thread(self):
        class FakeT3:
            def show(self, thread_id, *, messages):
                calls.append((thread_id, messages))
                return t3_client.State("t-other", "running", False, 0, "Other example",
                                       "codex", "example-model", [("assistant", marker)])

        calls = []
        marker = "Fixture output from the other thread."
        work = WorkService(t3=FakeT3())
        folder = self.tmp / "example-project"
        folder.mkdir()
        work._registry.add("sample-app", folder, provider="codex", model="example-model")
        work._ledger.record_start(thread_id="t-known", run_id="r-known",
                                  project="sample-app", title="Known example", branch=None,
                                  brief_sha256="0" * 64, at=1000.0)
        svc = self.talk(acceptance.reply(calls=[("work_show", {"thread_id": "t-known"})]),
                        acceptance.reply("Done."), work=work)
        shown, = self.run_turn(svc)
        self.assertEqual(calls, [("t-known", 4)])
        self.assertNotIn(marker, shown)

    def test_project_listing_does_not_disclose_absolute_folders(self):
        project = replace(acceptance.APP, path="/srv/example/private-example-folder")
        svc = self.talk(acceptance.reply(calls=[("work_projects", {})]),
                        acceptance.reply("Done."), work=acceptance.FakeWork(projects=[project]))
        listed, = self.run_turn(svc)
        self.assertIn(project.name, listed)
        self.assertNotIn(project.path, listed)



if __name__ == "__main__":
    unittest.main()
