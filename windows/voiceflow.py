"""Sequential voice workflow; dependencies are injected for pure checks."""
from __future__ import annotations

import re
import queue
import threading

if __package__:
    from .voicetype import mentions_command, might_be_command, plan, sanitize
    from .voicewire import LinkError
else:
    from voicetype import mentions_command, might_be_command, plan, sanitize
    from voicewire import LinkError


def _wake_app_intent(text):
    """Resolve explicit app commands without a model, after wake verification."""
    words = re.sub(r"^jarvis(?:\s*[,.:]\s*|\s+)", "", sanitize(text), flags=re.I)
    command = re.fullmatch(r"(?:please )?(open|launch|switch to) (.{1,80})", words.rstrip(".!?"), re.I)
    if not command:
        return None
    verb, target = command.groups()
    # Windows launch resolves the name in the Start menu index. Leave
    # sentences and specialized requests such as URLs to the normal router.
    if not re.fullmatch(r"[\w .'+&()–-]+", target) or len(target.split()) > 5:
        return None
    kind = "switch_app" if verb.casefold() == "switch to" else "open_app"
    return {"kind": kind, "text": text, "target": target}


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
            if source == "hotkey" and not might_be_command(text):
                self._type(text)
            else:
                self.ui.set_state("thinking")
                try:
                    intent = _wake_app_intent(text) if source == "wake" else None
                    if intent is None:
                        intent = self.client.route(text, source)
                except Exception as exc:
                    # Routing is optional: preserve the utterance on failure.
                    # Retain transport failures so the agent still reconnects.
                    if isinstance(exc, LinkError):
                        self.link_error = exc
                    intent = {"kind": "dictate" if source == "hotkey" else "ask",
                              "text": text, "target": ""}
                kind = intent["kind"]
                words, target = intent.get("text", text), intent.get("target", "")
                if kind == "dictate" and source == "hotkey":
                    self._type(words)
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
                    action = "Opened" if kind == "open_app" else "Switched to"
                    self.ui.set_state("done", f"{action} {target}.")
                    return
                elif kind == "search":
                    self.actions.search(target or words)
                elif kind == "read_selection":
                    selection = self.actions.read_selection()
                    if not selection:
                        self.ui.set_state("error", "No selected text to read.")
                        return
                    self.ui.set_state("answering", selection)
                    self.player.play(self.client.say(selection))
                else:
                    self.ui.set_state("error", "Unknown voice action.")
                    return
            self.ui.set_state("done")
        except LinkError as exc:
            self.link_error = exc
            self.ui.set_state("error", str(exc))
        except (OSError, RuntimeError):
            # OS exception messages can contain document text or local paths.
            self.ui.set_state("error", "The voice action could not be completed.")
