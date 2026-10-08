# Plan: Discord update poster, project channels and mirrored chats (Jarvis v2), revision 3

**Status:** revision 3, 2026-10-07. It follows
[`2026-10-07-discord-decisions.md`](2026-10-07-discord-decisions.md),
including "Discord open items, answered" and C1. Where they disagree, that
file wins. Slash plumbing is owned by
[`2026-10-07-slash-commands-plan.md`](2026-10-07-slash-commands-plan.md);
thread-to-thread messaging by
[`2026-10-07-peers-plan.md`](2026-10-07-peers-plan.md).

**Build order (S-1):** PR #4 → PR #3 → **S1 slash core** → **PR A** (poster) →
**B1** (guild and linking) → **C** (mirrored chats, new) → **B2** (projects and
folders from Discord) → **S2** (drop keywords) → **S3**.

**Deferred:**
- remembering the DM conversation's *pending dialogue state* across restarts
  (the DM chat itself is now persisted);
- the "which project?" follow-up;
- slash plumbing (owned by the slash plan; this plan marks where it plugs in).

## What you will see in Discord after this ships

**Layout**
- A **Jarvis** category with one channel per project, plus **#ungrouped** for
  Inbox work.
- Every project gets a channel. New projects get one automatically, and your
  four existing projects get one each after setup.
- Inside a project's channel, **every task and every chat is its own
  thread.**

**Chats are the same conversation in both places**
- A chat you start in the HUD appears as a thread in its project's channel
  (Inbox chats in #ungrouped) the moment you send its first message.
- What you typed shows as "You (HUD): …", followed by Jarvis's replies.
- Typing in that Discord thread continues the same chat, and the HUD shows it
  too.
- A new top-level message in a project channel starts a **new** chat, as a
  thread under your message.

**Your DM with Jarvis**
- It stays its own conversation and is not copied into #ungrouped.
- The HUD shows it as a chat.
- It is also the backup route if a project's channel ever breaks.

**Tasks**
- A task's thread appears when the task actually starts (`clarifying`).
- It has one status card, edited in place and never pinging, plus short
  milestone posts.

**Pings**
- Only approvals, questions, blocked, failed and done ping you.
- Each is followed by a separate one-line `@you`, so the notification lands
  right on it.
- Chat replies and everything else are silent. In DMs there is no extra line.

**Approvals**
- They appear in the thread that asked, with **Approve** and **Deny**
  buttons.
- "Always" is only the typed `/always`. Nothing is ever approved by voice.

**New projects from Discord**
- `/project new`. A bare name makes the folder `~/jarvis-work/<name>`; a full
  path is used as given.
- Jarvis shows the exact folder and makes it only after you confirm. It never
  overwrites anything.
- A Claude Sonnet or Opus chat can also offer to make a project for you;
  cheaper chats point you to `/project new`.

**Channel tidying**
- When you rename, archive or restore a project, its channel follows.
- If Jarvis wants to rename, move or archive a channel on its own (for example
  when the category gets crowded), it asks you with Approve/Deny first.
- **Channels are never deleted.**

**In the HUD**
- A Discord light shows ok, amber with the reason, or red.
- The project dialog shows the channel's status.
- Each chat links to its Discord thread.

---

## 0. What was verified in the code

- **The poster is never started.** `Reporter` is built only in a test.
  `start_discord()` builds only the `DiscordRouter`, and only after
  `runner.serve()`.
- **No guild id anywhere.** The token bundle has only `bot_token` and
  `owner_id`. The v1 invite is scope `bot` with permissions `68608`.
- **Owner data:**
  - projects School, e2e-calc ×2 and test, none with a channel; the Inbox is
    not yet created;
  - tasks: `571fbc98` is blocked, one is done;
  - the Windows profile is `/mnt/c/Users/johnw`.
- **Bug 1 (double threads):** `worktrees.ensure` holds a stale task object
  across `git worktree add`, then saves it whole, wiping `discord_thread_id`.
  - **The same lost-update class threatens any field Discord writes onto a
    `Thread`.** `daemon._record` and `_finish` call
    `threads.save(session.thread)` on every usage event, from the session's
    in-memory copy.
- **Bug 2 (answering questions):** moves to S1 (`_answer`, shared by
  `/answer` and plain text).
- **The owner's message is not published on the bus.**
  - `daemon.send` only appends `{"kind":"user", data:{text}}` to `log.jsonl`.
  - The text logged is the *assembled* text: `hud_api.assemble_turn` inlines
    attached files.
  - There is no record of where the message came from (HUD or Discord) or of
    the typed words alone.
  - `UserMessage` is `text`, `images` and `origin` only.
- **Threads have no automatic titles.** A title appears only through PR #4's
  `rename_thread`, which publishes `thread_updated`. PR #4 also publishes
  `thread_moved`, `thread_archived`, `thread_restored` and `thread_deleted`.
- **Discord chat today:**
  - `gateway._chat_threads` maps `"dm"` and each `project.id` to one fast-path
    thread, in memory only.
  - The gateway posts only the reply it waited for, and only the `text`
    records.
  - Approvals from chat threads have no `task_id`, so they always go to the DM.
- **Secret scrubbing exists.** `jarvis.tools.secrets.scrub` (already used by
  `assemble_turn`) can scrub everything posted to Discord.
- **PR #3:** `thread_model.effective(thread) -> (model, effort)`. The Claude
  default is `claude-opus-5-5`.
- **Peers plan, phase 0, builds two things this plan reuses:**
  - `runtime.caller()`, so a fast-path tool knows which chat called it;
  - per-session jarvis-mcp tokens, so a Claude or Codex chat's MCP call can be
    traced back to its chat.
- **Peers plan, phase 1:** proposes `Thread.surface = "discord:<channel_id>" |
  "dm"`. Reconciled in §4.2.

---

## 1. PR A: the update poster (after S1)

S1 already provides approval buttons, `/yes`, `/no`, `/always`, `/answer`,
bug 2 and the `Reply` sink. PR A builds none of those.

**Wiring: `DiscordSurface`**
- It owns `DiscordRest`, the `DiscordRouter` with S1's `InteractionRouter`, the
  `Reporter`, and later the `ChannelLinker` and `ChatMirror`.
- `start_discord(daemon)` returns it and sets `daemon.discord`.
- Start order:
  1. `daemon.start`
  2. Reporter subscribes
  3. `runner.serve`
  4. gateway
- Stop order:
  1. gateway
  2. runner
  3. `reporter.close(flush=True)` (2 s, bounded)
  4. linker and mirror
  5. hatch, then daemon
- Both start and stop are idempotent.

**Restarts**
- Sidecar `tasks/<id>/discord.json` holds the thread id, message ids, `phase`,
  `open_question`, `embed_sha` and `thread_gone`.
- **Bug 1 fix:** `TaskStore.save` never replaces a stored non-null
  `discord_thread_id` with None.
- **Reconcile on start (≤1 create per second):**
  - active tasks with a channel get a thread and a card;
  - a stale `embed_sha` triggers one edit;
  - terminal tasks are never backfilled;
  - pre-existing tasks are seeded silently.
- The reconcile runs again when the bus drops events.

**Posts (§11.3 with D1, D6, D8)**
- **When the thread is created (D6):** at the first snapshot whose phase is
  not intake, i.e. `clarifying`, after the proposal grace window.
  - The thread uses `auto_archive_duration=10080`.
  - The owner is added to the thread.
  - Started and the status card are posted.
- Card edits are coalesced to at most one per 5 s per thread and never ping.
- Milestones are capped at 400 characters, and the report at 1500 characters
  with `report.txt` for the rest.
- **Question:** from `task_question`, with its choices, plus "answer here or
  `/answer`".
- **Quiet posts:** Started, Verified and the new Cancelled post.
- **Ping-worthy:** question, approval, blocked, failed and done.
- **D1:**
  - The milestone itself goes out with `allowed_mentions.parse=[]`.
  - It is followed by a separate message: content `<@owner_id>`,
    `allowed_mentions={"parse":[], "users":[owner_id]}`.
  - New helper `DiscordRest.ping_owner(channel)`.
  - No ping line in DMs (decided).
- **Silent posts.** Every non-ping post in a guild carries the
  `SUPPRESS_NOTIFICATIONS` flag (4096). Otherwise a channel set to "All
  messages" on the phone would buzz on every card and chat line, against D1's
  "nothing else ever pings".

**DM is the safety net only (O1)**
- Before B1, no channels exist, so everything is in safety-net mode: attention
  milestones go to the DM, prefixed `[<project> · task <id>]`.
- After B1, the DM is used only when a project's channel is broken (Discord
  codes 10003 or 50013), and the HUD light says so.

