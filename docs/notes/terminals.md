# Terminals — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 779–865

<!-- claude-source:000779-000865:begin -->
**HUD terminals, backend (2026-10-09, WP-C; plan
`docs/plans/2026-10-09-hud-workspace-plan.md` §2.4, decisions W-2/W-3/W-5;
design §18; contract in `docs/hud-api.md`).** `jarvis/v2/terminals.py` runs
the owner's login shell (`JARVIS_TERMINAL_SHELL`, absolute only, overrides;
the **realpath** is checked and launched, under the given name as argv[0]
via `env --argv0`, so `rbash` stays restricted) on a real PTY through util-linux
`setsid --ctty` — **never `pty.fork()`** in this threaded daemon — with
HUP/INT/QUIT/TERM/TSTP reset to default (`env --default-signal`), in a
**clean environment** built from an allowlist (never a `.env` name,
`OPENROUTER_API_KEY`, `HF_HUB_OFFLINE`, `VIRTUAL_ENV`, venv `PATH`,
`JARVIS_*`/`ANTHROPIC_*`/`CLAUDE_*`/`CODEX_*`), in a folder resolved **from
ids, never a path**. Six at most, a 1 MiB ring each, replayed on reattach;
input goes through a 256 KiB queue and a writer thread per terminal (a full
queue drops the frame with `input_dropped`, never blocks the socket, and
**latches** the socket: every later frame but a lone Ctrl-C is refused until
the queue drains and the window sends `input_resume`, so a paste arrives as
a prefix, never spliced; a Ctrl-C flush keeps one waiting Ctrl-C); close
= SIGHUP to the whole session (found in /proc), then SIGKILL — the leader is
matched on its **start time** and a session once seen empty is never
signalled again, so a reused pid is never hit; two DELETEs pop it once;
**`Daemon.stop` ends them all** (W-3, no tmux). The socket is a hand-rolled
RFC 6455 endpoint (`jarvis/v2/ws.py`): masked client frames, 64 KiB
messages, control frames ≤125 bytes and unfragmented, no extensions, 20 s
pings, 60 s silence drops it, close codes validated (1005/1006/1015 and
out-of-range are protocol errors), **nothing sent after our close** (one
lock for the check and the put), the `101` written as `HTTP/1.1` by hand,
and the handler's 2 s timeout lifted. **The agent never gets a lever on a
terminal**: every route is `projects.owner_only` (HUD listener + an Origin
that must be present — on a plain GET, `Sec-Fetch-Site: same-origin` with no
Origin at all instead, since 2026-10-11, below), the attach also needs a **single-use 30 s ticket
bound to one terminal**, and a terminal another window shows is only taken
after that window says yes (refused after 20 s) — stricter than
`/approvals`, and still not a boundary against a program running as the
owner (it can forge an Origin, fetch a ticket and attach to a terminal no
window shows), which is why **every attach publishes `terminal_attached`
with exactly `{terminal_id, at}`** for WP-D to flag. **No tool, MCP tool,
fast-path tool or Discord verb may reach `terminals`**;
`tests/v2/terminal_check.py` greps every module under `jarvis/` but the
daemon, `hud_api` and the module itself. **Terminal output stays in memory
unless Jarvis reads it**: on the bus lifecycle records only
(`terminal_attached`, and WP-F's `terminal_read` — ids, a count, a time), no
log line (lifecycle only, and `/terminals` paths are logged without their
query — the ticket rides in it), no thread log, Discord or disk (BR-8a's
`dev_url` frame, below, goes to that terminal's own socket and nowhere
else). The
exception, since WP-F (corrected 2026-10-10): what `terminal_read` returns goes where any tool result goes — the fast-path transcript (and so OpenRouter, with every later request), that conversation's v1 session `messages.json`, `config.SPILL_DIR` if context truncation cuts it later, and anything the model's reply repeats (a reply is mirrored to the chat's Discord thread and spoken); its ticker
summary is fixed ("read N lines"), so the tool's own `tool_finished` record
carries none of it onto the bus or into the thread's `log.jsonl` — **but the
reply is not filtered**: it reaches `log.jsonl`, the bus, Discord and speech
like any reply, so whatever the model repeats lands there too. Startup files: `terminal_rc.bash` (bash
`--rcfile`, login emulation) and the POSIX `terminal_rc.sh` (`$ENV` for an
`sh`-family shell started `-i`; kept apart because a dash parses every line,
so bash syntax cannot hide behind an `if`); **zsh, fish and other shells get
none — no `sudo -k`, no marks — and the listing says `integration:
"none"`**. Each file **never touches disk**: it reaches the shell through a
pipe (`--rcfile /dev/fd/N`, `ENV=/dev/fd/N`) whose write end is closed
first, so the shell's first read drains it (bash closes the fd; POSIX
unsets `ENV`). It then reads the login files, sets `alias sudo='sudo -k'`
(W-5) and emits OSC 133 A/B (+ C with the `cmdline_url`, D in bash) marks
**signed with a per-terminal nonce** held only in an unexported variable
that PS1 *names* (`${__jarvis_nonce}`), never holds — nothing the terminal
runs can find it (the suite hunts from `~/.profile` mid-startup and later,
in real bash and dash, and scans the disk); a same-uid program outside the
terminal could only race the shell for the pipe. POSIX marks end in BEL (dash's
PS1 expansion eats the backslash of `ESC \`). They become `CommandSpan`s
(`Terminal.history()`: spans, `spans_from` — bytes before it lost their spans
to the 2000 cap — `integrated`, `readable`, `prompt`, and since WP-F `alt`)
for WP-F's `terminal_read` (below). **Spans are advisory**: a
program can print bytes between real marks, nested shells/`sudo -i`/`ssh`/
`python` sit under the outer span, an `ignorespace` line records its first
command only — so WP-F's text-pattern refusals apply to every read.
`terminal_check`'s no-tool test allows exactly `terminal_read`, whose module
may hold one line naming this one — `from ..terminals import read_for_tool`
— and call it once. Each terminal has the owner's `readable` switch, **on by
default**.
Follow-ups from the re-review (with WP-D): `integration` is what was
*configured*, **`marked`** (listing, attach row, one `{"type": "marked"}`) is
what *took* — a profile that `exec`s another shell reads "bash" but never
marked; the startup file must fit **8 KiB** (a pipe under
`pipe-user-pages-soft`) and is written **without blocking** under
`Terminals._lock` — a short write is a 409, never a wedged route; and **the
checked realpath always runs** — without `env --argv0` a name whose basename
differs (rbash, `sh` for bash) is refused rather than exec'd by its link.
Tests point `config.TERMINAL_SHELL` at `tests/v2/terminal_fake/sh` (named
`sh` so it is started as a POSIX shell) with a temp HOME, and never use
8402/8403/8405.

<!-- claude-source:000779-000865:end -->

## Original CLAUDE.md lines 1556–1731

<!-- claude-source:001556-001731:begin -->
**Jarvis reads a terminal, WP-F (2026-10-10; decisions W-2 is the spec;
design §18; contract in `docs/hud-api.md`).** `terminal_read(terminal="",
lines=200)` — `jarvis/v2/tools/terminal_read.py`, which imports exactly
`terminals.read_for_tool` — returns the last lines (≤ 1000) of one terminal's
**normal buffer** as plain text, named by what the HUD shows (id, title, its
folder part, or the panel number; never a path), fenced as untrusted web
content and through `dispatch()`'s scrub. `jarvis/v2/terminal_text.py`
(pure) strips CSI/OSC/DCS/APC/PM/SOS and single escapes and their C1 forms,
split anywhere; collapses CR, backspace, cursor motion, erases and `clear`
to what the terminal shows; and **never reads the alternate screen** — the
ring feeds every byte it drops to an `AltTracker`, so `history().alt` knows a
read starts inside vim even when the switch left the ring, and a switch the
cut split in two comes back whole as `history().lead`. It also keeps `ink`,
the states rows were in before they were changed again, erased or let go
(lazily: a row nobody returns to is read on screen), so a command line
cleared off the screen still counts. **The screen has the terminal's
`cols`** (xterm's autowrap: a wrapped row continues its logical line,
cursor numbers clamped, at most `MAX_ROWS` rows, ink capped and fail-closed
past it), and **a C0 control inside a sequence is executed and the sequence
goes on**, as in xterm. `jarvis/v2/terminal_guard.py` (pure) **refuses the
whole read** with exactly "possible credential in this output" — never what,
where or which rule — for (1) a `secret_values()` value, or one from the
terminal folder chain's credential env files (`secrets.env_files_in`, the
owner's `.env*` rule — since ENV-1 `.sample`/`.template` count, only
`.example` names are open; read `O_NOFOLLOW|O_NONBLOCK`, regular
files only, capped — a FIFO named `.env` used to hang the read), its base64
at any alignment or its URL-encoding; (2) a format in
`jarvis/credential_patterns.py` (one list; a private key's END footer too),
or a read that starts inside a `BEGIN … PRIVATE KEY` block opened in the
look-back and not closed there; (3) a secret-printing command
(`SECRET_PRINTERS`/`READERS`/`SUBCOMMANDS`, one place; robust to whitespace,
quoting, `sudo -k`, assignments and `x=env; $x`, paths, wrappers, the
shell's own words — `then`/`do`/`else`/`elif`/`!`/`{`/`case … )`/`coproc` —
leading and input redirections (`< .env cat`, `done < .env`), pipelines,
subshells, `$(…)`/backticks/`<(…)` judged as command lines, `sh -c`, `ssh
host cmd`, `git show HEAD:.env`, function definitions, `find -exec`/`xargs`
readers and readers of `$f`/`{}` on a line that names one, copies onto the
terminal (`cp .env /dev/stdout`), `file:` URLs, archive members and
compressors to stdout, decompressing readers, whole-environment dumps
(`tmux show-environment`, `docker compose config`, `dotenv list`, `php -i`,
`docker inspect` unless `--format` names only safe fields), and `.env.*` —
beyond v1's `is_protected`, inside the guard only). **What loads a file is
not what prints it** (2026-10-11 re-review): an `--env-file` loads one
(`node --env-file=.env server.js` is how Node 20 starts a dev server, and
its log used to be refused for as long as it ran), `wc`/the checksums count
one, and an interpreter prints only given inline source on a line naming a
credential file, inline source reading the environment (`node -p
process.env`, `os.environ`, a secret-named variable), or a credential file
as its script. **Node and bun take a cluster's source from the next
argument** (delta review, 2026-10-11): `-pe` read as `-p` with a glued "e"
took the real source for a script, so `node -pe process.env` read — and
`node --env-file=.env -pe process.env`, refused at 3393daf, read at eb718b5.
An interpreter that names no program reads one from standard input, so a
here-string, a piped `echo`/`printf` and a here-document's body are its
inline source (rules.py's reading of `… | python3`, through the guard's own
parse); a REPL's or here-document's input line at a line's start (`>>> `,
`> `, `... `, `php > `) that reads the environment **explicitly** refuses the
read — `process.env`, `os.environ`, `ENV[`/`ENV.`, `$ENV{`, a getenv call,
`Bun.env`, `Deno.env`, never the bare words `ENV`, `env:`, `environ` or
`getenv` (third review: those refused a here-doc of a Kubernetes manifest or
a Dockerfile, a README blockquote, an email-style quote and a REPL
assignment, and one such line refused everything after it); `--print`/
`--eval` never take an option-looking argument as the source, after one or
`--` the first operand left is judged as a source too (an ambiguous parse
fails toward refusing), and `bun run`'s own options are parsed; a word
prompt may carry git-status marks (`main*`, `main±`). **Accepted costs**: the loading
idioms `env $(cat .env | xargs) npm run dev` and `export $(grep -v '^#' .env
| xargs)` stay refused — `export`/`env` echo a word they cannot use in their
error message, so part of a value can reach the terminal. Two halves: a real span overlapping the read (or one that
backgrounds a printer), **and always the text** — any screen or `ink` line
in the read or the `LOOKBACK` (32 KiB) before it that shows such a command
at its start, after a prompt glyph or after a prompt made of words (`[proj]
cat .env`; never a reader's name in prose — a commit message "Add tail call
before loading .env.production" is not `tail`), raised only to a boundary
the signed marks prove and never by a span record below `spans_from`.
**Every scan is linear** (2026-10-10 review): lines are read to
`LINE_SCAN`, at most 16 candidates of 512 characters a line, the heuristic
in one pass, each distinct line text judged once, the command cache bounded
by size and by count (`CACHE_ENTRIES`; ~50 MiB worst case before); **and
bounded** (2026-10-11): a read parses at most `PARSE_BUDGET` (128 KiB) of
commands it has not judged before — ordinary reads spend 2–12 KiB — and is
refused past it, so a screen dense with candidates buys a refusal, not 18 s
or 85 s. **Every parse bound fails closed**: what a bound leaves unparsed (a
command past `COMMAND_CUT`, a candidate past its cap, candidates past the
per-line cap, a line past `SCAN_LINE_CAP`, a runner's arguments past
`RUNNER_TAILS`, wrappers past the unwrap, a span command past `COMMAND_CAP`
— `CommandSpan.unseen`) refuses the read when it `could_print` (names a
credential file or a glob onto one, a printer, a secret-named variable, an
environment dump), never for length alone. A 1 MiB ring of adversarial
shapes is judged in well under 2 s (`Speed` in `terminal_text_check`). Rules run on the text after
`strip_invisible`, so a zero-width character cannot split a key past them.
The heuristic **withholds lines** (a keyword, then within 40 characters past
`=`/`:`/space a ≥20-character token of entropy ≥ 3 that is not a path, dotted
name or joined words; a bare hash or UUID stays) and says `[N line(s)
withheld: possible secret]`. The switch off refuses by name — checked again after the render, so turning
it off mid-read refuses. **Every read that reaches a terminal publishes
`terminal_read` with exactly `{terminal_id, lines, at, refused}`**, and the terminal's bar says "Jarvis
read 200 lines · 15:42" / "refused a read · 15:42" (`TermManager.reads`,
`parseRead`; HUD chrome only). **Who holds it** (the holder decision): the
fast path only, and only for an owner's chat turn **the HUD's own window
sent** — `UserMessage.desk`, set by the send route only on the HUD listener
with its Origin (`projects.is_owner`); `FastPathProvider` binds
`runtime._DESK` per turn (`runtime.at_desk()`, fail closed), drops
`DESK_TOOLS` from the request on any other turn, refuses them in a task's
brief, and a steer from anywhere but the desk takes the running turn off it.
**A Discord or DM turn is not at the desk** (W-2 says "where the owner is
present"), nor is the escape hatch, a schedule, a sub-agent (`depth() != 0`),
a workflow, a goal, an attended task or any v1 surface —
`tools.EXPLICIT_ONLY` keeps it out of `default_names()`. **jarvis-mcp does not
offer it**: it is a separate process with no channel to the daemon's
terminals and no way to tell a chat from a task worker (the peers plan's
per-session tokens have not landed), so it fails closed. **The owner
confirmed both limits (2026-10-10): fast-path desk turns only for now;
Claude and Codex chats via jarvis-mcp are a follow-up; Discord, DM and
scheduled turns never.** The guard modules and the tool are SELF_PROTECTED.
Where read text goes: what `terminal_read` returns goes where any tool result goes — the fast-path transcript (and so OpenRouter, with every later request), that conversation's v1 session `messages.json`, `config.SPILL_DIR` if context truncation cuts it later, and anything the model's reply repeats (a reply is mirrored to the chat's Discord thread and spoken). **The limit**: a secret with no
shape and no keyword beside it, or one printed in pieces or otherwise
encoded, is not caught; rule 3 judges command lines by shape, so an alias, a
shell function, a script or a symlink that prints a secret is invisible to it
(its output still meets rules 1 and 2 and the heuristic), and so is a script
handed a credential file as an argument (`python3 show.py .env`) and a
program piped in from anything but a literal `echo`/`printf` (`cat x.py |
python3`); a minified
line past `SCAN_LINE_CAP` that holds a bare `process.env` or `.env` is
refused (the fail-closed side of the bound); the switch and the note are the
backstops. Free suites:
`tests/v2/terminal_text_check.py` (pure), `tests/v2/terminal_read_check.py`
(real PTY, the fake shell's new `hex` command, holders, fast-path turns), the
updated `terminal_check` (no-tool, the reader only reads, the bus record),
`terminal.test.ts` and `_read_note_checks` in `hud_v2_terminal_check.py`.
**Verified to bite**, each in a scratch copy of the branch (18 of 18): drop
the text fallback, trust spans only (skip the text when integrated), forget
`spans_from`, read the alternate screen (the renderer ignoring the switch;
the ring's-start state dropped; the tracker not fed evicted bytes), return
the refusal reason, offer the tool to a task's brief, count a task's turn as
the desk, skip the `readable` check, the tool skipping its desk check, any
HUD-route message counting as the desk, a Discord steer leaving the desk on,
`EXPLICIT_ONLY` not honoured, rules before `strip_invisible`, no base64
forms, no folder `.env` values, and `ink` forgetting cleared lines.
The review round's own, each also shown to bite (39 of 39 with round 1's,
re-anchored): no private-key footer, no key-block state, a closed block read
as open, shell keywords not stripped, input redirections not read,
substitutions not judged, `find -exec` readers not printers, `.env.*` not
protected (and templates protected), no `.env.*` folder values, no width, no
row cap, an unbounded command cache, the ticker showing the result, a split
switch not handed over, the tracker blind to C0 inside a switch, C0 aborting a
CSI, the desk slot carried over between turns, no re-check of the switch
after the render, a blocking env reader (the FIFO hangs), and URL passwords
not withheld. At bd95320 the `Speed` rings took over 30 s, 12.6 s or ran out
of a 3 GiB cap where they now take under 1 s. The re-review round's
(2026-10-11; `OrdinaryReads`, `UnseenParts` and new `Speed`/`Printers`
cases, most failing on 3393daf) — 40 of 41 mutations bite: an `--env-file`
read as a file, interpreters judged as before, counters printing, `inspect`
always printing, any prose word as a prompt, a loader naming a credential
for the line, a glyph as a command word, function heads unparsed, no copy
onto the terminal, no `file:` URLs, no archives, no environment reads, no
compression suffix, and each fail-closed bound dropped (the cut, the whole
line's and a prompt candidate's tails, the per-line cap, the scan, the
runner tails, the wrappers, a span's `unseen` ignored or not recorded, the
glob, keyword-variable and flat-form hazards), no parse budget, no dedupe,
free hazard scans, `SCAN_LINE_CAP` out of reach, no `CANDIDATES_PER_LINE`,
no `CANDIDATE_CAP`, no `COMMAND_CUT`, `Terminals.read` dropping the lead,
`_worth_parsing` always true, an unbounded cache count, and five earlier
ones re-anchored (no text fallback, input redirections, shell keywords,
substitutions, `find -exec`). The survivor: dropping only the
`line[:SCAN_LINE_CAP]` slice, which now costs at most ~0.5 s a read and
nothing else (what lies past the scan is the fail-closed check's). The delta
review's (`DeltaReview`, 4 of its 5 tests failing on eb718b5): 7 of 7 bite —
`-pe` reading a glued source, an option taken as the source, stdin never
read as source, a piped echo not followed, a here-document body not read,
REPL input not read, no git marks in a word prompt. The third review's
(`ThirdReview`, all 3 failing or erroring on 38cb5e6): 8 of 8 bite — bare
words counted on input lines, a bare `ENV`, `env:`/`environ`, a prose
`getenv()`, no PHP shell prompt, a long option taking an option as its
source, a pending source never judged, a verb's options skipped.

<!-- claude-source:001556-001731:end -->

## Current: the "Jarvis can read" switch follows every window (2026-10-15)

Amends the archived `readable` switch paragraph above. When a `PATCH
/terminals/{id}` actually changes the switch (`Terminals.set_readable`), the
daemon publishes `terminal_readable` with exactly `{terminal_id, readable, at}`
(an id, a boolean and a time: never output), so another HUD window's switch
catches up (PR #187). The daemon's flag stays the only truth, nothing reads
that record, and the HUD applies it to its display only.

## Current: a VEX pane's Brain tab is a place (2026-10-15, VX-UI-4a)

Besides the panel and a terminal pane (`place`), a terminal can be drawn in a
VEX pane's Brain tab: the `vex-<pane>` place (`TermManager.vexPlace`,
`holdInVex`, `shownIn`), used only for the brain terminal. It is still one
session and one place at a time (W-1): a terminal pane holding it wins, then
the first VEX pane whose Brain tab holds it, then the panel. The panel's tab
says "in the VEX view, pane N" (`data-place="vex-N"`), and the panel and
`visible()` treat it as drawn. `adoptProgram` claims a program terminal for
this window without opening the panel. `TermSession.printed` is true once
xterm's buffer holds visible text, never for control sequences alone. Nothing
about the session, the socket, input, the "Jarvis can read" switch or
`terminal_read` changed. Details are in `docs/notes/vex.md`, "Brain tab, VX-UI-4a".
