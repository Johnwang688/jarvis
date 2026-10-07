# Plan: Discord update poster + project channels (Jarvis v2)

Status: **draft, awaiting owner decisions** (section 7). Written 2026-10-07 by
a read-only planner subagent. It builds on PR #4 (projects/archive) and PR #3
(per-thread model), so it is built only after both merge. Deferred to after
this ships: DM conversation memory across restarts, the "which project?"
follow-up memory, and choosing a model from Discord.

## What you will see in Discord after this ships

- A **"Jarvis" category** in your server, with one channel per linked project,
  for example `#school`.
- **Every task gets its own thread** in its project's channel, named
  `a1b2c3d4 · <brief>`. You are added to the thread.
- **One status card per task, kept up to date in place.** It shows the phase,
  the step (`3/7`), elapsed time, cost, the last action, any open question and
  the routing. It updates at most once every 5 seconds, and an update never
  pings you.
- **Short milestone posts in the thread:**
  - Started, Verified and Cancelled appear quietly.
  - Question (with its choices), Approval (the whole command and a code),
    Blocked, Failed and Done (with the report, ≤1500 characters, the rest
    attached as `report.txt`) **ping your phone**.
- **You answer in the thread, with no @mention.**
  - `yes CODE` approves. A code typed anywhere else still runs nothing.
  - Plain text answers a blocking question, or steers the task.
  - `status` and `cancel` work as before.
- **Projects with no channel, and the Inbox (your DMs):** you get the
  ping-worthy milestones (question, blocked, failed, done) as DMs. They have no
  status card.
- **In the HUD:**
  - The Edit dialog of each project gets a Discord row: **Create channel**,
    **Link existing**, **Unlink**, and a status pill (ok / missing permission /
    not found).
  - The New Project dialog gets a "Create a Discord channel" box.
  - The status column gets a **Discord** light: green ok, amber with the
    reason, red unreachable.
- **When you rename, archive, restore or delete a project:**
  - Rename: a channel Jarvis created is renamed to match.
  - Archive: that channel moves to a "Jarvis Archive" category, with a note
    posted.
  - Restore: it moves back.
  - Permanent delete: a final note is posted and the channel is unlinked.
    **Jarvis never deletes a channel.** Channels you linked yourself are never
    renamed or moved; they only get the note.

---

## 0. What was verified in the code (corrections to the brief)

- **The poster (`Reporter`) is never started.** It is only constructed in
  `tests/v2/discord_render_check.py`. `daemon.main()` calls `start_discord()`,
  which builds only the `DiscordRouter`, and only after `runner.serve()`. A
  poster started at that point would also miss the recovery snapshots that
  `runner.serve()` publishes.
- **No guild id is stored anywhere.**
  - The token bundle `~/.config/jarvis/discord_token.json` has only `bot_token`
    and `owner_id`.
  - v1 `connect()` invites the bot with `permissions = 1024 + 2048 + 65536`
    (view, send, read history). That set has **no** Manage Channels, Create
    Public Threads, Send Messages in Threads, Embed Links or Attach Files.
  - `discord_channels` (a tool) only lists guilds live.
- **Your projects:** School `4040932c`, e2e-calc `571fbc98`, e2e-calc
  `d71b5c52`, test `f0f8bfb3`.
  - None has a channel.
  - The Inbox has not been created yet.
  - Two tasks exist: one **blocked** (e2e-calc `571fbc98`) and one done. No
    `discord.json` sidecars exist yet.
- **Bug 1: the poster can lose a task's thread id.**
  - `worktrees.ensure(task, …)` holds a task object across `git worktree add`
    (seconds) and then saves it whole.
  - That is exactly when the poster writes `discord_thread_id`. The stale save
    wipes the id, and the next snapshot creates a **second thread**.
  - `TaskStore.save` is last-writer-wins. The existing test only covers the
    other direction.
- **Bug 2: answering a question from Discord does nothing.**
  - A reply in a thread whose task is waiting in `clarifying` becomes
    `control.steer()`.
  - `steer()` queues the text for the next turn. `answer_question()` is never
    called from Discord.
  - So the "Question" milestone would be a dead end.
