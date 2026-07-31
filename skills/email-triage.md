---
description: Use when the owner asks about their email — "check my email", "anything important?", "any mail from X?"
---

1. `gmail_search` with `newer_than:2d` (or whatever window the owner asked
   for), `max_results` 15. Add `is:unread` only if they asked about unread.
2. Read (`gmail_read`) only what plausibly matters: security alerts, humans
   writing to the owner directly, anything with a deadline or money. Skip
   newsletters, receipts, and notifications unless asked.
3. Report in priority order: security first, then action-needed with their
   dates, then one line each for FYI. Name the senders. In voice mode keep
   it to a few sentences.
4. Never send, reply, archive, or delete on your own initiative. If a reply
   is warranted, propose the wording and wait — `gmail_send` will ask for
   approval, and the owner sees exactly what you drafted.
