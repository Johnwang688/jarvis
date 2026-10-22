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
account's home from pwd. That is why the guard is in code, not in HOME: a fake
login home and fake CLIs are set for the whole process, `config.login_home`
refuses the account's, `sandbox.launch` refuses a cell that would bind a real
CLI (one per-call opt-in: `codex sandbox`, no model call), and the live
daemon's quota reader raises.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["ROLEBENCH_TESTING"] = "1"
os.environ["ROLEBENCH_NET"] = "none"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# A fake login home and fake CLIs, for every cell a test builds: under
# ROLEBENCH_TESTING the account's login is never read (config.login_home
# raises) and a cell may bind only a CLI under ROLEBENCH_FAKE_BIN
# (sandbox.testing_launch_refusal). The stubs fail loudly if anything runs them.
FAKE_ROOT = Path(tempfile.mkdtemp(prefix="rolebench-guard-"))
atexit.register(shutil.rmtree, FAKE_ROOT, True)
FAKE_LOGIN = FAKE_ROOT / "login"
FAKE_BIN = FAKE_ROOT / "bin"
(FAKE_LOGIN / ".claude").mkdir(parents=True)
(FAKE_LOGIN / ".codex").mkdir()
(FAKE_LOGIN / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
    "accessToken": "sk-ant-guard-dummy", "expiresAt": (time.time() + 30 * 86400) * 1000,
    "scopes": ["user:inference"], "subscriptionType": "max"}}))
_payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 30 * 86400}).encode()).decode().rstrip("=")
(FAKE_LOGIN / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
    "access_token": f"h.{_payload}.s", "refresh_token": "guard-dummy", "id_token": "i", "account_id": "a"}}))
_STUB = "#!/bin/sh\necho 'rolebench test guard: a fake {name} ran' >&2\nexit 3\n"
(FAKE_BIN / "rel" / "bin").mkdir(parents=True)
(FAKE_BIN / "rel" / "codex-path").mkdir()
(FAKE_BIN / "claude").write_text(_STUB.format(name="claude"))
(FAKE_BIN / "rel" / "bin" / "codex").write_text(_STUB.format(name="codex"))
for _f in (FAKE_BIN / "claude", FAKE_BIN / "rel" / "bin" / "codex"):
    _f.chmod(0o755)
(FAKE_BIN / "codex").symlink_to(FAKE_BIN / "rel" / "bin" / "codex")
os.environ["ROLEBENCH_LOGIN_HOME"] = str(FAKE_LOGIN)
os.environ["ROLEBENCH_FAKE_BIN"] = str(FAKE_BIN)
os.environ["PATH"] = f"{FAKE_BIN}{os.pathsep}{os.environ.get('PATH', '')}"
REAL_PATH = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if p != str(FAKE_BIN))

from rolebench import prices, quota, runners, sandbox  # noqa: E402

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


REAL_DAEMON_READER = quota.daemon_reader      # the real one (it refuses under ROLEBENCH_TESTING too)


def _no_daemon(provider):
    raise AssertionError("rolebench test guard: a test reached for the live daemon's /usage; inject a reader")


quota.daemon_reader = _no_daemon
