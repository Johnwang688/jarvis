# Decisions: HUD workspace, layout toggles and terminal (2026-10-09)

The owner's answers to the open decisions in `2026-10-09-hud-workspace-plan.md` §4. **Where this file and the plan disagree,
this file wins.**

## Answered by the owner

| # | Decision | Answer |
|---|---|---|
| W-1 | Where the terminal lives | **Both.** The bottom panel is its home (⬓, Ctrl+\`), and any pane can also show a terminal. One terminal is drawn in one place at a time. |
| W-2 | Can Jarvis read a terminal? | **Yes, with a read-only `terminal_read` tool** that refuses output that may hold a secret (see W-2 below). This replaces the plan's recommendation of "no tool". WP-F is in scope, as amended here. |
| W-3 | Terminals survive a daemon restart (tmux) | **No.** Terminals and their dev servers end with the daemon. |
| W-4 | Dev apps in Preview keep their own origin | **Yes, opt-in per pane**, and only once `frame-ancestors` / `X-Frame-Options` have landed (WP-E orders it). |
| W-5 | `sudo` never caches in HUD terminals | **Yes** (the recommendation; the owner did not object). |
| W-6 | Where ambiguous input goes | **The currently selected chat** (the chat pane most recently clicked); input that belongs to a thread stays with it. |

Plan items 6–14 ("defaulted; say if you want otherwise") stand as written. The owner raised no objection to any of them.

## W-2: `terminal_read`, as the owner asked for it

The owner asked: "can we have a read only tool and have the terminal deny access if the output could contain secrets?"
The answers below were given 2026-10-09: heuristic hits **withhold lines**, and a new terminal is **readable by default**.

**What it reads.**
- One terminal's recent output, up to 200 lines by default.
- Plain text from xterm's normal buffer, with escape sequences stripped. The alternate screen (vim, less, top) is never
  read.
- It is the same ring the HUD replays, so nothing is stored that is not already in memory.

**It is read-only, structurally.**
- No tool or MCP tool can create, write to, resize, focus or close a terminal. `terminal_read` only reads.
- `tests/v2/archive_check.py`'s pattern proves no tool reaches a terminal write path.

**Who holds it.**
- **Foreground chat threads only, where the owner is present:**
  - the fast path's `FAST_TOOLS`;
  - `jarvis-mcp` for Claude and Codex chat threads.
- **Not** background work: workflows, sub-agents, goals and task workers do not hold it. The owner's terminal is a
  desk feature, like the desktop tools.
- Lead's default; say if otherwise.
- **2026-10-10, owner confirmed:** fast path desk turns only for now. Claude and Codex chats via jarvis-mcp are a
  follow-up (per-session tokens). Discord, DM and scheduled turns never.

**Refusals: the whole read is refused, saying only "possible credential in this output". The refusal never says what
or where.**
1. **Exact known values.** The output contains any value from `secrets.secret_values()`: the `.env` values and every
   token bundle. This is reliable and has no false positives.
2. **Known credential formats.** These have distinctive shapes:
   - OpenRouter `sk-or-`, plus the other `sk-…` provider keys;
   - GitHub `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` and `github_pat_`;
   - AWS `AKIA`/`ASIA` key ids;
   - Slack `xox[abpr]-`;
   - Google `AIza`;
   - Stripe `sk_live_`/`rk_live_`;
   - `-----BEGIN … PRIVATE KEY-----`;
   - JWTs (`eyJ…`, three dot-separated segments).

   The pattern list lives in one module, with tests.
3. **Secret-printing commands.** The terminal's startup file emits shell-integration marks (OSC 133) at each prompt and
   command, so the backend knows which output belongs to which command line. A read that covers the output of a command
   matching a small list is refused. The list:
   - `env`, `printenv`, `set`, `export -p`;
   - `cat`/`less`/`head`/`tail`/`bat` of a `secrets.is_protected` path;
   - `gh auth token`, `gh auth status -t`;
   - anything with `--show-token`;
   - `aws configure get`/`export-credentials`;
   - `vercel env pull`;
   - `git credential fill`.

   Without the marks (a shell that does not run the startup file), this rule falls back to refusing any read whose text
   contains one of those command lines.

**Withheld lines (the heuristic): only that line is dropped, and the read carries "[N line(s) withheld: possible
secret]".**
- A keyword appears with a long, high-entropy token beside it. The keywords are `key`, `token`, `secret`, `password`,
  `passwd`, `pwd`, `auth`, `bearer`, `credential`, `api_key`, `private`; the token is ≥ 20 characters of base64/hex-ish
  text, after `=`, `:` or whitespace.
- Bare 40-character hex git hashes and UUIDs are **not** withheld on their own. Next to a keyword, they are.

**The owner stays in control.**
- Each terminal has a **"Jarvis can read" switch**, **on by default**, in its header. Off means `terminal_read`
  refuses that terminal by name.
- Every read shows a note in the HUD on that terminal: "Jarvis read 200 lines · 15:42". This is HUD chrome, never
  bytes written into the PTY stream.

**What it returns.**
- The text is fenced and labelled untrusted, like fetched web pages (PR #19): a dev-server log can carry instructions.
- It still passes through `dispatch()`'s scrub, as every tool result does.

**Its limit, stated plainly.** A secret with no recognisable shape and no keyword beside it, such as a random password
printed on its own, is not caught. The switch and the visible note are the backstops for that.

**Tests (free).**
- Each refusal family, the heuristic withholding and the counter.
- Hashes and UUIDs on their own stay.
- The switch, and that the note is raised.
- The alternate screen is never read.
- No write path is reachable.
- Placeholder strings that *look* like each credential format, never real values. Temp HOME and config paths
  throughout.

## Order of work

- **WP-A** (title bar, toggles, presets, pane model) and **WP-C** (terminal backend) run in parallel. One is
  frontend-only and the other backend-only.
- Then **WP-B** (several chats) and **WP-D** (terminal panel and view).
- Then **WP-E** (Preview) and **WP-F** (`terminal_read`, as above). WP-F needs WP-C's ring and the shell-integration
  marks.
