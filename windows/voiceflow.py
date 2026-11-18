"""Sequential voice workflow; dependencies are injected for pure checks."""
from __future__ import annotations

import queue
import threading

if __package__:
    from .voicetype import JARVIS_ADDRESS, mentions_command, plan, sanitize, shortcut_open_target
    from .voicewire import LinkError, VoiceQuestionError
else:
    from voicetype import JARVIS_ADDRESS, mentions_command, plan, sanitize, shortcut_open_target
    from voicewire import LinkError, VoiceQuestionError


class SpokenAnswer:
    """Play WAVs in order while the link continues delivering text and sound."""
    def __init__(self, player):
        self.player = player
        self.pending = queue.Queue(maxsize=2)
        self.error = None
        self.worker = threading.Thread(target=self._play, name="voice-playback", daemon=True)

    def _play(self):
        try:
            while True:
                wav = self.pending.get()
                if wav is None:
                    return
                if not self.player.cancelled.is_set():
                    self.player.play(wav)
        except Exception:
            self.error = RuntimeError("Speech playback unavailable")

    def offer(self, wav):
        while self.worker.is_alive():
            if wav is not None and self.player.cancelled.is_set():
                return
            try:
                self.pending.put(wav, timeout=0.05)
                return
            except queue.Full:
                pass

    def ask(self, client, words, delta, *, defer_speech=False):
        self.worker.start()
        try:
            answer = client.ask_spoken(words, delta, self.offer, self.player.cancelled,
                                       defer_speech=defer_speech)
        except BaseException:
            self.player.stop()
            raise
        finally:
            self.offer(None)
            self.worker.join()
        if self.error:
            raise self.error
        return answer


class Flow:
    def __init__(self, client, typer, actions, ui, player):
        self.client, self.typer, self.actions = client, typer, actions
        self.ui, self.player = ui, player
        self.link_error = None
        self.typer.on_scratch = self._scratch

    def _scratch(self, which):
        try:
            self.client.scratch(which)
        except LinkError as exc:
            self.link_error = exc  # reconnect before the next clip, not into a dead link
        except Exception:
            pass

    def _category(self):
        try:
            app = self.typer.category()
        except Exception:
            app = "terminal"
        return "other" if app == "other" else "terminal"

    def _type(self, text):
        steps = []
        for step in plan(text):
            # The daemon refuses text over its cap; type such a step raw rather
            # than drop the link over it. Text holding a command phrase is never
            # cleaned, so command words are never altered.
            if step[0] == "text" and len(step[1]) <= 2000 and not mentions_command(step[1]):
                app = self._category()
                try:
                    got = self.client.clean(step[1], app)
                    # The focus may have moved during the request: a cleanup made
                    # for another kind of window is not typed (raw instead).
                    if (isinstance(got, dict) and isinstance(got.get("text"), str)
                            and got.get("pass") in ("off", "rules", "raw")
                            and self._category() == app):
                        step = ("text", got["text"], got["pass"])
                except LinkError as exc:
                    self.link_error = exc
                except Exception:
                    pass  # cleanup is optional; preserve every raw typing step
            steps.append(step)
        self.typer.run(steps)

    def _confirm_action(self, intent):
        receipt = intent.get("receipt")
        if receipt:
            try:
                self.client.confirm_action(receipt)
            except LinkError as exc:
                # The native action succeeded. Reconnect before the next clip
                # without replaying this action or reporting it as a failure.
                self.link_error = exc

    def on_clip(self, wav, source, *, transcript=None):
        self.link_error = None
        try:
            if source not in {"hotkey", "wake"}:
                raise ValueError("Unknown voice source")
            self.ui.set_state("transcribing")
            text = transcript if transcript is not None else self.client.dictate(wav, source=source)["text"]
            clean = sanitize(text)
            if not clean or (source == "wake" and clean.rstrip(".,!?").casefold() == "jarvis"):
                self.ui.set_state("done", "No speech heard")
                return
            if source == "hotkey" and len(text) > 2000:
                # Route replies are capped. A long paragraph stays complete
                # and literal; a quoted suffix must never become an Enter key.
                self.typer.run([("text", text)])
                self.ui.set_state("done")
                return
            addressed = source == "wake" or bool(JARVIS_ADDRESS.match(clean))
            self.ui.set_state("thinking")
            try:
                if getattr(self.client, "action_receipts", False) is True:
                    intent = self.client.route(text, source, with_receipt=True)
                else:
                    intent = self.client.route(text, source)
            except Exception as exc:
                # Routing is optional: preserve the utterance on failure.
                # Retain transport failures so the agent still reconnects.
                if isinstance(exc, LinkError):
                    self.link_error = exc
                intent = {"kind": "ask" if addressed else "dictate",
                          "text": text, "target": "" if addressed else "literal"}
            kind = intent["kind"]
            words, target = intent.get("text", text), intent.get("target", "")
            if not addressed and kind != "dictate":
                # Check the original speech even with a daemon that guesses a
                # target from prose. Only a unique exact Windows app may launch.
                candidate = shortcut_open_target(text) if kind == "open_app" else ""
                if (candidate and candidate.casefold() == target.casefold()
                        and self.actions.open_app(candidate, exact=True)):
                    self._confirm_action(intent)
                    self.ui.set_state("done", f"Opened {candidate}.")
                    return
                kind, words, target = "dictate", text, "literal"
            if kind == "dictate" and source == "hotkey":
                if target == "literal":
                    self.typer.run([("text", words)])
                else:
                    self._type(words)
            elif kind == "dictate" and target == "literal":
                self.ui.set_state("done", "Hold the dictation shortcut to type those words.")
                return
            elif kind in {"ask", "dictate"}:
                partial = ""

                def delta(piece):
                    nonlocal partial
                    partial += piece
                    self.ui.set_state("answering", partial)

                if getattr(self.client, "streaming", False) is True:
                    if self.player.cancelled.is_set():
                        return
                    answer = SpokenAnswer(self.player).ask(self.client, words, delta,
                                                           defer_speech=target == "task")
                    self.ui.set_state("answering", answer)
                else:
                    answer = self.client.ask(words, on_delta=delta)
                    self.ui.set_state("answering", answer)
                    self.player.play(self.client.say(answer))
            elif kind in {"open_app", "switch_app"}:
                if not getattr(self.actions, kind)(target):
                    self.ui.set_state("error", f"Could not find {target}.")
                    return
                self._confirm_action(intent)
                action = "Opened" if kind == "open_app" else "Switched to"
                self.ui.set_state("done", f"{action} {target}.")
                return
            elif kind == "search":
                if not self.actions.search(target or words):
                    self.ui.set_state("error", "Could not open the search.")
                    return
                self._confirm_action(intent)
            elif kind == "read_selection":
                selection = self.actions.read_selection()
                if not selection:
                    self.ui.set_state("error", "No selected text to read.")
                    return
                self.ui.set_state("answering", selection)
                self.player.play(self.client.say(selection))
                cancelled = getattr(self.player, "cancelled", None)
                if cancelled is None or not cancelled.is_set():
                    self._confirm_action(intent)
            else:
                self.ui.set_state("error", "Unknown voice action.")
                return
            self.ui.set_state("done")
        except VoiceQuestionError as exc:
            self.ui.set_state("error", str(exc))
        except LinkError as exc:
            self.link_error = exc
            self.ui.set_state("error", str(exc))
        except (OSError, RuntimeError):
            # OS exception messages can contain document text or local paths.
            self.ui.set_state("error", "The voice action could not be completed.")
