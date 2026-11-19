"""Regressions for voice reply, native invocation, and worker lifecycles."""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests import voice_chrome_picker_check as picker_checks
from tests.voice_chrome_picker_check import PICKER, card, window
from tests import voice_profile_flow_check as flow_checks
from windows import chromepicker as cp, voiceflow, voiceui
from windows.voiceactions import LaunchStatus


class NativeCounterexamples(unittest.TestCase):
    def setUp(self):
        self.fixture = picker_checks.Choose()
        self.actions = self.fixture.make()
        self.backend = self.fixture.backend

    def test_old_background_window_cannot_confirm_a_new_launch(self):
        self.fixture.desktop.foreground_window = Mock(side_effect=
            [(1, "Notes", "notepad.exe")] + [(5, "Old - Google Chrome", "chrome.exe")] * 50)
        self.fixture.desktop.windows = lambda: [(5, "Old - Google Chrome", "chrome.exe")]
        self.actions.launcher = Mock()
        self.assertEqual(self.actions.open_app("Chrome"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)

    def test_focus_lost_in_last_identity_read_prevents_invoke(self):
        original = self.backend.identity
        ready = False

        def snapshot(pid):
            nonlocal ready
            ready = True
            return frozenset(self.backend.windows)

        def identity(hwnd):
            if ready and hwnd == PICKER.hwnd:
                self.backend.fg = 99
            return original(hwnd)

        self.backend.process_windows = snapshot
        self.backend.identity = identity
        self.actions.choose_chrome_profile("Work")
        self.assertEqual(self.backend.invoked, [])

    def test_renamed_profile_is_not_the_offered_profile(self):
        self.assertIn("Work", self.actions.chrome_profile_options())
        self.backend.card_list = [card("Work!", key=(98,))]
        self.actions.choose_chrome_profile("Work")
        self.assertEqual(self.backend.invoked, [])

    def test_distinct_punctuation_names_stay_distinct(self):
        self.backend.card_list = [card("A-B", key=(1,)), card("A B", key=(2,))]
        self.actions.choose_chrome_profile("A-B")
        self.assertEqual(self.backend.invoked, ["A-B"])

    def test_disabled_duplicate_does_not_make_name_unique(self):
        self.backend.card_list = [card("Work", key=(1,)), card("WORK", key=(2,), enabled=False)]
        self.actions.choose_chrome_profile("Work")
        self.assertEqual(self.backend.invoked, [])

    def test_failed_invoke_and_manual_other_profile_does_not_confirm(self):
        def user_selects_personal(_):
            self.backend.windows.pop(PICKER.hwnd)
            self.backend.windows[30] = window(30, aumid="Chrome")
            self.backend.fg = 30
            raise cp.PickerUnavailable("UIA did not invoke Work")

        self.backend.on_invoke = user_selects_personal
        self.assertEqual(self.actions.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)

    def test_window_observed_only_after_confirmation_deadline_is_unconfirmed(self):
        def late(chosen):
            self.fixture.clock.now += self.actions.PICKER_CONFIRM_WAIT + 1
            self.backend.open_profile_window(chosen)

        self.backend.on_invoke = late
        self.assertEqual(self.actions.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)


class FlowCounterexamples(unittest.TestCase):
    def setUp(self):
        self.flow = flow_checks.ProfileFlow()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.flow.start_picker()

    def test_idle_expiry_then_profile_reply_does_not_type(self):
        self.flow.clock.return_value += voiceflow.PROFILE_CHOICE_SECONDS + 0.25
        voiceflow.Flow.expire_profile_choice(self.flow.client)
        self.flow.client.route.return_value = {"kind": "dictate", "target": "literal", "text": "Personal."}
        self.flow.clip("Personal.")
        self.flow.typer.run.assert_not_called()

    def test_stale_clip_cannot_consume_current_profile_choice(self):
        self.flow.clip("Personal.", can_act=lambda: False)
        self.flow.clip("Personal.")
        self.flow.actions.choose_chrome_profile.assert_called_once_with("Personal")

    def test_superseded_open_command_during_profile_choice_is_discarded(self):
        self.flow.clip("Open Calendar.", can_act=lambda: False)
        self.flow.typer.run.assert_not_called()
        self.flow.client.route.assert_called_once()
        self.flow.clip("Personal.")
        self.flow.actions.choose_chrome_profile.assert_called_once_with("Personal")

    def test_retired_session_reply_does_not_type(self):
        self.flow.client.session = "new-pair"
        self.flow.client.route.return_value = {"kind": "dictate", "target": "literal", "text": "Personal."}
        self.flow.clip("Personal.")
        self.flow.typer.run.assert_not_called()

    def test_disambiguating_reply_does_not_type(self):
        self.flow.client.route.return_value = {"kind": "dictate", "target": "literal",
                                              "text": "Work Account, not Personal."}
        self.flow.clip("Work Account, not Personal.")
        self.flow.typer.run.assert_not_called()

    def test_labels_survive_through_choice_window(self):
        overlay = voiceui._DisplayState(clock=self.flow.clock)
        overlay.set(*self.flow.ui.set_state.call_args.args)
        self.flow.clock.return_value += 5
        overlay.expire()
        self.assertIn("Work Account", overlay.text)

    def test_stop_during_uia_read_prevents_invoke_and_receipt(self):
        fixture = picker_checks.Choose()
        actions = fixture.make()
        stopped = threading.Event()
        fixture.backend.on_read = lambda _: stopped.set()
        self.flow.actions.choose_chrome_profile.side_effect = actions.choose_chrome_profile
        self.flow.clip("Personal.", can_act=lambda: not stopped.is_set())
        self.assertEqual((fixture.backend.invoked, self.flow.client.confirm_action.call_count), ([], 0))

    def test_stop_during_start_catalog_refresh_prevents_launch(self):
        fixture = picker_checks.Choose()
        actions = fixture.make()
        stopped = threading.Event()
        runner = actions.runner
        def refresh(*args, **kwargs):
            stopped.set()
            return runner(*args, **kwargs)
        actions.refreshed = None
        actions.runner = refresh
        actions.launcher = Mock()
        self.flow.actions.open_app.side_effect = actions.open_app
        self.flow.clip("Open Chrome.", can_act=lambda: not stopped.is_set())
        actions.launcher.assert_not_called()

    def test_choice_expiry_during_uia_read_prevents_invoke(self):
        fixture = picker_checks.Choose()
        actions = fixture.make()
        def expire(_):
            self.flow.clock.return_value = 116.0
        fixture.backend.on_read = expire
        self.flow.actions.choose_chrome_profile.side_effect = actions.choose_chrome_profile
        self.flow.clip("Personal.")
        self.assertEqual(fixture.backend.invoked, [])

    def test_replacement_during_picker_read_keeps_ten_seconds_of_reply_recognition(self):
        fixture = picker_checks.Choose()
        actions = fixture.make()
        superseded = threading.Event()

        def replace_clip(_):
            self.flow.clock.return_value = 104.0
            superseded.set()

        fixture.backend.on_read = replace_clip
        self.flow.actions.choose_chrome_profile.side_effect = actions.choose_chrome_profile
        self.flow.clip("Personal.", can_act=lambda: not superseded.is_set())
        self.flow.client.route.return_value = {"kind": "dictate", "target": "literal", "text": "Personal."}
        self.flow.clock.return_value = 110.0
        self.flow.clip("Personal.")
        self.flow.typer.run.assert_not_called()
        self.flow.clock.return_value = 114.0
        self.flow.clip("Personal.")
        self.flow.typer.run.assert_called_once_with([("text", "Personal.")])
        self.assertEqual(fixture.backend.invoked, [])
        self.flow.client.confirm_action.assert_not_called()

    def test_session_change_during_uia_read_prevents_invoke(self):
        fixture = picker_checks.Choose()
        actions = fixture.make()
        fixture.backend.on_read = lambda _: setattr(self.flow.client, "session", "new-pair")
        self.flow.actions.choose_chrome_profile.side_effect = actions.choose_chrome_profile
        self.flow.clip("Personal.")
        self.assertEqual(fixture.backend.invoked, [])


class WorkerCounterexamples(unittest.TestCase):
    def test_abandoned_queue_cannot_invoke_after_timeout(self):
        picker = object.__new__(cp.WindowsChromePicker)
        picker.worker, picker.invoke_timeout = cp._UiaWorker(), 0
        picker.worker.started, picker.worker.queue = 1, queue.Queue()
        old_queue = picker.worker.queue
        effect = Mock()
        pattern = SimpleNamespace(QueryInterface=lambda _: SimpleNamespace(Invoke=effect))
        element = SimpleNamespace(GetCurrentPattern=lambda _: pattern)
        with self.assertRaises(cp.PickerUnavailable):
            picker.invoke(card("Work", element=(1, element)))
        job, _, _ = old_queue.get_nowait()
        job(None, SimpleNamespace(IUIAutomationInvokePattern=object()))
        effect.assert_not_called()

    def test_close_during_pattern_lookup_withdraws_pending_click(self):
        picker = object.__new__(cp.WindowsChromePicker)
        picker.worker, picker.invoke_timeout = cp._UiaWorker(), 2
        jobs = queue.Queue()
        picker.worker.started, picker.worker.queue = 1, jobs
        entered, release = threading.Event(), threading.Event()
        click = Mock()

        def lookup(_):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release the provider")
            return SimpleNamespace(QueryInterface=lambda _: SimpleNamespace(Invoke=click))

        def close_during_lookup():
            if entered.wait(1):
                picker.close()
            release.set()

        def serve():
            job, result, done = jobs.get(timeout=1)
            try:
                result["value"] = job(None, SimpleNamespace(IUIAutomationInvokePattern=0))
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        closer, provider = threading.Thread(target=close_during_lookup), threading.Thread(target=serve)
        closer.start()
        provider.start()
        try:
            with self.assertRaises(cp.PickerUnavailable) as outcome:
                picker.invoke(card("Work", element=(1, SimpleNamespace(GetCurrentPattern=lookup))))
            self.assertNotIsInstance(outcome.exception, cp.ClickSent)
        finally:
            release.set()
            closer.join(2)
            provider.join(2)
        self.assertTrue(entered.is_set())
        click.assert_not_called()

    def test_closing_actions_disposes_picker_worker(self):
        fixture = picker_checks.Choose()
        actions = fixture.make()
        fixture.backend.close = Mock()
        actions.close()
        fixture.backend.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
