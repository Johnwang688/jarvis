"""A strict test spy's one carve-out, for running the role bench suites to the end under it.

    PYTHONPATH=tests/strictspy_carveout:<checkout> ROLEBENCH_STRICT_SPY=<the spy's sitecustomize.py> \\
        SPY_LOG=<log> ROLEBENCH_NET=none python tests/rolebench_check.py

It runs the strict spy (ROLEBENCH_STRICT_SPY, a sitecustomize that wraps
subprocess.Popen and refuses any real claude/codex launch or bind), then lets
exactly one kind of launch past that refusal: the opted-in real `codex sandbox`
loopback probe (tests/rolebench_check.py test_codex_loopback,
sandbox.launch(real_cli_probe="codex-sandbox"), which makes no model call). It
needs both signs: the launcher environment carries
ROLEBENCH_REAL_CLI_PROBE=codex-sandbox (sandbox.launch sets it only for an
opt-in) and the argv runs /opt/rolebench/codex/bin/codex sandbox. Such a launch
is logged OPTIN; every other launch keeps the strict spy's checks.
"""
import os
import subprocess

exec(compile(open(os.environ["ROLEBENCH_STRICT_SPY"]).read(), os.environ["ROLEBENCH_STRICT_SPY"], "exec"))
_StrictPopen = subprocess.Popen
_SpyBase = _StrictPopen.__mro__[1]          # the class the strict spy itself wraps (the lead spy or Popen)


def _optin_log(line):
    with open(os.environ["SPY_LOG"], "a") as f:
        f.write(line + "\n")


class OptIn(_StrictPopen):
    def __init__(self, args, *a, **k):
        argv = [args] if isinstance(args, (str, bytes)) else [str(x) for x in args]
        env = k.get("env") if isinstance(k.get("env"), dict) else {}
        shaped = any(argv[i] == "/opt/rolebench/codex/bin/codex" and argv[i + 1] == "sandbox" for i in range(len(argv) - 1))
        if shaped and env.get("ROLEBENCH_REAL_CLI_PROBE") == "codex-sandbox":
            _optin_log("OPTIN codex-sandbox " + " ".join(argv)[:300])
            _SpyBase.__init__(self, args, *a, **k)
            return
        super().__init__(args, *a, **k)


subprocess.Popen = OptIn
