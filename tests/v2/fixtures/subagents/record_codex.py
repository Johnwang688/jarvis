"""SV-0 recorder: the installed Codex app-server's child-agent protocol, offline.

Not a test and never run by a suite. It regenerates the committed recordings in
`codex/*.jsonl` (docs/plans/2026-10-15-sv0-findings.md says how they were made).

Run it only inside a rootless network namespace with loopback alone (the
method of docs/plans/2026-10-13-browser-native-offline-cli.md), under a memory
cap, with nothing of the owner's inherited:

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \
      timeout 900 unshare -Urn <venv>/bin/python -I record_codex.py --out DIR

`codex app-server --stdio` runs under a fresh temporary CODEX_HOME whose
config.toml names a custom model provider at a scripted local OpenAI Responses
endpoint with a dummy key. The production CodexProvider is deliberately not
used: it requires `model_provider="openai"` with ChatGPT login (see the offline
CLI plan), so this exercises the native protocol only.

Each line of a recording is one observation, in order:
  {"seq", "ms", "src": "rpc_in" | "rpc_out" | "model" | "harness", "data"}
`rpc_in` is every JSON-RPC message the app-server wrote, verbatim; `rpc_out`
every message the harness wrote.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

CODEX = pathlib.Path("/home/johnw/.local/bin/codex").resolve()
MODEL = "gpt-6.1-sol"


class Log:
    def __init__(self):
        self.rows: list[dict] = []
        self.lock = threading.Lock()
        self.t0 = time.monotonic()

    def add(self, src, data):
        with self.lock:
            row = {"seq": len(self.rows), "ms": int((time.monotonic() - self.t0) * 1000), "src": src, "data": data}
            self.rows.append(row)
            return row

    def find(self, pred, start=0):
        with self.lock:
            return next((r for r in self.rows[start:] if pred(r)), None)


LOG = Log()


# -- the scripted model ------------------------------------------------------

def fn_call(name, args, call_id, namespace=None):
    item = {"id": "fc_" + call_id, "type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(args), "status": "completed"}
    if namespace:
        item["namespace"] = namespace
    return item


def exec_call(code, call_id):
    return {"id": "ct_" + call_id, "type": "custom_tool_call", "call_id": call_id, "name": "exec",
            "namespace": "functions", "input": code, "status": "completed"}


def message(text):
    return {"id": "msg_" + str(abs(hash(text)) % 10**8), "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def shell(cmd, call_id, escalate=False):
    """A shell step through code mode's exec, by whichever shell tool the catalog has."""
    args = {"cmd": cmd}
    if escalate:
        args.update({"sandbox_permissions": "require_escalated",
                     "justification": "SV0 fixture asks for approval"})
    code = ("const t = ALL_TOOLS.find(t => /(^|__)(exec_command|shell|shell_command)$/.test(t.name));"
            " if (!t) throw new Error('sv0_no_shell_tool');"
            f" text(JSON.stringify(await tools[t.name]({json.dumps(args)})));")
    return exec_call(code, call_id)


def patch_call(call_id):
    """A rename out of the workspace through code mode's freeform
    `apply_patch`: outside the writable roots, so Codex asks first."""
    patch = ("*** Begin Patch\n*** Update File: notes.txt\n*** Move to: /sv0-outside/notes.txt\n"
             "@@\n-old\n+new\n*** End Patch\n")
    code = f"text(JSON.stringify(await tools.apply_patch({json.dumps(patch)})));"
    return exec_call(code, call_id)


