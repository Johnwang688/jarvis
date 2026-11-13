"""Opus verifier's adversarial checks for PR #356 (egress allowlist, extra
read-only binds). Each check encodes a bypass or gap found by probing; a FAIL
here is a finding, not a flaky test. No model, no network, no mount: cells are
only constructed and their argv built.

    PYTHONPATH=<checkout> HOME=<tmp> python tests/rolebench_opus_egress_check.py

Live-cell evidence (multicast leaving a pasta cell, extra_ro binds of the user
bus, a checkout's .env and other credential stores) is in the lead's probe
directory: ~/.cache/jarvis-lead/economy/probes/opus_egress_probe.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

from rolebench import config, netcell, sandbox  # noqa: E402

PASSED = 0
FAILED: list[str] = []
BASE = {"dns": "192.0.2.53", "block": list(sandbox.BLOCK_V4) + ["172.22.26.16/32"], "allow_tcp": [443]}
ANTHROPIC = ("160.79.104.0/23",)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def refused(fn, exc=Exception) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


# --- allow_dst breadth -------------------------------------------------------------

def test_allow_dst_breadth() -> None:
    """0.0.0.0/0 and 128.0.0.0/1 are refused only because they happen to overlap
    a blocked range. A huge prefix that dodges every blocked range is accepted,
    and one of them covers GitHub's own 140.82.112.0/20 and 143.55.64.0/20."""
    for wide in ("32.0.0.0/3", "140.0.0.0/6", "1.0.0.0/8"):
        check(f"an allow_dst entry as broad as {wide} is refused (no maximum breadth today)",
              refused(lambda w=wide: netcell.rules({**BASE, "allow_dst": [w]}), netcell.Refused))
    # sanity: the provider's own /23 must stay accepted whatever cap is chosen
    check("the Anthropic /23 is still accepted", not refused(lambda: netcell.rules({**BASE, "allow_dst": list(ANTHROPIC)})))


# --- extra_ro: what the module itself says is never mounted -----------------------

def refused_bind(src: Path, work: Path) -> tuple[bool, str]:
    try:
        sandbox.Cell("grade", work, extra_ro=((src, work / "dep"),))
    except sandbox.SandboxError as exc:
        return True, str(exc)
    return False, "accepted"


def test_extra_ro_system_doors(work: Path) -> None:
    """sandbox.py's docstring: "Not mounted, whatever the harness: ... /mnt/c ...,
    /run (so no user bus: nothing inside can ask systemd to start a process
    outside)". extra_ro can mount each of them. Live: a cell with
    extra_ro=(/run/user/<uid>, /hostrun) connected to /hostrun/bus."""
    doors = [Path(f"/run/user/{os.getuid()}"), Path("/run"), Path("/proc"),
             Path("/mnt/c"), Path("/mnt/wslg")]
    for door in doors:
        if not os.path.exists(door):
            print(f"  skip {door} (absent here)")
            continue
        ok, why = refused_bind(door, work)
        check(f"an extra read-only bind of {door} is refused", ok, why)


def test_extra_ro_other_credentials(root: Path, work: Path) -> None:
    """The refusal covers five trees only. Live, on this host, a cell mounted
    ~/.supabase (access-token), ~/.local/share (keyrings, the Vercel CLI's
    auth.json) and ~/projects/Jarvis (.env, plus .git with history past any
    contract base) and could read all of it."""
    home = root / "acct"
    for tree in sandbox.CREDENTIAL_TREES:
        (home / tree).mkdir(parents=True)
    stores = {
        ".claude.json": "file",                         # beside ~/.claude, not inside it
        ".supabase/access-token": "file",
        ".aws/credentials": "file",
        ".azure/msal_token_cache.json": "file",
        ".docker/config.json": "file",
        ".git-credentials": "file",
        ".netrc": "file",
        ".gitconfig": "file",
        ".local/share/keyrings/login.keyring": "file",
        ".local/share/jarvis/state": "file",
        ".local/share/com.vercel.cli/auth.json": "file",
        "projects/app/.env": "file",
    }
    for rel in stores:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dummy")
    real_home = config.real_home
    config.real_home = lambda: str(home)
    try:
        cases = [(".claude.json", home / ".claude.json"), ("~/.supabase", home / ".supabase"),
                 ("~/.aws", home / ".aws"), ("~/.azure", home / ".azure"), ("~/.docker", home / ".docker"),
                 ("~/.git-credentials", home / ".git-credentials"), ("~/.netrc", home / ".netrc"),
                 ("~/.local/share/keyrings", home / ".local/share/keyrings"),
                 ("~/.local/share (keyrings, jarvis state, vercel auth)", home / ".local/share"),
                 ("~/.local/share/jarvis", home / ".local/share/jarvis"),
                 ("a project checkout holding a .env", home / "projects/app"),
                 ("~/projects (holds checkouts with .env files)", home / "projects")]
        for label, src in cases:
            ok, why = refused_bind(src, work)
            check(f"an extra read-only bind of {label} is refused", ok, why)
    finally:
        config.real_home = real_home


# --- the provider itself as a fetcher ---------------------------------------------

def test_claude_cell_provider_side_web(root: Path) -> None:
    """allow_dst limits IP egress to the provider, but the provider fetches the
    web for you: a claude cell puts the claude CLI on PATH (/opt/rolebench/bin)
    and its login in $HOME, so the agent's Bash can run
    `claude -p --tools WebSearch ...` (Anthropic's server-side search) or POST
    /v1/messages with a server web tool, neither of which crosses netcell. The
    PR turns codex's server-side search off for exactly this reason and does
    nothing equivalent for claude. Not confirmed live (it needs a model call);
    this checks the precondition and the one in-cell mitigation for the CLI
    route: a managed-settings file, which no CLI flag or project setting can
    override, denying WebSearch and WebFetch."""
    work = root / "w-claude"
    work.mkdir()
    cell = sandbox.Cell("claude", work, allow_dst=ANTHROPIC)
    argv = sandbox.bwrap_argv(cell, ["/bin/true"], net="pasta")
    env = sandbox.env_for(cell)
    on_path = str(sandbox.BIN) in env["PATH"].split(":") and str(sandbox.BIN / "claude") in argv
    managed = "/etc/claude-code/managed-settings.json"
    has_managed = managed in argv
    check("a claude cell under allow_dst denies the nested claude CLI its server-side web tools "
          "(managed settings at /etc/claude-code)",
          not on_path or has_managed,
          f"claude on the agent's PATH: {on_path}; managed settings bound: {has_managed}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="rb-opus-egress-"))
    work = root / "work"
    work.mkdir()
    try:
        test_allow_dst_breadth()
        test_extra_ro_system_doors(work)
        test_extra_ro_other_credentials(root, work)
        test_claude_cell_provider_side_web(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
