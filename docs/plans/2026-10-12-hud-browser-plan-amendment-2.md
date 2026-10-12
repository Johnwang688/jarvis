# Amendment 2 to the HUD Browser plan: dev servers open by themselves, from any source (2026-10-12)

**Status:** revised 2026-10-12 after review (PASS with changes). A read-only planner wrote it from the lead's brief.
- The lead's decisions L1–L8 are in §0, together with what the lead decided at review (R-1 to R-9). Both are planned as written.
- §J lists the decisions this plan takes where those are silent.
- §K lists the planner's challenges and how each was settled.

**Precedence:**
- This amends `2026-10-10-hud-browser-plan.md` and `2026-10-11-hud-browser-plan-amendment-1.md`.
- **Where it differs from amendment 1's BR-8b (§D.2, BD-7, and the BR-8b rows of §G and §H), this wins.**
- The decisions file (`2026-10-10-hud-browser-decisions.md`) still wins over every plan. Its "Amendment 2" section records L1–L8 and the review decisions.

**Builds on:**
- **main at a4c2a82.** It includes:
  - BR-1a (#69, merged): the gate, `policy.refused_ports`, and loopback closed until the owner opens a port. L2's corrections are already in its CLAUDE.md paragraph, its decisions bullet and amendment 1 §D.2.
  - BR-2's `lib/devopen.ts`, BR-3a's rename, and WP-G2's `splitPane(…, {focus})`.
- **PR #70** (BR-8a, `feat/browser-devurl`, 0d7b8da, open): `devurl.py`, the `dev_url` frame and `browser.json` → `auto_open`.
- **PR #72** (flaky suites, open): it touches `hud_v2_terminal_check.py` and `hud_v2_layout_check.py`, which AO-3's tests extend.

**The owner's request (2026-10-12, verbatim):**

> "Can you have Jarvis/claude/codex auto open localhost servers when they are started? If there is 8 panels all taken already then have them open a new tab in a browser panel. If no browser panels are open then have it ask which panel to open in or if I don't want to open it"

---

## For the owner: what changes

**Opening servers**
- **A server an agent starts now opens too,** not only one your terminal announces.
  - When a Claude or Codex chat or task starts a local web server, a Browser pane opens beside that chat.
  - Opening one never moves your keyboard focus, the chat you're typing in, or the mic.
- **When there's no room for another pane** (8 panes, or a window too small for one more), the server opens as a **new tab** in a Browser pane you already have. If you aren't using that pane right now, the new tab comes to the front.
- **When there's no room and no Browser pane,** a small box near the top of the window asks: "Dev server localhost:5173 from *chat name* — Open in pane: 1 … 8 · Don't open".
  - It never takes your focus.
  - It ignores clicks for half a second after it appears, so a click you'd already started doesn't land on it.
  - It goes away by itself when the server stops.

**Browser panes**
- **Browser panes get tabs,** like the editor's. A terminal link's "Open in Browser" now opens a **new tab** rather than replacing the page you had.
- **No page in a Browser pane can take your keyboard by itself.** A page can call `focus()` with no click, and in Chrome that pulls your typing into it, even from a hidden tab. That is true today, for every Browser pane.
  - Now a page that does it is closed at once, and the pane says "This page took the keyboard · Reload". Your keys stay where you were typing.
  - Clicking into a page, tabbing into it, or reloading it yourself still works as usual.
- **An auto-opened page is always the plain, sandboxed local view** (Direct mode). It can't reach Jarvis, can't keep its own origin, and never opens a port in your Remote browser.

**Your controls**
- **Nothing opens, asks or moves under an approval card.** It waits until the card is gone.
- **One switch turns all of it off:** Settings → Browser → "Open dev servers automatically" (on by default).

**The costs, plainly:**
- **An agent can now put a page in front of you.** It is sandboxed: a fake "Approve" button inside it does nothing, and it can't grab your keyboard.
- **The daemon checks this machine's listening ports about once a second while a Claude or Codex turn runs,** and for a while after.
  - Measured: 1.5–7 ms per check with 1,000 processes and up to 5,000 listening sockets.
  - It does nothing while no such turn is running.
- **Some servers are missed on purpose (§F):**
  - servers you start outside Jarvis;
  - servers whose agent cleared its environment (`env -i`);
  - servers on ports Linux hands out at random (test fixtures);
  - pages that refuse to be framed, and answers that aren't web pages (JSON).

---

## 0. The lead's decisions, recorded

### 0.1 L1–L8 (the brief)

| # | Decision |
|---|---|
| **L1** | **Sources.** (a) The owner's HUD terminals: BR-8a's banner detection. (b) Servers started by an agent (a chat or a task, on the fast path, Claude or Codex), detected as **new loopback TCP listeners**. Such a listener must: be owned by a process of the owner's uid; have a working folder, or a parent's, inside that thread's folder (`Thread.cwd`, the task's worktree, a project root); start while that thread has a turn running or within a grace period after it; not be a HUD terminal's process (BR-8a covers those); not be the daemon or anything of Jarvis's own, and never on Jarvis's ports; and answer HTTP on `/`. Process-tree descent alone is not enough (`setsid` and `nohup` reparent). Servers the owner starts outside Jarvis are **not** included. *The folder condition is replaced at review by R-2.* |
| **L2** | **Direct mode only.** An auto-opened server loads in WP-E's sandboxed frame, judged by `judgeBrowserUrl` with `/status` → `refused_ports`, never with keep-origin. **Auto-open never opens a port in the Remote browser's gate**; only the owner's own navigation does (#69's rule). This reverses the earlier note that BR-8b's auto-open counts as an owner-route navigation, because an agent can now trigger auto-open. |
| **L3** | **Placement, in order**, in L5's window. (1) **A pane can be added** (fewer than 8, `splitVerdict` allows it): split a new Browser pane beside the pane showing that chat, task or terminal, else beside the focused pane, with `{focus:false}`. DOM focus, `ws.focused` and the selected chat (W-6) never move. (2) **No pane can be added** (8 drawn, or `splitVerdict` refuses for room): a **new tab in a drawn Browser pane**, the one nearest the source, else the most recently used. (3) **No pane can be added and no Browser pane is drawn:** a **non-blocking prompt**, "Dev server `localhost:5173` from <name> — Open in pane: [1] … [8] · Don't open". Choosing a pane switches it to the Browser view with the URL. A pane holding an unsaved edit (ED-2 `paneLoses`), a VEX view mid-chain or a terminal's running program is not offered, or is offered with its reason. "Don't open" dismisses it. The prompt expires when the server stops listening. |
| **L4** | **Browser panes get tabs in Direct mode.** A strip like the Editor's: a list of URLs per pane, each judged again on load, with a keep-origin grant per tab stored as its origin. A cap on tabs; middle-click or × closes. Stored in `PaneSpec`, migrated from `browserUrl`. Built so BR-3b's Remote mode reuses the strip. |
| **L5** | **One window acts.** The window whose pane shows that thread or terminal opens it. Otherwise one window claims it: the most recently focused, through an owner-only claim route where the first claim wins. With no window open, nothing opens; the next window that loads shows a quiet notice listing servers still listening, with the same choices. |
| **L6** | **The gentle rules.** Nothing opens, prompts or moves under an approval card: it waits until the card goes, and never steals focus or the mic. De-dup per port while it keeps listening: a restart on the same port doesn't reopen a pane or tab that still shows it. Rate limit. BR-8a's `auto_open` switch (`browser.json`) covers every source; on by default. |
| **L7** | **What leaves memory.** Only a loopback URL (no userinfo, query or fragment) and the source's thread or terminal id. Never terminal text or agent tool output. Say which events carry what, and where they go. *Narrowed at review by R-5: no port on the bus.* |
| **L8** | **Stated limits.** An agent can put a page of its choosing in front of the owner, in a sandboxed frame that can't reach Jarvis. A server an agent starts and hands to the owner's own process is missed. A listener on 0.0.0.0 is loopback-reachable too, so it is included. |

### 0.2 Decided at review (the lead, 2026-10-12)

| # | Decision | Where |
|---|---|---|
| **R-1** (H-1) | **No Browser frame may take the keyboard without an owner gesture.** A frame that gains focus without one is unmounted at once, and the pane says "This page took the keyboard · Reload". This covers **every** Browser frame, the live Direct pane on main included, and ships in AO-2. A hidden or background frame gaining focus is always theft. Under a card, any frame gaining focus is theft | §A.12, §E.8, §F, §H AO-2 |
| **R-2** (M-1, M-3) | **An agent-started listener must carry its session's origin tag and be in the daemon's cgroup.** An untagged listener is not attributed, so L1's folder rule is moot and is dropped. A terminal's tmux, `setsid` or pm2 is never pinned on a chat; BR-8a's banners cover terminals. The cost: a child started with `env -i` is missed | §B.5, §B.6, §F, §K C3 |
| **R-3** (M-2) | **Rule 1 may also take a blank kept pane:** default spec, no thread, no words, no tabs, no terminal. A free id comes first. A kept pane holding anything is never taken | §E.2 |
| **R-4** (Q-A1, Q-A2) | **A non-page does not open** (JSON, a status page with no markup). **A new tab comes to the front** in a Browser pane the owner isn't using | §B.7, §E.2 |
| **R-5** (M-4) | **No port on any bus record.** The bus carries `id`, `state`, `source` and time only. The URL comes from the owner-only claim answer and `GET /dev-servers` | §D.1, §D.5 |
| **R-6** (M-5) | **The prompt sits away from every input bar** (Send, Stop, Steer) and ignores clicks for about 500 ms after it appears. **A rule-1 split waits while a pointer button is down** | §E.2, §E.4 |
| **R-7** (LOWs) | Read `environ` only for the owning pid, after the cheap rules, as raw bytes: never decoded, logged or raised with. For a non-2xx/3xx answer, require markup in the body. Skip ports 9222, 9229, 5678, 5432, 3306, 6379 and 27017, and keep GET. Skip 127/8 binds other than 127.0.0.1. Key `opened` by entry id. Renew the lease while a prompt or a wait is live, and let it lapse when the window's SSE disconnects. Show a visible "Opened for <chat> · localhost:5173". Tests count work rather than wall time, check an accept count of 0 for refused listeners, and cap live frames window-wide | throughout |
| **R-8** (NITs) | Split AO-1b so the watcher doesn't wait on #70. Poll only during Claude or Codex turns. Give slow binders a longer grace. Tell the owner "Open in Browser" now opens a new tab | §B.1, §G |
| **R-9** | #69 merged with the L2 corrections. §M keeps only #70's half | §M |

---

## A. Facts this amendment relies on

