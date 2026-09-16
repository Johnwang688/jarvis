# WP10b — Discord routing: guild threads, verbs, thread-scoped approvals

## Built (`jarvis/v2/discord/gateway.py`; `jarvis/v2/daemon.py` startup only)
- `DiscordRouter(daemon, stores, router, approvals, control, rest, listener_factory)`
  places every owner message: DM, task thread (`tasks.list(discord_thread_id=…)`),
  project channel (`projects.list(discord_channel_id=…)`), or elsewhere.
- `should_respond` is v1's, asked about the same message *as if it had carried the
  mention* when the place is owned — owner-only, never-bots and never-empty stay one
  copy of one rule, and only the mention requirement is lifted (§11.2).
- Voice notes: v1 `voice_attachment` → `voice.stt` → `spoken=True`. A spoken turn
  never reaches the approval parser and `classify` refuses to make a verb of it, so
  it can only steer or chat. Reply is text plus a synthesized attachment.
- Verbs: `yes/no/always [code]`; `steer:`/plain text in a thread → `control.steer`;
  `cancel` → `router.cancel_proposal` first (grace window), then `control.cancel`;
  `status` → `render.status_embed`; `resume <id> [on <provider>]` → `control.resume`;
  `projects`/`tasks` → one capped message saying how many rows it dropped. A
  `ControlError` is shown to the owner, never raised at the surface.
- Intake: `task: …` in a project channel, or in a DM with `in <project>:` else the
  inbox; then `control.start` and §8.1's one line.
- Chat: one persistent `role=CHAT`/`FAST` thread per project channel and one for DMs;
  `daemon.send`, the turn's `TEXT` collected off the bus, split at 2000 for chat only.
  A `TURN_FINISHED` carrying a proposal goes to `router.on_turn_finished` (not
  `on_event`, so a runner accounting usage on the durable path is not double-counted);
  it is idempotent per turn id, so neither caller can double-open.
- Approvals: `approval_requested` → `render.approval_text` (the entire command, never
  a digest) into the task's thread, else the owner's DM. The channel is recorded under
  `req_id` with the request's one-shot lifetime, so a code typed anywhere else
  resolves nothing and says so; a bare `yes` with two open here asks for the code;
  `always` mints through the broker. `approval_resolved` posts one line naming which
  surface answered.
- Daemon startup: `discord_connected()` (never reads or logs the token value),
  `start_discord()`, and — when the bundle exists — `PendingApprovals(remote=True)` so
  a DM gets WP5's long timeout. 4014 stops via v1's inherited `_serve`.

## Validation (free; fake listener/control/REST transport, temp stores and allowlist)
Prefix each command below with:
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python`
| Command | Last lines (exit 0) |
| --- | --- |
| tests/v2/discord_routing_check.py | Ran 26 tests in 0.185s; OK |
| tests/discord_gateway_check.py | ok live: gateway handshake complete; all gateway checks passed |
| tests/discord_approvals_check.py | all discord approval checks passed |
| tests/v2/discord_render_check.py | Ran 21 tests in 5.359s; OK |
| tests/v2/daemon_check.py | Ran 15 tests in 1.439s; OK |
| tests/v2/router_check.py | Ran 21 tests in 0.386s; OK |
`git diff --check` clean. Three checks were verified to **fail** against a relaxed
gateway: the same-channel rule, spoken-never-authorizes, and the mention lift
confined to owned places.

**Caveat, reported not buried:** `tests/discord_gateway_check.py` ends with a *live*
handshake (v1's own design), so the brief's test list opened one real websocket with
the owner's bundle. Nothing was posted; everything I wrote is offline.

## Workarounds / proposed interface changes
- **`_V2Listener` copies v1's `_session` loop** to change one line: v1 gates
  MESSAGE_CREATE on its module-level `should_respond` before `_answer`, and v2's gate
  needs a store lookup v1 cannot do. `_serve` (with the 4014 stop) is inherited.
  Propose a v1 `GatewayListener.dispatch(message)` hook; the override then disappears.
- **`classify` is a module function, not a `Router` method** (the brief says
  `router.classify`); the module function is called directly.
- **`ApprovalRequest` has no `origin_channel_id`**, so the router keeps the map.
  Propose the field: the map is in memory, so a restart with a request still open
  loses its scope (the broker denies on timeout, so it fails closed).
- **`TaskControl` is WP11's.** `daemon._NoControl` refuses every verb with a
  `ControlError` sentence until the runner exists; WP11 should pass its runner to
  `start_discord(daemon, control)`, and may own that call site instead.
- **`DiscordRest.post` labels every attachment `text/plain`**, so a voice-note reply
  uploads as a file, not an inline player. Propose `content_type` on the file tuples.
- "cancel"/"stop" are v1 denial words *and* v2 verbs: they never claim an open
  approval, so the verb wins and the approval times out (which denies).
