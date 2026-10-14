# B-NATIVE implementation interfaces and cancellation races

Status: proposed implementation, claim #147 merged at 6068dfad. No native
subscription/model probe is authorized by this note. Root source review passed
the relay boundary before its implementation, with the clarifications below. Existing fast-path driving is installed.

## Native integration

- Extend frozen SessionTool with owner_gated; require foreground_only,
  desk_only, not read_only, and a registered nondangerous implementation.
  Production offers exactly hud_browser_act under this flag to both chats.
- Add jarvis-gated after named always-ask matching, with strict denial. A named
  rule asks once using the existing private metadata-only delegation card.
  Claude asks in its hook; Codex asks in bridge preflight. No second Claude card.
- Each native owner chat turn gets a memory-only DriveTurn, with actual provider,
  original desk words and latest accepted desk steer (500 characters combined).
  Queued/refused/off-desk text never adds authority or enters those owner words.
  Bind without retirement for each exact current Browser RPC. Turn/session
  completion, Stop, close/lost/error revokes it and refs; active reservations
  remain held through actual CDP settlement and activity cleanup.
- Reuse the installed singleton BrowserDrive/DriveState, switches, private
  consent, static floors, 40 judged actions/turn and 200 actions/tab/hour.
  drive_notice gains a validated provider keyword defaulting to fast. No sound,
  activity, profile restoration or stream-capture identity change is required.
- Redact exact canonical/prefixed action lifecycle names before event labels,
  public bus and logs. Result text still reaches the actual provider transcript.
  Native tasks/children/off-desk/stale/replayed calls remain refused.

## Exact relay request cancellation (proposed)

The stdlib relay owns at most 32 active tools/call requests, each identified by
its typed JSON-RPC request ID and an immutable per-request cancellation object.
Duplicate live IDs and over-cap calls return fixed protocol errors. Completed
workers remove only their identical record. A cancellation notification names
only an active exact ID; unknown/malformed notifications do nothing. IDs are not
logged. A reused completed ID is a fresh object and an old completion or already-captured
cancellation object cannot remove or cancel it. A late cancellation notification
with a reused ID inherently names the current request: the protocol supplies no
generation token, so this transport cannot distinguish that case.

A cancellation object contains an Event, a lock and at most its own socket.
The HTTPConnection subclass connects once explicitly, binds the resulting socket
under that lock, then rechecks cancellation BEFORE any HTTP request bytes.
Cancel sets the Event atomically and shutdowns only that exact socket. It does
not call HTTPConnection.close in a way that could cause request() to reconnect.
The worker keeps ownership until finally closes the connection and retires the
record. Cancel-before-connect denies; cancel-during-connect is caught at bind;
cancel-after-bind wakes a blocked response read via socket shutdown. No retry.
The bounded loopback connect retains an active socket deadline. No token or
argument enters diagnostics. stdin EOF cancels every active request before the
bounded worker joins; notifications/cancelled cancels only its exact request.

After bridge consumes the complete bounded request body, it captures the exact
accepted socket in a request_alive predicate. It uses select with zero timeout
and MSG_PEEK | MSG_DONTWAIT, synchronized as needed for concurrent checks. EOF,
invalid descriptor and unexpected socket errors fail closed; BlockingIOError
means no EOF was observed. It consumes no bytes and starts no monitor thread.
The bridge current predicate combines request_alive for Browser calls only with
its existing exact session/worker/turn/native identity and Stop checks. A direct
in-process bridge call has no transport predicate. This is transport lifetime,
not proof of provider identity; existing native identity admission stays intact.

Existing Guard.should_stop checks disconnect before the next CDP event and
inside broker waits. A pending private card is withdrawn. An event already sent
to Chrome settles and its activity/lease remains held until real cleanup. The
result gives a truthful partial count; no subsequent input event is dispatched.
Disconnect never retires the whole chat turn or cancels another relay request.

## Required free evidence

1. Socket tests: mark-before-connect, cancel-during-connect, cancel-after-bind,
   invalid/unknown cancellation, duplicate live ID, bounded map/workers, EOF,
   exact cleanup and reused ID races; a parallel survivor still completes.
2. Real daemon and real stdio relay with fake providers: abandoned Browser card
   withdraws; exact caller still pinned; denied/cancelled request sends no IO.
3. Real isolated Chrome and fixture-only network: partial type cancellation
   sends no later events, keeps busy activity until dispatched work settles;
   fresh ref/turn, provider banners/audit, takeover and shared-state exclusion.
4. Catalog, strict, alias named rule single private card, child/native metadata,
   privacy canaries, accepted/refused/queued desk steers and lifecycle regressions.
5. A 115-second actual wall-clock free real-relay wait proves daemon/relay only;
   fake clocks are broker unit timing evidence only. Native CLI
   115-second acceptance for BOTH providers remains UNRUN pending a separate
   concrete root-approved probe. No paid calls, owner credentials, live daemon,
   real accounts/devices, installed HUD edits or service restart in free checks.

Heavy checks require the lead slot grant, >=4 GiB available and systemd scope
MemoryMax=3G / MemorySwapMax=0. Preserve actual exits, deadlines and failure logs.
