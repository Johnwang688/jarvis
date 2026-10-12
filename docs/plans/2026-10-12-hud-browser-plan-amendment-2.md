# Amendment 2 to the HUD Browser plan: dev servers open by themselves, from any source (2026-10-12)

**Status:** draft, 2026-10-12, written by a read-only planner from the lead's brief. The lead's decisions L1–L8 are recorded in §0 and planned as written. §K lists the planner's challenges to them, and §J the decisions this plan takes where L1–L8 are silent.

**Precedence:**
- This amends `2026-10-10-hud-browser-plan.md` and `2026-10-11-hud-browser-plan-amendment-1.md`.
- **Where it differs from amendment 1's BR-8b (§D.2, BD-7 and the BR-8b rows of §G and §H), this wins.**
- The decisions file (`2026-10-10-hud-browser-decisions.md`) still wins over every plan. Its "Amendment 2" section records L1–L8.

**Builds on:**
- main at 3ee010b (BR-2's `lib/devopen.ts`, BR-3a's rename, WP-G2's `splitPane(…, {focus})`);
- PR #69 (BR-1a, `feat/browser-engine`, a98c267): the gate, `refused_ports`, and loopback closed until the owner opens a port;
- PR #70 (BR-8a, `feat/browser-devurl`, 0d7b8da): `devurl.py`, the `dev_url` frame and `browser.json` → `auto_open`;
- PR #72 (flaky suites): it touches `hud_v2_terminal_check.py` and `hud_v2_layout_check.py`, which AO-3's tests extend.

**The owner's request (2026-10-12, verbatim):**

> "Can you have Jarvis/claude/codex auto open localhost servers when they are started? If there is 8 panels all taken already then have them open a new tab in a browser panel. If no browser panels are open then have it ask which panel to open in or if I don't want to open it"

---

## For the owner: what changes

- **A server an agent starts now opens too, not only one your terminal announces.** When a Claude or Codex chat or task starts a local web server in its project, a Browser pane opens beside that chat. Your keyboard focus, the chat you're typing in and the mic never move.
- **When there's no room for another pane** (8 panes, or a window too small for one more), the server opens as a **new tab** in a Browser pane you already have.
- **When there's no room and no Browser pane,** a small box asks: "Dev server localhost:5173 from *chat name* — Open in pane: 1 … 8 · Don't open". It never takes your focus. It goes away by itself when the server stops.
- **Browser panes get tabs,** like the editor's.
- **It is always the plain, sandboxed local view** (Direct mode). An auto-opened page can't reach Jarvis, can't keep its own origin, and never opens a port in your Remote browser.
- **Nothing opens, asks or moves under an approval card.** It waits until the card is gone.
- **One switch turns all of it off:** Settings → Browser → "Open dev servers automatically" (on by default).

**The costs, plainly:**
- **An agent can now put a page in front of you.** It is sandboxed: a fake "Approve" button inside it can do nothing.
- **The daemon looks at this machine's listening ports about once a second while an agent's turn runs.** Measured: 1.5–7 ms per look, with 1,000 processes and up to 5,000 listening sockets. It does nothing while no turn is running.
- **Some servers are missed on purpose:** servers you start outside Jarvis, servers on ports Linux hands out at random (test fixtures), and pages that refuse to be framed (§F).

---

## 0. The lead's decisions, recorded

| # | Decision |
|---|---|
| **L1** | **Sources.** (a) The owner's HUD terminals: BR-8a's banner detection. (b) Servers started by an agent (a chat or a task, on the fast path, Claude or Codex), detected as **new loopback TCP listeners**. Such a listener must: be owned by a process of the owner's uid; have a working folder, or a parent's, inside that thread's folder (`Thread.cwd`, the task's worktree, a project root); start while that thread has a turn running or within a grace period after it; not be a HUD terminal's process (BR-8a covers those); not be the daemon or anything of Jarvis's own, and never on Jarvis's ports; and answer HTTP on `/`. Process-tree descent alone is not enough (`setsid` and `nohup` reparent). Servers the owner starts outside Jarvis are **not** included. |
| **L2** | **Direct mode only.** An auto-opened server loads in WP-E's sandboxed frame, judged by `judgeBrowserUrl` with `/status` → `refused_ports`, never with keep-origin. **Auto-open never opens a port in the Remote browser's gate**; only the owner's own navigation does (#69's rule). This reverses the earlier note that BR-8b's auto-open counts as an owner-route navigation, because an agent can now trigger auto-open. |
| **L3** | **Placement, in order**, in L5's window. (1) **A pane can be added** (fewer than 8, `splitVerdict` allows it): split a new Browser pane beside the pane showing that chat, task or terminal, else beside the focused pane, with `{focus:false}`. DOM focus, `ws.focused` and the selected chat (W-6) never move. (2) **No pane can be added** (8 drawn, or `splitVerdict` refuses for room): a **new tab in a drawn Browser pane**, the one nearest the source, else the most recently used. (3) **No pane can be added and no Browser pane is drawn:** a **non-blocking prompt**, "Dev server `localhost:5173` from <name> — Open in pane: [1] … [8] · Don't open". Choosing a pane switches it to the Browser view with the URL. A pane holding an unsaved edit (ED-2 `paneLoses`), a VEX view mid-chain or a terminal's running program is not offered, or is offered with its reason. "Don't open" dismisses it. The prompt expires when the server stops listening. |
| **L4** | **Browser panes get tabs in Direct mode.** A strip like the Editor's: a list of URLs per pane, each judged again on load, with a keep-origin grant per tab stored as its origin. A cap on tabs; middle-click or × closes. Stored in `PaneSpec`, migrated from `browserUrl`. Built so BR-3b's Remote mode reuses the strip. |
| **L5** | **One window acts.** The window whose pane shows that thread or terminal opens it. Otherwise one window claims it: the most recently focused, through an owner-only claim route where the first claim wins. With no window open, nothing opens; the next window that loads shows a quiet notice listing servers still listening, with the same choices. |
| **L6** | **The gentle rules.** Nothing opens, prompts or moves under an approval card: it waits until the card goes, and never steals focus or the mic. De-dup per port while it keeps listening: a restart on the same port doesn't reopen a pane or tab that still shows it. Rate limit. BR-8a's `auto_open` switch (`browser.json`) covers every source; on by default. |
| **L7** | **What leaves memory.** Only a loopback URL (no userinfo, query or fragment) and the source's thread or terminal id. Never terminal text or agent tool output. Say which events carry what, and where they go. |
| **L8** | **Stated limits.** An agent can put a page of its choosing in front of the owner, in a sandboxed frame that can't reach Jarvis. A server an agent starts and hands to the owner's own process is missed. A listener on 0.0.0.0 is loopback-reachable too, so it is included. |

---

## A. Facts this amendment relies on

1. **The pieces already on main.**
   - `splitPane(ws, source, id, spec, {focus: false})` exists (WP-G2). It keeps `ws.focused`.
   - `__hud.split(source, {focus})` is its test hook. `App.splitFrom` calls `splitVerdict`, `allocate(ws, dirty, holdingIds())` (which may take a ⊞-hidden pane and park its words) and `copySpec`.
   - `lib/devopen.ts` (BR-2) is pure and unwired: `autoOpenPlan` → `split | focused | offer | wait (30 s) | skip`, with per-terminal and per-window rate limits and an `opened` origin set per page.
2. **`splitVerdict` folds side panes for the render.** It refuses only when the next shape would drop a pane. To fit, it lets `fitWorkspace` fold a side pane or the panel (`sides.autoLeft`/`autoRight`, `panel.auto`). For a split nobody asked for, that is a visible move (§E.2).
3. **The fast path cannot start a process.** `FAST_TOOLS` holds no `run_command` or `run_readonly`, and `_validate_toolset` forbids them (§8.1). Work that would start a server becomes a task (`task_propose`) on Claude or Codex, which L1 covers as a task. See §K, C1.
4. **Codex runs commands in `workspace-write` with the network off.** `codex_config.config_text` and `CodexProvider` require `sandbox_mode = "workspace-write"`. A sandboxed command probably cannot bind a TCP port, or binds in a namespace the HUD cannot reach. A Codex server appears only after a network widening, which the owner approves on a card (`permit(…, widening=…)`). AO-M1 measures this; see §K, C2.
5. **Claude workers run unsandboxed**, as the owner's uid, in the daemon's cgroup. The CLI spawns its Bash tool's processes, and anything they start inherits the CLI's environment.
6. **Orphans stay in the daemon's cgroup.** `jarvis-daemon2` runs in `…/app.slice/jarvis-daemon2.service` (cgroup v2, read 2026-10-12). The systemd user manager is the subreaper there. A process that `nohup`s or `setsid`s itself and loses its parent is adopted by `systemd --user`, but it stays in the unit's cgroup, along with HUD terminals and the Claude and Codex processes. Processes the owner starts in a Windows Terminal or VS Code shell sit in other cgroups. An agent's test run started with `systemd-run --scope` (this repo's rule) also leaves it.
   - The unit has `Delegate=no`, so nothing under it can make a sub-cgroup.
   - It has `KillMode=control-group`, so a daemon restart ends every agent-started server, as it ends the HUD terminals today.
