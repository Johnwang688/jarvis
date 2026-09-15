"""Spikes R1/R2 (design §15). Live; spends a little subscription quota.

R1 (answered 2026-09-15): the SDK runs on the CLI's subscription login with no API key.
R2: under permission_mode="auto", which of can_use_tool / a PreToolUse hook is consulted?
    Round 1 passed allowed_tools=[...], which auto-approves before the callback (the SDK
    warns about exactly this) — so this version passes no allowed_tools and installs both.
"""
import asyncio, os, tempfile
for k in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
    os.environ.pop(k, None)
from claude_agent_sdk import (query, ClaudeAgentOptions, ResultMessage, AssistantMessage,
                              PermissionResultAllow, HookMatcher)

permit_calls, hook_calls = [], []

async def permit(tool_name, input_data, context):
    permit_calls.append(tool_name)
    return PermissionResultAllow()

async def pre_tool(input_data, tool_use_id, context):
    hook_calls.append(input_data.get("tool_name"))
    return {}                      # observe only; a real gate would return a permissionDecision

async def main():
    tmp = tempfile.mkdtemp(prefix="r2-")
    opts = ClaudeAgentOptions(
        cwd=tmp, permission_mode="auto", max_turns=4,
        can_use_tool=permit,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool])]},
        system_prompt="Answer tersely.",
    )
    result = None
    async for m in query(prompt="Run `echo spike-ok` with Bash, then write the word done to a file named out.txt, then reply with the file's contents.", options=opts):
        if isinstance(m, ResultMessage):
            result = m
    print("R1:", "ok" if result and not result.is_error else f"FAILED {result and result.result}")
    print("R2 can_use_tool consulted for:", permit_calls or "NONE")
    print("R2 PreToolUse hook fired for:", hook_calls or "NONE")
    print("cost_usd (equivalent, not billed on subscription):", getattr(result, "total_cost_usd", None))
    print("out.txt exists:", os.path.exists(os.path.join(tmp, "out.txt")))

asyncio.run(main())
