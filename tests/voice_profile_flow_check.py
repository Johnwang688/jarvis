"""Exercise Chrome profile follow-ups across per-clip flows without Win32."""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from windows import voice_agent, voiceflow
from windows.voiceactions import LaunchStatus
from windows.voicewire import LinkError


class ProfileFlow(unittest.TestCase):
    def setUp(self):
        self.client, self.typer, self.actions, self.ui, self.player = (Mock() for _ in range(5))
        self.client.session = "paired-session"
        self.client.action_receipts = True
        self.client.route.return_value = {"kind": "open_app", "target": "Chrome", "receipt": "launch-ticket"}
        self.actions.open_app.return_value = LaunchStatus.CHROME_PROFILE_REQUIRED
        self.actions.chrome_profile_options.return_value = ("Personal", "Work Account")
        self.actions.choose_chrome_profile.return_value = LaunchStatus.LAUNCHED
        self.player.cancelled.is_set.return_value = False
        clock = patch.object(voiceflow.time, "monotonic", return_value=100.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def clip(self, words, *, source="hotkey", client=None, can_act=lambda: True):
        flow = voiceflow.Flow(client or self.client, self.typer, self.actions, self.ui, self.player, can_act=can_act)
        flow.on_clip(None, source, transcript=words)
        return flow

    def start_picker(self):
        self.clip("Open Chrome.")
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.confirm_action.assert_not_called()

    def test_overlay_has_options_and_speech_only_receives_the_generic_question(self):
        self.start_picker()
        for label in ("Personal", "Work Account"):
            self.assertIn(label, self.ui.set_state.call_args.args[1])
        self.client.say.assert_called_once_with("Which Chrome profile would you like?")
        self.player.play.assert_called_once_with(self.client.say.return_value)
        self.clip("Hey Jarvis, ＷＯＲＫ   account!", source="wake")
        self.actions.choose_chrome_profile.assert_called_once_with("Work Account")
        self.client.confirm_action.assert_called_once_with("launch-ticket")
        self.client.route.assert_called_once()
        self.client.ask.assert_not_called()

    def test_reconnect_during_grace_preserves_original_deadline(self):
        self.start_picker()
        expiry = self.client._pending_chrome_profile.expires_at
        deadline = expiry + voiceflow.PROFILE_REPLY_GRACE_SECONDS
        self.clock.return_value = expiry + 3
        retired = voiceflow.Flow.retire_profile_choice(self.client)
        other = Mock()
        other.session = "new-connection"
        other._pending_chrome_profile = retired
        other.route.return_value = {"kind": "dictate", "target": "literal", "text": "Personal."}
        self.clock.return_value = deadline - 0.01
        self.clip("Personal.", client=other)
        self.typer.run.assert_not_called()
        self.clip("I'd like Personal.", client=other)
        self.typer.run.assert_not_called()
        self.clock.return_value = deadline
        self.clip("Personal.", client=other)
        self.typer.run.assert_called_once_with([("text", "Personal.")])
        other.confirm_action.assert_not_called()
        self.actions.choose_chrome_profile.assert_not_called()

    def test_natural_unique_profile_request_confirms_once(self):
        self.start_picker()
        self.clip("I'd like to use Work Account.")
        self.actions.choose_chrome_profile.assert_called_once_with("Work Account")
        self.client.confirm_action.assert_called_once_with("launch-ticket")
        self.typer.run.assert_not_called()

    def test_no_options_keeps_manual_fallback(self):
        self.actions.chrome_profile_options.return_value = ()
        self.start_picker()
        self.ui.set_state.assert_called_with("error", "Chrome needs a profile. Choose one in Chrome.")
        self.client.say.assert_not_called()

    def test_partial_and_multi_name_replies_stay_local(self):
        for reply in ("Work.", "Personal or Work Account.", "Pick Personal, please.",
                      "Could you pick the Chrome profile Work Account?"):
            with self.subTest(reply=reply):
                self.client.route.reset_mock()
                self.start_picker()
                self.clip(reply)
                self.client.route.assert_called_once()
                self.actions.choose_chrome_profile.assert_not_called()
                self.typer.run.assert_not_called()

    def test_unrelated_dictation_resumes_and_retires_the_old_receipt(self):
        self.start_picker()
        self.client.route.side_effect = lambda text, *args, **kw: {"kind": "dictate", "target": "literal", "text": text}
        words = "Work and I sent it to finance."
        self.clip(words)
        self.typer.run.assert_called_once_with([("text", words)])
        self.clip("Work Account.")
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.confirm_action.assert_not_called()

    def test_unrelated_open_can_confirm_its_own_receipt(self):
        self.start_picker()
        self.actions.open_app.return_value = LaunchStatus.LAUNCHED
        self.client.route.return_value = {"kind": "open_app", "target": "Notepad", "receipt": "other-ticket"}
        self.clip("Open Notepad.")
        self.actions.open_app.assert_called_with("Notepad", exact=True)
        self.client.confirm_action.assert_called_once_with("other-ticket")
        self.actions.choose_chrome_profile.assert_not_called()

    def test_expiry_during_transcription_cannot_click(self):
        self.start_picker()
        self.clock.return_value += voiceflow.PROFILE_CHOICE_SECONDS
        self.clip("Personal.")
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.route.assert_called_once()
        self.typer.run.assert_not_called()

    def test_choice_is_scoped_to_client_and_paired_session(self):
        self.start_picker()
        other = Mock()
        other.route.return_value = {"kind": "dictate", "target": "literal"}
        self.clip("Personal.", client=other)
        self.client.session = "reconnected"
        self.client.route.return_value = other.route.return_value
        self.clip("Personal.")
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.confirm_action.assert_not_called()

    def test_stale_or_stopped_followup_has_no_native_side_effect(self):
        self.start_picker()
        self.clip("Personal.", can_act=lambda: False)
        self.actions.choose_chrome_profile.assert_not_called()
        self.client.route.assert_called_once()
        self.typer.run.assert_not_called()
        self.clip("Open Chrome.", can_act=lambda: False)
        self.actions.open_app.assert_called_once()

    def test_adapter_failure_consumes_choice(self):
        self.start_picker()
        self.actions.choose_chrome_profile.side_effect = OSError("native implementation detail")
        self.clip("Personal.")
        self.client.route.return_value = {"kind": "dictate", "target": "literal"}
        self.clip("Personal.")
        self.actions.choose_chrome_profile.assert_called_once_with("Personal")
        self.client.confirm_action.assert_not_called()

    def test_truthy_result_without_verified_status_does_not_confirm(self):
        self.start_picker()
        self.actions.choose_chrome_profile.return_value = True
        self.clip("Personal.")
        self.client.confirm_action.assert_not_called()

    def test_link_failure_while_prompting_clears_choice(self):
        self.client.say.side_effect = LinkError("disconnected")
        flow = self.clip("Open Chrome.")
        self.assertIsInstance(flow.link_error, LinkError)
        self.client.route.return_value = {"kind": "dictate", "target": "literal"}
        self.clip("Personal.")
        self.actions.choose_chrome_profile.assert_not_called()

    def test_network_worker_rechecks_generation_and_stop_after_wake_transcription(self):
        for invalidate in ("generation", "stop"):
            with self.subTest(invalidate=invalidate):
                self.client.confirm_action.reset_mock()
                self.client.route.reset_mock()
                self.actions.choose_chrome_profile.reset_mock()
                agent = voice_agent.Agent.__new__(voice_agent.Agent)
                agent.done, agent.online = threading.Event(), threading.Event()
                agent.flow_running, agent.recording = threading.Event(), threading.Event()
                agent.stop_requested, agent.profile_cancelled = threading.Event(), threading.Event()
                agent.overlay, agent.player = self.ui, self.player
                agent.host, agent.port, agent.key_path = "unused", 0, "unused"
                agent.user32 = SimpleNamespace(GetAsyncKeyState=lambda key: 0)
                agent.generation, agent.clips = 1, Mock(spec=queue.Queue)
                agent.discard_clips = Mock()

                def next_clip(*, timeout):
                    if not self.client.dictate.called:
                        return 1, b"open", "hotkey"
                    if self.client.dictate.call_count == 1:
                        agent.generation = 2
                        return 2, b"choice", "wake"
                    agent.done.set()
                    raise queue.Empty

                def dictate(wav, *, source):
                    if wav == b"open":
                        return {"text": "Open Chrome."}
                    if invalidate == "generation":
                        agent.generation = 3
                    else:
                        agent.stop_requested.set()
                    return {"text": "Personal."}

                agent.clips.get.side_effect = next_clip
                self.client.dictate.reset_mock(side_effect=True)
                self.client.dictate.side_effect = dictate
                with patch.object(voice_agent, "Client", return_value=self.client), \
                     patch.object(voice_agent, "load_key", return_value=b"key"), \
                     patch.object(voice_agent, "Typer", return_value=self.typer), \
                     patch.object(voice_agent, "Actions", return_value=self.actions):
                    agent.network()
                self.client.route.assert_called_once()
                self.actions.choose_chrome_profile.assert_not_called()
                self.client.confirm_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
