"""Offline checks that queued chat messages wait for the next turn."""

import threading
import unittest

import a6b1_acceptance_check as window


class ChatQueueCheck(window.Base):
    def start_held(self, first_result, *later_results):
        entered = threading.Event()
        release = threading.Event()

        def first(_messages):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture did not release the first completion.")
            if isinstance(first_result, Exception):
                raise first_result
            return first_result

        model = window.Model(first, *later_results)
        svc = self.service(model)
        self.addCleanup(release.set)
        svc.send("First question.")
        self.assertTrue(entered.wait(5))
        return svc, model, release

    def test_running_send_returns_unsteered_and_replays_queued_event(self):
        svc, _, _ = self.start_held(window.reply("First answer."))
        after = svc.snapshot()["seq"]
        self.assertEqual(svc.send("Phone question.", steer=False),
                         {"state": "running", "steered": False})
        (event,) = svc.events(after)["events"]
        self.assertEqual((event["kind"], event["text"], event["queued"]),
                         ("steer", "Phone question.", True))
        snapshot = svc.snapshot()
        self.assertEqual(snapshot["history"], [])
        self.assertEqual(snapshot["replay"]["events"][-1], event)

    def test_queue_is_invisible_to_the_running_turn(self):
        svc, model, release = self.start_held(window.reply(calls=[("get_datetime", {})]),
                                              window.reply("First answer."), window.reply("Next answer."))
        svc.send("Phone question.", steer=False)
        svc.send("Desk steering.")
        release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        users = [row["content"] for row in model.seen[1]["messages"] if row["role"] == "user"]
        self.assertEqual(users, ["First question.", "[owner steering] Desk steering."])

    def test_queue_runs_after_reply_and_is_saved_once(self):
        svc, model, release = self.start_held(window.reply("First answer."), window.reply("Next answer."))
        svc.send("Phone question.", steer=False)
        release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        events = svc.events(0)["events"]
        self.assertEqual([row["kind"] for row in events], ["user", "steer", "reply", "user", "reply"])
        self.assertEqual(events[3]["inputs"], [{"seq": events[1]["seq"], "text": "Phone question."}])
        self.assertEqual(len(model.seen), 2)
        self.assertIn({"role": "user", "content": "Phone question."}, model.seen[1]["messages"])
        snapshot = svc.snapshot()
        self.assertEqual(snapshot["replay"], {"history_prefix": 0, "events": events})

        self.assertTrue(svc.close())
        resumed = self.service(window.Model()).snapshot()
        self.assertEqual(resumed["session_id"], snapshot["session_id"])
        self.assertEqual(resumed["replay"], {"history_prefix": 4, "events": []})
        self.assertEqual([(row["role"], row["text"]) for row in resumed["history"]], [
            ("user", "First question."), ("assistant", "First answer."),
            ("user", "Phone question."), ("assistant", "Next answer."),
        ])

    def test_mixed_waiting_messages_run_in_send_order(self):
        svc, model, release = self.start_held(window.reply("First answer."), window.reply("Next answer."))
        messages = ["Phone one.", "Desk two.", "Phone three.", "Desk four."]
        for index, message in enumerate(messages):
            svc.send(message, steer=index % 2 == 1)
        waiting = svc.events(1)["events"]
        release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        (followup,) = [row for row in svc.events(0)["events"] if "inputs" in row]
        self.assertEqual(followup["inputs"], [{"seq": row["seq"], "text": row["text"]} for row in waiting])
        self.assertEqual(len(model.seen), 2)
        self.assertIn({"role": "user", "content": "\n\n".join(messages)}, model.seen[1]["messages"])

    def assert_discarded(self, svc, model, kind):
        events = svc.events(0)["events"]
        waiting = [row["seq"] for row in events if row["kind"] == "steer"]
        self.assertEqual(len(waiting), 2)
        self.assertEqual(events[-1]["kind"], kind)
        self.assertEqual(events[-1]["dropped"], 2)
        self.assertEqual(events[-1]["discarded"], waiting)
        self.assertEqual(len(model.seen), 1)

        svc.send("Fresh question.")
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(len(model.seen), 2)
        users = [row["content"] for row in model.seen[-1]["messages"] if row["role"] == "user"]
        self.assertEqual(users, ["First question.", "Fresh question."])
        self.assertEqual([row["text"] for row in svc.snapshot()["history"]],
                         ["First question.", "Fresh question.", "Fresh answer."])

    def test_stop_discards_queue_and_reports_its_sequences(self):
        svc, model, release = self.start_held(window.reply("Interrupted answer."), window.reply("Fresh answer."))
        svc.send("Phone question.", steer=False)
        svc.send("Desk question.")
        self.assertEqual(svc.stop(), {"state": "stopping"})
        release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assert_discarded(svc, model, "stopped")

    def test_error_discards_queue_and_reports_its_sequences(self):
        svc, model, release = self.start_held(RuntimeError("Fake provider failed."), window.reply("Fresh answer."))
        svc.send("Phone question.", steer=False)
        svc.send("Desk question.")
        release.set()
        self.assertEqual(self.wait_idle(svc), "error")
        self.assert_discarded(svc, model, "error")

    def test_inbox_cap_counts_steers_and_queued_messages(self):
        svc, _, _ = self.start_held(window.reply("First answer."))
        for index in range(self.chat.MAX_INBOX):
            svc.send(f"Waiting message {index}.", steer=index % 2 == 0)
        for steer in (True, False):
            with self.subTest(steer=steer):
                with self.assertRaisesRegex(self.chat.ChatInboxFull, "The chat steering queue is full"):
                    svc.send("One too many.", steer=steer)
        self.assertEqual(len(svc.events(1)["events"]), self.chat.MAX_INBOX)

    def test_idle_send_starts_an_ordinary_turn(self):
        model = window.Model(window.reply("First answer."))
        svc = self.service(model)
        self.assertEqual(svc.send("Phone question.", steer=False), {"state": "running", "steered": False})
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.kinds(svc), ["user", "reply"])
        self.assertIn({"role": "user", "content": "Phone question."}, model.seen[0]["messages"])

    def test_default_window_send_steers_the_current_turn(self):
        svc, model, release = self.start_held(window.reply(calls=[("get_datetime", {})]),
                                              window.reply("First answer."))
        self.assertEqual(svc.send("Desk steering."), {"state": "running", "steered": True})
        (event,) = svc.events(1)["events"]
        self.assertEqual(event["kind"], "steer")
        self.assertNotIn("queued", event)
        release.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(len(model.seen), 2)
        self.assertIn({"role": "user", "content": "[owner steering] Desk steering."}, model.seen[1]["messages"])


if __name__ == "__main__":
    unittest.main()