7. **`/proc` on this WSL2** is mounted `rw,nosuid,nodev,noexec,noatime`, with no `hidepid`, and `ptrace_scope` is 1. Measured from a non-descendant: a same-uid process's `cwd`, `fd/*` and `environ` are readable. A same-uid process holding capabilities, such as `systemd --user`, is not (EACCES). Yama's scope 1 restricts only attaching.
8. **The ephemeral port range** is 32768–60999 (`/proc/sys/net/ipv4/ip_local_port_range`). A server bound to port 0 gets a port there. Dev servers use fixed ports below it: 3000, 4200, 5173, 6006, 8000, 8080, 8888 and so on.
9. **The HUD's own test servers would match L1 as written.** In this repo an agent runs `MockDaemon` HUDs on ports 18000–19999 from its worktree, during its turn. They answer HTTP on `/` with a HUD page that refuses to be framed (`frame-ancestors 'none'`). §B.5 filters them three ways:
   - a run under `systemd-run --scope` leaves the daemon's cgroup (rule 3);
   - a run shorter than 2 s is too brief (rule 10);
   - the page's framing refusal (rule 11).
10. **BR-8a's frame reaches one window.** `{"type":"dev_url","url"}` goes on the terminal's attached socket only. With no window attached, the URL is dropped and not recorded.
11. **Codex's app-server is a session leader** (`RpcProcess`: `start_new_session=True`). The Claude CLI's pid is reachable only through SDK internals (`getattr`, not pinned).

---

## B. Detecting a server an agent started (daemon)

New module `jarvis/v2/devlisten.py` (pure: parsing, facts and rules, with an injectable `/proc` root) and the watcher in `jarvis/v2/devservers.py`.

### B.1 When it runs

- **Only while `browser.json` → `auto_open` is on** (L6). Off means no thread, no `/proc` read and no probe.
- **Only while there is something to watch:**
  - **1 s** cadence while any agent turn runs, or ended less than `GRACE_S` = 120 s ago;
  - **5 s** cadence, rows only, while the registry holds a listening entry (liveness for §D);
  - **off** otherwise. A turn start wakes it.
- **One thread, `jarvis-devwatch`.** It starts lazily at the first turn and is stopped by `Daemon.stop` after the terminals, before the bus closes.
- **The daemon tells it about turns.** `_begin` calls `turn_started(thread_id)` and `_turn`'s `finally` calls `turn_ended(thread_id)`, both recorded on `CLOCK_BOOTTIME`.
- **At start it takes a baseline.** Every listener already present is old, so a daemon restart never opens anything.

### B.2 Finding new listeners

1. Read `/proc/net/tcp` and `/proc/net/tcp6` whole, once per poll.
2. Keep rows in state `0A` (LISTEN). A cheap `b" 0A "` substring test runs before the split.
3. Keep a row only when its local address is **loopback or a wildcard**:
   - IPv4 `127.0.0.0/8` and `0.0.0.0`, hex little-endian per word;
   - IPv6 `::1`, `::`, and `::ffff:127.x.y.z`.

   A listener bound to a LAN or `eth0` address is not loopback-reachable and is skipped (L8: a wildcard **is** included).
4. **Refuse the port before anything else:**
   - `policy.refused_ports` from #69: `config.control_ports()`, the daemon's bound HUD, API and workshop ports, and 8403 unless it is this daemon's workshop;
   - the BR-1a gate's own port;
   - any port in the ephemeral range (§J AD-2).
5. **New** means an inode not in the previous poll's set. Rows are keyed `(inode, port)`.

`/proc/net/*` shows the daemon's network namespace: the root one, the HUD's. A listener in another namespace (a Codex `bwrap --unshare-net`, a container) is invisible here and unreachable from the HUD anyway.

### B.3 Who owns it (inode → pid)

- **Only when a poll finds a new candidate inode**, read `/sys/fs/cgroup/<the daemon's own cgroup>/cgroup.procs`. The daemon's cgroup is read from `/proc/self/cgroup` at start, so a test daemon in a `systemd-run` scope uses its own scope.
- Then `readlink` `/proc/<pid>/fd/*` for those pids only, looking for `socket:[<inode>]`. At most one fd scan per poll.
- **The cgroup is the first filter (§J AD-1).** A listener whose owner is not in the daemon's cgroup is never mapped. That excludes servers the owner starts outside Jarvis by construction (L1), and test fixtures run under `systemd-run --scope`.
  - The unit is not delegated (§A item 6), so `cgroup.procs` lists every process the daemon's work started.
  - A daemon started from a shell rather than its unit sits in that login session's scope, which also holds the owner's other processes. There the filter is weaker, and the folder, turn and tag rules carry it (§F).
- **Several pids holding one inode** (forked workers, an inherited socket): take the earliest-started pid that passes §B.5.
- **No pid found** (it died, or the socket moved out): the candidate is retried for 3 polls, then dropped.

### B.4 Facts about the process

For the owning pid and each ancestor up to `ANCESTRY_MAX` = 16, read:
- `/proc/<pid>/stat`: ppid, session id, start time in ticks since boot;
- the owner uid of `/proc/<pid>`;
- `readlink /proc/<pid>/cwd`. A target ending ` (deleted)` counts as no folder.

The ancestor walk stops at pid 1, the subreaper, the daemon or a provider root.

**The origin tag (§B.6),** from `/proc/<pid>/environ`:
- read without blocking, capped at 128 KiB;
- scanned for the single key `JARVIS_ORIGIN_TAG=`, whose value must match `^[0-9a-f]{32}$`;
- nothing else is kept, logged or compared, and the buffer is dropped at once.

Any read that fails (EACCES, a vanished pid, a pid whose start time changed) **fails closed**: no attribution.

### B.5 The rules (L1), in order

The rules live in a pure function, `devlisten.attribute(listener, facts, windows, now)`. It returns a thread or a refusal class. The classes are counted on `/status`, never logged per listener.

