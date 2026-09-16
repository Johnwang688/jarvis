"""Share repo skills with installed Claude Code and Codex consumers."""
from __future__ import annotations

from pathlib import Path

from jarvis import config
from jarvis.tools.skills import metadata


def link(*, repo: Path | None = None, home: Path | None = None,
         unlink: bool = False, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Return (consumer, skill, action) rows; never replace a foreign entry."""
    repo = (repo or config.REPO_ROOT).resolve()
    home = home or Path.home()
    sources = {p.parent.name: p.parent for p in sorted((repo / "skills").glob("*/SKILL.md"))}
    rows = []
    for consumer in (".claude", ".codex"):
        parent = home / consumer
        destination = parent / "skills"
        if not parent.is_dir():
            rows.append((consumer, "*", "absent parent; not created"))
            continue
        if destination.exists() and not destination.is_dir():
            rows.append((consumer, "*", "refused: skills path is not a directory"))
            continue
        names = sorted(set(sources) | ({p.name for p in destination.iterdir()}
                                     if unlink and destination.is_dir() else set()))
        for name in names:
            target = destination / name
            try:
                if unlink:
                    if target.is_symlink() and target.resolve().is_relative_to(repo):
                        action = "would unlink" if dry_run else "unlinked"
                        if not dry_run:
                            target.unlink()
                    else:
                        action = "kept: not a link into this repo"
                elif metadata((sources[name] / "SKILL.md").read_text(encoding="utf-8")).get(
                        "jarvis-only", "").lower() == "true":
                    action = "skipped: jarvis-only"
                elif target.is_symlink() and target.resolve() == sources[name].resolve():
                    action = "skipped: correct link"
                elif target.is_symlink() or target.exists():
                    action = "refused: existing entry points elsewhere or is not a link"
                else:
                    action = "would link" if dry_run else "linked"
                    if not dry_run:
                        destination.mkdir(exist_ok=True)
                        target.symlink_to(sources[name], target_is_directory=True)
                rows.append((consumer, name, action))
            except (OSError, RuntimeError) as exc:
                rows.append((consumer, name, f"refused: {exc}"))
    return rows


def main(*, unlink: bool = False, dry_run: bool = False) -> int:
    rows = link(unlink=unlink, dry_run=dry_run)
    print(f"{'Consumer':<10} {'Skill':<24} Action")
    for consumer, name, action in rows:
        print(f"{consumer:<10} {name:<24} {action}")
    return int(any(action.startswith("refused:") for _, _, action in rows))
