# Browser — preserved notes

Verbatim from `CLAUDE.md` at `5ef4e545432ff632fa47343743fd4b075828aa46`.
Archived instruction/history snapshot, not a new deployment receipt.
Both current obligations and dated/superseded findings appear here.
Moving a rule does not revoke it. Start with [the working guide](../../CLAUDE.md),
read this area's complete rules before changing a boundary, and resolve older
conflicts using the latest explicit owner decision and current implementation.
Paths in preserved prose are repository-relative. Source labels refer to the
original file. Keep payloads verbatim; later notes belong outside the markers.

## Original CLAUDE.md lines 338–645

<!-- claude-source:000338-000645:begin -->
**The Browser's engine, gate and navigation, BR-1a (2026-10-12; plan
`docs/plans/2026-10-10-hud-browser-plan.md` §2–§4, amendment 1 §C, the
decisions file beside them wins; contract in `docs/hud-api.md`).** Backend
only: the stream, input and tabs are BR-1b's, profile management BR-1c's.
`jarvis/v2/browser/` is fully separate from v1's `jarvis.browser` (whose
`SESSION` it never starts). Three layers keep the owner's daemon-owned
Chrome off the control plane and his LAN:
- **The URL judge** (`policy.judge_url`, pure): a typed or stored URL is
  `about:blank` or absolute http(s), never userinfo, another scheme
  (`file:`, `chrome:`, `view-source:`, typed `javascript:`/`data:`, any
  other `about:`), an `ambiguous_authority`, a control or invisible
  character, or Jarvis's ports in any loopback spelling (`is_face_origin`
  plus the daemon's reported ports). Its sentences are fixed and never hold
  the URL (the handler logs refusals). It is the only layer for `file:` and
  friends — a browser-initiated load of those never touches the network.
- **The SOCKS5 gate** (`gate.py`, its own thread `jarvis-browser-gate`):
  Chrome runs with `--proxy-server=socks5://…`, `--proxy-bypass-list=
  <-loopback>`, `MAP * ~NOTFOUND` (Chrome never resolves a name itself;
  defense in depth, since the proxy already gets every name), no QUIC, and
  **no UDP at all**: `--webrtc-ip-handling-policy=disable_non_proxied_udp`
  (a SOCKS proxy carries no UDP, so WebRTC gathers no candidate and sends no
  STUN) and WebRTC's mDNS responder off (`--disable-features=…,
  WebRtcHideLocalIpsWithMdns`: it joined 224.0.0.251 on every interface for
  any `RTCPeerConnection`; Chrome keeps only the last `--disable-features`).
  **The first cut's `--force-webrtc-ip-handling-policy` does not exist**:
  Chrome ignored it without a word, and the review measured 4 candidates, 158
  datagrams to a loopback sink and mDNS on the LAN (PR #69's HIGH). So a
  launch flag is held to its *effect* — `owner_browser_check`'s
  `test_04b`, and an AF_PACKET sniffer in a private network namespace that
  sees nothing leave on any interface but `lo` — never to its spelling.
  **Chrome 155 sends every host as a name,
  IP literals included** (measured), so the gate parses literals itself
  (every spelling `inet_aton` takes, IPv6, IPv4-mapped), maps `localhost`
  and `*.localhost` to this machine without DNS, **resolves a real name
  exactly once and connects its own socket to the numeric address it
  judged**. It refuses `policy.refused_ports` — the one list:
  `control_ports()`, the daemon's reported HUD and API ports, and 8403
  unless it is this daemon's workshop (`/status` → `refused_ports`) — and
  its own port, **on every address, before any DNS**; a *name* that
  resolves to loopback whatever the LAN switch (rebinding onto the owner's
  services; only literals and `localhost` reach loopback); **this machine
  unless the owner opened that port** (`loopback_closed`, below); private
  space unless `browser.json` → `lan` (B-5, off); multicast, reserved and
  the rest of 0/8; unresolvable and malformed names; anything but CONNECT
  with no authentication; past 256 connections. A mixed DNS answer connects
  only to its allowed addresses, and **a name keeps the address class it
  was first allowed on** for the gate's lifetime (`rebinding`: public stays
  public, private stays private; 8,192 names remembered, and **a pin younger
  than 30 min is never forgotten to make room** — the delta review flushed a
  pinned-public name with 8,193 others and it then reached a private address
  with the LAN on; while the table is full of young pins an unpinned name may
  not reach private space, `pins_full`). **Its DNS is bounded and waits
  rather than fails** (PR #69's LOW and delta review): 5 s for the whole
  lookup, 32 worker threads, connections asking for one name at once share
  one lookup, and a domain holds at most four worker threads — counted until
  `getaddrinfo` really returns — a lookup past that **waiting** for one
  within its 5 s (only then `dns_busy`): refusing at once had failed 6 of 8
  images on one CDN. The domain is `gate.domain_key`: the registrable domain
  where a small table of two-label public suffixes can tell (`co.uk`,
  `com.au`, `co.jp`, `github.io`, `vercel.app` … — no Public Suffix List
  dependency; `www.bbc.co.uk` no longer shares slots with `evil.co.uk`), the
  name's parent under a ccTLD whose second level the table does not know, the
  last two labels anywhere else. A positive answer is reused for 5 s (1,024
  names), judged like a fresh one, pin and LAN switch included. A
  black-holed domain cannot starve every other lookup. It logs and counts the
  class, never a host.
- **Loopback is closed like the LAN** (PR #69's MED 2, the lead's decision
  for the owner): a public page read a CORS-permissive service on this
  machine through the gate (Local Network Access does not see behind a
  proxy). A loopback port is reachable only after the owner opened it — a
  URL on this machine in `POST /browser/tabs` or `…/navigate` (`owner=True`,
  which only those owner-only routes pass) — held in the host's memory per
  port until it stops, never one of Jarvis's ports. A page's own move,
  history and any agent's `navigate` never open one; `browser.json` →
  `local_servers` (default off) opens every port that is not Jarvis's.
  **Auto-open never opens a port here** (amendment 2, decision L2): it loads
  only in a Direct frame, and only the owner's own navigation through these
  routes opens one. `/status` → `browser` → `loopback_opened` is a count,
  never which.
- **The landing guard** (`host.py`): every main-frame `framenavigated` is
  judged again (`landed=True`, which also admits `about:blank#…`, Chrome's
  error page and a `blob:` of an http(s) origin) and a refused one is
  replaced with an explanation page. **A pop-up is handed over before its
  first load with an empty URL, and that is not a landing** (PR #69's MED 1:
  judging "" blanked every `window.open` and `target=_blank`); its first real
  landing is judged as any other.

