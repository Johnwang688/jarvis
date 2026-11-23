#!/usr/bin/env python3
"""Report commits that touch the archived v2 code or its shared gates."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FROZEN_DIRS = ("jarvis/v2/", "hud/")
SHARED_GATE_FILES = {"jarvis/v2/owner_ops.py", "jarvis/v2/permissions.py"}
# Escape line breaks and keep literal backslashes distinct from those escapes.
PATH_ESCAPES = str.maketrans({
    char: ascii(char)[1:-1] for char in "\\" "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
})


def git(repo, *args):
    env = os.environ.copy()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_NO_LAZY_FETCH="1")
    result = subprocess.run(
        ["git", "--no-pager", "--no-replace-objects", "-C", str(repo),
         "-c", "core.quotepath=false", *args],
        capture_output=True, env=env,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"git {args[0]} exited {result.returncode}")
    return result.stdout


def report(repo, since):
    try:
        base = git(repo, "rev-parse", "--verify", "--end-of-options", f"{since}^{{commit}}")
    except RuntimeError as error:
        raise RuntimeError(f"cannot resolve {since!r} to a commit: {error}") from error

    if git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise RuntimeError("shallow repository: complete history is required")

    # Keep both sides of a rename visible as a deletion and an addition.
    history = git(
        repo, "log", "--no-merges", "--full-history", "--topo-order", "--root", "--no-follow",
        "--no-show-signature", "--no-notes", "--no-decorate", "--no-color",
        "--no-ext-diff", "--no-textconv", "--no-relative", "--no-renames",
        "--ignore-submodules=none", "--name-only", "-z",
        "--abbrev=7", "--encoding=UTF-8", "--format=%x00%h %s",
        f"{base.decode('ascii').strip()}..HEAD", "--", *FROZEN_DIRS,
    ).decode("utf-8", errors="surrogateescape")

    violated = False
    if not history:
        return violated
    # The format starts each header with a NUL, and -z ends each field with one.
    for record in history.removeprefix("\0").split("\0\0"):
        header, *paths = record.rstrip("\0").split("\0")
        if not paths:
            continue
        paths[0] = paths[0].removeprefix("\n")  # git separates the header and diff
        print(header)
        subject = header.partition(" ")[2]
        for path in sorted(paths):
            display_path = path.translate(PATH_ESCAPES)
            if path in SHARED_GATE_FILES:
                print(f"  shared gate: {display_path}")
            else:
                print(f"  frozen: {display_path}")
                if not subject.startswith("v2-fix:"):
                    violated = True
    return violated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT, help="repository to inspect")
    parser.add_argument("--since", default="v2-final", help="base commit or tag (default: v2-final)")
    parser.add_argument("--check", action="store_true", help="fail on unprefixed frozen changes")
    args = parser.parse_args()
    try:
        violated = report(args.repo, args.since)
    except (OSError, RuntimeError) as error:
        print(f"v2 freeze report: {error}", file=sys.stderr)
        return 2
    return int(args.check and violated)


if __name__ == "__main__":
    sys.exit(main())
