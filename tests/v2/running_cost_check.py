"""Free checks for `jarvis.v2.running_cost` (2026-10-12): the rule that turns
the Claude CLI's running cost total into what each result added, and the
one-time recount of Claude threads counted before it. Every write stays in a
temp dir; no CLI, no daemon, no network.

Run: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/running_cost_check.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.v2 import model as m  # noqa: E402
from jarvis.v2 import running_cost  # noqa: E402
from jarvis.v2.running_cost import figure, recount, recount_threads, step  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        _failures.append(label)


def near(value, want) -> bool:
    return value is not None and want is not None and abs(value - want) < 1e-9


class Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def figure_checks() -> None:
    print("\n-- figure: a usable running total")
    check(figure(1) == 1.0 and figure(0.25) == 0.25 and figure(0) == 0.0, "numbers are figures")
    check(all(figure(v) is None for v in (None, True, False, "1.0", -0.1, float("nan"), float("inf"))),
          "None, bools, strings, negatives and non-finite numbers are not")


def step_checks() -> None:
    print("\n-- step: what a result adds")
    last, added = 0.0, []
    for running in (0.10, 0.25, 0.45):
        cost, last = step(last, running)
        added.append(cost)
    check(near(sum(added), 0.45) and near(last, 0.45), "within one process the deltas sum to the last total")
    check(step(0.45, 0.55)[0] is not None and near(step(0.45, 0.55)[0], 0.10),
          "a resumed total above the saved one: the difference")
    check(step(0.45, 0.03) == (0.03, 0.03), "below the baseline: the total started again from zero")
    check(step(0.45, None) == (None, 0.45), "no figure: unknown, and the baseline holds")
    check(step(0.45, 0.0) == (None, 0.45), "a zeroed figure below a baseline is no reading")
    check(step(0.0, 0.0) == (0.0, 0.0), "zero on a fresh conversation is zero")
    check(step(None, 2.0) == (None, 2.0), "no baseline: the figure becomes it, counted as unknown")
    check(step(None, 0.0) == (None, None), "no baseline and a zero: still no baseline")


def usage(cost, **extra) -> dict:
    return {"kind": "usage", "data": {"input": 1, "output": 1, "cached": 0, "cost_usd": cost, **extra}}


def recount_checks() -> None:
    print("\n-- recount: what a log says a thread cost")
    old = [usage(1.0), {"kind": "text", "data": {}}, usage(2.5), usage(2.5), usage(4.0)]
    check(recount(old, 1.0 + 2.5 + 2.5 + 4.0) == (4.0, 4.0),
          "records before the fix carry running totals: the cost is the last one")
    reset = [usage(1.0), usage(2.0), usage(0.5), usage(1.5)]
    found = recount(reset, 5.0)
    check(found is not None and near(found[0], 3.5) and near(found[1], 1.5),
          "a process boundary that restarted from zero is counted from its own zero")
    mixed = [usage(1.0), usage(2.0), usage(0.5, cost_total=2.5)]
    found = recount(mixed, 1.0 + 2.0 + 0.5)
    check(found is not None and near(found[0], 2.5), "records since the fix carry the delta and cost_total")
    new = [usage(0.1, cost_total=0.1), usage(0.15, cost_total=0.25)]
    found = recount(new, 0.25)
    check(found is not None and near(found[0], 0.25), "a thread counted since the fix recounts to itself")
    check(recount(old, 4.0) is None, "a stored total the logged figures do not add up to: the log cannot say")
    check(recount([usage("1.0")], 1.0) is None, "a figure that is not a number: the log cannot say")
    check(recount([{"kind": "usage", "data": "x"}], 0.0) is None, "a usage record with no data: cannot say")
    check(recount([usage(None), usage(1.0)], 1.0) == (1.0, 1.0), "a missing figure is skipped")
    check(recount([], 0.0) == (0.0, 0.0), "no usage at all, nothing stored: zero")
    check(recount([], 1.5) is None, "no usage at all but a stored cost: cannot say")


def recount_thread_checks() -> None:
    print("\n-- recount_threads: once, Claude only, through the store")
    with tempfile.TemporaryDirectory(prefix="running-cost-") as tmp:
        stores = Stores(Path(tmp) / "v2")
        project = stores.projects.create("p", tmp)

        def make(provider, figures, stored, **values):
            thread = stores.threads.create(project.id, m.Role.CHAT, provider, cost_usd=stored, **values)
            stores.threads.save(thread)
            for value in figures:
                stores.threads._append(thread.id, "log.jsonl", usage(value))
            return thread.id

        def raw(thread_id):
            return stores.threads.path(thread_id).read_bytes()

        claude = make(m.ProviderName.CLAUDE, (1.0, 2.5, 4.0), 7.5)
        codex = make(m.ProviderName.CODEX, (1.0, 2.5, 4.0), 7.5)
        fast = make(m.ProviderName.FAST, (1.0, 2.5, 4.0), 7.5)
        unexplained = make(m.ProviderName.CLAUDE, (1.0, 2.0), 9.0)
        migrated = make(m.ProviderName.CLAUDE, (1.0, 2.0), 3.0, migrated_from="v1-session")
        right = make(m.ProviderName.CLAUDE, (), 0.0)
        broken = make(m.ProviderName.CLAUDE, (1.0,), 1.0)
        with stores.threads.path(broken).with_name("log.jsonl").open("a") as handle:
            handle.write("{not json\n")
        untouched = {t: raw(t) for t in (codex, fast, unexplained, migrated, broken)}

        lines = Lines()
        changed = recount_threads(stores, m.ProviderName.CLAUDE, log=_logger(lines))
        check(changed == [claude], f"only the Claude thread its log explains changed (got {changed})")
        done = stores.threads.get(claude)
        check(near(done.cost_usd, 4.0) and near(done.provider_cost_total, 4.0) and done.cost_recount == 1,
              "it now holds its last running total, saved for the next resume, and is marked")
        check(all(raw(t) == data for t, data in untouched.items()),
              "Codex, fast, an unexplained total, a v1 migration and a corrupt log: byte for byte untouched")
        check(stores.threads.get(right).cost_recount == 1 and stores.threads.get(right).cost_usd == 0.0,
              "a Claude thread already right is marked, its cost unchanged")
        check(f"thread {claude} cost recounted" in lines.messages,
              "one lifecycle line for the thread changed")
        without_ids = [line.replace(claude, "").replace(broken, "") for line in lines.messages]
        check(len(lines.messages) == 2 and not any(ch.isdigit() for line in without_ids for ch in line),
              f"and no figure in any line, the skipped one's included ({lines.messages})")
        check(sum("recounted" in line for line in lines.messages) == 1, "and none for the unchanged")

        before = {t: raw(t) for t in (claude, right)}
        check(recount_threads(stores, m.ProviderName.CLAUDE, log=_logger(Lines())) == [],
              "a second run changes nothing")
        check(all(raw(t) == data for t, data in before.items()), "and rewrites nothing it marked")


def _logger(handler: logging.Handler) -> logging.Logger:
    log = logging.getLogger(f"running-cost-check.{id(handler)}")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.addHandler(handler)
    return log


def main() -> int:
    figure_checks()
    step_checks()
    recount_checks()
    recount_thread_checks()
    check(running_cost.RECOUNTED == 1, "the marker is 1")
    if _failures:
        print(f"\n{len(_failures)} check(s) FAILED:")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("\nall running-cost checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
