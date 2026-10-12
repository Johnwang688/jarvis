"""A provider's running cost total, turned into what each result adds
(2026-10-12).

The Claude CLI's `ResultMessage.total_cost_usd` is not a turn's cost: it is
the CLI process's **running total** for its conversation ("cumulative across
turns in streaming-input sessions — each result carries the running total so
far, so read the latest result rather than summing across results", the
2.1.295 CLI's own field description). A resumed CLI "continues from the total
its transcript saved", so the first result after a resume already carries the
earlier turns; a mid-session `/clear` resets it; a crash or startup-error
result may carry zeroes. The daemon used to add every figure to the thread's
total, so each turn re-added everything before it (thread 447ff25e read
$109.97 where its CLI said $23.22).

`step` is the one rule, used live by the Claude provider and once, over the
logs, by `recount_threads`: measure each figure from the last one seen, start a
new session at 0 and a resumed one at the total the thread saved, read a
figure below that as a total that started again from zero, and never take a
zeroed figure for a reading.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable

LOG = logging.getLogger(__name__)

# The marker `recount_threads` leaves on a Claude thread it has checked.
RECOUNTED = 1


def figure(value: Any) -> float | None:
    """A usable running total: a finite, non-negative number. Anything else
    (None, a bool, a string, NaN, a negative) is no figure at all."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def step(last: float | None, running: float | None) -> tuple[float | None, float | None]:
    """One result's figure against the last one seen on this conversation.
    -> (what the result adds, or None when that cannot be told; the total the
    next figure is measured from).

    - no figure: nothing is known about this result; the baseline holds.
    - no baseline (a resume with nothing saved): the figure carries history
      of unknown size, so it becomes the baseline and adds nothing known.
    - a zero below a baseline: a crash or startup-error result's zeroed
      values, not a reading; the next real figure still carries its cost.
    - at or above the baseline: the difference.
    - below it: the total started again from zero (a `/clear`, or a resumed
      CLI that restored less than was seen), so all of it is new.
    """
    if running is None:
        return None, last
    if last is None:
        return None, (running if running > 0 else None)
    if running == 0 and last > 0:
        return None, last
    if running >= last:
        return running - last, running
    return running, running


def recount(records: Iterable[dict], stored: float) -> tuple[float, float] | None:
    """What a Claude thread's log says its cost is.

    `records` is its `log.jsonl` in order. A usage record from before this
    fix carries the CLI's running total as `cost_usd`; one written since
    carries the provider's delta as `cost_usd` and the running total as
    `cost_total`. -> (the thread's cost, the running total its next resume
    is measured from), or None when the log cannot say: a figure that is not
    a number, or logged figures that do not add up to `stored` (something
    else wrote that total, so the log does not explain it).
    """
    total, last, logged = 0.0, 0.0, 0.0
    for record in records:
        if not isinstance(record, dict) or record.get("kind") != "usage":
            continue
        data = record.get("data")
        if not isinstance(data, dict):
            return None
        added = data.get("cost_usd")
        if added is not None:
            if figure(added) is None:
                return None
            logged += float(added)
        raw = data["cost_total"] if "cost_total" in data else added
        if raw is None:
            continue
        running = figure(raw)
        if running is None:
            return None
        delta, last = step(last, running)
        total += delta or 0.0
    if not math.isclose(logged, stored, rel_tol=1e-9, abs_tol=1e-9):
        return None
    return total, (last or 0.0)


def recount_threads(stores, provider_name, *, log: logging.Logger = LOG) -> list[str]:
    """Once, at daemon start: put right the cost of every Claude thread whose
    total was counted before 2026-10-12, from its own log. -> the ids changed.

    Claude threads only (`provider_name`), never a Codex or fast-path one.
    Each checked thread is marked (`cost_recount`), so this runs once per
    thread however often the daemon starts; one the log cannot explain, or
    that came from a v1 session, is left untouched and unmarked. Reads and
    writes go through the store, under its lock. One lifecycle line per
    thread changed, no figures. Never raises: a thread that fails is skipped.
    """
    from . import stores as stores_mod

    changed = []
    threads = stores.threads
    for thread_id in threads.ids():
        try:
            with stores_mod._lock:
                thread = threads.get(thread_id)
                if (thread is None or thread.provider != provider_name
                        or thread.cost_recount >= RECOUNTED or thread.migrated_from):
                    continue
                found = recount(threads.read_log(thread_id), thread.cost_usd)
                if found is None:
                    continue
                cost, running = found
                moved = not math.isclose(cost, thread.cost_usd, rel_tol=1e-9, abs_tol=1e-9)
                thread.cost_usd = cost
                thread.provider_cost_total = running
                thread.cost_recount = RECOUNTED
                threads.save(thread)
        except Exception as exc:  # noqa: BLE001 — one thread never stops a start
            log.warning("thread %s cost recount skipped (%s)", thread_id, type(exc).__name__)
            continue
        if moved:
            changed.append(thread_id)
            log.info("thread %s cost recounted", thread_id)
    return changed
