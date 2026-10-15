"""SV-0 recorder: the installed Claude Code CLI's sub-agent stream, offline.

Not a test and never run by a suite. It regenerates the committed recordings in
`claude/*.jsonl` (docs/plans/2026-10-15-sv0-findings.md says how they were made).

Run it only inside a rootless network namespace with loopback alone (the
method of docs/plans/2026-10-13-browser-native-offline-cli.md), under a memory
cap, with nothing of the owner's inherited:

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \
      timeout 600 unshare -Urn <venv>/bin/python -I record_claude.py --out DIR

The model is a scripted local Anthropic Messages endpoint. Every reply is a
fixed fixture string chosen by a marker in the conversation (`SV0_PARENT:<s>`
for the orchestrator, `SV0_CHILD:<name>` in an Agent prompt for a sub-agent).
HOME, CLAUDE_CONFIG_DIR and XDG dirs are a fresh temporary directory, the key
is a dummy, and the process environment is constructed, never copied.

Each line of a recording is one observation, in order:
  {"seq", "ms", "src": "cli" | "hook" | "model" | "harness", "data"}
`cli` is the CLI's raw stdout JSON exactly as the SDK reads it (before
`parse_message`), so a replay can parse it with any SDK version.
"""
from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

CLI = pathlib.Path("/home/johnw/.local/bin/claude").resolve()
MODEL = "claude-sonnet-5-5"
MARK = re.compile(r"SV0_(PARENT|CHILD):([a-z_]+)")


class Log:
    """One scenario's ordered observations (shared by every thread)."""

    def __init__(self):
        self.rows: list[dict] = []
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.waiters: list[tuple] = []

    def add(self, src: str, data) -> dict:
        with self.lock:
            row = {"seq": len(self.rows), "ms": int((time.monotonic() - self.t0) * 1000),
                   "src": src, "data": data}
            self.rows.append(row)
            return row

    def find(self, pred):
        with self.lock:
            return next((r for r in self.rows if pred(r)), None)


LOG = Log()


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(part.get("text", ""))
            elif part.get("type") == "tool_result":
                out.append(text_of(part.get("content")))
        return "\n".join(out)
    return ""


# -- the scripted model ------------------------------------------------------

def agent_call(name: str, *, background=None, kind="general-purpose", desc=None):
    """`background=None` omits run_in_background: 2.1.295's default is background."""
    args = {"description": desc or f"SV0 {name} probe", "subagent_type": kind,
            "prompt": f"SV0_CHILD:{name} Do only the fixture step, then finish."}
    if background is not None:
        args["run_in_background"] = background
    return ("tool", "Agent", f"toolu_sv0_{name}", args)


def bash(cmd: str, ident: str):
    return ("tool", "Bash", ident, {"command": cmd, "description": "SV0 fixture step"})


def say(text: str):
    return ("text", text)


def parent_script(scenario: str, step: int, last_user: str):
    """The orchestrator's fixture replies, by scenario and step."""
    if "SV0_STEER" in last_user and "tool_result" not in last_user:
        return [say("SV0 parent: steer acknowledged.")], None
    if "SV0_FOLLOWUP" in last_user:
        return [say("SV0 parent: follow-up answered.")], None
    if step > 0:
        if scenario == "background_flood" and step >= 2:
            # A real model streams many deltas; 150 of them outrun the SDK's
            # 100-message buffer while nobody reads (the stall question).
            return [say("FLOOD:" + "x" * 150)], None
        return [say(f"SV0 parent: {scenario} step {step} done.")], None
    first = {
        "one_default": [agent_call("alpha")],
        "one_fg": [agent_call("alpha", background=False)],
        "parallel_fg": [say("Launching three."), agent_call("alpha", background=False),
                        agent_call("beta", background=False), agent_call("gamma", background=False)],
        "parallel_default": [agent_call("alpha"), agent_call("beta")],
        "background_unread": [agent_call("bgquiet")],
        "nested": [agent_call("outer", background=False)],
        "stop_fg": [agent_call("sleeper", background=False)],
        "stop_fg_parallel": [agent_call("sleeper", background=False), agent_call("napper", background=False)],
        "background_flood": [agent_call("quickflood"), agent_call("late")],
        "stop_bg": [agent_call("bgsleeper")],
        "interrupt_fg": [agent_call("sleeper", background=False)],
        "interrupt_bg": [agent_call("bgsleeper")],
        "steer_fg": [agent_call("napper", background=False)],
        "fail_fg": [agent_call("failer", background=False)],
        "explore_fg": [agent_call("scout", kind="Explore", background=False)],
    }[scenario]
    return first, None


