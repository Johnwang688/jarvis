# Design: the phone surface (Jarvis v2)

**Status (2026-10-24): draft for the owner's review.** Nothing here is built.
Written against `main` at `2229645e`. It depends on the
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

- **PH-1. A fourth listener, `REMOTE_PORT` 8406, on loopback,** that serves
  only the phone app and the phone API. It is never the HUD listener, and
  `is_owner` is never true on it. It shares no route table entry with the
  HUD unless that route is listed in §5.
- **PH-2. Reachability is `tailscale serve` on Windows**, terminating HTTPS
  with the tailnet's certificate at `https://<machine>.<tailnet>.ts.net` and
  proxying to `127.0.0.1:8406`, which WSL2's localhost forwarding carries
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
  can send them to 8406 directly.
- **PH-4. Every Allow and every Always is its own passkey assertion.** The
  challenge is bound to the approval and the exact answer:
  `sha256(req_id ‖ "allow" | "always" ‖ server nonce)`, single-use, valid
  60 s, so an assertion for Allow cannot be replayed as Always. The server
  verifies origin, rpId, the UV flag and the signature counter before it
  calls `PendingApprovals.resolve`. Deny needs only the session. This is
  "deny cheap, authorize deliberate", made physical. A program running as the
  owner can forge any header, but it cannot produce this assertion.
- **PH-5. Sessions are short and narrow.** After a passkey login, the phone
  holds a `__Host-` cookie (HttpOnly, Secure, SameSite=Strict, 12 h idle,
  7 days absolute). Every write also needs `Origin` equal to the ts.net
  origin. Logging out or revoking the device in the desktop HUD ends its
  sessions at once.
- **PH-6. Pairing starts only at the desk.** An owner-only HUD route opens a
  pairing window (5 minutes, one device) and shows a QR code holding a
  one-time code. The phone opens the URL, enters or scans the code, and
  registers its passkey. The HUD lists paired devices with last-seen and
  Revoke. At most 3 devices.
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
 Windows: tailscale serve  https://<machine>.<tailnet>.ts.net  →  127.0.0.1:8406
   │ WSL2 localhost forwarding
   ▼
 WSL: jarvis daemon
   ├─ REMOTE listener 8406 ── remote_auth (passkey, sessions, Origin)
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
3. a valid, unexpired, unrevoked session cookie names a paired device;
4. for anything but GET or HEAD, `Origin` is exactly
   `https://<machine>.<tailnet>.ts.net`;
5. for an Allow, a fresh assertion as in PH-4.

A request that fails any of these gets a 401 with no body detail. The login
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
   `127.0.0.1:8406`, with no Funnel entry for any port.
3. **The certificate is the tailnet's own**, valid for the ts.net name, and
   the request's `Host` and `Origin` match it (§4, items 2 and 4).
4. **The device is paired, unrevoked, and its passkey requires user
   verification**, and the assertion is fresh (PH-4).
5. **Nothing else is listening on 8406**, on any interface, in WSL or on
   Windows (`ss` in WSL, `Get-NetTCPConnection` on Windows, read-only).

