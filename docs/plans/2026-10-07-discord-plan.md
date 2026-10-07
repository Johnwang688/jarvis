# Plan: Discord update poster + project channels (Jarvis v2), revision 2

**Status:** revised 2026-10-07 to apply the owner's decisions in
[`2026-10-07-discord-decisions.md`](2026-10-07-discord-decisions.md). Where
this plan and that file disagree, the decisions file wins.

- D6 is still **pending**.
- Everything else in §9 is open, each with a recommendation.

**When it gets built:** after PR #4 (projects and archive) and then PR #3
(per-thread model) merge.

**Deferred until after this ships:**
- DM memory across restarts
- the pending "which project?" question
- choosing a model from Discord
- archive handling beyond what is written here

**The slash-command plumbing belongs to a separate plan.** This plan marks
its hooks as **[SLASH]**.

## What you will see in Discord after this ships

- **A "Jarvis" category** in your server, with one channel per project, for
  example `#school`.
  - Every channel belongs to a project whose folder exists on your computer.
- **Every task gets its own thread** in its project's channel, named
  `a1b2c3d4 · <brief>`.
- **One status card per task**, edited in place: phase, step `3/7`, time,
  cost, last action, open question. It updates at most every 5 seconds and
  never pings you.
- **Milestone posts in the thread.**
  - Started, Verified and Cancelled appear quietly.
  - Question (with choices), Approval (the whole command and a code), Blocked,
    Failed and Done (the report) are each followed by a one-line `@you`
    message.
  - Your phone notification opens right at the update. Nothing else ever pings
    you.
- **You answer in the thread with no @mention.**
  - `yes CODE` approves. A code typed anywhere else runs nothing.
  - Typed text answers a blocking question or steers the task.
  - `status` and `cancel` work as before.
- **Projects with no channel, and the Inbox, use your DMs.** You get question,
  blocked, failed and done there. A DM notifies you by itself, so no extra
  @line.
- **You can make a project from Discord.**
  - Say `new project`. Jarvis asks for a name and a folder.
  - If the folder doesn't exist, Jarvis asks before creating it, with
    `yes CODE`.
  - If the folder already has files in it, Jarvis asks before using it.
  - In your DMs, Jarvis creates a new channel for the project. In an unlinked
    channel, it links that channel.
  - `link <project>` links an existing channel to an existing project.
- **The HUD:**
  - Each project's Edit dialog gets Create channel / Link / Unlink and a
    status pill.
  - The New Project dialog gets a "Create a Discord channel" box.
  - The status column gets a Discord light: green ok, amber with the reason,
    red unreachable.
- **Your actions carry through to the channel.** When you rename, archive,
  restore or delete a project, its channel follows: it is renamed, moved to
  "Jarvis Archive", moved back, or gets a final note.
  - Jarvis may rename or move **any** linked channel when the action is yours.
  - Anything Jarvis wants to do on its own asks you first with a
    `yes/no CODE`. Example: archiving idle channels when the category gets
    crowded.
  - **Jarvis never deletes a channel.**
  - Archiving and deleting **projects** stays HUD-only.

---

## Decisions applied

| Owner decision | Where it lands |
|---|---|
| D1: a separate `<@owner>` line after each pinging milestone; no extra line in DMs | §1 |
| D2: projects and channels can be created and linked from Discord; a safe folder-creation function | §3 |
| D3: never delete channels; archiving them when crowded needs your permission | §2.4 |
| D4: rename and move any linked channel; your own actions count as permission; Jarvis-initiated actions go through the approval gate | §2.4 |
| D5: no Administrator; server-wide Manage Channels plus thread and message permissions; `applications.commands` in the invite (**pending owner confirmation**) | §2.1 |
| D6: thread created at `clarifying` | §1, **pending** |
| D7: typed answers only | §4 |
| D8: quiet "Cancelled" post | §1 |
| D11–D18: accepted | §9 |

One numbering note. The owner's D-numbers do not match revision 1's numbered
list:
- Owner D1 (pings) answers revision 1's item 7.
- Revision 1's item 1 (DM fallback for unlinked projects) and item 6 (who
  creates the categories) were not decided. Both are listed as open in §9.

---

## 0. What was verified in the code

- **The poster (`Reporter`, `jarvis/v2/discord/reporter.py`) is never
  started.**
  - It is only constructed in `tests/v2/discord_render_check.py`.
  - `daemon.main()` calls `start_discord()`, which builds only the
    `DiscordRouter`, and only after `runner.serve()`.
- **No guild id is stored anywhere.**
  - The token bundle has only `bot_token` and `owner_id`.
  - v1 `jarvis auth discord` invites the bot with permissions
    `1024+2048+65536`.