def child_script(name: str, step: int, tools: list[str]):
    """A sub-agent's fixture replies. Returns (blocks, http_error)."""
    if name == "failer":
        return None, (400, {"type": "error", "error": {"type": "invalid_request_error",
                                                       "message": "SV0 synthetic child failure"}})
    if name == "late" and step == 1:
        return [bash("echo SV0-late-second", "toolu_sv0_late_bash2")], None
    if step > 0:
        return [say(f"SV0 child {name}: finished.")], None
    if name == "quickflood":
        return [bash("sleep 1", "toolu_sv0_quickflood_bash")], None
    if name == "late":
        return [bash("sleep 4", "toolu_sv0_late_bash")], None
    if name in ("alpha", "beta", "gamma", "scout"):
        return [bash(f"echo SV0-{name}", f"toolu_sv0_{name}_bash")], None
    if name in ("sleeper", "bgsleeper"):
        return [bash("sleep 25", f"toolu_sv0_{name}_bash")], None
    if name in ("bg", "bgquiet"):
        return [bash("sleep 3", f"toolu_sv0_{name}_bash")], None
    if name == "napper":
        return [bash("sleep 4", f"toolu_sv0_{name}_bash")], None
    if name == "outer":
        if "Agent" in tools:
            return [agent_call("inner", background=False)], None
        return [say("SV0 child outer: no Agent tool in my catalog.")], None
    if name == "inner":
        return [say("SV0 child inner: finished.")], None
    return [say(f"SV0 child {name}: unknown.")], None


