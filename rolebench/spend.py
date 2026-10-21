"""The OpenRouter spend ledger: $10 total, $0.50 per run (decision O-2).

One JSON file, read and written only under an exclusive `flock` on its lock
file, so concurrent threads and concurrent processes see one ledger. A
reservation is a pending entry *in the file*: it counts against the total cap
until it is settled. Writes are atomic (temp file, then `os.replace`). A ledger
that exists but cannot be read is "cap reached", never a fresh $10.

A pending entry whose process is gone (the harness was killed) is settled at
its full reservation the next time anyone reserves: unknown spend is charged
as the most it could have been.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path

from . import config


class CapReached(RuntimeError):
    pass


def _fresh() -> dict:
    return {"total_usd": 0.0, "runs": [], "aborted_models": [], "pending": {}}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Ledger:
    def __init__(self, path: Path, total_cap: float = config.TOTAL_CAP_USD,
                 per_run_cap: float = config.PER_RUN_CAP_USD):
        self.path, self.total_cap, self.per_run_cap = Path(path), total_cap, per_run_cap

    def _read(self) -> dict:
        if not self.path.exists():
            return _fresh()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or isinstance(state.get("total_usd"), bool) \
                    or not isinstance(state.get("total_usd"), (int, float)):
                raise ValueError("not a ledger")
            state.setdefault("runs", [])
            state.setdefault("aborted_models", [])
            state.setdefault("pending", {})
            return state
        except (OSError, ValueError) as exc:
            raise CapReached(f"the spend ledger {self.path} is unreadable ({exc}): treated as cap reached")

    def _write(self, state: dict) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _update(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._read()
            out = fn(state)
            self._write(state)
            return out

    def _settle_into(self, state: dict, run_id: str, model: str, usd: float, abort: bool, note: str = "") -> None:
        state["pending"].pop(run_id, None)
        state["total_usd"] = round(state["total_usd"] + usd, 6)
        row = {"run_id": run_id, "model": model, "usd": round(usd, 6)}
        if note:
            row["note"] = note
        state["runs"].append(row)
        if abort and model not in state["aborted_models"]:
            state["aborted_models"].append(model)

    def _reap(self, state: dict) -> None:
        host = socket.gethostname()
        for run_id, p in list(state["pending"].items()):
            if p.get("host") == host and isinstance(p.get("pid"), int) and not _alive(p["pid"]):
                self._settle_into(state, run_id, p.get("model", "?"), float(p.get("usd", 0.0)), False,
                                  "stale reservation: its process died; charged in full")

    def spent(self) -> float:
        return float(self._read()["total_usd"])

    def pending(self) -> dict:
        return dict(self._read()["pending"])

    def reserve(self, run_id: str, model: str) -> float:
        """The dollar budget this run may spend, or CapReached."""
        def fn(state):
            self._reap(state)                     # persisted even when the answer is no
            if model in state["aborted_models"]:
                return CapReached(f"{model} was aborted after exceeding the per-run cap")
            held = sum(float(p.get("usd", 0.0)) for p in state["pending"].values())
            left = self.total_cap - state["total_usd"] - held
            if left <= 1e-9:
                return CapReached(f"total cap ${self.total_cap:.2f} reached")
            budget = round(min(self.per_run_cap, left), 6)
            state["pending"][run_id] = {"model": model, "usd": budget, "pid": os.getpid(),
                                        "host": socket.gethostname(), "ts": time.time()}
            return budget
        out = self._update(fn)
        if isinstance(out, CapReached):
            raise out
        return out

    def settle(self, run_id: str, model: str, usd: float, abort: bool | None = None, note: str = "") -> None:
        """Charge a run and release its reservation. `abort` defaults to
        "the run reached the per-run cap"."""
        abort = (usd >= self.per_run_cap - 1e-9) if abort is None else abort
        self._update(lambda state: self._settle_into(state, run_id, model, usd, abort, note))