class Model:
    def __init__(self, scenario_ref):
        self.scenario_ref = scenario_ref
        self.calls = 0
        self.children: dict[str, str] = {}  # child thread id -> child name
        self.bodies: dict[str, dict] = {}
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                try:
                    outer.post(self)
                except Exception as exc:
                    LOG.add("model", {"fixture_error": type(exc).__name__, "detail": str(exc)[:300]})
                    try:
                        self.send_error(500, "fixture failure")
                    except OSError:
                        pass

            def do_GET(self):
                LOG.add("model", {"unexpected_get": self.path.split("?")[0]})
                self.send_error(404)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def post(self, h):
        path = h.path.split("?", 1)[0]
        n = int(h.headers.get("Content-Length", 0))
        body = json.loads(h.rfile.read(n)) if n else {}
        if path != "/v1/responses":
            LOG.add("model", {"unexpected_path": path})
            h.send_error(404)
            return
        meta = body.get("client_metadata") or {}
        items = body.get("input") or []
        thread = meta.get("thread_id")
        child = "x-openai-subagent" in meta
        with self.lock:
            self.calls += 1
            if child and thread not in self.children:
                self.children[thread] = f"child{len(self.children) + 1}"
            name = self.children.get(thread, "parent")
        # Step: how many model outputs this conversation already holds.
        step = sum(1 for i in items if isinstance(i, dict) and (
            i.get("type") in ("function_call", "custom_tool_call")
            or (i.get("type") == "message" and i.get("role") == "assistant")))
        outputs = [{"type": i.get("type"), "call_id": i.get("call_id"),
                    "output": str(i.get("output"))[:600]}
                   for i in items if isinstance(i, dict) and i.get("type") in ("function_call_output",
                                                                                "custom_tool_call_output")]
        tools = []

        def collect(v, ns=None):
            if isinstance(v, list):
                for x in v:
                    collect(x, ns)
            elif isinstance(v, dict):
                if v.get("type") == "namespace" and isinstance(v.get("name"), str):
                    ns = v["name"]
                if isinstance(v.get("name"), str) and (v.get("type") in ("function", "custom")
                                                       or "parameters" in v):
                    tools.append((ns + "." if ns else "") + v["name"])
                for k in ("tools", "functions", "function", "content"):
                    if isinstance(v.get(k), (list, dict)):
                        collect(v[k], ns)
        collect(body.get("tools", []))
        for i in items:
            if isinstance(i, dict) and i.get("type") == "additional_tools":
                collect(i)
        key = name if name == "parent" else "child"
        if key not in self.bodies:
            self.bodies[key] = body
        reply = self.script(name, step, items, outputs)
        LOG.add("model", {"role": "child" if child else "parent", "agent": name, "step": step,
                          "thread_id": thread, "turn_id": meta.get("turn_id"),
                          "parent_thread_id": meta.get("x-codex-parent-thread-id"),
                          "parent_turn_id": meta.get("parent_turn_id"),
                          "subagent_header": meta.get("x-openai-subagent"),
                          "turn_metadata": _safe_json(meta.get("x-codex-turn-metadata")),
                          "client_metadata_keys": sorted(meta), "tools": tools, "outputs": outputs,
                          "input_types": [i.get("type") for i in items if isinstance(i, dict)],
                          "reply": [_brief(x) for x in reply["items"]] if reply.get("items") else None,
                          "delay_s": reply.get("delay"), "http_error": reply.get("error")})
        if reply.get("delay"):
            time.sleep(reply["delay"])
        if reply.get("error"):
            raw = json.dumps({"error": {"message": "SV0 synthetic child failure", "type": "invalid_request_error",
                                        "code": "sv0"}}).encode()
            h.send_response(reply["error"])
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)
            return
        raw = _sse(reply["items"], self.calls)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def script(self, name, step, items, outputs):
        scenario = self.scenario_ref[0]
        steer = any("SV0_STEER" in json.dumps(i) for i in items[-3:] if isinstance(i, dict)
                    and i.get("type") == "message" and i.get("role") == "user")
        if name == "parent":
            if scenario == "patch_approval":
                if step == 0:
                    return {"items": [patch_call("call_parent_patch")]}
                return {"items": [message(f"SV0 parent: {scenario} done at step {step}.")]}
            if steer and scenario == "steer_parent" and step >= 1:
                return {"items": [message("SV0 parent: steer acknowledged.")]}
            if step == 0:
                spawns = {"one": ["alpha"], "parallel": ["alpha", "beta"], "approval": ["asker"],
                          "child_interrupt": ["sleeper"], "parent_interrupt": ["sleeper"],
                          "nested": ["outer"], "fail": ["failer"], "steer_parent": ["sleeper_short"],
                          "close_agent": ["sleeper"], "no_wait": ["slowpoke"],
                          "patch_approval_child": ["patcher"]}[scenario]
                return {"items": [fn_call("spawn_agent", {"task_name": f"sv0_{s}",
                                                          "message": f"SV0_CHILD:{s} do the fixture step."},
                                          f"call_spawn_{s}", "collaboration") for s in spawns]}
            if scenario == "no_wait" and step == 1:
                return {"items": [message("SV0 parent: finished without waiting.")]}
            if scenario == "close_agent" and step == 1:
                return {"items": [fn_call("interrupt_agent", {"target": "sv0_sleeper"}, "call_interrupt_agent",
                                          "collaboration")]}
            if step <= 2 and scenario not in ("no_wait",):
                n_waits = sum(1 for i in items if isinstance(i, dict) and i.get("name") == "wait_agent")
                if n_waits < 2 and step < 3:
                    return {"items": [fn_call("wait_agent", {"timeout_ms": 30000}, f"call_wait_{step}",
                                              "collaboration")]}
            return {"items": [message(f"SV0 parent: {scenario} done at step {step}.")]}
        # Children are numbered by first request; scenario decides the role.
        role = {"one": "quick", "parallel": "quick", "approval": "asker", "child_interrupt": "sleeper",
                "parent_interrupt": "sleeper", "nested": "outer" if name == "child1" else "quick",
                "fail": "failer", "steer_parent": "napper", "close_agent": "sleeper",
                "no_wait": "napper", "patch_approval_child": "patcher"}[scenario]
        if role == "failer":
            return {"error": 400}
        if step == 0:
            if role == "quick":
                return {"items": [shell(f"echo SV0-{name}", f"call_{name}_sh")]}
            if role == "sleeper":
                return {"items": [shell("sleep 25", f"call_{name}_sh")]}
            if role == "napper":
                return {"items": [shell("sleep 4", f"call_{name}_sh")]}
            if role == "patcher":
                return {"items": [patch_call(f"call_{name}_patch")]}
            if role == "asker":
                return {"items": [shell("touch /sv0-outside-workspace", f"call_{name}_sh", escalate=True)]}
            if role == "outer":
                return {"items": [fn_call("spawn_agent", {"task_name": "sv0_inner",
                                                          "message": "SV0_CHILD:inner do the fixture step."},
                                          f"call_spawn_inner", "collaboration")]}
        if role == "outer" and step == 1:
            return {"items": [fn_call("wait_agent", {"timeout_ms": 30000}, "call_wait_inner", "collaboration")]}
        return {"items": [message(f"SV0 child {name}: finished.")]}