def sse(blocks, msg_id: str, model: str) -> bytes:
    events = [("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1}}})]
    stop = "end_turn"
    for i, block in enumerate(blocks):
        deltas = None
        if block[0] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block[1]}
            if block[1].startswith("FLOOD:"):
                deltas = [{"type": "text_delta", "text": ch} for ch in block[1]]
        else:
            _, name, ident, args = block
            start = {"type": "tool_use", "id": ident, "name": name, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(args)}
            stop = "tool_use"
        events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        for d in deltas or [delta]:
            events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": d}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                  "usage": {"output_tokens": 10}}),
               ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {k}\ndata: {json.dumps(d)}\n\n" for k, d in events).encode()


def summarize(blocks):
    out = []
    for b in blocks or []:
        out.append({"text": b[1]} if b[0] == "text" else {"tool": b[1], "id": b[2], "input": b[3]})
    return out


class Model:
    def __init__(self):
        self.calls = 0
        self.catalog: dict = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                try:
                    outer.post(self)
                except Exception as exc:  # recorded, never raised into the server
                    LOG.add("model", {"fixture_error": type(exc).__name__, "detail": str(exc)[:200]})
                    try:
                        self.send_error(500, "fixture failure")
                    except OSError:
                        pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def post(self, h):
        path = h.path.split("?", 1)[0]
        n = int(h.headers.get("Content-Length", 0))
        body = json.loads(h.rfile.read(n)) if n else {}
        if path.endswith("/count_tokens"):
            raw = b'{"input_tokens":100}'
            h.send_response(200)
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)
            return
        if path != "/v1/messages":
            LOG.add("model", {"unexpected_path": path})
            h.send_error(404)
            return
        self.calls += 1
        msgs = body.get("messages", [])
        tools = [t.get("name") for t in body.get("tools", []) if isinstance(t, dict)]
        first = next((m for m in msgs if m.get("role") == "user"), {})
        mark = MARK.search(text_of(first.get("content")))
        last_user = msgs[-1] if msgs and msgs[-1].get("role") == "user" else {}
        last_text = text_of(last_user.get("content"))
        if isinstance(last_user.get("content"), list) and any(
                isinstance(p, dict) and p.get("type") == "tool_result" for p in last_user["content"]):
            last_text += " tool_result"
        step = sum(1 for m in msgs if m.get("role") == "assistant")
        results = []
        for m in msgs[-1:]:
            for p in m.get("content", []) if isinstance(m.get("content"), list) else []:
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    results.append({"tool_use_id": p.get("tool_use_id"), "is_error": p.get("is_error", False),
                                    "text": text_of(p.get("content"))[:600]})
        last_parts = []
        content = msgs[-1].get("content") if msgs else None
        for p in (content if isinstance(content, list) else [{"type": "text", "text": content or ""}]):
            if isinstance(p, dict):
                last_parts.append({"type": p.get("type"), "tool_use_id": p.get("tool_use_id"),
                                   "text": (p.get("text") if p.get("type") == "text" else text_of(p.get("content")))[:700]})
        steer_seen = "SV0_STEER" in json.dumps(msgs[-1:]) if msgs else False
        error = None
        if mark is None:
            role, name, blocks = "other", None, [say("ok")]
        elif mark.group(1) == "PARENT":
            role, name = "parent", mark.group(2)
            blocks, error = parent_script(name, step, last_text)
        else:
            role, name = "child", mark.group(2)
            blocks, error = child_script(name, step, tools)
        key = f"{role}:{name}"
        if key not in self.catalog and role != "other":
            self.catalog[key] = {"tools": tools, "agent_schema": next(
                (t.get("input_schema") for t in body.get("tools", []) if t.get("name") == "Agent"), None)}
        LOG.add("model", {"role": role, "agent": name, "step": step, "model": body.get("model"),
                          "stream": body.get("stream", False), "n_messages": len(msgs),
                          "tool_count": len(tools), "has_agent_tool": "Agent" in tools,
                          "last_results": results, "last_message": {"role": msgs[-1].get("role") if msgs else None,
                                                          "parts": last_parts} if role != "other" else None, "steer_in_last_message": steer_seen,
                          "reply": summarize(blocks) if blocks else None,
                          "http_error": error[0] if error else None})
        if error:
            raw = json.dumps(error[1]).encode()
            h.send_response(error[0])
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)
            return
        model = body.get("model", MODEL)
        if body.get("stream"):
            raw = sse(blocks, f"msg_sv0_{self.calls}", model)
            ctype = "text/event-stream"
        else:
            raw = json.dumps({"id": f"msg_sv0_{self.calls}", "type": "message", "role": "assistant",
                              "model": model, "content": [{"type": "text", "text": "ok"}],
                              "stop_reason": "end_turn", "stop_sequence": None,
                              "usage": {"input_tokens": 100, "output_tokens": 1}}).encode()
            ctype = "application/json"
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)


# -- the harness ---------------------------------------------------------------

async def wait_for(pred, timeout: float):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = LOG.find(pred)
        if row is not None:
            return row
        await asyncio.sleep(0.05)
    return None


def is_cli(subtype=None, type_=None, **match):
    def pred(r):
        if r["src"] != "cli":
            return False
        d = r["data"]
        if type_ and d.get("type") != type_:
            return False
        if subtype and d.get("subtype") != subtype:
            return False
        return all(d.get(k) == v for k, v in match.items())
    return pred


def model_req(role, agent, step=None):
    return lambda r: (r["src"] == "model" and r["data"].get("role") == role
                      and r["data"].get("agent") == agent
                      and (step is None or r["data"].get("step") == step))


