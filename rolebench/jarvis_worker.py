"""One Jarvis-loop run in its own process: `python -m rolebench.jarvis_worker spec.json`.

Wraps `longbench.harness.run_jarvis` (the swecompare runner) and adds what it
does not report: token counts, the full reply text, the Moonshot price
override (Kimi K3 is budgeted at its direct price, never the catalog's) and the
provider pin. The API key arrives on stdin and is removed from the
environment before the loop can give the model a shell.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    key = sys.stdin.readline().strip() or None
    from jarvis import agent as agent_mod
    from jarvis import config, llm
    from longbench import harness as lh

    os.environ.pop("OPENROUTER_API_KEY", None)        # config's .env load may have added it
    if key:
        config.api_key = lambda: key
    if spec.get("provider"):
        config.CHAT_PROVIDER = spec["provider"]

    tally = {"prompt_tokens": 0, "completion_tokens": 0}
    texts: list[str] = []
    price = spec.get("price")
    real_chat, real_turn = llm.chat, agent_mod.Agent.run_turn

    def chat(*args, **kwargs):
        reply = real_chat(*args, **kwargs)
        tally["prompt_tokens"] += int(getattr(reply, "prompt_tokens", 0) or 0)
        tally["completion_tokens"] += int(getattr(reply, "completion_tokens", 0) or 0)
        if price:
            floor = (reply.prompt_tokens * price[0] + reply.completion_tokens * price[1]) / 1e6
            reply.cost_usd = max(reply.cost_usd, floor)
        return reply

    def run_turn(self, *args, **kwargs):
        turn = real_turn(self, *args, **kwargs)
        texts.append(turn.text or "")
        return turn

    llm.chat, agent_mod.Agent.run_turn = chat, run_turn
    record = lh.run_jarvis([spec["prompt"]], Path(spec["workspace"]), spec["model"], spec["max_turns"],
                           budget_usd=spec.get("budget_usd"))
    Path(spec["out"]).write_text(json.dumps({
        "text": texts[-1] if texts else record.reply, "cost_usd": record.cost_usd, "turns": record.turns,
        "tokens": tally, "tool_calls": record.tool_calls, "error": record.error,
    }), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
