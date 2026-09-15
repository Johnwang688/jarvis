"""Durable cumulative counters and resume offsets, adapted from the firm."""
from __future__ import annotations

import json
from pathlib import Path
from jarvis.v2.provider import Usage
from .codex_config import atomically_write
from .codex_rpc import RpcError

KEYS = ("inputTokens", "outputTokens", "cachedInputTokens")


def counters(value):
    if not isinstance(value, dict) or not all(type(value.get(k)) is int and value[k] >= 0 for k in KEYS):
        raise RpcError("Codex returned invalid token usage")
    result = {k: value[k] for k in KEYS}
    if result["cachedInputTokens"] > result["inputTokens"]:
        raise RpcError("Codex cached tokens exceed input tokens")
    return result


class Accounting:
    def __init__(self, path: Path, thread_id: str, resume: bool):
        self.path, self.thread_id = path, thread_id
        self.total = dict.fromkeys(KEYS, 0)
        self.reported = dict(self.total)
        self.turns = 0
        self.complete = True
        if resume:
            try:
                saved = json.loads(path.read_text())
                if saved["thread_id"] != thread_id or saved["complete"] is not True:
                    raise ValueError("identity or completeness")
                self.total = counters(saved["total"])
                self.reported = counters(saved["reported"])
                self.turns = saved["turns"]
                if type(self.turns) is not int or self.turns < 0:
                    raise ValueError("turn count")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RpcError("Codex resume accounting missing, mismatched or incomplete") from exc
        self.baseline = None
        self.process_total = dict.fromkeys(KEYS, 0)
        self.start_total = dict(self.total)

    def save(self):
        atomically_write(self.path, json.dumps(dict(thread_id=self.thread_id, total=self.total,
                         reported=self.reported, turns=self.turns, complete=self.complete)) + "\n")

    def update(self, payload):
        raw, last = counters(payload["total"]), counters(payload["last"])
        if any(last[k] > raw[k] for k in KEYS):
            raise RpcError("Codex last usage exceeds cumulative usage")
        if self.baseline is None and not any(raw.values()):
            return
        if self.baseline is None:
            baseline = {k: raw[k] - last[k] for k in KEYS}
            if baseline not in (dict.fromkeys(KEYS, 0), self.reported):
                raise RpcError("Codex resume usage baseline unknown; usage may be incomplete")
            self.baseline = baseline
            self.process_total = baseline
        if any(raw[k] < self.process_total[k] for k in KEYS):
            raise RpcError("Codex returned decreasing cumulative token usage")
        self.process_total = self.reported = raw
        self.total = {k: self.start_total[k] + raw[k] - self.baseline[k] for k in KEYS}
        counters(self.total)
        self.save()

    def usage(self):
        return Usage(self.total["inputTokens"], self.total["outputTokens"], self.total["cachedInputTokens"], None)
