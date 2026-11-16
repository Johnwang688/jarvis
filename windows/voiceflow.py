"""Sequential voice workflow; dependencies are injected for pure checks."""
from __future__ import annotations

if __package__:
    from .voicetype import might_be_command, plan, sanitize
    from .voicewire import LinkError
else:
    from voicetype import might_be_command, plan, sanitize
    from voicewire import LinkError


class Flow:
    def __init__(self, client, typer, actions, ui, player):
        self.client, self.typer, self.actions = client, typer, actions
        self.ui, self.player = ui, player
        self.link_error = None

    def on_clip(self, wav, source):
        self.link_error = None
        try:
            if source not in {"hotkey", "wake"}:
                raise ValueError("Unknown voice source")
            self.ui.set_state("transcribing")
            text = self.client.dictate(wav, source=source)["text"]
            if not sanitize(text):
                self.ui.set_state("done", "No speech heard")
                return
            if source == "hotkey" and not might_be_command(text):
                self.typer.run(plan(text))
            else:
                self.ui.set_state("thinking")
                intent = self.client.route(text, source)
                kind = intent["kind"]
                words, target = intent.get("text", text), intent.get("target", "")
                if kind == "dictate" and source == "hotkey":
                    self.typer.run(plan(words))
                elif kind in {"ask", "dictate"}:
                    partial = ""

                    def delta(piece):
                        nonlocal partial
                        partial += piece
                        self.ui.set_state("answering", partial)

                    answer = self.client.ask(words, on_delta=delta)
                    self.ui.set_state("answering", answer)
                    self.player.play(self.client.say(answer))
                elif kind in {"open_app", "switch_app"}:
                    if not getattr(self.actions, kind)(target):
                        self.ui.set_state("error", f"Could not find {target}.")
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