If any condition fails, the phone keeps working at **Discord-level authority**
(Allow and Deny for ordinary cards; no Always; HUD-only cards say "answer
at the desk"). The phone shows a red "limited: <which check failed>" line,
and the desktop HUD's status shows the same. Getting full authority back needs
nothing but the condition passing again. No setting widens it.

**The agent path.** An agent's shell could `curl 127.0.0.1:8406`. Without a
session cookie that is a 401. The cookie lives in the phone's browser and is
never written to disk on the PC. Defence in depth: `rules.py` DENYs any
command naming port 8406 or the remote listener's route prefix, the same
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
| Approvals are the owner's, deliberately | PH-4: a user-verified assertion (Face ID or passcode) bound to each Allow and each Always; no bulk Allow; Always shows its rule first |
| HUD-only cards answered only by a verified owner surface | `owner_verified` set only by the HUD route and by the phone under §4.1 with a fresh assertion (PH-7, O-3); private detail never in push, logs or the stream |
| No tool reaches owner routes | remote routes need a session no agent holds; `rules.py` DENY for 8406; nothing in the session MCP catalog |
| Credentials never leave the daemon | VAPID private key and WebAuthn data are protected paths; push payloads are content-free; logs carry status codes only |
| Nothing fetched speaks for the owner | the phone renders transcripts as the HUD does: scrubbed, untrusted, `textContent` for cards |
| Desk authority is the HUD's | `via="phone"`, desk false |
| Unknown or malformed fails closed | bad session, Origin, Host or assertion → 401; an expired challenge → 409 and a fresh one |
| Network exposure | loopback bind; tailnet only via `tailscale serve`; no Funnel; checked continuously (§4.1), and full authority drops to Discord level when any check fails |

## 10. Work packages

| WP | Package | Size | Acceptance |
|---|---|---|---|
| P-0 | **Spikes, each a script under `tests/spikes/` with its finding written here.** (a) `tailscale serve` on Windows reaching a WSL loopback listener on 8406, with a valid cert, and `tailscale serve status --json` readable from WSL; (b) a passkey registered and asserted with Face ID, and again with Face ID off using the passcode, from the owner's iPhone, in the home-screen app, on the ts.net origin, verified by `webauthn`; (c) a Web Push reaching the home-screen app on the iPhone with the screen locked | S | all three pass on the owner's iPhone; any failure stops the plan and comes back as a decision |
| P-1 | `REMOTE_PORT` listener, strict Host and Origin, CSP, static serving of an empty mobile shell | S | the API and HUD listeners never serve `/m/*`; the remote one never serves HUD routes (route-table test); non-ts.net Host refused |
| P-2 | `remote_auth`: pairing from the HUD, passkey registration and login, sessions, devices, revoke | M | pairing only within an open window; revoke ends sessions at once; replayed or cross-origin assertions refused; `rules.py` DENY for 8406 |
| P-3 | Read API and the Inbox, Chats and Tasks tabs (read-only) | M | headless mobile-viewport suite; transcripts identical to the HUD's projection; private cards show their private projection until P-4 lands |
| P-4 | Answering: approvals with per-answer assertion, `owner_verified` in `approvals.py`, the §4.1 security monitor, questions, decisions | M | Allow or Always without a fresh assertion refused; an Allow assertion replayed as Always refused; HUD-only card answered from a verified phone, refused from Discord and from a phone with any §4.1 check failing (one test per check); `owner_detail` never appears in push, logs or `/m/ws` (asserted); Deny works with session only; the card suite passes on the phone bundle; boundary review on the `approvals.py` change |
| P-5 | Sending messages, new chats, task cancel and resume | S | `via="phone"` recorded; desk false (fast-path suite); queues behind a running turn |
| P-6 | Web Push | S | payload is content-free (asserted); coalescing; 410 deletes; quiet hours |
| P-7 | Push to talk | S | transcription lands in the box; nothing sends without a tap; a voice note never answers a question |
| P-8 | Hub migration notes and docs | S | CLAUDE.md gets a Phone section; `C:\myday\hub\README.md` gets a pointer once the owner decides §8 step 2 |

Order: P-0 → P-1 → P-2 → P-3 → P-4 → P-6 → P-5 → P-7 → P-8. The inbox's IN-0 to
IN-2 must land before P-3.

## 11. Open questions for the owner (defaults in bold)

Answered 2026-10-24: the phone is an iPhone (O-1); Discord stays as a backup
(O-2); phone approvals equal the HUD's under §4.1, HUD-only cards included
(O-3); the other tailnet device is trusted, so no ACL change is needed (O-4).

1. **Should the phone be able to start tasks**, or only chat and answer?
   **Yes, through the same intake gate as the HUD.**
2. **Desk authority from the phone:** may a message typed on the phone let
   a chat make small direct edits the way a HUD-typed one does (fast path
   F-1..F-7)? **No**: O-3 covers approvals, and each edit made that way
   would skip a card.
3. **A Tailscale ACL anyway?** Optional now (O-4). It would limit port 443
   on this machine to the owner's own devices. **Not required.**
4. **Session length:** **12 h idle, 7 days absolute.**
5. **Quiet hours:** **23:00 to 07:00**, or none.

### Critical files for implementation

`jarvis/v2/daemon.py` (listeners, `send`, `answer`), `jarvis/v2/projects.py`
(`is_owner`, as the model to not reuse), `jarvis/v2/hud_api.py`
(`check_origin`, CSP), `jarvis/v2/approvals.py`, `jarvis/rules.py` (the
8406 DENY), `jarvis/v2/provider.py` (`UserMessage.via`), `jarvis/v2/desk.py`,
`hud/vite.config.ts` (second entry), `hud/public/manifest.webmanifest`,
`pyproject.toml` (`webauthn`, `pywebpush`).
