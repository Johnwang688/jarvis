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
        self.revocations: set[object] = set()
        self.carry: set[object] = set()
        self.pending: set[object] = set()

    def begin(self, brief: Brief, message: UserMessage, turn_id: str) -> dict:
        self.end()
        self.base = desk_message(brief, message) and not (self.carry or self.pending)
        self.carry.clear()
        self.revocations.clear()
        self.turn_id = turn_id
        self.active = True
        self.slot = {"present": self.base, "images": False}
        return self.slot

    def images(self, slot: dict, turn_id: str, capable: bool) -> None:
        """Pin capability without granting or restoring desk presence."""
        if self.active and self.slot is slot and self.turn_id == turn_id:
            self.slot["images"] = capable is True

    def reserve(self) -> SteerReservation:
        key = object()
        reservation = SteerReservation(key, self.slot, self.turn_id)
        self.carry.add(key)
        self.pending.add(key)
        self.revocations.add(key)
        self.slot["present"] = False
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
            self.slot["present"] = self.active and self.base and not self.revocations

    def end(self) -> None:
        self.active = False
        self.slot["present"] = False