- **The owner's projects:** School `4040932c`, e2e-calc `571fbc98`, e2e-calc
  `d71b5c52`, test `f0f8bfb3`.
  - None has a channel. The Inbox has not been created yet.
  - Tasks: one blocked (e2e-calc `571fbc98`), one done.
- **Bug 1: the poster can lose a task's thread id.**
  - `worktrees.ensure` holds a task object across `git worktree add` and then
    saves it whole.
  - That can wipe the `discord_thread_id` the poster just wrote, and the next
    snapshot creates a second thread.
- **Bug 2: answering a question from Discord does nothing.**
  - A reply in a `clarifying` thread becomes `control.steer()`, which only
    queues the text.
  - `answer_question()` is never called from Discord.
- **Smaller issues found in the code:**
  - **Unprotected fields on `/projects`:**
    - `POST`/`PATCH /projects` accept `discord_channel_id` from any loopback
      caller.
    - PR #4's `PATCH` name change is not owner-only either. This matters for
      D4.
  - **Pings and timing in the poster and REST layer:**
    - `allowed_mentions` is hard-coded to `{"parse": []}`.
    - Threads are created at intake.
    - Every edit is preceded by an unarchive call.
    - `task_question` is ignored.
    - `DiscordRest` sleeps for any `retry_after`, including the 10-minute
      waits a channel rename can trigger.
  - **Missing intent:** `INTENTS` lacks `GUILDS`. To verify live.
- **Already reusable:**
  - `hud_api._picker_allowed` (home and `/mnt/<drive>` scope)
  - `config.V2_CREDENTIAL_DIRS` (`~/.ssh`, `~/.aws`, `~/.config/gh`,
    `~/.config/jarvis`, `~/.gnupg`)
  - `config.V2_DATA_DIR`, `SESSIONS_DIR`, `SPILL_DIR`, `REPO_ROOT`
  - `ApprovalRequest(allowlistable=False)`
  - PR #4's `projects.owner_only`, `unique_name` and events
- `/mnt/c/Users/johnw` exists.

---

## 1. The update poster (PR A)

**Wiring: `DiscordSurface` in `jarvis/v2/discord/__init__.py`**
- It owns one `DiscordRest`, the `DiscordRouter`, the `Reporter`, later the
  `ChannelLinker`, and a shared `owner_dm()` (moved out of
  `gateway._owner_dm`).
- `start_discord(daemon, control=None)` returns the surface and sets
  `daemon.discord`.
- **Start order:**
  1. `daemon.start()`
  2. Reporter
  3. `runner.serve()`
  4. The gateway listener
- **Stop order:**
  1. The gateway
  2. `runner.stop()`
  3. `reporter.close(flush=True)`, bounded to 2 s
  4. The hatch, then the daemon
- `start()` and `stop()` are both idempotent.

**Restart behaviour**
- The `tasks/<id>/discord.json` sidecar gains `embed_sha` and `thread_gone`.
- **Reconcile pass on start, paced at ≤1 thread create per second:**
  - Active tasks in linked projects with no thread get a thread and a card.
    The milestone reads "Following task X (already blocked: …)".
  - A task whose card differs from `embed_sha` gets one edit.
  - Terminal tasks are never backfilled.
  - Tasks in unlinked projects get a sidecar seeded silently, so there is no
    DM storm on the first boot.
- Reconcile also re-runs when the bus `dropped` counter grows.

**Bug 1 fix**
- `TaskStore.save` treats `discord_thread_id` as write-once: an incoming None
  never overwrites a stored id.
- The Reporter reads the thread id from the sidecar first.

**Rate limits**
- Keep the 5 s edit coalescing per thread.
- One worker serializes all posts.
- `DiscordRest` caps a 429 sleep at 10 s. Longer waits raise
  `DiscordError(status, code, retry_after)`, and the caller defers.
- Circuit breaker: back off 30 s, then 60 s, up to 5 min after three failures
  in a row, then reconcile.
- Edit first. Only on Discord code 50083 (thread archived): unarchive and
  retry once.

**What is posted (§11.3, with D1 and D8)**

| When | Post | Followed by a `<@owner>` line? |
|---|---|---|
| First snapshot with phase ≠ intake (**D6 pending**) | Create the thread with `auto_archive_duration=10080` (7 days); `PUT` the owner as a thread member; "Started"; the status card | no |
| Every snapshot | Card edit, coalesced | never |
| `task_question` | "Question (blocking): … Choices: a / b. Reply here." (≤400) | **yes** |
| `approval_requested` | Unchanged `approval_text`, posted whole; attached if over 2000 characters | **yes** (in a thread) |
| → blocked / failed | Existing templates (≤400) | **yes** |
| verifying → done | "Verified: …" | no |
| → done | "Done" plus the report (≤1500, plus `report.txt`) | **yes** |
| → cancelled | "Cancelled." (new template) | no |