def _safe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value[:300]
    return value


def _brief(item):
    if item.get("type") == "message":
        return {"text": item["content"][0]["text"]}
    if item.get("type") == "function_call":
        return {"function": item.get("namespace", "") + "." + item["name"], "args": json.loads(item["arguments"])}
    return {"exec": item["input"][:400]}


def _sse(items, n):
    rid = f"resp_sv0_{n}"
    base = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "in_progress",
            "model": MODEL, "output": []}
    events = [("response.created", {"response": base})]
    for idx, item in enumerate(items):
        if item["type"] == "function_call":
            events += [("response.output_item.added", {"output_index": idx, "item": {**item, "arguments": "",
                                                                                      "status": "in_progress"}}),
                       ("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": idx,
                                                                   "delta": item["arguments"]}),
                       ("response.function_call_arguments.done", {"item_id": item["id"], "output_index": idx,
                                                                  "arguments": item["arguments"]})]
        elif item["type"] == "custom_tool_call":
            events += [("response.output_item.added", {"output_index": idx, "item": {**item, "input": "",
                                                                                      "status": "in_progress"}}),
                       ("response.custom_tool_call_input.delta", {"item_id": item["id"], "output_index": idx,
                                                                  "delta": item["input"]}),
                       ("response.custom_tool_call_input.done", {"item_id": item["id"], "output_index": idx,
                                                                 "input": item["input"]})]
        else:
            events += [("response.output_item.added", {"output_index": idx, "item": item})]
        events.append(("response.output_item.done", {"output_index": idx, "item": item}))
    events.append(("response.completed", {"response": {**base, "status": "completed", "output": items,
                                                        "usage": {"input_tokens": 100, "output_tokens": 10,
                                                                  "total_tokens": 110}}}))
    return "".join(f"event: {k}\ndata: {json.dumps({'type': k, 'sequence_number': i, **d})}\n\n"
                   for i, (k, d) in enumerate(events)).encode()


# -- the raw JSON-RPC client ---------------------------------------------------

