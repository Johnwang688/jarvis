"""CG-2 offline probe: the real Codex app-server under Jarvis's permission profile.

Plan docs/plans/2026-10-15-codex-git-credentials.md §5.1/§5.2 (phase 2). Runs
the installed `codex app-server --stdio` with the config
`codex_config.config_text` writes for an owner chat (only the model provider is
swapped for a scripted local endpoint, as in `subagents/record_codex.py`), and
checks, against what Codex actually does:

- `config/read` and the `thread/start`/`thread/resume` receipts pass Jarvis's
  own start-up checks (`codex_config.check_effective`/`check_receipt`), with
  no `sandbox` field sent;
- a plain sandboxed command cannot open any credential path, can read an
  ordinary file and the chat-git mirror (`git ls-remote <mirror>`), can write
  its worktree root, the commands' HOME and $TMPDIR, cannot write elsewhere,
  and has no network;
- a `with_additional_permissions` network request reaches the client in the
  shape `codex._widening` describes as "network on"; once accepted, that one
  command reaches the network and still cannot open a credential path; the
  next plain command has no network again;
- a declined one does not run; an accepted `require_escalated` one gets no
  network and no credential;
- the model is never offered `request_permissions`.

Opens are open-and-close only: nothing is read. Run it only inside a rootless
network namespace with loopback alone, under a memory cap:

    TMPDIR=/var/tmp/x systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \\
      timeout 600 unshare -Urn <venv>/bin/python -I tests/v2/fixtures/cg2_probe.py [--real-home]

Default: a temporary HOME holding fake credential files (hermetic; what
`tests/v2/codex_profile_probe_check.py` runs). `--real-home`: the owner's real
credential paths are the deny targets and are only opened and closed, never
read; V2_DATA_DIR and the chat worktrees still go to a temporary folder, so no
owner state is written. Exit 0 and a last line "ALL PASS" when every
expectation holds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]


# --- inside the sandbox: one probe command ---------------------------------------

def inside(label: str, port: int, spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text())
    out = {"label": label, "HOME": os.environ.get("HOME")}

    def opened(path):
        try:
            flags = os.O_RDONLY | (os.O_DIRECTORY if os.path.isdir(path) else 0)
            os.close(os.open(path, flags))
            return "openable"
        except FileNotFoundError:
            return "absent"
        except PermissionError:
            return "denied"
        except OSError as exc:
            return "err:" + type(exc).__name__

    def wrote(folder):
        target = os.path.join(folder, f".cg2-{label}-{os.getpid()}")
        try:
            with open(target, "w"):
                pass
            os.unlink(target)
            return "ok"
        except OSError as exc:
            return "refused:" + type(exc).__name__

    out["open"] = {p: opened(p) for p in spec["denied"] + spec["readable"]}
    import glob as _glob
    out["open_globs"] = {g: [opened(p) for p in sorted(_glob.glob(g))] for g in spec.get("readable_globs", [])}
    out["write"] = {p: wrote(p) for p in spec["writable"] + spec["unwritable"]}
    try:
        socket.create_connection(("127.0.0.1", port), 2).close()
        out["network"] = "connected"
    except OSError as exc:
        out["network"] = "blocked:" + type(exc).__name__
    git = subprocess.run(["git", "ls-remote", spec["mirror"]], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"),
                              "GIT_CONFIG_NOSYSTEM": "1"})
    out["mirror"] = "ok" if git.returncode == 0 and "refs/heads/" in git.stdout else f"failed:{git.returncode}"
    try:
        out["seccomp"] = next(l.split()[1] for l in open("/proc/self/status") if l.startswith("Seccomp:"))
    except (OSError, StopIteration):
        out["seccomp"] = "?"
    print("PROBE " + json.dumps(out))


# --- outside: the harness ----------------------------------------------------------

FAKE = "FAKE-CG2-NOT-A-SECRET\n"
# Credential stores a blocklist missed (review of #224): the default-closed
# dot-folders must hide each, approved network commands included.
STORES = (".docker/config.json", ".local/share/keyrings/login.keyring",
          ".local/share/com.vercel.cli/auth.json", ".claude.json", ".azure/msal_token_cache.json",
          ".supabase/access-token", ".agents/token", ".config/gcloud/credentials.db", ".npmrc",
          ".pypirc", ".kube/config", ".cargo/credentials.toml", ".cargo/credentials",
          ".cache/huggingface/token", ".bash_history", ".local/share/uv/credentials/credentials.toml",
          ".brand-new-tool/token", ".config/brand-new-tool/token")
# What toolchains may reach by absolute path or symlink: stays readable.
TOOLCHAIN = (".local/bin/tool", ".cargo/bin/tool", ".rustup/toolchains/x/bin/tool",
             ".local/share/uv/python/x/bin/python3", ".npm/_cacache/index", ".cache/uv/x")


def isolated() -> None:
    subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True)
    links = json.loads(subprocess.check_output(["/usr/sbin/ip", "-j", "link", "show"]))
    if [x["ifname"] for x in links] != ["lo"]:
        raise SystemExit("refusing: run inside `unshare -Urn` (loopback only)")


def setup(real_home: bool) -> tuple[Path, dict]:
    temp = Path(tempfile.mkdtemp(prefix="cg2-probe-")).resolve()
    env = {"JARVIS_V2_DATA": str(temp / "v2"), "JARVIS_CHAT_WORKTREES": str(temp / "chat-worktrees"),
           "JARVIS_CHAT_GIT_MIRRORS": str(temp / "chat-git-mirrors")}
    if not real_home:
        home = temp / "home"
        env["HOME"] = str(home)
        for key in ("JARVIS_V2_DATA", "JARVIS_CHAT_WORKTREES", "JARVIS_CHAT_GIT_MIRRORS"):
            env.pop(key)                # their defaults lie under the fake HOME
            os.environ.pop(key, None)
        for rel in (".config/gh/hosts.yml", ".gitconfig", ".git-credentials", ".codex/auth.json",
                    ".claude/.credentials.json", ".ssh/id_fixture", ".aws-real/credentials",
                    ".codex/skills/demo/SKILL.md", ".codex/packages/README",
                    ".config/jarvis/allowlist.json", ".local/share/jarvis/sessions/s.json",
                    ".local/share/jarvis/browser/profiles.json", "projects/jarvis/.env",
                    "projects/jarvis/README.md", *STORES, *TOOLCHAIN):
            path = home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE)
        # A symlinked credential folder, as the owner's ~/.aws is.
        (home / ".aws").symlink_to(home / ".aws-real", target_is_directory=True)
        env["CODEX_HOME"] = str(home / ".codex")
    for key in ("JARVIS_SESSIONS", "JARVIS_SPILL", "JARVIS_BROWSER_DIR", "JARVIS_SPEAKER_DIR", "CODEX_HOME"):
        os.environ.pop(key, None)
    os.environ.update(env)
    return temp, env


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null"})


def main(argv: list[str]) -> int:
    if argv[:1] == ["--inside"]:
        inside(argv[1], int(argv[2]), argv[3])
        return 0
    real_home = "--real-home" in argv
    isolated()
    temp, _env = setup(real_home)
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(HERE.parent / "subagents"))
    import record_codex as rc
    from jarvis import config
    from jarvis.v2.model import Role
    from jarvis.v2.provider import Brief, BriefRefused
    from jarvis.v2.providers import codex, codex_config
    from jarvis.v2 import chat_git

    results: list[tuple[bool, str]] = []

    def check(cond, what):
        results.append((bool(cond), what))
        print(("PASS " if cond else "FAIL ") + what, flush=True)

    home_dir = Path.home()
    project = (home_dir / "projects" / "jarvis") if not real_home else temp / "work" / "jarvis"
    project.mkdir(parents=True, exist_ok=True)
    (project / "ok.txt").write_text("ordinary\n")
    (project / "sub").mkdir(exist_ok=True)
    (project / "sub" / ".env.local").write_text(FAKE)
    git("init", "-q", cwd=project)
    git("-c", "user.name=f", "-c", "user.email=f@example.invalid", "add", "ok.txt", cwd=project)
    git("-c", "user.name=f", "-c", "user.email=f@example.invalid", "commit", "-qm", "f", cwd=project)
    if real_home:
        (project / ".env").write_text(FAKE)
    brief = Brief(Role.CHAT, str(project), model=rc.MODEL)
    home = Path(config.V2_DATA_DIR) / "codex" / "cg2probe"
    for folder in (home, home / "tmp", codex_config.sandbox_home(home)):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (Path(config.V2_DATA_DIR) / "store.db").write_text(FAKE)
    codex_config._session_skills(codex_config.session_skills(home), codex_config.owner_home() / "skills")
    config.CHAT_WORKTREES_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    mirror = chat_git.mirror_dir(project / ".git")
    git("--git-dir", str(mirror), "fetch", "-q", str(project), "+refs/heads/*:refs/heads/*")

    model = rc.Model([None])
    text = codex_config.config_text(brief, home)
    dropped = ("model_provider =", "forced_login_method =", "cli_auth_credentials_store =", "web_search =",
               "model =")
    lines = [line for line in text.splitlines() if not line.startswith(dropped)]
    lines = [f'model = "{rc.MODEL}"', 'model_provider = "fixture"', 'web_search = "disabled"'] + lines
    lines += ["[model_providers.fixture]", 'name = "offline"', f'base_url = "{model.base}"',
              'wire_api = "responses"', 'env_key = "OPENAI_API_KEY"']
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    (home / "config.toml").chmod(0o600)
    env = codex_config.clean_env(home)
    env["OPENAI_API_KEY"] = "dummy-cg2-offline"

    if real_home:
        h = Path.home()
        denied = [h / ".config/gh/hosts.yml", h / ".gitconfig", h / "projects/Jarvis/.env",
                  h / ".codex/auth.json", h / ".claude/.credentials.json", h / ".ssh", h / ".aws",
                  h / ".config/jarvis", Path(config.SESSIONS_DIR), Path(config.BROWSER_DIR),
                  *(h / rel for rel in STORES),
                  # the parents of stores whose file names differ here
                  h / ".docker", h / ".local/share/keyrings", h / ".local/share/com.vercel.cli",
                  h / ".azure", h / ".supabase", h / ".agents", h / ".cache/huggingface",
                  h / ".config/gcloud", h / ".kube"]
        denied = [p for p in dict.fromkeys(denied) if p.exists() or p.is_symlink()]
    else:
        h = home_dir
        denied = [h / ".config/gh/hosts.yml", h / ".gitconfig", h / ".git-credentials",
                  h / ".codex/auth.json", h / ".claude/.credentials.json", h / ".ssh/id_fixture",
                  h / ".aws/credentials", h / ".aws-real/credentials", h / ".config/jarvis/allowlist.json",
                  Path(config.SESSIONS_DIR) / "s.json", Path(config.BROWSER_DIR) / "profiles.json",
                  h / "projects/jarvis/.env", *(h / rel for rel in STORES)]
    denied += [project / "sub" / ".env.local", home / "config.toml", Path(config.V2_DATA_DIR) / "store.db"]
    if real_home:
        denied.append(project / ".env")
    readable = [str(project / "ok.txt")]
    if not real_home:
        readable += [str(codex_config.session_skills(home) / "demo" / "SKILL.md"),
                     str(codex_config.owner_home() / "packages" / "README"),
                     *(str(home_dir / rel) for rel in TOOLCHAIN)]
    else:
        readable += [str(p) for p in (Path.home() / ".local/bin", Path(rc.CODEX)) if p.exists()]
    spec = {"denied": [str(p) for p in denied], "readable": readable,
            "readable_globs": [str(codex_config.session_skills(home) / ".system" / "*" / "SKILL.md")],
            "writable": [str(project), str(config.CHAT_WORKTREES_DIR), str(codex_config.sandbox_home(home)),
                         str(home / "tmp"), str(project / ".git")],
            "unwritable": [str(temp), str(mirror), str(Path(config.V2_DATA_DIR))],
            "mirror": str(mirror)}
    spec_path = temp / "spec.json"
    spec_path.write_text(json.dumps(spec))
    port = model.server.server_port

    # The probe runs from a copy: the checkout may lie in a folder the
    # profile hides (a lead worktree under ~/.cache, closed by default).
    inside_copy = temp / "cg2_inside.py"
    shutil.copyfile(HERE, inside_copy)

    def cmd(label, cid, **extra):
        call = {"cmd": f"python3 -I {inside_copy} --inside {label} {port} {spec_path}", **extra}
        code = ("const t = ALL_TOOLS.find(t => /(^|__)(exec_command|shell|shell_command)$/.test(t.name));"
                " if (!t) throw new Error('no_shell_tool');"
                f" text(JSON.stringify(await tools[t.name]({json.dumps(call)})));")
        return rc.exec_call(code, cid)

    net = dict(sandbox_permissions="with_additional_permissions",
               additional_permissions={"network": {"enabled": True}}, justification="cg2 probe")
    esc = dict(sandbox_permissions="require_escalated", justification="cg2 probe")
    steps = [cmd("plain", "c0"), cmd("net-approved", "c1", **net), cmd("plain-after", "c2"),
             cmd("escalated", "c3", **esc), cmd("net-declined", "c4", **net)]

    def script(name, step, items, outputs):
        return {"items": [steps[step]] if step < len(steps) else [rc.message("done")]}

    model.script = script
    seen: list[dict] = []
    answers = {"c1": "accept", "c3": "accept", "c4": "decline"}

    def on_request(msg):
        method, params = msg["method"], msg.get("params") or {}
        if method == "item/commandExecution/requestApproval":
            seen.append(params)
            label = next((k for k, v in (("c1", "net-approved"), ("c3", "escalated"), ("c4", "net-declined"))
                          if v in str(params.get("command"))), None)
            return codex._decision(answers.get(label) == "accept")
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        return codex._decision(False)

    def rpc_open():
        rc.LOG = rc.Log()
        rpc = rc.Rpc([str(rc.CODEX), "app-server", "--strict-config", "--stdio"], env, str(project))
        rpc.on_request = on_request
        rpc.request("initialize", {"clientInfo": {"name": "cg2", "version": "0"},
                                   "capabilities": {"experimentalApi": True}})
        rpc.write({"method": "initialized"})
        return rpc

    params = {"cwd": str(project), "modelProvider": "fixture", "approvalPolicy": "on-request",
              "approvalsReviewer": "user", "model": rc.MODEL,
              "developerInstructions": codex_config.git_note(brief)}
    rpc = rpc_open()
    try:
        effective = rpc.request("config/read", {"includeLayers": False, "cwd": str(project)})["result"]["config"]
        try:
            codex_config.check_effective(brief, effective, home)
            check(True, "config/read passes check_effective")
        except BriefRefused as exc:
            check(False, f"config/read passes check_effective ({exc})")
        listed = rpc.request("permissionProfile/list", {}).get("result", {}).get("data", [])
        check(any(p.get("id") == codex_config.PROFILE for p in listed), "permissionProfile/list names the profile")
        started = rpc.request("thread/start", params)
        result = started.get("result") or {}
        if "error" in started:
            print("thread/start error " + json.dumps(started["error"])[:800].replace(str(temp), "<T>"))
            print("stderr", rpc.stderr[-8:])
        check("sandbox" not in params, "thread/start sent no sandbox field")
        try:
            codex_config.check_receipt(brief, result, home)
            check(True, "thread/start receipt passes check_receipt")
        except BriefRefused as exc:
            check(False, f"thread/start receipt passes check_receipt ({exc}; {json.dumps(result.get('sandbox'))})")
        thread_id = result["thread"]["id"]
        rpc.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "probe"}]})
        rc.wait_for(lambda r: rc.note(r, "turn/completed", threadId=thread_id), 180)
        rc.quiet(1, 10)
        probes: dict[str, dict] = {}
        for row in rc.LOG.rows:
            d = row["data"]
            if row["src"] == "rpc_in" and d.get("method") == "item/completed" and \
                    d["params"]["item"].get("type") == "commandExecution":
                text = d["params"]["item"].get("aggregatedOutput") or ""
                if "PROBE " not in text:
                    print("COMMAND WITHOUT PROBE OUTPUT", d["params"]["item"].get("status"),
                          d["params"]["item"].get("exitCode"), text[:400].replace(str(temp), "<T>"))
                for line in text.splitlines():
                    if line.startswith("PROBE "):
                        got = json.loads(line[6:])
                        probes[got["label"]] = got
        tools = [t for row in rc.LOG.rows if row["src"] == "model" for t in row["data"].get("tools") or []]
    finally:
        rpc.close()

    print("PROBES " + json.dumps(probes, indent=1, sort_keys=True).replace(str(temp), "<T>"))
    sandbox = str(codex_config.sandbox_home(home))
    plain = probes.get("plain", {})
    check(plain, "the plain command ran")
    check(plain.get("HOME") == sandbox, "commands get the sandbox HOME")
    for label in ("plain", "net-approved", "plain-after", "escalated"):
        got = probes.get(label, {})
        opened = got.get("open", {})
        leaks = [p for p in spec["denied"] if opened.get(p) == "openable"]
        check(got and not leaks, f"{label}: no credential path opens ({leaks})")
    for path in spec["readable"]:
        check(plain.get("open", {}).get(path) == "openable",
              f"plain: {path.replace(str(temp), '<T>')} opens")
    system = plain.get("open_globs", {}).get(spec["readable_globs"][0], [])
    check(system and all(x == "openable" for x in system),
          f"plain: Codex's bundled skills open ({len(system)} SKILL.md)")
    roots = [line for row in rc.LOG.rows if row["src"] == "model"
             for line in [json.dumps(row["data"])] if "Skill roots" in line]
    listed = json.dumps(model.bodies.get("parent") or {})
    check(str(codex_config.session_skills(home)) in listed,
          "the model is told skill paths under the session's skills folder")
    check(plain.get("mirror") == "ok", "plain: git ls-remote <mirror> works (the mirror is readable)")
    for folder in spec["writable"]:
        check(plain.get("write", {}).get(folder) == "ok", f"plain: writes {folder.replace(str(temp), '<T>')}")
    for folder in spec["unwritable"]:
        check(str(plain.get("write", {}).get(folder, "")).startswith("refused"),
              f"plain: cannot write {folder.replace(str(temp), '<T>')}")
    check(str(plain.get("network", "")).startswith("blocked"), "plain: no network")
    approved = probes.get("net-approved", {})
    check(approved.get("network") == "connected", "an approved network command reaches the network")
    check(approved.get("seccomp") == "2", "and still runs sandboxed (seccomp 2)")
    check(str(approved.get("write", {}).get(str(temp), "")).startswith("refused"),
          "and still cannot write outside its roots")
    check(str(probes.get("plain-after", {}).get("network", "")).startswith("blocked"),
          "the next plain command has no network (one approval, one command)")
    check(str(probes.get("escalated", {}).get("network", "")).startswith("blocked"),
          "require_escalated gets no network in profile mode")
    check("net-declined" not in probes, "a declined network command did not run")
    shapes = [p for p in seen if "net-approved" in str(p.get("command"))]
    check(shapes, "the network request reached the client")
    if shapes:
        p = shapes[0]
        check(p.get("additionalPermissions", {}).get("network") == {"enabled": True},
              f"it carries additionalPermissions.network ({json.dumps(p.get('additionalPermissions'))})")
        parts, grants, refusal = codex._widening(p, str(project))
        check(refusal is None and "network on" in parts and not grants,
              f"codex._widening describes it as 'network on' ({parts})")
        check(codex._approval_problem(p) is None, "and its shape is readable")
        print("NETWORK APPROVAL " + json.dumps({k: v for k, v in p.items() if k not in (
            "threadId", "turnId", "startedAtMs", "commandActions")}).replace(str(temp), "<T>"))
    check(tools and not any(t.endswith("request_permissions") for t in tools),
          "the model is never offered request_permissions")
    replies = [row["data"]["result"] for row in rc.LOG.rows if row["src"] == "rpc_out"
               and isinstance(row["data"].get("result"), dict) and "decision" in row["data"]["result"]]
    check(replies and all(r in ({"decision": "accept"}, {"decision": "decline"}) for r in replies),
          "only accept/decline were ever sent")

    rpc = rpc_open()
    try:
        resumed = rpc.request("thread/resume", {**{k: v for k, v in params.items() if k != "model"},
                                                "threadId": thread_id, "excludeTurns": True})
        try:
            codex_config.check_receipt(brief, resumed.get("result") or {}, home)
            check(True, "thread/resume without a sandbox field passes check_receipt")
        except BriefRefused as exc:
            check(False, f"thread/resume passes check_receipt ({exc})")
    finally:
        rpc.close()
    shutil.rmtree(temp, ignore_errors=True)
    failed = [w for good, w in results if not good]
    print(f"{len(results) - len(failed)}/{len(results)} expectations held")
    print("ALL PASS" if not failed else "FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
