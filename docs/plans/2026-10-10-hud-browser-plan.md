# Plan: a built-in Browser for the Jarvis v2 HUD, replacing Preview

**Status:** draft, 2026-10-10, written by a read-only planner. Where it disagrees with the decisions file beside it (`2026-10-10-hud-browser-decisions.md`), the decisions file wins.

**Builds on:**
- PR #31 (WP-E: frame hardening, keep-origin, the terminal link menu, the workshop sandbox)
- PR #33 (`terminal_read`)
- `2026-10-10-terminal-read-mcp-plan.md` and its decisions file
- The split-panes plan (WP-G1/G2/H). G1 is in progress on `feat/hud-panes-eight` and rewrites about 500 lines of `workspace.ts`.

---

## For the owner: what this gives you

- **Preview becomes Browser.** It opens any web page, not only local dev servers. You can log in, click, type, scroll, use tabs, upload and download.
- **Local dev servers still open directly, as they do today**, with no lag. Every other page runs in a real Chrome that Jarvis's daemon owns, and the pane shows it live.
- **Annotate** freezes the page so you can draw on it with a pen, arrows, boxes and text, with undo and clear.
- **Send to chat** puts the picture, the address and the page's visible text into the selected chat's box. Nothing is sent until you press Send.
- **"Jarvis, look at my browser."** A read-only tool, `hud_browser_view`, gives the fast path a screenshot and the page text. Claude and Codex chats get the same tool once their jarvis-mcp tokens land. The rules are `terminal_read`'s:
  - only on a turn you typed at the HUD;
  - a "Jarvis can see" tick per pane;
  - refused for pages that may show a credential;
  - a visible note for every look.
- **No agent can click or type in your browser.** It holds your logins.

**The costs, plainly:**
- **Lag:** an estimated 30–100 ms, to be measured.
- **Video:** choppy.
- **Sound:** none in the pane.
- **DRM video:** Netflix and the like will probably not play.
- **Native pop-ups:** some (dropdowns, date pickers) may not appear in the picture at first.
- **Logins:** kept in a Linux Chrome profile that any program running as you could read.

---

## 1. What the code says today (with corrections to the brief)

1. **Today's Preview pane.**
   - `PreviewTab.tsx` is an iframe with `sandbox="allow-scripts allow-forms"`.
   - On main, `judgePreviewUrl` (`hud/src/lib/preview.ts`) refuses **only 8402** and anything that is not loopback http(s).
   - PR #31 adds refusals for 8405, the ports `/status` reports and the window's own port. It also adds `state/daemonPorts.ts`, `keepOrigin` and the terminal link menu.
2. **The tab label is the view key itself.** `Workspace.tsx` draws `{view}` from `PANE_VIEWS`, so the tab reads "preview" and has the test id `tab-preview`.
3. **`browser_view` cannot be the tool's name.**
   - `fastpath.FORBIDDEN_PREFIXES` includes `"browser_"`, and `_validate_toolset()` fails at import if any FAST_TOOLS name starts with it.
   - That prefix keeps v1's driving tools out of the fast path. Naming around it is better than loosening it.
4. **A real v1 gap, beside this plan.**
   - `config.is_face_origin()` refuses only `FACE_PORT` (8402).
   - v1's agent browser allows `localhost` by name, so it can load `http://localhost:8405/`, where the v2 API listener serves the same HUD page.
   - `POST /approvals/<code>` is **not** `owner_only`. It passes `check_origin` whenever Origin equals Host, which a page served from 8405 satisfies.
   - So a v1 surface running beside the v2 daemon (`jarvis chat`) could drive the HUD on 8405 and approve. BR-0 fixes this.
   - Separately, and outside this plan: Codex's built-in browser features, and any Playwright MCP a CLI has, could reach 8405 the same way. That deserves its own look.
5. **A browser inside WSL shares loopback with the daemon.** 8402, 8403, 8404 (the desktop bridge), 8405 and 8406 (the Spotify callback) are all directly reachable. A URL check is not enough; the network layer must refuse those ports.
6. **Playwright's defaults are wrong for a daily browser.**
   - `--mute-audio` and `--enable-automation` are on.
   - The sandbox is off (`chromium_sandbox` defaults false).
   - Component updates and phishing checks are disabled.
   - `route()` disables the HTTP cache, so request interception must not be the policy layer.
7. **Most of the plumbing exists.**
   - Tools can return images (`ToolResult.image_b64`), and `jarvis/v2/mcp.py` already turns them into MCP image content.
   - The HUD stages files into the selected chat with `dispatch({type: "stage", key: draftKey(...)})`. Files dropped outside a chat take this path (`App.tsx`, "files dropped on the window").
   - The whiteboard (`jarvis/face/static/whiteboard.html`) has a clean ops-list model to port: `S.ops`, `redraw`, `drawShape`, `shapeOp`, `shapeIsReal`, and `boardXY`'s letterbox maths.
8. **The machine.**
   - 24 cores, 16 GB (about 10 GB free).
   - WSL2 in NAT mode, so WSL cannot reach Windows' localhost.
   - WSLg and its PulseServer, `/dev/dxg`, Xvfb and pactl are present.
   - Playwright 1.61 with Chrome-for-Testing build 1234, which includes `libwidevinecdm.so`.
   - Ubuntu 26.04 with user namespaces available (`max_user_namespaces` = 62777). `ptrace_scope` = 1.
   - 139 Linux fonts and no Segoe UI. 543 Windows fonts are reachable under `/mnt/c/Windows/Fonts`.

