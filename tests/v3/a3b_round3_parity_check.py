"""Compare A3b with its base. Fixtures are command text, never shell input.

A3b verifier r3 test, kept as a regression (orchestrator-owned). One accepted
wording change: Python spellings of `jarvis hud ... rebuild` are still refused,
with the plain `jarvis hud` reason.
"""

from itertools import product
from pathlib import Path
from shlex import join, quote
import subprocess
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

import jarvis
from jarvis import config, protected_state, rules


BASE = "9e63b514"  # live main before v3
ROOT = Path(__file__).resolve().parents[2]
CHECKOUT = Path("/home/u/projects/Jarvis")
NAMED_CHECKOUT = CHECKOUT / "daemon2-review"
RANK = {rules.ALLOW: 0, rules.ASK: 1, rules.DENY: 2}
CGROUP = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice"
CGROUP_THREADS = f"cat {CGROUP}/jarvis-daemon2.service/cgroup.threads | xargs -r kill"


def load_source(source, name):
    module = ModuleType(name)
    module.__package__ = "jarvis"
    with patch.dict(sys.modules, {name: module}):
        exec(compile(source, name, "exec"), module.__dict__)
    return module


def process_cases():
    patterns = ["^jarvis [d]aemon2", "^python3? -m jarvis d", "n2$", "^.{14,23}n2$",
                "DAEMON2", "(daemon|worker)[[:digit:]]", "[[:alnum:]]+2", "d(ae){1}mon2",
                "d(?:ae)(?=mon2)mon2", r"\b[a-z]*on2\b", r"\(daem\)\{1\}on2",
                r"\<daemon2\>", r"\(d\)\1", "daemon2[", "(.*)*daemon2",
                "(.*)*unrelated", "daemon2", "jarvis", "worker", "$PATTERN"]
    patterns += [f"{lead}.{{0,{n}}}{tail}"
                 for lead, n, tail in product((".*", "^.*", "(.?){2}"), range(6, 24),
                                              ("n2", "aemon2", "daemon[2]$"))]
    for pattern in patterns:
        p = quote(pattern)
        for tool in ("pkill -f", "pkill -fix", "pkill --full --ignore-case", "killall -r",
                     "killall --regexp --ignore-case", "pgrep -f", "pgrep -fix", "pgrep -fv",
                     "pgrep --inverse --full", "pgrep --exact", "pidof"):
            suffix = " | xargs -r kill" if tool.startswith(("pgrep", "pidof")) else ""
            yield f"{tool} -- {p}{suffix}"
        for grep in ("grep -G", "grep -E", "grep -P", "grep -F", "grep -Eiw", "grep -Ex",
                     "grep --perl-regexp --line-regexp", "egrep", "fgrep"):
            yield f"ps -eo pid,args | {grep} -e {p} | awk '{{print $1}}' | xargs -r kill"

    prefix = "a" * 1000
    for n, last in product(range(1, 7), ("daemon2", "^jarvis d", "(.*)*daemon2", "unrelated")):
        p = quote(last)
        yield " ; ".join([f"pkill -f {prefix}"] * n + [f"pkill -f {p}"])
        yield " ; ".join(["pkill -f '.*.{0,12}n2'"] * n + [f"killall -r {p}"])
        yield f"pkill -f {prefix}; kill $(pgrep -f {p}); kill $(pgrep -fx 'jarvis daemon2')"
        yield f"ps -eo pid,args | grep -E -e {prefix} -e {p} | grep -vi worker | xargs -r kill"


def checkout_cases():
    # Include the final daemon2 operand as well as the path in every selector.
    for lead, repeat, n, tool in product(("^", ""), (".*", ".*.*"), range(5),
                                         ("pkill -f", "killall -r")):
        yield f"{tool} '{lead}{NAMED_CHECKOUT}/{repeat}.{{0,{n}}} -m jarvis daemon2'"


