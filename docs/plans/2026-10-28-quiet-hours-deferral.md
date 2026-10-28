# Plan: quiet-hours deferral (QH)

**Status (2026-10-28): owner-approved direction, plan by the lead.** Nothing
is built. It builds on phone P-6's `jarvis/v2/quiet.py` (the quiet-hours
setting) and on the owner inbox (A3's missed records, B1's delivery).

## What the owner asked (2026-10-28)

> "Can we have 2300 to 0700 be quiet hours so approvals and questions wait
> until I wake up? If the agent runs into something blocking during that time
> it should try and do other things while waiting to be unblocked."

Then, choosing between "hold until morning" and "defer and keep working":
**defer and keep working.** A held approval blocks its chat's whole turn, so
an agent cannot do other work while it waits; a deferred one can.

Already decided with P-6: no push during quiet hours (rank 1 included), and
one coalesced push at 07:00 if anything at ranks 1 to 4 is still waiting.

## Decisions (lead's defaults, each reversible)

- **QH-1. Quiet is the P-6 setting.** `quiet.is_quiet()` at the moment of
  the ask decides. Default 23:00 to 07:00 local, on.
- **QH-2. An ask during quiet hours is deferred, not held.** The broker
  settles it at once as DENY with resolution `deferred`. The agent's denial
  text says, in code-built words: the owner is asleep until <time>; this was
  not approved; do not retry it tonight; carry on with work that does not
  need it; the owner will see it in the morning. Every surface's card
  clears, and nothing is posted to Discord.
- **QH-3. The owner at the desk is not asleep.** An ask is not deferred while
  the owner is demonstrably present: owner input in the HUD (or a verified
  phone answer) within the last 15 minutes. Then it asks normally, with the
  normal timeout. The builder picks the presence signal the desk already
  keeps and documents it.
- **QH-4. Questions wait as they do today.** Task questions and B1 decisions
  are already durable. A chat question (a provider `QUESTION` event) keeps
  its current behaviour; the builder checks whether it has a timeout and
  documents what happens overnight. No push wakes the owner for either.
- **QH-5. Deferred asks become inbox rows at rank 4,** kind `deferred`: "<tool>
  is waiting for your morning approval", linked to its chat or task. Rank 4
  means they count, are not markable, and trigger P-6's 07:00 push. They
  expire from the waiting list after 24 h (then they show as rank 8
  information, like A3's missed rows).
- **QH-6. "Approve and retry" is one exact, single-use grant.** From the
  deferred row (HUD, or the phone with a fresh passkey assertion, as Allow):
  1. A grant is recorded for **exactly** that call: same thread or task, same
     tool, same canonical arguments (compared by a hash of the full,
     unredacted request the broker saw; the arguments themselves stay in a
     protected store, never in a row, log or push).
  2. The asking chat or task is told, through B1's delivery path
     (`via="inbox"`, no desk authority, queued behind a running turn, never
     steering): "The owner approved <tool> that was deferred overnight. Retry
     it now if it is still needed." A task that ended `blocked` on it is
     resumed through `TaskControl`, as the HUD's resume does.
  3. The next ask that matches the grant exactly, within 30 minutes, is
     allowed once without a card, recorded as the owner's ("granted from the
     inbox"). Anything that differs asks normally. The grant is never
     "Always", never widens to similar calls, and is used up on first match.
  4. Excluded: HUD-only Browser cards (their decision is bound to live page
     state). Their deferred row offers "Open its chat" only.
- **QH-7. "Leave it"** clears the row. Nothing is sent.

## Safety

| Rule | How QH keeps it |
|---|---|
| Approvals are the owner's; deny cheap | deferral is a deny; the only allow is the owner's explicit Approve and retry |
| No operation of one's own gate | grants are written only by the owner-only inbox route (and the phone's verified answer route); the grant store is protected state; no tool, MCP or session path reaches it (`archive_check`) |
| Credentials never in rows, logs or push | rows use the card's public projection; the full request lives only in the protected grant store, scrubbed for display |
| Unknown or malformed fails closed | a malformed setting is "not quiet" (asks normally); a grant that cannot be read matches nothing |
| Speech never authorizes | no voice path creates a grant |

## Work package

| WP | Package | Acceptance |
|---|---|---|
| QH | Deferral in the broker, deferred rows, Approve and retry, docs | an ask in quiet hours returns DENY `deferred` at once with the code-built text, with no wait and no Discord post; owner present → normal ask; a deferred row at rank 4 with "Approve and retry" and "Leave it"; a grant matches only the identical call once within 30 min, never a different argument, thread or tool, never twice; Browser cards get no grant; the chat gets the B1-framed message, the blocked task resumes; phone Approve and retry needs a fresh assertion; agents cannot write grants (tool sweep plus shell probes at v2 layer 1 and v1) |