---

## 2. The engine

| | (a) Daemon Chromium, headless, streamed into the pane | (b) Headed Chromium in its own WSLg window | (c) Your Windows Chrome over CDP (separate profile) |
|---|---|---|---|
| Fidelity | The full Chrome engine. Native pop-ups may be missing from the picture (M9); fonts limited until fixed (M10); probably no GPU (M1) | Full; pop-ups visible; WSLg scaling is blurry on HiDPI | Best: Windows GPU and fonts, Widevine with VMP |
| Latency | Estimated 30–100 ms input-to-picture (M2) | Native in its own window; a mirror in the pane is as slow as (a) | Native in its own window; as slow as (a) if streamed into the pane |
| CPU and memory | In the WSL VM: software raster plus JPEG encoding. A static page costs nothing, because a screencast sends a frame only when the page repaints | The same plus the WSLg compositor | On the Windows side; the least WSL load |
| Audio | None in the pane. Possibly out of your speakers through WSLg (M3) | Yes, through WSLg | Yes |
| Clipboard | Bridged by the HUD, only when you copy or paste | WSLg clipboard sync | Native |
| IME | Needs a hidden-textarea proxy; follow-up | Weak in WSLg | Native |
| Upload and download | The HUD's own picker feeds `set_files`; downloads go to a private folder, then "Save a copy" | Linux GTK dialogs on the Linux filesystem | Native Windows |
| Pop-ups and new tabs | Tabs inside the pane | New desktop windows | New desktop windows |
| JS dialogs and HTTP auth | Dialogs drawn by the HUD in the pane; HTTP auth later | Native | Native |
| Agent can see your exact page? | Yes, over Playwright's private pipe | Yes | Yes, but through an unauthenticated debug port **any local program** can use |
| Card stays on top, no keys reach the page under it? | **Yes, by construction**: the page is pixels inside the HUD, and only the HUD forwards input | **No**: an OS window can cover the card and keeps its own keyboard focus | **No**, for the same reason |
| Lever for other local programs | No port; the profile on disk | An X or Wayland display as well | A debug port with full control of a logged-in Chrome, which could also open the HUD and approve. Also needs mirrored networking or a Windows relay |

**(d) Other routes considered:**
- **An Electron or WebView2 HUD shell** would give a native `<webview>`. But it changes how the HUD opens (Chrome app mode, and the mic grant in your Chrome profile), and its native child views draw *over* the card. Not now.
- **A WebRTC transport.** Headed Chromium under Xvfb, captured with ffmpeg or GStreamer and sent over WebRTC, would give sound in sync, 30–60 fps and native pop-ups. It needs heavy dependencies and an X display, which is another lever for same-user programs. This is the upgrade path *inside* (a) if the screencast proves too weak; only the transport changes.

**Recommendation: (a).** It is the only option where the approval card stays on top and input is held under it structurally, not by hoping. It is also the only one where the agent's view needs no open port.

**The iframe stays, as "Direct" mode for local dev servers.** Direct mode keeps:
- no lag, and HMR;
- sound, IME and the clipboard;
- DevTools, through the HUD window;
- Windows-side dev servers, which WSL's loopback cannot reach.

The pane picks its mode from the URL. A loopback URL the WP-E judge allows opens **Direct**; everything else opens **Remote**. A per-pane switch ("Open in Remote" / "Open direct") overrides that.

**How it runs.**
- `jarvis/v2/browser/host.py` owns Playwright's **async** API on one daemon thread, `jarvis-owner-browser`. This keeps invariant 9: all Playwright work stays on its own thread.
- Other threads submit work with `run_coroutine_threadsafe`, and every call has a timeout. v1's `Session._submit` has none.
- It is fully separate from v1's `browser.SESSION`.
- It starts lazily, when the first Remote pane is drawn.
- `Daemon.stop` closes it after `terminals.hangup_all()` and before the listeners close.

**The stream.**
- `Page.startScreencast` sends JPEG frames, acknowledged one at a time.
- The daemon keeps only the latest frame per socket, and sends the next one when the HUD acknowledges the last one drawn. A slow window never builds up lag.
- The HUD decodes frames with `createImageBitmap` into a `<canvas>`. That needs no `img-src` change under ED-4's planned CSP.
- Input goes back as JSON over the same ticketed WebSocket (`jarvis/v2/ws.py`). The daemon turns it into CDP `Input.dispatchMouseEvent` / `dispatchKeyEvent` / `insertText`.
- The pane renders at the window's `devicePixelRatio` times the HUD zoom, so pixels map one-to-one and text stays sharp.

---

## 3. The profile, logins and downloads

**Its own profile, never your real Chrome's.** With (a), importing your Windows logins is impossible anyway: they are encrypted with DPAPI and app-bound keys.

**Recommended: logins persist across restarts (B-4).**
- The profile lives in `config.BROWSER_DIR` = `~/.local/share/jarvis/browser/profile` (env `JARVIS_BROWSER_DIR`).
- The folder is 0700, and the engine refuses to start if it is a symlink or readable by others.
- The whole `browser/` folder joins `config.V2_CREDENTIAL_DIRS`:
  - Codex grants touching it are refused, reads included;
  - commands or file tools naming it hit the credentials-path ask.
