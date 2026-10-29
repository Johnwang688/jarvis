"""Mutation evidence for the shared summarizer (PM-A, 2026-10-28).

Each mutant breaks one guard in a temp copy of the package and runs the named
checks against the copy (`chat_git_mutants.run`). Every mutant must make a
check fail; one that survives is a guard no test holds. The first group are
approval summaries' own guards (#330's mutants that touch the engine or what
stays in `approval_summary.py`), run against the unchanged
`approval_summary_check.py`; the second are the engine's new ones (the
budget, its persistence, two engines apart), run against
`summary_engine_check.py`. Free: fakes, temp HOME.

The mutants of #330 that live outside these two files (the summary fed into
the QH digest, the Always rule, an allowlist write, the phone's withholding,
the deferred record's card; the summary before the card) are unchanged by
PM-A, which does not touch those files.

    python tests/v2/summary_engine_mutants.py          # all mutants
    python tests/v2/summary_engine_mutants.py 3 7      # by index
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_git_mutants import run  # noqa: E402  (same runner: temp copy, one mutation)

AS = "jarvis/v2/approval_summary.py"
EN = "jarvis/v2/summary_engine.py"
AS_CHECK = "tests/v2/approval_summary_check.py"
EN_CHECK = "tests/v2/summary_engine_check.py"

MUTANTS = [
    # -- AS's guards, against its unchanged check ---------------------------------------------
    ("AS: no scrub of what leaves", AS,
     'text = scrub(untrusted.strip_invisible("\\n".join(parts))).strip()',
     'text = untrusted.strip_invisible("\\n".join(parts)).strip()',
     # approval_summary_check.py does not hold this one (it survives there
     # on main as well: the card's projection already scrubs the command);
     # summary_engine_check.py's folder-line test does.
     [EN_CHECK, "AsInput"]),
    ("AS: a credential the card shows is sent", AS,
     "    if holds_back_credential(request.command) or holds_back_credential(request.args):\n        return None\n",
     "",
     [AS_CHECK]),
    ("AS: the agent's own description is sent", AS,
     "if str(k).lower() not in _SELF_DESCRIPTION}",
     "if True}",
     [AS_CHECK]),
    ("AS: no input cap", AS,
     "if not text or len(text) > INPUT_MAX_CHARS:",
     "if not text:",
     [AS_CHECK]),
    ("AS: a private card is sent", AS,
     '    if getattr(request, "private", False):\n        return None\n',
     "",
     [AS_CHECK]),
    ("AS: no output cap", AS,
     "    if len(text) <= OUT_MAX_CHARS:\n        return text\n",
     "    return text\n",
     [AS_CHECK]),
    ("AS: the answer is not cleaned", AS,
     "        return clean_summary(raw)",
     "        return raw",
     [AS_CHECK]),
    ("engine: no fence", EN,
     '{"role": "user", "content": untrusted.fence(text, self.source)}',
     '{"role": "user", "content": text}',
     [AS_CHECK]),
    ("engine: the chat-model pick, not the tier", EN,
     "    for model in (config.TIERS.get(tier), default):",
     "    for model in (models.selected(), default):",
     [AS_CHECK]),
    ("engine: a Chinese-hosted model is allowed", EN,
     "if model and not models.cn_hosted(model) and model not in out:",
     "if model and model not in out:",
     [AS_CHECK]),
    ("engine: no running cap", EN,
     "if left > 0 and self._slots.acquire(timeout=left):",
     "if left > 0:",
     [AS_CHECK]),
    ("engine: no waiting cap", EN,
     "if len(self._jobs) >= self.max_running + self.max_waiting:",
     "if False:",
     [AS_CHECK]),
    ("engine: begin ignores the day's cap", EN,
     "                if not self.ledger.open():",
     "                if False:",
     [AS_CHECK]),
    ("engine: the day's cap counts jobs, not calls", EN,
     "            if not self._room():\n                return False\n            self._calls += 1",
     "            self._calls += 1",
     [AS_CHECK]),
    ("engine: no watchdog", EN,
     "            timer.start()\n",
     "            pass\n",
     [AS_CHECK]),
    ("engine: identical asks do not join", EN,
     "            job = self._jobs.get(key)\n",
     "            job = None\n",
     [AS_CHECK]),
    ("engine: no cache", EN,
     "            hit = self._cache.get(key)\n",
     "            hit = None\n",
     [AS_CHECK]),
    ("engine: a job leaves the caps when its asks time out", EN,
     "            waiter.told = True\n        self._tell(waiter, FAILED, \"\")",
     "            waiter.told = True\n            for k, j in list(self._jobs.items()):\n"
     "                if waiter in j.waiters:\n                    self._jobs.pop(k)\n"
     "        self._tell(waiter, FAILED, \"\")",
     [AS_CHECK]),
    ("engine: the day never rolls", EN,
     "        day = self._today()\n        if day != self._day:\n            self._day, self._calls",
     "        day = self._today()\n        if not self._day:\n            self._day, self._calls",
     [AS_CHECK]),
    # -- the engine's new guards ---------------------------------------------------------------
    ("ledger: no cost cap", EN,
     "        return self.cost_cap_usd is None or self._cost < self.cost_cap_usd",
     "        return True",
     [EN_CHECK]),
    ("ledger: a call's cost is not spent", EN,
     "                self.ledger.spend(cost)\n",
     "",
     [EN_CHECK]),
    ("ledger: the stored budget is not read at a restart", EN,
     "            stored = self.load()\n",
     "            stored = None\n",
     [EN_CHECK]),
    ("ledger: a call is not saved", EN,
     "            self._calls += 1\n            self.save(self._day, self._calls, self._cost)\n",
     "            self._calls += 1\n",
     [EN_CHECK]),
    ("ledger: an unwritable store still lets the call go", EN,
     "            self._calls += 1\n            self.save(self._day, self._calls, self._cost)\n",
     "            self._calls += 1\n            try:\n                self.save(self._day, self._calls, self._cost)\n"
     "            except OSError:\n                pass\n",
     [EN_CHECK]),
    ("engine: a fixed nonce", EN,
     "untrusted.fence(text, self.source)}",
     'untrusted.fence(text, self.source, nonce="0123abcd")}',
     [EN_CHECK]),
    ("engine: two engines share one cache", EN,
     "        self._cache = cache if cache is not None else MemoryCache(self.cache_size)\n",
     "        if not hasattr(SummaryEngine, '_shared'):\n            SummaryEngine._shared = MemoryCache(self.cache_size)\n"
     "        self._cache = cache if cache is not None else SummaryEngine._shared\n",
     [EN_CHECK]),
    ("engine: two engines share one budget", EN,
     "        self.ledger = ledger if ledger is not None else DailyLedger(daily_cap, today=self._today)\n",
     "        if not hasattr(SummaryEngine, '_ledger'):\n"
     "            SummaryEngine._ledger = DailyLedger(daily_cap, today=self._today)\n"
     "        self.ledger = ledger if ledger is not None else SummaryEngine._ledger\n",
     [EN_CHECK]),
    ("engine: the subclass's key is ignored", EN,
     "        key = self.key(item, text)\n",
     "        key = input_key(text)\n",
     [EN_CHECK]),
    ("engine: the subclass's parser is ignored", EN,
     "                out = self.clean(reply_text(reply))\n",
     "                out = reply_text(reply).strip()\n",
     [EN_CHECK]),
]


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(index, MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}", flush=True)
        if not killed:
            survived.append(MUTANTS[index][0])
    print()
    if survived:
        print(f"{len(survived)} of {len(picks)} mutants survived")
        return 1
    print(f"all {len(picks)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
