"""Imported first by both role bench test modules: nothing a test runs may
reach a real model or the network.

- ROLEBENCH_TESTING=1: the runners themselves refuse a claude or codex run
  whose CLI is not under ROLEBENCH_FAKE_BIN (with a fake login home), and a
  jarvis run without a dummy key (`runners.testing_refusal`).
- ROLEBENCH_NET=none: the net gate refuses every real agent cell up front.
- The runner functions (and REGISTRY) are wrapped to raise instead of
  launching anything the rule above would refuse, so a call that slipped past
  a fake is a loud failure, not a quiet refusal; OpenRouter's price fetch
  raises too.

A temp HOME does not isolate the CLI logins: `config.login_home()` reads the
account's home from pwd. That is why the guard is in code, not in HOME.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["ROLEBENCH_TESTING"] = "1"
os.environ["ROLEBENCH_NET"] = "none"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rolebench import prices, runners, sandbox  # noqa: E402

BLOCKED: list[str] = []


def _wrap(name: str, harness: str):
    real = getattr(runners, name)

    def guarded(**kw):
        binary = (sandbox.claude_binary() if harness == "claude" else sandbox.codex_release() if harness == "codex" else None)
        key = None
        if harness == "jarvis":
            key = (kw.get("key_fn") or runners.openrouter_key)()
        why = runners.testing_refusal(harness, binary, key)
        if why:
            BLOCKED.append(f"{name} model={kw.get('model')} run_id={kw.get('run_id')}")
            raise AssertionError(f"rolebench test guard: {name} would reach a real model ({why})")
        return real(**kw)
    guarded.__name__ = name
    guarded.__wrapped__ = real
    return guarded


for _name, _harness in (("run_claude", "claude"), ("run_codex", "codex"), ("run_jarvis", "jarvis")):
    setattr(runners, _name, _wrap(_name, _harness))
    runners.REGISTRY[_harness] = getattr(runners, _name)


def _no_network(*args, **kwargs):
    raise AssertionError("rolebench test guard: a test reached for OpenRouter")


prices._http_get = _no_network
