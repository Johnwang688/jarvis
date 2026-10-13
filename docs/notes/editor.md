# Editor — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 2955–3301

<!-- claude-source:002955-003301:begin -->
**The editor's missing half, ED-1 (2026-10-10; plan
`docs/plans/2026-10-09-hud-editor-plan.md` §2.2, decisions file beside it
wins: E-1 no intellisense, E-10).** Monaco is **0.57.0, pinned exactly**, on
its supported entry points: `lib/monacoBundle.ts` is `monaco-editor/editor`
plus a **curated** `features/*/register` list (find, folding, comment,
multi-cursor, brackets, word suggestions, sticky scroll, go to line, F1, the
context menu …; nothing that needs a language server) and the Monarch
definitions, one lazy chunk (3.6 MB) behind `loadMonaco()`. **No
language-service worker exists** — no TS, JSON, CSS or HTML worker is built
or loaded (`getWorker` refuses any label but `editorWorkerService`), so a
`.json` file gets a small Monarch definition of ours and `.toml` borrows
INI's; `.h` is C++ (VEX headers); a shebang names shell or Python. **The
language comes from the registry by the model's URI** (`file:///<project>/<path>`,
a per-editor query until ED-2's buffers share one model per absolute path);
`languageFor` is gone. Links open only http/https, `noopener`
(`registerLinkOpener` answers every other scheme "handled" — Monaco's own
fallback navigates the window). ED-1's interim moves of vs-dark's amber and
red off those hues are gone: ED-3's scheme (below) replaced them. Diff models live under
`jarvis-diff:` and are disposed with their view (two leaked per file).
**Under a card every editor is read-only** (`setEditorsBlocked` from App,
above the error boundary, beside the terminals' hold) and unfocused (the
card's own focus grab, WP-A), and `.editorhost` is `isolation: isolate`, so
Monaco's thousand-z overlays (its context menu is 2575) stack inside it,
under `#authveil` — they also close when the card takes focus, so the check
for the isolation lays a layer at the veil's z-index over an open menu.
**Monaco's context view under the HUD's zoom**: it lays the right-click menu
(and its buttons' tooltips) out in viewport pixels and writes them as CSS
lengths, which `#root`'s zoom scales again — at 160% the menu opened off the
window. `monacoBundle.ts` patches `ContextView.prototype.doLayout` (Monaco
patches its own find widget the same way) to **lay the view out again in the
pixels it is drawn in** — its drawn size, the anchor's drawn rect, below or
above and left or right by what fits (Monaco's preference first), then kept
on screen (`lib/zoomfix.ts` `placePopup`), written back divided by the zoom it
sits under, across the shadow root. Dividing Monaco's own numbers was not
enough (review of PR #43): it flips by its *unzoomed* size, so at 70% a
right-click near the bottom put the menu's bottom 45 px above the pointer.
**Keys: the one interception place is still
`useLayout`'s capture handler.** It feeds every keydown to
`lib/editorkeys.ts`'s `chordStep`, so **the second key of a Ctrl+K chord
typed in Monaco is Monaco's, whatever HUD key it is** (Ctrl+K Ctrl+B sets the
selection anchor, Ctrl+K Ctrl+0 folds all, as in VS Code) and still kept from
Chrome; a lone Ctrl+B folds the sidebar from Monaco too (E-10), a lone zoom
key is the HUD's. **The tracker follows Monaco's own chord state**: it counts
only keys typed in an editor (Monaco dispatches only those, so a key in the
chat box between Ctrl+K and Ctrl+B ends neither chord), leaves the chord after
5 s or when the window loses focus, and ignores repeat and composition.
**Ctrl+S never reaches Chrome**: in an editor it saves **that editor's**
buffer — an `addAction` keybinding, scoped by Monaco to its `editorId`,
disposed with the editor and dead once it is (no editor has focus under a
card, so it cannot run); in a terminal it is the shell's. **Never
`addCommand`** (review of PR #43, the HIGH): in 0.57 it is a global
keybinding with no condition, so the last editor created owned Ctrl+S for
every editor and outlived itself — Ctrl+S in a Diff wrote the file blank (a
disposed editor's text is ""), and Ctrl+S in one pane saved another pane's
buffer. The other global registrations (languages, the link opener, the
theme) are made once in `loadMonaco` and hold no editor. **A save's answer
lands only on the buffer it was sent from** (a generation bumped per read): a
save still on its way when another file was opened used to put the old
file's record back over the new one's text. **An unsaved edit is never
dropped without a word**: opening another file or Reload over one asks
(`file-discard-ask`, Keep editing / Discard). **A File pane picks its own
project** (decisions E-13, built here): its header's `Project ▾`
(`pane-N-folder`) lists every project plus Follow chat and replaces the File
pane's "in: … · follow chat" chip (a Preview pane keeps the chip); it re-pins
that pane only, refuses with the refusal chip while the pane holds an unsaved
edit (the select snaps back), and is disabled under a card. **The dirty flag
the guards read is set in the keystroke's own change event** (`markDirty`
writes App's ref at once; the state follows for drawing): read from state a
frame behind, a project picked in the same breath as a keystroke remounted the
pane and dropped the edit. Only project
roots are offered — a folder becomes reachable by becoming a project, so
`folders.py` stays the one gate. EditContext stays on (headless
Chromium types into it). Space in Monaco **still** starts push-to-talk and
the space is lost — confirmed headless; it is the Ctrl+D push-to-talk PR's,
not ED-1's (the owner's decision, 2026-10-10). Test hooks (read-only):
`__hud.monacoModels()`, `monacoEditors()`, `monacoLanguage(path, text)`; the
editor host carries `data-language` (set once Monaco holds the file — what
`_edit` waits on now) and `data-readonly`. Free checks: `editorkeys.test.ts`,
`monaco.test.ts`, `zoomfix.test.ts`, `hud_v2_check`'s file and diff sections (tokens, find,
suggest, Ctrl+S in and out of an editor, links, Ctrl+B and the chords, the
discard ask, the diff leak, no request off loopback by a catch-all route, no
language worker requested or built, a `file:` link opening nothing) and
`hud_v2_layout_check`'s `_editor_card_checks` (a card over an open context
menu or suggest list, at 100% and 160%), `_save_scope_checks` (Ctrl+S in a
Diff after the file pane's editor went away, two editors alive, one switched
away; an editor created under a card is read-only), `_late_save_checks` (the
mock's `put_delays` holds a write's answer), `_chord_checks`,
`_menu_place_checks` (70% near the bottom, 160% near the right, a window too
short for either side) and the E-13 checks in `_workspace_checks` (a project
picked in the same breath as a keystroke). Every one that guards a behaviour
fails on origin/main, on the reviewed head (18381b9) or with its fix reverted
(PR body). Monaco's `getValue()` per keystroke (the change event hands the
whole text to React) was left for ED-2's buffers, which removed it (below).

**Buffers, tabs and the guarded save, ED-2 (2026-10-11; plan
`docs/plans/2026-10-09-hud-editor-plan.md` §2.3–§2.4, decisions E-7/E-8/E-13;
the owner's `.env*` rule; contract in `docs/hud-api.md`).** **A file's text
lives in the buffer registry, outside React** (`hud/src/state/buffers.ts`,
rules in `lib/buffers.ts`): **one Monaco model per file on disk, keyed by
`abs`** (where the read route says it resolves), so one file reached from two
projects is one buffer and the same file in two panes shares the text, the
dirty dot and the undo stack (each pane keeps its view state, in memory). A
buffer saves through the project it was read through. **Unmounting a view
never loses an edit**: a pane holds a reference per tab (`<pane>|<path>`); a
model goes when no tab holds it **and it is clean** — a dirty one nobody holds
stays (and `beforeunload` asks) until it is reopened or the owner discards it
(`releasePane(n, {discard})`, the project-gone "discard edit"). **The text is
read once, at save** (`getValue()` never per keystroke). **Dirty is
`getAlternativeVersionId() !== saved`**, said from the edit's own change event
and only when it moves, so App's guards (`dirtyNow(pane)` =
`buffers.paneLoses`, `mayShow`, the E-13 picker, `unpinProject`'s keep, ×'s
question and `allocate`) read
it in the keystroke's breath (#43's M1), and undo back to the saved text reads
clean. **A pane "holds an unsaved edit" only when it holds the last view of a
dirty buffer** (`buffers.paneLoses`; split-panes plan G-3): a view another
pane still shows may switch away, re-pin or close without asking. **Tabs** (`PaneSpec.editors {paths, active, preview}`, relative to the
pinned project, per-field fallback, only with a pin; `pinPane`/`unpinProject`
drop them; `setEditors` pins and sets in one step; a WP-G2 `filePath` with no
tabs reads as one): a single click opens an italic preview tab the next one
replaces, a double click or an edit pins it (a narrow pane opens every file
pinned and folds its tree at once), middle-click or × closes, the last
tab on an unsaved edit asks Save / Don't save / Cancel. Past 40 tabs the
oldest **clean** one closes; with every other tab dirty nothing opens and the
pane says why (a dirty tab is never closed unasked). Reload over an edit
still asks; opening another file just opens a tab. **One editor per pane**
(`PaneEditor`), its model swapped, created at the first buffer and kept.
**Saving is by content**: `expected_etag` (null: must not exist), 409 with the
disk's etag, never a force; `expected_mtime` is accepted one more release.
**The poll** (`buffers.poll`, App, every 2 s while visible and on focus):
clean + moved → reload in place (`minimalEdit`, one undo stop each side, EOL
followed), toast naming the writer (a `tool_started` of a **write** tool only
— Write/Edit/MultiEdit/NotebookEdit, `apply_patch`, `write_file`/`edit_file`
— through `writtenPaths` → `noteTouch`, **absolute paths only**, a relative
one resolved from the thread's `cwd`, never matched by its tail); dirty +
moved or a 409 → the amber `ConflictBar` (Compare —
`CompareEditor`, the disk read-only left via a `jarvis-disk:` model, the
buffer itself right, `useInlineViewWhenSpaceIsLimited: false`; Keep mine
against the shown etag; Take theirs asks once, undoable); gone → Save to
recreate / Close. **`jarvis/v2/editor_policy.py` is the one classifier**
(gate → withheld → safety → env → binary → text; see `docs/hud-api.md`), and
**every editor file route is owner-only, reads included** (E-7) — so tests
that read a file go through the HUD listener with the HUD's Origin
(`hud_backend_check.owner`). **Env files**: kind `env`, a lock "Jarvis can't
read this file", Add to chat disabled (`jarvisMayRead`), model language
`dotenv` (word suggestions draw only on same-language models, so a secret is
never offered in a file Jarvis can read), and the text in no request but its
own save, never localStorage or `__hud.buffers()`. **Add to chat** exists in
its smallest form (selection or line → `path:Lx-y` + fence into the selected
chat's box, unsent; W-6) so the env rule has something to disable; ED-7
extends it, and anything new that carries a buffer's text toward Jarvis must
ask `jarvisMayRead` first. Polling mock: `mock.disk_write`/`disk_delete`
write behind the editor; the mock's `files/stat` and etag 409s follow the
daemon's. The headless checks type no spaces (they were written while
Space was still push-to-talk; #40 moved it to Ctrl+D). Test hook:
`__hud.buffers()` (no text).
Free checks: `lib/buffers.test.ts`, `state/buffers.test.ts`,
`workspace.test.ts` (tabs), `tests/v2/editor_files_check.py` (the name rule,
the kinds, etag/409/stat, owner-only, env content kept off the bus, logs,
API listener, diffs, preview and attachments; real Chromium for the owner's
own page) and `tests/face/hud_v2_editor_check.py` (run by `hud_v2_check` on a
`MockDaemon(0)` of its own, or alone, under `hud_v2_check.guard` — no CSP
violation).

**ED-2's review round (2026-10-11, PR #49; each check failing on f869543).**
The rules the two reviews left, in one place:
- **An env file is never demoted** (MED1): `editor_policy.classify_bytes`
  keeps a non-UTF-8 env file `env` (UTF-16LE from PowerShell 5.1, Latin-1)
  with `Verdict.writable=False`; every read carries **`editable`**, and the
  HUD's editability is `editableOf(kind, editable)` — the read's word and an
  editable kind, both, never the kind alone — so the lock, closed Add to chat
  and `dotenv` stay while the file is read-only (`editor-readonly-reason` too).
- **The gate's readable exemption is the editor's own, and only for a gate
  path that resolves to itself** (LOW2, `Policy._readable_gate`): not a
  symlink anywhere on its way, not a protected/env name or login file, not in
  a credential folder other than `~/.config/jarvis`. `hud_api.protected` (an
  `@path`, the preview origin, a diff) asks `withheld_anywhere`, which has no
  exemption — the allowlist is no longer attachable by `@path`.
  `files._protected_state_paths()` is the unresolved list it reads.
- **Withheld widened** (LOW3): `~/.docker/config.json` (and
  `$DOCKER_CONFIG`), `~/.kube/config` (and `$KUBECONFIG`), `~/.npmrc` (and
  `$NPM_CONFIG_USERCONFIG`), `~/.pypirc` as login files; v2's session
  carriers (`V2_DATA_DIR/claude`, `V2_DATA_DIR/codex`) as credential folders;
  a dropped file named like a store (`editor_policy.credential_name`) is never
  attached. **The preview origin refuses any path with a hidden component
  inside the project except `.well-known`, and anything under Jarvis's config
  and data folders** (`hud_api._not_a_page`, which asks
  `editor_policy.hidden_component` and `editor_policy.jarvis_dirs`): it
  answers any loopback client, `fetch_page` included.
- The atomic write's temp is `<name>.<x>.tmp` (an env file's temp is an env
  file by name, LOW4); a save's unexpected `OSError` is 500 and a
  `PermissionError` 403, never the dispatcher's 409 (the conflict bar); a tree
  row is `env` only for a file; `jarvis/v2/editor_policy.py` is SELF_PROTECTED, and so, since
  the owner's answer (2026-10-12), are `editor_git.py` and `editor_nav.py`.
- **`Buffers.releasePane` touches only buffers this pane held** (H1): × on any
  pane used to discard every edit no tab held. **A project going away asks
  the panes on it together** (H2, `panesLose`/`panesHolding`): two panes
  sharing an edit each read "another pane holds it", both unpinned and the
  edit lost every tab; now each one holding it stays pinned with
  `pane-N-gone`, and FileTab stays keyed by the gone project (`goneHeld`).
- Compare re-reads once per new conflict etag (M2, `rereadFor`; it looped at
  ~340 GET/s while the disk disagreed with the poll, unbounded when hidden).
  Loads dedupe by `abs` after the read (L1, `making`), and `makeModel` never
  disposes a model at its URI — a second goes beside it under a query. An
  unknown kind is `unknown`: read-only, closed to Jarvis (L2). The crash
  prompt has **Save N unsaved files** (`hud-crashed-save`, `saveAll`; disabled
  and inert under a card, L3). A reload in place says nothing while it runs
  (`applying`, L4 — it used to read dirty for a moment and pin a preview
  tab). A save's late answer lands only on its own buffer; a 409 whose etag
  is not one asks the poll route and, still unknown, tells the owner; one
  write at a time per buffer (a double Ctrl+S sends one PUT).
- **A Ctrl+S during a save is never dropped** (delta review, LOW-2; the
  owner's VEX projects live on `/mnt/c`, where writes are slow): it waits for
  the first, then **exactly one** follow-up save runs against the etag *that
  answer returned* — further presses join it, a first 409 or error runs none
  (the bar), and nothing new since the first means no second PUT. The tab's
  dot is a hollow ring and the status says **saving…** (`editor-saving`,
  `data-saving`) until the last answers; Save stays pressable meanwhile. A
  poll whose stat crossed a save (sent while saving, or the etag moved under
  it) is ignored — it read the disk before the write landed. The preview
  origin refuses a hidden path in its own words, "hidden path: not served on
  the preview origin" (NIT-1); a credential and Jarvis's own folders stay
  "protected file".
Checks: `editor_files_check`'s `Review*` classes, the `review of PR #49`
blocks in `state/buffers.test.ts` and `lib/buffers.test.ts`, and the
`_rv49_*` sections of `hud_v2_editor_check` (the mock serves `editable`,
`uneditable` and on-cue `stat_etags`).

**The editor's navigation, ED-7 (2026-10-11; plan §2.8, §2.10; contract in
`docs/hud-api.md`).** The owner replaces VS Code with this at the lab, so:
**quick open** (Ctrl+P), **the palette** (Ctrl+Shift+P, F1), **find in
files** (Ctrl+Shift+F), **explorer ops** (right-click: new file, new folder,
rename, delete; `+`/`+▸` in the tree column's head), **the status bar**
(`EditorStatus`: Ln/Col and selections, language, indentation, encoding, EOL,
saved/unsaved, and ED-2's `saving…` — `data-queued` when a second Ctrl+S waits:
`BufferView.queued`) and **Add selection to chat** extended (every selection,
from the palette and Monaco's context menu — `jarvis.addSelectionToChat`,
shown only under the `jarvisMayAddToChat` context key, i.e. `jarvisMayRead`).
- **Backend** (`jarvis/v2/editor_nav.py`, three routes in `hud_api.route`,
  **`owner_only` before the body is read**): `GET …/files` (`git ls-files`
  through `gitops._git`, else a walk; dotfiles in; never a credential folder,
  the gate's state, a withheld file, `SKIP_DIRS`, a symlinked folder or a
  symlink out; 50k/20 deep/5 s), `POST …/search` (refused on the Inbox; 2,000
  matches, 10 s; `.gitignore` respected unless `ignore: false`; hidden files
  searched) and `POST …/fs`. **No copy of any rule**: `editor_policy` judges,
  `hud_api.scope` places, `folders` names Windows names, `trash.Trash.put`
  deletes (a project-owned `Trash`, so Jarvis's retention purge never empties
  the owner's deletes). `_policy_for(root)` narrows the policy's credential
  folders to the ones that can matter under the root — a speed-up, not a
  second rule. A folder is renamed or deleted only if it holds nothing the
  editor refuses: absolute anchors (gate files, credential folders, login
  files, the HUD roots, Jarvis's SELF_PROTECTED) **and** a walk of ≤20,000
  entries that never follows a symlink or enters `SKIP_DIRS`. A rename never
  replaces; **an env file keeps an env name** (403 otherwise); a new env file
  is 0600. One fs op at a time on its own lock, not the daemon's (a Recycle
  Bin move takes PowerShell a while).
- **Find in files and `grep_files` share one core and one filter**
  (`tools/search.py`: `refused()`, `Hit`, `_rg_record`, `_python_hits`,
  `search_bounded`). `refused` = `is_protected` **or `is_env_credential`** —
  so `grep_files` now refuses `.env.production` and the rest on both backends
  (ENV-1's grep half). The route adds `editor_policy`'s withheld/gate/env on
  top (two layers: dropping either alone keeps parity; dropping both breaks
  it, and `editor_nav_check`'s parity test says so). `_rg_args`' new options
  are keyword-only with defaults that leave `grep_files`' argv unchanged
  (files_check asserts it).
- **Keys**: `shortcutFor` returns `quickOpen`/`palette`/`findInFiles`;
  `useLayout(blocked, onEditorKey)` takes them in **the one capture handler**,
  prevents the default (no print dialog) and **stops propagation** (Monaco's
  F1 never opens its own quick command); a Ctrl+K chord's second key stays
  Monaco's; in a terminal **Ctrl+P is readline's** (`terminalTakesKey`); under
  a card they are swallowed, and `navBusy` refuses too. Quick open and the
  palette are **`state.picker`** values, so push-to-talk refuses and the mic
  claims nothing while one is open; Space types a space; a layout effect
  closes both when a card arrives; FileTab closes its menu and prompt on
  `blocked`. Targets: the focused File pane, else a drawn File pane on the
  focused pane's project, else **`placeFile`** — VX-2's `openAt` rule, one
  function for both since the lead merge: a split beside the focused pane
  (focus stays put), else the focused pane only when it shows File or chat
  (`mayShow`), else a sentence ("No room to open …"). **A file never replaces
  a VEX view** (nor a terminal, Preview or Diff): with a VEX view focused,
  Ctrl+P opens the file beside it. Quick open and the palette **close when
  focus leaves them** (VS Code's way; `PickList`'s `onLeave`) — left open,
  the palette's veil covered the VEX view's Download button. Neither ever
  offers a download or a device entry (`hud_v2_vex_check`'s
  `_ed7_beside_checks`). App talks to a File pane by
  **`fileAsk`** (open at a line, search, palette file commands), acted on once
  per nonce and cleared after the commit.
- **Rename keeps the edit**: `Buffers.renamed(fromAbs, toAbs, kind)` moves the
  buffer (same model: text, dirty, undo), rewrites every hold's path relative
  to its own project and returns the moves; App's `onRenamed` renames the tabs
  of every pane on the project (`renameTabs`, prefix-aware for folders) and of
  any other pane the moves name; the model's language follows
  (`monaco.relanguage`). A delete leaves an unsaved edit in its tab (the poll
  says gone; Save recreates); the prompt says so first.
- **Find in files never prefills from an env buffer** (`dotenv` model) — its
  text goes in no request but its own save.
- **The review of PR #53**, the rules it left. **The owner's search pattern
  is never run by Python in the daemon** (HIGH 1: `(\w+\s?)+:` on one line
  held the GIL 27.8 s): `search_bounded` takes every match and highlight span
  from ripgrep's `--json` (byte offsets turned into UTF-16 columns), ripgrep
  validates the pattern (`\p{L}` works), no ripgrep means a regex is refused
  with a sentence and a literal is escaped text; a view's `key` makes its newer
  search cancel its older one, and at most `MAX_SEARCHES` (4) run at once
  (429). ripgrep's exit 2 with matches or a silent stderr is **`partial`**,
  never a 500 (MED 3); `grep_files` keeps its own behaviour, its argv and its
  fallback's walk order. **`Buffers.renamed` never takes a buffer's place**
  (MED 2): a dirty one at the new name stays and the renamed buffer stays put;
  a clean one gives way with its tabs; the explorer refuses first ("… has an
  unsaved edit open"). The fs route uses the **whole** classifier (a project's
  extra folders are roots; `_policy_for`'s narrowing is quick open's walk
  only), refuses `~` names, anything in `editor_policy.jarvis_dirs()`, and a
  rename or delete of any project's folder or an ancestor of one; Windows
  names are judged where the path resolves. **Every change is made through
  fds** (delta review): the project folder is opened (each component
  `O_NOFOLLOW`, `editor_nav._Root`) **before** anything is judged, and the
  folder `scope` judged must still be that fd's (device and inode) or 409 — a
  project folder swapped for a link between the check and the act changes
  nothing; below it each folder is opened `O_DIRECTORY | O_NOFOLLOW`,
  existence and kind are read through the fds, a rename is
  `trash.rename_noreplace` (`renameat2(RENAME_NOREPLACE)`, check-then-rename
  where the filesystem cannot, 9p), and a delete is **`Trash.put_from`**: the
  entry moved by (folder fd, name) into a trash whose every component is
  opened `O_NOFOLLOW` (a trash folder that is a link refuses, nothing moves),
  its `.trashinfo` written through the `info` fd. A second item under a
  protected or env name goes into `jarvis-deleted[.<n>]/<name>` (never `.env.2`);
  anything else is numbered as the spec does (`notes.txt.2`). `put` and its
  other callers are unchanged. What is left: a delete on a Windows drive goes to
  the Recycle Bin by path (PowerShell takes no fd), after the inode re-check.
  Hits keep at most `search.LINE_CAP` (1,000) characters of their line, cut in
  the core with their spans. `_project_roots` reads the safe list. A folder
  named `.env` is a folder; the delete warning names unsaved buffers from any
  project (matched by the project's `root_real`, #50's); a buffer the registry
  kept in place keeps its tabs' paths (`Renamed.kept`).
Free checks: `tests/v2/editor_nav_check.py` (39: parity on both
backends, listing, search bounds/gitignore/never-withheld, fs refusals,
trash, owner-only), `fuzzy.test.ts`, `commands.test.ts` (no download or
device entry, keys), the ED-7 blocks in `state/buffers.test.ts` and
`lib/buffers.test.ts`, and `tests/face/hud_v2_editor_nav_check.py` (82, a
`MockDaemon(0)` of its own; `ONLY=card` runs one section; run by
`hud_v2_check` after the editor section). Guard removals that bite: owner_only,
the classifier refusal, the folder walk, the anchors (an install root under
`node_modules`), rename-replaces, the env rename, unlink-for-trash, the
listing's filter, the route's two search filters together, and in the HUD the
card close, the key guards (both), FileTab's blocked effect.
Not done: replace in files (cut), a "dev servers" list, symbol search (cut),
Ctrl+PageUp/Down as keys (palette only), and a tab on the same file through
*another* project's pane that never mounted keeps its old path.

<!-- claude-source:002955-003301:end -->

## Original CLAUDE.md lines 3658–4278

<!-- claude-source:003658-004278:begin -->
**The tree's unsaved-changes dot and file icons (2026-10-11, the owner's ask
for the robotics lab: "like VS Code").** A dirty file's tree row wears a light
dot (`--text-2`, `DirtyDot`, "unsaved changes" with a tooltip), and so does a
collapsed folder holding one at any depth — an open folder never does, its
child does (`lib/dirtyMarks.ts`, `rowDot`). **Dirty is the buffer's**, so
every pane's tree on that project marks it, opened there or not, and a file
read through another project is found under this one's root by its `abs` —
**the root as it resolves**: every project the daemon answers with carries
`root_real` (`projects.resolved_root`, display only, refused if sent back),
and the tree looks under it (`treeRoot`), so a root reached through a
symlink still matches. On
a tab the dot stands where × is and hover or focus brings × back (theme.css;
× only goes transparent, so it never moves and stays clickable); **while a
save is out (ED-2) the same slot holds a hollow ring**, dirty or not, named
"saving", and hover still brings × back. **The tree
never redraws per keystroke**: `useDirtyMarks` subscribes with a string key
of the dirty paths (the registry speaks on clean↔dirty and on a save's
saving/queued changes, and the key ignores the latter), and
the tree's `Node` is memoized with a stable `onOpen`, so a chat box's typing
no longer redraws it either — `FileTab.test.ts` counts the tree's row renders
through twenty window redraws, twenty keystrokes and a held save (dropping
`memo(` or the stable `onOpen` fails it). Each file row has a 16px inline-SVG icon after
the twistie's room (`components/FileIcon.tsx`; the rule is
`lib/fileIcons.ts`: whole name, then the longest extension, case-insensitive;
`.env*` or `policy: "env"` the lock); a folder is its chevron alone. **Icons
are drawn in currentColor from six theme tokens** (`ICON_TOKENS`: the four
`--syn-*`, `--text-2`, `--text-muted`) — never a hex and never amber or red,
so VS Code's yellow `{}` and orange HTML are deliberately not copied. Free
checks: `fileIcons.test.ts` (mapping, the colour rule from theme.css's own
text, every icon rendering aria-hidden with no `<text>`), `dirtyMarks.test.ts`,
`DirtyMark.test.ts` (one redraw per transition, none per keystroke or for
another project's file), `FileTab.test.ts` (the memo), `hud_backend_check`'s
`test_projects_say_where_their_root_resolves` and
`tests/face/hud_v2_editor_marks_check.py` (a `MockDaemon(0)` of its own, after
the editor section of `hud_v2_check`; the saving ring, and a linked project
whose mock `root_real` is p1's folder — it fails with `treeRoot` ignoring it).

**The Graphite editor scheme, ED-3 (2026-10-11; plan §2.5, decisions E-4).**
One scheme, no picker, **no syntax colour amber or red**. `lib/editorTheme.ts`
is the table: rows keyed by **nvim-treesitter capture names** (`LEGEND` —
ED-5's semantic token types), Monarch's tokens styled through the same rows,
each naming a HUD token and a style. Most code is `--text`; keywords bold,
comments italic `--text-muted`, punctuation `--text-2`, and four new `:root`
tokens: `--syn-fn` #8ec5d6 (functions, tags, headings), `--syn-string`
#a6c79b, `--syn-const` #b9aade (numbers, constants, escapes), `--syn-type`
#a8b5c2 (types, modules, directives, keys) — quieter siblings of HUD hues, so
`--accent` and `--green` keep single meanings. **theme.css is the one place a
colour is written**: `cssPalette()` reads the tokens off `:root` when Monaco
loads and `themeData()` derives the hex, with `inherit: false` (nothing of
vs-dark's survives; a token no rule names is `--text`); a vitest fails on any
hex literal in editorTheme.ts's code. The chrome is pinned in the same file —
the plan's list (current line `--bg-2`, line numbers `--text-muted`, cursor
`--accent-hi`, selection and find in the accent, word highlight `--selected`,
bracket pairs in the three syntax hues, unicode box `--syn-const`, error
squiggles `--red`, warnings `--text-2`, diff tints, whitespace, sticky
scroll) **plus every colour Monaco 0.57 resolves warm anywhere in its
registry**; red only on a row marked `red: "error" | "removed"`. **A control
character is not a registry colour**: Monaco draws ESC, BEL or ^L on its own
`.mtkcontrol` box, white on dark red, `!important` — theme.css pins
`.monaco-editor .mtkcontrol` to `--text-2` on `--bg-3` (review of PR #45).
**A theme failure fails soft**: `themeOrFallback` logs the unreadable token by
name and Monaco loads in plain vs-dark (a rejected `loadMonaco` used to drop
every editor to its textarea, silently, until reload), and each refined
tokenizer is compiled in its factory first (`refinedOrOwn`, with Monaco's own
Monarch compiler) — a throw or a compile error leaves that language Monaco's
own tokenizer, logged. A
per-language rule is `<capture>.<lang>` (Monarch's postfix; ED-5 passes the
language as the token's last modifier — Monaco matches a semantic token's
type and modifiers joined by dots). **Monarch's C, C++ and Python are
refined** (`lib/monarchRefine.ts`): a word before `(` is `function`, a C++
word before `::` a `module`, and the builtins split into
`constant`/`type`/`function`/`variable.builtin` (Python's keyword list *was*
its builtins); the one identifier rule is found by its exact shape, and a
definition that has moved keeps Monaco's own tokenizer. **Both live in the
lazy chunk** (`monacoBundle.ts`'s `applyGraphite`, called by `loadMonaco`):
imported from `monaco.ts` they cost 11 KB of first paint and, because
Python's definition imports Monaco's API, split Monaco into a second chunk.
For ED-5: the plan's "bold on `void`" test contradicts its own table (`void`
is `@type.builtin`, `--syn-type`); the table wins. Left for ED-5's tree-sitter
(fidelity, not safety): Markdown bullets and link URLs, untyped `=`/`*`/`::`/
`->`, `#define SQ(x)` drawn as a function, Python's `class Foo(Base)` drawn
as a function, `match` drawn as a keyword, and escapes inside Python strings. Vitest reads theme.css
raw only because `test.css.include` names it (Vitest empties CSS, `?raw`
included). Free checks: `editorTheme.test.ts` (≥ 4.5:1 on `--bg-1` and
`--bg-2`, the warm band 340°–65° at s ≥ 0.35, strict hex, the CSS and the
table agree, every capture and Monarch token resolved through Monaco's own
`TokenTheme`, every warm Monaco default pinned), `monarchRefine.test.ts`
(Monaco's own Monarch compiler and lexer on the installed definitions), and
`tests/face/hud_v2_editor_theme_check.py` (a `MockDaemon(0)` of its own, run
by `hud_v2_check` after the browser section, or alone: a C++ and a Python
file's rendered tokens against the page's `:root` tokens, Monaco's `.mtkN`
colour map, every `--vscode-*` colour it publishes, bracket pairs, find in
the accent, control characters off Monaco's red box, and the approval card's
computed colours unchanged once Monaco has loaded). 30 of its first 44 checks
fail on b04d6e8, and the control-character pair without the theme.css rule;
the rest are setup or guards (find was already pinned by ED-1), each shown to
bite in a scratch copy.

**Tree-sitter colour, ED-5 (2026-10-11; plan §2.6, the ED-5 package in §3).**
C/C++ (`.c` and VEX's `.h` on the C++ grammar), Python, JavaScript,
TypeScript, TSX, JSON, Bash, CSS, HTML, YAML, TOML and Markdown (block and
inline) are coloured from a parse tree; everything else keeps Monarch.
**`web-tree-sitter` 0.27.0 and every grammar package are pinned exactly**;
Markdown's npm package ships no `.wasm`, so its v0.5.3 release files are
vendored in `hud/vendor/tree-sitter/` and **every shipped `.wasm` has its
SHA-256 in `manifest.json`** (vitest fails on a difference).
**nvim-treesitter's queries are vendored raw at f603a2f4** under
`hud/vendor/nvim-treesitter/` (Apache-2.0 `LICENSE`, `NOTICE`) and translated
into `hud/src/syntax/queries/` by `hud/scripts/vendor-queries.mjs` (dev-run:
`; inherits:` concatenated, `#lua-match?` to `#match?`, `#set! priority` a
sort key, untranslatable patterns dropped and listed in `dropped.json` —
only html's `#gsub!` today); **every bundled query is compiled against its
bundled grammar in vitest**, so grammar/query drift fails the build. The
code is `lib/syntax/`: `grammars` (pure data: Monaco language → grammar,
fence aliases), `assets` (the loader gate), `worker` + `engine` (parse,
incremental reparse, injections, budgets, tokens), `client` (the provider,
in the lazy Monaco chunk), `positions`, `flatten`, `legend`, `translate`,
`budgets`. **One syntax worker, made only through Monaco's `getWorker` by
its own label** (`SYNTAX_WORKER`; every language-service label is still
refused, E-1); Vite builds workers as ES modules now (`worker.format: "es"`).
Monaco gets a `DocumentRangeSemanticTokensProvider` per covered language and
`semanticHighlighting.enabled` on every editor; **Monarch stays underneath**,
so colour is never missing, only quieter. Tokens are ED-3's capture names
with the language as the last modifier (`keyword.import.cpp`), styled by the
same table. **web-tree-sitter 0.27's query ranges are in UTF-16 bytes**
(twice the code units): `positions.queryRange` converts, a test pins it.
Edits go to the worker as `{offset, length, text}` with the version,
coalesce for 30 ms and reparse incrementally — **300 random edit sequences
(astral characters, CRLF, tabs) colour exactly as a fresh parse**.
Injections are one level deep (a fence by its info string, Markdown inline,
HTML `<script>`/`<style>`), capped at 256 KB of injected text per document.
**Budgets fall back to Monarch, never to a broken file**: over 1 MiB of
text ("syntax: basic (large file)", never sent to the worker), a first parse
over 1 s ("slow parse"), two reparses cancelled at 250 ms running ("slow
edits"), a grammar that fails to load ("grammar failed"), **a worker that
errors or leaves a token request unanswered for 10 s** (`REPLY_TIMEOUT_MS`:
it is dead — terminated, every model on Monarch, "no worker", and the
provider answers empty rather than null, which would leave tree-sitter's
stale tokens drawn); each editor shows the reason in a bottom-right corner
(`data-testid="syntax-status"`) only while a covered file is on Monarch.
**Every `Query.matches` run is budgeted** (`matchMs`, 250 ms, through its
`progressCallback`; a cancelled run's partial matches are never colour): a
viewport chunk stops the answer at the lines before it, the whole-document
injections query leaves fences their host's colour (twice running, off for
that document), an injected range keeps its host's colour. **A fence's info
string is a lookup key, so lookups read own keys only** (`Object.hasOwn`):
` ```constructor ` or ` ```__proto__ ` loaded Object.prototype's names (review
of PR #54). **A grammar is code the worker runs, so
the HUD's policy gained `'wasm-unsafe-eval'`** — in `hud_api.HUD_POLICY`'s
`script-src`, the one header, WebAssembly only (`eval`, `new Function` and a
string `setTimeout` stay refused, and a response may narrow it away) — **and
the loader fetches only its map**: `assets.fetchShipped` is the one `fetch`
in `lib/syntax/`, it resolves the URL against the worker's origin and
refuses anything but the runtime's and the 13 grammars' Vite `?url`s —
another origin, another path, `data:`, `blob:` — before a request is made;
`Language.load` takes only its bytes; and Emscripten's `locateFile`
answers its own runtime's name with the gate's URL (it is never fetched:
`wasmBinary` holds the bytes) and **throws for any other name** (it used to
throw for every name, which Emscripten calls even when handed the bytes, so
the worker never started in a real browser — vitest could not see it).
Test hooks, read-only: `window.__hudSyntax.states()` (each model's grammar,
state, reason, `served` count and `opens` — times its text was sent to the
worker, 0 past the size budget: the worker's own size check would hide the
client's), `.probe(url)` (asks the gate about a URL,
never fetches) and `.loaded()` (grammars compiled), each answering within
10 s even with a dead worker. Sizes: first paint 613,246 B (+168),
`monacoBundle` 3,648,370 B (+5 KB), the worker 213 KB, ~9.9 MB of `.wasm`
loaded per language on first sight. ED-3's "bold on `void`" note stands:
`void` is `type.builtin`, `--syn-type`, not bold. Free checks: `lib/syntax/
*.test.ts` (167; Node, the pinned runtime and real grammars; `client.test.ts`
on a stand-in Monaco and a silent worker) and
`tests/face/hud_v2_syntax_check.py` (a `MockDaemon(0)` of its own, run by
`hud_v2_check` after the editor-scheme section, or alone: a C++ call in
`--syn-fn`, `void` `--syn-type`, a type name Monarch leaves plain drawn
`--syn-type`; a Python fence in Markdown coloured as Python; a 1.2 MB file
on Monarch with its corner; all 13 grammars compiled under the CSP, each
`.wasm` from `/assets` as `application/wasm` under the one policy; the
loader refusing URLs outside its map). **A CSP violation inside a worker
never reaches `watch_csp`'s document listener** — the suite also reads the
page's and the worker's console for one. Shown to bite in a scratch copy:
the policy without `'wasm-unsafe-eval'` (12 of 39 fail), the gate returning
any URL (5), and the size budget dropped (2). `hud_v2_editor_theme_check`
now reads spans only after the worker has answered for the file, and
accepts `"vex.h"` as one string and `@property` as `@` + `property`.
Folding from the tree is ED-6, below.

**Folding and sticky scroll from the tree, ED-6 (2026-10-12; plan §2.6
"Folding from the tree", the ED-6 package in §3).** nvim-treesitter's
`folds.scm` for every grammar ED-5 ships is vendored at the same pin
(f603a2f4; 15 files, the inline grammar has none) and built like the other
queries (`vendor-queries.mjs` now does three kinds; `translate.WORKER` gained
`#trim!`); **`vendor/nvim-treesitter/manifest.json` records every vendored
nvim file's size and SHA-256** with the commit, and vitest fails on a
difference or an unlisted file. `lib/syntax/folds.ts` (pure) turns a folds
query's `@fold` matches into Monaco ranges as Neovim's fold expression
reads them — a quantified capture (`(preproc_include)+`) is one fold, first
node to last; `#trim!` drops trailing blank lines (Markdown sections); a
node ending at column 0 ends a line earlier; two lines at least — plus **one
VS Code rule: the node's own closer stays in view** (a `}`, `]`, `)`, end
tag or JSX closing element at most `CLOSER_DEPTH` = 3 last-children down,
past a `;`/`,`, that starts the fold's last line), so `class A {` folds with
its `};` below, while a Python `def` ending in a multi-line call folds whole
rather than leaving a lone `)`. Ranges sharing a start keep the widest.
`SyntaxEngine.folds(id, version)` runs the query over the whole tree and
**shares one `matchMs` (250 ms) between the query and reading its matches**
(near 1 MiB, 40k folds took ~200 ms to read, more than the query); over it
answers null, twice running the document folds by indentation from then
on; a stale tree (a reparse over budget) still answers from the edited old
tree; answers are cached per version and tree (Monaco's folding and its
sticky scroll both ask). Host language only: a fence is one fold. The worker
takes `{t: "folds", id, req, version}`; the client registers a
`FoldingRangeProvider` for exactly `COVERED_LANGUAGES` beside the tokens
provider, matched by request number, the same `REPLY_TIMEOUT_MS` (a silent
worker is dead, as for tokens), and **null means indentation**: Monaco's
folding and sticky scroll fall back to the indentation provider whenever
every provider answers null, so the provider answers null on Monarch (large
file, slow parse/edits, grammar failed, no worker), for an answer whose
version the model has passed, and when the query was over budget — never
`[]`, which would mean "no folds". `dotenv` has no provider and never reaches
the worker. **Sticky scroll follows the tree's folds** with no change of its
own: with no document symbol provider (E-1), Monaco's `outlineModel` falls
through to the folding providers. `__hudSyntax.states()` gains `folds`
(answers from the tree handed to Monaco). Sizes: first paint unchanged,
`monacoBundle` +936 B, the worker +10.6 KB. Free checks:
`lib/syntax/folds.test.ts` (a C++ class, a Python function, a Markdown
section, where folds end, CRLF, every grammar, versions, the stale tree, the
budget both halves, the cache), the folds half of `client.test.ts` (stand-in
Monaco: registration, kinds, request numbers, stale and null answers, Monarch,
the size budget, a silent worker) and `vendor.test.ts` (the manifest, every
folds query compiles and captures only `@fold`), and `_fold_checks` in
`tests/face/hud_v2_syntax_check.py` (the margin's fold controls in a C++
file are the tree's — 1, 4, 6, 7, 13 — not indentation's 5 and 15; folding
the class keeps `};`; sticky scroll holds `void autonomousRoutine(int
side,`, where indentation would hold `{`; a Markdown heading folds; a file
past the size budget folds by indentation). With the provider unregistered,
7 of those fail (sticky shows `{`); the null/`[]`, version, both budget and
off-after-two guards each fail their vitest when removed. Not done: region
markers (`// #region`) — the indentation provider honours them, tree folds
do not.

**The git gutter and the explorer's git colours, ED-9 (2026-10-12; plan §2.5,
§2.8, the ED-9 package in §3; design §18; contract in `docs/hud-api.md`).**
Two routes in `jarvis/v2/editor_git.py`, **owner-only before anything else**
(`hud_api.route` calls `owner_only` before the project is even looked up, so
a bare request or the API listener is 403 whatever the id). `GET
…/git/base?path=` answers HEAD's text; **the path is judged before git
runs** — the gate's state (resolved or as configured), anything
`editor_policy` withholds (`withheld_anywhere`: no gate exemption) and every
credential env file are 403 (`editor_git.refused`) — **and so is the name in
git**: a staged rename's source (`diff-index --cached -M HEAD`; `-M` reports
no copies, so a staged copy is `new`) is judged the same way, because `git mv
.env notes.txt` would otherwise serve `.env`'s committed text as
`notes.txt`'s, and **held to the project** (review of PR #61, LOW 1): placed
by `hud_api.scope` (a root, no `SKIP_DIRS` or `.git`), so a file moved in
from outside the project, or out of `node_modules`, is `new` and nothing of
its source is sent. HEAD's blob is read **by object id** (`cat-file blob`: no
textconv, filter or eol attribute applies), its size read first from `ls-tree
-l` and capped at the editor's 2 MiB, **in bytes** (`_git_blob`: gitops' own
binary, hardening and environment — `gitops._git` decodes with replacement)
and judged by the editor's own `editor_policy.is_text`, so a U+FFFD the file
holds is text and a NUL or non-UTF-8 blob is `binary` (no gutter); a file
HEAD lacks is `new` (no gutter, untracked and added alike — the explorer's
colour says which). `GET
…/git/status` is `git status --porcelain=v2 -z … --ignored=traditional
--renames --ignore-submodules=all -- .`, project-relative, **judged before it
is returned** (SKIP_DIRS, withheld and the gate's state dropped, lexically: a
status is names only, and a rename's source is never returned; the policy
is `editor_nav._policy_for` narrowed to the root as resolved and as
written), cached 2 s
per root — or three times what the status took, when longer, so a slow `/mnt/c`
repository rests between runs — refusals too, single-flight, 20,000 entries
and 5 s at most.
**Git only through `gitops._git`, and a repository whose own config names a
program is refused by both** (`gitops._risky_config`). **`--ignore-submodules=all`
is load-bearing**: `_risky_config` reads the superproject's config only, and
a status that recurses runs `git status` inside the submodule, which obeys
*its* config — measured: a submodule's `filter.x.clean` ran on a superproject
status through `gitops._git` (a same-size, mtime-moved file forces the
hash). **The same hole was open in the `git` tool** (`gitops.run(path,
["status"])` and `["diff"]` each ran a submodule's filter, measured in-process)
until 2026-10-12 (PR #65), when `gitops` itself was kept out of submodules (see *Agents
that cannot ask* in Safety design); the editor's own flag stays, since its
`_risky` reads the superproject's config only. A base read inside a submodule is refused anyway
(its config sets `core.worktree`). The owner's global config (a git-lfs
filter) still runs on a status, as in gitops. **The window**:
`lib/linediff.ts` is Myers in linear space (middle snake, explicit stack, never
recursion), lines interned to ints, **bounded by a work budget**
(`DIFF_BUDGET` 2M steps, about 1,400 changed lines exact): past it the rest of
a run is one coarse "modified" block, still a correct diff (`exact: false`);
two runs with no line in common are an exact replace. `gutterMarks`: inserted
runs `added`, replaced runs `modified` over the lines standing now, removed
runs a `deleted` wedge at the foot of the line above (the head of line 1).
`state/gitGutter.ts`: **one gutter per buffer (`abs`)**, the diff against the
**model's lines, never the disk**, `DEBOUNCE_MS` 150 (500 past 5,000 lines)
after the last change — so a pause, not a keystroke, reads the lines (the
ED-2 rule bends this far and no further) — no gutter past 100,000 lines
(counted with `getLineCount()` before the lines are read); HEAD's text read
again **when the status poll's `head` moves** — and, **for a file whose base
came from another HEAD than the project's** (a nested repository, a folder of
robot repositories, the Inbox at `~`, an extra folder: the poll can never see
that HEAD move), **every `RECHECK_MS` (10 s) while the window is visible**
(review of PR #61, LOW 2); a late answer for a closed gutter is dropped; a
byte-order mark in HEAD's text is the file's, as in the buffer. `PaneEditor` draws the marks in its
own `createDecorationsCollection` (linesDecorationsClassName `jgit-add`,
`-mod`, `-del`, `-top`; theme.css draws a 3px bar in `--syn-string` /
`--syn-fn` and a `--red` wedge at 60%, ED-3's "removed"; no Monaco colour
key — `editorGutter.*` is not registered in Monaco 0.57). **FileTab asks for
a gutter only on a `text` or `safety` buffer** — never env, withheld, gate or
binary — through the project the buffer was read through.
`state/gitStatus.ts` polls each watched project every 3 s while visible (and
on focus), one request at a time, and **bumps only when the answer
changes**; a 403/404 is kept as `error` and asked again after 30 s. The tree:
`useGitMarks` (`components/GitMarks.tsx`) hands the memoised `Node` a
`GitMarks` built from a string key (`lib/gitMarks.ts` `gitKey`), so #50's
rule holds — no redraw per keystroke or per unchanged poll; a row carries
`data-git` and a tooltip, the name modified `--syn-fn`, added/renamed/
untracked `--syn-string`, conflict `--syn-const`, ignored at 50% opacity; a
folder wears what git reported for it whole, else the strongest state under
it (conflict > modified/deleted > added/renamed > untracked; ignored never
colours a folder). The mock serves `world["git"]` (`git_route`; no key: not a
repository). Free checks: `tests/v2/editor_git_check.py` (29; owner-only with
no git run when refused, the states, every refusal including renames from
`.env`, a withheld file and the gate's state, renames from outside the
project and out of `node_modules` answering `new`, a staged copy `new`, strict
UTF-8, a repository-config filter and a submodule's that never run, the
status's states, judging, cache and cap),
`linediff.test.ts` (3,000 random pairs against an O(NM) LCS reference:
correct and minimal; budgets; a 50k-line file), `gitMarks.test.ts`,
`gitGutter.test.ts` (debounce, one read per pause, undo to clean, refetch on
the project's HEAD, a commit in a nested repository followed by the recheck and
none while hidden, no recheck in the project's own repository, the BOM, a long
file never read, the poll's bumps and back-off) and `tests/face/hud_v2_git_check.py`
(a `MockDaemon(0)` of its own, after the marks section of `hud_v2_check`: an
added bar on an unsaved line and clean again on undo, modified and the wedge
in their tokens, HEAD moving, an env file asking nothing, the explorer's
colours and a recolour on the poll, a MutationObserver seeing no tree change
while typing). Shown to bite in a scratch copy (7 of 7): the path's refusal,
the name in git, the status's and the base's config check, the submodule
flag (the filter ran), the status's judging, and `owner_only`; and in the
review round, the source's project check (both new cases) and the recheck
(vitest).

**Editor settings, `.editorconfig` and vim, ED-8 (2026-10-12; plan §2.8,
decisions E-9, E-11).** **Settings** (`lib/editorSettings.ts`, pure; the live
copy `state/editorSettings.ts`): font family, size (8–32), tab size (1–16),
spaces or tabs, word wrap (off) and Markdown word wrap (on), minimap (off),
sticky scroll, whitespace, bracket-pair colours, cursor style, vim (off) —
**auto-save and format-on-save are not settings: off, decided (E-9)**. Stored
in localStorage `jarvis.hud.editor` behind try/catch; **every field is judged
alone** (`FIELDS`), so garbage in one falls back to that field's default and
never costs the others, a dialog change that is bad keeps the current value
(`withChange`), and only the known fields are written. **The font family is a
list of names only** (`safeFontFamily`: bare names from a letter, or quoted,
of letters, digits, space, `_ . -`; at most 8 and 200 characters; never `;`,
braces, parentheses, `\`, `/`, `:`, a nested quote or an invisible
character): Monaco writes it into the window that gates approvals. The
defaults are what the editor drew before (Monaco's font, 12px, sticky scroll,
whitespace on selection, bracket colours, line cursor). **A change reaches
every live editor in the call that made it** (a synchronous listener, never
a render later; diff and Compare views take the display half,
`displayOptions`); another window's change arrives by the `storage` event.
The dialog (`components/EditorSettings.tsx`, `state.picker =
"editorSettings"`) opens from the File pane's ⚙ (`editor-settings`) or the
palette's "Editor Settings…"; it applies as you go (nothing to confirm), so
**under a card App closes it** with quick open and the palette, the ⚙ is
disabled, and its Escape never runs under one. **`.editorconfig`**
(`lib/editorconfig.ts` pure, `state/editorconfig.ts`) is read **only through
ED-2's owner-only routes**: one `files/stat` of `<dir>/.editorconfig` for the
file's folder and each above it **to the project's root, never above**, a
`GET /file` of each that exists (once per etag), applied nearest first,
**stopping at `root = true`**; a folder's answer is kept 3 s, and past 2.5 s
(a slow `/mnt/c`) opening a file goes on without it. It sets **indent style and size** (the
model's options; kept through a settings change, which makes Monaco
re-detect every model — `reapplyEditorConfig`) and **the line ending new
lines take in a file with no line break yet** — never a file's existing
endings (LF/CRLF only; `cr` ignored). **It never rewrites on save**:
`trim_trailing_whitespace`, `insert_final_newline` and `charset` are read and
ignored. It is applied by `BufferDeps.prepare`, awaited in `Buffers.make`
**before the buffer records the version it was read as**, so a new file's
`setEOL` (which moves Monaco's version) still opens clean. Globs follow the
spec (`*`, `**`, `**/` as zero or more folders, `?`, `[…]`/`[!…]`, nested
`{a,b}`, `{n..m}`, `\` escapes, a slash-less glob at any depth, a slash inside
brackets literal) and **are matched by a table over tokens × path, never a
backtracking regex**: an `.editorconfig` is agent-writable project content,
and `[*a*a*a*a*a*a*a*a*b]` as a regex hangs the window on a long name.
**The guard is structural, a clock and not a cost model** (the lead's
decision on PR #59's third review: 123 maximal sections had been a 689 ms
long task per open, and a work budget was out-counted three ways — failed
compiles charged nothing, a thrashing compile cache, under-weighted number
ranges — to a 1,517 ms task): (a) **a real clock** — parse, compile and
match all spend through one `Deadline` (`RESOLVE_MS` 5 ms), which reads
`performance.now()` every `CHECK_STEPS` (256) steps from inside the parser,
the compiler and the matcher and throws `OutOfTime`; the file then has **no
configuration at all, never a partial one**; (b) **poison** — the config
(by folder and etag) that spent the time takes a strike, and **two strikes
in a row** (one GC pause must not cost a real config its effect) **or any
cap** poison it: every later resolution touching it, for any path, is O(1)
and never queued, until its etag changes. **A resolution that judges a config
in time resets its strikes** (PR #72, LOW 3): with no reset, two time-outs
any distance apart poisoned the owner's real config, and on a loaded core
the real-world config timed out in 2.4% of 3,000 resolutions; (c) **no batching** — resolutions run one per
macrotask through a queue (`MessageChannel` in the window, `setTimeout` in
Node), so restored tabs never pile into one long task; `prepare` still
applies before the buffer records its text. Compiled globs are kept with
their parsed file by etag (`ConfigFile.compiled`; no global map to thrash),
caches drop their oldest entry, a path left with no configuration is said
once in the console by path (`warned` ≤ 64), number ranges count digits
arithmetically, and a character class remembers each character's answer.
Caps: 64 alternatives, 512 tokens in **each** (not the first alone) and
4,096 in all per glob, braces 16 deep, 128 sections per file, paths ≤ 1024;
resolutions are cached by (project, path, each config's folder and etag), and
a config the stat says is over 64 KB is never downloaded. Measured: the
review's shapes in Node, 40 tabs at once, longest task 5.1 ms (32 heavy
configs deep) and 5.0 ms (number ranges), 56 ms in all over 40 tasks; in
Chromium, 32 heavy configs and 20 tabs give no long task at all (506f670's
budget: one of 118 ms on the same shape; no clock: four of ~10 s) and the
tabs are ready ~150 ms later than with no configs. **Vim (E-11, yes)**:
`monaco-vim` **0.4.4, pinned exactly**, MIT, audited (no network, storage,
`eval` or `innerHTML`; its status line is text nodes and one input), loaded
in a lazy chunk of its own only when the setting is on (101 KB, 31 KB gzip).
It imports Monaco by pre-0.56 deep paths, which `vite.config.ts` aliases to
the same modules (`monaco-editor/esm/vs/*` → `monaco-editor/*`, and its ESM
build, not the UMD `browser` export) — **the dynamic import split Monaco's
lazy chunk in two** (`monacoBundle` + `editor.api`, 3.65 MB together as
before). **`:w` is that editor's own guarded save** (`adapter.save`, per
editor; monaco-vim's global `commands.save` is never set, and never
`addCommand`), and does nothing under a card. **`:colorscheme` is a no-op**
and the adapter's `theme` option never reaches Monaco's window-wide
`setTheme` (it took every editor off Graphite, an unknown name to the light
`vs`); **`u`, Ctrl+R, `:undo` and `:redo` do nothing on a read-only editor**
(monaco-vim calls the model's `undo()` past the editor's read-only); a
settings change keeps vim's cursor. Each File pane draws vim's
status line (`editor-vim-status`) below its editor. **Push-to-talk keeps
Ctrl+D in the editor, vim or not — the owner's decision (2026-10-12)**: the
window takes it before Monaco, so vim's half-page-down is gone; the HUD's
other window keys (Ctrl+B, Ctrl+P, zoom) are the HUD's in vim too. Test
hook, read-only: `window.__hud.editorSettings()` (stored, in force, each
File editor's raw options, vim, its model's indentation and EOL — never
text). Sizes: first paint +20 KB (685,073 → 705,426 B against f1de871). Free checks: `lib/editorSettings.test.ts`,
`lib/editorconfig.test.ts` (the parser, the spec's glob table, the
clock on the review's shapes — real and fake clocks — and the caps),
`state/editorconfig.test.ts` (the walk, root, caches, refusals, timeout, the
prepare hook leaving a buffer clean; 32 configs deep, 40 batched
resolutions and the number-range shape with no job over 16 ms, poison doing
no work and lifting on a new etag) and
`tests/face/hud_v2_editor_settings_check.py` (a `MockDaemon(0)` of its own,
run by `hud_v2_check` after the navigation section, or alone, `ONLY=vim`;
`_long_task_checks` records long tasks with a `PerformanceObserver`).
Shown to bite in a scratch copy, 7 of 7: the font unchecked, EOL set on a
file with endings, `prepare` not awaited, `:w` as one window-wide hook, vim
keeping Ctrl+D, the dialog left up under a card, no alternatives cap; and the
review round's 8 of 8: the work budget (since replaced by the clock), the
token cap on the first alternative only, no section cap, no resolution cache,
the size cap not read, `:colorscheme` reaching `setTheme`, vim's undo past
read-only, and a settings change rewriting vim's cursor; and the third
round's: the clock removed (eight vitests, and in Chromium four ~10 s long
tasks) and poison removed (three vitests). Not
done: a buffer renamed later keeps the `.editorconfig` it opened with, and an
edit to `.editorconfig` reaches files opened after it (3 s), not those open.

**The Diff pane shows the project's uncommitted changes (2026-10-12; the
owner: "the Diff doesn't work right now it just says to select a Task", and
yes to VS Code's Source Control view; design §18; contract in
`docs/hud-api.md`).** No new route: ED-9's `git/status` and `git/base` and
ED-2's read. **The mode** is `lib/scm.ts` `diffMode`: **a Diff pane never
shows another task than a drawn Task pane** (review of PR #67, L3) — while
one is drawn and shows the window's task, the Diff shows that task's diff,
over a pin and after a thread is picked, and its picker is off, saying why;
else a task picked more
recently than a thread (`taskFocus`, the rule Editor and Browser panes follow) shows
the task's diff exactly as before (`DiffTab`) — **the task beats the pin**,
because a pane's `projectId` is shared by its views (a File pane pins its
project when it opens a file, and keeps the pin when switched to Diff; with
the pin first, `hud_v2_csp_check`'s task click showed uncommitted changes),
and `workspace.show("task")` relies on a drawn Diff pane following the task;
else the pane's pinned live project (its header's `pane-N-diff-project`
picker, E-13's shape) shows that project's uncommitted changes; else the
chat's project's; else a sentence. **Picking a project in that picker sets
`taskFocus` false** (as picking a thread does), so the choice shows at once;
the picker reads Follow while a task is shown. The caption (`diff-mode`,
`data-mode`, `data-why`) says what and why — `diff-mode-what` and
`diff-mode-why`: the Task pane's task, the task picked last, this pane's
project or the chat's project (`diffWhy`). **The list** (`components/ProjectDiff.tsx`, `scmRows`): ED-9's
one status poller, subscribed only while the pane is drawn; **`ProjectDiff`
is `memo(...)` with a comparator that reads its callbacks through a ref**
(App hands it fresh closures every render) and its rows follow the status's
version, never the answer object — unmemoized, a chat keystroke redrew the
list, 17 ms at 2,000 changed files and 132 ms at the 20,000 cap (review M1;
`ProjectDiff.test.ts` counts row renders); modified/added/
untracked/renamed/deleted/conflict files and untracked folders, each with
its letter (M A U R D C) in the explorer's `data-git` colours and
`FileIcon`; `ignored` never; truncation and no repository/no changes said
in a sentence. **The sides**: HEAD from `git/base` in a `jarvis-diff:`
model this view makes and disposes; the right side **is the open buffer's
own model** when one holds the file (found by project and path, by
`root_real`, or by the read's `abs`) — borrowed, never copied, held or
disposed here, released on the model's `onWillDispose` so a File pane that
lets the buffer go never leaves a disposed model on screen (the view falls
back to a read, saying "…" meanwhile — and one disposed before Monaco
loaded is "…" too, never an empty side that reads as a deletion) — else
ED-2's read in a `jarvis-diff:` model; a deletion's
right side is empty; binary/too_large/not_a_file say so. **No diff
computation outlives its models** (review L1): the editor's view model is
the view's own (`createViewModel`) and is disposed — cancelling a diff the
worker is still computing — before any model goes. Handed `{original,
modified}`, Monaco 0.57 keeps a reference to the view model it makes that
nothing releases, so its computation was never cancelled, and a row picked
then opened and edited at once threw `no diff result available`, uncaught.
The task `DiffEditor` and `CompareEditor` in `Editor.tsx` own theirs too
since the follow-ups (below). Both sides
read-only, side by side even in a narrow pane; ↻ reads again; the right
side of an unopened file is re-read when the status moves. **Env files are
listed and never diffed**: `rowShows` sends a `.env*` row (fileIcons'
`isEnvName`) to a sentence with Open — no `git/base`, no read, no model;
`git/base` runs before any read, so an env file by resolution is refused
there (a 403 naming an env file shows the same sentence) and never read;
and a live buffer of kind `env` is never put in a diff. **`git/base` now
answers a file deleted from the working tree** (`deleted: true`; nothing at
the path, not even a broken link; judged exactly as before, before git runs;
not in HEAD either is still 404). **Open** is `toFilePane` → `placeFile` →
`askFile(open)` — never over the Diff pane or a VEX view. Read-only: no
staging, reverting, committing or discarding (SCM actions are out of
scope). Under a card the picker, Open and ↻ are disabled and a row click
does nothing. Free checks: `lib/scm.test.ts` (19), `components/ProjectDiff.test.ts`
(M1's render counts), `tests/v2/editor_git_check.py`
(deletions answered, judged and 404 when absent from HEAD),
`hud_v2_csp_check`'s single-pane task diff and `tests/face/hud_v2_scm_check.py`
(65, a `MockDaemon(0)` of its own after the git section of `hud_v2_check`;
the mock's `git/base` answers deletions and `states`; L1's swap with no wait,
L2's `link.txt` whose HEAD text is refused as an env file and is never read,
L3's chat · Task · Diff layout). Guard removals, each shown to bite: with
`rowShows`' env rule dropped, the headless check sees a `git/base?path=.env`
request and the vitest fails; `memo(` dropped or callbacks compared, 63 row
renders where 3 are right; the owned view model dropped, `no diff result
available`; `baseOk` dropped from the read's condition, a `/file?path=link.txt`
read; the Task pane left out of the mode, the Diff follows the chat. Not
done: SCM actions; a file not open in the
editor that changes on disk while the project's git states stay the same is
re-read only on ↻ or a click; the list is flat (no staged/unstaged groups).

**The Diff pane's follow-ups (2026-10-12; PR #67's reviews; design §18).**
**The change list is windowed** (`ProjectDiff.tsx` `ScmList`; the arithmetic
is `lib/scm.ts` `scmWindow`/`revealTop`): only the rows in view, and
`SCM_OVERSCAN` (12) either side, are in the DOM, in a spacer as tall as every
row; **every row is `SCM_ROW_PX` (24) CSS px, pinned inline**, so where a row
is needs no measuring, and `scrollTop` is the list's own CSS px (the HUD's
zoom does not change it; checked at 160%). **How much is in view is the
drawn height over `currentCSSZoom`** (`viewOf`), never `clientHeight`, which
is rounded: at 160% a list 400.6 px tall read as 401, and End clipped 0.8
drawn px of its row (review of PR #73, NIT). The window is
computed each render from where the list is scrolled, kept in whole rows (a
scroll redraws the list only when it crosses a row, and only the list: it is
`memo`'d, with a stable pick). At git/status's 20,000 cap a DOM of every row
made each chat keystroke beside it cost ~100 ms (the reviewer's measure;
74–103 ms here), and drawing took 0.2–0.4 s; windowed it is ~2 ms, the same
as no Diff pane. **Keys** (the list is a focusable `listbox`, focused by a
row click or Tab): ↑ ↓, Page Up/Down, Home, End (`scmKeyStep`) move the
selection and scroll it into view; Enter reads the selected row again. **A
key moves the highlight at once and reads the row only once the keys have
settled** (`SCM_KEY_SETTLE_MS`, 150 ms; the pane keeps `sel`, the highlight,
apart from `shown`, the file read, and says "…" between); a click and Enter
read at once, and a key pick never folds a narrow pane's list (a click
does). **A read nobody waits for is aborted**: `api.gitBase`/`api.readFile`
take a signal, aborted in `ScmFile`'s effect cleanup. Review of PR #73, MED:
↓ held over 60 rows started 60 `git/base` requests and reads, and against a
slow repository (the owner's VEX projects on /mnt/c) the last row's diff came
4 s late at 0.5 s a request; now one request and one read go out, the diff
0.65 s after the last key. **For a screen reader** each option carries an
id, `aria-setsize` (the whole list) and `aria-posinset`, and the listbox's
`aria-activedescendant` names the selected option whenever it is drawn (a
key always draws it; no dangling id when it is scrolled away). **The
selection is a path, never a drawn row**: scrolled out of the window it
stays selected, its diff stays shown, and the keys move from it. A file the
pane had (a reload) is scrolled into view once, when the rows first arrive —
or, in a narrow pane where a picked file folds the list, once the list is
shown again (ResizeObserver) — and never on a poll, which would pull the list
back from where the owner scrolled it. **The way out of a Task pane's hold:
the Task pane's header clears its task** (`pane-N-task` chip, `pane-N-task-clear`;
`taskId: null`, `taskFocus: false`), disabled under a card. The default 2×2
grid draws a Task pane (`DEFAULT_VIEWS` slot 4), and while it shows a task
every Diff pane shows that task's diff (L3) with its picker off — nothing took
the window off a task once one was picked. Of the brief's two ways out this
is the simpler: `diffMode` is unchanged, and a Diff pane still never shows
another task than a drawn Task pane (there is none to show). The held
picker's title names it. **Every diff view owns its view model**: the task
`DiffEditor` and `CompareEditor` (`Editor.tsx`) now do as `ScmDiffEditor`
does since L1 — `createViewModel`, set on the editor, disposed first (which
cancels a diff still waiting on the worker) and once on every path (`vm` is
cleared as it goes), then `setModel(null)`, then the editor, then the
models it made; Compare's right side is the buffer's model, which it never
disposes, and it lets go of it on the model's `onWillDispose`. Its text,
undo stack and dirty state are untouched (checked: edit kept, still dirty,
Ctrl+Z still undoes it). **`ScmDiffEditor` draws with ED-8's
`displayOptions`**, live on a settings change (`onEditorSettings`, then
`reapplyEditorConfig()`, as the other two) — it had a fixed `fontSize: 12`.
Free checks: `lib/scm.test.ts` (26), `components/ProjectDiff.test.ts` (4: the
20,000-row window in jsdom and the keys past it, five keys read nothing until
they settle and then the last row once, a click at once; the ARIA; nothing
moved under a card) and `tests/face/hud_v2_scm_check.py` (now 110;
`ONLY=<section>` runs one):
`_scale_checks` (20,000 rows, a window drawn, chat typing under 15 ms a
character, the last row reached, the keys, a selection held and moved out of
the window, a reload's file revealed at 160%, and at 160% Page Down and End
clipping nothing of a list 400.6 px tall), `_hold_checks` (↓ held at ~33 a
second over 60 rows, the mock's `git/base` taking 0.5 s (`base_delay`): at
most 3 requests and the last diff within 2 s; a row clicked away from before
HEAD answered is aborted), `_clear_task_checks` (the 2×2
grid with a Task pane), `_settings_checks` and `_midcompute_checks` (a
40,000-line task-diff file keeps Monaco's one editor worker busy for its 5 s
diff limit; a task diff, then Compare, is opened and closed while its own
diff waits behind it — main's code threw `no diff result available` each
time). Shown to bite in a scratch copy: every row drawn (the window and
out-of-window checks fail; main's build also fails the typing check at
74 ms); `clear` keeping the task id (three checks); the task diff or Compare
back to `{original, modified}`, or the task diff's view model never
disposed (`no diff result available`); the settings not live, or not used at
all (12px where 16px was chosen); keys that never scroll (five checks); the
review round's: no settle (60 `git/base`), neither settle nor abort (60, and
the last diff 4.14 s late), no abort (the clicked-away request completes),
the rounded `clientHeight` (End clips 0.812 px at 160%).

<!-- claude-source:003658-004278:end -->
