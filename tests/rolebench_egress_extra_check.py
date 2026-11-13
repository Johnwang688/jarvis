"""Further free checks for the egress allowlist and extra binds (EC-1a): the
read-back verifies destinations, the credential-tree refusals, the pasta
command line, and a real install + read-back in a throwaway empty network
namespace (`unshare -rn`; skipped when that is unavailable). No model, no
network, no paid call.

    PYTHONPATH=<checkout> HOME=<tmp> python tests/rolebench_egress_extra_check.py
"""

from __future__ import annotations

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

from rolebench import config, netcell, sandbox  # noqa: E402

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
        test_real_namespace()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
