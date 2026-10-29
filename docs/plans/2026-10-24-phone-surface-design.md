# Design: the phone surface (Jarvis v2)

**Status (2026-10-28): P-0 to P-7 built and merged (see §10.1 to §10.7),
and QH (quiet-hours deferral, §10.8) merged; MR is in progress; P-8 (the
CLAUDE.md Phone section, and the bare address `/` answering `302 /m/` on the
remote listener) is this PR.** The hub README pointer (§8 step 2) still waits
for the owner. Written against `main` at `2229645e`. It depends on the
[owner inbox](2026-10-24-owner-inbox-design.md): the inbox is the phone's main
screen. Decisions in §2 are the planner's defaults, except those marked
**owner, 2026-10-24**; the open questions in §11 have their defaults in bold.
Three spikes (§10, P-0) must pass before anything else is built, because the
design rests on them.

**Owner decisions, 2026-10-24.**

- **O-1. The phone is an iPhone** on current iOS (26 or later).
- **O-2. Discord stays unchanged, as a backup.**
- **O-3. Phone approvals are as authoritative as the HUD's, provided the
  network stays secured.** That means Always, and answering HUD-only
  (Browser) cards, from the phone. "Secured" is defined in §4.1. If any of
  its conditions fails, the phone falls back to Discord-level authority.
- **O-4. The other device on the tailnet belongs to a family member and is
  trusted.** The design still never treats the tailnet as proof of the owner
  (PH-3).
- **O-5 (owner, 2026-10-25). No Discord confirmation step for a new
  passkey.** The owner accepts that a program running as the owner can forge
  the HUD's owner test (`projects.is_owner`), open a pairing window, pair a
  software passkey and so produce valid PH-4 assertions, which then carry
  the HUD's authority on the phone (O-3) while §4.1 holds. This is at parity
  with the desktop's forgeable `POST /approvals`; the owner sees the extra
  passkey in Settings > Phone and can revoke it. It answers §10.2's open
  item; nothing new is built for it.

## 0. What the owner gets

- **Jarvis on the phone as an installed web app**, reachable from anywhere
  over the owner's Tailscale network, not only on home Wi-Fi. It has three
  tabs:
  - **Inbox:** the same ranked list as the HUD's.
  - **Chats:** each project's threads, the transcript, and an input box.
  - **Tasks:** status, plan, the report, and the diff, read-only.
- **A notification when something waits on you**, and only then: an
  approval, a blocking question, a blocked task. It carries a count, never
  the command or any text from the chat.
- **Approving from the phone carries the same authority as the HUD** (O-3):
  Allow, Always and HUD-only Browser cards. Every Allow and every Always takes
  Face ID or the iPhone passcode (§2, PH-3). Deny is a single tap.
- **Answering questions and decisions, sending a message to a chat,
  starting a task,** all from the phone.
- **Push to talk** that turns your speech into text in the box. You read
  it and press send. Speech never sends or answers by itself.
- **What stays at the desk:** archive and delete, settings, routing, the
  Bypass switch, permission profiles, terminals, the editor, and driving the
  Browser.

Why this rather than more Discord: Discord already gives a channel per
project, a thread per chat, and Approve/Deny buttons (`docs/notes/discord.md`).
It cannot show a diff, rank what waits on you, answer a HUD-only card, prove
it is you pressing Approve, or render a transcript the way the HUD does.
Discord stays unchanged as the backup (O-2); the phone is the richer surface
beside it.

Why an installed web app rather than an App Store app: on current iOS a web
app added to the home screen gets passkeys (Face ID or passcode), Web Push notifications,
its own icon and a full-screen window, which is everything this design uses.
A native app would need Xcode on a Mac, an Apple Developer membership
($99 a year) and App Store or TestFlight distribution, for no capability this
design needs. If a later feature needs one (widgets, Live Activities, Siri),
the native app would be a thin shell over the same `/m/api`.

## 1. What this builds on

- **The daemon binds only loopback** (`daemon.py`: `127.0.0.1` for the API
  listener 8405, `FACE_PORT` 8402 and `WORKSHOP_PORT` 8403) inside WSL2,
  which runs in NAT mode on this PC (no `networkingMode` in `.wslconfig`).
- **The owner test is loopback-shaped.** `projects.is_owner` requires the
  HUD listener plus `Origin: http://<Host>` or a same-origin plain read, and
  `hud_api.check_origin` refuses any non-loopback `Host`. Its own note says
  this is a speed bump, not a boundary. The boundary is that no tool reaches
  owner routes and every agent command passes the gate. A phone fails this
  test by construction, which is right: the phone needs its own test.
- **Tailscale is already installed and running on Windows** with MagicDNS.
  No phone is on the tailnet yet. The one other device on it belongs to a
  family member and is trusted (O-4). Even so, being on the tailnet never by
  itself means "is the owner" (§2, PH-3): a device can be shared, borrowed or
  compromised.
