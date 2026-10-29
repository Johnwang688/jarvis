"""The shared one-shot summarizer (PM-A of `docs/plans/2026-10-28-pr-inbox-merge.md`).

Factored out of approval summaries (AS, `approval_summary.py`, PR #330) so
the PR summaries (PM-C) run on the same engine. What is generic lives here;
what a summary says, what it reads and how its answer is cleaned stay with
its owner, as class attributes and hooks of a subclass:

  - `system`, `source` — the system prompt, and the fence's source line;
  - `candidates()` — the models to try, in order (`tier_candidates(tier,
    default)`: a `config.TIERS` entry then the default, never a
    Chinese-hosted model);
  - `model_input(item)` — the text the model reads, before the fence; None
    when nothing may be sent (the state is then `off`);
  - `key(item, text)` — the cache and join key (the SHA-256 of the text by
    default);
  - `clean(raw)` — the model's answer as the summary (any value; a falsy one
    is a failure);
  - `label`, `log`, `thread_name`, `attempts`, `out_max_tokens`.

The base fails closed: with no `system` the state is `off`; the base
`model_input` and `clean` raise (`off`, and `failed`), so a subclass that
forgets one sends nothing or shows nothing. The fence is not a hook: the
messages are built by `_messages`, and a subclass that overrides it or
`_ask` is refused at class creation.

What the engine guarantees, for every summary built on it:

  - **Fenced, one-shot.** The model reads `system`, then `model_input`'s
    text inside one `untrusted.fence` with a fresh nonce; `cache=False`,
    `stream=False`, `provider=""` (no chat pin), `temperature=0.0`.
  - **Never in the way.** `begin` returns at once with `(state, summary)`
    and never raises. A call runs on its own thread, never under a
    caller's lock. Each ask has its own watchdog (`deadline_s`): past it the
    ask is told `failed`, while its job runs on, still counted, and its late
    answer is cached for the next identical ask.
  - **Bounded.** At most `max_running` calls at once and `max_waiting`
    more queued; an identical ask in flight joins the running job (one
    paid call); the day's budget (`DailyLedger`) counts each model call —
    each try of each job — and, when given one, a cost cap. Past a cap:
    `off`.
  - **Cached** (`MemoryCache`, a bounded LRU, unless the caller passes its
    own `get`/`put` cache).
  - **Cost.** Each call's model, tokens, cost and time are logged;
    `stats()` keeps the day's totals for this process.

The budget is a `DailyLedger`: in memory by default (a restart resets it,
as AS has always done). A caller whose caps must survive a restart (PM-6)
subclasses it and overrides `load`/`save`, or passes any object with the
same methods.

Nothing here calls a model on import or construction; only `begin` does,
and only the daemon's `main()` attaches a summarizer that is built on it.
SELF_PROTECTED (`jarvis/tools/files.py`): it decides what text reaches an
approval card, as `approval_summary.py` does.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import logging
import threading
import time
from typing import Any, Callable

from jarvis import config, llm, models, untrusted

LOG = logging.getLogger("jarvis.summary_engine")

PENDING, READY, FAILED, OFF = "pending", "ready", "failed", "off"
STATES = frozenset({PENDING, READY, FAILED, OFF})

Done = Callable[[str, Any], None]


def tier_candidates(tier: str, default: str) -> list[str]:
    """The models to try, in order: the tier (read from `config.TIERS`, never
    `models.tier()`: a one-shot summary does not follow the chat-model pick),
    then the default; never a Chinese-hosted one (`models.cn_hosted`)."""
    out: list[str] = []
    for model in (config.TIERS.get(tier), default):
        model = (model or "").strip()
        if model and not models.cn_hosted(model) and model not in out:
            out.append(model)
    return out


def input_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def reply_text(reply: Any) -> str:
    content = getattr(reply, "text", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                          for part in content if not isinstance(part, dict) or part.get("type", "text") == "text")
    return content if isinstance(content, str) else ""


# -- the day's budget ----------------------------------------------------------------

class DailyLedger:
    """The day's model calls and their cost, against the caps.

    `open()` says whether a new job may start (every cap still has room);
    `take()` reserves one model call (False once a cap is reached); `spend`
    adds a finished call's cost. A cost cap is checked before a call starts,
    so the day can end past it by at most the calls already running.

    In memory by default. To survive a restart, override `load()` (the
    stored `(day, calls, cost_usd)`, or None) and `save(day, calls,
    cost_usd)`; both run under this ledger's lock, so they must be quick and
    never call back into the engine. A `load` or `save` that raises makes
    `open`/`take` raise, which the engine treats as no room (`off` or
    `failed`): a budget that cannot be read is never spent. `load` runs once,
    under this ledger's lock; `save` runs outside it (one at a time, the
    newest state wins), so a slow store never holds up `open`. A `save` that
    raises closes the budget for this ledger's life: no further call.
    """

    def __init__(self, daily_cap: int, *, cost_cap_usd: float | None = None,
                 today: Callable[[], str] | None = None):
        self.daily_cap = int(daily_cap)
        self.cost_cap_usd = None if cost_cap_usd is None else float(cost_cap_usd)
        self._today = today or (lambda: datetime.now().date().isoformat())
        self._lock = threading.Lock()
        self._loaded = False
        self._day = ""
        self._calls = 0
        self._cost = 0.0
        self._save_lock = threading.Lock()
        self._seq = 0                       # each change's number; the newest save wins
        self._saved = 0
        self._failed = False                # a save raised: no further call

    # -- persistence hooks (a subclass overrides both) ------------------------------

    def load(self) -> tuple[str, int, float] | None:
        return None

    def save(self, day: str, calls: int, cost_usd: float) -> None:
        return None

    def _persist(self, seq: int, day: str, calls: int, cost: float) -> None:
        with self._save_lock:
            if seq <= self._saved:
                return
            try:
                self.save(day, calls, cost)
            except BaseException:
                with self._lock:
                    self._failed = True
                raise
            self._saved = seq

    # -- the budget ------------------------------------------------------------------

    def _roll(self) -> None:
        if not self._loaded:
            stored = self.load()
            if stored is not None:
                day, calls, cost = stored
                self._day, self._calls, self._cost = str(day), max(0, int(calls)), max(0.0, float(cost))
            self._loaded = True
        day = self._today()
        if day != self._day:
            self._day, self._calls, self._cost = day, 0, 0.0

    def _room(self) -> bool:
        if self._failed or self._calls >= self.daily_cap:
            return False
        return self.cost_cap_usd is None or self._cost < self.cost_cap_usd

    def open(self) -> bool:
        with self._lock:
            self._roll()
            return self._room()

    def take(self) -> bool:
        with self._lock:
            self._roll()
            if not self._room():
                return False
            self._calls += 1
            self._seq += 1
            snapshot = (self._seq, self._day, self._calls, self._cost)
        self._persist(*snapshot)
        return True

    def spend(self, cost_usd: float) -> None:
        with self._lock:
            self._roll()
            self._cost += max(0.0, float(cost_usd or 0.0))
            self._seq += 1
            snapshot = (self._seq, self._day, self._calls, self._cost)
        self._persist(*snapshot)

    def used(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._day, "calls": self._calls, "cost_usd": self._cost}

    def cost_reached(self) -> bool:
        with self._lock:
            self._roll()
            return self.cost_cap_usd is not None and self._cost >= self.cost_cap_usd


class MemoryCache:
    """A bounded LRU of finished summaries. A caller that keeps its cache on
    disk passes any object with the same `get(key)` and `put(key, value)`
    (both called under the engine's lock: quick, never calling back).
    One instance per engine: two engines handed the same cache would read
    each other's summaries (an approval card showing a PR's text)."""

    def __init__(self, size: int):
        self.size = int(size)
        self._items: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str):
        hit = self._items.get(key)
        if hit is not None:
            self._items.move_to_end(key)
        return hit

    def put(self, key: str, value) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


# -- the engine ----------------------------------------------------------------------


@dataclass
class _Waiter:
    """One ask waiting on a job: told once, by the job's end or its own watchdog."""
    done: Done
    timer: Any = None
    told: bool = False


