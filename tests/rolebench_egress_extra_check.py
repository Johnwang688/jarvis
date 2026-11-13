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
    pol = {**BASE, "allow_dst": ANTHROPIC + ["34.0.0.0/16"]}
    exact = [netcell._key(r) for r in netcell.rules(pol)] + list(netcell.DEFAULTS_V4)
    check("verify accepts exactly the policy's rules", verify_with(pol, exact))
    swapped = [(k[0], "8.8.8.0/24", *k[2:]) if k[1] == "34.0.0.0/16" else k for k in exact]
    check("verify refuses a rule whose destination differs", not verify_with(pol, swapped))
    widened = [(k[0], "160.79.104.0/22", *k[2:]) if k[1] == ANTHROPIC[0] else k for k in exact]
    check("verify refuses a wider destination than the policy's", not verify_with(pol, widened))
    open443 = exact + [(110, "", "tcp", 443, "main")]
    check("verify refuses an extra TCP 443 rule open to any destination", not verify_with(pol, open443))
    check("verify refuses a missing allow rule", not verify_with(pol, [k for k in exact if k[1] != "34.0.0.0/16"]))
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
    two = netcell.rules({**BASE, "allow_tcp": [443, 8443], "allow_dst": ANTHROPIC + ["34.0.0.0/16"]})
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
    (state / "deps").mkdir(parents=True)
    (state / "deps" / "f").write_text("x")
    for door in ("/etc", "/run", "/proc", "/mnt/c", "/var", "/home", "/"):
        if os.path.exists(door):
            check(f"an extra bind of {door} is refused (not inside the state directory, /usr or /opt)", bind_refused(door, work))
    # production strictness: without the test guard's temp-dir allowance only the
    # state directory, /usr and /opt are sources
    with env(ROLEBENCH_TESTING=None, ROLEBENCH_HOME=str(state)):
        check("the state directory's own subtree is an allowed source",
              not bind_refused(state / "deps", work))
        check("the state directory itself is not a source (only inside it)", bind_refused(state, work))
        plain = root / "plain-outside"
        plain.mkdir()
        check("a plain directory in the system temp dir is refused outside the test guard", bind_refused(plain, work))
        usr = next((p for p in ("/usr/share/zoneinfo", "/usr/share/doc", "/usr/lib/python3") if os.path.isdir(p)), None)
        if usr:
            check(f"a directory inside /usr ({usr}) is an allowed source", not bind_refused(usr, work))
        check("/usr itself is not a source (only inside it)", bind_refused("/usr", work))
        link = state / "deps-link"
        link.symlink_to("/etc")
        check("a symlink inside the state directory that leaves it is refused after resolving", bind_refused(link, work))
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
        check("a .env below the top level is not the rule's business", not bind_refused(nested, work))


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
        check("the managed-settings write leaves no temp file behind", not f.with_suffix(".tmp").exists())
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
        test_real_namespace()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