- **Smaller issues found in the code:**
  - **Unprotected link field:** `POST`/`PATCH /projects` accept
    `discord_channel_id` with no validation, from any loopback caller.
  - **Ping and timing gaps in the poster:**
    - `allowed_mentions: {"parse": []}` means no post ever notifies you.
    - Threads are created on `task_created`, while still in intake. §11.1 says
      when the task enters `clarifying`.
    - The poster calls `unarchive` before **every** edit.
    - The `question` milestone has no choices: the poster ignores
      `task_question`.
  - **Rate-limit sleep:** `DiscordRest._api` sleeps for any `retry_after`. A
    channel rename is limited to 2 per 10 minutes, so this could freeze the
    poster's only worker for 10 minutes.
  - **Missing intent:** `INTENTS` lacks `GUILDS` (bit 0). Thread
    `MESSAGE_CREATE` should still arrive under `GUILD_MESSAGES` for a thread
    the bot created; to verify live (risk 12).
- **What PR #4 provides that this builds on:**
  - `Project.archived`
  - `projects.owner_only(handler, daemon)` (HUD port plus Origin)
  - Events `project_updated {changed, project}`, `project_archived`,
    `project_restored` (the name may be renumbered) and `project_deleted`
    (**no `discord_channel_id` in its data**)
  - `_locate`, `router.place` and `_list_projects` already skip archived
    projects
  - `impact()` already returns `discord_channel_id`
  - `ProjectDialog` (Pickers.tsx) with a read-only
    `data-testid="project-discord"`
  - `archive_check.py`, `hud_v2_mock_projects.py` and
    `hud_v2_projects_check.py`
- **What PR #3 provides:** it does not touch Discord. It only conflicts in
  `daemon.py`, in areas away from `start_discord` and `main`.

---

## 1. Starting the update poster (PR A)

**Wiring: new `DiscordSurface` in `jarvis/v2/discord/__init__.py` (or
`surface.py`)**
- It owns one `DiscordRest`, the `DiscordRouter`, the `Reporter` and (in PR B)
  the `ChannelLinker`, plus a shared `owner_dm()` (moved out of
  `gateway._owner_dm`).
- `start_discord(daemon, control=None)` keeps its signature, returns the
  surface and sets `daemon.discord = surface` (None means not connected).
- **Order in `main()`:**
  1. `daemon.start()`
  2. Build the surface and start the **Reporter before `runner.serve()`**, so
     recovery snapshots are seen.
  3. `runner.serve()`
  4. `router.start()` (the gateway)
- **Stop order:**
  1. router (no new verbs)
  2. `runner.stop()`
  3. `reporter.close(flush=True)`: one bounded (2 s) flush of pending edits,
     so a task that just finished does not keep a stale card
  4. hatch, then daemon
- `start()` and `stop()` are both idempotent. The existing `_stop` guards are
  kept, and `start()` gets a flag.

**Restart idempotency**
- Keep the `tasks/<id>/discord.json` sidecar.
- Add `embed_sha` (a hash of the last embed that was successfully posted or
  edited) and `thread_gone`.
- **Reconcile pass on start, paced at ≤1 create per second:**
  - (a) Active tasks in linked projects with no thread get a thread and a
    status card. The milestone reads "Following task X (already blocked: …)".
  - (b) Any task whose rendered embed differs from `embed_sha` gets an edit
    queued. This covers terminal tasks that finished during a shutdown.
  - (c) Terminal tasks are **never backfilled**.
  - (d) Tasks in unlinked projects get a seeded sidecar (current phase),
    **with no DM**. Your old blocked e2e-calc task does not ping you on first
    boot.
- Also re-run reconcile when `subscription.dropped` grows (the bus caps at
  256).

**Tasks with no channel: recommend DM fallback, attention milestones only**
- Covers unlinked projects and the Inbox.
- Posts question (with choices), blocked, failed and done (with the report) to
  your DM, prefixed `[<project> · task <id>]`.