- **"Sign out of everything"** (owner-only) wipes the profile and the downloads.
- **Stated plainly:** Chromium on Linux with no keyring stores cookies under a fixed key. Any program running as you can copy them, and Claude workers run unsandboxed on this WSL2. That is the same accepted limit as `~/.config/gh`, but weaker than your Windows Chrome. Keep banking and your main email in your own Chrome.

**Alternative: session-only.** Logins last until the daemon restarts, and cookies stay in memory, protected by `ptrace_scope`=1. Safer, but you would sign in again after every daemon restart.

**The password manager is off.** The browser never saves passwords: `credentials_enable_service` is false and the password-manager features are disabled.

**Downloads (B-6).**
- Each download is saved 0600, under a cleaned name, to `browser/downloads/`.
- The pane's download shelf has **"Save a copy…"**. It streams the file to your Windows Chrome as a normal HUD download (`GET /browser/downloads/<id>`, owner-only, `Content-Disposition: attachment`), so it lands wherever your Chrome saves files.
- The daemon never writes into `/mnt/c`.

**The engine binary (B-3).**
- **Recommended: Google Chrome stable**, installed with apt in WSL and launched with `channel="chrome"`. A daily browser on the open web needs prompt security updates; Playwright's bundled build only updates when Playwright does. The deb's setuid sandbox helper makes the sandbox work, and it has a better chance at Widevine.
- **Fallback:** the bundled build, with its age shown under `/status` → `browser`.

**A curated launch.**
- Keep the sandbox, Safe Browsing and component updates.
- Drop `--enable-automation` and `--mute-audio` (sound is off through its own switch, §9).
- The environment comes from an allowlist, as `terminals.clean_environment` does: no `JARVIS_*`, `OPENROUTER_API_KEY`, `ANTHROPIC_*` and so on.

---

## 4. Navigation policy for your browser

Three layers. The second is the real boundary.

### Layer 1: a URL judge
`jarvis/v2/browser/policy.py` `judge_url()`, mirrored in the HUD's `lib/browser.ts`. It checks every URL you type, every "Open in Browser" and every stored URL before loading.
- **Allowed:** `http`, `https` and `about:blank`.
- **Refused:**
  - a user name or password in the URL;
  - `file:`, `chrome:`, `chrome-untrusted:`, `devtools:`, `view-source:`, `chrome-extension:`;
  - typed `javascript:` and `data:`;
  - any `about:` page but blank.

### Layer 2: a SOCKS5 gate
`jarvis/v2/browser/gate.py` is a small loopback-only proxy in the daemon. Chrome launches with:
- `--proxy-server=socks5://127.0.0.1:<gate>`;
- `--proxy-bypass-list=<-loopback>`, so loopback goes through the gate too;
- `--disable-quic`;
- `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`.

Every TCP connection passes through the gate: pages, redirects, iframes, fetch, XHR, WebSockets, workers, service workers and prefetch. For each one the gate:
- **resolves the name itself and connects to the address it checked**, so there is no DNS rebinding between check and connect;
- **always refuses** Jarvis's ports, configured and default (8402, 8404, 8405, 8406), plus the HUD and API ports `/status` reports;
- refuses 8403 unless it is the daemon's reported workshop port, so the workshop and "Project root" keep working;
- refuses its own port;
- refuses unresolvable names;
- refuses private-network addresses unless the LAN switch is on (`config.is_lan_host` semantics);
- allows public addresses and any other loopback port (your dev servers).

It logs counts and the class of each refusal, never a host or URL. A refused or failed connection shows as a network error page. BR-1a's first test must confirm that Chrome sends hostnames to a SOCKS5 proxy (remote DNS).

