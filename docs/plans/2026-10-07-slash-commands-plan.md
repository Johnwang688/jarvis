# Plan: Discord slash commands, `/skill`, and a shared command registry (Jarvis v2)

Status: **draft, awaiting owner decisions** (see the end). Written 2026-10-07
by a read-only planner subagent, in parallel with revision 2 of
[`2026-10-07-discord-plan.md`](2026-10-07-discord-plan.md).

**Lead's reconciliation notes:**
- **Folder rules.** For `/project new`, the folder rules are the Discord
  plan's stricter `folders.py` (§3.2 there), not the HUD picker's
  `GET /fs/dirs` scope quoted in §1 below. The owner's D2 sends folder creation
  through that one function.
- **Sequencing.** This plan wants the slash core (S1) **before** the poster
  (PR A). The Discord plan wants it **after** PR A and B1. The owner decides;
  see the open decisions in both files. Global registration (below) removes
  the Discord plan's reason for waiting on B1, because it needs no guild id.

## What you'll see (summary for the phone)

- **Type `/` in your Jarvis DM, a project channel or a task thread** and
  Discord shows a menu of Jarvis's commands, each with a one-line
  description. Tap one and Discord gives you labelled boxes to fill in:
  - `/task` takes a brief and a project. The project box suggests your
    projects as you type. If you leave the brief empty, a multi-line box opens
    for a long brief.
  - `/status`, `/cancel`, `/steer`, `/answer` and `/resume` act on the thread
    you're in. Elsewhere they suggest your open tasks.
  - `/yes`, `/no` and `/always` approve or deny. The code box only suggests
    codes that were asked in *this* chat. If only one is open here, you can
    skip the code.
  - `/skill` suggests your skills (morning-briefing, email-triage,
    schoolwork, …). You pick one and add a request, for example
    `/skill name:morning-briefing`.
  - `/project` lists projects, makes a new one (with a "create this folder?"
    confirm), or links the channel you're in to a project.
- **Approval posts also get two buttons, Approve and Deny.** Once the approval
  is answered from anywhere, the buttons disappear. **Always** is only ever
  the typed `/always`.
- **Anything you type without a `/` is just a message.**
  - In a DM it's chat.
  - In a task thread it answers the open question, or otherwise steers the
    task.
  - A plain "yes" will never approve anything again. A voice note can never
    trigger a command, because Discord doesn't let voice notes send slash
    commands.
- **Nobody else sees or can use the commands.** Discord tells Jarvis who
  pressed what, and anyone who isn't you gets a private "only the owner can
  use Jarvis" reply.
- **The HUD gets the same `/` menu later** (for skills and task commands), but
  never `/yes`. HUD approvals stay on the card.

---

## 0. What was verified in the code

- **Keyword parsing lives in two places today.**
  - `router.classify` (`jarvis/v2/router.py:75`) matches these with one regex:
    `yes|no|always <code>`, `status|projects|tasks`, `cancel [id]`,
    `steer:|redirect:`, `resume <id> [on <p>]`, plus a `task:` prefix.
  - `DiscordRouter._approval_answer` (`gateway.py:257`) runs **before**
    `classify` on every typed message. It reuses v1's
    `discord_approvals._parse`, accepting `yes`, `ok`, `go` and so on, with an
    optional code.
  - `classify` is only called from Discord. The HUD never calls it; it uses
    buttons and routes.
- **Gateway.** `_V2Listener._session` (`gateway.py:88`) only dispatches
  `READY` and `MESSAGE_CREATE`. `READY` stores `bot_id` but not
  `application.id`. `INTENTS` = 9|15|12. `INTERACTION_CREATE` needs no intent.
- **Token bundle.** `~/.config/jarvis/discord_token.json` has the keys
  `bot_token` and `owner_id` only. There is no application id and no guild id.
  The application id can be read from `READY` (`d.application.id`).
- **Invite URL.** `jarvis/tools/discord.py:connect()` prints
  `scope=bot&permissions=68608`, with no `applications.commands` scope.
- **REST.** `DiscordRest` (`rest.py`) has `post`, `edit`, `create_thread`,
  `create_channel` and `unarchive`. It has no components, no interaction
  callbacks and no webhook endpoints. Its `_api` redacts only the bot token.
- **Verbs post directly.** Every verb method in `gateway.py` (`_verb`,
  `_steer`, `_cancel`, `_resume`, `_status`, `_intake`) posts via
  `self._post(channel_id, …)`. To host interactions they need a reply sink
  instead of a channel id.
- **Bug 2 from the Discord plan is confirmed.** `TaskControl.answer_question`
  (`runner.py:224`) exists, but Discord never calls it. `runner.py:486`
  publishes `task_question` with each question's `options`.
