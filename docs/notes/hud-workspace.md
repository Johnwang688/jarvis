# Hud Workspace — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 998–1555

<!-- claude-source:000998-001555:begin -->
**Chat titles (2026-10-12; `jarvis/v2/titles.py`, design §18, contract in
`docs/hud-api.md`).** The owner: "each chat just displays its id … keep the
id in the three dots but have the main display be the title and have the
title wrap". A `Titler` (one worker thread, started by daemon2's `main()` —
**never `Daemon.start`, so no test daemon calls a model** — stopped by
`Daemon.stop`) queues a chat thread at its first owner `user_message`
(`origin: owner`, via hud/discord/dm, typed words present; the bus observer
only queues an id) and, `BACKFILL_DELAY_S` after start, every untitled chat
(archived included) with an owner message in its log. Role `chat` only,
never a task's thread. **The words are the log's `typed`, never an inlined
file** (a pre-PR C record, `text` only, is cut at its first fence), capped at
2000, between v1's delimiters and labelled data. **Model**: Codex
`config.TITLE_CODEX_MODEL` (`gpt-6-luna`, `JARVIS_TITLE_CODEX_MODEL`) at
effort low when Codex is in the roster, the catalog lists it and its probe
passes, through **`CodexProvider.one_shot`** — `account_metadata`'s shape
(`MetadataBusy` before the probe, one 20 s deadline, the lock released in a
nested `finally`, `cancel_metadata()` ends it) in a private `_titles` home
(`codex_config.prepare_title`: read-only sandbox, approvals never, web search
disabled, no MCP servers, no skills), a fresh empty temp cwd, an ephemeral
thread with **`environments: []`** (experimental in 0.153.4 and 0.161.0:
"empty disables environment access" — the closest the app-server has to a
tool-less turn; read from the generated schema, never run live), and any
tool item or `item/<tool>/…` notification, server request but a clock read,
unhonoured model or wider sandbox is an error **and nothing from that turn
is used**, agent text before it included. **The limit, stated**: if a
ChatGPT-auth turn ignores `environments: []`, a read-only command can run
before the turn is refused; its output is discarded. A busy lock waits in the queue (`CODEX_BUSY_TRIES` × 30
s, others go ahead), then OpenRouter; so does any Codex failure or an
unusable answer. **OpenRouter**: `models.tier("orchestrator")` with no tools
and `max_tokens` 1000, **never a Chinese-hosted vendor** (`models.cn_hosted`
by id prefix — moved from `titles` in BR-9a, one list with the Browser's
reviewer; the configured orchestrator instead, else no title).
`clean_title`: first non-empty line, invisible characters out, a "Title:"
label, markdown, quotes and trailing punctuation off, more than 12 words is
an answer and is refused. The words are `secrets.scrub`bed before they leave
and the title before it is written. Nothing usable — or a write that raised —
is tried once more at the next owner message (`ATTEMPTS`). **The write is
rename's** (`projects._write_title`): `projects.auto_title` writes only if,
**under `daemon._lock`, the thread and its live session's copy still have no
title** — a rename before or during generation wins — unique in the project,
onto the session's copy, and the same `thread_updated` (now with
`title_source`), so the HUD and the Discord thread follow.
`Thread.title_source` is `""`/`"auto"`/`"owner"`; a rename sets `"owner"`.
**`ThreadStore.set_title` is the one writer of `title`/`title_source`**
(`_write_title`, a restore's renumbering); `save` keeps the stored ones
whatever the caller holds, as it keeps `surface` — a session copy read in
`resume_thread`'s window used to put an older automatic title back over the
owner's rename (review L1). **Tests that set a title use `set_title`**; a
`save` with a new title is silently a no-op. **A chat archived in the HUD
(the thread or its project) is never renamed on Discord** (review M1: Discord
refuses with 50083, the poster unarchived to retry and never re-archived, so
the backfill would have reopened every archived chat there): the mirror's
`_renamed` updates only the sidecar's name and marks `rename_pending`, and
`thread_restored` (after its "Restored." post) or `project_restored` applies
it. Logs name the thread and the route, never the title or the words.
**No tool can set or regenerate a title** (`titles_check` `NoTool`, by name
and by what tool modules call); `PATCH /threads/{id}` is not `owner_only`, as
before — an API client's rename counts as the owner's. HUD: the row's name
is `threadLabel` (`lib/threadTitle.ts`) — the title, truncated to one line
with an ellipsis (`.nm.ttl`; `HoverTitle` slowly scrolls overflowing titles
on hover and resets on leave, respects reduced motion, and keeps the whole
title in the tooltip; a pinned model's badge gives way), or "New thread"
(no turns) / "Untitled", muted italic, **never the id**, which is the ⋯ menu's **Copy id** (`tmenu-copyid`); a split's chat
header and the Archive view (its id the tooltip; `quotedName` in sentences)
name a chat the same way. Free checks: `tests/v2/titles_check.py` (35, with
a stand-in app-server over real pipes for `one_shot`), the mirror's
`…waits_for_its_restore` pair in `discord_mirror_check`,
`threadTitle.test.ts`, `tests/face/hud_v2_titles_check.py` (its own
`MockDaemon(0)`, run by `hud_v2_check` after the editor-marks section).
Guard removals that bite: the owner guard in `auto_title`, the session-copy
update, `save` keeping the stored title, the mirror's archived skip.

**Tool-call transcript, daemon half (TC-1, 2026-10-12; design §18,
contract `docs/hud-api.md`, plan `docs/plans/2026-10-12-hud-tool-calls-in-transcript.md`).**
`GET /threads/{id}/transcript?tools=1` interleaves tool starts with existing
rows, pairing finishes within their turn; no parameter preserves the old
bytes. `summary` and `args` never leave this route. `tool_calls.label` reads
only display fields, never file contents or diffs, makes cwd paths relative,
then scrubs known credential values (including the thread folder's credential
env files) and every format in `credential_patterns.PATTERNS`. URLs in any
label lose userinfo, query and fragment, keeping scheme, host, port and path.
Grep shows its pattern with ` in <path>` when scoped.
A value split by shell quoting can still appear in a label;
labels do not parse or evaluate shell commands. Also still visible: a password with a bare `/` in a URL, and scheme-less `user:pass@host` or `curl -u user:pass`. Only the first 4,096 characters are judged, so every pass stays linear (a 1 MB argument takes a millisecond). The env values come from the thread's folder and its parents up to home. Labels end as one cleaned
120-character line; tool names are cleaned to 40 characters. `_record` computes it once
**before `_lock`** (scrub reads files), onto the existing start's log and bus
data; a label exception silently falls back to the cleaned tool name. History
reuses it; only the newest 500 starts may derive old labels. Providers own
`ok`/`failed`/`denied` and preserve the pre-TC-1 `ok` calculation. Claude tracks
Jarvis-denied tool ids (only an actual denial lacking an id uses an error
flag plus hook prefix); its classifier signal stays unchanged. Fast path
recognizes dispatch/approval refusal wording, and never assigns an outcome
from raw-content tools' text. An older finish maps from `ok`.
An unfinished call runs only for the thread's current running turn, otherwise
it is stopped, including after restart. No new log lines or bus kinds.
Free suites: `tool_calls_check`, the backend golden and all three providers.

**Tool-call transcript, HUD half (TC-2, 2026-10-13).** Consecutive calls of
one turn appear as a collapsed transcript row, replacing the separate ticker.
Settled text, any owner message, system lines and new turns split groups;
finishes find their call across a steer split by both turn and call id.
`lib/toolCalls.ts` is the shared live/history rule. The HUD requests
`?tools=1`, falling back to the old route only on 400. Events heard while
history loads are replayed by stable ids/server timestamps; stops, optimistic
message ids and failed sends survive the load. The opt-in route also includes
text-row turn ids, so the acknowledged initial owner message reconciles by
turn without merging repeated text or same-turn steers. The running final group says
`Using tools… N`; all others say `Used N tools`, with failed/denied/stopped
counts. Every group starts closed. Expanded rows contain only the cleaned
name, daemon label, outcome word and duration, never args or result summaries.
`ChatState.ops` remains the thinking/orb/status source and is updated with the
transcript. Stops happen before queued-next handling and on idle reconcile.
Disclosure state is window-local, capped at 500, keyed by thread, turn and
first call (turn prevents reused provider ids from sharing a disclosure).
Per-key subscriptions and memoized rows keep typing from redrawing history;
more than 40 calls use the unchanged SCM window helper, 22px rows and a 16-row
viewport. New entries follow only within 24px of the bottom; toggling a group
preserves the log offset. Native buttons support Enter/Space, pointer clicks
preserve input focus, Ctrl+D remains talk, and approval cards keep the log
inert. `hud_v2_toolcalls_check.py` covers live/reload/races, fallback, keyboard,
scroll, card layering, hostile labels and 2,000-call histories in two panes.

**HUD zoom and panes (2026-10-08, design §18).** The rules are in
`hud/src/lib/layout.ts`. Zoom is 70–160% in 10% steps: `− 100% +` in the
title bar (since 2026-10-09; it used to fold away with the status pane), or
Ctrl+= / Ctrl+- / Ctrl+0 — **the HUD's everywhere**, input
bar, Monaco and terminals included (inside a terminal only Ctrl+_ —
Ctrl+Shift+-, readline's undo — goes to the shell instead of zooming out;
WP-D), because a key let through is Chrome's page zoom,
which the control cannot see and Chrome remembers per site. It is **CSS
`zoom` on `#root`**, so **every `vh`/`vw` in `theme.css` must divide by
`--ui-zoom`**, and a menu positioned from a screen rect must go through
`toCss()`. An undivided `86vh` put the approval card's buttons off screen
at 160%. **The approval button row is deliberately not sticky**: reaching
AUTHORIZE means scrolling past the whole command, so a long command's tail
(a `curl … | sh` on line 45) cannot stay hidden while the button is in
reach. **Every layout key is inert while a card is up** (`useLayout(blocked)`,
as PTT and wake are), so nothing re-lays the card out under an unmoved
pointer. Each side pane folds to a 36px rail: `«`/`»`, Ctrl+B for the left,
Ctrl+Alt+B for the right (from Monaco too since ED-1, except as the second
key of its Ctrl+K chord; not on auto-repeat or mid-composition; Ctrl+Shift+B
is the browser's).
The left rail keeps **+ New thread** and a shrunken orb, and the right rail
shows pending approvals and errors. A folded pane is hidden, **never
unmounted**, so its state survives. The inner edges are `role="separator"`:
drag them, use the arrows, or double-click to reset; Space on one does
nothing. Widths are unzoomed px (left 180–480, right 240–560). The
centre is kept at 480 by shrinking the panes and then, when even their
minimums do not fit, by **folding them for the render** (right first, then
left; `fitLayout`) — never by rewriting the stored widths or folded flags,
so the owner's layout returns when there is room, and opening a
window-folded pane folds the other one instead. Only a window too small with
both panes folded (under ~552 zoomed px) leaves the centre narrower. Zoom
and layout persist in localStorage behind try/catch. The free checks are
`tests/face/hud_v2_layout_check.py`, run first by `hud_v2_check.py` in a
context of its own, plus `hud/src/lib/layout.test.ts`.

**HUD installed app and orb icon (2026-10-12, design §18).** Settings offers
**Install as app** when Chrome supplies `beforeinstallprompt`; otherwise it
shows Chrome’s menu route. Standalone windows and `appinstalled` say Installed.
The install action is disabled and inert under an approval card. The manifest
is `/manifest.webmanifest`, served as `application/manifest+json` under the
one CSP’s `manifest-src 'self'`; Vite copies public icons into the installed
build’s integrity manifest. `hud/scripts/render_icons.py` copies the idle orb
without refactoring `Orb.tsx`, with simpler 16/32px rings and a safe-zone mask.
`jarvis hud` pins Windows Chrome to `JARVIS_HUD_CHROME_PROFILE` (Default),
refusing slash, backslash or `..`; the id uses Chromium’s verified double SHA-256.
Only the HUD root uses `--app-id` when its
Manifest Resources folder exists under `JARVIS_WINDOWS_LOCALAPPDATA` (else
converted LOCALAPPDATA, else johnw’s Windows Local AppData). No profile is read.

**HUD boot animation (2026-10-12, design §18; the owner: "this is literally
perfection … make sure that its disabled when automation is on").** The owner's
prototype (`prototypes/boot/index.html`) at its default settings, v2 only, no
audio: seeded binary dust drifts from the bottom left by geometric Brownian
motion and converges into the orb; two pens draw a sharp box counter-clockwise
while a pen per letter draws JARVIS, then the dots pop in. The pure half is
`hud/src/lib/boot.ts` (RNG, paths, timeline, `shouldPlay`), the drawing
`components/BootOverlay.tsx`. **It mounts on the first frame**, so the HUD is
never seen before it, and nothing waits on it: the HUD loads, connects and
renders underneath. **Never under automation**: with `navigator.webdriver` the
component is not mounted at all, unless the boot suite's own init script sets
`window.__hudBootForce = true` (which can only make it play). **Once per
window**: a sessionStorage flag (behind try/catch; without storage, once per
page load) is set when a window first considers it, so a reload never replays
it, and reduced motion never plays it. **A click skips it and clicks nothing
underneath** (the overlay eats the press, its release and the click); **a key
skips it and still does its job** (never prevented: Ctrl+D, Escape, typing,
Ctrl+`). **Approvals win**: it sits at z-index 99, below `#authveil`, never
takes focus, and is removed in the same render a card is drawn in, whether the
card was waiting when the window opened or arrives mid-play; answering the
card never restarts it. It fills the window at any zoom (`--ui-zoom` divided
out, canvases in device pixels), and its fade-out cancels the animation frame,
timers and listeners. Free checks: `lib/boot.test.ts` and
`tests/face/hud_v2_boot_check.py` (a `MockDaemon(0)` of its own, run by
`hud_v2_check`); removing the webdriver, card, click or session guard each
fails it.

**HUD title bar and split workspace, WP-A (2026-10-09, design §18; plan
`docs/plans/2026-10-09-hud-workspace-plan.md`, decisions file beside it
wins).** A 28px window-wide **title bar** holds Model · Voice · Avatar ·
Settings │ zoom │ ⊞ ◧ ⬓ ◨ (`components/Layout.tsx`, `TitleBar`). The
toggles are `aria-pressed` and follow what is **drawn**: a window-folded pane
reads closed with `data-auto`, and opening it folds the other. ⊞'s menu is
the only way to pick a preset: eight tiles, four to a row, since WP-G1
(single, cols2, rows2, cols3, main2, grid4, grid6, grid8; grid5 and grid7
come from split and × — WP-G2 — or a stored value; below).
`lib/workspace.ts` is the pure pane model: eight pane specs always stored
under `jarvis.hud.workspace` (apart from `jarvis.hud.layout`) by **id**, with
`order` the ids drawn in reading order and `kept` the ones ⊞ hid, a pane
mounted on first draw and then **hidden, never unmounted**, edges as
fractions per preset clamped to 360×200, and `fitWorkspace` (side panes fold
first against the shape's own centre minimum, then columns drop, then — down
— the panel shrinks, folds, and only then rows drop; **the focused pane is
never dropped**, nothing is stored). **A dropped set is sticky**: the last
render's panes (a render-only ref) are kept while the preset and drawn shape
are unchanged and they hold the focused pane — choosing them from focus
alone made a click into the other pane of a dropped pair redraw a different
pair under the pointer. Chat was a singleton in WP-A (choosing it in
another pane swapped the two panes' whole specs) — **WP-B lifted that**,
below; sidebar clicks go to the pane already showing that kind of thing, else
the focused one; "read" is any drawn pane. **A File pane pins its project** when it opens a file and `FileTab` is
keyed by it — it used to save to whatever project was current at save time.
An unsaved edit is never dropped silently: "follow chat" waits, a gone
project keeps the pane pinned until "discard edit", and closing the window
asks. Preview (the Browser since BR-3a, below WP-E; the File pane reads as the editor) keeps its URL per pane, re-judged on load. The bottom panel
(⬓, Ctrl+`) holds the terminals (WP-D, below). **Under
a card every title-bar, fold and rail button is disabled**, the layout menu
closes, and **the card takes focus** off a preview frame, Monaco or anything
in the workspace or panel (a framed page got its own key events, so Escape
never reached the card) — Enter on a focused toggle used to fold a pane
behind the card. Ctrl+Alt+1–8 focus the pane in that **slot** of the preset
(never id N), bringing a dropped one back. In the single layout — the default — every existing suite
passes unchanged; `_titlebar_checks`, `_workspace_checks`,
`_grid_card_checks`, `_sticky_checks`, `_card_focus_checks` and
`_seen_checks` in `hud_v2_layout_check.py` plus `workspace.test.ts` cover the
rest. **The layout section runs against a `MockDaemon` of its own** on an
ephemeral port (`MockDaemon(0)`; the mock records the port it bound), so its
saves, `/seen` posts, approval decisions and preview hits never reach the
world the main suite asserts on. **An event is read in its handler, never
inside a state updater** (2026-10-10, review of PR #29): ⊞ and ⋯ read the
click's `currentTarget` in `setX((m) => …)`, which React runs at render time
whenever an update is already pending — `currentTarget` is null by then, so
three quick clicks threw inside render and unmounted the whole window, an
open approval card included. **A render error in the title bar, the shell
or the orb no longer takes the card with it**: `WorkspaceBoundary`
(`components/Boundary.tsx`) draws a plain-text Reload prompt in their place
(it logs the error's name only, and says a reload loses unsent words and
staged files), while `ApprovalVeil` sits beside it, outside, so the card
stays up, Tab stays on it and Escape still denies; the veil makes the
prompt inert whichever came first. Its `onCrash` tells App, which aborts
every paste going out to a shell and stops the microphone (no push-to-talk,
wake word, follow-up window or dictated send), and **the terminals' hold
under a card is App's, above the boundary**, so it never freezes with the
workspace (review of PR #32). A picker or dialog that throws is closed
(`DialogBoundary`). **A throw anywhere else — App's own render, the store's
reducer, the veil itself — still unmounts the whole window, card
included.** (`_crash_checks` in the layout and terminal suites, driven by
the test-only `__hudCrashProbe`.)

**Several chats at once, WP-B (2026-10-09, design §18; plan §2.2 "Several
chats at once"; decisions W-6).** Any pane may show chat, each its **own
conversation**: its transcript, box (text and staged files), project and model
chips, Send/Steer and Stop. What was one set of window-wide fields is
`state.chats[pane]` (`lib/chats.ts`: threadId or compose, turnThreadId, busy,
status, messages, draft, ops, restore, pendingTranscript, input, files,
loadedThread), and the turn machinery — send, steer, Stop, give-back,
held-back words, the 15 s reconcile — is the old code keyed by pane, so the
single layout (pane 1) behaves as before, **with one deliberate change: an
unsent draft belongs to its conversation.** Opening another thread, or New
thread from a thread, parks the words typed (main left them in the box), and
they come back when that conversation is reopened; New thread while composing
still keeps them. There is no sidebar marker for a parked draft yet.
**What a conversation owns moves
with the conversation, never with the pane** (PR #27 review): a pane that
trades conversations takes its box text, files and REVIEW transcript along
(`chat_swap`); a pane that changes thread parks its unsent text and files
under that conversation (`drafts`, `draftKey`) and gets back what the new one
had; files read in for a box (a drop, the picker, a paste) are added, when
ready, to whichever pane holds the conversation they were dropped for, else
to its parked draft (`stage`) — never to whatever the pane shows by then;
`send()` re-finds its pane by conversation after every await and patches
nothing if none still shows it (which also fixed New thread pressed while a
compose send was in flight, in one pane or two); the transcript loader patches
whichever pane shows that thread when it arrives, drops it if none, and a pane
counts as loaded (`loadedThread`) only once it has; a hand-back goes to a
drawn pane showing its thread, else is held (`HeldBack`) and comes back when
one does — never into another conversation. A failed first send whose
compose row no pane holds any more (another thread was opened there
meanwhile) parks its words under that row, for the next new thread in that
pane, and says so; a hand-back never falls back to the selected chat. Until
its thread exists a first send's turn is tracked under a placeholder
(`local-N`) that **never reaches the daemon**: a Stop or orb press then is
held and interrupts the thread once the message is in. A model change whose
answer lands late updates the compose row holding that thread *then*, so New
thread meanwhile stays a new thread. **An SSE event with a
`thread_id` goes to the pane showing that thread or tracking its turn**
(`routeEvent`); approvals, activity and lifecycle stay window-wide.
**Ambiguous input goes to the selected chat** (the owner's rule, W-6): the
chat pane most recently clicked — a pointer or keyboard focus into it,
Ctrl+Alt+N, a sidebar click that opens a thread there. It is kept as an order
(`selectedOrder`, `selectedChatOf`), so a chat pane the layout drops gives way
to the one selected before it; clicking a Preview (Browser since BR-3a), File (editor) or other pane never
changes it; one chat drawn is the selected one, and with none drawn there is
none. Ambiguous means: a dictated transcript (AUTO sends, REVIEW fills the
box — **to the selected chat as it is when the transcript lands**, so
clicking another chat during STT sends it there), the orb's press and
interrupt, push-to-talk, the follow-up window, files dropped on the window
outside any pane (on the sidebar, a project row included, or a Preview or File
pane), and an SSE event with no thread. Input that belongs to a
thread (typed text, queued and steered messages, hand-backs, a send's own
result, a transcript load) stays with its thread, and a hand-back whose thread
is off screen is held, never put in the selected chat. With no chat drawn,
speech waits unsent in the box of the next chat selected, and a press or a
dropped file is refused with a sentence: nothing is ever sent. The selected
chat's status is the line under the orb (since PR #29 the dictation strip —
AUTO/REVIEW/OFF and the meter — is on the orb, acting for the selected chat;
every other chat pane shows only its own turn's status, `pane-status`), it carries
`data-selected` and, in a split, an accent bar down its header's edge and a mic
mark (`pane-N-mic`); **the orb follows and interrupts only its turn**, the
follow-up window opens only when its turn ends, **only its turn suppresses
the mic** — a long turn in another pane no longer silences it — and a capture
status ("LISTENING", "TRANSCRIBING", "STT FAILED" …) moves with the selection
instead of going stale in a pane that is no longer selected. The store's
`orb` is window-wide, but a pane's turn event moves it only for the selected
chat (`select` repaints it from the new one's turn). **A thread is open in one
pane at most**: a click on a thread a drawn pane shows focuses that pane; one
held off screen (a hidden pane, or a pane showing another view) trades
conversations with the pane it opens in (`chat_swap`), so its turn and
hand-backs move with it; a pane that moved on mid-turn hands that turn, Stop
and all, to the pane that opens the thread (`open_thread`) — and if that pane
is itself waiting on another turn, **the two trade tracked turns**, so neither
is left without a Stop. Click rule (`chatTarget`): the pane showing it, else
the focused chat pane, else the selected chat, else the focused pane switches
to chat. **Alt+click or ⋯ → Open beside** opens the next pane to the right
(single → two columns, the right of two → three). The sidebar marks every
drawn chat pane's thread — the selected one `sel`, others `panesel`
(dimmer), each row `data-pane` — plus the selected chat's conversation even
off screen (as the one conversation was), and numbers compose rows by pane
when more than one is marked. A pane that shows chat for the first time opens
a new thread in the last project worked in. "Read" covers every drawn chat
pane's thread. **A File pane holding an unsaved edit is never switched
away** — its own tabs, Open beside, a sidebar thread or task, New thread:
refused, and the refusal shows where the owner clicked (`pane-N-refused` on
the refusing pane, or on the focused pane naming it when it is not drawn).
**While a card is up, everything outside it is `inert`** (`Approvals.tsx` sets
it on every sibling of `#authveil` and takes it off before focus goes back),
and Tab/Shift+Tab stay on the card: the card takes focus off anything outside
it (`behindTheCard`), but Tab used to walk focus back behind the veil, where
Enter pressed things. **AUTHORIZE and ALWAYS are `tabIndex=-1`**, so no Tab
or Shift+Tab ever puts one under an Enter — DENY is the only stop (they stay
clickable; the coordinator's default, told to the owner). The chat box is not
`disabled` under a card (inert covers it): a disabled box dropped focus to
the page before the card could record it, so focus never came back. **Each
card is keyed by its request**: a queued card used to inherit the answered
one's busy state and countdown, its buttons dead (main too), and a new top
card takes the focus. Two latent single-layout quirks were
fixed in passing: a pane's project chip and profile select now show *its*
conversation's project even after a task was picked, and an archived
project's *task* no longer clears an unrelated chat's transcript.
`window.__hud.state()` flattens the selected chat's conversation (else the
one selected last) over the window's fields (the single-layout suites read
those; `chats` has every pane) and `__hud.dispatch` routes a legacy `patch`
of conversation fields there. Free checks: `lib/chats.test.ts`, the per-pane
cases in `store.test.ts` and `compose.test.ts`, and
`tests/face/hud_v2_multichat_check.py` (run by `hud_v2_check` after the
layout section, on a `MockDaemon(0)` of its own, or alone) — including one
`review:` check per PR #27 finding and one `W-6:` check per case of the rule,
each shown to fail on the commit before the fixes (dbd2096), and `review 2:`
checks for the re-review (failing on feb61ff), each naming the mutation it
kills. **Typing re-renders the window** (the box's words are the store's), so
`ChatTab` is memoized with stable props: a long transcript redrawn per
keystroke cost milliseconds per character per chat pane.

**HUD terminals, panel and view, WP-D (2026-10-09; plan §2.3–§2.4,
decisions W-1/W-2/W-5; contract in `docs/hud-api.md`).** xterm.js pinned
exactly (`@xterm/xterm` 6.0.0, `addon-fit` 0.11.0, `addon-web-links` 0.12.0)
and lazy-loaded (`lib/xterm.ts`); **no clipboard addon** (no OSC 52), window
reports off. `lib/terminal.ts` holds the rules (pure, `terminal.test.ts`),
`components/Terminal.tsx` the sockets and views. **One session per terminal
per window**: made the first time the window draws it, it attaches with a
**fresh ticket** and a URL from `location` (never a port), resizes only after
`replayed`, and **stays attached while hidden**; its xterm element is moved,
not rebuilt, between the panel and a pane. **A replay is never answered**
(PR #28 review): xterm answers some output — DA, a cursor-position report,
an OSC 11 colour, DECRQSS — through the owner's own input channel, so a ring
holding old queries used to type their answers into the program on every
reload or dropped socket (50,000 `ESC[6n` → 300 KB in 50k frames, past the
daemon's 256 KiB latch). Output now goes through `OutputPipe`: one write in
xterm's hands at a time, **tagged with its socket's generation**, so an older
socket's queued output is never parsed into a new session; the reset is an
in-band RIS (a JS `reset()` lets what xterm already holds be drawn again
after it); and **nothing is sent from a new socket until its replay has been
parsed** — the flag clears from the pipe's step after the replay, never on
the `replayed` message itself. **The pipe's backlog is bounded** (re-review):
output faster than xterm parses (~14 MB/s) used to queue without limit (a
reviewer reached ~1.5 GB, the display ~40 s behind, once xterm's own 50 MB
refusal no longer applied); past `OUTPUT_HIGH_WATER` (8 MiB) the backlog is
dropped, that socket's output ends and the session reattaches, so the replay
shows the latest 1 MiB, with a notice that it skipped ahead. A `term.write`
that throws clears the in-flight flag in a `finally`, so it cannot stall the
pipe. **A terminal another window shows is never
taken unasked**: listed `shown` and not one this tab has shown (a
sessionStorage list, `jarvis.hud.terminals.mine`; `taken`/`refused` forget
an id), it is drawn as "in another window · Show it here" — a takeover
question the owner did not cause is one they learn to wave through. **A
copy of the list is not the list** (re-review): Chrome copies sessionStorage
into a duplicated tab and a reopened closed one, so the list carries its
`holder`, the live page's per-load nonce (memory only), which that page's
`pagehide` sets to null; a new page inherits the list only if the holder is
null **and** its navigation type is `reload` — a duplicate's copy is held by
its live original, a reopened tab's is released but a restore. A reload
still attaches straight back even while the daemon counts the old page's
socket. A first attach reads a fresh listing first, and so does ×
before deciding whether to ask (`freshList`: never a listing already on its
way, which may predate `busy`; a failed listing asks, saying it could not
check). **One terminal is drawn in one
place** (`placeTerminals`): a drawn pane holding it wins and its panel tab
reads "in pane N" and jumps there; a hidden panel or undrawn pane attaches
nothing (a reload must not ask another window for a terminal nobody here
sees). Panel: a tab per terminal, `+` (the focused pane's folder **as an
id**, `terminalSpecFor`; since WP-B a focused chat pane gives its *own*
conversation, and any other pane defers to `activeChat` — the selected chat,
else the one selected last — the chat File and Preview panes already follow,
not W-6's input rule, which is about where words go), `▾` (Home or a project), × per tab; **every
way the owner opens the panel — Ctrl+`, ⬓, the layout menu's Panel row —
opens a terminal when there is none, and a pane the owner switches to the
terminal view opens one there** (the owner's request, 2026-10-11: "when I
open the terminal pane it needs to auto open a terminal instead of having me
press another button"; WP-D had it on Ctrl+` only, ⬓ never). One path,
`LayoutControl.ownerTogglePanel` → `TermManager.ownerOpened(where, still)`;
`togglePanel` stays the plain flip a stored layout, a reload and
`showPanel` use, and opens nothing. Only on that gesture — never a load, a
restored open panel, a reload or a reattach; never under a card (every
opener is disabled, and any card raised since the gesture drops it, answered
or not — a card count, review of #41); only on a listing asked for then that
came back (never on `listError`); never when any terminal is listed, another
window's or an exited one included; never if a terminal was opened meanwhile,
the panel was closed again or the pane switched away (`still`) — asked again
once the daemon has made it, and if it changed meanwhile the terminal stays
in the list and nothing opens, reopens or moves; one at a time, and on ⬓ and
the menu row only a click's first counts (a double-click is one gesture and
opens one, as Ctrl+` ignores auto-repeat); a refusal is said in the panel, as
`+` says it. While it is on its way the panel and the
pane say "Opening a terminal…" instead of offering a second button. With
terminals open, a pane switched to the terminal view keeps its picker, "+ New
terminal here" first. It needed the owner-only read fix above to work on the
real daemon at all. A dot on ⬓ means one in the hidden panel exited. In a terminal **Ctrl+B, Ctrl+Alt+B and Ctrl+_ are the
shell's** (`terminalTakesKey`), and so is **Ctrl+D** (end of input, never
push-to-talk: `talkKeyAction`), the zoom keys, Ctrl+` and Ctrl+Alt+N stay the
HUD's, and every key stops at the terminal.
**Under a card nothing reaches the shell**: `disableStdin`, a key filter
that lets every key bubble (Escape denies), a guard on the bytes, a paste in
flight waits, and the card takes focus (WP-A). **Pastes**: 16 KiB chunks
paced 8 ms from a queue that belongs to **one socket** (`InputGate`);
`input_dropped` throws the rest away at once and typing waits for the
owner's **Resume typing** (`input_resume`, retried while refused, with
Reattach/close as the way out — Ctrl-C is not); a late in-flight
`input_dropped` adds to the notice and never undoes a resume; **a socket
that closes takes its paste with it**, never continued on the next.
**Every paste is inert as a control stream**: a capture-phase listener on the
host takes it before xterm, strips ESC and C1 (`cleanPaste`), then
`term.paste()`s it — a pasted `ESC[201~` used to end bracketed paste early
and run the rest. Keys or a paste dropped while connecting (or not running
here) are said, never swallowed (an `unsent` notice; only the owner's own
gestures count, never xterm's answers).
Takeover asks here (Let it / Keep it, disabled under a card — unanswered is
kept); "taken", "refused", "exited (code N)" with Restart/Close, "ended"
with New terminal here (the spec it was opened with, remembered under
`jarvis.hud.terminals`); a `busy` terminal is asked about before it closes.
A `terminal_attached` not matched to one of the window's own sockets
(`AttachLedger`) is a quiet, dismissible notice. Each terminal's bar has the
"Jarvis can read" switch (owner-only PATCH) and an integration note (`none`,
or `inactive` when a configured shell has not `marked` 4 s after attaching).
**An OSC title is text** in the pane header and the bar, capped at 80 —
**never `document.title`**; links open only for http(s), only on Ctrl+click
(`judgeLink`); "Open in Preview" for loopback links is WP-E's. **xterm under
CSS zoom**: the host is counter-zoomed (`zoom: 1/level`) and the font scaled
by the zoom, so cells and the pointer are measured unzoomed. Free checks:
`terminal.test.ts`, `workspace.test.ts`, and
`tests/face/hud_v2_terminal_check.py` — the PTY is **played in the browser
by `route_web_socket`** (`FakePty`; no shell, no HOME) against a
`MockDaemon(0)` of its own, the `/terminals` routes in
`hud_v2_mock_terminals.py`; `hud_v2_layout_check`'s 2×2 grid now holds a
terminal under the card. **`hud_v2_check.guard_live` refuses HTTP and
WebSockets to 8402/8403/8405** (`ctx.route` never sees a socket), and the
terminal suite proves the socket half against a port of its own. Two
Playwright facts that cost a debugging round: sync route handlers run only
during a Playwright call, so a wait on the fake's own state must pump
(`pumping`); and a socket must not be closed from inside its own message
handler. **Verified to bite**, each mutation in a scratch copy: the key
filter's card hold alone (Escape stops denying), every hold layer (a typed
`y⏎` and a pasted `rm -rf` reach the shell), the paste abort (128 of 128
chunks sent), a queue that outlives its socket (the rest of the paste goes
out ahead of the next keystroke — the direct check only bit once it typed
on the new socket), the guard's socket half, and the three backend fixes.
The review round added, each also shown to bite in a scratch copy: the
pump's card hold (chunks left while a card was up), a late `input_dropped`
undoing a resume, input during the replay (a reload, a drop and a 50k-query
ring each typed answers), clearing the replay flag on `replayed` rather than
after the parse, the pipe keeping an older socket's queue (vitest), the paste
listener and `cleanPaste`, "in another window", ×'s fresh listing, a card
that queues rather than drops, the `unsent` notice, and the tab's own set.
The re-review's three, likewise: no backlog cap (a 40 MiB flood never
reattached, the backlog unbounded), no reattach on overflow, a copied list
trusted (a duplicated tab opened a socket), the navigation test or the
`pagehide` release dropped, and the `finally` (vitest: the pipe stalls).
The test-only write hook `__hudTerminals.paste` is gone; the hooks left are
read-only (`backlog` among them).
While typing is paused the notice cannot be dismissed: it holds the only
way to resume.
**A terminal that ended with Jarvis continues where it was** (2026-10-12,
the owner's option (a); no route changed). A typing key there (printable,
Enter, Backspace, Tab, an arrow — `lib/terminal.ts` `continuesOn`), a paste
or **Continue here** is one `POST /terminals` with its remembered `in` and
its switch (`readableCarry`: off when unknown, so never wider; a session's
`lastRow` now follows every listing and `PATCH`). `TermManager.rekey` moves
the session to the new id everywhere — panel tab position (first-seen
`ranks`), pane (`renameTerminal`, every pane holding it), selected tab,
remembered spec, `mine` — with `aliases` for the render a pane still holds
the old id, and the panel's view keyed by its first id (`viewKey`), so the
**same xterm** stays mounted and focused. The old text is rebuilt from
**xterm's cells** (`snapshotText`: characters and SGR only, wrapped lines
joined, each row starting and ending plain; **walked from the bottom and
stopped at 1 MiB** — built top down a full scrollback took 1.9 s on the
keydown — and one logical line longer than that keeps its tail, cut at a
row), never the old bytes — so it holds no query, mode, alternate screen or
title — and written after the RIS of **every** attach, before the replay,
then `── restarted HH:MM ──` (plain text). Keys and pastes while it starts
are `HeldInput` (4 KiB, a paste dropped whole) and go after the replay is
parsed, **typed, never run** (the lead's decision, PR #64 review L6:
`HeldInput.release` sends keys up to the first Enter — never it nor anything
after — and no paste holding a line break; the screen showed the old
context's prompt, `pi@robot:~$` or `>>>`, and an Enter there would run the
line in a fresh local shell; the `continued` notice says check the prompt,
then press Enter, or paste it again). **Any refusal drops the whole hold**
(M1: `make deploy⏎` then Ctrl+C used to run the line): a key that does not
type, past 4 KiB, a card since the gesture (`cardCount`), a crash
(`abortPastes` sets `crashed`), or the terminal leaving the screen (L1: the
panel closed, another tab, its pane switched away — `dropHiddenHolds`), all
said as `unsent`. **Only the owner's gesture in that terminal continues it**
(`mayContinue`: ended, a shell, in this tab's `mine`, a **remembered `in`**
— never the row's project, which is not a task's worktree (N1) — not
crashed; never under a card, on a load, a restored panel or a reattach, for
another window's, an exited shell or a program terminal; Ctrl+D and HUD
keys never). After a refused continuation (six open) typing asks no more;
only Continue here tries again (L5). **Memory only: a page reload loses the
old text.** Running programs do not survive; real survival (a pty host)
would reverse W-3. **Limit (L4, predates this)**: the switch comes from this
window's last-seen row, which another window's change can leave stale; a
full fix needs a daemon event. Free checks: `lib/terminalContinue.test.ts`
(with a real, unopened xterm: the rebuilt text answers nothing and leaves no
mode) and `_continue_checks` in `hud_v2_terminal_check.py`; guard removals
that bite: a paste under a card, the held keys' card count, continuing
without a gesture, the switch failing open, a raw-byte prefix written after
the replay window, the `held.closed` check (M1), and L6's stop at Enter (for
keys and for a paste).

<!-- claude-source:000998-001555:end -->

## Original CLAUDE.md lines 2492–2582

<!-- claude-source:002492-002582:begin -->
**The HUD's CSP and installed build, ED-4 (2026-10-11; editor plan §1.7,
§2.7; decision C-1; contract in `docs/hud-api.md`).** Every HUD and API
response carries the **full policy** (`hud_api.HUD_POLICY`) in WP-E's one
header: `default-src 'none'`, `script-src 'self' 'wasm-unsafe-eval'` (no
inline, no eval; WebAssembly since ED-5),
`worker-src`/`connect-src 'self'` (the same-origin `ws://` terminal socket
included, CSP3), `style-src 'self' 'unsafe-inline'`, `img-src`/`font-src
'self' data:`, `media-src 'self'`, `frame-src` on the loopback names a CSP can
express (so the Preview judge refuses `[::1]` and `127.0.0.2` with a
sentence), and `base-uri`/`form-action`/`object-src 'none'`.
`'wasm-unsafe-eval'` came with ED-5's tree-sitter (below), with the test
that loads every grammar under the policy. A response's own directives may
only **narrow** it (`_narrowed`: restate a directive the policy sets with a
subset of its sources; a `default-src 'none'` drops the fetch directives it
does not restate — the avatar keeps its old lockdown); anything else raises.
**The HUD is served from an installed copy (C-1)**, never the repo's
agent-writable `hud/dist`: `jarvis hud install` (`jarvis/v2/hud_install.py`;
a human/lead CLI step, **never a tool** — `tests/v2/hud_install_check.py`
pins that only `__main__` and `hud_api`'s read side import it) runs `npm run
build` and copies `hud/dist` into `config.HUD_DIR/<build-id>/`
(`~/.local/share/jarvis/hud/`, `JARVIS_HUD_DIR` overrides), read-only, with a
SHA-256 manifest, through a temp folder renamed into place, keeping three
builds; it refuses **uncommitted changes under `hud/`** unless
`--allow-dirty` (Jarvis's chats leave edits in the main checkout) and records
`git_head`/`dirty` in the manifest (checked again after the build; its git
runs hardened through `gitops._git`); it refuses a symlinked root or ancestor, a
root in the repo or writable by others, and a build output with a link,
special file, odd name or no `index.html`. The daemon serves the **newest
intact** build (manifest reads, `index.html` matches — a newer broken one is
passed over; a hostile manifest is ignored, never raised), only files its
manifest lists, **SHA-256 checked on every read, never cached** (a stat-keyed
cache served a same-size rewrite made within one timestamp tick), an asset
only an older kept build has served from it (so an open window's lazy chunks
survive an install); serving an older build because a newer one failed is
`hud_state: "fallback"` (with `hud_passed_over`), never silent; with nothing
intact `GET /` is a 503 saying "not installed" or "failed verification", and
`/status` reports what is served (`hud_build`, `hud_git_head`, `hud_state`).
daemon2 **warns at start-up** in dev mode, on a fallback, with nothing
served, and when `hud/` changed since the served build's commit (`git diff
--quiet <commit> HEAD -- hud`) — a forgotten install shows up in the log, a
Python-only merge does not. `config.HUD_DIST`
is that install root — **`hud/dist` only under `JARVIS_HUD_DEV=1`**, which
only tests set — and BR-0's one write check (`files._hud_roots`) refuses the
install root always, plus `hud/dist` in dev mode, to every agent write tool and
v2 grant. **Jarvis never runs the installer**: `protected_state.owner_op`
(**the table `OWNER_OPS` is the extension point** for any other owner-only
`jarvis` command; `OWNER_OP_MODULES` for its module), asked by `rules.decide`
(so v1's gate and v2's layer 1) and by `run_command`, makes `jarvis hud
install` — and since 2026-10-11 `jarvis hud rebuild`, `jarvis restart` and
`jarvis daemon2 restart` (below) — DENY, judged on the **command word** after wrappers and runners
(`env -S` here, and since 2026-10-12 everything in rules' one table through
`rules.starts`: `nohup`, `timeout`, `flock`, `systemd-run`, `uv run`, `npx`,
`npm exec`/`-c`, `pnpm`, `yarn`, `bun x`/`exec`, `ssh`, `xargs` with input
that may complete it — every reading of an option the table does not know),
`python -m jarvis`, a line's
own `$VAR` or `ln -s` alias, the installer module, Python source that
*invokes* it, and shells/`eval`/here-documents/substitutions bash would run
— never on a line that only mentions it (`echo`, `grep`, a commit message,
single quotes, a quoted here-document): a DENY cannot be approved, so a false
one blocks the owner. A jarvis argv word only known at run time (an unset
`$E`, `$@`, a glob, `{hud,install}`) counts as any words, so a line that
could expand to an op is refused; and **Jarvis's own CLI is ASK in every
spelling** (`python -m jarvis…`, `uv run jarvis`, the venv's python —
`protected_state.runs_jarvis`, asked by `rules.decide`), never auto-approved. A cheap line and per-segment filter (`_could_be_owner_op`)
keeps `rules.decide` at its old speed on lines that cannot hold an op, held
to the full parse by `hud_install_check`. An agent can edit `hud/src`, so
running the installer would undo C-1. A shell write *into* the install root
asks (`protected_state.hud_trees`). A shell can still
write it in a form nobody sees (the accepted gap). **After a merge, the lead
runs, from a synced, clean main checkout, `jarvis hud rebuild` — adding
`--restart` only if Python changed; a HUD-only change needs just a reload of
the window (Ctrl+R)** — and `jarvis restart` alone when only Python changed.
`rebuild` runs `npm ci` first when `package-lock.json` moved, refuses
uncommitted `hud/` changes (commit or stash them; `--allow-dirty` only on
purpose), installs, and checks `/status` shows the new `hud_build`,
`hud_git_head` equal to the checkout's HEAD and `hud_state: "installed"` (not
`"fallback"`), exiting 4 if not; `--pull` fast-forwards main first. (`jarvis
hud install` and `systemctl --user restart jarvis-daemon2` still work; the
new commands do the checks those leave to the reader.)
Never `jarvis hud` to restart. **Tests point
`config.HUD_DIR`/`HUD_DIST` at temp dirs** (or set `HUD_DEV` with a temp
dist); never install into the owner's real root. **Every context of every
headless HUD suite runs under `hud_v2_check.watch_csp`**: a CSP violation
anywhere fails the run, because a CSP breaks quietly (Monaco even falls back
to a textarea). `tests/face/hud_v2_csp_check.py` (run by `hud_v2_check` after
the browser section, on a `MockDaemon(0)` of its own) proves the HUD's needs
work under it — SSE, the terminal socket, Monaco and its worker, the avatar,
the mic and `/stt`, a loopback frame — and that an inline or remote script,
`eval`, a `blob:` worker, WebAssembly, a cross-origin fetch, socket, image,
object, `<base>`, form or non-loopback frame is refused with a violation.

<!-- claude-source:002492-002582:end -->

## Original CLAUDE.md lines 2844–2954

<!-- claude-source:002844-002954:begin -->
**Input bar declutter (2026-10-10, PR #29; ported onto WP-B and reviewed the
same day).** The bar is `📎 [box] ⬆` over one row, `in: project · provider ▾ ·
model · effort ▾`. **The mic is on the orb, not in any bar**: the dictation
mode (AUTO/REVIEW/OFF, testids `dictation-*`) and the level meter sit in a
strip above the orb (`#micstrip`, inside `#orbdock`), acting for the selected
chat (W-6), and **the line under the orb is the selected chat's status**
(`orb-status`, lib/dictation `orbLine`: the turn's status, else ANSWER THE
AUTHORIZATION under a card, else MIC MUTED, else the orb state — only OFF ever
says MUTED; up to three lines, a little wider than the orb, so nothing is cut
off). Every other chat pane keeps its own `pane-status` line (an empty one
takes no room); the selected chat's bar draws one only while the sidebar is
folded, when the orb is a 36px dot with no line under it. The strip folds into
one button, `#modecycle` (`dictation-cycle`), that cycles **OFF → REVIEW →
AUTO → OFF** (`nextMode`: one click from muted never lands on a mic that
sends on its own) and goes red at OFF — above the mini orb when the sidebar is
folded, and beside the full orb in a short window (under `MIC_TIGHT_H` = 560
zoomed px, `#shell.mic-tight`, which gives the sidebar the strip's 44px back
for its rows). **Under a card the mode buttons are disabled as well as
inert**, and the card takes focus off them: focus left on the cycle button
used to let Enter switch OFF to AUTO behind the card. **Model and effort are
one button per chat pane** (`model-chip-btn`, carrying `data-model` and
`data-effort`) opening one popover (`ModelChip.tsx` `Popover`, portaled to
`#root`; `data-pane` says whose) with a Model section, the effort slider
(below) and "Set
<provider> default…"; the provider stays a select. It is placed by
`placePopover` through `toCss` — upward, downward only when there is too little
room above and more below, never taller than the room it opens into — and
closes on Escape (focus back on the button; **under a card it never swallows
Escape**, which denies), a click outside, focus leaving it, a resize, a zoom
change, or its button moving (a rAF watch: a fold, a split, a re-layout). It
opens on the chosen option, the arrows move within a list, Tab goes round its
stops and never leaves it, its keys stop there, and its
listeners are set once (the close is read through a ref). The box grows to the
least of 15 lines (`MAX_LINES`), 35% of its pane (`MAX_PANE_SHARE`), and what
the pane has left once its fixed parts (header, ticker, the rest of the bar,
measured with the box at one line) and **the conversation's minimum** —
`MIN_LOG_PX` 96 zoomed px or `MIN_LOG_SHARE` 30% of the pane, whichever is
more — are taken out, but never under two lines (`MIN_LINES`): a pane that
cannot fit the minimum gets a two-line box that scrolls. It is re-measured when
the pane's height changes (a split, the zoom, the bottom panel), not only its
width, and when the bar's own parts change. Fifteen lines in a 2×2 grid, two
rows or a small window at 160% pushed Send and the chips out of the pane and
left the conversation 32px, and after #28 the panel left a pane at its 200px
minimum where 35% alone still left 36px. Send and Attach are
SVG icon buttons (testids unchanged), Stop a square, and Steer an outlined bent
arrow (`.steer`, `data-steer`) — it must not look like Send. Tests drive the
popover through helpers at the top of `hud_v2_check.py` (`pick_model`,
`pick_effort`, `chip_model`… — each takes `pane=1`; unscoped, Playwright's
strict mode fails on several chips at once); a read made while a dialog's veil
is up must close the dialog first, because the popover cannot be opened under
a veil. Free checks: `tests/face/hud_v2_declutter_check.py` — one `review #29
Fn` check per finding of the review, run by `hud_v2_check` after the multichat
section on a `MockDaemon(0)` of its own, or alone — plus `ModelChip.test.ts`
(`placePopover`) and `dictation.test.ts` (`orbLine`, `nextMode`). The layout
section's `_edit` now types until the pane's Save is enabled: it used to type
into an editor not yet showing the file, and four "unsaved edit" checks failed
under load.

**The thinking line, WP-H (2026-10-10; plan
`docs/plans/2026-10-10-hud-split-panes-plan.md` §4, decisions S-4; design
§18; contract in `docs/hud-api.md`).** While a chat's turn runs, its foot —
`[data-testid=thinking]`, right after the log's scroller and before the ticker,
**outside `[data-testid=log]`** — reads mini orb · word · elapsed (`12s`,
`3m 07s`, `1h 02m`). The word is a pure precedence table (`lib/thinking.ts`
`thinkingLine`): "Error" (an `error` in the running turn) · "Ended on an error"
(static, no time, until the next turn or send) · "Waiting for approval" (an
approval with this `thread_id`) · "Waiting for your answer" (`needs_input`,
no approval) · "Running <tool>…" (an unfinished call **this turn** made in this
pane, name cleaned and capped at 40) · "Thinking…". **The turn is keyed by
thread, window-wide** (`State.turns`, H-1): `turn_started`, `error`,
`turn_finished` (`stop`, `next`), this window's sends (a steer leaves the
running turn's time alone; a failed send takes back only the entry it made)
and the activity records feed it, so it follows its thread across trades of
conversations, Discord-started turns and reloads. **Only a change of a
thread's activity counts, and "working" reaches the line only through the
entry it makes**: the record that turns a thread idle follows its
`turn_finished`, so a view still saying "working" must never outlive a finish
already heard. Into working/needs_input with no entry (or an ended one) is a
turn noticed late, timed from the record's **`running_since`** (H-b — the
daemon's live, never-stored field, present only while `running`) else from now
and corrected when the records arrive; into failed makes it ended; into
idle/unread deletes it — never one a send in flight made, never an ended one.
**A running turn no pane tracks is adopted** (H-3) by the drawn chat pane
showing it when that pane waits on nothing, so Stop and Enter-to-steer return
after a reload — only with a live entry *and* "working", so a pane a finish
just freed is never adopted back; the 15 s reconcile undoes a wrong one. It
never meets WP-G2's `State.loose`: a loose turn's pane closed, so no pane shows
the thread, and `open_thread` takes the loose turn up before the adopt effect
looks (it adopts only a turn no pane tracks).
**The mini orb is an 18px inline SVG, never a canvas** (`components/MiniOrb.tsx`,
H-2): CSS `transform` keyframes only, no requestAnimationFrame, `aria-hidden`;
the word is the `role="status"`, the time is not; reduced motion stops every
animation and leaves colour, word and time. **The palette is one module both
orbs import** (`lib/orbpalette.ts`; a grep test refuses a second `palette()`,
`ACCENTABLE` or amber/red hue in any source): the avatar's accent recolours
"Thinking…" only, and an accent that is not `r,g,b` is ignored. The times
share **one 1 s clock** that runs only while a drawn line shows one, and only
those spans redraw on a tick — never the window or a transcript; each pane's
line is handed to the memoized `ChatTab` as the same object while it says the
same thing. The line is in flow, no z-index, no button or link,
`pointer-events: none`, inert under a card, and never says Authorize, Approve or
Deny. The input bar's status words stay (the suites read them; removing the
duplicate is a follow-up), and **the input bar re-measures its box's cap when
the line comes and goes** (`footLine`), or a long draft held at the
conversation's minimum would let the line take the log below it. Free checks:
`thinking.test.ts`, `orbpalette.test.ts`, the `turns` cases in
`store.test.ts`, `hud_backend_check`'s `running_since` case, and the
`_thinking_*` sections of `tests/face/hud_v2_multichat_check.py` (the mock's
`thread_running(tid, since)` sets `running`/`running_since`), every mutation
named in the plan's table shown to bite in a scratch copy.

<!-- claude-source:002844-002954:end -->

## Original CLAUDE.md lines 4279–4659

<!-- claude-source:004279-004659:begin -->
**The effort is a slider, not pills (2026-10-10, owner's ask: "like Claude's").**
`components/EffortSlider.tsx`, rules in `lib/effortSlider.ts`: "Effort
<level> · default (?)", Faster … Smarter, a dot per stop, a tick with
**Default** under the default stop, a warm-white thumb. **The stops are exactly
the levels the pills offered** (the effective model's ladder, or the provider's
words when it is unknown — `ultra` is a stop on Codex only, and no ladder means
no slider), sorted Faster → Smarter; `xhigh` reads "Extra". **The default stop
means "follow the default", as in Claude's app**: the pills' `default · high`
is not a stop of its own but the stop it names, and **choosing it clears the
effort to null** — so a thread on that stop follows whatever the default
becomes, never a pinned copy of today's level (A4/A6 unchanged: `applyEffort`
+ `patchBody`, a default thread stays unpinned); a default that names no level
becomes a leading "Default" stop. **One change, one request**: a drag previews
and commits on release, a click commits at once, arrows/Home/End commit once
350 ms after the last key (`SETTLE_MS`) or at once on blur or when the popover
closes, and a commit that lands where the thread already is sends nothing.
**A move made before a card is committed as the popover closes; keys under
the card change nothing**: a card closes the popover (and disables its
button), and a key move still settling then is committed like any other close
— the owner made it before the card — while every key pressed under the card
reaches the card (inert behind it, the slider gone), so Escape denies. **A
move made before a crash is dropped**, though — the one exception: once the
workspace's boundary has caught a crash (PR #32's `onCrash`, `crashedRef`),
nothing is sent on the owner's behalf, so `change()` drops whatever the
slider's unmount commits as the crash tears it down.
**A move belongs to the conversation it was made on** (review of PR #30): the
chip passes its key (`keyOf(paneChips(…))`: the thread, or **that compose
row** — every compose row has its own `Compose.id`, minted by the store's
`chat` reducer for a row that arrives without one, `composeSeq` keeping the
reducer pure; a row spread from another keeps it, so New thread or a re-aim
makes a new conversation even in the same project, where every row used to
key as `"|true"`) as the slider's `conversation`; when the pane moves on with
a move in hand — another thread opened there, the thread or its project
archived from another window and the pane re-aimed at a new compose row — the
slider drops the move and stops drawing what it sent, and `change(pane, next,
from)` drops a commit whose pane shows another conversation by then. That
second guard is the only one when the conversation changes and a card comes
up in the same render (the slider unmounts without ever seeing the change),
and a check pins it.
Keying the slider on the thread instead would pair the old render's choice
with the new pane in its unmount commit, and `patchBody` could add a `model`
key. A move whose stop the model no longer offers (its model changed under it,
from another window) is dropped too — without that, it cleared the effort to null.
**An older answer never undoes a newer one**: a sequence number in the slider
(what it draws), and per thread in `ThreadModelControls.change` a success is
written (the record and the opened compose row holding the thread) unless a
later change's success already was — so a late success whose newer change was
refused still lands, which an opened row (a failed first send, no record, no
`thread_updated`) needs; only the latest change's refusal is shown. The SSE
`thread_updated` still carries every change in the daemon's order. Pointer
math goes through `toCss` against the rail's own `offsetWidth`. Keys stop at
the slider (Space is never push-to-talk) as well as at the popover — **except
Tab**, which goes on to the popover's round (#29's review): the model list's
chosen option → the slider → "Set default…" (when there is one) → round. The
popover now stays open while the slider moves. **It lives in #29's reworked
popover** (merged 2026-10-10): one per chat pane (`data-pane`), and a pick
PATCHes only that pane's thread; under a card the popover closes and its
button is disabled, and the slider's own `disabled` (out of the Tab order,
`aria-disabled`, no key or pointer moves it) holds wherever it is mounted. The
Model picker's per-model `<select>` and the provider-default dialog's
`pd-effort` are still selects. Free checks: `effortSlider.test.ts`,
`EffortSlider.test.ts` (jsdom: keys, Tab — heard by a React handler around the
slider, since a listener on the root container hears keys React already
stopped — disabled, settle, unmount commit, the sequence guard, a move dropped
when the conversation or the model changes), `effort_slider_checks` in
`hud_v2_check.py` (drives it like the owner, at 70% and 160%, and Escape under
a card; the mock's `patch_delays` holds a PATCH's answer to make one arrive
late), and `tests/face/hud_v2_effort_check.py` (the review's findings, a
`MockDaemon(0)` of its own, run by `hud_v2_check` after the declutter section
or alone: Tab on an OpenRouter popover with no footer, a pane switch, a thread
archive, a project archive (OpenRouter and Claude rows), a fresh row in the
same project, and a swap with a card in one render, each with a move settling;
a model change under one; an opened row's late success; a workspace crash
with a move settling) and `store.test.ts`
(a compose row's id). A late HTTP success can still overwrite a newer state
another window's SSE brought (main does too). The backend's
`set_thread_model` does set `thread.updated`, so comparing it would work
against the daemon; it is left open because the mock never bumps it, so no
check could show the comparison doing anything. **Breaking the close-under-a-card alone bit no check at first**
(shown in a scratch copy at the merge): the card taking focus closes the
popover anyway, by #29's focus-leaving rule. So a card also arrives with focus
on the chip's own button and the popover open — the one place only the disable
closes it — and that check fails without it; inert still keeps every key off
the slider. #29's own checks are ported to it: `hud_v2_declutter_check`'s F4
(Tab round, arrows) and F6 (a card with focus on the slider), and
`hud_v2_multichat_check`'s late-answer writeback, which clicks a stop.

**Up to eight panes, WP-G1 (2026-10-10, design §18; plan
`docs/plans/2026-10-10-hud-split-panes-plan.md` §3.1–§3.3, §3.7–§3.8,
decisions file beside it wins: S-1 5 = 3 over 2, 6 = 3×2, 7 = 4 over 3, 8 =
4×2; S-3 three is three columns).** The owner: "I'm not seeing the 6 and 8
panel layouts." ⊞ now offers 3×2 and 4×2 directly (eight tiles, four to a
row, the head "Layout · N panes"; it opens on the chosen tile, else the first
tile — never on a toggle row, where Enter folded the sidebar with 3 over 2
current, review of PR #37); 3 over 2 and 4 over 3 exist as presets
(`LADDER` is the shape per count for WP-G2's split and ×, below) and draw
from a stored value. Every pane can show chat, task,
file, diff, preview or a terminal; panes 5–8 default to chat. **A pane's id
is not its slot** (plan G-1): `pane-<id>`, its React key (`${id}.${incarnation}`
— the incarnation is render-only, bumped by WP-G2's close),
`chats[id]` and the dirty flag are by id for life; `ws.order` lists the ids
drawn in reading order, and **every number the owner reads is the slot**
(`slotOf`): "New thread · N", the terminal panel's "in pane N" (the manager's
`slotOf`, fed by the workspace), the dirty-refusal chip, aria labels, each
section's `data-slot`, and Ctrl+Alt+N (`paneAt`). A stored WP-A/WP-B value has
no `order`, so it reads as ids 1…n — **the same fit as before**, held to a
golden file (`workspace.legacy-fits.json`, 300 fits produced by main's
`workspace.ts`). ⊞ to fewer panes moves the rest to `kept` (most recently
hidden first); more panes take `kept` back first, then the lowest free ids,
so 8 → 1 → 8 restores every pane in its place (plan G-2: ⊞ hides, never
closes). `ShapeSpec.rows` (panes per row; null for main2) replaced the static
grid areas: `cellsOf`/`gridTemplate` lay each row on the union of every row's
edges, so 3 over 2 and 4 over 3 have **bottom-row edges of their own**
(`Split.bottom`, separators `split-colb-N`, the top row's `split-col-N`
spanning the top row only), and `slotWidths` gives each slot its own row's
width for `narrow`/`compact`. The drops are generic over rows: a column drop
keeps, in every row, the window as wide as the widest row less one — the
focused pane's row the leftmost window holding it, the others the same start,
clamped — and a row drop keeps the focused pane's row (a four-wide one also
losing a column, so there is no drawn-only cols4); main2 keeps its own cases,
and WP-A's drop expectations hold unchanged. **A sticky set is kept only if
these panes in this order can draw it** (the same drops from some focused pane
give exactly it), so a reordered workspace is never drawn from a stale set.
Eight panes need ~1512px at 100% (4 × 360 plus two rails): 1920×1080 draws all
eight with the side panes squeezed to 480; 1280×800 draws 6 of 8 on both
rails; 1280×800 at 150% draws 4. Free checks: `workspace.test.ts` (the golden
file, field-by-field parse fallback, all ten grids, the generic drops plus a
never-drop-the-focused-pane property over every shape, focus, order and size,
the plan's window table, ⊞ 8 → 1 → 8), `layout.test.ts` (focus5…8, AltGr
symbols), `chats.test.ts`, `hud_v2_layout_check._eight_checks` and
`hud_v2_multichat_check._eight_chats_checks`; each check the plan names a
mutation for was shown to fail with it applied in a scratch copy, and so were
the review's slot-label checks (a stored order `[1, 5, 2]`, so slot ≠ id, for
both "shown in pane N" notices and the refusal chip).

**Push-to-talk is Ctrl+D (2026-10-10, design §18; the owner: "change it to
Ctrl + D just like it is in claude").** Held to talk, let go — D or Ctrl, in
either order — to send; **Space is no longer push-to-talk anywhere** (it
types a space, presses a focused button, does nothing on a separator — a
space typed in Monaco used to start the mic and never appear). The rules are
`hud/src/lib/talkkey.ts` (`isTalkKey`, `talkKeyAction`, `endsTalk`): Ctrl and D
only — Shift, Alt or Meta make it another key, **AltGr (Ctrl+Alt) never
matches**, nothing mid-composition, a layout whose D types no Latin letter
matched on `code` KeyD. App.tsx listens **on the window, capture phase**, so
the chat box, Monaco, menus, chips, the sidebar and pane headers (each of
which stops its own keys) never stand in the way, and **its default is always
prevented and it stops at the window** — no bookmark dialog, nothing under the
cursor acts on it — under a card, muted or after a crash too; auto-repeat never
presses twice; and **a hold is decided where it began** (review of PR #40,
`TalkHold`): once Ctrl+D began as push-to-talk, or as a press refused under
a card, its **auto-repeat** stays the window's when focus lands in a terminal
mid-hold, so a card answered elsewhere, a click into a terminal or a Preview
frame taking focus never sends the shell `\x04\x04\x04`. A window `blur`
mid-hold lets the mic go (the keyup may never come) but not the hold; a
**fresh** (non-repeat) press always decides afresh, so a lost keyup cannot
keep the shell from its next Ctrl+D. What it does is `press()`/`release()`, unchanged from Space: the selected chat
(W-6), its turn interrupted first, nothing recorded muted (`MIC MUTED`),
refused under a card, with a picker or the chip popover open, with no chat on
screen and after a crash. **In a focused terminal it is the shell's EOF**, left
to xterm untouched (xterm prevents the default itself; **never
`preventDefault` it there** — the terminal's key filter drops a key whose
default is prevented); under a card the window takes it there too, so it is
never an EOF behind the card. From a terminal push-to-talk is the orb (its
tooltip names the key). **In Monaco it is push-to-talk**, and Monaco's
add selection to next find match moved to **Ctrl+Shift+D** (`lib/monaco.ts`
`editorKeyRules`, applied in ED-1's `loadMonaco` after `registerExtras`; the
old binding removed, not shadowed) — free in Monaco and the HUD, and Chrome's
"bookmark all tabs" only outside the editor. **A pending Ctrl+K chord's
Ctrl+D is Monaco's** (E-10, as for Ctrl+B): the window has **one** chord
tracker (`lib/editorkeys.ts` `chordSecond`/`resetChord`, a keydown stepped
once whichever capture listener asks first — `useLayout`'s or push-to-talk's,
whose order a re-run effect changes), and push-to-talk then keeps the key
from Chrome but leaves it to Monaco, starting nothing; Ctrl+K Ctrl+D keeps
Monaco's move-to-next-match. Taking it left Monaco's chord waiting to swallow
the next key typed. A preview frame is another document:
Ctrl+D in one is the framed page's. **Space now presses a focused button**,
as on any page, so a button whose second press matters never takes focus
from a pointer (`noPointerFocus`, a `mousedown` preventDefault; its click
still works): the card's **AUTHORIZE and ALWAYS** (an aborted click — pressed,
slid off — used to leave focus there, and the next Space or Enter
authorized), the terminal's "Jarvis can read" switch, the folded mic's mode
cycle and the Preview's "Keep its origin". The first Space pressed outside a
typing target (`isTypingTarget`) says once, on the selected chat's line,
"PUSH-TO-TALK IS CTRL+D NOW" (`jarvis.hud.spaceHint`); a card that arrives
meanwhile clears it before paint, so ANSWER THE AUTHORIZATION is the line.
Free checks: `talkkey.test.ts` (`TalkHold`),
`editorkeys.test.ts` (the shared tracker), `monaco.test.ts`, `tests/face/hud_v2_ptt_check.py` (after the terminal and
browser sections in `hud_v2_check`, a `MockDaemon(0)` of its own, or alone; its helpers
`_ptt_on_ctrl_d`/`_hold_keys`/`TALK_PROBE` live in `hud_v2_layout_check.py`;
`TALK_PROBE` also counts a Ctrl+D that gets past the window, which must be
none), and the ported checks in the terminal (`\x04` reaches the PTY; nothing
under a card; a hold through a card, into a terminal or across a blur sends
no EOF; the readable switch), multichat (W-6), layout (a separator) and preview (Keep its
origin) suites. The v1 `jarvis.html`
face keeps Space.

**Split and close, WP-G2 (2026-10-11, design §18; plan
`docs/plans/2026-10-10-hud-split-panes-plan.md` §3.4–§3.6, decisions file
beside it wins: S-2 a split is a copy, S-6 a terminal's copy is a new shell in
its folder, S-7 a task's copy is its diff, S-8 a dirty file is saved first —
lifted by ED-2's shared buffers, below).**
The owner asked for the × several times. Every pane's strip has a **split
button** (`pane-<id>-split`, the title bar's `.tb-icon` — the rules were
lifted off `#titlebar` — an outline with one divider), in the single layout
too, and while more than one pane is drawn a **×** at its right end
(`pane-<id>-close`). Both are pure in `lib/workspace.ts`: `splitPane(ws,
source, id, spec, {focus})` inserts the id **right after its source** and
lands on `LADDER`'s shape for the new count (rows2 and main2 are ⊞'s alone);
`closePane(ws, id, then)` drops it, resets only its spec, leaves `kept` and
every edge alone, and never closes the last pane; `allocate` takes the
lowest free id, else the oldest hidden pane holding nothing, else the oldest
whose compose row holds unsent words (`holding` — **its words are parked for
the next new thread, `ORPHAN_DRAFT`**, never dropped: a hidden pane cannot
ask; review of PR #47, H1), never one with an unsaved edit; `copySpec`
copies a file pane's project and tabs (`editors`, ED-2: the same buffers), a
diff's project and `filePath`, a preview's URL — **never
its keep-origin grant** — turns a task into its diff, and leaves a chat or a
terminal for App to fill; `splitVerdict` refuses past eight (`max`), when
the next shape would not be drawn whole even with the side panes and panel
folded (`room`, so at 1280×800 six panes cannot become seven), and when every
free id is a hidden dirty pane. A dirty File pane splits like any other since
ED-2: the copy holds the same buffers, edit and all. `{focus: false}` (the browser amendment's dev-server pane,
§D.2) moves neither `ws.focused`, DOM focus nor the selected chat
(`__hud.split(source, {focus})` is its test hook). **App does what a pane
held** (`releasePane`): a thread's unsent words and files are parked under
the thread (they come back when it opens anywhere), words handed back are
held for it, a turn the pane tracked is left in `State.loose` and **the next
pane that opens that thread takes it up, Stop and all** (`open_thread`,
trading a turn of its own into `loose`; `turn_finished` and the 15 s
reconcile clear it, the reconcile also when the thread is no longer listed —
archived or deleted). **A first send whose pane closed while its thread
opened** leaves the new turn `loose` too (`send()`: nothing tracks or shows
it), and one that fails after the close parks its words for the next new
thread anywhere and says so (review of PR #47, M2); one that fails at
`/send` after the close says its words wait with that thread, by name (review
2 of PR #47, LOW-2). **A park is never silent** (review 2, MED-2/NIT-3): a
release that parks words says where — the thread, by title, or the next new
thread with the project they were written in — and how many files past eight
were left out. **Only a new thread the owner starts takes `ORPHAN_DRAFT` up**
— New thread and a split's copy, the only patches carrying `takeOrphan:
true` (review 3 of PR #47: any new `Compose.id` used to, so a pane × closed,
an undrawn one or one re-aimed by a project's archive took the words and the
window called them "back, in this new thread's box"); a pane × closed or
never drawn gets no compose row at all until it is drawn (`inWorkspace`).
It takes them even from a pane already composing
— after the words it has — says so, and names the project they were written
in when the row is in another (`Draft.projects`); the row keeps its own
project. Notices arriving together (within `NOTICE_BREATH_MS`, one handler
and its effects) **join** on the one error line rather than replace it. A compose row's unsent
words are asked about (G-9) **whatever view the pane shows** — a pane
switched to File keeps its chat, and × releases it too (review 2, MED-1) —
and Discard drops them for good; a terminal goes back to the panel as its
selected tab (`releaseToPanel`) — **no DELETE, no new ticket, the shell lives
on**. × asks first, in an inline strip (`pane-<id>-close-ask`, focus on
Keep/Cancel, never a modal), for a compose row's unsent words (Discard and
close / Keep) or an unsaved edit **no other pane holds**
(`buffers.paneLosing`; Save / Don't save / Cancel — `pane-<id>-close-save`,
`-close-discard`, `-close-keep` — Save closing only once every edit is on
disk; with both, one question names both and says the words go either way);
the strip is dropped when the question no longer holds (the words were sent
or cleared, a file edit joined them — a switch of view alone keeps it, the
words still being the pane's) and Discard re-asks at the click, so it never
answers a question nobody asked; Keep/Cancel is not `disabled` under a card
(inert covers it), so focus comes back to it. **Focus after a close goes where
the mic is** (W-6): the selected chat while it is another drawn pane — closing
a File pane never hands the mic, or dropped files, to the chat that slides
into its slot (review of PR #47, M1) — else, the selected chat closing, the
chat selected before it, else the pane now in that slot (else the slot
before); always the pane itself, never its box, so a held Enter cannot send.
**One click closes one pane**: a × click within 400 ms
of a close, or the second of a double-click, does nothing (3 over 2's top-
right × sits exactly where 2×2's lands). A split's chat copy is a compose row
in the source's project, written before the layout changes; a terminal's is
`terminals.create(specOf(source), id, …, {readable})` — **`POST /terminals`
takes `readable` now, so a copy of a terminal Jarvis may not read is made
with the switch off from its first byte** (never PATCHed after), and a source
whose row is unknown fails closed: a copy never widens what Jarvis can read.
Restart and "New terminal here" keep the old terminal's switch the same way
(`TermManager.readableOf`: its row, else its session's last; review 2 of
PR #47, LOW-1). A
reused id mounts fresh: Workspace unmounts a pane neither drawn nor `kept`,
and `useLayout` bumps the incarnation on a close and when a split takes back
a hidden pane (else its old FileTab stayed). File panes store their tabs
(ED-2's `editors`; a WP-G2 `filePath` reads as one tab) and reopen them on
mount; two File panes on one file share its buffer, so G2's save-and-reload
messages (`lib/filesync.ts`) are gone; Diff panes store and seed their file
(`filePath`).
Open beside's two grow cases are splits now (a free id, so a pane ⊞ hid keeps
what it showed). **A stored WP-A/WP-B value's panes 2–4 are `kept`** when its
preset does not draw them (it has no `order` or `kept`), so a split takes a
free id rather than writing over what pane 3 showed, and a grow brings them
back in the order it always did. `focusPaneElement` looks for a pane a split
just made for a few frames, since a split from a socket or a timer is drawn
after the next frame. **Nothing assumes pane 1**: the store starts with no
selected chat, `activePane` falls back to `home` (the pane in slot 1, which
App syncs), `#tabs` is slot 1's, and Workspace no longer seeds pane 1;
WP-G1's surviving unit mutants (U4 bottom edges unclamped, U34 a preferred
panel's row drop unrecorded, U35 no side refit, U36/U37 fallbacks to id 1)
each have a test now. Under a card every split, × and question button is
disabled and inert, and the eight-pane card check asserts it at 160% and
70%. Free checks: `workspace.test.ts`, `chats.test.ts`, `store.test.ts`,
the layout suite's `_split_checks`,
`_split_close_one_checks` and `_split_file_checks`, the multichat suite's
`_split_close_chat_checks`, `_split_close_selected_checks` and the
`_rv47_*` review checks (each failing on bf09633), the terminal suite's
`_split_close_terminal_checks`, the preview suite's `_split_checks` and
`tests/v2/terminal_check.py`'s unreadable-from-the-first-byte case; the PR's
mutation tables show each failing with its mutation. WP-G3's shared buffer
arrived with ED-2, and WP-H's thinking line is above.

**Sidebar status dots (2026-10-08, design §18; contract in
`docs/hud-api.md`).** The `·` left of each sidebar thread and task is what it
is doing: idle `·`, working (pulsing ring, sweeping row), needs input
(amber: an approval or question open, a blocked task — outranks working,
since both arrive mid-turn), unread (blue: finished, not opened since) and
failed (red ⚠: ended in an error, not opened since). An interrupt, a
cancel and an unstarted (INTAKE) task are idle. A provider question ends
with its turn but a broker approval only with its `approval_resolved` (the
escape hatch asks after `turn_finished`). A task row shows its phase this
way (the word is its tooltip), and a folded project shows its most urgent
row at the row's end; every dot has a fixed slot, so no status moves a
name. **The daemon
decides every status** (`jarvis/v2/activity.py`, an observer on the bus —
`EventBus.observe`, called after the fan-out and outside the lock, so it
cannot drop a record and what it publishes follows its cause); the HUD only
draws `GET /activity` plus SSE `activity`, whose ids ride in `data` so no
`?thread=`/`?project=` stream (the runner's turn wait) ever carries one.
**Read means opened in the HUD** — `POST /threads|tasks/<id>/seen` on
opening, or when it finishes open in a visible window; a Discord read does
not count (owner's call). Unread and failed persist in an `activity.json`
sidecar, written only while `thread.json`/`task.json` still exists (under
the store lock, as `mirror._save` does) so it never resurrects a deleted
one; no sidecar is idle, so nothing from before turned blue. A task's own
threads are never unread or failed. **An answer older than a record must not
undo it** (`ActivitySync` in `lib/activity.ts`): records heard while `GET
/activity` is in flight are replayed over the snapshot, and `/seen`'s own
answer is drawn unless that row heard something newer. Free suites: `tests/v2/activity_check.py`,
`hud/src/lib/activity.test.ts`, `tests/face/hud_v2_activity_check.py`.

Per-thread model (2026-10-06, decisions A, design §8.1/§12.1/§18): **a chat
thread runs on OpenRouter (the fast path), Claude or Codex**, picked with
provider ▾ · model ▾ · effort ▾ beside `in: <project>`. The provider is fixed
by the first message; model and effort change from the next message. The
choice lives on the Thread record (`jarvis/v2/thread_model.py`) — **never
rewrite `brief.json`** — and the daemon hands it to the provider per turn
(`set_model` on all three providers). A default thread follows the global
Model picker **every turn** (whose choice is *the* default — set, reset and
unpinned in the HUD's Model picker, 2026-10-08; see *Model selector*); Claude
and Codex default threads follow **their provider's HUD default** when one is
set (the chip's `default ▾` menu → `POST /thread-models {provider, model,
effort?}`, `model: ""` resets; stored atomically in
`~/.config/jarvis/provider_defaults.json`, `config.PROVIDER_DEFAULTS_PATH`,
its own file because models.json is rewritten whole by two processes, and
refused to every agent write tool — v2's `permissions.protected_paths` and
v1's `files._protected_state`, one set asserted equal, which since this
change also covers models.json, routing.json and the guild file for v1, and since PR #20 the saved Codex catalog), else Claude defaults
to `claude-opus-5-5` at high and Codex to its routing default. **A HUD Codex
default beats routing for chat threads only and never writes routing.json**;
tasks keep routing's table. Effort defaults to `high` (or the roster's pin)
within the model's ladder. **Only an explicit model pins a thread** (A4
amendment, 2026-10-07): an effort-only change on a default thread stores the
effort and leaves `model` null, and `thread_model.effective` clamps it to
whatever the default supports at each turn (none for a default with no
reasoning control); choosing default again clears both. Claude and Codex chat threads are full agents in
the thread's folder, behind the same §6 permit as task workers, so **a
caller's `POST /threads` brief may not loosen the project** — `cwd`,
`profile`, `always_ask` and `mcp_servers` are the project's (or stricter),
else 400 (`Daemon.make_brief`). A provider the project cannot run (Codex off
`auto` or with always-ask additions; Claude and the fast path under `strict`)
is refused before any record exists, from the table in
`thread_model.PROFILES`, which the HUD greys providers out from too. A
refused mid-thread switch rolls the record back to what the provider still
runs, writes `switch to X refused: …; still on Y`, and sends the message on
the old model. An archived thread, or one in an archived project, cannot
change model (409, as with rename and move). The CLI model lists are `router.CLI_MODELS`, the one table the
router's vision filter, the chip, `routing.json`/`/route` validation and a
project's own `routing.models` (on `POST`/`PATCH /projects`) all read; **its Codex half is the signed-in account's catalog** (cached at `config.CODEX_CATALOG_PATH`, loaded at start, else `CODEX_FALLBACK`), a new choice is held to it, and a saved model or effort that drifts from it degrades to the default or clamps down, with a note, and is never rewritten. **`ultra` is Codex-only** and the default effort stays `high` within the ladder (PR #20). **No tool can change a thread's model or provider, or a provider's default** (asserted in
`tests/v2/fastpath_check.py` and `tests/models_check.py`). The picker controls take exactly `{model}`,
`{model, effort}` (on `/models`), `{voice}`, `{muted}`, and refuse any other
key — the HUD sent the wrong keys for weeks and every click reset itself (A6).

<!-- claude-source:004279-004659:end -->


## Shared Markdown math (2026-10-13)

Replies and Markdown file previews render paired dollar and backslash math
delimiters through local KaTeX 0.19.0; its CSS/fonts ship in the HUD build and
use the existing CSP. Drafts remain plain text until completion. Code spans,
fences, escaped delimiters and ordinary unpaired prices retain their text.
Unsupported or unfinished math stays readable as source.

The existing prose HTML allowlist and link scheme checks are unchanged.
Markdown math tokens emit a per-render random placeholder; only its registered
entry is replaced after prose sanitization, with separately sanitized
KaTeX HTML/MathML. Math admits no links, images, controls, handlers, ids or data
attributes. Rendering uses trust=false, strict errors, 100 macro expansions,
a 20-em user size cap and a fresh macro dictionary per expression. Generated
negative lengths below -20 em or positioning/margin offsets beyond +/-20 em
fall back to source; natural positive widths remain available for scrolling. Math work is
limited to 64 expressions, 8192 source code units each, 32768 in total; excess
math stays literal and ordinary prose is retained. Inline closing positions
are indexed once and display-block lookahead is bounded.

Checks: markdownMath.test.ts covers the reported Mean Value Theorem fraction,
numeric formulas, all four delimiter forms, partial completion, code/currency,
hostile TeX commands, macro/size/source bounds and delimiter floods.
hud_v2_math_check.py runs actual chat under the CSP on MockDaemon(0), verifying
MathML, local font loads, fraction geometry, stream completion, hostile
commands and narrow-viewport scrolling. It is part of hud_v2_check.py.


## `jarvis hud rebuild` runs no install scripts (2026-10-15)

**The owner's decision of 2026-10-15: install scripts can be malicious, so
`jarvis hud rebuild` runs `npm ci --ignore-scripts`** (`owner_ops.NPM_CI_ARGS`;
the archived paragraph above still says `npm ci`). The flag is on the command
line, which outranks every npm config file, so no `.npmrc` can turn scripts
back on (the clean-`hud/` refusal still runs first). `npm run build` is
unchanged: it runs `hud/package.json`'s own reviewed `build` script. The CLI
help, `hud_install.npm_build`'s refusal and every face suite's "build first"
hint now say `npm ci --ignore-scripts` too. `tests/v2/owner_ops_check.py`
pins the argv (`test_it_never_runs_install_scripts`, and `rebuild --clean`'s
own call); the old `[npm, "ci"]` fails four tests.

**The install scripts that no longer run** — the lock's whole list
(`npm query ':attr(scripts, [install])'`, `[postinstall]`; no `preinstall`,
and `hud/package.json` has none of its own):

- `node-gyp-build` (`install`) in 12 tree-sitter packages:
  `tree-sitter-{bash,c,cpp,css,html,javascript,json,python,typescript}`,
  `tree-sitter-typescript/node_modules/tree-sitter-javascript` and
  `@tree-sitter-grammars/tree-sitter-{toml,yaml}`. It loads or compiles each
  grammar's **native Node binding**, which the HUD never uses: the browser
  build imports each package's shipped `.wasm`, and vitest's syntax kit
  uses web-tree-sitter on those same files.
- `esbuild`'s `node install.js` (`postinstall`): it checks the platform
  package and swaps `bin/esbuild`'s JS shim for the native binary. Without
  it the shim runs `@esbuild/linux-x64`'s binary (an optional dependency
  npm installs either way); Vite works unchanged.
- `protobufjs`'s `node scripts/postinstall` (`postinstall`; onnxruntime-web
  pulls it in): a version notice for its CLI.
- `fsevents` (macOS only) is never installed here.

None is needed. A clean `npm ci --ignore-scripts` in a fresh worktree
(`rm -rf node_modules` first, 196 packages) followed by `npm run build`
produced a `hud/dist` **byte-identical** (`diff -rq`) to the main
checkout's build of the same commit, whose `node_modules` had run every
script (its `bin/esbuild` is the ELF binary): all 16 `.wasm` (web-tree-sitter,
13 grammars, onnxruntime, RNNoise) and the four `worker-*.js`. The syntax,
CSP, VAD, wake-word and denoise suites passed on that build (counts in
`editor.md`'s 2026-10-15 section). If a dependency ever needs its script,
name it and run that one script by hand; never drop the flag.

Ignoring install scripts closes one route only: a malicious package's code
still runs when the build or the tests import it (Vite, its plugins, tsc,
esbuild's binary). The lock's integrity hashes and reviewing dependency
bumps remain the guard for that.

The approval page's CSP changed the same day (no `'wasm-unsafe-eval'` on
`/`; only a worker's fetch of its script carries it): see `editor.md`, "WebAssembly leaves the
approval page".

## Sidebar: five most recent chats per project (2026-10-15)

Owner request: a project in the sidebar shows its **five most recently
modified chats, newest first**, then a "Show N more" button (and "Show less"
once open). The rule lives in `hud/src/lib/recentChats.ts`; `Sidebar.tsx` only
draws it.

- **Recently modified is `Thread.updated`**, the daemon's last-activity stamp
  (set on each turn in `jarvis/v2/daemon.py`), not `created`. An unreadable
  stamp sorts last and ties keep the daemon's order. The window already
  re-reads `/threads` on `turn_started`/`turn_finished`, so the order follows a
  finished turn. No daemon change.
- **Never hidden, however old**, kept in their sorted place and not counted by
  "Show N more": a chat open in any drawn pane (the selected one included),
  the row being renamed, a running turn (`Thread.running`) and any non-idle
  activity status (needs input, unread, failed, working), so an approval card's
  dot, a blue unread and a red failure are never behind the button. A folded
  project's dot still reads every chat, hidden ones included.
- **State**: per project, in localStorage `jarvis.hud.sidebar.showmore`
  (`{"<project id>": true}`), read and written inside try/catch and ignored
  when malformed. The button is a real `<button>` with `aria-expanded` and the
  count; a project with five or fewer chats has none.
- Unchanged: archived chats (not in `/threads`), the Task list and a task's
  own threads (they are not chats of the project), the chat panes.
- Tests: `hud/src/lib/recentChats.test.ts` and
  `tests/face/hud_v2_recent_chats_check.py` (8 chats: 5 newest-first, "Show 3
  more", an old open chat kept, attention states kept, reload, keyboard).

## Native sub-agents: rows, the read-only pane and the card's link (SV-3, 2026-10-15)

Plan: `docs/plans/2026-10-13-subagent-view.md` (§D5, the owner's decisions of
2026-10-13 and 2026-10-15); API: `docs/hud-api.md` "Native sub-agents".
No Stop (SV-4) and no between-turn drain (SV-6) here.

- **Store** (`hud/src/state/subagents.ts`, outside React like
  `state/toolGroups.ts`). It holds a thread's children only while something
  shows them (a chat pane, a sub-agent pane, a card): read from the owner's
  `GET /threads/{id}/subagents` when first watched and on every SSE
  reconnect, and moved in between by the bus's `subagent` records (ids,
  status, times, counts). Since the bus carries no words, the list is read
  again behind it: 250 ms after a new child or an end, at most every 1.5 s for
  progress. Records heard while a read is on its way are laid over it
  (`lib/subagents.ts` `foldList`); a stale one (fewer `entries`, or a live word
  for an ended child that is not Codex's own `started`) never takes a row
  back. A fold-equals-live property test covers 1,000 random runs. Bounds: 64
  children per thread (the daemon's), 32 threads (least recently used
  unwatched out), 256 records waiting on a read (past that, read again).
  Nothing is stored; a reload reads it all again.
- **Rows** (`components/SubagentRow.tsx`) sit where the child was spawned:
  a Claude child right after the transcript entry holding its Agent call
  (its sid is that call's id), a Codex child by its start time (Codex's
  spawn is no parent call), at the end when nothing is timed. The parent
  shows the top level; a nested child (`parent_sid`) is drawn only in its
  parent's pane. A row is one button (`aria-label` "Open sub-agent ‹name› in a
  pane", Enter/Space native, `noPointerFocus`): ◇ name · provider badge ·
  kind · status word · last tool · elapsed · tools · tokens · declined. Status
  colours are Graphite's: amber running/backgrounded, `--green` done, red
  failed, muted stopped (its reason as the title). More than six rows at one
  spot collapse into "Sub-agents · 8 (5 running)", a disclosure button.
  One `useSyncExternalStore` per row, so a progress tick redraws that row
  only; the transcript's placement changes only when a child comes or goes.
  Names, kinds and labels are model-chosen: re-cleaned (`toolLabel`, 40
  characters) and drawn as text. Under a card everything is inert, rows
  included.
- **The view** is `View` `"subagent"` with `PaneSpec.subagent = {threadId,
  sid}` (stored in `jarvis.hud.workspace`; never a word). It is never offered
  by the strip: only a pane showing it keeps its button. `parsePane` turns a
  bad reference into the pane's default view, `setView` refuses to switch to
  it without one and drops the reference when leaving, and a split copied
  from it is a new chat. **Opening** (App `openSubagent`): focus the pane
  already showing the sid; else split beside the chat pane showing the
  thread (else the focused pane); else show it in the last sub-agent pane;
  else the notice "No room for another pane: close one or use ⊞". Never a
  chat pane taken over, never under a card.
- **The pane** (`components/SubagentPane.tsx`) is the transcript route as
  served, read again behind each record about that child: header (name,
  provider, kind, status and reason, elapsed, tools, tokens, declined, "of
  ‹chat›", "Show in chat"), the **brief collapsed** ("Brief" disclosure,
  plain text; "Codex does not send a sub-agent's brief." for a Codex child),
  then texts (Markdown, sanitised), tool calls as `ToolGroupRow` groups
  (label and outcome only), the closing result and reviewer declines. No
  input box. It follows the bottom only when the reader is there. A 404 says
  "This sub-agent's record is gone." with Close; a 400 "Update Jarvis to see
  sub-agents."
- **The card's link.** A card a child's call raised carries its sid
  (`subagent`; Codex's since this package, the owner's 2026-10-15 decision).
  The card shows "from sub-agent ‹name›" under REQUESTED BY, with
  "(not the orchestrator)" unchanged in the origin or headline. The link is
  out of the Tab order and never takes focus from a pointer, like AUTHORIZE,
  and decides nothing. Behind a card nothing can take focus, so a click only
  scrolls to and marks the row (`.sa-flash`); once the card is answered the
  child's pane gets focus if one shows it, else its row, else its pane opens.
- **Silent.** A `subagent` record never touches the chat's ops, status line,
  orb, speech or Discord. The bus record is handled by its thread id only.
- **Daemon side, for the brief and the card** (see `docs/hud-api.md`):
  Claude's `task_started.prompt` rides that one lifecycle event to
  `subagents.py`, which scrubs (`redact.scrub_text`) and caps it (4000
  characters, 16 KiB JSON) and serves it on the transcript route only.
  `permissions.build_permit` takes `subagent_sid` and puts a valid sid on the
  card (`ApprovalRequest.subagent`); providers pass it only to a callback
  that takes the keyword.
- **Tests:** `hud/src/lib/subagents.test.ts`, `state/subagents.test.ts`,
  `components/SubagentRow.test.ts`, `Approvals.test.ts` and
  `lib/workspace.test.ts` (vitest); `tests/face/hud_v2_subagents_check.py`
  (its own mock, owner-only routes enforced by `hud_v2_mock_subagents.py`;
  also a section of `hud_v2_check.py`); `tests/v2/subagent_routes_check.py`,
  `claude_subagents_check.py` and `codex_provider_check.py` for the brief and
  the card's sid.
- **Deploying is the owner's step**: `jarvis restart` for the daemon half,
  then `jarvis hud rebuild` and reload the window.
