"""Further free checks for the egress allowlist and extra binds (EC-1a): the
read-back verifies destinations, the credential-tree refusals, the pasta
command line, and a real install + read-back in a throwaway empty network
namespace (`unshare -rn`; skipped when that is unavailable). No model, no
network, no paid call.

    PYTHONPATH=<checkout> HOME=<tmp> python tests/rolebench_egress_extra_check.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

from rolebench import config, netcell, runners, sandbox  # noqa: E402

PASSED = 0
FAILED: list[str] = []
BASE = {"dns": "192.0.2.53", "block": ["10.0.0.0/8", "192.168.0.0/16", "172.16.5.4/32"], "allow_tcp": [443]}
ANTHROPIC = ["160.79.104.0/23"]


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def verify_with(policy: dict, installed: list[tuple]) -> bool:
    """True when netcell.verify accepts `installed` as the IPv4 read-back."""
    real_installed, real_conf = netcell._installed, netcell.IPV6_CONF
    netcell._installed = lambda family: list(installed)
    netcell.IPV6_CONF = "/nonexistent"                       # skip the IPv6 half
    try:
        netcell.verify(policy)
        return True
    except netcell.Refused:
        return False
    finally:
        netcell._installed, netcell.IPV6_CONF = real_installed, real_conf


def test_verify_destinations() -> None:
    pol = {**BASE, "allow_dst": ANTHROPIC + ["34.0.0.0/17"]}
    exact = [netcell._key(r) for r in netcell.rules(pol)] + list(netcell.DEFAULTS_V4)
    check("verify accepts exactly the policy's rules", verify_with(pol, exact))
    swapped = [(k[0], "8.8.8.0/24", *k[2:]) if k[1] == "34.0.0.0/17" else k for k in exact]
    check("verify refuses a rule whose destination differs", not verify_with(pol, swapped))
    widened = [(k[0], "160.79.104.0/22", *k[2:]) if k[1] == ANTHROPIC[0] else k for k in exact]
    check("verify refuses a wider destination than the policy's", not verify_with(pol, widened))
    open443 = exact + [(110, "", "tcp", 443, "main")]
    check("verify refuses an extra TCP 443 rule open to any destination", not verify_with(pol, open443))
    check("verify refuses a missing allow rule", not verify_with(pol, [k for k in exact if k[1] != "34.0.0.0/17"]))
    check("verify against an allow_dst-less policy refuses the allowlisted read-back",
          not verify_with(BASE, exact))


def test_policy_validation() -> None:
    nets = netcell.allow_networks({**BASE, "allow_dst": ["160.79.104.1/23", "160.79.104.0/23"]})
    check("allow_dst is normalised and de-duplicated", [str(n) for n in nets] == ["160.79.104.0/23"], str(nets))
    check("a bare string allow_dst is refused (not split into characters)",
          raises(lambda: netcell.rules({**BASE, "allow_dst": "160.79.104.0/23"}), netcell.Refused))
    check("an IPv6 allow_dst entry is refused", raises(lambda: netcell.rules({**BASE, "allow_dst": ["2001:db8::/32"]}), netcell.Refused))
    check("an allow_dst entry overlapping a blocked host address is refused",
          raises(lambda: netcell.rules({**BASE, "allow_dst": ["172.16.5.0/24"]}), netcell.Refused))
    check("an allow_dst entry that contains a blocked range is refused",
          raises(lambda: netcell.rules({**BASE, "allow_dst": ["0.0.0.0/0"]}), netcell.Refused))
    check("an allow_dst entry covering the DNS forwarder is refused",
          raises(lambda: netcell.rules({**BASE, "allow_dst": ["192.0.2.0/24"]}), netcell.Refused))
    check("a non-string allow_dst entry is refused", raises(lambda: netcell.rules({**BASE, "allow_dst": [5]}), netcell.Refused))
    two = netcell.rules({**BASE, "allow_tcp": [443, 8443], "allow_dst": ANTHROPIC + ["34.0.0.0/17"]})
    check("one rule per (CIDR, port)", len([r for r in two if r["pref"] == 110]) == 4, str(two))


def test_install_commands() -> None:
    ran: list[list[str]] = []
    real_run, real_verify, real_conf = subprocess.run, netcell.verify, netcell.IPV6_CONF
    subprocess.run = lambda argv, **kw: (ran.append(list(argv)), subprocess.CompletedProcess(argv, 0, "", ""))[1]
    netcell.verify, netcell.IPV6_CONF = lambda policy: None, "/nonexistent"
    try:
        netcell.install({**BASE, "allow_dst": ANTHROPIC})
    finally:
        subprocess.run, netcell.verify, netcell.IPV6_CONF = real_run, real_verify, real_conf
    joined = [" ".join(a[1:]) for a in ran]
    check("install adds the allow_dst rule with its destination",
          "-4 rule add pref 110 to 160.79.104.0/23 ipproto tcp dport 443 lookup main" in joined, str(joined))
    check("install adds no destination-less allow rule",
          "-4 rule add pref 110 ipproto tcp dport 443 lookup main" not in joined)


def test_extra_ro_refusals(root: Path) -> None:
    work = root / "work2"
    work.mkdir()
    dest = work / "deps"
    # a stand-in account home whose credential trees exist, so each refusal is the
    # credential rule and not just a missing source
    home = root / "acct"
    for tree in sandbox.CREDENTIAL_TREES:
        (home / tree / "sub").mkdir(parents=True)
    real_home = config.real_home
    config.real_home = lambda: str(home)
    try:
        test_extra_ro_credentials(root, work, dest, home)
    finally:
        config.real_home = real_home


def refused_for_credentials(extra_ro, work) -> bool:
    try:
        sandbox.Cell("grade", work, extra_ro=extra_ro)
    except sandbox.SandboxError as exc:
        return "credential" in str(exc)
    return False


def test_extra_ro_credentials(root: Path, work: Path, dest: Path, home: Path) -> None:
    for tree in sandbox.CREDENTIAL_TREES:
        check(f"an extra bind of ~/{tree} is refused as a credential tree",
              refused_for_credentials(((home / tree, dest),), work))
    check("an extra bind of a path inside ~/.claude is refused as a credential tree",
          refused_for_credentials(((home / ".claude" / "sub", dest),), work))
    check("an extra bind of the home directory itself (it contains the credential trees) is refused",
          refused_for_credentials(((home, dest),), work))
    check("an extra bind of / is refused as containing the credential trees",
          refused_for_credentials(((Path("/"), dest),), work))
    src = root / "plain"
    src.mkdir()
    link = root / "link-to-creds"
    link.symlink_to(home / ".ssh" / "sub")
    check("a symlink into a credential tree is refused after resolving it",
          refused_for_credentials(((link, dest),), work))
    check("a destination with .. is refused",
          raises(lambda: sandbox.Cell("grade", work, extra_ro=((src, Path("/a/../etc")),)), sandbox.SandboxError))
    ok = sandbox.Cell("grade", work, extra_ro=((src, dest),))
    check("a plain source is accepted and resolved", sandbox._extra_ro_binds(ok.extra_ro) == [(str(src.resolve()), str(dest))])
    swapped = sandbox.Cell("grade", work, extra_ro=((src, dest),))
    swapped.extra_ro = ((home / ".claude", dest),)             # a mutation after construction
    try:
        sandbox.bwrap_argv(swapped, ["/bin/true"])
        why = ""
    except sandbox.SandboxError as exc:
        why = str(exc)
    check("a bind changed after construction is re-checked when the argv is built", "credential" in why, why)
    check("allow_dst on a codex cell (host netns, no netcell) is refused",
          raises(lambda: sandbox.Cell("codex", work, allow_dst=("160.79.104.0/23",)), sandbox.SandboxError))
    check("allow_dst on a cell whose network is switched off is refused",
          raises(lambda: sandbox.Cell("claude", work, network=False, allow_dst=("160.79.104.0/23",)), sandbox.SandboxError))
    check("an empty or bare-string allow_dst is refused at construction",
          raises(lambda: sandbox.Cell("claude", work, allow_dst=()), sandbox.SandboxError)
          and raises(lambda: sandbox.Cell("claude", work, allow_dst="160.79.104.0/23"), sandbox.SandboxError))


def test_cell_command(root: Path) -> None:
    if sandbox.pasta_available() or not shutil.which("claude"):
        print("  skip cell_command checks (pasta or the claude CLI is not installed here)")
        return
    work = root / "work3"
    work.mkdir()
    old = os.environ.get("ROLEBENCH_NET")
    os.environ["ROLEBENCH_NET"] = "pasta"
    try:
        argv, net = sandbox.cell_command(sandbox.Cell("claude", work, allow_dst=("160.79.104.0/23",)), ["/bin/true"])
        i = argv.index(str(sandbox.NETCELL))
        policy = json.loads(argv[i + 1])
        check("cell_command passes the cell's allow_dst in netcell's policy", net == "pasta" and policy.get("allow_dst") == ["160.79.104.0/23"], str(policy))
        argv2, _ = sandbox.cell_command(sandbox.Cell("claude", work), ["/bin/true"])
        check("a cell without allow_dst gets no allow_dst in the policy", "allow_dst" not in json.loads(argv2[argv2.index(str(sandbox.NETCELL)) + 1]))
        bad = sandbox.Cell("claude", work, allow_dst=("10.0.0.0/8",))
        check("a cell whose allow_dst hits a blocked range is refused before launch",
              raises(lambda: sandbox.cell_command(bad, ["/bin/true"]), sandbox.SandboxError))
        malformed = sandbox.Cell("claude", work, allow_dst=("nonsense",))
        check("a malformed allow_dst is refused before launch",
              raises(lambda: sandbox.cell_command(malformed, ["/bin/true"]), sandbox.SandboxError))
    finally:
        if old is None:
            os.environ.pop("ROLEBENCH_NET", None)
        else:
            os.environ["ROLEBENCH_NET"] = old


@contextlib.contextmanager
def env(**kw):
    """Set (or, with None, remove) environment variables for a block."""
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_breadth() -> None:
    check("netcell.MIN_PREFIX is 16", netcell.MIN_PREFIX == 16)
    for wide in ("32.0.0.0/3", "140.0.0.0/6", "1.0.0.0/8", "34.0.0.0/15"):
        check(f"an allow_dst entry as broad as {wide} is refused",
              raises(lambda w=wide: netcell.rules({**BASE, "allow_dst": [w]}), netcell.Refused))
    check("a /16 allow_dst entry is accepted", not raises(lambda: netcell.rules({**BASE, "allow_dst": ["34.0.0.0/16"]})))
    check("a /32 allow_dst entry is accepted", not raises(lambda: netcell.rules({**BASE, "allow_dst": ["160.79.104.7/32"]})))
    work = Path(tempfile.mkdtemp(prefix="rb-egress3-"))
    try:
        cell = sandbox.Cell("claude", work, allow_dst=("32.0.0.0/3",))
        with env(ROLEBENCH_NET="pasta"):
            check("cell_command refuses an over-broad allow_dst before launch (when pasta is installed here)",
                  sandbox.pasta_available() is not None or not shutil.which("claude")
                  or raises(lambda: sandbox.cell_command(cell, ["/bin/true"]), sandbox.SandboxError))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def bind_refused(src, work: Path, dest: Path | None = None) -> bool:
    return raises(lambda: sandbox.Cell("grade", work, extra_ro=((Path(src), dest or work / "dep"),)), sandbox.SandboxError)


def test_extra_ro_allowlist(root: Path) -> None:
    work = root / "work-al"
    work.mkdir()
    state = root / "state"
    (state / "binds" / "deps").mkdir(parents=True)
    (state / "binds" / "deps" / "f").write_text("x")
    for sealed in ("pool", "spotcheck-keys", "io", "snap", "runs", "deps"):
        (state / sealed).mkdir()
        (state / sealed / "x.json").write_text("{}")
    for door in ("/etc", "/run", "/proc", "/mnt/c", "/var", "/home", "/"):
        if os.path.exists(door):
            check(f"an extra bind of {door} is refused (not inside the state directory, /usr or /opt)", bind_refused(door, work))
    # production strictness: without the test guard's temp-dir allowance only the
    # state directory, /usr and /opt are sources
    with env(ROLEBENCH_TESTING=None, ROLEBENCH_HOME=str(state)):
        check("the state directory's binds/ subtree is an allowed source",
              not bind_refused(state / "binds" / "deps", work))
        check("the state directory itself is not a source", bind_refused(state, work))
        check("binds/ itself is not a source (only inside it)", bind_refused(state / "binds", work))
        for sealed in ("pool", "spotcheck-keys", "io", "snap", "runs", "deps"):
            check(f"the state directory's {sealed}/ (outside binds/) is not a source", bind_refused(state / sealed, work))
        plain = root / "plain-outside"
        plain.mkdir()
        check("a plain directory in the system temp dir is refused outside the test guard", bind_refused(plain, work))
        usr = next((p for p in ("/usr/share/zoneinfo", "/usr/share/doc", "/usr/lib/python3") if os.path.isdir(p)), None)
        if usr:
            check(f"a directory inside /usr ({usr}) is an allowed source", not bind_refused(usr, work))
        check("/usr itself is not a source (only inside it)", bind_refused("/usr", work))
        link = state / "binds" / "deps-link"
        link.symlink_to("/etc")
        check("a symlink inside binds/ that leaves it is refused after resolving", bind_refused(link, work))
        back = state / "binds" / "pool-link"
        back.symlink_to(state / "pool")
        check("a symlink inside binds/ back into the state directory's pool/ is refused after resolving",
              bind_refused(back, work))
    with env(ROLEBENCH_TESTING="1", ROLEBENCH_HOME=str(state)):
        check("under the test guard the temp-dir root does not reopen the state directory's pool/",
              bind_refused(state / "pool", work))
        check("... nor a temp-dir parent that contains the state directory", bind_refused(root, work))
        check("... while binds/ still works", not bind_refused(state / "binds" / "deps", work))
    for bad_state in ("/", config.real_home(), os.path.expanduser("~"), os.path.dirname(os.path.expanduser("~"))):
        with env(ROLEBENCH_TESTING="1", ROLEBENCH_HOME=bad_state):
            try:
                sandbox.Cell("grade", work, extra_ro=((Path("/usr/share"), work / "dep"),))
                why = ""
            except sandbox.SandboxError as exc:
                why = str(exc)
            check(f"a state directory of {bad_state} (/, or is/contains a home) refuses every extra bind",
                  "state directory" in why and "cannot be a bind root" in why, why or "accepted")
    with env(ROLEBENCH_TESTING="1", ROLEBENCH_HOME="/"):
        check("a bad state directory does not refuse a cell with no extra binds",
              not raises(lambda: sandbox.Cell("grade", work), sandbox.SandboxError))
    real_tmp = tempfile.tempdir
    tempfile.tempdir = "/"
    try:
        with env(ROLEBENCH_TESTING="1"):
            try:
                sandbox.Cell("grade", work, extra_ro=((Path("/usr/share/zoneinfo"), work / "dep"),))
                why = ""
            except sandbox.SandboxError as exc:
                why = str(exc)
        check("under the test guard a temp dir of / is refused as a root", "temp directory is /" in why, why or "accepted")
    finally:
        tempfile.tempdir = real_tmp
    with env(ROLEBENCH_TESTING="1"):
        plain = root / "plain-in-tmp"
        plain.mkdir()
        check("under the test guard the temp dir is a source (the suites' fixtures)", not bind_refused(plain, work))
        check("... but never the temp dir itself", bind_refused(tempfile.gettempdir(), work))
    # the top level may not hold repository history or a credential file
    git = root / "with-git"
    (git / ".git").mkdir(parents=True)
    envf = root / "with-env"
    envf.mkdir()
    (envf / ".env.local").write_text("K=v")
    envfile = root / ".env"
    envfile.write_text("K=v")
    nested = root / "nested-env"
    (nested / "sub").mkdir(parents=True)
    (nested / "sub" / ".env").write_text("K=v")
    with env(ROLEBENCH_TESTING="1"):
        check("a source whose top level holds .git is refused", bind_refused(git, work))
        check("a source whose top level holds a .env.local is refused", bind_refused(envf, work))
        check("a source that is itself a .env file is refused", bind_refused(envfile, work))
        check("a .env below the top level is refused too (any depth)", bind_refused(nested, work))
        deep = root / "deep-git"
        (deep / "a" / "b" / "c" / ".git").mkdir(parents=True)
        check("a .git three levels down is refused", bind_refused(deep, work))
        linked = root / "linked-env"
        linked.mkdir()
        (linked / ".env").symlink_to("/nonexistent")
        check("a symlink named .env (dangling) is refused by its own name", bind_refused(linked, work))
        hidden = root / "behind-link"
        (hidden / "real").mkdir(parents=True)
        outside = root / "outside-with-git"
        (outside / ".git").mkdir(parents=True)
        (hidden / "real" / "via").symlink_to(outside)
        check("the walk does not follow a symlinked directory (bwrap mounts the link, not its target)",
              not bind_refused(hidden, work))
        locked = root / "locked"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner").chmod(0)
        try:
            readable = os.access(locked / "inner", os.R_OK)
            if not readable:
                check("a source with an unreadable directory is refused (fail closed)", bind_refused(locked, work))
        finally:
            (locked / "inner").chmod(0o755)
        clean = root / "clean-deep"
        (clean / "x" / "y").mkdir(parents=True)
        (clean / "x" / "y" / "env.txt").write_text("not a dotfile")
        check("a deep tree with no .git or .env* entry is accepted", not bind_refused(clean, work))


def test_readonly_work_mountpoint(root: Path) -> None:
    src = root / "ro-src"
    src.mkdir()
    for ro in (True, False):
        work = root / f"ro-work-{ro}"
        work.mkdir()
        dest = work / "node_modules"
        cell = sandbox.Cell("grade", work, work_ro=ro, extra_ro=((src, dest),))
        try:
            sandbox.bwrap_argv(cell, ["/bin/true"])
            why = ""
        except sandbox.SandboxError as exc:
            why = str(exc)
        if ro:
            check("a missing destination inside a read-only work tree is refused at argv time, saying to create it",
                  "does not exist" in why and "create it" in why, why or "accepted")
            dest.mkdir()
            check("a pre-created destination inside a read-only work tree is accepted",
                  not raises(lambda: sandbox.bwrap_argv(cell, ["/bin/true"]), sandbox.SandboxError))
        else:
            check("a missing destination inside a writable work tree is accepted (bwrap makes the mountpoint)", why == "", why)
    work = root / "ro-work-outside"
    work.mkdir()
    cell = sandbox.Cell("grade", work, work_ro=True, extra_ro=((src, Path("/opt/rolebench/not-in-work")),))
    check("a missing destination outside the work tree is not the placeholder rule's business",
          not raises(lambda: sandbox.bwrap_argv(cell, ["/bin/true"]), sandbox.SandboxError))
    judge = sandbox.Cell("judge", root / "ro-work-True", extra_ro=((src, root / "ro-work-True" / "gone"),))
    check("a judge cell (always read-only) gets the same refusal",
          raises(lambda: sandbox.bwrap_argv(judge, ["/bin/true"], net="none"), sandbox.SandboxError))


def pipe_with(data: bytes) -> int:
    r, w = os.pipe()
    os.write(w, data)
    os.close(w)
    return r


def test_managed_settings(root: Path) -> None:
    with env(ROLEBENCH_HOME=str(root / "ms-home")):
        expected = {"permissions": {"deny": ["WebSearch", "WebFetch"]}}
        for profile in ("claude", "judge"):
            work = root / f"ms-{profile}"
            work.mkdir()
            cell = sandbox.Cell(profile, work)
            argv = sandbox.bwrap_argv(cell, ["/bin/true"], net="none")
            file = str(config.home() / "sandbox" / "managed-settings.json")
            i = argv.index("/etc/claude-code/managed-settings.json")
            check(f"a {profile} cell binds the managed-settings file read-only at /etc/claude-code",
                  argv[i - 2:i] == ["--ro-bind", file], " ".join(argv[i - 3:i + 1]))
            check(f"a {profile} cell mounts a tmpfs at /etc/claude-code before binding the file",
                  "--tmpfs" in argv and argv[argv.index("/etc/claude-code") - 1] == "--tmpfs"
                  and argv.index("/etc/claude-code") < i)
            pairs = [(argv[k], argv[k + 1]) for k in range(len(argv) - 1)]
            check(f"a {profile} cell's /etc is a throwaway overlay (it needs the mountpoint) made read-only again, after the file bind",
                  ("--tmp-overlay", "/etc") in pairs and ("--remount-ro", "/etc") in pairs
                  and ("--remount-ro", "/etc/claude-code") in pairs
                  and argv.index("--remount-ro") > i and ("--ro-bind", "/etc") not in pairs)
            check(f"the {profile} cell's managed settings are only the web-tool denies",
                  json.loads(Path(file).read_text()) == expected, Path(file).read_text())
        for profile in ("grade", "jarvis", "codex"):
            work = root / f"ms-{profile}"
            work.mkdir()
            try:
                argv = sandbox.bwrap_argv(sandbox.Cell(profile, work), ["/bin/true"], net="none")
            except sandbox.SandboxError:
                continue                                      # no codex release on this host: nothing to inspect
            check(f"a {profile} cell gets no managed settings and keeps the read-only /etc bind",
                  "/etc/claude-code/managed-settings.json" not in argv and "--tmp-overlay" not in argv)
        f = config.home() / "sandbox" / "managed-settings.json"
        f.write_text("{}")
        sandbox.bwrap_argv(sandbox.Cell("claude", root / "ms-claude"), ["/bin/true"], net="none")
        check("a tampered managed-settings file is rewritten before the next cell", json.loads(f.read_text()) == expected)
        check("the managed-settings write leaves no temp file behind",
              not [p for p in f.parent.iterdir() if p.name.endswith(".tmp")], str(list(f.parent.iterdir())))
        # a real bwrap: the nested CLI's managed settings are present, and nothing in the cell can change them
        if sandbox.available():
            print("  skip managed-settings bwrap probe (bwrap or systemd-run missing)")
            return
        cell = sandbox.Cell("claude", root / "ms-claude")
        dests = [d for d, _ in sandbox.credential_binds(cell)]
        fds = {d: pipe_with(b"{}") for d in dests}
        script = ("cat /etc/claude-code/managed-settings.json; echo ---; "
                  "(echo x > /etc/claude-code/managed-settings.json) 2>/dev/null && echo WROTE; "
                  "(touch /etc/claude-code/managed-settings.d) 2>/dev/null && echo DROPIN; "
                  "(touch /etc/zz) 2>/dev/null && echo ETCWRITE; "
                  "(mv /etc/claude-code/managed-settings.json /etc/claude-code/gone) 2>/dev/null && echo MOVED; "
                  "test -e /etc/passwd && echo PASSWD")
        argv = sandbox.bwrap_argv(cell, ["/bin/sh", "-c", script], net="none", cred_fds=fds)
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, pass_fds=list(fds.values()))
        for fd in fds.values():
            os.close(fd)
        out = done.stdout
        check("in a real cell the managed settings are readable, unwritable and unremovable, and /etc stays read-only",
              json.loads(out.split("---")[0]) == expected
              and not any(w in out for w in ("WROTE", "DROPIN", "ETCWRITE", "MOVED")) and "PASSWD" in out,
              f"rc={done.returncode} out={out!r} err={done.stderr[-300:]!r}")


def test_codex_search_exact() -> None:
    argv = runners.codex_argv("gpt-6.1-sol", "/work", Path("/opt/rolebench/io/last.txt"))
    pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1)]
    check('codex_argv carries exactly one `-c web_search="disabled"` pair', pairs.count(("-c", 'web_search="disabled"')) == 1, str(argv))
    check("codex_argv sets web_search once and to nothing else",
          len([a for a in argv if a.startswith("web_search")]) == 1)
    check("codex_argv carries no live, cached or indexed search and no --search / --enable web_search",
          not any(m in a for a in argv for m in ('"live"', '"cached"', '"indexed"', "--search", "web_search_request"))
          and "--enable" not in argv)


def test_github_and_aggregate() -> None:
    """Round 2 (Opus): MIN_PREFIX is per entry, so GitHub's own /16s and any
    number of /16s passed. An entry overlapping GitHub's published ranges and an
    allowlist covering more than one /16 in all are refused."""
    check("netcell.MAX_ALLOW_ADDRESSES is one /16", netcell.MAX_ALLOW_ADDRESSES == 65536)
    import ipaddress
    gh = [ipaddress.IPv4Network(g) for g in netcell.GITHUB_V4]
    check("GITHUB_V4 is a nonempty, collapsed list of IPv4 networks",
          bool(gh) and [str(n) for n in ipaddress.collapse_addresses(gh)] == list(netcell.GITHUB_V4))
    for known in ("140.82.112.0/20", "143.55.64.0/20", "185.199.108.0/22", "192.30.252.0/22"):
        check(f"GITHUB_V4 holds GitHub's {known}", ipaddress.IPv4Network(known) in gh)
    for net in ("140.82.112.0/20", "140.82.121.4/32", "185.199.111.0/24", "192.30.0.0/16", "20.26.156.210/32"):
        check(f"an allow_dst entry {net} overlapping GitHub is refused",
              raises(lambda n=net: netcell.rules({**BASE, "allow_dst": ["160.79.104.0/23", n]}), netcell.Refused))
    for provider in (["160.79.104.0/23"], ["153.61.224.0/22"], ["160.79.104.0/23", "153.61.224.0/22"]):
        check(f"the Anthropic ranges {provider} stay accepted",
              not raises(lambda pr=provider: netcell.rules({**BASE, "allow_dst": pr})))
    check("one /16 alone is accepted (exactly the cap)", not raises(lambda: netcell.rules({**BASE, "allow_dst": ["34.0.0.0/16"]})))
    check("a /16 plus one more address is refused in aggregate",
          raises(lambda: netcell.rules({**BASE, "allow_dst": ["34.0.0.0/16", "160.79.104.7/32"]}), netcell.Refused))
    check("two /17s (exactly one /16 in all) are accepted",
          not raises(lambda: netcell.rules({**BASE, "allow_dst": ["34.0.0.0/17", "35.0.0.0/17"]})))
    many = [f"34.{b}.0.0/24" for b in range(256)] + ["35.0.0.0/24"]
    check("257 /24s (one more than a /16's worth) are refused in aggregate",
          raises(lambda: netcell.rules({**BASE, "allow_dst": many}), netcell.Refused))
    check("an entry inside another counts once (the union is what is reachable)",
          not raises(lambda: netcell.rules({**BASE, "allow_dst": ["34.0.0.0/16", "34.0.1.0/24"]})))


def test_disable_userns(root: Path) -> None:
    """Round 2 (Opus): claude and judge cells forbid nested user namespaces in
    every network mode; the other profiles are unchanged."""
    for profile in ("claude", "judge"):
        work = root / f"un-{profile}"
        work.mkdir()
        for net in ("none", "pasta"):
            argv = sandbox.bwrap_argv(sandbox.Cell(profile, work), ["/bin/true"], net=net)
            check(f"a {profile} cell ({net}) has --unshare-user and --disable-userns",
                  "--disable-userns" in argv and "--unshare-user" in argv
                  and argv.index("--disable-userns") < argv.index("--"))
    for profile in ("grade", "jarvis"):
        work = root / f"un-{profile}"
        work.mkdir()
        argv = sandbox.bwrap_argv(sandbox.Cell(profile, work), ["/bin/true"], net="none")
        check(f"a {profile} cell is unchanged (no --disable-userns)", "--disable-userns" not in argv)
    if sandbox.available() or not shutil.which("unshare"):
        print("  skip nested-userns bwrap probe (bwrap, systemd-run or unshare missing)")
        return
    cell = sandbox.Cell("claude", root / "un-claude")
    script = ("unshare -Urm sh -c 'mount -t tmpfs none /etc/claude-code && echo OVERMOUNT_OK' 2>&1; "
              "unshare -U true 2>/dev/null && echo NESTED_USERNS_OK; "
              "cat /etc/claude-code/managed-settings.json >/dev/null && echo MANAGED_PRESENT")

    def run(argv_filter):
        dests = [d for d, _ in sandbox.credential_binds(cell)]
        fds = {d: pipe_with(b"{}") for d in dests}
        argv = argv_filter(sandbox.bwrap_argv(cell, ["/bin/sh", "-c", script], net="none", cred_fds=fds))
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, pass_fds=list(fds.values()))
        for fd in fds.values():
            os.close(fd)
        return done

    done = run(lambda a: a)
    check("in a real claude cell nested user namespaces fail, so /etc/claude-code cannot be over-mounted",
          "MANAGED_PRESENT" in done.stdout and "OVERMOUNT_OK" not in done.stdout
          and "NESTED_USERNS_OK" not in done.stdout, f"rc={done.returncode} out={done.stdout!r} err={done.stderr[-300:]!r}")
    control = run(lambda a: [x for x in a if x != "--disable-userns"])
    if "NESTED_USERNS_OK" in control.stdout:
        check("control: without --disable-userns the same cell can over-mount (so the flag is what closes it)",
              "OVERMOUNT_OK" in control.stdout, control.stdout)
    else:
        print("  skip nested-userns control (this host gives no user namespaces inside bwrap anyway)")


def test_dest_symlink(root: Path) -> None:
    """Round 2 (Sol): a destination in the work tree reached through a symlink,
    dangling or not, is refused before launch, in a read-only or writable tree."""
    with env(ROLEBENCH_TESTING="1"):
        src = root / "sym-src"
        src.mkdir()
        for ro in (True, False):
            work = root / f"sym-work-{ro}"
            (work / "vendor").mkdir(parents=True)
            dangling = work / "node_modules"
            dangling.symlink_to("vendor/node_modules", target_is_directory=True)

            def why(dest, w=work, r=ro):
                try:
                    sandbox.bwrap_argv(sandbox.Cell("grade", w, work_ro=r, extra_ro=((src, dest),)), ["/bin/true"])
                except sandbox.SandboxError as exc:
                    return str(exc)
                return ""
            got = why(dangling)
            check(f"a dangling symlink destination (work_ro={ro}) is refused, saying its target does not exist",
                  "symlink" in got and "does not exist" in got, got or "accepted")
            (work / "vendor" / "node_modules").mkdir()
            got = why(dangling)
            check(f"a symlink destination with an existing target (work_ro={ro}) is refused too", "symlink" in got, got or "accepted")
            (work / "real").mkdir()
            (work / "via").symlink_to("real", target_is_directory=True)
            got = why(work / "via" / "deps")
            check(f"a destination under a symlinked parent in the tree (work_ro={ro}) is refused", "symlink" in got, got or "accepted")
            out = work / "esc"
            out.symlink_to("/etc", target_is_directory=True)
            got = why(out)
            check(f"an in-tree symlink aiming the mount outside the tree (work_ro={ro}) is refused", "symlink" in got, got or "accepted")
            (work / "plain").mkdir()
            check(f"a real directory destination in the tree (work_ro={ro}) is accepted", why(work / "plain") == "")
        work = root / "sym-work-True"
        side = root / "side-door"
        side.symlink_to(work, target_is_directory=True)
        got = why(side / "plain", work, True)
        check("a destination that reaches the work tree through an outside symlink is refused", "symlink" in got, got or "accepted")


def test_publish_unique_temp(root: Path) -> None:
    """Round 2 (Sol): concurrent first launches each stage into their own temp
    file, so none renames another's away; the result is always whole."""
    import threading
    with env(ROLEBENCH_HOME=str(root / "pub-home")):
        target = config.home() / "sandbox" / "managed-settings.json"
        errors: list[str] = []
        gate = threading.Barrier(8)

        def build():
            try:
                gate.wait(timeout=5)
                for _ in range(20):
                    if target.exists():
                        target.write_text("{}")         # force a rewrite each round
                    sandbox._managed_settings_file()
                    sandbox._gitconfig_file()
            except Exception as exc:                     # noqa: BLE001  any failure is the finding
                errors.append(f"{type(exc).__name__}: {exc}")
        threads = [threading.Thread(target=build) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("8 threads publishing the managed settings and gitconfig at once all succeed", not errors, str(errors[:3]))
        check("the published managed settings are whole", target.read_text() == sandbox.MANAGED_SETTINGS_TEXT)
        check("no temp file is left behind", not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")],
              str(sorted(p.name for p in target.parent.iterdir())))


PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from rolebench import netcell
netcell.IPV6_CONF = "/nonexistent"
pol = json.loads(sys.argv[2])
netcell.install(pol)                 # adds the rules, then reads them back and verifies
import subprocess
print(subprocess.run([netcell.IP, "-4", "rule", "show"], capture_output=True, text=True).stdout)
"""


def test_real_namespace() -> None:
    if not shutil.which("unshare") or not os.access(netcell.IP, os.X_OK):
        print("  skip namespace round trip (unshare or ip missing)")
        return
    pol = {**BASE, "allow_dst": ANTHROPIC}
    repo = str(Path(__file__).resolve().parents[1])
    try:
        done = subprocess.run(["unshare", "-rn", sys.executable, "-I", "-c", PROBE, repo, json.dumps(pol)],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  skip namespace round trip ({exc})")
        return
    if done.returncode and "unshare" in done.stderr:
        print(f"  skip namespace round trip ({done.stderr.strip()[:80]})")
        return
    check("install and read-back verify succeed in a real (empty) network namespace", done.returncode == 0, done.stderr[-300:])
    check("the kernel holds the destination-scoped allow rule and no open TCP 443 rule",
          "to 160.79.104.0/23 ipproto tcp dport 443 lookup main" in done.stdout
          and not any(line.startswith("110:") and " to " not in line for line in done.stdout.splitlines()), done.stdout)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="rb-egress2-"))
    try:
        test_verify_destinations()
        test_policy_validation()
        test_install_commands()
        test_extra_ro_refusals(root)
        test_cell_command(root)
        test_breadth()
        test_extra_ro_allowlist(root)
        test_readonly_work_mountpoint(root)
        test_managed_settings(root)
        test_codex_search_exact()
        test_github_and_aggregate()
        test_disable_userns(root)
        test_dest_symlink(root)
        test_publish_unique_temp(root)
        test_real_namespace()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
