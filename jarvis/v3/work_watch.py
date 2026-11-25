"""Watch the live T3 threads recorded in Jarvis's work ledger.

A thread the owner continues with work_send after its final status is no
longer watched. W1b can re-arm it with record_status(status="running").
"Gone" is a heuristic for a failed show on a thread unchanged for three days.
Rotation starts over when the daemon restarts. W1b chat tools and the W3
HUD panel and route are outside this job.
"""

from dataclasses import dataclass
import hashlib
from itertools import islice
import json
import time

from . import t3_client


MAX_PER_RUN = 12
BUDGET_S = 60
GONE_AFTER_S = 3 * 86400
_FINISHED = frozenset({"completed", "idle", "exported"})
_STOPPED = {
    "failed": "failed",
    "interrupted": "was interrupted",
    "cancelled": "was cancelled",
    "rolled_back": "was rolled back",
    "gone": "is no longer",
}
FINAL = _FINISHED.union(_STOPPED)
_T3_STATUSES = t3_client.RUNNING | t3_client.SETTLED
_RANKS = {"waiting": 2, "stopped": 3, "finished": 4}
_PHRASES = {"waiting": "is waiting on you", "finished": "finished"}


@dataclass(frozen=True)
class Result:
    outcome: str
    polled: int
    recorded: int
    posted: int
    errors: int


def _category(status, waiting):
    if status in _FINISHED:
        return "finished"
    if status in _STOPPED:
        return "stopped"
    return "waiting" if waiting else "running"


def _brief(thread, status, waiting, category, *, titled=True):
    phrase = _STOPPED[status] if category == "stopped" else _PHRASES[category]
    head, tail = f'{thread.project}: "', f'" {phrase} in T3'
    title = thread.title
    room = 120 - len(head) - len(tail)
    if len(title) > room:
        title = title[:room - 1].rstrip() + "…"
    if not titled:
        # The feed refuses some text a title may hold (an emoji joiner, say).
        # The owner is still told, without the title.
        head, title, tail = f"{thread.project}: ", "a thread", f" {phrase} in T3"

    transition = json.dumps((thread.thread_id, thread.updated_at, status, waiting))
    key = hashlib.sha256(transition.encode()).hexdigest()
    return {"kind": "news", "rank": _RANKS[category], "ask": head + title + tail,
            "context": [{"label": "T3 thread", "ref": thread.thread_id}],
            "idempotency_key": f"work-watch:{key}"}


class Watcher:
    def __init__(self, *, ledger, t3, feed, clock=time.time,
                 max_per_run=MAX_PER_RUN, budget_s=BUDGET_S):
        self._ledger = ledger
        self._t3 = t3
        self._feed = feed
        self._clock = clock
        self._max_per_run = max_per_run
        self._budget_s = budget_s
        self._cursor = None

    def _observe(self, thread):
        try:
            state = self._t3.show(thread.thread_id, messages=0)
        except t3_client.T3Error as exc:
            if exc.kind == "failed" and self._clock() - thread.updated_at >= GONE_AFTER_S:
                return "gone", False
            raise
        # An answer about another thread says nothing about this one.
        if (state.thread_id != thread.thread_id or state.status not in _T3_STATUSES
                or type(state.waiting_on_owner) is not bool):
            raise t3_client.T3Error("shape")
        return state.status, state.waiting_on_owner

    def run(self) -> Result:
        polled = recorded = posted = errors = 0
        changes = []
        try:
            started = self._clock()
            threads = self._ledger.threads()
            # Anchor the cursor in the whole ledger so final threads can
            # disappear from the live set without shifting the next poll.
            offset = next((i + 1 for i, thread in enumerate(threads)
                           if thread.thread_id == self._cursor), 0)
            live = (thread for thread in threads[offset:] + threads[:offset]
                    if thread.status not in FINAL)
            for thread in islice(live, self._max_per_run):
                if self._clock() - started >= self._budget_s:
                    break
                self._cursor = thread.thread_id
                polled += 1
                try:
                    status, waiting = self._observe(thread)
                except t3_client.T3Error as exc:
                    if exc.kind == "missing":
                        return Result("unavailable", polled, 0, 0, 0)
                    errors += 1
                    continue
                except Exception:
                    errors += 1
                    continue
                if (status, waiting) != (thread.status, thread.waiting_on_owner):
                    changes.append((thread, status, waiting))
        except Exception:
            return Result("error", polled, 0, 0, errors)

        # Finish polling before writing so an unavailable T3 leaves the
        # feed and ledger untouched even if its binary vanished mid-run.
        for thread, status, waiting in changes:
            try:
                category = _category(status, waiting)
                if category in _RANKS and category != _category(thread.status, thread.waiting_on_owner):
                    try:
                        self._feed.post(_brief(thread, status, waiting, category), actor="jarvis")
                    except Exception:
                        self._feed.post(_brief(thread, status, waiting, category, titled=False), actor="jarvis")
                    posted += 1
                self._ledger.record_status(thread.thread_id, status=status,
                                           waiting_on_owner=waiting, at=self._clock())
                recorded += 1
            except Exception:
                errors += 1
        return Result("ok", polled, recorded, posted, errors)