**Approvals in task threads**
- `_post_approval` (S1 adds the buttons and message id) prefers the task
  thread.
- New:
  - if posting to the thread fails, post to the DM and record the **DM** as
    where the request was asked;
  - add the D1 ping line (not in DMs).

**Rate limits and failures**
- `DiscordError` gains `.status`, `.code` and `.retry_after`.
- `_api` sleeps at most 10 s; longer waits raise, and the caller defers.
- Circuit breaker: 3 failures → back off 30 s, then 60 s, up to 5 min, then
  reconcile.
- Edits are sent first. On code 50083 (thread archived), unarchive and retry
  once.
- **Logging:** the log names the operation, HTTP status and Discord code,
  **never the URL**. Interaction webhook paths carry an interaction token (S1).
- **`GET /discord`:** `{connected, guild, gateway, commands (S1), reporter,
  linker, mirror, permissions}`.
- **HUD:** SSE `discord_status` and a `DiscordPanel` in `Panels.tsx`.
- **DM alert:** a permission error sends one DM per channel per 24 h.

---

## 2. Permissions, setup and the #ungrouped channel (part of B1)

**D5 is decided: no Administrator.** The server-wide set:

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

- **Integer: `309237763088`.**
- Invite shape: `https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot+applications.commands&permissions=309237763088&guild_id=<GUILD_ID>&disable_guild_select=true`
- The owner switches the permissions on by hand; the invite is the shortcut.
- `applications.commands` is included (S1 needs it).
- **Open item O-P1:** renaming or archiving the bot's *own* threads should work
  as their creator; if the live check fails, add Manage Threads (integer
  becomes `326417632272`).

