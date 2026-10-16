"""Mutation evidence for the Codex permission profile (CG phase 2,
2026-10-16; plan 2026-10-15 §5.2's named mutants, then more).

Each mutant breaks one guard in a temp copy of the package and runs the
named checks against the copy. Every mutant must make a check fail; one that
survives is a guard no test holds. Free: fake app-server peers, temp HOME.

    python tests/v2/codex_profile_mutants.py          # all mutants
    python tests/v2/codex_profile_mutants.py 3 7      # by index
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_git_mutants import run  # noqa: E402  (same runner: temp copy, one mutation)

CFG = "jarvis/v2/providers/codex_config.py"
CODEX = "jarvis/v2/providers/codex.py"
CONFIG = "jarvis/config.py"
CHAT = "jarvis/v2/chat_git.py"


def pc(*names):
    return ["tests/v2/codex_provider_check.py", *(f"Checks.{n}" for n in names)]


PROFILE = pc("test_profile_denies_every_credential_path")
TAMPER = pc("test_each_start_up_check_refuses_when_tampered")
NEST = pc("test_denies_never_nest_and_globs_never_reach_into_a_denied_folder")
ONLY = pc("test_only_accept_or_decline_is_ever_sent")
NET = pc("test_a_network_approval_asks_the_owner_every_time_with_no_always")
CLOSED = pc("test_owner_dot_folders_are_closed_by_default")
WIN = pc("test_windows_profiles_are_closed_too")
KEYS = pc("test_loose_key_files_are_closed_as_a_class")
CHURN = pc("test_owner_home_churn_after_prepare_never_refuses_the_session")
MIRROR = pc("test_a_session_inside_a_denied_folder_or_a_writable_mirror_is_refused")

# (name, file, old, new, check)
MUTANTS = [
    # The plan's (§5.2) and the brief's named ones.
    # (Dropping one dot-entry from the explicit lists is masked by the
    # default-closed home; the closure's own mutants below hold that.)
    ("drop the credential folders", CFG,
     "    found = [*config.credential_dirs(), *_codex_home_entries(),",
     "    found = [*_codex_home_entries(),", PROFILE),
    ("accept a receipt without the profile", CFG,
     '    if result.get("activePermissionProfile") != {"id": PROFILE, "extends": PROFILE_EXTENDS}:',
     "    if False:", TAMPER),
    ("send acceptForSession", CODEX,
     '    return {"decision": APPROVAL_DECISIONS[0] if allow else APPROVAL_DECISIONS[1]}',
     '    return {"decision": "acceptForSession" if allow else APPROVAL_DECISIONS[1]}', ONLY),
    ("send acceptWithExecpolicyAmendment", CODEX,
     '    return {"decision": APPROVAL_DECISIONS[0] if allow else APPROVAL_DECISIONS[1]}',
     '    return {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["*"]}}'
     ' if allow else APPROVAL_DECISIONS[1]}', ONLY),
    ("feature missing is not refused", CFG,
     "    if not isinstance(features, dict) or features.get(FEATURE) is not True:",
     "    if not isinstance(features, dict):", TAMPER),
    ("feature not written", CFG,
     '    lines += ["[features]", f"{FEATURE} = true"]\n', "", pc("test_config_and_shapes")),
    ("turn-wide request_permissions accepted", CFG,
     "    if features.get(TURN_WIDE_FEATURE) not in (None, False):", "    if False:", TAMPER),
    ("effective network not checked", CFG,
     '    if (not isinstance(network, dict) or network.get("enabled") is not False',
     '    if (not isinstance(network, dict)', TAMPER),
    ("filesystem entries not compared", CFG,
     '    if profile.get("filesystem") != expected:', "    if False:", TAMPER),
    ("legacy sandbox mode accepted", CFG,
     '    if effective.get("sandbox_mode") is not None or effective.get("sandbox_workspace_write") is not None:',
     "    if False:", TAMPER),
    ("receipt network not checked", CFG,
     '            or legacy.get("networkAccess", True) is not False', "", TAMPER),
    ("thread/start sends a sandbox mode again", CODEX,
     '                params = {"cwd": brief.cwd, "modelProvider": "openai",',
     '                params = {"cwd": brief.cwd, "modelProvider": "openai", "sandbox": "workspace-write",',
     pc("test_thread_start_and_resume_send_no_sandbox_mode")),
    ("a permissions overlay not described or judged", CODEX,
     '    if _content(p.get("additionalPermissions")):\n        words, more = _describe_permissions(',
     '    if False:\n        words, more = _describe_permissions(', NET),
    ("permissions requests granted turn-wide", CODEX,
     '            self._reply(s, request, {"permissions": {}, "scope": "turn"})',
     '            self._reply(s, request, {"permissions": {"network": {"enabled": True}}, "scope": "turn"})',
     pc("test_permissions_and_unknown_fail_closed")),
    # Measured 0.161 hazards (bwrap fails every command).
    ("nested denies kept", CFG,
     "    return [p for p in resolved if not any(other in p.parents for other in resolved)]",
     "    return resolved", NEST),
    (".env globs reach into denied folders", CFG,
     "        if not blocked:\n            out.append(f\"{base}/{ENV_GLOB}\")",
     "        if True:\n            out.append(f\"{base}/{ENV_GLOB}\")", NEST),
    ("~/.codex denied whole (its own binary with it)", CFG,
     "*_codex_home_entries(), owner", "*_codex_homes(), owner", NEST),
    ("the binary check off", CFG,
     "    if any(d == path or d in path.parents for d in denies):",
     "    if False:", NEST),
    ("symlinked credential folders denied as links", CFG,
     "        return Path(path).expanduser().resolve()", "        return Path(path).expanduser()",
     pc("test_symlinked_credential_folders_are_denied_where_they_lead")),
    # Further guards.
    ("no .env globs", CFG,
     '    table.update((glob, "deny") for glob in globs)\n', "", PROFILE),
    ("dot-files not closed, only dot-folders", CFG,
     '            if (dots_only and not entry.name.startswith(".") and not (extra and extra(entry.name))',
     '            if (not entry.is_dir() and not (keys and key_like(entry.name, broad=True))) or (dots_only and not entry.name.startswith(".") and not (extra and extra(entry.name))', CLOSED),
    ("V2_DATA_DIR readable", CFG,
     "             config.V2_DATA_DIR, config.SESSIONS_DIR,", "             config.SESSIONS_DIR,", PROFILE),
    ("commands keep the denied private HOME", CFG,
     "        lines.append(f\"set = {_value({'HOME': str(sandbox_home(home))})}\")\n",
     "        pass\n", PROFILE),
    ("a write root over the mirrors accepted", CFG,
     "        if _overlaps(write, mirrors):", "        if False:", MIRROR),
    ("a session inside a denied folder accepted", CFG,
     "    if any(deny == cwd or deny in cwd.parents for deny in denies):", "    if False:", MIRROR),
    ("the mirrors back under V2_DATA_DIR", CONFIG,
     'Path(V2_DATA_DIR).parent / "chat-git-mirrors"', 'Path(V2_DATA_DIR) / "chat-git"',
     pc("test_profile_denies_every_credential_path", "test_a_session_inside_a_denied_folder_or_a_writable_mirror_is_refused")),
    ("mirrors allowed inside the chat worktrees", CHAT,
     "        if path == other or other in path.parents or path in other.parents:", "        if False:",
     ["tests/v2/chat_git_check.py", "mirror_root_checks"]),
    ("the skills folder not mounted back (skills unreadable)", CFG,
     "        roots = roots + [str(sandbox_home(home)), str(session_skills(home))]",
     "        roots = roots + [str(sandbox_home(home))]", PROFILE),
    ("the old whole-folder skills link kept", CFG,
     "    if folder.is_symlink():\n        folder.unlink()\n", "    if folder.is_symlink():\n        return\n",
     pc("test_session_skills_replace_the_old_whole_folder_link")),
    ("a planted skill link kept", CFG,
     "            if entry.is_symlink() and wanted.get(entry.name) != Path(os.readlink(entry.path)):",
     "            if entry.is_symlink() and entry.name not in wanted:",
     pc("test_session_skills_replace_the_old_whole_folder_link")),
    # Review of #224: default-closed dot-folders (HIGH), the written table (MED).
    ("dot-folders not closed", CFG, "            out.append(target)\n", "            pass\n", CLOSED),
    ("kept folders not closed in turn", CFG,
     "                if rel in spec and entry.is_dir(follow_symlinks=False):\n"
     "                    close(base, path, spec[rel], False, spec, strict=strict)",
     "                if False:\n                    close(base, path, spec[rel], False, spec, strict=strict)",
     CLOSED),
    ("a needed path's folder denied", CFG,
     "            if any(n == path or n == target or path in n.parents or target in n.parents\n"
     "                   for n in needed):",
     "            if False:", CLOSED),
    ("uv's credentials readable", CFG,
     '".local/share/com.vercel.cli", ".local/share/uv/credentials",',
     '".local/share/com.vercel.cli",', PROFILE),
    ("missing paths denied too", CFG,
     "        if path not in resolved and os.path.lexists(path) and not _GLOB_SYNTAX.search(str(path)):",
     "        if path not in resolved and not _GLOB_SYNTAX.search(str(path)):", CLOSED),
    ("a blocked base's own .env files not denied", CFG,
     "                        files.append(Path(entry.path))", "                        pass", CLOSED),
    ("the start-up check rescans", CFG,
     "    expected = written(home) if home is not None else filesystem(brief, None)",
     "    expected = filesystem(brief, home)", CHURN),
    # Review of #224 (delta): Windows profiles.
    ("Windows profiles not closed", CFG,
     "    for profile in windows_profiles():", "    for profile in []:", WIN),
    ("AppData readable", CFG,
     '    return folded == "appdata" or folded.startswith("ntuser")',
     '    return folded.startswith("ntuser")', WIN),
    ("a profile's dot-entries readable", CFG,
     '            if (dots_only and not entry.name.startswith(".") and not (extra and extra(entry.name))',
     '            if (dots_only and not (extra and extra(entry.name)) and (extra or not entry.name.startswith("."))', WIN),
    ("an unlistable profile refuses the session", CFG,
     '        close(profile, profile, (), True, {}, extra=_windows_extra, strict=False, keys=True)',
     '        close(profile, profile, (), True, {}, extra=_windows_extra, strict=True, keys=True)', WIN),
    ("a repo inside AppData denied, not closed around", CFG,
     "                if entry.is_dir(follow_symlinks=False) and any(path in n.parents for n in needed):",
     "                if False:", WIN),
    ("literal denies with glob syntax kept", CFG,
     "os.path.lexists(path) and not _GLOB_SYNTAX.search(str(path)):", "os.path.lexists(path):",
     pc("test_a_path_with_glob_syntax_is_never_a_literal_deny")),
    ("npm's debug logs readable", CFG, '    ".npm": ("_cacache", "_npx"),\n', "", CLOSED),
    # Decision on #224's residual: loose key files as a class.
    ("loose key files at the top of a home readable", CFG,
     "                    and not (keys and key_like(entry.name, broad=True) and _is_file(entry))):",
     "                    and not False):", KEYS),
    ("key files at a repository's top readable", CFG,
     "             *_root_key_files(brief),\n", "", KEYS),
    ("the narrow key patterns gone", CFG,
     "    return bool(_KEY_NARROW.fullmatch(name) or (broad and _KEY_BROAD.fullmatch(name)))",
     "    return bool(broad and _KEY_BROAD.fullmatch(name))", KEYS),
    ("the broad set applied at repository tops", CFG,
     "                        if key_like(e.name, broad=False) and _is_file(e)]",
     "                        if key_like(e.name, broad=True) and _is_file(e)]", KEYS),
    ("examples and public keys treated as secrets", CFG,
     "    if _KEY_NOT.fullmatch(name):\n        return False\n", "", KEYS),
    ("the feature pin moved without the probe", CFG,
     'FEATURE_VERIFIED = "0.161.0"', 'FEATURE_VERIFIED = "0.162.0"',
     pc("test_the_feature_is_pinned_to_the_verified_codex")),
]


def main(argv: list[str]) -> int:
    picks = [int(a) for a in argv if a.isdigit()] or list(range(len(MUTANTS)))
    survived = []
    for index in picks:
        killed, detail = run(index, MUTANTS[index], sys.executable)
        print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d} {MUTANTS[index][0]}: {detail}", flush=True)
        if not killed:
            survived.append(MUTANTS[index][0])
    print()
    if survived:
        print(f"{len(survived)} of {len(picks)} mutants survived")
        return 1
    print(f"all {len(picks)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
