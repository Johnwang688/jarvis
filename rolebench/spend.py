"""The OpenRouter spend ledger: $10 total, $0.50 per run (decision O-2)."""

from __future__ import annotations

import fcntl
import json
import threading
from pathlib import Path

from . import config


class CapReached(RuntimeError):
    pass


def estimate(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Cost at the pinned provider's price (Kimi K3: $3 / $15 per M), or None."""
    price = config.PRICE_OVERRIDES.get(model)
    if not price:
        return None
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1e6


class Ledger:
    def __init__(self, path: Path, total_cap: float = config.TOTAL_CAP_USD,
                 per_run_cap: float = config.PER_RUN_CAP_USD):
        self.path, self.total_cap, self.per_run_cap = Path(path), total_cap, per_run_cap
        self._reserved: dict[str, float] = {}
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"total_usd": 0.0, "runs": [], "aborted_models": []}

    def _update(self, fn) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._read()
            fn(state)
            self.path.write_text(json.dumps(state, indent=1), encoding="utf-8")
            return state

    def spent(self) -> float:
        return float(self._read()["total_usd"])

    def reserve(self, run_id: str, model: str) -> float:
        """The dollar budget this run may spend, or CapReached."""
        with self._lock:
            state = self._read()
            if model in state["aborted_models"]:
                raise CapReached(f"{model} was aborted after exceeding the per-run cap")
            left = self.total_cap - state["total_usd"] - sum(self._reserved.values())
            if left <= 1e-9:
                raise CapReached(f"total cap ${self.total_cap:.2f} reached")
            budget = min(self.per_run_cap, left)
            self._reserved[run_id] = budget
            return budget

    def settle(self, run_id: str, model: str, usd: float) -> None:
        with self._lock:
            self._reserved.pop(run_id, None)

            def fn(state):
                state["total_usd"] = round(state["total_usd"] + usd, 6)
                state["runs"].append({"run_id": run_id, "model": model, "usd": round(usd, 6)})
                if usd >= self.per_run_cap and model not in state["aborted_models"]:
                    state["aborted_models"].append(model)
            self._update(fn)
