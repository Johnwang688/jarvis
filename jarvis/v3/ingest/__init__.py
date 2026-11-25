"""Polling support for index ingesters."""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import Lock
import time
from typing import TYPE_CHECKING, Iterable, Protocol

if TYPE_CHECKING:
    from jarvis.v3.index import Index


@dataclass
class Doc:
    ref: str
    title: str
    ts: float
    messages: Iterable[tuple[str, str, float | None]]
    meta: dict | None = None
    deleted: bool = False


class RateLimited(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("Rate limited")
        self.retry_after = retry_after


class Ingester(Protocol):
    name: str

    def poll(self, cursor: dict | None) -> tuple[Iterable[Doc], dict]: ...


class Runner:
    def __init__(self, index: Index, ingesters: Iterable[Ingester], *,
                 clock=time.time, interval_s=300, max_backoff_s=3600):
        self._index = index
        self._ingesters = tuple(ingesters)
        self._clock = clock
        self._interval = interval_s
        self._max_backoff = max_backoff_s
        self._next = {}
        self._backoff = {}
        self._lock = Lock()

    def tick(self) -> dict:
        """Poll each due ingester once and report its counts and next due time."""
        with self._lock:
            return {ingester.name: self._tick(ingester) for ingester in self._ingesters
                    if self._clock() >= self._next.get(ingester.name, -math.inf)}

    def _tick(self, ingester: Ingester) -> dict:
        name = ingester.name
        docs = deleted = 0
        error = None
        wait = self._interval
        try:
            docs, deleted = self._poll(ingester)
        except Exception as exc:
            error = "rate_limited" if isinstance(exc, RateLimited) else "ingest_error"
            wait = min(self._max_backoff, self._backoff.get(name, self._interval))
            if isinstance(exc, RateLimited) and isinstance(exc.retry_after, (int, float)):
                wait = min(self._max_backoff, max(wait, exc.retry_after))
            self._backoff[name] = min(self._max_backoff, 2 * wait)
        else:
            self._backoff.pop(name, None)
        next_poll = self._next[name] = self._clock() + wait
        return {"docs": docs, "deleted": deleted, "error": error, "next": next_poll}

    def _poll(self, ingester: Ingester) -> tuple[int, int]:
        name = ingester.name
        revision, cursor = self._index.get_ingest_cursor(name)
        poll_indexed = getattr(ingester, "poll_indexed", None)
        if poll_indexed is None:
            changes, new_cursor = ingester.poll(cursor)
        else:
            changes, new_cursor = poll_indexed(cursor, self._index)
        if not isinstance(new_cursor, dict):
            raise ValueError("An ingest cursor must be a JSON object.")
        docs = deleted = 0
        with self._index.write_transaction() as db:
            # A second Runner may have committed while this one was polling.
            if self._index.get_ingest_cursor(name)[0] != revision:
                return 0, 0
            for doc in changes:
                if doc.deleted:
                    self._index.remove(db, name, doc.ref)
                    deleted += 1
                else:
                    self._index.replace(db, name, doc.ref, title=doc.title, ts=doc.ts,
                                        messages=doc.messages, meta=doc.meta)
                    docs += 1
            self._index.save_ingest_cursor(db, name, new_cursor)
        return docs, deleted