async def run(scenario: str, work: pathlib.Path, unread_after_result: float = 0.0):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

    global LOG
    LOG = Log()
    stderr: list[str] = []

    def hook(event):
        async def record(data, tool_use_id, context):
            keep = {k: data.get(k) for k in ("hook_event_name", "tool_name", "agent_id", "agent_type",
                                               "stop_hook_active", "agent_transcript_path", "permission_mode")
                    if k in data}
            keep["tool_use_id"] = tool_use_id
            keep["input_keys"] = sorted(data)
            if event == "PreToolUse":
                keep["tool_input"] = data.get("tool_input")
            LOG.add("hook", keep)
            if event == "PreToolUse":
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                               "permissionDecisionReason": "SV0 fixture"}}
            return {}
        return record

    options = ClaudeAgentOptions(
        cli_path=str(CLI), cwd=str(work), model=MODEL, setting_sources=[],
        permission_mode="default", include_partial_messages=True, allowed_tools=[],
        hooks={e: [HookMatcher(matcher=None, hooks=[hook(e)])]
               for e in ("PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop")},
        extra_args={"replay-user-messages": None}, stderr=stderr.append,
    )
    client = ClaudeSDKClient(options)
    await client.connect()
    reading = asyncio.Event()
    reading.set()
    results = 0

    async def reader():
        nonlocal results
        async for data in client._query.receive_messages():
            LOG.add("cli", data)
            if data.get("type") == "result":
                results += 1
                if unread_after_result and results == 1:
                    reading.clear()
                    LOG.add("harness", {"action": "stop_reading", "seconds": unread_after_result})
                    await asyncio.sleep(unread_after_result)
                    LOG.add("harness", {"action": "resume_reading"})
                    reading.set()

    task = asyncio.create_task(reader())

    async def send(text):
        LOG.add("harness", {"action": "query", "text": text})
        await client.query(text)

    async def quiet(seconds: float, cap: float):
        """Wait until nothing new is observed for `seconds` (at most `cap`)."""
        end = time.monotonic() + cap
        last = len(LOG.rows)
        stamp = time.monotonic()
        while time.monotonic() < end:
            await asyncio.sleep(0.2)
            if len(LOG.rows) != last:
                last, stamp = len(LOG.rows), time.monotonic()
            elif time.monotonic() - stamp >= seconds:
                return True
        return False

    async def stop(tool_use_id: str, after_model: tuple):
        started = await wait_for(is_cli("task_started", "system", tool_use_id=tool_use_id), 20)
        if started is None:
            LOG.add("harness", {"action": "stop_task_skipped", "reason": "no task_started for spawn"})
            return
        await wait_for(model_req(*after_model), 20)
        await asyncio.sleep(1.0)  # let the child's Bash start
        tid = started["data"]["task_id"]
        LOG.add("harness", {"action": "stop_task", "task_id": tid})
        try:
            await client.stop_task(tid)
            LOG.add("harness", {"action": "stop_task_returned"})
        except Exception as exc:
            LOG.add("harness", {"action": "stop_task_raised", "error": type(exc).__name__, "detail": str(exc)[:300]})

    try:
        await send(f"SV0_PARENT:{scenario} Run the fixture scenario.")
        if scenario in ("stop_fg", "stop_fg_parallel"):
            await stop("toolu_sv0_sleeper", ("child", "sleeper", 0))
        elif scenario == "stop_bg":
            await stop("toolu_sv0_bgsleeper", ("child", "bgsleeper", 0))
        elif scenario in ("interrupt_fg", "interrupt_bg"):
            child = "sleeper" if scenario == "interrupt_fg" else "bgsleeper"
            await wait_for(model_req("child", child, 0), 20)
            await asyncio.sleep(1.0)
            LOG.add("harness", {"action": "interrupt"})
            await client.interrupt()
            LOG.add("harness", {"action": "interrupt_returned"})
            await wait_for(is_cli(type_="result"), 20)
            await quiet(2, 10)
            await send("SV0_FOLLOWUP after the interrupt.")
        elif scenario == "steer_fg":
            await wait_for(model_req("child", "napper", 0), 20)
            await asyncio.sleep(0.5)
            await send("SV0_STEER change of plan while the sub-agent runs.")
        await quiet(8, 60)
    finally:
        LOG.add("harness", {"action": "disconnect"})
        try:
            await asyncio.wait_for(client.disconnect(), 15)
        except Exception as exc:
            LOG.add("harness", {"action": "disconnect_raised", "error": type(exc).__name__})
        task.cancel()
    return LOG.rows, stderr


