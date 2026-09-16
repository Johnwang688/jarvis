# WP10b — Discord routing: guild threads, verbs, thread-scoped approvals

The other half of work package 10, in a git worktree on branch
`jarvis/wp10b-discord-routing`. Claude leads and you are Claude; this half
decides what a typed "yes" is allowed to authorize, so read the safety
material first:

1. `docs/jarvis-v2-design.md` — §11 in full, §8.1 (stage one), §6, §2 D4.
2. `CLAUDE.md` — *Discord shipped*, *Discord Gateway listener*, *Discord
   voice messages*, *Remote approval over Discord DM*, *`jarvis daemon`*:
   the rules you carry over verbatim — owner only, bot never the owner's
   account, **a transcription can never resolve an authorization**, an
   answer counts only in the channel the question was asked in, one-shot
   codes, anything unclear denies.
3. v1 code you reuse: `jarvis/discord_gateway.py` (`GatewayListener`,
   `should_respond`, `voice_attachment`, `strip_mention`),
   `jarvis/discord_agent.py`, `jarvis/discord_approvals.py` (the parser and
   the code rule), `jarvis/tools/discord.py`. Tests:
   `tests/discord_gateway_check.py`, `tests/discord_approvals_check.py`.
4. v2, merged: `jarvis/v2/control.py` (**fixed** — `TaskControl`; WP11
   implements it in parallel, you code against the protocol and a fake),
   `router.py` (`classify`, `Incoming`, the verb grammar — `WP9-notes.md`),
   `approvals.py` (`PendingApprovals.resolve`, codes, `pending()`,
   `on_request` — `WP5-notes.md`), `discord/rest.py`, `discord/render.py`
   (`approval_text`, `status_embed`), `discord/reporter.py`
   (`WP10a-notes.md`), `daemon.py`, `stores.py`, `providers/fastpath.py`.

## Deliverables (`jarvis/v2/discord/`)

### `gateway.py` — `DiscordRouter(daemon, stores, router, approvals,
control: TaskControl, rest, listener_factory)`

Runs the v1 `GatewayListener` (or a thin subclass) with a v2 message
handler. `should_respond` stays the outer gate (owner only, never bots);
the **mention requirement is lifted only inside a task thread or a project
channel Jarvis owns** — elsewhere in the guild, v1's mention rule stands.

Per message, in this order:

1. **Where is it?** DM; a task thread (`stores.tasks` lookup by
   `discord_thread_id`); a project channel (`stores.projects` by
   `discord_channel_id`); or elsewhere (ignore unless mentioned → treat as
   DM chat).
2. **Spoken?** A voice note (v1 `voice_attachment`) is transcribed with
   `voice.stt` and marked `spoken=True`; `router.classify` already refuses
   to make a verb of it, and you must additionally never pass it to
   `approvals.resolve`. Reply in text plus a voice note as v1 does.
3. **Classify** with `router.classify(Incoming(...))`.
4. **Verbs.** `yes|no|always <code>` → look up the pending request by code
   and resolve it **only if the request was posted to this same
   channel/thread** (`request.origin_channel_id`, which you set when you
   post it — see below); otherwise reply "that code was asked elsewhere"
   and resolve nothing. A bare `yes` with two open requests in this
   channel → ask for the code. `always` → `resolve(..., always=True)`.
   `steer:`/plain text in a task thread → `control.steer`; `cancel` →
   `control.cancel` (task thread) or `router.cancel_proposal` (grace
   window); `status` → post `status_embed`; `resume <id> [on <provider>]`
   → `control.resume`; `projects`/`tasks` → short lists (≤ 2000 chars,
   truncated with a count).
5. **Intake.** `task: …` in a project channel → `stores.tasks.create` in
   that project → `control.start`; in a DM → the inbox project unless the
   text names one (`in <project>:`); reply with the one line §8.1 gives.
6. **Chat.** Everything else → a fast-path thread: per project channel one
   persistent chat thread (create on first use, `role=CHAT`, provider
   FAST), per DM the inbox chat thread; `daemon.send` and reply with the
   `TEXT`; a `TURN_FINISHED` with a `proposal` → `router.on_event` (WP11
   may own the call — coordinate via the daemon; do not double-open).
   Long replies split at 2000 chars only for chat text (never for
   approvals).

### Approvals on Discord

Subscribe to `approval_requested` on the bus: post `render.approval_text`
(the whole command, the code, the origin) into the task's thread when the
request has a task, else into the owner's DM; record the channel id on the
request (`approvals` exposes a way, or keep a `req_id → channel_id` map
in the router with the same one-shot lifetime). On `approval_resolved`,
post one line saying which surface answered. When the remote surface is
attached, construct `PendingApprovals` with the long timeout (WP5's flag);
the daemon's `main()` passes it — minimal edit.

Nowhere-to-ask (no DM channel resolvable, gateway down) is not your
concern to fix: the broker already denies on timeout; just log that
Discord could not deliver.

### `jarvis/v2/daemon.py` (minimal)

Start the `DiscordRouter` in `main()` when the v1 token bundle exists and
the listener connects; a 4014 (intents off) logs the v1 explanation and
stops, never retry-loops.

## Tests: `tests/v2/discord_routing_check.py`

Free: a fake listener that feeds message dicts in Discord's shape, a fake
`TaskControl` recording calls, a real `PendingApprovals`, a real `Router`,
fake REST transport, stores on a temp root, `voice.stt` stubbed. Cover:
owner-only and never-bots still hold; mention lifted only in owned
channels/threads; a `yes <code>` in the thread the request was posted to
resolves it; the same code typed in another thread, in the project
channel, in a DM, or in a non-owned guild channel resolves nothing;
a spoken "yes" with a valid code resolves nothing and is treated as
steering; bare `yes` with two open → asks for the code; `always` mints via
the broker; steer/cancel/status/resume/projects/tasks each hit the fake
control with the right arguments; `task:` creates and starts in the right
project; DM chat and channel chat each reach a fast-path thread and reply;
a proposal on `TURN_FINISHED` is handed to the router once; approval
posted to the task thread with the entire command and the code, or to the
DM when there is no task; the 4014 path stops without retrying; nothing
posted exceeds 2000 chars except an attached approval; no credential
value in any request. Then run `tests/discord_gateway_check.py`,
`tests/discord_approvals_check.py`, `tests/v2/discord_render_check.py`,
`tests/v2/daemon_check.py`.

## Rules

Stay inside `jarvis/v2/discord/gateway.py`, `jarvis/v2/daemon.py`
(startup only), `tests/v2/discord_routing_check.py`,
`docs/codex-briefs/WP10b-notes.md`. Do not edit v1 Discord code, fixed
interfaces, or other merged modules; propose in notes. No new dependencies.
When green, commit with a message starting `v2 WP10b: Discord routing —
guild threads, verbs, thread-scoped approvals` ending with
`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Do not push or
merge. Finish with `WP10b-notes.md` (under 70 lines). Report branch,
worktree path and notes path as your final message.