**D1 mechanics**
- The milestone itself is posted with `allowed_mentions: {"parse": []}` and
  contains no `<@`.
- Immediately after, `rest.ping(channel, owner_id)` posts exactly
  `<@owner_id>` with `allowed_mentions: {"parse": [], "users": [owner_id]}`.
  Nothing else ever sets `users`.
- **DMs:** no ping line.
- In a thread, the mention also adds the owner to the thread. The `PUT` is
  kept so threads appear in the owner's list before the first ping.

**DM fallback**
- For projects with no channel and for the Inbox: question, blocked, failed
  and done go to the owner's DM, prefixed `[<project> · task <id>]`. No card
  and no "Started".
- This is still open; see §9.1.

**Approvals**
- `_post_approval` already prefers the task's thread.
- New: if the thread post fails (10003 Unknown Channel, or 403), post to the
  DM instead and record the DM as that request's only answer channel.
- **[SLASH]** In PR A, split `_approval_answer` into `parse` and
  `answer(channel_id, verdict, code)`. A slash `/yes` then calls `answer()`
  with the interaction's channel, and nothing is built twice.

**Failures are never silent**
- **Log:** a WARNING with the operation, HTTP status and Discord code. Never
  the token.
- **HUD:**
  - `GET /discord` returns `{connected, guild, admin, gateway, reporter:
    {state, counters, dropped, last_error}}`.
  - An SSE `discord_status` event on every state change.
  - A `DiscordPanel` in `hud/src/components/Panels.tsx`.
- **DM:** a missing permission is reported once per channel per 24 h.

---

## 2. Linking projects to channels (PR B1)

### 2.1 Guild setup and permissions (D5)

**New human-only command: `jarvis auth discord-guild`**
(`jarvis/v2/discord/setup.py`, plus a choice in `jarvis/__main__.py`
`cmd_auth`)
1. Lists the servers the bot is in; the owner picks one.
2. Prints the re-invite URL.
3. Waits for the owner.
4. Finds or (after a y/N prompt) creates the categories **Jarvis** and
   **Jarvis Archive**.
5. Computes the bot's effective permissions with `perms.py`.
6. Prints anything missing, or "ok".
7. Writes `~/.config/jarvis/discord_guild.json` `{guild_id, category_id,
   archive_category_id}`. This is the new `config.DISCORD_GUILD_PATH`.

`discord_guild.json` is added to `permissions.protected_paths()`, so no agent
can write it.

**Permission set: no Administrator, Manage Roles, Manage Server, Ban or Kick**

| Permission | Bit |
|---|---|
| Manage Channels | 16 |
| View Channel | 1024 |
| Send Messages | 2048 |
| Embed Links | 16384 |
| Attach Files | 32768 |
| Read Message History | 65536 |
| Create Public Threads | 1<<35 |
| Send Messages in Threads | 1<<38 |

**Total: `309237763088`.** Manage Channels is granted server-wide, because D4
means moving channels that sit outside the two categories.

