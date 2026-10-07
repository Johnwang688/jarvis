# Owner decisions: Discord update poster and project channels

Recorded 2026-10-07 against
[`2026-10-07-discord-plan.md`](2026-10-07-discord-plan.md).

Where this file and a plan disagree, this file wins. The D-numbers follow the
plan's §7. Lines marked *Interpretation* are where the lead filled a gap. They
are the owner's to overrule.

Status: decisions D5 and D6 are **still open**. The plan is being revised to
match the decisions below. Two more plans were started for item 9 (slash
commands; thread-to-thread messaging).

---

**D1. A ping is a separate mention line.**
- A milestone that needs the owner is posted without a mention.
- Directly after it, Jarvis posts a separate one-line `@owner` message. The
  phone notification lands right where the update is.
- Nothing else ever pings.
- In DMs the extra line is skipped, because a DM already notifies
  (confirmed by the owner).

**D2. Channels can be created from Discord too, not only from the HUD.**
- Every channel links to a project whose local folder exists.
- From Discord, Jarvis can make a new project:
  1. It asks for the name and the folder path.
  2. If the folder does not exist, it asks whether to create it.
  3. It creates the folder only after the owner confirms.
- Linking an existing channel from Discord is allowed.
- The owner confirmed this flow.
- The folder creation is owner-only and typed-only. It refuses protected
  locations and never overwrites. The exact location policy is in the revised
  plan.
- B11 still stands: archiving and permanently deleting projects stay
  HUD-only.

**D3. Channels are never deleted.**
- Jarvis may *archive* a channel (move it to the Jarvis Archive category) when
  the server gets crowded, with the owner's permission.

**D4. Jarvis may rename, move or archive any linked channel, with the owner's
permission.**
- This applies whoever created the channel.
- An action the owner takes counts as that permission (confirmed by the owner). That
  covers renaming or archiving the project in the HUD, and a Discord command
  the owner typed.
- Anything Jarvis starts on its own goes through the approval gate as a yes/no
  with a code.

**D5. Bot permissions: OPEN.** The owner asked about Administrator.
- **Lead's recommendation: not Administrator.**
  - Administrator ignores every channel restriction and adds roles, bans,
    kicks, webhooks and server settings.
  - "Never delete a channel" would then rest on Jarvis's code alone.
  - A leaked bot token would mean a full server takeover.
- **Recommended set:**
  - Manage Channels server-wide, which D4 needs for channels outside the Jarvis
    categories;
  - View Channel, Send Messages, Send Messages in Threads, Create Public
    Threads, Embed Links, Attach Files, Read Message History;
  - the `applications.commands` scope, for slash commands.
- **Caveat:** Discord cannot grant "rename but not delete", so Manage Channels
  technically allows deleting. "Never delete" is enforced by the code and by a
  test that no DELETE is ever sent.

**D6. When a task's thread is created: OPEN** (being explained to the owner).
- The recommendation is to create it at `clarifying`, after the proposal's
  grace window.

**D7. Questions are answered by typed messages only.** Yes.

**D8. A quiet "Cancelled" post.** Yes.

**D11–D18** (bug fixes and accepted risks in the plan): accepted as
recommended.

**9. New, planned separately.**
- **Slash commands instead of keywords:**
  - `/status`, `/cancel`, `/yes` `/no` `/always`, and so on;
  - `/` for skills and commands too;
  - plain text then always means conversation.
- **Thread-to-thread messaging:**
  - Jarvis threads and channels can message each other for context and
    collaboration, like Claude Code sessions do.
  - A peer's message is never the owner's authority.
