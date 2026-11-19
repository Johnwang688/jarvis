"""Exercise two real Flow instances, as the Windows network worker does for two clips."""
import unittest
from unittest.mock import Mock

from windows.voiceactions import Actions, LaunchStatus
from windows.voiceflow import Flow


class ProfileChoice(unittest.TestCase):
    def setUp(self):
        self.actions = Mock(spec=Actions)
        self.actions.open_app.return_value = LaunchStatus.CHROME_PROFILE_REQUIRED
        self.actions.chrome_profile_options.return_value = ("Personal", "Work")
        self.actions.choose_chrome_profile.return_value = LaunchStatus.LAUNCHED
        self.client = Mock()
        self.client.action_receipts = True
        self.client.route.return_value = dict(kind="open_app", target="Chrome", text="Open Chrome.", receipt="ticket")
        self.typer, self.ui, self.player = Mock(), Mock(), Mock()
        self.player.cancelled.is_set.return_value = False

    def say(self, words):
        Flow(self.client, self.typer, self.actions, self.ui, self.player).on_clip(None, "hotkey", transcript=words)

    def start(self):
        self.say("Open Chrome.")
        self.client.confirm_action.assert_not_called()
        self.assertIn("profile", str(self.ui.set_state.call_args).lower())

    def test_choice_on_next_clip_verifies_before_receipt(self):
        self.start()
        self.say("Work.")
        self.actions.choose_chrome_profile.assert_called_once_with("Work")
        self.client.confirm_action.assert_called_once_with("ticket")
        self.client.route.assert_called_once()
        self.typer.run.assert_not_called()

    def test_closed_picker_or_unverified_result_never_confirms(self):
        self.start()
        self.actions.choose_chrome_profile.return_value = LaunchStatus.CHROME_WINDOW_UNCONFIRMED
        self.say("Personal.")
        self.actions.choose_chrome_profile.assert_called_once_with("Personal")
        self.client.confirm_action.assert_not_called()
        self.typer.run.assert_not_called()

    def test_cancel_does_not_click_or_type(self):
        self.start()
        self.say("Cancel.")
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.confirm_action.assert_not_called()
        self.typer.run.assert_not_called()

    def test_ambiguous_profile_never_clicks(self):
        self.actions.chrome_profile_options.return_value = ("Work", "WORK")
        self.start()
        self.say("Work.")
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.confirm_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