| # | Refused when | Class |
|---|---|---|
| 1 | The port is Jarvis's: §B.2 step 4's list, checked again here | `jarvis_port` |
| 2 | The port is in the kernel's ephemeral range (AD-2) | `ephemeral` |
| 3 | No owning pid in the daemon's cgroup (AD-1) | `outside` |
| 4 | The owner uid is not the daemon's | `uid` |
| 5 | The pid is **Jarvis's own**: the daemon, a provider root (a Claude CLI, a Codex app-server), a jarvis-mcp relay, the BrowserHost's Playwright driver or Chrome (`chrome_pids`) | `jarvis` |
| 6 | It is a **HUD terminal's**: its session id is a live terminal's session (leader start time matches, `terminals.session_members`' rule), or an ancestor is a terminal leader. A program terminal (VEX) counts too | `terminal` |
| 7 | **No thread's turn window holds it.** A window is open from `turn_start` to `turn_end + GRACE_S`. The listener's first sighting must fall inside it, and the process must have started at or after `turn_start − START_SLACK_S` (2 s) | `no_turn` |
| 8 | **No folder holds it.** The thread's folders are `Thread.cwd`, the task's worktree, the project root and its `extra_dirs`, all realpath'd. The cwd of the process or of one of its ancestors must be equal to one or below it. **The owner's home and `/` never count as a folder** (an Inbox chat's cwd is `~`; AD-11) | `no_folder` |
| 9 | **Attribution:** with an origin tag, the tag's thread, which must also pass 7 and 8. A tag naming no live or recent session is refused, never re-guessed. Without a tag: the one thread passing 7 and 8; with several, the one whose running turn started last | `stale_tag` / — |
| 10 | It closes before it has been seen on two polls at least 2 s apart (`MIN_LIFE_S`) | `brief` |
| 11 | The HTTP probe (§B.7) does not show a page that may be framed | `not_http`, `not_page`, `no_frame` |

### B.6 The origin tag: who started it, surviving `setsid` and `nohup`

L1 says descent alone fails. The environment does not: a variable survives `nohup`, `setsid`, a double fork and reparenting.

**How it is set.**
- `_run_brief` mints one 128-bit hex tag per session.
- It goes into **the run copy of the brief only** (`Brief.origin_tag`, never `brief.json`), the way `mcp_servers["jarvis"]` does.
- The Claude provider adds `JARVIS_ORIGIN_TAG=<tag>` to `ClaudeAgentOptions.env`.
- The Codex provider adds it to the app-server's `env`. `shell_environment_policy` has `inherit = "all"`, and the name avoids Codex's default `KEY`/`SECRET`/`TOKEN` excludes.
- The registry maps tag → thread while the session lives, and for `GRACE_S` after it closes.
- HUD terminals never carry it: their environment is built from an allowlist.

**What it is not.** It is not a secret and not a boundary.
- Any same-uid program can read another process's environment and copy a tag. That only changes which chat a page is labelled with and where it lands, for a page it could open anyway.
- `env -i` removes it, and then §B.5's untagged rule applies.
- The tag never relaxes rule 8 (§K, C3).

### B.7 The HTTP probe

- **Only after rules 1–10 pass.** At most 2 probes in flight, one per listener.
- **Where it connects:**
  - `127.0.0.1:<port>` for an IPv4 or wildcard bind;
  - `[::1]:<port>` for `::1` or `::`;
  - the IPv4 address for `::ffff:127.x.y.z`.

  It never connects to a non-loopback address. The port is checked against §B.2's refused list again **immediately before `connect`**.
- **What it sends:** `GET / HTTP/1.0`, plus `Host: localhost:<port>`, `User-Agent: Jarvis-devwatch`, `Accept: text/html` and `Connection: close`. No cookie, no credential, no body.
- **Bounds:**
  - connect within 1 s;
  - the status line and headers within 20 s (a Next.js dev server compiles `/` on its first request);
  - read at most 4 KiB, then close;
  - a refused or reset connection retries at 1, 2, 4 and 8 s, within `PROBE_BUDGET_S` = 30 s of the first sighting;
  - then the listener is dropped, and not retried until its inode changes.
- **Redirects are never followed.** A 3xx counts as answering.
- **What counts (AD-3):** a status line `HTTP/1.[01] <3 digits>`, and **a page**: a 2xx or 3xx, or any status with `Content-Type: text/html`. A JSON 404 at `/` is skipped.
- **A page that refuses to be framed is skipped:** `X-Frame-Options` `DENY` or `SAMEORIGIN`, or a CSP `frame-ancestors` without `*` or `http://localhost:*`. That also drops `MockDaemon` HUDs (§A item 9), because a Direct frame could only draw an error.
- **TLS:** if the plain probe's first byte is `0x15` (a TLS alert) or the server closes without a byte, one TLS probe runs with verification off (loopback only). If that answers, the scheme is `https`.
- **The response is never kept.** Only the status class and the three header facts survive, as booleans. Nothing is logged.

### B.8 Races

| Race | Handling |
|---|---|
| The server dies between the row and the fd scan | No pid; retried 3 polls, then dropped |
| A pid is reused | Facts are keyed `(pid, start_time)` and checked again before publishing |
| An inode is reused | Rows are keyed `(inode, port)`; a disappeared inode clears its state |
| The same port is rebound (a restart) | The registry is keyed by port. A listener returning within `DOWN_KEEP_S` = 60 s of `down` reuses the entry and its id: no second `up`. Later, it is a new entry, and the HUD's `opened` set and "a tab shows it" check stop a reopen (L6) |
| Forked workers share one inode | The earliest-started passing pid |
| Two sources claim one port (a terminal banner and the watcher) | One entry per port; the first source wins |
| A turn ends while the probe runs | The turn window is judged at first sighting, not at probe end |
| The daemon restarts | The baseline (§B.1) makes every existing listener old. The registry is memory only |
| WSL's localhost relay | `wslrelay` notices a new WSL listener after a moment. `MIN_LIFE_S` and the probe make an `up` arrive after the port is reachable from Windows |

### B.9 WSL specifics

- **No `hidepid`**, so all processes are visible. Other-uid processes are skipped: their `fd` folders are unreadable, and rule 4 refuses them.
- **Capability-holding same-uid processes** (`systemd --user`) refuse `cwd` and `fd` reads. They fail closed.
- **The HUD's Chrome runs on Windows.** A Direct frame reaches a WSL server through WSL's localhost forwarding:
  - binds on `127.0.0.1` and `0.0.0.0` are forwarded;
  - a `::1`-only bind (Node ≥ 17 on `localhost`) depends on the relay. AO-M4 checks it. It is the same as today's typed Direct URLs.
- **Windows-side servers** started through interop (`cmd.exe /c npm run dev`) listen on Windows' loopback. They are not in `/proc/net/tcp` and are missed (§F).
- **Clocks:** process start times are ticks since boot (`CLK_TCK` = 100 here). Turn times use `CLOCK_BOOTTIME`, so a WSL VM pause does not skew the comparison.

### B.10 Cost, measured (2026-10-12, this machine, under the 3G cap)

The planner ran a throwaway script under `systemd-run --user --scope -p MemoryMax=3G`. It started N sleeping children and M loopback listeners on ephemeral ports in its own scope, then timed each step of §B.2–§B.4 over real `/proc`. It printed numbers only: no names, command lines, environments or paths. AO-1b's cost check repeats it.

| Load | Steady poll (rows + diff) | Fd scan, uid-wide | Fd scan, own cgroup | Facts per pid |
|---|---|---|---|---|
| 1,151 processes, 557 LISTEN rows, 5,375 fds | **1.5 ms** | 15.7 ms | **8.1 ms** (1,052 pids) | 0.02 ms |
| 1,177 processes, 5,107 LISTEN rows, 9,472 fds | **7.0 ms** | 25.2 ms | **15.5 ms** (1,102 pids) | 0.01 ms |

`cgroup.procs` itself reads in 0.1 ms. The machine normally runs about 70 processes and 7 LISTEN rows.
- **Steady state** while a turn runs: one rows read per second. At worst 0.7 % of one core, and a GIL hold of at most 7 ms.
- **A new candidate** adds one fd scan, 8–16 ms.
- If an owner's machine ever holds tens of thousands of TCP rows, the rows read can move to netlink `sock_diag` (LISTEN only, filtered in the kernel) without changing anything else. This is not needed now.

---

## C. Servers the owner's HUD terminals announce (BR-8a), in the same registry

BR-8a's sniffer, rules and frame are unchanged, with two additions in `Terminal._sniff`:

1. **Every URL the sniffer would send is registered** with the registry: `announce_terminal(terminal_id, url) → id | None`. This is one entry per port, and the registry's rate limit applies.
2. **With a window attached,** the frame gains the entry id, `{"type":"dev_url","url":…,"id":…}`, and the entry is **claimed for that window at once** (L5: the window showing the terminal opens it). The bus gets `state: "claimed"`, so no other window acts.
3. **With no window attached,** the entry stays **unclaimed** and goes through §D.4's claim (§K, C6).
   - This **reverses** BR-8a's "dropped and not recorded" for this one case.
   - The URL, with BR-8a's cleaned path, is held **in daemon memory** and returned **only** by the owner-only routes.
   - The bus carries the port alone.
4. **Liveness:** a terminal entry is `down` once its port has had no LISTEN row for 2 polls, counted from 5 s after the banner (a banner may print before the bind).

Terminal sources are never probed: the banner is the announcement, as BR-8a decided.

---

## D. The registry, events and routes (the contract)

`jarvis/v2/devservers.py` `DevServers`, attached as `daemon.devservers`, in memory only. It holds at most `ENTRIES_MAX` = 32 entries.

**An entry:**
- `id`: `ds_` plus 8 random hex characters;
- `port`, `scheme`, and `path`: `/` for agents, BR-8a's cleaned path for terminals;
- `source`: `{kind: "thread", thread_id, task_id|null}` or `{kind: "terminal", terminal_id}`;
- `state`: `up` → `claimed` → `placed` | `dismissed`, or `down` at any time;
- `first_seen`, and the claim (`window`, `lease_until`).

**Rate limits:**
- one new entry per 10 s per source;
- six per minute across all sources.

Past either, an entry is not created, and only a counter moves.

### D.1 The bus record

```json
{"kind": "dev_server",
 "data": {"id": "ds_3f9a1c2e", "state": "up",
          "port": 5173, "scheme": "http",
          "source": {"kind": "thread", "thread_id": "…", "task_id": null},
          "at": "2026-10-12T15:42:03Z"}}
```

- **States published:** `up` (once per entry), `claimed`, `placed`, `dismissed` and `down`.
- **Ids ride inside `data`,** as `activity` records do. No top-level `thread_id` means no `?thread=` stream (the task runner's) carries it. In the HUD, an SSE event with no thread is W-6's "ambiguous input" and would reach the selected chat. So AO-3 takes `dev_server` before `routeEvent`, and it never reaches a pane's reducer.
- **Never in it:** a path, a host spelling, a pid, a cwd, a command line, an environment, a process name, or anything from the probe's response.
- **Where it goes:** the bus fan-out, so SSE `/events` on both listeners. It is never written to a thread's `log.jsonl`, the Discord mirror or the gateway. `Activity` ignores the kind.

### D.2 The terminal frame

BR-8a's `{"type":"dev_url","url"}` gains `"id"`, the registry entry. The HUD's `parseControl` accepts it with or without the id (rollout order).

### D.3 Routes

All are on `hud_api.route`, and all are **`projects.owner_only` plus a strict loopback Host, checked before the body is read**. On the API listener they answer 404/403, like `/browser/*`.

| Route | Body (exactly) | Answer |
|---|---|---|
| `GET /dev-servers` | — | `{"servers": [{id, url, port, state, source, claimed, mine}]}`. `url` is `http://localhost:<port><path>`. Only entries still listening |
| `POST /dev-servers/<id>/claim` | `{"window": "<32 hex>", "shows": bool, "focused_at": int}` | `{"won": true, "url": …}` or `{"won": false}`; 404 unknown; 409 `{"error", "code": "gone"}` if `down` or `dismissed` |
| `POST /dev-servers/<id>/placed` | `{"window": …}` | 200; publishes `placed`. 409 if not this window's claim |
| `POST /dev-servers/<id>/dismiss` | `{"window": …}` | 200; publishes `dismissed`. Any window may dismiss: the owner pressed "Don't open" |

- **No route opens, starts, probes or navigates anything.** None of them reaches `/browser/*` or `daemon.browser`.
- **Frame headers** come from `end_headers` (WP-E). `tests/v2/frame_check.py` must learn the `dev-servers` segment, or it fails.
- **CSP is unchanged.** `frame-src` already names `http://localhost:*`, which also matches `https:` (CSP3).
- **`/status` gains `dev_watch`:** `{active, entries, polls, last_poll_ms, refused: {class: count}}`. Counts only, no ports.

### D.4 The claim (L5)

`window` is the page's `pageNonce()` (`lib/terminal.ts`), held in memory only.

- **The terminal case** is pre-claimed (§C).
- **Otherwise, every window that hears `up` claims at once:**
  - `shows` is true when a drawn pane shows the source: a chat pane on that thread, or a task pane while the window's current task is that task;
  - `focused_at` is the epoch ms of the window's last `focus` event, or 0;
  - **a window under a card claims too.** Claiming is not acting: it acts once the card goes (§E.6).
- **The daemon collects claims for `ARBITRATE_MS` = 600 ms from the first one, then awards one** (pure `devservers.arbitrate`):
  1. the earliest claim with `shows`;
  2. else the claim with the newest `focused_at`;
  3. ties go to the earliest claim.

  Every claim is answered after the decision, a long poll of at most about 1 s. A claim arriving after the decision is `won:false` while the lease holds.
- **A lease:** a winner that neither posts `placed` nor `dismiss` within `CLAIM_LEASE_S` = 900 s (a closed window, a long card) loses its claim. The entry goes back to `up`, unclaimed, for the next window's notice (§E.5).
- **No window connected:** nobody claims, and the entry waits for a window to load.

### D.5 What leaves memory (L7)

| What | Carries | Goes to | Never carries |
|---|---|---|---|
| Bus `dev_server` | id, state, port, scheme, source kind and id, time | SSE `/events` on both listeners | path, pid, cwd, cmdline, env, process name, probe response |
| Terminal frame `dev_url` | BR-8a's cleaned URL (with path), id | That terminal's attached socket only | — |
| `GET /dev-servers`, the claim answer | URL (`/` for agents; the cleaned path for terminals), port, state, source | The owner's HUD window only (owner-only) | — |
| The probe | `GET /` to the server itself | Loopback, the server's own port | Cookies, credentials, any Jarvis port |
| Log lines | Counts and refusal classes ("dev watch: 1 server up (thread)") | The journal | Port, path, thread title, process facts |
| `/status` → `dev_watch` | Counts | Any local client | Ports |
| The HUD's stored workspace | A Browser pane's tab URLs, as `browserUrl` already is | The owner's browser (localStorage) | — |

**Can an agent read the list?**
- An agent on the API listener can read `/events`, so it sees ports and source ids. The port is not a secret: `ss -ltn` shows the same.
- It cannot read `GET /dev-servers` or the claim answer, so it never sees a terminal's path (BR-8a's review: a path can carry terminal text).
- Agent-detected entries carry only `/`.
- **No tool, MCP tool, fast-path tool or Discord verb reaches `devservers` or `devlisten`.** A grep pin allows only the daemon, `hud_api` and `terminals` to import them, as `terminal_check` does.

---

## E. The HUD

### E.1 What a window does with a server

1. **It hears of it:** SSE `dev_server up` (any source), or a terminal frame `dev_url` with an id (already claimed for this window).
2. **It claims it (§D.4)**, unless it is a terminal frame. On `won:false` it does nothing more.
3. **It plans** with `devopen.autoOpenPlan` (rewritten, §E.2). That plan judges the URL, de-dups, rate-limits and places.
4. **Under a card it waits** (§E.6). Once the card goes, it checks again that the entry is still `claimed` by this window and not `down`.
5. **It acts:** a split, a tab or the prompt. It then posts `placed`, or `dismiss` on "Don't open".

State lives outside React in `state/devservers.ts`, like `state/vex.ts`. The pure rules stay in `lib/devopen.ts`.

### E.2 Placement (L3), exactly

`autoOpenPlan(state, window, now)` answers one of:
- `split{source, url}`
- `tab{pane, url, activate}`
- `prompt{entryId, url}`
- `wait`
- `skip{why}`

**The URL first.** It passes `judgeBrowserUrl(url, ports)` and devopen's own refused-port set, which takes `/status` → `refused_ports` once #69 lands and the defaults until then. The query and fragment are dropped. A URL the Direct judge refuses (`[::1]`, `127.0.0.2`, a control port) is `skip{refused}`. **It is never sent to Remote instead** (L2).

**Rule 1: a new pane.**
- **The verdict:** `autoSplitVerdict(layout, ws, avail, availH, prefer, preferPanel)`, new in `lib/workspace.ts`. It is `splitVerdict`'s count and room check, plus two things (AD-6):
  - **only a free id**: `freeId(ws)`, the lowest id neither drawn nor `kept`. It never uses `allocate`'s second and third tiers, so a ⊞-hidden pane, holding words or not, is never taken or discarded;
  - **the trial fit must not fold anything the current render shows.** The trial's `sides.autoLeft`/`autoRight` and `panel.auto` must not be newly true, and it must drop no pane.
- **The source pane:**
  - a chat source: the drawn chat pane whose conversation is that thread;
  - a task source: a drawn task pane while the window's current task is that task;
  - a terminal source: the drawn pane whose `terminalId` it is;
  - else the focused pane, if drawn.
- **The new pane:**
  - App's `splitFrom` gains a spec argument, so the new pane takes a fresh Browser spec rather than a copy: `{view: "browser", projectId: <the source's project or null>, terminalId: null, browser: {tabs: [{id, url, auto: kind}], active: id}}`;
  - with `{focus: false}`: `ws.focused` unchanged, `focusPaneElement` not called, `select` not dispatched, so the selected chat and the mic stay put (W-6);
  - nothing is parked and no hidden pane is released, because only a free id is used.

**Rule 2: a tab.** When rule 1 fails (8 drawn, no free id, or no room without a fold):
- **Which pane:** the drawn Browser pane nearest the source's slot (smallest slot distance; on a tie, the one after it). With no source pane drawn, the most recently used Browser pane (App memory: the last owner pointer or key in that pane, or the last tab change the owner made). A pane at the tab cap is skipped.
- **Whether it comes to the front:** `activate` is true unless that pane is in use (AD-7). In use means any of:
  - it is `ws.focused`;
  - DOM focus is inside it, the frame included;
  - the owner used it in the last 10 s;
  - BR-9c later: an agent is driving it.
- **A tab added behind** gets a dot (accent colour, never amber) and a one-line note in the strip ("new tab: localhost:5173"). Nothing moves.

**Rule 3: the prompt** (§E.4).

**De-dup and the rate limit** (BR-2's, re-keyed):
- skip when any Browser tab in any pane, drawn or hidden, shows that origin, or when this page has opened or offered it before (`opened`);
- one per 10 s per **source key** (`thread:<id>`, `task:<id>`, `terminal:<id>`) and three a minute per window. Past either: `skip{rate}`.

### E.3 Tabs in Direct mode (L4)

**The model** (`lib/workspace.ts`):

```ts
interface BrowserTabSpec {
  id: string;            // 6 random base-36 characters, unique in the pane
  url: string;           // judged again on every load
  keepOrigin?: string;   // WP-E's grant, per tab, stored as the origin it was given for
  auto?: "thread" | "task" | "terminal";   // opened by auto-open: a mark on the tab, nothing else
  // BR-3b adds: mode?: "remote", remote?: {tab_id}. A Private tab stores no url (amendment 1 §C).
}
interface BrowserTabs { tabs: BrowserTabSpec[]; active: string }
// PaneSpec.browser?: BrowserTabs   (replaces browserUrl/keepOrigin)
```

- **Cap:** `BROWSER_TABS_MAX` = 12 per pane, B-13's number. At the cap, `+` refuses with a sentence, and auto-open treats the pane as full. BR-1b's global cap of 12 Remote tabs still applies to Remote tabs.
- **Live frames:** the `LIVE_TABS` = 3 most recently active tabs keep their iframes mounted. The two behind the active one are `display: none`, `inert` and `aria-hidden`. Older tabs unmount and reload their URL when picked again (AD-9).
- **Grants:** a tab's keep-origin applies only while `keptOrigin(tab.keepOrigin, tab.url, ports)` passes, and is dropped from storage when it doesn't (WP-E's rule, per tab). Switching tabs never carries a grant. **An auto-opened tab never gets one** (L2). Only the owner's tick on that tab adds one.
- **Migration** (`parsePane`):
  - a valid `browser` is parsed per tab: bad tabs dropped, unknown fields dropped, at most 12, `active` falls back to the first;
  - with none, `browserUrl ?? previewUrl`, with `keepOrigin` while it still matches, becomes one tab;
  - saves write `browser`, **and mirror the active tab's `browserUrl` and `keepOrigin` for one release**, so a rolled-back HUD still shows the active page (BR-3a lost the URL on rollback; this does not).
- **Helpers:**
  - `addBrowserTab(ws, pane, url, {activate, auto})`, `closeBrowserTab`, `activateBrowserTab`, `setTabKeepOrigin`;
  - `setBrowserUrl` now sets the **active** tab's URL;
  - `copySpec` copies every tab's URL with new ids and the active index: never a grant, never `auto` (WP-G2's rule, per tab).
- **The strip** (`BrowserTab.tsx`):
  - a `role="tablist"` row above the toolbar, styled like the Editor's tabs;
  - each tab is a button labelled with `host:port` plus the path. The label is cleaned (`remote.cleanTitle`), capped at 40, React text only, never a page title (Direct cannot read one);
  - an auto-opened tab has a small mark, with the tooltip "Opened for <source name>";
  - × (`browser-tab-close`) and middle-click close a tab. `+` (`browser-tab-new`) opens a blank tab with the URL box focused;
  - closing the last tab leaves "Nothing loaded";
  - arrows move between tabs. There are no window keys (Ctrl+Tab and Ctrl+W are Chrome's);
  - under a card, everything is disabled and inert (the workspace is inert).
- **The terminal's "Open in Browser"** (`openBrowser`, the owner's click) now opens a **new tab, brought to the front**, in the target pane, instead of replacing its page. A tab already on that URL is brought to the front instead.
- **For BR-3b:** the strip takes a tab list and an active id, and renders any tab. Remote tabs add their mode and daemon tab id. The profile stays per pane (amendment 1 §C), and so do "Jarvis can see" and "drive" (§E). A pane may hold Direct and Remote tabs together.

### E.4 The prompt (L3, rule 3)

`components/DevServerPrompt.tsx` is drawn **inside the workspace** at the centre's bottom-right, above the panel. Its z-index is below `#authveil`, so it is inert behind a card like everything else. At most 3 are shown; more collapse into "+N more".

**What it shows:**
- One line: "Dev server `localhost:5173` from **<name>**". The name is the chat's `threadLabel`, the task's title, or the terminal's title. It is text, capped at 60.
- Then "Open in pane:" and one button per drawn slot, labelled with the slot number and the pane's view label ("1 Chat", "3 Editor"; slots, never ids, G-1).
- Then "Don't open".

**Which panes are offered** (judged when drawn, and again on click):

| Pane | Offered? |
|---|---|
| A File pane holding the last view of an unsaved edit (`buffers.paneLoses`) | **Disabled**, "has an unsaved edit" |
| A VEX view while this window's chain runs (`vex` store) | **Disabled**, "a VEX download is running" |
| A terminal pane whose terminal is `busy` | Offered, with "its program keeps running in the panel" (the terminal goes back to the panel, WP-G2's `releaseToPanel`, never closed) |
| The source terminal's own pane | Offered, with "this is where it started" |
| A chat pane with words in its box or a turn running | Offered, with "the chat keeps its words and its turn". A pane switched away keeps its conversation (WP-B) |
| Anything else | Offered |

Choosing pane N switches it to the Browser view with one tab on the URL. This is the owner's click, so it may change the selected chat as any view switch does. The window then posts `placed`.

**Focus and keys.**
- It **never takes focus**. Every button has `noPointerFocus`, so a click leaves focus where it was.
- Keys work **only while focus is inside the prompt** (Tab reaches it): `1`–`8` pick that slot, and Escape is "Don't open". They stop there.
- Digits typed in a chat box or the editor never reach it.

**Under a card.** It is not rendered (removed in a layout effect before paint) and comes back when the card goes. Its key handler also refuses while `blocked`.

**It goes away on:**
- `down`, `dismissed`, or `placed` by another window;
- "Don't open" here, which posts `dismiss` so other windows drop it too.

Nothing else expires it (L3).

### E.5 The notice when a window loads (L5)

After `/status` answers, the window reads `GET /dev-servers`. It keeps entries that are `up` and unclaimed, including expired claims.

They are listed in the same component as a quiet notice: no sound, no focus, a muted style. Each says "Dev server localhost:5173 from <name> is listening · Open · Don't open".
- **Open** claims, then runs §E.2's placement as if the server had just started. If that ends at rule 3, Open expands into the pane buttons.
- A lost claim says "opened in another window".

### E.6 The gentle rules (L6)

- **Under a card:**
  - no split, tab, prompt or view switch;
  - the plan answers `wait`, and the entry waits while it is listening, **until the card goes**, with a 10-minute backstop (AD-14; this replaces BR-2's 30 s expiry);
  - several waiting entries keep their order, at most 4; the oldest is dropped past that;
  - after the card, the plan runs again on the window as it is then.
- **Never moved:** DOM focus, `ws.focused`, the selected chat and the mic.
- **De-dup per port** and **the rate limit:** §B.8 and §E.2.
- **One switch:** `browser.json` → `auto_open`. Off stops the daemon's watcher and BR-8a's sniffer. The HUD also skips (`skip{off}`) in case a record was already in flight.

### E.7 Direct only (L2), as code

- `devopen.ts`, `state/devservers.ts` and `DevServerPrompt.tsx` **never import** `state/browser` (BR-3b's Remote manager), and never call `/browser/tabs` or `/navigate`. A vitest grep pins it.
- The new tab and the new pane carry **no `mode`**, so they are Direct, and **no `keepOrigin`**.
- A URL the Direct judge refuses is skipped (§E.2).
- The mock records every `/browser/*` request; AO-3's suite asserts **0** across every auto-open.

---

## F. Stated limits

- **(L8) An agent can put a page of its choosing in front of the owner.** It is a Direct frame, sandboxed `allow-scripts allow-forms`, with no keep-origin. The HUD and API refuse to be framed (`frame-ancestors 'none'`), and the judge refuses Jarvis's ports, so it cannot reach Jarvis. A fake "Approve" button in it does nothing. It can still show anything, including text telling the owner to do something, and a sandboxed page can still run script and post forms to its own server.
- **(L8) A server an agent starts and hands to the owner's own process** (a launcher outside the cgroup, a Windows program) is missed.
- **(L8) Wildcard binds (`0.0.0.0`, `::`) are included.** They are also reachable from the LAN, which this feature neither causes nor fixes.
- **Missed on purpose:**
  - servers the owner starts outside Jarvis (L1), and anything outside the daemon's cgroup, including an agent's `systemd-run --user` server;
  - ephemeral ports (AD-2): a dev server an agent starts with `--port 0` is missed;
  - JSON or other non-page answers at `/` (AD-3; Q-A1 asks the owner);
  - pages that refuse framing;
  - servers alive under 2 s.
- **Missed by nature:**
  - Windows-side servers (interop);
  - servers in another network namespace (containers; a Codex `bwrap --unshare-net`);
  - a server whose process and ancestors all have their cwd outside the thread's folders (`cd /tmp && python -m http.server 5000`) (§K, C3);
  - a server that only starts listening after the grace period.
- **Can be mislabelled:**
  - An owner's HUD-terminal server started with `setsid` loses its session, has no tag and sits in the daemon's cgroup. Its banner opens it once through BR-8a anyway (one entry per port), but if the watcher sees it first while a turn runs in that folder, it is labelled with that chat.
  - Two untagged threads in one project resolve to the latest running turn.
- **The tag** can be copied or stripped by a same-uid program (§B.6). It is attribution, not a boundary.
- **The daemon reads other processes' environments** (`/proc/<pid>/environ`, same uid, capped). Those can hold secrets. Only one key is scanned, nothing is kept, and the read is never logged. It is a read the daemon could always make, now made on purpose.
- **Not a boundary against a same-uid program**, like every HUD rule. Such a program can open a listener in the daemon's cgroup, copy a tag, or call nothing and just start a browser itself.

---

## G. Packages

Common to every package: free tests, a temp HOME and config, the daemon on port 0, never 8402, 8403 or 8405, every test under `systemd-run … MemoryMax=3G`, docs in the same PR (CLAUDE.md, `docs/hud-api.md`, design §18), and each check shown to fail against its named mutation in a scratch copy.

**In flight:** #69 BR-1a, #70 BR-8a and #72 (flakes) are open. BR-1b and BR-1c come next. BR-3b is Remote mode.

| Package | What | Size, risk | Starts | Touches shared files |
|---|---|---|---|---|
| **AO-0** | This plan; the decisions file's "Amendment 2"; amendment 1 marked superseded where it differs; CLAUDE.md's L2 decision | S | this PR | docs only |
| **AO-1a** | `jarvis/v2/devlisten.py`, pure: the rows parser, address classes, refused and ephemeral ports, the facts readers over an injectable `/proc` root, `attribute()`, the probe-response parser and `arbitrate()` | M, medium | **now** (new files) | none |
| **AO-2** | Browser tabs in Direct mode (§E.3): `PaneSpec.browser`, migration, helpers, `copySpec`, the strip, live frames, `openBrowser` → new tab | M, medium | **now** (BR-3a is merged) | `workspace.ts`, `BrowserTab.tsx`, `App.tsx` (`openBrowser` only), `theme.css`, `hud_v2_browser_check.py` |
| **AO-1b** | `jarvis/v2/devservers.py`: the watcher thread, registry, probe, routes and `/status` → `dev_watch`. Daemon wiring (turn start and end, `Brief.origin_tag` in the run copy, stop order). The tag in `providers/claude.py` and `providers/codex.py`. `terminals._sniff` registration and the frame's id. `frame_check` learns `dev-servers` | L, high | **after #70 merges** (it extends `_sniff` and reads `auto_open`) and after AO-1a. Parallel with BR-1b and BR-1c: small hunks in `daemon.py` and `hud_api.py` | `daemon.py`, `hud_api.py`, `terminals.py`, `claude.py`, `codex.py` |
| **AO-3** | Auto-open in the HUD. **Replaces amendment 1's BR-8b.** `devopen.ts` rewritten (sources, rules 1–3, `wait` until the card goes); `autoSplitVerdict`/`freeId`; `splitFrom(source, opts, spec)`; `state/devservers.ts` (claim, placed, dismiss, the load notice); `DevServerPrompt.tsx`; the SSE `dev_server` handling and Terminal's `dev_url` id; the mock routes (`hud_v2_mock_devservers.py`, calling AO-1a's `arbitrate`) | L, medium-high | after AO-1b and AO-2; merges main after #72 | `App.tsx`, `Terminal.tsx`, `devopen.ts`, `workspace.ts`, `hud_v2_mock.py` |
| BR-3b | Remote mode, **reusing AO-2's strip** | L, high | after BR-1b, BR-1c, BR-2, BR-3a **and AO-2** | as amendment 1 |

**Order:**
1. AO-1a and AO-2 start now, beside BR-1b and BR-1c.
2. AO-1b follows #70's merge.
3. AO-3 follows AO-1b and AO-2.

The owner sees terminal servers open (BR-8a's half) as soon as AO-3 lands. BR-8a without AO-3 still opens nothing: `parseControl` drops the frame.

---

## H. Tests

### AO-1a: `tests/v2/devlisten_check.py` (pure, fixture text, temp proc root)

| Check | Mutation it must catch |
|---|---|
| Only state `0A` rows count. IPv4 and IPv6 hex in kernel byte order; `::ffff:127.0.0.1`, `0.0.0.0`, `::` and `127.0.0.5` are loopback or wildcard; a 192.168/172.x bind is not | IPv6 words read big-endian; ESTABLISHED rows counted |
| `refused_ports`, the workshop, the daemon's bound ports and the gate's port are refused in every family, before any other rule | refused set not applied, or applied after attribution |
| The ephemeral range is read from the file (fallback 32768–60999), and its ends are inclusive | range ignored |
| Attribution: a tag goes to its thread. Untagged: the one thread whose folder holds the process's or an ancestor's cwd; with two, the latest running turn | tagged listener skips the folder rule |
| The turn window: a first sighting inside `[start, end + 120 s]` and a process start at or after `start − 2 s`. A server started before the turn and rebound inside it is refused; one first seen at end + 121 s is refused | grace ignored; process start time ignored |
| Home and `/` are never a fallback folder (an Inbox chat's `~`) | home counted |
| A HUD terminal's session (leader start matches) and a terminal-leader ancestor are refused. A reused session number with another start time is not a terminal | leader start not checked |
| The daemon, provider roots, relays and Chrome pids are refused | — |
| Facts fail closed on EACCES, a vanished pid, a changed start time and a ` (deleted)` cwd | EACCES read as a match |
| `environ`: only `JARVIS_ORIGIN_TAG` is returned, the value matches 32 hex, the read stops at 128 KiB, and no other byte is kept | whole environ returned; no cap |
| The probe parser: `HTTP/1.x NNN`; 2xx and 3xx pass; `text/html` passes at any status; a JSON 404 is skipped; `X-Frame-Options` DENY or SAMEORIGIN and `frame-ancestors 'none'`/`'self'` are skipped; headers past 4 KiB or garbage are skipped | framing headers not read |
| `arbitrate`: the earliest `shows` claim wins over a newer `focused_at`; else the newest `focused_at`; ties go to the earliest; a claim after the decision loses | first claim wins regardless of `shows` |

### AO-1b: `tests/v2/devservers_check.py` (real processes, a port-0 daemon, a stand-in provider whose session runs a scripted turn, temp HOME)

Servers bind fixed free ports in 20000–29999, found by a bind test. Never 8402, 8403 or 8405, never the ephemeral range.

| Check | Mutation it must catch |
|---|---|
| A tagged `python3 -m http.server <p>` started with `setsid nohup` in the thread's folder during its turn gives exactly one bus record, `{kind: dev_server, data: {id, state: up, port, scheme, source, at}}`, and no other keys. `GET /dev-servers` from the owner lists `http://localhost:<p>/` | — |
| Untagged, the same process is attributed by its folder | fallback removed |
| No record for: a server started after the turn plus grace; one with its cwd outside the folders; one moved to its own `systemd-run --user --scope` (another cgroup); an ephemeral port; a server sending `X-Frame-Options: DENY`; a JSON 404 at `/`; a server that accepts and never answers (probe timeout, ≤ 2 in flight); a server alive for 1 s | cgroup filter removed; framing check removed; minimum life removed |
| A fake-shell HUD terminal's server: the watcher records nothing for it, and its banner registers once through `_sniff` | terminal exclusion removed |
| Jarvis's ports: stand-ins on test ports configured as `control_ports()`, the workshop and the gate keep an accept count of **0** while a tagged process listens on them during a turn | port checked after the probe |
| The probe sends one `GET /`, follows no redirect (a 302 to a stand-in leaves its accept count at 0) and reads at most 4 KiB from a server streaming forever | redirect followed |
| `down` after the listener closes (2 polls); a restart within 60 s reuses the id and publishes no second `up` | no de-dup by port |
| No path, pid, cwd, cmdline or environment in any bus record. No log line names a port or path. The thread's `log.jsonl` is unchanged. The Discord mirror posts nothing | pid added to the record |
| `auto_open` off: no `/proc` read (a counter), no probe, nothing registered by `_sniff` | switch ignored |
| The watcher polls only while a turn runs, is in grace or an entry is live; `Daemon.stop` leaves no thread | always polling |
| Routes: owner-only (the API listener 403; foreign, `null` and missing Origin on POST 403; a rebinding Host refused); exact bodies (unknown keys 400); frame headers; `frame_check` walks `dev-servers` | `owner_only` removed |
| The claim through the route: two windows, a `shows` claim 300 ms after a focused claim still wins; `dismiss` publishes `dismissed` and later claims get 409; a lease that expires returns the entry to unclaimed | lease never expires |
| A terminal source with no window attached: the bus carries the port only; the claim answer carries the path. With a window attached, the frame carries the id and the entry is `claimed` | path on the bus |
| The tag is in the run brief, not `brief.json`. It reaches a Claude stand-in's and a Codex stand-in's child process (`setsid` and `nohup` included). A HUD terminal's environment never holds it | tag written to `brief.json` |
| The grep pin: only `daemon`, `hud_api` and `terminals` import `devservers` or `devlisten`; no tool module does | — |
| Cost: with 1,000 sleeping children and 500 loopback listeners in the test's cgroup, a steady poll stays under 20 ms and a new-listener poll under 60 ms (generous; §B.10 measured 1.5–7 and 8–16 ms) | fd scan on every poll |

### AO-2: vitest (`workspace.test.ts`, `browser.test.ts`) and `tests/face/hud_v2_browser_check.py`

| Check | Mutation it must catch |
|---|---|
| Migration: `browserUrl` + `keepOrigin` (and `previewUrl`) become one tab with the grant while it matches. Saves write `browser` and mirror the active tab's `browserUrl` | migration dropped; mirror missing |
| Each tab's grant covers only its own origin; switching tabs carries none; `copySpec` copies URLs, never grants or `auto` | grant per pane |
| A stored tab on 8405 (or the reported API port) is refused with the sentence on load, and the other tabs load | stored tab trusted |
| Cap of 12: `+` refuses with a sentence; × and middle-click close; the last tab closed leaves "Nothing loaded" | no cap |
| At most 3 iframes are mounted; the 4th-oldest unmounts and reloads its URL when picked | unbounded frames |
| The terminal's "Open in Browser" opens a new tab brought to the front, and the old page is still a tab | replaces the page |
| A hostile path (bidi, markup, 5,000 characters) is a capped text label, and `document.title` is unchanged | label through `innerHTML` |
| Under a card the strip is disabled and inert, and Escape denies | strip live under a card |

### AO-3: vitest (`devopen.test.ts`) and `tests/face/hud_v2_autoopen_check.py`

The suite runs on its own `MockDaemon(0)` with its `dev_port`, SSE `dev_server` records on cue, the mock's claim routes (using the real `arbitrate`), and `FakePty` for terminal sources. It runs after the terminal section of `hud_v2_check`.

| Check | Mutation it must catch |
|---|---|
| Single chat pane, owner typing: an agent `up` gives cols2, with a Direct Browser pane on `dev_port` beside the chat. The chat box keeps DOM focus and the typing continues; `ws.focused`, the selected chat and the mic are unchanged | the split takes focus |
| A window sized so the next shape fits only with the sidebar folded: no split; it goes on to rule 2 or 3 | room check ignores folds |
| 4 drawn and 4 kept (no free id) plus a drawn Browser pane: a new tab there; every kept pane's spec unchanged | `allocate` used (a kept pane taken) |
| 8 drawn, the Browser pane focused: the tab is added behind it with a dot. Not focused: brought to the front | always brought to the front |
| No free id and no Browser pane: the prompt, with pane buttons by slot. An unsaved-edit File pane and a VEX pane mid-chain are disabled with reasons; a busy terminal is offered with its note. Choosing 3 shows the Browser on `dev_port` in slot 3. "Don't open" POSTs `dismiss`, and a second window's prompt goes too | a dirty pane offered |
| The prompt never takes focus. Digits typed in the chat box never pick a pane. `1`–`8` and Escape work only with focus inside | global keys |
| `down` removes the prompt | no expiry |
| Under a card: no split, no tab, no prompt (none in the DOM before paint). Placement runs after the card resolves. A `down` while waiting means nothing happens | opens under a card |
| Two windows: the one showing the thread opens it, and the other does nothing. With neither showing, the most recently focused one does. With no window, a window loaded later shows the notice, and Open runs placement | — |
| De-dup: the same port twice, a restart, and BR-8a plus the watcher on one port all give one pane or tab | no de-dup |
| Rate limit: three sources in a second open one; the next opens after 10 s | — |
| **L2:** the mock records **0** requests to `/browser/*` across every check. The frame's sandbox is `allow-scripts allow-forms` with no `allow-same-origin`. A `[::1]` or control-port URL is skipped, never Remote | auto-open falls back to Remote; keep-origin carried |
| `auto_open` off: nothing happens | — |
| A terminal source: BR-8a's frame with an id opens beside that terminal's pane with no claim POST, then posts `placed` | — |
| A grep pin: `devopen.ts`, `state/devservers.ts` and `DevServerPrompt.tsx` import nothing from `state/browser` | — |

---

## I. Measurements (manual, never in a suite)

- **AO-M1, Codex.** Does a command run under `codex sandbox linux` in `workspace-write`, with the network off, manage to `bind` and `listen` on 127.0.0.1? Is its listener in the root namespace's `/proc/net/tcp`? With the network widened (an approved escalation), does the server appear and carry the tag? Use the real binary; no model call is needed for the sandbox half.
- **AO-M2, Claude.** In a real Claude chat on a fixture project, do `run_in_background` and `nohup … &` servers inherit `JARVIS_ORIGIN_TAG`? Does a server started in the chat open beside it? This costs one turn.
- **AO-M3, cost on the owner's machine.** `/status` → `dev_watch.last_poll_ms` over a working day.
- **AO-M4, WSL forwarding.** A Vite server on Node ≥ 17 bound to `::1` only: does the Direct frame load it from Windows Chrome?

---

## J. Decisions taken here (where L1–L8 are silent)

| # | Decision | Why |
|---|---|---|
| AD-1 | **Two signals beyond L1:** the daemon's own cgroup narrows the candidates (and is the only fd scan), and the per-session `JARVIS_ORIGIN_TAG` attributes. Every L1 check still applies | Folder plus time alone counts the owner's own server in the same folder (an Inbox chat's folder is `~`). It also cannot tell two chats in one project apart. The cgroup halves the scan |
| AD-2 | **Ephemeral ports are not opened automatically** (agent sources only; BR-8a banners keep their ports) | Test fixtures bind port 0. Dev servers use fixed ports |
| AD-3 | **"Answers HTTP on `/`" means a page that may be framed:** 2xx/3xx or `text/html`, and no framing refusal | A JSON 404 or a page Direct can only show as an error is noise. It also excludes `MockDaemon` HUDs |
| AD-4 | `MIN_LIFE_S` 2 s; probe connect 1 s, response 20 s, budget 30 s, 4 KiB, 2 in flight | Short-lived fixtures; Next compiles on its first request; bounded work |
| AD-5 | `GRACE_S` 120 s; `START_SLACK_S` 2 s | Servers often bind after the turn's last step |
| AD-6 | **Rule 1 uses only a free id and never folds a side pane or the panel** | ⊞ promises a hidden pane comes back as it was; an unasked split must not fold what the owner sees |
| AD-7 | **Rule 2 brings the tab to the front unless that pane is in use** (focused, focus inside, used in the last 10 s, or driven) | The owner asked to see it; a pane in use is not disturbed |
| AD-8 | **The claim collects for 600 ms**, and then a window showing the source beats the most recently focused | §K, C5 |
| AD-9 | 12 tabs per pane, 3 live frames, `browserUrl` mirrored for one release | Memory in the owner's Chrome; a safe rollback |
| AD-10 | Polls 1 s while a turn or grace is active; 5 s rows-only while entries live; otherwise off | §B.10 |
| AD-11 | The home folder and `/` never count as a thread's folder for the untagged rule | An Inbox chat would otherwise match nearly everything |
| AD-12 | An unattached terminal's URL is held in memory (path to owner-only routes only); the bus carries its port | L5's "no window" case needs it; L7 allows the URL to the owner's window |
| AD-13 | The prompt: slots, the reasons table in §E.4, keys only with focus inside, never focused | L3, L6 |
| AD-14 | **Under a card, waiting lasts until the card goes** (10-minute backstop), replacing BR-2's 30 s | L6 |
| AD-15 | A URL the Direct judge refuses is skipped, never sent to Remote | L2 |

---

## K. Challenges to L1–L8 (planned as written regardless)

- **C1 (L1, the fast path).** The fast path cannot start a process (§A item 3). Its servers come from tasks, which L1 covers. The rules keep the fast path in at no cost; there is simply nothing to test live.
- **C2 (L1, Codex).** With the network off, a Codex command probably cannot bind at all (AO-M1). The feature then reaches Codex only for commands the owner let out of the sandbox. If AO-M1 shows sandboxed binds succeed in the root namespace, nothing changes here.
- **C3 (L1, the folder rule).** A tag is stronger evidence than a folder. Keeping rule 8 for tagged processes misses an agent's `cd /tmp && python -m http.server 5000`. The plan keeps it, as L1 says. Dropping it for tagged processes only would be a one-line change, if the lead prefers.
- **C4 (L3 rule 1, "a pane can be added").** `splitVerdict` says yes when a hidden pane can be taken (parking its words) or when side panes fold. The plan reads L3's "can be added" more narrowly for an unasked split (AD-6). Otherwise auto-open could discard a ⊞-hidden pane or fold the sidebar under the owner's pointer.
- **C5 (L5, "the first claim wins").** Literally, whichever window's SSE arrives first wins, not the most recently focused one, and that could beat the window showing the thread. The plan collects for 600 ms, then awards a window showing the source, else the most recently focused (AD-8). After the decision, first-come stands.
- **C6 (L5 with BR-8a).** "The next window that loads shows … servers still listening" needs the daemon to keep an unattached terminal's announcement. That reverses #70's "dropped, not recorded" for that one case (AD-12).
- **C7 (L6, waiting).** BR-2's 30 s expiry under a card contradicts "waits until the card goes". The plan follows L6 (AD-14).
- **C8 (L1, "answers HTTP on `/`").** Read literally, this would auto-open every agent's test HUD (`MockDaemon` on 18000–19999) and every JSON API. AD-2, AD-3 and the cgroup narrow it. Q-A1 asks the owner about JSON.

---

## L. Open questions for the owner

| # | Question | Recommendation |
|---|---|---|
| Q-A1 | An agent's server answers `/` with something that is not a web page, such as an API's "not found" in JSON. Open it anyway? | **No.** Only pages (and redirects) open. You can still type its address |
| Q-A2 | When the only place left is a Browser pane you aren't using, should the new tab come to the front? The page you had stays as a tab and reloads when you pick it | **Yes**, unless you're using that pane right now; then it waits behind a dot |

---

## M. Corrections owed by the open PRs (L2)

On main there is no sentence saying auto-open counts as an owner-route navigation. Amendment 1 §D.2 on main says only "It opens Direct, by the loopback rule"; this PR marks its placement rules superseded, and adds L2 to CLAUDE.md's decisions. The sentence lives in **#69** and **#70**, which should apply these replacements before merging (or the lead applies them at merge).

**PR #69 (BR-1a, `feat/browser-engine`, a98c267):**
1. **CLAUDE.md, the BR-1a paragraph's loopback bullet.**
   - Old: "**BR-8b's auto-open of a dev server goes through the window, and the window drives these routes, so it counts as the owner's opening** — BR-8b must keep it that way."
   - New: "**Auto-open never opens a port here** (amendment 2, decision L2): it loads only in a Direct frame, and only the owner's own navigation through these routes opens one."
2. **CLAUDE.md, the decisions bullet "The owner's Browser keeps this machine closed like the LAN".**
   - Old: "BR-8b's auto-open counts because it goes through the window."
   - New: "Auto-open never opens one (amendment 2, L2)."
3. **`docs/plans/2026-10-11-hud-browser-plan-amendment-1.md` §D.2**, the bullet #69 adds, "If it ever opens Remote … which is what opens that loopback port in the Browser's gate …".
   - New: "**It never opens Remote, and never opens a port in the Browser's gate** (amendment 2, L2): an agent can trigger auto-open, so counting it as the owner's opening would hand agents a lever on the gate."
4. **The PR body's MED 2 bullet**, "BR-8b note: if an auto-open ever goes Remote, it must go through the window's owner route".
   - New: "Auto-open is Direct only and never opens a port (amendment 2, L2)."

**PR #70 (BR-8a, `feat/browser-devurl`, 0d7b8da):**
1. **The PR body, "For BR-8b", last bullet.**
   - Old: "Under #69's new rule, a loopback port is reachable in Remote mode only after an owner-route navigation names it. Auto-open goes through the window, so it counts as one."
   - New: "Under #69's rule a loopback port opens in Remote only through the owner's own navigation. Auto-open never counts as one (amendment 2, L2): it is Direct only."
2. **CLAUDE.md, `docs/hud-api.md` and design §18** call the window half "BR-8b". It is now AO-3, and the frame gains an `id` (§D.2). That is worth a word, not required.
3. **"No window attached: the URL is dropped and not recorded"** stays true until AO-1b, which changes it (§C, AD-12).

---

### Critical files for implementation

- **New:**
  - `jarvis/v2/devlisten.py` (AO-1a) and `jarvis/v2/devservers.py` (AO-1b);
  - `hud/src/state/devservers.ts` and `hud/src/components/DevServerPrompt.tsx` (AO-3);
  - `tests/v2/devlisten_check.py`, `tests/v2/devservers_check.py`, `tests/face/hud_v2_autoopen_check.py` and `tests/face/hud_v2_mock_devservers.py`.
- `jarvis/v2/daemon.py`: turn start and end, `_run_brief`'s tag, stop order, `/status` → `dev_watch`.
- `jarvis/v2/hud_api.py`: mounting `/dev-servers`.
- `jarvis/v2/terminals.py`: the `_sniff` registration and the frame id (on #70's version).
- `jarvis/v2/providers/claude.py` (`ClaudeAgentOptions.env`) and `jarvis/v2/providers/codex.py` (the app-server's `env`).
- `hud/src/lib/workspace.ts`: `PaneSpec.browser`, the migration, the tab helpers, `copySpec`, `freeId`, `autoSplitVerdict`.
- `hud/src/lib/devopen.ts`: the rewrite.
- `hud/src/components/BrowserTab.tsx`: the strip and live frames.
- `hud/src/App.tsx`: `splitFrom(source, opts, spec)`, `openBrowser` → new tab, SSE `dev_server`, the prompt.
- `hud/src/components/Terminal.tsx`: `dev_url` with an id.
- `tests/v2/frame_check.py`: the `dev-servers` segment.
