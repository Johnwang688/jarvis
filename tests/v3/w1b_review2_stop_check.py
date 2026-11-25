import unittest
from threading import Event
from unittest.mock import Mock, patch

import w1b_acceptance_check as acceptance
from jarvis.v3 import t3_client, work
from jarvis.v3.daemon import Daemon
from jarvis.v3.work_watch import FINAL


class StopAtWorkBoundary(acceptance.Base):
    def setUp(self):
        super().setUp()
        self.t3 = Mock(spec=t3_client.T3)
        self.t3.start.return_value = t3_client.Run("t-new", "r-new", "running")
        self.t3.send.return_value = t3_client.Run("t-known", "r-new", "running")
        self.service_work = work.WorkService(t3=self.t3)
        folder = self.tmp / "example-project"
        folder.mkdir()
        self.service_work._registry.add("sample-app", folder, provider="codex", model="example-model")
        ledger = self.service_work._ledger
        ledger.record_start(thread_id="t-known", run_id="r-known", project="sample-app",
                            title="Example", branch=None, brief_sha256="0" * 64, at=1)
        ledger.record_status("t-known", status="completed", waiting_on_owner=False, at=2)

    def approve(self, tool):
        args = acceptance.START if tool == "start" else acceptance.SEND
        chat = self.talk(acceptance.reply(calls=[(f"work_{tool}", args)]),
                         acceptance.reply("Done."), work=self.service_work)
        chat.send("Please run the example.")
        return chat, self.card_after()

    def cancel(self, chat, how):
        if how == "stop":
            self.assertEqual(chat.stop()["state"], "stopping")
        elif how == "close":
            self.assertFalse(chat.close(grace_s=0))
        else:
            daemon = Daemon(port=0, data_dir=self.v3, jobs=(), chat=chat)
            daemon._server = Mock()
            close = chat.close
            with patch.object(chat, "close", side_effect=lambda: close(grace_s=0)):
                daemon.stop()

    def test_cancellation_inside_work_validation_prevents_t3_call(self):
        entered, release = Event(), Event()
        validate = work._message

        def hold_message(message):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release work validation.")
            return validate(message)

        for tool in ("start", "send"):
            for how in ("stop", "close", "daemon stop"):
                with self.subTest(tool=tool, cancellation=how):
                    self.t3.reset_mock()
                    entered.clear()
                    release.clear()
                    chat, card = self.approve(tool)
                    worker = chat._worker
                    with patch.object(work, "_message", side_effect=hold_message):
                        try:
                            self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
                            self.assertTrue(entered.wait(5))
                            self.cancel(chat, how)
                            self.assertEqual(self.t3.mock_calls, [])
                        finally:
                            release.set()
                            worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(self.t3.mock_calls, [])

    def test_started_thread_remains_watchable_after_stop(self):
        entered, release = Event(), Event()

        def start(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release T3.")
            return t3_client.Run("t-new", "r-new", "running")

        self.t3.start.side_effect = start
        chat, card = self.approve("start")
        try:
            self.assertEqual(self.broker.answer(card["id"], card["digest"], "approve"), "approved")
            self.assertTrue(entered.wait(5))
            self.cancel(chat, "stop")
        finally:
            release.set()
        self.assertEqual(self.wait_idle(chat), "idle")
        thread = self.service_work._ledger.get("t-new")
        self.assertNotIn(thread.status, FINAL)


if __name__ == "__main__":
    unittest.main()