class Rpc:
    def __init__(self, argv, env, cwd):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=cwd, text=True, bufsize=1)
        self.q: queue.Queue = queue.Queue()
        self.ids = iter(range(1, 10**6))
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()
        self.on_request = None

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                LOG.add("rpc_in", {"unparsed": line[:300]})
                continue
            LOG.add("rpc_in", msg)
            if "method" in msg and "id" in msg and self.on_request:
                reply = self.on_request(msg)
                self.write({"id": msg["id"], "result": reply})
            self.q.put(msg)

    def _err(self):
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip()[:300])
            del self.stderr[:-40]

    def write(self, msg):
        LOG.add("rpc_out", msg)
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=20):
        rid = next(self.ids)
        self.write({"id": rid, "method": method, **({"params": params} if params is not None else {})})
        row = wait_for(lambda r: r["src"] == "rpc_in" and r["data"].get("id") == rid and "method" not in r["data"],
                       timeout)
        if row is None:
            raise TimeoutError(method)
        return row["data"]

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


def wait_for(pred, timeout, start=0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = LOG.find(pred, start)
        if row is not None:
            return row
        time.sleep(0.05)
    return None


def note(r, method, **match):
    if r["src"] != "rpc_in" or r["data"].get("method") != method:
        return False
    p = r["data"].get("params") or {}
    return all(p.get(k) == v for k, v in match.items())


def quiet(seconds, cap):
    end = time.monotonic() + cap
    last, stamp = len(LOG.rows), time.monotonic()
    while time.monotonic() < end:
        time.sleep(0.2)
        if len(LOG.rows) != last:
            last, stamp = len(LOG.rows), time.monotonic()
        elif time.monotonic() - stamp >= seconds:
            return True
    return False


def run(scenario, temp, model, approval_reply):
    global LOG
    LOG = Log()
    model.scenario_ref[0] = scenario
    model.children.clear()
    home = pathlib.Path(temp) / "codex"
    work = pathlib.Path(temp) / f"work-{scenario}"
    work.mkdir()
    if scenario.startswith("patch_approval"):
        (work / "notes.txt").write_text("old\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": temp, "CODEX_HOME": str(home), "XDG_CONFIG_HOME": temp + "/config",
           "XDG_DATA_HOME": temp + "/data", "XDG_CACHE_HOME": temp + "/cache", "OPENAI_API_KEY": "dummy-sv0-offline"}
    rpc = Rpc([str(CODEX), "--strict-config", "app-server", "--stdio"], env, str(work))

    def on_request(msg):
        method = msg.get("method")
        if "Approval" in method or "approval" in method:
            return approval_reply
        return {"decision": "decline"}
    rpc.on_request = on_request
    try:
        rpc.request("initialize", {"clientInfo": {"name": "sv0", "version": "0"},
                                   "capabilities": {"experimentalApi": True}})
        rpc.write({"method": "initialized"})
        started = rpc.request("thread/start", {"cwd": str(work), "model": MODEL, "modelProvider": "fixture",
                                               "approvalPolicy": "on-request", "approvalsReviewer": "user",
                                               "sandbox": "workspace-write", "ephemeral": False})
        thread = started["result"]["thread"]["id"]
        turn = rpc.request("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": f"SV0_PARENT:{scenario} run the fixture scenario."}]})
        turn_id = turn["result"]["turn"]["id"]
        if scenario == "child_interrupt":
            row = wait_for(lambda r: r["src"] == "model" and r["data"].get("role") == "child", 20)
            child_turn = wait_for(lambda r: note(r, "turn/started") and r["data"]["params"]["threadId"] != thread, 20)
            time.sleep(1.5)
            if child_turn:
                p = child_turn["data"]["params"]
                LOG.add("harness", {"action": "turn/interrupt on the child", "child_thread": p["threadId"]})
                rpc.request("turn/interrupt", {"threadId": p["threadId"], "turnId": p["turn"]["id"]})
            else:
                LOG.add("harness", {"action": "no child turn/started seen", "model_row": bool(row)})
        elif scenario == "parent_interrupt":
            wait_for(lambda r: r["src"] == "model" and r["data"].get("role") == "child", 20)
            time.sleep(1.5)
            LOG.add("harness", {"action": "turn/interrupt on the parent"})
            rpc.request("turn/interrupt", {"threadId": thread, "turnId": turn_id})
        elif scenario == "steer_parent":
            wait_for(lambda r: r["src"] == "model" and r["data"].get("role") == "child", 20)
            time.sleep(0.5)
            LOG.add("harness", {"action": "turn/steer on the parent while a child runs"})
            rpc.request("turn/steer", {"threadId": thread, "expectedTurnId": turn_id, "input": [
                {"type": "text", "text": "SV0_STEER change of plan."}]})
        wait_for(lambda r: note(r, "turn/completed", threadId=thread), 60)
        quiet(6, 40)
        if scenario in ("parent_interrupt", "no_wait"):
            # What a child does after the parent's turn: keep listening.
            quiet(8, 40)
        try:
            listed = rpc.request("thread/loaded/list", {}, timeout=5)
            LOG.add("harness", {"action": "thread/loaded/list", "reply": listed.get("result")})
        except Exception as exc:
            LOG.add("harness", {"action": "thread/loaded/list failed", "error": type(exc).__name__})
    finally:
        LOG.add("harness", {"action": "close"})
        rpc.close()
    return LOG.rows, rpc.stderr


