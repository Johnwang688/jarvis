"""Sequential voice workflow; dependencies are injected for pure checks."""
from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger("jarvis.voice")

if __package__:
    from .voicetype import mentions_command, might_be_command, plan, sanitize
    from .voicewire import LinkError
else:
    from voicetype import mentions_command, might_be_command, plan, sanitize
    from voicewire import LinkError


class Flow:
    def __init__(self, client, typer, actions, ui, player):
        self.client, self.typer, self.actions = client, typer, actions
        self.ui, self.player = ui, player
        self.link_error = None
        self.typer.on_scratch = self._scratch

    def _state(self, state, text=None, *, log_text=None):
        if state == "error":
            self.outcome = "error"
            exc = sys.exc_info()[1]
            log.warning("%s (%s)", log_text or text or "Voice unavailable",
                        type(exc).__name__ if exc else "NoException", exc_info=True,
                        extra={"voice_private": True})
        self.ui.set_state(state, text)

    def _timed(self, stage, call, *args, **kwargs):
        start = time.perf_counter()
        try:
            return call(*args, **kwargs)
        finally:
            self.timings[stage] += (time.perf_counter() - start) * 1000

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

    def on_clip(self, wav, source):
        self.link_error = None
        self.timings = dict.fromkeys(("transcribe", "route", "ask"), 0.0)
        self.outcome, kind = "ok", "dictate"
        try:
            if source not in {"hotkey", "wake"}:
                raise ValueError("Unknown voice source")
            self.ui.set_state("transcribing")
            text = self._timed("transcribe", self.client.dictate, wav, source=source)["text"]
            if not sanitize(text):
                self.ui.set_state("done", "No speech heard")
                self.outcome = "empty"
                return
            if source == "hotkey" and not might_be_command(text):
                self._type(text)
            else:
                self.ui.set_state("thinking")
                try:
                    intent = self._timed("route", self.client.route, text, source)
                except Exception as exc:
                    log.warning("Voice routing failed; using fallback (%s)", type(exc).__name__,
                                exc_info=True, extra={"voice_private": True})
                    self.outcome = "fallback"
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

                    answer = self._timed("ask", self.client.ask, words, on_delta=delta)
                    self.ui.set_state("answering", answer)
                    self.player.play(self.client.say(answer))
                elif kind in {"open_app", "switch_app"}:
                    if not getattr(self.actions, kind)(target):
                        self._state("error", f"Could not find {target}.", log_text="Could not find [app].")
                        return
                elif kind == "search":
                    self.actions.search(target or words)
                elif kind == "read_selection":
                    selection = self.actions.read_selection()
                    if not selection:
                        self._state("error", "No selected text to read.")
                        return
                    self.ui.set_state("answering", selection)
                    self.player.play(self.client.say(selection))
                else:
                    self._state("error", "Unknown voice action.")
                    return
            self.ui.set_state("done")
        except LinkError as exc:
            self.link_error = exc
            self._state("error", str(exc))
        except Exception:
            # OS exception messages can contain document text or local paths.
            self._state("error", "The voice action could not be completed.")
        finally:
            log.info("Clip source=%s route=%s transcribe_ms=%.1f route_ms=%.1f ask_ms=%.1f outcome=%s",
                     source if source in {"hotkey", "wake"} else "unknown",
                     kind if kind in {"dictate", "ask", "open_app", "switch_app", "search", "read_selection"} else "unknown",
                     self.timings["transcribe"], self.timings["route"], self.timings["ask"], self.outcome)