The **host** runs Playwright's async API on `jarvis-owner-browser`, every
call timed; nothing starts with the daemon (the first `POST /browser/tabs`
starts the gate, Playwright and the profile's Chrome), and `Daemon.stop`
ends it after the terminals and VEX, before the bus and listeners — final,
with a backstop that kills, by exact PID, any process whose command line
names the profile's folder as a whole path (Chrome's helpers rewrite their
argv into one space-separated string, so a space is a boundary too) **and
whose `/proc/<pid>/exe` is the launched Chrome** (`chrome` or
`chrome_crashpad_handler` beside `CHROME_PATH`'s target; PR #69's LOW: a
process merely naming the folder is not ours to signal). `stop()` takes
`_start_lock`, so a stop during a start waits for it instead of leaving its
gate, loop and driver running. **Keyed
by profile from the start:** `profiles.json` (0600) lists `p-<8 hex>` ids,
never a label (the first start writes **Main**; a corrupt list refuses
rather than recreating Main over the owner's logins); one Chrome per
persistent profile on `profiles/<id>/`; **Private** is one more Chrome on a
throwaway `private/run-<hex>/` (removed at stop, swept with its Chrome at the
next start — never Playwright's own temp folder in `/tmp`), each Private
pane an off-the-record `new_context()` given the gate explicitly and closed
with its last tab. **`config.BROWSER_DIR`** (`~/.local/share/jarvis/browser`,
`JARVIS_BROWSER_DIR`) is 0700 throughout and refused when it or a folder in
it is a symlink, not ours or open to anyone else; **`config.credential_dirs()`**
is `V2_CREDENTIAL_DIRS` plus it, read at call time, and is what v2's
permissions (`_credential_dirs`, `denied_grant`), `folders` and
`editor_policy` ask now. **`config.BROWSER_SETTINGS_PATH`**
(`~/.config/jarvis/browser.json`) is protected state (`files.
_protected_state`). **A curated launch** (`launch_args`, Playwright's
defaults replaced whole by `ignore_default_args=True`): the deb's sandbox
kept (renderers run under seccomp), Safe Browsing, component updates,
popup blocking and **sign-in and sync kept** (BD-5: no `--disable-sync` on a
persistent profile; Private has it); no `--enable-automation`, no
`--no-sandbox`, DevTools only over the private pipe, extensions off,
permission prompts denied, the media router off (it searches the LAN around
any proxy), `--mute-audio` (sound is BR-10's). **A clean environment** built
from nothing — `HOME` is `BROWSER_DIR/home`, so Chrome's NSS store and font
cache stay in the credential folder; locale variables only, no `JARVIS_*`,
key, display, `PULSE_SERVER` or D-Bus. `TMPDIR` is left alone on purpose:
Chrome's singleton socket lives there and a long path crashed it (measured).
Downloads are BR-1b's (below). **Routes** (`browser/routes.py`): every one
`projects.owner_only` plus a strict loopback Host (its own copy — a module
importing `terminals` fails `terminal_check`) and no query, checked before a
body is read or Chrome started; a load reports a failure by its `net::ERR_…`
code only; `/browser` paths are logged without a query. **No URL, title or
label** reaches `/status` → `browser`, the bus or the log. **An unknown
field in any route's body is a 400 naming the count, never the keys**
(`daemon._object`, every route: "unknown fields (2)"; the names are the
client's text and the message is logged). **Only the daemon and the browser
package reach the engine**: `browser_gate_check`'s grep pin fails if any
other module under `jarvis/` imports `browser.host`/`browser.gate`, names
`BrowserHost`, builds a `Gate` or a `/browser` path, or calls the daemon's
`browser.open_tab`/`navigate`/… — no tool, MCP tool or fast-path module
(BR-2's tools will need their own row in it). Measured under the 3G cap
(Chrome 155): launch ~0.3 s, first tab ~0.6 s, the tree's PSS ~365 MiB with
one tab, ~490 MiB after the attack pages and ~470 MiB for Private (RSS
summed over its 14–19 processes 1.1–1.9 GiB, shared pages counted once per
process), stop ~0.1 s. A first tab asked for twice at once starts one Chrome
(`_start_lock`).
**v1's file tools refuse every credential folder** (PR #69's LOW and delta
review): `read_file`, `write_file`, `edit_file`, `grep_files` (both
backends; `search.refused`) and `query_sqlite` refuse anything in
`config.credential_dirs()` — the Browser profile, `~/.ssh`, `~/.aws`,
`~/.config/gh`, `~/.config/jarvis`, `~/.gnupg` — as written or where it
resolves (`files._credential_folder`); the gate's own state files keep their
rule and stay readable by their own, un-symlinked path. Before, the fast
path's `read_file` and `query_sqlite` could read the profile's History (a
SQLite file). **A search judges with one `search.refuser()`**
(`files.credential_folder_judge`): the folders and the gate's paths read
once, each directory's real path once, a file's own only when it is a
symlink — 20k paths in ~0.25 s, where calling the single-path rule per file
took ~3 s; `files_check` holds it under 1 s and the two backends to parity.
Free suites: `tests/v2/browser_gate_check.py` (no Chrome: the judge, every
refusal family against stand-in listeners whose accept counters stay 0,
rebinding through an injected resolver asked once, a name's class pinned,
loopback closed until opened, a black-holed domain not starving other
lookups, the LAN switch, every ATYP, a dev port piped both ways, the grep
pin) and `tests/v2/owner_browser_check.py` (real headless Chrome, one
instance at a time, 3 tabs, its gate's resolver answering only `pub.test` —
a public address its connector carries back to the fixture — and its
connector refusing all else but loopback, so nothing reaches DNS or the
network: the twelve ways into the HUD — typed, 302, meta refresh,
`location=`, iframe, fetch, XHR, WebSocket, a service worker's fetch,
`<img>`, form POST, `window.open` — from Main and from Private, each with a
sanity half that reaches an ordinary server by all twelve once the owner
opened its port; a "public" page and every way refused a loopback port nobody
opened, reaching it once the owner did, and another port still refused; no
ICE candidate and no datagram at a loopback UDP sink (STUN, TURN, a remote
candidate, WebTransport); pop-ups landing on their own page, one sent to
Jarvis's port on Chrome's error page, one moved past the judge replaced;
refused schemes; owner-only; the folder; the launch, sandbox and
environment; persistence across a restart and forget; settings; stop during
a start; a decoy naming the folder left alone; stop). With
`JARVIS_NETNS_SNIFF=1` inside `unshare --user --map-current-user --net
--keep-caps` (only `lo` and a dummy interface holding the default routes),
its sniffer sees ~15,000 packets on `lo` and none elsewhere. `files_check`'s
`credential_folder_checks`; `frame_check` walks `/browser`. Each of the
plan's mutations and each review fix's guard removal bites (the PR lists
them; the two UDP ones run only in the namespace, since the old flag sends
mDNS onto the real LAN). **Stated limits:** the Playwright driver (a node
process) inherits the daemon's environment, as v1's does; none of this is a
boundary against a program running as the owner, which can read the profile
or use the gate (no new lever: it could connect directly); the gate's name
pins live in its memory (a restart forgets them); a public answer for an
unpinned name is allowed while the pin table is full of young pins, so such a
name could rebind once the pins age; **a loopback port the owner opened stays
open to every page, every profile and Private, until the daemon stops** (a
follow-up for BR-1c: count the panes per port and close it when the last one
leaves). No idle stop or
instance cap yet (BR-1c), and **BD-10's memory ceiling is not set** (BR-1c's):
one tab ~365 MiB PSS, ~490 MiB after the attack pages, Private ~470 MiB.

**The Browser's stream, input, hold, dialogs, tabs and files, BR-1b
(2026-10-12; plan §2 "The stream", §3, §8, §11; amendment 1 §B.3 H3/H8,
§B.4, §B.8–§B.9; contract in `docs/hud-api.md`).** Backend only; BR-3b draws
it with BR-2's `remote.ts`/`drive.ts`. `browser/stream.py` (the socket, its
messages, tickets, `TabStream`), `browser/files.py` (downloads, uploads) and
the host's CDP half. **A socket per tab per window**, the terminal's pattern
on `ws.py`: owner-only routes, a **single-use 30 s ticket bound to one tab**
(`POST /browser/tabs/<id>/ticket`; the attach's `?ticket=` is the one query a
`/browser` route takes), a tab another window shows taken only on its yes
(20 s, else refused), and **every attach publishes `browser_attached` with
exactly `{tab_id, at}`**. **Frames** are binary messages (an 8-byte header:
uint32 id, uint16 CSS width and height, big-endian; then the JPEG,
quality 70), **one in flight**: the next goes only after the window's `ack`
("up to here", BR-2's `FramePipe`), and what Chrome sends meanwhile
replaces one waiting slot — Chrome's own frames are acked at once, so it
keeps capturing at the page's repaint rate and the newest picture is what
goes next (a slow window costs Chrome's encoding, never a backlog); a window
that attaches gets the current picture at once (`_last`, forgotten whenever
the screencast stops, since an unwatched page can change); a tab no window
streams, or a window that says `visible: false`, is not cast. **Every message
is validated in the daemon** (`stream.parse`: exact fields, types, bounds —
BR-2's `CdpKeyEvent`/`CdpMouseEvent`, text ≤ 16 KiB with no control but tab
and line ends, x/y from BR-2's `OFF_PAGE` (-1) to 4096, wheel ±5000; a bad
one is `{t:"bad", reason}`, a fixed sentence, and acts on nothing).
**The hold** (H3): with any `daemon.approvals.pending()`, input is dropped on
arrival and again on the Browser's thread immediately before each CDP event
(`_pump`, one event at a time), `held` is sent, and **the moment a card is
raised every tab's queue is dropped** (`BrowserHost._observe`, a bus observer
on `approval_requested`, synchronous before the broker waits), with a
`_drop_through` mark so a message received before the card is never sent
after it — a dispatch-time check alone would have replayed input that sat in
the queue through a card no input arrived during. The owner's `navigate`,
`history`, `POST /browser/tabs` with a URL and `files` answer 409 under a
card; Resume is refused (`held`). **A press a card cut off never clicks**, the
daemon's rule and not only the window's: BR-2 assumed a release at
`OFF_PAGE` with `clickCount: 0` fires no click, and **Chrome 155 fires
`click` on `<html>`** (the common ancestor of the press and the release),
`detail: 1`, whatever `clickCount` says — measured by the fixture BR-2 asked
for. So a release of a button pressed before a hold (`tab.pressed` keeps the
stream's `hold_count` at the press), and any release off the page, is sent
as a pointer move with no button down (`_let_go`, Blink's own reset): no
`mouseup`, no `click`, and the next click is ordinary; a socket that drops
mid-press is let go the same way (never under a card). Keys also record
the hold count before dispatch; a keyup or char from before a later card
is dropped, so Space never activates after the card. **Owner input
stamps** every tab it reaches (keys but a lone modifier, text, wheel, press,
release, drag, dialog and chooser answers — never a plain move), and while
an agent holds the tab pauses it **before** the event is queued
(`{t:"agent", kind:"paused", by:"owner"}`); the `{t:"agent"}` channel
(`driver`, `action` capped at 60, `paused`, `list` of 50, replayed on attach)
and `owner_state(tab_id)` are BR-9b's to drive. **Dialogs** go to the window
(`{t:"dialog", id, kind, message, default}`, cleaned to 1000) and are
answered there; dismissed after `DIALOG_TIMEOUT_S` (60), and at once with no
window attached, with automatic dismissal waiting under a card. An open
dialog drops further input to that tab on arrival, and in the pump so does an
earlier event Chrome has not answered within `DELIVER_WAIT_S` (never on
arrival: every event of an ordinary burst finds its predecessor in flight,
which dropped 190 of 200 keys — PR #77 delta review), with one
`input_dropped` notice; nothing is banked for later. **Pop-ups** open as tabs and the
opener's window hears
`{t:"popup", tab_id}` (the listing carries `opener`); **the cap stays 12 in
all** (decision B-13 "12 in all", which also bounds each profile's Chrome;
the brief said per instance) and **past it the pop-up is refused**
(`popup_refused`), never an older tab closed: an older tab may hold the
owner's work, and a page could otherwise evict the owner's tabs one by one.
`POST …/activate` brings a tab to the front. **Downloads**: Chrome writes
into `downloads/<profile>/incoming/`; a finished one is copied by fd to
`downloads/<profile>/<id>/<cleaned name>` (the folder 0700, made fresh, the
file 0600 `O_EXCL|O_NOFOLLOW`; `files.clean_name`: last
component, invisible and Windows-reserved characters out, no leading dot,
200 UTF-8 bytes keeping the extension) and Playwright's copy deleted;
listed and served
owner-only (`GET /browser/downloads[/<id>]`, `application/octet-stream`,
attachment, `nosniff`, the policy narrowed to `default-src 'none'`, and
**refused for any `Sec-Fetch-Dest` but `document`/`empty`** — a downloaded
HTML or script must never run on the HUD's origin, where it could POST
`/approvals`); **an executable is held** (`act_rules.executable_download`, or
its first bytes `MZ`/ELF/`#!`), 409 until BR-9b's card, deletable; executable
suffixes also cover scripts and launchers (`.js`, `.py`, `.com`, `.url`,
`.desktop` …), ignoring case and including `x.pdf.exe`; a Private
download lives in `downloads/private/<ctx>/` and goes with its context or
Private Chrome. **`set_files`**: a page's file
chooser (`page.on("filechooser")`, so Playwright intercepts every one) is
answered only by `POST …/files {chooser, files: [{project, path}]}`, each
judged by **`act_rules.upload_refusal` (H8), called**, then read through an
`O_NOFOLLOW` fd whose realpath and device/inode must match H8's judgment,
and handed to Chrome
as bytes (≤ 16 files, 50 MB in all). Ctrl+C's `{t:"selection"}` reads the
selection in an isolated world (`jarvis-copy`; a password field gives "").
`/status` → `browser` gains counts only (`streams`, `downloading`,
`popups_refused`). Free suite: **`tests/v2/browser_routes_check.py`** (35:
13 pure — every BR-2 shape parses, ~50 wrong shapes and bounds refused, names,
tickets, a `../` name kept in its folder — and 22 on one real headless Chrome
at 3 tabs, its resolver answering nothing and its connector loopback only:
ticket and takeover, frames under a slow reader, keys/text/mouse/wheel at a
logging page, the hold and no replay, the cut-off press, the agent channel,
selection, dialogs with beforeunload, pop-ups and the cap, downloads, uploads,
a Private tab, nothing on the bus or in the log, stop); `frame_check` walks
the new routes and a served download's headers. Guard removals in scratch
copies, 10 of 10 bite with a passing control: no hold, held input replayed,
an unbounded backlog, a `../` name, no card-raised drop, a cut-off press
released as is, a reusable ticket, downloads not owner-only, H8 not asked, a
bad message delivered. Measured (Chrome 155, 1280×800 at DPR 1, quality 70):
60 fps with an immediate ack on an animated page, ~6 KB a frame there; the
tree's PSS 270–400 MiB through the suite. **Stated limits:** Chrome encodes
at the repaint rate however slow the window (the M1 spike's question); no
download size cap (a page can fill the disk the profile is on); the window's
`takeover` answer is not in BR-2's `isTakeover`
control list (BR-3b adds it); a page tracking a drag sees no `mouseup` for a
press a card cut off.

<!-- claude-source:000338-000645:end -->

## Original CLAUDE.md lines 1732–2230

<!-- claude-source:001732-002230:begin -->
**Preview and dev servers, WP-E (2026-10-10; plan
`docs/plans/2026-10-09-hud-workspace-plan.md` §2.5, decisions W-4; design
§18; contract in `docs/hud-api.md`).** **No HUD (8402) or API (8405)
response may be framed**: `Content-Security-Policy: frame-ancestors 'none'`
and `X-Frame-Options: DENY` on every one — page, JSON, SSE, every error, a
malformed request's refusal (answered HTTP/1.0, never a header-less 0.9
body) — added in the daemon handler's `end_headers`, the one place every
status line passes; the terminal's hand-written `101` passes
`hud_api.frame_headers` to `ws.upgrade` explicitly. **One** CSP header,
built by `hud_api.content_security_policy(*extra)` — a response's own
directives (the avatar SVG's) fold in, and **a comma** (it starts a second
policy: `img-src 'self', frame-ancestors *` let a frame land on the HUD),
anything outside printable ASCII (CR/LF split the header; U+2028 failed
after the status line) or a reserved `frame-ancestors`/`sandbox`/`report-*`
**raises** (`_own_directives`; `binary()` checks before writing a byte, so
it is one 400, never two status lines); the HUD's full CSP (ED-4, below)
is built by that same function, never a second header. The preview
listener (8403) carries **neither** frame header — the HUD frames it — but
**sandboxes every document it serves** with one CSP, `sandbox allow-scripts
allow-forms` (`workshop_security_policy`): a keep-origin frame that navigated
itself to the workshop used to get its real origin (PR #31 review), and a
workshop page in a top-level tab is now opaque too. v1's whiteboard workshop
is its own server, untouched.
`tests/v2/frame_check.py` walks every route family on both listeners and
**fails if a route module gains a first path segment it does not walk**,
and fails if any status line but `ws.py`'s is written by hand. `/status`
gains `hud_port`, `api_port` and `frame_hardened` (the handler class's own
flag, beside `end_headers`). **The Preview pane** (the **Browser** since BR-3a, `lib/browser.ts`; below) refuses
the HUD's and API's ports — as reported **and** 8402/8405, the live
daemon — and the window's own, and restores nothing until `/status`
answers (`state/daemonPorts.ts`, read once per page). **"Keep its origin"**
(`preview-keep-origin`, `browser-keep-origin` since BR-3a) adds `allow-same-origin`, the only flag ever added
to `allow-scripts allow-forms` (never top navigation, popups or downloads):
off by default, per pane, offered only for a loopback port that is none of
the daemon's three nor the window's own, and only while `/status` says
`frame_hardened: true` — a HUD on an older daemon never offers it. **It is
stored as the origin it was given for** (`PaneSpec.keepOrigin`), so another
port, host or scheme turns it off by construction; it is judged again on
every load and **dropped from storage** when it no longer passes; disabled
and inert under a card. Safe because scripts plus same-origin are no sandbox
only for a page on the parent's origin, which the port check refuses and
`frame-ancestors` keeps a frame from becoming. **A Ctrl+clicked loopback
link in a terminal opens a menu** (`term-link-menu`): Open in Preview (Open in Browser, `term-link-pane`, since BR-3a) — a
drawn Preview pane, else the focused pane, by the sidebar's click rule;
refused with the reason for the daemon's ports — or Open in a browser tab (Open in a Chrome tab, `term-link-tab`);
other links still open a tab directly. It is measured and placed by
`placeMenu` (`lib/terminal.ts`): below the click, else **above** it — a dev
server's `Local:` line is usually the panel terminal's last row, where it
used to open below the window — and clamped on both axes, zoom-aware; its
Escape listener is a layout effect that ignores Escape once a card is up.
`judgePreviewUrl` (`judgeBrowserUrl` since BR-3a) refuses userinfo, as `judgeLink` does. xterm finds a link only when the
pointer enters a new cell, so a test that clicks the same cell twice must
move through another first. The mock serves an API listener and a dev server
on ephemeral ports (`api_port`, `dev_port`) and asserts the API one is never
hit; the mock HUD, API and workshop send the real `hud_api.frame_headers()`,
so the HUD under test is always rendered under them. Not yet: §2.5 item 5, a "dev servers in your terminals" list.

**A framed page never takes the keyboard on its own (2026-10-12, R-1, AO-2's
first half, shipped alone; `docs/plans/2026-10-12-hud-browser-plan-amendment-2.md`
§E.8, §H AO-2; contract in `docs/hud-api.md`).** The gap PR #74's review
found (Chrome 155): `el.focus()` or `window.focus()` in a Browser frame,
with no user activation, moves focus out of the HUD, and every later key —
chat words, an editor's `.env` value, a terminal password — goes to the
page, which can send it home. `inert`, `display: none`, `visibility:
hidden`, re-focusing the box and `contentWindow.blur()` do not stop it, a
`display: none` frame included; **removing the iframe does.** So each
Browser iframe is made by script in **`FrameHost`** (`useLayoutEffect`,
never JSX, so a removal never trips React; `data-browser-frame="<pane>:<tab>"`,
tab 0 until Direct tabs; a fresh frame per load), the decision is pure in
**`lib/frameguard.ts`**, and **`state/frameguard.ts`** (installed once by
App, a layout effect) removes a frame that gains focus without the owner's
gesture, puts focus back **only on the HUD element that lost it to this
theft** (caught in its own `focusout`, its text selection kept) — never on one
that had it earlier, so a page that grabs from another page the owner was
typing a password in can never hand the next keys to the chat box, where an
Enter sends them (review of PR #78, H1); with no such element focus goes to
nothing that types or sends (the notice's container, `tabIndex=-1`, never its
Reload), or under a card the card — tells the pane — `browser-stolen`, "This page took the keyboard · Reload"
(`browser-stolen-reload`, React text, plain chrome, disabled and inert under
a card; the tab's URL stays; memory only) — and writes one console line
naming the pane slot, never the URL. **Measured in Chromium 149, headless
and headed under WSLg:** a script steal fires `blur` on the HUD element,
`focusout`, then the window's own `blur` with `activeElement` already the
iframe, all before any key reaches the page; no `focus`/`focusin` fires on
the iframe element. The guard decides in that `blur`, synchronously. **A
steal from one frame into another fires nothing at all** (contrary to
§E.8's `focusin` claim): `focusin` is listened for anyway, and what catches
it is a per-animation-frame check while the keyboard is in a frame, with the
250 ms `activeElement` belt behind it. **Headed, the HUD sees no pointer
event over a frame and the iframe never matches `:hover`** (it runs in a
process of its own; headless it does see `pointerover`), so "the pointer is
over it" is read from a **transparent shield** over every frame
(`browser-shield`): a `pointermove`, `pointerdown` or `wheel` on it means
the owner's pointer is there, and it steps aside; a pointer event anywhere
else in the HUD brings it back, and so does **any key typed in the HUD**
(decision: a pointer parked over a page stops counting at the owner's first
key, narrower than §F's stated limit), and so does the window losing focus
to another program or being hidden (the HUD never hears the pointer leave a
frame, so one that left the window from inside a page would else count on).
**While a HUD text control has the keyboard** (an input, a textarea, Monaco,
a terminal, contenteditable; `isTypingTarget`), **or another Browser page
does** (delta review: crossing page B while typing in page A gave B the
keys), **the shield steps aside only
on a press**, never a move or a wheel, and the HUD then puts the keyboard in
the page with `contentWindow.focus()` (the lead's option (a) on review M2): a
pointer crossing a page mid-typing is never "over" it. The cost, accepted:
the first click after typing in the HUD only focuses the page, and a wheel
there scrolls nothing until a click. **A Browser iframe is never
`.focus()`ed** (H2): FrameHost replaces each frame's `focus` with its
window's, since an element focus left `activeElement` on that frame after
another frame took the keyboard, blinding the guard. The gestures: the pointer over it;
Tab or Shift+Tab within `TAB_MS` 300 — **not cancelled** (Monaco's indent is
not one) **and only for the frame it landed in**, the first place focus went
after it (M1); the owner's own
load — Load, Enter in the URL box, Project root, the keep-origin switch,
Reload — claimed by the first frame made after it, live until 2 s after
that frame's `load` (20 s if it never loads) and **revoked by any key in the
HUD or a focus outside that pane** (decision); and the HUD's own give-back
after a card (`allowRestore`, ApprovalVeil, once, 300 ms — otherwise every
card would unmount the page the owner was typing in). A stored URL restored
at mount and a terminal's "Open in Browser" are no gesture. **Never**,
whatever the pointer says: a card up (read from the DOM, `#authveil`), or a
frame not the active tab of a drawn pane (or with no box on screen); a held
frame under a card goes; a held frame that stops being drawn loses the
keyboard to nothing that types, without being removed. **Playwright's `locator(frame).click()`
hit-tests before it moves the mouse and reads the shield as covering the
frame**: suites click frames with `page.mouse` (the layout suite's
card-focus check changed so). Free checks: `lib/frameguard.test.ts`,
`state/frameguard.test.ts` (jsdom), and `hud_v2_browser_check`'s `_guard_*`
sections (`BROWSER_ONLY=guard`), against the mock dev server's
`/focus-mount`, `/focus-timer`, `/focus-load`, `/focus-none`,
`/focus-pointer` and `/focus-reload` pages and their key log
(`mock.dev_keys`); the review round's checks (H1's exact scenario — no POST,
the draft unchanged — H2's give-back, M1's three Tabs, M2's crossing and
click-then-type, and the two surviving mutants) each fail with their guard
removed. **Limits**: a key in flight as focus moved can be lost, IME
composition in the box may be cut, a frame-to-frame steal is caught within
an animation frame (a key typed in that gap reaches the page), a pointer
resting over a page while no text control has the keyboard counts until the
owner's first key, and none of it binds a same-uid program.

**Dev servers announced in a terminal, BR-8a (2026-10-12; Browser plan
`docs/plans/2026-10-10-hud-browser-plan.md`, amendment 1 §D.1/§G/§H, the
decisions file wins: B-2; design §18; contract in `docs/hud-api.md`).** The
daemon half of "a dev server opens in a Browser pane by itself"; BR-8b opens
the pane. `jarvis/v2/devurl.py` (pure) is fed a **shell** terminal's **live**
output by `Terminal._sniff`, from `_output` under the lock it already holds,
**only while `browser.json` → `auto_open` is on** — off, the sniffer is told
the marks' letters and never fed a byte. It reads **only a command's
output**: `Marks.events` hands it the letters of *signed* OSC 133 marks, and
it skips `A`/`B` to `C` once the shell has marked a command (bash), else
`B` to that line's line feed (the POSIX startup file marks no command), so a
typed `curl http://localhost:3000` opens nothing; never the alternate screen.
Escapes are stripped by **`terminal_text`'s own compiled patterns** (imported,
not copied) — colour draws nothing (a run of text and whole SGRs is one
C-level match, `_PLAIN`), cursor forward is a space, cursor back,
BS or a deleted character spoils the line, any other move, CR and the line
feeds end it. **Two cheap tests that cannot change an answer run before the
table** (PR #70 review, LOW: a line holding `://` ran up to 17 regexes, and
URL-dense output went through `_output` at ~9 MB/s): a loopback host
spelling (`_HOSTS`: `localhost` in any ASCII case, `127.`, `0.0.0.0`, `[:`,
`[0`) and one of the shapes' words (`_WORDS`; `local` only before a space or
`:`), as plain substring searches first (`_has_host`, `_has_word` — the
latter folds the four characters IGNORECASE equates with ASCII letters, ſ ı
İ and the Kelvin sign, so it is the regex or wider), over a chunk's whole run
of lines and then per line; and a chunk (or a mark) matches at most
`MATCH_BUDGET` (32) lines against the table — only a flood of real
announcements reaches it, and one frame a second can go anyway. **A new
`SHAPES` row adds its word to `_WORDS` and `_WORD_LITERALS`**; devurl_check's
`Prefilter` differential (20,000 lines, every case and fold) fails
otherwise. Measured through `_output` with a socket that takes everything:
another host's URLs 191 MB/s (was 9.8), a loopback access log 103 (10.9),
plain logs 262 (124), colour 44 (21), repeated or distinct announcements
62–69 (7.1 and 2.7); xterm draws ~14. A finished line is matched against `devurl.SHAPES` (Vite,
Next, CRA, Angular, webpack-dev-server, `http.server`, Flask, uvicorn,
Django, Puma, Hugo, Jekyll, Streamlit, Gradio, `php -S`, `serve`, Expo,
Jupyter, Astro, Storybook, Parcel, MkDocs, Eleventy) and `devurl.clean` keeps
only **loopback http(s) with an explicit port** (`localhost`, `127/8`,
`[::1]`, `0.0.0.0`, `[::]`) and **sends every one as `localhost`**, the port
and path kept (PR #70 review: the window can frame no other loopback name,
and `[::1]` or `127.0.0.2` only burned the server's frame), no userinfo,
**never Jarvis's
ports** (`devurl.jarvis_ports()` = `config.control_ports()` ∪ 8403 ∪ the
configured workshop, plus the daemon's listeners as bound, `own_ports`), the
query and fragment dropped (a Jupyter `?token=` never travels). **Memory: a
512-byte line (dropped whole past it), a 64-byte escape carry, the last four
origins and four send times** in the sniffer, whatever it is fed. **One
`{"type": "dev_url", "url", "id"}` frame on a nonce-bound attached socket**
(AO-1b-ii below; legacy sockets omit `id`), after the bytes that carried it;
`may_send` de-duplicates by (scheme, port) and rate-limits (one a second,
four a minute per terminal). `sent` records a URL after a successful socket
send or unattached registry retention. A refused frame leaves the next
banner eligible; **unattached URLs are retained in the owner-only memory
registry**, superseding BR-8a's drop behavior. A replay never reaches it; a
sniffer that raises is dropped for that terminal (one log line, the
exception's type) and never stops its output.
**Nothing on the bus, in a log line, a thread log, Discord or a file carries a
URL or a byte of the text**; the "Jarvis can read" switch is unrelated.
**`browser.json`** (`config.BROWSER_SETTINGS_PATH`, env
`JARVIS_BROWSER_SETTINGS` — empty means unset, never `.`, which would put the
working folder in the protected state) holds BR-1a's `lan` and
`local_servers` (both off) and `auto_open` (**on**). **One reader and one
writer: `jarvis/v2/browser/settings.py`** (`BrowserSettings`), the daemon's
`browser_settings`, which the gate (`value("lan")`, per connection), the host
(`value("local_servers")`), `/status` → `browser` and the terminals
(`auto_open()`) all read — never a second loader; a test fails on a second
definition of the path, a second `load_settings`/`save_settings` or another
module naming `browser.json`. It is **gate state** (`files.
_protected_state_paths`, by path and by name beside the allowlist) and
changes only by **owner-only `GET`/`PATCH /browser/settings`** in
`browser/routes.py`, which owns every `/browser*` path (`is_owner` plus a
strict loopback Host, no query; a body of any non-empty subset of `{lan,
local_servers, auto_open}`, each a boolean, an unknown field counted and
never named); written atomically at 0600 keeping keys it does not know
(BR-10's sound); read without blocking, never through a symlink, re-`stat`ed
at most once a second so a hand edit lands without a restart. **An embedded
or test daemon keeps it in memory** (`BrowserSettings()`); daemon2's
`main()` attaches the owner's file, as it loads the Codex catalog. **Auto-open
never opens a port in the Remote gate** (amendment 2): the window loads the
URL only in a Direct frame; only the owner's own navigation opens one. The
HUD ignores the frame until BR-8b (`parseControl` drops unknown types). **Limits**: in bash a server
started with `&` whose banner lands while the prompt is up is skipped
(between `B` and `C`); typed-ahead input echoed during a running command is
read as that command's output; text that only looks like a banner (`cat` of a
log) is read as one — the window judges the URL again. Free suites:
`tests/v2/devurl_check.py` (every `SHAPES` row from real banner text, whole,
split at every byte and at random cuts, with escapes in every form; the
refusals; the marks; 10 MB leaving at most a line; the rate limit and
de-dup; the settings file and its gate-state refusal) and `terminal_check`'s
`DevUrls` (one window hears it, another does not; the typed line never read;
Jarvis's ports never sent; bus, log and disk clean; off feeds nothing; a
replay or a second announcement sends nothing; the switch is the owner's;
an unattached URL not remembered; a forged mark moves nothing; a failing sniffer
never stops the output; a frame the socket refuses not remembered; five
floods at twice xterm's rate through `_output`); `frame_check`
walks `/browser`. Seven guard removals, each in a scratch copy, fail all nine
of their checks (a control port passed — both suites, a URL on the bus, marks
ignored — both suites, escapes kept, the query kept, `auto_open` ignored,
whole lines kept); an unmutated copy passes them. The review round's seven
fail all nine of theirs: the prefilter removed (another host's URLs spend the
budget, so an announcement among them is missed), the prefilter and the
budget removed (both speed checks), a prefilter too strict (no bracketed
hosts; no `token=`: the differential), recorded before the send, the host
sent as printed, and the empty setting read as `.`. The reconciliation with
BR-1a's six fail theirs (`devurl_check`'s `test_one_reader_one_writer` and
`terminal_check`'s `test_one_set_of_switches_for_every_reader`): a second
definition of the path in `config.py`, a second reader in `host.py` (the
gate's `local_servers` from the file), a second writer in `routes.py`, a host
keeping a `BrowserSettings` of its own, the daemon's constructor reading the
owner's file, and the empty setting read as `.` on the merged definition.

**The Browser's pure HUD modules, BR-2 (2026-10-12; browser plan
`docs/plans/2026-10-10-hud-browser-plan.md` §2, §5, §8, §9, §11; amendment 1
§B.9, §D.2, §G; the decisions file wins).** Four new files under `hud/src/lib/`,
each with its vitest; **nothing imports them yet** — BR-3b (Remote mode),
BR-4 (annotate), BR-8b (auto-open) and BR-9c (driving) wire them in.
`remote.ts`: keys and the pointer to CDP `Input.dispatch*` shapes (**AltGr is
the character it typed, never Ctrl+Alt**: `@` on a German layout arrives as
Ctrl+Alt+@), `routeKeyDown` (held under a card and left to bubble, so Escape
denies; the HUD's keys; the pane's own F5/Ctrl+R/Ctrl+L/Alt+←→; a paste is
never a key, its text goes as `insertText`; copy and cut forwarded and
marked), **`browserTakesKey`** — the page gets every key but Ctrl+D
(push-to-talk), the zoom keys, Ctrl+Alt+1–8 (the plan's "Ctrl+Alt+N"), Ctrl+B,
Ctrl+Alt+B and Ctrl+`; Ctrl+P, Ctrl+Shift+P, F1 and Ctrl+Shift+F are the
page's, as Ctrl+P is the shell's; BR-3b owes it an exception in `useLayout`'s
capture handler, beside `terminalTakesKey`'s — and **a lone modifier is never
sent** (review of PR #66: push-to-talk's Ctrl, pressed before its D, reached the
page and paused a driving agent; `modifiers` carries the state), `HeldKeys`
and `HeldButtons` (a keyup only where its keydown went; nothing released under
a card — they wait for `releaseAll(false)`; **a press a card cut off is let
go off the page, never as a click**: a held-button move to `OFF_PAGE` (-1,-1),
then the release there, every event `clickCount: 0` — released where it was
pressed, Chrome would click what the owner never finished pressing),
`pasteMessages` (`cleanPaste`, then ≤ 16 KiB per `{t:"text"}` once
JSON-escaped, never between a CR and its LF), the remote viewport (the canvas's CSS size at DPR × zoom, capped at
16 Mpx), pointer positions **divided by the zoom** (`toCss`) before they are
page points, wheel deltas (screen px ÷ zoom, lines 40 px, pages a viewport,
Shift sideways, capped), a **generation-tagged, latest-only `FramePipe`** (a
frame of an older socket, even one still decoding, is never drawn or acked)
and `cleanTitle` (Cc/Cf/Zl/Zp, tags, variation selectors, U+034F and fillers out; one
line, capped by code point). `drive.ts`: the daemon's `{t:"agent", kind:
driver|action|paused|list}` frames (shapes proposed in its header for BR-1b),
the banner (never Authorize/Approve/Deny), the newest 20 actions, the target's
outline (a reattach's `driver` or running `list` restarts the banner), Resume
refused under a card, and **`isTakeover`** (a pointer move is not, a drag is;
a lone modifier and the socket's own messages never; an unknown kind of input
is). `devopen.ts`: `autoOpenPlan` → split (focus never moves) | focused |
offer | wait (under a card; 30 s) | skip (off, the WP-E judge
`judgeBrowserUrl` or Jarvis's control ports, an origin already opened or shown, one per 10 s per terminal,
three a minute); the query and fragment are dropped; a Browser pane already
showing a page counts as busy. (Amendment 2, 2026-10-12, replaces `focused`
and `offer` with a tab in a drawn Browser pane, else a prompt, and the 30 s
wait with "until the card goes"; AO-3 rewrites it.) `annotate.ts`: the
whiteboard's ops model in the still's pixels (pen, arrow, box, text; three
colours), `render` at any scale, undo/clear, the zoom-aware letterbox fit, and
the staged picture's size and type (1920 px; `page.jpg` at 0.9 past 3.5 MB).
**A trap found building it:** the Write tool decodes `\uXXXX` in what it
writes, so a regex meant to name U+2028 held the character and stopped
parsing; write `\u{…}`, or fix escapes afterwards with a script.

**File reads as the editor, Preview is the Browser, BR-3a (2026-10-12; the
owner: "also rename "File" to "Editor" and "Preview" to "Browser""; browser
plan `docs/plans/2026-10-10-hud-browser-plan.md` §7, cut by amendment 1 to
the rename and the migration; design §18; contract in `docs/hud-api.md`).**
**The Browser changes in name and id both**: view `"browser"`,
`PaneSpec.browserUrl` (`keepOrigin` unchanged), `setBrowserUrl`,
`openBrowser`/`BrowserRequest`/`browserAsk` in App, `BrowserTab.tsx`,
`lib/browser.ts` (`judgeBrowserUrl`), `.browserframe`, test ids `tab-browser`
and `browser-*`, and the terminal link menu's `term-link-pane` ("Open in
Browser") and `term-link-tab` ("Open in a Chrome tab"; WP-E's
`term-link-browser` collided). Which links get the menu is unchanged —
loopback only; B-11's any-http(s) waits for Remote mode — and so is every
rule of WP-E's. **The stored layout migrates as it parses**, under the same
`jarvis.hud.workspace` key (`parsePane`, `storedView`): `view: "preview"`
reads as `"browser"`, `previewUrl` as `browserUrl` (only when no valid
`browserUrl` is stored), and a `keepOrigin` is kept only while it is still
exactly that URL's origin (`storableKeepOrigin`; the frame judges it again on
load). A save is a parse, so it writes the new names only; a rolled-back HUD
shows that pane's default view and loses its URL. **The editor changes in
name only**: its view id stays `"file"` — a stored `"file"` pane, `tab-file`,
`pane-N-…` and every editor suite are untouched — and **`VIEW_LABEL`** beside
`PANE_VIEWS` (`lib/workspace.ts`) is the one label table, drawn by the
strip's buttons, its compact `<select>` and the pane's `aria-label` (`Pane N:
editor`); the CSS capitalises it. Owner-visible sentences moved with it: the
palette's "Show Files" is "Show Editor", "No room to open …" says "switch one
to the editor", the judge says "Refused: the Browser only loads local
servers." **Python is left alone**: `preview_route`, `preview_only` and
`workshop_port` name the workshop listener (8403), which the docs now call
"the workshop origin". Not done here: WP-E's `DirectFrame` split (BR-3b's,
with Remote mode). `hud_v2_preview_check.py` is
**`hud_v2_browser_check.py`** (`browser_checks`), and its `_rename_checks`
seed a pre-rename layout with the Preview pane in pane 1 (default chat, so a
dropped view migration shows), check its URL and grant come back and the
save writes only new names, an old grant for another origin is dropped, and a
stored `"file"` pane opens as the editor with its tabs. Both of the plan's
mutations bite: dropping the migration fails four `workspace.test.ts` cases
and the headless section, carrying `keepOrigin` across an origin mismatch
fails the grant case. A stored seed in any suite may still say `"preview"`
(it migrates); new ones should not.

**Repeated terminal banners refresh liveness (AO-1b-ii review).** Frame
deduplication does not suppress a same-terminal registry restart: an already
announced origin refreshes its same-source entry, retaining its id, first path
and placed/dismissed state. After the 60-second tombstone expires, a new
unclaimed entry uses the normal source/global rates and registry cap. Neither
path preclaims or sends a second frame; a different terminal cannot revive a
retained tombstone through this path. Terminal-output regressions cover all
prior states, startup grace, expiry, switch-off and the caps.

**Dev servers an agent started, AO-1a (2026-10-12; plan
`docs/plans/2026-10-12-hud-browser-plan-amendment-2.md` §B and §D.4, review
decisions R-2 and R-7; the decisions file wins).** Pure and **not wired yet**:
AO-1b's watcher (`devservers.py`), routes and daemon hooks use it.
`jarvis/v2/devlisten.py`:
- **Rows.** `parse_rows` reads `/proc/net/tcp`/`tcp6`: LISTEN rows only, the
  kernel's native word order, binds on `127.0.0.1`, `0.0.0.0`, `::1`, `::` and
  `::ffff:127.0.0.1`. Another 127/8 address is kept as `odd_loopback`, to be
  counted and refused; a LAN bind is dropped.
- **Ports first.** `cheap_refusal` (rules 1-2) runs before anything about a
  process is read. `port_rules` is `policy.refused_ports` plus the daemon's
  HUD, API, workshop, gate and bound ports; then the seven service ports, and
  the ephemeral range read from `ip_local_port_range` (inclusive; fallback
  32768-60999). A socket owned by another uid (the daemon's uid is passed in)
  and a bind class that is neither loopback nor wildcard are refused there too.
- **Facts.** `ProcReader` reads over an injectable `/proc` and cgroup root and
  counts every read in `Work` (counts only, for `/status`). **It never reads
  `cwd`, `cmdline` or `exe`.** It fails closed on EACCES, a vanished pid, a
  start time that changed, or a parent younger than its child.
  - **The ancestor walk** reads `stat` only, at most `ANCESTRY_MAX` = 64. A
    chain still going past that is `truncated`, and rule 6 refuses it as a
    terminal's: fail closed, never attributed.
  - **The fd scan is bounded.** `socket_owners` makes at most `FD_SCAN_MAX` =
    50,000 readlinks a scan (a pid's folder is walked lazily, so a process
    with a million fds costs no more). Past it the scan stops, counts the pids
    it left in `Work.fd_skipped`, and answers `complete` False;
    `attribute(scan_complete=False)` then answers pending ("scan") and
    attributes nothing this poll. `scan_complete` has no default, so a
    caller that forgets the scan's answer fails rather than trusting it.
  - **A shared socket fails closed:** if any holder is Jarvis's own, a
    terminal's, or a pid whose facts could not be read (it could be either),
    the listener is refused, whatever another holder's tag says.
- **The origin tag is the attribution (R-2).** There is no folder rule, and an
  untagged process is refused. `/proc/<pid>/environ` is read only for the
  owning pid (the earliest-started pid passing rules 4-7), only after rules
  1-7, `O_NONBLOCK`, at most 128 KiB. The bytes go into a bytearray searched
  raw for `JARVIS_ORIGIN_TAG=` at an entry's start (exactly 32 lowercase hex,
  NUL-ended), which is zeroed in a `finally`. They are never decoded, logged
  or raised with: a read error is `unreadable` with no message, and **the
  module logs nothing**. A found tag and `untagged` are cached per `(pid,
  start)`; **a failed read (`unreadable`) is never cached**, so the next poll
  reads again. **Limit, stated: a cached tag outlives an `exec` of the same
  pid** — `exec` keeps the start time, so a process that execs a program with
  another environment is still judged by the one first read. A pid reused by
  another process has another start time and is read afresh.
- **Turn windows.** `Sessions` holds the tags (live, then `GRACE_S` 600 s
  after close) and the turn windows, which only Claude and Codex turns open: a
  listener counts if first seen in `[start, end + 600 s]` from a process
  started at or after `start − 2 s`.
- **`attribute()`** applies rules 1-11 in order. It answers attributed, a
  refusal class, or pending: `scan` for an incomplete fd scan, `life` for
  rule 10's two sightings 2 s apart, `probe` for rule 11.
  - **A socket several processes hold fails closed.** If any holder is
    refused as Jarvis's own (rule 5) or as a terminal's (rule 6), the listener
    is refused with that class, even when another holder carries a live tag.
    Only with no such holder does the earliest-started pid passing rules 4-7
    own it (this replaced "earliest passing pid owns it" in all cases).
  - Rules 7 and 9 are judged at the listener's first sighting, so a verdict
    waiting on its probe cannot turn `stale_tag` as the clock passes the
    session's grace.
- **The probe's rules.** `parse_response` judges at most 4 KiB, and only the
  status class and booleans survive:
  - a page is a 3xx; a 2xx that is `text/html`, or untyped with markup; any
    other status only `text/html` with markup;
  - `X-Frame-Options` DENY/SAMEORIGIN (any token of a comma list), or an
    enforced `frame-ancestors` without `*` or `http://localhost:*` in any
    comma-joined policy, is `no_frame` (a 3xx too); the last `Content-Type`
    wins;
  - the header block ends at the earlier of CRLF-CRLF and LF-LF, an obs-fold
    continuation (a line starting with a space or tab) is joined onto the
    previous header with one space, and markup needs a tag name that ends
    (`<header` and `<htmlx` are not `<head` or `<html`);
  - a silent close or a TLS alert asks for one TLS probe.

  `probe_address` (127.0.0.1 or `::1`, with the port judged again) and
  `probe_request` (one plain `GET /`) are its other rules.
- **`arbitrate()`** takes the claims within 600 ms of the first: the
  earliest that `shows` the source, else the newest `focused_at`, ties to the
  earliest. A later claim, or a window's second, loses. A malformed claim (a
  field of the wrong type or out of range, or not a `Claim`) is dropped, never
  raised on and never the one that opens the window.

Free suite: `tests/v2/devlisten_check.py`, 258 checks over fixture `/proc`
trees, plus a read-only parse of the real `/proc/net` tables.
- A fake key in a fixture `environ` appears in no return value, exception,
  log record or counter.
- A monitor shows that the module opens no `cmdline`, `cwd` or `exe`, and no
  real `environ`.
- Work is counted, not timed: a steady poll reads 2 files, and one candidate
  costs one fd scan.

All 24 mutations bite: every one in §H's AO-1a table, plus the review's
guards. The review round (PR #76) ran 53 guard removals against the fixed
module, each failing the suite: the review's own 20 (13 already bit; the seven
it had survived are the buffer zeroing, a read cut at the cap reported
whole, the tag-cache key's start, rule 7's per-window conjunction,
comma-joined CSPs, X-Frame-Options lists and the last `Content-Type`),
`environ` opened following a symlink, `find_tag` taking a later occurrence,
and every guard of the fixes above. A grep pin allows only `daemon`,
`hud_api`, `terminals` and `devservers` to name the module.

**Dev-server daemon wiring, AO-1b-i (2026-10-12; amendment 2 §B–§D).**
`devservers.DevServers`, attached to the daemon, calls AO-1a's rules with
the fd scan's `complete` answer. A lazy `jarvis-devwatch` polls only during
Claude/Codex turns and their 600-second grace, or for live registry entries;
`auto_open` comes only through `devservers.enabled()` → `BrowserSettings`.
A poll exception logs its type once and stops the watcher; only a new turn
start or settings change can re-arm it. Off means no watcher work. `main()`
supplies the real proc/cgroup roots;
embedded/test daemons must explicitly supply fixture roots to watch.
An initial baseline is old. Transient missing facts retry three polls;
before publication the same `(pid, start)` must still hold the inode.
Two bounded loopback probes send only GET /, never follow redirects, and
retry TLS once after a silent close or alert. Responses are never retained.
The memory registry de-dups ports for 60 seconds after down and publishes
only `{kind: dev_server, data: {id, state, source, at}}`; no port or URL,
no thread-log record, and no Discord delivery. Owner-only `/dev-servers`
routes return URLs, arbitrate claims for 600 ms, and renew/complete/dismiss
90-second leases. Only the last owner HUD stream for a window ending lapses
its claims; the API listener's parameter has no effect. `/status.dev_watch` is counts.
`Brief.origin_tag` is hidden from repr, exists only in the session run copy,
and reaches Claude's SDK env and Codex's app-server env, never argv or
`brief.json`. `_open` writes it only when absent; it previously only
round-tripped the stored brief, and the tag is never in it.
Close retains attribution for the grace. Stop ends terminals,
then the watcher and probes, then the bus. Terminal announcements join the
registry through AO-1b-ii below. `devservers_check` uses fixture
proc trees, fake CLIs and fixed free loopback ports; `frame_check` walks the
new family. No tool or Discord verb reaches either dev module.

**Terminal server registry, AO-1b-ii (2026-10-13; amendment 2 §C/AD-12).**
Shell-terminal banners register even with no window attached; only owner
registry/claim responses and the attached socket expose their cleaned paths.
The bus keeps exactly id/state/source/time. The optional attach query
`window=<32 lowercase hex>` binds to that socket and survives takeover only
as the new holder's nonce. It preclaims before a `{type: dev_url, url, id}`
frame; legacy attaches omit id and remain unclaimed. First source on a port
wins, and claimed/placed/dismissed entries are not redelivered. Registry and
settings callbacks run outside the terminal lock; an opaque delivery receipt
prevents a failed or stale socket send from lapsing a newer claim. A final
registry-locked enqueue refuses a claim that lapsed or changed before delivery.
Unattached
registration wakes the lazy watcher without any agent turn; terminal entries
are never HTTP-probed, and two absent LISTEN polls after five seconds mark
down, including the initial baseline poll. Restart within 60 seconds reuses
id/dismissal and resets terminal startup grace. Program/VEX terminals stay
outside the sniffer. AO-3 must pass one shared **32-hex** page nonce to attach,
events and claims: the existing frontend helper's 16-hex nonce is incompatible.
The backend preserves existing legacy clients until that HUD change.

<!-- claude-source:001732-002230:end -->

## Original CLAUDE.md lines 3564–3657

<!-- claude-source:003564-003657:begin -->
**The Browser's action rules and reviewer, BR-9a (2026-10-12; amendment 1 to
the Browser plan, §B.3–§B.5, decisions BD-1..BD-4). Pure, and not wired yet:
BR-9b calls them from `hud_browser_act`'s gate.** Two modules in
`jarvis/v2/browser/`. **`act_rules.pre_judge(Facts) → PreVerdict`** answers
REFUSE, ASK(category), ALLOW or CLASSIFY with no model call. `Facts` (frozen)
holds the action, key, text, `nav_url`, the page's `url`, the target
`Element` and its `Form` — attributes only, **no field for a value** — the
upload path and project folder. **Every state fact defaults to None and only
an explicit value passes** (review of PR #71, MED 2): a flag must be the
exact bool it needs, a number a finite non-negative int or float — `None`,
`"no"`, a str number, a negative count, NaN and infinity all refuse, so a
fact the caller forgot refuses (`STATE_FACTS` names them; the suite unsets
each in turn) — and `pre_judge` turns any exception into a refusal. Order:
H12 (desk, depth, sub-agent), H11, H5 (Private needs its own drive switch),
H6, H3, H4 (paused; an `owner_input_age_s`, whenever one is given, must be
valid and at least `OWNER_QUIET_S` = 5 s, `owner_has_input=True` needs one,
and `owner_has_input=False` beside one is a contradiction that refuses — the
delta review's regression), H9, H10's counts, H7's fact; a valid action
(`press` takes named keys only, a printable character is refused; at most
10,000 characters to type and 8 KB to select; `type`/`select` need a
target); H7's belt (a navigate address: http(s) only, at most 8 KB,
Jarvis's ports, an ambiguous authority, userinfo; a link or a submitted
form's action: at most 8 KB, Jarvis's ports, an ambiguous authority); **H1** (type, select, Backspace/Delete/Space into `type=password`,
an `autocomplete` token current-/new-password, one-time-code, webauthn or
**any `cc-*` anywhere in the token list**, the label words and their joined
spellings on name, id, label, placeholder, aria-label and accessible name
(English only), a numeric 4–8 field labelled just "code", four or more
one-character boxes, any field in a `PAYMENT_FRAME_HOSTS` frame, or a focused
<iframe> showing one — clicking into one is not refused); **H2** (text, a
navigate address, a clicked link or a submitted form's action holding a
`secrets.secret_values()` or caller's value in `terminal_guard.value_forms`'
spellings, or a `credential_patterns` shape); **H8** (`upload_refusal`: a
regular file with one name on disk, under the project folder lexically and
resolved, at most 50 MB, not `secrets.is_credential`, not `editor_policy`'s
withheld, not the gate's state, not in Jarvis's own folders, and no hidden
component but `.well-known` — `editor_policy.hidden_component`, the preview
origin's own rule, moved there from `hud_api._not_a_page` so both call one
function in a SELF_PROTECTED module; `PreVerdict.path` is what was judged, and **BR-9b must judge again
after the card and upload a private copy opened `O_NOFOLLOW`**). Then the
ASK floors (every upload; a click on anything but a text field or on
anything that submits, Enter, or Space on such a control, in a form — or
beside a field — holding a password, OTP or payment field;
`ASK_URL_SHAPES` on the route of the address, link or submit's action — the
path, the fragment (`#/login`) and route-query values (`?route=checkout/…`),
decoded and lower-cased, a host's own shapes first; `chrome:` pages as
account_security; links ending in an `EXECUTABLE_EXTENSIONS` name by
`executable_name`, which `executable_download` shares; an editing key or Enter
with no target named, as `no_target`), the turn's 40 judged actions (floors
and CLASSIFY count, the ALLOW tier does not), the ALLOW tier (scroll, back,
forward; **the nav keys only on a known target that is no frame and is in no
form and beside no field holding a password, OTP or card** — with autofill
on, ArrowDown or Tab in a card field accepts a saved card, MED 3, for BR-9b
to confirm in real Chrome; **Backspace/Delete only in a text field** — on a
mail row, a file, a grid cell or a button they delete the selection, the
review's HIGH; at most 64 characters with no line break into `type=search`,
`role=searchbox` or a GET form's field — never beside a sensitive field),
else CLASSIFY. **Linear**: no regex is built from page text, labels are
normalised by C-level passes (camelCase split for `name`/`id` only, which
fail closed past 1024 characters), route segments go into sets; a 1 MB label
is judged in a few tens of ms, and the URL matchers stay linear on 100,000
characters though the rules refuse past 8 KB unread. Every refusal reason is
a fixed `R_*` sentence.
**`act_review.judge(facts, owner_words, *, extra_values) →
ActVerdict(verdict, category, reason, source)`** is `command_review`'s shape:
the owner's words (≤ 500) outside, everything else in one `untrusted.fence`
with a tag per call, capped as §B.5 says; a flagged context, or a credential
value (the caller's too) or shape anywhere in the request, means no call and
ask (source `guard`); **never a Chinese-hosted model** (MED 4: the review
tier follows the owner's selection, so a model `models.cn_hosted` names falls
back to the configured orchestrator, and if that is CN-hosted too, no call
and ask); JSON only, exactly three keys, each once; **every failure is ask**
(category `unjudged`); only `ordinary` is ever allowed;
`JARVIS_BROWSER_REVIEW_AUTOALLOW=0` turns allow into ask and leaves deny;
only ordinary allows are cached (15 min, 512), **keyed by the profile, the
owner's words and a digest of everything the model saw plus every text,
address, element and form in full** (MED 1: a narrower key reused an "OK"
after "saved" under "Delete this repository", a Download link for another
href and a top-page button in an ad frame). **The call is `max_retries=1,
stream=False`**: `llm.chat`'s `max_retries` is its attempt count, so the
amendment's `max_retries=0` would send nothing and every action would ask.
The model is `models.tier("browser_review")` once a config defines that tier
(BD-4), else the review tier. `act_review.decide(pre, verdict)` is the gate's
outcome: anything but exactly one of the four kinds refuses, a floor is never
lowered, the belts apply again, and CLASSIFY with no answer asks. Free
suites: `tests/v2/browser_act_rules_check.py` (62) and
`tests/v2/browser_act_review_check.py` (34), with `llm.chat` raising
throughout and `secrets._JARVIS_ROOT` a temp folder; 47 guard removals each
fail their suite in a scratch copy (the PR lists them). The two moved
functions are pinned where they are used: `hud_backend_check`'s
`OneHiddenRule` and `titles_check`'s `OneVendorList` patch the one function
and see the verdict move, so a copy fails them. Left for BR-9b: the
wiring, the `browser_review` tier, SELF_PROTECTED for both modules, and the
re-check and private copy for uploads.

<!-- claude-source:003564-003657:end -->

## Current Direct Browser tabs (AO-2, 2026-10-13)

**Direct Browser tabs (AO-2, amendment 2 §E.3).** Browser panes store
`browser: {tabs, active}`: up to 12 URLs with stable ids and per-tab origin
grants. Old `browserUrl`/`previewUrl` migrate; saves mirror only the active
`browserUrl`/`keepOrigin` for one release. Explicit empty tabs stay empty.
Every tab's grant is rejudged with the serving daemon's ports, dormant tabs
included; splits copy URLs with fresh ids, never grants or auto-source marks.
The strip supports arrows, ×, middle-click and a blank + (URL box focused).
At 12 a sentence refuses another. Terminal **Open in Browser opens a new tab**
or selects an exact URL already present, keeping the previous page.
`lib/browserframes.ts` and one App hook retain up to 3 visited frames per pane
and 8 per window, drawn active tabs first, oldest backgrounds evicted. Hidden
frames count; never-drawn stored panes do not. An evicted page reloads when
picked. Each foreground/background iframe still belongs to `FrameHost`,
guarded by stable `<pane>:<tab-id>`; tab selection grants only that exact
frame. A stopped tab stays stopped until Reload. Background content is
hidden/inert, and every strip/control is disabled under a card. Labels are
cleaned host/path text, capped at 40; auto-source names cap at 60 and appear in
visible chrome. Remote mode and autonomous placement remain later packages.

## Current automatic-opening identity and OFF contract (AO-3, 2026-10-13)

The HUD's shared `lib/windowId.ts` provides one fresh memory-only 32-lowercase-hex
identity per live page. Terminal holder records, dev-server ownership and future
Remote front-window reporting use that same identity; copied storage is never
its source. Missing secure random bytes yields no valid auto-open identity.

An owner settings change publishes a fixed `dev_server_settings` boolean-only
record before invalidating pending claims on OFF. Existing arbitration and claims
are revoked, and claim/renew/placed refuse while disabled; a later ON does not
revive old ownership. Owner-only listing includes effective `auto_open`.
No URL, port or source text is added to that settings event. UI placement wiring
belongs to the remaining AO-3 work, not to this backend contract alone.
## Browser source provenance (2026-10-13, BR3b/AO3 shared seam)

Auto-open display labels do not authorize reading. Each tab independently keeps
`source: {kind:"dev_server", entryId}` or `{kind:"unknown"}`. Legacy auto tabs
and invalid source records become unknown; copies retain source even when they
drop the display mark and origin grant. Passive URL changes, tab selection,
reload and project-root actions preserve source. Only a successfully judged
owner address-field Enter/Load submission clears it, including the same URL;
that also drops the obsolete auto label so storage cannot recreate a legacy
restriction. `submitBrowserUrl` is separate from ordinary `setBrowserUrl`.
The later daemon pane binding resolves the entry to immutable source identity;
labels and client-supplied terminal IDs never authorize BR5. Unknown stays
fail-closed. This shared model change alone does not enable Remote or a tool.

## Current: automatic Direct opening (AO-3)

A memory-only 32-hex page identity now binds HUD event streams, terminal
attachments and dev-server claims. Agent announcements and terminal preclaims
flow through the owner-only registry; initial unclaimed entries are quiet
notices. Claims renew while waiting, and a fresh ownership check precedes
every placement. OFF is broadcast as a boolean-only event and invalidates
pending work across windows; stale OFF/ON replies cannot restore it.

Automatic placement splits without moving focus or folding visible areas,
reuses only strictly blank kept panes, then appends to a drawn normal Browser
pane, then offers the bounded nonblocking pane-choice prompt. Private panes
are excluded. Cards and Settings defer placement. Prompts rearm for 500ms
after movement; their keys stay local and dirty-editor/VEX choices are
disabled. Busy terminals move to the panel alive on explicit pane choice.

Automatic tabs are Direct, query/fragment-free, without keep-origin grants,
and retain immutable dev-server source provenance. Existing frame guards
apply to every resulting frame. No automatic action calls /browser/*;
Settings uses /browser/settings only for explicit preference writes, while
the registry listing supplies the effective preference on connect/reconnect.
The rates are one action per source per 10 seconds and three per window
per minute (amendment E.2; the older H summary row is superseded).

Validation lives in devopen/state unit tests and hud_v2_autoopen_check.py:
real production claim arbitration over isolated mock SSE/HTTP, fake terminal
sockets, focus/card/Settings/lease/window races, and a zero-Remote-route
request log assertion in every scenario.

## Current BR-1c implementation (2026-10-13)

This current note supersedes the archived BR-1a host-lifetime loopback rule and
pre-BD-5 sync-off wording above. Profile metadata is strict atomic 0600 JSON,
with random path IDs, up to eight labels (1–32 visible NFC characters, unique
case-insensitively; Private reserved). GET/POST/PATCH/DELETE owner profile routes
never start Chrome by themselves. Deleting the final profile leaves no profiles.
See [HUD API](../hud-api.md#additions-2026-10-12-the-browsers-engine--br-1a) for
route shapes, refusal codes and the dedicated sign-in entry.

A maximum of three persistent Chrome instances includes launches and closes in
flight, plus one shared Private engine. Persistent instances stop 300 seconds
after their last tab; reuse cancels the idle task. Private panes use independent
off-record contexts; explicitly sharing a ctx shares its login state. Context
close removes its tabs/downloads; the last context stops Private and removes
its temporary profile, HOME and artifacts. Forget stops one profile, waits for
download workers, wipes its profile/downloads, and leaves its label and an empty
0700 folder. Delete also removes metadata after the wipe. Per-profile locks
cover queued opens, downloads and even a cancelled filesystem wipe; a failure
cannot be reported as a successful deletion. Failed or cancelled context
retirement retains its identity and closing state until strict data cleanup
finishes, so DELETE can retry it. Healthy existing Private contexts stay usable;
new-context admission waits for unresolved cleanup. Repeated cancellations of
a profile wipe still cannot release its lock while the worker is running.

Only an admitted owner navigation temporarily grants a local port, retained
while its authorizing tab remains there. Failure/cancellation rolls it back;
last-tab leave/close/forget/delete revokes both new and established local SOCKS
tunnels. The connector rechecks permission after its await. Pages, popups,
history and agents do not create grants. `local_servers` stays a global owner
override. The gate is shared across profiles/Private: cookie separation does
not imply per-profile network permission.

BD-5 keeps persistent sign-in, sync and autofill enabled. The dedicated,
hold-respecting owner sign-in route admits only exact `chrome://settings/syncSetup`
and its locally measured `chrome://settings/people` redirect in that tab; leaving
ends the capability. Generic navigation still refuses internal URLs. A temporary
profile with blocked external network rendered and captured the settings page.
Actual Google login, sync, saved credentials and keyring behavior are **not
verified** (manual M4/M4b/M12). `browser.sync` now reports the store each
running Chrome was observed to encrypt with and the keyring's state; see
*Password store: the owner's keyring* below. It never inspects secrets.
`navigator.webdriver` remained true in the real local fixture.

The profile regression suite uses one real Chrome at a time and checks summed
RSS below 1.5 GiB; concurrent three-plus-Private admission uses barrier-controlled
doubles. This does not establish simultaneous production memory demand or a
production memory ceiling (BD-10). No HUD profile chip is included here; BR-3b
consumes these backend routes.

## Browser source provenance (2026-10-13, BR3b/AO3 shared seam)

Auto-open display labels do not authorize reading. Each tab independently keeps
`source: {kind:"dev_server", entryId}` or `{kind:"unknown"}`. Legacy auto tabs
and invalid source records become unknown; copies retain source even when they
drop the display mark and origin grant. Passive URL changes, tab selection,
reload and project-root actions preserve source. Only a successfully judged
owner address-field Enter/Load submission clears it, including the same URL;
that also drops the obsolete auto label so storage cannot recreate a legacy
restriction. `submitBrowserUrl` is separate from ordinary `setBrowserUrl`.
The later daemon pane binding resolves the entry to immutable source identity;
labels and client-supplied terminal IDs never authorize BR5. Unknown stays
fail-closed. This shared model change alone does not enable Remote or a tool.



## Current owner pane registry (2026-10-13, BR3b incremental interface)

`browser/panes.py` now owns live window/pane/tab mappings for the pending Remote
UI and BR5. Exact owner-only front, permission and address routes are documented
in `docs/hud-api.md`. All reports carry explicit see and drive preferences. Effective Remote drive requires
the global switch and see; Private additionally requires this pane/context owner opt-in.
Resolved dev-server identities survive entry expiry and cannot be erased by a
passive report. Only successful owner address submission clears source; failure
and stale navigation preserve it. Cached host document identities invalidate
same-URL reloads before the next report. Snapshot URL/source stay memory-only;
`browser_viewed` carries only a host-issued view ID, timestamp and refusal flag.
This registry commit does not yet supply Remote UI or a browser-reading tool.

## Current Remote restore and landing replacement (BR3b, 2026-10-13)

Stored persistent Remote tabs allocate a blank tab and use the owner-only `POST /browser/tabs/<id>/restore {url}` route. It calls `navigate(owner=False)`: restoring state cannot open the shared local-server gate. A live stored handle reattaches without navigation; expired Private state never replays a saved URL. Actual address entry continues through the pane registry transaction and may open the ordinary owner navigation lease.

The landing guard captures the offending document generation when scheduling its explanation. The queued replacement takes the same per-profile lifecycle lock as owner navigation, checks the original tab/generation before navigating, then checks tab/generation/about:blank again before setting explanation content. It cannot blank a newer owner document or write into a page closed during replacement. This is scheduled work; callers never await it while holding the same lock.
## Current read-only Browser look (2026-10-13, BR5)

`hud_browser_view(pane="", whole_page=False)` is an explicit HUD owner-chat
tool in the fast path and the foreground Claude/Codex session MCP catalog.
The pane number is its displayed slot (1–12), not a tab ID;
empty selects the last owner-focused eligible Browser pane. It uses BR3b's live
owner-window registry and never discovers background tabs or hidden windows.
The registry, see switch, source, terminal readability, hold/approval state,
desk presence and page/document identity are checked before and after capture.
A terminal-derived page keeps that restriction across passive reports, copies,
mode changes and report expiry. Unknown provenance refuses. An owner address
submission is the separate explicit disclosure boundary. Browser input and
arbitrary navigation remain unavailable to this tool.

The host scans in a CDP isolated world using the same visibility judgement as
v1. Every open-shadow descent checks its host's visibility, including nested
hosts; direct ShadowRoot text uses the host for style ancestry. No DOM attribute
stamps, edits, scroll, focus or input are issued. Default text is viewport text; whole-page text includes rendered reachable DOM without
scrolling. Refs and the image stay viewport-only. Refs additionally require
centre hit-testing, at least 8×8 pixels and opacity above 0.05; ordinary text
visibility does not promise occlusion filtering. Limits are 20,000 visited
nodes, 256 KiB raw text/attributes, 200 refs and a five-second scan stage within
a 30-second capture deadline. Incomplete scans refuse. Presentation caps page
text at 4,000 characters after the complete bounded URL/title/attribute/text
payload is credential-checked, so a hidden tail cannot evade the guard.

Known secrets and recognized credential formats refuse text and image together.
Keyword-based withheld lines also suppress the image. Form values are never
read; input, textarea, select, editable and unscanned embedded-frame rectangles
are masked in memory before the viewport PNG is resized to a 1,568-pixel long
edge. Pillow is a runtime dependency. The image is emitted only if the actual
pinned model is known to accept images; turn-scope restoration prevents this
capability from leaking into another turn. The guard is not OCR: canvas/image
text, closed/inaccessible DOM, unknown secrets and adversarial changes between
scan and pixels are not fully detectable. These limits remain in result notes.

A Direct look is explicitly a fresh load, not the owner's exact view. It uses
a new off-record context only when an existing owner lease or global
`local_servers` already permits the port. It never grants a port. A dedicated
reservation counts against the tab cap; failure/cancellation retires the context
through BR1c's retryable cleanup before releasing the reservation, without
closing healthy owner Private contexts. Pending cleanup blocks another hidden
look until resolved. Network policy is still the shared gate, not per-profile
network isolation.

Page data is fenced as untrusted and enters only the owner's tool conversation.
`PRIVATE_RESULT` summaries say only `looked at a page` or `refused`; the
`browser_viewed` bus record contains exactly the bound opaque `tab_id`, timestamp
and refusal flag. Direct uses the registry's host-minted view ID, never a hidden
page ID or a pane-local ID. The tool is not parallel-safe and is not available to
background tasks, workflows, subagents or default session tools. The foreground
MCP catalog uses the existing WP2/WP3 host provenance and desk ticket: child,
missing, stale and replayed calls refuse before capture. Revocation during a
capture withholds text and pixels together. Normal assistant prose can describe
the result; private normalization protects tool/event summaries, not that prose.

## Current foreground Browser MCP (2026-10-13, BR6)

Claude and Codex receive the same parent-only `hud_browser_view` catalog entry.
The exact applied provider/model is pinned after the provider's apply-choice
callback, only for the current session, worker, turn and desk slot. A requested
model switch that fails retains the surviving model's capability. Only a model
entry with the boolean `vision: true` enables images; unknown models receive
text. Updating that capability cannot restore a lost desk presence.

The existing relay carries PNG image blocks without a new capture path. Shared
Browser presentation limits the image to 6 MiB of base64 so the bounded text and
JSON envelope fit the relay's unchanged 8 MiB response limit. Oversized images
are omitted with an explicit size note and the guarded text remains available;
there is no second capture or image degradation. Foreground, see/source,
credential, document and post-capture checks are otherwise unchanged. Provider
events use fixed looked/refused summaries, including interrupted calls and
unmatched Codex MCP approval-review completions.

Synthetic acceptance checks cover actual Chrome pixels, masks, no page mutation,
the real relay, current-model pinning, child refusal and revocation. Native CLI
image acceptance and deployment are separate release gates; this implementation
does not establish real account authentication or browser driving.

## Current Remote restore and landing replacement (BR3b, 2026-10-13)

Stored persistent Remote tabs allocate a blank tab and use the owner-only `POST /browser/tabs/<id>/restore {url}` route. It calls `navigate(owner=False)`: restoring state cannot open the shared local-server gate. A live stored handle reattaches without navigation; expired Private state never replays a saved URL. Actual address entry continues through the pane registry transaction and may open the ordinary owner navigation lease.

The landing guard captures the offending document generation when scheduling its explanation. The queued replacement takes the same per-profile lifecycle lock as owner navigation, checks the original tab/generation before navigating, then checks tab/generation/about:blank again before setting explanation content. It cannot blank a newer owner document or write into a page closed during replacement. This is scheduled work; callers never await it while holding the same lock.

## Current owner capture boundary (2026-10-13, BR4)

Owner captures use resolve_owner/owner_current on one exact current binding.
They bypass only initial see/source disclosure policy for that explicit capture;
full snapshot equality, page/document generations, held state and a monotonic
approval epoch remain checked. Agent still_current and BrowserLook.read retain
their existing gates. The lower CDP capture adapter has an optional1920px owner
image edge; its BR5 default remains1568px. Known credential/heuristic hits omit
image and text together. Capture-time masks do not prove arbitrary pixels free
of secrets; send-time credential revalidation promises retained text only.

Capture references are bounded, memory-only and owner-send-only, with fresh
untrusted fences and canonical mirror names. Capture metadata is not logged or
published on SSE. The HUD drawing layer uses the existing pixel-coordinate ink
model and stages an atomic image/reference pair into a pinned draft. Direct
app-mode Region Capture remains unverified until a real M6 receipt exists;
never substitute a fresh hidden Remote load for the owner's Direct pixels.

## Current owner Remote Browser UI (BR3b, 2026-10-13)

The Browser strip retains Direct tabs and adds Remote tabs backed by the owner host. Public HTTP(S) address entry chooses Remote; loopback defaults to Direct, with explicit mode conversion. Profile is per pane; mode is per tab. Jarvis can see defaults on and persists an explicit off across navigation, profile changes and copies. Agents may drive is an owner setting, default on; named panes retain an explicit driving opt-out. Private opt-in is runtime-only and bound to the exact pane/context. Private is always Remote, with one new off-record context for each copied pane; Private URLs, titles and origin grants are omitted from stored workspace state. An ended Private context never replays a stored page.

One ordered publisher uses the shared live SSE window identity for front reports, permission changes and owner address transactions. Layout/Settings/visibility changes revoke a slow address immediately with a newer sequence; confirmed mutation receipts remain current when React catches up. Only successful explicit address entry clears source restrictions. Default profile discovery preserves existing handles, while explicit profile conversion reserves allocation ahead of lazy restore. Restored persistent pages use the non-owner restore route; existing handles reattach without navigation. Failed host cleanup remains visible with an owner retry action.

Each visible active Remote stream consumes a fresh ticket and owns its decode/input generation. Hidden, covered and disconnected streams close while retained handles stay. Approval and annotation blocking stop all page input, clipboard cuts, paste/IME and file/dialog actions without interrupting incoming frames. Unsolicited selection cannot write the local clipboard; cut follows a successful owner clipboard write only. Page dialogs are plain text and never autofocus. Uploads send selected project/path references, and downloads save only on an owner gesture; interrupted late responses cannot create a local save.

Profile controls expose creation, rename, Chrome sign-in, confirmed clearing and deletion through the owner routes. The HUD states Chrome's basic password store and possible sync limitations (since 2026-10-15 it states the store actually in effect: *Password store: the owner's keyring*). This package does not enable agent driving or prove a real account sign-in. BR4 adds capture/annotation controls separately; its identity getter uses the exact current host receipt, not stream pixels.

## BR7 Windows fonts (2026-10-13)

When `/mnt/c/Windows/Fonts` exists, Chrome uses a private `home/fontconfig/fonts.conf` and first-choice cache under the Browser credential folder. The distro Fontconfig include retains Linux aliases and fallback; no system config or font files change. Private uses its disposable run/home. Unreachable Windows fonts keep the original curated environment. Config files are atomic 0600, directories 0700; invalid existing config types, ownership or modes refuse startup. No inherited Fontconfig/XDG/Pulse settings are accepted.

A local one-series probe with the exact include-based config rendered 36 Segoe UI glyphs (CDP verified). Baseline ready 0.238 s / RSS 918.1 MiB; new-cache ready 2.594 s / 926.5 MiB; warmed font cache with fresh profile 0.187 s / 917.1 MiB. All remain below the 30 s launch deadline. These are synthetic observations, not a general performance claim. Linux sans-serif/monospace matching and distro rules stayed unchanged; created font caches were private and external cache metadata stayed unchanged.

## Current owner capture HUD (BR4, 2026-10-13)

Annotate and Send to chat are explicit gestures on the active drawn Browser tab.
Remote uses the exact current host receipt; see/source permissions remain unchanged.
Whole-page text is chosen before capture. The still keeps its captured metadata
and text scope while the live stream continues; annotation blocks page input and
navigation. Approval cards and Settings suspend drawing and invalidate pending
work without discarding existing ink.

Send stages one flattened picture and one opaque page-text reference together,
never a chat turn. The selected drawn chat is pinned before asynchronous work.
Stable compose identity follows a later opened thread; closure or replacement
keeps the pair for the next new thread with an explicit notice. Final attachment
capacity is judged atomically, and a rejected pair leaves the still and ink.

Direct capture remains unverified on this host. The real app-mode capability
probe found getDisplayMedia/CropTarget but returned no accepted stream or pixels;
synthetic stream/UI tests are not M6 acceptance. Direct therefore offers
“Open this in Remote to annotate” and converts only on that second owner gesture.
**Since B-DIRECT-CAPTURE (2026-10-19) a Direct capture path is built, gated, not
enabled until the owner's probe passes**; the fallback above is still the default
(next section).

## Direct capture behind the owner's probe (B-DIRECT-CAPTURE, BR-4/M6, 2026-10-19)

Built, gated, not enabled until the owner's probe passes. Plan and reasons:
`docs/plans/2026-10-19-direct-capture-plan.md`.

**The gate.** `browser.json` has one more owner switch, `direct_capture`. It is
**off** by default, fail-closed on a malformed file, and protected like the
others. `PATCH /browser/settings {direct_capture: true}` is refused (409) until
the owner's probe has passed on this daemon. Turning it off always works. The
pass is held in daemon memory only. The probe's JSON report
(`config.DIRECT_CAPTURE_PROBE_PATH`, default
`~/.local/share/jarvis/direct-capture-probe.json`, 0600, outside the Browser's
credential folder) is written for the lead and never read back, so a planted
file unlocks nothing. A switch rather than self-enabling, because every other
Browser widening is an owner switch (B-5, B-12, `local_servers`). The probe in
front of it, because the M6 rule says no Direct capture without the owner's
prompt and pixel evidence.

**The capture** (`hud/src/lib/directCapture.ts`). The switch must be on, and the
press must be trusted: the existing Annotate or Send to chat button only. The
tab must be the active one and drawn, with no card (`blocked`). Options close and
the pane waits for the fresh uncovered receipt, as Remote does.

Then the frame is checked. It must be fully on screen, not under a
hidden/inert/aria-hidden ancestor, and the page must be visible. A hit-test
grid at most 48 CSS px apart (capped near 2000 points), inset 8 px past the
panes' transparent resize handles, may hit only the frame or its own shield.
Overlays with `pointer-events: none` defeat hit testing, so each overlay's own
rect is also tested against the frame's. The overlays checked are dialogs,
menus, listboxes, tooltips, the approval card, the dev-server prompt layer
(`.devserver-prompts`, `devserver-prompt`) and terminal toasts (`.termtoasts`).
Approvals are also covered through `blocked`.

Then the capture itself:
- `CropTarget.fromElement(iframe)`;
- `getDisplayMedia({preferCurrentTab: true, displaySurface: "browser", audio: false, …})`;
- `cropTo`;
- one `ImageCapture.grabFrame()`, at most 3 tries 150 ms apart, each bounded to 3 s, since a track can have no
  frame yet just after the crop.

Coverage is checked before each grab and again at it; both must be clear.
Every track stops in a `finally` on every path. A stream that arrives after the gesture went stale is stopped
the moment it resolves.

The frame is checked once more, before it is used. Coverage and the iframe's rect are read at the grab, before
the tracks stop, because Chrome's own sharing bar can resize the page while it shares:
- it is uncovered;
- **size rule** (`scaleOf`, mirrored in `direct_probe.scale_of`): the frame is the iframe's rect at one uniform
  scale *s* with 0.5 ≤ *s* ≤ `devicePixelRatio`. The long edge sets *s*, give or take 2 px of rounding, and the
  short edge must agree within 1 + short/long + 0.5 px (at most 2.5, since Chrome may round both edges).
  Chrome delivered CSS pixels under a 1.5 DPR (review of PR #276, MED-1)
  and may downscale a large pane at 4K;
- a pane with the window's shape is refused as ambiguous. That is decided from the rect and the window
  alone, before the frame is looked at. A frame with the window's shape at **any** scale is refused as not
  cropped, including an uncropped tab delivered below 0.5 (delta review of #276);
- it is not blank on a 64×64 sample.

Anything else discards it with a sentence and offers Open in Remote. Only then
does `POST /browser/direct-capture` (owner-only) mint a URL-only reference.
The URL is the registry's own record of that exact Direct binding, through the
usual capture guard; it has no title and no text. The daemon refuses it while
the switch is off or a card is pending. The still then joins Remote's
annotation and atomic Send flow unchanged. Nothing focuses the frame, and a
cancelled prompt leaves no still, stream or reference.

**Stated residual.** Direct cannot read a cross-origin page's text, so a Direct
still has no credential check. Remote withholds an image when its page text
holds a credential. The switch's help text and the still's note say so; the
picture reaches an agent only through the owner's own Send. Also, a page that
navigates itself while the prompt is up can show pixels from a different
address than the binding URL attached with it.

**The probe.** It lives in Settings → Browser → Test Direct capture. It runs on
the owner's trusted press and only in the installed app (`display-mode:
standalone`). It crops to a HUD-made test pattern: a sandboxed `srcdoc` frame,
no script, never a web page. The pattern is 7:5, a shape windows rarely have
(in a 7:5 window the probe is refused as ambiguous),
and as wide as Settings allows, from 320 to 1120 CSS px. That way a large capture,
which Chrome may downscale, is measured as well as a small one. It reports facts
only:
- display mode and Chrome major version;
- whether the APIs are present;
- the prompt outcome and the error name;
- track settings;
- whether the crop was applied;
- the pattern's rect, the window size, the DPR and the frame size;
- nine sample positions and the colours found there;
- the live tracks left afterwards;
- step timings.

What `direct_probe.py` checks itself:
- every field against a fixed schema;
- the rect has the pattern's 7:5 shape (±1%) and is at least 160 px wide;
- the size rule above, applied to the reported rect, window, DPR and frame;
- the sample positions against its own nine fractions of the frame;
- each colour is nearer its own pattern colour than any other by a margin of 20.

The pattern colours and sample positions live on the daemon; the HUD sends no
expected values. The report records the scale that matched. What the HUD says
it *measured* is trusted as measurement.

**Evidence so far.** These are not M6:
- Vitest fakes (`directCapture.test.ts`, `BrowserCapture.test.ts`, `DirectCaptureSetting.test.ts`).
- `tests/v2/direct_capture_check.py`.
- `tests/face/hud_v2_direct_capture_check.py`. It runs Chromium's real getDisplayMedia, CropTarget, cropTo and
  ImageCapture against the built HUD in new headless with `--auto-accept-this-tab-capture` and a faked
  standalone media query. The probe passes there and a real Direct frame is staged.

Under Playwright's *emulated* `device_scale_factor` 1.5, Chromium delivered CSS-pixel cropped frames: 1120×800
for a 1120×800 pattern, which records scale 1.0. The first build's DPR-only rule refused these. The face check
now runs at both 1 and 1.5. What a real Windows DPR does is what the owner's probe records. M6, the real Windows
app-mode prompt and pixels, is still the owner's to produce (`docs/plans/2026-10-16-owner-live-checks.md`).

## BR10 sound helper groundwork (historical, superseded; 2026-10-13)

The protected BrowserSettings schema now recognizes `sound`, a strict owner boolean defaulting OFF. `audio.py` contains the isolated WSLg launch composition and bounded owned-Pulse-stream helper: OFF keeps the original argument order and strips ambient PULSE_SERVER, ON removes only the mute flag and uses the fixed WSLg socket after capability checks. Font environment composition is retained by the caller. Process ownership pins Chrome's profile, executable, UID, start time and ancestry; mute uses fresh stream/process checks and fixed reasons, never global volume or unmute. Pulse index attribution remains best effort between its last check and command.

A mute attempt taints that instance before command dispatch, including timeout or a late completion after ON. Only a fresh ON instance clears the taint. A killed utility whose exit has not settled keeps the instance pending until actual cleanup; the bounded caller returning does not release that reservation. Pure helper tests exercise these races with synthetic processes/Pulse replies and disposable utility children. This is implementation groundwork: Chrome launch/restart and HUD wiring are separate pending changes, and this paragraph is not a production sound or physical audibility receipt.
The pure `activity.py` helper adds exact-instance activity and restart capabilities. Parent and child cleanup holds are independent; canceled waiters cannot erase actual coroutine work. Settings changes supersede only uncommitted replacements, current page holds invalidate a pending restart, and stale/forged cleanup cannot free another token. Its methods do no IO or permission work; broker-to-activity is the planned commit lock order. Twelve pure tests plus eight in-memory mutants verify admission/cleanup races. Host, broker and BR9b wiring still require the integrated implementation and independent review; helper tests do not establish that every production operation is covered.
The independent HUD portion adds one window sound-status store, shared by the Browser Settings switch and Direct/Remote speaker buttons. The speaker opens Browser settings without sending page input or changing the preference; its Direct label explicitly leaves Direct audio unchanged. Desired and per-instance applied modes remain distinct, with fixed pending, Private, unavailable and retry wording. Approval/annotation blocking, Settings focus restoration, mounted workspace lifetime and source/see retention use the existing seams. Status is read from `/status.browser`; only an explicit enabled switch writes `sound`. Ambiguous read/save results disable new mutations until an authoritative retry/reconnect read. This UI contract still requires the pending host sound status/restart integration; it is not a claim of physical audio support.


## BR9b Browser action backend (2026-10-13)

`hud_browser_act` is explicit-only and serial in the fast path, with typed click,
type, press, scroll, select, upload, navigate/back/forward operations. Production
`_shipping_enabled(daemon)` reads only BrowserSettings.agents_may_drive, per the
owner's 2026-10-13 DR-0 decision. Missing file/key defaults on; existing bad or
unreadable state fails closed. An unrelated repair never silently enables it.
The owner-only settings PATCH changes it; no alternate environment/CLI gate exists.
Free fixture coverage is separate from real owner interaction and paid model drills.

A private turn scope binds the actual fast caller, original owner words, thread,
turn, project and strict flag. Only a successful presented Remote look publishes
refs, scoped to that exact page/document and caller. Direct/owner captures do not.
Every action needs live desk/depth zero, non-strict uncancelled session, see+drive,
drawn uncovered Remote binding, readable terminal provenance, current refs where
required, attached visible owner socket, no held/paused/depressed input, and five
seconds without owner input. Budgets are 40 judged actions/turn and 200 actions/tab
per rolling hour. The same tab cannot be driven concurrently; cancellation retires
its reservation but holds it through actual in-flight CDP settlement and cleanup.
No navigation creates a local-port lease. Post-input reconciliation pins physical
page, pane/window/source/profile/permission identity; only URL/document generation
may advance. The eight-step fast-path ending reports the pane and asks the owner
to say "go on"; it does not automatically create a task or reset caller scope.

BR9a H1 refusals run before the model. Its stricter static rules/floors remain;
uncertain/sensitive actions ask the owner even if the configured browser review
model says safe. Private cards expire after 120 seconds, cannot create an Always
rule, and accept ALLOW only from the authenticated owner HUD route. Public bus,
transcript and audit retain fixed summaries and digests, never command/typed text,
URL, file path, page labels or model reasons. The owner detail includes the exact
command and selected text plus fixed action/category, bounded host/path/target,
and page/document identity. Current Chrome lacks proven stream capture identity,
so BR9c's separately accepted fallback is Preview unavailable/no target outline.

Full raw bounded facts and credential checks precede truncation. Isolated-world
pins retain the exact DOM node, never field values. Pointer press AND release
hit-test the original dispatch coordinates; an inserted overlay stops the release.
Text chunks recheck focus/type and every event rechecks the gate/fingerprint.
Select assignment, input and change are separate awaited guarded operations.
A handler replacing or sensitizing the control stops later events. Cancellation
waits for each already dispatched event to settle, sends no subsequent event, and
reports truthful partial input; neutral mouse cleanup never manufactures a click.

Uploads supersede the old proposed filesystem private-copy mechanism above.
H8 judges one permitted project file; before review, a bounded stable no-follow
read records identity and a content digest. After yes, path, metadata and digest
must match the exact stable immutable buffer sent to Chrome. There are no new
staging-tree writes or lazy path references. A fixed isolated-world helper receives
at most 1 MiB of base64 per CDP message, uses bounded Uint8Array storage, then
creates a byte-backed File (50 MiB cap). Assignment/input/change are separately
awaited with exact-node, type, enabled, directory and gate checks. A later source
edit/deletion cannot change submission bytes. Old staging-remnant recovery walks
and pins every ancestor with directory descriptors, never follows symlinks, and
only deletes bounded private one-file remnants; unknown/replaced entries remain
and close the service. No content or digest goes to the bus/log. Owner chooser
uploads retain their existing route and behavior.

Agent-caused downloads retain at most eight URL-digest/MIME response entries and
four downloads per action, with no headers or navigation history. Unknown/overflow
or late attribution fails closed. Every such save writes a fixed private hold marker
before file bytes, so an owner GET cannot race classification. A confidently
attributed executable name/magic/MIME gets one private release card (not an agent
`release_download` operation). Approval rechecks exact identity+digest of the
buffer served from a sealed anonymous descriptor; mutable disk cannot change the
response. Release never executes a file. Denial/stop deletes only the exact file;
replacement or cleanup failure stays held. Content and hashes are not persisted.

A conservatively tainted tab stays tainted until closure, even after later owner
input. Thus a later owner download in that tab can remain held. There is no manual
release endpoint for ambiguous/late files: the current limit is owner deletion and
reopening a fresh owner tab for a new download. Untouched owner tabs retain ordinary
behavior. Restart forgets all memory-only release grants, including ordinary agent
files; their persistent marker keeps them held. Files over the 50 MiB release cap
stay held. Owner download/delete UI remains the only applicable existing control.

New SELF_PROTECTED modules: browser/drive.py, drive_state.py, drive_scan.py,
drive_execute.py, drive_files.py, drive_downloads.py (under jarvis/v2/) and
jarvis/v2/tools/hud_browser_act.py, in addition to act_rules.py and act_review.py.
They enforce OFF, caller/turn ownership, credentials, private approval, actual input
and exact-file release. Only the owner may change these gate files; runtime.py and
toolgroups.py retain the owner's existing non-protected decision.


### BR9b full-size upload test resource accommodation

Lead review approved an explicit exception to amendment1 section H for the one
full50MiB upload fixture: summed Chrome sampled-peak and endpoint RSS below1.75GiB,
plus actual entire cgroup memory.peak below1.5GiB. Ordinary Browser fixtures retain
1.5GiB summed RSS; all test trees retain MemoryMax3GiB/MemorySwapMax0. The fixture
refreshes owned Chrome PIDs every20ms and at transfer phases, records baseline,
phase maxima, sampled peak, endpoint and actual cgroup peak, and uses no forced GC.
A sampled peak is not a mathematically exact continuous maximum. The exception
changes neither production driving OFF nor the50MiB file cap.

Unmodified standalone full-cap tests exceeded the original summed-RSS target:
about1559–1581MiB, versus actual complete cgroup peaks below827MiB. Shared pages
are multiply counted by summed RSS. Baseline-only GC and temporary buffer
retirement did not reliably reach the original target; their failures were kept.
Transport now passes constant functions with bounded data arguments, decodes raw
192KiB chunks natively, transfers temporary ArrayBuffers to zero after immutable
Blob ownership, and composes a byte-backed File. Neither production nor the
accepted fixture forces page-global collection to conceal a previous peak.

### BR10 exact-instance activity wiring

The host now admits owner page operations and Remote capture/prepare/execute on their actual event-loop coroutine. A driver parent token spans preparation, review/cards, execution, download review and final notice. An unanswered owner CDP send keeps a separate child token after the short delivery waiter returns. Shutdown does not cancel already-running cleanup a second time. Owner-attention tokens bind the current tab object and instance generation; pending dialogs/choosers, popup adoption and download cleanup cannot be mistaken for idle work. The narrow `page_hold`/`release_page_hold` seam is available for later authentication challenges; no authentication feature is implemented here. Late old-tab callbacks cannot mutate a same-ID replacement's port lease, metadata, stream or download attribution. Only in-process pane metadata includes the instance generation.

`PendingApprovals.run_if_idle` provides the approved broker-lock → activity-lock commitment ordering. The callback may only validate/mark an in-memory token, never perform IO or service callbacks. Four real-broker ordering tests include a card already inserted while its announcement is held. The sound coordinator is installed; its guarded replacement commitment uses this seam (current owner-controls note below). Pure integrated checks and six isolated actual-Chrome lifecycle probes passed; no audio was played or routed by those probes.
### BR10 refused-attention cleanup

The activity cap cannot leave a newly refused dialog or download untracked. A reserved overflow hold (one per exact instance, at most nine) covers a bounded batch of dismiss/cancel/delete cleanup. A failed, timed-out or over-cap batch retires only its exact original instance, checked again after the profile lock. Cancellation retains the hold through the child coroutine's actual finally. An already committed restart owns old-context teardown and admits no second cleanup batch. These paths never accept a dialog, release a download, or change a permission. Seven focused tests cover cap refusal, a newer preference during cleanup, cancellation, stale replacement, queue capacity, pre-start shutdown and committed ownership; four lifecycle mutants are assertion-detected.

## Browser sound owner controls — October 13 reconciliation

This current implementation note supersedes the earlier BR-10 helper behavior that persisted playback intent and opened Settings from the speaker. The merged October 13 sound plan selects a per-profile Chrome instance control. Archived decision payloads above remain unchanged.

The protected sound setting is permission to use the owner speaker, not a playback preference. A missing file/key allows it by default. An existing malformed/unreadable setting fails closed, and an unrelated repair preserves sound OFF unless the owner explicitly changes it. Every ordinary daemon/instance start launches muted without a Pulse route, even if a prior instance had an in-memory grant. Only an explicit owner gesture can request profile sound; the exact committed replacement token and generation can transfer that request into its replacement. Master OFF clears all pending grants, and later master ON does not revive them.

The trusted Remote speaker toggles that memory-only request directly, without opening Settings or forwarding focus/input to the page. Its label discloses that all Remote tabs in the profile share sound, including new tabs opened into the same running instance. Changing sites cannot independently mute a tab with this route. Closing the last profile tab or instance, forgetting/deleting the profile, an owner profile change, and daemon restart clear the grant. A profile change first sends the validated owner OFF action against an exact captured old-pane Remote handle, trying another handle if the first is absent; only all known absent handles (404) permit proceeding without that action. Held/rejected requests remain blocked.

Already playing hidden/covered tabs keep playing and retain a readable profile indicator. Card/annotation, hidden/offline, unknown-state, and busy controls cannot toggle sound. Private ON is refused while Private sessions exist; Private URLs are never replayed to change launch mode. A fresh Private instance remains quiet. Direct pages retain their own page-click policy and receive no iframe autoplay delegation.

Native OFF uses only validated owned Pulse streams and never global volume or another application's stream. It is best effort while an instance is busy; status does not claim immediate physical silence. Native operations settle before cleanup/replacement, and a tainted instance cannot be reused for ON. Replacement commitment uses the broker/activity boundary and exact instance generation. Restoration keeps immutable URL/lease admission, source/see state, and page identity invalidation; it creates no fresh local-network permission. Unsolicited restoration pages and cleanup tasks are bounded by the tab cap even across a waiting new-page allocation. Overflow retires the replacement for context teardown. After OFF or last-tab close, new allocations/adoptions into an audible launch mode are refused until structural mute/replacement completes; muting existing streams alone cannot admit new pages. No replacement stream ticket, socket attach, or pane identity is usable until controlled restoration navigation and the complete replacement finish. Retry is an explicit owner action and obeys the hold.

The existing isolated M3 probe observed headless Chrome routing without Xvfb: its synthetic WAV advanced from 0.061902 to 0.821803 seconds, and one owned Pulse stream was float32le, mono, 44100 Hz, uncorked and unmuted. Muting that exact owned stream succeeded and read back muted. This establishes routing/control evidence; physical speaker audibility and echo behavior have not been confirmed by the owner. The implementation uses the accepted per-profile relaunch route, with no unproven claim that a subscription race is leak-free.

Voice-queue ducking remains pending its consuming voice package. There is no new HTTP/App speaking hook or claimed ducking behavior in this release.

Early verification before independent release review: 63 focused Python checks, a separate 49-check owner-route/dev-server pass, 54 focused HUD unit checks, 19 actual Chromium checks against an isolated mock HUD, and a HUD production build passed. Nine isolated owner-grant/admission guard mutations each produced a failed assertion. The review follow-ups cover bounded allocation floods, partial-restoration admission, owner close, OFF during an awaited allocation, and exact-handle profile fallback. These receipts verify control behavior and guarded replacement, not installation or physical playback.


Final implementation receipts for installed [PR #137](https://github.com/Johnwang688/jarvis/pull/137): runtime freeze 502f62b62c75a125d572228d88e22c61effe1531 passed the production build, 119 HUD unit files / 1,901 tests, and the complete 3,217-check mock HUD sweep (948.64 seconds). The immutable backend sweep completed 124/125 canonical suites in 864.48 seconds. Its sole failure was a stale Browser routes fixture: two unregistered synthetic pages and the old single-event bus allowlist. Independently reviewed fixture-only 3655a15e1a33a0df43dbd82c8c9419bc672d3dc2 then passed the entire 36-test routes suite in 42.504 seconds. The original failure receipt remains preserved; no production guard or privacy canary was weakened. Together these receipts cover all 125 canonical suites, including real Chrome capture/drive/view and synthetic local TTS using existing public model assets.

Speaker fix 5f5219f4783a0137658fb1e1299796e0e7b6140d separately passed 32 real mouse/touch/keyboard/lifecycle/375px checks and two isolated built-bundle assertion mutants. A dismissed options gesture sends no sound or page-input request; the next deliberate gesture works. A pending press cannot survive cards, options, instance replacement or production pagehide/pageshow disconnect/reconnect. This is lifecycle coverage, not physical network-outage measurement. Root and independent UI reviews passed the exact speaker and combined driving integration. Subsequent main integration changed only documentation and the reviewed routes fixture, with jarvis/ and hud/src/ byte-identical to the tested freeze. PR #137 merged at `a857c1f19c8b0cdab393e90a7e039ea7ea81be4a` and is installed/served as build `20261014T004949720546Z-7ff98f434711`. Normal capped guarded rebuild/restart exited with code 0; exact source/build and all three healthy providers were verified. Physical audio/echo remains unmeasured; V15 coordination is still pending.

## BR9c owner driving presentation (current)

Production driving uses live Settings and pane switches. Global off preserves
named pane preference; see-off clears it. Private permission requires an explicit
owner mutation and expires with the pane/context or owner-window lifetime.
The ticketed Remote socket alone supplies the memory-only driver banner and last
20 actions. Displayed chat slots are resolved from the current workspace; hidden
chats use fixed wording. Reattachment needs a fresh paused/list replay. Successful
owner input mirrors takeover, Stop uses the current socket, and Resume stays
paused until daemon confirmation. Cards, annotations, hidden panes and disconnects
block these controls. AO3 adds an inactive Direct tab to a driven destination.

Only the current top HUD-only Browser card fetches its private owner detail. Exact
request metadata, bounded action fields and complete command/argument equality
are checked; details never enter the public approval store or browser storage.
Replacement, unmount and expiry invalidate old replies and consent callbacks.
Deny remains available, Authorize requires deliberate pointer activation, and
Always is absent. Long exact commands scroll without hiding consent controls.
Held downloads expose deletion and explain closing/reopening a fresh owner tab;
there is no manual executable release route.

The installed Chrome155 screencast timestamp is processing time, not proven
capture time. Therefore cards say **Preview unavailable** and no target outline
is drawn. Amendment B.6/B.9 positive pixel identity remains capability-blocked,
not verified by the synthetic stream tests. No screenshot or capture call is
created to manufacture that proof.

**Re-checked 2026-10-20 (B-PREVIEW probe, still blocked).** Probes against an
isolated Chrome 155.0.8059.39, launched directly over a CDP pipe and not with the
product's `launch_args`, found no mechanism that proves frame identity:

- No `HeadlessExperimental` domain with the three flag sets tried (begin-frame
  control, deterministic mode, `--headless=old`).
- Screencast metadata has no frame or commit id. Under 11 ms DOM changes, the DOM
  state at a frame's `timestamp` matched its pixels in 2 of 182 frames (1 of 183
  in an unarchived first run).
- No `Screenshot` trace events in headless.
- `LayerTree.makeSnapshot` refused the one content layer tried.
- The strongest scheme tried was defeated. It froze the page lifecycle, paused
  animations, and bracketed `captureScreenshot` with an isolated-world mutation
  and rect check. The page's timers ran during a capture, and a CSSOM rule toggle
  is not a DOM mutation: 7 of 28 accepted captures were false (24 of 29 in the
  first run).

Not probed: `Emulation.setScriptExecutionDisabled`, `Debugger.pause`, virtual
time policies other than pause, and `DOMSnapshot`.

Only chrome-headless-shell (151) with begin-frame control and paused virtual time
matched (120/120). That mostly shows that pausing stops the page. This route:
- is a different browser;
- needs daemon-driven frames (in the shell, the screencast sent no frames
  otherwise and `captureScreenshot` timed out);
- freezes the page for each card;
- is not proven against cross-site iframes, media or loads.

It is not adopted. Evidence, logs and unblock conditions are in
`docs/plans/2026-10-19-browser-preview-capability.md`.

The named always-ask layer uses a distinct private delegation card with exactly
`{kind: "delegation", tool: "hud_browser_act"}`, empty args and null command. It
permits only reaching subsequent Browser checks, never authorizes a page action.
It shares the same deadline, identity and one-shot no-Always consent lifecycle;
no page identity, target, command or preview is invented for that card.

## Driving release switch semantics (2026-10-13)

DR-0 authorizes the production enable seam and two switches only. All classifier
floors, private cards, five-second quiet rule, refs, desk/depth checks, action and
hour budgets, ownership and between-event guards retain their previous behavior.
Named HUD panes explicitly report their default-on preference and persist only
an opt-out. Private consent is never serialized: only the owner permission PATCH
grants it, bound to window, pane and live context; copied/forged front reports
cannot grant it. See-off clears preference; navigation does not enable it.
The fast prompt is re-narrowed from its pristine composition whenever the desk
toolset changes, preserving the caller suffix while off-desk turns omit action
guidance. Chrome preview capability remains unavailable. Paid classifier accuracy,
real owner switch interaction and installation are separate acceptance evidence.


## Compact owner controls and automatic addresses (2026-10-13)

Browser keeps tabs, history/reload, address, Load and a three-dot **Browser options**
button visible. Profile management, see/drive preferences, Project root, Keep its
origin and capture actions live in the pane-local options panel. Pending/errors,
page status, held downloads and driver/takeover controls remain outside the panel.
Local Direct previews retain disabled history buttons with an explanation; reload
uses the existing guarded owner load.

Owner address submission uses the existing automatic target rule: localhost and
project previews use Direct, external pages use Remote, and Private stays Remote.
Manual mode selection is removed. Direct capture still offers its explicit
Open in Remote fallback without silently capturing or granting a port.

Opening options immediately holds local Remote input and reports this pane as
not drawn to the host. This invalidates old action refs without changing saved
see/drive preferences or transient Private consent. Exact component ownership
prevents a stale unmount from uncovering a replacement pane. Escape restores the
options button; outside clicks close it without forwarding the first Remote page
gesture. Dismissal cancels pointerdown, consuming its compatibility mouse events;
a transparent shield catches Direct iframe presses on the stable HUD frame host.
Cards, hidden panes, Settings and annotations disable options.

Capture pins its destination and cancellation token before closing options,
then waits for the new front receipt. Card, visibility, profile/context, active
tab or pane replacement during that wait cancels the original capture. Capture
controllers stay mounted while only their action controls are portaled; existing
stills and ink survive menu changes. Ending annotation releases the local hold.


## Owner HTTP authentication (B-AUTH, October 13)

Owner-adopted Remote tabs intercept Server Basic/Digest through their exact page
CDP session. The existing SOCKS/URL/control-port gates retain authority. A first
protected address returns `loaded:false, auth_pending:true` and an exact live
allocation receipt, then the real ticketed socket presents a plain owner form.
The actual blank/prior URL remains document identity until commit. Only the
existing explicit address transaction discloses source/adopts a fresh lease;
stored Remote restore creates neither permission. No agent/MCP auth tool exists.

One supervised navigation child keeps actual activity ownership through cleanup.
Normal work retains an accumulated 20-second budget; authentication shares one
original 60-second expiry and at most 3 submissions per explicit operation. One
prompt per tab/eight per host, 128 paused-request records/callbacks, exact origin,
Page/document/instance/socket/hold/visibility identity and bounded reply admission
prevent stale or unbounded work. Authentication admission drops prior page input
and old presses through the existing hold count. Owner answer/cancel stamps takeover.

Credentials stay in transient owner socket fields and one guarded CDP response;
replies clear fields on every controller exit, and no credentials enter replay,
registry, bus, status, logs, config or browser storage. Chrome may retain its
ordinary in-memory HTTP-auth cache. The form never autofocuses and higher cards
invalidate old consent. Reconnect rotates only the nonce, preserving the original
clock/attempts. Auth blocks preparation and between-event driving as owner attention.

Protected navigation during a committed Sound replacement cancels/settles without
a prompt or a usable replacement ticket, retaining existing explicit Retry. This
batch does not hand pending auth through the exclusive Sound restart barrier.
Hidden Direct capture does not adopt owner tabs or install the prompting controller.
Separate installed-Chrome probes delivered same-origin iframe auth. A popup's
first protected request had no OwnerAuth session/prompt within two seconds;
explicit owner Reload returned `auth_pending` and delivered a prompt. A previously
established separate OOPIF target reached the protected fixture without delivering
an owner prompt during the bounded two-second observation. These measured gaps
sent no credentials. Use explicit top-level owner navigation for protected embedded
pages; the popup Reload recovery was measured. Do not infer universal popup/OOPIF
support from top-level Basic/Digest.

Focused backend 46 checks passed. Actual production-host/SOCKS/ticket/socket Basic
proved first pending address, a 22.153-second owner wait followed by one successful
reply, and original 60.002-second expiry; Chrome tree RSS 1054.7 MiB and cleanup passed.
A later 62-check auth/Sound run and nine-case native matrix passed, including
Digest, first allocation cancel, reconnect/old nonce, superseding navigation,
profile close, three wrong attempts, stored restore without a new lease, Private
cleanup and hidden capture exclusion. Six guard baseline assertions passed and
five scratch mutants failed their intended assertions. The WT production HUD
build passed, with 2,092 HUD units plus the remaining 15 syntax asset checks under
a finite fixture allowlist. Focused actual auth HUD passed 14 checks, including
keyboard buttons, approval/Escape priority, strict Settings detach/reattach with a
fresh ticket, cleared typed fields and narrow layout. A Python-only poll had
starved routed socket callbacks; the fixture now pumps sync Playwright without
changing the product. These are synthetic local credentials and ephemeral daemons,
not real accounts, installation or physical-device evidence; final review and the
combined release sweep remain separate.
See [the focused interface plan](../plans/2026-10-13-browser-http-auth.md).


## Native parent Browser driving (BR9d)

Foreground Claude ASK/AUTO and Codex AUTO owner HUD chats now offer explicit
`hud_browser_act` through the same `BrowserDrive.act` gate as the fast path.
Tasks, Discord/scheduled turns, children and strict contexts cannot acquire it.
Native calls retain the live global/pane/Private switches, visible attachment,
owner quiet/input/auth holds, exact guarded look refs, 40 judged actions/turn,
200 actions/tab/hour, private cards and in-flight cleanup ownership. Provider
attribution uses the actual caller; no independent native gate exists.

Only confirmed desk steering changes bounded in-memory owner words. Unknown
Codex steer delivery keeps the existing no-requeue policy but grants no Browser
authority. Off-desk text never becomes owner context. Native completion/Stop and
carrier/session replacement retire the exact DriveTurn and its refs; relay
abandonment cancels only its captured call/socket, preserving parallel calls.

**For the owner:** pane Stop and Resume Jarvis work for both native providers.
After Codex Chat Stop, start a fresh chat: its existing incomplete-accounting
rule refuses continuation in that interrupted native thread. Normal resume of a
completed session and pane Stop/Resume are separate supported operations.

[Native acceptance](../plans/2026-10-13-browser-native-offline-cli.md) distinguishes
actual local CLI/HUD/CDP tests, Claude child catalog omission plus hook tests,
Codex actual child host denial, the raw-wire Codex socket limitation and the
production Chat Stop entry point. Scripted local providers prove transport and
guard composition; subscription authentication, paid-model quality and physical
owner/device behavior remain unmeasured. No installation is claimed by these
fixture receipts.

## Remote pages wider than their pane (2026-10-14)

Headless Chrome hides scrollbars, so a page with a CSS min-width (Spotify's
body is 800 px) was cut off on the right of a narrow pane. `host.py` `_fit`
now lays such a page out at its own width C and keeps the window's pane
W × H at scale S as `stream.viewport`: the page gets C × round(H·C/W) at
S·W/C (`stream.fitted`, read through `stream.page_viewport()`). The aspect
and the frame's device pixels stay the pane's, and the HUD's `placeFrame`
draws it at W/C; `clientToPage` already maps input through that scale. C is
CDP `Page.getLayoutMetrics` `cssContentSize`, so no page code runs. A page
can only make its own picture smaller. Bounds: overflow of 1% or less is
left; C stops at 3 × W, `VIEW_MAX` (both ways) and the device-pixel cap.
Triggers: a window `resize` and every new main-frame document (CDP
`Page.frameNavigated` on the tab's own session, `_new_document`) **reset**
to the pane and measure. A same-document navigation (an SPA's route
change, `pushState`, a hash) is **not** a reset (lead review: each would
cost three or four relayouts and likely a narrow-frame flash per click);
narrowing after one waits for the next resize or real navigation.
Screencast frames measure again (at most every 0.5 s, so an idle page
costs nothing), which catches a min-width set after hydration or an SPA
route. Between resets they only widen. A widening
that the page answers by growing as much (`calc(100vw + 50px)`, Spotify's
12 px past any width) is undone, and widening stops until the next reset,
so nothing creeps or loops. Passes are serialized by `tab.view_lock`, each
reading the newest `stream.viewport`, so the last resize wins. Readers of
page geometry use the fitted viewport: `_view()` (screencast size, the
agent action's centre point) and the owner cursor query's bounds. Look
capture and agent scans read `innerWidth` from the page itself. No setting
and no wire message was added; frame headers already carry the fitted size.
The HUD divides pixel and line wheel deltas by the frame's placement scale
(`normalizeWheel`'s `placeScale`), so a notch moves a fitted page as far on
screen as before; a page-mode notch is already the fitted view's height.
Receipts: `tests/v2/browser_fit_check.py` (real Chrome), `remote.test.ts`
and `hud_v2_remote_check` ("Fit:").

## Superseding an auth prompt stops before it cancels (QA-LOAD, 2026-10-14)

`OwnerAuth.cancel_navigation` used to answer a pending challenge with
`CancelAuth` and only then send `Page.stopLoading`. `CancelAuth` lets Chrome
go on to commit the 401 it was paused on; when the renderer committed it late
(any busy machine), it interrupted the owner's newer navigation, which came
back `{"loaded": false, "error": "navigation failed"}` ("Navigation to
about:blank is interrupted by another navigation to …/superseded"). The lead's
sweeps failed `test_new_navigation_cancels_pending_without_late_resurrection`
five times on that line. The cancel now stops the navigation while it is still
paused on its challenge, then finishes the prompt if the stop has not already
settled it (reason `changed`, as before; no credential is sent either way).

The prompt is superseded from the moment the cancel starts, before it awaits
the stop (up to `_send`'s 2 s): it is marked `answering`, so a `refresh` in
that window (HUD `visible`, `approval_resolved`, an attach) cannot republish
it with a fresh nonce, no reply can answer it, and neither `receipt_current`
nor the host's `auth_pending_current` cache adopts it; `_finish` still sends
its `auth_closed`. Without that (review MED-1) a refresh republished the
prompt as a second answerable card and an answer sent `ProvideCredentials`.

Receipts: `browser_auth_check`'s
`test_a_superseded_prompt_is_never_republished_or_answered_while_it_stops`
holds the fake `Page.stopLoading`, refreshes and answers: one card, no
credentials, no receipt (without the mark: two cards, a receipt and one
`ProvideCredentials`). `browser_auth_matrix_real_check`'s
`test_superseding_a_prompt_stops_its_401_before_it_can_commit` stalls that
test's own renderers (its temp profile's Chrome tree only) for 0.5 s across the
cancel, which makes the late commit happen every time, and checks the window's
`auth_closed` (`changed`) and that no credential request reached the server:
the old order failed 3 of 3, the new one passes.

## Sharp frames and a pointer that keeps up (2026-10-14)

The owner reported soft Remote pages ("like 720p") and lag in pointer moves
and clicks. Both measured on Chrome 155 headless, raw CDP, before any change:

- **The screencast ignores the emulated scale.** `setDeviceMetricsOverride`
  at 1.5 or 2 changes the page's devicePixelRatio, but every screencast
  frame of a 960 × 600 view was 960 × 600: Chrome draws frames at its own
  device scale. The owner's display is 4K at 150% (AppliedDPI 144), so the
  HUD stretched 1× frames by 1.5. Chrome now launches with
  `--force-device-scale-factor=RASTER_SCALE` (1.5, the owner's display), and
  `_sync_screencast`'s maxWidth/maxHeight (the pane's device pixels,
  unchanged) scales each frame to the pane: a 1.5× pane gets 1.5× pixels,
  drawn natively. 2 was tried first and dropped: it pushed
  `hud_browser_view_check`'s 1.5 GiB Chrome bound to 1.62 GB, rastered 78%
  more pixels than the owner's panes use, and its wheel `deltaY` (below) was
  off at 150%. A pane at more than 1.5 (another display, HUD zoom) is
  upscaled from 1.5, which is still sharper than before.
- **What else follows Chrome's own scale** (measured): `fromSurface`
  screenshots (looks, captures) come at RASTER_SCALE, then the edge cap;
  `masked_image` scales each mask by the image's own size, so masks still
  cover their fields; the two mask tests place their probe pixels by that
  scale, one of them near each field's far corner, where an unscaled mask
  would not reach. Layout and input coordinates do not change: a click at CSS
  (600, 400) lands at clientX/Y (600, 400) under every scale. A page no
  window shows keeps devicePixelRatio 1: Playwright emulates its context's
  `viewport` at `deviceScaleFactor || 1`. Chrome divides the emulated scale by its own in
  float32, so a page's devicePixelRatio can read 1.0000000298 for 1 or
  0.62499998 for 0.625 (`browser_fit_check` compares it to 1e-6); a page
  choosing `srcset` candidates may then take the next one up. A wheel scrolls by the delta sent under every scale,
  but the WheelEvent's `deltaY` reads delta × Chrome's scale ÷ the page's
  (it read 400 for 600 at 150% before this change, 600 now by that rule; a 1× window now
  reads 900 for 600), which matters only to pages with their own wheel handlers.
- **The cost**, on a full page of text at 1200 × 800 CSS: press → changed
  frame p50/p95 17/18 ms at 1× against 38/45 ms at 1.5×, 338 against 638 KB
  a frame, nearly all of it JPEG encoding of 2.25× the pixels. Quality stays
  70 (85 cost another 6 ms and 40% bytes). Chrome rasters CSS size × 1.5
  whatever the emulated scale, so a fitted page at VIEW_MAX (4096 × 2304
  CSS, `_fitted`) rasters 6144 × 3456, about 21M px; MAX_DEVICE_PIXELS bounds
  only the frame Chrome sends, and a look of such a page at 1.5× can pass
  `masked_image`'s PIXEL_CAP and be refused (fails closed). Unmeasured. A
  full-screen 4K frame of noisy content nearer FRAME_MAX_BYTES (8 MB) is
  dropped and counted (`frames_dropped`), as any over-size frame was.
  `browser_routes_check` test 04 asserts a 640 × 400 pane at 1.5 receives
  960 × 600 JPEG pixels.
- **Pointer moves arrived faster than they could go out.** Chrome answers
  `Input.dispatchMouseEvent` for a move, a drag or a wheel only at its next
  frame (p50 16.7 ms; a key 0.5 ms), and the pump sends one event at a time
  and waits for each answer, so at most 61 moves a second went out. The
  owner's display refreshes at 160 Hz and the HUD sends every `mousemove`,
  so moves queued (up to INPUT_QUEUE_MAX) and every click and key waited
  behind them. `TabStream.enqueue` now merges a still-queued move into the
  next move with the same button, buttons and modifiers, and a still-queued
  wheel into the next wheel at the same point with the same modifiers
  (deltas summed, within WHEEL_MAX); `merged` counts them, and a full queue
  still takes an event that merges into its tail. Only an untaken event of
  the same attachment is merged, so a page that keeps up sees every event, a
  press, release, key or text is never merged or crossed, and the hold's
  `_drop_through` still drops a merged event (every place that raises it
  clears the queue). A drag on a page that falls behind reaches it as fewer
  points (CDP has no coalesced-events list), accepted.
  `browser_input_merge_check` covers each rule; five mutants (no type,
  buttons, point, attachment or bound check) each fail it, and the cap rule's
  test fails against the version that checked the cap before merging.

The Direct tab no longer shows "the Browser only loads local servers" while
the owner's typed external address opens in Remote: `DirectTab.ownerGo`
marks the address `opening` until its own submit settles and shows
"Opening…" instead; a failed submit shows its own error as before, and only
the submit that set the mark clears it.

## Remote input to picture, stage by stage (B-PERF, 2026-10-15)

The owner found PR #186 "noticeably better, usable" with some delay left.
**`scripts/browser_measure.py`** (manual, never in a suite) measures the
whole path: a port-0 daemon with its real Browser built as
`browser_routes_check.BrowserStream` builds it (temp HOME, fixture pages on
an ephemeral loopback port, the gate resolving nothing), and a headless
Playwright Chromium at DPR 1.5 playing the HUD: a page the daemon's own HUD
listener serves from a temp `HUD_DIST`, running the pane's real
`FramePipe`, `decodeFrame` and `paintFrame` (`remote.ts`; extracted from
RemoteView for this, unchanged in behaviour). Each stage is clocked: HUD
send → daemon reader → pump → CDP answered → page handler → Chrome's frame
timestamp → daemon `_frame` → offered → sent after the window's ack →
written → HUD receive → decode → draw → ack. The fixture pages draw a 16-bit
counter of handled events as blocks (the scroll page draws each row's
number), and the HUD reads it from each frame after acking it, so "the first
frame that shows the input" is read from the picture, never guessed from
timing. End to end is HUD clock only; the stage split lines up three
real-time clocks on one machine (offsets by round trip: under 0.7 ms).

Conditions: Chrome 155 headless (chrome://gpu: compositing, raster and
canvas "Software only"), RASTER_SCALE 1.5, quality 70; the HUD stand-in has
no GPU either; 24-core WSL2, load average 3–9 from other work. A typical
pane, 1200 × 800 CSS (1800 × 1200 frame pixels, ~430 KB), and a full-screen
4K pane at 150%, 2540 × 1340 (3810 × 2010, ~1.5 MB). Typing: a key every
125 ms into a page of text (and every 400 ms); hover: moves at 160 Hz over a
9000-cell grid with `:hover` styles; wheel: a 96 px notch every 250 ms;
scroll: 24 px at 120 Hz. p50/p95 ms, before this change (ack after draw):

| stage | typing pane | typing 4K | hover pane | hover 4K | wheel pane | wheel 4K |
|---|---|---|---|---|---|---|
| **send → frame drawn** | **44/47** | **161/181** | **90/106** | **292/384** | **53/66** | **124/147** |
| send → daemon | 0/0 | 0/2 | 5/17 | 19/44 | 0/0 | 0/1 |
| queued behind input | 0/0 | 0/0 | 3/8 | 6/14 | 0/0 | 0/0 |
| CDP answer | 2/3 | 2/11 | 17/23 | 47/61 | 2/3 | 2/3 |
| handler → Chrome frame | 13/14 | 44/52 | 32/36 | 94/142 | † | † |
| frame → daemon (encode + base64 transport) | 17/18 | 64/73 | 17/20 | 62/86 | 20/23 | 47/56 |
| waiting for the window's ack | 0/0 | 0/0 | 0/2 | 15/46 | 0/0 | 0/0 |
| socket write + to the HUD | 2/3 | 7/10 | 2/6 | 17/22 | 3/4 | 5/7 |
| HUD decode (createImageBitmap) | 10/10 | 40/52 | 11/13 | 37/48 | 12/16 | 30/37 |
| draw | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 |

† A wheel scrolls on the compositor before the page's (passive) handler
runs, so that split is negative; the wheel's handler waits for the frame
(18 ms pane, 43 ms 4K). Base64 decode and the daemon's two frame copies
are under 2 ms; scroll streams ran 54 fps (pane) and 19 fps (4K).

**Where it goes**: the picture costs what its pixels cost, three times, all
in software: Chrome's frame (the next BeginFrame, raster, software
composite, readback: 13 ms pane, 44 ms 4K), the JPEG encode and its base64
through CDP (17 / 64 ms; a 2 MB string takes 38–41 ms through Playwright,
30 ms over a raw CDP pipe, so the transport is about 60% of it and Node
about a quarter of that), and the HUD's decode (10 / 40 ms). A pane at
4K is 3.5× the pixels and 3.5× the time. A move waits for Chrome's next
frame before the pump sends the next (the CDP answer, 17 ms), which
`TabStream` already merges behind (PR #186).

**Changed: the HUD acks a frame when its decode starts** (`FramePipe`; it
acked after the draw). The daemon's next frame now crosses the socket while
this one decodes. Bounds kept: the daemon still sends one frame per ack
(one in flight, FRAME_MAX_BYTES, nothing to hidden tabs, all unchanged),
and the HUD holds one frame decoding and one waiting, the waiting one
replaced by newer arrivals and acked only when its own decode starts. A
continuous stream at 4K no longer waits for the ack (p50 15–20 → 0 ms):
hover 4K 292/384 → 259/286 in the paired run; three interleaved repeats,
draw 294, 308, 328 against decode 283, 277 (one more repeat ran during a
load spike, 449, and is not counted). Scroll 19.0, 18.2, 16.7 fps → 20.0,
19.8, 20.0 at 4K, and 51.8, 52.3, 50.2 → 54.7, 53.4, 53.4 at the pane.
Single inputs (typing, wheel notches) never waited for an ack and are
unchanged within noise (typing pane 43–49 both ways). The decode stage reads
longer at 4K (37 → 43 ms) because a receive now overlaps it on the same
CPUs. `remote.test.ts` pins the ack at decode start and the one-decoding,
one-waiting bound; three of its tests fail against the old ack.

**Tried and rejected, measured:**

- *Adaptive JPEG quality* (50 while the owner's input moves the page, 70
  again after 150 ms quiet). Chrome 155 refuses a second
  `Page.startScreencast` ("Screencast is already active"), so each switch
  is a stop and a start, and a start draws one frame of its own. Switching
  before the input that starts a burst delayed that input's own frame:
  typing every 400 ms, pane 44 → 74 ms (149 in a second run), wheel notches
  53 → 72–80 ms. Switching after a burst's first frame instead: wheel pane
  49 → 68, slow typing 4K 144 → 197; only steady typing at the pane gained
  (44 → 40). Quality 50 throughout cuts a frame's bytes by 23%, but two
  repeats at 4K under load showed no latency gain (hover 315, 341 at 70
  against 333, 326 at 50). A `Page.captureScreenshot` at rest as the sharp
  frame does not work either: it makes Chrome emit a screencast frame of its
  own about 50 ms later, which would replace it. Quality stays 70.
- *Ack on receipt*: as fast as decode-start, but frames the HUD never
  draws still cross the socket; decode-start leaves them in the daemon's
  one waiting slot.
- *Decode*: `colorSpaceConversion`/`premultiplyAlpha: "none"` is slower
  (23.5 vs 16 ms pane, 65 vs 45 ms 4K); dropping `bytes.slice()` changes
  nothing measurable; `ImageDecoder` (12.4 / 48 against 11.8 / 38 ms) and
  `<img>.decode()` (29 / 88 against 16 / 45 ms) are slower than
  `createImageBitmap`.
- *Chrome flags* (security flags untouched): `--num-raster-threads=8`
  is worse (hover 4K 348 → 550 ms); `--enable-gpu-rasterization` does
  nothing without a GPU; `--disable-frame-rate-limit` is worse (typing pane
  45 → 56 ms, scroll pane 52 → 27 fps), and so is `--disable-gpu-vsync`
  (typing pane 45 → 54 ms).
- *Playwright's pipe reader* joins 32 KiB chunks one by one; a linear reader
  measured the same (2 MB: 40–42 against 38–41 ms). Talking to Chrome
  without Playwright saves about a quarter of the transport, but Playwright
  holds the only debugging pipe, and a debugging port would let any local
  process drive the owner's Browser.
- `everyNthFrame` is already 1.

**What would cut it further** (owner decisions, not done): fewer pixels
while the page moves (frames at 1× during input would divide 4K's encode,
transport and decode by 2.25, at the cost of a soft picture while typing,
which is what the owner reported before PR #186); a video stream (tab
capture and WebRTC: inter-frame coding, hardware decode in the HUD) instead
of a JPEG per frame; a GPU for Chrome, which headless WSL2 does not give.

**Measuring it on Windows (the missing M2 number).** Serve the fixture in
WSL, `python3 -m http.server 8765 --bind 127.0.0.1 --directory
scripts/browser_measure`, and type `http://localhost:8765/flip.html` into a
Remote pane (each key press flips the whole page's colour; no caret).
Open DevTools on the HUD window (Ctrl+Shift+I), paste
`scripts/browser_measure/owner_snippet.js`, click into the pane and press
about 100 keys, two or three a second, then run `__remoteLatency()`. It
reports key (the OS event's timestamp) → frame drawn, and → the HUD's next
frame, p50/p95; the display adds a refresh or two (6–12 ms at 160 Hz). It
only listens, and a reload removes it (checked on a scripted page: 10 of
10 keys matched to their frames).

Receipts: `remote.test.ts` (FramePipe, 53 tests), the Browser suites
(`browser_routes_check` 36, `browser_fit_check` 5,
`browser_input_merge_check` 12, `hud_browser_view_check` 33,
`browser_capture_chrome_check` 5) and the HUD face suites that drive
Remote, all passing. The raw runs are not kept; the script reproduces them
(`--ack draw` emulates the old ack; `--quality` and `--chrome-flag` were the
knobs for the rest).

## Chrome tile-size sweep (B-PERF, 2026-10-20)

The last open B-PERF measurement: does Chrome's raster tile size change
input-to-picture latency, frame encode time or frame size at `RASTER_SCALE`
1.5? **No. No tile configuration beats the default outside run-to-run noise,
and none changes frame bytes or memory. The launch flags stay as they are;
nothing is recommended.** Scripts and raw runs are kept outside the repo, in
`~/.cache/jarvis-lead/tile-sweep/` (`verify_tiles.py`, `sweep.py`,
`analyze.py`, `results.jsonl`, `confirm.jsonl`, `sweep.log`, `analysis.md`,
`v_*.json`); `sweep.py` drives `scripts/browser_measure.py` as a library and
adds only a `flip` fixture and raw-sample capture.

**Which flags Chrome 155 honours** (155.0.8059.39, headless, software raster,
`--force-device-scale-factor=1.5`; checked by a `cc` + `disabled-by-default-cc.debug`
trace over twelve wheel notches and six key flips on a 500-paragraph page,
reading the tile `content_rect`s the compositor snapshots, not by whether
Chrome launched):

| flag | tile sizes in the trace | raster tasks in the trace |
|---|---|---|
| none (default) | 256 × 256 | 1344 |
| `--default-tile-width/height=256` | 256 × 256 (same as the default) | 1344 |
| `=128` | 128 × 128 | 4830 |
| `=512` | 512 × 512 | 344 |
| `=1024` | 1024 × 1024 | 92 |
| `=2048` | **clamped** to 1856 × 512 | 86 |
| `width=1024 height=256` | 1024 × 256 | 354 |
| `--max-untiled-layer-width/height=2048` or `=64` | unchanged (256 × 256; not tried on a small layer) | 1340 / 1344 |
| `--enable-gpu-rasterization` with `=512` | 512 × 512 (no GPU, so nothing else changes) | 344 |

So `--default-tile-width` and `--default-tile-height` work (the software
default is 256 × 256) and Chrome clamps what it will not tile; the
`--max-untiled-layer-*` pair changed nothing observable. Raster task counts
are one trace each (the page scrolls on a timer), so read them as the order
of magnitude: a larger tile means fewer, bigger tasks, not less raster work.

**The sweep.** Seven configurations, four interleaved rounds (the order
rotated each round and reversed on odd ones, so no configuration always ran
first or last), a fresh daemon and Chrome per configuration and round, the
real `scripts/browser_measure.py` pipeline (JPEG quality 70, ack at decode
start). `default` and `t256` are the same tiles, so their difference is the
noise floor. The machine was idle for every run: `sweep.py` waited for
load1 below 1.5 and CPU busy below 10% (it waited 9 times, up to 2.3);
load1 at each run's start was 0.56–1.43 (median 0.96); the only other work
was the trading firm's idle loop and the daemon at 2%. Chrome's summed
user + system time and PSS are read from `/proc`. Samples: 200 key presses
per configuration for the text page (50 × 4) and 160 for the flip page
(40 × 4), 2560 moves for hover, 4 × 4 s of scroll. p50 / p95 ms, pooled over
the four rounds:

| scenario (what moves) | config | press → frame drawn | Chrome frame (handler → capture) | encode + base64 → daemon | frame KB p50 |
|---|---|---|---|---|---|
| text page, typing every 125 ms, pane (1800 × 1200 px) | default | 43.7 / 55.1 | 12.7 / 15.2 | 16.3 / 19.4 | 428 |
| | t256 (control) | 42.4 / 44.7 | 12.1 / 13.7 | 15.8 / 16.7 | 428 |
| | t128 | 43.8 / 50.9 | 13.1 / 15.6 | 16.0 / 18.9 | 428 |
| | t512 | 42.5 / 47.5 | 12.2 / 14.5 | 15.7 / 17.6 | 428 |
| | t1024 | 42.2 / 45.4 | 12.0 / 13.6 | 15.7 / 16.8 | 428 |
| | 1024 × 256 | 42.7 / 50.6 | 12.3 / 14.7 | 15.8 / 18.1 | 428 |
| | t2048 (1856 × 512) | 42.7 / 45.2 | 12.1 / 13.5 | 15.9 / 16.8 | 428 |
| flip page, a key every 400 ms, pane | default | 31.0 / 37.9 | 13.5 / 17.3 | 8.5 / 9.4 | 14 |
| | t256 (control) | 30.8 / 41.5 | 13.5 / 18.4 | 8.5 / 9.6 | 14 |
| | t128 | 31.3 / 35.4 | 13.8 / 16.8 | 8.5 / 9.2 | 14 |
| | t512 | 31.2 / 39.3 | 13.9 / 18.5 | 8.4 / 9.5 | 14 |
| | t1024 | 31.2 / 39.0 | 13.9 / 18.1 | 8.5 / 9.7 | 14 |
| | 1024 × 256 | 31.0 / 37.1 | 13.6 / 17.9 | 8.4 / 9.1 | 14 |
| | t2048 | 31.9 / 40.1 | 14.3 / 17.7 | 8.6 / 9.4 | 14 |
| hover grid, 160 Hz, pane | default | 78.6 / 88.9 | 28.2 / 30.7 | 14.0 / 16.5 | 351 |
| | t256 (control) | 79.2 / 88.9 | 28.3 / 31.0 | 14.2 / 16.7 | 351 |
| | t128 | 80.2 / 90.2 | 29.0 / 32.3 | 14.3 / 16.8 | 351 |
| | t512 | 79.3 / 89.4 | 28.4 / 31.1 | 14.2 / 16.8 | 351 |
| | t1024 | 78.6 / 88.3 | 28.1 / 30.7 | 14.0 / 16.3 | 351 |
| | 1024 × 256 | 79.0 / 88.7 | 28.3 / 31.0 | 14.0 / 16.7 | 351 |
| | t2048 | 78.8 / 88.8 | 28.0 / 31.0 | 14.1 / 17.2 | 351 |
| text page, typing every 125 ms, 4K pane (3810 × 2010 px) | default | 137.7 / 144.0 | 37.0 / 38.7 | 55.4 / 59.6 | 1492 |
| | t256 (control) | 138.6 / 147.3 | 37.3 / 39.7 | 55.6 / 60.2 | 1492 |
| | t128 | 140.5 / 149.7 | 39.5 / 42.2 | 55.9 / 61.4 | 1492 |
| | t512 | 137.8 / 143.7 | 37.2 / 38.8 | 55.4 / 59.1 | 1492 |
| | t1024 | 137.2 / 144.3 | 36.8 / 38.8 | 55.4 / 59.6 | 1492 |
| | 1024 × 256 | 138.3 / 145.7 | 37.2 / 39.3 | 55.7 / 60.4 | 1492 |
| | t2048 | 136.7 / 144.5 | 36.0 / 37.4 | 55.3 / 59.1 | 1492 |
| flip page, a key every 400 ms, 4K pane | default | 81.8 / 93.0 | 34.8 / 41.9 | 25.5 / 28.5 | 46 |
| | t256 (control) | 84.3 / 94.0 | 36.7 / 42.0 | 26.2 / 28.7 | 46 |
| | t128 | 82.7 / 93.2 | 35.5 / 44.6 | 25.6 / 28.1 | 46 |
| | t512 | 83.4 / 86.2 | 37.0 / 38.5 | 25.4 / 26.7 | 46 |
| | t1024 | 83.2 / 86.0 | 36.5 / 37.5 | 25.5 / 26.6 | 46 |
| | 1024 × 256 | 83.1 / 90.8 | 36.2 / 39.9 | 25.7 / 27.3 | 46 |
| | t2048 | 84.6 / 97.5 | 37.7 / 46.1 | 25.8 / 28.0 | 46 |

Scroll at 120 Hz, pane: every configuration drew 57–60 fps (default 59.9,
t512 60.0, t2048 59.9, t128 56.9, t1024 58.6, 1024 × 256 59.0, control 58.2),
the frame at 491 KB p50 in all, encode + base64 18.6–19.1 ms p50 (the
default's p95 21.5 ms against 21.3–25.7). The 1–3 fps spread is the same
size as the default-against-control gap in other rows.

**Reading it.** Frame bytes are identical in every configuration (the JPEG
depends on the pixels, not on how they were rastered). Where the tile size
could matter, nothing moves beyond noise: between `default` and `t256`,
which are the same tiles, press → frame differs by up to 1.3 ms p50 (text
pane) and 2.5 ms (flip 4K), and p95 by up to 10 ms (text pane, where one
round of `default` had a few slow keys); the round-by-round p50s of each
configuration overlap. **The only difference outside that noise is that
128 × 128 is a little worse**: text 4K 140.5 against 137.7 ms (p50 of every
round 140–141 against 137–138, Chrome's frame 39.5 against 37.0 ms), and
hover pane 80.2 against 78.6–79.3 ms. More, smaller tiles mean more per-tile
work for the same pixels; the large tiles buy none of it back, because a
software raster costs what its pixels cost.

A text-page confirmation (`confirm.jsonl`): default, 256, 512 and 1024, eight
interleaved rounds, 400 key presses each, since the first sweep's `default`
text row sat 1.3 ms above the control. Press → frame p50 / p95: default
42.6 / 45.2, t256 42.7 / 45.2, t512 42.9 / 46.3, t1024 43.1 / 52.7 ms; Chrome
CPU 2.81–2.83 s in each. That earlier gap was noise.

Chrome's CPU time over a scenario is within about 9% across configurations in every row, with the default and the control among the extremes
(text pane 2.77–3.01 s, hover 7.2–7.6 s, scroll 8.9–9.2 s, text 4K 5.8–6.2 s).
PSS summed over Chrome's processes showed no tile-size trend (text pane
346–360 MB, hover 300–301 MB; at 4K it split into two groups, 341–399 MB, by
run rather than by configuration, which looks like which helper processes
were alive at the read). Peak working-set differences between tile sizes
were not separately measured.

**Result.** The default (256 × 256 in software raster) is as fast as any
other size tried and cheaper than 128 × 128. Nothing here changes
`launch_args`, and no tile flag is recommended. That closes the B-PERF tile
question; a bigger change has to come from the levers already listed (frames
at 1× while the pointer moves, a video stream, a GPU for Chrome).

## Password store: the owner's keyring (B-LOGIN, M12, 2026-10-15)

The owner asked for synced Google passwords to stop sitting under Chrome's
`basic` store, whose key is public. `jarvis/v2/browser/password_store.py`
chooses each launch's `--password-store`; the lead decided the rules.

- **Private** stays `basic` and never gets the session bus.
- **A named profile** gets `--password-store=gnome-libsecret` and the daemon's
  `DBUS_SESSION_BUS_ADDRESS` only when the keyring is **unlocked**. The
  address must be `unix:path=` naming a socket directly in `/run/user/<uid>/`,
  owned by us, in a 0700 folder we own. The state is read before the launch;
  after it, **the store Chrome actually used is observed** (below), because
  Chrome falls back to `basic` silently when it cannot reach its key. A
  keyring launch that Chrome did not honour closes that Chrome and refuses.
  With the bus, `HardwareMediaKeyHandling` joins the one `--disable-features`
  switch, so Chrome publishes no MPRIS players.
- **locked**, **no_keyring** (the Secret Service answers, but has no default
  collection) or **error** (an answer the probe does not recognise) refuse the
  launch with 409: "Unlock the keyring in Windows Terminal (not a Jarvis
  terminal): <command>". Jarvis never prompts, never unlocks, never downgrades.
- **unavailable** (no Secret Service, no usable bus) launches `basic`, and
  `/status` and the HUD say "Unprotected (basic store)" with the install hint.
- **Sticky**: a keyring launch records it twice *before* Chrome starts: a
  marker file, `profiles/<id>/.jarvis-password-store` (0600, inside the
  profile's own folder), then `"password_store": "keyring"` in that
  profile's `profiles.json` row. **Sticky is the row flag or the marker**
  (anything at the marker's name, or a name that cannot be read, counts);
  clearing either alone does not un-stick the profile, and its next launch
  writes the missing one back. Such a profile is never launched with `basic`
  again; its newer data would be unreadable there. "Sign out of everything"
  wipes the folder and writes the marker again; only deleting the profile
  removes both.
- **Owner override**: `"password_store": "basic"` in `browser.json` (only that
  exact word; hand-edited, no route writes it) launches `basic` without asking
  the bus, loudly unprotected, except for a sticky profile.

The probe is one `gdbus call` of `Properties.Get` for `Locked` on
`/org/freedesktop/secrets/aliases/default`, with a four-variable environment
and a timeout. It never searches items, opens a session or reads a secret.
`/status` reads a cached state at most every 5 s from a background thread,
so it never blocks. **A `Keyring()` is detached**: it reports `unavailable`,
runs nothing and gives Chrome no bus. Only daemon2's `main()` attaches it, as
it attaches `browser.json`, so no test or embedded daemon reaches the owner's
keyring. The probe's runner is `subprocess.run` looked up at each call, so
the suite's patch of it guards every probe (it used to be bound at import).

**`unavailable` effectively never happens on this machine.** The probe names
`org.freedesktop.secrets`, so the session bus **starts gnome-keyring for it**
(D-Bus activation) when it is not running. After every WSL boot that daemon
starts **locked**: the probe reads `locked`, and every named profile is
refused with the unlock command until the owner unlocks the keyring in
Windows Terminal. Private still opens. `unavailable` (basic, loudly) is only
seen without gnome-keyring installed or without a usable session bus.

**The observation (follow-up review of #215, MED 1).** The keyring reading
`unlocked` proves the keyring was unlocked, not that Chrome used it. So after
each named launch the daemon has Chrome write one probe cookie (a fresh
random name, value `1`, on `password-store.jarvis.invalid`, a name that never
resolves) and reads back **only the first three bytes** of that row's
`encrypted_value` from the profile's `Default/Cookies` database, opened
read-only and immutable (`password_store.observed_store`; the database must
be a plain file, not a link; the row is matched by `host_key` and name). `v11` is the keyring's key, `v10` the basic
store's; anything else, or no row within 5 s, is `unverified`. Then the probe
is expired again. Never a value, never another row.

- **Why a cookie, measured on Chrome 155.** `Local State` → `os_crypt` holds
  only the portal provider's `prev_init_success`, nothing about the key
  Chrome encrypts with. Chrome's `OSCrypt.*` histograms (over CDP's
  `Browser.getHistogram`) say whether `FreedesktopSecretKeyProvider`
  initialised, but they are an indirect statistic and were not yet recorded
  when the first CDP call arrived. The cookie's prefix is what Chrome
  actually wrote with.
- **How the row reaches the disk at once.** Chrome commits cookie changes on
  a 30 s timer (measured: 30.06 s for one cookie) or as soon as 512 changes
  are pending. So the probe is followed by one filler cookie set 300 times
  with different values (each replacement is a delete and an add: 600
  changes), and the probe's row is on disk in 0.02 s (basic) to 0.09 s
  (keyring). Both are expired straight after the read; a session cookie
  would never be written at all. This leans on two Chrome internals, the
  512-change batch and a database that is not in WAL mode (an immutable read
  does not see a WAL file). **A Chrome update that moves Cookies to WAL or
  changes the commit batch makes every keyring launch `unverified`**: every
  sticky profile is then refused (Private and non-sticky basic launches still
  open) until this code is updated. The 409 and the HUD say "Jarvis couldn't
  confirm Chrome's password store (possibly after a Chrome update)", never
  the install hint. Re-measure on a Chrome update.
- **On a mismatch.** A keyring launch is always sticky by then (it was
  recorded before Chrome started), so it is closed, its processes stopped,
  and the launch refused with 409: "The keyring locked while Chrome was
  starting" when the keyring now reads locked, "Chrome started without the
  keyring (it wrote with the basic store)" for `v10`, "Jarvis couldn't
  confirm Chrome's password store (possibly after a Chrome update)" for
  `unverified`. **The check comes after Chrome started**: a keyring Chrome
  that silently fell back runs with `basic` for about 1–10 s (its start, the
  probe, the close) until it is refused and closed, and anything it writes in
  that window (its own startup writes; no page is loaded yet) is `v10`. A basic launch (no
  keyring, or the owner's override) is reported as observed, always
  unprotected. Private is not observed: it is `basic` by its flag and has no
  bus.
- **`libsecret-1.so.0` is not a launch requirement.** The review asked for
  it, on the premise that Chrome falls back when it cannot load it. Chrome
  155 does not load it at all: the binary names no `libsecret-1.so`
  (its dlopen list has `libdbus-1.so.3`, linked directly), its keyring
  provider is `components/os_crypt/async/browser/freedesktop_secret_key_provider.cc`,
  which speaks the Secret Service over D-Bus itself (`OpenSession`,
  `GetSecrets`), and in the live run below the keyring Chrome's process had
  `libdbus-1` mapped and no `libsecret`. A requirement for it would refuse
  launches for a library nothing uses; the observation catches any fallback
  whatever its cause.

`/status` → `browser.sync`: `password_store` is the store the running named
instances were **observed** to use (`gnome-libsecret`, `basic`,
`unverified`, `mixed`, or null with none running), `source: "observed"`.
Each instance row carries its own `password_store` too. Also reported:
`requested` (`keyring` or `basic`), `keyring` (`unavailable`, `no_keyring`,
`locked`, `unlocked`, `error`, or `unknown` before the first read),
`next_launch` for a new profile (`gnome-libsecret`, `basic`, `refused`, or
`checking` while the state is `unknown` — never `refused` for that), and
`unprotected` and `hint`. **`profiles`** gives each named profile's own
state: `store` (observed, or null when not running), `sticky`,
`next_launch`, `unprotected` and `hint`, so a sticky profile under
`browser.json`'s `basic` says to remove that override, not to unlock the
keyring. The HUD's profile **Manage** panel shows the selected profile's
line (the global one when `/status` has none), red when the store is
unprotected or a launch would be refused, and reads `/status` again every
3 s while it is open and when the window regains focus, so an unlock in
Windows Terminal shows within seconds.

**What it protects, stated plainly.** The keyring protects passwords *at
rest*: WSL shut down, the files read from Windows, a backup, a copied profile
folder. **It does not protect against a program running as the owner while
the keyring is unlocked**, which Chrome needs: gnome-keyring has no
per-application rule, so such a program can ask the Secret Service for
Chrome's key. That makes the read a distinct D-Bus request instead of a file
read. **Agents' shell calls to it are refused at the gate** (follow-up,
2026-10-15; `rules.secret_service_reading`, the safety notes' *Agent calls
to the Secret Service*): `secret-tool` and its kin or Python's `keyring` CLI
as a program, a D-Bus client, interpreter or shell whose own arguments name
the Secret Service, and a session-bus monitor are DENY in v1's gate and v2's
layer 1 alike; a line that only mentions one is not. That
is an accident guard, not a wall: a script file that talks D-Bus, or a call
by unique name with a computed path, is not seen. A same-user program could
also claim `org.freedesktop.secrets` itself while gnome-keyring is not
running; that is the same same-user boundary.

**Live check, 2026-10-15** (after the owner installed gnome-keyring 50 and
unlocked it; temporary BROWSER_DIR, a throwaway profile, a test cookie, no
secret read; `busctl` confirmed the keyring still unlocked afterward):

- **Production path, real bus.** Chrome 155 ran with
  `--password-store=gnome-libsecret` and the bus in its environment. A
  `Chrome Safe Storage` item appeared in the `login` collection (found by
  `SearchItems` on `application=chrome`, then reading its label). The test
  cookie was stored as `v11`. The `profiles.json` row turned sticky. `/status`
  said `gnome-libsecret`, `unlocked`, not unprotected. `Local State` → `os_crypt`
  held only a `portal` key. The item was deleted afterward, and none had existed
  before the run.
- **Unreachable bus** (a bogus `unix:path=` in `/run/user/<uid>/`): the sticky
  profile was refused with 409 and the unlock command, and no Chrome started.
  A new, non-sticky profile opened with `--password-store=basic`, no bus, a
  `v10` cookie and `/status` `unprotected: true` with the install hint.
- **Chrome's own fallback, measured.** A raw Chrome given
  `--password-store=gnome-libsecret` with the unreachable bus started in 0.3 s
  without hanging, and wrote the cookie as `v10`. That is the silent downgrade,
  which is why Jarvis checks the keyring before and after each launch.
- The real Secret Service answers a missing collection with
  `org.freedesktop.DBus.Error.UnknownMethod` ("Object does not exist at
  path"), which the probe reads as `no_keyring`. The locked path is
  covered by fakes only; the owner's keyring was not locked.

**Live check of the follow-up, 2026-10-15** (Chrome 155.0.8059.39; the
owner's keyring held no Chrome item before or after and stayed unlocked,
checked by `SearchItems` and `Locked` only). The keyring Chrome used was a
**throwaway Secret Service**: a private `dbus-daemon` with no service
activation and a `gnome-keyring-daemon` whose HOME, XDG folders and runtime
folder were all temporary, unlocked with a throwaway password, deleted
afterward. Nothing ran against the owner's bus or keyring.

- **Raw Chrome, observation alone.** Unlocked: the probe read `v11` in
  0.09 s and Chrome created its `Chrome Safe Storage` item in the throwaway
  keyring. Then that collection was locked: Chrome started in 0.16 s,
  prompted nothing, `OSCrypt.FreedesktopSecretKeyProvider.InitStatus` was
  10 and `OSCrypt.EncryptorKeyCount.Available` 1, and the probe read **`v10`**
  — a locked keyring is a silent downgrade too. An unreachable bus: `v10`,
  `InitStatus` 6. One probe cookie without the filler reached the disk after
  30.06 s; with it, 0.02 s.
- **The real `BrowserHost`, temporary BROWSER_DIR.** Unlocked: Main opened
  with `--password-store=gnome-libsecret`, observed `gnome-libsecret`, the
  marker and the row written, `/status` `source: "observed"`, the profile's
  `store: gnome-libsecret`, `sticky: true`; the Chrome process had
  `libdbus-1` mapped and no `libsecret`. Locked: Main was refused before
  Chrome started (the pre-check). With the pre-check made to read
  `unlocked` (as if the lock came during the launch, or Chrome's own call
  failed), Chrome fell back, the observation read `basic`, and the launch was
  refused with "Chrome started without the keyring", no Chrome left
  running; a new profile went the same way and stayed sticky. With the real
  after-launch read also locked, the message was "The keyring locked while
  Chrome was starting".

**Migration (`v10`/`v11`): new writes are measured above; whether old `v10` data stays readable is still for M12.** Chrome on Linux tags
`basic` data `v10` (a hard-coded key) and keyring data `v11`. After the
switch, existing `v10` passwords and cookies stay readable but are **not
re-encrypted**: they remain on disk under the public key. Only new writes are
`v11`. The reverse (keyring to basic) is what loses data, which is why
profiles are sticky. Advice given to the owner: hold password sync until
the keyring is live; if passwords already synced under `basic`, delete
that profile from the HUD, then delete the trashed copy, create a new one
and sign in again so everything lands as `v11`.

**Owner acceptance steps** (not done; they need the packages and the owner):

1. Install: `sudo apt install --no-install-recommends gnome-keyring libsecret-1-0`
   (about 30 packages via gcr4/GTK4). Side effects: `pinentry` (auto) moves
   to `pinentry-gnome3` (`sudo update-alternatives --set pinentry
   /usr/bin/pinentry-curses` reverts it). gcr4 may add a `gcr-ssh-agent`
   socket: check `SSH_AUTH_SOCK` in a new shell, and
   `systemctl --user mask --now gcr-ssh-agent.socket` if it changed.
2. Create and unlock the login keyring once per WSL boot, in **Windows
   Terminal, not a Jarvis terminal** (a HUD terminal is a Jarvis PTY). Until
   then every named profile is refused with this command (the keyring is
   started locked by the Browser's own probe; see *`unavailable` effectively
   never happens*), and Private still opens:
   `read -rsp 'Keyring password: ' KP; echo; printf '%s' "$KP" | gnome-keyring-daemon --unlock >/dev/null; unset KP`.
   The first run creates the keyring with that password. Use a new password,
   not the Google one. If a running daemon ignores `--unlock`, use
   `--replace --unlock` then `gnome-keyring-daemon --start --components=secrets`.
3. Check: `busctl --user get-property org.freedesktop.secrets
   /org/freedesktop/secrets/aliases/default org.freedesktop.Secret.Collection Locked`
   prints `b false`; the same with `Label` prints `s "Login"`;
   `ls -l ~/.local/share/keyrings` shows `login.keyring`.
4. After merge: `jarvis hud install`, restart daemon2, then check
   `/status` → `browser.sync` shows `keyring: unlocked` and, with a named
   profile open, `password_store: gnome-libsecret`.
5. M12, left for the owner: with the keyring locked (`gdbus call --session
   --dest org.freedesktop.secrets --object-path /org/freedesktop/secrets
   --method org.freedesktop.Secret.Service.Lock "['/org/freedesktop/secrets/aliases/default']"`),
   a named profile is refused, not downgraded. Then unlock it again (step 2).
   Also left: an existing `v10` password stays readable after the switch.

**Rolling back.** A daemon older than #215 cannot read a `profiles.json`
row that carries `"password_store"` ("profiles.json is unreadable") and
opens every profile with `basic`. To roll back that far, delete that key from
each row by hand: the marker in each profile's folder survives, so when this
version returns those profiles are still sticky (refused, never downgraded)
and their rows are written back at their next launch. **Do not open a
sticky profile under the older daemon**: it would look signed out (its `v11`
data unreadable) and write new data under the public key. A #215 daemon
reads the row flag but not the marker, so rolling back only to #215 needs
nothing removed; leave the row flags.

**`profiles.json` is not protected state** (decided in the follow-up). It
already lives in the Browser's folder, a credential folder
(`config.credential_dirs()`): v1's file tools refuse it for reads and writes,
v2's file tools and grants refuse it, and a v2 shell write under it is
always-ask (`credentials-path`). Protected state is for files that grant or
withdraw permissions; the sticky flag grants nothing. Joining that set
would also make the file **readable** to v1's `read_file`, since the
credential-folder judge exempts the gate's own state files by their exact
path (`files.credential_folder_judge`). And the marker, which now carries
stickiness alone, sits in a per-profile folder no fixed list can name. An
agent that can write in the Browser's folder can already destroy far more
than the flag; stickiness now needs both records cleared.

Tests: `tests/v2/browser_password_store_check.py` (fake `gdbus` runner,
temporary runtime folder with an unlistened socket, fake Playwright; the real
`subprocess.run` is patched to fail). Eleven mutants are each killed: a
locked or unrecognised state downgraded to basic, sticky ignored or never
recorded, a keyring attached by default to the real bus, Private given the
bus or the keyring flag, the after-launch check removed, status reporting a
constant, the bus socket check removed, and a probe that asks for more than
`Locked`. `hud/src/components/BrowserProfile.test.ts` covers the Manage line.
**The follow-up** adds a fake Chrome that writes the probe's row into a
temporary Cookies database with the prefix a real one would (or the one a
test makes it fall back to), `observed_store` against real SQLite (a
traced connection: one statement, `substr(encrypted_value, 1, 3)`, read-only
and immutable), marker and rollback cases, per-profile status, and a guard
that fails the run on an error in the status refresh thread. 37 mutants are
each killed (`~/.cache/jarvis-lead/kr-followup/mutants.py`): the observation
ignored, status reporting the requested store, a mismatched Chrome left open,
no flush filler, the probe never expired, the whole encrypted value read, the
probe row not matched by host, the marker ignored (at launch or in
`/status`), never written, or dropped by "Sign out of everything", a sticky
profile's missing record not written back, `unknown` shown as refused (daemon
and HUD, per profile and global), `unverified` given the install hint, the
per-profile `browser.json` reason lost, the probe runner bound at import; for
the gate: the rule removed (v1 and v2 layer 1), the whole-text reading
instead of command position, a caller that need not be the segment's
program, a reader matched as a word anywhere, readers, the `keyring` CLI or
`-m keyring` not refused, `keyring` refused as a bare word, monitors not
refused or judged on the whole line, a bus-address assignment keeping the
`--system` exemption, program-word or service-name spellings not normalised,
the impl portal dropped; and the Manage panel's interval or focus refresh
removed, and its per-profile state ignored.

## Chrome's own sign-in tab joins the sign-in pane (2026-10-15)

Owner report: "Sign in to Chrome" ended on the "does not open this kind of
address" page. The owner route was not the cause. A real-Chrome 155 run with
an owner tab already open, and one through the installed HUD build against a
real daemon, both ended on `chrome://settings/people` with no landing-guard
replacement (`browser_profiles_check` 06d). In the live log the sign-in tab
attached at 20:02:38 and the five replacements came 11–27 s later with no new
attach. So the replaced pages were in tabs already shown. That matches
navigation inside the settings page or Back. Measured: the people page's
"Customize profile", "Google services" and "Import bookmarks and settings"
go to `chrome://settings/manageProfile`, `/googleServices` and `/importData`.
Each is replaced and ends the capability. That is BD-5's exact rule, kept.
None of those pages belongs to sign-in.

What was broken: the page's own blue **Sign in to Chrome** button opens
`https://accounts.google.com/signin/chrome/sync?ssp=1&continue=https://www.google.com/&flow=history_opt_in&theme=mn`
in a **new tab with no opener**. Chrome sends `X-Chrome-ID-Consistency-Request`
with it. Real Gaia answers that request with a 302 to
`/v3/signin/identifier?…&flowName=GlifDesktopChromeSync`, and answers 404
without the header (PR #217 delta review). Chrome opens the tab, not the
page, so
`_link_opener` found no parent. The HUD was never told, and the owner saw
nothing happen. No other chrome:// page is visited on this path.

Now that tab becomes the sign-in tab's pop-up: its window gets
`{t:"popup"}` and the HUD adopts it. "No opener" alone is not "Chrome opened
it". The PR #217 review measured that Ctrl-, Shift- and middle-clicks on a
link give no opener, real or scripted (even `dispatchEvent` with no input).
None of them fires CDP `Page.windowOpen`, which fires only for `target=_blank`
and `window.open`, and those have an opener. So every condition below is
required (`_link_signin`, `_signin_popup`):

- **Jarvis did not open it.** `_adopt(own=)` is set at construction, before
  its first landing; the context's page event counts `instance.opening`.
- **No opener** (Playwright's `opener()`), and no opener named meanwhile.
- **Its request was exactly `https://accounts.google.com/signin/chrome/sync`**
  (scheme, netloc and path; any query), checked before Gaia's redirect.
  Measured on Chrome 155: the redirect leaves one history entry, whose
  `userTypedURL` is the requested address and whose `url` is the identifier
  page. Playwright's context `request` events never saw that request, so the
  check reads the history entry.
- **Its first landing is Google's sign-in origin exactly** (`https`,
  `accounts.google.com`, no port, no userinfo), and the entry's `url` is still
  on that origin. The other allowed landing is Chrome's error page:
  `chrome-error://chromewebdata/` when Google cannot be reached. There the
  entry's `url` is the failed request itself. That tab is shown too, so the
  owner sees Chrome's error page rather than nothing.
- **Its only history entry carries Chrome's own transition.** That is
  `auto_bookmark`, measured on Chrome 155. A page's open of the same
  address, in any of the shapes above, is `link`, and no page can make a
  browser-UI transition. This is the structural check, in place of
  `windowOpen`.
- **The owner pressed in that sign-in tab within `SIGNIN_ADOPT_S` (15 s).**
  The press is stamped in `_deliver`, which only the owner's stream reaches;
  driving sends its own input. Only `mousePressed` and `keyDown`/`rawKeyDown`
  count. A hover, a wheel, a release or a key-up does not.
- **At most once per sign-in tab** (`signin_adopted`), and only while its
  capability lasts.

The Google tab is judged like any page and holds no internal capability;
its chrome:// landings are still replaced. Any other tab with no opener
stays unshown, as before. `browser_profiles_check` covers this:
`SigninAdoption` tests each condition on doubles; 06e is the real flow with
a local TLS stand-in for `accounts.google.com` behind the suite's gate. That
stand-in 302s Chrome's request as Gaia does. 06e also covers a second attempt
and Jarvis's own tab at the address. 06f is the forgery shapes after an owner
press. 06g covers input that is not a press, a stale press, and a capability
that ended before Google's page landed. 06h is Google unreachable, showing
Chrome's error page beside the sign-in tab. Nothing leaves loopback. Through the HUD, the
Google tab opened as the pane's active tab.

**Not verified** (no Google contact in tests): what comes after Google's
page. `navigator.webdriver` is true, and Google may refuse the login as an
unsafe browser. After a login, Chrome's sync/history confirmation is browser
UI (a dialog), which a headless screencast does not draw. If Chrome then sends
the Google tab to a chrome:// page, it is replaced with the explanation. In
this fresh profile the people page offers "Sign in to Chrome", not "Turn on
sync…". That button appears only with an account, which needs Google.

## Owner pickers for native select, date and colour controls (B-PICKERS, 2026-10-16)

Design, measurements and the datalist assessment:
[the B-PICKERS plan](../plans/2026-10-16-browser-pickers.md); wire contract in
`docs/hud-api.md` ("owner pickers"). Chrome 155's screencast never draws a
`<select>`, date, time, datetime-local, month, week or colour popup, and the
popup still takes the keyboard (ArrowDown reaches no page listener; Enter
commits). `jarvis/v2/browser/pickers.py` offers the owner a HUD overlay instead.

- **Detection is the owner's own click on that element.** Only `_deliver` (the
  owner's ticketed socket) calls the module. Before an owner left press is sent
  it is pinned by CDP hit test (`Page.getLayoutMetrics` + `DOM.getNodeForLocation`,
  which takes document coordinates: measured; two calls, p50 about 3 ms, p95
  about 4 ms per press); after that press's own release is answered (never a
  let-go), a script-free `DOM.describeNode` drops anything but a select or an
  input whose `type` attribute is a picker kind (p50 1.2 ms on a text field),
  then the pinned `backendNodeId` is resolved in a `jarvis-pickers` isolated world of its
  own frame and must be connected, in that frame's current document
  (`ownerDocument === document`), a picker kind and `:open`. Throughout the
  probe, up to the moment the picker is recorded under the lock, nothing may
  hold the tab's attention: no dialog, file chooser or auth prompt, and its
  window attached and showing it (review of PR #232, MED 1: a select whose
  `mousedown` opened a file chooser was offered, then applied once the chooser
  closed). CDP has no popup event (measured). A `base-select` select is skipped (Chrome draws that picker
  in the page). An agent's click, direct CDP input, a page's `showPicker()` on
  another element, a list box and a date field's text segments offer nothing.
- **Read, measured and built before anything is sent**: the frame's `loaderId`
  (`Page.getFrameTree`) before the node is resolved and again after the read,
  the rectangle, and the whole message. A read that cannot be offered leaves
  the native popup as it was.
- **The native popup is closed** with a trusted `rawKeyDown` Escape and no
  key-up while `:open` is true (Chrome hands an open popup every key; the page
  sees no event, focus and value stay). A date-like popup still loading its own
  page ignores one, so it is asked again up to 12 times, 25 ms apart, until
  `:open` reads false; otherwise nothing is offered.
- **Reading** is one fixed isolated-world function: option DOM index, label,
  disabled (with a disabled optgroup), group; `hidden` options left out;
  date-like value/min/max/step/required; colour value. `DOM.getBoxModel` gives
  the rectangle. Labels are cleaned (`approvals.clean_line`) and capped at 80,
  at most 500 options and 60,000 bytes of JSON, the exact encoding sent, with
  a group's label charged when a kept item first uses it and only used groups
  sent; a message past the cap is never offered (`total` says how many exist;
  review LOW 2 measured 234,894 bytes from 500 single-option groups before).
  Option values stay in the daemon. Nothing on the bus or in a log.
- **Applying** (`{t:"pick"}`) is queued owner input: ordered, dropped under a
  card, a takeover stamp. On the Browser's thread the binding is re-checked
  (tab, page, page identity, main document generation, instance, attachment,
  hold count, approval epoch, no dialog/chooser/auth, visible), the frame's
  `loaderId` is still the one it was offered under (before the node is resolved
  and after: a `backendNodeId` from before a cross-process commit could name
  another node in the new document), then the same node in the same frame: connected, same document, same kind, enabled, not
  read-only, option count and the chosen option's value and label unchanged,
  a date-like value valid for a detached same-type input with the control's
  `min`/`max`/`step`. One apply per picker: the first `pick` takes it, a second
  with the same id does nothing. Assignment, `input` and `change` are three
  separately awaited calls; a handler that replaces the control or changes its
  value stops `change` (both tested); the same value dispatches nothing. The
  binding and loader re-checks between those steps are defence in depth, not
  each pinned by its own test. **The events are
  untrusted** (no trusted CDP path picks an exact option or date: measured).
- **Closing**: a main-frame navigation (same-document too), tab close, attach or
  detach, resize, hidden, a card (`held`), a dialog or file chooser, a new owner
  press (`cancelled`), a new picker (`replaced`); `pick_cancel` from the window.
- **The HUD** (`lib/remotepicker.ts`, `components/RemotePicker.tsx`,
  RemoteView): shown only as the answer to this window's own left press in the
  same stream generation within 5 s, once; drawn only while connected, live and
  unblocked (no card, global hold, auth, dialog, chooser or Downloads), closed
  the moment any of them appears. A picker that arrives while any of them is up
  is refused (`pick_cancel`), never kept to show once it goes: dialogs and the
  chooser are tracked as the socket reports them, ahead of React's render. A
  press made before a card does not count after it. **It never calls `focus()`**: keys typed in
  the Remote proxy are routed to it (arrows skip disabled options, Home/End,
  Page Up/Down, type-ahead, Enter, Escape; the HUD's own shortcuts keep theirs;
  the rest swallowed, as the native popup would); options keep focus where it
  is; the HUD's own date and colour inputs take focus only from the owner's
  click, and Set/Cancel return it to the proxy. A press or wheel on the page
  cancels it and is consumed whole (with its context menu). Graphite styling,
  page text as React text only.

**Datalist**: assessed, not built. Chrome 155 headless shows no datalist
suggestions at all (no `:open`; ArrowDown and Enter leave the typed text), so
nothing invisible captures keys. Offering suggestions needs a non-modal design
(keys keep going to the page while suggestions refilter), not this overlay.

**Also from the review**: the press is never sent when a card is raised while
its pin is awaited (the hold is asked again after the pin); a let-go release
drops the pin; the object-group release after a probe waits at most 1 s, so a
hung renderer cannot wedge the tab's input task; an IME commit while the picker
is open never reaches the page. After B-IME (#233) the picker and `RemoteIme`
share the proxy: a pending commit is flushed before the picker's key branch,
the branch ignores composing and keyCode 229 keys (an IME's, never type-ahead),
an open picker counts as no input for `RemoteIme`'s sink (`canInput`), so a
composition committed while the overlay is open is never sent, and a picker
that opens interrupts any composition. Tested: a composition and its 229 keys
while the picker is open reach neither the page nor the type-ahead (mutants:
the sink check, the 229 guard). The interrupt on open is defence in depth: the
owner's own press already interrupts a composition before any picker arrives,
and the flush can find nothing while a picker is open (both mutants survive,
as expected).

**Limits**: the `loaderId` check closes the gap between a cross-process commit
and `framenavigated` by reasoning, not by a test that produces one; cross-site (out-of-process) iframes are not covered (the hit test
stops at the `<iframe>`; the invisible popup behaves as before); keyboard-opened
pickers (Space, Alt+Down, F4) are not covered; the events are untrusted; a page
that closes its own popup between the `:open` check and the Escape receives the
Escape key-down; the rectangle is read once (a page that scrolls itself leaves
the overlay where the control was; a wheel or resize closes it). `pickers.py`
is not SELF_PROTECTED (an owner call, like `owner_tools.py`).

Receipts: `tests/v2/browser_pickers_check.py` (3 pure, 23 real Chrome 155
through the production ticket and socket, and an agent's real
`hud_browser_act` click on a select through BR9b's fixture; summed Chrome RSS
about 1.33 GiB); `lib/remotepicker.test.ts` and
`components/RemoteViewPicker.test.ts` (26 vitest);
`tests/face/hud_v2_pickers_check.py` (29 checks over the mock HUD, real
Chromium). Twenty-seven guard removals, each applied in place and restored,
fail their named checks (two IME defence-in-depth mutants, above, survive by
design). The review's: the probe not checking dialogs, the file
chooser or either (the reviewed code: the real chooser page is offered a
picker), group labels not charged (nothing offered) and neither charged nor
capped (a 131,377-byte message), the hold not asked after the pin (the press
reached the page), no one-apply-per-picker (re-applied), the HUD accepting a
picker behind a dialog or chooser, its gesture surviving a card, and, after
the B-IME merge, `RemoteIme` sending while a picker is open and IME keys fed to
the type-ahead. The first round's: the identity check (a detached twin applied), applying to
the frame's focused element instead of the pinned node, the option count, the
option value/label, a card not closing the picker, `change` after a handler
changed the value, no closing Escape, an agent's click probed (the BR9b
fixture), any open picker in the frame offered instead of the pressed element;
in the HUD the overlay focusing itself, no gesture needed, drawn under a card,
keys leaking to the page (vitest), and focus, card and gesture again in the face
check. The script is `~/.cache/jarvis-lead/pickers-mutants/mutants.py` (not in
the repository).

## IME composition in a Remote pane (B-IME, BR-7, 2026-10-16)

**The owner composes in the pane's own input proxy, never in the page**
(`hud/src/lib/remoteime.ts`, wired in `RemoteView.tsx`). The operating
system's IME runs against the proxy textarea; the page receives only
committed text, as `{t:"text"}` → `Input.insertText` (the existing paste
path, chunked, epoch-checked, a takeover like any owner input). No
`Input.imeSetComposition` is ever sent: the page never sees half-typed pinyin
or kana, and no remote composition state exists for a lost socket, a card or
a navigation to strand or commit later. While composing, the proxy is drawn
as a small box (`.remoteinput[data-composing]`) at the owner's last press in
the pane, so the owner sees the composition and the IME's candidate window
opens near the field they clicked, not at the pane's corner. It never takes
focus; `pointer-events: none` keeps it from intercepting a press.

Measured on Chrome 155 (CDP-driven, both ends real) and what the HUD does:

- A commit ends with `compositionend` carrying the text; Escape ends it with
  `""`, and nothing is sent.
- **A focus change mid-composition commits the raw text**: `compositionend`
  (`ni`) fires before the proxy's `blur`, in the same task. Chrome on Windows
  also commits on any mouse press before the press reaches the page. So a
  commit waits `COMMIT_HOLD_MS` (40 ms) and is dropped by a blur or any
  pointer press (a window capture listener); the next owner keystroke, paste
  or insertion sends it first, so order is kept.
- Emptying the proxy mid-composition ends Blink's composition without a
  `compositionend`; that is how an interruption cancels. Whether the
  system IME is told cannot be seen here, so after such a cancel the first
  plain `insertText` before a new composition or a forwarded key is dropped
  as the IME's late commit (`stale`).
- Text inserted with no composition (the Windows emoji panel, an IME's
  direct punctuation, a key whose keydown was not forwarded) arrives as an
  `input` of type `insertText` and is sent once. It used to be discarded.
- Any keydown with keyCode 229 is dropped whatever its `key` (some IMEs
  report the letter on the first key, before `compositionstart`), as are
  `isComposing`, `Process`, `Dead` and `Unidentified` keys.

Interruptions that send nothing and empty the proxy: blur, a pointer press,
an approval card, an options hold seen mid-composition (or a composition
begun under one), a reconnect or socket loss, `owner_reset` (any main-frame
navigation, same-document ones included) and an auth prompt. Pane-switch
keys are already ignored mid-composition (`Layout.tsx`).

Checks: `hud/src/lib/remoteime.test.ts` (the state machine, fake timers),
`RemoteView.test.ts` "IME composition" (jsdom wiring), and
`tests/face/hud_v2_remote_ime_check.py`: the built HUD in headless Chrome 155
with composition driven by CDP on the HUD page, each `key`/`text` message
parsed by `stream.parse` and applied by CDP to an isolated page holding an
`<input>` and a `contenteditable`. Twelve mutants each fail at least one of
them. **Limits**: real Windows IMEs (pinyin, Japanese, the emoji panel) were
not automated; the owner's live checklist is below (and in the owner live-check list, `docs/plans/2026-10-16-owner-live-checks.md`). A click into the page
mid-composition discards the composition, where Chrome itself would commit
the raw text. The candidate window follows the last press, not the page's
real caret (a daemon caret report would be a follow-up). Composition text is
never logged. A click away also drops what is still composing even when it is
real final text: a Korean syllable not yet closed, or Japanese with live
conversion before Enter. The stale guard can discard one legitimate
emoji-panel insertion, the first after a card, reconnect or navigation
interrupted a composition, if no key or composition came between. `end()`
sends any earlier waiting commit before holding the new one, so a commit is
never silently lost.

**Owner live checklist** (Windows, installed HUD, a Remote pane with a text
field):

1. Microsoft Pinyin: `nihao` shows the box where you clicked and the
   candidates beside it; Space or 1 puts 你好 in the page once; a full-width
   comma (，) appears once.
2. Microsoft IME (Japanese): `nihon`, Space, Enter gives 日本 once; kana
   committed without converting appear once.
3. Win+. emoji panel: the emoji appears once; again right after a letter.
4. Esc: `nihao` then Esc until the box is gone puts nothing in the page; the
   next letter appears once.
5. Mid-composition (`nihao`, not committed): clicking a chat pane, Ctrl+Alt+1
   and clicking back into the page each put nothing in the page; a new word
   then commits once with no leftover pinyin.
6. Microsoft Pinyin in **English mode** on a key-driven page (j/k shortcuts, a
   web terminal): some IMEs send keyCode 229 for every key, which is now
   dropped, so those keys could arrive only as text and the page's shortcuts
   would not fire.
7. Pick a candidate **with the mouse** in the IME's candidate window. If that
   blurs the HUD, a mouse-picked candidate is cancelled instead of committed.
8. Optional: a dead key (´ then e gives é once); an approval card arriving
   mid-composition puts nothing in the page.
