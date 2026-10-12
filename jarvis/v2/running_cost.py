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

# `Thread.cost_recount`: 0, a Claude thread from before the fix, not yet
# checked; RECOUNTED, counted right (recounted, or opened since the fix, whose
# usage is per result from the start); UNEXPLAINED, checked once and left as
# it was, because its log cannot say what it cost. Only 0 is ever read again.
RECOUNTED = 1
UNEXPLAINED = -1


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
    fix carries the CLI's running total as `cost_usd`, so its cost is derived
    with `step`. One written since carries the provider's own delta as
    `cost_usd`, which is taken as it is: the provider measured it live and saw
    what the log never records (a `/clear`'s reset — review of PR #63), and
    its `cost_total` is only where the next figure is measured from.
    -> (the thread's cost, the running total its next resume is measured
    from), or None when the log cannot say: a figure that is not a number, or
    logged figures that do not add up to `stored` (something else wrote that
    total, so the log does not explain it).
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
        if "cost_total" in data:
            total += float(added) if added is not None else 0.0
            if data["cost_total"] is not None:
                last = figure(data["cost_total"])
                if last is None:
                    return None
            continue
        if added is None:
            continue
        delta, last = step(last, float(added))
        total += delta or 0.0
    if not math.isclose(logged, stored, rel_tol=1e-9, abs_tol=1e-9):
        return None
    return total, (last or 0.0)


def recount_threads(stores, provider_name, *, log: logging.Logger = LOG) -> list[str]:
    """Once, at daemon start: put right the cost of every Claude thread whose
    total was counted before 2026-10-12, from its own log. -> the ids changed.

    Claude threads only (`provider_name`), never a Codex or fast-path one,
    and only those still at 0: a thread opened since the fix is created
    RECOUNTED (`Daemon.open_thread`), so a post-fix log is never re-derived.
    Each one checked is marked, so this reads a thread's log once however
    often the daemon starts: RECOUNTED when its log explains its total,
    UNEXPLAINED — its cost left exactly as it was, one line said once — when
    the log is unreadable or does not add up. One from a v1 session is never
    read. Reads and writes go through the store, under its lock. One lifecycle
    line per thread changed, no figures. Never raises: a thread whose record
    cannot be read or written is skipped.
    """
    from . import stores as stores_mod

    changed = []
    threads = stores.threads
    for thread_id in threads.ids():
        moved, said = False, None
        try:
            with stores_mod._lock:
                thread = threads.get(thread_id)
                if (thread is None or thread.provider != provider_name
                        or thread.cost_recount != 0 or thread.migrated_from):
                    continue
                try:
                    found = recount(threads.read_log(thread_id), thread.cost_usd)
                except Exception as exc:  # noqa: BLE001 — an unreadable log
                    found, said = None, type(exc).__name__
                if found is None:
                    thread.cost_recount = UNEXPLAINED
                else:
                    cost, running = found
                    moved = not math.isclose(cost, thread.cost_usd, rel_tol=1e-9, abs_tol=1e-9)
                    thread.cost_usd = cost
                    thread.provider_cost_total = running
                    thread.cost_recount = RECOUNTED
                threads.save(thread)
        except Exception as exc:  # noqa: BLE001 — one thread never stops a start
            log.warning("thread %s cost recount skipped (%s)", thread_id, type(exc).__name__)
            continue
        if found is None:
            log.warning("thread %s cost left as it was: its log %s", thread_id,
                        f"could not be read ({said})" if said else "does not explain it")
        elif moved:
            changed.append(thread_id)
            log.info("thread %s cost recounted", thread_id)
    return changed