1. **The pieces already on main.**
   - `splitPane(ws, source, id, spec, {focus: false})` exists (WP-G2), and keeps `ws.focused`.
   - `__hud.split(source, {focus})` is its test hook.
   - `App.splitFrom` calls `splitVerdict`, `allocate(ws, dirty, holdingIds())` (which may take a ⊞-hidden pane and park its words) and `copySpec`.
   - `lib/devopen.ts` (BR-2) is pure and unwired: `autoOpenPlan` → `split | focused | offer | wait (30 s) | skip`, with per-terminal and per-window rate limits and an `opened` origin set per page.
2. **`splitVerdict` folds side panes for the render.** It refuses only when the next shape would drop a pane. To fit, it lets `fitWorkspace` fold a side pane or the panel (`sides.autoLeft`/`autoRight`, `panel.auto`). For a split nobody asked for, that is a visible move (§E.2).
3. **The fast path cannot start a process.** `FAST_TOOLS` holds no `run_command` or `run_readonly`, and `_validate_toolset` forbids them (§8.1). Work that would start a server becomes a task (`task_propose`) on Claude or Codex. So the watcher runs only during Claude and Codex turns (R-8).
4. **Codex runs commands in `workspace-write` with the network off.**
   - `codex_config.config_text` and `CodexProvider` require `sandbox_mode = "workspace-write"`.
   - A sandboxed command probably cannot bind a TCP port, or binds in a namespace the HUD cannot reach.
   - A Codex server appears only after a network widening, which the owner approves on a card (`permit(…, widening=…)`). AO-M1 measures this (§K, C2).
5. **Claude workers run unsandboxed**, as the owner's uid, in the daemon's cgroup. The CLI spawns its Bash tool's processes, and anything they start inherits the CLI's environment.
6. **Orphans stay in the daemon's cgroup.**
   - `jarvis-daemon2` runs in `…/app.slice/jarvis-daemon2.service` (cgroup v2, read 2026-10-12). The systemd user manager is the subreaper there.
   - A process that `nohup`s or `setsid`s itself and loses its parent is adopted by `systemd --user`, but it stays in the unit's cgroup, along with HUD terminals and the Claude and Codex processes.
   - The unit has `Delegate=no`, so nothing under it can make a sub-cgroup.
   - It has `KillMode=control-group`, so a daemon restart ends every agent-started server, as it ends the HUD terminals today.
   - Processes the owner starts in a Windows Terminal or VS Code shell sit in other cgroups. So does an agent's test run started with `systemd-run --scope` (this repo's rule).
7. **`/proc` on this WSL2:**
   - It is mounted `rw,nosuid,nodev,noexec,noatime`, with no `hidepid`, and `ptrace_scope` is 1.
   - Measured from a non-descendant: a same-uid process's `fd/*` and `environ` are readable.
   - A same-uid process holding capabilities, such as `systemd --user`, is not readable (EACCES).
   - Yama's scope 1 restricts only attaching.
8. **The ephemeral port range** is 32768–60999 (`/proc/sys/net/ipv4/ip_local_port_range`). A server bound to port 0 gets a port there. Dev servers use fixed ports below it: 3000, 4200, 5173, 6006, 8000, 8080, 8888 and so on.
9. **The HUD's own test servers would match L1 as written.**
   - In this repo an agent runs `MockDaemon` HUDs on ports 18000–19999 from its worktree, during its turn. Started from Claude's shell, they also carry the tag.
   - They answer HTTP on `/` with a HUD page that refuses to be framed (`frame-ancestors 'none'`).
   - Two rules filter them: a run under `systemd-run --scope` leaves the daemon's cgroup (rule 3), and the framing refusal (rule 11).
10. **BR-8a's frame reaches one window.** `{"type":"dev_url","url"}` goes on the terminal's attached socket only. With no window attached, the URL is dropped and not recorded.
11. **Codex's app-server is a session leader** (`RpcProcess`: `start_new_session=True`). The Claude CLI's pid is reachable only through SDK internals (`getattr`, not pinned).
12. **A framed page can take the keyboard with no user activation** (H-1; the reviewer reproduced it in Chrome 155).
    - `el.focus()` or `window.focus()` in an `allow-scripts allow-forms` frame on another port moves keyboard focus out of the HUD's text box. Later keys go to the page, which may send them to its own server.
    - None of these stops it: `inert`, `display: none`, `visibility: hidden`, re-focusing the box, or `contentWindow.blur()`.
    - A React-style focus on mount split the owner's typing between the box and the page.
    - Removing the iframe in the parent window's `blur` handler is what worked.
    - At risk: words for a chat, a `.env` value typed in the editor, a password typed in a terminal.
    - **This applies to every Browser frame on main today**, auto-open or not.

---

## B. Detecting a server an agent started (daemon)

There are two new modules:
- `jarvis/v2/devlisten.py`, pure: parsing, facts and rules, with an injectable `/proc` root;
- the watcher in `jarvis/v2/devservers.py`.

### B.1 When it runs

- **Only while `browser.json` → `auto_open` is on** (L6). Off means no thread, no `/proc` read and no probe.
- **Only during and after Claude and Codex turns** (R-8). Fast-path turns never wake it (§A item 3).
  - **1 s** cadence while such a turn runs, and for the first 120 s after it ends;
  - **5 s** cadence from then until `GRACE_S` = 600 s after the turn. That is the longer grace for slow binders (R-8): a first Rails or Next boot, or a compose stack;
  - **5 s** cadence, rows only, while the registry holds a listening entry (liveness, §D);
  - **off** otherwise. A turn start wakes it.
- **One thread, `jarvis-devwatch`.** It starts lazily at the first such turn and is stopped by `Daemon.stop` after the terminals, before the bus closes.
- **The daemon tells it about turns.**
  - `_begin` calls `turn_started(thread_id, provider)`, and `_turn`'s `finally` calls `turn_ended(thread_id)`.
  - Both are recorded on `CLOCK_BOOTTIME`. Only Claude and Codex turns are kept.
- **At start it takes a baseline.** Every listener already present is old, so a daemon restart never opens anything.

### B.2 Finding new listeners

1. Read `/proc/net/tcp` and `/proc/net/tcp6` whole, once per poll.
2. Keep rows in state `0A` (LISTEN). A cheap `b" 0A "` substring test runs before the split.
3. Keep a row only when its local address is **127.0.0.1, `0.0.0.0`, `::1`, `::` or `::ffff:127.0.0.1`** (kernel byte order).
   - **Other 127/8 addresses are skipped** (R-7). The HUD frames only `localhost`/`127.0.0.1` among them, and `localhost` never reaches `127.0.0.5`.
   - A listener bound to a LAN or `eth0` address is skipped too (L8: a wildcard **is** included).