@dataclass
class _Job:
    key: str
    text: str
    deadline: float
    waiters: list = field(default_factory=list)
    ended: bool = False             # the worker has returned (its slot is free)


class SummaryEngine:
    """One summary per distinct input, never more than the caps allow.

    `begin(item, done)` answers at once with `(state, summary)`: `("ready",
    summary)` from the cache, `("pending", "")` when a call is under way
    (then `done(state, summary)` is called exactly once, later, from another
    thread, with `ready` or `failed`), or `("off", "")` when there will be
    none (`model_input` gave nothing, no acceptable model, a cap reached,
    closed). It never raises.

    Each ask has its own watchdog (`deadline_s` from its own ask). A job
    stays counted against the caps until its worker really returns, even
    after every ask it served timed out, and an identical ask in that window
    waits on the same job rather than starting a second paid call. The
    daily cap counts model calls, each try of each job, not jobs.

    A subclass sets the class attributes and overrides the hooks below; the
    caps, the clock, the model call, the budget and the cache are
    constructor arguments, made fresh for each engine (a caller passing its
    own `ledger` or `cache` gives each engine its own instance).

    The engine's lock is never held while the ledger reads or writes its
    store, nor while a model is called.
    """

    # What a subclass sets.
    label = "summary"                       # the start of each log line
    log = LOG
    thread_name = "jarvis-summary"
    system = ""
    source = "text to be summarized"
    attempts = 2                            # tries in all, across the models, inside the deadline
    out_max_tokens = 1024

    def __init__(self, *, chat: Callable[..., Any] | None = None, deadline_s: float = 8.0,
                 daily_cap: int = 1000, max_running: int = 3, max_waiting: int = 12,
                 cache_size: int = 512, mono: Callable[[], float] = time.monotonic,
                 today: Callable[[], str] | None = None, ledger: DailyLedger | None = None,
                 cache: Any = None):
        self._chat = chat
        self.deadline_s = float(deadline_s)
        self.max_running = int(max_running)
        self.max_waiting = int(max_waiting)
        self.cache_size = int(cache_size)
        self._mono = mono
        self._today = today or (lambda: datetime.now().date().isoformat())
        self.ledger = ledger if ledger is not None else DailyLedger(daily_cap, today=self._today)
        self.daily_cap = self.ledger.daily_cap
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(self.max_running)
        self._cache = cache if cache is not None else MemoryCache(self.cache_size)
        self._jobs: dict[str, _Job] = {}
        self._closed = False
        self._day = ""
        self._stats = self._zero()

    # -- the hooks ---------------------------------------------------------------

    def candidates(self) -> list[str]:
        return []

    def model_input(self, item) -> str | None:
        raise NotImplementedError("a summary says what it sends")

    def key(self, item, text: str) -> str:
        return input_key(text)

    def clean(self, raw: str):
        raise NotImplementedError("a summary says how its answer is cleaned")

    # -- final: every engine fences ----------------------------------------------

    _FINAL = ("_messages", "_ask")

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        for name in SummaryEngine._FINAL:
            if name in cls.__dict__:
                raise TypeError(f"{cls.__name__} may not override SummaryEngine.{name}: every engine fences")

    def _messages(self, text: str) -> list[dict]:
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": untrusted.fence(text, self.source)}]

    @staticmethod
    def _zero() -> dict:
        return {"calls": 0, "jobs": 0, "ready": 0, "failed": 0, "cache_hits": 0, "off": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    # -- the asking side ---------------------------------------------------------

    def begin(self, item, done: Done) -> tuple[str, Any]:
        try:
            return self._begin(item, done)
        except Exception as exc:  # noqa: BLE001 — a summary is never worth a failure elsewhere
            self.log.warning("%s not started (%s)", self.label, type(exc).__name__)
            return OFF, ""

    def _begin(self, item, done: Done) -> tuple[str, Any]:
        if not self.system:
            self._count("off")
            return OFF, ""
        text = self.model_input(item)
        if text is None or not self.candidates():
            self._count("off")
            return OFF, ""
        key = self.key(item, text)
        waiter = _Waiter(done)
        room = None                     # the budget, read outside the lock when a new job needs it
        while True:
            start = None
            with self._lock:
                self._roll()
                if self._closed:
                    return OFF, ""
                hit = self._cache.get(key)
                if hit is not None:
                    self._stats["cache_hits"] += 1
                    return READY, hit
                job = self._jobs.get(key)
                if job is None:
                    if len(self._jobs) >= self.max_running + self.max_waiting:
                        self._stats["off"] += 1
                        return OFF, ""
                    if room is False:
                        self._stats["off"] += 1
                        return OFF, ""
                    if room:
                        self._stats["jobs"] += 1
                        job = start = _Job(key, text, self._mono() + self.deadline_s)
                        self._jobs[key] = job
                if job is not None:
                    # A job already running (its own asks' watchdogs may have
                    # fired): this ask waits on it, with a watchdog of its own.
                    job.waiters.append(waiter)
                    timer = threading.Timer(self.deadline_s, self._expire, args=(waiter,))
                    timer.daemon = True
                    waiter.timer = timer
                    break
            # The ledger's own lock and store, never under ours; then everything
            # above is read again (the cache or another ask may have moved).
            room = bool(self.ledger.open())
        try:
            timer.start()
            if start is not None:
                threading.Thread(target=self._work, args=(start,), name=self.thread_name,
                                 daemon=True).start()
        except BaseException:
            if start is not None:
                self._end(start, "")
            self._expire(waiter)
            raise
        return PENDING, ""

    def stats(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._day, **self._stats}

    def running(self) -> int:
        """Jobs whose worker has not returned yet (what the caps count)."""
        with self._lock:
            return len(self._jobs)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            waiters = [w for job in self._jobs.values() for w in job.waiters]
        for waiter in waiters:
            self._expire(waiter)

    # -- internals ---------------------------------------------------------------

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self._stats = self._zero()

    def _count(self, name: str) -> None:
        with self._lock:
            self._roll()
            self._stats[name] += 1

    def _live(self, job: _Job) -> bool:
        with self._lock:
            return not self._closed and any(not w.told for w in job.waiters)

    def _work(self, job: _Job) -> None:
        summary = ""
        try:
            left = job.deadline - self._mono()
            if left > 0 and self._slots.acquire(timeout=left):
                try:
                    if self._live(job):
                        summary = self._ask(job)
                except Exception as exc:  # noqa: BLE001
                    self.log.info("%s failed (%s)", self.label, type(exc).__name__)
                finally:
                    self._slots.release()
        finally:
            self._end(job, summary)

    def _take_call(self) -> bool:
        """One model call against the day's budget: False once a cap is reached.
        The ledger is asked outside the engine's lock."""
        if not self.ledger.take():
            return False
        with self._lock:
            self._roll()
            self._stats["calls"] += 1
        return True

    def _cap_reached(self) -> None:
        if self.ledger.cost_reached():
            self.log.info("%s: the day's cost cap of $%.2f is reached", self.label, self.ledger.cost_cap_usd)
        else:
            self.log.info("%s: the day's cap of %d calls is reached", self.label, self.daily_cap)

    def _ask(self, job: _Job):
        chat = self._chat or llm.chat
        messages = self._messages(job.text)
        tries = 0
        for model in self.candidates():
            while tries < self.attempts:
                left = job.deadline - self._mono()
                if left <= 0.05 or not self._live(job):
                    return ""
                if not self._take_call():
                    self._cap_reached()
                    return ""
                tries += 1
                started = time.monotonic()
                try:
                    reply = chat(model, messages, temperature=0.0, max_tokens=self.out_max_tokens,
                                 timeout=left, max_retries=1, stream=False, cache=False, provider="")
                except Exception as exc:  # noqa: BLE001 — the next try, or the next model
                    self.log.info("%s: %s failed (%s)", self.label, model, type(exc).__name__)
                    break
                cost = float(getattr(reply, "cost_usd", 0.0) or 0.0)
                prompt = int(getattr(reply, "prompt_tokens", 0) or 0)
                completion = int(getattr(reply, "completion_tokens", 0) or 0)
                with self._lock:
                    self._roll()
                    self._stats["prompt_tokens"] += prompt
                    self._stats["completion_tokens"] += completion
                    self._stats["cost_usd"] += cost
                self.log.info("%s: %s, %d in, %d out, $%.5f, %.2fs", self.label, model, prompt, completion,
                              cost, time.monotonic() - started)
                try:
                    self.ledger.spend(cost)
                except Exception as exc:  # noqa: BLE001 — the answer is paid for; the ledger refuses further calls
                    self.log.warning("%s: the day's spend was not recorded (%s)", self.label, type(exc).__name__)
                out = self.clean(reply_text(reply))
                if out:
                    return out
        return ""

    def _end(self, job: _Job, summary) -> None:
        """The worker returned: the job leaves the caps, its answer is
        cached (even when it came too late for its asks: the next identical
        ask reads it), and every ask still waiting is told."""
        with self._lock:
            job.ended = True
            if self._jobs.get(job.key) is job:
                self._jobs.pop(job.key)
            if summary:
                self._cache.put(job.key, summary)
            waiters = [w for w in job.waiters if not w.told]
            for w in waiters:
                w.told = True
            job.waiters = []
        for w in waiters:
            self._tell(w, READY if summary else FAILED, summary)

    def _expire(self, waiter: _Waiter) -> None:
        """An ask's watchdog (or `close`): told `failed` once; its job runs on."""
        with self._lock:
            if waiter.told:
                return
            waiter.told = True
        self._tell(waiter, FAILED, "")

    def _tell(self, waiter: _Waiter, state: str, summary) -> None:
        if waiter.timer is not None:
            waiter.timer.cancel()
        with self._lock:
            self._roll()
            self._stats["ready" if state == READY else "failed"] += 1
        try:
            waiter.done(state, summary if state == READY else "")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("%s: a waiter failed (%s)", self.label, type(exc).__name__)