def scrub(value, temp: str):
    raw = json.dumps(value)
    raw = raw.replace(temp, "<TMP>").replace(str(pathlib.Path.home()), "<HOME>")
    raw = raw.replace("/home/johnw", "<HOME>")
    raw = raw.replace("johnw", "<user>")
    raw = re.sub(r'("installationId": ")[^"]*', r"\1<redacted>", raw)
    raw = re.sub(r"sv0-claude-[A-Za-z0-9_]+", "sv0-claude-TMP", raw)
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="*")
    opts = parser.parse_args()
    # Refuse to run with a route out: the endpoint must be the only model.
    subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True)
    links = json.loads(subprocess.check_output(["/usr/sbin/ip", "-j", "link", "show"]))
    if [x["ifname"] for x in links] != ["lo"] or subprocess.check_output(["/usr/sbin/ip", "route", "show"]).strip():
        raise SystemExit("refusing: not inside an isolated loopback-only network namespace")
    out = pathlib.Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    temp = tempfile.mkdtemp(prefix="sv0-claude-")
    try:
        model = Model()
        env = {"PATH": "/usr/bin:/bin", "HOME": temp, "XDG_CONFIG_HOME": temp + "/config",
               "XDG_DATA_HOME": temp + "/data", "XDG_CACHE_HOME": temp + "/cache",
               "CLAUDE_CONFIG_DIR": temp + "/claude", "ANTHROPIC_API_KEY": "dummy-sv0-offline",
               "ANTHROPIC_BASE_URL": model.base, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
               "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
               "PYTHONDONTWRITEBYTECODE": "1"}
        os.environ.clear()
        os.environ.update(env)
        for d in ("config", "data", "cache", "claude"):
            os.makedirs(os.path.join(temp, d), mode=0o700, exist_ok=True)
        scenarios = opts.only or ["one_default", "one_fg", "parallel_fg", "parallel_default", "nested",
                                  "explore_fg", "fail_fg", "stop_fg", "stop_fg_parallel", "stop_bg",
                                  "interrupt_fg", "interrupt_bg", "steer_fg", "background_unread",
                                  "background_flood"]
        version = subprocess.run([str(CLI), "--version"], env=env, capture_output=True, text=True).stdout.strip()
        summary = {"cli": version, "cli_path": str(CLI), "scenarios": {}}
        prior = out / "summary.json"
        if opts.only and prior.exists():
            summary = json.loads(prior.read_text())
            summary["scenarios"] = summary.get("scenarios", {})
        for scenario in scenarios:
            work = pathlib.Path(temp) / f"work-{scenario}"
            work.mkdir()
            begin = time.monotonic()
            try:
                unread = {"background_unread": 6.0, "background_flood": 12.0}.get(scenario, 0.0)
                rows, stderr = asyncio.run(run(scenario, work, unread))
                status = "ran"
            except Exception as exc:
                rows, stderr, status = LOG.rows, [], f"raised {type(exc).__name__}: {str(exc)[:300]}"
            with (out / f"{scenario}.jsonl").open("w") as f:
                for row in rows:
                    f.write(json.dumps(scrub(row, temp)) + "\n")
            summary["scenarios"][scenario] = {"status": status, "rows": len(rows),
                                              "seconds": round(time.monotonic() - begin, 1),
                                              "stderr_tail": scrub(stderr[-5:], temp)}
            print(json.dumps({scenario: summary["scenarios"][scenario]}), flush=True)
        summary["catalog"] = {**summary.get("catalog", {}), **scrub(model.catalog, temp)}
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        # The CLI keeps task output under /tmp/claude-<uid> (uid 0 inside the
        # namespace); remove only this run's own project folders.
        side = pathlib.Path("/tmp/claude-0")
        for d in side.glob("-tmp-sv0-claude-*") if side.is_dir() else []:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
