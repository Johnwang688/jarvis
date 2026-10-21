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
  the call whose *estimated* cost would cross the budget is refused before it
  is sent, and the call that still crosses it raises. At most one call can
  overshoot. The estimate prices the call at the model's most expensive
  OpenRouter endpoint (the spec's `prices`) and counts all of it: the
  messages and the tool schemas (chars/4), a completion allowance
  (min of the call's `max_tokens`, the endpoint's maximum completion and
  16k) and a
  long-context tier (Haiku 5.5: the whole request at 5x from 100k prompt
  tokens). The total is the usage OpenRouter reports (BYOK upstream cost
  included, `llm._cost`).
- **No Kimi.** A `moonshotai/` id is refused (Kimi left the matrix), and so
  is a call to any model the spec does not price.
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
# Positions in llm.chat's *args after (model, messages): llm.chat(model,
# messages, tools, temperature, max_tokens, timeout, max_retries, provider, ...).
# Pinned against the real signature by the free checks.
CHAT_ARGS = {"tools": 0, "max_tokens": 2, "provider": 5}
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
        self.allowance_cap = int(spec.get("allowance_cap") or 16_000)
        self.prices = {k.lower(): dict(v) for k, v in (spec.get("prices") or {}).items()}
        self.stream = stream if stream is not None else sys.stdout
        self.total, self.calls, self.models = 0.0, 0, {}
        self.tokens = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

    def route(self, model: str) -> str:
        """The provider pin for one call: always '' (OpenRouter picks), but a
        `moonshotai/` id is refused."""
        if (model or "").strip().lower().startswith(MOONSHOT_PREFIX):
            raise BudgetExceeded(f"refused: {model} is a moonshotai/ id (Kimi left the matrix)")
        return ""

    def estimate(self, model: str, messages=None, max_tokens: int | None = None, tools=None) -> dict:
        """The most this call could cost: {"usd", "prompt_tokens", "completion_tokens", "multiplier"}."""
        price = self.prices.get((model or "").strip().lower())
        if not price:
            raise BudgetExceeded(f"refused: no endpoint price for {model}, so no estimate")
        prompt_tokens = (_chars(messages) + (len(json.dumps(tools)) if tools else 0)) / 4
        completion_tokens = min(x for x in (max_tokens, price.get("max_completion"), self.allowance_cap) if x)
        tier = price.get("tier")
        multiplier = float(tier[1]) if tier and prompt_tokens >= tier[0] else 1.0
        usd = (prompt_tokens * price["prompt"] + completion_tokens * price["completion"]) * multiplier / 1e6
        return {"usd": usd, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "multiplier": multiplier}

    def before(self, model: str, messages=None, max_tokens: int | None = None, tools=None) -> str:
        if self.total >= self.budget - 1e-12:
            raise BudgetExceeded(f"budget ${self.budget:.4f} spent (${self.total:.4f}); no further calls")
        pin = self.route(model)
        est = self.estimate(model, messages, max_tokens, tools)["usd"]
        if self.total + est > self.budget + 1e-12:
            raise BudgetExceeded(
                f"refused before sending: estimated ${est:.4f} for this call would cross the "
                f"${self.budget:.4f} budget (${self.total:.4f} already spent)")
        return pin

    def after(self, model: str, reply) -> None:
        prompt = int(getattr(reply, "prompt_tokens", 0) or 0)
        completion = int(getattr(reply, "completion_tokens", 0) or 0)
        usage = (getattr(reply, "raw", None) or {}).get("usage") or {}
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        cost = float(getattr(reply, "cost_usd", 0.0) or 0.0)      # reported usage, BYOK included
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
    # Room for a full review plus its findings block in one reply (llm.chat
    # reads config.MAX_TOKENS at call time when a caller passes none).
    jconfig.MAX_TOKENS = int(spec.get("max_output_tokens") or jconfig.MAX_TOKENS)
    jmodels.selected = lambda: ""                              # no roster selection can move a tier
    return jconfig


CUT_OFF_MARK = "[cut off at the token limit]"     # jarvis.agent appends it when the last reply ended on length


def final_truncated(text: str) -> bool:
    """The final answer still ended at the output limit (after the loop's own continuations)."""
    return (text or "").rstrip().endswith(CUT_OFF_MARK)


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
    limits: list[bool] = []
    jconfig = sys.modules["jarvis.config"]
    real_chat, real_turn = llm.chat, agent_mod.Agent.run_turn

    def chat(model, messages, *args, **kwargs):
        def arg(name):
            i = CHAT_ARGS[name]
            return args[i] if len(args) > i else kwargs.get(name)
        # A call with no max_tokens gets config.MAX_TOKENS from llm.chat: estimate that, not 0.
        pin = meter.before(model, messages, arg("max_tokens") or jconfig.MAX_TOKENS, arg("tools"))
        if len(args) > CHAT_ARGS["provider"]:     # provider passed positionally
            args = list(args)
            args[CHAT_ARGS["provider"]] = pin
        else:
            kwargs["provider"] = pin
        reply = real_chat(model, messages, *args, **kwargs)
        meter.after(model, reply)
        return reply

    def run_turn(self, *args, **kwargs):
        turn = real_turn(self, *args, **kwargs)
        texts.append(turn.text or "")
        limits.append(bool(getattr(turn, "truncated", False)))
        return turn

    llm.chat, agent_mod.Agent.run_turn = chat, run_turn
    lh.JARVIS_TOOLS = list(TOOLS)                 # no run_subagent / run_fleet
    record = lh.run_jarvis([spec["prompt"]], Path(spec["workspace"]), spec["model"], spec["max_turns"],
                           budget_usd=meter.budget)
    text = texts[-1] if texts else record.reply
    return {"text": text, "truncated": final_truncated(text), "hit_output_limit": any(limits),
            "cost_usd": record.cost_usd, "turns": record.turns,
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
