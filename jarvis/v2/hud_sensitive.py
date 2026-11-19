"""Owner HUD region counts, used as a cross-check for the desktop bridge."""

from collections.abc import Mapping
import threading
import time

FRESH_S = 5.0
RETAIN_S = 60.0
Report = tuple[int | None, int | None, float]


def valid_count(value) -> bool:
    return type(value) is int and 0 <= value <= 64


def count(reports: Mapping[str, Report], pending_cards: int, now: float) -> int | None:
    """Return a single fresh HUD's count, or protect the entire HUD."""
    fresh = [(cards, editors) for cards, editors, received in reports.values()
             if 0 <= now - received < FRESH_S]
    if len(fresh) != 1:
        return None
    cards, editors = fresh[0]
    if not valid_count(cards) or not valid_count(editors) or (pending_cards > 0 and cards == 0):
        return None
    return cards + editors


class Reports:
    def __init__(self):
        self._lock = threading.Lock()
        self._reports: dict[str, Report] = {}
        self._expiry: threading.Timer | None = None
        self._closed = False

    def _expire(self):
        with self._lock:
            self._expiry = None
            now = time.monotonic()
            self._reports = {client: report for client, report in self._reports.items()
                             if now - report[2] < RETAIN_S}
            self._arm_expiry(now)

    def _arm_expiry(self, now):
        if self._expiry is None and self._reports:
            oldest = min(report[2] for report in self._reports.values())
            self._expiry = threading.Timer(max(0, oldest + RETAIN_S - now), self._expire)
            self._expiry.daemon = True
            self._expiry.start()

    def record(self, client: str, cards, editors) -> bool:
        valid = valid_count(cards) and valid_count(editors)
        with self._lock:
            if self._closed:
                return False
            now = time.monotonic()
            # Invalidate any previous usable report from this page on malformed counts.
            self._reports[client] = (cards, editors, now) if valid else (None, None, now)
            self._arm_expiry(now)
        return valid

    def count(self, pending_cards: int) -> int | None:
        with self._lock:
            return count(self._reports, pending_cards, time.monotonic())

    def close(self):
        with self._lock:
            self._closed = True
            self._reports.clear()
            if self._expiry is not None:
                self._expiry.cancel()
                self._expiry = None