**Invite URL shape** (the setup command fills in the application id the way v1
does):

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot%20applications.commands&permissions=309237763088&guild_id=<GUILD_ID>&disable_guild_select=true
```

- **`applications.commands` is needed for the slash work. Include it.**
  - Guild-scoped commands register instantly with `guild_id` from this file.
  - Interactions arrive over the existing gateway, and no extra intent is
    needed.
- Re-authorizing should update the bot's managed role. Verify this live. If it
  doesn't, edit the bot's role in Server Settings → Roles.

**Channel moves never send `lock_permissions` or `permission_overwrites`.**
Those would need Manage Roles.

**If the owner chooses Administrator anyway (bit 8)**
- **Setup:** it detects bit 8, skips the missing-permission check (everything
  passes), and prints this warning:
  > "The bot has Administrator. Channel overwrites cannot restrict it, and a
  > leaked token would control the whole server (roles, bans, webhooks,
  > settings). Recommended: remove Administrator and re-invite with
  > 309237763088."
- **Status:** `GET /discord` reports `admin: true`, and the HUD panel shows an
  amber "Administrator" note. It stays green otherwise.
- **Nothing else changes.** The code-level guarantees stay the same:
  `DiscordRest` has no delete method, and a test asserts no `DELETE` request
  is ever made.

### 2.2 Naming
- `slug(name)`: NFKD, lowercase, `[^a-z0-9]+` becomes `-`, capped at 90
  characters. An empty result becomes `project-<id>`.
- If the slug is taken, append `-<id[:4]>`. This covers the owner's two
  `e2e-calc` projects.
- Topic: `Jarvis project · <name> · <id>`.
- `Project.discord_channel_origin` (`"created"`, `"adopted"` or `None`) is
  kept **for display only**: the pill says "created by Jarvis" or "linked by
  you". It no longer changes behaviour.

### 2.3 HUD and API (owner-only, PR #4's `owner_only`)
- **`GET /projects/{id}/discord`** returns `{channel_id, origin, name,
  category, state, missing, checked_at}`.
  - `state` is one of `linked_ok | unlinked | not_found | no_access |
    wrong_guild | missing_permissions | folder_missing | unconfigured |
    unreachable`.
  - Cached 60 s, with a 5 s timeout.
- **`POST /projects/{id}/discord`** (owner-only) takes `{action: "create" |
  "link" | "unlink" | "archive_channel" | "restore_channel", channel_id?}`.
- **Link validation:**
  - The channel id is a snowflake.
  - The bot can see the channel.
  - It is in the configured guild.
  - It is a text channel (type 0).
  - It is not linked to another project.
  - No permissions are missing.
  - The project is not the Inbox and not archived.
  - **The project's root is an existing directory (D2).**
- `POST`/`PATCH /projects` now refuse `discord_channel_id` with a 400.
- **HUD:**
  - `ProjectDialog` (Pickers.tsx, PR #4) gets a `DiscordLink` control. It
    replaces the read-only `project-discord`. The Unlink text says the channel
    is kept.
  - The New Project dialog gets the checkbox.
  - Additions to `api.ts` and `types.ts`.

### 2.4 Channel lifecycle (D3, D4)

The created/adopted distinction is gone. The only rule is **who started the
action**.

- **The owner's action** is permission for that project's channel. Owner
  actions are:
  - an owner-only HUD route (archive, restore, delete, the Discord routes);
  - a `PATCH /projects` that passes the `owner_only` test;
  - a typed Discord command from the owner.
- **Anything else goes through the approval gate:**
  `ApprovalRequest(tool="discord_channel_<op>", args={channel, from, to},
  allowlistable=False, origin="Jarvis housekeeping")`.
  - It goes to the owner's DM and shows as a HUD card.
  - It is one-shot and typed-only. A timeout means no.
  - "always" is not offered.
- **Change to PR #4:** its `PATCH /projects` handler adds `"by": "owner" |
  "api"` to the `project_updated` event data, worked out with `owner_only` as
  a non-raising check. This closes a gap: an agent `curl`-ing a rename would
  otherwise get the channel renamed with no approval.

| Event | What happens to the linked channel (created or adopted alike) |
|---|---|
| `project_updated`, name changed, `by: owner` | Renamed to the slug, deferred on a 429 ("rename pending" on the pill) |
| `project_updated`, name changed, `by: api` | An approval request; renamed only on yes |
| `project_archived` (HUD, owner's) | Moved to Jarvis Archive, with a note: "Archived in Jarvis on <date>." |
| `project_restored` (owner's) | Moved back to Jarvis; renamed if the restore renumbered the name; "Restored." |
| `project_deleted` (owner's) | A note: "Permanently deleted in Jarvis; this channel is kept." Then unlinked. PR #4's event gains `discord_channel_id` |
| Jarvis category at ≥45 of its 50 channels (Jarvis-initiated) | One approval listing the channels of projects idle 60+ days: "Move these N to Jarvis Archive?" On yes, each is moved and gets a note |
| Jarvis Archive category at ≥45 | An approval to create "Jarvis Archive 2" |

- **Never delete:**
  - `DiscordRest` gets no delete method.
  - A test greps `jarvis/v2/discord/` for `"DELETE"`.
  - Recorded fake calls never contain `DELETE`.
- A channel moved for crowding stays linked and keeps working. Jarvis never
  moves it back by itself. The owner can type `restore channel` in it.

---

## 3. Making projects and links from Discord (PR B2, D2)

### 3.1 Verbs and dialogue
These are the handlers in `jarvis/v2/discord/commands.py`. Each is one pure
function. **[SLASH]** marks where a slash command maps onto it.

- **Who and where:**
  - Owner only, typed only. A voice note gets "Please type that."
  - Works in the owner's DM, in an unlinked channel (an @mention is needed,
    except inside the Jarvis category, where any unlinked channel counts as
    owned for these verbs), or in a linked channel where stated below.
- **No Discord verb archives or deletes a project (B11).**
  `archive project …` gets the reply "Only from the HUD."

| Verb (typed) | Handler | [SLASH] maps to |
|---|---|---|
| `new project <name> at <abs path>` | `new_project(ctx, name, path)` | `/project new name: path:` |
| `new project` alone | Asks "What's it called, and where's its folder? Reply `<name> at /absolute/path`." It keeps a pending entry for this channel (in memory, 10 min, cleared by `cancel` or a restart). | Not needed: the slash form takes both options |
| `new project at <path>` in an unlinked channel | The name defaults to the channel's name | The same handler |
| `link <project>` in an unlinked channel | `link_here(ctx, project)` | `/project link project:` |
| `unlink` in a linked channel | `unlink_here(ctx)`, after a `yes CODE` | `/project unlink` |
| `archive channel` / `restore channel` in a linked channel | `move_here(ctx, archive=True/False)`. The owner's command is the permission, so no code | `/channel archive` / `/channel restore` |

**`new_project` step by step**
1. `folders.check_new_root(path)` returns one of: refused (with the rule, said
   plainly), `create`, `adopt_empty` or `adopt_nonempty`.
2. If another project already uses the folder, refuse: "Project X already uses
   it; `link X` instead."
3. If the result is `create` or `adopt_nonempty`, Jarvis asks for a
   confirmation through the broker:
   - `ApprovalRequest(tool="project_folder_create"|"project_folder_adopt",
     args={name, path, summary}, allowlistable=False, origin="Discord: new
     project")`.
   - It is posted in **this channel**. The text names the full path, says
     "nothing inside will be changed" for an adoption, adds the 9p slowness
     note for `/mnt/c`, and ends with "Reply `yes CODE` or `no CODE`".
   - The existing answer rules apply: only here, typed only, one-shot, and a
     timeout means no. The HUD card can also answer.
   - **[SLASH]** `/yes code:` answers it.
4. `adopt_empty` needs no confirmation: it only reads.
5. After a yes, `folders.create_root(check, approval)` runs. Then the project
   is created through PR #4's create path (refactored into
   `projects.create_project(daemon, name, root)`), so it gets numbering and
   `project_created`.
