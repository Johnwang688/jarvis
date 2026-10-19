# Decisions: chats that read and message each other (2026-10-19)

The owner's request and answers. **Where this file and the plan
(`2026-10-19-chat-to-chat-plan.md`) disagree, this file wins.** It widens the
session tools' rule ("session tools read, never switch the owner's live
session"). No tool switches the owner's live session, and reading is
widened as CC-1 says. Messaging is new, and CC-2 to CC-5 bound it.

## What the owner asked for

> I want him to be able to read and communicate with other chats in the HUD,
> including the Claude and codex chats. Claude chats should be able to read from
> other Claude, codex, and Jarvis chats as well. Same for codex.

## Answered by the owner (2026-10-19)

| # | Decision | Answer |
|---|---|---|
| CC-1 | Who reads | **Every HUD chat (Jarvis, Claude, Codex) can read every other HUD chat**: Jarvis, Claude and Codex chats alike. |
| CC-2 | Who sends | **Every chat can send** to every other chat, Claude and Codex included. The owner chose this over read-only for Claude and Codex. |
| CC-3 | When a chat may send without asking | **Only during a turn the owner typed at the HUD.** A task, schedule, Discord message or sub-agent cannot send, so text the chat read (a web page, an email, a file) cannot start work in another chat on its own. |
| CC-4 | The reply | **Asynchronous.** The sender sends and moves on. When the target's turn finishes, its response is streamed back to the sender. The sender waits only when it is blocked on that answer (an explicit wait). |
| CC-5 | A target that is mid-turn | **Queued.** The message starts its turn once the current turn ends. It never interrupts. |
| CC-6 | (plan O-1) Spoken or dictated HUD turns | **They can send too.** CC-3 reads "a turn the owner started at the HUD, typed or spoken". The plan's `typed` slot key (P-5) is dropped: the desk check is enough. |
| CC-7 | (plan O-3) Tools in a relayed or reply turn | **Every tool is kept.** No `RELAY_REFUSED` list; P-11 is dropped. Such a turn has the tools a typed turn has, under the same gates and approvals. It still cannot send onward (no chains). |
| CC-8 | (plan O-5) SELF_PROTECTED | **Yes.** `jarvis/v2/chat_relay.py` and `jarvis/v2/tools/chats.py` join SELF_PROTECTED. |
| CC-9 | (plan O-11) Merge rule | **Merge on review.** Each C-PR merges once its lead review and full sweep pass. A daemon restart is still asked separately. |

## Lead's defaults (not asked; reversible, say if wrong)

- **No chains.** A turn started by another chat's message is not one the owner typed, so under CC-3 it cannot send onward. That rules out ping-pong loops and relays. It can still read, and it answers its sender through CC-4.
- **The receiving chat runs its tools under its own gates**, exactly as if the owner had typed. Approval cards still go only to the owner. A message from a chat never approves, denies or answers a card. That is the "no operation of one's own gate" rule.
- **Incoming text is untrusted data**, fenced, with its provenance shown: the sending chat's title, provider and that it came from a chat, not the owner. The receiving model is told it was not the owner's words.
- **The HUD shows every relayed message** in both chats: "from Jarvis · ‹chat title›" on the receiving side, and a "sent to ‹title›" line on the sending side. The owner can always see who started what.
- **Reads go through the same redaction as the HUD's display** (secrets display redaction, #205) and the credential-pattern scrub. A read is never a route to a credential the reader could not otherwise see.
- **Scope:** non-archived chats only, across projects. Archived and trashed chats are not readable or reachable.
- **A reply is delivered once.** If the sender is mid-turn, it lands at the sender's next step. Otherwise it starts a short turn in the sender, which reports it. That turn is not owner-typed, so it cannot send.
- **Bounds:** a queue limit per target chat, a size limit per message, and a per-turn send budget, with values set in the plan.
- **Plan defaults adopted by the lead (2026-10-19, reversible):**
  - O-2: a mid-turn reply keeps the sender's desk.
  - O-4: card provenance (C6) is added, and Always is kept.
  - O-6: archived v1 sessions are hidden in a follow-up.
  - O-7: `chat_summary` is deterministic.
  - O-8: Discord and read-aloud skip relayed and reply turns.
  - O-9: Stop withdraws queued sends.
  - O-10: the plan's numbers.
