"""Trusted desk authority for one daemon session; never serialized.

The daemon owns this state under its lock. A slot is shared with in-flight
calls, so revocation mutates the old object rather than just replacing it.
Steer reservations exist before provider delivery; a successor consumes their
uncertainty conservatively, and a late refusal cannot re-enable that successor.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .model import Role
from .provider import Brief, UserMessage


def desk_message(brief: Brief, message: UserMessage) -> bool:
    """Exactly the fast path's owner-chat/HUD predicate (WP-F)."""
    return (brief.role == Role.CHAT and brief.task_id is None
            and message.desk is True and message.origin == "owner"
            and (message.via or "hud") == "hud")


@dataclass
class SelectedVoiceMessage(UserMessage):
    """Built only by the paired helper after resolving a selected chat in the daemon."""

    via: str | None = "voice"
    spoken: bool = True


def local_voice_message(brief: Brief, message: UserMessage) -> bool:
    """The paired Windows helper sends owner speech to an ordinary selected chat."""
    return (isinstance(message, SelectedVoiceMessage) and brief.role == Role.CHAT
            and brief.task_id is None and message.origin == "owner" and message.via == "voice"
            and message.spoken is True and message.desk is False)


@dataclass(frozen=True)
class SteerReservation:
    key: object
    slot: dict = field(compare=False, repr=False)
    turn_id: str | None


class DeskState:
    def __init__(self):
        self.slot = {"present": False}
        self.turn_id = None
        self.active = False
        self.base = False
        self.desktop_base = False
        self.desktop_revocations: set[object] = set()
        self.revocations: set[object] = set()
        self.carry: set[object] = set()
        self.pending: set[object] = set()

    def begin(self, brief: Brief, message: UserMessage, turn_id: str) -> dict:
        self.end()
        certain = not (self.carry or self.pending)
        self.base = desk_message(brief, message) and certain
        self.desktop_base = self.base or (certain and local_voice_message(brief, message))
        self.desktop_revocations.clear()
        self.carry.clear()
        self.revocations.clear()
        self.turn_id = turn_id
        self.active = True
        self.slot = {"present": self.base, "images": False, "hud_desktop": self.desktop_base}
        return self.slot

    def images(self, slot: dict, turn_id: str, capable: bool) -> None:
        """Pin capability without granting or restoring desk presence."""
        if self.active and self.slot is slot and self.turn_id == turn_id:
            self.slot["images"] = capable is True

    def reserve(self, *, local_voice: bool = False) -> SteerReservation:
        key = object()
        reservation = SteerReservation(key, self.slot, self.turn_id)
        self.carry.add(key)
        self.pending.add(key)
        self.revocations.add(key)
        self.slot["present"] = False
        if not local_voice:
            self.desktop_revocations.add(key)
            self.slot["hud_desktop"] = False
        return reservation

    def settled(self, reservation: SteerReservation) -> None:
        # Settlement never changes an existing slot, including a successor
        # which consumed carry while delivery was uncertain.
        self.pending.discard(reservation.key)

    def refused_before_write(self, reservation: SteerReservation) -> None:
        self.settled(reservation)
        self.carry.discard(reservation.key)
        if self.slot is reservation.slot and self.turn_id == reservation.turn_id:
            self.revocations.discard(reservation.key)
            self.desktop_revocations.discard(reservation.key)
            self.slot["hud_desktop"] = self.active and self.desktop_base and not self.desktop_revocations
            self.slot["present"] = self.active and self.base and not self.revocations

    def end(self) -> None:
        self.active = False
        self.slot["hud_desktop"] = False
        self.slot["present"] = False
