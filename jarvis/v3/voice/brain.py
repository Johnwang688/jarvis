"""The event source and input methods used by text-only voice turns."""

from typing import Callable, Protocol


BRAIN_SETUP_CODES = frozenset({"api_key_env", "cli_missing", "private_cwd", "cli_version", "not_signed_in"})
BRAIN_ERROR_CODES = BRAIN_SETUP_CODES | {
    "connect_timeout", "connect_failed", "process_failed", "empty_response", "tool_timeout", "closed",
}


class VoiceSessionError(RuntimeError):
    """A fixed reason for failure of the voice brain."""


def brain_error_code(reason):
    return reason if isinstance(reason, str) and reason in BRAIN_ERROR_CODES else "unknown"


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
    - auto: tool, title, target, level and why describe a successful automatic
      run after tool_end. Voice may ignore this event.
    - connected: the brain has connected for this turn.
    - reply: text is the final reply. error may carry a reason code.
      reply, error, and stopped terminate the turn after its final model and tool events.

    pending_approvals(turn_id) returns a current snapshot for that turn.
    Each item has id, digest, tool and card with title and label/value rows.
    """

    def send(self, text: str, *, steer: bool, input_id: str, fresh_after: float) -> object:
        """Send plain text, starting fresh after the specified idle seconds."""
        ...

    def stop(self) -> object: ...

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]: ...

    def pending_approvals(self, turn_id: str) -> list[dict]: ...
