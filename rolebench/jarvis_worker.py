"""One Jarvis-loop run in its own process: `python -m rolebench.jarvis_worker spec.json`.

Runs inside the jarvis sandbox cell (`rolebench.sandbox`), from a copy of the
code with no `.env` and no cases. Wraps `longbench.harness.run_jarvis` and adds:

- **No key in the environment.** The key arrives on stdin. `os.environ` is
  snapshotted before `jarvis.config` is imported (its `.env` load can add
  keys) and every key the import added is dropped again, so no shell the model
  is given inherits one.
- **Every tier is the cell's model.** Sub-agent, compaction, review and the
  cheap tiers are pinned before import; delegation tools are not offered.
- **A hard dollar ceiling.** `llm.chat` is wrapped: every reply, from any
  caller, is added to a running total streamed on stdout (a `ROLEBENCH_SPEND`
  line) after each call; a call is refused once the total reaches the budget,
  the call whose *estimated* prompt cost would cross the budget is refused
  before it is sent (one Kimi call can be ~$0.45), and the call that still
  crosses it raises. At most one call can overshoot.
- **The Moonshot pin only for Moonshot ids.** A call to a `moonshotai/` model
  is pinned to the provider the spec names (no fallbacks) and floored at its
  price; any other model is sent unpinned. A `moonshotai/` id the spec does not
  price is refused.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOOLS = ["read_file", "write_file", "edit_file", "list_dir", "find_files", "grep_files",
         "run_readonly", "run_command", "get_datetime", "plan_write"]
TIER_ENV = ("JARVIS_ORCHESTRATOR", "JARVIS_SUBAGENT", "JARVIS_COMPACTION", "JARVIS_REVIEW_MODEL",
            "JARVIS_BROWSER_REVIEW_MODEL", "JARVIS_WORKER", "JARVIS_CHEAP", "JARVIS_SPEECH_SUMMARY_MODEL")
MOONSHOT_PREFIX = "moonshotai/"
# The runner reads the running total from the worker's stdout, not a file.
SPEND_PREFIX = "ROLEBENCH_SPEND "


class BudgetExceeded(RuntimeError):
    pass


def _chars(messages) -> int:
    total = 0
    for m in messages or ():
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            total += sum(len(str(part.get("text", ""))) if isinstance(part, dict) else len(str(part)) for part in c)
        if isinstance(m, dict) and m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return total


class Meter:
    """The running total over every chat call in this process. It is streamed on
    stdout so the harness has it even if the cell is killed mid-call."""

    def __init__(self, spec: dict, stream=None):
        self.budget = float(spec["budget_usd"])
        self.prices = {k.lower(): tuple(v) for k, v in (spec.get("prices") or {}).items()}
        self.pins = {k.lower(): v for k, v in (spec.get("pins") or {}).items()}
        self.stream = stream if stream is not None else sys.stdout
        self.total, self.calls, self.models = 0.0, 0, {}
        self.tokens = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

    def route(self, model: str) -> str:
        """The provider pin for one call ('' = OpenRouter picks)."""
        key = (model or "").strip().lower()
        if key.startswith(MOONSHOT_PREFIX):
            if key not in self.prices or key not in self.pins:
                raise BudgetExceeded(f"refused: {model} is a moonshotai/ id with no known price or pin")
            return self.pins[key]
        return ""

    def before(self, model: str, messages=None, max_tokens: int | None = None) -> str:
        if self.total >= self.budget - 1e-12:
            raise BudgetExceeded(f"budget ${self.budget:.4f} spent (${self.total:.4f}); no further calls")
        price = self.prices.get((model or "").strip().lower())
        if price and messages is not None:
            # Estimate at the bench's price, not the catalog's: at Kimi's $3/M a
            # 150k-token prompt is ~$0.45. Refuse before sending if it would
            # cross the budget. chars/4 is the loop's own rough token estimate.
            est_prompt = _chars(messages) / 4
            est_completion = max_tokens or 0
            est = (est_prompt * price[0] + est_completion * price[1]) / 1e6
            if self.total + est > self.budget + 1e-12:
                raise BudgetExceeded(
                    f"refused before sending: estimated ${est:.4f} for this call would cross the "
                    f"${self.budget:.4f} budget (${self.total:.4f} already spent)")
        return self.route(model)

    def after(self, model: str, reply) -> None:
        prompt = int(getattr(reply, "prompt_tokens", 0) or 0)
        completion = int(getattr(reply, "completion_tokens", 0) or 0)
        usage = (getattr(reply, "raw", None) or {}).get("usage") or {}
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        cost = float(getattr(reply, "cost_usd", 0.0) or 0.0)
        price = self.prices.get((model or "").strip().lower())
        if price:
            cost = max(cost, (prompt * price[0] + completion * price[1]) / 1e6)
            try:
                reply.cost_usd = cost
            except Exception:
                pass
        self.total += cost
        self.calls += 1
        self.models[model] = self.models.get(model, 0) + 1
        self.tokens["prompt_tokens"] += prompt
        self.tokens["completion_tokens"] += completion
        self.tokens["cached_tokens"] += cached
        self.write()
        if self.total >= self.budget - 1e-12:
            raise BudgetExceeded(f"budget ${self.budget:.4f} reached (${self.total:.4f} after {self.calls} calls)")

    def write(self) -> None:
        try:
            self.stream.write(SPEND_PREFIX + json.dumps({"usd": round(self.total, 6), "calls": self.calls,
                                                         "models": self.models, "budget_usd": self.budget}) + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass


def prepare(spec: dict, key: str | None):
    """Import jarvis with every tier on the cell's model, the key reachable
    only through `config.api_key`, and nothing the import added left in
    `os.environ`. Returns jarvis.config."""
    model = spec["model"]
    for name in TIER_ENV:
        os.environ[name] = model
    os.environ["JARVIS_CHAT_PROVIDER"] = ""
    os.environ.pop("OPENROUTER_API_KEY", None)
    before = dict(os.environ)
    from jarvis import config as jconfig                      # its .env load may add keys
    from jarvis import models as jmodels
    for name in list(os.environ):
        if name not in before:
            del os.environ[name]
        elif os.environ[name] != before[name]:
            os.environ[name] = before[name]
    os.environ.pop("OPENROUTER_API_KEY", None)
    if key:
        jconfig.api_key = lambda: key
    jconfig.CHAT_PROVIDER = ""
    for tier in list(jconfig.TIERS):
        jconfig.TIERS[tier] = model
    jmodels.selected = lambda: ""                              # no roster selection can move a tier
    return jconfig


def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def run(spec: dict, key: str | None, io_dir: Path) -> dict:
    prepare(spec, key)
    from jarvis import agent as agent_mod
    from jarvis import llm
    from longbench import harness as lh

    meter = Meter(spec)                           # streams the running total on stdout
    meter.write()
    texts: list[str] = []
    real_chat, real_turn = llm.chat, agent_mod.Agent.run_turn

    def chat(model, messages, *args, **kwargs):
        max_tokens = args[3] if len(args) > 3 else kwargs.get("max_tokens")
        pin = meter.before(model, messages, max_tokens)
        if len(args) > 5:                         # provider passed positionally
            args = list(args)
            args[5] = pin
        else:
            kwargs["provider"] = pin
        reply = real_chat(model, messages, *args, **kwargs)
        meter.after(model, reply)
        return reply

    def run_turn(self, *args, **kwargs):
        turn = real_turn(self, *args, **kwargs)
        texts.append(turn.text or "")
        return turn

    llm.chat, agent_mod.Agent.run_turn = chat, run_turn
    lh.JARVIS_TOOLS = list(TOOLS)                 # no run_subagent / run_fleet
    record = lh.run_jarvis([spec["prompt"]], Path(spec["workspace"]), spec["model"], spec["max_turns"],
                           budget_usd=meter.budget)
    return {"text": texts[-1] if texts else record.reply, "cost_usd": record.cost_usd, "turns": record.turns,
            "spend_usd": round(meter.total, 6), "calls": meter.calls, "models": meter.models,
            "tokens": meter.tokens, "tool_calls": record.tool_calls, "error": record.error,
            "over_budget": meter.total >= meter.budget - 1e-12}


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    key = sys.stdin.readline().strip() or None
    io_dir = Path(spec["io"])
    doc = run(spec, key, io_dir)
    _atomic_write(io_dir / "result.json", doc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
