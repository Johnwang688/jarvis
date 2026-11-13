"""Opus verifier, PR #356 round 2: adversarial checks on the fix round
(managed settings, extra_ro root allowlist, MIN_PREFIX). A FAIL is a finding.
No model, no network, no mount: cells are only constructed and argv built.

    PYTHONPATH=<checkout> HOME=<tmp> python tests/rolebench_opus_egress_r2_check.py

Live evidence for the first check (a nested user+mount namespace shadows the
managed settings while the login and CLI stay usable; bwrap --disable-userns
closes it): ~/.cache/jarvis-lead/economy/probes/opus_managed_settings_probe.py.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rolebench_testguard  # noqa: E402,F401  first

from rolebench import netcell, sandbox  # noqa: E402

PASSED = 0
FAILED: list[str] = []
BASE = {"dns": "192.0.2.53", "block": list(sandbox.BLOCK_V4) + ["172.22.26.16/32"], "allow_tcp": [443]}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))


def refused(fn, exc=Exception) -> tuple[bool, str]:
    try:
        fn()
    except exc as e:
        return True, str(e)
    return False, "accepted"


def test_managed_settings_cannot_be_shadowed(root: Path) -> None:
    """The managed file is read-only, but bwrap leaves nested user namespaces on:
    `unshare -Urm sh -c 'mount -t tmpfs none /etc/claude-code; claude -p --tools
    WebSearch ...'` runs the nested CLI (same netns, login readable as the
    nested root) with no managed settings. Live: OVERMOUNT_OK and FILEBIND_OK.
    With --disable-userns after --unshare-user, unshare fails (ENOSPC)."""
    work = root / "w"
    work.mkdir()
    argv = sandbox.bwrap_argv(sandbox.Cell("claude", work), ["/bin/true"], net="pasta")
    check("a claude cell forbids nested user namespaces (bwrap --disable-userns), so the managed "
          "settings cannot be over-mounted", "--disable-userns" in argv, " ".join(a for a in argv if a.startswith("--")
                                                                                 and "userns" in a) or "no userns flag")


def test_state_dir_override_reopens_everything(work: Path) -> None:
    """The allowlist root and the home exemption both come from config.home(),
    which ROLEBENCH_HOME sets. ROLEBENCH_HOME=/ (or the account home, or any
    ancestor of it) makes every path 'inside the bench's state directory'."""
    bus = Path(f"/run/user/{os.getuid()}")
    if not bus.exists():
        print("  skip state-dir override check (no /run/user/<uid>)")
        return
    old = os.environ.get("ROLEBENCH_HOME")
    os.environ["ROLEBENCH_HOME"] = "/"
    try:
        ok, why = refused(lambda: sandbox.Cell("grade", work, extra_ro=((bus, work / "dep"),)), sandbox.SandboxError)
    finally:
        if old is None:
            os.environ.pop("ROLEBENCH_HOME", None)
        else:
            os.environ["ROLEBENCH_HOME"] = old
    check("with ROLEBENCH_HOME=/ an extra bind of the user bus directory is still refused "
          "(a state dir that is / or holds the home is not a root)", ok, why)


def test_history_and_env_below_top_level(root: Path, work: Path) -> None:
    """The .git/.env refusal reads only the source's top level."""
    src = root / "deps"
    (src / "pkg" / ".git").mkdir(parents=True)
    (src / "pkg" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    ok, why = refused(lambda: sandbox.Cell("grade", work, extra_ro=((src, work / "dep"),)), sandbox.SandboxError)
    check("an extra bind whose subdirectory is a git checkout is refused (history past the base)", ok, why)
    src2 = root / "deps2"
    (src2 / "app").mkdir(parents=True)
    (src2 / "app" / ".env").write_text("X=dummy\n")
    ok, why = refused(lambda: sandbox.Cell("grade", work, extra_ro=((src2, work / "dep"),)), sandbox.SandboxError)
    check("an extra bind holding a .env one level down is refused", ok, why)


def test_sealed_material_in_state_dir(root: Path, work: Path) -> None:
    """The allowlisted root is the bench's own state directory, which holds the
    pooled ground truth (pool/), the spot-check keys and every other cell's
    io/work trees."""
    state = root / "state"
    for sub in ("pool", "spotcheck-keys"):
        (state / sub).mkdir(parents=True)
        (state / sub / "x.json").write_text("{}")
    old = os.environ.get("ROLEBENCH_HOME")
    os.environ["ROLEBENCH_HOME"] = str(state)
    try:
        for sub in ("pool", "spotcheck-keys"):
            ok, why = refused(lambda s=sub: sandbox.Cell("grade", work, extra_ro=((state / s, work / "dep"),)),
                              sandbox.SandboxError)
            check(f"an extra bind of the bench's {sub}/ (sealed answers) is refused", ok, why)
    finally:
        if old is None:
            os.environ.pop("ROLEBENCH_HOME", None)
        else:
            os.environ["ROLEBENCH_HOME"] = old


def test_min_prefix_still_admits_github() -> None:
    """MIN_PREFIX=16 is per entry: 140.82.0.0/16 (GitHub's 140.82.112.0/20) and
    185.199.0.0/16 (githubusercontent) pass, and so does any number of /16s."""
    for net in ("140.82.0.0/16", "185.199.0.0/16", "192.30.0.0/16"):
        ok, why = refused(lambda n=net: netcell.rules({**BASE, "allow_dst": [n]}), netcell.Refused)
        check(f"an allow_dst entry {net} covering GitHub's published ranges is refused", ok, why)
    many = [f"{a}.{b}.0.0/16" for a in (20,) for b in range(0, 64)]
    ok, why = refused(lambda: netcell.rules({**BASE, "allow_dst": many}), netcell.Refused)
    check("64 /16 entries (a /10 of Azure, where github.com also lives) are refused in aggregate", ok, why)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="rb-opus-r2-"))
    work = root / "work"
    work.mkdir()
    try:
        test_managed_settings_cannot_be_shadowed(root)
        test_state_dir_override_reopens_everything(work)
        test_history_and_env_below_top_level(root, work)
        test_sealed_material_in_state_dir(root, work)
        test_min_prefix_still_admits_github()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