6. Where the channel comes from:
   - **Asked in the owner's DM:** Jarvis creates `#slug` in Jarvis.
   - **Asked in an unlinked channel:** Jarvis links that channel, after the
     §2.3 validation.
   - **Asked in a linked channel:** refused: "This channel belongs to Y."
7. The reply names the project, the folder and the `<#channel>`.

**`link_here`**
- The owner's command is the permission.
- If the project is already linked elsewhere, a confirmation code asks "Move
  the link here? #old is kept."

### 3.2 The folder function: `jarvis/v2/folders.py`
No tool, MCP entry or open HTTP route reaches it. A test asserts it is called
only from `discord/commands.py` (and later the slash handler).

- **`check_new_root(path: str) -> RootCheck(kind, path, summary)`** raises
  `FolderRefused(reason)` for:
  - **Bad input:** not a string, a NUL or control character, not absolute, a
    `~`, any `..`, more than 4096 characters, a component over 255 characters.
  - **Outside the allowed roots,** checked both lexically and after
    `realpath` of the deepest existing ancestor. **Recommended policy:**
    - inside `$HOME`, at least one level below it;
    - inside `/mnt/c/Users/<owner>`, at least one level below it, where
      `<owner>` is configured and checked to exist (here `johnw`).
    - Everything else is refused by construction: `/`, `/etc`, `/usr`,
      `/var`, `/opt`, `/root`, `/tmp`, `/proc`, `/sys`, `/dev`, `/boot`,
      `/srv`, `/mnt/wsl*`, `/mnt/c` itself, `/mnt/c/Windows`,
      `Program Files`, `ProgramData`, other users.
  - **Protected even inside the roots:**
    - any path component starting with `.` (this covers `~/.ssh`,
      `~/.config/jarvis`, `~/.local/share/jarvis`, `~/.claude`, `~/.codex`
      and every `.jarvis/`);
    - explicitly as well: `V2_CREDENTIAL_DIRS`, `V2_DATA_DIR`,
      `SESSIONS_DIR`, `SPILL_DIR`, the allowlist's directory;
    - `REPO_ROOT` and everything below it;
    - any task worktree;
    - `AppData` under the Windows profile.
  - **On `/mnt/c`:** a final name with `<>:"\|?*` or a trailing space or dot.
  - **Parent must exist (recommended): yes.**
    - It is one `mkdir`, never `parents=True`. A typo in a middle folder then
      can't silently build a tree.
    - The reply is: "/x/y doesn't exist either; create it first or pick
      another path."
  - **Symlinks:** a final component that is a symlink, or a parent that
    resolves outside the roots or into a protected place.
- **Kinds:**
  - `create`: the path doesn't exist and its parent does.
  - `adopt_empty`: an existing empty real directory.
  - `adopt_nonempty`: an existing directory with contents.
    - The summary is read-only: an item count, whether it is a git repo, the
      branch.
    - No file contents are read.
