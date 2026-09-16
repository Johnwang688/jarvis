---
name: wharton
description: Use when the owner mentions Wharton — the vault, its docs/briefs/index, IC meetings ("what did we say about X"), or preparing an analysis doc for the team
---

1. Everything goes through the `wharton` CLI (allowlisted for `run_command`;
   `wharton --help` lists commands). Start with `wharton status` to see the
   vault's state: index issues, doc statuses, git, meetings.
2. The vault (~/finance/Wharton) is index-first: `run_readonly` cat of
   `INDEX.md` answers "what do we have on X". Read `briefs/<slug>.md` next;
   open full docs/sources only when the brief isn't enough.
3. Past meetings: `wharton meet search "topic"` (flags: --speaker, --after,
   --meeting). Whole meeting: `wharton meet list`, then
   `wharton meet transcript <id>`.
4. New recording in meeting-rag/recordings/: `wharton meet ingest` (GPU,
   ~1-3 min per 10 min of audio), then `wharton meet speakers` — ask the owner
   who each Unknown-N is and `wharton meet label "Unknown-1" "Name"`.
5. New vault files: `wharton new doc|brief|note <slug>` — never create them by
   hand (it scaffolds AND indexes). After writing content, replace the TODO:
   `wharton index set <path> --desc "..."`. Finish with `wharton check`; it
   must exit clean.
6. Publishing: commit the doc with git first, then `wharton render
   docs/<slug>.md`. That is where you stop — you cannot create Google Docs
   (your drive_create_text is text/plain only). Tell the owner the render is
   in .render-cache/ ready for Claude Code to upload.
7. Boundaries: notes/ is private — never render or upload it; everything in
   the shared Drive folder is visible to the team. Never edit meeting-rag/
   code or its DB directly — the CLI is the interface.
