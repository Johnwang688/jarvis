"""Dump a saved session's call sequence — hand-run diagnosis helper.
Usage: .venv/bin/python tests/probe_session_dump.py <session-id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sid = sys.argv[1] if len(sys.argv) > 1 else None
base = Path.home() / ".local/share/jarvis/sessions"
if not sid:
    sid = sorted(p.name for p in base.iterdir() if p.is_dir())[-1]
messages = json.load(open(base / sid / "messages.json", encoding="utf-8"))
print(f"session {sid}: {len(messages)} messages\n")
for msg in messages:
    role = msg.get("role")
    content = msg.get("content")
    if role == "user" and isinstance(content, str):
        print(f"USER: {content[:300]}")
    elif role == "user":
        print("USER: [multimodal]")
    elif role == "assistant":
        for c in msg.get("tool_calls") or []:
            f = c.get("function", {})
            print(f"  CALL {f.get('name')} {str(f.get('arguments', ''))[:120]}")
        if content:
            print(f"  SAY: {str(content)[:200]}")
    elif role == "tool":
        text = str(content)
        flag = " <-- ERROR" if text.lower().startswith("error") else ""
        print(f"    -> {text[:150]}{flag}")
