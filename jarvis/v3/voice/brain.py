"""The event source and input methods used by text-only voice turns."""

from typing import Callable, Protocol


class VoiceBrain(Protocol):
    """Accept utterances with send, cancel a running turn with stop, deliver
    events through subscribe, and expose live cards via pending_approvals.

    Each subscriber sees events serially in emission order, outside the
    brain's lock. Delivery may occur asynchronously, including after send
    returns. subscribe returns an unsubscribe function that discards queued
    events. An in-progress callback may finish.

    Every event has kind and turn (the brain's turn ID). Voice consumes:

    - user: input_id echoes send for a new turn. A queued follow-up instead
      lists inputs, each with its original steer seq.
    - steer: input_id echoes send, and seq identifies the offered input.
    - steering_delivered: inputs lists steer seq IDs before the model sees
      those inputs. A late steer can instead appear in a later user event.
    - delta: text is a streamed chunk from the current model step.
    - step_end: text is the complete model step, and has_tools says whether
      it requests tools. Follows its deltas and precedes any tool_start.
    - tool_start: name is the tool name. Precedes its approval and result.
    - approval_pending: the turn may now have a card in pending_approvals.
    - tool_end: name, outcome (ok, refused or failed), and changes_state
      (whether the tool is a write or otherwise acts beyond reading).
      An approval for that tool has resolved by this event.
    - reply: text is the final reply. error and stopped carry no voice fields.
      Each terminates that turn after its final model and tool events.

    pending_approvals(turn_id) returns a current snapshot for that turn.
    Each item has id, digest, tool and card with title and label/value rows.
    """

    def send(self, text: str, *, steer: bool, input_id: str, fresh_after: float) -> object:
        """Send plain text, starting fresh after the specified idle seconds."""
        ...

    def stop(self) -> object: ...

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]: ...

    def pending_approvals(self, turn_id: str) -> list[dict]: ...