def pid_sources():
    for path, take in product(("/proc/*/cmdline", "/proc/[1-9]*/comm", "/proc/123/status",
                               '"$PROC/stat"', "/proc/{1,2}/environ"),
                              ("cut -d/ -f3", "awk -F/ '{print $3}'", "sed -E 's@/proc/([0-9]+)/.*@\\1@'")):
        yield f"grep -lE -e 'daem[o]n2' {path} | {take} | xargs -r kill"
    for file, unit in product(("cgroup.procs", "cgroup.threads"),
                              ("jarvis-daemon2.service", "jarvis-daem*.service", '"$UNIT"', "pipewire.service")):
        path = f"{CGROUP}/{unit}/{file}"
        yield f"cat {path} | xargs -r kill"
        yield f"pkill --pidfile {path}"
        yield f"pgrep --cgroup {path} | xargs -r kill"
    for tool, pattern, tail in product(
            ("ss -ltnp", "netstat --program -ltn"),
            (":8402", ":8405", ":18405", "84[024][235]", "[0-9]{4}", "^(LISTEN|tcp)",
             *(f".*.{{0,{n}}}:84[024][235].{{0,5}}" for n in range(5, 30))),
            ("grep -v worker", "grep -F python3", "grep -P 'pid=|/python'")):
        yield f"{tool} | grep -E {quote(pattern)} | {tail} | xargs -r kill"
    for port in ("8402", "8403", "8405", "8408", "8409", "18405", "8402-8408", "8401,8405", "$PORT", "5173"):
        for source in (f"lsof -t -iTCP:{port}", f"ss -ltnp 'sport = :{port}'", f"fuser -n tcp {port}"):
            yield f"{source} | xargs -r kill"
        yield f"fuser -k -n tcp {port}"
    for obj, member in product(("jarvis_2ddaemon2", "pipewire"), ("MainPID", "ControlPID", "UnitFileState")):
        dst = "org.freedesktop.systemd1"
        path = "/org/freedesktop/systemd1/unit/" + obj + "_2eservice"
        yield f"busctl --user get-property {dst} {path} {dst}.Service {member} | xargs -r kill"
        yield f"dbus-send --session --dest={dst} {path} org.freedesktop.DBus.Properties.Get string:{member} | xargs -r kill"
        yield f"gdbus call --session --dest {dst} --object-path {path} --method org.freedesktop.DBus.Properties.Get {dst}.Service {member} | xargs -r kill"
        yield f"qdbus6 {dst} {path} {dst}.Service.{member} | xargs -r kill"


def systemctl_cases():
    units = ("jarvis-daemon2", "jarvis-daemon2.service", "'jarvis-d[a-z]*'", "'jarvis-*'",
             "jarvis-daemon{1..3}", "{pipewire,jarvis-daemon2}.service", "{1..257}",
             "app.slice", "shutdown.target", "'*.slice'", "pipewire.service", '"$UNIT"')
    verbs = ("stop", "restart", "try-restart", "reload-or-restart", "freeze", "kill", "mask --now",
             "--marked restart", "disable --now", "isolate", "exit", "show -p MainPID --value",
             "show --property=ControlPID", "status", "reset-failed", "--odd restart", "'re*'", "$VERB",
             "set-environment", "unset-environment", "import-environment")
    for head, verb, unit in product(("systemctl --user", "systemctl -q --us", "systemctl --system"), verbs, units):
        yield f"{head} {verb} {unit}"
        if verb.startswith(("show", "status")):
            yield f"UNIT=jarvis-daemon2; {head} {verb} {unit} | xargs -r kill"
    for verb in ("", "restart", "status", "--marked reload", "--odd stop"):
        yield f"printf '%s\\n' jarvis-daemon2 | xargs -r systemctl --user {verb}".rstrip()


def owner_cases():
    ops = ("restart --force", "daemon2 --force restart", "hud install", "hud --no-window rebuild")
    launchers = ("jarvis", "./.venv/bin/jarvis", "/opt/bin/jarvis.exe", "python3.14 -m jarvis",
                 "python.exe -m jarvis.__main__", "python -I -P .venv/bin/jarvis", "pypy3 -m jarvis",
                 *(f"python -m {runner} jarvis" for runner in ("runpy", "cProfile -m", "profile -m",
                                                              "pdb -c continue -m", "trace --module",
                                                              "coverage run -m")), "coverage run -m jarvis")
    wrappers = ("", "env --unset=ZZ", "nohup", "chronic", "timeout -k 1 2", "setsid -w", "stdbuf -oL",
                "nice -n 10", "ionice -c 2", "command -p", "exec -a cli", "time -p", "watch -x",
                "busybox", "systemd-run --user --scope", "flock ./lock", "xargs -r", "ssh localhost",
                "uv run --no-sync", "uv tool run", "uvx", "pipx run", "poetry run", "pdm run", "rye run",
                "hatch run", "pixi run", "pixi exec", "conda run", "mamba run", "micromamba run", "npx",
                "npm exec --", "npm x --", "pnpm exec", "pnpm dlx", "pnpx", "bunx", "bun x", "bun run",
                "yarn exec", "yarn dlx", "yarnpkg exec", "yarn workspace sample exec", "corepack yarn@4 exec")
    for op in ops:
        for launcher in launchers:
            yield f"{launcher} {op}"
        for wrapper in wrappers:
            yield f"{wrapper} jarvis {op}".lstrip()
        cmd = f"jarvis {op}"
        for shell in sorted(rules.SHELLS | {"eval"}):
            yield f"{shell}{'' if shell == 'eval' else ' -c'} {quote(cmd)}"
        for launcher in ("env -S", "flock -c", "script -c", "npm exec -c", "npx -c", "bun exec"):
            yield f"{launcher} {quote(cmd)}"
        for source in (f"import os; os.system({cmd!r})", f"import subprocess; subprocess.run({cmd.split()!r})",
                       f"import jarvis.__main__ as j; j.main({op.split()!r})",
                       f"import sys, runpy; sys.argv={cmd.split()!r}; runpy.run_module('jarvis')"):
            yield join(("python", "-c", source))
    for module in ("hud_install", "owner_ops"):
        yield f"python -m jarvis.v2.{module}"
        yield f"python jarvis/v2/{module}.py"
    yield join(("python", "-c", "from jarvis.v2.owner_ops import restart as invoke; invoke()"))
    yield 'CLI=jarvis; "$CLI" daemon2 restart'
    yield "ln -s /opt/bin/jarvis ./jv; ./jv daemon2 restart"