def scrub(value, temp):
    raw = json.dumps(value)
    raw = raw.replace(temp, "<TMP>").replace("/home/johnw", "<HOME>")
    raw = raw.replace("johnw", "<user>")
    raw = re.sub(r'("installationId": ")[^"]*', r"\1<redacted>", raw)
    raw = re.sub(r"sv0-codex-[A-Za-z0-9_]+", "sv0-codex-TMP", raw)
    raw = re.sub(r'("x-codex-installation-id": ")[^"]*', r"\1<redacted>", raw)
    raw = re.sub(r'(installation_id\\?": \\?")[^"\\]*', r"\1<redacted>", raw)
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--approval", default="decline")
    parser.add_argument("--enable", nargs="*", default=[], help="Codex features to switch on, e.g. multi_agent_v2")
    opts = parser.parse_args()
    subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True)
    links = json.loads(subprocess.check_output(["/usr/sbin/ip", "-j", "link", "show"]))
    if [x["ifname"] for x in links] != ["lo"] or subprocess.check_output(["/usr/sbin/ip", "route", "show"]).strip():
        raise SystemExit("refusing: not inside an isolated loopback-only network namespace")
    out = pathlib.Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    temp = tempfile.mkdtemp(prefix="sv0-codex-")
    try:
        os.environ.clear()
        os.environ.update({"PATH": "/usr/bin:/bin", "HOME": temp})
        for d in ("codex", "config", "data", "cache"):
            os.makedirs(os.path.join(temp, d), mode=0o700, exist_ok=True)
        model = Model([None])
        cfg = "\n".join([
            f'model = "{MODEL}"', 'model_provider = "fixture"', 'approval_policy = "on-request"',
            'approvals_reviewer = "user"', 'sandbox_mode = "workspace-write"', 'web_search = "disabled"',
            "check_for_update_on_startup = false",
            "[model_providers.fixture]", 'name = "SV0 offline fixture"', f'base_url = "{model.base}"',
            'wire_api = "responses"', 'env_key = "OPENAI_API_KEY"',
            "[sandbox_workspace_write]", "network_access = false",
            *(["[features]"] + [f"{name} = true" for name in opts.enable] if opts.enable else []), ""])
        (pathlib.Path(temp) / "codex" / "config.toml").write_text(cfg)
        version = subprocess.run([str(CODEX), "--version"], env={"PATH": "/usr/bin:/bin", "HOME": temp,
                                                               "CODEX_HOME": temp + "/codex"},
                                 capture_output=True, text=True).stdout.strip()
        scenarios = opts.only or ["one", "parallel", "nested", "fail", "approval", "child_interrupt",
                                  "parent_interrupt", "steer_parent", "close_agent", "no_wait"]
        approval = {"decline": {"decision": "decline"}, "accept": {"decision": "accept"}}[opts.approval]
        summary = {"cli": version, "config": scrub(cfg, temp), "approval_reply": approval, "scenarios": {}}
        for scenario in scenarios:
            begin = time.monotonic()
            try:
                rows, stderr = run(scenario, temp, model, approval)
                status = "ran"
            except Exception as exc:
                rows, stderr, status = LOG.rows, [], f"raised {type(exc).__name__}: {str(exc)[:300]}"
            with (out / f"{scenario}.jsonl").open("w") as f:
                for row in rows:
                    f.write(json.dumps(scrub(row, temp)) + "\n")
            summary["scenarios"][scenario] = {"status": status, "rows": len(rows),
                                              "seconds": round(time.monotonic() - begin, 1),
                                              "stderr_tail": scrub(stderr[-8:], temp)}
            print(json.dumps({scenario: summary["scenarios"][scenario]}), flush=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        (out / "first-bodies.json").write_text(json.dumps(scrub(model.bodies, temp), indent=1))
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