4. **Refuse the port before anything else:**
   - `policy.refused_ports(...)` (on main since #69): `config.control_ports()`, the daemon's HUD and API ports, and the default workshop port unless it is this daemon's;
   - the gate's own port;
   - **the service ports** 9222 (Chrome DevTools), 9229 (Node inspector), 5678 (debugpy), 5432, 3306, 6379 and 27017 (databases) (R-7);
   - any port in the ephemeral range (§J AD-2).
5. **New** means an inode not in the previous poll's set. Rows are keyed `(inode, port)`.

`/proc/net/*` shows the daemon's network namespace: the root one, the HUD's. A listener in another namespace (a Codex `bwrap --unshare-net`, a container) is invisible here and unreachable from the HUD anyway.

### B.3 Who owns it (inode → pid)

- **Only when a poll finds a new candidate inode:**
  - read `/sys/fs/cgroup/<the daemon's own cgroup>/cgroup.procs`. The daemon's cgroup is read from `/proc/self/cgroup` at start, so a test daemon in a `systemd-run` scope uses its own scope;
  - then `readlink` `/proc/<pid>/fd/*` for those pids only, looking for `socket:[<inode>]`. At most one fd scan per poll.
- **The cgroup is the first filter** (R-2, §J AD-1).
  - A listener whose owner is not in the daemon's cgroup is never mapped. That excludes servers the owner starts outside Jarvis (L1), and test fixtures run under `systemd-run --scope`.
  - The unit is not delegated (§A item 6), so `cgroup.procs` lists every process the daemon's work started.
  - A daemon started from a shell rather than its unit sits in a login scope that also holds the owner's processes. There the scan does more work but finds nothing more, because the tag is required (§B.5).
- **Several pids holding one inode** (forked workers, an inherited socket): take the earliest-started pid that passes §B.5.
- **No pid found** (it died, or the socket moved out): the candidate is retried for 3 polls, then dropped.

### B.4 Facts about the process

**For the owning pid**, read `/proc/<pid>/stat` (ppid, session id, start time in ticks since boot) and the owner uid of `/proc/<pid>`.

**For its ancestors**, read only `stat`, up to `ANCESTRY_MAX` = 16. This is for the terminal check (rule 6). The walk stops at pid 1, the subreaper, the daemon or a provider root.

**No `cwd` and no `cmdline` are read.** The folder rule is gone (R-2), and rule 5 uses known pids.

**The origin tag, read only after rules 1–7 pass, and only for the owning pid** (R-7):
- open `/proc/<pid>/environ` `O_RDONLY | O_NONBLOCK` and read at most 128 KiB as **raw bytes**;
- find `b"JARVIS_ORIGIN_TAG="` at offset 0 or right after a NUL;
- take the value bytes up to the next NUL. They must be exactly 32 bytes of `[0-9a-f]`, compared as bytes against the live tags;
- **the buffer is never decoded, logged, formatted, put into an exception or passed out of the function.** A read error becomes the refusal class `unreadable`, with no message text. The buffer is dropped before the function returns.

Any read that fails (EACCES, a vanished pid, a pid whose start time changed) **fails closed**: no attribution.

### B.5 The rules, in order (L1 as amended by R-2)

The rules live in a pure function, `devlisten.attribute(listener, facts, sessions, now)`. It returns a thread or a refusal class. The classes are counted on `/status`, never logged per listener. Cheap rules come first; `environ` is read only at rule 8.

| # | Refused when | Class |
|---|---|---|
| 1 | The port is Jarvis's: §B.2 step 4's refused list, checked again here | `jarvis_port` |
| 2 | The port is a service port, or in the ephemeral range; or the bind is a 127/8 address other than 127.0.0.1 | `service_port`, `ephemeral`, `odd_loopback` |
| 3 | No owning pid in the daemon's cgroup | `outside` |
| 4 | The owner uid is not the daemon's | `uid` |
| 5 | The pid is **Jarvis's own**: the daemon, a provider root (a Claude CLI, a Codex app-server) or a BrowserHost Chrome or driver (`chrome_pids`). These are known pids; no `cmdline` is read. jarvis-mcp relays speak only stdio and never listen | `jarvis` |
| 6 | It is a **HUD terminal's**: its session id is a live terminal's session (with a matching leader start time, `terminals.session_members`' rule), or an ancestor is a terminal leader. Program terminals (VEX) count too | `terminal` |
| 7 | **No Claude or Codex turn window holds it.** A window runs from `turn_start` to `turn_end + GRACE_S` (600 s). The listener's first sighting must fall inside one, and the process must have started at or after that turn's `turn_start − START_SLACK_S` (2 s) | `no_turn` |
| 8 | **No origin tag** (R-2): `env -i`, a cleared environment, or an unreadable `environ` | `untagged`, `unreadable` |
| 9 | **The tag names no session whose window passed rule 7** (a closed session past its grace, or another thread's turn window) | `stale_tag` |
| 10 | It closes before it has been seen on two polls at least 2 s apart (`MIN_LIFE_S`) | `brief` |
| 11 | The HTTP probe (§B.7) does not show a page that may be framed | `not_http`, `not_page`, `no_frame` |

**What passes is attributed to the tag's thread.** There is **no folder rule**:
- R-2 makes the tag the attribution;
- an agent's `cd /tmp && python3 -m http.server 5000` opens too.

### B.6 The origin tag: the one attribution (R-2)

A process-tree walk fails once `setsid` or `nohup` reparents (L1). The environment does not: a variable survives `nohup`, `setsid`, a double fork and reparenting.

**How it is set.**
- `_run_brief` mints one 128-bit hex tag per Claude or Codex session.
- It goes into **the run copy of the brief only** (`Brief.origin_tag`, never `brief.json`), the way `mcp_servers["jarvis"]` does.
- The Claude provider adds `JARVIS_ORIGIN_TAG=<tag>` to `ClaudeAgentOptions.env`.
- The Codex provider adds it to the app-server's `env`. `shell_environment_policy` has `inherit = "all"`, and the name avoids Codex's default `KEY`/`SECRET`/`TOKEN` excludes.
- The registry maps tag → thread while the session lives, and for `GRACE_S` after it closes.
- HUD terminals never carry it: their environment is built from an allowlist.

**What it costs.** Anything started with a cleared environment is missed, and is never guessed at:
- `env -i`, or `sudo` without `-E`;
- a daemoniser that resets the environment;
- a process manager whose daemon was started before the turn (pm2's daemon, a long-running `docker` or `systemd-run`).

That is R-2's stated cost. An untagged process also means a terminal's tmux, `setsid` or pm2 server is never pinned on a chat.

**What it is not.** It is not a secret and not a boundary.
- Any same-uid program can read another process's environment and copy a tag. That only changes which chat a page is labelled with and where it lands, for a page it could show anyway.
- Copying the tag never reaches anything §D's routes guard: claims are the owner's window's.

### B.7 The HTTP probe

- **Only after rules 1–10 pass.** At most 2 probes in flight, one per listener.
- **Where it connects:** `127.0.0.1:<port>` for an IPv4 or wildcard bind, `[::1]:<port>` for `::1` or `::`.
  - It never connects to a non-loopback address.
  - The port is checked against §B.2's refused and service lists again **immediately before `connect`**.
- **What it sends** (R-7: keep GET): `GET / HTTP/1.0`, with `Host: localhost:<port>`, `User-Agent: Jarvis-devwatch`, `Accept: text/html` and `Connection: close`. No cookie, credential or body.
- **Bounds:**
  - connect within 1 s;
  - the status line and headers within 20 s (a Next.js dev server compiles `/` on its first request);
  - read at most 4 KiB, then close;
  - a refused or reset connection retries at 1, 2, 4 and 8 s, within `PROBE_BUDGET_S` = 30 s of the first sighting. Past that the listener is dropped, and not retried until its inode changes.
- **Redirects are never followed.**
- **What counts as a page** (R-4 and R-7, AD-3):
  - a **3xx**;
  - a **2xx** with `Content-Type: text/html`, or with no content type and markup in the body;
  - **any other status** only with `text/html` **and** markup in the body read: `<!doctype html`, `<html`, `<head` or `<body`, any case;
  - anything else is `not_page`: a 200 `application/json`, or a JSON or plain-text 404.
- **A page that refuses to be framed is skipped:** `X-Frame-Options` `DENY` or `SAMEORIGIN`, or a CSP `frame-ancestors` without `*` or `http://localhost:*`. That also drops `MockDaemon` HUDs (§A item 9), because a Direct frame could only draw an error.
- **TLS:** if the plain probe's first byte is `0x15` (a TLS alert) or the server closes without a byte, one TLS probe runs with verification off (loopback only). If that answers, the scheme is `https`.
- **The response is never kept.** Only the status class and the header facts survive, as booleans. Nothing is logged.

### B.8 Races

| Race | Handling |
|---|---|
| The server dies between the row and the fd scan | No pid; retried 3 polls, then dropped |
| A pid is reused | Facts are keyed `(pid, start_time)` and checked again before publishing |
| An inode is reused | Rows are keyed `(inode, port)`; a disappeared inode clears its state |
| The same port is rebound (a restart) | The registry is keyed by port. A listener returning within `DOWN_KEEP_S` = 60 s of `down` reuses the entry and its id: no second `up`, and a `dismissed` entry stays dismissed. Later, it is a new entry with a new id, and the HUD's "a tab shows this origin" check stops a reopen while a tab shows it (L6) |
| Forked workers share one inode | The earliest-started passing pid |
| Two sources claim one port (a terminal banner and the watcher) | One entry per port; the first source wins |
| A turn ends while the probe runs | The turn window is judged at first sighting, not at probe end |
| The daemon restarts | The baseline (§B.1) makes every existing listener old. The registry is memory only |
| WSL's localhost relay | `wslrelay` notices a new WSL listener after a moment. `MIN_LIFE_S` and the probe make an `up` arrive after the port is reachable from Windows |

### B.9 WSL specifics

- **No `hidepid`**, so all processes are visible. Other-uid processes are skipped: their `fd` folders are unreadable, and rule 4 refuses them.
- **Capability-holding same-uid processes** (`systemd --user`) refuse `fd` and `environ` reads, so they fail closed.
- **The HUD's Chrome runs on Windows.** A Direct frame reaches a WSL server through WSL's localhost forwarding.
  - Binds on `127.0.0.1` and `0.0.0.0` are forwarded.
  - A `::1`-only bind (Node ≥ 17 on `localhost`) depends on the relay. AO-M4 checks it; it is the same as today's typed Direct URLs.
- **Windows-side servers** started through interop (`cmd.exe /c npm run dev`) listen on Windows' loopback. They are not in `/proc/net/tcp`, so they are missed (§F).
- **Clocks:** process start times are ticks since boot (`CLK_TCK` = 100 here). Turn times use `CLOCK_BOOTTIME`, so a pause of the WSL VM does not skew the comparison.

### B.10 Cost, measured (2026-10-12, this machine, under the 3G cap)

The planner ran a throwaway script under `systemd-run --user --scope -p MemoryMax=3G`.
- It started N sleeping children and M loopback listeners on ephemeral ports in its own scope.
- It then timed each step of §B.2–§B.4 over the real `/proc`.
- It printed numbers only: no names, command lines, environments or paths.

AO-1b-i's cost check counts the same work (§H).

| Load | Steady poll (rows + diff) | Fd scan, uid-wide | Fd scan, own cgroup | Facts per pid |
|---|---|---|---|---|
| 1,151 processes, 557 LISTEN rows, 5,375 fds | **1.5 ms** | 15.7 ms | **8.1 ms** (1,052 pids) | 0.02 ms |
| 1,177 processes, 5,107 LISTEN rows, 9,472 fds | **7.0 ms** | 25.2 ms | **15.5 ms** (1,102 pids) | 0.01 ms |

`cgroup.procs` itself reads in 0.1 ms. The machine normally runs about 70 processes and 7 LISTEN rows.
- **Steady state:** one rows read per second while a Claude or Codex turn runs, at worst 0.7 % of one core with a GIL hold of at most 7 ms.
- **A new candidate** adds one fd scan (8–16 ms) and, for a candidate that passes rules 1–7, one capped `environ` read.
- **If ever needed:** an owner's machine holding tens of thousands of TCP rows could move the rows read to netlink `sock_diag` (LISTEN only, filtered in the kernel) without changing anything else. Not needed now.

---

## C. Servers the owner's HUD terminals announce (BR-8a), in the same registry

BR-8a's sniffer, rules and frame are unchanged. AO-1b-ii adds the following to `Terminal._sniff`, after #70 merges:

1. **Every URL the sniffer would send is registered:** `announce_terminal(terminal_id, url) → id | None`. That makes one entry per port, under the registry's rate limit.
2. **With a window attached:**
   - the frame gains the entry id: `{"type":"dev_url","url":…,"id":…}`;
   - the entry is **claimed for that window at once** (L5: the window showing the terminal opens it);
   - the bus gets a `claimed` record, which carries no URL or port (R-5), so no other window acts.
3. **With no window attached:**
   - the entry stays **unclaimed** and goes through §D.4's claim (§K, C6). This **reverses** BR-8a's "dropped and not recorded" for this one case;
   - the URL, with BR-8a's cleaned path, is held **in daemon memory** and returned **only** by the owner-only routes;
   - the bus carries the entry id and the terminal id alone.
4. **Liveness:** a terminal entry is `down` once its port has had no LISTEN row for 2 polls, counted from 5 s after the banner (a banner may print before the bind).
5. **#70's `test_nothing_reaches_the_bus_a_log_or_a_file` is amended on purpose.**
   - It asserts the bus's kinds are exactly `["terminal_attached"]`. With registration they become `["terminal_attached", "dev_server"]`.
   - It adds two checks: the `dev_server` record carries neither the marker nor the port, and its `data` keys are exactly `{id, state, source, at}`.

Terminal sources are never probed: the banner is the announcement, as BR-8a decided.

---

## D. The registry, events and routes (the contract)

`jarvis/v2/devservers.py` `DevServers` is attached as `daemon.devservers`. It lives in memory only and holds at most `ENTRIES_MAX` = 32 entries.

**An entry holds:**
- `id`: `ds_` plus 8 random hex characters;
- `port`, `scheme`, and `path` (`/` for agents; BR-8a's cleaned path for terminals);
- `source`: `{kind: "thread", thread_id, task_id|null}` or `{kind: "terminal", terminal_id}`;
- `state`: `up` → `claimed` → `placed` | `dismissed`, or `down` at any time. A claim whose lease lapses goes back to `up`;
- `first_seen`, and the claim (`window`, `lease_until`).

**Rate limits:** one new entry per 10 s per source, and six per minute across all sources. Past either, no entry is created; only a counter moves.

### D.1 The bus record (R-5)

```json
{"kind": "dev_server",
 "data": {"id": "ds_3f9a1c2e", "state": "up",
          "source": {"kind": "thread", "thread_id": "…", "task_id": null},
          "at": "2026-10-12T15:42:03Z"}}
```

- **States published:**
  - `up` (once per entry, and again when a lease lapses);
  - `claimed`, `placed`, `dismissed` and `down`.
- **Never in it:** a port, a scheme, a host, a path, a pid, a cwd, a command line, an environment, a process name, or anything from the probe's response.
- **Ids ride inside `data`,** as `activity` records do. With no top-level `thread_id`, no `?thread=` stream (the task runner's) carries it.
- **In the HUD,** an SSE event with no thread is W-6's "ambiguous input" and would reach the selected chat. So AO-3 takes `dev_server` before `routeEvent`, and it never reaches a pane's reducer.
- **Where it goes:** the bus fan-out, so SSE `/events` on both listeners.
  - It is never written to a thread's `log.jsonl`, the Discord mirror or the gateway.
  - `Activity` ignores the kind.

### D.2 The terminal frame

BR-8a's `{"type":"dev_url","url"}` gains `"id"`, the registry entry. The HUD's `parseControl` accepts it with or without the id, whichever lands first.

### D.3 Routes

**Every route** is on `hud_api.route`, behind **`projects.owner_only` plus a strict loopback Host, checked before the body is read**. On the API listener they answer 404/403, like `/browser/*`.

| Route | Body (exactly) | Answer |
|---|---|---|
| `GET /dev-servers` | — | `{"servers": [{id, url, state, source, claimed, mine}]}`, where `url` is `http://localhost:<port><path>`. Lists only entries still listening and not dismissed |
| `POST /dev-servers/<id>/claim` | `{"window": "<32 hex>", "shows": bool, "focused_at": int}` | `{"won": true, "url": …}` or `{"won": false}`. 404 if unknown; 409 `{"error", "code": "gone"}` if `down` or `dismissed` |
| `POST /dev-servers/<id>/renew` | `{"window": …}` | 200, with the lease extended. 409 if this window does not hold the claim |
| `POST /dev-servers/<id>/placed` | `{"window": …}` | 200; publishes `placed` and ends the lease. 409 if this window does not hold the claim |
| `POST /dev-servers/<id>/dismiss` | `{"window": …}` | 200; publishes `dismissed`. Any window may dismiss: the owner pressed "Don't open" |

- **`/events?window=<32 hex>`** on the HUD listener, from an owner request (`projects.is_owner`, a same-origin GET), ties that SSE stream to the window. When the stream ends, that window's claims lapse (§D.4). On the API listener, or from a request that is not the owner's, the parameter is ignored.
- **No route opens, starts, probes or navigates anything.** None of them reaches `/browser/*` or `daemon.browser`.
- **Frame headers** come from `end_headers` (WP-E). `tests/v2/frame_check.py` must learn the `dev-servers` segment, or it fails.
- **CSP is unchanged.** `frame-src` already names `http://localhost:*`, which also matches `https:` (CSP3).
- **`/status` gains `dev_watch`:** `{active, entries, polls, refused: {class: count}, work: {rows, fd_readlinks, environ_reads, probes}}`. Counts only; no port.

### D.4 The claim (L5)

`window` is the page's `pageNonce()` (`lib/terminal.ts`), held in memory only. It is sent only to owner-only routes, and never appears on the bus.

**The terminal case** is pre-claimed (§C).

**Otherwise every window that hears `up` claims at once:**
- `shows` is true when a drawn pane shows the source: a chat pane on that thread, or a task pane while the window's current task is that task;
- `focused_at` is the epoch ms of the window's last `focus` event, or 0;
- **a window under a card claims too.** Claiming is not acting: it acts once the card goes (§E.6).

**The daemon collects claims for `ARBITRATE_MS` = 600 ms from the first one, then awards one** (pure `devlisten.arbitrate`):
1. the earliest claim with `shows`;
2. else the claim with the newest `focused_at`;
3. ties go to the earliest claim.

Every claim is answered after the decision, a long poll of at most about 1 s. A claim arriving after the decision answers `won:false` while the winner's lease holds.

**The lease (R-7):**
- It runs `CLAIM_LEASE_S` = 90 s from the award.
- The winning window **renews it every 30 s while its prompt or its wait under a card is live**. It ends at `placed` or `dismiss`.
- It **lapses at once when that window's SSE stream ends** (the window closed or reloaded), and otherwise when it is not renewed.
- A lapsed entry goes back to `up`, unclaimed, and is published `up` again. Windows still open claim it afresh, and a window loaded later lists it (§E.5).

**No window connected:** nobody claims, and the entry waits for a window to load.

### D.5 What leaves memory (L7, R-5)

| What | Carries | Goes to | Never carries |
|---|---|---|---|
| Bus `dev_server` | id, state, source kind and id, time | SSE `/events` on both listeners | port, URL, path, pid, cwd, cmdline, env, process name, probe response |
| Terminal frame `dev_url` | BR-8a's cleaned URL (with path), id | That terminal's attached socket only | — |
| `GET /dev-servers` and the claim answer | URL (`/` for agents; the cleaned path for terminals), state, source | The owner's HUD window only (owner-only) | — |
| The probe | `GET /` to the server itself | Loopback, the server's own port | Cookies, credentials, any Jarvis or service port |
| Log lines | Counts and refusal classes ("dev watch: 1 server up (thread)") | The journal | Port, path, thread title, process facts, any `environ` byte |
| `/status` → `dev_watch` | Counts | Any local client | Ports |
| The HUD's stored workspace | A Browser pane's tab URLs, as `browserUrl` already is | The owner's browser (localStorage) | — |

**Can an agent read the list?**
- An agent on the API listener can read `/events`. It sees entry ids, states, source ids and times. No port, URL or path.
- It cannot read `GET /dev-servers` or a claim answer.
- **No tool, MCP tool, fast-path tool or Discord verb reaches `devservers` or `devlisten`.** A grep pin allows only the daemon, `hud_api` and `terminals` to import them, as `terminal_check` does.

---

## E. The HUD

### E.1 What a window does with a server

1. **It hears of it:** SSE `dev_server up` (any source), or a terminal frame `dev_url` with an id (already claimed for this window).
2. **It claims it (§D.4)**, unless it came as a terminal frame.
   - On `won:false` it does nothing more.
   - On `won:true` it now has the URL, from the claim answer.
3. **It plans** with `devopen.autoOpenPlan`, rewritten in §E.2. The plan judges the URL, de-dups, rate-limits and places.
4. **Under a card, or while a pointer button is down, it waits** (§E.6), renewing the lease.
   - When the wait ends, it checks again that the entry is still this window's and not `down`.
5. **It acts:** a split, a tab or the prompt. It then posts `placed`, or `dismiss` on "Don't open".

State lives outside React in `state/devservers.ts`, like `state/vex.ts`. The pure rules stay in `lib/devopen.ts`.

### E.2 Placement (L3), exactly

`autoOpenPlan(state, window, now)` answers one of:
- `split{source, url}`
- `tab{pane, url, activate}`
- `prompt{entryId, url}`
- `wait{why: "card" | "pointer"}`
- `skip{why}`

**The URL first.**
- It must pass `judgeBrowserUrl(url, ports)` and devopen's own refused-port set, which takes `/status` → `refused_ports` (on main since #69).
- The query and fragment are dropped.
- A URL the Direct judge refuses (`[::1]`, `127.0.0.2`, a control port) is `skip{refused}`. **It is never sent to Remote instead** (L2).

**De-dup and the rate limit** (BR-2's, re-keyed):
- **`opened` is keyed by entry id** (R-7). An entry this page acted on, or offered, never acts again.
- **A tab already showing that origin** in any pane, drawn or hidden, means `skip{duplicate}`. A restart, or BR-8a and the watcher both seeing one port, therefore opens nothing new (L6).
- **Rate:** one per 10 s per **source key** (`thread:<id>`, `task:<id>` or `terminal:<id>`), and three a minute per window. Past either, `skip{rate}`.

**Rule 1: a new pane.**
- **The verdict:** `autoSplitVerdict(layout, ws, avail, availH, prefer, preferPanel, blank)`, new in `lib/workspace.ts`. It is `splitVerdict`'s count and room check, plus three things:
  - **the id:** `freeId(ws)`, the lowest id neither drawn nor `kept`. Else the **oldest blank kept pane** (R-3). Blank means:
    - its spec equals `defaultPane(n)`: its default view, no pin, no terminal, no Browser tabs, no editor tabs, no `filePath`;
    - App's `blank` list adds that its compose row holds no thread, no words, no files and no hand-back.

    `allocate`'s other tiers are never used, so a ⊞-hidden pane that holds anything is never taken or discarded;
  - **the trial fit must not fold anything the current render shows.** The trial's `sides.autoLeft`/`autoRight` and `panel.auto` must not be newly true, and it must drop no pane;
  - **while any pointer button is down** in the window (a drag, a selection, a click in progress), the plan answers `wait{pointer}` until 150 ms after the last button goes up (R-6). Tracked by a window capture listener on `pointerdown`/`pointerup`/`pointercancel`.
- **The source pane:**
  - a chat source: the drawn chat pane whose conversation is that thread;
  - a task source: a drawn task pane while the window's current task is that task;
  - a terminal source: the drawn pane whose `terminalId` it is;
  - else the focused pane, if drawn.
- **The new pane:**
  - App's `splitFrom` gains a spec argument, so the new pane takes a fresh spec rather than a copy: `{view: "browser", projectId: <the source's project or null>, terminalId: null, browser: {tabs: [{id, url, auto: {kind, label}}], active: id}}`;
  - **focus:** with `{focus: false}`: `ws.focused` unchanged, `focusPaneElement` not called, `select` not dispatched. The selected chat and the mic stay put (W-6);
  - **a blank kept pane** is released (`releasePane(id, {keep: true})`) before the split, which drops nothing since it holds nothing.

**Rule 2: a tab.** This applies when rule 1 fails: 8 drawn, no free or blank id, or no room without a fold.
- **Which pane:**
  - the drawn Browser pane nearest the source's slot (smallest slot distance; on a tie, the one after it);
  - with no source pane drawn, the most recently used Browser pane. That is App memory: the last owner pointer or key in the pane, or the last tab change the owner made;
  - a pane at the tab cap is skipped.
- **Whether it comes to the front (R-4):** `activate` is true unless that pane is **in use**. In use means any of:
  - it is `ws.focused`;
  - DOM focus is inside it, the frame included;
  - the owner used it in the last 10 s;
  - BR-9c later: an agent is driving it.
- **A tab added behind** gets a dot (accent colour, never amber) and a one-line note in the strip ("new tab: localhost:5173"). Nothing moves.
- **A tab brought to the front** keeps the previous one as a tab behind it. That page stays live, or reloads when picked if it is past the live-frame caps (§E.3).

**Rule 3: the prompt** (§E.4).

### E.3 Tabs in Direct mode (L4)

**The model** (`lib/workspace.ts`):

```ts
interface BrowserTabSpec {
  id: string;            // 6 random base-36 characters, unique in the pane
  url: string;           // judged again on every load
  keepOrigin?: string;   // WP-E's grant, per tab, stored as the origin it was given for
  auto?: { kind: "thread" | "task" | "terminal"; label: string };  // opened by auto-open
  // BR-3b adds: mode?: "remote", remote?: {tab_id}. A Private tab stores no url (amendment 1 §C).
}
interface BrowserTabs { tabs: BrowserTabSpec[]; active: string }
// PaneSpec.browser?: BrowserTabs   (replaces browserUrl/keepOrigin)
```

**Caps.**
- `BROWSER_TABS_MAX` = 12 tabs per pane, B-13's number. At the cap, `+` refuses with a sentence, and auto-open treats the pane as full. BR-1b's global cap of 12 Remote tabs still applies to Remote tabs.
- **Live frames per pane:** the `LIVE_TABS` = 3 most recently active tabs keep their iframes mounted. The background ones are `display: none`, `inert` and `aria-hidden`.
- **Live frames per window:** `LIVE_FRAMES_WINDOW` = 8 (R-7). Every drawn Browser pane's active tab counts first, then background frames oldest-dropped.
- A tab past either cap unmounts, and reloads its URL when picked again (AD-9).
- A background live frame is still subject to §E.8: one that takes focus is unmounted.

**Grants.**
- A tab's keep-origin applies only while `keptOrigin(tab.keepOrigin, tab.url, ports)` passes. It is dropped from storage when it doesn't (WP-E's rule, per tab).
- Switching tabs never carries a grant.
- **An auto-opened tab never gets one** (L2). Only the owner's tick on that tab adds one.

**The label (R-7).**
- An auto-opened tab has a mark on the tab.
- While it is active, its pane shows a visible line in the toolbar: **"Opened for <name> · localhost:5173"**. That is text in the pane chrome, not a tooltip. The name is the chat's `threadLabel`, the task's title or the terminal's title, capped at 60.

**Migration** (`parsePane`).
- A valid `browser` is parsed per tab: bad tabs dropped, unknown fields dropped, at most 12, `active` falls back to the first.
- With none, `browserUrl ?? previewUrl`, with `keepOrigin` while it still matches, becomes one tab.
- Saves write `browser`, **and mirror the active tab's `browserUrl` and `keepOrigin` for one release**, so a rolled-back HUD still shows the active page.

**Helpers.**
- `addBrowserTab(ws, pane, url, {activate, auto})`, `closeBrowserTab`, `activateBrowserTab` and `setTabKeepOrigin`.
- `setBrowserUrl` now sets the **active** tab's URL.
- `copySpec` copies every tab's URL with new ids and the active index. It never copies a grant or `auto` (WP-G2's rule, per tab).

**The strip** (`BrowserTab.tsx`).
- A `role="tablist"` row above the toolbar, styled like the Editor's tabs.
- Each tab is a button labelled `host:port` plus the path: cleaned (`remote.cleanTitle`), capped at 40, React text only, never a page title (Direct mode cannot read one).
- × (`browser-tab-close`) and middle-click close a tab. Closing the last tab leaves "Nothing loaded".
- `+` (`browser-tab-new`) opens a blank tab with the URL box focused.
- Arrows move between tabs. There are no window keys (Ctrl+Tab and Ctrl+W are Chrome's).
- Under a card, everything is disabled and inert (the workspace is inert).

**The terminal's "Open in Browser"** (`openBrowser`, the owner's click) now opens a **new tab, brought to the front**, in the target pane, instead of replacing its page (R-8 tells the owner). A tab already on that URL is brought to the front instead.

**For BR-3b.**
- The strip takes a tab list and an active id, and renders any tab.
- Remote tabs add their mode and daemon tab id. A pane may hold Direct and Remote tabs together.
- The profile, "Jarvis can see" and "drive" stay per pane (amendment 1 §C, §E).
- A Remote tab is a canvas, not an iframe, so §E.8 does not apply to it. BR-3b must keep it that way.

### E.4 The prompt (L3, rule 3)

**Where (R-6).**
- A floating box at the **top of the centre**, horizontally centred, 8 px below the title bar.
- It is **never over a chat input bar** (Send, Stop, Steer), which sit at the panes' feet. It may cover part of a pane header.
- It is inside the workspace, with a z-index below `#authveil`, so it is inert behind a card like everything else.
- At most 3 are shown; more collapse into "+N more".

**What it shows.**
- One line: "Dev server `localhost:5173` from **<name>**". The name is text, capped at 60.
- Then "Open in pane:" and one button per drawn slot, labelled with the slot number and the pane's view label ("1 Chat", "3 Editor"). These are slots, never ids (G-1).
- Then "Don't open".

**Arming (R-6).** For `PROMPT_ARM_MS` = 500 ms after the prompt appears, or after it moves (a resize, a fold, another prompt above it), it ignores clicks and keys. Its buttons are drawn but `aria-disabled`. A click the owner began on what was there before can't land on it.

**Which panes are offered** (judged when drawn, and again on click):

| Pane | Offered? |
|---|---|
| A File pane holding the last view of an unsaved edit (`buffers.paneLoses`) | **Disabled**: "has an unsaved edit" |
| A VEX view while this window's chain runs (the `vex` store) | **Disabled**: "a VEX download is running" |
| A terminal pane whose terminal is `busy` | Offered, with "its program keeps running in the panel". The terminal goes back to the panel (WP-G2's `releaseToPanel`); it is never closed |
| The source terminal's own pane | Offered, with "this is where it started" |
| A chat pane with words in its box or a turn running | Offered, with "the chat keeps its words and its turn". A pane switched away keeps its conversation (WP-B) |
| Anything else | Offered |

**Choosing.** Choosing pane N switches it to the Browser view with one tab on the URL. This is the owner's click, so it may change the selected chat, as any view switch does. The window then posts `placed`.

**Focus and keys.**
- It **never takes focus**. Every button has `noPointerFocus`, so a click leaves focus where it was.
- Keys work **only while focus is inside the prompt** (Tab reaches it) and it is armed: `1`–`8` pick that slot, and Escape is "Don't open". They stop there.
- Digits typed in a chat box or the editor never reach it.

**Under a card** it is not rendered (it is removed in a layout effect before paint), and it comes back when the card goes. Its key handler also refuses while `blocked`.

**It goes away on:**
- `down`, `dismissed`, or `placed` by another window;
- "Don't open" here, which posts `dismiss` so other windows drop it too.

Nothing else expires it (L3). While it is shown, the window renews the lease.

### E.5 The notice when a window loads (L5)

After `/status` answers, the window reads `GET /dev-servers`. It keeps entries that are `up` and unclaimed, including lapsed claims.

It lists them in the same component and the same place, as a quiet notice: no sound, no focus, a muted style, armed like the prompt. Each line says "Dev server localhost:5173 from <name> is listening · Open · Don't open".
- **Open** claims, then runs §E.2's placement as if the server had just started. If placement ends at rule 3, Open expands into the pane buttons.
- **A lost claim** says "opened in another window".

### E.6 The gentle rules (L6, R-6)

**Under a card:**
- no split, tab, prompt or view switch. The plan answers `wait{card}`;
- the entry waits while it is listening, **until the card goes**, with a 10-minute backstop (AD-14; this replaces BR-2's 30 s expiry);
- several waiting entries keep their order, at most 4; the oldest is dropped past that;
- after the card, the plan runs again on the window as it is then.

**While a pointer button is down:** `wait{pointer}` for a rule-1 split (§E.2).

**Focus:**
- DOM focus, `ws.focused`, the selected chat and the mic are never moved by placement;
- no frame may take the keyboard without an owner gesture (§E.8).

**De-dup per port** and **the rate limit:** §B.8 and §E.2.

**One switch:** `browser.json` → `auto_open`.
- Off stops the daemon's watcher and BR-8a's sniffer.
- The HUD also skips (`skip{off}`) any record already in flight.

### E.7 Direct only (L2), as code

- `devopen.ts`, `state/devservers.ts` and `DevServerPrompt.tsx` **never import** `state/browser` (BR-3b's Remote manager), and never call `/browser/tabs` or `/navigate`. A vitest grep pins it.
- The new tab and the new pane carry **no `mode`**, so they are Direct, and **no `keepOrigin`**.
- A URL the Direct judge refuses is skipped (§E.2).
- The mock records every `/browser/*` request, and AO-3's suite asserts the count is **0** across every auto-open.

### E.8 No frame takes the keyboard (R-1; in AO-2, for every Browser frame)

**The rule:** a Browser frame holds the keyboard only when the owner put it there.

**Who owns the iframe.**
- Each Browser iframe is created and owned **imperatively** by a small `FrameHost` (a `useLayoutEffect` that creates the element, the way the terminal panel moves xterm's element), never rendered by JSX.
- The guard can therefore remove it from the DOM at once without React's reconciler tripping over a missing node.
- Every Browser iframe carries `data-browser-frame="<pane>:<tab>"`.

**Detection** (`lib/frameguard.ts` pure, with one window-level hook in App). Any of three triggers runs the same decision:
- the HUD window's **`blur`** event, when `document.activeElement` is a Browser iframe. This is what the reviewer found works; it fires synchronously as focus moves, before any key reaches the page;
- a **`focusin`** on the document whose target is a Browser iframe. That catches focus moving from one frame into another, when the window is already blurred;
- a **250 ms check** of `document.activeElement` while any Browser frame is mounted, as a belt.

**The owner's gesture for that frame:** the focus is allowed only if one of these holds.
- **The pointer is over it.** The parent saw `pointerover` on that iframe element (or its pane's frame area) and no `pointerout` since. A click into the page is always this case.
- **Tab into it.** A Tab or Shift+Tab keydown in the HUD within the last `TAB_MS` = 300 ms, and the frame is where focus landed.
- **The owner's own load of that tab.** Their Load, Enter in the URL box, Reload (the theft notice's included), or picking the tab, from that action until 2 s after the frame's `load` event. So a page that focuses on load still works when the owner opened it. An auto-open is never this case.
- **Fallback, only if AO-2's checks show a missed case:** if `pointerover` misses a frame drawn under a resting pointer, AO-2 adds a transparent click shield over an unfocused frame. It takes the first `pointerdown`, records the gesture and lets later ones through.

**Never a gesture, whatever the pointer says:**
- **a card is up:** any frame gaining focus is theft;
- **a hidden or background frame:** a frame that is not the active tab of a drawn pane gaining focus is always theft.

**On theft, synchronously in the handler:**
1. **Remove the iframe element from the DOM.** That is what returns the keyboard: `inert`, hiding and `blur()` do not.
2. **Put focus back** on the HUD element that last had it before the theft. The guard tracks `focusin` on HUD elements, and that element keeps its selection. With none, focus goes to the selected chat's box; under a card, to the card (its own focus rule, so Escape still denies).
3. **Mark the tab `took-keyboard`** (memory only, never stored). The pane shows **"This page took the keyboard · Reload"** (`browser-stolen`, `browser-stolen-reload`). The tab and its URL stay.
4. **Log nothing.** The console gets one line naming the pane slot, never the URL.

**No key reaches the page.**
- The `blur` and `focusin` handlers run as focus moves, before the next key event is dispatched to the page.
- The 250 ms check is only a belt: a key typed in that gap would already have gone to the page, which is why the events come first.

**Scope.**
- Every iframe a Browser pane renders, Direct, live or background: the workshop's "Project root" page, typed URLs and auto-opened pages alike.
- No other iframe exists in the HUD.
- Remote mode (BR-3b) draws a canvas, and the HUD forwards keys itself.

---

## F. Stated limits

**(L8) An agent can put a page of its choosing in front of the owner.**
- It is a Direct frame, sandboxed `allow-scripts allow-forms`, with no keep-origin.
- The HUD and API refuse to be framed (`frame-ancestors 'none'`), and the judge refuses Jarvis's ports, so it cannot reach Jarvis. A fake "Approve" button in it does nothing.
- It cannot take the keyboard without the owner's gesture (§E.8).
- It can still show anything, including text telling the owner to do something, and a sandboxed page can still run script and post forms to its own server.

**What remains of focus theft (R-1).**
- **A page can take focus while the owner's pointer rests over its frame,** even if the owner is typing elsewhere: a parked mouse counts as "over" (the reviewer's rule).
- **Once the owner has clicked into a page,** that page has the keyboard, as any page does, until the owner clicks elsewhere.
- **A key typed in the instant between a focus change and the guard's 250 ms check** could reach the page only if neither `blur` nor `focusin` fired. The checks show they do in Chrome 155.
- **IME composition** in progress in the HUD box may be cut by a theft.
- **None of this is a boundary against a same-uid program.**

**(L8) A server an agent starts and hands to another process is missed:**
- a launcher outside the cgroup, a Windows program, or a process manager started before the turn.

**(L8) Wildcard binds (`0.0.0.0`, `::`) are included.** They are also reachable from the LAN, which this feature neither causes nor fixes.

**Missed on purpose:**
- servers the owner starts outside Jarvis (L1), and anything outside the daemon's cgroup, including an agent's `systemd-run --user` server;
- **an agent's server whose environment was cleared** (R-2): `env -i`, `sudo`, a resetting daemoniser;
- ephemeral ports (AD-2), so a dev server an agent starts with `--port 0` is missed; service ports (9222, 9229, 5678, 5432, 3306, 6379, 27017); 127/8 binds other than 127.0.0.1;
- non-pages (R-4): JSON, and error pages with no markup;
- pages that refuse framing;
- servers alive under 2 s;
- servers from fast-path turns, which cannot start one.

**Missed by nature:**
- Windows-side servers (interop);
- servers in another network namespace (containers, or a Codex `bwrap --unshare-net`);
- a server that only starts listening more than 600 s after its turn.

**The tag** can be copied or stripped by a same-uid program (§B.6). It is attribution, not a boundary.

**The daemon reads one other process's environment per candidate** (`/proc/<pid>/environ`, same uid, capped at 128 KiB).
- That environment can hold secrets. Only one key is searched, as raw bytes; nothing is decoded, kept or logged (§B.4).
- It is a read the daemon could always make, now made on purpose and only after the cheap rules pass.

**Not a boundary against a same-uid program,** like every HUD rule. Such a program can open a tagged listener in the daemon's cgroup, or simply start a browser itself.

---

## G. Packages

**Common to every package:**
- free tests, with a temp HOME and config and the daemon on port 0;
- never 8402, 8403 or 8405;
- every test under `systemd-run … MemoryMax=3G`;
- docs in the same PR: CLAUDE.md, `docs/hud-api.md` and design §18;
- each check shown to fail against its named mutation in a scratch copy.

**In flight:** #70 BR-8a and #72 (flakes) are open; #69 BR-1a has merged. BR-1b and BR-1c come next. BR-3b is Remote mode.

| Package | What | Size, risk | Starts | Touches shared files |
|---|---|---|---|---|
| **AO-0** | This plan; the decisions file's "Amendment 2"; amendment 1 marked superseded where it differs; CLAUDE.md's L2 decision and the frame-focus gap | S | this PR | docs only |
| **AO-2** | **First, the focus guard for every Browser frame (§E.8)**: `FrameHost`, `lib/frameguard.ts`, the notice. Main is exposed today, so the guard can merge on its own if tabs slip. **Then Direct tabs (§E.3)**: `PaneSpec.browser`, migration, helpers, `copySpec`, the strip, the live-frame caps, the label, `openBrowser` → new tab | M–L, high (the guard), medium (tabs) | **now** | `workspace.ts`, `BrowserTab.tsx`, `App.tsx` (one hook, `openBrowser`), `theme.css`, `hud_v2_browser_check.py`, `hud_v2_mock.py` (the `dev_port` pages) |
| **AO-1a** | `jarvis/v2/devlisten.py`, pure: the rows parser, address and port classes, the facts readers over an injectable `/proc` root, the raw-bytes tag finder, `attribute()`, the probe-response parser, `arbitrate()` | M, medium | **now** (new files) | none |
| **AO-1b-i** | `jarvis/v2/devservers.py`: the watcher thread, registry, probe, routes (claim, renew, placed, dismiss, `GET`), lease lapse on SSE end, `/status` → `dev_watch`. Daemon wiring: Claude and Codex turn start and end, `Brief.origin_tag` in the run copy, stop order. The tag in `providers/claude.py` and `providers/codex.py`. `frame_check` learns `dev-servers`. It reads `auto_open` through one seam (`devservers.enabled()`), so it does not wait on #70; whichever of the two lands second makes the seam read #70's settings loader | L, high | after AO-1a; parallel with BR-1b and BR-1c (small hunks in `daemon.py` and `hud_api.py`) | `daemon.py`, `hud_api.py`, `claude.py`, `codex.py` |
| **AO-1b-ii** | `terminals._sniff` registration, the frame's id, the unattached-terminal claim, and #70's bus test amended on purpose (§C) | S, medium | after #70 merges and AO-1b-i | `terminals.py`, `terminal_check.py` |
| **AO-3** | Auto-open in the HUD; **replaces amendment 1's BR-8b**. `devopen.ts` rewritten (sources, rules 1–3, the `wait` reasons, `opened` by entry id); `autoSplitVerdict`, `freeId` and the blank kept pane; `splitFrom(source, opts, spec)`; `state/devservers.ts` (claim, renew, placed, dismiss, the load notice, the `/events?window=` stream); `DevServerPrompt.tsx`; SSE `dev_server` handling and Terminal's `dev_url` id; the mock routes (`hud_v2_mock_devservers.py`, calling AO-1a's `arbitrate`) | L, medium-high | after AO-1b-i and AO-2 (the terminal half after AO-1b-ii); merges main after #72 | `App.tsx`, `Terminal.tsx`, `devopen.ts`, `workspace.ts`, `hud_v2_mock.py` |
| BR-3b | Remote mode, **reusing AO-2's strip** | L, high | after BR-1b, BR-1c, BR-2, BR-3a **and AO-2** | as amendment 1 |

**Order:**
1. AO-2 (the guard first) and AO-1a start now, beside BR-1b and BR-1c.
2. AO-1b-i follows AO-1a. AO-1b-ii follows #70.
3. AO-3 follows AO-1b-i and AO-2. Its terminal half follows AO-1b-ii.

The owner sees terminal servers open once AO-3 lands. BR-8a without AO-3 still opens nothing: `parseControl` drops the frame.

---

## H. Tests

### AO-2: the focus guard (R-1)

Suite: `tests/face/hud_v2_browser_check.py`, plus a new `frameguard.test.ts`.

**The mock's `dev_port` server gains:**
- `/focus-mount`: focuses an input on load, React-style;
- `/focus-timer`: calls `window.focus()` and an input's `focus()` every 400 ms;
- `/focus-load`: focuses once on its `load` event;
- a key log: each page posts every key it receives to `/keys`, and the mock counts them (`dev_keys`).

**Before the guard rows count:** they first show the theft happens without the guard (the mutation). If headless Chromium cannot reproduce it, these rows run in a headed Chromium under WSLg, as the reviewer's Chrome 155 did.

| Check | Mutation it must catch |
|---|---|
| A `/focus-mount` page loaded with no gesture (stored and restored, or opened by the terminal's "Open in Browser" while the pointer is elsewhere), while the owner types 50 characters in the chat box: the frame is removed and `browser-stolen` shown; the box holds all 50 characters, in order; `dev_keys` is **0** | guard removed |
| `/focus-timer` in a drawn pane, while the owner types 200 characters in the chat box and then in the editor (a fake `.env` buffer): every character lands where it was typed; `dev_keys` is 0; the frame is removed on its first grab | guard removed |
| `/focus-timer` in a **background live tab** (`display: none`) while the active tab shows another page: that frame is removed; the active tab is untouched; `dev_keys` is 0 | background frames exempted |
| **Under a card**, a frame grabs focus with the pointer over it: it is removed; the card keeps focus; Escape denies; `dev_keys` is 0 | the card case exempted |
| A grab from frame A into frame B, after the owner clicked into A (focus already in a frame, so no `blur` fires): B is removed and focus returns to the HUD element the owner last used, by the `focusin` path | only window `blur` watched |
| **Gestures still work.** Clicking into a page (pointer over): focus stays and keys reach it (`dev_keys` counts them). Tab from the URL box into the frame: allowed. Reload on the notice, then `/focus-load`: it keeps focus | the pointer-over or own-load gesture missing (an over-refusal mutation) |
| After a removal: no React error in the console, the pane draws the notice, and Reload makes a fresh frame | iframe rendered by JSX (`removeChild` error) |

### AO-2: tabs

Suites: vitest (`workspace.test.ts`, `browser.test.ts`) and `tests/face/hud_v2_browser_check.py`.

| Check | Mutation it must catch |
|---|---|
| Migration: `browserUrl` + `keepOrigin` (and `previewUrl`) become one tab with the grant while it matches; saves write `browser` and mirror the active tab's `browserUrl` | migration dropped; mirror missing |
| Each tab's grant covers only its own origin; switching tabs carries none; `copySpec` copies URLs, never grants or `auto` | grant per pane |
| A stored tab on 8405 (or the reported API port) is refused with the sentence on load, and the other tabs load | stored tab trusted |
| Cap of 12: `+` refuses with a sentence; × and middle-click close; closing the last tab leaves "Nothing loaded" | no cap |
| At most 3 iframes per pane and 8 per window (four Browser panes with 3 tabs each: 8 mounted, the oldest background ones dropped); a dropped tab reloads its URL when picked | no window-wide cap |
| The terminal's "Open in Browser" opens a new tab brought to the front; the old page is still a tab | replaces the page |
| A hostile path (bidi, markup, 5,000 characters) is a capped text label, and `document.title` is unchanged | label through `innerHTML` |
| An auto-opened tab shows the visible "Opened for <name> · localhost:5173" line | label only in a tooltip |
| Under a card the strip is disabled and inert, and Escape denies | strip live under a card |

### AO-1a: `tests/v2/devlisten_check.py`

Pure: fixture text and a temp proc root.

| Check | Mutation it must catch |
|---|---|
| Only state `0A` rows count. IPv4 and IPv6 are read in kernel byte order. `127.0.0.1`, `0.0.0.0`, `::1`, `::` and `::ffff:127.0.0.1` pass; `127.0.0.5` is `odd_loopback`; a 192.168 or 172.x bind is skipped | IPv6 words read big-endian; ESTABLISHED counted; any 127/8 accepted |
| `refused_ports`, the workshop, the daemon's bound ports, the gate's port and the seven service ports are refused in every family, before any other rule | refused set applied after attribution |
| The ephemeral range is read from the file (fallback 32768–60999), ends inclusive | range ignored |
| **Tag required (R-2):** an untagged process is `untagged` whatever its folder and turn | untagged attributed (a folder fallback re-added) |
| **No folder rule:** a tagged process whose cwd and ancestors' cwds are all outside the thread's folders (`/tmp`) is attributed to the tag's thread | folder rule applied to a tagged process |
| The turn window: first sighting inside `[start, end + 600 s]` and process start at or after `start − 2 s`. A process started before the turn and bound inside it is refused; one first seen at end + 601 s is refused; a fast-path turn opens no window | grace ignored; process start ignored; fast-path turns counted |
| A tag naming a closed session past its grace, or a thread whose window does not hold the listener: `stale_tag` | — |
| A HUD terminal's session (leader start matches) and a terminal-leader ancestor are refused; a reused session number with another start time is not a terminal | leader start not checked |
| The daemon, provider-root and Chrome pids are refused without reading `cmdline` (a counter shows 0 `cmdline` reads) | — |
| Facts fail closed on EACCES, a vanished pid and a changed start time | EACCES read as a match |
| **The tag finder:** a fake environment holding `OPENROUTER_API_KEY=sk-or-v1-FAKE…` and the tag returns only the 32 tag bytes. The fake value appears in no return value, exception text, `repr`, log record or `/status`. A 1 MiB environment is read only to 128 KiB. A key without a preceding NUL (`XJARVIS_ORIGIN_TAG=`) is not matched. `environ` is read only after rules 1–7 pass (a counter) | the buffer decoded or put in an exception; no cap; read before the cheap rules |
| The probe parser: 3xx passes; 2xx passes with `text/html`, or with no type and markup; a 200 `application/json` is `not_page`; a 404 `text/html` with markup passes and without markup does not; `X-Frame-Options` DENY or SAMEORIGIN and `frame-ancestors 'none'`/`'self'` are `no_frame`; garbage, or headers past 4 KiB, are skipped | framing headers not read; markup not required |
| `arbitrate`: the earliest `shows` claim beats a newer `focused_at`; else the newest `focused_at`; ties go to the earliest; a claim after the decision loses | first claim wins regardless of `shows` |

### AO-1b-i: `tests/v2/devservers_check.py`

Real processes; a port-0 daemon with a stand-in Claude provider whose session runs a scripted turn and sets the tag in its child's environment; a temp HOME. Servers bind fixed free ports in 20000–29999, found by a bind test: never 8402, 8403 or 8405, never the ephemeral range.

| Check | Mutation it must catch |
|---|---|
| A tagged `python3 -m http.server <p>`, started with `setsid nohup` from the session's child during its turn: exactly one bus record `{kind: dev_server, data: {id, state: up, source, at}}`, with **no port** and no other keys. `GET /dev-servers` from the owner lists `http://localhost:<p>/`; the claim answer carries the URL | port added to the bus record |
| The same server started with its cwd in `/tmp` is still attributed | folder rule applied |
| The same server started under `env -i` (untagged): no record | untagged attributed |
| **No record**, and **the server's accept count is 0** (never probed), for: a server started after the turn plus grace; one in its own `systemd-run --user --scope` (another cgroup); an ephemeral port; a service port (a stand-in on a test port added to the service list); an untagged one; one from a fast-path turn | probe before attribution |
| Probed but no record: a server sending `X-Frame-Options: DENY`; a 200 `application/json` at `/`; a 404 with no markup; a server that accepts and never answers (probe timeout, ≤ 2 in flight); a server alive 1 s (no probe) | framing check removed; markup check removed |
| A fake-shell HUD terminal's server: the watcher records nothing for it | terminal exclusion removed |
| Jarvis's ports: stand-ins on test ports configured as `control_ports()`, the workshop and the gate keep an accept count of **0** while a tagged process listens on them during a turn | port checked after the probe |
| The probe sends one `GET /`, follows no redirect (a 302 to a stand-in leaves its accept count at 0) and reads at most 4 KiB from a server streaming forever | redirect followed |
| `down` after the listener closes (2 polls); a restart within 60 s reuses the id and publishes no second `up`; a dismissed entry stays dismissed across the restart | no de-dup by port |
| No URL, port, path, pid, cwd, cmdline or environment in any bus record. No log line names a port or path. The thread's `log.jsonl` is unchanged. The Discord mirror posts nothing | pid added to the record |
| A fake secret in the server's environment appears in no bus record, log line, `/status` or route answer | buffer logged |
| `auto_open` off: no `/proc` read (`/status` work counters stay 0), no probe | switch ignored |
| **Work counts, not wall time (R-7):** with 1,000 sleeping children and 500 loopback listeners in the test's cgroup, a steady poll does 2 file reads, 0 fd readlinks and 0 `environ` reads. A poll after one new listener does one fd scan (readlinks ≤ the cgroup's fd count) and at most one `environ` read. No poll runs while no Claude or Codex turn or grace is active and no entry lives | fd scan on every poll; polling during a fast-path turn |
| Routes: owner-only (the API listener 403; foreign, `null` and missing Origin on POST 403; a rebinding Host refused); exact bodies (unknown keys 400); frame headers; `frame_check` walks `dev-servers` | `owner_only` removed |
| Claims through the route, with two windows: a `shows` claim 300 ms after a focused claim still wins. `dismiss` publishes `dismissed`, and later claims get 409. An unrenewed lease lapses after 90 s and `up` is published again. Closing the winner's `/events?window=` stream lapses it at once. `?window=` on the API listener changes nothing | lease never lapses; SSE end ignored |
| The tag is in the run brief, not `brief.json`. It reaches the stand-in Claude client's and a stand-in Codex app-server's children (`setsid` and `nohup` included). A HUD terminal's environment never holds it | tag written to `brief.json` |
| The grep pin: only `daemon`, `hud_api` and `terminals` import `devservers` or `devlisten`; no tool module does | — |

### AO-1b-ii: additions to `tests/v2/terminal_check.py`

| Check | Mutation it must catch |
|---|---|
| With a window attached: the frame carries `id`; the bus gets one `dev_server` with `state: claimed` and no port or marker (#70's test amended on purpose: kinds `["terminal_attached", "dev_server"]`) | path or port on the bus |
| With no window: the entry is unclaimed, and the bus carries the ids alone. The claim answer returns the cleaned URL with its path; the API listener cannot read it | path on the bus |
| `auto_open` off: `_sniff` registers nothing | switch ignored |

### AO-3: `devopen.test.ts` and `tests/face/hud_v2_autoopen_check.py`

The headless suite runs on its own `MockDaemon(0)` with its `dev_port`, SSE `dev_server` records on cue, the mock's claim routes (using the real `arbitrate`) and `FakePty` for terminal sources. It runs after the terminal section of `hud_v2_check`.

| Check | Mutation it must catch |
|---|---|
| Single chat pane, owner typing: an agent `up` gives cols2 with a Direct Browser pane on `dev_port` beside the chat. The chat box keeps DOM focus and the typing continues; `ws.focused`, the selected chat and the mic are unchanged | the split takes focus |
| A pointer button held down (a drag in the chat log): no split until 150 ms after it goes up | no pointer deferral |
| A window sized so the next shape fits only with the sidebar folded: no split, and it goes on to rule 2 or 3 | room check ignores folds |
| **⊞ 8→1, then a server comes up** (R-3): it splits, taking the oldest blank kept pane. Every non-blank kept spec (a File pane with tabs, a chat with words, a terminal) is unchanged, and ⊞ 1→8 brings each back | a non-blank kept pane taken |
| 4 drawn and 4 kept, every kept pane non-blank, plus a drawn Browser pane: a new tab there; kept specs unchanged | `allocate` used |
| 8 drawn, the Browser pane focused: the tab is added behind it with a dot. Not focused and not used for 10 s: brought to the front | always brought to the front |
| No free or blank id, and no Browser pane: the prompt at the top of the centre, clear of every input bar. An unsaved-edit File pane and a VEX pane mid-chain are disabled with reasons; a busy terminal is offered with its note. Choosing 3 shows the Browser on `dev_port` in slot 3. "Don't open" POSTs `dismiss`, and a second window's prompt goes too | a dirty pane offered |
| A click on the prompt within 500 ms of it appearing does nothing; one after does | no arming |
| The prompt never takes focus. Digits typed in the chat box never pick a pane. `1`–`8` and Escape work only with focus inside | global keys |
| `down` removes the prompt | no expiry |
| Under a card: no split, tab or prompt (none in the DOM before paint). Placement runs after the card resolves. A `down` while waiting means nothing happens. The lease is renewed while waiting (a 3-minute card does not lose it) | opens under a card; no renewal |
| Two windows: the one showing the thread opens it, and the other does nothing. With neither showing, the most recently focused one does. Closing the winner before it acts makes the other act (lease lapse). With no window, a window loaded later shows the notice, and Open runs placement | — |
| De-dup: the same entry twice, a restart, and BR-8a plus the watcher on one port all give one pane or tab. A new entry on a port a tab still shows opens nothing | `opened` by origin only |
| Rate limit: three sources in a second open one; the next opens after 10 s | — |
| **L2:** the mock records **0** requests to `/browser/*` across every check. The frame's sandbox is `allow-scripts allow-forms` with no `allow-same-origin`. A `[::1]` or control-port URL is skipped, never sent to Remote | auto-open falls back to Remote; keep-origin carried |
| An auto-opened `/focus-mount` page: removed by AO-2's guard ("This page took the keyboard"); the owner's typing is intact | — |
| `auto_open` off: nothing happens | — |
| A terminal source: BR-8a's frame with an id opens beside that terminal's pane with no claim POST, then posts `placed` | — |
| A grep pin: `devopen.ts`, `state/devservers.ts` and `DevServerPrompt.tsx` import nothing from `state/browser` | — |

---

## I. Measurements (manual, never in a suite)

- **AO-M1, Codex.**
  - Under `codex sandbox linux` in `workspace-write`, with the network off, can a command `bind` and `listen` on 127.0.0.1? Is its listener in the root namespace's `/proc/net/tcp`?
  - With the network widened (an approved escalation), does the server appear, carrying the tag?
  - Use the real binary. The sandbox half needs no model call.
- **AO-M2, Claude.** In a real Claude chat on a fixture project:
  - do `run_in_background` and `nohup … &` servers inherit `JARVIS_ORIGIN_TAG`?
  - does a server started in the chat open beside it?

  This costs one turn.
- **AO-M3, cost on the owner's machine:** `/status` → `dev_watch.work` over a working day.
- **AO-M4, WSL forwarding:** a Vite server on Node ≥ 17 bound to `::1` only. Does the Direct frame load it from Windows Chrome?
- **AO-M5, the focus guard in the owner's Chrome 155 (Windows).** On a page that steals focus on a timer:
  - typing in the chat, the editor and a terminal keeps every key;
  - clicking into the page still works.

---

## J. Decisions taken here (where L1–L8 and R-1–R-9 are silent)

| # | Decision | Why |
|---|---|---|
| AD-1 | The daemon's own cgroup narrows candidates and is the only fd scan. The tag (R-2) attributes | It excludes the owner's own processes by construction, and halves the scan |
| AD-2 | Ephemeral ports are never opened automatically (agent sources only; BR-8a banners keep their ports) | Test fixtures bind port 0. Dev servers use fixed ports |
| AD-3 | "Answers HTTP on `/`" means a page that may be framed (§B.7) | A page Direct can only draw as an error is noise. It also excludes `MockDaemon` HUDs |
| AD-4 | `MIN_LIFE_S` 2 s; probe connect 1 s, response 20 s, budget 30 s, 4 KiB, 2 in flight | Short-lived fixtures; Next compiles on its first request; bounded work |
| AD-5 | `GRACE_S` 600 s (1 s polls for the first 120 s, then 5 s); `START_SLACK_S` 2 s | R-8's longer grace, at a low cost after the first two minutes |
| AD-6 | Rule 1 never folds a side pane or the panel | An unasked split must not fold what the owner sees |
| AD-8 | The claim collects for 600 ms, then a window showing the source beats the most recently focused | §K, C5 |
| AD-9 | 12 tabs per pane; 3 live frames per pane and 8 per window; `browserUrl` mirrored for one release | Memory in the owner's Chrome; a safe rollback |
| AD-10 | Polls run only during and after Claude and Codex turns, or while entries live | §B.10; R-8 |
| AD-12 | An unattached terminal's URL is held in memory, with the path only on owner-only routes | L5's "no window" case needs it; L7 allows the URL to the owner's window |
| AD-13 | The prompt: slots, the reasons table in §E.4, keys only with focus inside, never focused, placed and armed as R-6 says | L3, L6, R-6 |
| AD-14 | Under a card, waiting lasts until the card goes (10-minute backstop), replacing BR-2's 30 s | L6 |
| AD-15 | A URL the Direct judge refuses is skipped, never sent to Remote | L2 |
| AD-16 | The focus guard's gestures are pointer over, Tab within 300 ms, and the owner's own load until 2 s after `load`. The iframe is owned imperatively. Detection uses `blur`, `focusin` and a 250 ms belt | R-1; React must not own a node the guard removes |
| AD-17 | The lease is 90 s, renewed every 30 s, and lapses when the window's SSE ends (`/events?window=`, owner GET only) | R-7 |

AD-7 and AD-11 were taken at review (R-4; R-2 made the folder rule moot) and are no longer the planner's.

---

## K. Challenges to L1–L8, and how they were settled

- **C1 (L1, the fast path).** The fast path cannot start a process (§A item 3). **Settled (R-8):** the watcher polls only during Claude and Codex turns.
- **C2 (L1, Codex).** With the network off, a Codex command probably cannot bind at all, so the feature reaches Codex only for commands the owner let out of the sandbox. **Open:** AO-M1 measures it. If sandboxed binds succeed in the root namespace, nothing changes here.
- **C3 (L1, the folder rule).** A tag is stronger evidence than a folder. **Settled (R-2):** the tag is required and the folder rule is dropped. The cost, an `env -i` child missed, is stated in §F.
- **C4 (L3 rule 1, "a pane can be added").** `splitVerdict` alone would take a ⊞-hidden pane (parking its words) or fold the sidebar. **Settled:** a free id or a blank kept pane (R-3), and no folding (AD-6).
- **C5 (L5, "the first claim wins").** Literally, whichever window's SSE arrives first wins, and that could beat the window showing the thread. **Planned:** collect for 600 ms, then a window showing the source, else the most recently focused (AD-8). After the decision, first-come stands.
- **C6 (L5 with BR-8a).** "The next window that loads shows … servers still listening" needs the daemon to keep an unattached terminal's announcement. That reverses #70's "dropped, not recorded" for that one case (AD-12, AO-1b-ii).
- **C7 (L6, waiting).** BR-2's 30 s expiry under a card contradicts "waits until the card goes". **Planned:** follow L6 (AD-14).
- **C8 (L1, "answers HTTP on `/`").** Read literally, it would auto-open every agent's test HUD and every JSON API. **Settled:** R-4 and R-7 (pages only, markup, service ports), with AD-2 and AD-3.

---

## L. Questions for the owner

**None open.** The two this plan first asked were decided by the lead at review (R-4). The owner can overturn either:

| # | Question | Decided |
|---|---|---|
| Q-A1 | Should an agent's server that answers `/` with something that isn't a web page (an API's JSON "not found") open anyway? | **No.** Only pages and redirects open, and the owner can still type the address |
| Q-A2 | When the only place left is a Browser pane the owner isn't using, should the new tab come to the front? | **Yes.** The page that was there stays as a tab. A pane in use gets the tab behind a dot |

---

## M. A correction owed by the open PR #70 (L2)

#69 merged (a4c2a82) with the L2 corrections already in place:
- its CLAUDE.md BR-1a paragraph says "Auto-open never opens a port here";
- its decisions bullet says "Auto-open never opens one";
- amendment 1 §D.2 says "It never opens Remote, and never opens a port in the Browser's gate".

**PR #70 (BR-8a, `feat/browser-devurl`, 0d7b8da) still carries the old sentence:**
1. **The PR body, "For BR-8b", last bullet.**
   - Old: "Under #69's new rule, a loopback port is reachable in Remote mode only after an owner-route navigation names it. Auto-open goes through the window, so it counts as one."
   - New: "Under #69's rule a loopback port opens in Remote only through the owner's own navigation. Auto-open never counts as one (amendment 2, L2): it is Direct only."
2. **Worth a word, not required:** #70's CLAUDE.md, `docs/hud-api.md` and design §18 call the window half "BR-8b". It is now AO-3, and the frame gains an `id` (§D.2).
3. **No change needed:** "No window attached: the URL is dropped and not recorded" stays true until AO-1b-ii changes it (§C, AD-12). AO-1b-ii also amends #70's bus test on purpose (§C).

---

### Critical files for implementation

**New files**
- `jarvis/v2/devlisten.py` (AO-1a) and `jarvis/v2/devservers.py` (AO-1b-i).
- `hud/src/lib/frameguard.ts` and the `FrameHost` part of `BrowserTab.tsx` (AO-2).
- `hud/src/state/devservers.ts` and `hud/src/components/DevServerPrompt.tsx` (AO-3).
- `tests/v2/devlisten_check.py`, `tests/v2/devservers_check.py`, `tests/face/hud_v2_autoopen_check.py` and `tests/face/hud_v2_mock_devservers.py`.

**Backend**
- `jarvis/v2/daemon.py`: Claude and Codex turn start and end, `_run_brief`'s tag, stop order, `/status` → `dev_watch`, and `/events?window=`.
- `jarvis/v2/hud_api.py`: mount `/dev-servers`.
- `jarvis/v2/terminals.py`: the `_sniff` registration and the frame id (AO-1b-ii, on #70's version).
- `jarvis/v2/providers/claude.py` (`ClaudeAgentOptions.env`) and `jarvis/v2/providers/codex.py` (the app-server's `env`).

**HUD**
- `hud/src/lib/workspace.ts`: `PaneSpec.browser`, migration, the tab helpers, `copySpec`, `freeId`, the blank kept pane, `autoSplitVerdict`.
- `hud/src/lib/devopen.ts`: rewritten.
- `hud/src/components/BrowserTab.tsx`: the strip, the live-frame caps, the label, `FrameHost`.
- `hud/src/App.tsx`: the focus-guard hook, `splitFrom(source, opts, spec)`, `openBrowser` → new tab, SSE `dev_server`, pointer tracking, the prompt.
- `hud/src/components/Terminal.tsx`: `dev_url` with an id.

**Tests touched**
- `tests/v2/frame_check.py`: the `dev-servers` segment.
- `tests/face/hud_v2_mock.py`: the `dev_port` focus pages and the key log.
