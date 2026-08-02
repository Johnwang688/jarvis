---
description: Use when the owner says "/compact", "compact context", "shorten the context", or "summarize so we can continue"
---

1. Invoke `compact_context` immediately.
2. It uses the same context manager and summarizer as automatic compaction.
3. Preserve decisions, established facts, files or URLs touched, and outstanding work.
4. Do not summarize manually or ask for confirmation.
5. Report only: `Compacted; saved X tokens.`
6. Never compact a different conversation or expose the internal summary unless asked.