**`jarvis auth discord-guild`**
- Human-only. Code in `jarvis/v2/discord/setup.py`, plus a `cmd_auth` choice.
- **What it does:**
  1. Lists guilds; the owner picks one.
  2. Prints the invite.
  3. Checks permissions, listing **missing** (blocking) and **excess** (a
     warning; Administrator gets a loud warning).
  4. After a y/N prompt (O6), creates **Jarvis**, **Jarvis Archive** and
     **#ungrouped** (under Jarvis).
  5. Writes `~/.config/jarvis/discord_guild.json` `{guild_id, category_id,
     archive_category_id, ungrouped_channel_id}`.
- The guild file is a protected path.
- On start, the daemon links the Inbox to `ungrouped_channel_id`.
  - The Inbox's folder is `$HOME`, which exists, so the rule "every channel
    links to a project with a real folder" holds.
  - The Inbox's channel cannot be relinked from the HUD or Discord. Only
    re-running setup changes it.
  - If #ungrouped is broken, Inbox work falls back to DM, and the light goes
    amber.
- **Never delete** is enforced in code: `DiscordRest` has no delete method, and
  a test asserts no `DELETE` is ever sent.

---

## 3. B1: linking projects to channels

**Model**
- `Project.discord_channel_id` (exists).
- New `discord_channel_origin: "created" | "linked" | None`, for display only.
- A link requires `Path(project.root).is_dir()` and a project that is not
  archived.

**Naming:** the slug is lowercase ASCII with hyphens, ≤90 characters; empty
becomes `project-<id>`; a clash gets `-<id[:4]>` appended. Topic: `Jarvis
project · <name> · <id>`.

**Every project gets a channel (O1)**
- The New Project dialog's "Create a Discord channel" box is on by default.
- The Discord panel offers **"Create channels for 4 projects"** once after
  setup.
  - It is an owner-only route, `POST /discord/backfill`, because the CLI cannot
    pass the HUD's owner check.
  - Linking runs that project's reconcile, so the blocked e2e-calc task gets
    its thread.