- **The HUD already installs as an app** (`hud/public/manifest.webmanifest`,
  #83), and it uses the Graphite theme and a sanitized markdown renderer.
- **Approval timeouts already know about remote owners:**
  `PendingApprovals(remote=True)` waits 600 s instead of 120 s.
- **The `C:\myday` hub** (`C:\myday\hub`) is a separate, Windows-side always-on
  Claude session on Remote Control that launches Claude sessions and
  headless Codex jobs (`cx.py`). It exists because there was no way to reach
  this PC's agents from the phone.

## 2. Decisions (planner's defaults, each reversible)

- **PH-1. A fourth listener, `REMOTE_PORT` 8408, on loopback** (not 8406,
  which is `SPOTIFY_AUTH_PORT`, the Spotify consent callback whose redirect
  URI is registered by hand; 8407 is the trading firm). P-1 adds it to
  `config.DEFAULT_CONTROL_PORTS` and to `CONTROL_PORTS` in
  `hud/src/lib/devopen.ts`, which mirrors it, so the agent Browser refuses
  it. It serves only the phone app and the phone API. It is never the HUD listener, and
  `is_owner` is never true on it. It shares no route table entry with the
  HUD unless that route is listed in §5.
- **PH-2. Reachability is `tailscale serve` on Windows**, terminating HTTPS
  with the tailnet's certificate at `https://<machine>.<tailnet>.ts.net` and
  proxying to `127.0.0.1:8408`, which WSL2's localhost forwarding carries
  into the VM. Nothing is bound to a LAN or public address. **No Tailscale
  Funnel**: the surface is not on the public internet.
- **PH-3. The owner on the phone is proved by a passkey, not by the network.**
  Pairing registers a WebAuthn credential (`userVerification: required`)
  on the phone for the relying party `<machine>.<tailnet>.ts.net`. Being on
  the tailnet only gets a request to the login page. User verification is
  whatever iOS accepts for the passkey: Face ID, or the device passcode
  when Face ID is off or fails. Both set the UV flag, and Jarvis neither
  knows nor cares which was used. An iPhone with no passcode cannot hold a
  passkey, so pairing needs one set. Tailscale's identity
  headers are logged as advisory, never trusted, because a local process
  can send them to 8408 directly.
- **PH-4. Every Allow and every Always is its own passkey assertion.** The
  challenge is bound to the approval and the exact answer:
  `sha256(req_id ‖ "allow" | "always" ‖ server nonce)`, single-use, valid
  60 s, so an assertion for Allow cannot be replayed as Always. The server
  verifies origin, rpId, the UV flag and, when the authenticator keeps
  one, the signature counter, before it calls `PendingApprovals.resolve`.
  iCloud Keychain passkeys always report 0 (P-0), so a counter of 0 is
  accepted while the stored counter is also 0; once a credential has
  reported a nonzero counter, each new one must be greater (`webauthn`'s
  own rule). For iCloud passkeys the counter is therefore no defence against
  a cloned key. Deny needs only the session. This is
  "deny cheap, authorize deliberate", made physical. A program running as the
  owner can forge any header, but it cannot produce this assertion **with a
  passkey it does not hold**: it cannot use the owner's iCloud passkey,
  whose private key never leaves the owner's devices and needs Face ID or
  the passcode. It can, though, pair a passkey of its own (§10.2: it can
  forge the HUD's owner test and open a pairing window) and then produce
  valid assertions with it. The owner accepted that residual (O-5): it is at
  parity with the forgeable desktop `POST /approvals`, and the extra passkey
  shows in Settings > Phone, where it can be revoked.
- **PH-5. Sessions are short and narrow.** After a passkey login, the phone
  holds a `__Host-` cookie (HttpOnly, Secure, SameSite=Strict, 12 h idle,
  7 days absolute). Every write also needs `Origin` equal to the ts.net
  origin. Logging out, or revoking the passkey in the desktop HUD, ends its
  sessions at once.
- **PH-6. Pairing starts only at the desk.** An owner-only HUD route opens a
  pairing window (5 minutes, one registration) and shows a QR code holding a
  one-time code. The phone opens the URL, enters or scans the code, and
  registers a passkey. **What is paired is the passkey, not a device**
  (P-0): an iCloud Keychain passkey syncs to every Apple device on the
  owner's Apple ID, and each of them can use it once it is on the tailnet.
  The HUD lists paired passkeys with their sessions (user agent and
  last-seen per session) and Revoke, which ends every session that passkey
  opened. At most 3 passkeys. A limit on devices cannot be enforced and is
  not claimed.
- **PH-7. Phone approvals equal the HUD's (O-3); phone messages do not
  carry desk authority.**
  - **Approvals.** `PendingApprovals.resolve`'s `hud=True` becomes
    `owner_verified=True`, set by exactly two callers: the HUD route
    (`projects.is_owner`) and the phone route after a valid PH-4 assertion
    under a secured network (§4.1). Nothing else may set it: Discord, the
    inbox's dispatch from any other listener, and tools all pass False.
    With it, the phone may Allow, Always, and answer HUD-only Browser cards.
  - **Always from the phone** shows the rule that would be written (what
    `entry_for` mints) on the card before the iOS verification prompt, because a
    standing rule outlives the approval.
  - **HUD-only cards** send their private detail (`owner_json`) only to a
    verified phone session, only while the card is pending, and never in a
    push payload, a log, or the `/m/ws` stream. The phone fetches it when
    the card is opened.
  - **Messages** are `UserMessage(origin="owner", via="phone")` with desk
    false, the same footing as Discord: a chat may act on them only through
    the §6 gate, never through fast-path desk edits (F-1..F-7). O-3 is about
    approvals; desk authority for the phone is §11 question 2.
  - **Owner decision required to build:** `approvals.py` and
    `permissions.py` are SELF_PROTECTED and this changes who may answer a
    HUD-only card. O-3 is that decision; the change still goes through the
    boundary review the Safety design requires.
- **PH-8. Notifications are Web Push with no content.** The daemon signs
  with a VAPID key it holds (a protected path, mode 600) and sends
  `{count, rank}` only. The push service (Apple or Google) never sees a
  command, a title or a project name. The phone fetches the inbox after it
  opens. Push follows Discord's ping rule: one per attention event (approval,
  blocking question, blocked or failed task), never per progress line, with
  a 60 s coalescing window.
- **PH-9. Separate frontend, shared parts.** A second Vite entry,
  `hud/mobile.html`, built into the same `dist/`. It shares the approval
  card, the markdown renderer and the Graphite tokens. The desktop HUD's
  panes, editor, terminals and Browser are not in the phone bundle.
- **PH-10. Remote approvals wait 600 s while a phone is paired and active**
  (a session seen in the last 24 h), the same as with Discord attached.

## 3. Architecture

```
 phone (installed PWA)
   │ HTTPS, tailnet only
   ▼
 Windows: tailscale serve  https://<machine>.<tailnet>.ts.net  →  127.0.0.1:8408
   │ WSL2 localhost forwarding
   ▼
 WSL: jarvis daemon
   ├─ REMOTE listener 8408 ── remote_auth (passkey, sessions, Origin)
   │     ├─ static: mobile bundle (strict CSP, frame-ancestors 'none')
   │     ├─ /m/api/*  (§5)            → the same functions the HUD uses
   │     └─ /m/ws     event fan-out, filtered to inbox_changed + thread updates
   ├─ HUD listener 8402 (unchanged; adds owner-only pairing and device routes)
   ├─ API listener 8405 (unchanged; never serves /m/*)
   └─ push sender (VAPID) ← inbox_changed
```

New modules: `jarvis/v2/remote_auth.py` (WebAuthn registration and
assertion, sessions, devices; dependency `webauthn`), `jarvis/v2/remote_api.py`
(the route table in §5), `jarvis/v2/push.py` (subscriptions and VAPID send;
dependency `pywebpush`). All three join SELF_PROTECTED, because they decide
who the owner is off the box.

## 4. The owner test on the remote listener

`remote_auth.is_remote_owner(handler)` is true only when all of these hold:

1. the request arrived on `REMOTE_PORT`;
2. `Host` is exactly the configured ts.net name (strict, like
   `check_origin`);
3. a valid, unexpired, unrevoked session cookie names a paired passkey;
4. for anything but GET or HEAD, `Origin` is exactly
   `https://<machine>.<tailnet>.ts.net`;
5. for an Allow, a fresh assertion as in PH-4.

A request that fails any of these is refused with no body detail: a `Host`
other than the ts.net name gets 400 (item 2), a write without the exact
`Origin` gets 403 (item 4), and a `/m/api` request without a valid
session gets 401 (item 3; as built in P-1, every `/m/api` request). The login
page and the static bundle are the only things served without a session.

### 4.1 "The network stays secured": the conditions for full authority (O-3)

The phone answers with HUD authority (`owner_verified`) only while **all** of
these hold. The daemon checks them at start-up and every 5 minutes, and
again before each verified answer:

1. **The remote listener is bound to loopback only** (checked from the
   socket itself, not the config).
2. **Tailscale serve is the only path, and Funnel is off.** The daemon reads
   `tailscale serve status --json` through the Windows CLI (read-only) and
   requires exactly one handler for the ts.net name, pointing at
   `127.0.0.1:8408`, with no Funnel entry for any port.
3. **The certificate is the tailnet's own**, valid for the ts.net name, and
   the request's `Host` and `Origin` match it (§4, items 2 and 4).
4. **The passkey is paired, unrevoked, and requires user
   verification**, and the assertion is fresh (PH-4).
5. **Nothing else is listening on 8408**, on any interface, in WSL or on
   Windows (`ss` in WSL, `Get-NetTCPConnection` on Windows, read-only).

If any condition fails, the phone keeps working at **Discord-level authority**
(Allow and Deny for ordinary cards; no Always; HUD-only cards say "answer
at the desk"). The phone shows a red "limited: <which check failed>" line,
and the desktop HUD's status shows the same. Getting full authority back needs
nothing but the condition passing again. No setting widens it.

**The agent path.** An agent's shell could `curl 127.0.0.1:8408`. Without a
session cookie that is a 401. The cookie lives in the phone's browser and is
never written to disk on the PC. Defence in depth: `rules.py` DENYs any
command naming port 8408 or the remote listener's route prefix, the same
treatment as the daemon-kill rule, and the session MCP catalog gains no
remote routes.

## 5. What the phone can call

| Route (`/m/api/...`) | Calls | Notes |
|---|---|---|
| `GET inbox` | `inbox.rows()` | private approvals in their private projection |
| `GET inbox/<id>/detail` | `PendingApprovals.owner_details` | HUD-only cards, verified session under §4.1 only, while pending |
| `POST inbox/<id>/answer` | the inbox dispatch (inbox IN-2) | Allow and Always need the PH-4 assertion and §4.1; Deny needs the session only |
| `POST inbox/decisions/<id>` | inbox decisions | |
| `GET projects`, `GET threads?project=`, `GET threads/<id>/log?after=` | the HUD's reads | transcript rows as the HUD shows them, scrubbed (`chat_reads` projection) |
| `POST threads/<id>/messages` | `daemon.send` with `via="phone"` | queues behind a running turn; attachments refused in v1 |
| `POST threads` | new chat in a project | provider, model and effort chips as the HUD's |
| `GET tasks`, `GET tasks/<id>`, `GET tasks/<id>/diff` | the HUD's reads | diff read-only, size-capped |
| `POST tasks/<id>/cancel`, `POST tasks/<id>/resume` | `TaskControl` | cancel needs a confirmation tap |
| `POST stt` | the existing speech-to-text path, owner-only on this listener | returns text only; nothing is sent |
| `POST push/subscribe`, `DELETE push/subscribe` | `push.py` | one subscription per device |

Not on the phone in any version without a new owner decision: archive,
delete, rename, settings, routing, model roster, Bypass, permission
profiles, the always-ask list, terminals, the editor, the Browser and its
stream, dev servers, VEX, project folder creation, schedules editing.

## 6. The phone app

- **Inbox tab:** the inbox rows, the same rank groups as the HUD. A card
  shows in full before any button works; pressing Allow or Always raises
  Face ID or the passcode. Always shows the rule it would write first. Pull to refresh; live
  updates over `/m/ws`.
- **Chats tab:** projects → threads (the five most recent per project, as
  the desktop sidebar shows) → transcript. The input box has push to talk:
  hold to record, release to transcribe, the text lands in the box in
  REVIEW mode only.
- **Tasks tab:** running first, then blocked, then the rest. A task shows
  state, plan, the last report and the diff, with file-by-file folding.
- **Install and offline:** an installed app with its own manifest
  (`/m/manifest.webmanifest`, scope `/m/`). There is no offline write queue:
  without a connection the app shows "offline" and nothing it shows is
  presented as current.
- **Theme:** Graphite, same tokens. Touch targets at least 44 px. Allow and
  Deny are far apart, and Deny is never the default.

## 7. Notifications in detail

- **Subscribe** after pairing, from a button (browsers require a user gesture;
  iOS requires the app to be installed to the home screen first).
- **Sends:** on `inbox_changed` when the count of ranks 1 to 4 rises, at most
  one push per 60 s, coalesced. The payload is `{"count": n, "rank": r}`, and
  the notification reads "Jarvis: 2 waiting" (rank 1 adds "an approval
  expires soon").
- **Quiet hours:** a desk setting. During them only rank 1 pushes, and only
  if the approval came from a task the owner started that day.
  **Superseded by the owner's answer (2026-10-28, §11 question 5):** 23:00
  to 07:00 local, on by default, and nothing pushes during them, rank 1
  included; at 07:00 one push gives the count if anything at ranks 1 to 4
  still waits (§10.5).
- **Failure:** a subscription the push service reports gone (404/410) is
  deleted; any other failure is logged with status only, never the endpoint
  URL (the endpoint is a capability, like Discord's interaction URL).

## 8. The `C:\myday` hub

The phone surface plus Jarvis tasks cover most of what the hub was built for:
starting Claude and Codex work on this PC from the phone, and watching it.
What the hub has that this does not: Claude's own Remote Control UI, and
Windows-native sessions (Jarvis works `/mnt/c` projects from WSL). Proposed
path, each step the owner's call:

1. Ship P-1 to P-5. Use both for two weeks.
2. Move Codex jobs from `cx.py` to Jarvis Codex tasks, which already have
   worktrees, the gate, the usage ledger and review.
3. Keep or retire the hub's Remote Control session for Windows-side work
   in `C:\myday`. Nothing here forces it.

## 9. Safety: each Safety-design bullet this touches

| Rule | How the phone keeps it |
|---|---|
| Approvals are the owner's, deliberately | PH-4: a user-verified assertion (Face ID or passcode) bound to each Allow and each Always; no bulk Allow (one recorded exception, §10.10: the morning review's Approve selected for deferred rows, ≤10, one assertion bound to exactly that set, all §4.1 checks, all or nothing); Always shows its rule first |
| HUD-only cards answered only by a verified owner surface | `owner_verified` set only by the HUD route and by the phone under §4.1 with a fresh assertion (PH-7, O-3); private detail never in push, logs or the stream |
| No tool reaches owner routes | remote routes need a session no agent holds; `rules.py` DENY for 8408; nothing in the session MCP catalog |
| Credentials never leave the daemon | VAPID private key and WebAuthn data are protected paths; push payloads are content-free; logs carry status codes only |
| Nothing fetched speaks for the owner | the phone renders transcripts as the HUD does: scrubbed, untrusted, `textContent` for cards |
| Desk authority is the HUD's | `via="phone"`, desk false |
| Unknown or malformed fails closed | bad Host → 400, bad Origin → 403, bad session or assertion → 401; an expired challenge → 409 and a fresh one |
| Network exposure | loopback bind; tailnet only via `tailscale serve`; no Funnel; checked continuously (§4.1), and full authority drops to Discord level when any check fails |

## 10. Work packages

| WP | Package | Size | Acceptance |
|---|---|---|---|
| P-0 | **Spikes, each a script under `tests/spikes/` with its finding written here.** (a) `tailscale serve` on Windows reaching a WSL loopback listener on 8408, with a valid cert, and `tailscale serve status --json` readable from WSL; (b) a passkey registered and asserted with Face ID, and again with Face ID off using the passcode, from the owner's iPhone, in the home-screen app, on the ts.net origin, verified by `webauthn`; (c) a Web Push reaching the home-screen app on the iPhone with the screen locked | S | all three pass on the owner's iPhone; any failure stops the plan and comes back as a decision |
| P-1 | `REMOTE_PORT` listener, strict Host and Origin, CSP, static serving of an empty mobile shell | S | the API and HUD listeners never serve `/m/*`; the remote one never serves HUD routes (route-table test); non-ts.net Host refused |
| P-2 | `remote_auth`: pairing from the HUD, passkey registration and login, sessions, paired passkeys, revoke | M | pairing only within an open window; revoke ends sessions at once; replayed or cross-origin assertions refused; `rules.py` DENY for 8408 |
| P-3 | Read API and the Inbox, Chats and Tasks tabs (read-only) | M | headless mobile-viewport suite; transcripts identical to the HUD's projection; private cards show their private projection until P-4 lands |
| P-4 | Answering: approvals with per-answer assertion, `owner_verified` in `approvals.py`, the §4.1 security monitor, questions, decisions | M | Allow or Always without a fresh assertion refused; an Allow assertion replayed as Always refused; HUD-only card answered from a verified phone, refused from Discord and from a phone with any §4.1 check failing (one test per check); `owner_detail` never appears in push, logs or `/m/ws` (asserted); Deny works with session only; the card suite passes on the phone bundle; boundary review on the `approvals.py` change |
| P-5 | Sending messages, new chats, task cancel and resume | S | `via="phone"` recorded; desk false (fast-path suite); queues behind a running turn |
| P-6 | Web Push | S | payload is content-free (asserted); coalescing; 410 deletes; quiet hours |
| P-7 | Push to talk | S | transcription lands in the box; nothing sends without a tap; a voice note never answers a question |
| P-8 | Hub migration notes and docs | S | CLAUDE.md gets a Phone section; `C:\myday\hub\README.md` gets a pointer once the owner decides §8 step 2 |

Order: P-0 → P-1 → P-2 → P-3 → P-4 → P-6 → P-5 → P-7 → P-8. The inbox's IN-0 to
IN-2 must land before P-3.

### 10.1 P-0 findings (2026-10-25)

The phone was the owner's iPhone (Tailscale lists it as `iphone-12`), on
iOS 26 by the owner's report, on LTE (the owner's screenshot of the
Tailscale app). The run's user-agent lines were lost to the spike bug below.

Scripts: `tests/spikes/p0_network.py` (a) and `tests/spikes/p0_phone.py` (b, c).
Server-side log of the run: `~/.cache/jarvis-p0/findings.jsonl` on the PC.

- **(a) Network: pass, 5 of 5.** `tailscale serve --bg --https=443
  http://127.0.0.1:8408` on Windows reaches the WSL listener through WSL2's
  localhost forwarding (NAT mode, no `.wslconfig` change). The listener is
  loopback-only in WSL, and on Windows only `wslrelay` listens on 8408, on
  loopback. `tailscale serve status --json` is readable from WSL through
  the Windows CLI, with one handler and Funnel off. Two one-time owner steps were
  needed: enabling Serve for the tailnet in the admin console, and waiting
  for the first certificate. The first HTTPS request after enabling timed out
  at 15 s while the certificate was issued; it answered within 90 s and
  immediately from then on. (The setup steps and timings are from this
  session's own run, not the findings log.) The run used port 8406; the
  spike moved to 8408 after review found the Spotify collision (PH-1), and
  check (a) was re-run there: 5 of 5.
- **(b) Passkey: pass.** Registered in the home-screen app with
  `userVerification: required`. It is stored in iCloud Keychain
  (`backed_up: true`, multi-device). Two assertions, labelled "faceid"
  and "passcode" by the button pressed, both verified with `user_verified:
  true`. **The server cannot tell which method was used** (both set the UV
  flag; the label is only the button), and the two were 11 s apart, so the
  log alone does not show the passcode path. **Re-run for the passcode
  (06:05 UTC, on 8408):** the page opened as the home-screen app (logged:
  `standalone: true`, Safari `Version/26.6.1`; iOS freezes the OS token in
  the user agent at `18_7`). With the front camera covered so Face ID
  failed, **iOS asked for the passcode, not Face ID** (the owner's report),
  and the assertion verified 8 s after the page opened with `user_verified:
  true`. So the passcode satisfies PH-4 exactly as Face ID does. Replaying a
  used assertion, as "allow" or as "always", was refused.
  - **Signature counter is always 0** for these passkeys. PH-4 is amended:
    a zero counter is accepted while the stored one is zero (PH-4).
  - **The passkey syncs to every Apple device on the owner's Apple ID.**
    Any of those devices can approve, once it is on the tailnet and opens the
    paired origin. Pairing records the credential, not the device, so
    "Revoke" in the HUD (PH-6) revokes the passkey everywhere at once.
  - **Not exercised:** an unused "allow" assertion submitted as "always".
    The replay hit the single-use check first. P-4 tests the answer binding
    with a software authenticator (`webauthn`'s test helpers or a fake), since
    the binding is server logic and needs no phone.
- **(c) Web Push: pass on the server side.** The home-screen app subscribed
  through `web.push.apple.com`. A content-free payload, `{"count": 1, "rank":
  1}`, signed with the spike's VAPID key and sent 20 s later, was accepted
  (HTTP 201), and **the notification showed on the locked iPhone about 20 s
  later** (the owner's report). For `rank: 1` the service worker builds the
  text "1 waiting - an approval expires soon" under the title "Jarvis", so
  that is what was shown. Opening the app from the tap was not recorded (the
  spike bug below).
- **Spike bug, fixed after the run:** the page's own client log lines
  ("opened as home-screen app", "opened from the notification") crashed the
  log route, because a field named `event` collided with the logger's own
  argument. Those lines were lost for this run. That the subscription
  succeeded shows the page ran as the home-screen app, since iOS offers push
  only there.

Verdict: P-0 passes. P-1 may start.

### 10.2 P-2 as built (2026-10-25, phone C2)

Built as the P-2 row says (`docs/notes/control-plane.md`, "The phone's
passkeys, pairing and sessions"). Where it is more specific than the text
above:

- **A registration never signs in.** Pairing registers the passkey; the phone
  then signs in with an assertion. With `none` attestation a registration
  proves nothing about the private key, so only an assertion makes a session.
- **Login options name no credential** (`allowCredentials` empty): the
  passkey is discoverable (`residentKey: required`), and no credential id is
  handed to whoever asks.
- **Status codes:** a wrong, absent or expired pairing code is 401, like a bad
  assertion; a 4th passkey is 409; an unknown, spent or expired challenge is
  409 (§9); passkey state the daemon cannot read is 503.
- **Revoked passkeys are kept** (at most 16, marked) so a revoked credential
  id stays refused; the HUD lists only active ones.
- **The 8408 DENY** refuses a loopback host joined to the port anywhere on
  the line (whatever program holds it), socket paths to it, the port with a
  loopback spelling and a connector apart, and `/m/api` in a URL to this
  machine on any port or to a `ts.net` name. Bare mentions (the number, a
  hostless `:8408`, `REMOTE_PORT` as a word) are not refused; a loopback URL
  in a commit message is.
- **Revoke is durable only once written.** It holds in memory at once; if the
  passkeys file cannot be written the HUD shows "revoked, not saved" and the
  write is retried on every later call and once more at `Daemon.stop()`
  (an ERROR names the passkey if that fails too). A restart before it lands
  loses it: accepted and documented.
- **Open: a program running as the owner can pair a passkey.** It can forge
  the HUD's owner test (`projects.is_owner`: a raw HTTP client sending
  `Origin: http://<Host>` to the HUD port), so it can open a pairing window,
  register a software passkey (no screen, no phone needed) and sign in. That
  is at parity with today's forgeable desktop `POST /approvals`, which the
  owner test already concedes ("a speed bump, not a boundary") — but it
  leaves a **lasting** credential, and P-4 (PH-4) means that credential would
  then produce valid per-answer assertions: PH-4's "a program running as the
  owner … cannot produce this assertion" does not hold once such a program
  has paired its own key. The owner sees the extra passkey in Settings >
  Phone and can revoke it, but nothing stops the pairing itself. **The
  remedy is pending an owner decision**; nothing here implements one.
  **Decided 2026-10-25 (O-5): accepted, no remedy built** (no Discord
  confirmation step); PH-4's wording now states the residual.

### 10.3 P-3 as built (2026-10-25, phone C3)

Built as the P-3 row says (`docs/notes/control-plane.md`, "The phone's read
API and tabs"). Where it is more specific than the text above:

- **The reads are `jarvis/v2/remote_api.py`** (SELF_PROTECTED), reached only
  past `remote_auth.owner_status`: GET only (405 otherwise, HEAD included),
  404 for anything not in its table. Each calls the HUD route's own function
  and keeps an allowlist of fields; `remote_api.DROPPED` lists, with reasons,
  what the HUD sends and the phone does not (paths, permission
  configuration, other surfaces' addressing, an approval's 4-character code,
  provider session handles), and the check holds each route equal to the
  HUD's minus exactly that, and the inbox's row and detail fields against
  pinned constants (`ROW_FIELDS`, `APPROVAL_DETAIL_FIELDS`, `DETAIL_FIELDS`,
  `GENERIC_DETAIL_FIELDS`). Every string passes `chat_reads.scrub`, an
  approval card's too (review of PR #308: `card_args` misses a private key's
  body in an argument, and a card's `origin` and `relayed_from.title` are
  only `clean_line`d), so the phone's card can withhold more than the desk's:
  **P-4 must answer from the full shared card**, not from this read-only view
  (which also cuts a command at 4,000 characters and hides the arguments when
  there is a command). A task status's `last_file` (a worktree path) is
  dropped. The session is checked again before an answer leaves.
- **The transcript is `chat_reads`' projection** (§5), paged by `after=<seq>`
  (the log-record index) with `next` as the cursor. Without `after`, the
  newest 60 rows; the phone does not page back (older rows are at the desk).
- **The stream is SSE at `/m/api/events`**, not a WebSocket at `/m/ws`: the
  HUD's own stream is SSE (`/events`), and under `/m/api` it inherits the 401
  and the agent-path DENY without a new rule. It carries
  `inbox_changed {ids, reason}`, `thread_changed` and `task_changed` (ids and
  a source kind) and `resync`, never text. `task_changed` is an addition to
  §3's "inbox_changed + thread updates": the Tasks tab's live updates need
  it, and it carries ids only. It ends with its session (logout, a revoke,
  expiry) within a second, through a check that does not count as activity.
- **The diff is the Diff tab's, made stricter**: its patches also pass
  `chat_reads.scrub`, because the v1 scrub the Diff tab uses lets a GitHub
  token in a changed file through (the desk still shows it; a finding for
  the Diff tab, not changed here). Capped at 200 files, 256 KiB a file and
  1 MiB in all, with "Too large to show here: open it at the desk."
- **The CSP is C1's, unwidened.** Assistant replies use the HUD's sanitized
  renderer; the phone then drops KaTeX's styled HTML layer and every `style`
  attribute, keeping the MathML layer iOS draws natively. The shared
  renderer still builds KaTeX's styled spans before that, so the browser
  reports (and refuses) `style-src-attr` for each formula: harmless, and the
  alternative was `'unsafe-inline'` for styles.
- **Private (HUD-only) approval rows show their private projection** and
  "a private card: its detail is shown only at the desk"; the §4.1
  `detail` fetch is P-4's.

### 10.4 P-4 as built (2026-10-25, phone C4)

Built as the P-4 row says (`docs/notes/control-plane.md`, "Answering from
the phone"). Where it is more specific than the text above:

- **`owner_verified`** replaces `resolve(…, hud=)` in `approvals.py`, with
  the same refusal. Its True comes only from the HUD's two answer routes
  (`POST /approvals/<id>`, `POST /inbox/<id>/answer`, the HUD listener's own
  Host after `owner_only`) and from `remote_answer` after a verified
  assertion while all five §4.1 checks pass; a test greps every assignment.
- **PH-4's challenge** is `sha256(domain ‖ req_id ‖ answer ‖ nonce)`, each
  part delimited, bound server side to the session and its passkey too, at
  most 4 per session and never a shared cap (a repeat for the same card and
  answer replaces the earlier one). The assertion is verified before
  anything else, then §4.1 runs in full; a failure after the assertion
  still refuses.
- **What the phone's scrub withholds** is read over the card's structure
  too: keys, each value with its key, and the card as one JSON text; and a
  value nested past the card redaction's depth withholds the card. A
  withheld card is Deny only. At the desk, a card with such a value offers
  DENY only as well (a small change to the desk, failing closed).
- **§4.1** is `remote_secure.py`. Condition 3, "the certificate is the
  tailnet's own", is read from Tailscale rather than from a TLS handshake:
  the host is this machine's MagicDNS name, under the tailnet's `.ts.net`
  suffix, in `CertDomains`, and serve terminates HTTPS on 443 (serve uses no
  other certificate); and an answer's own Host and Origin are the host's.
  Condition 5 on WSL also requires the one listener's socket inode to be the
  daemon's own. The five run at start-up, every 5 minutes, and in full
  before every detail, options and answer; the last result is only shown
  (`/status` `remote.secure`, `/m/api/me` `secure`). Only daemon2's
  `main()` attaches the Windows reads, so a test or embedded daemon is
  limited.
- **Limited** answers an ordinary card's Allow with `owner_verified=False`
  (Discord's authority) and refuses Always and every HUD-only Allow (409
  "answer at the desk"). Deny always works, a HUD-only card's included.
- **The card** is the HUD's projection (`to_json`, or `owner_details` for a
  HUD-only card), sent through the phone's scrub uncut; if the scrub changes
  any field, Allow and Always are refused ("this card has withheld text:
  answer it at the desk"). The phone draws it with the HUD card's words
  (`hud/src/lib/approvalCard.ts`, which the HUD's own card now uses),
  not the React component itself: the phone bundle has no React, and the
  HUD component reads HUD-only routes (a sub-agent's name, the Browser
  card's `/approvals/<id>`). The sub-agent line says "from a sub-agent"
  without its name.
- **Always** shows `PendingApprovals.always_rule` (a dry run of
  `resolve(always=True)`) and asks for Confirm before the passkey.
- **Questions and decisions**: B1's decisions are answerable on the phone
  (`via="phone"`, B1's framing and queue, no desk authority, no Bypass); a
  chat question's log record names "phone"; a task question goes through
  `TaskControl.answer_question`, which takes no `via`. A proposal's
  withdraw stays at the desk (P-5 brings task verbs; it did, §10.x P-5).
- **PH-10**: `PendingApprovals.wait_s()` waits 600 s while a phone session
  was seen in the last 24 h; a HUD-only card keeps its 120 s cap (its
  Browser deadline is bound to it), so the phone has 2 minutes for those.
- **Not built here:** Web Push (P-6), so "never in a push" holds trivially;
  the stream's shaper, which a push would read, is asserted to carry no
  private detail.

### 10.5 P-6 as built (2026-10-28, phone C6)

Built as the P-6 row says (`docs/notes/control-plane.md`, "Web Push to the
phone"), with quiet hours as the owner answered §11 question 5. Where it is
more specific than the text above:

- **Quiet hours are their own module**, `jarvis/v2/quiet.py`
  (SELF_PROTECTED; `<data root>/quiet.json`, protected state): enabled,
  start and end, default on 23:00 to 07:00 local, `is_quiet(now)` and
  `ends_at(now)`, changed only at the desk (`GET`/`POST /quiet`, owner-only).
  No push or HTTP import, so the next package (approvals asked at night)
  builds on it. P-6 changes no approval or timeout.
- **When a push is due:** a row new to ranks 1 to 4 (so the count rises;
  a row that only changes rank within them is not new). At most one per
  60 s; what arrives inside the window is one push at its end, with the
  count then, and none if nothing waits by then. Nothing during quiet
  hours; when they end (the sender wakes at `ends_at`), one push if
  anything at ranks 1 to 4 still waits. The desk's Notifications switch
  off: nothing.
- **The payload** is `{"count": n, "rank": r}`, `r` the most urgent rank
  waiting (0 for none: the desk's test with nothing waiting). The
  notification reads "n waiting", rank 1 adds " - an approval expires soon",
  0 reads "Nothing is waiting now". A tap focuses the app on the Inbox or
  opens `/m/#inbox`.
- **Subscriptions live as long as their passkey**, one per session (the
  same endpoint moves to a newer session): logout, the desk's End and a
  revoke delete it at once; a session that only expires keeps it, so the
  notification can still bring the owner back to sign in. At most 8.
  The app hands a subscription it holds to the new session at sign-in.
- **The endpoint check** runs at subscribe and at every send: `https`, port
  443, no userinfo, a push service's host (`web.push.apple.com`,
  `*.push.apple.com`, `fcm.googleapis.com`, `*.notify.windows.com`,
  `updates.push.services.mozilla.com`, `*.push.services.mozilla.com`),
  never an IP literal, every resolved address global. The send connects to
  the address it checked (TLS verified for the host name): no proxy, no
  redirect, so DNS cannot change between the check and the connection.
- **What is protected (review of PR #325, MED-1):** `<data root>/remote/`
  is a credential folder (`config.remote_dir()` in
  `config.credential_dirs()`), so P-2's passkeys and sessions and P-6's
  `jarvis_push_key.json` (the VAPID private key) and
  `jarvis_push_subscriptions.json` are read by no agent through any
  spelling: v1's file and shell tools refuse it (none of its files is
  "readable config"), v2's read tools are refused there and v2's shell
  reads are always asked (`credentials-path`). The push files and
  `push_settings.json` are protected state too (no agent write). The two
  secret files are also refused by exact name anywhere (never as a glob,
  so `git tag -l 'v*'` and `ls *push*` keep main's verdict) and scrubbed
  from v1's tool output (delta review MED-A); v2's read tools are refused at
  the folders holding the phone's folder below `$HOME` (LOW-A). A search
  from `$HOME` or above is the limit every credential folder shares.
- **Bounded and retried (review of PR #325):** one push, every
  subscription, ends within 15 s (names resolved within 5 s on their own
  thread; the stop flag checked between subscriptions and addresses), and
  `stop()` waits for it. A push that reached no phone is tried again after
  1, 5, then every 15 minutes, still coalesced and silent in quiet hours.
  The payload is padded to 48 bytes. A start in quiet hours or within 30
  minutes of their end still sends the morning push. The VAPID `sub` stays
  the app's https origin, which Apple accepted in P-0 (a `@localhost`
  mailto is reported refused by Apple). The phone drops a subscription made
  for another key and asks for notifications again.
- **The service worker** is a third Vite entry (`hud/src/mobile/sw.ts`),
  served at `/m/sw.js` from the build manifest's entry; no fetch handler,
  no cache. The CSP is unwidened (a worker falls under `script-src 'self'`).
- **Only `main()` attaches the sender** (`Push.attach_system`); a test or
  embedded daemon never sends, and the first read after start sends
  nothing for what already waits.
- **Dependency:** `pywebpush>=2.5.0,<3` (built and tested on 2.5.0;
  `py-vapid` 1.9.6, `http-ece` 1.2.1), imported only when used.

### 10.6 P-5 as built (2026-10-28, phone C5)

Built as the P-5 row says (`docs/notes/control-plane.md`, "Writing from the
phone"). Where it is more specific than the text above:

- **The writes are `jarvis/v2/remote_write.py`** (SELF_PROTECTED), past
  `owner_status` like P-3 and P-4: `POST threads/<id>/messages`, `POST
  threads` (new chat), `GET thread-models`, `POST tasks`, `POST
  tasks/<id>/start|resume`, `POST tasks/<id>/cancel/options` and `POST
  tasks/<id>/cancel`. 400 malformed, 404 unknown or hidden, 409 wrong
  state, 429 past 30 writes a minute per session.
- **A phone message is `UserMessage(origin="owner", via="phone",
  desk=False)`** through the HUD's own `assemble_turn` (`paths=False`) and
  `Daemon.deliver`, the owner queue every surface shares. No desk (§11
  question 2), so no fast-path direct edits and no Bypass. Recorded `via:
  "phone"`; chat reads say "via the phone", the Discord mirror "You
  (phone)".
- **It never steers.** While a turn runs it queues behind it
  (`daemon.NEVER_STEER`, read from the message). §5's "queues behind a
  running turn" made a rule: steering stays at the desk.
- **Attachments and images are refused** (400), as §5 said for v1; the
  text is capped at 20,000 characters.
- **New chat:** a listed project and a provider (`fast`, `claude`,
  `codex`) with an optional model and effort, through `open_thread` as the
  HUD's `POST /threads`, then the first message. The sheet offers what
  `thread_model.describe()` lists, unavailable providers disabled.
- **Tasks:** `POST tasks` is the HUD's `POST /tasks` (`Daemon.create_task`):
  the task waits in INTAKE until Start (§11 question 1, the intake gate).
  Start and resume are `daemon.task_verb`, the HUD's own call, with the
  HUD's arguments. `remote_write.TASK_START` turns task making and
  starting off on its own.
- **Cancel takes a passkey assertion, not only the session** (a change
  from the P-5 brief, which assumed "cancel is reversible by resume"): a
  cancelled task is terminal (`model.TRANSITIONS`) and only a blocked task
  resumes, so cancel is irreversible and takes PH-4's footing. The
  challenge is P-4's, bound to `task:<id>` and "cancel"; an approval's
  assertion is never a cancel's, nor the reverse. §4.1 is not required: a
  cancel grants no authority. Start and resume need the session only.
- **A proposal's withdraw** moved to the phone (`remote_answer`), the
  session only: Deny's footing, inside the grace window. It calls only the
  router's withdraw; a window that closed meanwhile is 409, never a cancel.
- **Review of PR #324:** a `client_id` per draft makes a retry after a lost
  answer safe (5 minutes); a chat started from the phone is titled; provider
  and OS failures are 409 without their text; cancel and approval prompts
  are capped apart.
- **Every task verb from the phone is journaled** (`owner_verb`, `via:
  "phone"`).
- **Never on the phone:** permanent delete, archive, merge, rename, steer a
  task, worktrees, Bypass (none is a route; asserted).
- **The app:** a message box on an owner's chat (Send is a tap; Enter is a
  new line), "New chat" and "New task" sheets, Start/Resume/Cancel on a
  task each confirmed first (Cancel then the passkey), Withdraw on a
  proposal. Shown only after the 2xx; offline nothing is sent.

### 10.7 P-7 as built (2026-10-28, phone C7)

Built as the P-7 row says (`docs/notes/control-plane.md`, "Push to talk from
the phone"). Where it is more specific than the text above:

- **The route is `jarvis/v2/remote_stt.py`** (SELF_PROTECTED), `POST
  /m/api/stt`, past `owner_status` like P-3 to P-5: the session, the exact
  Host and the exact Origin (with the SameSite=Strict cookie, P-5's CSRF
  rule). The API and HUD listeners never serve `/m/`. The reply is
  `{text}` and nothing else.
- **It is the desk's path, through one function.** The desk's `POST /stt`
  now calls `hud_api.dictate`, and so does the phone (after
  `hud_api.watch_stt`, as the desk): `voice.stt_result(prefer_local=True)`
  under the owner's switches (`stt_settings`). Local only stays local: with
  the cloud fallback off, the phone's clip never leaves the machine, and a
  local failure is the desk's refusal ("… so nothing was uploaded"), never
  a quiet cloud call. The desk's statuses and sentences come back as they
  are (409 blocked, 502 unavailable). The phone sets nothing.
- **No speaker verification on the phone (owner, 2026-10-28).** Only a
  passkey-verified phone session can upload, and the phone's microphone
  would often refuse the owner's own voice. The phone calls `dictate` with
  `verify=False`: no voiceprint is read and no verdict is remembered
  (`speaker._remember`). The desk keeps verification exactly as before
  (422 for another voice, nothing transcribed). Asserted both ways: an
  enrolled, non-matching voiceprint, the phone's clip transcribed and the
  same clip at the desk refused.
- **The clip is WAV, built on the phone, not MediaRecorder's file
  (a change from the P-7 brief).** Safari's MediaRecorder on the iPhone
  writes `audio/mp4` (AAC). The desk's local engine takes 16-bit PCM WAV
  only and runs no media decoder on uploaded bytes by design
  (`stt_local`), and the desk's speaker verification takes WAV only, so an AAC clip
  could only ever have gone to the cloud, or been refused with the desk set
  to local. So the phone records with Web Audio (`getUserMedia`, a
  ScriptProcessor, the desk's resampler `lib/resample.ts`) and writes the
  desk's own WAV (`lib/vad.ts` `wavBytes`): 16 kHz, mono, 16-bit. The route
  takes `Content-Type: audio/wav` only (415 for mp4, webm or anything else)
  and checks the file itself (RIFF/WAVE, PCM, mono, 16-bit, 16 kHz, 1 frame
  to 60 s; 400 otherwise). No ffmpeg, nothing to install: the desk's STT
  handles it today, local and cloud. Not run on the owner's iPhone yet.
- **The phone uses a ScriptProcessor, not the desk's AudioWorklet.** The
  worklet is a separate build file that the build manifest does not list
  for `mobile.html`, so the remote listener does not serve it, and a
  `blob:` module would need a wider CSP. ScriptProcessor is deprecated but
  present in Safari.
- **Caps.** A clip is at most 60 s (`MAX_S`); the listener reads up to
  `remote_stt.BODY_CAP` (a minute of WAV plus a little, about 1.9 MB) for
  this one path, within a deadline scaled to the declared length (30 s plus
  2 s per 100 KB, at most 120 s: a full minute needs about 0.23 Mbit/s;
  review of #327), and only once the request already passes the owner test
  (`remote_listener._limits`). Every other request, a clip without a
  session included, keeps the 64 KiB cap and the 10 s deadline. 12 clips a
  minute per session (429, `Retry-After: 60`) and one at a time (429,
  `Retry-After: 2`).
- **Never kept, never logged.** The clip stays in memory: no temp file at
  all (the local engine reads it from memory; the cloud chain posts the
  bytes). INFO lines say only "transcribed" or "refused (status)".
- **The CSP is unchanged.** Recording is `getUserMedia` and Web Audio,
  which the CSP does not govern; nothing is played (no `media-src`); the
  upload is a `fetch` to `'self'` (`connect-src 'self'`); no `blob:` URL is
  made. The listener sends no `Permissions-Policy`, so the page may ask for
  the microphone; iOS still asks the owner.
- **The app** (`hud/src/mobile/talk.ts`, `talk.css`, in P-5's message box):
  "Hold to talk" (56 px tall, no scroll, no selection or long-press menu).
  Press: the mic is asked for then, inside the gesture, never before.
  "Listening · release" while held, "Transcribing…" after. Release adds the
  words to the chat's draft after what is typed and draws the box again:
  "Added to the box, unsent. Read it, then tap Send." Sliding more than
  24 px off the button, a cancelled pointer, or the app going to the
  background cancels, and nothing is uploaded. Under 0.3 s nothing is sent.
  At 60 s it stops itself and transcribes. Failures say which: permission
  denied, no microphone, offline, too slow, speech-to-text unavailable (the
  desk's sentence). A clip that never arrived (offline, the proxy with
  nothing behind it, a 408) is kept in memory for one "Retry", then
  dropped. The outcome line shows only in the chat the words went to. While
  that chat's send waits for its answer the mic is off, and a 202 removes
  only the words that were sent, so words a clip added meanwhile stay; P-5's
  "may not have been sent" note stays when words are added (review of
  #327). Every track is stopped and the audio context
  closed however the recording ends, a permission that arrives after the
  finger left included, so the iPhone's mic indicator goes out.
- **A voice note never answers.** The mic is only in an owner's chat's
  message box. Inbox rows, approval cards, questions, decisions, proposals
  and a task's chats have none, and the route calls no send, answer or
  approval (asserted, and by mutation).

## 11. Open questions for the owner (defaults in bold)

Answered 2026-10-24: the phone is an iPhone (O-1); Discord stays as a backup
(O-2); phone approvals equal the HUD's under §4.1, HUD-only cards included
(O-3); the other tailnet device is trusted, so no ACL change is needed (O-4).

1. **Should the phone be able to start tasks**, or only chat and answer?
   **Yes, through the same intake gate as the HUD.** **Answered
   2026-10-28 (owner): yes, through the same intake gate as the HUD**
   (built in P-5, §10.x).
2. **Desk authority from the phone:** may a message typed on the phone let
   a chat make small direct edits the way a HUD-typed one does (fast path
   F-1..F-7)? **No**: O-3 covers approvals, and each edit made that way
   would skip a card.
3. **A Tailscale ACL anyway?** Optional now (O-4). It would limit port 443
   on this machine to the owner's own devices. **Not required.**
4. **Session length:** **12 h idle, 7 days absolute.**
5. **Quiet hours:** **23:00 to 07:00**, or none. **Answered 2026-10-28:**
   23:00 to 07:00 local, on by default, one desk setting; nothing pushes
   during them, rank 1 included (the "task started that day" exception is
   dropped); when they end, one push with the count if anything at ranks 1
   to 4 still waits. Approvals that "wait until I wake up" are a separate
   package, not P-6.

### Critical files for implementation

`jarvis/v2/daemon.py` (listeners, `send`, `answer`), `jarvis/v2/projects.py`
(`is_owner`, as the model to not reuse), `jarvis/v2/hud_api.py`
(`check_origin`, CSP), `jarvis/v2/approvals.py`, `jarvis/rules.py` (the
8408 DENY), `jarvis/v2/provider.py` (`UserMessage.via`), `jarvis/v2/desk.py`,
`hud/vite.config.ts` (second entry), `hud/public/manifest.webmanifest`,
`pyproject.toml` (`webauthn`, `pywebpush`).

### 10.8 QH as built (2026-10-28, quiet-hours deferral)

Approvals asked during quiet hours are deferred, not held: the plan, the
owner's words and the as-built notes are
`docs/plans/2026-10-28-quiet-hours-deferral.md`; the rules,
`docs/notes/control-plane.md`, "Quiet hours: approvals deferred to the
morning". On the phone: a deferred row shows the card it would have shown;
Leave it needs the session; Approve and retry needs its own passkey
assertion (bound to `defer:<id>`, verified before anything is written),
then §4.1 in full and the Allow's gate. No push is sent for a deferral
during quiet hours (P-6's rule); a rank-4 deferred row counts toward the
one push when they end.

### 10.9 P-8 as built (2026-10-28)

- **The bare address opens the app.** On the remote listener, `GET /` and
  `HEAD /` (the request target exactly `/`, no query) answer `302` with
  `Location: /m/` (relative), an empty body, `Cache-Control: no-store` and the
  listener's usual headers. It runs after the strict Host check, so a wrong
  Host is still 400, and writes are unchanged (no Origin 403; with it, 404).
  `/x`, `/?x` and every other path are as before (`http.server` itself
  collapses a leading `//` to `/`, so that one redirects the same way). The API, HUD and
  workshop listeners gain nothing (asserted in
  `tests/v2/remote_listener_check.py`). `jarvis/v2/remote_listener.py` is
  SELF_PROTECTED, so an agent could not have made this edit.
- **CLAUDE.md has a Phone section** with the current rules (P-1 to P-7, QH).

### 10.10 MR as built (2026-10-28, the morning review)

The plan, the owner's words and four answers, and the as-built notes:
`docs/plans/2026-10-28-morning-review.md`; the rules,
`docs/notes/control-plane.md`, "The morning review". On the phone: a
Review tab beside the Inbox while the review has open items and the next
night has not begun (`hud/src/mobile/review.ts`). A launch, or a
notification's tap on an open app, reads `GET /m/api/review` and opens the
Review tab when `opens_here` (the last push was the 07:00 one, in the
daemon's memory), else the Inbox; the push payload and `sw.ts` are
unchanged. The tab: a summary line, one jump chip per chat, groups by
project then chat or task, each deferred card whole with a checkbox
(unticked until its own tap, no select-all), questions and decisions
answered in place (a decision's default marked, never chosen), and a
sticky footer with Leave selected (N) at the left and Approve selected (N)
at the right. Approve selected first lists exactly the ticked items, then
asks `POST /m/api/review/approve/options` for one challenge bound to
`review.subject` of those ids and tags, then Face ID once, then `POST
/m/api/review/approve` with the same items and the assertion. The daemon
verifies the assertion, runs §4.1 in full (a limited phone gets no group
approval: per row only), checks the set again under the store's lock, and
grants all or none; a 409 `changed` unticks what changed and says why.
Leave selected needs the session only. Every tap target is at least 44 px.


### 10.11 The chat layout and the compact notifications row (2026-10-28)

The owner, on the iPhone, asked for the standard mobile agent chat and chose
Claude/Codex style. UI, plus one small daemon change (below): no route or
rule changed.

- **The transcript** (`views.ts` `logRow`, `rowStyle`): no boxes. The owner's
  words typed in this chat (role `owner`, every mark one of via the phone,
  via Discord, via DM, spoken) in a right-aligned bubble with its marks and
  time under it; any other owner row (a decision's answer "not typed in
  this chat", a steer, one "not sent", an unknown mark) flat, its marks
  above the words, "not sent" in amber; a peer or a relay is never a
  bubble (review of #331, MED 1). Only in an owner's own chat: a task's
  threads (the runner's prompts are "owner" rows) and a thread not yet
  known draw every owner row flat. **The daemon change:** quiet hours'
  "The owner approved … retry it now" was delivered as origin "owner" via
  "phone" with nothing else to tell it from the owner's typing;
  `Daemon.deliver_queued` now takes `written_by` (as `decision_id`),
  `quiet_defer` passes "deferral", and `chat_reads` marks it "written by
  Jarvis (a deferred approval), not typed", `titles.owner_typed` is false
  for it and the Discord mirror says "Jarvis wrote" (delta review of #331).
  A reply (Jarvis, Claude, Codex)
  as plain full-width text through the same sanitized Markdown; every other
  row (the daemon's own lines, another chat's relay) one muted line, folded,
  that a tap opens to its whole role and then its whole text; the folded
  line cuts the middle of the role, never ", not the owner" (MED 2). Tool calls are still not in the
  projection (§5); nothing new is shown.
- **The message box** (`write.ts` `composerView`): one slim bar pinned above
  the tab bar: the box (one line, growing to five, then scrolling), the mic
  and a round Send arrow, lit only when there is text. Its notes ("Sent.",
  "Queued…", "Sending…", offline, the talk line) sit above it. Send's rules
  are P-5's unchanged: a tap only, never Enter; off while empty, offline or
  sending; NEVER_STEER at the daemon.
- **The mic** (`talk.ts`): P-7's "Hold to talk" button, now a 44 px round
  icon in the bar (it was a 56 px full-width button). Its words stay its
  text for screen readers. A touch counts as a hold only after 150 ms within
  the slop, so a brush while scrolling does nothing at all (LOW 5); the
  audio context alone starts in the pointerdown, inside the gesture, and is
  closed if the touch never becomes a hold; every
  other hold, slide-off, cancel, permission and upload rule is unchanged.
  Send redraws the box in place, its tap does not take the focus, and
  while sending the box refuses input by a beforeinput guard rather than
  readOnly, so the keyboard stays up (LOW 6).
- **Scrolling and the keyboard:** a new message, or a redraw, keeps the
  newest message in view only when the owner was already at the bottom.
  The app is sized to the visual viewport (`viewport.ts`), and while the
  keyboard is up the tab bar steps aside, so the keyboard never covers the
  bar; the tab bar keeps the bottom safe area. A pinch-zoom is never read as
  the keyboard, and a resize or rotation reads it all again (LOW 3-4).
- **Notifications** (`notify.ts`): one compact row, about 46 px instead of
  136: "Notifications on · count only, never what" with a small "Turn off"
  at its right ("Notifications off" and "Turn on" likewise; the hint is the
  "on" state's only). The states and
  what each does are unchanged; the long sentences (install, blocked,
  unsupported, unavailable) keep their words in the same small type.

### 10.12 The UI audit's safety fixes and quick wins (2026-10-29)

The phone UI/UX audit of main 79da9b70 (findings C1 to L11; the owner chose
"Safety + quick wins"). UI only: no route, daemon or rule changed, and every
daemon string is still set as text or through the sanitized Markdown.

- **C1, the owner's rows cannot be imitated.** Every owner row, bubble or
  flat, carries a code-built "You" chip (`.who-owner`, a light filled tag)
  before its marks, and sits at the right; a flat row's rule is on the
  right (`--text-2`). A reply has a continuous left gutter, and a quote in
  it is a filled italic box with no rule. The Markdown sanitizer keeps no
  `class`, `data-*` or `style`, so no reply can carry the chip or an owner
  row's classes (`OWNER_ROW_CLASSES`); `views.test.ts` renders the audit's
  spoof and a dozen other constructs (headings, lists, tables, bold, inline
  code, raw HTML, a fence whose info string names an owner class), and 8
  mutants (each part of the fix reverted, the sanitizer keeping `class`,
  H1's old rule) are each killed by a named test.
- **H1:** a folded relay line's ", not the owner" never shrinks
  (`.step-role-tail{flex:none}`); the line's own text gives way first, then
  the chat's title (ellipsized, so the 78% cap holds). Measured at 390 and
  320 px in `hud_v2_phone_check.py`.
- **H2:** a collapsed command (card, inbox and review rows) is up to three
  wrapped lines (an inner `-webkit-line-clamp: 3` box), never one ellipsized
  line; "show full command" stays.
- **H4, until PM-F:** a PR row shows the PR's title, code's facts parts
  (names in `<bdi>`), the safety-file flags, and the summary's Does and
  Blocked lines in an "AI SUMMARY" slot, then "Merge it at the desk for now".
  Never the head SHA, a raw key or a link; no buttons.
- **H5:** an unread chat says "Loading…", or offline "Not read yet: you're
  offline.", never "No messages yet."; the task view and the card likewise.
- **H6:** a deferred row's Leave it (left) and Approve and retry (right) sit
  apart in one row, as the Review tab's. There is no arming delay on either
  surface; none was added.
- **M1, M2:** the card's notice has one fixed slot under the allow row (two
  lines held), so nothing it says moves a button; while busy it says "Asking
  for Face ID…", then "Sending…" (a refusal in amber).
- **M3:** the limited state is said once: "limited: <label>. AUTHORIZE
  answers with Discord's authority; ALWAYS is off."
- **M4:** an inbox decision's suggestion is marked, never preselected (as
  MR-3), and its options are listed once.
- **M5:** a task's open question offers "Answer it in the Inbox", which opens
  the Inbox at its `tq:` row.
- **M7:** `--control-edge`, a 55% mix of `--text-muted` into
  `--border-strong` (#737069), edges every button and field: 3.73:1 on
  `--bg-0`, 3.53 on `--bg-1`, 3.22 on `--bg-2`, 3.88 on `--bg-inset` (it
  was 1.92/1.81). It is the phone's own, outside the `:root` copy of
  theme.css. "Open the card to answer" is cyan-edged.
- **M9a:** the Review tab keeps its slot, off, while there is no review, so
  Chats and Tasks never move.
- **M13:** owner rows start with a hidden "You: "; each Review checkbox is
  named "Select: <title>"; a card's heading takes the focus when it opens,
  and on close the focus goes to the answer's line or back to the row's
  button (kept across redraws; never on AUTHORIZE or ALWAYS).
- **L1:** the safety gap holds "Denies automatically at 08:36 · 9 min left"
  (code's words, redrawn every 15 s); the foot line is gone.

Not done here: the compact inbox (H3), swipe-back and history (M10), Dynamic
Type (M8, px to rem: not trivial across every rule), and the audit's other
mediums and lows.
