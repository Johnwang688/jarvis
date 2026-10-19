# Duplicate-helper cleanup (2026-10-18)

Owner: "you may … clean up the duplicate helpers" (2026-10-18). Source: the five
scout reports in `~/.cache/jarvis-lead/workflows/dup-scout-*.md` and
`docs/plans/2026-10-18-security-fix-specs.md` ("Found but not filed").

**Rule for every card: behaviour does not change.** A cleanup replaces a copy only
where the copy and the shared helper behave identically for every caller. A copy
that differs is left in place and listed in the PR body, with the difference. No
assertion is removed or weakened. Every touched test file reports the same pass
count before and after. Each PR body has a table: file, copies removed, "was
identical?" (yes or the difference that kept it).

Not in scope here. Each needs an owner call; see "Questions" at the end.
- Copies inside SELF_PROTECTED files: `rules._tokens` / `terminal_guard._tokens`,
  the `local=` predicate in `browser/drive.py` / `look_capture.py`,
  `discord_approvals._describe` / `face/approvals._for_display`, the
  `editor_policy` / `editor_nav` / `act_rules` `_under` copies, `gitops._git` /
  `chat_git._run`.
- The HUD's two `isModifierKey` sets (`lib/frameguard.ts:49`,
  `lib/remote.ts:303`): they disagree, and frameguard enforces "no frame takes
  the keyboard".
- `protected_state._under` (its equality semantics differ on purpose).
- Behaviour-changing tidy-ups: env-number readers that crash on bad input,
  `diffreview.costLine` token formatting, `check()`/`ok()` print styles.

| Card | Scope | Tier (impl / review) |
|---|---|---|
| CL-1a | `free_port` + `wait_for` in `tests/*.py`, `tests/face/` | Haiku / lead |
| CL-1b | the same in `tests/v2/` | Haiku / lead |
| CL-2 | HUD: path basename/dirname, guarded storage read/write | Sonnet / lead |
| CL-3 | one Host-header check for the seven v2 listeners | Sonnet / lead (Opus) |
| CL-4 | small exact Python copies | Haiku / lead |

## CL-1: shared test helpers (`tests/checklib.py`, written by the lead first)

`free_port() -> int` and `wait_for(predicate, what, timeout=30.0, interval=0.02)`,
which raises `AssertionError("timed out waiting for <what>")`. Replace only those
copies whose behaviour is identical: same return or raise, same default timeout
(a call site may pass its own timeout explicitly, to keep its old value), and a
predicate re-checked until the deadline. Leave copies that return a bool (e.g.
`workflows_check._wait`), copies with a final grace check, and
`daemon_check.dead_port`. Import style: `tests/v2` files already put the repo
root on `sys.path`, so they use `from tests import checklib`, like
`from tests import loadclock`. Top-level `tests/*.py` and `tests/face/*.py` use
whatever import already works for that file; check by running it. Never change
what a test asserts.

## CL-2: HUD helpers

New `hud/src/lib/paths.ts`: `basename(path)` and `dirname(path)`, for `/`
paths only. Replace only those inline copies whose result is identical
(`EditorTabs.tsx:20`, `lib/fileIcons.ts:136`, `lib/scm.ts:102`,
`lib/monaco.ts:222`, `SearchView.tsx:176`, `FileTab.tsx:178` parentOf). The
copies differ on empty and trailing-slash input. Either give the helper an
explicit, documented fallback parameter, or leave a differing site alone.
`speech/prose.ts pathBasename` is prose parsing and stays.

New `hud/src/lib/storage.ts`: `readKey(key, storage?)` and
`writeKey(key, value, storage?)`. Each wraps `getItem`/`setItem` in try/catch
(returning null on read failure, ignoring write failure) and keeps the
injected-`Storage` parameter that the tests use. Each call site keeps its own
parse rule (`=== "1"`, `!== "off"`, JSON …). Sites: `settings.ts`, `speaker.ts`,
`BootOverlay.tsx` (sessionStorage), `sparkle.ts`, `listen/pref.ts`,
`speech/index.ts`, `voiceTiming.ts`, `compose.ts`, `wake/wakeSettings.ts`,
`workspace.ts`. The one deliberate behaviour change is that a throwing storage
no longer crashes the injected-storage sites. Add a vitest per helper, plus one
test showing that the injected sites survive a throwing storage.

Gates: `npm run build`, `npx vitest run`, and the full `hud_v2_check` once at the
end.

## CL-3: one Host-header check

New `config.own_host_header(value, port)`: True only when `value` equals
`127.0.0.1:{port}` or `localhost:{port}`, compared exactly as every current copy
does. It is a DNS-rebinding guard. Replace the seven inline copies:
`v2/browser/routes.py:91`, `v2/hud_api.py:1410`, `v2/terminals.py:1937`,
`v2/vex.py:2315`, `v2/devservers.py:751`, `v2/projects.py:165`,
`v2/daemon.py:2781`. Read each copy first, because some take a different port
(`face_port`, `hud=`); keep that. Behaviour is byte-identical: no new spelling
(`[::1]`, a trailing dot, case) is admitted.

Tests: a unit table for the helper (both spellings, other ports, `evil.example`,
`127.0.0.1.nip.io`, uppercase, a trailing dot, a missing port, `[::1]:port`; only
the two exact spellings pass). Also a mutation proving every site goes through
the helper: make the helper return True and the existing per-listener refusal
tests (`owner_read_check`, `terminal_check`, `browser_routes_check`,
`vex_check`, `devservers_check`, `projects`…) must fail. Report which do.

## CL-4: small exact Python copies

- `jarvis/daemon.py:57 face_is_serving` and `jarvis/face/server.py:1466
  _face_already_running` are identical: one calls the other.
- `jarvis/v2/browser/stream.py:432 _clean` and `jarvis/v2/browser/host.py:623
  _clean` are identical wrappers over `clean_line`: keep one and import it in the
  other.
- `jarvis/v2/speech_voices.py:86 _write`: a fixed `<file>.<pid>.tmp` name with no
  cleanup on failure. Use `stores._write_bytes`, but only if the resulting mode
  and error type are compatible with its callers; otherwise report.

## Questions for the owner (not blocking)

1. Consolidate the copies inside SELF_PROTECTED files (list above)? Each is
   identical today, but editing those files is your call.
2. HUD `isModifierKey`: merge the two key sets? Which set wins for frameguard?
   (CapsLock and Fn are in frameguard only; Super, Hyper and FnLock in remote only.)