- **`create_root(check, approval) -> Path`:**
  - Requires a resolved ALLOW for this exact path, and re-runs
    `check_new_root` to guard against the folder changing in between.
  - Calls `os.mkdir(path, 0o755)` exactly once, with no `exist_ok`. A
    `FileExistsError` means "Something appeared there; ask again."
  - **Writes nothing inside:** no README, no `git init`.
  - An adoption never calls `mkdir` and never touches anything.
- Logs INFO with the path, the project id and the approval code, and
  publishes `project_folder_created`.

---

## 4. Routing changes (gateway)

1. **`_locate`:** when the guild is configured, `task` and `project`
   placement require the message's `guild_id` to match.
   - A new place, `jarvis_category_unlinked`, is owned for the §3 verbs only.
   - Anything else typed there gets "This channel isn't linked yet:
     `link <project>` or `new project <name> at <path>`."
   - The category comes from `GET /channels/{id}` `parent_id`, cached.
   - This avoids needing the GUILDS intent for `CHANNEL_CREATE`.
2. **An archived project's channel** gets a fixed reply ("restore it in the
   HUD") and no chat or intake.
3. **Typed text in a `clarifying` thread** with an open blocking question
   calls `answer_question(task, first_blocking_index, text)`. Spoken text is
   refused (**D7**).
4. **Intake replies** point to the thread or `<#channel>`.
5. **The approval DM fallback** (§1).
6. **`router.place` and `classify`:** unchanged. The §3 verbs are parsed in
   `commands.py` before `classify`.
   - **[SLASH]** Interactions bypass the text parsing and call the same
     handlers with a `ctx` of `{channel_id, place, owner, typed=True}`.

---

## 5. Security and safety

- **Owner-only** (`should_respond`, unchanged), and the guild check.
- **Typed-only** for approvals, answers and every §3 verb.
- **Approvals only where asked,** including the folder confirmations.
- **No agent path to channels or folders:**
  - `"discord_"` is added to `fastpath.FORBIDDEN_PREFIXES`.
  - A test asserts the only Discord tool in `MCP_TOOLS` is
    `discord_dm_owner`.
  - Calls to `create_channel` and `modify_channel` are allowed only in
    `linker.py`, `commands.py` and `setup.py`.
  - `folders.create_root` is reachable only from `commands.py`.
  - The guild file is protected.
  - A rename initiated through the API is approval-gated (§2.4).
- **Never delete channels:** enforced in code and asserted in tests.
- **The token is never logged.** Everything goes through `DiscordRest`'s
  redaction.
- **Every test uses a fake transport,** and `httpx.request` is patched to
  fail.
- **Accepted, as in PR #4:** the Origin check is a speed bump. An agent's
  shell can forge it, but the folder and channel actions also need a
  Discord-typed confirmation, and those need the owner.

---

## 6. Tests

Each of these fails on today's main.

**`discord_render_check.py`**
- The write-once thread id.
- No thread at intake (D6).
- Auto-archive set to 10080, and the thread-member `PUT`.
- **D1:**
  - Each pinging milestone has `parse: []` and no `<@`, and is immediately
    followed by a post whose content is exactly `<@OWNER>` with
    `users: [OWNER]`.
  - Started, verified, cancelled and card edits are never followed by one.
  - DM fallback posts are never followed by one.
- Choices in the question post.
- The cancelled post.
- The DM fallback (replaces `test_missing_channel_silent_count`).
- Reconcile: active tasks, terminal tasks, seeded tasks, `embed_sha`.
- 50083 leads to unarchive plus one retry.
- A 429 with a 600 s wait raises at once.
- The circuit breaker.
- No token in any log.

**`discord_routing_check.py`**
- Another guild is ignored.
- The archived reply.
- A typed answer while clarifying is recorded; a spoken one is refused.
- The approval DM fallback, with its answer valid only in the DM.
- The Reporter and router together: thread, then approval, then the answer in
  the thread; an answer in the DM is refused.
- **The §3 dialogue:**
  - `new project` leads to the prompt, then the code.
  - `yes CODE` from another channel, a spoken one, or a non-owner does
    nothing.
  - On yes: the folder is created, the project is created, and the channel is
    created (DM) or linked (unlinked channel).
  - `link X`, `unlink` with a code, `archive channel`.
  - `archive project X` gets "HUD only".
- The surface starts and stops idempotently, with the Reporter started before
  `serve()`.

**New `tests/v2/folders_check.py`** (HOME and the Windows root patched to temp
dirs). One case each:
- relative path, `..`, `~`
- `/etc/x`, `/mnt/c/Windows/x`
- `<home>/.ssh/x`, `<home>/.config/jarvis/x`
- `V2_DATA_DIR/x`, `REPO_ROOT/x`
- `AppData/x`
- home itself
- missing parent
- symlinked parent pointing to `/etc`
- final component a symlink
- `create` without approval, or with an approval for a different path, raises
- `FileExistsError` handled
- an adoption leaves the folder's mtime and listing unchanged
- `mkdir` is called exactly once, with no `parents`