ORDINARY = (
    "git add -A", "git checkout -b feature", "git reset HEAD~1", "git branch --list",
    "grep -in worker README.md", "grep -Fx daemon2 README.md", "rg -n 'agentd|daemon2' docs",
    "python -m compileall jarvis", "python3 -m json.tool sample.json", "python -V",
    "npm run build", "npm list --depth=0", "npm test", "pkill -f jarvis", "pgrep -f daemon2",
)


def judge(module, command):
    with patch.object(jarvis, "protected_state", module):
        verdict = rules.decide(command)
        return verdict, module.owner_op(command), module.kill_unsure(command)


class BaseParity(unittest.TestCase):
    def test_gate_keeps_legacy_verdicts(self):
        source = subprocess.check_output(["git", "show", f"{BASE}:jarvis/protected_state.py"], cwd=ROOT)
        base = load_source(source, "jarvis._a3b_round3_base")
        # The base under its own configuration: 8410 was not a control port yet.
        base._daemon_ports = lambda: (config.control_ports() | {8403, config.WORKSHOP_PORT}) - {8410, config.V3_API_PORT}
        total = 0
        differences = []
        counts = dict(weaker=0, reasons=0, ordinary=0)
        corpus = (*process_cases(), *pid_sources(), *systemctl_cases(), *owner_cases(), *ORDINARY)
        for checkout, cases in ((CHECKOUT, corpus), (NAMED_CHECKOUT, checkout_cases())):
            with patch.multiple(config, REPO_ROOT=checkout, V3_API_PORT=8410, DAEMON_PORT=18405):
                for command in dict.fromkeys(cases):
                    total += 1
                    before, old_op, old_unsure = judge(base, command)
                    after, new_op, new_unsure = judge(protected_state, command)
                    # Inverted selectors can definitely select v3 while excluding v2
                    # or leaving v2 too costly to judge. That DENY is intentional.
                    if (command.startswith(("pgrep -fv ", "pgrep --inverse ")) and
                            "kills the v3 daemon" in after.reason and before.decision != rules.DENY):
                        continue
                    if RANK[after.decision] < RANK[before.decision] or (old_op and not new_op):
                        counts["weaker"] += 1
                        differences.append(f"{command!r}: {before.decision} -> {after.decision} "
                                           f"(kill_unsure: {bool(old_unsure)} -> {bool(new_unsure)})")
                    elif ((old_op or old_unsure) and
                          (before.reason, old_op, old_unsure) != (after.reason, new_op, new_unsure)):
                        if (command.startswith("python -c") and "rebuild" in command
                                and before.decision == after.decision == rules.DENY and new_op):
                            continue
                        counts["reasons"] += 1
                        differences.append(f"{command!r}: reason {before.reason!r} -> {after.reason!r}; "
                                           f"owner_op {old_op!r} -> {new_op!r}; "
                                           f"kill_unsure {old_unsure!r} -> {new_unsure!r}")
                    if command in ORDINARY and (before.decision, after.decision) != (rules.ALLOW, rules.ALLOW):
                        counts["ordinary"] += 1
                        differences.append(f"{command!r}: expected ALLOW in both, got {before.decision}/{after.decision}")
        print(f"Compared {total:,} command strings against {BASE}.", flush=True)
        self.assertEqual(len(differences), 0, f"{counts}\n" + "\n".join(differences[:20]))


def probe_mutant():
    source = (ROOT / "jarvis" / "protected_state.py").read_text()
    original = 'if "cgroup.procs" not in flat and "cgroup.threads" not in flat:'
    assert source.count(original) == 1
    mutant = load_source(source.replace(original, 'if "cgroup.procs" not in flat:'),
                         "jarvis._a3b_round3_mutant")
    # These are the existing v3 checks that exercise the command gate.
    modules = ("daemon2_golden_check", "gate_v3_daemon_check", "a3b_acceptance_check",
               "a3b_anchored_pattern_reason_check", "a3b_socket_ask_parity_check")
    with patch.object(jarvis, "protected_state", mutant), patch.dict(sys.modules, {"jarvis.protected_state": mutant}):
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        if not unittest.TextTestRunner().run(suite).wasSuccessful():
            return 1
    before, _, _ = judge(protected_state, CGROUP_THREADS)
    after, _, _ = judge(mutant, CGROUP_THREADS)
    print(f"cgroup.threads mutation: {before.decision} -> {after.decision}")
    assert before.decision == rules.DENY and RANK[after.decision] < RANK[before.decision]
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe-mutant"]:
        sys.exit(probe_mutant())
    unittest.main()
