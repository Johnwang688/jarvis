"""BR10 activity admission races: no browser, broker, model or owner state."""
from pathlib import Path
import sys
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser.activity import BrowserActivity, ActivityRefused, ActivityToken, RestartToken, MAX_ACTIVITIES


class Activity(unittest.TestCase):
    def setUp(self):
        self.gate = BrowserActivity()
        self.assertTrue(self.gate.opened("main", 1))
        self.gate.desired(1)

    def test_owner_action_wins_before_restart_admission(self):
        active = threading.Event(); release = threading.Event(); errors = []
        def owner():
            try:
                token = self.gate.enter("main", 1)
                active.set(); release.wait(2)
                self.gate.leave(token)
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=owner); worker.start()
        try:
            self.assertTrue(active.wait(1))
            self.assertIsNone(self.gate.try_restart("main", 1, 1))
        finally: release.set(); worker.join(1)
        self.assertFalse(worker.is_alive()); self.assertEqual(errors, [])
        self.assertIsNotNone(self.gate.try_restart("main", 1, 1))

    def test_restart_wins_before_queued_owner_or_child_starts(self):
        restart = self.gate.try_restart("main", 1, 1)
        self.assertIsNotNone(restart)
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 1)
        self.assertTrue(self.gate.commit_restart(restart))
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 1)
        self.assertTrue(self.gate.opened("main", 2, restart))
        self.gate.finish_restart(restart)
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 1)
        current = self.gate.enter("main", 2); self.gate.leave(current)

    def test_canceled_waiter_does_not_release_child_cleanup(self):
        parent = self.gate.enter("main", 1)
        child = self.gate.enter("main", 1)
        self.gate.leave(parent)
        self.assertEqual(self.gate.counts("main"), (1, False, False))
        self.assertIsNone(self.gate.try_restart("main", 1, 1))
        self.gate.leave(child)
        self.assertIsNotNone(self.gate.try_restart("main", 1, 1))

    def test_unstarted_child_after_parent_cancel_does_not_run_io(self):
        parent = self.gate.enter("main", 1); self.gate.leave(parent)
        restart = self.gate.try_restart("main", 1, 1)
        calls = []
        def child():
            token = self.gate.enter("main", 1)
            try: calls.append("IO")
            finally: self.gate.leave(token)
        with self.assertRaises(ActivityRefused): child()
        self.assertEqual(calls, []); self.assertTrue(self.gate.commit_restart(restart))

    def test_forged_and_duplicate_cleanup_cannot_release_other_tokens(self):
        first = self.gate.enter("main", 1); second = self.gate.enter("main", 1)
        self.gate.leave(ActivityToken("main", 1))
        self.assertEqual(self.gate.counts("main")[0], 2)
        self.gate.leave(first); self.gate.leave(first)
        self.assertEqual(self.gate.counts("main")[0], 1)
        self.assertIsNone(self.gate.try_restart("main", 1, 1))
        self.gate.leave(second)

    def test_page_hold_cancels_uncommitted_restart_but_stale_hold_is_refused(self):
        restart = self.gate.try_restart("main", 1, 1)
        dialog = self.gate.observed_hold("main", 1)
        self.assertFalse(self.gate.commit_restart(restart))
        self.assertIsNone(self.gate.try_restart("main", 1, 1))
        self.gate.leave(dialog)
        replacement = self.gate.try_restart("main", 1, 1)
        self.assertTrue(self.gate.commit_restart(replacement))
        with self.assertRaises(ActivityRefused): self.gate.observed_hold("main", 1)

    def test_new_setting_supersedes_only_uncommitted_restart(self):
        old = self.gate.try_restart("main", 1, 1)
        self.gate.desired(2)
        self.assertFalse(self.gate.commit_restart(old))
        self.assertIsNone(self.gate.try_restart("main", 1, 1))
        current = self.gate.try_restart("main", 1, 2)
        self.assertTrue(self.gate.commit_restart(current))
        self.gate.desired(3)
        self.assertEqual(self.gate.counts("main"), (0, True, True))
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 1)
        self.assertTrue(self.gate.opened("main", 2, current))
        self.gate.finish_restart(current)
        self.assertIsNotNone(self.gate.try_restart("main", 2, 3))

    def test_stale_restart_cleanup_and_commit_cannot_free_current_reservation(self):
        old = self.gate.try_restart("main", 1, 1); self.gate.finish_restart(old)
        current = self.gate.try_restart("main", 1, 1)
        self.gate.finish_restart(old)
        self.assertFalse(self.gate.commit_restart(RestartToken("main", 1, 1)))
        self.assertEqual(self.gate.counts("main"), (0, True, False))
        self.assertTrue(self.gate.commit_restart(current))
        self.assertFalse(self.gate.commit_restart(current), "destructive commitment happens once")

    def test_owner_close_prevents_old_restore_and_waits_for_stale_cleanup(self):
        token = self.gate.enter("main", 1)
        self.gate.closed("main", 1)
        self.assertFalse(self.gate.opened("main", 2), "canceled old work still owns cleanup")
        self.gate.leave(token)
        self.assertTrue(self.gate.opened("main", 2))
        self.gate.closed("main", 1)
        self.assertIsNotNone(self.gate.enter("main", 2), "stale old close cannot retire new instance")

    def test_owner_close_during_committed_restart_does_not_resurrect(self):
        restart = self.gate.try_restart("main", 1, 1)
        self.assertTrue(self.gate.commit_restart(restart))
        self.gate.closed("main", 1)
        self.assertFalse(self.gate.opened("main", 2, restart))
        self.assertFalse(self.gate.commit_restart(restart))
        self.gate.finish_restart(restart)
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 2)

    def test_profiles_are_independent_but_generation_is_exact(self):
        self.assertTrue(self.gate.opened("other", 2))
        token = self.gate.enter("other", 2)
        self.assertIsNotNone(self.gate.try_restart("main", 1, 1))
        self.assertIsNone(self.gate.try_restart("other", 2, 1))
        self.assertIsNone(self.gate.try_restart("other", 1, 1))
        with self.assertRaises(ActivityRefused): self.gate.enter("other", 1)
        self.gate.leave(token)

    def test_bounds_and_shutdown_cannot_drop_live_cleanup_or_reopen(self):
        tokens = [self.gate.enter("main", 1) for _ in range(MAX_ACTIVITIES)]
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 1)
        self.gate.stop()
        self.assertEqual(self.gate.counts("main")[0], MAX_ACTIVITIES)
        self.assertFalse(self.gate.opened("main", 2))
        self.assertIsNone(self.gate.try_restart("main", 1, 1))
        with self.assertRaises(ActivityRefused): self.gate.enter("main", 1)
        for token in tokens: self.gate.leave(token)
        self.assertEqual(self.gate.counts("main"), (0, False, False))


if __name__ == "__main__": unittest.main()