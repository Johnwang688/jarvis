---
description: Use when the owner asks to add, remove, or inspect a command in Jarvis’s persistent permissions allowlist.
---

1. Read `jarvis/config.py` to locate `ALLOWLIST_PATH`.
2. Read the existing allowlist JSON before changing it.
3. Read `jarvis/permissions.py` to confirm prefix semantics.
4. For a command request like `python3 *`, add `{"tool":"run_command","prefix":"python3"}`.
5. Preserve all existing entries and avoid duplicates.
6. Write the JSON and reread it to verify the change.
7. Explain that prefixes match the command’s first token.
8. Never change permission-gating code or enable process-wide bypass mode through the allowlist.
