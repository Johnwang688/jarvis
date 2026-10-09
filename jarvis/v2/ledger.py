"""Durable local admission accounting, design §8.3 (not subscription quota).

R6 remains open: no provider reports a verified visible window. Until one
is supplied, use local daily allowances: Codex 15M work tokens, Claude 4M,
OpenRouter fast path $5; no_new_work defaults to .85. Days use America/Chicago,
as at the firm. Claude dollars are equivalents, never subscription spend.

WP7 converts Codex counters for Thread.tokens but publishes the ORIGINAL
cumulative USAGE payload. on_event therefore takes high-water deltas per
thread; Claude and fast events are increments. Explicit usage_mode='delta'
is supported for a future normalized publisher; never difference those twice.
The runner owns mid-turn ceiling checks and admission/grace scheduling.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

from .model import ProviderName, to_json
from .provider import Event, Usage
from .stores import Stores

DAY_ZONE = ZoneInfo("America/Chicago")
DEFAULT_ALLOWANCES = {"codex": {"work_tokens": 15_000_000},
                      "claude": {"work_tokens": 4_000_000}, "fast": {"spend_usd": 5.0}}
COOLING_S = 15 * 60


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("usage and allowance values must be finite nonnegative numbers")
    return value


class UsageLedger:
    def __init__(self, stores_root, *, clock=time.time):
        self.stores = stores_root if isinstance(stores_root, Stores) else Stores(Path(stores_root))
        self.root = self.stores.root / "ledger"
        self.clock = clock
        self._lock = threading.RLock()
        self._rows = []
        self._baselines = {}
        self._seen = set()
        self._health = {}
        # Historical rows retain lifetime task ceilings and cumulative offsets;
        # daily queries select today, including after a Chicago midnight rollover.
        for path in sorted(self.root.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                self._apply(json.loads(line))

    def _day(self, at=None):
        return datetime.fromtimestamp(self.clock() if at is None else at, DAY_ZONE).date().isoformat()

    def _apply(self, row):
        self._rows.append(row)
        if row.get("event_id"):
            self._seen.add(row["event_id"])
        if "baseline" in row:
            self._baselines[row["thread_id"]] = row["baseline"]

    def _append(self, row):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / (row["day"] + ".jsonl")).open("a") as handle:
            handle.write(json.dumps(row, allow_nan=False) + "\n")
            handle.flush()
        self._apply(row)

    def on_event(self, event, *, provider=None, task_id=None, event_id=None):
        """Consume daemon Event/dict. Optional stable event_id makes replay idempotent."""
        event = to_json(event) if isinstance(event, Event) else event
        if event.get("kind") not in ("usage", "error"):
            return
        with self._lock:
            event_id = event_id or event.get("event_id")
            if event_id and event_id in self._seen:
                return
            thread_id = event.get("thread_id", "")
            thread = self.stores.threads.get(thread_id) if thread_id else None
            provider = ProviderName(provider or event.get("provider") or (thread.provider if thread else ""))
            task_id = task_id or event.get("task_id") or (thread.task_id if thread else None)
            now = self.clock()
            row = {"at": now, "day": self._day(now), "provider": provider.value,
                   "task_id": task_id, "thread_id": thread_id, "event_id": event_id,
                   "kind": event["kind"]}
            data = event.get("data", {})
            if row["kind"] == "error":
                reported = data.get("provider_reported") or {}
                signal = json.dumps([data.get("message", ""), reported]).lower()
                retry = data.get("retry_at", reported.get("retry_at"))
                if retry is None and not any(s in signal for s in
                        ("rate limit", "rate_limit", "ratelimit", "capacity", "usage limit", "429")):
                    return
                row.update(cooling_until=max(now + COOLING_S, _number(retry or 0)),
                           reason=data.get("message", "rate limit/capacity"))
            else:
                values = [_number(data.get(k, 0)) for k in ("input", "output", "cached")]
                usage = Usage(*values, data.get("cost_usd"))
                tokens = usage.work_tokens
                if provider == ProviderName.CODEX and data.get("usage_mode", "cumulative") != "delta":
                    if not thread_id:
                        raise ValueError("cumulative Codex usage requires a thread_id")
                    old = self._baselines.get(thread_id, 0)
                    row["baseline"] = max(old, tokens)
                    tokens = max(0, tokens - old)
                row["work_tokens"] = tokens
                if provider != ProviderName.CODEX and usage.cost_usd is not None:
                    key = "equivalent_usd" if provider == ProviderName.CLAUDE else "spend_usd"
                    row[key] = _number(usage.cost_usd)
            self._append(row)

    def totals(self, *, provider=None, task_id=None, day=None):
        result = {"work_tokens": 0, "equivalent_usd": 0.0, "spend_usd": 0.0}
        with self._lock:
            for row in self._rows:
                if ((provider is not None and row["provider"] != provider) or
                    (task_id is not None and row["task_id"] != task_id) or
                    (day is not None and row["day"] != day)):
                    continue
                for key in result:
                    result[key] += row.get(key, 0)
        return result

    def set_health(self, provider, ok, reason):
        with self._lock:
            self._health[provider] = (bool(ok), str(reason))

    def state(self, provider, *, no_new_work=None, allowances=None, visible_window=None):
        """Return {state, reason, totals}; visible_window is measured used/limit.

        The optional window must come from a verified provider signal, not an
        inferred subscription quota. No current adapter supplies one (R6).
        """
        if no_new_work is None or allowances is None:
            # Only the thresholds: never the chains or models, so a routing
            # table at odds with the model catalog cannot stop accounting.
            from .router import usage_settings
            settings = usage_settings()
            if no_new_work is None:
                no_new_work = settings["no_new_work"]
            if allowances is None:
                allowances = settings["allowances"]
        if isinstance(no_new_work, bool) or not 0 < no_new_work <= 1:
            raise ValueError("no_new_work must be in (0, 1]")
        provider = ProviderName(provider).value
        with self._lock:
            totals = self.totals(provider=provider, day=self._day())
            ok, reason = self._health.get(provider, (True, "health not yet checked"))
            state = "available"
            cooling = [r for r in self._rows if r["provider"] == provider and
                       r.get("cooling_until", 0) > self.clock()]
            if not ok:
                state = "unavailable"
            elif cooling:
                state, reason = "cooling", cooling[-1]["reason"]
            else:
                limits = (allowances or DEFAULT_ALLOWANCES)[provider]
                if visible_window is not None:
                    used, limit = visible_window["used"], visible_window["limit"]
                    label = "provider visible window"
                else:
                    key, limit = next(iter(limits.items()))
                    used, label = totals[key], "local daily " + key
                _number(used)
                _number(limit)
                if used >= limit * no_new_work:
                    state = "over_threshold"
                reason = f"{label}: {used:g}/{limit:g}; no_new_work={no_new_work:g}"
            return {"state": state, "reason": reason, "totals": totals}

    def ceiling_hit(self, task):
        totals = self.totals(task_id=task.id)
        elapsed = task.status.elapsed_s
        if task.status.started:
            start = datetime.fromisoformat(task.status.started)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            elapsed = max(elapsed, self.clock() - start.timestamp())
        values = {"usd": max(task.status.cost_usd, totals["equivalent_usd"] + totals["spend_usd"]),
                  "tokens": max(task.status.tokens, totals["work_tokens"]), "hours": elapsed / 3600}
        for kind, used in values.items():
            if kind in task.ceilings and used >= task.ceilings[kind]:
                return f"{kind} ceiling hit: {used:g} >= {task.ceilings[kind]:g}"
        return None
