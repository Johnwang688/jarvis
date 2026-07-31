---
description: Use when the owner wants to create, improve, or rework a skill — "make a skill for X", "help me build a skill", "that should be a skill"
---

1. Interview before writing. Ask short questions until you can state:
   - **Trigger** — what would the owner actually say? Collect 2-3 phrasings.
   - **Steps** — their way of doing it, concrete tool names per step
     (`gmail_search`, `run_readonly`, …), in order.
   - **Boundaries** — what the skill must never do without asking (sending,
     deleting, spending money).
2. Draft the `description` as a trigger: **"Use when the owner …"** with the
   real phrases. The description lives in your always-visible skills index —
   it is how future-you decides to load the skill, so vague descriptions
   make dead skills.
3. Keep instructions numbered and short (aim under 12 lines). Name exact
   tools. Put the boundaries in as their own step, not a footnote.
4. Show the owner the draft (name, description, instructions) and get a yes
   before calling skill_write. Same name overwrites, so iterating is cheap.
5. After saving, confirm it landed with skill_list and remind the owner they
   can edit the file directly in skills/<name>.md.
