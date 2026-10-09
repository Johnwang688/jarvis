"""Bounded, nonblocking fan-out shared by HTTP and future daemon clients."""
from __future__ import annotations

from copy import deepcopy
import queue
import threading
from typing import Callable

from .model import to_json
from .provider import Event


class Subscription(queue.Queue):
    """A queue of JSON-ready records; dropped counts evicted oldest records."""

    def __init__(self, capacity: int):
        super().__init__(maxsize=capacity)
        self.dropped = 0

    def offer(self, record: dict) -> None:
        # Use Queue's mutex so a concurrent consumer cannot race eviction.
        with self.not_empty:
            if self._qsize() >= self.maxsize:
                self._get()
                self.unfinished_tasks -= 1
                self.dropped += 1
            self._put(record)
            self.unfinished_tasks += 1
            self.not_empty.notify()


class EventBus:
    """Filters are field mappings or quick predicates over JSON-ready records.

    Predicates must not block. Subscriber consumers never run in publish().
    Every subscriber gets its own copy; shutdown bypasses every filter.
    """

    def __init__(self, capacity: int = 256):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._subscribers: dict[Subscription, dict | Callable | None] = {}
        self._observers: list[Callable[[dict], None]] = []
        self._closed = False

    def observe(self, observer: Callable[[dict], None]) -> None:
        """Call `observer(record)` synchronously on every publish, after the
        fan-out and outside the bus lock, so it may publish in turn (what it
        publishes follows the record that caused it) and can never miss a
        record the way a full subscription can. It must not block, and an
        exception it raises is swallowed."""
        with self._lock:
            self._observers.append(observer)

    def subscribe(self, filter=None) -> Subscription:
        with self._lock:
            q = Subscription(self.capacity)
            if self._closed:
                q.offer({"kind": "shutdown"})
            else:
                self._subscribers[q] = filter
            return q

    def unsubscribe(self, q: Subscription) -> None:
        with self._lock:
            self._subscribers.pop(q, None)

    def publish(self, event: Event | dict) -> None:
        record = to_json(event) if isinstance(event, Event) else event
        with self._lock:
            if self._closed:
                return
            observers = list(self._observers)
            for q, predicate in self._subscribers.items():
                try:
                    matches = (predicate is None or
                               (predicate(record) if callable(predicate) else
                                all(record.get(k) == v for k, v in predicate.items())))
                except Exception:
                    matches = False
                if matches:
                    q.offer(deepcopy(record))
        for observer in observers:
            try:
                observer(record)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                for q in self._subscribers:
                    q.offer({"kind": "shutdown"})
                self._subscribers.clear()