### Layer 3: a landing guard
On every main-frame `framenavigated`, a URL with a disallowed scheme (other than Chrome's own error pages) is replaced with an explanation page, as v1's `_guard_landing()` does.

**Not used:** Playwright's `context.route` for policy. It disables the HTTP cache and slows every request.

### Shared rules
- **One port list.** `config.control_ports()` is the single Python definition, read by the gate, by BR-0's v1 fix, and by `/status` → `refused_ports`. The HUD's Direct judge (WP-E) reads that list and keeps refusing the defaults.
- **Your LAN (B-5):** off by default.
  - A Settings switch turns it on. It is stored in `~/.config/jarvis/browser.json`, which becomes protected state so no agent can flip it.
  - Chrome's own Local Network Access check is a second layer against public pages probing your LAN (M7).
  - In NAT mode, Windows-side services are reachable only at the host's private IP, so they need the switch or Direct mode.
- **Refused outright:**
  - extensions (`--disable-extensions`);
  - DevTools: no `--remote-debugging-port`, ever, because it is a full-control port. Use Direct mode or your own Chrome for DevTools;
  - the browser's permission prompts (camera, mic, location, notifications, clipboard read, pointer lock): all denied;
  - fullscreen: it stays inside the remote viewport.
- **No browsing leaves memory.** No URL, title or page text goes on the bus, in a log line, in a thread log, on Discord or on disk, apart from the profile and the downloads themselves. The bus carries only `{tab_id, at}` records, because `/events` is open to any local client on the API listener.

---

## 5. Annotation and "Send to chat"

### The flow
1. **Annotate** freezes the view as a still. The live page keeps running underneath.
2. A drawing layer sits over the still: pen, arrow, box, text, undo and clear, in three colours. The ink is content, not HUD chrome, so red is fine.
3. **Send to chat** adds two attachments to the **selected chat's** box (W-6, `selectedChatOf`) through the existing `stage` path, and **never sends**:
   - `page.png`: the still with the ink flattened in, downscaled to at most 1920 px on the long edge. It switches to JPEG at quality 0.9 if it would exceed 3.5 MB.
   - A **page-capture reference**: `{name: "page text", mime: "application/x-jarvis-page-capture", data_b64: <capture id>}`.

   With no chat drawn it is refused with the existing sentence. Clicking a Browser pane never changes the selected chat (the WP-B rule). Parked drafts and hand-backs carry these attachments automatically.
4. **On send,** `hud_api.assemble_turn` resolves the reference **only for an owner request** (HUD listener plus Origin; `send` passes `owner=`).
   - It builds `untrusted.fence("URL: …\nTitle: …\n\n<visible text>", url)` **server-side**, minted at send time. The page's own title therefore never lands unfenced among your words, and there is one fence implementation, not two.
   - An expired reference, a restarted daemon or a non-HUD request gets a note instead.
   - The page text passes the same credential guard as the agent's look (§6). A hit gives "page text withheld: possible credential".
   - The Discord mirror shows only the names "page.png" and "page text".

**What is attached by default (B-10):** the URL, the title and the text **visible on screen**, matching the picture. "Include the whole page's text" is an unticked box.

### How the still is taken
- **Remote:**
  - `Page.captureScreenshot` of the viewport at the remote DPR, through `GET /browser/tabs/<id>/capture` (owner-only).
  - The route returns `{capture_id, image, url, title (cleaned), text_chars}`.
  - The text is held in daemon memory in `jarvis/v2/browser/captures.py`: random 128-bit ids, a 24-hour TTL, at most 16 entries, and a byte cap.
- **Direct:**
  - `getDisplayMedia({preferCurrentTab: true})` cropped to the iframe with `CropTarget`: one frame, then the tracks stop. Chrome shows its "share this tab" prompt once per capture (M6).
  - Only the URL the HUD loaded is attached, fenced through `POST /browser/captures {url}`. The HUD cannot read a cross-origin frame's title or text.
  - If M6 fails, Direct mode offers "Open this in Remote to annotate".

### Reusing the whiteboard
- The whiteboard's ops model is ported into `hud/src/lib/annotate.ts`, a pure module:
  - ops in image pixels;
  - `render(ctx, ops, scale)`, `undo` and `clear`;
  - the letterbox mapping, made zoom-aware through `toCss`;
  - a new arrow op `t: "a"`.
- The view is `hud/src/components/Annotate.tsx`.
- `whiteboard.html` stays untouched. It is v1 and has no module system to share through.

---

## 6. "Tell Jarvis, Claude or Codex to look at it"

### (i) Send to chat
As in §5. You start it, and it is available without the "Jarvis can see" tick.

### (ii) `hud_browser_view`, a read-only tool
**Signature.** `hud_browser_view(tab: str = "", whole_page: bool = False) -> ToolResult`, in `jarvis/v2/tools/hud_browser_view.py`.
- An empty `tab` means the front tab: the Browser pane you focused last, which the HUD reports with `POST /browser/front` (owner-only).
- `tab` may also be a pane number or a tab id.
- When the choice is ambiguous it answers with "pane N (host)" lines: hosts cleaned, titles never shown.

**Desk-only, exactly as #33.**
- It checks `runtime.at_desk()` and `runtime.depth() == 0`, and refuses otherwise with a `NOT_AT_DESK`-style sentence.
- It joins `DESK_TOOLS`, `FAST_TOOLS` and `tools.EXPLICIT_ONLY`.
- Discord, DM, schedules, tasks, workflows, sub-agents and the escape hatch never get it.

**The "Jarvis can see" tick (B-7).**
- It is per pane and per origin, like WP-E's `keepOrigin`. It is stored as the origin it was given for (`PaneSpec.seeOrigin`), so navigating to another origin turns it off.
- The daemon is the authority, set with an owner-only `PATCH /browser/tabs/<id>`.
- It is **on by default for loopback dev pages and off for every other site.**
- A refused look says: "the owner has not let Jarvis see this page (<host>); ask them to tick 'Jarvis can see', or to use Send to chat."

**Credentials.**
- Page text passes the #33 guard's value and format families (`secrets.secret_values()`, `credential_patterns`). A hit refuses the whole look, **including the screenshot**, because the pixels show the same thing.
- The keyword heuristic withholds single lines.
- No form-control value is ever read, so password values never appear. The screenshot shows dots, as you see them.
- **Stated limit:** text drawn in a canvas or an image cannot be checked.

**It never touches your page.**
- It runs a **read-only** scan in an **isolated world** (`Page.createIsolatedWorld`), so built-ins the page has patched do not apply.
- No node moves and no `data-jarvis-ref` stamps, unlike v1's `_SNAPSHOT_JS`, which mutates the DOM.
- It shares one judge with v1 by moving `_JUDGE_JS` into a single module both import.
- No scroll, focus or navigation.

**Direct-mode panes** are not visible to the daemon. A look at one does a fresh hidden load of the same loopback URL and says so: "a fresh load, not your exact view".

**What it returns.**
- `untrusted.fence(URL, title, visible text or up to 4000 characters of the page, notes)`.
- A viewport image, downscaled to at most 1568 px on the long edge. A model without vision gets no image and a note.

**Its trace.**
- The bus event `browser_viewed` is exactly `{tab_id, at, refused}`.
- The pane shows "Jarvis looked · 15:42".
- `TOOL_FINISHED.summary` is fixed ("looked at a page" or "refused"), so no page text reaches `log.jsonl` or the bus. This is #33's review fix.

**Claude and Codex, after MCP WP-1 to WP-3.**
- It goes into `session_tools.SESSION_TOOLS`, read-only and foreground-only.
- `/mcp/call` gains an `image` field, which the relay turns into MCP image content.
- The `jarvis-tool` permit layer allows it.
- Claude's hook allows it, but refuses a call carrying `agent_id`.
- Codex gets `approval_mode = "approve"` and `readOnlyHint`.
- Unverified: whether Codex 0.161 accepts MCP image results (a live check).

**Where the text goes.** Extend the MCP plan's §5 wording: page text and screenshots go to OpenRouter, Anthropic or OpenAI with the chat, and stay in each CLI's transcript.

### (iii) Driving: recommended no (B-9)
- No tool, MCP tool, Discord verb or escape hatch may navigate, click, type or upload in your browser.
- A grep test in `tests/v2/browser_check.py`, shaped like `terminal_check`, allows exactly one read path: the tool's single `look_for_tool(` call.
- Your logins make this a far bigger lever than v1's fresh agent browser, and the desktop rule ("no browser") already settles it.
- An agent that must act on a public page uses its own browser, which has no logins.
- Driving, if you ever want it, needs its own decision: per-action approval, a "Jarvis is driving" banner and takeover.

---

## 7. The rename: "Preview" becomes "Browser"

**When:** after G2 merges, as BR-3a, landing just before Remote mode (B-14). The label then never promises more than the pane can do, and nothing collides with G1 and G2's `workspace.ts` rewrite.

**Names in the code.**
- In `lib/workspace.ts`:
  - `View`/`PANE_VIEWS` use `"browser"`, and `DEFAULT_VIEWS[2] = "browser"`;
  - `PaneSpec.previewUrl` becomes `browserUrl`. `keepOrigin` stays; `mode?` and `seeOrigin?` are added;
  - `setPreviewUrl` becomes `setBrowserUrl`.
- In `App.tsx`: `openPreview` becomes `openBrowser`, `PreviewRequest` becomes `BrowserRequest`, and `previewAsk` becomes `browserAsk`.
- `PreviewTab.tsx` becomes `BrowserTab.tsx`. WP-E's iframe code moves into a `DirectFrame` part with its rules unchanged.
- `lib/preview.ts` and its test become `lib/browser.ts` and its test.
- The CSS class `.previewframe` becomes `.browserframe`.

**Test ids.**
- `tab-preview` becomes `tab-browser`.
- `preview-url`, `preview-go`, `preview-project`, `preview-refused`, `preview-frame` and `preview-keep-origin` become `browser-*`.
- **A collision:** WP-E already uses `term-link-browser` for "Open in a browser tab". Rename WP-E's two items to `term-link-pane` ("Open in Browser") and `term-link-tab` ("Open in a Chrome tab").

**Terminal links (B-11).** With a real browser, Ctrl+click on **any** http(s) link opens the menu, with "Open in Browser" first. Today only loopback links get the menu.

**Stored-state migration.** The same key, `jarvis.hud.workspace`, migrates as it parses:
- `view:"preview"` becomes `"browser"`;
- `previewUrl` becomes `browserUrl`;
- `keepOrigin` is kept only if it still matches its URL.

Saves write only the new names. A rolled-back HUD shows that pane's default view and loses its URL; nothing worse.

**Python is left alone.** `preview_route`, `preview_only` and `workshop_port` name the workshop listener (8403), not the pane. The docs will call 8403 "the workshop origin".

**The test sweep.**
- `hud_v2_preview_check.py` becomes `hud_v2_browser_check.py`.
- Other suites' references change: `hud_v2_layout_check` (31), `hud_v2_check` (29), `hud_v2_multichat_check` (14), `hud_v2_projects_check` (8), `hud_v2_mock` (7), and `hud_v2_declutter_check` and `hud_v2_screens` (1 each).
- `workspace.test.ts` gains migration cases, and the layout suite seeds an old-shape value and checks it survives.

**Docs.** CLAUDE.md (a rename note in the WP-A, WP-B and WP-E paragraphs), design §18 and `docs/hud-api.md`.

**The split plan's copy rule carries over.** A split copies the URL into a new tab sharing the cookie session. `keepOrigin` and `seeOrigin` are never copied.

---

## 8. Safety under the approval card

1. **Inert.** The pane, canvas and annotation layer are inside the workspace, so `Approvals.tsx` makes them `inert`. The card takes focus off them (`behindTheCard`), and the focus proxy is blurred.
2. **The HUD holds input.** Every pointer, wheel, key, paste and IME handler drops its event while `blocked`. Escape and Enter are never forwarded under a card; Escape bubbles and denies.
3. **The daemon holds input too, which is the real layer.**
   - The browser socket drops every input frame while `daemon.approvals.pending()` is non-empty, and replies `{t:"held"}`.
   - Input queued before or during a card is **dropped, never delivered later**, and the HUD says so.
   - Frames keep streaming, view-only.
4. **The browser cannot be driven into the HUD.** The judge, the gate and the landing guard (§4) refuse every Jarvis port at the TCP level, whatever the spelling, redirect or script. That matters because any page on 8402 or 8405 could POST approvals.
5. **The card stays on top.**
   - Pages are pixels in a canvas below `#authveil` (z-index 100).
   - Headless Chrome opens no OS windows, and pop-ups become tabs.
   - JS dialogs are drawn by the HUD inside the pane, labelled "This page says:". They are never modal, never styled like the card, and never take focus from it.
6. **No page content reaches the HUD's DOM.**
   - Frames are JPEG bytes decoded into a canvas.
   - Titles, URLs, dialog text and file names are cleaned (one line; control, bidi and invisible characters stripped; capped).
   - They are rendered as React text, never `innerHTML` and never `document.title`. The host is emphasised and bidi-isolated.
   - No favicon in v1.
7. **Clipboard** crosses only when you copy or paste. Ctrl+C reads the selection in the isolated world; Ctrl+V comes from the HUD's own paste event. A page can never write your clipboard.
8. **A headless check:** `elementFromPoint` at the card's buttons returns the card, with Browser panes underneath, at 70% and 160% and in an 8-pane layout.

---

## 9. Limits, stated plainly

- **Sound is not in the pane.**
  - It might reach your speakers through WSLg if headless Chrome can play it (M3).
  - An Xvfb display would add sound, but it is another lever for same-user programs, so it waits until you ask (B-12).
  - One switch covers the whole browser; there is no per-tab mute.
- **Video** runs at roughly 15–30 fps with no A/V sync.
- **DRM** (EME) video may show black. Netflix-class services will probably refuse.
- **WebRTC calls won't work:** there is no UDP through the gate.
- **Native pop-ups may be missing from the picture (M9):** `<select>`, date and colour pickers, autofill and the context menu. BR-7 adds a HUD-drawn select list and HUD menus. Cursor shapes are not streamed.
- **Not at first:** IME composition, HTTP auth prompts (401 pages show their body) and find-in-page.
- **Some sign-ins may refuse an automated Chrome:** Google sign-in and some anti-bot pages (M4).
- **Fonts** fall back until Windows fonts are added (M10, B-16).
- **WSL's loopback is not Windows' loopback.** Windows-side dev servers need Direct mode.
- **Keys.**
  - Ctrl+W, Ctrl+T and Ctrl+N belong to Chrome and close or open HUD windows. Use the pane's tab × instead.
  - F5 and Ctrl+R reload the tab, not the HUD.
  - Ctrl+L focuses the URL bar.
  - Alt+←/→ go back and forward.
- **Memory:** each tab is a renderer of roughly 50–300 MB. There is a cap of 12 tabs in all, and undrawn tabs stop streaming.
- **Cookies on disk** are readable by any program running as you (§3).

---

## 10. Measurements before BR-1 is finalised

These run in a spike script, `scripts/browser_measure.py`: manual, never in a suite, with a temp HOME, a local fixture server and ephemeral ports.

- **M1, frame rate, CPU and memory.**
  - Pages: a fixture with a 60 fps CSS animation, and a long scroll page.
  - Sizes: 1280×800 at DPR 1, 1920×1080 at DPR 1, and 1600×900 at DPR 1.5.
  - JPEG quality 60 and 80, 30 s runs, with an immediate ack and with a 16 ms consumer.
  - Record:
    - fps;
    - mean and p95 frame bytes;
    - total %CPU of the Chrome processes (from `/proc/<pid>/stat` deltas);
    - the daemon thread's CPU;
    - the RSS of the Chrome tree;
    - page-load time through the gate against direct.
  - Repeat with GPU flags (default, `--use-angle=vulkan`, `--use-gl=egl`) and record the text of `chrome://gpu`.
- **M2, input to picture,** in your real HUD window on Windows. Press a key that flips a fixture page's colour 100 times, and measure from send to the first changed frame drawn. Report the median and p95.
- **M3, audio.**
  1. Headless without `--mute-audio`, with `PULSE_SERVER=unix:/mnt/wslg/PulseServer`, playing a local WAV. Do you hear it?
  2. Then headed under Xvfb.
  3. Then check `pactl list sink-inputs` and `set-sink-input-mute`.
- **M4, sign-in and bot checks,** run by you on live sites: accounts.google.com, a Cloudflare challenge and GitHub. Try the bundled build and Chrome stable, with and without `--enable-automation`.
- **M5, memory** with 1, 4 and 8 real tabs.
- **M6, Direct capture in app mode.** `getDisplayMedia({preferCurrentTab: true})` plus `CropTarget` on the iframe. Record the prompt and the pixels.
- **M7, Local Network Access.** From an https public page, `fetch("http://127.0.0.1:<fixture>/")` with permissions denied. Is it blocked?
- **M8, the sandbox.** Does Chrome start with `chromium_sandbox=True`? Does the renderer get its own PID namespace, and `Seccomp: 2` in `/proc/<pid>/status`?
- **M9, native pop-ups.** Does a `<select>`, date, colour or `<datalist>` pop-up appear in screencast frames, headless and under Xvfb?
- **M10, fonts.** With a private fontconfig that includes `/mnt/c/Windows/Fonts`: the first-start time, the RSS, and whether a "Segoe UI" page renders correctly.

---

## 11. Work packages and order

**Common to every package:**
- Free tests, with a temp HOME and config and the daemon on port 0.
- Never 8402, 8403 or 8405; local HTTP fixture servers only.
- Each check fails against its named mutation in a scratch copy.
- Each package merges on review, with docs in the same PR.

**BR-0, close v1's control-plane gap.** Size S, low risk, can start now.
- **Change:** add `config.control_ports()`. `is_face_origin` (v1's browser and `fetch_page`) refuses 8402, 8404, 8405 and 8406, and keeps allowing 8403, which v1 uses on purpose.
- **Tests:** `tests/browser/policy_check.py` and the web tests.
- **Mutation:** drop 8405, and the case where 8405 loads the HUD must fail.

**BR-1a, engine, gate and navigation.** Backend only. Size L, high risk. Can start once #31 has merged; runs **in parallel with G1, G2 and H**.
- **New code:** `jarvis/v2/browser/{policy,gate,host,routes}.py`.
- **Config:** `BROWSER_DIR`, the credential-dir entry and `browser.json`.
- **Daemon:** lazy start and stop, and `/status` → `browser`, `refused_ports`.
- **Routes:** `/browser` is mounted in `hud_api.route` behind `owner_only` plus `_strict_host`.
- **Tests:**
  - `tests/v2/browser_gate_check.py` (no Chrome):
    - every refusal family: reported and default ports, `localhost`, `foo.localhost`, `[::1]`, `0.0.0.0`, `127.1`, `0x7f000001`, `::ffff:127.0.0.1`;
    - rebinding, through an injected resolver;
    - the LAN switch, unresolvable names, its own port, and every SOCKS ATYP;
    - a dev port is piped through.
  - `tests/v2/owner_browser_check.py` (real headless Chrome):
    - **twelve ways into the HUD:** typed in each spelling, a 302, a meta refresh, `location=`, an iframe, fetch, XHR, a WebSocket, a service-worker fetch, `<img>`, a form POST and `window.open`. The test listeners' accept counters must stay **0**;
    - the refused schemes;
    - the profile is 0700 and on the credential list;
    - persistence and forget;
    - no Chrome left after `stop()`;
    - v1's `SESSION` is never started;
    - the launch flags and a clean environment;
    - no URL or title on the bus or in the log.
  - `tests/v2/frame_check.py` learns `/browser`.
- **Mutations:** a second DNS resolution; the bypass list removed; owner-only removed; the typed-URL judge removed; the profile created 0755.

**BR-1b, stream, input, hold, dialogs, tabs and files.** Backend only. Size L, high risk. Runs in parallel with the G packages.
- **Tests:** `tests/v2/browser_routes_check.py`:
  - the ticket and takeover (the terminal pattern);
  - latest-frame delivery under a slow reader;
  - keys, text, mouse and wheel arriving at a fixture page that logs them;
  - **the hold:** with an approval pending, nothing arrives, `held` is sent, and nothing is replayed after the card resolves;
  - dialogs forwarded and answered;
  - pop-ups opening in the opener's pane, and the 12-tab cap;
  - downloads saved 0600 with cleaned names, and served owner-only;
  - `set_files` uploads;
  - message caps.
- **Mutations:** remove the hold; replay held input; an unbounded backlog; a `../` download name.

**BR-2, pure HUD modules.** New files only. Size M, medium risk. **Can start now.**
- **Modules:**
  - `lib/annotate.ts`;
  - `lib/remote.ts`: the key and mouse mapping to CDP, `browserTakesKey`, coordinates under zoom and DPR, wheel normalisation, a generation-tagged latest-only `FramePipe`, and `cleanTitle`.
- **Tests:** vitest.
- **Mutations:** AltGr sent as Ctrl+Alt; zoom not divided; a frame from an older socket drawn.

**BR-3a, the rename and migration (§7).** Size S–M. Starts **after G2 merges**.
- **Mutations:** drop the migration; carry `keepOrigin` across an origin mismatch.

**BR-3b, Remote mode in the Browser pane.** Size L, high risk. Starts after BR-1b, BR-2 and BR-3a.
- **Code:**
  - `components/BrowserTab.tsx` and `RemoteView.tsx`;
  - `state/browser.ts`, a `BrowserManager` like `TermManager`;
  - a toolbar showing the security state from CDP, and a tab strip;
  - automatic mode choice plus the switch, and "Open in Browser".
- **Tests:** `tests/face/hud_v2_browser_check.py`, with a **fake stream** (`route_web_socket`, a new `hud_v2_mock_browser.py`). No real Chrome runs in the HUD suite.
  - frames are drawn;
  - under a card the fake records **zero** input, Escape denies, and the card is on top at 70% and 160%;
  - hostile titles are drawn as text, and `document.title` is unchanged;
  - dialogs stay in the pane;
  - in 8 panes, only drawn panes stream;
  - W-6 is untouched.
- **Mutations:** drop the HUD-side `blocked` check; a dialog rendered through `innerHTML`.

**BR-4, annotate and Send to chat.** Size M. Starts after BR-3b; the Direct half waits for M6.
- **Code:** capture references in `assemble_turn`, and `captures.py`.
- **Tests:**
  - the drawing tools;
  - Escape under a card denies rather than cancels;
  - Send to chat stages one image and one reference into the selected chat in a split, and never POSTs `/send`;
  - with no chat drawn, the refusal sentence;
  - backend:
    - the reference resolves only for the owner;
    - the fence is minted per send;
    - placeholder credentials are withheld;
    - an expired reference gives a note;
    - Discord sees names only.
- **Mutations:** resolve on the API listener; fence in the HUD.

**BR-5, `hud_browser_view` on the fast path.** Size M, medium-high risk. Starts after #33, #34 and BR-1b; the tick and the note come after BR-3b.
- **Tests:** `tests/v2/hud_browser_view_check.py`:
  - every desk refusal: Discord, DM, the escape hatch, a schedule, a task, a sub-agent, a turn steered off the desk;
  - the tick's default, and its drop on navigation;
  - credential families refused with no image;
  - a password marker never appears;
  - a MutationObserver sees **zero** mutations;
  - a page that patches `getComputedStyle` does not change the result;
  - the exact event shape;
  - the private summary;
  - `FORBIDDEN_PREFIXES` unchanged;
  - the grep test.
- **Mutations:** a main-world evaluate; a scan that mutates the DOM; skip the tick; a summary carrying text; remove the desk check.

**BR-6, Claude and Codex.** Size S–M. Starts after MCP WP-1, WP-2 and WP-3. Live checks: Claude sees the image, and Codex accepts MCP image content.

**BR-7, follow-ups.** Each is size S–M and driven by the measurements:
- the select and date pickers (M9);
- IME;
- HTTP auth through CDP `Fetch.authRequired`;
- sound (M3);
- Windows fonts (M10);
- find-in-page;
- the context menu and cursor shapes.

**What runs in parallel with the split-panes work.** BR-0, BR-1a, BR-1b, BR-2 and BR-5's backend touch none of `workspace.ts`, `Workspace.tsx`, `Layout.tsx` or `ChatTab`. BR-3a, BR-3b and BR-4 wait for G2.

---

## 12. Open decisions, with recommendations

| # | Decision | Recommendation |
|---|---|---|
| B-1 | Engine | (a) a daemon-owned Chrome streamed into the pane; the iframe stays as Direct mode |
| B-2 | Default mode for local dev URLs | Direct (as today), with a per-pane switch to Remote |
| B-3 | Engine binary | Google Chrome stable via apt (updates, sandbox, Widevine); the bundled build as fallback, its age shown |
| B-4 | Logins across restarts | Persist, in a credential dir, 0700, with "Sign out of everything" and no saved passwords. Keep banking and main email in your own Chrome |
| B-5 | LAN addresses | Off by default; a Settings switch that is protected state |
| B-6 | Downloads | A private folder plus "Save a copy" through your Chrome; never a daemon write to `/mnt/c` |
| B-7 | "Jarvis can see" default | On for local dev pages; a per-pane, per-site tick for everything else, dropped on navigation |
| B-8 | Tool name | `hud_browser_view`, keeping `browser_` forbidden on the fast path |
| B-9 | May an agent drive your browser? | No. Revisit only as its own decision |
| B-10 | What Send to chat attaches | Picture, URL, title and the text on screen; whole-page text unticked |
| B-11 | Terminal links | Any http(s) link offers "Open in Browser" first |
| B-12 | Sound | Off in v1; on if headless plays through WSLg (M3); no Xvfb unless you ask |
| B-13 | Tabs | Tabs in each pane, pop-ups in the opener's pane, 12 in all |
| B-14 | Rename timing | After G2, merged just before Remote mode |
| B-15 | The v1 gap (8405 and other ports) | Fix now, as BR-0 |
| B-16 | Fonts | A private fontconfig with the Windows fonts, if M10's start-up cost is acceptable |

### Critical files for implementation
- `hud/src/components/PreviewTab.tsx` and `hud/src/lib/preview.ts` (PR #31's versions) become `BrowserTab.tsx` (Direct and Remote) and `lib/browser.ts`.
- `hud/src/lib/workspace.ts`: the View/PaneSpec rename and the stored-state migration, after G1 and G2.
- `jarvis/v2/hud_api.py`:
  - `route` mounts `/browser`;
  - `assemble_turn` takes capture references;
  - `check_origin` and `frame_headers`.
- `jarvis/v2/daemon.py`:
  - lazy start;
  - `stop()` ordering;
  - `status()` → `browser`/`refused_ports`;
  - the input hold while an approval is pending.
- The new `jarvis/v2/browser/` package: `policy.py`, `gate.py`, `host.py`, `routes.py`, `captures.py`.
- `jarvis/config.py`: `is_face_origin` and the new `control_ports()`, `BROWSER_DIR`, `V2_CREDENTIAL_DIRS`.
- `jarvis/v2/providers/fastpath.py`: `FAST_TOOLS` and `DESK_TOOLS` gain the tool; `FORBIDDEN_PREFIXES` is left as it is.
- For reference:
  - `jarvis/browser.py`: `_JUDGE_JS`, `_guard_landing`, the thread model;
  - `jarvis/face/static/whiteboard.html`: the ops model to port;
  - `hud/src/App.tsx`: the `stage` path for Send to chat;
  - `docs/plans/2026-10-10-terminal-read-mcp-plan.md`.