## QH as built (2026-10-28, PR #326)

Module `jarvis/v2/quiet_defer.py` (SELF_PROTECTED); the broker's hook in
`approvals.py` is one attribute (`quiet_desk`) and one call under its lock.
Details and the full rules: `docs/notes/control-plane.md`, "Quiet hours:
approvals deferred to the morning".

- **QH-1, QH-2.** Built as planned. The desk is attached by daemon2's
  `main()` only (`Daemon.attach_quiet_desk`), as the PR source and §4.1
  reads are, so no embedded or test daemon depends on the time of day.
  The agent's words come from `denial_text`, with the end of quiet hours
  as HH:MM in the machine's zone.
- **QH-3, the presence signal.** The desk's own presence
  (`desk.DeskState`) is per turn and keeps no time, so it is not used. A
  new monotonic stamp, `Daemon.owner_seen()`, is set by the owner's own
  input: a HUD message (`is_owner`, the test that grants the desk), the
  HUD's answer to an approval (`POST /approvals/<id>`, `POST
  /inbox/<id>/answer`), the phone's answer after a verified passkey
  assertion, and the owner's Approve and retry (or Leave it at the HUD).
  `owner_present()` is that stamp within 15 minutes. A Leave it from the
  phone (session only) is not presence. So an Approve and retry at night
  makes the next 15 minutes' asks ordinary cards.
- **A malformed setting.** `quiet.py` reads a link, a malformed or
  oversized `quiet.json` as its default (on), right for the push. The
  deferral probes the file itself (`setting_readable`) and treats such a
  file as "not quiet": asked normally. A missing file is the default.
  `quiet.py` is unchanged.
- **The words reach each model:** Claude (the hook's and `can_use_tool`'s
  denial reason), the fast path (`tools.Refusal`), the jarvis-mcp bridge
  (`_unasked_refusal`). **Codex cannot**: its approval reply is a bare
  "decline" (the 0.161 `CommandExecutionRequestApprovalResponse` has no
  reason field), so the chat's log gets a non-fatal line saying why and
  the model only sees a decline (since the review, Codex chats are told
  up front in their instructions).
- **A Discord command's own ask** (`discord_channel_id`, the owner's
  `/project new` a moment ago) is never deferred.
- **QH-4.** Unchanged, checked: a Codex chat question has no timeout (it
  waits until answered, stopped or the transport closes), so overnight it
  stays a rank-2 row; Claude emits no provider QUESTION.
- **QH-5.** As planned; the store keeps the card's public projection and
  the digest only. A card over 16 KB is kept cut and never granted.
- **QH-6.** As planned, with the work per id (`approve_and_retry`,
  `leave`: one result each, never raising), so the morning review can call
  them for a selected group under one confirm. A task that is not
  `blocked` gets no grant (Q2, below). The phone's assertion is bound to
  `defer:<id>`; `remote_answer.defer_binding` gives a set of ids one
  binding (`defers:` + SHA-256 of the sorted ids) for the morning review.
- **QH-7.** As planned.

## The #326 review's fixes (2026-10-28)

Lead's decisions on the open questions: **Q1** hide Approve and retry for
every Codex ask; **Q2** a task that is not blocked gets no grant ("it is
running; open the task"), its row stays; **Q3** Codex chats are told in
their instructions that a decline in quiet hours may be a deferral.

- **H1.** The digest now covers `cwd` (where the gate judged the call:
  Claude's shell folder, Codex's item cwd, else the brief's), the
  headline, the layer, the sub-agent, the relayed turn and the provider;
  and an owner-only ask (widening, merge gate or always-ask layer 2, a
  dangerous Jarvis tool, any headline; the permit passes `grantable=False`)
  is never grantable, and never taken by the desk whatever the index holds.
- **M1.** Codex asks: `grantable` false, "Open its chat" only.
- **M2.** One record per waiting (thread, digest), "asked N times"; at most
  20 waiting rows per thread a day, the rest counted in "and N more from
  this chat"; eviction per thread and by size; what is evicted, or cannot
  be written, is counted and still shows.
- **M3.** CLAUDE.md's pinned sentences restored; the Inbox line shortened.
- **L1** a card longer than the rows show is never grantable; **L2** no
  chat or task, no retry; the HUD's new VEX project is never deferred and
  stamps presence; **L3** the escape hatch passes the deferral's words;
  **L4** presence age is the larger of the wall and monotonic ages; **L5**
  the phone's retry runs §4.1 and the Allow's gate, and needs the monitor;
  **L6** live grants are an in-memory index taken under the broker's lock,
  the spend written after. NITs: sub-agent and relay in the digest; a
  grant also ends by the monotonic clock in the process that minted it.

Still open: a Codex canonical digest (Codex asks cannot be granted until
then).
