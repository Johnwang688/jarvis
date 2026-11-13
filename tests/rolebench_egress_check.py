"""Free checks for the egress allowlist, extra read-only binds and the codex
web-search switch (no paid calls, no model, no network beyond a cell probe).

    PYTHONPATH=<checkout> HOME=<tmp> python tests/rolebench_egress_check.py

Acceptance tests for the agent-economy EC-1 rolebench change (written by the
lead before the build; the implementer may not edit them).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

from rolebench import netcell, runners, sandbox  # noqa: E402

PASSED = 0
FAILED: list[str] = []
ANTHROPIC = ("160.79.104.0/23",)


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


def test_netcell_allow_dst_rules() -> None:
    base = {"dns": "192.0.2.53", "block": ["10.0.0.0/8"], "allow_tcp": [443]}
    open_rules = netcell.rules(base)
    check("without allow_dst, TCP 443 is open to any destination (unchanged default)",
          any(r.get("dport") == 443 and not r.get("dst") and r["do"] == "main" for r in open_rules))
    pol = {**base, "allow_dst": list(ANTHROPIC)}
    rules = netcell.rules(pol)
    allow = [r for r in rules if r["do"] == "main" and r.get("dport") == 443]
    check("with allow_dst, every TCP 443 allow names a destination",
          bool(allow) and all(r.get("dst") for r in allow), str(allow))
    check("with allow_dst, the allowed destinations are exactly the list",
          sorted(str(r.get("dst")) for r in allow) == sorted(ANTHROPIC), str(allow))
    check("with allow_dst, no rule lets TCP 443 out to anywhere",
          not any(r.get("dport") == 443 and not r.get("dst") and r["do"] == "main" for r in rules))
    check("with allow_dst, DNS to the forwarder is still allowed",
          any(r.get("dport") == 53 and r["do"] == "main" for r in rules))
    check("with allow_dst, the final rule still prohibits everything else", rules[-1] == {"pref": 120, "do": "prohibit"})
    check("allow_dst rules sit after the blocked ranges (blocked ranges still win)",
          all(r["pref"] > 100 for r in allow))
    check("an allow_dst entry inside a blocked range is refused",
          raises(lambda: netcell.rules({**base, "allow_dst": ["10.1.2.0/24"]}), netcell.Refused))
    check("a malformed allow_dst entry is refused",
          raises(lambda: netcell.rules({**base, "allow_dst": ["not-a-net"]})))
    check("an empty allow_dst list is refused (it would mean 'no API at all' by accident)",
          raises(lambda: netcell.rules({**base, "allow_dst": []}), netcell.Refused))
    argv = netcell._argv(allow[0])
    check("an allow_dst rule installs as 'to <dst> ipproto tcp dport 443 lookup main'",
          argv[argv.index("to") + 1] == allow[0]["dst"] and "dport" in argv and argv[-2:] == ["lookup", "main"], str(argv))
    keys = [netcell._key(r) for r in rules]
    check("the read-back key of an allow_dst rule carries its destination",
          any(k[1] == ANTHROPIC[0] and k[3] == 443 for k in keys), str(keys))


def test_cell_policy_and_binds(root: Path) -> None:
    work = root / "work"
    work.mkdir()
    cell = sandbox.Cell("claude", work, allow_dst=ANTHROPIC)
    pol = sandbox.pasta_policy(cell)
    check("a cell's allow_dst reaches the pasta policy", pol.get("allow_dst") == list(ANTHROPIC), str(pol))
    check("a cell without allow_dst keeps today's policy (no allow_dst key)",
          "allow_dst" not in sandbox.pasta_policy(sandbox.Cell("claude", work)))
    check("pasta_policy() with no cell still works (existing callers)", "allow_dst" not in sandbox.pasta_policy())
    check("allow_dst on a grade cell (no network) is refused",
          raises(lambda: sandbox.Cell("grade", work, allow_dst=ANTHROPIC), sandbox.SandboxError))
    src = root / "deps"
    src.mkdir()
    dest = work / "node_modules"
    cell2 = sandbox.Cell("grade", work, extra_ro=((src, dest),))
    argv = sandbox.bwrap_argv(cell2, ["/bin/true"])
    work_i = max(i for i, a in enumerate(argv) if a in ("--bind", "--ro-bind") and argv[i + 2] == str(work.resolve()))
    ro = [i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == str(src.resolve())]
    check("an extra read-only bind is in the argv", bool(ro), " ".join(argv))
    check("an extra read-only bind is mounted after the work tree (so it can sit inside it)",
          bool(ro) and ro[0] > work_i)
    check("an extra read-only bind's destination is the requested path",
          bool(ro) and argv[ro[0] + 2] == str(dest), " ".join(argv))
    check("an extra bind whose source is missing is refused",
          raises(lambda: sandbox.bwrap_argv(sandbox.Cell("grade", work, extra_ro=((root / "nope", dest),)), ["/bin/true"]),
                 sandbox.SandboxError))
    check("an extra bind onto a relative destination is refused",
          raises(lambda: sandbox.bwrap_argv(sandbox.Cell("grade", work, extra_ro=((src, Path("rel")),)), ["/bin/true"]),
                 sandbox.SandboxError))


def test_codex_web_search_off() -> None:
    argv = runners.codex_argv("gpt-6.1-sol", "/work", Path("/opt/rolebench/io/last.txt"))
    joined = " ".join(argv)
    check("codex_argv turns web search off explicitly", any("web_search" in a for a in argv), joined)
    check("codex_argv never passes --search", "--search" not in argv)
    check("codex_argv keeps the command sandbox's network off",
          "sandbox_workspace_write.network_access=false" in argv)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="rb-egress-"))
    test_netcell_allow_dst_rules()
    test_cell_policy_and_binds(root)
    test_codex_web_search_off()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