**HUD routes** (`discord/routes.py`, mounted like PR #4's `projects.route`)
- `GET /projects/{id}/discord` returns the state, the missing permissions and
  the origin. Cached 60 s; 5 s timeout.
- `POST /projects/{id}/discord` (owner-only): `create`, `link {channel_id}` or
  `unlink`.
- **Link validation:** snowflake; the bot can see the channel; same guild; text
  channel; not linked to another project; no missing permissions.
- `POST`/`PATCH /projects` refuse `discord_channel_id` with 400.

**D4 (decided)**
- **The owner's actions act at once:**
  - the owner-only HUD routes;
  - a `PATCH /projects` rename that passes `projects.owner_only` (the
    `project_updated` event gains `by: "owner" | "api"`);
  - any slash command the owner ran.
- **Anything Jarvis starts goes through the broker:**
  - `ApprovalRequest(tool="discord_channel", args={action, channel, from, to,
    why}, origin="Jarvis housekeeping", allowlistable=False)`, on the linker's
    worker.
  - It is shown with Approve/Deny (S1), never Always.
  - A deny or a timeout does nothing.
- **What counts as Jarvis-initiated:**
  - a rename after an `api` PATCH;
  - D3 crowding archives (at 45 of 50 channels, channels idle **30 days**);
  - an overflow "Jarvis Archive 2" category;
  - a move back the owner did not type.

**Lifecycle (`ChannelLinker`, any linked channel)**

| Event | Action |
|---|---|
| Rename, `by: owner` | Rename the channel now. On a 429, defer it, and the HUD shows "rename pending". |
| Rename, `by: api` | Ask through the approval gate, then rename |
| `project_archived` | Move to Jarvis Archive (no `lock_permissions`) and post a note |
| `project_restored` | Move back, renaming if PR #4 renumbered the name, and post "Restored." |
| `project_deleted` | Post a note and unlink. **Never deleted.** PR #4's event gains `discord_channel_id`. |

**HUD:** a `DiscordLink` control in `ProjectDialog` (pill, Create, Link,
Unlink), the New Project checkbox, and the archive confirmation showing where
the channel goes.

---

## 4. PR C: every chat is a Discord thread (C1)

### 4.1 Places after C

| Where a message arrives | What it is |
|---|---|
| A DM | The **DM conversation**: an Inbox chat with `surface="dm"`, now persisted. Not copied into #ungrouped (recommended). |
| A task thread (`Task.discord_thread_id`) | That task (S1: plain text answers or steers) |
| A chat thread (`Thread.surface == "discord:<id>"`) | A turn in that chat |
| A top-level message in a project channel or #ungrouped | **A new chat** in that project or the Inbox. Jarvis starts a Discord thread *from the owner's message* (Start Thread from Message, `POST /channels/{c}/messages/{m}/threads`) and replies inside it. |
| An archived project's channel, or an archived chat's thread | A fixed reply: "restore it in the HUD" |
| Anything else | Ignored. S1 allows only `/project link` there. |

- This replaces `gateway._chat_threads`. One rolling chat per channel would mix
  unrelated conversations and could not be mirrored one-to-one.
- New chats from Discord stay on the fast path until S3's `/new provider:`.

### 4.2 One field, reconciled with the peers plan

**`Thread.surface: str | None`**
- `None`: not shown on Discord yet.
- `"dm"`: the DM conversation.
- `"discord:<id>"`, where `<id>` is the **Discord thread's own id**. A Discord
  thread is a channel, so the peers plan's encoding is unchanged; only the
  meaning narrows from "a channel's chat" to "this chat's thread".

**Rules**
- It is set by the gateway (when a chat starts from Discord) or by the
  `ChatMirror` (at the first message of a HUD chat).
- It changes only through `stores.threads.set_surface(id, value)`, under the
  store lock.
- `ThreadStore.save` never replaces a stored non-null `surface` with None. The
  daemon's stale session copies make this necessary, the same bug class as
  bug 1.
- Sidecar `threads/<id>/discord.json`: `{surface, retired: [ids],
  mirrored_through: <log line count>, name}`.

**Changes for the peers plan:**
- rename its §1 row to "each thread in a project channel is a chat or a task";
- drop the idea of "the project channel's chat";
- post its quiet `↪ Request from thread "X"` lines through this `ChatMirror`,
  so there is one mirror. Peers phase 1 comes after PR C.

### 4.3 What the mirror posts

New module `jarvis/v2/discord/mirror.py`, class `ChatMirror`. It runs on its
own worker, separate from the Reporter, so a busy chat never delays task
milestones.

**Which chats:** only `Role.CHAT` threads. Task worker threads are never
mirrored; the task has its own thread.

**When:** at the chat's **first owner message**, not when compose opens.
Specifically, at the first `user_message` on a thread with `surface=None` whose
project has a working channel.

**Prerequisite:** `daemon.send` publishes a new bus event `user_message`:
- shape: `{thread_id, project_id, turn_id, data: {typed, via:
  "hud"|"discord"|"dm"|"system"|"peer", origin, images: n, attachments:
  [names], discord_message_id}}`;
- the same fields go into the `user` log record;
- `UserMessage` gains `via`, `typed` (the owner's words before file inlining)
  and `discord_message_id`, all with defaults;
- `assemble_turn` fills `typed`;
- `docs/hud-api.md` adds the SSE event.

**Thread name**
- The chat's title, else `<id> · <first 50 characters of the first message>`,
  capped at 100 characters.
- The id alone is unreadable on a phone; this is the id fallback plus the
  excerpt.
- Renamed on `thread_updated` (the owner's HUD rename, an owner action).
- A rename made inside Discord is ignored: the HUD is the source of names.

**What is posted**

| HUD event | Discord post (silent unless noted) |
|---|---|
| The owner's HUD message | `You (HUD): <typed>`, or `You (HUD, voice): …` for dictation. Over 2000 characters it is attached whole (existing behaviour). Attachments are listed by name only (`[attached: notes.md]`); file contents never go to Discord. Images become a note, `[2 images, in the HUD]` (O-C2). |
| Settled `text` events | Jarvis's reply, split at 2000 characters with `_split`. Never deltas, never thinking. |
| `turn_finished` | If tools ran, one footer line appended to the last reply (`· 4 tools: read_file, grep_files +2`). An error or interrupt becomes `· turn failed (<class>)` or `· interrupted`. |
| Approval | `_post_approval` resolves `request.thread_id` to the chat's surface. The post has buttons and a **ping line**. |
| Provider question (Codex `requestUserInput`) | The question, then a **ping line**. The owner's next typed message in that thread answers it (the existing `/threads/{id}/answer` path) instead of starting a turn. |
| Peer-driven turn (peers plan) | The quiet `↪ Request from thread "X"` line, then the reply |

- Everything posted is run through `secrets.scrub()` first, so a pasted key
  never reaches the server.
- Every mirrored post is silent (flag 4096). The only pings are the D1 lines
  for approvals and questions.

**Messages typed in Discord**
- Run as `UserMessage(via="discord", discord_message_id=…)`. The mirror never
  echoes them, because the owner's message is already there.
- The HUD shows them as the owner's messages, labelled "via Discord".
- **Images:** passed to the turn under `assemble_turn`'s caps (8 files, 4 MB
  each). Text files are inlined under the same rules, and protected file names
  are refused. Other file types are refused with a note.
- **Voice notes** in an owned thread are transcribed (v1 rules) and run with
  `spoken=True`. The reply comes as text plus speech, as today's DM does. A
  voice note never approves or answers anything (S-2, D7).
- **The owner's message while a turn is still running:** queued, up to 3, with
  a quiet "I'll take this next".

**Edits and deletes**
- Editing or deleting a Discord message after it ran changes nothing.
  `MESSAGE_UPDATE` and `MESSAGE_DELETE` are not dispatched.
- The HUD has no message edit.
- Jarvis never edits or deletes mirrored posts, apart from card edits and
  stripping approval buttons.

**Echo prevention**
1. Bot authors are never heard (v1 `should_respond`).
2. `via="discord"` messages are never re-posted.
3. A bounded set of processed `discord_message_id`s (the last 500) stops a
   duplicate dispatch from running twice.
4. Jarvis posts as the bot, never through a webhook that could look like a
   user.

### 4.4 Chat lifecycle

- **Moved to another project in the HUD (`thread_moved`).** Discord cannot move
  threads. **Recommended:**
  1. Create a new thread in the new channel with the same name.
  2. Its first line reads "Continued from <#old>".
  3. The old thread gets "Moved to <project> → <#new>".
  4. `surface` points to the new thread; the old id goes to `retired`.
  5. A message typed in the old thread still runs the chat (P-0), and the
     reply posts there with a pointer to the new thread.
  6. The rejected alternative, keeping the chat in the old channel, would leave
     School chats under e2e-calc forever.
- **Archived (PR #4, owner-only):** a quiet note "Archived in the HUD", then
  the Discord thread is archived (as its creator; see O-P1). A message typed
  there gets the fixed "restore it in the HUD" reply.
- **Restored:** "Restored." The next post reopens the thread.
- **Permanently deleted:** a note, then unlinked. The Discord thread is
  **kept**.
- **The whole project is archived:** the channel moves (§3); its threads go
  with it.

### 4.5 Rate limits and restarts

- **Rate limits:**
  - Each Discord thread has its own queue, limited to 4 posts per 5 s.
  - Above 12 pending posts, the rest collapse into one line, "(N more
    messages, open the HUD)", with the turn attached as `reply.txt`.
  - 429s and the breaker are as in PR A.
- **Progress is tracked against `log.jsonl`, not the bus.**
  - Each bus event only triggers "bring thread X up to the end of its log".
  - So bus drops and outages are recoverable.
- **Restart reconcile:**
  - For each chat with a surface, any log lines after `mirrored_through` are
    posted if there are 6 or fewer.
  - Otherwise one line: "(N messages while Discord was unavailable, see the
    HUD)".
- **Linking a project later** does not backfill old chats. A chat gets its
  Discord thread at its next message.

### 4.6 Inbox, DMs and HUD changes

- Inbox **tasks** and **HUD chats** go to #ungrouped as threads.
- **The DM conversation:**
  - It is one Inbox chat with `surface="dm"`, found by that value instead of
    the in-memory cache.
  - HUD turns typed into it are posted to the DM **silently**.
  - Replies to messages the owner sent from Discord notify, as DMs do today
    (O-C3).
  - S3's `/new` sets the new chat to `"dm"` and the old one to `"dm:retired"`.
- **HUD:**
  - `GET /threads` includes `surface`.
  - The chat header shows "On Discord: #school › <name>" with a
    `https://discord.com/channels/<guild>/<id>` link.
  - Messages that arrived from Discord show "via Discord".
  - Files touched: `ChatTab.tsx`, `types.ts`, `api.ts`.

---

## 5. B2: projects and folders from Discord (slash-first)

**Commands** (the slash plan registers them; this plan supplies the handlers):

| Command | What it does |
|---|---|
| `/project new name:<> folder:<optional>` | No folder given: a bare name means `~/jarvis-work/<slug(name)>`. With nothing filled in, it opens a modal asking for Name and Folder. That covers D2's "asks for name and path", with no typed dialogue. |
| `/project link project:<>` | Links the unlinked channel the owner is in |
| `/project unlink` | Unlinks the channel the owner is in. The channel is kept. |
| `/project channel project:<>` | Creates a channel for a project that has none |
| `/channel archive` · `/channel restore` | Moves this project's channel to Jarvis Archive and back. The project stays active, and threads keep working. The owner typed it, so it counts as permission (D4). |

- **Typed forms: not needed.**
  - S2 drops keywords, and B2 lands after S1, so it adds no plain-text verbs.
  - The rev-2 typed dialogue and its in-memory pending state are removed.
  - Natural language goes through the model path below.
- Archive and delete of **projects** stay HUD-only (B11). The fixed reply comes
  from the slash plan's tripwire.

**After `/project new`:**
1. `folders.check_project_folder` runs, then a confirmation if one is needed.
2. Then `folders.make_project_folder`, `projects.create_project` (PR #4
   numbering, refactored out of `POST /projects`), and the channel.
3. From a DM, the channel is created in the Jarvis category. From an unlinked
   channel, that channel is linked instead.

**The confirmation**
- A broker request: `ApprovalRequest(tool="project_folder", args={action:
  "create"|"adopt", path, name}, allowlistable=False)`.
- It shows the **exact path**, with Approve and Deny, also as a HUD card.
- It is one-shot, owner-only, never by voice, and denied on timeout.
- **An empty existing folder is used without asking (O5).** A missing folder
  asks first. A non-empty folder asks, showing its entry count and whether it
  is a git repo.
- The check runs again after the owner's yes. If the folder's state has
  changed, Jarvis asks again.

**`jarvis/v2/folders.py`**
- **Input:** a string with no control characters.
  - A bare name with no `/` means `~/jarvis-work/<slug>`.
  - A leading `~/` is expanded once.
  - Otherwise the path must be absolute.
  - No `..`; ≤4096 characters in total and ≤255 per component.
- **Checking:** done lexically **and** on the realpath of the nearest existing
  ancestor.
- **Allowed roots:** `$HOME` and `/mnt/c/Users/johnw`, strictly below them and
  never the roots themselves. `~/jarvis-work` itself is refused as a project
  folder.
- **Refused:**
  - any component starting with `.`;
  - `config.V2_CREDENTIAL_DIRS`, `config.V2_DATA_DIR` and `config.REPO_ROOT`,
    checked explicitly;
  - `/mnt/c/Users/johnw/AppData`;
  - under `/mnt/c`, names Windows cannot hold;
  - a path that is already another project's root;
  - a file or a symlink at the path.
- **The parent must exist (O4).** If `~/jarvis-work` is missing, that is
  reported.
- **Creating:** exactly one `os.mkdir(path, 0o755)`, with no `parents` and no
  `exist_ok`. Nothing is ever written inside it.
- **Reachability:** only the confirmation handler calls it. It is not a tool
  and not an HTTP route.

**The model rule (O3)**
- **New tool `project_propose(name, folder="")`.** It never creates anything.
  It raises the same `project_folder` confirmation, from the chat that called
  it.
- **Enforced in the daemon, not in the model or the MCP process:**
  - The tool reaches the daemon route `POST /project-proposals`.
  - The daemon identifies the calling chat by its jarvis-mcp session token
    (peers phase 0), or by `runtime.caller()` for in-process tools.
  - It allows the call only if `thread.provider == ProviderName.CLAUDE`
    **and** `thread_model.effective(thread)[0]` matches
    `^claude-(sonnet|opus)-` once resolved.
  - Anything else is refused, including an unknown or unresolved model.
  - Limits: one pending proposal per chat, 3 per hour.
- **The fast path cannot propose at all.**
  - `project_propose` is not in `FAST_TOOLS`.
  - Its system prompt says: "To make a project, the owner can use `/project
    new`, or ask in a Claude chat."
  - A Claude model reached through OpenRouter on the fast path does not count.
- **Codex** sees the tool through MCP but is refused, with the same pointer.
- **The folder is always made by code**, after the owner's yes, from the
  request's stored args. The model never makes it.
- **Dependency:** this part needs peers phase 0. If that has not landed, B2
  ships the slash path, and the model path follows as a small B2b.

---

## 6. Security

- **Owner-only:** v1 `should_respond`; S1's interaction gate; the guild check
  in `_locate` and on `interaction.guild_id`.
- **Answers count only where the question was asked.**
  - Approvals, folder confirmations and housekeeping requests all go through
    the broker with `allowlistable=False`.
  - Buttons are checked against owner, message id and channel (S1).
- **No voice approvals and no voice answers** (S-2, D7).
- **No agent can create, rename, move or delete a channel, or create a
  folder:**
  - the `discord_` tool prefix is forbidden;
  - only `linker.py`, `setup.py` and the B2 handlers call channel writes;
  - `make_project_folder` is called only by the confirmation handler;
  - grep tests prove both;
  - the guild file is protected; `PATCH` no longer takes the channel field;
    non-owner renames are tagged `by: api` and gated.
- **Known gap (not closed):** a model's own shell can still `mkdir` under the
  existing permission gates. The O3 rule governs *making projects*. Risk R-M
  below.
- **No secrets reach Discord or the logs:**
  - everything mirrored goes through `scrub()`;
  - file contents are never mirrored;
  - tokens are never logged, URLs are never logged, and both the bot token and
    interaction tokens are redacted.
- **No live Discord in tests:** a fake transport, `httpx.request` patched to
  raise, and temp folder roots.

---

## 7. Tests

Each test below fails on today's main.

**PR A, `discord_render_check.py`**
- The bug 1 stale save keeps the thread id.
- No thread during intake (D6).
- `auto_archive_duration` is set, and the owner is added to the thread.
- **D1:** the ping line has the exact content and `allowed_mentions`; quiet
  posts carry flag 4096; DMs get no ping line.
- `task_question` choices; the Cancelled post.
- Safety-net DMs, including the broken-channel case.
- Reconcile and `embed_sha`.
- 50083 handling; a long 429 raises; the breaker trips.
- Logs contain neither tokens nor URLs.

**PR A, `discord_routing_check.py`**
- Approval DM fallback with the answer channel recorded.
- The ping line after an approval.
- Startup and stop are idempotent; the Reporter subscribes before `serve()`.

**B1, new `discord_linker_check.py`**
- Slug table and `perms.py` table.
- D4: `by: owner` renames now; `by: api` raises an approval request with no
  call before the yes; a deny means no call.
- Lifecycle notes and moves.
- D3 at 45 channels, and the overflow category gated.
- **No `DELETE` ever.**

**B1, setup check**
- Missing and excess permissions listed; the Administrator warning text.
- Categories and #ungrouped are created only after y.
- The Inbox is linked to #ungrouped and cannot be relinked.

**B1, `hud_backend_check.py`**
- Owner-only routes, the validation table, `POST /discord/backfill`.
- `PATCH` refuses the channel field; `project_updated.by`.

**PR C, new `discord_mirror_check.py`**
- No Discord thread until the first owner message; then exactly one.
- Name fallback, and rename on `thread_updated`.
- `You (HUD):` shows the typed text, never the inlined file contents;
  attachments by name; images as a note; scrubbed output.
- Replies are split at 2000; one tool footer; no deltas or thinking.
- A chat approval lands in the chat's thread with buttons and a ping.
- A Codex question pings, and the next typed message answers it.
- A message typed in Discord runs the same chat, is not echoed, and is labelled
  "via Discord" in the transcript.
- A duplicate message id runs once; bot authors are ignored.
- A message during a running turn is queued (up to 3).
- A voice note: the turn runs, but approvals stay untouched.
- A top-level message in a project channel starts a new chat with a thread from
  that message; in #ungrouped, an Inbox chat.
- The DM chat persists across a restart through `surface="dm"`.
- **Move:** a new thread with both link lines; the old id is an alias.
- Archive, restore and delete notes; nothing deleted.
- Above 12 pending posts the queue collapses.
- Restart reconcile with ≤6 and with >6 messages.
- `ThreadStore.save` keeps `surface` against a stale copy.

**PR C, `hud_backend_check.py` and HUD headless**
- `user_message` SSE.
- `GET /threads` includes `surface`.
- The chat header link, and the "via Discord" label.

**B2, new `folders_check.py`**
- Every refusal case.
- The bare-name mapping, `~/` expansion, and a missing parent.
- An empty folder is used silently; a non-empty one asks.
- Exactly one directory is made, with nothing written inside.
- A race leads to asking again.

**B2, `discord_commands_check.py` additions**
- The modal for `/project new`.
- Confirmation shows the exact path; one-shot; never "always".
- Link and unlink; `/channel archive|restore` act immediately.

**B2, model-rule check**
- Allowed: Claude with `claude-opus-*` or `claude-sonnet-*`.
- Refused: fast path (`project_propose` is absent from `FAST_TOOLS`), Codex, an
  unknown model, a Claude model on OpenRouter.
- The folder exists only after the yes.

---

## 8. Live verification (owner)

**At your desk**
1. Check that only daemon2 is running, and that the Interactions Endpoint URL
   is empty (S1).
2. Run `jarvis auth discord-guild`. Open the invite, switch on the 8
   permissions, and let it create Jarvis, Jarvis Archive and #ungrouped. It
   should report nothing missing.
3. Restart daemon2. The light should be green. Click **"Create channels for 4
   projects"**.
4. Open a new HUD chat in School and send "hello".
   - A thread should appear in #school with "You (HUD): hello" and the reply.
   - Your phone should not buzz.

**On your phone**

5. Reply in that thread. The HUD should show it "via Discord", and the answer
   should come back to both.
6. Post a new top-level message in #school. A new thread should start under it,
   and a new chat should appear in the HUD.
7. Run `/task` in #school with a harmless brief.
   - The thread should appear once it starts.
   - The card should update silently.
   - Question, approval and done should each ping, with the tap landing in the
     thread.
   - Approve with the button.
8. In a chat, trigger an approval. It should appear in that chat's thread, not
   the DM.
9. Run `/project new name:robotics`. It should offer `~/jarvis-work/robotics`.
   - Tap Approve. The folder should exist and be empty, and #robotics should
     appear.
   - Then try `/project new folder:/home/johnw/.ssh/x`: it should be refused.
10. In a Claude Opus HUD chat, ask it to make a project "test-sonnet".
    - You should get the exact path to approve.
    - Try the same in a fast-path chat: you should be pointed to `/project
      new`.
11. DM Jarvis. The conversation should stay in the DM and show in the HUD
    Inbox, but not in #ungrouped.

**At your desk**

12. Rename School in the HUD, and move a chat from School to test.
    - The channel should be renamed.
    - The moved chat should get a new thread in #test, with "Moved
      to"/"Continued from" links.
13. Archive test, then restore it. Check the channel moves both ways, with
    notes.
14. Remove "Send Messages in Threads" from the bot. The light should turn
    amber, one DM should arrive, and the log should show `50013` with no token
    or URL. Put the permission back.
15. Restart daemon2 while a chat and a task are both active. There should be no
    duplicate threads, and the catch-up line should appear if more than 6
    messages are missing.
16. Rename a chat in the HUD. If the Discord thread is not renamed, the light
    should show the 403; that settles O-P1.

---

## 9. Sequence and staffing

**Order:** PR #4 → PR #3 → **S1** → **PR A** → **B1** → **C** and **B2** in
parallel → **S2** → **S3**. Peers phase 0 must land before B2b; peers phase 1
comes after C.

**What each PR contains**
- **PR A:** shrinks, because S1 took bug 2, the buttons and the reply sink. Its
  tests run on DMs alone (safety-net mode).
- **B1:** setup (permissions, scopes, categories, #ungrouped), `perms.py`, the
  linker with D3/D4, the routes, backfill, the HUD link control.
- **C:** new. The mirror, `Thread.surface`, `user_message`, `_locate` and the
  new-chat-per-message rule, the persisted DM chat, chat approvals.
  - It is a separate PR because it is the largest change and reshapes
    `gateway._chat`.
  - Folding it into B1 would make B1 too large to review on a phone.
- **B2:** `/project` and `/channel` handlers, `folders.py`,
  `projects.create_project`.
  - B2b: `project_propose` and the model rule, after peers phase 0.

**Staffing**
- Agent 1: A → B1 → C.
- Agent 2: B2 once B1 merges, in parallel with C. They touch mostly different
  files (`folders.py`, `projects.py`, `interactions.py` handlers versus
  `mirror.py` and `gateway._locate`/`_chat`).
- S2 waits for C, so the plain-text path is final before keywords go.

---

## 10. Decisions

**Decided:**

| | Decision |
|---|---|
| D1 | A separate ping line, none in DMs |
| D2 | Projects and folders from Discord, owner-confirmed |
| D3 | Never delete channels |
| D4 | The owner's actions count as permission; Jarvis-initiated changes go through the gate |
| D5 | No Administrator; `309237763088`, plus `applications.commands` |
| D6 | The task thread is created at `clarifying` |
| D7 | Typed answers only |
| D8 | A quiet Cancelled post |
| D11–D18 | As recommended |
| O1 | Every project gets a channel; DM only as a safety net and for the DM conversation |
| O3 | Folders in `~/jarvis-work`, with the model rule |
| O4 | The parent must exist |
| O5 | An empty folder is used silently |
| O6 | Setup creates the categories |
| O7 | Crowding at 45 channels, 30 days idle |
| R7 | Channel privacy is the owner's to handle |
| S-1, S-2, S-3 | Slash first; no voice approvals and buttons allowed; keywords phased out |
| C1 | Every chat is a Discord thread; the mirror is two-way |

**Still open (the planner's recommendation for each):**
1. **O-P1, Manage Threads:** don't add it up front. Renaming and archiving the
   bot's own threads should work as their creator. If live step 16 shows a
   403, add it (`326417632272`).
2. **O-C1, a new top-level message in a project channel:** starts a new chat,
   threaded under the message. The alternative is one rolling chat per channel.
3. **O-C2, HUD images on Discord:** a note only, `[2 images, in the HUD]`,
   never uploaded. Images sent *from* Discord do reach the chat.
4. **O-C3, notifications in the DM:** replies to messages sent from Discord
   notify, as today; anything typed in the HUD and copied to the DM is silent.
5. **O-C4, the DM conversation in #ungrouped:** no. It stays DM-only and shows
   in the HUD Inbox.
6. **O-C5, moving a chat to another project:** a new Discord thread with links
   both ways; the old thread still works as an alias.
7. **O-C6, a message while a turn is running:** queue up to 3, with a quiet
   "next" note.
8. **O-C7, opting a chat out of Discord:** none at first, because C1 says every
   chat.
9. **O-M1, a cheap-model chat asked to make a project:** it cannot propose one;
   it points to `/project new` or a Claude chat. A Claude model reached through
   OpenRouter on the fast path does not count.
10. **O-S1, slash-first B2:** no typed forms; `/project new` with nothing filled
    in opens a modal asking for the name and folder.

**Risks:**
- **R-M:** a model's shell can still `mkdir` under existing gates. The O3 rule
  covers making *projects*. Adding an always-ask rule for `mkdir` outside the
  project root would close it.
- **R12:** the `GUILDS` intent; verify that thread messages arrive.
- **R13:** a v1 listener running alongside doubles message replies. Slash
  commands are immune.
- **R-T:** the server can hold at most 1000 active threads. Chats auto-archive
  after 7 idle days, so this is far off.

### Critical files
- `jarvis/v2/discord/gateway.py` (`_locate`, `_chat` replaced,
  `_post_approval`)
- `jarvis/v2/discord/reporter.py`
- `jarvis/v2/daemon.py` (`send` publishes `user_message`, `start_discord`,
  `main`, `/projects`)
- `jarvis/v2/stores.py` (write-once `Task.discord_thread_id` and
  `Thread.surface`, `set_surface`)
- `jarvis/v2/discord/rest.py`
- New: `jarvis/v2/discord/{surface,linker,mirror,perms,routes,setup}.py`,
  `jarvis/v2/folders.py`
- Also changed: `jarvis/v2/model.py` (`Thread.surface`,
  `Project.discord_channel_origin`), `jarvis/v2/provider.py`
  (`UserMessage.via`, `typed`, `discord_message_id`), `jarvis/v2/hud_api.py`
  (`assemble_turn` sets `typed`), PR #4's `jarvis/v2/projects.py`, PR #3's
  `jarvis/v2/thread_model.py` (`effective`), `jarvis/v2/providers/fastpath.py`,
  `jarvis/v2/permissions.py`, `jarvis/config.py`, `jarvis/__main__.py`,
  `hud/src/components/{Pickers,Panels,ChatTab}.tsx`
- Tests: `tests/v2/discord_render_check.py`, `discord_routing_check.py`,
  `hud_backend_check.py`, new `discord_linker_check.py`,
  `discord_mirror_check.py`, `folders_check.py`,
  `tests/face/hud_v2_projects_check.py`
