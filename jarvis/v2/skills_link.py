"""Share skills with installed Claude Code and Codex consumers.

The repo's skills are linked into both `~/.claude/skills` and
`~/.codex/skills`. The owner's own Claude Code skills (`config.CLAUDE_SKILLS_DIR`,
SK-2) are linked into `~/.codex/skills` only, at the resolved skill folder:
`~/.claude/skills` is where they come from, so nothing is made there for them.
A repo skill wins a name. Refused external entries are reported, never linked
(`jarvis.tools.skills.external_skills`)."""
from __future__ import annotations

import os
from pathlib import Path

from jarvis import config
from jarvis.tools.skills import external_skills, metadata


def _external(repo: Path, repo_names: set[str]) -> tuple[dict[str, Path], list[tuple[str, str]]]:
    """({name: resolved skill folder}, [(name, refusal)]) for external skills
    the repo does not hold, and not links back into the repo."""
    found, refused = external_skills()
    skills_dir = repo / "skills"
    sources = {name: path.parent for name, path in found.items()
               if name not in repo_names and not path.is_relative_to(skills_dir)}
    return sources, [(n, why) for n, why in refused if n not in repo_names]


def _external_roots() -> list[Path]:
    root = getattr(config, "CLAUDE_SKILLS_DIR", None)
    if root is None:
        return []
    root = Path(os.path.abspath(Path(root).expanduser()))
    try:
        return [root, root.resolve()]
    except (OSError, RuntimeError):
        return [root]


def _recorded(target: Path) -> Path:
    """Where a symlink says it points, absolute, without following it."""
    raw = Path(os.readlink(target))
    return Path(os.path.normpath(raw if raw.is_absolute() else target.parent / raw))


def _ours(target: Path, consumer: str, repo: Path, external: dict[str, Path]) -> bool:
    """Is `target` a link `link()` made? Into the repo (either consumer, as
    before); or, under .codex only, at a current external skill's folder, or
    dangling with its recorded target inside the repo's skills or the external
    root (a skill deleted since it was linked)."""
    if not target.is_symlink():
        return False
    resolved = target.resolve()
    if resolved.is_relative_to(repo):
        return True
    if consumer != ".codex":
        return False
    if resolved in {path.resolve() for path in external.values()}:
        return True
    if os.path.exists(target):
        return False
    recorded = _recorded(target)
    return any(recorded.is_relative_to(root) for root in [repo / "skills", *_external_roots()])


def link(*, repo: Path | None = None, home: Path | None = None,
         unlink: bool = False, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Return (consumer, skill, action) rows; never replace a foreign entry."""
    repo = (repo or config.REPO_ROOT).resolve()
    home = home or Path.home()
    repo_sources = {p.parent.name: p.parent for p in sorted((repo / "skills").glob("*/SKILL.md"))}
    external, refused = _external(repo, set(repo_sources))
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
        # ~/.claude/skills is where external skills come from: only the repo's go there.
        sources = dict(repo_sources)
        if consumer == ".codex":
            sources.update(external)
            if not unlink:
                rows.extend((consumer, name, why) for name, why in refused)
        names = sorted(set(sources) | ({p.name for p in destination.iterdir()}
                                     if unlink and destination.is_dir() else set()))
        for name in names:
            target = destination / name
            try:
                if unlink:
                    if _ours(target, consumer, repo, external):
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