- No status card and no "Started": the intake reply already confirms.
- This replaces `test_missing_channel_silent_count`.
- Alternatives: skip silently (today's behaviour, but invisible), or post
  everything to DM (too noisy).

**Rate limits**
- Keep the 5 s coalescing per thread. Each task has its own thread, so
  concurrent tasks don't starve each other.
- One worker serializes posts.
- In `DiscordRest`, cap the 429 sleep at 10 s. Above that, raise
  `DiscordError(status=429, retry_after=…)` and let the caller defer.
- `DiscordError` gains `.status` and `.code` (Discord's JSON error code).
- **Circuit breaker** in the Reporter: after 3 consecutive failures, back off
  30 s, then 60 s, up to 5 min. On recovery, run a reconcile pass.
- Edit first. Only on code **50083** (thread archived) do `unarchive` and
  retry once. This halves the edit traffic.

**What is posted, when, and how long (§11.3)**

| When | Post | Pings you? | Length |
|---|---|---|---|
| First snapshot with phase ≠ intake (so proposals still inside their grace window make nothing) | Create the thread with `auto_archive_duration=10080` (7 days); `PUT /channels/{thread}/thread-members/{owner}`; post "Started"; post the status card | no | started ≤400; card ≤6000 (existing `status_embed`) |
| Any `task_status_changed` | Edit the card (coalesced) | never | — |
| `task_question` (new subscription) | "Question (blocking): … Choices: a / b. Reply here to answer." | yes | ≤400 |
| `approval_requested` | Unchanged: `gateway._post_approval` with `approval_text`, whole and attached if >2000 | yes | unbounded (attached) |
| → blocked / failed | Existing templates | yes | ≤400 |
| verifying → done | "Verified: …" | no | ≤400 |
| → done | "Done" plus `report_text` | yes | ≤1500, plus `report.txt` |
| → cancelled (**new template**) | "Cancelled." | no | ≤400 |

- Pings use `allowed_mentions: {"users": [owner_id]}` and never `parse`, so
  nobody else can ever be pinged.
- `render.milestone("approval")` stays unused. The gateway's `approval_text` is
  the real approval post.

**Thread auto-archive**
- Seven days of inactivity archives a thread. A post (a milestone or an
  approval) to an unlocked archived thread reopens it.
- Card edits use the 50083 path above.
- Jarvis never locks or archives threads itself, so Manage Threads is not
  needed.

**Approvals in the thread**
- `_post_approval` already prefers `task.discord_thread_id`. It works once
  threads exist, provided bug 1 is fixed.
- **Fix for bug 1:** in `TaskStore.save`, `discord_thread_id` is write-once.
  If the incoming object has None and the stored one has an id, keep the
  stored id. The Reporter also reads the sidecar's thread id before the task's.
- **Add:** if posting to the thread fails (404 code 10003 Unknown Channel, or
  403), post to the DM instead and record **the DM** as that request's
  channel. The answer is then only valid there. Without this, the approval
  would silently time out and be denied.

**Failure surfacing: never silent**
- **Daemon log:** a WARNING with the operation, HTTP status and Discord code,
  for example `create_thread 403 code 50013`.
  - The `str()` of a `DiscordError` is logged; it is already token-redacted.
  - Other exceptions are logged by class only, as today.
- **HUD:**
  - New `GET /discord` returns `{connected, guild: {configured, id}, gateway:
    {state}, reporter: {state: ok|degraded|down, counters, dropped,
    last_error: {op, status, code, channel, at}}}`.
  - An SSE `discord_status` event is published on every state change.
  - A `DiscordPanel` in the status column
    (`hud/src/components/Panels.tsx`).
- **DM:** once per channel per 24 h, for **permission** errors only, for
  example "I can't post in #school: missing Send Messages in Threads." An
  outage can't be DMed; the HUD light and the log cover it.

---

## 2. Linking projects to channels (PR B)

**Create or adopt: recommend both**
- **Create:** a button in the Edit dialog, and a New Project checkbox that is
  checked by default once the guild is set up.
- **Link existing:** paste a channel id.
- **Unlink.**
- All of these go through a new **owner-only** route. `POST`/`PATCH
  /projects` stop accepting `discord_channel_id`: they return 400 "link a
  channel from the project dialog". The HUD never sends that field, so nothing
  breaks.

**Guild setup is human-only: new `jarvis auth discord-guild`**
- Code: `jarvis/v2/discord/setup.py`, plus a new choice in
  `jarvis/__main__.py` `cmd_auth`.
- **The command:**
  1. Lists `GET /users/@me/guilds`; you pick one.
  2. Checks you are a member of it.
  3. Finds the categories **"Jarvis"** and **"Jarvis Archive"** by name.
  4. Computes the bot's effective permissions on each.
  5. Prints any missing permissions, with the fix.
  6. Writes `~/.config/jarvis/discord_guild.json` `{guild_id, category_id,
     archive_category_id}` (new `config.DISCORD_GUILD_PATH`, env
     `JARVIS_DISCORD_GUILD`).
- Add `discord_guild.json` to `permissions.protected_paths()` names, so no
  agent can retarget the guild.
- The daemon re-reads the file on each link operation, so no restart is
  needed.
- **Least privilege. Recommendation:**
  - You create the two categories by hand.
  - On each category, add an overwrite for the bot's role: **View Channel,
    Send Messages, Embed Links, Attach Files, Read Message History, Create
    Public Threads, Send Messages in Threads, Manage Channels**.
  - Manage Channels is **not** granted guild-wide, so a bug or a leaked token
    cannot touch your other channels.
  - Re-inviting with a guild-wide integer is the fallback: `309237763088` (the
    same set plus Manage Channels).
- **New `jarvis/v2/discord/perms.py`:**
  - `effective(guild, roles, member, channel) -> int`: Discord's documented
    algorithm (owner and Administrator bypass, then @everyone, then roles,
    then member overwrites).
  - `REQUIRED` and `missing(bits) -> [names]`.
  - Used by setup, link validation and the status check.

**Channel naming**
- `slug(name)`: NFKD, lowercase, `[^a-z0-9]+` becomes `-`, trimmed, capped at
  90 characters. An empty result becomes `project-<id>`.
- `"e2e-calc (1)"` becomes `e2e-calc-1`.
- If the slug is already taken in the category, append `-<id[:4]>`. This
  covers your two `e2e-calc` projects.
- Topic: `Jarvis project · <name> · <id>`.
- New project field: `discord_channel_origin: "created" | "adopted" | None`
  (in `model.py`, beside `discord_channel_id`).

**Project lifecycle**
- A new `ChannelLinker` (`jarvis/v2/discord/linker.py`) subscribes to PR #4's
  events. It has its own worker and deferred retries, because renames are
  limited to 2 per 10 minutes per channel.

| Event | Created channel | Adopted channel |
|---|---|---|
| `project_updated` with `name` in `changed` | `PATCH` name (deferred on 429; status says "rename pending") | nothing |
| `project_archived` | Move to the Archive category, and post "Archived in Jarvis on <date>; restore from the HUD's Archive." | Note only |
| `project_restored` | Move back; rename if restore renumbered the name; post "Restored." | Note only |
| `project_deleted` | Note: "Permanently deleted in Jarvis. This channel and its threads are kept; delete it in Discord if you like." Unlinked by construction. | Same |

- **Does a Discord channel count as one of your things? Yes, even channels
  Jarvis created.** Jarvis never calls `DELETE` on a channel. The test suite
  asserts that no `DELETE` request is ever made.
- One change to PR #4's shape: add `discord_channel_id` to `project_deleted`'s
  data in `projects.delete_project`.

**Backfill**
- Not automatic.
- Per project, use **Create channel** in the Edit dialog. Linking a project
  triggers that project's reconcile, so the **blocked e2e-calc task gets a
  thread** at that moment.
- Recommend: School now; the others when you want them.

**The Inbox stays in DMs.** The route refuses the Inbox ("the Inbox uses your
DMs"), and refuses archived projects with 409 "restore first".

**Routes, mounted from `hud_api.route` like PR #4's `projects.route`, in
`jarvis/v2/discord/routes.py`**
- `GET /projects/{id}/discord` returns `{channel_id, origin, name, state,
  missing, checked_at}`.
  - `state` is one of `linked_ok | unlinked | not_found | no_access |
    wrong_guild | missing_permissions | unconfigured | unreachable`.
  - Results are cached 60 s; `?refresh=1` forces a check. It uses a 5 s REST
    timeout, not 30.
- `POST /projects/{id}/discord` is owner-only. The body is `{action:
  "create"} | {action: "link", channel_id} | {action: "unlink"}`. It
  publishes `project_updated` with `changed: ["discord_channel_id"]`.
- **Link validation:**
  - The id must be a numeric snowflake.
  - `GET /channels/{id}` must not return 404/403 ("the bot can't see that
    channel").
  - Its `guild_id` must match the configured guild.
  - It must be a text channel (`type == 0`).
  - It must not already be linked to another project (409).
  - `perms.missing()` must be empty.

**HUD**
- In `Pickers.tsx` `ProjectDialog`, replace the read-only `project-discord`
  with a `DiscordLink` control: a status pill, Create / Link / Unlink, and an
  inline error.
  - The Unlink confirmation says "the channel is kept".
  - When the guild is not set up, the buttons are disabled and the dialog
    shows `jarvis auth discord-guild`.
- New-project checkbox, in `App.tsx` after create.
- Additions to `api.ts` and `types.ts`.
- PR #4's archive confirmation can add "#school moves to Jarvis Archive", from
  `impact().discord_channel_id`.

---

## 3. Routing changes (gateway and router)

1. **`gateway._locate`:** when the guild is configured, `task` and `project`
   placement require `message["guild_id"] == guild_id`. Otherwise the message
   is `other`. A matching channel id from another server is never treated as
   Jarvis's.
2. **Archived project channel:** PR #4 returns `other` for it. Make it return
   a new `archived` place, which counts as owned. The reply is a fixed
   sentence: "This project is archived; restore it in the HUD." Never chat or
   intake there.
3. **Answering questions in a task thread (fixes bug 2):** when the task is
   `CLARIFYING` with an unanswered blocking question, a **typed** non-verb
   message calls `control.answer_question(task.id, index_of_first_blocking,
   text)`. The bot replies "Answered (1 of 2). Next: …". A voice answer is
   refused politely (decision 9).
4. **Intake replies:**
   - In a project channel: "Opened task X; updates in its thread."
   - DM with `in <project>:` for a linked project: "…updates in <#channel>".
5. **`_post_approval`:** DM fallback as in section 1.
6. **`router.place`:** unchanged. The surface fallback stays, since the
   gateway already sets `surface` to the parent channel for thread messages.
   `classify` is also unchanged.
7. **`discord_routing_check.py` fixtures:** patch `config.DISCORD_GUILD_PATH`
   to a temp file with `GUILD`, so the existing tests keep passing.

---

## 4. Security and safety

- **Owner-only, as in v1.** `should_respond` is unchanged. The mention is
  lifted only in owned places. Item 3.1 adds the guild check on top.
- **Approvals only where asked:** unchanged. The recorded channel is the one
  actually posted to, including on DM fallback.
- **No agent tool can create or delete channels:**
  - Add `"discord_"` to `fastpath.FORBIDDEN_PREFIXES`.
  - A test asserts the only Discord name in `MCP_TOOLS` is `discord_dm_owner`.
  - A test asserts `create_channel` and `modify_channel` are called only from
    `discord/linker.py` and `discord/setup.py`.
  - The link routes are owner-only, the guild file is a protected path, and
    `PATCH` no longer takes the field.
  - Caveat, same as PR #4: an Origin header can be forged by a shell command.
    The damage is bounded: posts into a channel already in your guild that the
    bot can post in. Shell commands still pass the permission gate.
- **The bot token is never logged.** All new calls go through `DiscordRest`,
  which already redacts it. A test feeds a token-bearing error into every new
  code path and asserts it never appears in the logs.
- **No live Discord in tests:** a fake transport everywhere, and
  `httpx.request` patched to raise "live network", as the routing check
  already does.

---

## 5. Tests

Each test below fails on today's main.

**`tests/v2/discord_render_check.py` (Reporter)**
- A stale save after the Reporter wrote `discord_thread_id` keeps the id, and
  only one thread is ever created (bug 1).
- An intake-phase snapshot creates no thread; the first `clarifying` snapshot
  does.
- `create_thread` sends `auto_archive_duration: 10080` and adds the owner to
  the thread.
- Attention milestones send `allowed_mentions.users == [OWNER]`; started,
  verified and card edits send none.
- `task_question` renders the choices.
- The cancelled milestone is posted.
- An unlinked project gets attention-only DMs, with no card. This replaces
  `test_missing_channel_silent_count`.
- Reconcile on start: an active task gets a thread; a terminal task is
  untouched; a pre-existing unlinked task is seeded with no DM; a stale
  `embed_sha` triggers one edit.
- Edits make no pre-emptive unarchive call; code 50083 leads to unarchive plus
  one retry.
- A 429 with `retry_after: 600` raises in under 1 s.
- After 3 failures the circuit breaker makes no calls and reports `degraded`.
- A 403 log line carries the status and code but never the token.

**`tests/v2/discord_routing_check.py`**
- A message from another guild with a matching channel id is ignored.
- An archived project's channel gets the fixed reply.
- A typed answer while `clarifying` calls `answer_question`; a spoken one does
  not.
- When the thread post fails, the approval goes to the DM, and `yes CODE` in
  the thread is refused.
- Integration: a real `Reporter` and `DiscordRouter` on one fake REST.
  `task_created`, then `clarifying`, creates the thread; an approval lands in
  it; an answer in the thread resolves it; an answer in the DM is refused.
- `DaemonStartupChecks`: `start_discord` returns a surface whose Reporter is
  alive; `stop()` twice is safe; the Reporter subscribes before
  `runner.serve()`.

**New `tests/v2/discord_linker_check.py`**
- Slug table.
- Permissions-math table: owner, Administrator, @everyone deny plus role
  allow, member overwrite.
- Renames: created channels are renamed; adopted ones are not; a 429 defers
  and retries.
- Archive, restore and delete moves and notes.
- **No `DELETE` in any recorded call.**

**`tests/v2/hud_backend_check.py`**
- `GET /discord` shape when connected and when not.
- `POST /projects/{id}/discord` returns 403 without the HUD port and Origin.
- The link validation table: 404, 403, wrong guild, voice channel, missing
  permissions, already linked, Inbox, archived.
- `PATCH`/`POST /projects` with `discord_channel_id` returns 400.

**`tests/v2/archive_check.py` (PR #4) and `tests/v2/permissions_check.py`**
- The Discord routes appear in the owner-only list.
- `"discord_"` is in the fast path's forbidden prefixes.
- A write to `discord_guild.json` is denied.

**HUD headless (`tests/face/hud_v2_projects_check.py`,
`hud_v2_mock_projects.py`)**
- The pill renders each state.
- A bad id shows the mock's error.
- Create is disabled when the guild is unconfigured and shows the setup
  command.
- The Unlink text says "kept".
- The New Project checkbox posts the create.
- `DiscordPanel` states, and a redraw on `discord_status`.

---

## 6. Live verification (owner)

**At your desk**
1. Make sure only `jarvis daemon2` is running. No v1 `jarvis face` or `jarvis
   daemon`, because a second listener doubles every reply.
2. In Discord, create the categories **Jarvis** and **Jarvis Archive**. On
   each one, open Edit Category → Permissions and add the bot's role with the
   eight permissions from section 2.
3. Run `jarvis auth discord-guild` and pick your server. It should say "ok" or
   list exactly what is missing.
4. Restart daemon2. The HUD status column should show **Discord: ok**.
5. Open HUD → School → Edit → Discord → **Create channel**. `#school` should
   appear under Jarvis, and the pill should read "Linked · created by Jarvis ·
   ok".
6. Make `#scratch` elsewhere, copy its id, link it to **test**, and check the
   pill shows ok.
   - Then paste a voice channel's id: it should be refused.
   - Then paste a channel from another server: it should be refused.

**On your phone**

7. In `#school`, send `task: add one line to README saying hello` (something
   harmless).
   - You should get the reply.
   - A thread should appear, with "Started" and a status card that updates no
     more than every 5 s.
   - Your phone should ping on a question, an approval and Done.
8. If an approval comes:
   - Reply `yes CODE` in your DMs: it should be refused with "asked
     elsewhere".
   - Reply in the thread: it should run.
9. In the thread, send `status`, then `steer: keep it short`. Check that both
   are acknowledged.
10. At Done, check that the report is under 1500 characters and that
    `report.txt` is attached if the report is longer.
11. DM `task: …` with no project. You should get question and done DMs, and no
    thread.

**At your desk**

12. Rename School and check `#school` follows. It may take up to 10 minutes.
13. Archive **test**.
    - `#scratch` gets a note and stays where it is.
    - A message posted there gets the "archived" reply.
    - Restore it.
14. Remove "Send Messages in Threads" from the bot's role on the category,
    then start a task. Check that:
    - the HUD light turns amber and names the permission;
    - the daemon log shows `403 code 50013` and no token;
    - you get one DM.

    Put the permission back and check the light turns green.
15. Cut the network for a minute during a task. The light should go red. When
    the network is back, the card should catch up, with no duplicate thread.
16. Restart daemon2 in the middle of a task. There should be no duplicate
    thread and no second "Started".

---

## 7. Risks and open decisions (planner's recommendation for each)

1. **DM fallback for projects with no channel and for the Inbox:** attention
   milestones only. (Alternative: silent.)
2. **Creating channels automatically:** only through the owner-only route,
   with the HUD checkbox checked by default. Never on `POST /projects`.
3. **Does a Discord channel count as one of your things?** Yes. Jarvis never
   deletes one; on project delete it posts a note and unlinks.
4. **Renaming and moving:** only channels Jarvis created. Linked (adopted)
   channels get notes only.
5. **Manage Channels:** granted on the two categories only, not server-wide.
6. **Categories:** made by you; the setup command finds them by name.
7. **Pings:** ping you on question, approval, blocked, failed and done, and
   add you to every thread.
8. **When a thread is created:** at `clarifying`, as §11.1 says. A proposal
   withdrawn inside its grace window leaves nothing behind.
9. **Answering questions in a thread:** typed answers only, the same rule as
   approvals.
10. **"Cancelled" milestone:** add it, quietly.
11. **Bug 1 (two threads per task):** fix it with the write-once field in
    `TaskStore.save`. It is a real double-thread bug.
12. **Missing `GUILDS` intent:** verify at step 7. If thread messages don't
    arrive, add bit 0 to the v2 listener's IDENTIFY.
13. **A v1 listener running at the same time doubles every reply:** checked at
    step 1. Teaching v1 to defer to daemon2 is a follow-up.
14. **The Origin check is a speed bump:** accepted, the same as PR #4. The
    damage is bounded by the guild check and the bot's permissions.
15. **A category holds at most 50 channels:** the setup command warns at 45.
    Pruning the archive is up to you.
16. **A crash between a post and its sidecar save can post the same milestone
    twice:** accepted as rare.
17. **`project_deleted` lacks the channel id:** add it to PR #4's event (one
    line).
18. **Bus overflow drops events:** reconcile when the dropped count grows.

---

## Order and staffing

**One build agent, two stacked PRs, after PR #4 then PR #3 merge.**

**PR A, the update poster**
1. `DiscordError` gets `.status` and `.code`; the 429 cap.
2. The write-once fix in `TaskStore.save`.
3. Reporter changes: clarifying gating, auto-archive, pings, owner added to
   threads, `task_question`, cancelled, DM fallback, reconcile, `embed_sha`,
   circuit breaker, edit-then-unarchive.
4. `DiscordSurface` and the `main()` ordering.
5. Gateway: answering questions, approval DM fallback.
6. `GET /discord`, SSE, `DiscordPanel`.
7. Tests.

PR A is live-checkable through DMs alone.

**PR B, linking**
1. Guild config, protected path, `perms.py`, `jarvis auth discord-guild`.
2. Linker and routes; `PATCH` refuses the field; `project_deleted` gets the
   channel id.
3. Gateway guild check and archived reply.
4. HUD `DiscordLink` control, checkbox, mock and headless checks.
5. Docs: design §11, `hud-api.md`, CLAUDE.md.

Splitting keeps each review phone-sized. PR A also fixes the double-thread bug
before any channel exists to show it.

### Critical files
- `jarvis/v2/discord/reporter.py`, `gateway.py`, `rest.py`
- `jarvis/v2/daemon.py` (`start_discord`, `main`, `/projects` POST/PATCH)
- `jarvis/v2/stores.py` (`TaskStore.save`)
- New: `jarvis/v2/discord/{linker,perms,routes,setup}.py`
- `jarvis/v2/model.py`, `jarvis/v2/permissions.py`,
  `jarvis/v2/providers/fastpath.py`, `jarvis/__main__.py`, `jarvis/config.py`
- PR #4's `jarvis/v2/projects.py` and `hud/src/components/Pickers.tsx`
- `hud/src/components/Panels.tsx`
- Tests: `tests/v2/discord_render_check.py`, `discord_routing_check.py`,
  `hud_backend_check.py`, `tests/face/hud_v2_projects_check.py`