**New `tests/v2/discord_linker_check.py`**
- The slug table.
- `perms.py` math, including the Administrator flag and its warning text.
- The invite URL contains `bot%20applications.commands` and `309237763088`.
- Rename with `by: owner` is immediate; with `by: api` it is approval-gated
  and nothing happens on deny.
- Archive, restore and delete moves and notes.
- Crowding at 45 produces one `allowlistable=False` request; no on deny,
  moves on allow.
- No `DELETE` anywhere.

**Other suites**
- `hud_backend_check.py`:
  - `GET /discord`.
  - The owner-only 403 on `POST /projects/{id}/discord`.
  - The link validation table, including `folder_missing`.
  - `PATCH`/`POST /projects` with `discord_channel_id` returns 400.
  - The `project_updated.by` field.
- `archive_check.py` (PR #4): add the new routes to its owner-only list.
- `permissions_check.py`: a write to `discord_guild.json` is denied.
- HUD headless (`tests/face/hud_v2_projects_check.py`,
  `hud_v2_mock_projects.py`):
  - The pill renders each state.
  - A bad id shows the error.
  - Create is disabled when the guild isn't set up.
  - The Unlink text says "kept".
  - The checkbox.
  - The `DiscordPanel` states, including the Administrator note.

---

## 7. Live verification (owner)

**At your desk**
1. Make sure only `jarvis daemon2` is running. No v1 `jarvis face` or
   `jarvis daemon`, or every reply doubles.
2. Run `jarvis auth discord-guild`.
   - Open the printed invite (it has `applications.commands` and
     `309237763088`) and authorize it for your server.
   - Let it create the categories.
   - It should print "ok" and should **not** mention Administrator.
3. Restart daemon2 and check the HUD panel shows **Discord: ok**.
4. HUD → School → Edit → **Create channel**. Check `#school` appears and the
   pill says "created by Jarvis · ok".

**On your phone**

5. DM `new project`, then reply `robotics at /home/johnw/projects/robotics`.
   - You should get a code. Answer `yes CODE` **in a channel**: it should be
     refused.
   - Answer it in the DM. Check that the folder now exists and is empty, the
     project is in the HUD, and `#robotics` is under Jarvis.
6. DM `new project x at /home/johnw/.ssh/x` and check it is refused, naming
   the rule. Then try `/etc/x`, then a path whose parent doesn't exist; both
   should be refused too.
7. Create a channel `#scratch` yourself inside the Jarvis category and type
   `link test` in it. Check it links.
8. In `#school`, send `task: add one line to README saying hello`. Check:
   - a thread appears with "Started" and a card that updates at most every
     5 s;
   - on a question, approval or done, your phone buzzes and opens on the
     `@you` line right below the update.
9. If an approval comes, `yes CODE` in your DMs should be refused; answer it
   in the thread instead.
10. If the task asks a question, answer it in the thread with typed text.
    Check the task moves on.

**At your desk**

11. Rename School in the HUD. Check `#school` follows.
12. **Approval gate check:** from a terminal, run `curl -X PATCH
    http://127.0.0.1:<port>/projects/<id> -d '{"name":"x"}'` with no Origin
    header.
    - A DM approval should arrive. Answer `no CODE` and check the channel is
      unchanged.
    - Rename it back in the HUD afterwards.
13. Archive **test** in the HUD.
    - `#scratch` should move to Jarvis Archive with a note.
    - A message typed there should get the "archived" reply.
    - Restore it and check it moves back.
14. Remove "Send Messages in Threads" from the bot's role, then start a task.
    Check:
    - the panel turns amber and names the permission;
    - the daemon log shows `403 / 50013` and no token;
    - one DM arrives.

    Put the permission back and check the panel turns green.
15. Cut the network for a minute during a task. Check the panel goes red, then
    catches up with no duplicate thread.
16. Restart daemon2 in the middle of a task. Check there is no second thread
    and no second "Started".

---

## 8. Order, staffing, and where slash commands fit

**Recommended sequence.** PR B is split into **B1** and **B2**.

1. **PR A, the poster** (as in §1, with the D1 ping line). It has no slash
   dependency.
   - It adds **no new approval verbs**. Approvals already work as typed verbs
     on main.
   - PR A only moves where approvals are posted, adds the ping line and the
     DM fallback, and factors `answer()` out for slash to call.
   - **So landing slash before PR A is not needed:** nothing would be built
     twice, and PR A is the feature the owner asked for first.
2. **PR B1, linking:**
   - guild setup with the `applications.commands` invite;
   - `perms.py` and the protected guild file;
   - the linker with the D3/D4 rules and the `project_updated.by` change;
   - the HUD control and the owner-only routes;
   - the guild check and the archived reply in `_locate`.

   Slash needs `guild_id` and the new invite scope from B1, **so B1 lands
   before slash**.
3. **Slash PR (separate plan):**
   - the command core: interactions, registration and ephemeral replies;
   - `/yes /no /always` calling PR A's `answer()`, plus `/status /cancel
     /task`.

   It can start in parallel once PR A merges, using this plan's guild-file
   contract (path and keys).
4. **PR B2, Discord-side projects:**
   - `folders.py`, `commands.py` (§3) and `projects.create_project`;
   - link, unlink and archive-channel from Discord.

   Built **on the slash command core**, so the dialogue is written once and
   gets both a typed front end and a `/project` front end. B2 is the most
   security-sensitive piece (a filesystem write started from a phone), so it
   gets its own small review.

**Staffing**
- **Agent 1:** A, then B1, then B2.
- **Agent 2:** slash, after A merges. The two touch `gateway.py` in different
  places: Agent 1 changes `_locate` and `_post_approval`; slash adds the
  interaction dispatch.

**If fewer PRs are preferred:** merge B1 and B2 and land them after slash. Do
not merge A with anything.

**Critical files**
- `jarvis/v2/discord/reporter.py`, `gateway.py`, `rest.py`
- New: `jarvis/v2/discord/{linker,perms,routes,setup,commands}.py`
- New: `jarvis/v2/folders.py`
- `jarvis/v2/daemon.py`
- `jarvis/v2/stores.py`
- PR #4's `jarvis/v2/projects.py`
- `jarvis/v2/permissions.py`
- `jarvis/v2/providers/fastpath.py`
- `jarvis/config.py`
- `jarvis/__main__.py`
- `hud/src/components/Pickers.tsx`, `Panels.tsx`
- Tests: `tests/v2/discord_render_check.py`, `discord_routing_check.py`,
  `hud_backend_check.py`
- New tests: `tests/v2/folders_check.py`, `discord_linker_check.py`
- `tests/face/hud_v2_projects_check.py`

---

## 9. Still-open decisions (planner's recommendation for each)

1. **DM fallback for projects with no channel and for the Inbox.**
   **Recommend:** question, blocked, failed and done to the owner's DM, with
   no card and no ping line.
2. **D6 (pending): when a thread is created.** **Recommend:** at
   `clarifying`, after the proposal grace window. A withdrawn proposal leaves
   nothing in Discord.
3. **Allowed folder roots.** **Recommend:** `$HOME` and `/mnt/c/Users/johnw`,
   each at least one level deep, with dot-folders and the §3.2 list refused.
   Add more roots later in config.
4. **Must the parent folder exist?** **Recommend:** yes, so only one `mkdir`.
5. **Adopting an existing empty folder without a code.** **Recommend:** yes,
   because it only reads. A non-empty folder always needs the code.
6. **Who creates the categories.** **Recommend:** the setup command, after a
   y/N prompt. This is now possible because Manage Channels is server-wide.
7. **Crowding threshold and idleness.** **Recommend:** ask at 45 of 50
   channels, and propose channels idle 60+ days. Moving a channel back stays
   the owner's call (`restore channel`).
8. **A rename that doesn't come from the HUD** (for example an agent's
   `curl`). **Recommend:** approval-gated, as in §2.4. The alternative, never
   following such renames, leaves channel names stale.
9. **Renaming a project from Discord.** Not requested. **Recommend:** leave it
   out of this plan. The slash plan can add `/project rename` later on the
   same handlers, and it would count as the owner's action.
10. **Administrator.** **Recommend:** no (D5). If the owner chooses it, only
    the setup warning and the HUD note change (§2.1).
11. **Do new channels inherit the Jarvis category's privacy?** A risk to
    verify live: Discord's API may create the channel without copying the
    category's overwrites.
    - If the server ever has other members, check at step 4 that `#school` is
      private.
    - If it isn't, the fix needs Manage Roles, which the D5 set excludes. The
      decision is then either: add Manage Roles, or keep the server private.

**Accepted as recommended (D11–D18):**
- D11: the write-once `discord_thread_id`.
- D12: verify the GUILDS intent live, and add bit 0 if thread messages don't
  arrive.
- D13: check that no v1 listener is running at the same time.
- D14: the Origin check is a speed bump.
- D15: the 50-channel category cap. Now handled by the D3 crowding approval.
- D16: a rare duplicate post on a crash.
- D17: `project_deleted` gains the channel id.
- D18: reconcile when the bus drops events.

D3 and D4 change two of them:
- D14 is now narrower, because API-initiated renames are gated.
- D15 is now handled by the crowding approval rather than left to the owner.