- **Skills.**
  - There are 11 under `skills/`. Four are `jarvis-only`: manual-compaction,
    permission-allowlist, self-improve and whiteboard.
  - All four need tools the v2 fast path forbids (`compact_context`,
    `run_command`) or a protected file (`allowlist.json`), so none can run
    from a v2 chat thread.
  - `~/.claude/skills` also holds three **non-repo** symlinks
    (investment-research, valuation-coach, valuation-example) that only Claude
    sees.
  - The fast path has `skill_list` and `skill_read`. `UserMessage`
    (`provider.py:95`) has no skill field.
- **Discord chat always opens a fast-path thread** (`_chat_thread`:
  `ProviderName.FAST`). PR #3 gives HUD chat threads Claude, Codex or
  OpenRouter, but nothing on Discord yet. The Discord plan defers "choosing a
  model from Discord".
- **PR #4** (`feat/projects-archive`):
  - `gateway.py` changes three small places (archived filters).
  - `router.place` matches names case-insensitively and skips archived
    projects.
  - `projects.owner_only` gates archive, restore and delete to the HUD only
    (B11).
- **PR #3** (`feat/thread-model`) touches `InputBar.tsx` (`modelChip`,
  `imageNote`), `hud_api.py` (`PATCH /threads/{id}` with model and effort,
  `GET /thread-models`) and `daemon.set_thread_model`.

---

## 1. The command set

Eleven commands in the first build, plus three after PR #3. Option names are
short because they're typed on a phone.

