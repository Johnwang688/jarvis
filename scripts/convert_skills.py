#!/usr/bin/env python3
"""Migrate flat skills in place; tracked files use git mv to preserve history."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

JARVIS_ONLY = frozenset({"whiteboard", "self-improve", "manual-compaction", "permission-allowlist"})


def converted(source: Path) -> bytes:
    text = source.read_bytes().decode("utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match or not re.search(r"^description:", match[1], re.M):
        raise ValueError(f"Missing description frontmatter: {source}")
    # Preserve the original frontmatter and body, including whitespace.
    newline = "\r\n" if text.startswith("---\r\n") else "\n"
    fields = f"name: {source.stem}{newline}"
    if source.stem in JARVIS_ONLY:
        fields += f"jarvis-only: true{newline}"
    return (text[:match.start(1)] + fields + text[match.start(1):]).encode("utf-8")


def convert(root: Path) -> list[Path]:
    plans = []
    for source in sorted(root.glob("*.md")):
        target = root / source.stem / "SKILL.md"
        data = converted(source)
        if target.is_symlink() or (target.exists() and target.read_bytes() != data):
            raise ValueError(f"Refusing to overwrite differing SKILL.md: {target}")
        plans.append((source, target, data))
    # Preflight every destination before moving anything.
    for source, target, data in plans:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", source.name],
            cwd=root, capture_output=True,
        ).returncode == 0
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if tracked:
                subprocess.run(["git", "rm", "--", source.name], cwd=root, check=True)
            else:
                source.unlink()
        else:
            if tracked:
                subprocess.run(["git", "mv", "--", source.name,
                                str(target.relative_to(root))], cwd=root, check=True)
            else:
                source.rename(target)
            target.write_bytes(data)
    return [target for _, target, _ in plans]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1] / "skills")
    args = parser.parse_args()
    try:
        paths = convert(args.directory.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Converted {len(paths)} skill(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
