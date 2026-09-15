"""Spike R1 (design §15): does the Agent SDK run on the CLI's subscription login with no API key?
Also R2: is can_use_tool consulted under permission_mode="auto" for a tool we mark always-ask?
Live; spends a little subscription quota. Prints findings, exits 0 either way.
"""
import asyncio, os, sys, tempfile
for k in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
    os.environ.pop(k, None)
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, SystemMessage, PermissionResultAllow, PermissionResultDeny

calls = []
async def permit(tool_name, input_data, context):
    calls.append((tool_name, dict(input_data)))
    return PermissionResultAllow()

async def main():
    tmp = tempfile.mkdtemp(prefix="r1-")
    opts = ClaudeAgentOptions(
        cwd=tmp, permission_mode="auto", max_turns=4,
        allowed_tools=["Bash", "Read", "Write"], can_use_tool=permit,
        system_prompt="Answer tersely.",
    )
    text = ""; result = None
    async for m in query(prompt="Run `echo spike-ok` with Bash, then write the word done to a file named out.txt, then reply with the file's contents.", options=opts):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if hasattr(b, "text"): text += b.text
        elif isinstance(m, ResultMessage):
            result = m
    print("R1 auth: subscription login worked" if result and not result.is_error else f"R1 FAILED: {result and result.result}")
    print("R1 result:", (result.result or "")[:200] if result else None)
    print("R1 cost_usd:", getattr(result, "total_cost_usd", None), "usage:", getattr(result, "usage", None))
    print("R2 can_use_tool calls under auto mode:", [c[0] for c in calls] or "NONE (auto mode settled everything itself)")
    print("out.txt exists:", os.path.exists(os.path.join(tmp, "out.txt")))

asyncio.run(main())
