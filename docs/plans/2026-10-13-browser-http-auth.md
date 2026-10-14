# Owner HTTP authentication — B-AUTH

This is the repository-visible interface for the October 13 second Browser batch.
It implements the independently accepted `br7-auth-plan-amendment.md` held by the
lead, with the explicit Sound-restoration restriction agreed for this batch.
No agent tool accepts credentials or answers authentication.

## Authority and integration

`browser/auth.py:OwnerAuth` owns CDP authentication state on the existing host
loop. `auth_state.py` owns pure exact-origin, field, clock and attempt bounds.
Only adopted owner `_Tab` objects can install this handler. Hidden BR5 Direct
capture contexts never become owner tabs or install it. The existing SOCKS gate
continues to judge every connection; Fetch adds no lease or network permission.

`BrowserHost._goto` and history start one supervised navigation child, using
Playwright timeout zero and an actual watchdog. Its `ActivityWork` token lasts
through its real coroutine cleanup. Normal work has an accumulated 20-second
budget. Authentication has one original monotonic 60-second budget and at most
three submissions per explicit owner address/history/reload operation. Focus,
reconnect, document changes and repeated challenges cannot reset that budget.
A newer explicit navigation cancels and settles the old child before starting.
No owner credential reply acquires the profile lifecycle lock.

A first validated challenge returns `{loaded:false, auth_pending:true, nav_id}`
while navigation remains supervised. `BrowserHost.auth_pending_current` checks a
private live receipt cache. `BrowserPanes.address` accepts only that exact current
receipt, then rechecks window transaction, profile/context and actual pane identity
before adopting the allocation. The actual blank/prior URL stays the document
identity until Chrome commits. Explicit owner address remains the existing source
disclosure/lease boundary; restore creates no lease or source/see permission.
HUD ensure adopts a pending stored Remote restore and attaches its real ticketed
socket. Orphan allocation cleanup remains ownership checked.

## Exact challenge and reply

Every challenge must match a bounded Fetch paused-request record on this exact
CDP/Page/instance, and its request URL must match the normalized challenge origin
by scheme, hostname and effective port. Only Server Basic/Digest is accepted.
Proxy, unknown/malformed schemes, userinfo, control ports and origin mismatch deny.
There is one active prompt per tab, eight per host, 128 request records and 128
scheduled callback tasks per session. Overflow retires only the exact original
tab after cancelling its child; request headers are excluded before scheduling.
Ordinary intercepted requests continue unchanged through the existing gate.

Private socket frames are `{t:"auth", id, origin, realm, scheme, expires_in}` and
`{t:"auth_closed", id, reason}`. Owner replies use the same `t:"auth"` with exact
nonce and either `cancel:true` or bounded `username,password`. They are bound to
current Page/document/instance, exact socket attachment generation, approval epoch
and a visibility-invalidating session epoch. All conditions are checked again
inside the CDP dispatch coroutine. Reconnect rotates the nonce only; hidden or
detached tabs do not show a prompt. Owner answer/cancel stamps takeover. Approval
holds invalidate replies permanently and block input; resolution may issue a
fresh nonce without extending time or attempts.

Credentials exist only in the transient socket reply and one CDP answer. Replies
clear their field dictionary on every exit. Credentials never enter stream replay,
registry, status, bus, logs, config or storage. Chrome may retain its normal
in-memory HTTP-auth cache; this does not promise memory zeroization.

## Owner UI and shared seam

`BrowserAuth` is an isolated plain-text form using the existing Graphite tokens.
It does not autofocus, masks password entry, disables autocomplete, clears fields
on replacement/hold/send and never queues a reply through a card. Enter submits
from its fields and retains standard button activation; Escape gives higher approvals priority. `RemoteView` keeps
existing driving, instance, annotation, composition and clipboard guards. Active
auth participates in `canInput`, while its own guarded submit bypasses ordinary
page input. The auth controller exposes `_auth.blocked(tab)` for B-FIND admission.
Shared host hooks invalidate on navigation/hold/detach and refresh on attachment,
visibility and approval resolution. B-FIND remains a separate module/package.

## Restricted restoration and capability claims

A protected page during a committed Sound relaunch cancels authentication and
settles its navigation child immediately. It cannot open a prompt, mint a usable
replacement ticket or weaken the exclusive restart barrier. Existing failed
restoration placeholders expose the existing explicit owner Retry. This batch
does not support answering authentication during Sound replacement.

Installed-Chrome capability probes use the actual host/gate/socket with no
credentials. Same-origin iframe authentication delivered its exact owner prompt.
A popup's first protected request had no OwnerAuth session or prompt during the
two-second observation; explicit owner Reload returned `auth_pending` and delivered
the prompt. An established separate OOPIF target reached the protected fixture but
sent no owner prompt during the same bounded observation. These are measured gaps,
not universal popup/OOPIF support. Use explicit top-level owner navigation for a
protected embedded page; the popup Reload path was separately measured. Missing
CDP setup refuses with fixed words and never bypasses the network gate.

## Verification contract

Before merge: real first protected owner address and tab allocation must return
pending before ticket/socket attachment, wait beyond the normal 20-second bound,
accept an exact socket answer and commit. Actual 60-second expiry is measured
separately from shortened deterministic-clock checks. Include Basic and Digest,
wrong/cancel/reconnect/hold/visibility/new-nav/close/Private cleanup, origin/proxy/
duplicate/cap limits, blocked control ports, hidden capture exclusion, no-secret
canaries and Sound failed-restore cleanup. Independent source review and meaningful
guard mutations precede the clean full regression sweep and exact-head merge.

Current verification: 46 focused backend checks and a later 62-check auth/Sound
run passed. The actual first-address Basic gate passed a 22.153-second pending
wait/one reply and unchanged 60.002-second expiry. Nine native matrix cases passed
Digest, socket/navigation/attempt cleanup, stored restore without a new lease,
Private cleanup and hidden capture exclusion. Three capability cases measured the
limits above. Six guard baseline assertions passed; five scratch mutants failed
for their intended assertions. The WT production HUD build passed, as did 2,092
HUD unit checks plus the remaining 15 syntax asset checks under a finite fixture
allowlist. Focused actual auth HUD passed all 14 checks, including standard Enter
buttons, approval/Escape priority, strict Settings detach/reattach with a fresh
ticket, cleared typed fields and narrow-pane layout. Its Python-only reconnect
poll originally starved Playwright routed-socket callbacks; a side-effect-free
browser round trip fixed that fixture without a product change. Root review and
the combined integration sweep remain separate release gates.