| Command | Options | DM | Project channel | Task thread | Replaces |
|---|---|---|---|---|---|
| `/task` | `brief` (string, optional: if empty, a multi-line box opens), `project` (autocomplete; defaults to this channel's project, or the Inbox in a DM), `skill` (autocomplete, optional), `provider` (choice: claude/codex, optional) | yes | yes | yes (new task, same project) | `task: …`, `in <project>: …`, "use codex" in a brief |
| `/status` | `task` (autocomplete, optional) | lists active tasks | this project's tasks | this task's card | `status`, `tasks` |
| `/cancel` | `task` (autocomplete; defaults to this thread's task) | needs `task` | needs `task` | default | `cancel [id]`, including the 60 s proposal withdrawal |
| `/steer` | `text` (required), `task` (default: this thread) | with `task` | with `task` | yes | `steer:` / `redirect:` |
| `/answer` | `text` (required; autocomplete offers the question's choices), `question` (autocomplete; defaults to the first blocking one), `task` (default: this thread) | with `task` (for DM-delivered questions) | with `task` | yes | new; fixes bug 2 |
| `/yes` `/no` `/always` | `code` (autocomplete: only codes open **in this chat**; optional if exactly one is open here) | yes | yes | yes | `yes/no/always CODE` |
| `/resume` | `task` (autocomplete: blocked tasks), `provider` (choice) | yes | yes | default | `resume X [on Y]` |
| `/skill` | `name` (autocomplete), `request` (string, optional) | chat turn | project chat turn | becomes a steer | new |
| `/project` | subcommands: `list`; `new name folder [channel:bool]`; `link project` (adopts the channel you're in); `unlink`; `channel project` (creates one) | list, new | link, unlink, channel | — | `projects`; the HUD's "paste channel id" |
| later: `/model`, `/effort`, `/new provider` | After PR #3: change the model or effort of the chat thread behind this place, or start a fresh one | yes | yes | — | — |

Notes:
- **Goal verbs:** none survive in v2. Goals were folded into tasks (design
  §13). `resume` is the only leftover.
- **Places are enforced on the server.** Discord cannot hide a command in one
  channel type, so `/steer` shows in a DM but replies "which task?" unless
  `task` is given. The registry declares `places` per command, and the handler
  checks it before anything else.
- **"Other" guild channels** (not a Jarvis place) accept only `/project link`.
  Everything else gets a private "This channel isn't a Jarvis place."
- **`/project`:**
  - `new` folder rule, confirm-then-mkdir: if the folder exists, the project
    is created. If not, the reply is "Create folder `~/projects/foo`?" with
    Confirm and Cancel buttons. The pending id is one-shot, expires in 10
    minutes and is owner-only. *(Lead: the folder rules are the Discord plan's
    `folders.py`; see the reconciliation note at the top.)*
  - `link`: the channel id comes from the interaction, which Discord
    authenticates. You never paste an id.
  - `channel` and `channel:true` need PR B's guild config. Before PR B they
    refuse with "run `jarvis auth discord-guild` first".
  - Archive, restore and delete stay **HUD-only** (PR #4 B11).
- **Jarvis-initiated channel renames, moves and archives** (the owner's
  revised decision) are broker approvals. They are answered with the same
  `/yes` or buttons, so they need no extra machinery.
- **Long briefs.** Discord's mobile option box is single-line. `/task` with no
  `brief` replies with a Modal (interaction response type 9) holding one
  paragraph field of up to 4000 characters. On submit, the modal's
  `custom_id` carries the other options, which are validated again on the
  server.

---

## 2. Skills via `/`

**Recommendation: one `/skill name:<autocomplete> request:<text>` command,
not one command per skill.**

- **Limits.** A per-skill set would eat into the 100-command cap. More
  importantly, it would make **registration depend on data an agent can
  write** (`skill_write`, the self-improve skill). An agent-authored skill name
  and description would become a registered Discord command. With `/skill`,
  the registered set is static code, and skills appear only as autocomplete
  suggestions at read time.
- **Autocomplete returns at most 25 choices**, filtered by what you've typed
  (case-insensitive prefix, then substring). With 7 invocable skills this is
  moot.
- **Description cap.** Choice names are capped at 100 characters, as
  `name — description` truncated.
- **On submit, the name is checked against the live catalogue.** Autocomplete
  is only a suggestion, so a hand-typed name is re-checked.

**The catalogue**: `commands.invocable_skills()` in the new
`jarvis/v2/commands.py`.
- It is the repo `skills/*/SKILL.md` (D8, one source), read through
  `jarvis.tools.skills._paths` and `metadata`.
- **It excludes `jarvis-only`.** All four need v1-loop tools or protected
  files the v2 fast path refuses. Typing one by hand gets: "`whiteboard` only
  runs in the v1 loop."
- The non-repo Claude-only skills are not listed (open decision 6).

**How an invocation reaches each provider.** Add one field:
`UserMessage.skill: str | None = None` (`jarvis/v2/provider.py`). It is
backward compatible. Each provider maps it onto its own mechanism, which keeps
§3's rule that only providers talk to models:

- **FastPath** (`providers/fastpath.py` `_run`): read the body with the same
  parser `skill_read` uses, cap it at 8k characters (refuse above that), and
  prepend it:
  `[The owner invoked the skill "<name>". Follow it.]\n<body>\n\n[Request]\n<text>`
  This is deterministic and saves a `skill_read` step. The trust model is
  unchanged: skill bodies are the owner's words.
- **Claude** (`providers/claude.py`): the skill is installed natively, so send
  `Use the "<name>" skill for this request.\n\n<text>`. Claude Code's Skill
  tool loads it.
  - Spike first: whether sending `/<name> <text>` as the SDK prompt invokes the
    skill directly. Use whichever the live check shows. The journal should
    show a `Skill` tool event.
- **Codex** (`providers/codex.py`): check whether 0.153.4's `turn/start` input
  accepts a skill input item (`{type:"skill", name, path}`). If yes, use it; if
  not, use the same directive text as Claude. One spike, written into the
  provider's notes.
- **Thread log:** record `skill` beside the user text, so transcripts show
  `/skill morning-briefing …`.

**Where `/skill` lands from Discord:**
- **DM or project channel:** that place's chat thread (`_chat_thread`), as a
  turn with `skill` set.
- **Task thread:** `control.steer(task, 'Use the "<name>" skill for:
  <request>')`. Orchestrators and implementers are Claude or Codex, which have
  the skill natively.
- **`/task skill:`** appends `Use the "<name>" skill.` to the brief.

---

## 3. Discord mechanics

### Registration

**Recommendation: global commands with `contexts: [0 GUILD, 1 BOT_DM]`,
`integration_types: [0 GUILD_INSTALL]` and `default_member_permissions:
"0"`.**

- **Global, because of DMs.** DMs are a primary surface (Inbox, DM-delivered
  approvals and questions), and **guild commands never appear in DMs**. Only
  global commands can.
- **Global, because the registry is static.** Skills are autocomplete, not
  commands, so the slower propagation of global updates almost never matters.
  Changes happen only when the code changes. If commands don't show,
  force-quit Discord on the phone.
- **Global, because it needs no guild id**, so S1 does not depend on PR B.
- **The alternative** is guild copies (instant updates) plus global copies
  with `contexts: [1]` only (DM only, so no duplicates in the guild). That
  doubles the payload for little gain. Use it only to iterate on the command
  set often.
- **`default_member_permissions: "0"`** hides the commands in the guild from
  everyone without Administrator. As server owner the owner still sees them.
  It does not apply in DMs, so the owner check (section 4) is the real gate.

**Who registers:**
- **The daemon syncs on start**, on a worker thread after the first `READY`
  (which supplies the application id). It runs `GET /applications/{app}/
  commands`, normalizes both sides (dropping `id`, `version`, `application_id`
  and defaults), and **only if they differ** sends one `PUT` (bulk
  overwrite). It never DELETEs commands one at a time. It logs `discord
  commands: up to date (11)` or `synced (11; 2 changed)`.
- **Human CLI:** `jarvis discord commands [--check | --sync]` in
  `jarvis/__main__.py`, for manual checks or a forced sync.
- **Why the daemon may sync:** the payload is a pure function of code in the
  repo. No runtime data (skills, projects, tasks) enters it, and the call
  needs the bot token, which no agent can read (use-but-never-see). A bad
  registry fails closed: commands vanish, approvals time out and deny, and the
  HUD still works. The diff keeps registration far below the 200 creates per
  day limit.

**Scope:** change `connect()`'s invite URL to `scope=bot+applications.commands`
(with PR B's permission integer once that lands). Re-inviting is harmless; ask
the owner to do it once (live step 2).

### Receiving and answering

- **Receiving.** `INTERACTION_CREATE` arrives over the existing gateway with no
  new intent. **This requires the Developer Portal's "Interactions Endpoint
  URL" to be empty**, otherwise Discord sends interactions over HTTP instead
  (live step 1). v1's `GatewayListener` ignores `INTERACTION_CREATE`, so a
  stray v1 listener can't double-handle commands. That is a bonus over the
  message path's risk 13.
- **The 3-second deadline.** `_V2Listener` hands each interaction to a worker
  thread immediately. The handler then:
  1. Synchronously runs the fast checks (application id, owner, context,
     place, command allowed here). A refusal goes out as a direct type 4
     response with flag 64 (EPHEMERAL).
  2. For an accepted command, sends type 5 (DEFERRED, public or ephemeral per
     the list below) **before** touching any store write or control call, then
     runs the verb.
  3. **Delivers the result** by editing `@original`, with followups for more
     chunks or attachments.
- **Autocomplete** (interaction type 4) is answered directly with type 8 and
  at most 25 choices. Store reads only, no deferral.
- **Buttons and modals** use type 7 (UPDATE_MESSAGE, which strips the buttons)
  and type 9 (open a modal).
- **The 15-minute token.** `InteractionReply` tracks `deadline = received + 14
  min`. A fast-path turn can run 900 s (`TURN_TIMEOUT_S`). After the deadline,
  or on a 401 or 404 from the webhook, it falls back to a normal bot post in
  `channel_id`, as today.
- **Ephemeral or public:**
  - **Public** (the chat keeps a record of what you did): `/task`, `/cancel`,
    `/steer`, `/answer`, `/yes`, `/no`, `/always`, `/resume`, `/skill`
    replies, `/project new|link|unlink|channel`.
  - **Ephemeral** (lookups and every refusal): `/status`, `/project list`, and
    all refusals ("not here", "not the owner", "unknown code", "asked
    elsewhere").
- **Autocomplete sources** (shared completers in `jarvis/v2/commands.py`):
  - projects: not archived, value = project id;
  - tasks: active, or blocked for `/resume`, shown as
    `` `id` state — brief… ``;
  - codes: open requests whose recorded channel equals this interaction's
    `channel_id`, shown as `ab12 · Bash: pnpm run deploy…`; `/always` lists
    only `allowlistable` ones;
  - skills;
  - `/answer` questions and their `options`;
  - after PR #3, models and efforts from `thread_model.describe()`.
- **Where the code goes:**
  - `rest.py` `DiscordRest` gains:
    - `callback(interaction_id, token, kind, data=None)` (POST
      `/interactions/{id}/{token}/callback`);
    - `edit_original(app_id, token, *, content, embed, components)`;
    - `followup(app_id, token, *, content, embed, files, ephemeral)`;
    - `get_commands(app_id)` and `put_commands(app_id, payload)`;
    - a `components=` argument on `post` and `edit`;
    - `_api(..., secrets=())`, which redacts every given secret (bot token
      **and** interaction token) from error strings.
    - **No path is ever logged**, because webhook paths contain the
      interaction token. The Discord plan's "log status and code" must log the
      operation name, never the URL.
  - `gateway.py` `_V2Listener._session`: store `self.application_id` from
    `READY`; dispatch `INTERACTION_CREATE` to `self._interaction_handler`.
  - New `jarvis/v2/discord/interactions.py`: `InteractionRouter(surface)` with
    `handle(payload)`, `_gate`, `_autocomplete`, `_command`, `_component`,
    `_modal`, and `InteractionReply`.
  - **Refactor in `gateway.py`:** verbs take a `reply` sink instead of
    `channel_id`. The existing message path uses `ChannelReply(rest,
    channel_id)`, and the interaction path uses `InteractionReply`. There stays
    one implementation of each verb.
    `class Reply(Protocol): def send(self, content=None, *, embed=None,
    files=(), components=None, ephemeral=False) -> None`
  - New `jarvis/v2/discord/commands.py`: `to_discord(registry) -> list[dict]`,
    `normalize(remote)`, `validate(payload)` (name regex, description ≤100
    characters, ≤25 options, ≤25 choices, ≤4000 characters per command, ≤100
    commands) and `sync(rest, app_id) -> SyncResult`.
  - New `jarvis/v2/commands.py` (surface-neutral, also served to the HUD):
    `Opt(name, kind, description, required=False, complete=None, choices=())`,
    `Cmd(name, description, options, places, surfaces, subcommands=())`,
    `REGISTRY`, `completions(kind, prefix, ctx) -> list[tuple[str, str]]` and
    `invocable_skills()`.
  - `daemon.start_discord`: unchanged in signature. The sync is triggered from
    the listener's first `READY`. Sync state (`ok | failed: <reason> |
    pending`, count, time) goes to the Discord plan's `GET /discord` and its
    HUD light.

---

## 4. Security

1. **Owner only, checked on every interaction**, including autocomplete.
   Autocomplete would otherwise leak project names, task briefs and approval
   commands to anyone who shares a server with the bot and opens a DM.
   - The user is `member.user.id` in a guild and `user.id` in a DM. It must
     equal the bundle's `owner_id`.
   - Also required: `application_id` equals ours; `context` is in {0, 1}
     (refuse 2, private channels); and once PR B lands, `guild_id` equals the
     configured guild.
   - Non-owner commands get an ephemeral "Only the owner can use Jarvis."
     Non-owner autocomplete gets an empty list. Non-owner buttons get an
     ephemeral refusal.
   - To be fair about what improves: a message's author id is also
     Discord-authenticated. The real gains are **structure** (no parsing, no
     `yes` collisions), **no guessing** (an explicit code or a single open
     request in this chat), and **commands cannot come from a voice note at
     all**.
2. **Approvals stay valid only where they were asked.**
   - `/yes` uses the interaction's `channel_id` against the same
     `_approval_channels` map (a thread id is its own channel). It keeps the
     same "asked elsewhere — nothing ran" reply.
   - Buttons are checked more strictly. `_post_approval` records the posted
     **message id** with the request id. A button press must match all three:
     owner; `message.id` equal to the recorded id; `channel_id` equal to the
     recorded channel. The `custom_id` (`jv:a:<req_id>` / `jv:d:<req_id>`) is
     treated as untrusted and must agree with the message-id lookup.
   - When an approval resolves anywhere (HUD, timeout, slash),
     `_post_resolution` edits the post to remove its buttons, so a stale tap
     can't happen. A tap that races resolution gets "already answered".
3. **Typed-only holds by construction.** Voice notes arrive as
   `MESSAGE_CREATE`, which never reaches the command handlers. The plain-text
   path keeps `spoken`: a spoken message in a task thread with an open question
   is politely refused as an answer (the owner's D7).
4. **Plain-text keywords. Recommendation: keep them for one PR, then drop them,
   and keep a tripwire for good.**
   - **S1 (transition):** keywords still work, including `yes CODE` at today's
     security level, but every keyword reply ends with "(next time:
     `/cancel`)". This guards against a failed registration (wrong scope,
     endpoint URL set) locking the owner out of phone approvals.
   - **S2 (once the owner's live check passes):**
     - Delete the verb regex and the `task:` prefix from `classify`, so it
       returns only `NewTask` (`intake=True` from the HUD), `Steer` or
       `FastPath`.
     - Remove `_approval_answer` from the plain-text path.
     - **Tripwire, kept for good:** a typed message that is exactly a former
       keyword form (`status`, `tasks`, `projects`, `cancel [id]`,
       `resume …`, `steer: …`, `yes|no|always <4-char code>`) is **held, not
       forwarded**, with a reply pointing to the slash command. A bare
       `yes`/`no`/`always` while an approval is open **in this chat** is also
       held ("Approvals are `/yes` now — open here: `ab12`"). Otherwise a bare
       yes or no is ordinary text, so it can answer a question.
     - **The property after S2: plain text is always conversation (DM or
       project channel) or an answer or steer (task thread). It never
       authorizes, opens a task by prefix, or cancels.** The fast path's
       `task_propose` is unchanged and still has its 60 s withdrawal grace.
5. **Buttons. Recommendation: add Approve and Deny buttons to approval posts in
   S1. Always stays `/always` only**, because it mints a persistent allowlist
   entry and should be deliberate. A tap is authenticated and cannot come from
   speech, so the planner reads "typed-only answers" as "never spoken". If the
   owner meant literally typed, skip the buttons (open decision 3). The buttons
   give deny-cheap, authorize-deliberate, with no default, as on the HUD card.
6. **No agent can register or alter commands.**
   - The registry is static code. No runtime data (skills, projects) enters
     the registered payload.
   - The only callers of `/applications/` are `discord/commands.sync`, from the
     daemon's `READY` and the human CLI. A test greps the tree to prove it.
   - No tool or MCP tool exposes it. Add `"discord_"` to
     `fastpath.FORBIDDEN_PREFIXES`, as the Discord plan also proposes.
   - The bot token is already a protected secret.
   - Interactions only come from real Discord users, filtered by the owner
     gate. Bot-authored messages are ignored, and after S2 text cannot
     authorize anyway.
7. **No token is ever logged.**
   - The interaction `token` is a 15-minute credential that can post as the
     app.
   - Interaction payloads are never logged; exceptions are logged by class
     only, as `handle` does today.
   - `DiscordRest` redacts both tokens.
   - Tests use synthetic tokens and `assertLogs` to check that neither appears
     in logs, exception strings or any request body.

---

## 5. The HUD

**Recommendation: yes, one shared registry, with a smaller HUD subset in its
own PR after S1 and after PR #3**, because PR #3 rewrites `InputBar.tsx`.

- **The HUD set:** `/skill`, `/task`, `/status`, `/cancel`, `/steer` (in task
  view), and `/model`, `/effort`, `/new` after PR #3.
- **Never `/yes`, `/no`, `/always`.** The approval card is the HUD's approval
  surface, and its rule is "nothing keyboard-defaulted". A typed `/yes` plus
  Enter breaks that rule. The registry marks those commands
  `surfaces={"discord"}`.
- **Backend:** in `jarvis/v2/hud_api.py`:
  - `GET /commands?surface=hud` returns the registry as JSON.
  - `GET /commands/complete?kind=&q=&thread=&task=` uses the same completers.
  - The thread send route accepts `skill`, validated against
    `invocable_skills()`, near `hud_api.py:305` where the `UserMessage` is
    built.
  - **Execution reuses the existing routes** (`createTask`, `cancelTask`,
    `steerTask`, `send`), all of which already call `TaskControl`. There is no
    generic "run command" endpoint for an agent to reach with curl.
- **Frontend:**
  - `hud/src/lib/commands.ts`: parse `/name args` and filter; vitest in
    `commands.test.ts`.
  - `hud/src/components/CommandMenu.tsx`: a popup above the textarea, opened
    when the text starts with `/` and the caret is in the first token. Arrow
    keys and Tab complete; Escape closes.
  - `InputBar.tsx` gains an `onCommand` prop; `App.tsx` maps commands to
    `api.*`.
  - **Only known names are commands.** `/mnt/c/...` or an unknown `/foo` is
    sent as text with a visible note. On a Claude thread, an unknown `/x` may
    be treated as Claude Code's own command; the note says so.
  - **Spoken text is never command-parsed.** AUTO-dictation sends bypass the
    parser. REVIEW transcripts land in the box, so they're typed by the time
    Enter is pressed.

---

## 6. Interaction with the Discord poster/channel plan

**Recommendation: build S1 (slash core) after PR #4 merges and before PR A.**
Otherwise approvals and answers get built twice. S1 touches the same
`gateway.py` regions as PR #4's archived filters and the Discord plan's
item 5.

**Order:** PR #4 → **S1** → PR A → PR B (adds the `/project
link|unlink|channel|new` channel half) → **S2** (drop keywords) → PR #3 at any
point → **S3** (`/model`, `/effort`, `/new` on Discord, plus the HUD popup).

**Exact changes to `docs/plans/2026-10-07-discord-plan.md`:**

- **"What you will see":** "`yes CODE` approves …", "`status` and `cancel` work
  as before" become `/yes` (or the buttons), `/status`, `/cancel`. "Plain text
  answers a blocking question, or steers" stays.
- **§1 table, `approval_requested` row:** no longer "Unchanged". The post gets
  Approve and Deny buttons, `_post_approval` records the message id, and the
  resolution edit strips the buttons. `render.approval_text`'s "Reply yes/no
  {code} in this thread." becomes "Answer here: tap a button, or `/yes {code}`
  · `/no {code}` · `/always {code}`." The same applies to
  `milestone("approval")`.
- **§1 "Approvals in the thread", DM fallback:** the logic is unchanged. DM
  answers work because commands are global with the `BOT_DM` context.
- **Bug 2 (answering questions):** moves into S1. Plain text in a clarifying
  thread calls `answer_question`, and so does `/answer`, through one
  `_answer(task, index, text, reply)`. The Discord plan's PR A shrinks to the
  approval DM fallback.
- **Intake replies:** produced by `/task`. `in <project>:` is retired in S2.
  The `router.orchestrator_unavailable` text becomes `/resume task:<id>
  provider:<p>`, and the `on_turn_finished` reply gains "(`/cancel` within
  60 s to withdraw)".
- **"Link existing: paste a channel id":** the HUD keeps it, and Discord gets
  `/project link` run inside the channel. Link validation is shared through
  one function.
- **"Create" plus the owner's revised decisions:**
  - project and channel creation from Discord is `/project new|channel`;
  - the confirm-then-mkdir flow is a button confirm (or a typed `/project new
    … create_folder:true` if buttons are declined);
  - Jarvis-initiated renames, moves and archives are broker approvals answered
    by `/yes` or buttons.
- **Guild check:** also applied to `interaction.guild_id`.
- **Security:** add the interaction-token redaction, owner-gated autocomplete,
  the `/applications/` single-caller test and the button message-id check.
- **`GET /discord`:** gains `commands: {state, count, synced_at, error}`, and
  the HUD light shows it.
- **Live steps for approvals and status:** rewritten as slash steps (see
  section 8 below).
- **Typed-only (D7):** now structural for commands. It still applies to
  plain-text answers.
- **v1 doubling (risk 13):** commands are immune, because v1 ignores
  interactions.

---

## 7. Tests (all free, nothing live)

**New `tests/v2/discord_commands_check.py`:**
- **Harness:**
  - `FakeListener.feed_interaction(payload)`.
  - Payload builders `slash(name, options, channel, guild, user)`,
    `autocomplete(...)`, `button(custom_id, message_id, ...)`,
    `modal_submit(...)`.
  - `FakeTransport` records `/interactions/{id}/{tok}/callback` and
    `/webhooks/{app}/{tok}[/messages/@original]` and returns ids.
  - `httpx.request` is patched to raise "live network", as
    `discord_routing_check.py` already does.
- **Registration:**
  - `to_discord(REGISTRY)` passes `validate` (name regex, description and
    option limits, ≤4000 characters, ≤100 commands, `contexts == [0, 1]`,
    `integration_types == [0]`, `default_member_permissions == "0"`).
  - The payload is identical across calls and is unchanged when a skill or
    project is added (the static-registry property).
  - Against a fake GET: equal means no PUT; any field drift means exactly one
    PUT with the full list.
  - A PUT failure yields `state=failed` and no retry loop.
  - A grep test shows `/applications/` appears only in `discord/commands.py`.
- **Every refusal path:**
  - a non-owner in the guild and in a DM (ephemeral, no control call, no store
    write);
  - a non-owner autocomplete (empty choices) and a non-owner button;
  - a wrong `application_id`; `context == 2`; a wrong guild once configured;
  - an unowned channel (only `/project link` is allowed);
  - a command outside its places (`/steer` in a DM without `task`, `/answer`
    with no question open);
  - `/yes` with an unknown code, a code asked elsewhere, an expired code, and
    two open with no code (asks which);
  - `/always` on a non-allowlistable request;
  - an autocomplete bypass with an unknown skill, a `jarvis-only` skill, an
    archived or unknown project, or an unknown task id;
  - a missing required option or a wrong type;
  - an unknown command name ("commands may be out of date");
  - a button whose `custom_id` request id doesn't match the message-id lookup,
    and a button after resolution;
  - a modal `custom_id` naming an archived project.
- **Timing:**
  - With a blocking `FakeControl`, the type 5 callback is recorded before the
    control call.
  - Refusals are type 4 with flag 64.
  - Autocomplete is type 8 with ≤25 choices.
  - After the 14-minute deadline (fake clock), or on a 404 from
    `edit_original`, the reply falls back to a channel post.
- **Scoping:** `/yes` autocomplete lists only codes recorded for this channel.
  A DM-fallback approval is refused from the thread.
- **Answers:** `/answer` and plain text in a clarifying thread call
  `answer_question(task, index, text)`. Plain text with no question steers.
  Spoken text never answers.
- **Secrets:** synthetic bot and interaction tokens never appear in
  `assertLogs` output, exception strings or request bodies. The bot token only
  appears in the `Authorization` header; interaction tokens only in URL paths.
- **S2:** keyword tripwire holds `cancel`, `status` and `yes ab12` without
  acting. A bare `yes` with an approval open here is held; a bare `yes` with a
  question open answers it. `classify` no longer returns `Verb`.

**Modified:**
- `discord_routing_check.py`: verbs via the `Reply` refactor; transition
  nudges in S1; approval posts carry `components`; resolution strips them.
- `router_check.py`: `classify` in S2.
- `fastpath_check.py`: `UserMessage(skill=…)` injects the body, the 8k cap
  refuses, and a jarvis-only skill is refused.
- `claude_provider_check.py` and `codex_provider_check.py`: the skill
  directive or input item.
- `hud_backend_check.py`: `/commands` excludes yes/no/always;
  `/commands/complete`; send with an invalid `skill` returns 400.
- `permissions_check.py`: `"discord_"` is in the forbidden prefixes.
- HUD: `lib/commands.test.ts` (parser, `/mnt/c` is not a command, unknown
  names pass through); `tests/face/hud_v2_check.py` (popup opens, Tab
  completes, Escape closes, Enter with the popup closed sends text, an
  AUTO-dictation send is never parsed).

---

## 8. Live verification (owner)

**At your desk**
1. Developer Portal → your app → General Information: **Interactions Endpoint
   URL must be empty.**
2. Open the invite URL with `scope=bot+applications.commands` (printed by
   `jarvis discord commands --check`) and re-authorize into your server. This
   is harmless if the bot is already in it.
3. Make sure only `jarvis daemon2` is running, then restart it. The log should
   say `discord commands: synced (11)`. A second restart should say
   `up to date (11)`, with no new PUT.

**On your phone**

4. In your Jarvis DM, type `/`. Jarvis's commands should be listed. If not,
   force-quit Discord and reopen it.
5. `/status`: an ephemeral list of active tasks.
6. `/task`, tap `project`: your projects should be suggested (not archived
   ones). Leave `brief` empty: a multi-line box should open. Submit a harmless
   brief.
7. `/skill`: 7 skills should be suggested, none of the 4 jarvis-only ones. Run
   `/skill name:morning-briefing` and check you get a briefing.
8. Trigger an approval in a task thread:
   - The post shows Approve and Deny.
   - `/yes` in your DM: the code is not suggested, and typing it gets "asked
     elsewhere".
   - In the thread, tap **Deny**: the buttons vanish and "Denied" appears.
   - Trigger another and use `/yes` with the suggested code.
9. In a clarifying thread, use `/answer` and pick a suggested choice. Check
   that the task proceeds.
10. Send a voice note "yes" while an approval is open: nothing should be
    authorized.
11. (After S2) Type plain `cancel` in a thread: you should get "That's
    `/cancel` now", and the task keeps running.
12. Ask someone else in the server, or an alt account: they should not see
    Jarvis commands in the server. If they DM the bot and use `/status`, they
    should get "Only the owner can use Jarvis".
13. Check the daemon log for the run: no token, no `/webhooks/` path.

---

## Open decisions (each with the planner's recommendation)

1. **Global versus guild registration.** Recommend **global** with `contexts
   [GUILD, BOT_DM]` and `default_member_permissions "0"`, because DMs need it
   and the set is static. Guild plus DM-only global is the fallback for
   instant iteration.
2. **Who registers.** Recommend **the daemon syncs on start (diff, then one
   bulk PUT), plus a human CLI for check and sync**, because the payload is
   static code and needs a token no agent can read. The stricter alternative
   is CLI-only.
3. **Buttons on approval posts.** Recommend **Approve and Deny buttons, with
   Always only as `/always`**, reading "typed-only" as "never spoken". If the
   owner meant literally typed, keep slash only.
4. **Keywords.** Recommend a **one-PR transition (they still work, with a
   nudge), then drop them in S2, with a permanent tripwire that holds old
   keyword forms without acting.**
5. **One `/skill name:` command versus one command per skill.** Recommend
   **`/skill name:<autocomplete>`**. It keeps the registry static and
   agent-proof, and has no 100-command ceiling.
6. **Which skills `/skill` offers.** Recommend **repo skills minus
   `jarvis-only`**. The Claude-only `~/.claude/skills` entries
   (investment-research, valuation-*) could later be offered only on Claude
   threads.
7. **How Claude and Codex receive a skill.** Recommend a **directive message by
   default, upgraded to `/name` (Claude) or a skill input item (Codex) if the
   S1 spikes show they work headless.** The fast path gets the body injected.
8. **Ephemeral versus public.** Recommend **actions public, lookups and
   refusals ephemeral.**
9. **Plain text in a task thread.** Recommend that it **answers the open
   blocking question if there is one, otherwise steers**, with `/answer` and
   `/steer` as explicit overrides.
10. **Sequencing.** Recommend **PR #4, then S1, then PR A, then PR B (+
    `/project` channel half), then S2, then S3/HUD**. S1 also fixes bug 2, so
    PR A loses its question-answering item.
11. **HUD scope.** Recommend **the same registry, a subset without `/yes`,
    `/no` and `/always`, after PR #3**, with execution through the existing
    routes rather than a generic command endpoint.
12. **Archive, restore and delete from Discord.** Recommend **no, keep them
    HUD-only** (PR #4 B11).
13. **Model, effort and new-thread commands on Discord.** Recommend **S3, after
    PR #3**. They change the chat thread behind the current place. The provider
    is fixed at open, so `/new provider:` starts a fresh thread.

### Critical files
- `jarvis/v2/discord/gateway.py`: listener dispatch, the `Reply` refactor,
  approval buttons and scoping, answers.
- `jarvis/v2/discord/rest.py`: interaction callback, webhook and command
  endpoints, components, token redaction.
- `jarvis/v2/router.py`: `classify` keyword removal (S2), reply texts.
- `jarvis/v2/provider.py` and `jarvis/v2/providers/fastpath.py` (plus
  `claude.py`, `codex.py`): `UserMessage.skill` and its per-provider mapping.
- New: `jarvis/v2/commands.py` (shared registry and completers),
  `jarvis/v2/discord/interactions.py`, `jarvis/v2/discord/commands.py`
  (payload, validate, sync).
- Also touched: `jarvis/v2/discord/render.py`, `jarvis/v2/daemon.py`
  (`start_discord`), `jarvis/tools/discord.py` (`connect` invite scope),
  `jarvis/__main__.py` (CLI), `jarvis/v2/hud_api.py`,
  `hud/src/components/InputBar.tsx`, `tests/v2/discord_routing_check.py`,
  `docs/plans/2026-10-07-discord-plan.md`, `docs/jarvis-v2-design.md` (§8.1
  rows 1–2, §9.1, §11.2–11.3).
